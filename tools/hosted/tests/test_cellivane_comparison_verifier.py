"""Synthetic comparison receipts only: no tensor engine or hardware qualification."""
from __future__ import annotations

import ast
import copy
import importlib.util
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location("independent_comparison", TOOLS / "verify_cellivane_comparison.py")
v = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(v)


def response(request, body, status=200):
    return {"status": status, "body": copy.deepcopy(body), "raw_response": json.dumps(body, ensure_ascii=False), "request": copy.deepcopy(request)}


def receipt(request, content, name="ollama-tensor"):
    raw_request = {"model": request["model"], "messages": request["messages"], "stream": False,
                   "think": False, "options": {"num_predict": 64}}
    raw = {"model": request["model"], "message": {"role": "assistant", "content": content}, "done": True,
           "done_reason": "stop", "created_at": "2026-10-12T00:00:00Z", "prompt_eval_count": 10,
           "eval_count": 2, "eval_duration": 1000000000}
    request_body = v.encoded(raw_request).decode()
    response_body = json.dumps(raw, ensure_ascii=False)
    return {"name": name, "kind": "ollama", "model": request["model"], "outcome": "ANSWERED", "text": content,
        "elapsed_s": .1, "request_sha256": v.sha256(request_body.encode()).hexdigest(),
        "response_sha256": v.sha256(response_body.encode()).hexdigest(), "raw_response": raw, "error": None,
        "usage": {"prompt_eval_count": 10, "eval_count": 2}, "request_body": request_body,
        "response_body": response_body, "http_status": 200}


def qualified(config):
    ollama = config["kind"] == "ollama"
    values = [{"version": "fixture-only"}, {"models": [{"name": config["model"], "model": config["model"], "digest": "a" * 64, "size": 10}]}] if ollama else [
        {"status": "ok"}, {"object": "list", "data": [{"id": config["model"]}]}]
    paths = ("/api/version", "/api/tags") if ollama else ("/health", "/v1/models")
    return {"schema_version": 1, "name": config["name"], "kind": config["kind"], "model": config["model"],
        "base_url": config["base_url"], "outcome": "CONFIRMED", "model_present": True,
        "server_version": "fixture-only" if ollama else None, "model_digest": "a" * 64 if ollama else None,
        "responses": [{"endpoint": path, "status": 200, "response_body": json.dumps(value),
            "response_sha256": v.sha256(json.dumps(value).encode()).hexdigest(), "raw_response": value}
            for path, value in zip(paths, values)], "error": None, "elapsed_s": .01}


