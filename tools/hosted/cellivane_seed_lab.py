#!/usr/bin/env python3
"""Launch Cellivane Seed with an owned, pinned small CPU tensor worker.

Smoke also starts an isolated Ollama candidate and uses the existing bridge.
Serving/scheduling is our Seed code; tensor computation is external llamafile.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from hashlib import sha256
import http.client
import json
import os
from pathlib import Path
import sys
import threading
import time
from uuid import uuid4

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "hosted" / "experimental"))
import cellivane_bridge_lab as lab
from cellivane_bridge.contracts import EngineConfig
from cellivane_bridge.runtime import run
from cellivane_bridge.storage import local_root
from cellivane_seed.core import SeedEngine
from cellivane_seed.server import Audit, Backend, SeedHTTPServer

ALIAS = lab.ALIAS


def exchange(port, path, payload=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=100)
    try:
        raw = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        conn.request("GET" if raw is None else "POST", path, body=raw,
                     headers={"Content-Type": "application/json", "Connection": "close"})
        reply = conn.getresponse()
        body = reply.read(256 * 1024 + 1)
        if len(body) > 256 * 1024:
            raise ValueError("response-size")
        return {"status": reply.status, "body": json.loads(body),
                "raw_response": body.decode("utf-8"), "request": payload}
    finally:
        conn.close()


def payload(prompt):
    return {"model": ALIAS, "messages": [{"role": "user", "content": prompt}],
            "stream": False, "temperature": 0, "max_tokens": 128}


def smoke_checks(observations):
    """A completed launcher is not a successful engine experiment."""
    errors = []
    for row in observations[:3]:
        cold, warm = row["cold"], row["warm"]
        if (cold["status"] != 200 or warm["status"] != 200
                or cold["body"].get("cellivane", {}).get("execution") != "computed"
                or warm["body"].get("cellivane", {}).get("execution") != "cache"
                or cold["body"].get("choices") != warm["body"].get("choices")
                or row["backend_calls_delta"] != 1):
            errors.append("cold-warm-contract:" + row["name"])
    shared = observations[3]
    if (any(row["status"] != 200 for row in shared["responses"])
            or {r["body"].get("cellivane", {}).get("execution") for r in shared["responses"]} != {"computed", "coalesced"}
            or shared["responses"][0]["body"].get("choices") != shared["responses"][1]["body"].get("choices")
            or shared["backend_calls_delta"] != 1 or shared["overload"]["status"] != 429):
        errors.append("coalescing-or-overload-contract")
    report = observations[4]["report"]
    if (report["outcome"] != "COMPLETE" or report["dispatch_count"] != 2
            or not report["output_allowed"] or len(report["results"]) != 2
            or any(row["outcome"] != "ANSWERED" for row in report["results"].values())):
        errors.append("bridge-contract")
    return errors


def smoke(port, engine, root, evidence):
    observations = []
    for name, prompt, expected in lab.CASES:
        before = engine.status()["backend_calls"]
        begin = time.perf_counter()
        cold = exchange(port, "/v1/chat/completions", payload(prompt))
        cold_s = time.perf_counter() - begin
        begin = time.perf_counter()
        warm = exchange(port, "/v1/chat/completions", payload(prompt))
        warm_s = time.perf_counter() - begin
        text = cold["body"].get("choices", [{}])[0].get("message", {}).get("content")
        observations.append({"name": name, "expected": expected, "cold": cold, "warm": warm,
                             "cold_s": cold_s, "warm_s": warm_s, "exact": text == expected,
                             "backend_calls_delta": engine.status()["backend_calls"] - before})
    # Two equal requests merge while a different prompt has no free compute lease.
    concurrent_payload = payload("Reply with exactly this text and nothing else: SEED_SHARED_OK")
    before = engine.status()["backend_calls"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(exchange, port, "/v1/chat/completions", concurrent_payload)
        until = time.monotonic() + 5
        while engine.status()["active"] == 0 and not first.done() and time.monotonic() < until:
            time.sleep(.005)
        second = pool.submit(exchange, port, "/v1/chat/completions", concurrent_payload)
        overload = exchange(port, "/v1/chat/completions", payload("A distinct overloaded prompt."))
        concurrent = [first.result(), second.result()]
    observations.append({"name": "coalescing-and-overload", "responses": concurrent,
                         "overload": overload,
                         "backend_calls_delta": engine.status()["backend_calls"] - before})
    bridge_config = {"schema_version": 1, "storage_root": str(root), "parallelism": 1,
                     "deadline_s": 180, "engines": [
        EngineConfig("seed-reference", "openai", f"http://127.0.0.1:{port}/v1", ALIAS,
                     "reference", 90, 128).record(),
        EngineConfig("ollama-candidate", "ollama", "http://127.0.0.1:18932", ALIAS + ":latest",
                     "candidate", 90, 128).record()]}
    bridge_engines = [EngineConfig(**row) for row in bridge_config["engines"]]
    report = run(bridge_config, bridge_engines, "Reply with exactly this text and nothing else: CELLIVANE_OK")
    observations.append({"name": "bridge", "report": report})
    lab.save(evidence / "observations.json", observations)
    return observations


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("serve", "smoke"), required=True)
    parser.add_argument("--storage-root", default=str(lab.default_data_root() / "seed-lab"))
    parser.add_argument("--asset-root", default=str(lab.default_data_root() / "bridge-lab"))
    parser.add_argument("--asset-cache", default=str(lab.default_data_root() / "asset-cache"))
    parser.add_argument("--port", type=int, default=18941)
    parser.add_argument("--worker-port", type=int, default=18942)
    args = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("owned small-model launcher is currently Windows only")
    if (not 1024 <= args.port <= 65535 or not 1024 <= args.worker_port <= 65535
            or args.port == args.worker_port or 18932 in (args.port, args.worker_port)):
        parser.error("distinct nonreserved ports 1024..65535 required")
    root, asset_root = local_root(args.storage_root), local_root(args.asset_root)
    evidence = local_root(root / "lab-runs") / str(uuid4())
    evidence.mkdir()
    summary = {"schema_version": 1, "engine": "cellivane-seed", "version": "0.1.0",
               "mode": args.mode, "scope": "isolated-windows-cpu-small-model",
               "tensor_backend": "llamafile-0.10.5-thin", "model": ALIAS,
               "model_sha256": lab.MODEL_SHA, "binary_sha256": lab.LLAMAFILE_SHA,
               "token_mtp": False, "kv_sharing": False, "independent_tensor_kernels": False,
               "canonical_main_binding": False, "quality_improvement_claim": False,
               "source_sha256": {str(p.relative_to(REPO)).replace('\\', '/'): lab.digest(p)
                   for p in sorted([REPO / "hosted" / "experimental" / "cellivane_seed" / name
                       for name in ("__init__.py", "__main__.py", "core.py", "server.py")]
                       + list((REPO / "hosted" / "experimental" / "cellivane_bridge").glob("*.py"))
                       + [Path(__file__), Path(lab.__file__)])},
               "launcher_sha256": lab.digest(__file__), "cleanup": []}
    owned, engine, server, thread, audit = [], None, None, None, None
    try:
        # Never download or overwrite assets silently; every use rehashes them.
        model = asset_root / "models" / lab.MODEL_NAME
        binary = asset_root / "engines" / "llamafile-0.10.5-thin.exe"
        for path, expected in ((model, lab.MODEL_SHA), (binary, lab.LLAMAFILE_SHA)):
            local_root(path.parent)
            if path.is_symlink() or not path.is_file() or lab.digest(path) != expected:
                raise ValueError("pinned-asset-missing-or-changed: " + str(path))
        for port in (args.port, args.worker_port) + ((18932,) if args.mode == "smoke" else ()):
            lab.port_free(port)
        with ExitStack() as stack:
            worker_args = [str(binary), "-m", str(model), "--server", "--host", "127.0.0.1",
                           "--port", str(args.worker_port), "-c", "2048", "-ngl", "0",
                           "-t", "4", "--parallel", "1", "--alias", ALIAS,
                           "--reasoning", "off", "--no-webui"]
            worker = lab.OwnedProcess(stack, worker_args, lab.child_env(root, binary.parent), evidence, "seed-worker")
            owned.append(worker)
            lab.ready(worker.process, args.worker_port, "/health")
            if args.mode == "smoke":
                cache = Path(args.asset_cache)
                assets = lab.prepare(asset_root, cache)
                ollama = lab.OwnedProcess(stack, [str(assets["ollama"]), "serve"],
                    lab.child_env(asset_root, assets["ollama"].parent), evidence, "ollama-candidate")
                owned.append(ollama)
                lab.ready(ollama.process, 18932, "/api/version")
                model_file = evidence / "Modelfile"
                model_file.write_text('FROM "' + str(model).replace('\\', '/') + '"\nPARAMETER num_ctx 2048\nPARAMETER temperature 0\nPARAMETER num_gpu 0\nPARAMETER num_thread 4\n', encoding="utf-8")
                create = lab.OwnedProcess(stack, [str(assets["ollama"]), "create", ALIAS, "-f", str(model_file)],
                    lab.child_env(asset_root, assets["ollama"].parent), evidence, "ollama-create")
                owned.append(create)
                if create.process.wait(timeout=90) != 0:
                    raise RuntimeError("ollama-model-create")
            audit = Audit(root)
            backend_config = EngineConfig("seed-tensor-worker", "openai",
                f"http://127.0.0.1:{args.worker_port}/v1", ALIAS, timeout_s=90, max_tokens=128)
            identity = sha256(json.dumps({"binary": lab.LLAMAFILE_SHA, "model": lab.MODEL_SHA,
                "arguments": worker_args}, sort_keys=True).encode()).hexdigest()
            audit.append("configuration", {"engine": "cellivane-seed", "version": "0.1.0",
                "model": ALIAS, "identity": identity, "slots": 1,
                "backend": backend_config.record(), "worker_args": worker_args,
                "model_sha256": lab.MODEL_SHA, "binary_sha256": lab.LLAMAFILE_SHA})
            engine = SeedEngine(Backend(backend_config, audit), model=ALIAS, identity=identity,
                event_sink=lambda event: audit.append("scheduler", event))
            server = SeedHTTPServer(("127.0.0.1", args.port), engine, audit)
            summary["audit_directory"] = str(audit.directory)
            summary["backend_identity"] = identity
            print(json.dumps({"ready": True, "url": f"http://127.0.0.1:{args.port}/v1",
                "model": ALIAS, "evidence_directory": str(evidence)}, ensure_ascii=False), flush=True)
            try:
                if args.mode == "smoke":
                    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05})
                    thread.start()
                    summary["observations"] = smoke(args.port, engine, root, evidence)
                else:
                    server.serve_forever(poll_interval=.1)
            except KeyboardInterrupt:
                summary["user_interrupt"] = True
            finally:
                try:
                    engine.close()
                finally:
                    try:
                        if thread is not None:
                            server.shutdown()
                            thread.join(10)
                            if thread.is_alive():
                                raise RuntimeError("seed-http-thread-still-alive")
                    finally:
                        server.server_close()
                summary["state"] = engine.status()
                audit.append("shutdown", summary["state"])
            summary["execution_outcome"] = "COMPLETED"
            summary["errors"] = smoke_checks(summary["observations"]) if args.mode == "smoke" else []
            if args.mode == "smoke" and (summary["state"]["failures"] or summary["state"]["poisoned"]):
                summary["errors"].append("scheduler-failure")
            summary["outcome"] = "PASS" if not summary["errors"] else "FAIL"
    except BaseException as exc:
        summary["outcome"] = "FAIL"
        summary["error"] = type(exc).__name__ + ":" + str(exc)[:512]
    finally:
        summary["cleanup"] = [p.cleanup for p in owned]
        summary["source_unchanged_during_run"] = all(
            (REPO / path).is_file() and lab.digest(REPO / path) == expected
            for path, expected in summary["source_sha256"].items())
        if not summary["source_unchanged_during_run"]:
            summary["outcome"] = "FAIL"
            summary.setdefault("errors", []).append("source-changed-during-run")
        summary["ports_closed"] = {}
        for port in (args.port, args.worker_port) + ((18932,) if args.mode == "smoke" else ()):
            try:
                lab.port_free(port)
                summary["ports_closed"][str(port)] = True
            except OSError:
                summary["ports_closed"][str(port)] = False
        if any(not value for value in summary["ports_closed"].values()) or any(
                not p or not p.get("job_cleanup_confirmed") for p in summary["cleanup"]):
            summary["outcome"] = "FAIL"
            summary.setdefault("errors", []).append("cleanup-not-confirmed")
        lab.save(evidence / "lab-summary.json", summary)
        print(json.dumps({"outcome": summary["outcome"], "summary": str(evidence / "lab-summary.json")}), flush=True)
    return 0 if summary["outcome"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
