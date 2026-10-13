#!/usr/bin/env python3
"""Independent replay of a recorded, single-server adaptive CPU smoke run.

Only independent evidence readers are imported. Recorded hashes and owned
cleanup receipts are integrity evidence, not process attestation, a RAM
reservation, current-host calibration, or a future performance guarantee.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from uuid import UUID
import zipfile

import verify_cellivane_speculation as spec
from verify_cellivane_comparison import (audit_records, check, digest, encoded,
    exchange, hex_digest, integer, keys, load, number, raw_file, safe_path, same,
    text as plain_text)

GIB = 1024**3
SOURCE_PATHS = spec.SOURCE_PATHS | {
    "hosted/experimental/cellivane_seed/adaptive.py",
    "tools/hosted/cellivane_seed_adaptive.py",
    *{"tools/hosted/" + n + ".py" for n in (
        "verify_cellivane_speculation", "verify_cellivane_comparison", "verify_cellivane_bridge")},
}
SETTINGS = ("profile", "threads", "context", "slots", "cache_entries", "max_tokens",
    "temperature", "seed", "no_think", "reasoning_format", "draft_max", "draft_min",
    "draft_sampling", "cache_prompt")
CONFIGURATION = {"schema_version": 1, "alias": "cellivane-spec-target", "profile": "cpu",
    "threads": 4, "context": 2048, "slots": 1, "cache_entries": 0, "max_tokens": 128,
    "temperature": 0, "seed": 42, "no_think": {"enable_thinking": False},
    "reasoning_format": "auto", "repeat": 1, "draft_max": 4, "draft_min": 0,
    "draft_sampling": "greedy", "cache_prompt": False}
CASES = [
    {"id": "fixed-list", "prompt": "Write exactly the numbers 1 through 20 in order, separated by commas. No title or explanation."},
    {"id": "short-code", "prompt": "Write only a Python function named square_sum(xs) that returns the sum of the squares in xs. Use exactly three short lines, no Markdown or explanation."},
    {"id": "korean", "prompt": "고양이를 처음 돌보는 사람에게 세 가지 조언을 한국어로 써 줘. 조언마다 한 문장, 모두 합쳐 80자 이내. 제목이나 맺음말 없이 세 줄만 써 줘."},
    {"id": "copy-lines", "prompt": "Copy the next sentence exactly four times, one copy per line, then stop. No title or explanation. Sentence: The quiet garden is bright today."},
]


def absolute_path(value):
    check(type(value) is str and Path(value).is_absolute(), "absolute-local-path-required")
    return safe_path(value)


def verify_models(models):
    check(type(models) is dict and set(models) in ({"target"}, {"target", "draft"}), "model-fields")
    for kind, row in models.items():
        keys(row, ("path", "bytes", "sha256"), "model-record-fields")
        pin = spec.MODEL_PINS[kind]
        check(type(row["bytes"]) is int and (row["bytes"], row["sha256"]) == pin, "model-pin")
        path = absolute_path(row["path"])
        check(path.suffix.lower() == ".gguf" and path.is_file() and path.stat().st_size == pin[0]
              and spec.file_hash(path) == pin[1], "model-file-changed")


def verify_sources(summary, source_root):
    sources = summary["source_sha256"]
    check(type(sources) is dict and set(sources) == SOURCE_PATHS
          and all(hex_digest(value) for value in sources.values()), "source-closure")
    snapshot = absolute_path(summary["source_snapshot"])
    root = absolute_path(str(source_root)) if source_root is not None else snapshot
    check(root.is_dir(), "source-directory")
    for name, expected in sources.items():
        parts = PurePosixPath(name).parts
        check(spec.file_hash(root.joinpath(*parts)) == expected, "source-content-changed")
    return root


def verify_calibration(value):
    if value is None:
        return None
    keys(value, ("report", "source_root", "replay", "replay_sha256"), "calibration-validation-fields")
    absolute_path(value["report"]["path"])
    source = absolute_path(value["source_root"])
    raw = spec.artifact(value["report"], maximum=8 * 1024**2)
    report = load(raw)
    # A supplied verdict, even one with a consistent digest, is not evidence.
    verdict = spec.replay(value["report"]["path"], str(source))
    check(verdict["outcome"] == "PASS" and verdict["source_files_checked"] is True
          and same(verdict, value["replay"]) and digest(verdict) == value["replay_sha256"],
          "calibration-independent-replay-mismatch")
    check(spec.artifact(value["report"], maximum=8 * 1024**2) == raw, "calibration-changed-during-replay")
    ratio = verdict["median_paired_wall_speed_ratio"]
    check(integer(verdict["paired_total"], 1, 4097)
          and integer(verdict["paired_text_equal"], 0, verdict["paired_total"] + 1)
          and number(ratio) and 0 < ratio <= 100 and type(verdict["speculation_observed"]) is bool,
          "calibration-result-types")
    result = {"schema_version": 1, "engine_identity": report["backend_assets"]["engine_identity"],
        "target_sha256": report["models"]["target"]["sha256"],
        "draft_sha256": report["models"]["draft"]["sha256"],
        "settings_sha256": digest({name: report["configuration"][name] for name in SETTINGS}),
        "report_sha256": value["report"]["sha256"], "replay_sha256": digest(verdict),
        "paired_total": verdict["paired_total"], "paired_text_equal": verdict["paired_text_equal"],
        "median_wall_ratio": ratio, "speculation_observed": verdict["speculation_observed"],
        "scope": "advisory-historical-calibration-not-current-host-qualification"}
    check(all(hex_digest(result[name]) for name in ("engine_identity", "target_sha256", "draft_sha256",
        "settings_sha256", "report_sha256", "replay_sha256")), "calibration-identity-types")
    return result


def verify_admission(plan, summary, engine_identity, mode):
    keys(plan, ("schema_version", "outcome", "reason", "capabilities", "demand", "budget",
        "require_observed", "ram_required_bytes", "vram_required_bytes", "scope",
        "os_memory_limit_enforced", "tensor_residency_managed", "measured_peak_verified",
        "actual_backend_qualification"), "admission-fields")
    caps, demand, budget = (plan[name] for name in ("capabilities", "demand", "budget"))
    expected_caps = {"schema_version": 1, "backend": "llama.cpp", "engine_identity": engine_identity,
        "declared_modes": ["draft", "none"], "declared_placements": ["cpu"],
        "observed_modes": [], "observed_placements": [], "evidence_sha256": None,
        "nonstream_chat": True, "observation_identity": None,
        "scope": "caller-declarations-and-evidence-labels", "actual_backend_qualification": False}
    check(same(caps, expected_caps), "capability-declaration-not-observation")
    target = summary["models"]["target"]
    draft = summary["models"].get("draft") if mode == "draft" else None
    check(mode != "draft" or draft is not None, "draft-model-missing")
    expected_demand = {"schema_version": 1, "engine_identity": engine_identity,
        "target_sha256": target["sha256"], "target_bytes": target["bytes"],
        "draft_sha256": draft["sha256"] if draft else None, "draft_bytes": draft["bytes"] if draft else 0,
        "configuration_sha256": digest({"configuration_sha256": summary["configuration_sha256"], "mode": mode}),
        "mode": mode, "placement": "cpu", "context_tokens": 2048,
        "host_overhead_bytes": 2 * GIB, "context_workspace_bytes": 512 * 1024**2,
        "gpu_required_bytes": None, "gpu_estimate_sha256": None,
        "scope": "declared-files-and-total-runtime-workspace-estimate"}
    check(same(demand, expected_demand), "demand-configuration-or-workspace")
    keys(budget, ("schema_version", "ram_available_bytes", "ram_headroom_bytes",
        "vram_available_bytes", "vram_headroom_bytes", "scope"), "budget-fields")
    available = budget["ram_available_bytes"]
    check(integer(available) and same(budget, {"schema_version": 1, "ram_available_bytes": available,
        "ram_headroom_bytes": min(4 * GIB, available), "vram_available_bytes": 0,
        "vram_headroom_bytes": 0, "scope": "caller-supplied-available-byte-snapshot"}), "budget-allowances")
    required = target["bytes"] + (draft["bytes"] if draft else 0) + 2 * GIB + 512 * 1024**2
    check(integer(required), "ram-sum-overflow")
    fits = required <= available - budget["ram_headroom_bytes"]
    expected = {"schema_version": 1, "outcome": "ADMITTED" if fits else "BLOCKED",
        "reason": None if fits else "ram-budget-insufficient", "capabilities": caps, "demand": demand,
        "budget": budget, "require_observed": False, "ram_required_bytes": required,
        "vram_required_bytes": 0, "scope": "declared-budget-preflight-only",
        "os_memory_limit_enforced": False, "tensor_residency_managed": False,
        "measured_peak_verified": False, "actual_backend_qualification": False}
    check(same(plan, expected), "admission-independent-recompute")
    return plan


def verify_choice(summary, engine_identity, calibration):
    choice = summary["choice"]
    keys(choice, ("schema_version", "policy", "selected_mode", "outcome", "reason", "expected_output_tokens",
        "settings_sha256", "maximum_output_tokens", "baseline", "draft", "calibration", "scope",
        "per_request_switching", "automatic_retry", "current_host_speedup_guaranteed"), "choice-fields")
    check(choice["policy"] in ("auto", "none", "draft") and integer(choice["expected_output_tokens"], 1, 129)
          and same(choice["calibration"], calibration), "choice-policy-or-calibration")
    baseline = verify_admission(choice["baseline"], summary, engine_identity, "none")
    draft = choice["draft"]
    check((draft is not None) == ("draft" in summary["models"]), "choice-draft-model-join")
    if draft is not None:
        verify_admission(draft, summary, engine_identity, "draft")
        check(same(baseline["budget"], draft["budget"]), "choice-budget-mismatch")
    settings = digest({name: summary["configuration"][name] for name in SETTINGS})
    policy = choice["policy"]
    if baseline["outcome"] != "ADMITTED":
        mode, reason = None, "baseline-" + baseline["outcome"].lower()
    elif policy == "none":
        mode, reason = "none", "explicit-none"
    elif draft is None or draft["outcome"] != "ADMITTED":
        mode = None if policy == "draft" else "none"
        reason = "draft-missing" if draft is None else "draft-" + draft["outcome"].lower()
    elif policy == "draft":
        mode, reason = "draft", "explicit-draft"
    elif choice["expected_output_tokens"] < 64:
        mode, reason = "none", "short-output-profile"
    elif calibration is None:
        mode, reason = "none", "calibration-missing"
    elif tuple(calibration[name] for name in ("engine_identity", "target_sha256", "draft_sha256", "settings_sha256")) != (
            engine_identity, summary["models"]["target"]["sha256"], summary["models"]["draft"]["sha256"], settings):
        mode, reason = "none", "calibration-identity-mismatch"
    elif calibration["paired_total"] < 8 or calibration["paired_text_equal"] != calibration["paired_total"] \
            or not calibration["speculation_observed"] or calibration["median_wall_ratio"] < 1.05:
        mode, reason = "none", "calibration-benefit-not-established"
    else:
        mode, reason = "draft", "calibrated-advisory-benefit"
    expected = {"schema_version": 1, "policy": policy, "selected_mode": mode,
        "outcome": "ADMITTED" if mode is not None else "BLOCKED", "reason": reason,
        "expected_output_tokens": choice["expected_output_tokens"], "settings_sha256": settings,
        "maximum_output_tokens": 128, "baseline": baseline, "draft": draft, "calibration": calibration,
        "scope": "startup-selection-only", "per_request_switching": False,
        "automatic_retry": False, "current_host_speedup_guaranteed": False}
    check(same(choice, expected) and summary["choice_sha256"] == digest(expected), "choice-independent-recompute")
    return baseline if mode == "none" else draft if mode == "draft" else None


def adaptive_status(summary):
    choice = summary["choice"]
    return {"policy": choice["policy"], "mode": choice["selected_mode"], "reason": choice["reason"],
        "expected_output_tokens": choice["expected_output_tokens"], "maximum_output_tokens": 128,
        "choice_sha256": summary["choice_sha256"], "per_request_switching": False}


def audit_status(directory, records):
    session = Path(directory).name
    check(str(UUID(session)) == session, "audit-session-uuid")
    size = sum(len(encoded(record)) for record in records)
    count = len(records)
    return {"session_id": session, "records": count, "bytes": size, "record_limit": 10000,
        "byte_limit": 64 * 1024**2, "remaining_records": 10000 - count,
        "remaining_bytes": 64 * 1024**2 - size, "warning_threshold_percent": 80,
        "near_capacity": count * 100 >= 10000 * 80 or size * 100 >= 64 * 1024**2 * 80,
        "poisoned": False}


def verify_rows(phase, summary):
    """Replay exactly five owners and one rejected request, with no hidden submit."""
    config, records = summary["configuration"], audit_records(phase["audit_directory"])
    check(type(phase["rows"]) is list and len(phase["rows"]) == 4
          and len(records) == 19, "smoke-audit-coverage")
    check(records[0]["kind"] == "adaptive_configuration" and same(records[0]["value"], {
        "choice_sha256": summary["choice_sha256"], "configuration_sha256": summary["configuration_sha256"],
        "mode": phase["mode"], "admission_sha256": phase["identity"]}), "audit-choice-binding")
    counters, sequence, ids, outputs = dict.fromkeys(spec.COUNTERS, 0), 0, set(), []

    def event(record, kind, key=None, value=None):
        nonlocal sequence
        sequence += 1
        reason = {"reject": "invalid-request", "close": "engine-closed"}.get(kind)
        expected = {**counters, "seq": sequence, "kind": kind, "key": key,
            "execution": "computed" if kind in ("start", "finish") else None, "reason": reason,
            "response_sha256": digest(value) if value is not None else None,
            "active": 1 if kind == "start" else 0, "waiters": 0, "cache_entries": 0, "cache_bytes": 0}
        check(record["kind"] == "scheduler" and same(record["value"], expected), "scheduler-transition-or-counters")

    for index, row in enumerate([phase["warmup"], *phase["rows"]]):
        keys(row, ("response", "wall_s", "text", "backend_receipt", "backend_exchange_count",
            *(["case"] if index else [])), "computed-row-fields")
        response = row["response"]
        exchange(response)
        check(response["status"] == 200 and number(row["wall_s"]) and row["wall_s"] > 0
              and type(row["backend_exchange_count"]) is int and row["backend_exchange_count"] == 1,
              "computed-row-required")
        prompt = CASES[index - 1]["prompt"] if index else "Reply with only OK."
        request = {"model": config["alias"], "messages": [
            {"role": "system", "content": spec.SYSTEM}, {"role": "user", "content": prompt}],
            "temperature": 0, "max_tokens": 128, "stream": False}
        check(same(response["request"], request) and (not index or row["case"] == CASES[index - 1]["id"]), "request-corpus-or-settings")
        key = digest({"identity": phase["identity"], "inference": request})
        start, backend, finish = records[1 + 3 * index:4 + 3 * index]
        counters["backend_calls"] += 1
        event(start, "start", key)
        receipt = row["backend_receipt"]
        check(backend["kind"] == "backend_exchange" and same(backend["value"], receipt)
              and receipt["name"] == "llama-cpp-tensor", "backend-audit-binding")
        spec.bridge.result(receipt, {"name": "llama-cpp-tensor", "kind": "openai",
            "model": config["alias"], "max_tokens": 128}, {"messages": request["messages"]})
        check(receipt["outcome"] == "ANSWERED" and row["text"] == receipt["text"]
              and plain_text(receipt["text"]), "backend-not-answered")
        raw = receipt["raw_response"]
        check(not any(name in raw for name in ("error", "thinking", "reasoning", "reasoning_content", "tool_calls")), "unsupported-answer-content")
        usage = raw["usage"]
        keys(usage, ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_tokens_details"), "usage-fields")
        check(all(integer(usage[name]) for name in ("prompt_tokens", "completion_tokens", "total_tokens"))
              and usage["completion_tokens"] <= 128
              and same(usage["prompt_tokens_details"], {"cached_tokens": 0}), "usage-or-prompt-cache")
        value = {"model": raw["model"], "choices": [{"index": 0,
            "message": {"role": "assistant", "content": receipt["text"]}, "finish_reason": "stop"}],
            "usage": {name: usage[name] for name in ("prompt_tokens", "completion_tokens", "total_tokens")}}
        counters["computed"] += 1
        event(finish, "finish", key, value)
        body = response["body"]
        keys(body, ("id", "object", "created", "model", "choices", "usage", "cellivane"), "seed-envelope-fields")
        check(same({name: body[name] for name in ("model", "choices", "usage")}, value)
              and body["object"] == "chat.completion" and integer(body["created"]), "seed-response-binding")
        check(str(UUID(body["id"])) == body["id"] and body["id"] not in ids, "seed-response-uuid")
        ids.add(body["id"])
        check(same(body["cellivane"], {"engine": "seed", "version": "0.1.0", "execution": "computed",
            "key": key, "backend_calls": counters["backend_calls"]}), "seed-execution-binding")
        if index:
            outputs.append({"case": row["case"], "wall_s": row["wall_s"], "completion_tokens": usage["completion_tokens"]})
    oversized = phase["oversize_response"]
    exchange(oversized)
    expected_request = {"model": config["alias"], "messages": [
        {"role": "system", "content": spec.SYSTEM},
        {"role": "user", "content": "This must be rejected before backend dispatch."}],
        "temperature": 0, "max_tokens": 129, "stream": False}
    check(oversized["status"] == 400 and same(oversized["request"], expected_request)
          and same(oversized["body"], {"error": {"message": "invalid-request"}}), "oversize-before-dispatch")
    counters["rejected"] += 1
    event(records[16], "reject")
    state = {"engine": "seed", "version": "0.1.0", "model": config["alias"], "identity": phase["identity"],
        "closed": False, "poisoned": False, "slots": 1, "active": 0, "inflight": 0, "waiters": 0,
        "max_waiters": 8, "cache_entries": 0, "cache_bytes": 0, "cache_entries_limit": 0,
        "cache_bytes_limit": 1048576, **counters, "event_seq": sequence, "adaptive": adaptive_status(summary)}
    status = phase["status_response"]
    exchange(status)
    expected_status = {**state, "tensor_backend": "external-openai-subset", "token_mtp": False,
        "kv_sharing": False, "canonical_main_binding": False, "audit": audit_status(phase["audit_directory"], records[:17])}
    check(status["status"] == 200 and status["request"] is None and same(status["body"], expected_status), "status-before-shutdown")
    event(records[17], "close")
    state.update(closed=True, event_seq=sequence)
    check(same(phase["state"], state) and records[18]["kind"] == "shutdown"
          and same(records[18]["value"], state), "shutdown-state-or-counters")
    check(same(phase["audit_status"], audit_status(phase["audit_directory"], records)), "audit-committed-usage")
    return outputs


def verify_inspection(assets):
    inspection = assets["inspection"]
    keys(inspection, ("help", "version", "commands", "cleanup"), "inspection-fields")
    help_text = spec.artifact(inspection["help"], maximum=1024**2).decode("utf-8")
    version = spec.artifact(inspection["version"], maximum=1024**2).decode("utf-8")
    check("version: 0.6.0-dev (build 11429, commit d81235049)" in version
          and all(flag in help_text for flag in ("--spec-type none,draft-simple", "--no-cache-prompt", "--offline",
              "--spec-draft-model", "--spec-draft-sampling", "--chat-template-kwargs")), "backend-help-or-version")
    check(same(inspection["commands"], [[assets["server"], "--help"], [assets["server"], "--version"]]), "inspection-commands")
    spec.verify_cleanup(inspection["cleanup"])


def replay(summary_path, source_root=None):
    summary = load(raw_file(summary_path, 32 * 1024**2))
    keys(summary, ("schema_version", "experiment", "run_id", "configuration", "configuration_sha256",
        "models", "backend_assets", "source_sha256", "source_snapshot", "calibration_validation", "choice",
        "choice_sha256", "phase", "launch_mode", "outcome", "error"), "summary-fields")
    check(type(summary["schema_version"]) is int and summary["schema_version"] == 1
          and summary["experiment"] == "cellivane-seed-adaptive" and summary["launch_mode"] == "smoke"
          and summary["outcome"] == "COMPLETED" and summary["error"] is None, "adaptive-smoke-incomplete")
    check(str(UUID(summary["run_id"])) == summary["run_id"], "run-uuid")
    check(same(summary["configuration"], CONFIGURATION)
          and summary["configuration_sha256"] == digest(CONFIGURATION), "configuration-binding")
    verify_sources(summary, source_root)
    verify_models(summary["models"])
    identity = spec.verify_assets(summary["backend_assets"])
    verify_inspection(summary["backend_assets"])
    calibration = verify_calibration(summary["calibration_validation"])
    selected = verify_choice(summary, identity, calibration)
    check(selected is not None and selected["outcome"] == "ADMITTED", "selected-plan-not-admitted")
    phase = summary["phase"]
    keys(phase, ("phase_id", "round", "mode", "identity", "capabilities", "budget", "demand", "admission",
        "memory_snapshot", "command", "environment", "audit_directory", "warmup", "rows", "state", "cleanup",
        "ports_closed", "worker_log", "speculation_observed", "owned_memory", "source_unchanged_during_run",
        "models_unchanged_during_run", "backend_assets_unchanged_during_run", "status_response",
        "oversize_response", "audit_status", "outcome", "error"), "phase-fields")
    check(phase["mode"] == summary["choice"]["selected_mode"]
          and phase["phase_id"] == "adaptive-" + phase["mode"] and type(phase["round"]) is int and phase["round"] == 0
          and phase["outcome"] == "COMPLETED" and phase["error"] is None
          and phase["identity"] == digest(selected) and same(phase["admission"], selected)
          and all(same(phase[name], selected[name]) for name in ("capabilities", "budget", "demand")), "phase-choice-binding")
    spec.verify_plan(phase, summary, identity)
    spec.verify_command(phase, summary)
    spec.verify_cleanup([phase["cleanup"]])
    spec.verify_owned_memory(phase["owned_memory"])
    check(all(phase[name] is True for name in ("source_unchanged_during_run", "models_unchanged_during_run",
        "backend_assets_unchanged_during_run")), "run-artifacts-changed")
    check(type(phase["ports_closed"]) is dict and len(phase["ports_closed"]) == 2
          and all(re.fullmatch(r"[0-9]{4,5}", port) and 1024 <= int(port) <= 65535 and closed is True
                  for port, closed in phase["ports_closed"].items()), "port-cleanup")
    rows = verify_rows(phase, summary)
    observed = spec.read_speculation(spec.artifact(phase["worker_log"], maximum=8 * 1024**2))
    check(same(observed, phase["speculation_observed"]) and not observed["errors"], "worker-log-recompute")
    check(phase["mode"] != "none" or not observed["draft_attempt_observed"], "unexpected-baseline-speculation")
    spec.verify_counter_budget(observed, phase, CONFIGURATION)
    return {"outcome": "PASS", "scope": "recorded-adaptive-startup-integrity-and-semantic-replay",
        "source_files_checked": True, "source_file_count": len(SOURCE_PATHS), "selected_mode": phase["mode"],
        "reason": summary["choice"]["reason"], "choice_sha256": summary["choice_sha256"],
        "computed_responses": 5, "rejected_before_dispatch": 1, "audit_records": 19,
        "calibration_replayed": calibration is not None, "calibration_scope": "advisory-historical-not-current-host-qualification",
        "actual_speculation_observed": observed["actual_speculation_observed"],
        "draft_offered_tokens": observed["offered_tokens"], "draft_accepted_tokens": observed["accepted_tokens"],
        "corpus_wall_s": sum(row["wall_s"] for row in rows),
        "timing_scope": "four successful computed corpus HTTP exchanges including prefill, generation and synchronous audit; startup and warmup excluded; advisory small corpus",
        "native_mtp": False, "current_host_speedup_guaranteed": False, "per_request_switching": False,
        "endpoint_pid_attestation": False, "actual_backend_qualification": False,
        "tensor_residency_managed": False, "os_memory_limit_enforced": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary")
    parser.add_argument("--source-root")
    args = parser.parse_args(argv)
    try:
        result = replay(args.summary, args.source_root)
    except (ValueError, KeyError, TypeError, OSError, OverflowError, RecursionError, zipfile.BadZipFile) as exc:
        result = {"outcome": "FAIL", "reason": str(exc), "native_mtp": False,
            "current_host_speedup_guaranteed": False, "per_request_switching": False, "endpoint_pid_attestation": False}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["outcome"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
