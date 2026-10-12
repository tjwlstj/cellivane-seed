"""Synthetic independent-reader attacks; never a tensor or speed qualification.

Fixtures are authored here as wire bytes and audit records. No runtime contract,
planner, server, adapter or experiment producer is imported to make them valid.
"""
from __future__ import annotations

import ast
import copy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location("independent_speculation", TOOLS / "verify_cellivane_speculation.py")
v = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(v)
ENGINE = "a" * 64
GIB = 1024**3


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def digest(value):
    return sha256(encode(value)).hexdigest()


def reseal_plan(phase):
    required = sum(phase["demand"][k] for k in
                   ("target_bytes", "draft_bytes", "host_overhead_bytes", "context_workspace_bytes"))
    phase["admission"] = {"schema_version": 1, "outcome": "ADMITTED", "reason": None,
        "capabilities": copy.deepcopy(phase["capabilities"]), "demand": copy.deepcopy(phase["demand"]),
        "budget": copy.deepcopy(phase["budget"]), "require_observed": False,
        "ram_required_bytes": required, "vram_required_bytes": 0,
        "scope": "declared-budget-preflight-only", "os_memory_limit_enforced": False,
        "tensor_residency_managed": False, "measured_peak_verified": False,
        "actual_backend_qualification": False}
    phase["identity"] = digest(phase["admission"])


def plan_fixture(mode="none"):
    summary = {"configuration": {"context": 2048, "alias": "cellivane-spec-target", "max_tokens": 64},
               "system": "Answer the requested task concisely.",
               "corpus": [{"id": "fixture-case", "prompt": "Reply with only fixture."}]}
    summary["configuration_sha256"] = digest(summary["configuration"])
    phase = {"mode": mode, "round": 0, "phase_id": "round-0-" + mode,
        "capabilities": {"schema_version": 1, "backend": "llama.cpp", "engine_identity": ENGINE,
            "declared_modes": ["draft", "ngram", "none"], "declared_placements": ["cpu"],
            "observed_modes": [], "observed_placements": [], "evidence_sha256": None,
            "observation_identity": None, "nonstream_chat": True,
            "scope": "caller-declarations-and-evidence-labels", "actual_backend_qualification": False},
        "demand": {"schema_version": 1, "engine_identity": ENGINE,
            "target_sha256": "8c2f07f26af9747e41988551106f149b03eb9b5cb6df636027b6bf6278473300",
            "target_bytes": 4280404704,
            "draft_sha256": "9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031" if mode == "draft" else None,
            "draft_bytes": 639446688 if mode == "draft" else 0,
            "configuration_sha256": digest({"configuration_sha256": summary["configuration_sha256"], "mode": mode}),
            "mode": mode, "placement": "cpu", "context_tokens": 2048,
            "host_overhead_bytes": GIB, "context_workspace_bytes": GIB,
            "gpu_required_bytes": None, "gpu_estimate_sha256": None,
            "scope": "declared-files-and-total-runtime-workspace-estimate"},
        "budget": {"schema_version": 1, "ram_available_bytes": 16 * GIB, "ram_headroom_bytes": 2 * GIB,
            "vram_available_bytes": 0, "vram_headroom_bytes": 0,
            "scope": "caller-supplied-available-byte-snapshot"}}
    reseal_plan(phase)
    return phase, summary


def cleanup_fixture():
    return {"name": "llama-cpp", "pid": 1234, "method": "owned-job-terminate-and-close", "returncode": 1,
        **dict.fromkeys(("created_suspended", "assigned_before_resume", "primary_thread_closed",
            "process_handle_closed", "process_exited", "job_assigned", "job_terminated", "job_closed",
            "job_cleanup_confirmed"), True),
        "job_active_processes": 0, "job_error": None, "process_error": None,
        "backend_stop_api_confirmed": False, "kv_release_confirmed": False}


