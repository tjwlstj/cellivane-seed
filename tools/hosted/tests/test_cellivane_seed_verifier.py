"""Synthetic self-contained Seed smoke replay; no runtime or actual D: inputs.

These fabricated fixtures test verifier semantics, never tensor execution,
process liveness, source authenticity, or model quality. Every audit chain is
built here independently, including resealed semantic mutations.
"""
from __future__ import annotations

import ast
import copy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from uuid import UUID

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("seed_independent_verifier", ROOT / "tools/hosted/verify_cellivane_seed.py")
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)

SOURCES = (
    "hosted/experimental/cellivane_bridge/__init__.py", "hosted/experimental/cellivane_bridge/__main__.py",
    "hosted/experimental/cellivane_bridge/adapters.py", "hosted/experimental/cellivane_bridge/cli.py",
    "hosted/experimental/cellivane_bridge/contracts.py", "hosted/experimental/cellivane_bridge/runtime.py",
    "hosted/experimental/cellivane_bridge/storage.py", "hosted/experimental/cellivane_seed/__init__.py",
    "hosted/experimental/cellivane_seed/__main__.py", "hosted/experimental/cellivane_seed/core.py",
    "hosted/experimental/cellivane_seed/server.py", "tools/hosted/cellivane_bridge_lab.py",
    "tools/hosted/cellivane_seed_lab.py")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def hashed(value):
    return sha256(canonical(value)).hexdigest()


