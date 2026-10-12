"""Synthetic comparison checks; no engines, subprocesses, HTTP or D: model inputs.

Operational acceptance is distinct from corpus accuracy. These fixtures do not
qualify tensor execution, placement, process cleanup or an actual model artifact.
"""
from __future__ import annotations

import ast
import copy
from hashlib import sha256
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/hosted"))
import cellivane_seed_compare as compare
from cellivane_seed.placement import artifact_identity, build_plan


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def synthetic_summary(profile="cpu", repeat=2):
    rows = [{"case": case["id"], "repeat": iteration, "wall_s": .2, "generation_tokens_s": 10.0,
        "exact": False, "text": "synthetic wrong answer", "backend_exchange_count": 1,
        "backend_receipt": {"outcome": "ANSWERED", "http_status": 200, "error": None},
        "response": {"status": 200, "body": {"cellivane": {"execution": "computed"}}}}
        for iteration in range(repeat) for case in compare.CASES]
    count = len(rows)
    observed = {"status": "CPU_OBSERVED", "offloaded_layers": None, "total_layers": None,
                "gpu_offload_observed": False, "gpu_qualified": False, "multi_gpu_qualified": False}
    if profile == "hybrid":
        observed.update(status="GPU_OFFLOAD_OBSERVED", offloaded_layers=14,
                        total_layers=37, gpu_offload_observed=True)
    return {"synthetic_fixture": True, "scope": "comparison-unit-fixture-only", "repeat": repeat,
        "plan": build_plan(profile).record(), "warmup": {"status": 200}, "rows": rows,
        "state": {"backend_calls": count + 2, "computed": count + 2, "closed": True, "poisoned": False,
            "active": 0, "inflight": 0, "waiters": 0, "cache_hits": 0, "coalesced": 0, "failures": 0, "timeouts": 0},
        "bridge_report": {"outcome": "COMPLETE", "dispatch_count": 2, "output_allowed": True,
            "results": {"seed-reference": {"outcome": "ANSWERED"}, "ollama-candidate": {"outcome": "ANSWERED"}}},
        "placement_observed": observed}


class ComparisonAcceptanceTests(unittest.TestCase):
    def test_wrong_quality_can_pass_operational_checks_without_accuracy_claim(self):
        summary = synthetic_summary()
        self.assertEqual(compare.acceptance_checks(summary), [])
        result = compare.metrics(summary["rows"])
        self.assertEqual((result["answered"], result["exact"], result["total"]), (24, 0, 24))

    def test_repeat_count_and_all_correct_quality_do_not_change_lifetime_contract(self):
        for repeat in (1, 2, 3):
            summary = synthetic_summary(repeat=repeat)
            for row in summary["rows"]:
                row["exact"] = True
            with self.subTest(repeat=repeat):
                self.assertEqual(compare.acceptance_checks(summary), [])
                self.assertEqual(compare.metrics(summary["rows"])["exact"], 12 * repeat)

    def test_failed_or_missing_warmup_cannot_pass(self):
        for warmup in ({}, {"status": 503}, {"status": 502}):
            summary = synthetic_summary()
            summary["warmup"] = warmup
            with self.subTest(warmup=warmup):
                self.assertIn("warmup-failed", compare.acceptance_checks(summary))

    def test_uncertain_transport_or_missing_row_cannot_pass(self):
        for status in (502, 503, 429):
            summary = synthetic_summary()
            summary["rows"][0]["response"]["status"] = status
            summary["rows"][0]["backend_receipt"]["outcome"] = "UNKNOWN"
            with self.subTest(status=status):
                self.assertIn("computed-corpus-incomplete", compare.acceptance_checks(summary))
        summary = synthetic_summary()
        summary["rows"].pop()
        self.assertIn("computed-corpus-incomplete", compare.acceptance_checks(summary))

    def test_every_timed_row_requires_one_computed_backend_exchange(self):
        for count, execution in ((0, "computed"), (2, "computed"), (1, "cache"), (1, "coalesced")):
            summary = synthetic_summary()
            row = summary["rows"][0]
            row["backend_exchange_count"] = count
            row["response"]["body"]["cellivane"]["execution"] = execution
            with self.subTest(count=count, execution=execution):
                self.assertIn("computed-corpus-incomplete", compare.acceptance_checks(summary))

    def test_incomplete_bridge_unknown_result_and_output_fence_are_rejected(self):
        for mutation in ("incomplete", "dispatch", "unknown", "fenced", "missing"):
            summary = synthetic_summary()
            bridge = summary["bridge_report"]
            if mutation == "incomplete":
                bridge["outcome"] = "INCOMPLETE"
            elif mutation == "dispatch":
                bridge["dispatch_count"] = 1
            elif mutation == "unknown":
                bridge["results"]["ollama-candidate"]["outcome"] = "UNKNOWN"
            elif mutation == "fenced":
                bridge["output_allowed"] = False
            else:
                bridge["results"].pop("seed-reference")
            with self.subTest(mutation=mutation):
                self.assertIn("bridge-incomplete", compare.acceptance_checks(summary))

    def test_nonzero_final_lifetime_or_cache_counters_fail(self):
        for field in ("active", "inflight", "waiters", "cache_hits", "coalesced", "failures", "timeouts"):
            summary = synthetic_summary()
            summary["state"][field] = 1
            with self.subTest(field=field):
                self.assertIn("scheduler-final-state", compare.acceptance_checks(summary))

    def test_final_backend_count_closed_and_poisoned_are_checked(self):
        for field, value in (("backend_calls", 25), ("computed", 25), ("closed", False), ("poisoned", True)):
            summary = synthetic_summary()
            summary["state"][field] = value
            with self.subTest(field=field):
                self.assertIn("scheduler-final-state", compare.acceptance_checks(summary))

    def test_missing_unknown_failed_placement_is_not_accepted(self):
        for status in (None, "UNKNOWN", "FAILED"):
            summary = synthetic_summary()
            summary["placement_observed"]["status"] = status
            with self.subTest(status=status):
                self.assertIn("placement-unresolved-or-failed", compare.acceptance_checks(summary))

    def test_hybrid_partial_observation_passes_without_qualification(self):
        summary = synthetic_summary("hybrid")
        self.assertEqual(compare.acceptance_checks(summary), [])
        self.assertIs(summary["placement_observed"]["gpu_qualified"], False)
        self.assertIs(summary["placement_observed"]["multi_gpu_qualified"], False)

    def test_hybrid_requires_numeric_partial_offload(self):
        for offloaded, total, gpu in ((0, 37, True), (37, 37, True), (38, 37, True),
                                     (True, 37, True), (14, True, True), (14, 37, False), (None, None, False)):
            summary = synthetic_summary("hybrid")
            summary["placement_observed"].update(offloaded_layers=offloaded, total_layers=total,
                                                gpu_offload_observed=gpu)
            with self.subTest(offloaded=offloaded, total=total, gpu=gpu):
                self.assertIn("hybrid-split-not-observed", compare.acceptance_checks(summary))

    def test_auto_may_resolve_to_cpu_without_claiming_gpu(self):
        summary = synthetic_summary("auto")
        self.assertEqual(compare.acceptance_checks(summary), [])
        self.assertFalse(summary["placement_observed"]["gpu_offload_observed"])


