"""Owned Python-only Windows Job fixtures; no engine, download, or PID kill."""
from __future__ import annotations

from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("cellivane_owned_lab", ROOT / "tools/hosted/cellivane_bridge_lab.py")
lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab)

PARENT = """
from pathlib import Path
import subprocess, sys, time
root = Path(sys.argv[1])
(root / 'executed').write_text('running', encoding='ascii')
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
(root / 'child-pid').write_text(str(child.pid), encoding='ascii')
time.sleep(60)
"""


def until(condition, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(.01)
    return bool(condition())


@unittest.skipUnless(os.name == "nt", "Windows Job Object contract")
class OwnedLifecycleTests(unittest.TestCase):
    def python(self, code="import time; time.sleep(60)", *args):
        return [sys.executable, "-c", code, *map(str, args)]

    def test_job_api_has_pointer_sized_handles_and_empty_cleanup(self):
        job = lab._OwnedJob()
        try:
            self.assertIs(job.api.CreateJobObjectW.restype, wintypes.HANDLE)
            self.assertEqual(job.api.AssignProcessToJobObject.argtypes, [wintypes.HANDLE, wintypes.HANDLE])
            self.assertEqual(job.active(), 0)
        finally:
            receipt = job.close()
        self.assertTrue(receipt["job_closed"])
        self.assertTrue(receipt["job_cleanup_confirmed"])
        self.assertEqual(receipt["job_active_processes"], 0)

    def test_parent_exit_does_not_skip_inherited_child_cleanup_or_kill_canary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canary = subprocess.Popen(self.python(), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                with ExitStack() as stack:
                    owned = lab.OwnedProcess(stack, self.python(PARENT, root), os.environ.copy(), root, "owned")
                    self.assertTrue(owned._job.assigned)
                    self.assertTrue(until(lambda: (root / "child-pid").exists()))
                    # Interpreter redirectors/host children can add members.
                    self.assertTrue(until(lambda: owned._job.active() >= 2))
                    owned.process.terminate()  # Only the process handle returned by our Popen.
                    owned.process.wait(timeout=3)
                    self.assertTrue(until(lambda: owned._job.active() >= 1))
                self.assertEqual(owned.cleanup["method"], "owned-job-terminate-and-close")
                self.assertTrue(owned.cleanup["process_exited"])
                self.assertEqual(owned.cleanup["job_active_processes"], 0)
                self.assertTrue(owned.cleanup["job_cleanup_confirmed"])
                self.assertFalse(owned.cleanup["backend_stop_api_confirmed"])
                self.assertFalse(owned.cleanup["kv_release_confirmed"])
                for key in ("created_suspended", "assigned_before_resume", "primary_thread_closed", "process_handle_closed"):
                    self.assertTrue(owned.cleanup[key])
                self.assertIsNone(owned.cleanup["process_error"])
                self.assertIs(owned.close(), owned.cleanup)  # Repeated stack/explicit close is harmless.
                self.assertIsNone(canary.poll())
            finally:
                canary.terminate()
                canary.wait(timeout=3)

    def test_last_job_handle_close_kills_own_parent_and_grandchild(self):
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        # Read/wait access only to the exact child PID reported by our own fixture.
        # This handle is never used for termination; no process enumeration.
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as directory, open(os.devnull, "wb") as log:
            root, job, process, child_handle = Path(directory), lab._OwnedJob(), None, None
            try:
                process = lab._SuspendedProcess(self.python(PARENT, root), os.environ.copy(), root, log)
                job.assign(process)
                process.resume(job)
                self.assertTrue(until(lambda: (root / "child-pid").exists()))
                self.assertTrue(until(lambda: job.active() >= 2))
                child_pid = int((root / "child-pid").read_text(encoding="ascii"))
                child_handle = api.OpenProcess(0x00100000, False, child_pid)  # SYNCHRONIZE only.
                self.assertTrue(child_handle)
                self.assertTrue(job.api.CloseHandle(job.handle))  # KILL_ON_JOB_CLOSE, not TerminateJobObject.
                job.handle = None
                process.wait(timeout=3)
                self.assertEqual(api.WaitForSingleObject(child_handle, 3000), 0)  # WAIT_OBJECT_0.
            finally:
                if job.handle is not None:
                    job.close()
                if process is not None and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=3)
                if process is not None:
                    process.close_handles()
                if child_handle:
                    api.CloseHandle(child_handle)

    def test_assignment_failure_terminates_only_the_new_popen_and_raises(self):
        class RejectJob:
            def assign(self, _process):
                raise OSError("fixture-assignment-rejected")
            def close(self):
                return {"job_assigned": False, "job_terminated": True, "job_active_processes": 0,
                    "job_closed": True, "job_cleanup_confirmed": True, "job_error": None}
        created = []
        class CapturedProcess(lab._SuspendedProcess):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created.append(self)
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            with mock.patch.object(lab, "_OwnedJob", return_value=RejectJob()), \
                    mock.patch.object(lab, "_SuspendedProcess", CapturedProcess):
                with self.assertRaisesRegex(OSError, "fixture-assignment-rejected"):
                    lab.OwnedProcess(stack, self.python(), os.environ.copy(), Path(directory), "rejected")
            self.assertEqual(len(created), 1)
            self.assertIsNotNone(created[0].poll())
            self.assertFalse(created[0].assigned_before_resume)
            self.assertTrue(created[0].primary_thread_closed)
            self.assertTrue(created[0].process_handle_closed)

    def test_failed_job_cleanup_cannot_claim_confirmation(self):
        class FailedJob(lab._OwnedJob):
            def close(self):
                receipt = super().close()
                receipt.update(job_terminated=False, job_active_processes=1,
                               job_cleanup_confirmed=False, job_error="fixture-denied")
                return receipt
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            with mock.patch.object(lab, "_OwnedJob", FailedJob):
                owned = lab.OwnedProcess(stack, self.python(), os.environ.copy(), Path(directory), "failed")
            with self.assertRaisesRegex(RuntimeError, "owned-job-cleanup-unconfirmed"):
                owned.close()
            self.assertTrue(owned.cleanup["process_exited"])
            self.assertFalse(owned.cleanup["job_cleanup_confirmed"])
            self.assertEqual(owned.cleanup["job_error"], "fixture-denied")
            self.assertFalse(owned.cleanup["backend_stop_api_confirmed"])
            self.assertFalse(owned.cleanup["kv_release_confirmed"])

    def test_spawn_failure_closes_the_already_created_job(self):
        import _winapi
        job = lab._OwnedJob()
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            with mock.patch.object(lab, "_OwnedJob", return_value=job), \
                    mock.patch.object(_winapi, "CreateProcess", side_effect=OSError("fixture-spawn-failed")):
                with self.assertRaisesRegex(OSError, "fixture-spawn-failed"):
                    lab.OwnedProcess(stack, self.python(), os.environ.copy(), Path(directory), "no-spawn")
            self.assertIsNone(job.handle)

    def test_primary_thread_cannot_execute_before_assignment_and_explicit_resume(self):
        with tempfile.TemporaryDirectory() as directory, open(os.devnull, "wb") as log:
            root, job, process = Path(directory), lab._OwnedJob(), None
            try:
                process = lab._SuspendedProcess(self.python(PARENT, root), os.environ.copy(), root, log)
                time.sleep(.15)
                self.assertFalse((root / "executed").exists())
                self.assertFalse((root / "child-pid").exists())
                self.assertEqual(job.active(), 0)
                job.assign(process)
                time.sleep(.15)
                self.assertFalse((root / "executed").exists())
                process.resume(job)
                self.assertTrue(until(lambda: (root / "child-pid").exists()))
                self.assertTrue(process.assigned_before_resume)
                self.assertTrue(process.primary_thread_closed)
                self.assertTrue(until(lambda: job.active() >= 2))
            finally:
                receipt = job.close()
                if process is not None:
                    if not job.assigned and process.poll() is None:
                        process.terminate()
                    process.wait(timeout=3)
                    process.close_handles()
            self.assertTrue(receipt["job_cleanup_confirmed"])

    def test_resume_failure_never_executes_and_closes_both_owned_handles(self):
        job, created = lab._OwnedJob(), []
        class CapturedProcess(lab._SuspendedProcess):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created.append(self)
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            with mock.patch.object(lab, "_OwnedJob", return_value=job), \
                    mock.patch.object(lab, "_SuspendedProcess", CapturedProcess), \
                    mock.patch.object(job.api, "ResumeThread", return_value=0xffffffff):
                with self.assertRaises(OSError):
                    lab.OwnedProcess(stack, self.python(PARENT, root), os.environ.copy(), root, "no-resume")
            self.assertFalse((root / "executed").exists())
            self.assertFalse((root / "child-pid").exists())
            self.assertEqual(len(created), 1)
            self.assertIsNotNone(created[0].poll())
            self.assertFalse(created[0].assigned_before_resume)
            self.assertTrue(created[0].primary_thread_closed)
            self.assertTrue(created[0].process_handle_closed)
            self.assertIsNone(job.handle)

    def test_stdio_release_failure_after_create_does_not_strand_suspended_handles(self):
        import _winapi
        real_create, real_close = _winapi.CreateProcess, _winapi.CloseHandle
        handles, closed, injected = [], [], []
        def create(*args):
            self.assertTrue(args[5] & 0x00000004)  # CREATE_SUSPENDED.
            self.assertTrue(args[4])
            self.assertEqual(len(args[8].lpAttributeList['handle_list']), 2)
            result = real_create(*args)
            handles.extend(result[:2])
            return result
        def close(handle):
            real_close(handle)
            closed.append(handle)
            if handles and handle not in handles and not injected:
                injected.append(True)
                raise OSError("fixture-stdio-release-failed")
        with tempfile.TemporaryDirectory() as directory, open(os.devnull, 'wb') as log:
            root = Path(directory)
            with mock.patch.object(_winapi, 'CreateProcess', side_effect=create), \
                    mock.patch.object(_winapi, 'CloseHandle', side_effect=close):
                with self.assertRaisesRegex(OSError, "fixture-stdio-release-failed"):
                    lab._SuspendedProcess(self.python(PARENT, root), os.environ.copy(), root, log)
            self.assertFalse((root / 'executed').exists())
            self.assertEqual(len(handles), 2)
            self.assertTrue(all(handle in closed for handle in handles))
            for handle in handles:
                with self.assertRaises(OSError):
                    _winapi.WaitForSingleObject(handle, 0)


if __name__ == "__main__":
    unittest.main()