class Bundle:
    def __init__(self, parent, wrong=False, failed=False, profile="cpu"):
        self.root = Path(parent)
        self.summary_path = self.root / "comparison.json"
        self.audit = self.root / "audit"
        self.audit.mkdir()
        gpu = 0 if profile == "cpu" else 14 if profile == "hybrid" else -1
        plan = {"schema_version": 1, "backend": "ollama", "profile": profile, "num_ctx": 2048,
            "num_thread": 4, "slots": 1, "num_gpu": gpu, "temperature": 0, "scope": "requested-placement-only"}
        alias = "cellivane-compare-0.6b-" + profile + ":latest"
        members = {"ollama.exe": "1" * 64, "runner.dll": "2" * 64}
        execution = {"schema_version": 1, "alias": alias, "options": {"num_ctx": 2048, "num_thread": 4, "num_gpu": gpu, "temperature": 0},
            "environment": {"CUDA_VISIBLE_DEVICES": "-1" if profile == "cpu" else "0", "HIP_VISIBLE_DEVICES": "-1", "ROCR_VISIBLE_DEVICES": "-1", "OLLAMA_VULKAN": "0",
                "OLLAMA_NO_CLOUD": "1", "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_CONTEXT_LENGTH": "2048", "OLLAMA_DEBUG": "1"},
            "device_scope": "one visible NVIDIA device for hybrid/auto; CPU-only for cpu"}
        artifact_id = v.digest({"schema_version": 1, "model_sha256": v.MODELS["0.6b"][1], "backend_tree_sha256": v.digest(members), "plan_sha256": v.digest(plan)})
        identity = v.digest({"schema_version": 2, "artifact_identity": artifact_id, "execution_configuration_sha256": v.digest(execution)})
        sources = {}
        self.source_root = self.root / "source"
        for name in v.SOURCE_PATHS:
            path = self.source_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"# synthetic source fixture\n")
            sources[name] = v.sha256(path.read_bytes()).hexdigest()
        log = b'load_backend: loaded CPU backend\nload_tensors: CPU model buffer size = 100.00 MiB\nload_tensors: offloaded 0/29 layers to GPU\n'
        (self.root / "ollama.log").write_bytes(log)
        modelfile = ('FROM "/fixture/Qwen3-0.6B-Q8_0.gguf"\nPARAMETER num_ctx 2048\nPARAMETER num_thread 4\nPARAMETER num_gpu ' + str(gpu) + '\nPARAMETER temperature 0\n').encode()
        (self.root / "Modelfile").write_bytes(modelfile)
        self.summary = {"schema_version": 1, "experiment": "cellivane-seed-comparison", "model": "0.6b", "alias": alias,
            "model_sha256": v.MODELS["0.6b"][1], "model_bytes": v.MODELS["0.6b"][0], "backend_archive_sha256": "e29ad1d5063dd4b54b2492d9b00adab2cff9621bfa654b6c92aa8d6f1fdfe7fc",
            "backend_members_sha256": members, "backend_tree_sha256": v.digest(members), "plan": plan, "plan_sha256": v.digest(plan),
            "identity_schema_version": 2, "artifact_identity": artifact_id, "execution_configuration": execution,
            "execution_configuration_sha256": v.digest(execution), "identity": identity, "modelfile_sha256": v.sha256(modelfile).hexdigest(),
            "source_sha256": sources, "cases": copy.deepcopy(v.CASES), "corpus_sha256": v.digest(v.CASES), "system": v.SYSTEM,
            "repeat": 1, "max_tokens": 64, "temperature": 0, "think": False, "cache_entries": 0, "rows": [], "cleanup": [],
            "gpu_before": {"source": "nvidia-smi", "unavailable": True}, "gpu_loaded": {"source": "nvidia-smi", "unavailable": True},
            "gpu_after": {"source": "nvidia-smi", "unavailable": True}, "multi_gpu_qualified": False, "native_mtp": False,
            "quality_generalization_claim": False, "endpoint_pid_attestation": False, "audit_directory": str(self.audit),
            "warmup_wall_s": .1, "outcome": "COMPLETED", "ports_closed": {"18951": True, "18952": True},
            "source_unchanged_during_run": True, "model_unchanged_during_run": True, "log_sha256": v.sha256(log).hexdigest(),
            "placement_observed": {"schema_version": 1, "scope": "run-log-observation-only", "status": "CPU_OBSERVED", "offloaded_layers": 0,
                "total_layers": 29, "offload_count_source": "native", "runtimes": ["CPU"], "buffers": [{"runtime": "CPU", "device": "CPU", "kind": "model", "mib": 100.0}],
                "gpu_offload_observed": False, "gpu_qualified": False, "multi_gpu_qualified": False, "errors": [], "warnings": [], "log_sha256": v.sha256(log).hexdigest()}}
        self.summary["loaded_models"] = response(None, {"models": [{"name": alias, "model": alias, "digest": "a" * 64,
            "size": 900000000, "size_vram": 0, "context_length": 2048, "details": {"quantization_level": "Q8_0"}}]})
        if profile != "cpu":
            off = 14 if profile == "hybrid" else 29
            log = ('load_backend: loaded CPU backend\nload_backend: loaded CUDA backend\n'
                'llama_kv_cache: CPU KV buffer size = 10.00 MiB\nload_tensors: CUDA0 model buffer size = 50.00 MiB\n'
                f'load_tensors: offloaded {off}/29 layers to GPU\n').encode()
            (self.root / "ollama.log").write_bytes(log)
            self.summary["log_sha256"] = v.sha256(log).hexdigest()
            self.summary["placement_observed"].update(status="GPU_OFFLOAD_OBSERVED", offloaded_layers=off,
                runtimes=["CPU", "CUDA"], buffers=[{"runtime": "CPU", "device": "CPU", "kind": "kv", "mib": 10.0},
                {"runtime": "CUDA", "device": "CUDA0", "kind": "model", "mib": 50.0}], gpu_offload_observed=True, log_sha256=v.sha256(log).hexdigest())
            loaded = self.summary["loaded_models"]
            loaded["body"]["models"][0]["size_vram"] = 500000000
            loaded["raw_response"] = json.dumps(loaded["body"])
        self.records = [("comparison_configuration", {k: self.summary[k] for k in ("identity", "plan", "model_sha256", "backend_tree_sha256", "corpus_sha256", "alias")})]
        self.counts = {k: 0 for k in v.COUNTERS}
        self.event_seq = 0
        self.summary["warmup"] = self.call(v.payload(alias, "Reply with only OK."), "OK")
        for i, case in enumerate(v.CASES):
            request = v.payload(alias, case["prompt"])
            content = "wrong fixture answer" if wrong else case["expected"]
            reply = self.call(request, content, fail=failed and i == 0)
            backend = copy.deepcopy(self.records[-2][1])
            self.summary["rows"].append({"case": case["id"], "repeat": 0, "wall_s": .2, "response": reply,
                "text": "" if reply["status"] != 200 else content, "exact": reply["status"] == 200 and content == case["expected"],
                "backend_exchange_count": 1, "backend_receipt": backend, "generation_tokens_s": 2.0})
        self.make_bridge()
        self.emit("close", None, reason="engine-closed")
        self.summary["state"] = {"engine": "seed", "version": "0.1.0", "model": alias, "identity": identity, "closed": True,
            "poisoned": False, "slots": 1, "active": 0, "inflight": 0, "waiters": 0, "max_waiters": 8, "cache_entries": 0,
            "cache_bytes": 0, "cache_entries_limit": 0, "cache_bytes_limit": 1048576, **self.counts, "event_seq": self.event_seq}
        self.records.append(("shutdown", copy.deepcopy(self.summary["state"])))
        for i, name in enumerate(("ollama", "create"), 1):
            self.summary["cleanup"].append({"name": name, "pid": i, "method": "owned-job-terminate-and-close", "returncode": 0,
                "process_exited": True, "process_error": None, "backend_stop_api_confirmed": False, "kv_release_confirmed": False,
                "job_assigned": True, "job_terminated": True, "job_active_processes": 0, "job_closed": True,
                "job_cleanup_confirmed": True, "job_error": None, "created_suspended": True, "assigned_before_resume": True,
                "primary_thread_closed": True, "process_handle_closed": True})
        good = [r for r in self.summary["rows"] if r["response"]["status"] == 200]
        self.summary["metrics"] = {"total": 12, "answered": len(good), "exact": sum(r["exact"] for r in self.summary["rows"]),
            "median_wall_s": .2, "median_generation_tokens_s": 2.0, "timing_scope": v.TIMING_SCOPE}
        self.summary["acceptance_errors"] = ["computed-corpus-incomplete", "scheduler-final-state"] if failed else []
        self.summary["acceptance"] = "FAIL" if failed else "PASS"
        self.write()

    def emit(self, kind, key, value=None, reason=None):
        self.event_seq += 1
        self.records.append(("scheduler", {"seq": self.event_seq, "kind": kind, "key": key,
            "execution": None if kind == "close" else "computed", "reason": reason,
            "response_sha256": v.digest(value) if value is not None else None, "active": 1 if kind == "start" else 0,
            "waiters": 0, "cache_entries": 0, "cache_bytes": 0, **self.counts}))

    def call(self, request, content, fail=False):
        key = v.digest({"identity": self.summary["identity"], "inference": request})
        self.counts["backend_calls"] += 1
        self.emit("start", key)
        backend = receipt(request, content)
        if fail:
            backend.update(outcome="UNKNOWN", text=None, usage=None, error="response-finish-length")
            backend["raw_response"]["done_reason"] = "length"
            backend["response_body"] = json.dumps(backend["raw_response"], ensure_ascii=False)
            backend["response_sha256"] = v.sha256(backend["response_body"].encode()).hexdigest()
        self.records.append(("backend_exchange", backend))
        if fail:
            self.counts["failures"] += 1
            self.emit("failure", key, reason="backend-error")
            return response(request, {"error": {"message": "backend-not-completed"}}, 502)
        value = {"model": request["model"], "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}
        self.counts["computed"] += 1
        self.emit("finish", key, value)
        return response(request, {"id": str(uuid4()), "object": "chat.completion", "created": 1, **value,
            "cellivane": {"engine": "seed", "version": "0.1.0", "execution": "computed", "key": key, "backend_calls": self.counts["backend_calls"]}})

    def make_bridge(self):
        alias = self.summary["alias"]
        prompt = "Reply with exactly this text and nothing else: CELLIVANE_OK"
        messages = [{"role": "system", "content": "Answer the user briefly and accurately. /no_think"}, {"role": "user", "content": prompt}]
        request = {"model": alias, "messages": messages, "stream": False, "temperature": 0, "max_tokens": 64}
        reply = self.call(request, "CELLIVANE_OK")
        engines = [{"name": "seed-reference", "kind": "openai", "base_url": "http://127.0.0.1:18951/v1", "model": alias, "role": "reference", "timeout_s": 120, "max_tokens": 64},
            {"name": "ollama-candidate", "kind": "ollama", "base_url": "http://127.0.0.1:18952", "model": alias, "role": "candidate", "timeout_s": 120, "max_tokens": 64}]
        ref = {"name": "seed-reference", "kind": "openai", "model": alias, "outcome": "ANSWERED", "text": "CELLIVANE_OK", "elapsed_s": .1,
            "request_sha256": v.sha256(v.encoded(request)).hexdigest(), "response_sha256": v.sha256(reply["raw_response"].encode()).hexdigest(),
            "raw_response": reply["body"], "error": None, "usage": reply["body"]["usage"], "request_body": v.encoded(request).decode(),
            "response_body": reply["raw_response"], "http_status": 200}
        candidate = receipt(request, "CELLIVANE_OK", "ollama-candidate")
        quals = [qualified(e) for e in engines]
        directory = self.root / str(uuid4())
        directory.mkdir()
        accepted = {"prompt": prompt, "messages": messages, "engines": engines, "parallelism": 1, "call_budget": 2,
            "deadline_s": 180, "directory_fsync": False, "scope": "isolated-local-experiment", "policy": "reference-is-authoritative"}
        final = {"schema_version": 1, "run_id": directory.name, "run_directory": str(directory), "outcome": "COMPLETE", "error": None,
            "reference_engine": "seed-reference", "answer": "CELLIVANE_OK", "qualifications": {q["name"]: q for q in quals},
            "results": {"seed-reference": ref, "ollama-candidate": candidate}, "dispatch_count": 2, "call_budget": 2, "parallelism": 1,
            "elapsed_s": .5, "output_allowed": True, "model_training": False, "token_mtp": False, "kv_sharing": False,
            "canonical_main_binding": False, "directory_fsync": False, "cancellation_requested": False, "deadline_exceeded": False,
            "qualification_scope": "http-model-availability-only"}
        rows = [("accepted", accepted), ("qualification", quals[0]), ("qualification", quals[1]),
            ("dispatch", {"engine": "seed-reference", "role": "reference", "effective_timeout_s": 120}), ("result", ref),
            ("dispatch", {"engine": "ollama-candidate", "role": "candidate", "effective_timeout_s": 120}), ("result", candidate), ("final", final)]
        previous = "0" * 64
        for seq, (kind, value) in enumerate(rows, 1):
            raw = v.encoded({"schema_version": 1, "sequence": seq, "kind": kind, "previous_sha256": previous, "payload": value})
            (directory / f"{seq:04d}-{kind}.json").write_bytes(raw)
            previous = v.sha256(raw).hexdigest()
        self.summary["bridge_report"] = final

    def write(self):
        for path in self.audit.iterdir():
            path.unlink()
        previous = None
        for seq, (kind, value) in enumerate(self.records, 1):
            raw = v.encoded({"seq": seq, "previous_sha256": previous, "kind": kind, "value": value})
            (self.audit / f"{seq:06d}.json").write_bytes(raw)
            previous = v.sha256(raw).hexdigest()
        self.summary_path.write_bytes(v.encoded(self.summary))


class ComparisonVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.bundle = Bundle(self.temporary.name)

    def reject(self, change, reason=None):
        change(self.bundle)
        self.bundle.write()
        with self.assertRaises(ValueError) as caught:
            v.replay(self.bundle.summary_path)
        if reason:
            self.assertIn(reason, str(caught.exception))

    def test_full_synthetic_replay_never_qualifies_engine_or_quality(self):
        result = v.replay(self.bundle.summary_path, self.bundle.source_root)
        self.assertEqual(result["outcome"], "PASS")
        self.assertEqual(result["metrics"]["exact"], 12)
        self.assertEqual(result["operational_acceptance"], "PASS")
        self.assertTrue(result["source_files_checked"])
        for name in ("actual_backend_qualification", "endpoint_pid_attestation", "process_liveness_verified", "quality_improvement_verified", "quality_generalization_verified", "native_mtp", "multi_gpu_qualified"):
            self.assertIs(result[name], False)

    def test_all_wrong_answers_still_complete_without_quality_claim(self):
        with tempfile.TemporaryDirectory() as root:
            result = v.replay(Bundle(root, wrong=True).summary_path)
            self.assertEqual(result["metrics"]["exact"], 0)
            self.assertEqual(result["operational_acceptance"], "PASS")

    def test_hybrid_and_auto_numeric_log_and_ps_joins(self):
        for profile in ("hybrid", "auto"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as root:
                result = v.replay(Bundle(root, profile=profile).summary_path)
                self.assertTrue(result["placement_observed"]["gpu_offload_observed"])
                self.assertIs(result["multi_gpu_qualified"], False)

    def test_raw_crlf_hash_and_normalized_text_hash_are_separate(self):
        b = self.bundle
        path = b.root / "ollama.log"
        raw = path.read_bytes().replace(b"\n", b"\r\n")
        path.write_bytes(raw)
        b.summary["log_sha256"] = v.sha256(raw).hexdigest()
        b.write()
        self.assertEqual(v.replay(b.summary_path)["outcome"], "PASS")

    def test_exact_masked_discovery_debug_is_not_runner_failure(self):
        b = self.bundle
        path = b.root / "ollama.log"
        raw = (b'time=2026-10-12T00:00:00Z level=DEBUG source=native_probe.go:12 msg="native GPU discovery failed" '
               b'stderr="ggml_cuda_init: failed to initialize CUDA: no CUDA-capable device is detected\\n"\n') + path.read_bytes()
        path.write_bytes(raw)
        b.summary["log_sha256"] = v.sha256(raw).hexdigest()
        b.summary["placement_observed"].update(log_sha256=v.sha256(raw).hexdigest(), warnings=["gpu-discovery-unavailable", "gpu-unavailable"])
        b.write()
        self.assertEqual(v.replay(b.summary_path)["outcome"], "PASS")

    def test_requested_hybrid_layer_count_cannot_be_rebound(self):
        with tempfile.TemporaryDirectory() as root:
            b = Bundle(root, profile="hybrid")
            path = b.root / "ollama.log"
            raw = path.read_bytes().replace(b"14/29", b"13/29")
            path.write_bytes(raw)
            b.summary["log_sha256"] = v.sha256(raw).hexdigest()
            b.summary["placement_observed"].update(log_sha256=v.sha256(raw).hexdigest(), offloaded_layers=13)
            b.write()
            with self.assertRaisesRegex(ValueError, "requested-hybrid-violated"):
                v.replay(b.summary_path)

    def test_source_and_model_changed_flags_reject(self):
        for field in ("source_unchanged_during_run", "model_unchanged_during_run"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root:
                b = Bundle(root)
                b.summary[field] = False
                b.write()
                with self.assertRaisesRegex(ValueError, "run-source-or-model-changed"):
                    v.replay(b.summary_path)

    def test_oversized_wire_rejected_before_digest_semantics(self):
        self.reject(lambda b: b.records[5][1].update(response_body="x" * (256 * 1024 + 1)), "wire-size")

    def test_generation_rate_must_come_from_raw_token_duration(self):
        self.reject(lambda b: b.summary["rows"][0].update(generation_tokens_s=999.0), "generation-rate")

    def test_unknown_scheduler_kind_cannot_hide_coalescing(self):
        self.reject(lambda b: b.records[4][1].update(kind="join"), "scheduler-transition")

    def test_network_source_paths_rejected_before_open(self):
        with self.assertRaisesRegex(ValueError, "network-path"):
            v.safe_path("\\\\untrusted-host\\share\\source")

    def test_failed_case_replays_but_operational_acceptance_fails(self):
        with tempfile.TemporaryDirectory() as root:
            result = v.replay(Bundle(root, failed=True).summary_path)
            self.assertEqual(result["metrics"]["answered"], 11)
            self.assertEqual(result["operational_acceptance"], "FAIL")

    def test_metric_and_quality_fabrications_are_rejected(self):
        self.reject(lambda b: b.summary["metrics"].update(exact=11), "metric-recompute")

    def test_corpus_expected_answer_rebind_is_rejected_after_digest_reseal(self):
        def attack(b):
            b.summary["cases"][0]["expected"] = "forged"
            b.summary["corpus_sha256"] = v.digest(b.summary["cases"])
        self.reject(attack, "corpus-binding")

    def test_case_order_and_repeat_coverage_are_exact(self):
        self.reject(lambda b: b.summary["rows"].reverse(), "case-order")

    def test_wire_request_think_and_num_predict_are_bound(self):
        def attack(b):
            row = b.records[5][1]
            raw = json.loads(row["request_body"])
            raw["think"] = True
            row["request_body"] = v.encoded(raw).decode()
            row["request_sha256"] = v.sha256(row["request_body"].encode()).hexdigest()
        self.reject(attack, "backend-request-binding")

    def test_boolean_temperature_cannot_equal_integer_zero(self):
        self.reject(lambda b: b.summary["rows"][0]["response"]["request"].update(temperature=False), "seed-request-binding")

    def test_response_hash_tamper_is_rejected(self):
        self.reject(lambda b: b.records[5][1].update(response_sha256="f" * 64), "backend-response-hash")

    def test_finish_length_cannot_be_answered_after_raw_reseal(self):
        def attack(b):
            row = b.records[5][1]
            row["raw_response"]["done_reason"] = "length"
            row["response_body"] = json.dumps(row["raw_response"])
            row["response_sha256"] = v.sha256(row["response_body"].encode()).hexdigest()
        self.reject(attack, "ollama-finish")

    def test_boolean_token_stat_is_rejected_after_raw_reseal(self):
        def attack(b):
            row = b.records[5][1]
            row["raw_response"]["eval_count"] = True
            row["response_body"] = json.dumps(row["raw_response"])
            row["response_sha256"] = v.sha256(row["response_body"].encode()).hexdigest()
        self.reject(attack, "ollama-statistic")

    def test_row_receipt_cannot_be_rebound_to_another_exchange(self):
        self.reject(lambda b: b.summary["rows"][0].update(backend_receipt=b.summary["rows"][1]["backend_receipt"]), "row-backend-receipt-join")

    def test_forged_cache_hit_response_is_rejected(self):
        def attack(b):
            r = b.summary["rows"][0]["response"]
            r["body"]["cellivane"]["execution"] = "cache"
            r["raw_response"] = json.dumps(r["body"])
        self.reject(attack, "seed-execution-binding")

    def test_scheduler_counter_forgery_rejected_after_chain_reseal(self):
        self.reject(lambda b: b.records[6][1].update(computed=99), "scheduler-counters")

    def test_missing_backend_exchange_is_rejected_after_chain_reseal(self):
        self.reject(lambda b: b.records.pop(5), "audit-order-or-count")

    def test_original_hash_chain_break_is_rejected(self):
        path = self.bundle.audit / "000002.json"
        row = json.loads(path.read_bytes())
        row["value"]["key"] = "f" * 64
        path.write_bytes(v.encoded(row))
        with self.assertRaisesRegex(ValueError, "audit-chain"):
            v.replay(self.bundle.summary_path)

    def test_source_file_tamper_is_rejected_when_source_root_given(self):
        (self.bundle.source_root / sorted(v.SOURCE_PATHS)[0]).write_bytes(b"# changed\n")
        with self.assertRaisesRegex(ValueError, "source-file-hash"):
            v.replay(self.bundle.summary_path, self.bundle.source_root)

    def test_source_closure_cannot_use_same_count_unknown_path(self):
        def attack(b):
            b.summary["source_sha256"]["unrelated.py"] = b.summary["source_sha256"].pop(sorted(v.SOURCE_PATHS)[0])
        self.reject(attack, "source-closure")

    def test_environment_configuration_cannot_be_rebound(self):
        self.reject(lambda b: b.summary["execution_configuration"]["environment"].update(OLLAMA_VULKAN="1"), "execution-configuration")

    def test_modelfile_tamper_rejected_even_if_declared_hash_resealed(self):
        raw = (self.bundle.root / "Modelfile").read_bytes().replace(b"num_gpu 0", b"num_gpu 14")
        (self.bundle.root / "Modelfile").write_bytes(raw)
        self.reject(lambda b: b.summary.update(modelfile_sha256=v.sha256(raw).hexdigest()), "modelfile-options")

    def test_model_backend_plan_identity_is_recomputed(self):
        self.reject(lambda b: b.summary.update(identity="f" * 64), "execution-identity")

    def test_placement_status_cannot_claim_gpu_from_cpu_log(self):
        self.reject(lambda b: b.summary["placement_observed"].update(status="GPU_OFFLOAD_OBSERVED", gpu_offload_observed=True), "placement-status")

    def test_cpu_plan_requires_zero_numeric_ps_vram(self):
        def attack(b):
            r = b.summary["loaded_models"]
            r["body"]["models"][0]["size_vram"] = 42
            r["raw_response"] = json.dumps(r["body"])
        self.reject(attack, "placement-cpu-join")

    def test_ps_alias_quantization_context_and_boolean_vram_rejected(self):
        for field, value in (("model", "other"), ("context_length", 4096), ("size_vram", False)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root:
                b = Bundle(root)
                r = b.summary["loaded_models"]
                r["body"]["models"][0][field] = value
                r["raw_response"] = json.dumps(r["body"])
                b.write()
                with self.assertRaises(ValueError):
                    v.replay(b.summary_path)

    def test_log_tamper_requires_actual_file_hash(self):
        (self.bundle.root / "ollama.log").write_bytes(b"forged\n")
        with self.assertRaisesRegex(ValueError, "placement-log-hash"):
            v.replay(self.bundle.summary_path)

    def test_actual_runtime_error_not_hidden_by_cpu_buffers(self):
        raw = (self.bundle.root / "ollama.log").read_bytes() + b'ggml_cuda_init: failed to initialize CUDA: invalid driver\n'
        (self.bundle.root / "ollama.log").write_bytes(raw)
        self.reject(lambda b: (b.summary.update(log_sha256=v.sha256(raw).hexdigest()), b.summary["placement_observed"].update(log_sha256=v.sha256(raw).hexdigest())), "placement-runtime-error")

    def test_unconfirmed_cleanup_and_ports_reject_pass(self):
        self.reject(lambda b: b.summary["cleanup"][0].update(job_cleanup_confirmed=False), "cleanup-unconfirmed")

    def test_port_receipt_false_is_rejected(self):
        self.reject(lambda b: b.summary["ports_closed"].update({"18951": False}), "port-receipts")

    def test_bridge_missing_final_cannot_be_hidden_by_summary(self):
        directory = Path(self.bundle.summary["bridge_report"]["run_directory"])
        sorted(directory.iterdir())[-1].unlink()
        with self.assertRaises(ValueError):
            v.replay(self.bundle.summary_path)

    def test_cancelled_bridge_cannot_claim_operational_success(self):
        self.reject(lambda b: b.summary["bridge_report"].update(cancellation_requested=True), "bridge-not-complete")

    def test_acceptance_label_cannot_hide_failed_semantics(self):
        self.reject(lambda b: b.summary.update(acceptance="FAIL", acceptance_errors=[]), "acceptance-recompute")

    def test_unknown_summary_fields_rejected(self):
        self.reject(lambda b: b.summary.update(unsupported=True), "summary-fields")

    def test_duplicate_nonfinite_and_overdeep_json_rejected(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b'[' * 22 + b'0' + b']' * 22):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                v.load(raw)

    def test_raw_duplicate_json_response_rejected(self):
        def attack(b):
            row = b.records[5][1]
            row["response_body"] = row["response_body"][:-1] + ',"done":true}'
            row["response_sha256"] = v.sha256(row["response_body"].encode()).hexdigest()
        self.reject(attack, "duplicate-json-key")

    def test_verifier_imports_no_producer_runtime(self):
        tree = ast.parse((TOOLS / "verify_cellivane_comparison.py").read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(n.name for n in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        self.assertFalse(any(n.startswith(("cellivane_seed", "cellivane_bridge", "hosted")) for n in imports))
        self.assertIn("verify_cellivane_bridge", imports)

    def test_cli_json_verdict_exit_codes(self):
        command = [sys.executable, "-B", str(TOOLS / "verify_cellivane_comparison.py"), str(self.bundle.summary_path), "--source-root", str(self.bundle.source_root)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["outcome"], "PASS")
        self.bundle.summary["native_mtp"] = True
        self.bundle.write()
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["outcome"], "FAIL")


if __name__ == "__main__":
    unittest.main()
