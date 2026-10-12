#!/usr/bin/env python3
"""Owned CPU draft-model experiment through Seed's unchanged HTTP/backend path.

Qwen3-4B is the target and Qwen3-0.6B is the draft. Each interleaved phase
starts a fresh pinned llama.cpp server, then executes one warmup and four cases.
Seed result caching and backend prompt reuse are disabled. This producer saves
observations; it does not produce an independent verdict or claim native MTP,
general quality improvement, endpoint/PID attestation, or GPU qualification.
All raw prompts, output, command paths and logs stay in the local run folder.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import ctypes
from hashlib import sha256
import http.client
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
import threading
import time
from uuid import uuid4

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "hosted" / "experimental"))
import cellivane_bridge_lab as lab
from cellivane_bridge.adapters import _load
from cellivane_bridge.contracts import EngineConfig
from cellivane_bridge.storage import encoded, local_root, read_json
from cellivane_seed.core import SeedEngine
from cellivane_seed.server import Audit, Backend, SeedHTTPServer
from cellivane_seed.capabilities import BackendCapabilities
from cellivane_seed.resources import ExecutionDemand, ResourceBudget, plan_admission
import cellivane_llama_assets as assets

MODELS = {
    "target": {"bytes": 4280404704, "sha256": "8c2f07f26af9747e41988551106f149b03eb9b5cb6df636027b6bf6278473300"},
    "draft": {"bytes": 639446688, "sha256": "9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031"},
}
CASES = [
    {"id": "fixed-list", "prompt": "Write exactly the numbers 1 through 20 in order, separated by commas. No title or explanation."},
    {"id": "short-code", "prompt": "Write only a Python function named square_sum(xs) that returns the sum of the squares in xs. Use exactly three short lines, no Markdown or explanation."},
    {"id": "korean", "prompt": "고양이를 처음 돌보는 사람에게 세 가지 조언을 한국어로 써 줘. 조언마다 한 문장, 모두 합쳐 80자 이내. 제목이나 맺음말 없이 세 줄만 써 줘."},
    {"id": "copy-lines", "prompt": "Copy the next sentence exactly four times, one copy per line, then stop. No title or explanation. Sentence: The quiet garden is bright today."},
]
SYSTEM = "Follow the requested output format. Give only the requested answer. /no_think"
MAX_LOG_BYTES = 8 * 1024**2
SOURCE_PATHS = tuple(sorted([
    "hosted/experimental/cellivane_bridge/" + n + ".py"
    for n in ("__init__", "adapters", "contracts", "runtime", "storage")
] + [
    "hosted/experimental/cellivane_seed/" + n + ".py"
    for n in ("__init__", "core", "server", "capabilities", "resources")
] + ["tools/hosted/" + n + ".py" for n in (
    "cellivane_bridge_lab", "cellivane_llama_assets", "cellivane_seed_speculate")]))


def digest(value):
    return sha256(encoded(value)).hexdigest()


def source_hashes():
    return {name: assets.file_sha256(REPO / name) for name in SOURCE_PATHS}


def configuration(repeat=3, max_tokens=128):
    if type(repeat) is not int or not 1 <= repeat <= 3:
        raise ValueError("repeat-must-be-1-to-3")
    if type(max_tokens) is not int or not 64 <= max_tokens <= 128:
        raise ValueError("max-tokens-must-be-64-to-128")
    return {"schema_version": 1, "alias": "cellivane-spec-target", "profile": "cpu",
            "threads": 4, "context": 2048, "slots": 1, "cache_entries": 0,
            "max_tokens": max_tokens, "temperature": 0, "seed": 42,
            "no_think": {"enable_thinking": False}, "reasoning_format": "auto", "repeat": repeat,
            "draft_max": 4, "draft_min": 0, "draft_sampling": "greedy",
            "cache_prompt": False}


def phase_order(repeat):
    configuration(repeat)
    return [(r, mode) for r in range(repeat)
            for mode in (("none", "draft") if r % 2 == 0 else ("draft", "none"))]


def payload(config, prompt):
    return {"model": config["alias"], "messages": [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt}], "max_tokens": config["max_tokens"],
            "temperature": 0, "stream": False}


def checked_model(value, kind):
    path = Path(value)
    local_root(path.parent)
    if (not path.is_file() or path.is_symlink()
            or getattr(path.lstat(), "st_file_attributes", 0) & 0x400
            or path.stat().st_size != MODELS[kind]["bytes"]
            or assets.file_sha256(path) != MODELS[kind]["sha256"]):
        raise ValueError("model-pin-mismatch:" + kind)
    return {"path": str(path.resolve(strict=True)), **MODELS[kind]}


def file_record(path, maximum=MAX_LOG_BYTES):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
        raise ValueError("evidence-file-missing-or-reparse")
    if not 1 <= path.stat().st_size <= maximum:
        raise ValueError("evidence-file-size")
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": assets.file_sha256(path)}


def validate_help(text):
    required = ("--spec-type none,draft-simple", "--spec-draft-model", "--spec-draft-n-max",
        "--spec-draft-n-min", "--spec-draft-sampling", "--spec-draft-threads",
        "--spec-draft-threads-batch", "--spec-draft-ngl", "--chat-template-kwargs",
        "--reasoning [on|off|auto]", "--reasoning-format", "--no-cache-prompt",
        "--no-cache-idle-slots", "--offline", "--fit [on|off]", "--log-verbosity")
    if any(flag not in text for flag in required):
        raise ValueError("pinned-help-unsupported-flags")


def inspect_backend(prepared, root, evidence):
    directory = local_root(evidence / "inspection")
    records, commands, cleanup = {}, [], []
    for name in ("help", "version"):
        command = [prepared["server"], "--" + name]
        with ExitStack() as stack:
            child = lab.OwnedProcess(stack, command,
                lab.child_env(root, Path(prepared["server"]).parent), directory, name)
            code = child.process.wait(timeout=20)
        cleanup.append(child.cleanup)
        if code != 0:
            raise ValueError("backend-inspection-failed:" + name)
        records[name] = file_record(directory / (name + ".log"), 1024**2)
        commands.append(command)
    validate_help(Path(records["help"]["path"]).read_text(encoding="utf-8"))
    version = Path(records["version"]["path"]).read_text(encoding="utf-8")
    if "version: 0.6.0-dev (build 11429, commit d81235049)" not in version:
        raise ValueError("backend-version-pin-mismatch")
    return {**records, "commands": commands, "cleanup": cleanup}


def cpu_environment(root, binary_dir):
    env = lab.child_env(root, binary_dir)
    # The launcher's parent variables never reach the tensor worker. Keep only
    # lab's named OS/profile locations and force explicit device/offline flags.
    env = {k: v for k, v in env.items() if not k.startswith("OLLAMA_")}
    env.update(CUDA_VISIBLE_DEVICES="-1", HIP_VISIBLE_DEVICES="-1",
               ROCR_VISIBLE_DEVICES="-1", GGML_VK_VISIBLE_DEVICES="-1")
    return env


def command(prepared, models, config, mode, worker_port):
    if mode not in ("none", "draft"):
        raise ValueError("unsupported-speculation-mode")
    if type(worker_port) is not int or not 1024 <= worker_port <= 65535:
        raise ValueError("worker-port-range")
    result = [prepared["server"], "--model", models["target"]["path"],
        "--alias", config["alias"], "--host", "127.0.0.1", "--port", str(worker_port),
        "--threads", "4", "--threads-batch", "4", "--threads-http", "2", "--parallel", "1",
        "--ctx-size", "2048", "--batch-size", "512", "--ubatch-size", "128",
        "--gpu-layers", "0", "--fit", "off", "--temp", "0", "--seed", "42",
        "--n-predict", str(config["max_tokens"]), "--jinja", "--reasoning", "off",
        "--reasoning-format", config["reasoning_format"], "--chat-template-kwargs", '{"enable_thinking":false}',
        "--no-cache-prompt", "--cache-ram", "0", "--no-cache-idle-slots", "--no-context-shift",
        "--offline", "--no-webui", "--metrics", "--log-colors", "off",
        "--log-verbosity", "5", "--no-log-prefix", "--no-log-timestamps",
        "--spec-type", "none" if mode == "none" else "draft-simple"]
    if mode == "draft":
        result.extend(["--spec-draft-model", models["draft"]["path"],
            "--spec-draft-threads", "4", "--spec-draft-threads-batch", "4",
            "--spec-draft-ngl", "0", "--spec-draft-n-max", "4", "--spec-draft-n-min", "0",
            "--spec-draft-sampling", "greedy"])
    return result


def memory_snapshot():
    if os.name != "nt":
        raise ValueError("speculation-lab-currently-windows-only")
    class Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [
            (name, ctypes.c_uint64) for name in ("total_phys", "avail_phys", "total_page",
                "avail_page", "total_virtual", "avail_virtual", "avail_extended")]
    status = Status()
    status.length = ctypes.sizeof(status)
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(Status)]
    api.GlobalMemoryStatusEx.restype = ctypes.c_int
    if not api.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"source": "GlobalMemoryStatusEx", "available_bytes": status.avail_phys,
            "total_bytes": status.total_phys, "scope": "host-available-physical-memory-snapshot"}


def owned_memory_snapshot(process):
    """Read only the retained process handle; never OpenProcess an arbitrary PID."""
    if os.name != "nt" or getattr(process, "_handle", None) is None:
        return {"scope": "owned-process-working-set-only", "available": False}
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_uint32), ("faults", ctypes.c_uint32)] + [
            (name, ctypes.c_size_t) for name in ("peak_working_set", "working_set", "peak_paged",
                "paged", "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile")]
    counts = Counters()
    counts.cb = ctypes.sizeof(counts)
    api = ctypes.WinDLL("psapi", use_last_error=True)
    api.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_uint32]
    api.GetProcessMemoryInfo.restype = ctypes.c_int
    if not api.GetProcessMemoryInfo(process._handle, ctypes.byref(counts), counts.cb):
        return {"scope": "owned-process-working-set-only", "available": False,
                "error": "GetProcessMemoryInfo:" + str(ctypes.get_last_error())}
    return {"scope": "owned-process-working-set-only", "available": True,
            "working_set_bytes": counts.working_set, "peak_working_set_bytes": counts.peak_working_set,
            "pagefile_bytes": counts.pagefile, "peak_pagefile_bytes": counts.peak_pagefile,
            "gpu_memory_measured": False}


def exchange(port, path, request=None):
    if type(port) is not int or not 1024 <= port <= 65535 or path not in (
            "/v1/chat/completions", "/health", "/v1/models"):
        raise ValueError("bounded-exchange-endpoint")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=125)
    try:
        raw = encoded(request) if request is not None else None
        if raw is not None and len(raw) > 16 * 1024:
            raise ValueError("exchange-request-size")
        conn.request("POST" if raw is not None else "GET", path, body=raw,
                     headers={"Content-Type": "application/json", "Connection": "close"})
        response = conn.getresponse()
        content = response.read(256 * 1024 + 1)
        if len(content) > 256 * 1024:
            raise ValueError("exchange-response-size")
        if (response.getheader("Content-Type", "").split(";", 1)[0].strip() != "application/json"
                or response.getheader("Content-Encoding", "identity").lower() != "identity"):
            raise ValueError("exchange-response-headers")
        return {"status": response.status, "body": _load(content),
                "raw_response": content.decode("utf-8"), "request": request}
    finally:
        conn.close()


def observe_speculation(raw):
    """Parse pinned native per-request counters, never infer from a mode flag.

    Slot timing counters are per request and are summed. Common implementation
    counters are cumulative snapshots, retained individually rather than summed.
    Zero accepted tokens and a slowdown are valid observations, not failures.
    Native TRACE token pieces can contain incomplete UTF-8 byte sequences.
    Decode this observation view with replacement; only ASCII full-line native
    counters are accepted. The stored file and its SHA retain every raw byte.
    HTTP response JSON continues to use strict UTF-8 through exchange/bridge.
    """
    if type(raw) is not bytes or len(raw) > MAX_LOG_BYTES:
        raise ValueError("worker-log-size")
    text = raw.decode("utf-8", errors="replace")
    rows, common, errors, warnings = [], [], [], []
    lines = text.splitlines()
    ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

    def nonfatal_template_probe(index):
        # Pinned differential autoparser catches this exact synthetic
        # reasoning/tool scope failure and continues the other analyses.
        # A bare error, a changed exception, or a truncated context still fails.
        if index < 5 or index + 3 >= len(lines):
            return False
        context = [ansi.sub("", line) for line in lines[index - 5:index + 4]]
        return (context[0] == "Template application failed: "
            and context[1] == "------------"
            and context[2] == "While executing CallExpression at line 41, column 134 in source:"
            and "content.lstrip('\\n')" in context[3]
            and re.fullmatch(r"[ \t]*\^[ \t]*", context[4]) is not None
            and context[5] == "Error: Callee is not a function: got Undefined (hint: 'lstrip')"
            and context[6] == "compare_reasoning_scope: Template application failed"
            and context[7] == "Phase 2: Content analysis"
            and context[8] == "Phase 3: Tool call analysis")
    prefix = r"slot\s+print_timing[s]?:\s+id\s+(\d+)\s+\|\s+task\s+(\d+)\s+\|\s+"
    pattern = re.compile(prefix + r"draft acceptance = ([0-9]+\.[0-9]+) \(\s*(\d+) accepted /\s*(\d+) generated\), mean len =\s*([0-9]+\.[0-9]+)\s*")
    # Pinned SPC_TRC prints the first 12 characters of __func__:
    # common_speculative_print_stats -> common_specu.
    statistics_pattern = re.compile(r"spec\s+common_specu:\s+statistics\s+draft-simple: #calls\(b,g,a\) =\s*(\d+)\s+(\d+)\s+(\d+), #gen drafts =\s*(\d+), #acc drafts =\s*(\d+), #gen tokens =\s*(\d+), #acc tokens =\s*(\d+)(?:,.*)?")
    for index, line in enumerate(lines, 1):
        match = pattern.fullmatch(line)
        if match:
            if not line.isascii():
                raise ValueError("worker-speculation-counter-nonascii")
            slot, task, ratio, accepted, generated, mean = match.groups()
            accepted, generated = int(accepted), int(generated)
            if not 0 <= accepted <= generated < 2**31 or generated == 0 or not math.isclose(float(ratio), accepted / generated, abs_tol=0.000011):
                raise ValueError("worker-speculation-counter")
            rows.append({"line": index, "raw": line, "slot": int(slot), "task": int(task),
                "accepted": accepted, "generated": generated, "ratio": float(ratio), "mean_length": float(mean)})
        elif re.match(prefix + r"draft acceptance\b", line):
            raise ValueError("worker-speculation-format")
        match = statistics_pattern.fullmatch(line)
        if match:
            if not line.isascii():
                raise ValueError("worker-speculation-statistics-nonascii")
            values = dict(zip(("calls_begin", "calls_draft", "calls_accept", "generated_drafts", "accepted_drafts", "generated_tokens", "accepted_tokens"), map(int, match.groups())))
            if (any(n >= 2**31 for n in values.values()) or values["accepted_tokens"] > values["generated_tokens"]
                    or values["accepted_drafts"] > values["generated_drafts"]):
                raise ValueError("worker-speculation-statistics")
            common.append({"line": index, "raw": line, **values})
        if re.search(r"\bGGML_ASSERT\b|\bCUDA error\b|\bout of memory\b|^error:|^.*failed to load (?:the )?model", line, re.I):
            if nonfatal_template_probe(index - 1):
                warnings.append({"line": index, "raw": line,
                    "classification": "template-analysis-reasoning-probe-fallback"})
            else:
                errors.append({"line": index, "raw": line[:2048]})
    offered = sum(row["generated"] for row in rows)
    accepted = sum(row["accepted"] for row in rows)
    return {"scope": "run-log-observation-only", "parsing_policy": {
                "decode": "utf-8", "errors": "replace", "counter_lines": "ASCII-only-full-line",
                "raw_bytes_preserved": True, "template_probe_errors": "bounded-context-warning"},
            "request_counters": rows, "implementation_counters": common,
            "offered_tokens": offered, "accepted_tokens": accepted,
            "draft_attempt_observed": offered > 0, "accepted_token_observed": accepted > 0,
            "actual_speculation_observed": offered > 0 and accepted > 0 and not errors,
            "errors": errors, "warnings": warnings, "native_mtp": False, "actual_backend_qualification": False}


def request_record(port, config, prompt, audit):
    before = audit.sequence
    start = time.perf_counter()
    response = exchange(port, "/v1/chat/completions", payload(config, prompt))
    wall = time.perf_counter() - start
    receipts = []
    for path in sorted(audit.directory.glob("*.json")):
        if int(path.stem) > before:
            row = read_json(path)
            if row["kind"] == "backend_exchange":
                receipts.append(row["value"])
    body = response["body"]
    text = body.get("choices", [{}])[0].get("message", {}).get("content") if response["status"] == 200 else None
    return {"response": response, "wall_s": wall, "text": text,
            "backend_receipt": receipts[0] if len(receipts) == 1 else None,
            "backend_exchange_count": len(receipts)}


def shutdown_seed(engine, server, thread, audit, phase):
    """Close every created local component even when persistence fails."""
    try:
        if engine is not None:
            engine.close()
    finally:
        try:
            if server is not None and thread is not None and thread.is_alive():
                server.shutdown()
                thread.join(10)
                if thread.is_alive():
                    raise RuntimeError("seed-thread-cleanup-unconfirmed")
        finally:
            if server is not None:
                server.server_close()
            if engine is not None:
                phase["state"] = engine.status()
                if audit is not None:
                    audit.append("shutdown", phase["state"])


def validate_phase_inputs(prepared, models, expected_sources):
    """Recheck physical pins at the phase boundary, before any owned launch.

    This detects changes since the experiment's initial manifest. It does not
    claim atomic filesystem/loader attestation across a concurrent mutation.
    """
    if source_hashes() != expected_sources:
        raise ValueError("phase-source-pin-mismatch")
    for kind in ("target", "draft"):
        if checked_model(models[kind]["path"], kind) != models[kind]:
            raise ValueError("phase-model-pin-mismatch:" + kind)
    current = assets.prepare_assets(Path(prepared["directory"]).parent, download=False)
    if current != prepared:
        raise ValueError("phase-backend-assets-pin-mismatch")


def run_phase(root, evidence, prepared, models, config, round_index, mode, ports, *, expected_sources):
    phase_id = f"round-{round_index}-{mode}"
    directory = local_root(evidence / phase_id)
    snapshot = memory_snapshot()
    budget = ResourceBudget(ram_available_bytes=snapshot["available_bytes"], ram_headroom_bytes=4 * 1024**3)
    caps = BackendCapabilities(backend="llama.cpp", engine_identity=prepared["engine_identity"],
                              declared_modes=("none", "draft"), declared_placements=("cpu",))
    demand = ExecutionDemand(engine_identity=prepared["engine_identity"],
        target_sha256=models["target"]["sha256"], target_bytes=models["target"]["bytes"],
        configuration_sha256=digest({"configuration_sha256": digest(config), "mode": mode}), mode=mode,
        draft_sha256=models["draft"]["sha256"] if mode == "draft" else None,
        draft_bytes=models["draft"]["bytes"] if mode == "draft" else 0,
        host_overhead_bytes=2 * 1024**3, context_workspace_bytes=512 * 1024**2)
    admission = plan_admission(caps, demand, budget)
    cmd = command(prepared, models, config, mode, ports[1])
    env = cpu_environment(root, Path(prepared["server"]).parent)
    phase = {"phase_id": phase_id, "round": round_index, "mode": mode, "identity": admission.sha256,
        "capabilities": caps.record(), "budget": budget.record(), "demand": demand.record(),
        "admission": admission.record(), "memory_snapshot": snapshot, "command": cmd,
        "environment": {k: env[k] for k in ("CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "GGML_VK_VISIBLE_DEVICES")},
        "audit_directory": None, "warmup": None, "rows": [], "state": None,
        "cleanup": None, "ports_closed": {}, "worker_log": None, "speculation_observed": None,
        "owned_memory": None, "source_unchanged_during_run": False, "models_unchanged_during_run": False,
        "backend_assets_unchanged_during_run": False,
        "outcome": "NOT_STARTED", "error": None}
    child, engine, server, thread, audit = None, None, None, None, None
    try:
        admission.require_admitted()
        for port in ports:
            lab.port_free(port)
        validate_phase_inputs(prepared, models, expected_sources)
        with ExitStack() as stack:
            child = lab.OwnedProcess(stack, cmd, env, directory, "worker")
            health = lab.ready(child.process, ports[1], "/health", seconds=120)
            if health != {"status": "ok"}:
                raise ValueError("backend-not-healthy")
            try:
                audit = Audit(root)
                phase["audit_directory"] = str(audit.directory)
                audit.append("speculation_configuration", {"identity": phase["identity"], "phase_id": phase_id,
                    "configuration_sha256": digest(config), "mode": mode, "admission_sha256": admission.sha256})
                backend = Backend(EngineConfig("llama-cpp-tensor", "openai", f"http://127.0.0.1:{ports[1]}/v1",
                    config["alias"], timeout_s=120, max_tokens=config["max_tokens"]), audit)
                engine = SeedEngine(backend, model=config["alias"], identity=phase["identity"], cache_entries=0,
                                    timeout_s=120, event_sink=lambda event: audit.append("scheduler", event))
                server = SeedHTTPServer(("127.0.0.1", ports[0]), engine, audit)
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05})
                thread.start()
                phase["warmup"] = request_record(ports[0], config, "Reply with only OK.", audit)
                for case in CASES:
                    phase["rows"].append({"case": case["id"], **request_record(ports[0], config, case["prompt"], audit)})
                    file_record(directory / "worker.log")
                    print(json.dumps({"phase": phase_id, "case": case["id"],
                        "status": phase["rows"][-1]["response"]["status"]}), flush=True)
                phase["owned_memory"] = owned_memory_snapshot(child.process)
                phase["outcome"] = "COMPLETED"
            finally:
                # The Seed HTTP handlers and synchronous backend request finish
                # before closing this phase's tensor process Job.
                shutdown_seed(engine, server, thread, audit, phase)
    except BaseException as exc:
        phase["outcome"] = "FAIL"
        phase["error"] = type(exc).__name__ + ":" + str(exc)[:512]
    finally:
        phase["cleanup"] = child.cleanup if child is not None else None
        for port in ports:
            try:
                lab.port_free(port)
                phase["ports_closed"][str(port)] = True
            except OSError:
                phase["ports_closed"][str(port)] = False
        try:
            phase["source_unchanged_during_run"] = source_hashes() == expected_sources
            phase["models_unchanged_during_run"] = all(checked_model(models[kind]["path"], kind) == models[kind]
                for kind in ("target", "draft"))
            phase["backend_assets_unchanged_during_run"] = assets.prepare_assets(
                Path(prepared["directory"]).parent, download=False) == prepared
            if not all(phase[key] for key in ("source_unchanged_during_run", "models_unchanged_during_run",
                                             "backend_assets_unchanged_during_run")):
                raise ValueError("phase-inputs-changed-during-run")
        except Exception as exc:
            phase["outcome"] = "FAIL"
            phase["error"] = (phase["error"] + ";" if phase["error"] else "") + type(exc).__name__ + ":" + str(exc)[:512]
        try:
            if (directory / "worker.log").is_file():
                phase["worker_log"] = file_record(directory / "worker.log")
                phase["speculation_observed"] = observe_speculation((directory / "worker.log").read_bytes())
        except Exception as exc:
            phase["outcome"] = "FAIL"
            phase["error"] = (phase["error"] + ";" if phase["error"] else "") + type(exc).__name__ + ":" + str(exc)[:512]
    return phase


def summarize(phases, repeat):
    pairs = []
    for r in range(repeat):
        for case in CASES:
            found = {p["mode"]: next((row for row in p["rows"] if row["case"] == case["id"]), None)
                     for p in phases if p["round"] == r}
            left, right = found.get("none"), found.get("draft")
            both = left is not None and right is not None and all(row["response"]["status"] == 200 for row in (left, right))
            pairs.append({"round": r, "case": case["id"], "both_answered": both,
                "text_equal": both and left["text"] == right["text"],
                "none_wall_s": left["wall_s"] if left else None, "draft_wall_s": right["wall_s"] if right else None})
    metrics = {}
    for mode in ("none", "draft"):
        rows = [row for p in phases if p["mode"] == mode for row in p["rows"]]
        good = [row for row in rows if row["response"]["status"] == 200]
        metrics[mode] = {"total": len(rows), "answered": len(good),
            "median_wall_s": statistics.median(row["wall_s"] for row in good) if good else None,
            "completion_tokens": sum(row["response"]["body"]["usage"]["completion_tokens"] for row in good),
            "timing_scope": "successful-computed-corpus-responses-only; warmup excluded; Seed cache and backend prompt reuse disabled"}
    metrics["pairs"] = {"total": len(pairs), "both_answered": sum(row["both_answered"] for row in pairs),
                        "text_equal": sum(row["text_equal"] for row in pairs)}
    return pairs, metrics


def recording_errors(summary):
    errors = []
    if len(summary["phases"]) != 2 * summary["configuration"]["repeat"]:
        errors.append("phase-coverage")
    for phase in summary["phases"]:
        label = phase["phase_id"] + ":"
        if phase["outcome"] != "COMPLETED":
            errors.append(label + "phase-not-completed")
        rows = ([phase["warmup"]] if phase["warmup"] else []) + phase["rows"]
        if len(rows) != 5 or any(row["response"]["status"] != 200 or row["backend_exchange_count"] != 1
                or type(row["backend_receipt"]) is not dict or row["backend_receipt"].get("outcome") != "ANSWERED"
                or row["response"]["body"].get("cellivane", {}).get("execution") != "computed" for row in rows):
            errors.append(label + "computed-responses-incomplete")
        state = phase["state"] or {}
        if (state.get("closed") is not True or state.get("poisoned") is not False
                or state.get("backend_calls") != 5 or state.get("computed") != 5
                or any(state.get(k) != 0 for k in ("active", "inflight", "waiters", "cache_hits", "coalesced", "failures", "timeouts"))):
            errors.append(label + "scheduler-final-state")
        if not phase["cleanup"] or phase["cleanup"].get("job_cleanup_confirmed") is not True or not all(phase["ports_closed"].values()):
            errors.append(label + "cleanup-unconfirmed")
        if not all(phase[k] for k in ("source_unchanged_during_run", "models_unchanged_during_run",
                                     "backend_assets_unchanged_during_run")):
            errors.append(label + "sources-models-or-backend-assets-changed")
        observed = phase["speculation_observed"] or {}
        if observed.get("errors") or (phase["mode"] == "none" and observed.get("draft_attempt_observed")):
            errors.append(label + "worker-log-error-or-unexpected-draft")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-root", required=True)
    parser.add_argument("--storage-root", required=True)
    parser.add_argument("--target-file", required=True)
    parser.add_argument("--draft-file", required=True)
    parser.add_argument("--profile", choices=("cpu",), default="cpu", help="GPU remains unqualified and is not admitted by this CPU binary")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--port", type=int, default=18951)
    parser.add_argument("--worker-port", type=int, default=18952)
    parser.add_argument("--download-assets", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args(argv)
    config = configuration(args.repeat, args.max_tokens)
    if args.port == args.worker_port or any(not 1024 <= port <= 65535 for port in (args.port, args.worker_port)):
        parser.error("distinct loopback ports in 1024..65535 required")
    if os.name != "nt":
        parser.error("owned model lab currently requires Windows Job Objects")
    root = local_root(args.storage_root)
    evidence = local_root(root / "speculation-runs") / str(uuid4())
    evidence.mkdir(exist_ok=False)
    summary = {"schema_version": 1, "experiment": "cellivane-seed-speculation", "run_id": evidence.name,
        "backend_assets": None, "models": None, "source_sha256": source_hashes(),
        "corpus": CASES, "corpus_sha256": digest(CASES), "system": SYSTEM,
        "configuration": config, "configuration_sha256": digest(config),
        "phases": [], "pairs": [], "metrics": {}, "outcome": "FAIL", "recording_errors": [],
        "unsupported_claims": {"native_mtp": False, "quality_improvement": False, "multi_gpu": False,
                               "endpoint_pid_attestation": False, "independent_verdict": False}, "error": None}
    try:
        prepared = assets.prepare_assets(args.asset_root, download=args.download_assets)
        summary["backend_assets"] = {**prepared, "inspection": inspect_backend(prepared, root, evidence)}
        models = {"target": checked_model(args.target_file, "target"), "draft": checked_model(args.draft_file, "draft")}
        summary["models"] = models
        if args.prepare_only:
            summary["outcome"] = "PREPARED"
        else:
            for round_index, mode in phase_order(args.repeat):
                phase = run_phase(root, evidence, prepared, models, config, round_index, mode,
                    (args.port, args.worker_port), expected_sources=summary["source_sha256"])
                summary["phases"].append(phase)
                if phase["outcome"] != "COMPLETED":
                    break
            summary["pairs"], summary["metrics"] = summarize(summary["phases"], args.repeat)
            summary["recording_errors"] = recording_errors(summary)
            if source_hashes() != summary["source_sha256"]:
                summary["recording_errors"].append("source-changed-during-experiment")
            summary["outcome"] = "COMPLETED" if not summary["recording_errors"] else "FAIL"
    except BaseException as exc:
        summary["outcome"] = "FAIL"
        summary["error"] = type(exc).__name__ + ":" + str(exc)[:512]
    finally:
        lab.save(evidence / "speculation.json", summary)
        print(json.dumps({"outcome": summary["outcome"], "summary": str(evidence / "speculation.json")}), flush=True)
    return 0 if summary["outcome"] in ("COMPLETED", "PREPARED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
