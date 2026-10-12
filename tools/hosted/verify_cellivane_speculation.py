#!/usr/bin/env python3
"""Independent replay of a recorded CPU draft/target experiment.

Imports independent wire/audit readers, never runtime or producer validators.
PASS concerns recorded integrity and bounded-run semantics, not live process
attestation, general model quality, native MTP or a portable speed guarantee.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
from pathlib import Path, PurePosixPath
import re
import statistics
from uuid import UUID
import zipfile

import verify_cellivane_bridge as bridge
from verify_cellivane_comparison import (audit_records, check, digest, encoded,
    exchange, hex_digest, integer, keys, load, number, raw_file, safe_path, same, text as plain_text, wire)

MODEL_PINS = {
    "target": (4280404704, "8c2f07f26af9747e41988551106f149b03eb9b5cb6df636027b6bf6278473300"),
    "draft": (639446688, "9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031"),
}
ARCHIVE_SHA = "1283323272b04cd07905816a597a0da810918102de958f4ff6f7bbaa70ed2efe"
COMMIT = "d81235049384534c167caea52b85a694f6103d14"
COUNTERS = ("backend_calls", "computed", "cache_hits", "coalesced", "failures", "rejected", "timeouts")
SOURCE_PATHS = {"hosted/experimental/cellivane_bridge/" + n + ".py" for n in
    ("__init__", "adapters", "contracts", "runtime", "storage")} | {
    "hosted/experimental/cellivane_seed/" + n + ".py" for n in
    ("__init__", "core", "server", "capabilities", "resources")} | {
    "tools/hosted/" + n + ".py" for n in
    ("cellivane_bridge_lab", "cellivane_llama_assets", "cellivane_seed_speculate")}
TIMING_SCOPE = "successful-computed-corpus-responses-only; warmup excluded; Seed cache and backend prompt reuse disabled"
CORPUS_SHA = "2acf80190a26c5a9d597ce2205dd0066f6446035fa4436c6005ab0f6641c4a95"
SYSTEM = "Follow the requested output format. Give only the requested answer. /no_think"


def file_hash(path):
    path = safe_path(path)
    check(path.is_file(), "artifact-file-missing")
    h = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024**2), b""):
            h.update(chunk)
    return h.hexdigest()


def artifact(record, *, maximum=16 * 1024**2):
    keys(record, ("path", "bytes", "sha256"), "artifact-fields")
    check(integer(record["bytes"], 0, maximum + 1) and hex_digest(record["sha256"]), "artifact-size-or-digest")
    raw = raw_file(record["path"], maximum)
    check(len(raw) == record["bytes"] and sha256(raw).hexdigest() == record["sha256"], "artifact-content-changed")
    return raw


def verify_assets(value):
    check(value["schema_version"] == 1 and type(value["schema_version"]) is int
          and value["backend"] == "llama.cpp" and value["stable_release"] == "v0.6.0"
          and value["release_tag"] == "b11429" and value["source_commit"] == COMMIT
          and value["archive_bytes"] == 19398918 and type(value["archive_bytes"]) is int
          and value["archive_sha256"] == ARCHIVE_SHA, "backend-pin")
    check(value["release_id"] == 403936548 and type(value["release_id"]) is int
          and value["asset_id"] == 613152134 and type(value["asset_id"]) is int
          and value["asset_name"] == "llama-b11429-bin-win-cpu-x64.zip"
          and value["asset_url"] == "https://github.com/ggml-org/llama.cpp/releases/download/b11429/llama-b11429-bin-win-cpu-x64.zip"
          and value["stable_pointer_sha256"] == "5677c5a4561fad44f2a58fc14187dd9cde253874584e958b37094c723ea8e8a5", "backend-release-identity")
    directory = safe_path(value["directory"])
    archive = directory.parent / "downloads" / value["asset_name"]
    check(archive.stat().st_size == value["archive_bytes"] and file_hash(archive) == ARCHIVE_SHA, "backend-archive")
    expected = {}
    with zipfile.ZipFile(archive) as package:
        for item in package.infolist():
            if item.is_dir():
                continue
            parts = PurePosixPath(item.filename).parts
            check(parts and not PurePosixPath(item.filename).is_absolute() and ".." not in parts
                  and all(":" not in p for p in parts), "backend-member-path")
            check(item.filename not in expected, "backend-duplicate-member")
            expected[item.filename] = sha256(package.read(item)).hexdigest()
            target = directory.joinpath(*parts)
            check(target.stat().st_size == item.file_size and file_hash(target) == expected[item.filename], "backend-member-changed")
    check(same(expected, value["member_sha256"]), "backend-member-manifest")
    actual = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()}
    check(actual == set(expected) | {"cellivane-assets.json"}, "backend-extra-or-missing-member")
    binaries = {k: v for k, v in expected.items() if PurePosixPath(k).suffix.lower() in (".exe", ".dll")}
    check(same(binaries, value["binary_sha256"]) and value["binary_tree_sha256"] == digest(binaries), "backend-binary-tree")
    identity = digest({"backend": "llama.cpp", "commit": COMMIT, "archive_sha256": ARCHIVE_SHA, "binary_sha256": binaries})
    check(identity == value["engine_identity"] and safe_path(value["server"]).parent == directory
          and Path(value["server"]).name == "llama-server.exe", "backend-engine-identity")
    return identity


def verify_plan(phase, summary, engine_identity):
    cap, demand, budget, plan = (phase[k] for k in ("capabilities", "demand", "budget", "admission"))
    keys(cap, ("schema_version", "backend", "engine_identity", "declared_modes", "declared_placements",
        "observed_modes", "observed_placements", "evidence_sha256", "nonstream_chat", "observation_identity",
        "scope", "actual_backend_qualification"), "capability-fields")
    check(cap["backend"] == "llama.cpp" and cap["engine_identity"] == engine_identity
          and cap["schema_version"] == 1 and type(cap["schema_version"]) is int
          and cap["nonstream_chat"] is True and cap["actual_backend_qualification"] is False
          and cap["scope"] == "caller-declarations-and-evidence-labels", "capability-scope")
    for field, allowed in (("declared_modes", {"none", "draft", "ngram"}), ("declared_placements", {"cpu", "hybrid", "auto"})):
        check(type(cap[field]) is list and all(type(x) is str for x in cap[field])
              and cap[field] == sorted(set(cap[field])) and set(cap[field]) <= allowed, "capability-declaration")
    check(phase["mode"] in cap["declared_modes"] and "cpu" in cap["declared_placements"]
          and cap["observed_modes"] == [] and cap["observed_placements"] == []
          and cap["evidence_sha256"] is None and cap["observation_identity"] is None, "capability-not-observation")
    keys(demand, ("schema_version", "engine_identity", "target_sha256", "target_bytes", "draft_sha256", "draft_bytes",
        "configuration_sha256", "mode", "placement", "context_tokens", "host_overhead_bytes", "context_workspace_bytes",
        "gpu_required_bytes", "gpu_estimate_sha256", "scope"), "demand-fields")
    check(demand["schema_version"] == 1 and type(demand["schema_version"]) is int
          and demand["engine_identity"] == engine_identity and demand["placement"] == "cpu"
          and demand["mode"] == phase["mode"] and demand["context_tokens"] == summary["configuration"]["context"]
          and integer(demand["context_tokens"], 1, 2**20 + 1)
          and demand["scope"] == "declared-files-and-total-runtime-workspace-estimate", "demand-binding")
    check((demand["target_bytes"], demand["target_sha256"]) == MODEL_PINS["target"], "target-demand")
    draft_expected = MODEL_PINS["draft"] if phase["mode"] == "draft" else (0, None)
    check((demand["draft_bytes"], demand["draft_sha256"]) == draft_expected
          and (demand["gpu_required_bytes"] is None or type(demand["gpu_required_bytes"]) is int and demand["gpu_required_bytes"] == 0)
          and demand["gpu_estimate_sha256"] is None, "draft-demand")
    check(demand["configuration_sha256"] == digest({"configuration_sha256": summary["configuration_sha256"], "mode": phase["mode"]}), "demand-config")
    keys(budget, ("schema_version", "ram_available_bytes", "ram_headroom_bytes", "vram_available_bytes", "vram_headroom_bytes", "scope"), "budget-fields")
    check(type(budget["schema_version"]) is int and budget["schema_version"] == 1
          and budget["scope"] == "caller-supplied-available-byte-snapshot", "budget-scope")
    for field in ("ram_available_bytes", "ram_headroom_bytes", "vram_available_bytes", "vram_headroom_bytes"):
        check(integer(budget[field]), "budget-byte-count")
    check(budget["ram_headroom_bytes"] <= budget["ram_available_bytes"]
          and budget["vram_headroom_bytes"] <= budget["vram_available_bytes"], "budget-headroom")
    for field in ("target_bytes", "draft_bytes", "host_overhead_bytes", "context_workspace_bytes", "context_tokens"):
        check(integer(demand[field]), "demand-integer")
    required = sum(demand[k] for k in ("target_bytes", "draft_bytes", "host_overhead_bytes", "context_workspace_bytes"))
    check(required < 2**63 and required <= budget["ram_available_bytes"] - budget["ram_headroom_bytes"], "admission-ram-fit")
    expected = {"schema_version": 1, "outcome": "ADMITTED", "reason": None, "capabilities": cap,
        "demand": demand, "budget": budget, "require_observed": False, "ram_required_bytes": required,
        "vram_required_bytes": 0, "scope": "declared-budget-preflight-only", "os_memory_limit_enforced": False,
        "tensor_residency_managed": False, "measured_peak_verified": False, "actual_backend_qualification": False}
    check(same(plan, expected) and phase["identity"] == digest(expected), "admission-recompute")


def verify_cleanup(rows):
    check(type(rows) is list and rows, "cleanup-missing")
    seen = set()
    for row in rows:
        check(integer(row.get("pid"), 1) and row["pid"] not in seen, "cleanup-pid")
        seen.add(row["pid"])
        check(row.get("method") == "owned-job-terminate-and-close"
              and all(row.get(k) is True for k in ("created_suspended", "assigned_before_resume", "primary_thread_closed",
                  "process_handle_closed", "process_exited", "job_assigned", "job_terminated", "job_closed", "job_cleanup_confirmed"))
              and type(row.get("job_active_processes")) is int and row["job_active_processes"] == 0
              and row.get("job_error") is None and row.get("process_error") is None
              and row.get("backend_stop_api_confirmed") is False and row.get("kv_release_confirmed") is False,
              "cleanup-unconfirmed")


def verify_command(phase, summary):
    config, backend, models = (summary[k] for k in ("configuration", "backend_assets", "models"))
    command = phase["command"]
    check(type(command) is list and len(command) < 100 and all(type(x) is str for x in command), "command-bound")
    check("--port" in command and command.count("--port") == 1, "command-port")
    port = command[command.index("--port") + 1]
    check(port in phase["ports_closed"], "command-port-join")
    expected = [backend["server"], "--model", models["target"]["path"], "--alias", config["alias"],
        "--host", "127.0.0.1", "--port", port, "--threads", "4", "--threads-batch", "4",
        "--threads-http", "2", "--parallel", "1", "--ctx-size", "2048", "--batch-size", "512",
        "--ubatch-size", "128", "--gpu-layers", "0", "--fit", "off", "--temp", "0", "--seed", "42",
        "--n-predict", str(config["max_tokens"]), "--jinja", "--reasoning", "off", "--reasoning-format", "auto",
        "--chat-template-kwargs", '{"enable_thinking":false}', "--no-cache-prompt", "--cache-ram", "0",
        "--no-cache-idle-slots", "--no-context-shift", "--offline", "--no-webui", "--metrics", "--log-colors", "off",
        "--log-verbosity", "5", "--no-log-prefix", "--no-log-timestamps", "--spec-type",
        "none" if phase["mode"] == "none" else "draft-simple"]
    if phase["mode"] == "draft":
        expected += ["--spec-draft-model", models["draft"]["path"], "--spec-draft-threads", "4",
            "--spec-draft-threads-batch", "4", "--spec-draft-ngl", "0", "--spec-draft-n-max", "4",
            "--spec-draft-n-min", "0", "--spec-draft-sampling", "greedy"]
    check(same(command, expected), "command-settings-changed")
    check(same(phase["environment"], {k: "-1" for k in ("CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES",
        "ROCR_VISIBLE_DEVICES", "GGML_VK_VISIBLE_DEVICES")}), "device-environment")
    snapshot = phase["memory_snapshot"]
    check(snapshot["source"] == "GlobalMemoryStatusEx" and snapshot["scope"] == "host-available-physical-memory-snapshot"
          and integer(snapshot["available_bytes"]) and integer(snapshot["total_bytes"], 1)
          and snapshot["available_bytes"] <= snapshot["total_bytes"]
          and snapshot["available_bytes"] == phase["budget"]["ram_available_bytes"], "budget-snapshot-join")


def verify_owned_memory(value):
    # Validate the recorded OS counters; this is not independent OS attestation.
    check(type(value) is dict and value.get("scope") == "owned-process-working-set-only"
          and type(value.get("available")) is bool, "owned-memory-scope")
    if value["available"]:
        keys(value, ("scope", "available", "working_set_bytes", "peak_working_set_bytes",
            "pagefile_bytes", "peak_pagefile_bytes", "gpu_memory_measured"), "owned-memory-fields")
        check(all(integer(value[k]) for k in ("working_set_bytes", "peak_working_set_bytes",
            "pagefile_bytes", "peak_pagefile_bytes")) and value["gpu_memory_measured"] is False
            and value["working_set_bytes"] <= value["peak_working_set_bytes"]
            and value["pagefile_bytes"] <= value["peak_pagefile_bytes"], "owned-memory-counters")
    else:
        check(set(value) <= {"scope", "available", "error"}
              and ("error" not in value or type(value["error"]) is str and len(value["error"]) <= 128),
              "owned-memory-unavailable")


def known_template_probe(lines, index):
    """Recognize only the pinned Qwen3 differential reasoning-probe fallback."""
    if index < 5 or index + 3 >= len(lines):
        return False
    color = re.compile(r"\x1b\[[0-9;]*m")
    block = [color.sub("", line) for line in lines[index - 5:index + 4]]
    return (block[0] == "Template application failed: " and block[1] == "------------"
        and block[2] == "While executing CallExpression at line 41, column 134 in source:"
        and r"content.lstrip('\n')" in block[3] and block[4] == "                                           ^"
        and block[5] == "Error: Callee is not a function: got Undefined (hint: 'lstrip')"
        and block[6:] == ["compare_reasoning_scope: Template application failed",
            "Phase 2: Content analysis", "Phase 3: Tool call analysis"])


def read_speculation(raw):
    check(type(raw) is bytes and len(raw) <= 8 * 1024**2, "worker-log-bound")
    counters, implementation, errors, warnings, tasks = [], [], [], [], set()
    prefix = r"slot\s+print_timings?:\s+id\s+(\d+)\s+\|\s+task\s+(\d+)\s+\|\s+"
    pattern = re.compile(prefix + r"draft acceptance = ([0-9]+\.[0-9]+) \(\s*(\d+) accepted /\s*(\d+) generated\), mean len =\s*([0-9]+\.[0-9]+)\s*")
    common = re.compile(r"spec\s+common_specu:\s+statistics\s+draft-simple: #calls\(b,g,a\) =\s*(\d+)\s+(\d+)\s+(\d+), #gen drafts =\s*(\d+), #acc drafts =\s*(\d+), #gen tokens =\s*(\d+), #acc tokens =\s*(\d+)(?:,.*)?")
    # Native TRACE writes token-byte fragments which need not form UTF-8.
    # Preserve and hash the original artifact; decode only this derived view.
    lines = raw.decode("utf-8", errors="replace").splitlines()
    for line_number, line in enumerate(lines, 1):
        match = pattern.fullmatch(line) if line.isascii() else None
        if match:
            slot, task, ratio, accepted, generated, mean = match.groups()
            slot, task, accepted, generated = map(int, (slot, task, accepted, generated))
            check((slot, task) not in tasks and slot == 0 and 0 <= accepted <= generated < 2**31 and generated > 0
                  and math.isclose(float(ratio), accepted / generated, abs_tol=0.000011), "speculation-counter-invalid")
            tasks.add((slot, task))
            counters.append({"line": line_number, "raw": line, "slot": slot, "task": task,
                "accepted": accepted, "generated": generated, "ratio": float(ratio), "mean_length": float(mean)})
        elif re.match(prefix + r"draft acceptance\b", line):
            raise ValueError("speculation-counter-malformed")
        match = common.fullmatch(line) if line.isascii() else None
        if match:
            values = dict(zip(("calls_begin", "calls_draft", "calls_accept", "generated_drafts", "accepted_drafts",
                "generated_tokens", "accepted_tokens"), map(int, match.groups())))
            check(all(integer(n, 0, 2**31) for n in values.values()) and values["accepted_tokens"] <= values["generated_tokens"]
                  and values["accepted_drafts"] <= values["generated_drafts"], "speculation-implementation-counters")
            implementation.append({"line": line_number, "raw": line, **values})
        if re.search(r"\bGGML_ASSERT\b|\bCUDA error\b|\bout of memory\b|^error:|^.*failed to load (?:the )?model", line, re.I):
            if known_template_probe(lines, line_number - 1):
                warnings.append({"line": line_number, "raw": line,
                    "classification": "template-analysis-reasoning-probe-fallback"})
            else:
                errors.append({"line": line_number, "raw": line[:2048]})
    offered = sum(row["generated"] for row in counters)
    accepted = sum(row["accepted"] for row in counters)
    return {"scope": "run-log-observation-only", "parsing_policy": {"decode": "utf-8", "errors": "replace",
        "counter_lines": "ASCII-only-full-line", "raw_bytes_preserved": True,
        "template_probe_errors": "bounded-context-warning"},
        "request_counters": counters, "implementation_counters": implementation,
        "offered_tokens": offered, "accepted_tokens": accepted, "draft_attempt_observed": offered > 0,
        "accepted_token_observed": accepted > 0, "actual_speculation_observed": offered > 0 and accepted > 0 and not errors,
        "errors": errors, "warnings": warnings, "native_mtp": False, "actual_backend_qualification": False}


def verify_counter_budget(observed, phase, config):
    requests = [phase["warmup"], *phase["rows"]]
    count = len(requests)
    completion = sum(row["backend_receipt"]["raw_response"]["usage"]["completion_tokens"] for row in requests)
    # Permit one final draft block/EOS of slack per request. A log task is not
    # authenticated to an HTTP request, but impossible phase totals must fail.
    slack = count * (config["draft_max"] + 1)
    check(len(observed["request_counters"]) <= count
          and observed["accepted_tokens"] <= completion + slack
          and observed["offered_tokens"] <= (count * config["max_tokens"] + slack) * config["draft_max"],
          "speculation-phase-token-budget")


def verify_rows(phase, summary):
    config = summary["configuration"]
    rows = [phase["warmup"], *phase["rows"]]
    check(len(phase["rows"]) == len(summary["corpus"]), "phase-corpus-count")
    records = audit_records(phase["audit_directory"])
    check(len(records) == 3 * len(rows) + 3 and records[-1]["kind"] == "shutdown", "audit-count-or-terminal")
    check(records[0]["kind"] == "speculation_configuration" and same(records[0]["value"], {
        "identity": phase["identity"], "phase_id": phase["phase_id"], "configuration_sha256": summary["configuration_sha256"],
        "mode": phase["mode"], "admission_sha256": phase["identity"]}), "audit-identity-binding")
    counters = dict.fromkeys(COUNTERS, 0)
    sequence, ids, outputs = 0, set(), []
    def event(record, kind, key, value=None):
        nonlocal sequence
        check(record["kind"] == "scheduler", "scheduler-record-order")
        sequence += 1
        row = record["value"]
        keys(row, ("seq", "kind", "key", "execution", "reason", "response_sha256", "active", "waiters", "cache_entries", "cache_bytes", *COUNTERS), "scheduler-fields")
        expected = {**counters, "seq": sequence, "kind": kind, "key": key,
            "execution": None if kind == "close" else "computed",
            "reason": "engine-closed" if kind == "close" else None,
            "response_sha256": digest(value) if value is not None else None,
            "active": 1 if kind == "start" else 0, "waiters": 0, "cache_entries": 0, "cache_bytes": 0}
        check(same(row, expected), "scheduler-transition-or-counters")
    for index, row in enumerate(rows):
        response = row["response"]
        exchange(response)
        check(response["status"] == 200 and type(response["status"]) is int
              and number(row["wall_s"]) and row["wall_s"] > 0
              and type(row["backend_exchange_count"]) is int and row["backend_exchange_count"] == 1,
              "computed-row-required")
        request = response["request"]
        keys(request, ("model", "messages", "temperature", "max_tokens", "stream"), "request-fields")
        check(request["model"] == config["alias"] and request["stream"] is False
              and type(request["temperature"]) is int and request["temperature"] == 0
              and type(request["max_tokens"]) is int and request["max_tokens"] == config["max_tokens"], "request-settings")
        check(type(request["messages"]) is list and len(request["messages"]) == 2
              and same(request["messages"][0], {"role": "system", "content": summary["system"]})
              and set(request["messages"][1]) == {"role", "content"}
              and request["messages"][1]["role"] == "user", "request-messages")
        if not index:
            check(request["messages"][1]["content"] == "Reply with only OK.", "warmup-prompt")
        if index:
            case = summary["corpus"][index - 1]
            check(row["case"] == case["id"] and request["messages"][1]["content"] == case["prompt"], "row-corpus-join")
        key = digest({"identity": phase["identity"], "inference": request})
        start, backend, finish = records[1 + 3 * index:4 + 3 * index]
        counters["backend_calls"] += 1
        event(start, "start", key)
        check(backend["kind"] == "backend_exchange" and same(backend["value"], row["backend_receipt"]), "backend-audit-join")
        receipt = row["backend_receipt"]
        check(receipt["name"] == "llama-cpp-tensor", "backend-adapter-name")
        engine = {"name": receipt["name"], "kind": "openai", "model": config["alias"], "max_tokens": config["max_tokens"]}
        bridge.result(receipt, engine, {"messages": request["messages"]})
        check(receipt["outcome"] == "ANSWERED", "backend-not-answered")
        raw = receipt["raw_response"]
        check(not any(k in raw for k in ("error", "thinking", "reasoning", "reasoning_content", "tool_calls"))
              and plain_text(receipt["text"]), "backend-unsupported-content")
        usage = raw["usage"]
        check(set(usage) <= {"prompt_tokens", "completion_tokens", "total_tokens", "prompt_tokens_details"}
              and all(integer(usage.get(k)) for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
              and usage["completion_tokens"] <= config["max_tokens"]
              and "prompt_tokens_details" in usage, "usage-contract")
        if "prompt_tokens_details" in usage:
            details = usage["prompt_tokens_details"]
            keys(details, ("cached_tokens",), "prompt-cache-details")
            check(type(details["cached_tokens"]) is int and details["cached_tokens"] == 0, "prompt-cache-count")
        value = {"model": raw["model"], "choices": [{"index": 0,
            "message": {"role": "assistant", "content": receipt["text"]}, "finish_reason": "stop"}],
            "usage": {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")}}
        counters["computed"] += 1
        event(finish, "finish", key, value)
        body = response["body"]
        keys(body, ("id", "object", "created", "model", "choices", "usage", "cellivane"), "seed-envelope-fields")
        check(same({k: body[k] for k in ("model", "choices", "usage")}, value)
              and body["object"] == "chat.completion" and integer(body["created"]), "seed-envelope-join")
        check(str(UUID(body["id"])) == body["id"] and body["id"] not in ids, "seed-response-uuid")
        ids.add(body["id"])
        check(same(body["cellivane"], {"engine": "seed", "version": "0.1.0", "execution": "computed",
            "key": key, "backend_calls": counters["backend_calls"]}), "seed-execution-identity")
        if index:
            check(row["text"] == receipt["text"], "row-text-join")
            outputs.append({"round": phase["round"], "mode": phase["mode"], "case": row["case"],
                "text": row["text"], "wall_s": row["wall_s"], "completion_tokens": usage["completion_tokens"]})
    event(records[-2], "close", None)
    state = phase["state"]
    check(same(records[-1]["value"], state) and state["closed"] is True and state["poisoned"] is False
          and state["identity"] == phase["identity"] and state["model"] == config["alias"], "shutdown-state")
    for key, expected in {**counters, "active": 0, "inflight": 0, "waiters": 0, "cache_entries": 0,
            "cache_entries_limit": 0, "cache_bytes": 0, "event_seq": sequence, "slots": 1}.items():
        check(type(state.get(key)) is int and state[key] == expected, "shutdown-counters")
    return outputs


def replay(summary_path, source_root=None):
    summary = load(raw_file(summary_path, 32 * 1024**2))
    check(type(summary["schema_version"]) is int and summary["schema_version"] == 1
          and summary["experiment"] == "cellivane-seed-speculation" and summary["outcome"] == "COMPLETED"
          and summary["recording_errors"] == [], "experiment-incomplete")
    engine_identity = verify_assets(summary["backend_assets"])
    for name, pin in MODEL_PINS.items():
        value = summary["models"][name]
        check((value["bytes"], value["sha256"]) == pin and type(value["bytes"]) is int, "model-pin")
        if source_root is not None:
            check(safe_path(value["path"]).stat().st_size == pin[0] and file_hash(value["path"]) == pin[1], "model-file-changed")
    sources = summary["source_sha256"]
    check(type(sources) is dict and set(sources) == SOURCE_PATHS and all(hex_digest(v) for v in sources.values()), "source-closure")
    for name, expected in sources.items():
        relative = PurePosixPath(name)
        check(not relative.is_absolute() and ".." not in relative.parts and name.endswith(".py"), "source-path")
        if source_root is not None:
            check(file_hash(safe_path(source_root).joinpath(*relative.parts)) == expected, "source-content-changed")
    config = summary["configuration"]
    keys(config, ("schema_version", "alias", "profile", "threads", "context", "slots", "cache_entries", "max_tokens",
        "temperature", "seed", "no_think", "repeat", "draft_max", "draft_min", "draft_sampling", "cache_prompt", "reasoning_format"), "configuration-fields")
    for field, expected in (("schema_version", 1), ("threads", 4), ("context", 2048), ("slots", 1), ("cache_entries", 0),
            ("temperature", 0), ("seed", 42), ("draft_max", 4), ("draft_min", 0)):
        check(type(config[field]) is int and config[field] == expected, "configuration-integer")
    check(summary["configuration_sha256"] == digest(config) and config["profile"] == "cpu" and config["alias"] == "cellivane-spec-target"
          and config["threads"] == 4 and type(config["threads"]) is int
          and config["context"] == 2048 and config["slots"] == 1 and config["cache_entries"] == 0
          and config["temperature"] == 0 and type(config["temperature"]) is int
          and config["seed"] == 42 and same(config["no_think"], {"enable_thinking": False})
          and config["draft_max"] == 4 and integer(config["repeat"], 1, 4)
          and integer(config["max_tokens"], 64, 129) and config["draft_sampling"] == "greedy"
          and config["cache_prompt"] is False and config["reasoning_format"] == "auto", "fixed-experiment-configuration")
    corpus = summary["corpus"]
    check(type(corpus) is list and len(corpus) == 4 and summary["corpus_sha256"] == digest(corpus) == CORPUS_SHA
          and summary["system"] == SYSTEM
          and len({case["id"] for case in corpus}) == 4, "corpus-identity")
    phases = summary["phases"]
    expected_order = [(r, mode) for r in range(config["repeat"])
                      for mode in (("none", "draft") if r % 2 == 0 else ("draft", "none"))]
    check([(p["round"], p["mode"]) for p in phases] == expected_order, "phase-order-or-count")
    rows = []
    observations = []
    for phase in phases:
        check(phase["phase_id"] == f"round-{phase['round']}-{phase['mode']}"
              and type(phase["round"]) is int and phase["outcome"] == "COMPLETED" and phase["error"] is None, "phase-completion")
        verify_plan(phase, summary, engine_identity)
        verify_cleanup([phase["cleanup"]])
        verify_command(phase, summary)
        verify_owned_memory(phase["owned_memory"])
        check(phase["source_unchanged_during_run"] is True and phase["models_unchanged_during_run"] is True
              and phase["backend_assets_unchanged_during_run"] is True,
              "run-artifacts-changed")
        check(type(phase["ports_closed"]) is dict and len(phase["ports_closed"]) == 2
              and all(re.fullmatch(r"[0-9]{4,5}", port) and 1024 <= int(port) <= 65535 and closed is True
                      for port, closed in phase["ports_closed"].items()), "port-cleanup")
        observed = read_speculation(artifact(phase["worker_log"], maximum=8 * 1024**2))
        check(same(phase["speculation_observed"], observed) and not observed["errors"], "worker-log-recompute")
        check(phase["mode"] != "none" or not observed["draft_attempt_observed"], "unexpected-baseline-speculation")
        observations.append({"phase": phase["phase_id"], "mode": phase["mode"], **observed})
        rows.extend(verify_rows(phase, summary))
        verify_counter_budget(observed, phase, config)
    pairs = []
    for round_id in range(config["repeat"]):
        for case in corpus:
            pair = {r["mode"]: r for r in rows if r["round"] == round_id and r["case"] == case["id"]}
            check(set(pair) == {"none", "draft"}, "pair-missing")
            pairs.append({"round": round_id, "case": case["id"], "both_answered": True,
                "text_equal": pair["none"]["text"] == pair["draft"]["text"],
                "none_wall_s": pair["none"]["wall_s"], "draft_wall_s": pair["draft"]["wall_s"]})
    metrics = {}
    for mode in ("none", "draft"):
        current = [r for r in rows if r["mode"] == mode]
        metrics[mode] = {"total": len(current), "answered": len(current),
            "median_wall_s": statistics.median(r["wall_s"] for r in current),
            "completion_tokens": sum(r["completion_tokens"] for r in current), "timing_scope": TIMING_SCOPE}
    equal = sum(p["text_equal"] for p in pairs)
    metrics["pairs"] = {"total": len(pairs), "both_answered": len(pairs), "text_equal": equal}
    check(same(summary["pairs"], pairs) and same(summary["metrics"], metrics), "comparison-metric-recompute")
    check(same(summary["unsupported_claims"], {"native_mtp": False, "quality_improvement": False, "multi_gpu": False,
        "endpoint_pid_attestation": False, "independent_verdict": False}), "unsupported-claim-promoted")
    inspection = summary["backend_assets"]["inspection"]
    help_text = artifact(inspection["help"], maximum=1024**2).decode("utf-8")
    version_text = artifact(inspection["version"], maximum=1024**2).decode("utf-8")
    check("version: 0.6.0-dev (build 11429, commit d81235049)" in version_text
          and all(flag in help_text for flag in ("--spec-type none,draft-simple", "--no-cache-prompt", "--offline",
              "--spec-draft-model", "--spec-draft-sampling", "--chat-template-kwargs")), "backend-help-or-version")
    check(same(inspection["commands"], [[summary["backend_assets"]["server"], "--help"],
        [summary["backend_assets"]["server"], "--version"]]), "inspection-commands")
    verify_cleanup(inspection["cleanup"])
    ratios = [p["none_wall_s"] / p["draft_wall_s"] for p in pairs if p["text_equal"]]
    draft_observed = all(o["actual_speculation_observed"] for o in observations if o["mode"] == "draft")
    return {"outcome": "PASS", "scope": "recorded-cpu-speculation-integrity-and-semantic-replay",
        "source_files_checked": source_root is not None, "phases": len(phases), "computed_responses": len(rows),
        "paired_text_equal": equal, "paired_total": len(pairs), "greedy_equivalence": "PASS" if equal == len(pairs) else "FAIL",
        "median_paired_wall_speed_ratio": statistics.median(ratios) if ratios else None,
        "speed_ratio_scope": "equal-text-pairs-only; advisory-small-corpus", "speculation_observed": draft_observed,
        "draft_offered_tokens": sum(o["offered_tokens"] for o in observations if o["mode"] == "draft"),
        "draft_accepted_tokens": sum(o["accepted_tokens"] for o in observations if o["mode"] == "draft"),
        "counter_scope": "phase-log-totals-including-warmup; bounded-against-completions; no-request-task-attestation",
        "metrics": metrics, "pairs": pairs, "actual_backend_qualification": False, "endpoint_pid_attestation": False,
        "native_mtp": False, "tensor_residency_managed": False, "quality_generalization_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary")
    parser.add_argument("--source-root")
    args = parser.parse_args(argv)
    try:
        value = replay(args.summary, args.source_root)
    except (ValueError, KeyError, TypeError, OSError, OverflowError, RecursionError, zipfile.BadZipFile) as exc:
        value = {"outcome": "FAIL", "reason": str(exc), "actual_backend_qualification": False,
                 "native_mtp": False, "endpoint_pid_attestation": False}
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    return 0 if value["outcome"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
