#!/usr/bin/env python3
"""Owned Windows model comparison through Seed, with no result-cache timing.

Requires externally obtained, hash-pinned GGUF and verified Ollama assets. All
raw prompts, responses, absolute paths and engine logs remain in local storage.
The fixed twelve-case exact-answer corpus is a smoke comparison, not a general
model benchmark. No native MTP, training, multi-GPU or Strata qualification.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from hashlib import sha256
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
from uuid import uuid4

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "hosted" / "experimental"))
import cellivane_bridge_lab as lab
from cellivane_bridge.contracts import EngineConfig
from cellivane_bridge.runtime import run as bridge_run
from cellivane_bridge.storage import local_root, encoded
from cellivane_seed.core import SeedEngine
from cellivane_seed.server import Audit, SeedHTTPServer
from cellivane_seed.ollama import OllamaBackend
from cellivane_seed.placement import build_plan, artifact_identity, observe_placement
from cellivane_seed_lab import exchange

MODELS = {
    "0.6b": ("Qwen3-0.6B-Q8_0.gguf", 639446688,
        "9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031"),
    "1.7b": ("Qwen3-1.7B-Q8_0.gguf", 1834426016,
        "061b54daade076b5d3362dac252678d17da8c68f07560be70818cace6590cb1a"),
    "4b": ("Qwen3-4B-Q8_0.gguf", 4280404704,
        "8c2f07f26af9747e41988551106f149b03eb9b5cb6df636027b6bf6278473300"),
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
    {"id": "json-read", "prompt": "Given {\"a\": 3, \"b\": 9}, return only the value of b.", "expected": "9"},
    {"id": "python", "prompt": "What does sum(range(5)) evaluate to in Python? Return only the number.", "expected": "10"},
    {"id": "binary", "prompt": "Convert binary 1010 to decimal. Return only the number.", "expected": "10"},
    {"id": "unknown-ko", "prompt": "이 글에는 가방 색깔이 없습니다. '나는 가방을 들었다.' 가방 색깔은? 알 수 없으면 '모름'만 답해 줘.", "expected": "모름"},
]
SYSTEM = "Answer briefly and accurately. Follow the requested output format. /no_think"


def execution_configuration(alias, plan):
    """Allowlisted effective settings only; never dump the parent environment."""
    return {"schema_version": 1, "alias": alias, "options": plan.ollama_options(),
        "environment": {"CUDA_VISIBLE_DEVICES": "-1" if plan.profile == "cpu" else "0",
            "HIP_VISIBLE_DEVICES": "-1", "ROCR_VISIBLE_DEVICES": "-1", "OLLAMA_VULKAN": "0",
            "OLLAMA_NO_CLOUD": "1", "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1",
            "OLLAMA_CONTEXT_LENGTH": "2048", "OLLAMA_DEBUG": "1"},
        "device_scope": "one visible NVIDIA device for hybrid/auto; CPU-only for cpu"}


def acceptance_checks(summary):
    """Operational acceptance is separate from completing/scoring the corpus."""
    errors, count = [], len(CASES) * summary["repeat"]
    if summary.get("warmup", {}).get("status") != 200:
        errors.append("warmup-failed")
    rows = summary.get("rows", [])
    if len(rows) != count or any(r["response"]["status"] != 200 or r["backend_exchange_count"] != 1
            or r["response"]["body"].get("cellivane", {}).get("execution") != "computed" for r in rows):
        errors.append("computed-corpus-incomplete")
    bridge = summary.get("bridge_report", {})
    if (bridge.get("outcome") != "COMPLETE" or bridge.get("dispatch_count") != 2
            or bridge.get("output_allowed") is not True or len(bridge.get("results", {})) != 2
            or any(v.get("outcome") != "ANSWERED" for v in bridge.get("results", {}).values())):
        errors.append("bridge-incomplete")
    state = summary.get("state", {})
    if (state.get("backend_calls") != count + 2 or state.get("computed") != count + 2
            or state.get("closed") is not True or state.get("poisoned") is not False
            or any(state.get(k) != 0 for k in ("active", "inflight", "waiters", "cache_hits", "coalesced", "failures", "timeouts"))):
        errors.append("scheduler-final-state")
    observed = summary.get("placement_observed", {})
    if observed.get("status") in (None, "UNKNOWN", "FAILED"):
        errors.append("placement-unresolved-or-failed")
    if summary["plan"]["profile"] == "hybrid":
        n, total = observed.get("offloaded_layers"), observed.get("total_layers")
        if (observed.get("gpu_offload_observed") is not True or type(n) is not int
                or type(total) is not int or not 0 < n < total):
            errors.append("hybrid-split-not-observed")
    return errors


def source_hashes():
    # This experiment's frozen family does not import optional acceleration
    # modules. Keep its sixteen-file closure aligned with its independent replay.
    paths = [REPO / "hosted/experimental/cellivane_seed" / (name + ".py")
             for name in ("__init__", "__main__", "core", "server", "placement", "ollama")]
    paths.extend(REPO / "hosted/experimental/cellivane_bridge" / (name + ".py")
                 for name in ("__init__", "__main__", "adapters", "cli", "contracts", "runtime", "storage"))
    paths.extend(REPO / "tools/hosted" / name for name in (
        "cellivane_seed_compare.py", "cellivane_seed_lab.py", "cellivane_bridge_lab.py"))
    return {p.relative_to(REPO).as_posix(): lab.digest(p) for p in sorted(paths)}


def gpu_snapshot():
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used,memory.free",
            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True)
        return {"source": "nvidia-smi", "lines": result.stdout.strip().splitlines()}
    except (OSError, subprocess.SubprocessError):
        return {"source": "nvidia-smi", "unavailable": True}


def backend_tree(binary):
    root = binary.parent
    members = {p.relative_to(root).as_posix(): lab.digest(p)
        for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in (".exe", ".dll")}
    if not members:
        raise ValueError("backend-tree-empty")
    return members, sha256(encoded(members)).hexdigest()


def metrics(rows):
    good = [r for r in rows if r["response"]["status"] == 200]
    rates = [r["generation_tokens_s"] for r in good if r.get("generation_tokens_s") is not None]
    return {"total": len(rows), "answered": len(good), "exact": sum(r["exact"] for r in rows),
        "median_wall_s": statistics.median([r["wall_s"] for r in good]) if good else None,
        "median_generation_tokens_s": statistics.median(rates) if rates else None,
        "timing_scope": "successful-computed-responses-only; no Seed result cache; backend prompt KV reuse may occur"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=tuple(MODELS), required=True)
    parser.add_argument("--model-file", required=True)
    parser.add_argument("--asset-root", required=True, help="verified legacy lab asset root (Ollama archive/extraction)")
    parser.add_argument("--storage-root", required=True)
    parser.add_argument("--profile", choices=("cpu", "hybrid", "auto"), default="cpu")
    parser.add_argument("--layers", type=int, default=14)
    parser.add_argument("--repeat", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--port", type=int, default=18951)
    parser.add_argument("--worker-port", type=int, default=18952)
    args = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("owned comparison launcher currently Windows only; attached Seed backend is portable")
    if any(not 1024 <= n <= 65535 for n in (args.port, args.worker_port)) or args.port == args.worker_port:
        parser.error("distinct ports 1024..65535 required")
    plan = build_plan(args.profile, layers=args.layers)
    root, assets_root = local_root(args.storage_root), local_root(args.asset_root)
    evidence = local_root(root / "compare-runs") / str(uuid4())
    evidence.mkdir()
    model = Path(args.model_file)
    local_root(model.parent)
    name, size, expected = MODELS[args.model]
    if model.is_symlink() or model.name != name or model.stat().st_size != size or lab.digest(model) != expected:
        raise ValueError("pinned-model-missing-or-changed")
    # Revalidate the complete existing archive/extracted asset set before use.
    # prepare also checks llamafile; avoid copying by using already-pinned originals as cache.
    assets = lab.prepare(assets_root, Path(os.environ.get("LOCALAPPDATA", "")) / "AIOS/hosted-inference")
    members, tree_sha = backend_tree(assets["ollama"])
    alias = "cellivane-compare-" + args.model + "-" + args.profile + ":latest"
    artifact_id = artifact_identity(model_sha256=expected, backend_tree_sha256=tree_sha, plan_sha256=plan.sha256)
    execution_config = execution_configuration(alias, plan)
    configuration_sha = sha256(encoded(execution_config)).hexdigest()
    identity = sha256(encoded({"schema_version": 2, "artifact_identity": artifact_id,
        "execution_configuration_sha256": configuration_sha})).hexdigest()
    summary = {"schema_version": 1, "experiment": "cellivane-seed-comparison", "model": args.model,
        "alias": alias, "model_sha256": expected, "model_bytes": size,
        "backend_archive_sha256": lab.OLLAMA_ZIP_SHA, "backend_members_sha256": members,
        "backend_tree_sha256": tree_sha, "plan": plan.record(), "plan_sha256": plan.sha256,
        "identity": identity, "identity_schema_version": 2, "artifact_identity": artifact_id,
        "execution_configuration": execution_config, "execution_configuration_sha256": configuration_sha,
        "source_sha256": source_hashes(), "cases": CASES,
        "corpus_sha256": sha256(encoded(CASES)).hexdigest(), "system": SYSTEM,
        "repeat": args.repeat, "max_tokens": 64, "temperature": 0, "think": False,
        "cache_entries": 0, "rows": [], "cleanup": [], "gpu_before": gpu_snapshot(),
        "multi_gpu_qualified": False, "native_mtp": False, "quality_generalization_claim": False,
        "endpoint_pid_attestation": False}
    owned, engine, server, thread, audit = [], None, None, None, None
    try:
        lab.port_free(args.port)
        lab.port_free(args.worker_port)
        with ExitStack() as stack:
            env = lab.child_env(root, assets["ollama"].parent)
            env.update(execution_config["environment"])
            env["OLLAMA_HOST"] = f"127.0.0.1:{args.worker_port}"
            worker = lab.OwnedProcess(stack, [str(assets["ollama"]), "serve"], env, evidence, "ollama")
            owned.append(worker)
            version = lab.ready(worker.process, args.worker_port, "/api/version")
            if version != {"version": lab.OLLAMA_VERSION}:
                raise ValueError("backend-version")
            modelfile = evidence / "Modelfile"
            options = plan.ollama_options()
            modelfile.write_text('FROM "' + str(model).replace('\\', '/') + '"\n' +
                ''.join(f"PARAMETER {key} {value}\n" for key, value in options.items()), encoding="utf-8")
            summary["modelfile_sha256"] = lab.digest(modelfile)
            create = lab.OwnedProcess(stack, [str(assets["ollama"]), "create", alias, "-f", str(modelfile)],
                env, evidence, "create")
            owned.append(create)
            if create.process.wait(timeout=120) != 0:
                raise RuntimeError("model-create-failed")
            audit = Audit(root)
            summary["audit_directory"] = str(audit.directory)
            audit.append("comparison_configuration", {k: summary[k] for k in (
                "identity", "plan", "model_sha256", "backend_tree_sha256", "corpus_sha256", "alias")})
            backend = OllamaBackend(EngineConfig("ollama-tensor", "ollama",
                f"http://127.0.0.1:{args.worker_port}", alias, timeout_s=120, max_tokens=64), audit)
            engine = SeedEngine(backend, model=alias, identity=identity, cache_entries=0, timeout_s=120,
                event_sink=lambda event: audit.append("scheduler", event))
            server = SeedHTTPServer(("127.0.0.1", args.port), engine, audit)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05})
            thread.start()
            # Warm-up is separately recorded; excludes model loading from task timings.
            body = lambda prompt: {"model": alias, "messages": [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt}], "temperature": 0, "max_tokens": 64, "stream": False}
            start = time.perf_counter()
            summary["warmup"] = exchange(args.port, "/v1/chat/completions", body("Reply with only OK."))
            summary["warmup_wall_s"] = time.perf_counter() - start
            summary["loaded_models"] = exchange(args.worker_port, "/api/ps")
            summary["gpu_loaded"] = gpu_snapshot()
            for repeat in range(args.repeat):
                for case in CASES:
                    before = audit.sequence
                    start = time.perf_counter()
                    response = exchange(args.port, "/v1/chat/completions", body(case["prompt"]))
                    wall = time.perf_counter() - start
                    text = response["body"].get("choices", [{}])[0].get("message", {}).get("content", "")
                    exchanges = [json.loads(p.read_text(encoding="utf-8"))["value"]
                        for p in sorted(audit.directory.glob("*.json"))
                        if int(p.stem) > before and json.loads(p.read_text(encoding="utf-8"))["kind"] == "backend_exchange"]
                    raw = exchanges[0].get("raw_response") or {} if len(exchanges) == 1 else {}
                    n, duration = raw.get("eval_count"), raw.get("eval_duration")
                    row = {"case": case["id"], "repeat": repeat, "wall_s": wall,
                        "response": response, "text": text, "exact": response["status"] == 200 and text.strip() == case["expected"],
                        "backend_exchange_count": len(exchanges), "backend_receipt": exchanges[0] if len(exchanges) == 1 else None,
                        "generation_tokens_s": n * 1e9 / duration if type(n) is int and type(duration) is int and duration > 0 else None}
                    summary["rows"].append(row)
                    print(json.dumps({"model": args.model, "profile": args.profile, "case": case["id"],
                        "repeat": repeat, "status": response["status"], "exact": row["exact"], "wall_s": round(wall, 3)}, ensure_ascii=False), flush=True)
            summary["metrics"] = metrics(summary["rows"])
            # Exercise the public bridge with this very Seed/placement/model.
            bridge_engines = [
                EngineConfig("seed-reference", "openai", f"http://127.0.0.1:{args.port}/v1",
                    alias, "reference", 120, 64),
                EngineConfig("ollama-candidate", "ollama", f"http://127.0.0.1:{args.worker_port}",
                    alias, "candidate", 120, 64)]
            bridge_config = {"schema_version": 1, "storage_root": str(root), "parallelism": 1,
                "deadline_s": 180, "engines": [item.record() for item in bridge_engines]}
            summary["bridge_report"] = bridge_run(bridge_config, bridge_engines,
                "Reply with exactly this text and nothing else: CELLIVANE_OK")
            summary["outcome"] = "COMPLETED"
    except BaseException as exc:
        summary["outcome"] = "FAIL"
        summary["error"] = type(exc).__name__ + ":" + str(exc)[:512]
    finally:
        # No detached requests: the owned job context closes before final receipts.
        if engine is not None:
            engine.close()
        if thread is not None:
            server.shutdown()
            thread.join(10)
        if server is not None:
            server.server_close()
        if engine is not None:
            summary["state"] = engine.status()
            audit.append("shutdown", summary["state"])
        summary["cleanup"] = [p.cleanup for p in owned]
        summary["ports_closed"] = {}
        for port in (args.port, args.worker_port):
            try:
                lab.port_free(port)
                summary["ports_closed"][str(port)] = True
            except OSError:
                summary["ports_closed"][str(port)] = False
        summary["source_unchanged_during_run"] = source_hashes() == summary["source_sha256"]
        summary["model_unchanged_during_run"] = lab.digest(model) == expected
        summary["gpu_after"] = gpu_snapshot()
        log = evidence / "ollama.log"
        summary["placement_observed"] = observe_placement(log.read_text(encoding="utf-8", errors="replace") if log.exists() else "")
        summary["log_sha256"] = lab.digest(log) if log.exists() else None
        if (not summary["source_unchanged_during_run"] or not summary["model_unchanged_during_run"]
                or not all(summary["ports_closed"].values())
                or any(not p.get("job_cleanup_confirmed") for p in summary["cleanup"])):
            summary["outcome"] = "FAIL"
        summary["acceptance_errors"] = acceptance_checks(summary)
        if summary["outcome"] != "COMPLETED":
            summary["acceptance_errors"].append("experiment-not-completed")
        summary["acceptance"] = "FAIL" if summary["acceptance_errors"] else "PASS"
        lab.save(evidence / "comparison.json", summary)
        print(json.dumps({"outcome": summary["outcome"], "summary": str(evidence / "comparison.json")}), flush=True)
    return 0 if summary["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
