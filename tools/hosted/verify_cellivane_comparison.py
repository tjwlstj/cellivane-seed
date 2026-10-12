#!/usr/bin/env python3
"""Independent comparison replay using only the standard library.

Checks recorded wire, scheduler, corpus, metrics, placement and cleanup joins.
Unkeyed hashes and local receipts do not attest a backend, process or device.
Passing this replay is not a model quality, MTP or multi-GPU qualification.
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

import verify_cellivane_bridge as bridge_verifier

SOURCE_PATHS = {"hosted/experimental/cellivane_bridge/" + n + ".py" for n in
    ("__init__", "__main__", "adapters", "cli", "contracts", "runtime", "storage")} | {
    "hosted/experimental/cellivane_seed/" + n + ".py" for n in
    ("__init__", "__main__", "core", "server", "ollama", "placement")} | {
    "tools/hosted/" + n + ".py" for n in
    ("cellivane_bridge_lab", "cellivane_seed_lab", "cellivane_seed_compare")}
MODELS = {
    "0.6b": (639446688, "9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031"),
    "1.7b": (1834426016, "061b54daade076b5d3362dac252678d17da8c68f07560be70818cace6590cb1a"),
    "4b": (4280404704, "8c2f07f26af9747e41988551106f149b03eb9b5cb6df636027b6bf6278473300"),
}
CASES = [
    {"id": "add", "prompt": "Reply with only the number: 7 + 8 =", "expected": "15"},
    {"id": "multiply", "prompt": "Reply with only the number: 12 * 12 =", "expected": "144"},
    {"id": "control", "prompt": "Reply with exactly this text: CELLIVANE_OK", "expected": "CELLIVANE_OK"},
    {"id": "capital-ko", "prompt": "대한민국의 수도를 한 단어로만 답해 줘.", "expected": "서울"},
    {"id": "translate-ko", "prompt": "영어 apple의 한국어 번역을 한 단어로만 답해 줘.", "expected": "사과"},
    {"id": "extract-ko", "prompt": "문장 '세종은 책을 읽었다.'에서 주어만 답해 줘.", "expected": "세종"},
    {"id": "list-count", "prompt": "How many prime numbers are in [2, 3, 4, 5, 9, 11]? Reply only with the count.", "expected": "4"},
    {"id": "logic", "prompt": "A is before B. B is before C. Which letter is in the middle? Reply with one letter.", "expected": "B"},
    {"id": "json-read", "prompt": 'Given {"a": 3, "b": 9}, return only the value of b.', "expected": "9"},
    {"id": "python", "prompt": "What does sum(range(5)) evaluate to in Python? Return only the number.", "expected": "10"},
    {"id": "binary", "prompt": "Convert binary 1010 to decimal. Return only the number.", "expected": "10"},
    {"id": "unknown-ko", "prompt": "이 글에는 가방 색깔이 없습니다. '나는 가방을 들었다.' 가방 색깔은? 알 수 없으면 '모름'만 답해 줘.", "expected": "모름"},
]
SYSTEM = "Answer briefly and accurately. Follow the requested output format. /no_think"
TIMING_SCOPE = "successful-computed-responses-only; no Seed result cache; backend prompt KV reuse may occur"
COUNTERS = ("backend_calls", "computed", "cache_hits", "coalesced", "failures", "rejected", "timeouts")
STATS = {"total_duration", "load_duration", "prompt_eval_count", "prompt_eval_cached_count",
         "prompt_eval_duration", "eval_count", "eval_duration"}


def check(condition, reason):
    if not condition:
        raise ValueError(reason)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return sha256(encoded(value)).hexdigest()


def integer(value, low=0, high=2**63):
    return type(value) is int and low <= value < high


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 86400


def hex_digest(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def keys(value, expected, reason):
    check(type(value) is dict and set(value) == set(expected), reason)


def same(left, right):
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return set(left) == set(right) and all(same(left[k], right[k]) for k in left)
    if type(left) is list:
        return len(left) == len(right) and all(same(a, b) for a, b in zip(left, right))
    return left == right


def load(raw):
    check(type(raw) is bytes and len(raw) <= 32 * 1024**2, "json-bound")
    def pairs(items):
        value = {}
        for key, item in items:
            check(key not in value, "duplicate-json-key")
            value[key] = item
        return value
    def constant(_value):
        raise ValueError("nonfinite-json")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    def visit(item, depth=0):
        check(depth <= 20, "json-depth")
        if type(item) is dict:
            for part in item.values():
                visit(part, depth + 1)
        elif type(item) is list:
            for part in item:
                visit(part, depth + 1)
        elif type(item) is float:
            check(math.isfinite(item), "nonfinite-number")
        elif type(item) is str:
            item.encode("utf-8")
    visit(value)
    return value


def safe_path(path):
    check(not str(path).startswith(("\\\\", "//")), "network-path")
    path = Path(path).absolute()
    for part in (path, *path.parents):
        check(not part.is_symlink(), "symlink-path")
        if part.exists():
            check(not getattr(part.lstat(), "st_file_attributes", 0) & 0x400, "reparse-path")
    return path


def raw_file(path, limit):
    path = safe_path(path)
    check(path.is_file() and path.stat().st_size <= limit, "file-bound")
    raw = path.read_bytes()
    check(len(raw) <= limit, "file-bound")
    return raw


def wire(value, limit=256 * 1024):
    check(type(value) is str, "wire-type")
    raw = value.encode("utf-8")
    check(len(raw) <= limit, "wire-size")
    return raw


def text(value):
    return (type(value) is str and bool(value.strip()) and len(value.encode("utf-8")) <= 256 * 1024
            and not any(ord(c) < 32 and c not in "\n\t" for c in value)
            and "<think>" not in value.lower() and "</think>" not in value.lower())


def plan_identity(summary):
    plan = summary["plan"]
    keys(plan, ("schema_version", "backend", "profile", "num_ctx", "num_thread", "slots", "num_gpu", "temperature", "scope"), "plan-fields")
    for field, value in (("schema_version", 1), ("num_ctx", 2048), ("num_thread", 4), ("slots", 1), ("temperature", 0)):
        check(type(plan[field]) is int and plan[field] == value, "plan-" + field)
    profile, gpu = plan["profile"], plan["num_gpu"]
    check(plan["backend"] == "ollama" and plan["scope"] == "requested-placement-only", "plan-scope")
    check(type(gpu) is int and ((profile == "cpu" and gpu == 0) or (profile == "hybrid" and 1 <= gpu <= 128)
          or (profile == "auto" and gpu == -1)), "plan-num-gpu")
    check(summary["plan_sha256"] == digest(plan), "plan-digest")
    check(summary["model"] in MODELS, "model-profile")
    size, model_hash = MODELS[summary["model"]]
    check(type(summary["model_bytes"]) is int and summary["model_bytes"] == size and summary["model_sha256"] == model_hash, "model-pin")
    check(summary["alias"] == "cellivane-compare-" + summary["model"] + "-" + profile + ":latest", "model-alias")
    members = summary["backend_members_sha256"]
    check(type(members) is dict and 1 <= len(members) <= 10000, "backend-members")
    for name, value in members.items():
        path = PurePosixPath(name)
        check(not path.is_absolute() and ".." not in path.parts and ":" not in name and "\\" not in name
              and path.as_posix() == name and path.suffix.lower() in (".exe", ".dll") and hex_digest(value), "backend-member")
    check("ollama.exe" in members and summary["backend_tree_sha256"] == digest(members), "backend-tree-digest")
    check(summary["backend_archive_sha256"] == "e29ad1d5063dd4b54b2492d9b00adab2cff9621bfa654b6c92aa8d6f1fdfe7fc", "backend-archive-pin")
    expected = digest({"schema_version": 1, "model_sha256": model_hash,
        "backend_tree_sha256": summary["backend_tree_sha256"], "plan_sha256": summary["plan_sha256"]})
    check(summary["artifact_identity"] == expected and type(summary["identity_schema_version"]) is int
          and summary["identity_schema_version"] == 2, "artifact-identity")
    configuration = {"schema_version": 1, "alias": summary["alias"],
        "options": {"num_ctx": 2048, "num_thread": 4, "num_gpu": gpu, "temperature": 0},
        "environment": {"CUDA_VISIBLE_DEVICES": "-1" if profile == "cpu" else "0", "HIP_VISIBLE_DEVICES": "-1",
            "ROCR_VISIBLE_DEVICES": "-1", "OLLAMA_VULKAN": "0", "OLLAMA_NO_CLOUD": "1", "OLLAMA_NUM_PARALLEL": "1",
            "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_CONTEXT_LENGTH": "2048", "OLLAMA_DEBUG": "1"},
        "device_scope": "one visible NVIDIA device for hybrid/auto; CPU-only for cpu"}
    check(same(summary["execution_configuration"], configuration)
          and summary["execution_configuration_sha256"] == digest(configuration), "execution-configuration")
    check(summary["identity"] == digest({"schema_version": 2, "artifact_identity": expected,
          "execution_configuration_sha256": summary["execution_configuration_sha256"]}), "execution-identity")


def payload(alias, prompt):
    return {"model": alias, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            "temperature": 0, "max_tokens": 64, "stream": False}


def backend_receipt(row, request):
    keys(row, ("name", "kind", "model", "outcome", "text", "elapsed_s", "request_sha256", "response_sha256",
        "raw_response", "error", "usage", "request_body", "response_body", "http_status"), "backend-fields")
    check(row["name"] == "ollama-tensor" and row["kind"] == "ollama" and row["model"] == request["model"] and number(row["elapsed_s"]), "backend-identity")
    raw_request = wire(row["request_body"], 16 * 1024)
    check(sha256(raw_request).hexdigest() == row["request_sha256"], "backend-request-hash")
    wanted = {"model": request["model"], "messages": request["messages"], "stream": False, "think": False, "options": {"num_predict": 64}}
    check(same(load(raw_request), wanted) and raw_request == encoded(wanted), "backend-request-binding")
    response = None
    if row["response_body"] is not None:
        raw = wire(row["response_body"])
        check(sha256(raw).hexdigest() == row["response_sha256"], "backend-response-hash")
        response = load(raw)
        check(same(response, row["raw_response"]), "backend-response-parse")
    else:
        check(row["raw_response"] is None and (row["response_sha256"] is None or hex_digest(row["response_sha256"])), "backend-missing-wire")
    if row["outcome"] == "UNKNOWN":
        check(row["text"] is None and row["usage"] is None and type(row["error"]) is str and row["error"]
              and (row["http_status"] is None or integer(row["http_status"], 100, 600)), "backend-failure")
        count = response.get("eval_count") if type(response) is dict else None
        duration = response.get("eval_duration") if type(response) is dict else None
        rate = count * 1e9 / duration if type(count) is int and type(duration) is int and duration > 0 else None
        return None, rate
    check(row["outcome"] == "ANSWERED" and row["http_status"] == 200 and type(row["http_status"]) is int
          and row["error"] is None and type(response) is dict, "backend-not-answered")
    check({"model", "message", "done", "done_reason"} <= set(response)
          and set(response) <= {"model", "message", "done", "done_reason", "created_at"} | STATS, "ollama-fields")
    check(response["model"] == request["model"] and response["done"] is True and response["done_reason"] == "stop", "ollama-finish")
    keys(response["message"], ("role", "content"), "ollama-message")
    content = response["message"]["content"]
    check(response["message"]["role"] == "assistant" and text(content) and row["text"] == content, "ollama-content")
    for name in STATS & set(response):
        check(integer(response[name]), "ollama-statistic")
    if "created_at" in response:
        check(type(response["created_at"]) is str and 0 < len(response["created_at"]) <= 128, "ollama-created")
    counts = {name: response[name] for name in ("prompt_eval_count", "eval_count") if name in response}
    check(same(row["usage"], counts or None), "ollama-usage-binding")
    if set(counts) != {"prompt_eval_count", "eval_count"} or any(not integer(n, 0, 2**62) for n in counts.values()):
        return None, None  # Seed rejects unavailable/oversized token counters.
    value = {"model": request["model"], "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": counts["prompt_eval_count"], "completion_tokens": counts["eval_count"], "total_tokens": sum(counts.values())}}
    check(len(encoded(value)) <= 256 * 1024 - 1024, "normalized-size")
    duration = response.get("eval_duration")
    rate = counts["eval_count"] * 1e9 / duration if type(duration) is int and duration > 0 else None
    return value, rate


def exchange(response):
    keys(response, ("status", "body", "raw_response", "request"), "http-exchange-fields")
    check(integer(response["status"], 100, 600) and same(load(wire(response["raw_response"])), response["body"]), "http-wire-parse")


def audit_records(directory):
    directory = safe_path(directory)
    check(directory.is_absolute() and directory.is_dir(), "audit-directory")
    files = sorted(directory.iterdir())
    check(5 <= len(files) <= 10000 and sum(p.stat().st_size for p in files) <= 64 * 1024**2, "audit-bound")
    records, previous = [], None
    for seq, path in enumerate(files, 1):
        check(path.name == f"{seq:06d}.json", "audit-filename")
        raw = raw_file(path, 1024 * 1024)
        record = load(raw)
        keys(record, ("seq", "previous_sha256", "kind", "value"), "audit-fields")
        check(type(record["seq"]) is int and record["seq"] == seq and record["previous_sha256"] == previous
              and encoded(record) == raw, "audit-chain")
        previous = sha256(raw).hexdigest()
        records.append(record)
    return records


def observed_placement(summary, path):
    raw = raw_file(Path(path).parent / "ollama.log", 1024 * 1024)
    check(sha256(raw).hexdigest() == summary["log_sha256"], "placement-log-hash")
    log = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    counts, runtimes, buffers = set(), set(), []
    native, structured, unavailable = False, False, False
    for line in log.splitlines():
        # DEBUG discovery failures are not inference failures. Real error levels,
        # native CUDA errors and terminated runners remain fail closed.
        if re.match(r'^time=\S+\s+level=DEBUG\s+source=native_probe\.go:[0-9]+\s+msg="native GPU discovery failed"\s+.*stderr="ggml_cuda_init: failed to initialize CUDA: no CUDA-capable device is detected(?:\\r|\\n|\\"|"|$)', line):
            unavailable = True
            continue
        check(not re.search(r"\blevel=(?:ERROR|FATAL)\b|\bCUDA error\b|\bout of memory\b|\bfailed to (?:allocate|load|initialize)\b|\berror loading model\b|\bfatal error\b|\brunner process has terminated\b", line, re.I), "placement-runtime-error")
        if re.search(r"no usable GPU found|no compatible GPUs|no CUDA-capable device|falling back to CPU", line, re.I):
            unavailable = True
        match = re.fullmatch(r"(?:llama_model_)?load_tensors:\s+offloaded ([0-9]+)/([0-9]+) layers to GPU\s*", line)
        if match:
            off, total = map(int, match.groups())
            check(0 <= off <= total <= 4096 and total > 0, "placement-layer-range")
            counts.add((off, total))
            native = True
        elif re.match(r"(?:llama_model_)?load_tensors:\s+offloaded\b", line):
            raise ValueError("placement-layer-format")
        if re.match(r'^time=\S+\s+level=(?:INFO|DEBUG)\s+source=\S+\s+msg="offload to (cpu|cuda|vulkan)"(?:\s|$)', line, re.I):
            fields = re.findall(r'(?:^|\s)layers\.(offload|model)=(\S+)', line)
            check(len(fields) == 2 and {k for k, _v in fields} == {"offload", "model"}
                  and all(re.fullmatch(r"[0-9]{1,4}", v) for _k, v in fields), "structured-layers")
            values = {k: int(v) for k, v in fields}
            off, total = values["offload"], values["model"]
            check(0 <= off <= total <= 4096 and total > 0, "placement-layer-range")
            counts.add((off, total))
            structured = True
        match = re.match(r"load_backend:\s+loaded (CPU|CUDA|Vulkan) backend\b", line, re.I) or re.match(r'^time=\S+\s+level=INFO\s+source=\S+\s+msg="inference compute".*\blibrary=(cpu|cuda|vulkan)(?:\s|$)', line, re.I)
        if match:
            runtimes.add(match.group(1).upper())
        match = re.fullmatch(r"(?:load_tensors|llama_model_load_tensors|llama_context|llama_kv_cache|sched_reserve):\s+(CPU|CUDA[0-9]*|Vulkan[0-9]*)\s+(model|KV|compute|output) buffer size\s*=\s*([0-9]+(?:\.[0-9]+)?) MiB\s*", line, re.I)
        if match:
            device, kind, size = match.groups()
            size = float(size)
            check(math.isfinite(size) and 0 <= size <= 2**40, "placement-buffer-range")
            runtime = re.sub(r"[0-9]+$", "", device).upper()
            runtimes.add(runtime)
            buffers.append({"runtime": runtime, "device": device.upper(), "kind": kind.lower(), "mib": size})
    check(len(counts) <= 1, "placement-conflicting-layers")
    off, total = next(iter(counts)) if counts else (None, None)
    observed = summary["placement_observed"]
    keys(observed, ("schema_version", "scope", "status", "offloaded_layers", "total_layers", "offload_count_source",
        "runtimes", "buffers", "gpu_offload_observed", "gpu_qualified", "multi_gpu_qualified", "errors", "warnings", "log_sha256"), "placement-fields")
    check(type(observed) is dict and observed.get("schema_version") == 1 and observed.get("scope") == "run-log-observation-only"
          and observed.get("log_sha256") == sha256(log.encode("utf-8")).hexdigest() and observed.get("gpu_qualified") is False
          and observed.get("multi_gpu_qualified") is False, "placement-scope")
    check(same(observed.get("offloaded_layers"), off) and same(observed.get("total_layers"), total)
          and same(observed.get("runtimes"), sorted(runtimes)) and same(observed.get("buffers"), buffers)
          and observed.get("offload_count_source") == ("native" if native else "structured" if structured else "none")
          and observed.get("errors") == [], "placement-log-binding")
    loaded = summary["loaded_models"]
    exchange(loaded)
    check(loaded["status"] == 200 and loaded["request"] is None and type(loaded["body"]) is dict
          and set(loaded["body"]) == {"models"}, "loaded-models-response")
    models = loaded["body"]["models"]
    check(type(models) is list and len(models) == 1, "loaded-model-count")
    model = models[0]
    check(type(model) is dict and model.get("name") == summary["alias"] and model.get("model") == summary["alias"]
          and hex_digest(model.get("digest")) and integer(model.get("size"), 1)
          and integer(model.get("size_vram")), "loaded-model-identity-numeric")
    check(type(model.get("context_length")) is int and model["context_length"] == 2048
          and type(model.get("details")) is dict and model["details"].get("quantization_level") == "Q8_0", "loaded-model-quant-context")
    gpu_buffer = any(b["runtime"] in ("CUDA", "VULKAN") and b["kind"] == "model" and b["mib"] > 0 for b in buffers)
    gpu = off is not None and off > 0 and bool({"CUDA", "VULKAN"} & runtimes) and (native or gpu_buffer) and not unavailable
    check(observed.get("gpu_offload_observed") is gpu and observed.get("status") in ("GPU_OFFLOAD_OBSERVED", "CPU_OBSERVED", "UNKNOWN"), "placement-status")
    if gpu:
        check(observed["status"] == "GPU_OFFLOAD_OBSERVED" and gpu_buffer and model["size_vram"] > 0, "placement-gpu-join")
    elif observed["status"] == "CPU_OBSERVED":
        check("CPU" in runtimes and buffers and not off and model["size_vram"] == 0, "placement-cpu-join")
    if summary["plan"]["profile"] == "cpu":
        check(not gpu and not gpu_buffer and not off and model["size_vram"] == 0 and observed["status"] == "CPU_OBSERVED"
              and any(b["runtime"] == "CPU" and b["mib"] > 0 for b in buffers), "requested-cpu-violated")
    if summary["plan"]["profile"] == "hybrid":
        check(gpu and off == summary["plan"]["num_gpu"] and off < total and model["size_vram"] > 0, "requested-hybrid-violated")
    return {"status": observed["status"], "size_vram": model["size_vram"], "gpu_offload_observed": gpu}


def replay(summary_path, source_root=None):
    summary = load(raw_file(summary_path, 32 * 1024**2))
    keys(summary, ("schema_version", "experiment", "model", "alias", "model_sha256", "model_bytes", "backend_archive_sha256",
        "backend_members_sha256", "backend_tree_sha256", "plan", "plan_sha256", "identity", "source_sha256", "cases",
        "corpus_sha256", "system", "repeat", "max_tokens", "temperature", "think", "cache_entries", "rows", "cleanup",
        "gpu_before", "multi_gpu_qualified", "native_mtp", "quality_generalization_claim", "endpoint_pid_attestation",
        "audit_directory", "warmup", "warmup_wall_s", "loaded_models", "gpu_loaded", "metrics", "bridge_report", "outcome",
        "state", "ports_closed", "source_unchanged_during_run", "model_unchanged_during_run", "gpu_after", "placement_observed", "log_sha256",
        "identity_schema_version", "artifact_identity", "execution_configuration", "execution_configuration_sha256",
        "modelfile_sha256", "acceptance", "acceptance_errors"), "summary-fields")
    check(type(summary) is dict and type(summary.get("schema_version")) is int and summary["schema_version"] == 1
          and summary.get("experiment") == "cellivane-seed-comparison" and summary.get("outcome") == "COMPLETED"
          and "error" not in summary, "comparison-not-completed")
    check(all(summary.get(k) is False for k in ("multi_gpu_qualified", "native_mtp", "quality_generalization_claim", "endpoint_pid_attestation")), "unsupported-claim")
    check(summary.get("source_unchanged_during_run") is True and summary.get("model_unchanged_during_run") is True, "run-source-or-model-changed")
    sources = summary["source_sha256"]
    check(type(sources) is dict and set(sources) == SOURCE_PATHS and all(hex_digest(v) for v in sources.values()), "source-closure")
    if source_root is not None:
        root = safe_path(source_root)
        for relative, expected in sources.items():
            check(sha256(raw_file(root / relative, 4 * 1024**2)).hexdigest() == expected, "source-file-hash")
    plan_identity(summary)
    modelfile = raw_file(Path(summary_path).parent / "Modelfile", 4096)
    check(sha256(modelfile).hexdigest() == summary["modelfile_sha256"], "modelfile-hash")
    lines = modelfile.decode("utf-8").splitlines()
    model_name = "Qwen3-" + {"0.6b": "0.6B", "1.7b": "1.7B", "4b": "4B"}[summary["model"]] + "-Q8_0.gguf"
    check(len(lines) == 5 and re.fullmatch(r'FROM "[^"\r\n]+/' + re.escape(model_name) + r'"', lines[0])
          and lines[1:] == [f"PARAMETER {k} {summary['execution_configuration']['options'][k]}"
                           for k in ("num_ctx", "num_thread", "num_gpu", "temperature")], "modelfile-options")
    check(same(summary["cases"], CASES) and summary["corpus_sha256"] == digest(CASES) and summary["system"] == SYSTEM, "corpus-binding")
    check(type(summary["repeat"]) is int and 1 <= summary["repeat"] <= 3 and type(summary["max_tokens"]) is int
          and summary["max_tokens"] == 64 and type(summary["temperature"]) is int and summary["temperature"] == 0
          and summary["think"] is False and type(summary["cache_entries"]) is int and summary["cache_entries"] == 0, "comparison-budget")
    rows = summary["rows"]
    check(type(rows) is list and len(rows) == len(CASES) * summary["repeat"], "corpus-coverage")
    requests = [(summary["warmup"], payload(summary["alias"], "Reply with only OK."), None)]
    check(number(summary["warmup_wall_s"]), "warmup-wall")
    for row, (repeat, case) in zip(rows, ((r, c) for r in range(summary["repeat"]) for c in CASES)):
        keys(row, ("case", "repeat", "wall_s", "response", "text", "exact", "backend_exchange_count", "backend_receipt", "generation_tokens_s"), "comparison-row-fields")
        check(type(row["repeat"]) is int and row["repeat"] == repeat and row["case"] == case["id"] and number(row["wall_s"]), "case-order-or-wall")
        check(type(row["backend_exchange_count"]) is int and row["backend_exchange_count"] == 1, "row-backend-count")
        requests.append((row["response"], payload(summary["alias"], case["prompt"]), row))
    bridge = summary["bridge_report"]
    check(type(bridge) is dict and bridge.get("outcome") == "COMPLETE" and bridge.get("output_allowed") is True
          and type(bridge.get("dispatch_count")) is int and bridge["dispatch_count"] == 2
          and bridge.get("cancellation_requested") is False and bridge.get("deadline_exceeded") is False, "bridge-not-complete")
    bridge_directory = safe_path(bridge["run_directory"])
    bridge_verdict = bridge_verifier.replay(bridge_directory)
    check(bridge_verdict["verdict"] == "PASS" and bridge_verdict["run_outcome"] == "COMPLETE", "bridge-journal-replay")
    bridge_files = sorted(bridge_directory.iterdir())
    accepted = load(raw_file(bridge_files[0], 4 * 1024**2))["payload"]
    final = load(raw_file(bridge_files[-1], 4 * 1024**2))["payload"]
    check(same(bridge, final) and accepted["prompt"] == "Reply with exactly this text and nothing else: CELLIVANE_OK"
          and same(accepted["messages"], [{"role": "system", "content": "Answer the user briefly and accurately. /no_think"},
                                        {"role": "user", "content": accepted["prompt"]}])
          and type(accepted["parallelism"]) is int and accepted["parallelism"] == 1
          and type(accepted["deadline_s"]) is int and accepted["deadline_s"] == 180, "bridge-summary-join")
    engines = accepted["engines"]
    ports = summary["ports_closed"]
    check(len(engines) == 2 and {e["name"] for e in engines} == {"seed-reference", "ollama-candidate"}
          and all(e["model"] == summary["alias"] and e["max_tokens"] == 64 and e["timeout_s"] == 120 for e in engines), "bridge-engine-binding")
    ref = next(e for e in engines if e["name"] == "seed-reference")
    candidate = next(e for e in engines if e["name"] == "ollama-candidate")
    check(ref["kind"] == "openai" and ref["role"] == "reference" and candidate["kind"] == "ollama"
          and candidate["role"] == "candidate" and ref["base_url"].endswith("/v1")
          and {ref["base_url"].removeprefix("http://127.0.0.1:").removesuffix("/v1"), candidate["base_url"].removeprefix("http://127.0.0.1:")} == set(ports), "bridge-endpoint-binding")
    ref_result = bridge["results"]["seed-reference"]
    bridge_request = {"model": summary["alias"], "messages": accepted["messages"], "temperature": 0, "max_tokens": 64, "stream": False}
    check(same(load(wire(ref_result["request_body"])), bridge_request), "bridge-reference-request")
    requests.append(({"status": 200, "body": ref_result["raw_response"], "raw_response": ref_result["response_body"], "request": bridge_request}, bridge_request, None))
    records = audit_records(summary["audit_directory"])
    check(len(records) == 3 * len(requests) + 3 and records[0]["kind"] == "comparison_configuration"
          and records[-1]["kind"] == "shutdown", "audit-order-or-count")
    check(same(records[0]["value"], {k: summary[k] for k in ("identity", "plan", "model_sha256", "backend_tree_sha256", "corpus_sha256", "alias")}), "audit-configuration-binding")
    counts = {k: 0 for k in COUNTERS}
    event_seq, response_ids = 0, set()
    def event(record, kind, key, value=None):
        nonlocal event_seq
        check(record["kind"] == "scheduler", "scheduler-order")
        event_seq += 1
        row = record["value"]
        keys(row, ("seq", "kind", "key", "execution", "reason", "response_sha256", "active", "waiters", "cache_entries", "cache_bytes", *COUNTERS), "scheduler-fields")
        check(type(row["seq"]) is int and row["seq"] == event_seq and row["kind"] == kind and row["key"] == key, "scheduler-transition")
        check(row["execution"] == ("computed" if kind != "close" else None)
              and row["response_sha256"] == (digest(value) if value is not None else None), "scheduler-response-binding")
        check(row["reason"] == ("engine-closed" if kind == "close" else None) if kind != "failure"
              else row["reason"] in ("backend-error", "backend-deadline", "backend-size", "backend-text"), "scheduler-reason")
        expected = {**counts, "active": 1 if kind == "start" else 0, "waiters": 0, "cache_entries": 0, "cache_bytes": 0}
        check(all(type(row[k]) is int and row[k] == v for k, v in expected.items()), "scheduler-counters")
        return row
    for i, (response, wanted, comparison_row) in enumerate(requests):
        exchange(response)
        check(same(response["request"], wanted), "seed-request-binding")
        key = digest({"identity": summary["identity"], "inference": wanted})
        start, backend, finish = records[1 + 3 * i:4 + 3 * i]
        counts["backend_calls"] += 1
        event(start, "start", key)
        check(backend["kind"] == "backend_exchange", "backend-phase-order")
        receipt = backend["value"]
        value, rate = backend_receipt(receipt, wanted)
        if comparison_row is not None:
            check(same(comparison_row["backend_receipt"], receipt), "row-backend-receipt-join")
        if response["status"] == 200:
            check(value is not None, "failed-backend-promoted")
            counts["computed"] += 1
            event(finish, "finish", key, value)
            body = response["body"]
            keys(body, ("id", "object", "created", "model", "choices", "usage", "cellivane"), "seed-envelope-fields")
            check(same({k: body[k] for k in ("model", "choices", "usage")}, value) and body["object"] == "chat.completion"
                  and integer(body["created"]), "seed-normalized-join")
            check(type(body["id"]) is str and str(UUID(body["id"])) == body["id"] and body["id"] not in response_ids, "seed-response-id")
            response_ids.add(body["id"])
            check(same(body["cellivane"], {"engine": "seed", "version": "0.1.0", "execution": "computed", "key": key, "backend_calls": counts["backend_calls"]})
                  and type(body["cellivane"]["backend_calls"]) is int, "seed-execution-binding")
            answer = value["choices"][0]["message"]["content"]
        else:
            check(response["status"] == 502 and response["body"] == {"error": {"message": "backend-not-completed"}}, "comparison-failed-http")
            counts["failures"] += 1
            failed = event(finish, "failure", key)
            check(value is None or failed["reason"] == "backend-deadline", "valid-backend-false-failure")
            answer = ""
        if comparison_row is not None:
            case = CASES[i - 1 - ((i - 1) // len(CASES)) * len(CASES)]
            check(comparison_row["text"] == answer and type(comparison_row["exact"]) is bool
                  and comparison_row["exact"] == (response["status"] == 200 and answer.strip() == case["expected"]), "quality-score-binding")
            check(comparison_row["generation_tokens_s"] == rate, "generation-rate")
    event(records[-2], "close", None)
    state = summary["state"]
    keys(state, ("engine", "version", "model", "identity", "closed", "poisoned", "slots", "active", "inflight", "waiters",
        "max_waiters", "cache_entries", "cache_bytes", "cache_entries_limit", "cache_bytes_limit", *COUNTERS, "event_seq"), "shutdown-fields")
    check(same(records[-1]["value"], state) and type(state) is dict and state.get("engine") == "seed" and state.get("version") == "0.1.0"
          and state.get("model") == summary["alias"] and state.get("identity") == summary["identity"]
          and state.get("closed") is True and state.get("poisoned") is False, "shutdown-binding")
    expected_state = {**counts, "active": 0, "inflight": 0, "waiters": 0, "cache_entries": 0, "cache_bytes": 0,
        "cache_entries_limit": 0, "slots": 1, "event_seq": event_seq, "max_waiters": 8, "cache_bytes_limit": 1048576}
    check(all(type(state.get(k)) is int and state[k] == v for k, v in expected_state.items()), "shutdown-counters")
    good = [r for r in rows if r["response"]["status"] == 200]
    rates = [r["generation_tokens_s"] for r in good if r["generation_tokens_s"] is not None]
    expected_metrics = {"total": len(rows), "answered": len(good), "exact": sum(r["exact"] for r in rows),
        "median_wall_s": statistics.median([r["wall_s"] for r in good]) if good else None,
        "median_generation_tokens_s": statistics.median(rates) if rates else None, "timing_scope": TIMING_SCOPE}
    check(same(summary["metrics"], expected_metrics) and all(type(summary["metrics"][k]) is int for k in ("total", "answered", "exact")), "metric-recompute")
    cleanup = summary["cleanup"]
    check(type(cleanup) is list and len(cleanup) == 2 and {r.get("name") for r in cleanup} == {"ollama", "create"}, "cleanup-count")
    pids = set()
    for row in cleanup:
        keys(row, ("name", "pid", "method", "returncode", "process_exited", "process_error", "backend_stop_api_confirmed",
            "kv_release_confirmed", "job_assigned", "job_terminated", "job_active_processes", "job_closed", "job_cleanup_confirmed",
            "job_error", "created_suspended", "assigned_before_resume", "primary_thread_closed", "process_handle_closed"), "cleanup-fields")
        check(integer(row.get("pid"), 1) and row["pid"] not in pids and integer(row.get("returncode"), -(2**32), 2**32), "cleanup-pid")
        pids.add(row["pid"])
        check(row.get("method") == "owned-job-terminate-and-close" and all(row.get(k) is True for k in (
            "created_suspended", "assigned_before_resume", "primary_thread_closed", "process_handle_closed", "process_exited",
            "job_assigned", "job_terminated", "job_closed", "job_cleanup_confirmed"))
            and type(row.get("job_active_processes")) is int and row["job_active_processes"] == 0
            and row.get("job_error") is None and row.get("process_error") is None
            and row.get("backend_stop_api_confirmed") is False and row.get("kv_release_confirmed") is False, "cleanup-unconfirmed")
    check(type(ports) is dict and len(ports) == 2 and all(re.fullmatch(r"[0-9]{4,5}", p) and 1024 <= int(p) <= 65535
          and v is True for p, v in ports.items()), "port-receipts")
    placement = observed_placement(summary, summary_path)
    acceptance_errors = []
    if summary["warmup"]["status"] != 200:
        acceptance_errors.append("warmup-failed")
    if len(good) != len(rows):
        acceptance_errors.append("computed-corpus-incomplete")
    if counts["computed"] != len(requests) or counts["failures"]:
        acceptance_errors.append("scheduler-final-state")
    if placement["status"] == "UNKNOWN":
        acceptance_errors.append("placement-unresolved-or-failed")
    observed = summary["placement_observed"]
    if summary["plan"]["profile"] == "hybrid" and not (placement["gpu_offload_observed"]
            and type(observed["offloaded_layers"]) is int and type(observed["total_layers"]) is int
            and 0 < observed["offloaded_layers"] < observed["total_layers"]):
        acceptance_errors.append("hybrid-split-not-observed")
    acceptance = "FAIL" if acceptance_errors else "PASS"
    check(same(summary["acceptance_errors"], acceptance_errors) and summary["acceptance"] == acceptance, "acceptance-recompute")
    return {"outcome": "PASS", "scope": "comparison-record-integrity-and-semantic-replay", "experiment_completed": True,
        "operational_acceptance": acceptance, "acceptance_errors": acceptance_errors,
        "metrics": expected_metrics, "records": len(records), "placement_observed": placement,
        "source_files_checked": source_root is not None, "artifact_identity_scope": "model-backend-plan-hash-label-only",
        "actual_backend_qualification": False, "endpoint_pid_attestation": False, "process_liveness_verified": False,
        "quality_generalization_verified": False, "quality_improvement_verified": False, "native_mtp": False, "multi_gpu_qualified": False}


verify = replay


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary")
    parser.add_argument("--source-root")
    args = parser.parse_args(argv)
    try:
        verdict = replay(args.summary, args.source_root)
    except (ValueError, KeyError, TypeError, OSError, RecursionError, OverflowError) as exc:
        verdict = {"outcome": "FAIL", "reason": str(exc), "actual_backend_qualification": False,
                   "endpoint_pid_attestation": False, "native_mtp": False, "multi_gpu_qualified": False}
    print(json.dumps(verdict, ensure_ascii=False, allow_nan=False))
    return 0 if verdict["outcome"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
