"""Pure journal fixtures and attacks; none qualify an actual model backend."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

TOOL = Path(__file__).resolve().parents[1] / "verify_cellivane_bridge.py"
SPEC = importlib.util.spec_from_file_location("independent_cellivane_bridge_verifier", TOOL)
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def wire(value):
    # Noncanonical server bytes exercise hashing of actual wire, not parsed dicts.
    return json.dumps(value, ensure_ascii=False)


def qualified(config):
    if config["kind"] == "ollama":
        first = {"version": "fixture-only"}
        listed = {"models": [{"name": config["model"], "model": config["model"],
                              "digest": "a" * 64, "size": 42}]}
        paths = ["/api/version", "/api/tags"]
    else:
        first = {"status": "ok"}
        listed = {"object": "list", "data": [{"id": config["model"], "created": 0}]}
        paths = ["/health", "/v1/models"]
    responses = []
    for path, value in zip(paths, [first, listed]):
        body = wire(value)
        responses.append({"endpoint": path, "status": 200, "response_body": body,
                          "response_sha256": sha(body.encode()), "raw_response": value})
    return {"schema_version": 1, "name": config["name"], "kind": config["kind"], "model": config["model"],
            "base_url": config["base_url"], "outcome": "CONFIRMED", "model_present": True,
            "server_version": "fixture-only" if config["kind"] == "ollama" else None,
            "model_digest": "a" * 64 if config["kind"] == "ollama" else None,
            "responses": responses, "error": None, "elapsed_s": .01}


def answered(config, messages, content):
    request = {"model": config["model"], "messages": messages, "stream": False}
    if config["kind"] == "ollama":
        request.update(think=False, options={"num_predict": config["max_tokens"]})
        raw = {"model": config["model"], "created_at": "2026-10-09T00:00:00Z", "done": True,
               "done_reason": "stop", "message": {"role": "assistant", "content": content},
               "prompt_eval_count": 10, "eval_count": 3}
        usage = {"prompt_eval_count": 10, "eval_count": 3}
    else:
        request.update(max_tokens=config["max_tokens"], temperature=0)
        raw = {"id": "fixture-response", "model": config["model"], "created": 0,
               "choices": [{"index": 0, "finish_reason": "stop",
                            "message": {"role": "assistant", "content": content}}],
               "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}
        usage = copy.deepcopy(raw["usage"])
    request_body, response_body = canonical(request).decode(), wire(raw)
    return {"name": config["name"], "kind": config["kind"], "model": config["model"],
            "outcome": "ANSWERED", "text": content, "elapsed_s": .02,
            "request_sha256": sha(request_body.encode()), "response_sha256": sha(response_body.encode()),
            "raw_response": raw, "error": None, "usage": usage,
            "request_body": request_body, "response_body": response_body, "http_status": 200}


def fixture():
    configs = [{"name": "reference", "kind": "openai", "model": "fixture-ref-model",
                "base_url": "http://127.0.0.1:18081/v1", "role": "reference", "timeout_s": 10.0,
                "max_tokens": 64},
               {"name": "child", "kind": "ollama", "model": "fixture-child-model",
                "base_url": "http://127.0.0.1:18082", "role": "candidate", "timeout_s": 10.0,
                "max_tokens": 64}]
    prompt = "fixture question"
    messages = [{"role": "system", "content": "Answer the user briefly and accurately. /no_think"},
                {"role": "user", "content": prompt}]
    accepted = {"prompt": prompt, "messages": messages, "engines": configs, "parallelism": 2,
                "call_budget": 2, "deadline_s": 30.0, "directory_fsync": False,
                "scope": "isolated-local-experiment", "policy": "reference-is-authoritative"}
    quals = [qualified(e) for e in configs]
    results = [answered(configs[0], messages, "authoritative fixture answer"),
               answered(configs[1], messages, "different child fixture answer")]
    final = {"schema_version": 1, "run_id": None, "run_directory": None, "outcome": "COMPLETE",
             "error": None, "reference_engine": "reference", "answer": results[0]["text"],
             "qualifications": {r["name"]: copy.deepcopy(r) for r in quals},
             "results": {r["name"]: copy.deepcopy(r) for r in results}, "dispatch_count": 2,
             "call_budget": 2, "parallelism": 2, "elapsed_s": .5, "output_allowed": True,
             "cancellation_requested": False, "deadline_exceeded": False,
             "qualification_scope": "http-model-availability-only", "model_training": False,
             "token_mtp": False, "kv_sharing": False, "canonical_main_binding": False,
             "directory_fsync": False}
    return [("accepted", accepted), ("qualification", quals[0]), ("qualification", quals[1]),
            ("dispatch", {"engine": "reference", "role": "reference", "effective_timeout_s": 10.0}),
            ("result", results[0]),
            ("dispatch", {"engine": "child", "role": "candidate", "effective_timeout_s": 10.0}),
            ("result", results[1]), ("final", final)]


def refresh_final(rows):
    final = rows[-1][1]
    final["qualifications"] = {r["name"]: copy.deepcopy(r) for kind, r in rows if kind == "qualification"}
    final["results"] = {r["name"]: copy.deepcopy(r) for kind, r in rows if kind == "result"}
    final["dispatch_count"] = sum(kind == "dispatch" for kind, _ in rows)


def write_fixture(parent, rows=None):
    root = Path(parent) / str(uuid4())
    root.mkdir(parents=True)
    rows = copy.deepcopy(fixture() if rows is None else rows)
    if rows and rows[-1][0] == "final":
        rows[-1][1]["run_id"] = root.name
        rows[-1][1]["run_directory"] = str(root.resolve())
    previous = "0" * 64
    for sequence, (kind, payload) in enumerate(rows, 1):
        record = {"schema_version": 1, "sequence": sequence, "kind": kind,
                  "previous_sha256": previous, "payload": payload}
        raw = canonical(record)
        (root / f"{sequence:04d}-{kind}.json").write_bytes(raw)
        previous = sha(raw)
    return root


class BridgeVerifierTests(unittest.TestCase):
    def setUp(self):
        parent = os.environ.get("AIOS_BRIDGE_VERIFIER_EVIDENCE_DIR")
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="bridge-verifier-", dir=parent)
        self.parent = Path(self.temporary.name).resolve()

    def tearDown(self):
        # Only test-created children of this exact TemporaryDirectory are removed.
        self.assertTrue(self.parent.name.startswith("bridge-verifier-"))
        self.temporary.cleanup()

    def reject(self, rows, reason=None):
        root = write_fixture(self.parent, rows)
        with self.assertRaises(ValueError) as caught:
            verifier.replay(root)
        if reason:
            self.assertIn(reason, str(caught.exception))

    def test_complete_both_protocols_extract_raw_text(self):
        report = verifier.replay(write_fixture(self.parent))
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(report["dispatch_count"], 2)
        self.assertFalse(report["actual_backend_qualification"])
        self.assertFalse(report["process_liveness_verified"])

    def test_cancelled_after_valid_receipts_suppresses_output(self):
        rows = fixture()
        rows[-1][1].update(cancellation_requested=True, output_allowed=False, answer=None, outcome="CANCELLED")
        self.assertEqual(verifier.replay(write_fixture(self.parent, rows))["run_outcome"], "CANCELLED")

    def test_cancelled_fabricated_output_rejected(self):
        rows = fixture()
        rows[-1][1].update(cancellation_requested=True, outcome="CANCELLED")
        self.reject(rows, "final-output-fence")

    def test_deadline_fabricated_output_rejected(self):
        rows = fixture()
        rows[-1][1].update(deadline_exceeded=True, error="deadline", outcome="INCOMPLETE")
        self.reject(rows, "final-output-fence")

    def test_missing_final_rejected(self):
        self.reject(fixture()[:-1], "journal-terminals")

    def test_missing_result_rejected_even_when_final_resealed(self):
        rows = fixture()
        del rows[6]
        refresh_final(rows)
        self.reject(rows, "missing-dispatch-result")

    def test_tampered_final_answer_rejected(self):
        rows = fixture()
        rows[-1][1]["answer"] = rows[6][1]["text"]
        self.reject(rows, "final-reference-answer")

    def test_rebound_result_identity_rejected(self):
        rows = fixture()
        rows[4][1]["model"] = "other-generation-model"
        refresh_final(rows)
        self.reject(rows, "result-identity")

    def test_rebound_qualification_identity_rejected(self):
        rows = fixture()
        rows[1][1]["base_url"] = "http://127.0.0.1:19000"
        refresh_final(rows)
        self.reject(rows, "qualification-identity")

    def test_request_budget_resealed_digest_rejected(self):
        rows = fixture()
        result = rows[4][1]
        request = json.loads(result["request_body"])
        request["max_tokens"] = 512
        result["request_body"] = canonical(request).decode()
        result["request_sha256"] = sha(result["request_body"].encode())
        refresh_final(rows)
        self.reject(rows, "request-contract")

    def test_request_messages_resealed_digest_rejected(self):
        rows = fixture()
        result = rows[4][1]
        request = json.loads(result["request_body"])
        request["messages"][1]["content"] = "substituted question"
        result["request_body"] = canonical(request).decode()
        result["request_sha256"] = sha(result["request_body"].encode())
        refresh_final(rows)
        self.reject(rows, "request-contract")

    def test_request_sha_rejected(self):
        rows = fixture()
        rows[4][1]["request_sha256"] = "f" * 64
        refresh_final(rows)
        self.reject(rows, "request-sha256")

    def test_response_sha_rejected(self):
        rows = fixture()
        rows[4][1]["response_sha256"] = "f" * 64
        refresh_final(rows)
        self.reject(rows, "response-sha256")

    def test_fabricated_text_and_final_resealed_rejected(self):
        rows = fixture()
        rows[4][1]["text"] = "forged without changing wire"
        rows[-1][1]["answer"] = rows[4][1]["text"]
        refresh_final(rows)
        self.reject(rows, "answered-text-usage-join")

    def test_response_parsed_dict_rebound_rejected(self):
        rows = fixture()
        rows[4][1]["raw_response"]["model"] = "other"
        refresh_final(rows)
        self.reject(rows, "response-raw-join")

    def test_response_control_and_length_finish_rejected(self):
        for content, finish in [("bad\rtext", "stop"), ("truncated", "length")]:
            with self.subTest(content=content, finish=finish):
                rows = fixture()
                result = rows[4][1]
                choice = result["raw_response"]["choices"][0]
                choice["message"]["content"], choice["finish_reason"] = content, finish
                result["response_body"] = wire(result["raw_response"])
                result["response_sha256"] = sha(result["response_body"].encode())
                result["text"] = rows[-1][1]["answer"] = content
                refresh_final(rows)
                self.reject(rows, "response-")

    def test_oversized_answer_resealed_every_field_rejected(self):
        rows = fixture()
        result = rows[4][1]
        content = "x" * (256 * 1024)
        result["raw_response"]["choices"][0]["message"]["content"] = content
        result["response_body"] = wire(result["raw_response"])
        result["response_sha256"] = sha(result["response_body"].encode())
        result["text"] = rows[-1][1]["answer"] = content
        refresh_final(rows)
        self.reject(rows, "response-wire-size")

    def test_child_dispatch_before_reference_result_rejected(self):
        rows = fixture()
        rows[4], rows[5] = rows[5], rows[4]
        self.reject(rows, "reference-dispatch-fence")

    def test_qualification_after_dispatch_rejected(self):
        rows = fixture()
        rows[2], rows[3] = rows[3], rows[2]
        self.reject(rows, "dispatch-qualification-role")

    def test_budget_or_bool_counter_rejected(self):
        for key, value in [("call_budget", 3), ("call_budget", True), ("parallelism", 3), ("parallelism", True)]:
            with self.subTest(key=key, value=value):
                rows = fixture()
                rows[0][1][key] = value
                rows[-1][1][key] = value
                self.reject(rows)

    def test_final_counter_rejected(self):
        rows = fixture()
        rows[-1][1]["dispatch_count"] = 1
        self.reject(rows, "final-budget-join")

    def test_alias_endpoint_rejected(self):
        rows = fixture()
        rows[0][1]["engines"][1]["base_url"] = "http://127.0.0.1:18081"
        self.reject(rows, "engine-alias")

    def test_unknown_record_fields_rejected(self):
        rows = fixture()
        rows[-1][1]["actual_backend_qualification"] = True
        self.reject(rows, "final-fields")

    def test_three_engines_parallel_submission_budget(self):
        rows = fixture()
        third = copy.deepcopy(rows[0][1]["engines"][1])
        third.update(name="reviewer", role="reviewer", base_url="http://127.0.0.1:18083")
        rows[0][1]["engines"].append(third)
        rows[0][1]["call_budget"] = rows[-1][1]["call_budget"] = 3
        q = qualified(third)
        result = answered(third, rows[0][1]["messages"], "reviewer fixture answer")
        rows.insert(3, ("qualification", q))
        rows.insert(7, ("dispatch", {"engine": "reviewer", "role": "reviewer", "effective_timeout_s": 10.0}))
        rows.insert(9, ("result", result))
        refresh_final(rows)
        self.assertEqual(verifier.replay(write_fixture(self.parent, rows))["journal_records"], 11)
        rows[0][1]["parallelism"] = rows[-1][1]["parallelism"] = 1
        self.reject(rows, "dispatch-parallelism-budget")

    def test_unqualified_child_allows_only_reference_answer(self):
        rows = fixture()
        rows[2][1].update(outcome="UNKNOWN", model_present=False, responses=[],
                           server_version=None, model_digest=None, error="http-transport")
        rows = rows[:5] + [rows[-1]]
        refresh_final(rows)
        rows[-1][1].update(outcome="INCOMPLETE", error="subengine-incomplete")
        report = verifier.replay(write_fixture(self.parent, rows))
        self.assertEqual(report["dispatch_count"], 1)
        self.assertTrue(report["output_allowed"])

    def test_cancelled_not_started_reference_is_valid_zero_child_generation(self):
        rows = fixture()
        rows[4][1].update(outcome="NOT_STARTED", text=None, usage=None, raw_response=None,
                           response_body=None, response_sha256=None, http_status=None,
                           error="cancelled-before-dispatch")
        rows = rows[:5] + [rows[-1]]
        refresh_final(rows)
        rows[-1][1].update(outcome="CANCELLED", cancellation_requested=True, output_allowed=False,
                           answer=None, error="reference-result-uncertain")
        self.assertEqual(verifier.replay(write_fixture(self.parent, rows))["dispatch_count"], 1)

    def test_unknown_invalid_utf8_hash_does_not_qualify_missing_wire(self):
        rows = fixture()
        rows[6][1].update(outcome="UNKNOWN", text=None, usage=None, raw_response=None,
                           response_body=None, response_sha256=sha(b'\xff'), error="response-json")
        refresh_final(rows)
        rows[-1][1].update(outcome="INCOMPLETE", error="subengine-incomplete")
        report = verifier.replay(write_fixture(self.parent, rows))
        self.assertEqual(report["unverifiable_wire_records"], 1)
        self.assertFalse(report["actual_backend_qualification"])

    def test_qualification_raw_wire_hash_and_model_list_tamper_rejected(self):
        rows = fixture()
        rows[1][1]["responses"][1]["response_sha256"] = "b" * 64
        refresh_final(rows)
        self.reject(rows, "response-sha256")
        rows = fixture()
        response = rows[1][1]["responses"][1]
        response["raw_response"]["data"][0]["id"] = "other-model"
        response["response_body"] = wire(response["raw_response"])
        response["response_sha256"] = sha(response["response_body"].encode())
        refresh_final(rows)
        self.reject(rows, "qualification-model-match")

    def test_dispatch_wrong_role_or_timeout_rejected(self):
        for key, value, reason in [("role", "reference", "dispatch-qualification-role"),
                                   ("effective_timeout_s", 10.001, "dispatch-timeout-budget"),
                                   ("effective_timeout_s", True, "dispatch-timeout-budget")]:
            with self.subTest(key=key, value=value):
                rows = fixture()
                rows[5][1][key] = value
                self.reject(rows, reason)

    def test_raw_usage_bool_and_impossible_total_rejected(self):
        for value in [True, 999]:
            rows = fixture()
            result = rows[4][1]
            result["raw_response"]["usage"]["total_tokens"] = value
            result["usage"]["total_tokens"] = value
            result["response_body"] = wire(result["raw_response"])
            result["response_sha256"] = sha(result["response_body"].encode())
            refresh_final(rows)
            self.reject(rows, "response-usage")

    def test_failed_reference_has_no_child_generation(self):
        rows = fixture()
        q = rows[1][1]
        q.update(outcome="UNKNOWN", model_present=False, error="http-transport", responses=[], server_version=None)
        rows = [rows[0], rows[1], rows[-1]]
        rows[-1][1].update(outcome="INCOMPLETE", error="reference-not-qualified", answer=None, output_allowed=False)
        refresh_final(rows)
        report = verifier.replay(write_fixture(self.parent, rows))
        self.assertEqual(report["dispatch_count"], 0)

    def test_uncertain_reference_with_child_generation_rejected(self):
        rows = fixture()
        rows[4][1].update(outcome="UNKNOWN", text=None, error="deadline", usage=None)
        refresh_final(rows)
        rows[-1][1].update(outcome="INCOMPLETE", error="reference-result-uncertain", answer=None, output_allowed=False)
        self.reject(rows, "reference-dispatch-fence")

    def test_strata_reference_mock_qualification_rejected(self):
        rows = fixture()
        rows[0][1]["engines"][0]["kind"] = rows[1][1]["kind"] = "strata"
        refresh_final(rows)
        self.reject(rows, "strata-qualification-fence")

    def test_strata_unimplemented_reference_zero_generation_valid(self):
        rows = fixture()
        rows[0][1]["engines"][0]["kind"] = "strata"
        q = rows[1][1]
        q.update(kind="strata", outcome="UNKNOWN", model_present=False, server_version=None,
                 responses=[], error="strata-runtime-qualification-not-implemented", elapsed_s=0.0)
        rows = [rows[0], rows[1], rows[-1]]
        rows[-1][1].update(outcome="INCOMPLETE", error="reference-not-qualified", answer=None, output_allowed=False)
        refresh_final(rows)
        self.assertEqual(verifier.replay(write_fixture(self.parent, rows))["dispatch_count"], 0)

    def test_cancel_before_generation_valid(self):
        rows = fixture()
        rows = [rows[0], rows[-1]]
        # An accepted turn can be cancelled before even its first metadata probe.
        rows[-1][1].update(outcome="CANCELLED", error="subengine-incomplete", answer=None,
                           cancellation_requested=True, output_allowed=False)
        refresh_final(rows)
        report = verifier.replay(write_fixture(self.parent, rows))
        self.assertEqual(report["dispatch_count"], 0)

    def test_missing_answered_wire_rejected(self):
        rows = fixture()
        rows[4][1].update(response_body=None, response_sha256=None, raw_response=None)
        refresh_final(rows)
        self.reject(rows, "response-model")

    def test_duplicate_raw_response_key_rejected(self):
        rows = fixture()
        result = rows[4][1]
        result["response_body"] = result["response_body"].replace('"model":', '"model":"duplicate", "model":', 1)
        result["response_sha256"] = sha(result["response_body"].encode())
        refresh_final(rows)
        self.reject(rows, "invalid-response-parsed")

    def test_record_chain_tamper_rejected(self):
        root = write_fixture(self.parent)
        target = root / "0001-accepted.json"
        value = json.loads(target.read_bytes())
        value["payload"]["prompt"] = "tampered"
        target.write_bytes(canonical(value))
        with self.assertRaisesRegex(ValueError, "record-hash-chain"):
            verifier.replay(root)

    def test_record_filename_sequence_rejected(self):
        root = write_fixture(self.parent)
        (root / "0004-dispatch.json").rename(root / "0004-result.json")
        with self.assertRaisesRegex(ValueError, "record-file-sequence"):
            verifier.replay(root)

    def test_duplicate_json_nonfinite_and_whitespace_rejected(self):
        for raw in [b'{"schema_version":1,"schema_version":1}', b'{"value":NaN}', b'{"value":1e999}']:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    verifier.decode(raw)
        root = write_fixture(self.parent)
        path = root / "0008-final.json"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "record-canonical-bytes"):
            verifier.replay(root)

    def test_terminal_followed_by_additional_file_rejected(self):
        root = write_fixture(self.parent)
        (root / "stale-pass.json").write_text('{}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "journal-file-name"):
            verifier.replay(root)

    def test_cli_pass_and_failure_are_json_with_exit_codes(self):
        root = write_fixture(self.parent)
        result = subprocess.run([sys.executable, str(TOOL), "--run-directory", str(root)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["verdict"], "PASS")
        result = subprocess.run([sys.executable, str(TOOL), "--run-directory", str(root / "missing")],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["verdict"], "FAIL")

    def test_runtime_modules_not_imported(self):
        program = ("import importlib.util,sys; "
                   "s=importlib.util.spec_from_file_location('reader',sys.argv[1]); "
                   "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                   "assert not any(n=='cellivane_bridge' or n.startswith('cellivane_bridge.') for n in sys.modules)")
        result = subprocess.run([sys.executable, "-c", program, str(TOOL)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
