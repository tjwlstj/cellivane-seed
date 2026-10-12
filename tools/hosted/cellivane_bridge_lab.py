#!/usr/bin/env python3
"""Owned local CPU engine lab. Pinned assets and mutable state use local storage.

This launcher requires free ports before startup and installs no system service.
The preflight does not authenticate a listener's PID across the bind race. The
subengine-smoke profile explicitly uses llamafile as a temporary baseline; it
does not claim Strata inference. The strata-reference profile is fail-closed.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from hashlib import sha256
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from uuid import uuid4
import zipfile

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "hosted" / "experimental"))
from cellivane_bridge.contracts import EngineConfig  # noqa: E402
from cellivane_bridge.runtime import run  # noqa: E402
from cellivane_bridge.storage import encoded, local_root  # noqa: E402

OLLAMA_VERSION = "0.40.2"
OLLAMA_ZIP_SHA = "e29ad1d5063dd4b54b2492d9b00adab2cff9621bfa654b6c92aa8d6f1fdfe7fc"
OLLAMA_ZIP_BYTES = 1468085195
OLLAMA_URL = f"https://github.com/ollama/ollama/releases/download/v{OLLAMA_VERSION}/ollama-windows-amd64.zip"
LLAMAFILE_SHA = "55c69c1be9d6ad2172e2d1c0acc677a60ea8ff60232009a8c8170f5bcb917611"
MODEL_SHA = "9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031"
MODEL_NAME = "Qwen3-0.6B-Q8_0.gguf"
ALIAS = "cellivane-qwen3-0.6b"


def default_data_root():
    """Per-user local storage; callers can always provide an explicit root."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "Cellivane"


def digest(path):
    h = sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())