class ComparisonConfigurationTests(unittest.TestCase):
    def test_execution_configuration_has_exact_allowlist_and_profile_options(self):
        expected = {"CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "OLLAMA_VULKAN",
            "OLLAMA_NO_CLOUD", "OLLAMA_NUM_PARALLEL", "OLLAMA_MAX_LOADED_MODELS", "OLLAMA_CONTEXT_LENGTH", "OLLAMA_DEBUG"}
        for profile in ("cpu", "hybrid", "auto"):
            plan = build_plan(profile)
            config = compare.execution_configuration("synthetic:latest", plan)
            with self.subTest(profile=profile):
                self.assertEqual(set(config["environment"]), expected)
                self.assertEqual(config["environment"]["CUDA_VISIBLE_DEVICES"], "-1" if profile == "cpu" else "0")
                self.assertEqual(config["environment"]["OLLAMA_VULKAN"], "0")
                self.assertEqual(config["environment"]["OLLAMA_NO_CLOUD"], "1")
                self.assertEqual(config["options"], plan.ollama_options())

    def test_identity_v2_changes_with_effective_mask_option_and_alias(self):
        plan = build_plan("hybrid")
        artifact = artifact_identity(model_sha256="a" * 64, backend_tree_sha256="b" * 64, plan_sha256=plan.sha256)
        config = compare.execution_configuration("synthetic:latest", plan)
        def identity(value):
            return sha256(canonical({"schema_version": 2, "artifact_identity": artifact,
                "execution_configuration_sha256": sha256(canonical(value)).hexdigest()})).hexdigest()
        initial = identity(config)
        for name in ("mask", "options", "alias"):
            changed = copy.deepcopy(config)
            if name == "mask":
                changed["environment"]["CUDA_VISIBLE_DEVICES"] = "-1"
            elif name == "options":
                changed["options"]["num_gpu"] = 15
            else:
                changed["alias"] = "other-synthetic:latest"
            with self.subTest(name=name):
                self.assertNotEqual(initial, identity(changed))

    def test_backend_tree_binds_nested_executable_and_dll_content(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary, dll = root / "ollama.exe", root / "lib/cuda/ggml.dll"
            dll.parent.mkdir(parents=True)
            binary.write_bytes(b"synthetic executable, never launched")
            dll.write_bytes(b"synthetic shared library, never loaded")
            members, tree_hash = compare.backend_tree(binary)
            self.assertEqual(set(members), {"ollama.exe", "lib/cuda/ggml.dll"})
            self.assertEqual(members["lib/cuda/ggml.dll"], sha256(dll.read_bytes()).hexdigest())
            self.assertEqual(tree_hash, sha256(canonical(members)).hexdigest())
            dll.write_bytes(b"changed synthetic library")
            self.assertNotEqual(tree_hash, compare.backend_tree(binary)[1])

    def test_empty_backend_tree_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "backend-tree-empty"):
                compare.backend_tree(Path(folder) / "ollama.exe")

    def test_compare_source_closure_is_six_seed_seven_bridge_three_tools(self):
        actual = compare.source_hashes()
        expected = {"hosted/experimental/cellivane_seed/" + name + ".py" for name in
            ("__init__", "__main__", "core", "server", "placement", "ollama")}
        expected |= {"hosted/experimental/cellivane_bridge/" + name + ".py" for name in
            ("__init__", "__main__", "adapters", "cli", "contracts", "runtime", "storage")}
        expected |= {"tools/hosted/" + name + ".py" for name in
            ("cellivane_seed_compare", "cellivane_seed_lab", "cellivane_bridge_lab")}
        self.assertEqual(set(actual), expected)
        self.assertTrue(all(re.fullmatch("[0-9a-f]{64}", value) for value in actual.values()))

    def test_old_smoke_source_family_remains_thirteen_without_new_backend_imports(self):
        path = ROOT / "tools/hosted/verify_cellivane_seed.py"
        spec = importlib.util.spec_from_file_location("synthetic_old_seed_verifier", path)
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        self.assertEqual(len(verifier.SOURCE_PATHS), 13)
        self.assertNotIn("hosted/experimental/cellivane_seed/placement.py", verifier.SOURCE_PATHS)
        self.assertNotIn("hosted/experimental/cellivane_seed/ollama.py", verifier.SOURCE_PATHS)
        for relative in ("tools/hosted/cellivane_seed_lab.py", "hosted/experimental/cellivane_seed/__init__.py",
                         "hosted/experimental/cellivane_seed/__main__.py", "hosted/experimental/cellivane_seed/server.py"):
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
            with self.subTest(relative=relative):
                self.assertFalse(any(name in ("ollama", "placement", "cellivane_seed.ollama", "cellivane_seed.placement") for name in imports))

    def test_gpu_snapshot_is_advisory_and_mocked_failure_is_unavailable(self):
        with patch.object(compare.subprocess, "run", return_value=SimpleNamespace(stdout="synthetic GPU, 0, 8, 2, 6\n")) as call:
            self.assertEqual(compare.gpu_snapshot()["lines"], ["synthetic GPU, 0, 8, 2, 6"])
            self.assertEqual(call.call_args.kwargs["timeout"], 10)
        for failure in (OSError("synthetic missing tool"), subprocess.TimeoutExpired("synthetic", 10)):
            with self.subTest(failure=type(failure)), patch.object(compare.subprocess, "run", side_effect=failure):
                self.assertIs(compare.gpu_snapshot()["unavailable"], True)

    def test_invalid_ports_reject_before_storage_prepare_or_owned_process(self):
        base = ["--model", "0.6b", "--model-file", "synthetic.gguf", "--asset-root", "synthetic-assets",
                "--storage-root", "synthetic-storage"]
        for ports in (("80", "18952"), ("18951", "18951"), ("18951", "65536")):
            with self.subTest(ports=ports), patch.object(compare.os, "name", "nt"), \
                    patch.object(compare, "local_root") as storage, patch.object(compare.lab, "prepare") as prepare, \
                    patch.object(compare.lab, "OwnedProcess") as owned, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    compare.main(base + ["--port", ports[0], "--worker-port", ports[1]])
                self.assertEqual(result.exception.code, 2)
                storage.assert_not_called()
                prepare.assert_not_called()
                owned.assert_not_called()

    def test_metrics_exclude_failed_transport_timings(self):
        rows = [{"response": {"status": status}, "exact": False, "wall_s": wall,
                 "generation_tokens_s": rate} for status, wall, rate in
                ((200, 1., 10.), (200, 3., None), (503, 1000., 999.))]
        result = compare.metrics(rows)
        self.assertEqual((result["total"], result["answered"], result["exact"]), (3, 2, 0))
        self.assertEqual(result["median_wall_s"], 2.)
        self.assertEqual(result["median_generation_tokens_s"], 10.)
        self.assertIn("backend prompt KV reuse may occur", result["timing_scope"])
        self.assertIsNone(compare.metrics([])["median_wall_s"])
        self.assertIsNone(compare.metrics([])["median_generation_tokens_s"])


if __name__ == "__main__":
    unittest.main()