class WireFixture:
    """A tiny, manually authored computed-only transcript with an owned tmpdir."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.phase, self.summary = plan_fixture()
        self.phase["audit_directory"] = str(self.directory)
        rows = []
        for prompt in ("Reply with only OK.", self.summary["corpus"][0]["prompt"]):
            request = {"model": self.summary["configuration"]["alias"], "messages": [
                {"role": "system", "content": self.summary["system"]}, {"role": "user", "content": prompt}],
                "temperature": 0, "max_tokens": 64, "stream": False}
            raw = {"model": request["model"], "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "fixture"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10,
                    "prompt_tokens_details": {"cached_tokens": 0}}}
            receipt = {"name": "llama-cpp-tensor", "kind": "openai", "model": request["model"],
                "outcome": "ANSWERED", "text": "fixture", "elapsed_s": .05,
                "request_body": encode(request).decode(), "response_body": encode(raw).decode(),
                "request_sha256": digest(request), "response_sha256": digest(raw), "raw_response": raw,
                "error": None, "usage": copy.deepcopy(raw["usage"]), "http_status": 200}
            rows.append({"response": {"request": request}, "backend_receipt": receipt,
                         "wall_s": .1, "backend_exchange_count": 1})
        self.phase["warmup"], self.phase["rows"] = rows[0], [rows[1]]
        rows[1].update(case="fixture-case", text="fixture")
        self.reseal()

    def reseal(self):
        """Recompute all cheap hashes: semantic attacks cannot fail just on stale SHA."""
        phase, summary = self.phase, self.summary
        records = [{"kind": "speculation_configuration", "value": {
            "identity": phase["identity"], "phase_id": phase["phase_id"],
            "configuration_sha256": summary["configuration_sha256"], "mode": phase["mode"],
            "admission_sha256": phase["identity"]}}]
        counts = dict.fromkeys(("backend_calls", "computed", "cache_hits", "coalesced", "failures", "rejected", "timeouts"), 0)
        seq = 0

        def event(kind, key=None, value=None):
            nonlocal seq
            seq += 1
            return {"kind": "scheduler", "value": {**counts, "seq": seq, "kind": kind, "key": key,
                "execution": None if kind == "close" else "computed",
                "reason": "engine-closed" if kind == "close" else None,
                "response_sha256": digest(value) if value is not None else None,
                "active": 1 if kind == "start" else 0, "waiters": 0, "cache_entries": 0, "cache_bytes": 0}}

        for row in [phase["warmup"], *phase["rows"]]:
            request, receipt = row["response"]["request"], row["backend_receipt"]
            receipt["request_body"], receipt["request_sha256"] = encode(request).decode(), digest(request)
            receipt["response_body"] = encode(receipt["raw_response"]).decode()
            receipt["response_sha256"] = digest(receipt["raw_response"])
            receipt["usage"] = copy.deepcopy(receipt["raw_response"]["usage"])
            value = {"model": receipt["raw_response"]["model"], "choices": copy.deepcopy(receipt["raw_response"]["choices"]),
                "usage": {k: receipt["usage"][k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")}}
            key = digest({"identity": phase["identity"], "inference": request})
            counts["backend_calls"] += 1
            records += [event("start", key), {"kind": "backend_exchange", "value": copy.deepcopy(receipt)}]
            counts["computed"] += 1
            records.append(event("finish", key, value))
            body = {**value, "id": str(uuid4()), "object": "chat.completion", "created": 0,
                "cellivane": {"engine": "seed", "version": "0.1.0", "execution": "computed", "key": key,
                              "backend_calls": counts["backend_calls"]}}
            row["response"].update(status=200, body=body, raw_response=encode(body).decode())
        records.append(event("close"))
        phase["state"] = {**counts, "engine": "seed", "version": "0.1.0", "identity": phase["identity"],
            "model": summary["configuration"]["alias"], "closed": True, "poisoned": False, "slots": 1,
            "active": 0, "inflight": 0, "waiters": 0, "max_waiters": 8, "cache_entries": 0, "cache_bytes": 0,
            "cache_entries_limit": 0, "cache_bytes_limit": 0, "event_seq": seq}
        records.append({"kind": "shutdown", "value": copy.deepcopy(phase["state"])})
        self.records = records
        self.write_chain()

    def write_chain(self):
        previous = None
        for seq, item in enumerate(self.records, 1):
            raw = encode({"seq": seq, "previous_sha256": previous, **item})
            (self.directory / f"{seq:06d}.json").write_bytes(raw)
            previous = sha256(raw).hexdigest()


class AdmissionReplayTests(unittest.TestCase):
    def test_none_and_draft_include_full_files_and_declared_workspace(self):
        for mode, expected in (("none", 4280404704 + 2 * GIB), ("draft", 4919851392 + 2 * GIB)):
            with self.subTest(mode=mode):
                phase, summary = plan_fixture(mode)
                self.assertEqual(phase["admission"]["ram_required_bytes"], expected)
                self.assertIsNone(v.verify_plan(phase, summary, ENGINE))

    def test_exact_ram_boundary_and_resealed_one_byte_over_budget(self):
        phase, summary = plan_fixture("draft")
        phase["budget"]["ram_available_bytes"] = phase["admission"]["ram_required_bytes"] + phase["budget"]["ram_headroom_bytes"]
        reseal_plan(phase)
        v.verify_plan(phase, summary, ENGINE)
        phase["budget"]["ram_available_bytes"] -= 1
        reseal_plan(phase)
        with self.assertRaisesRegex(ValueError, "ram-fit"):
            v.verify_plan(phase, summary, ENGINE)

    def test_ram_sum_overflow_cannot_be_resealed_as_admitted(self):
        phase, summary = plan_fixture()
        phase["demand"]["host_overhead_bytes"] = 2**63 - 1
        phase["budget"].update(ram_available_bytes=2**63 - 1, ram_headroom_bytes=0)
        reseal_plan(phase)
        with self.assertRaises(ValueError):
            v.verify_plan(phase, summary, ENGINE)

    def test_boolean_and_float_byte_counts_cannot_use_numeric_equality(self):
        for section, field in (("budget", "ram_headroom_bytes"), ("budget", "vram_available_bytes"),
                               ("demand", "context_workspace_bytes"), ("demand", "draft_bytes")):
            for value in (False, 0.0):
                with self.subTest(section=section, field=field, value=repr(value)):
                    phase, summary = plan_fixture()
                    phase[section][field] = value
                    reseal_plan(phase)
                    with self.assertRaises(ValueError):
                        v.verify_plan(phase, summary, ENGINE)

    def test_cpu_gpu_estimate_rejects_false_and_float_zero(self):
        for value in (False, 0.0, 1):
            with self.subTest(value=repr(value)):
                phase, summary = plan_fixture()
                phase["demand"]["gpu_required_bytes"] = value
                reseal_plan(phase)
                with self.assertRaises(ValueError):
                    v.verify_plan(phase, summary, ENGINE)

    def test_context_outside_contract_bound_even_with_resealed_summary(self):
        for value in (0, 2**20 + 1, True):
            with self.subTest(value=value):
                phase, summary = plan_fixture()
                phase["demand"]["context_tokens"] = summary["configuration"]["context"] = value
                summary["configuration_sha256"] = digest(summary["configuration"])
                phase["demand"]["configuration_sha256"] = digest({"configuration_sha256": summary["configuration_sha256"], "mode": phase["mode"]})
                reseal_plan(phase)
                with self.assertRaises(ValueError):
                    v.verify_plan(phase, summary, ENGINE)

    def test_unknown_or_blocked_admission_is_not_success(self):
        for outcome, reason in (("UNKNOWN", "backend-gpu-memory-estimate-missing"), ("BLOCKED", "ram-budget-insufficient")):
            with self.subTest(outcome=outcome):
                phase, summary = plan_fixture()
                phase["admission"].update(outcome=outcome, reason=reason)
                phase["identity"] = digest(phase["admission"])
                with self.assertRaisesRegex(ValueError, "admission-recompute"):
                    v.verify_plan(phase, summary, ENGINE)

    def test_observation_and_actual_qualification_claims_are_not_declarations(self):
        for patch in ({"observed_modes": ["none"], "observed_placements": ["cpu"], "evidence_sha256": "b" * 64,
                       "observation_identity": "c" * 64}, {"actual_backend_qualification": True}, {"nonstream_chat": 1}):
            with self.subTest(patch=patch):
                phase, summary = plan_fixture()
                phase["capabilities"].update(patch)
                reseal_plan(phase)
                with self.assertRaises(ValueError):
                    v.verify_plan(phase, summary, ENGINE)

    def test_engine_model_and_phase_identity_are_not_rebindable(self):
        for section, field, value in (("capabilities", "engine_identity", "b" * 64),
                ("demand", "engine_identity", "b" * 64), ("demand", "target_sha256", "b" * 64),
                ("demand", "draft_sha256", "b" * 64)):
            with self.subTest(field=field, section=section):
                phase, summary = plan_fixture("draft")
                phase[section][field] = value
                reseal_plan(phase)
                with self.assertRaises(ValueError):
                    v.verify_plan(phase, summary, ENGINE)
        phase, summary = plan_fixture()
        phase["identity"] = "d" * 64
        with self.assertRaisesRegex(ValueError, "admission-recompute"):
            v.verify_plan(phase, summary, ENGINE)

    def test_stale_configuration_digest_and_nested_budget_copy_rejected(self):
        phase, summary = plan_fixture()
        summary["configuration"]["context"] = phase["demand"]["context_tokens"] = 4096
        summary["configuration_sha256"] = digest(summary["configuration"])
        reseal_plan(phase)  # The old demand config digest must not become valid.
        with self.assertRaisesRegex(ValueError, "demand-config"):
            v.verify_plan(phase, summary, ENGINE)
        phase, summary = plan_fixture()
        phase["admission"]["budget"]["ram_available_bytes"] += 1
        phase["identity"] = digest(phase["admission"])
        with self.assertRaisesRegex(ValueError, "admission-recompute"):
            v.verify_plan(phase, summary, ENGINE)

    def test_unknown_fields_and_unsupported_placement_reject_even_when_resealed(self):
        for section in ("capabilities", "demand", "budget"):
            with self.subTest(section=section):
                phase, summary = plan_fixture()
                phase[section]["unreviewed"] = True
                reseal_plan(phase)
                with self.assertRaises(ValueError):
                    v.verify_plan(phase, summary, ENGINE)
        phase, summary = plan_fixture()
        phase["demand"]["placement"] = "auto"
        reseal_plan(phase)
        with self.assertRaises(ValueError):
            v.verify_plan(phase, summary, ENGINE)


class CleanupReplayTests(unittest.TestCase):
    def test_owned_job_cleanup_confirms_exit_without_backend_stop_claim(self):
        self.assertIsNone(v.verify_cleanup([cleanup_fixture()]))

    def test_each_missing_lifecycle_ack_or_active_child_blocks_success(self):
        row = cleanup_fixture()
        for field in [k for k, value in row.items() if value is True] + ["job_active_processes", "process_error", "job_error"]:
            with self.subTest(field=field):
                changed = copy.deepcopy(row)
                changed[field] = 1 if field == "job_active_processes" else "unconfirmed" if field.endswith("error") else False
                with self.assertRaisesRegex(ValueError, "cleanup-unconfirmed"):
                    v.verify_cleanup([changed])

    def test_bool_pid_active_count_and_promoted_kv_claim_rejected(self):
        for field, value in (("pid", True), ("job_active_processes", False), ("job_assigned", 1),
                             ("kv_release_confirmed", True), ("backend_stop_api_confirmed", True)):
            with self.subTest(field=field):
                row = cleanup_fixture()
                row[field] = value
                with self.assertRaises(ValueError):
                    v.verify_cleanup([row])

    def test_missing_or_duplicate_owner_receipts_rejected(self):
        for rows in ([], [cleanup_fixture(), cleanup_fixture()]):
            with self.subTest(rows=len(rows)):
                with self.assertRaises(ValueError):
                    v.verify_cleanup(rows)


class SpeculationLogTests(unittest.TestCase):
    LINE = "slot print_timing: id 0 | task 7 | draft acceptance = 0.50000 ( 2 accepted / 4 generated), mean len = 1.00000"

    def test_recorded_draft_counters_are_observations_without_mtp_qualification(self):
        value = v.read_speculation((self.LINE + "\n").encode())
        self.assertEqual((value["offered_tokens"], value["accepted_tokens"]), (4, 2))
        self.assertIs(value["actual_speculation_observed"], True)
        self.assertIs(value["native_mtp"], False)
        self.assertIs(value["actual_backend_qualification"], False)
        empty = v.read_speculation(b"fixture baseline with no speculation counters\n")
        self.assertIs(empty["actual_speculation_observed"], False)

    def test_ratio_lies_duplicate_task_and_impossible_acceptance_reject(self):
        for raw in (self.LINE.replace("0.50000", "0.90000"), self.LINE + "\n" + self.LINE,
                    self.LINE.replace("2 accepted", "5 accepted"),
                    self.LINE.replace("0.50000", "nan")):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    v.read_speculation(raw.encode())

    def test_worker_failure_prevents_positive_observation_even_with_accepted_tokens(self):
        value = v.read_speculation((self.LINE + "\nerror: failed to load model\n").encode())
        self.assertEqual(value["accepted_tokens"], 2)
        self.assertTrue(value["errors"])
        self.assertIs(value["actual_speculation_observed"], False)

    def test_invalid_utf8_token_trace_preserves_raw_artifact_and_ascii_counters(self):
        raw = b"spec token fragment: \xea\xb3\n" + self.LINE.encode() + b"\r\ntrace fragment: \xff\n"
        with tempfile.TemporaryDirectory(prefix="cellivane-raw-log-") as directory:
            path = Path(directory) / "worker.log"
            path.write_bytes(raw)
            captured = v.artifact({"path": str(path), "bytes": len(raw), "sha256": sha256(raw).hexdigest()})
            value = v.read_speculation(captured)
            self.assertEqual(captured, raw)
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual((value["offered_tokens"], value["accepted_tokens"]), (4, 2))
            self.assertEqual(value["request_counters"][0]["raw"], self.LINE)
            self.assertEqual(value["parsing_policy"], {"decode": "utf-8", "errors": "replace",
                "counter_lines": "ASCII-only-full-line", "raw_bytes_preserved": True,
                "template_probe_errors": "bounded-context-warning"})

    def test_only_exact_pinned_template_probe_context_is_a_warning(self):
        block = ["\x1b[0mTemplate application failed: ", "------------",
            "While executing CallExpression at line 41, column 134 in source:",
            r"... content.lstrip('\n') ...", "                                           ^",
            "Error: Callee is not a function: got Undefined (hint: 'lstrip')",
            "\x1b[1mcompare_reasoning_scope: Template application failed",
            "\x1b[0mPhase 2: Content analysis", "Phase 3: Tool call analysis"]
        raw = ("\n".join(block) + "\n" + self.LINE).encode()
        observed = v.read_speculation(raw)
        self.assertEqual(observed["errors"], [])
        self.assertEqual(len(observed["warnings"]), 1)
        self.assertIs(observed["actual_speculation_observed"], True)
        for bad in (raw.replace(b"hint: 'lstrip'", b"hint: 'other'"),
                    raw.replace(b"Phase 3: Tool call analysis", b"Phase 3: Failed"),
                    (block[5] + "\n" + self.LINE).encode()):
            with self.subTest(bad=bad):
                result = v.read_speculation(bad)
                self.assertTrue(result["errors"])
                self.assertIs(result["actual_speculation_observed"], False)
        with_fatal = v.read_speculation(raw + b"\nGGML_ASSERT failed\n")
        self.assertEqual(len(with_fatal["warnings"]), 1)
        self.assertTrue(with_fatal["errors"])
        self.assertIs(with_fatal["actual_speculation_observed"], False)

    def test_invalid_fragment_in_counter_line_never_becomes_valid_statistics(self):
        for raw in (self.LINE.encode() + b"\xff", self.LINE.replace("2 accepted", "X accepted").encode().replace(b"X", b"\xff")):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "counter-malformed"):
                v.read_speculation(raw)
        common = (b"spec common_specu: statistics draft-simple: #calls(b,g,a) = 1 2 3, "
            b"#gen drafts = 2, #acc drafts = 1, #gen tokens = 4, #acc tokens = 2\xff")
        value = v.read_speculation(common)
        self.assertEqual(value["implementation_counters"], [])
        self.assertIs(value["actual_speculation_observed"], False)


class CounterBudgetTests(unittest.TestCase):
    def test_phase_totals_have_explicit_eos_and_final_block_slack(self):
        row = {"backend_receipt": {"raw_response": {"usage": {"completion_tokens": 2}}}}
        phase = {"warmup": row, "rows": [row]}
        observed = {"request_counters": [{}, {}], "accepted_tokens": 14, "offered_tokens": 552}
        self.assertIsNone(v.verify_counter_budget(observed, phase, {"max_tokens": 64, "draft_max": 4}))

    def test_unrelated_tasks_and_impossible_totals_are_rejected(self):
        row = {"backend_receipt": {"raw_response": {"usage": {"completion_tokens": 2}}}}
        phase = {"warmup": row, "rows": [row]}
        base = {"request_counters": [{}, {}], "accepted_tokens": 14, "offered_tokens": 552}
        for patch in ({"request_counters": [{}, {}, {}]}, {"accepted_tokens": 15}, {"offered_tokens": 553}):
            with self.subTest(patch=patch), self.assertRaisesRegex(ValueError, "phase-token-budget"):
                v.verify_counter_budget({**base, **patch}, phase, {"max_tokens": 64, "draft_max": 4})


class OwnedMemoryTests(unittest.TestCase):
    def test_available_and_unavailable_records_preserve_measurement_scope(self):
        value = {"scope": "owned-process-working-set-only", "available": True,
            "working_set_bytes": 10, "peak_working_set_bytes": 20,
            "pagefile_bytes": 30, "peak_pagefile_bytes": 40, "gpu_memory_measured": False}
        self.assertIsNone(v.verify_owned_memory(value))
        self.assertIsNone(v.verify_owned_memory({"scope": "owned-process-working-set-only", "available": False}))
        self.assertIsNone(v.verify_owned_memory({"scope": "owned-process-working-set-only", "available": False,
                                               "error": "GetProcessMemoryInfo:5"}))

    def test_false_peak_bool_bytes_and_promoted_tensor_or_gpu_claims_rejected(self):
        original = {"scope": "owned-process-working-set-only", "available": True,
            "working_set_bytes": 10, "peak_working_set_bytes": 20,
            "pagefile_bytes": 30, "peak_pagefile_bytes": 40, "gpu_memory_measured": False}
        for patch in ({"working_set_bytes": 21}, {"peak_pagefile_bytes": 29}, {"working_set_bytes": True},
                      {"gpu_memory_measured": True}, {"scope": "total-system-memory"},
                      {"tensor_residency_managed": True}, {"available": 1}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                v.verify_owned_memory({**original, **patch})
        with self.assertRaises(ValueError):
            v.verify_owned_memory({"scope": "owned-process-working-set-only", "available": False,
                                   "working_set_bytes": 10})


class WireAuditReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cellivane-speculation-verifier-")
        self.addCleanup(self.tmp.cleanup)
        self.fixture = WireFixture(self.tmp.name)

    def test_valid_wire_audit_is_only_synthetic_integrity_evidence(self):
        rows = v.verify_rows(self.fixture.phase, self.fixture.summary)
        self.assertEqual(rows, [{"round": 0, "mode": "none", "case": "fixture-case", "text": "fixture",
                                 "wall_s": .1, "completion_tokens": 2}])

    def test_resealed_wrong_phase_or_configuration_in_audit_rejected(self):
        for field in ("identity", "phase_id", "configuration_sha256", "admission_sha256"):
            with self.subTest(field=field):
                self.fixture.reseal()
                self.fixture.records[0]["value"][field] = "different-recorded-execution"
                self.fixture.write_chain()
                with self.assertRaisesRegex(ValueError, "audit-identity-binding"):
                    v.verify_rows(self.fixture.phase, self.fixture.summary)

    def test_wire_duplicate_nonfinite_or_mismatched_raw_body_rejected(self):
        for raw in ('{"model":"duplicate","model":"duplicate"}', '{"value":NaN}', '{"value":1}'):
            with self.subTest(raw=raw):
                self.fixture.reseal()
                receipt = self.fixture.phase["rows"][0]["backend_receipt"]
                receipt["response_body"] = raw
                receipt["response_sha256"] = sha256(raw.encode()).hexdigest()
                self.fixture.records[5]["value"] = copy.deepcopy(receipt)
                self.fixture.write_chain()
                with self.assertRaises(ValueError):
                    v.verify_rows(self.fixture.phase, self.fixture.summary)

    def test_resealed_length_finish_or_wrong_model_cannot_be_answered(self):
        for field, value in (("finish_reason", "length"), ("model", "other-model")):
            with self.subTest(field=field):
                self.fixture = WireFixture(self.tmp.name)
                raw = self.fixture.phase["rows"][0]["backend_receipt"]["raw_response"]
                if field == "model":
                    raw[field] = value
                else:
                    raw["choices"][0][field] = value
                self.fixture.reseal()
                with self.assertRaises(ValueError):
                    v.verify_rows(self.fixture.phase, self.fixture.summary)

    def test_usage_details_must_be_seed_admissible_before_normalization(self):
        for details in ({"cached_tokens": 9}, {"cached_tokens": 1}, {"cached_tokens": 0, "unknown": 0}, {"cached_tokens": False}):
            with self.subTest(details=details):
                self.fixture = WireFixture(self.tmp.name)
                self.fixture.phase["rows"][0]["backend_receipt"]["raw_response"]["usage"]["prompt_tokens_details"] = details
                self.fixture.reseal()
                with self.assertRaises(ValueError):
                    v.verify_rows(self.fixture.phase, self.fixture.summary)

    def test_cache_execution_is_not_a_computed_backend_exchange(self):
        row = self.fixture.phase["rows"][0]["response"]
        row["body"]["cellivane"]["execution"] = "cache"
        row["raw_response"] = encode(row["body"]).decode()
        with self.assertRaisesRegex(ValueError, "seed-execution-identity"):
            v.verify_rows(self.fixture.phase, self.fixture.summary)

    def test_drop_final_record_and_tampered_chain_are_rejected(self):
        final = Path(self.tmp.name) / "000009.json"
        final.unlink()
        with self.assertRaisesRegex(ValueError, "audit-count-or-terminal"):
            v.verify_rows(self.fixture.phase, self.fixture.summary)
        self.fixture.write_chain()
        path = Path(self.tmp.name) / "000002.json"
        record = json.loads(path.read_bytes())
        record["value"]["backend_calls"] = 99
        path.write_bytes(encode(record))
        with self.assertRaisesRegex(ValueError, "audit-chain"):
            v.verify_rows(self.fixture.phase, self.fixture.summary)

    def test_imports_do_not_use_runtime_or_producer_validators(self):
        tree = ast.parse((TOOLS / "verify_cellivane_speculation.py").read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        self.assertFalse(any(name.startswith("hosted.") or name.startswith("cellivane_seed_")
                             or name == "cellivane_llama_assets" for name in modules))


if __name__ == "__main__":
    unittest.main()
