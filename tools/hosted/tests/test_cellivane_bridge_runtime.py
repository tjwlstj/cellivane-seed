"""Application lifecycle negatives; fixtures are not real-model evidence."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "hosted" / "experimental"))
from cellivane_bridge.contracts import EngineConfig, EngineResult
from cellivane_bridge.runtime import load_config, run
from cellivane_bridge.storage import Journal, local_root, read_json


def qualification(engine, confirmed=True):
    return {"schema_version": 1, "name": engine.name, "kind": engine.kind, "model": engine.model,
            "base_url": engine.base_url, "outcome": "CONFIRMED" if confirmed else "UNKNOWN",
            "model_present": confirmed, "server_version": None, "model_digest": None,
            "responses": [], "error": None if confirmed else "fixture-unavailable", "elapsed_s": 0.0}


def answer(engine, messages, cancel):
    return EngineResult(engine.name, engine.kind, engine.model, "ANSWERED", engine.name,
                        0.0, "0" * 64, None, None, None, None)


class BridgeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {"schema_version": 1, "storage_root": self.temp.name,
                       "parallelism": 2, "deadline_s": 10}
        self.engines = [EngineConfig("reference", "openai", "http://127.0.0.1:19001/v1", "tiny", "reference"),
                        EngineConfig("candidate", "ollama", "http://127.0.0.1:19002", "tiny")]

    def execute(self, **kwargs):
        return run(self.config, self.engines, "fixture only", probe_fn=qualification,
                   request_fn=answer, **kwargs)

    def test_reference_is_only_authoritative_output(self):
        report = self.execute()
        self.assertEqual(report["outcome"], "COMPLETE")
        self.assertEqual(report["answer"], "reference")
        self.assertEqual(report["results"]["candidate"]["text"], "candidate")
        self.assertEqual(report["dispatch_count"], 2)
        self.assertFalse(report["canonical_main_binding"])

    def test_reference_probe_failure_fences_all_generation(self):
        called = []
        report = run(self.config, self.engines, "fixture", probe_fn=lambda e: qualification(e, False),
                     request_fn=lambda *args: called.append(args))
        self.assertEqual(report["error"], "reference-not-qualified")
        self.assertEqual(report["dispatch_count"], 0)
        self.assertFalse(called)

    def test_strata_cannot_be_qualified_by_mock_http_server(self):
        self.engines[0] = replace(self.engines[0], kind="strata")
        called = []
        report = run(self.config, self.engines, "fixture", probe_fn=lambda e: called.append(e), request_fn=answer)
        self.assertFalse(called)
        self.assertEqual(report["dispatch_count"], 0)
        self.assertEqual(report["qualifications"]["reference"]["error"], "strata-runtime-qualification-not-implemented")

    def test_unknown_reference_never_falls_back(self):
        called = []
        def uncertain(engine, messages, cancel):
            called.append(engine.name)
            return replace(answer(engine, messages, cancel), outcome="UNKNOWN", text=None, error="timeout")
        report = run(self.config, self.engines, "fixture", probe_fn=qualification, request_fn=uncertain)
        self.assertEqual(called, ["reference"])
        self.assertIsNone(report["answer"])
        self.assertEqual(report["error"], "reference-result-uncertain")

    def test_missing_candidate_does_not_replace_reference(self):
        report = run(self.config, self.engines, "fixture", request_fn=answer,
                     probe_fn=lambda e: qualification(e, e.role == "reference"))
        self.assertEqual(report["outcome"], "INCOMPLETE")
        self.assertEqual(report["answer"], "reference")
        self.assertEqual(report["dispatch_count"], 1)

    def test_valid_late_reference_is_preserved_but_not_output(self):
        cancel = threading.Event()
        def late(engine, messages, event):
            event.set()
            return answer(engine, messages, event)
        report = run(self.config, self.engines, "fixture", cancel=cancel, probe_fn=qualification, request_fn=late)
        self.assertEqual(report["results"]["reference"]["outcome"], "ANSWERED")
        self.assertEqual(report["outcome"], "CANCELLED")
        self.assertIsNone(report["answer"])
        self.assertFalse(report["output_allowed"])
        self.assertEqual(report["dispatch_count"], 1)

    def test_pre_cancel_records_acceptance_without_probe_or_dispatch(self):
        cancel = threading.Event()
        cancel.set()
        report = self.execute(cancel=cancel)
        self.assertEqual(report["outcome"], "CANCELLED")
        self.assertEqual(report["dispatch_count"], 0)
        self.assertEqual(len(list(Path(report["run_directory"]).glob("*.json"))), 2)

    def test_interrupt_collecting_children_preserves_late_results(self):
        self.engines.append(EngineConfig("reviewer", "openai", "http://127.0.0.1:19003/v1", "tiny", "reviewer"))
        def late_child(engine, messages, cancel):
            if engine.role != "reference":
                self.assertTrue(cancel.wait(timeout=2))
            return answer(engine, messages, cancel)
        with patch("cellivane_bridge.runtime.wait", side_effect=KeyboardInterrupt):
            report = run(self.config, self.engines, "fixture", probe_fn=qualification, request_fn=late_child)
        self.assertEqual(len(report["results"]), 3)
        self.assertTrue(all(row["outcome"] == "ANSWERED" for row in report["results"].values()))
        self.assertEqual(report["outcome"], "CANCELLED")
        self.assertIsNone(report["answer"])

    def test_interrupt_reference_keeps_journal_incomplete(self):
        def interrupted(*args):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            run(self.config, self.engines, "fixture", probe_fn=qualification, request_fn=interrupted)
        records = list((Path(self.temp.name) / "runs").glob("*/*-final.json"))
        self.assertFalse(records)

    def test_remaining_deadline_is_given_to_transport(self):
        self.config["deadline_s"] = 1
        seen = []
        def inspect(engine):
            seen.append(engine.timeout_s)
            return qualification(engine)
        report = run(self.config, self.engines, "fixture", probe_fn=inspect, request_fn=answer)
        self.assertTrue(all(0.1 <= value <= 1 for value in seen))
        self.assertEqual(report["outcome"], "COMPLETE")

    def test_deadline_blocks_child_submission_and_reference_output(self):
        self.config["deadline_s"] = 1
        def slow(engine, messages, cancel):
            time.sleep(1.02)
            return answer(engine, messages, cancel)
        report = run(self.config, self.engines, "fixture", probe_fn=qualification, request_fn=slow)
        self.assertEqual(report["dispatch_count"], 1)
        self.assertTrue(report["deadline_exceeded"])
        self.assertIsNone(report["answer"])

    def test_two_subengines_overlap_after_reference_completion(self):
        self.engines.append(EngineConfig("reviewer", "openai", "http://127.0.0.1:19003/v1", "tiny", "reviewer"))
        gate = threading.Barrier(2)
        reference_complete = threading.Event()
        def concurrent(engine, messages, cancel):
            if engine.role == "reference":
                reference_complete.set()
            else:
                self.assertTrue(reference_complete.is_set())
                gate.wait(timeout=2)
            return answer(engine, messages, cancel)
        report = run(self.config, self.engines, "fixture", probe_fn=qualification, request_fn=concurrent)
        self.assertEqual(report["outcome"], "COMPLETE")
        self.assertEqual(report["dispatch_count"], 3)

    def test_reference_result_binding_is_checked(self):
        with self.assertRaisesRegex(ValueError, "result-binding"):
            run(self.config, self.engines, "fixture", probe_fn=qualification,
                request_fn=lambda e, m, c: replace(answer(e, m, c), name="different"))

    def test_accept_write_failure_prevents_all_network(self):
        calls = []
        with patch.object(Journal, "append", side_effect=OSError("fixture-disk-full")):
            with self.assertRaises(OSError):
                run(self.config, self.engines, "fixture", probe_fn=lambda e: calls.append(e), request_fn=answer)
        self.assertFalse(calls)

    def test_dispatch_write_failure_prevents_generation(self):
        original = Journal.append
        calls = []
        def disk_full(journal, kind, payload):
            if kind == "dispatch":
                raise OSError("fixture-disk-full")
            return original(journal, kind, payload)
        with patch.object(Journal, "append", disk_full):
            with self.assertRaises(OSError):
                run(self.config, self.engines, "fixture", probe_fn=qualification, request_fn=lambda *a: calls.append(a))
        self.assertFalse(calls)

    def test_same_host_port_aliases_are_rejected(self):
        self.engines[1] = replace(self.engines[1], kind="openai", base_url="http://127.0.0.1:19001")
        with self.assertRaisesRegex(ValueError, "endpoint-alias"):
            self.execute()

    def test_mutated_frozen_config_is_revalidated_before_acceptance(self):
        self.engines[0].__dict__["base_url"] = "http://example.com:80"
        with self.assertRaises(ValueError):
            self.execute()
        self.assertFalse((Path(self.temp.name) / "runs").exists())

    def test_journal_poison_and_single_writer(self):
        journal = Journal(self.temp.name)
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.assertRaisesRegex(RuntimeError, "writer-unavailable"):
                executor.submit(journal.append, "accepted", {}).result()
        with patch("cellivane_bridge.storage.os.replace", side_effect=OSError("fixture-disk-full")):
            with self.assertRaises(OSError):
                journal.append("accepted", {})
        with self.assertRaisesRegex(RuntimeError, "writer-unavailable"):
            journal.append("final", {})

    def test_json_duplicates_nonfinite_and_network_storage_rejected(self):
        target = Path(self.temp.name) / "bad.json"
        for text in ('{"a":1,"a":2}', '{"a":NaN}'):
            target.write_text(text)
            with self.assertRaises(ValueError):
                read_json(target)
        with self.assertRaises(ValueError):
            local_root("\\\\server\\share\\bridge")

    def test_load_config_requires_unique_physical_endpoint(self):
        config = {**self.config, "engines": [e.record() for e in self.engines]}
        target = Path(self.temp.name) / "config.json"
        target.write_text(json.dumps(config))
        loaded, engines = load_config(target)
        self.assertEqual(len(engines), 2)
        config["engines"][1].update(kind="openai", base_url="http://127.0.0.1:19001")
        target.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, "endpoint-alias"):
            load_config(target)


if __name__ == "__main__":
    unittest.main()