class SyntheticSmoke:
    def __init__(self, root):
        self.root, self.audit = Path(root), Path(root) / "synthetic-audit"
        self.audit.mkdir()
        self.path = self.root / "synthetic-summary.json"
        self.model, self.records, self.sequence, self.uid = "tiny-synthetic", [], 0, 0
        self.counts = {key: 0 for key in ("backend_calls", "computed", "cache_hits", "coalesced", "failures", "rejected", "timeouts")}
        self.active, self.waiters, self.cached = 0, 0, {}
        self.config = {"engine": "cellivane-seed", "model": self.model, "slots": 1,
            "binary_sha256": "b" * 64, "model_sha256": "a" * 64,
            "worker_args": ["synthetic-fixture-not-an-engine", "--host", "127.0.0.1"], "synthetic_fixture": True}
        identity_fields = {"binary": self.config["binary_sha256"], "model": self.config["model_sha256"],
                           "arguments": self.config["worker_args"]}
        self.identity = sha256(json.dumps(identity_fields, sort_keys=True).encode()).hexdigest()
        self.config["identity"] = self.identity
        self.append("configuration", self.config)
        observations = []
        for prompt, text, expected in (("synthetic arithmetic", "15", "15"),
                ("synthetic control", "CELLIVANE_OK", "CELLIVANE_OK"), ("synthetic korean", "서울", "서울")):
            request = self.request(prompt)
            key, value = self.compute(request, text)
            cold = self.response(request, value, key, "computed")
            self.counts["cache_hits"] += 1
            self.event("cache_hit", key, "cache", value=value)
            warm = self.response(request, value, key, "cache")
            observations.append({"name": prompt, "cold": cold, "warm": warm, "expected": expected,
                                 "exact": text == expected, "backend_calls_delta": 1, "cold_s": .1, "warm_s": .01})
        request = self.request("synthetic simultaneous subscribers")
        key = hashed({"identity": self.identity, "inference": request})
        self.counts["backend_calls"] += 1
        self.active = 1
        self.event("start", key, "computed")
        self.waiters = 1
        self.event("join", key, "coalesced")
        self.counts["rejected"] += 1
        overloaded = self.request("synthetic different overloaded request")
        self.event("reject", hashed({"identity": self.identity, "inference": overloaded}), reason="slot-capacity")
        value = self.value("SHARED_OK")
        self.exchange(request, value)
        self.cached[key] = value
        self.active = 0
        self.counts["computed"] += 1
        self.event("finish", key, "computed", value=value)
        owner = self.response(request, value, key, "computed")
        self.waiters = 0
        self.counts["coalesced"] += 1
        self.event("finish", key, "coalesced", value=value)
        shared = self.response(request, value, key, "coalesced")
        overload_body = {"error": {"message": "compute-capacity-full"}}
        observations.append({"name": "coalescing-and-overload", "backend_calls_delta": 1,
            "responses": [owner, shared], "overload": {"request": overloaded, "status": 429,
                "body": overload_body, "raw_response": canonical(overload_body).decode()}})
        bridge_request = self.request("synthetic bridge reference request")
        bridge_key, bridge_value = self.compute(bridge_request, "BRIDGE_OK")
        seed_response = self.response(bridge_request, bridge_value, bridge_key, "computed")
        seed = self.engine_result("seed-reference", "openai", bridge_request, seed_response["body"])
        ollama_request = {"model": self.model + ":latest", "messages": bridge_request["messages"],
            "stream": False, "think": False, "options": {"num_predict": 128}}
        ollama_body = {"model": self.model + ":latest", "created_at": "2026-10-12T00:00:00Z",
            "message": {"role": "assistant", "content": "BRIDGE_OK"}, "done": True, "done_reason": "stop",
            "prompt_eval_count": 3, "eval_count": 2}
        ollama = self.engine_result("ollama-candidate", "ollama", ollama_request, ollama_body)
        bridge = {"outcome": "COMPLETE", "dispatch_count": 2, "output_allowed": True,
            "answer": "BRIDGE_OK", "error": None, "results": {"seed-reference": seed, "ollama-candidate": ollama},
            "reference_engine": "seed-reference", "canonical_main_binding": False, "token_mtp": False,
            "kv_sharing": False, "model_training": False, "synthetic_fixture": True}
        observations.append({"name": "bridge", "report": bridge})
        self.cached.clear()
        self.event("close", reason="engine-closed")
        final = {"engine": "seed", "version": "0.1.0", "model": self.model, "identity": self.identity,
            "active": 0, "inflight": 0, "waiters": 0, "closed": True, "poisoned": False,
            "slots": 1, "max_waiters": 8, "cache_entries": 0, "cache_bytes": 0,
            "cache_entries_limit": 32, "cache_bytes_limit": 1048576, "event_seq": self.sequence, **self.counts}
        self.append("shutdown", final)
        cleanup = [{"name": name, "pid": 100 + index, "method": "owned-job-terminate-and-close",
            "returncode": 1, "created_suspended": True, "assigned_before_resume": True,
            "primary_thread_closed": True, "process_handle_closed": True, "process_error": None,
            "process_exited": True, "job_assigned": True, "job_terminated": True,
            "job_closed": True, "job_cleanup_confirmed": True, "job_active_processes": 0, "job_error": None,
            "backend_stop_api_confirmed": False, "kv_release_confirmed": False}
            for index, name in enumerate(("seed-worker", "ollama-candidate", "ollama-create"))]
        self.summary = {"schema_version": 1, "engine": "cellivane-seed", "version": "0.1.0",
            "scope": "synthetic-verifier-fixture-only", "synthetic_fixture": True,
            "outcome": "PASS", "execution_outcome": "COMPLETED", "errors": [], "mode": "smoke",
            "source_unchanged_during_run": True,
            "source_sha256": {path: sha256(("synthetic source label:" + path).encode()).hexdigest() for path in SOURCES},
            "backend_identity": self.identity, "binary_sha256": self.config["binary_sha256"],
            "model_sha256": self.config["model_sha256"], "model": self.model,
            "audit_directory": str(self.audit.resolve()), "state": final, "observations": observations,
            "ports_closed": {"18932": True, "18941": True, "18942": True}, "cleanup": cleanup,
            "canonical_main_binding": False, "token_mtp": False, "kv_sharing": False,
            "independent_tensor_kernels": False, "quality_improvement_claim": False}
        self.seal()

    def request(self, prompt):
        return {"model": self.model, "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 128, "temperature": 0, "stream": False}

    def value(self, text):
        return {"model": self.model, "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}

    def append(self, kind, value):
        self.records.append({"kind": kind, "value": copy.deepcopy(value)})

    def event(self, kind, key=None, execution=None, reason=None, value=None):
        self.sequence += 1
        self.append("scheduler", {"seq": self.sequence, "kind": kind, "key": key, "execution": execution,
            "reason": reason, "response_sha256": hashed(value) if value is not None else None,
            "active": self.active, "waiters": self.waiters, **self.counts,
            "cache_entries": len(self.cached),
            "cache_bytes": sum(len(canonical(v)) + len(k) for k, v in self.cached.items())})

    def exchange(self, request, value):
        result = self.engine_result("tensor-fixture", "openai", request, value)
        self.append("backend_exchange", result)

    def compute(self, request, text):
        key, value = hashed({"identity": self.identity, "inference": request}), self.value(text)
        self.active = 1
        self.counts["backend_calls"] += 1
        self.event("start", key, "computed")
        self.exchange(request, value)
        self.cached[key] = value
        self.active = 0
        self.counts["computed"] += 1
        self.event("finish", key, "computed", value=value)
        return key, value

    def response(self, request, value, key, execution):
        self.uid += 1
        body = {"id": str(UUID(int=self.uid)), "created": 0, "object": "chat.completion", **copy.deepcopy(value),
            "cellivane": {"engine": "seed", "version": "0.1.0", "execution": execution,
                "key": key, "backend_calls": self.counts["backend_calls"]}}
        return {"request": copy.deepcopy(request), "status": 200, "body": body, "raw_response": canonical(body).decode()}

    def engine_result(self, name, kind, request, response):
        request_body, response_body = canonical(request).decode(), canonical(response).decode()
        text = response["message"]["content"] if kind == "ollama" else response["choices"][0]["message"]["content"]
        usage = {"prompt_eval_count": 3, "eval_count": 2} if kind == "ollama" else copy.deepcopy(response["usage"])
        return {"name": name, "kind": kind, "model": request["model"], "outcome": "ANSWERED", "text": text,
            "elapsed_s": .1, "request_body": request_body, "response_body": response_body,
            "request_sha256": sha256(request_body.encode()).hexdigest(),
            "response_sha256": sha256(response_body.encode()).hexdigest(), "raw_response": copy.deepcopy(response),
            "usage": usage, "http_status": 200, "error": None}

    def row(self, outer_kind, **fields):
        return next(record["value"] for record in self.records if record["kind"] == outer_kind
                    and all(record["value"].get(key) == value for key, value in fields.items()))

    def seal(self):
        # All paths belong to this newly created TemporaryDirectory.
        for path in self.audit.glob("*.json"):
            path.unlink()
        previous = None
        for seq, source in enumerate(self.records, 1):
            record = {"seq": seq, "previous_sha256": previous, **source}
            raw = canonical(record)
            (self.audit / f"{seq:06d}.json").write_bytes(raw)
            previous = sha256(raw).hexdigest()
        self.path.write_bytes(canonical(self.summary))

    def sync_response(self, response):
        response["raw_response"] = canonical(response["body"]).decode()


class SeedVerifierTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bundle = SyntheticSmoke(self.directory.name)

    def reject(self):
        self.bundle.seal()  # Repair unkeyed hashes; semantic checks must still fail.
        with self.assertRaises((ValueError, KeyError, TypeError, OSError)):
            verifier.verify(self.bundle.path)

    def test_full_synthetic_smoke_gold_path_has_honest_verdict_scope(self):
        result = verifier.verify(self.bundle.path)
        self.assertEqual(result["outcome"], "PASS")
        self.assertEqual((result["backend_calls"], result["cache_hits"], result["coalesced"]), (5, 3, 1))
        for key in ("actual_backend_qualification", "process_liveness_verified", "token_mtp", "quality_improvement_verified"):
            self.assertIs(result[key], False)

    def test_verifier_imports_no_producer_runtime(self):
        tree = ast.parse(Path(SPEC.origin).read_text(encoding="utf-8"))
        names = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        names += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        self.assertFalse(any(name and (name.startswith("cellivane_") or name.startswith("aios_")) for name in names))

    def test_backend_wire_hash_mismatch_is_rejected(self):
        self.bundle.row("backend_exchange")["response_sha256"] = "0" * 64
        self.reject()

    def test_raw_backend_response_cannot_disagree_with_parse(self):
        self.bundle.row("backend_exchange")["raw_response"]["model"] = "forged"
        self.reject()

    def test_cache_hit_must_have_finished_origin(self):
        self.bundle.row("scheduler", kind="cache_hit")["key"] = "0" * 64
        self.reject()

    def test_response_identity_key_is_bound_after_raw_body_reseal(self):
        response = self.bundle.summary["observations"][0]["warm"]
        response["body"]["cellivane"]["key"] = "0" * 64
        self.bundle.sync_response(response)
        self.reject()

    def test_backend_identity_cannot_change_without_artifact_binding(self):
        self.bundle.config["identity"] = "0" * 64
        self.bundle.records[0]["value"]["identity"] = "0" * 64
        self.bundle.summary["backend_identity"] = "0" * 64
        self.reject()

    def test_scheduler_counter_tamper_is_rejected_after_chain_reseal(self):
        self.bundle.row("scheduler", kind="finish")["backend_calls"] = 999
        self.reject()

    def test_duplicate_backend_exchange_is_rejected(self):
        record = next(record for record in self.bundle.records if record["kind"] == "backend_exchange")
        self.bundle.records.insert(self.bundle.records.index(record) + 1, copy.deepcopy(record))
        self.reject()

    def test_missing_backend_record_is_rejected_even_when_chain_is_repaired(self):
        record = next(record for record in self.bundle.records if record["kind"] == "backend_exchange")
        self.bundle.records.remove(record)
        self.reject()

    def test_missing_coalesced_finish_cannot_claim_zero_waiters(self):
        record = next(record for record in self.bundle.records if record["kind"] == "scheduler"
                      and record["value"]["kind"] == "finish" and record["value"]["execution"] == "coalesced")
        self.bundle.records.remove(record)
        self.reject()

    def test_503_cold_response_cannot_be_promoted_to_smoke_pass(self):
        self.bundle.summary["observations"][0]["cold"]["status"] = 503
        self.reject()

    def test_incomplete_bridge_blocks_overall_pass(self):
        self.bundle.summary["observations"][4]["report"]["outcome"] = "INCOMPLETE"
        self.reject()

    def test_cleanup_false_or_unclosed_port_blocks_pass(self):
        self.bundle.summary["cleanup"][0]["job_cleanup_confirmed"] = False
        self.reject()

    def test_missing_source_closure_member_blocks_pass(self):
        self.bundle.summary["source_sha256"].pop(SOURCES[0])
        self.reject()

    def test_raw_duplicate_json_keys_are_rejected_before_semantics(self):
        self.bundle.path.write_bytes(b'{"outcome":"FAIL","outcome":"PASS"}')
        with self.assertRaisesRegex(ValueError, "duplicate-json-key"):
            verifier.verify(self.bundle.path)

    def test_noncanonical_audit_and_broken_chain_are_rejected(self):
        path = self.bundle.audit / "000001.json"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "audit-chain"):
            verifier.verify(self.bundle.path)

    def test_false_exact_quality_label_cannot_be_promoted(self):
        self.bundle.summary["observations"][0]["exact"] = False
        self.reject()

    def test_normalized_rejects_length_bool_usage_and_reasoning(self):
        for field, value in (("usage", {"prompt_tokens": True, "completion_tokens": 2, "total_tokens": 3}),
                             ("reasoning_content", "hidden")):
            raw = self.bundle.value("answer")
            raw[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                verifier.normalized(raw, self.bundle.model)
        raw = self.bundle.value("answer")
        raw["choices"][0]["finish_reason"] = "length"
        with self.assertRaises(ValueError):
            verifier.normalized(raw, self.bundle.model)

    def test_read_rejects_nonfinite_and_json_depth(self):
        for raw in (b'{"x":NaN}', b'{"x":1e999}', b'{"x":' + b'[' * 21 + b'0' + b']' * 21 + b'}'):
            self.bundle.path.write_bytes(raw)
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                verifier.read(self.bundle.path)

    def test_coalesced_finish_cannot_precede_durable_computed_finish(self):
        owner_index = next(i for i, record in enumerate(self.bundle.records) if record["kind"] == "scheduler"
            and record["value"]["kind"] == "finish" and record["value"]["execution"] == "computed"
            and record["value"]["waiters"] == 1)
        owner, waiter = self.bundle.records[owner_index:owner_index + 2]
        self.bundle.records[owner_index:owner_index + 2] = [waiter, owner]
        waiter["value"]["active"], waiter["value"]["computed"] = 1, 3
        owner["value"]["waiters"], owner["value"]["coalesced"] = 0, 1
        for seq, record in enumerate((record for record in self.bundle.records if record["kind"] == "scheduler"), 1):
            record["value"]["seq"] = seq
        self.reject()

    def test_same_count_forged_source_path_is_rejected(self):
        closure = self.bundle.summary["source_sha256"]
        closure["unrelated/forged.py"] = closure.pop(SOURCES[0])
        self.reject()

    def test_per_response_backend_call_counter_is_verified(self):
        response = self.bundle.summary["observations"][0]["cold"]
        response["body"]["cellivane"]["backend_calls"] = 999
        self.bundle.sync_response(response)
        self.reject()

    def test_bridge_answered_label_does_not_override_http_503(self):
        self.bundle.summary["observations"][4]["report"]["results"]["seed-reference"]["http_status"] = 503
        self.reject()

    def test_extra_unobserved_shared_subscriber_is_rejected(self):
        shared = self.bundle.summary["observations"][3]["responses"]
        extra = copy.deepcopy(shared[1])
        extra["body"]["id"] = str(UUID(int=999))
        self.bundle.sync_response(extra)
        shared.append(extra)
        self.reject()

    def test_bad_engine_or_version_cannot_qualify_seed_envelope(self):
        response = self.bundle.summary["observations"][0]["cold"]
        response["body"]["cellivane"]["engine"] = "different-engine"
        self.bundle.sync_response(response)
        self.reject()

    def test_boolean_temperature_rejected_with_consistent_keys_wire_and_chain(self):
        class BooleanTemperatureSmoke(SyntheticSmoke):
            def request(self, prompt):
                request = super().request(prompt)
                request["temperature"] = False
                return request

        root = Path(self.directory.name) / "boolean-temperature"
        root.mkdir()
        invalid = BooleanTemperatureSmoke(root)
        # Build every key and wire hash from False; no stale digest causes rejection.
        request = json.loads(invalid.row("backend_exchange")["request_body"])
        self.assertIs(request["temperature"], False)
        with self.assertRaisesRegex(ValueError, "wire-request"):
            verifier.verify(invalid.path)

    def test_overload_cannot_reject_existing_inflight_key_after_chain_reseal(self):
        shared = self.bundle.summary["observations"][3]
        self.bundle.row("scheduler", kind="reject")["key"] = self.bundle.row("scheduler", kind="join")["key"]
        shared["overload"]["request"] = copy.deepcopy(shared["responses"][0]["request"])
        self.bundle.seal()
        # Both overload evidence and event now name the owner; it should have joined.
        with self.assertRaisesRegex(ValueError, "reject-capacity"):
            verifier.verify(self.bundle.path)


if __name__ == "__main__":
    unittest.main()