def pinned_copy(source, target, expected):
    source, target = Path(source), Path(target)
    local_root(target.parent)
    if target.is_symlink():
        raise ValueError("cached-asset-symlink")
    if target.exists():
        if digest(target) != expected:
            raise ValueError("cached-asset-hash-mismatch: " + str(target))
        return target
    if not source.is_file() or digest(source) != expected:
        raise ValueError("source-asset-missing-or-hash-mismatch: " + str(source))
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, target.open("xb") as dst:
        shutil.copyfileobj(src, dst, 4 * 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
    if digest(target) != expected:
        raise ValueError("copied-asset-hash-mismatch")
    return target


def prepare(root, cache):
    if os.name != "nt":
        raise ValueError("launcher-currently-windows-only")
    for directory in ("models", "engines", "downloads", "ollama-models", "owned-profile"):
        local_root(root / directory)
    model = pinned_copy(cache / MODEL_NAME, root / "models" / MODEL_NAME, MODEL_SHA)
    llama = pinned_copy(cache / "llamafile-0.10.5-thin.exe",
                        root / "engines" / "llamafile-0.10.5-thin.exe", LLAMAFILE_SHA)
    archive = root / "downloads" / f"ollama-v{OLLAMA_VERSION}-windows-amd64.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        partial = archive.with_suffix(".zip." + str(uuid4()) + ".part")
        request = urllib.request.Request(OLLAMA_URL, headers={"User-Agent": "Cellivane-local-lab/0.1"})
        count, h = 0, sha256()
        with urllib.request.urlopen(request, timeout=60) as src, partial.open("xb") as dst:
            while chunk := src.read(4 * 1024 * 1024):
                count += len(chunk)
                if count > OLLAMA_ZIP_BYTES:
                    raise ValueError("download-size")
                h.update(chunk)
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        if count != OLLAMA_ZIP_BYTES or h.hexdigest() != OLLAMA_ZIP_SHA:
            raise ValueError("download-digest")
        if archive.exists():
            raise ValueError("concurrent-download")
        partial.rename(archive)
    if archive.stat().st_size != OLLAMA_ZIP_BYTES or digest(archive) != OLLAMA_ZIP_SHA:
        raise ValueError("ollama-archive-digest")
    engine_dir = root / "engines" / f"ollama-{OLLAMA_VERSION}"
    marker = engine_dir / "cellivane-extraction.json"
    if not marker.exists():
        if engine_dir.exists():
            raise ValueError("incomplete-extraction-use-a-fresh-storage-root")
        engine_dir.mkdir(parents=True)
        total = 0
        with zipfile.ZipFile(archive) as package:
            for item in package.infolist():
                name = PurePosixPath(item.filename.replace("\\", "/"))
                total += item.file_size
                if (name.is_absolute() or ".." in name.parts or any(":" in p for p in name.parts)
                        or (item.external_attr >> 16) & 0o170000 == 0o120000
                        or total > 8 * 1024**3):
                    raise ValueError("unsafe-archive-entry")
                target = engine_dir.joinpath(*name.parts)
                if not target.resolve().is_relative_to(engine_dir.resolve()):
                    raise ValueError("unsafe-archive-path")
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(item) as src, target.open("xb") as dst:
                        shutil.copyfileobj(src, dst, 4 * 1024 * 1024)
        save(marker, {"archive_sha256": OLLAMA_ZIP_SHA, "extracted_bytes": total})
    ollama = engine_dir / "ollama.exe"
    if not ollama.is_file():
        raise ValueError("ollama-executable-missing")
    local_root(engine_dir)
    # Validate the loaded DLLs as well as the executable on every reuse. A
    # completion marker is not an integrity identity for the extracted cache.
    expected_files = set()
    with zipfile.ZipFile(archive) as package:
        for item in package.infolist():
            if item.is_dir():
                continue
            parts = PurePosixPath(item.filename.replace("\\", "/")).parts
            target = engine_dir.joinpath(*parts)
            local_root(target.parent)
            expected_files.add(target.relative_to(engine_dir).as_posix())
            if (not target.is_file() or target.is_symlink()
                    or getattr(target.lstat(), "st_file_attributes", 0) & 0x400
                    or target.stat().st_size != item.file_size):
                raise ValueError("cached-ollama-member-missing-or-reparse")
            expected_member = sha256()
            with package.open(item) as original:
                for chunk in iter(lambda: original.read(4 * 1024 * 1024), b""):
                    expected_member.update(chunk)
            if digest(target) != expected_member.hexdigest():
                raise ValueError("cached-ollama-member-hash-mismatch")
    actual_files, pending_dirs = set(), [engine_dir]
    while pending_dirs:
        for item in pending_dirs.pop().iterdir():
            info = item.lstat()
            if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("cached-ollama-tree-reparse")
            if item.is_dir():
                pending_dirs.append(item)
            elif item.is_file():
                actual_files.add(item.relative_to(engine_dir).as_posix())
            else:
                raise ValueError("cached-ollama-tree-type")
    if actual_files != expected_files | {marker.name}:
        raise ValueError("cached-ollama-extra-or-missing-files")
    actual_executable = digest(ollama)
    return {"model": model, "llamafile": llama, "ollama": ollama,
            "model_sha256": MODEL_SHA, "llamafile_sha256": LLAMAFILE_SHA,
            "ollama_sha256": actual_executable, "ollama_archive_sha256": OLLAMA_ZIP_SHA,
            "ollama_executable_matches_pinned_archive": True,
            "ollama_all_extracted_members_verified": len(expected_files)}


def child_env(root, binary_dir):
    # Pass only the named OS locations needed by these owned children.
    env = {k: os.environ[k] for k in ("SystemRoot", "WINDIR", "ComSpec", "TEMP", "TMP",
           "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE") if k in os.environ}
    profile = root / "owned-profile"
    profile.mkdir(parents=True, exist_ok=True)
    env.update(USERPROFILE=str(profile), LOCALAPPDATA=str(profile / "AppData" / "Local"),
        APPDATA=str(profile / "AppData" / "Roaming"),
        PATH=str(binary_dir) + os.pathsep + str(Path(env.get("SystemRoot", "C:/Windows")) / "System32"),
        CUDA_VISIBLE_DEVICES="-1", HIP_VISIBLE_DEVICES="-1", ROCR_VISIBLE_DEVICES="-1",
        OLLAMA_VULKAN="0",
        OLLAMA_HOST="127.0.0.1:18932", OLLAMA_MODELS=str(root / "ollama-models"),
        OLLAMA_NO_CLOUD="1", OLLAMA_NUM_PARALLEL="1", OLLAMA_MAX_LOADED_MODELS="1",
        OLLAMA_CONTEXT_LENGTH="2048", OLLAMA_KEEP_ALIVE="5m")
    return env


def port_free(port):
    with socket.socket() as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", port))


def get_json(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        conn.request("GET", path, headers={"Connection": "close"})
        response = conn.getresponse()
        raw = response.read(256 * 1024 + 1)
        if response.status != 200 or len(raw) > 256 * 1024:
            return None
        return json.loads(raw)
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def ready(process, port, path, seconds=90):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if process.poll() is not None:
            raise RuntimeError("owned-engine-exited: " + str(process.returncode))
        value = get_json(port, path)
        if type(value) is dict:
            return value
        time.sleep(.2)
    raise TimeoutError("owned-engine-readiness")


class _SuspendedProcess:
    """The small process-handle surface used by this Windows-only launcher.

    CreateProcess returns both owned handles. The primary thread cannot execute
    until Job assignment succeeds; only explicit stdio duplicates are inherited.
    """
    def __init__(self, args, env, cwd, log):
        import _winapi
        import msvcrt

        self.api, self.args = _winapi, list(args)
        self._handle, self._thread_handle = None, None
        self.returncode, self.pid = None, None
        self.created_suspended, self.assigned_before_resume = False, False
        self.primary_thread_closed, self.process_handle_closed = False, False
        inherited = []
        try:
            try:
                current = _winapi.GetCurrentProcess()
                with open(os.devnull, "rb") as null:
                    for source in (msvcrt.get_osfhandle(null.fileno()), msvcrt.get_osfhandle(log.fileno())):
                        inherited.append(_winapi.DuplicateHandle(current, source, current, 0, True,
                                                                 _winapi.DUPLICATE_SAME_ACCESS))
                    startup = subprocess.STARTUPINFO()
                    startup.dwFlags = subprocess.STARTF_USESTDHANDLES
                    startup.hStdInput, startup.hStdOutput, startup.hStdError = inherited[0], inherited[1], inherited[1]
                    startup.lpAttributeList = {"handle_list": inherited}
                    flags = 0x00000004 | subprocess.CREATE_NO_WINDOW | 0x00080000
                    hp, ht, self.pid, _tid = _winapi.CreateProcess(os.fspath(self.args[0]),
                        subprocess.list2cmdline(self.args), None, None, True, flags, env, os.fspath(cwd), startup)
                    self._handle, self._thread_handle = hp, ht
                    self.created_suspended = True
            finally:
                try:
                    if inherited:
                        _winapi.CloseHandle(inherited[0])
                finally:
                    if len(inherited) > 1:
                        _winapi.CloseHandle(inherited[1])
        except BaseException:
            # Even a failure releasing stdio duplicates after CreateProcess
            # must not strand an unassigned suspended process or thread handle.
            try:
                if self._handle is not None:
                    _winapi.TerminateProcess(self._handle, 1)
                    self.wait(timeout=10)
            finally:
                self.close_handles()
            raise

    def resume(self, job):
        if not job.assigned or not self.created_suspended or self._thread_handle is None:
            raise RuntimeError("owned-process-resume-before-assignment")
        previous = job.api.ResumeThread(self._thread_handle)
        if previous == 0xffffffff:
            raise job.ctypes.WinError(job.ctypes.get_last_error())
        if previous != 1:
            raise RuntimeError("owned-process-unexpected-suspend-count")
        self.assigned_before_resume = True
        self.api.CloseHandle(self._thread_handle)
        self._thread_handle = None
        self.primary_thread_closed = True

    def poll(self):
        if self.returncode is None and self.api.WaitForSingleObject(self._handle, 0) == self.api.WAIT_OBJECT_0:
            self.returncode = self.api.GetExitCodeProcess(self._handle)
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is not None:
            return self.returncode
        milliseconds = self.api.INFINITE if timeout is None else min(0xfffffffe, max(0, int(timeout * 1000 + .999)))
        result = self.api.WaitForSingleObject(self._handle, milliseconds)
        if result == self.api.WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if result != self.api.WAIT_OBJECT_0:
            raise OSError("owned-process-wait-failed")
        self.returncode = self.api.GetExitCodeProcess(self._handle)
        return self.returncode

    def terminate(self):
        if self.poll() is None:
            try:
                self.api.TerminateProcess(self._handle, 1)
            except PermissionError:
                if self.poll() is None:
                    raise

    kill = terminate

    def close_handles(self):
        try:
            if self._thread_handle is not None:
                self.api.CloseHandle(self._thread_handle)
                self._thread_handle = None
                self.primary_thread_closed = True
        finally:
            if self._handle is not None:
                self.api.CloseHandle(self._handle)
                self._handle = None
                self.process_handle_closed = True


class _OwnedJob:
    """Unnamed, non-inheritable job containing our suspended new process.

    Normal CreateProcess descendants join automatically. No breakaway flag,
    named job adoption, OpenProcess, PID enumeration, or process-tree killing.
    Assignment happens before any code in the primary thread executes.
    """
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ctypes, self.wintypes = ctypes, wintypes
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        handle, dword, pointer = wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p
        signatures = {
            "CreateJobObjectW": ([pointer, wintypes.LPCWSTR], handle),
            "SetInformationJobObject": ([handle, ctypes.c_int, pointer, dword], wintypes.BOOL),
            "AssignProcessToJobObject": ([handle, handle], wintypes.BOOL),
            "TerminateJobObject": ([handle, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": ([handle, ctypes.c_int, pointer, dword, pointer], wintypes.BOOL),
            "CloseHandle": ([handle], wintypes.BOOL),
            "ResumeThread": ([handle], dword),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", dword),
                ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", dword), ("Affinity", ctypes.c_size_t),
                ("PriorityClass", dword), ("SchedulingClass", dword)]
        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in
                ("TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")]
            _fields_ += [(name, dword) for name in
                ("TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]
        self.Accounting = Accounting
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.assigned = False
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.api.CloseHandle(self.handle)
            self.handle = None
            raise

    def assign(self, process):
        # Keep CreateProcess's existing process handle; never reopen a PID.
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        self.assigned = True

    def active(self):
        accounting = self.Accounting()
        if not self.api.QueryInformationJobObject(self.handle, 1, self.ctypes.byref(accounting),
                                                  self.ctypes.sizeof(accounting), None):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return accounting.ActiveProcesses

    def close(self, timeout=10):
        receipt = {"job_assigned": self.assigned, "job_terminated": False,
            "job_active_processes": None, "job_closed": False, "job_cleanup_confirmed": False,
            "job_error": None}
        errors = []
        try:
            if not self.api.TerminateJobObject(self.handle, 1):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            receipt["job_terminated"] = True
            until = time.monotonic() + timeout
            while True:
                receipt["job_active_processes"] = self.active()
                if receipt["job_active_processes"] == 0:
                    break
                if time.monotonic() >= until:
                    raise TimeoutError("owned-job-processes-still-active")
                time.sleep(.01)
        except (OSError, TimeoutError) as exc:
            errors.append(type(exc).__name__ + ":" + str(exc)[:256])
        finally:
            if self.api.CloseHandle(self.handle):
                receipt["job_closed"] = True
                self.handle = None
            else:
                errors.append("CloseHandle:" + str(self.ctypes.get_last_error()))
        receipt["job_error"] = ";".join(errors) or None
        receipt["job_cleanup_confirmed"] = (receipt["job_terminated"]
            and receipt["job_active_processes"] == 0 and receipt["job_closed"] and not errors)
        return receipt


class OwnedProcess:
    def __init__(self, stack, args, env, log_dir, name):
        self.name, self.args, self.cleanup = name, args, None
        log = stack.enter_context((log_dir / (name + ".log")).open("xb"))
        self.process = None
        self._job = _OwnedJob() if os.name == "nt" else None
        stack.callback(self.close)
        try:
            if self._job is not None:
                self.process = _SuspendedProcess(args, env, log_dir, log)
                self._job.assign(self.process)
                self.process.resume(self._job)
            else:
                self.process = subprocess.Popen(args, cwd=log_dir, env=env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT)
        except BaseException:
            # Assignment failure must never return an uncontained running engine.
            try:
                self.close()
            except Exception:
                pass
            raise

    def close(self):
        if self.cleanup is not None:
            return self.cleanup
        process = self.process
        method = "already-exited"
        job_receipt = self._job.close() if self._job is not None else {}
        if job_receipt.get("job_assigned"):
            method = "owned-job-terminate-and-close"
        process_error = None
        try:
            if process is not None and process.poll() is None:
                # Terminate only the process handle created by this launcher.
                if not job_receipt.get("job_terminated") or not job_receipt.get("job_assigned"):
                    try:
                        process.terminate()
                    except PermissionError:
                        # Job termination can precede signaling this process
                        # handle. Prove exit by waiting; never assume it exited.
                        process.wait(timeout=10)
                    method = "owned-process-terminate"
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                    method = "owned-process-kill"
        except (OSError, subprocess.SubprocessError) as exc:
            process_error = type(exc).__name__ + ":" + str(exc)[:256]
        process_exited = process is None or process.poll() is not None
        startup_receipt = {}
        if isinstance(process, _SuspendedProcess):
            try:
                process.close_handles()
            except OSError as exc:
                process_error = "owned-handle-close:" + str(exc)[:256]
            startup_receipt = {key: getattr(process, key) for key in
                ("created_suspended", "assigned_before_resume", "primary_thread_closed", "process_handle_closed")}
        self.cleanup = {"name": self.name, "pid": process.pid if process is not None else None, "method": method,
                        "returncode": process.returncode if process is not None else None,
                        "process_exited": process_exited, "process_error": process_error,
                        "backend_stop_api_confirmed": False, "kv_release_confirmed": False,
                        **job_receipt, **startup_receipt}
        if process_error or not process_exited or (self._job is not None and not job_receipt["job_cleanup_confirmed"]):
            raise RuntimeError("owned-job-cleanup-unconfirmed:" + str(process_error or job_receipt.get("job_error")))
        return self.cleanup


CASES = [("arithmetic", "Reply with only the number: 7 + 8 =", "15"),
         ("control", "Reply with exactly this text and nothing else: CELLIVANE_OK", "CELLIVANE_OK"),
         ("korean", "대한민국의 수도를 한 단어로만 답해 줘.", "서울")]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-root", default=str(default_data_root() / "bridge-lab"))
    parser.add_argument("--asset-cache", default=str(default_data_root() / "asset-cache"))
    parser.add_argument("--profile", choices=("subengine-smoke", "strata-reference"), required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--prompt", default="Reply with only the number: 7 + 8 =")
    parser.add_argument("--parallel-smoke", action="store_true", help="Add a second owned llamafile instance; no engine-diversity claim for that duplicate")
    args = parser.parse_args(argv)
    root = local_root(args.storage_root)
    evidence = local_root(root / "lab-runs") / str(uuid4())
    evidence.mkdir(parents=True, exist_ok=False)
    base = {"schema_version": 1, "profile": args.profile, "scope": "isolated-local-cpu-lab",
            "strata_actual_inference": "NOT_RUN", "canonical_main_binding": False,
            "evidence_directory": str(evidence), "model_training": False, "token_mtp": False,
            "source_launcher_sha256": digest(__file__),
            "source_runtime_sha256": {p.name: digest(p) for p in sorted((REPO / "hosted" / "experimental" / "cellivane_bridge").glob("*.py"))}}
    if args.profile == "strata-reference":
        # No weights downloaded, mock or alternative server cannot qualify this route.
        engines = [EngineConfig("strata-reference", "strata", "http://127.0.0.1:18933/v1",
                    "Qwen3.8-Flash-Next", "reference", 5, 64),
                   EngineConfig("ollama-subengine", "ollama", "http://127.0.0.1:18932", ALIAS + ":latest")]
        config = {"schema_version": 1, "storage_root": str(root), "parallelism": 2,
                  "deadline_s": 120, "engines": [e.record() for e in engines]}
        save(evidence / "config.json", config)
        report = run(config, engines, args.prompt)
        save(evidence / "report.json", report)
        save(evidence / "lab-summary.json", {**base, "outcome": "INCOMPLETE", "reports": [report],
            "error": "strata-supported-model-and-owned-runtime-qualification-required", "owned_processes": []})
        print(json.dumps({"outcome": report["outcome"], "error": report["error"], "dispatch_count": report["dispatch_count"],
            "evidence_directory": str(evidence)}, ensure_ascii=False))
        return 1
    assets = prepare(root, Path(args.asset_cache))
    save(evidence / "asset-provenance.json", {k: str(v) if isinstance(v, Path) else v for k, v in assets.items()})
    if args.prepare_only:
        print(json.dumps({"outcome": "PREPARED", "evidence_directory": str(evidence)}))
        return 0
    port_free(18931)
    port_free(18932)
    if args.parallel_smoke:
        port_free(18934)
    owned, reports, errors = [], [], []
    try:
        with ExitStack() as stack:
            llama_env = child_env(root, assets["llamafile"].parent)
            llama_args = [str(assets["llamafile"]), "--server", "--model", str(assets["model"]),
                "--host", "127.0.0.1", "--port", "18931", "--ctx-size", "2048", "--n-gpu-layers", "0",
                "--threads", "4", "--parallel", "1", "--alias", ALIAS, "--reasoning", "off", "--no-webui"]
            llama = OwnedProcess(stack, llama_args, llama_env, evidence, "llamafile")
            owned.append(llama)
            ready(llama.process, 18931, "/health")
            ollama_env = child_env(root, assets["ollama"].parent)
            ollama = OwnedProcess(stack, [str(assets["ollama"]), "serve"], ollama_env, evidence, "ollama")
            owned.append(ollama)
            ready(ollama.process, 18932, "/api/version")
            save(evidence / "owned-launch.json", {"processes": [{"name": p.name, "pid": p.process.pid, "args": p.args} for p in owned],
                 "gpu_mode": "cpu-only-requested", "ollama_model_storage": str(root / "ollama-models")})
            # Reapply our isolated alias parameters: a previous cache entry may
            # have different GPU placement defaults. Blob data is reused.
            modelfile = evidence / "Modelfile"
            modelfile.write_text('FROM "' + assets["model"].as_posix() + '"\nPARAMETER num_ctx 2048\nPARAMETER temperature 0\nPARAMETER num_gpu 0\nPARAMETER num_thread 4\n', encoding="utf-8")
            with (evidence / "ollama-import.log").open("xb") as log:
                imported = subprocess.run([str(assets["ollama"]), "create", ALIAS, "-f", str(modelfile)],
                    cwd=evidence, env=ollama_env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if imported.returncode:
                raise RuntimeError("ollama-model-import-failed")
            engines = [EngineConfig("llamafile-baseline", "openai", "http://127.0.0.1:18931/v1", ALIAS, "reference", 60, 128),
                       EngineConfig("ollama-subengine", "ollama", "http://127.0.0.1:18932", ALIAS + ":latest", "candidate", 60, 128)]
            if args.parallel_smoke:
                alternate = llama_args.copy()
                alternate[alternate.index("18931")] = "18934"
                second = OwnedProcess(stack, alternate, llama_env, evidence, "llamafile-reviewer")
                owned.append(second)
                ready(second.process, 18934, "/health")
                engines.append(EngineConfig("llamafile-reviewer", "openai", "http://127.0.0.1:18934/v1", ALIAS, "reviewer", 60, 128))
            config = {"schema_version": 1, "storage_root": str(root), "parallelism": 2,
                      "deadline_s": 180, "engines": [e.record() for e in engines]}
            save(evidence / "config.json", config)
            cases = CASES if args.benchmark else [("custom", args.prompt, None)]
            for label, prompt, expected in cases:
                report = run(config, engines, prompt)
                save(evidence / (label + "-report.json"), report)
                scores = {name: {"expected": expected, "exact_match": result.get("text", "").strip() == expected
                                  if expected is not None and type(result.get("text")) is str else None}
                          for name, result in report["results"].items()}
                reports.append({"label": label, "report": report, "quality": scores})
                print(json.dumps({"case": label, "outcome": report["outcome"], "answer": report["answer"],
                    "subengine_answers": {n: r["text"] for n, r in report["results"].items()},
                    "run_directory": report["run_directory"]}, ensure_ascii=False), flush=True)
                if report["outcome"] != "COMPLETE":
                    errors.append(label + ":" + str(report["error"]))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        errors.append(type(exc).__name__ + ":" + str(exc)[:256])
    finally:
        summary = {**base, "outcome": "PASS" if reports and not errors else "FAIL", "reports": reports,
                   "errors": errors, "owned_processes": [p.cleanup for p in owned],
                   "engine_families": ["llamafile", "ollama"],
                   "parallel_duplicate_instance": args.parallel_smoke,
                   "same_model_weights": True, "quality_improvement_claimed": False}
        save(evidence / "lab-summary.json", summary)
    print(json.dumps({"outcome": summary["outcome"], "errors": errors, "evidence_directory": str(evidence)}, ensure_ascii=False), flush=True)
    return 0 if summary["outcome"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
