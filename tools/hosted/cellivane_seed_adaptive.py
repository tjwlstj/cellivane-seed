#!/usr/bin/env python3
"""Start one owned CPU worker after budget and advisory calibration selection.

Explicit target profiles are pinned Qwen3-0.6B, 1.7B or 4B Q8_0. Optional 0.6B
draft is accepted for the 1.7B/4B target. The CPU profile uses llama.cpp b11429,
context 2048, four threads, one slot, greedy output <=128 tokens. Auto
chooses once before launch. It never retries a dispatched request, dynamically
switches speculation, changes quantization, or promises a speed improvement.
Raw local run records contain prompts, responses, logs and filesystem paths.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
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
import cellivane_seed_speculate as spec
import cellivane_llama_assets as assets
import cellivane_bridge_lab as lab
import verify_cellivane_speculation as calibration_replay
from cellivane_bridge.adapters import _load
from cellivane_bridge.contracts import EngineConfig
from cellivane_bridge.storage import encoded, local_root, read_json
from cellivane_seed.adaptive import Calibration, StartupChoice, settings_identity
from cellivane_seed.capabilities import BackendCapabilities
from cellivane_seed.core import InvalidRequest, SeedEngine
from cellivane_seed.profiles import (ModelProfile, PROFILE_NAMES, get_profile,
                                    identify_profile, validate_pair)
from cellivane_seed.resources import ExecutionDemand, ResourceBudget, plan_admission
from cellivane_seed.server import Audit, Backend, SeedHTTPServer

SOURCE_PATHS = tuple(sorted(set(spec.SOURCE_PATHS) | {
    "hosted/experimental/cellivane_seed/adaptive.py",
    "hosted/experimental/cellivane_seed/profiles.py",
    "tools/hosted/cellivane_seed_adaptive.py",
    *{"tools/hosted/" + n + ".py" for n in (
        "verify_cellivane_speculation", "verify_cellivane_comparison", "verify_cellivane_bridge")},
}))


class BoundedSeedEngine(SeedEngine):
    """Enforce this admitted profile before allocating a compute lease."""

    def __init__(self, backend, *, choice, target_profile=None, draft_profile=None, **kwargs):
        if type(choice) is not StartupChoice or choice.selected is None:
            raise ValueError("adaptive-start-blocked")
        choice.selected.require_admitted()
        if target_profile is not None:
            validate_pair(target_profile, draft_profile)
            demand = choice.baseline.demand
            if (demand.target_sha256, demand.target_bytes) != (target_profile.sha256, target_profile.bytes):
                raise ValueError("adaptive-target-profile-demand-mismatch")
            if (choice.draft is not None) != (draft_profile is not None):
                raise ValueError("adaptive-draft-profile-demand-mismatch")
            if draft_profile is not None and (choice.draft.demand.draft_sha256, choice.draft.demand.draft_bytes) != (draft_profile.sha256, draft_profile.bytes):
                raise ValueError("adaptive-draft-profile-demand-mismatch")
        elif draft_profile is not None:
            raise ValueError("adaptive-target-profile-required")
        self._choice = choice
        self._profiles = (target_profile, draft_profile)
        super().__init__(backend, **kwargs)

    def _request(self, payload):
        normalized, key = super()._request(payload)
        if normalized["max_tokens"] > self._choice.maximum_output_tokens:
            raise InvalidRequest("adaptive-profile-max-tokens")
        return normalized, key

    def status(self):
        mode, reason = self._choice.decision()
        return {**super().status(), **({"profiles": profile_records(*self._profiles)} if self._profiles[0] else {}), "adaptive": {
            "policy": self._choice.policy, "mode": mode, "reason": reason,
            "expected_output_tokens": self._choice.expected_output_tokens,
            "maximum_output_tokens": self._choice.maximum_output_tokens,
            "choice_sha256": self._choice.sha256, "per_request_switching": False}}


def profile_records(target, draft=None):
    validate_pair(target, draft)
    return {"target": target.record(), "draft": draft.record() if draft else None}


def model_profiles(models):
    target = identify_profile(models["target"]["bytes"], models["target"]["sha256"])
    draft = identify_profile(models["draft"]["bytes"], models["draft"]["sha256"]) if "draft" in models else None
    validate_pair(target, draft)
    return target, draft


def checked_model(value, profile):
    if type(profile) is not ModelProfile:
        raise ValueError("model-profile-exact-type")
    profile.validate()
    path = Path(value)
    local_root(path.parent)
    if not path.is_file() or path.name != profile.filename:
        raise ValueError("model-profile-file-required:" + profile.name)
    before = path.lstat()
    if (path.is_symlink() or getattr(before, "st_file_attributes", 0) & 0x400
            or before.st_size != profile.bytes or assets.file_sha256(path) != profile.sha256):
        raise ValueError("model-profile-file-pin-mismatch:" + profile.name)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("model-profile-file-changed-during-hash")
    return {"path": str(path.resolve(strict=True)), "bytes": profile.bytes, "sha256": profile.sha256}


def source_hashes():
    return {name: assets.file_sha256(REPO / name) for name in SOURCE_PATHS}


def snapshot_sources(directory, hashes):
    for name, expected in hashes.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = (REPO / name).read_bytes()
        if spec.sha256(raw).hexdigest() != expected:
            raise ValueError("source-changed-before-snapshot")
        with target.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())


def load_calibration(path, source_root):
    """Replay raw evidence; a user-supplied PASS file alone is insufficient."""
    if path is None and source_root is None:
        return None, None
    if path is None or source_root is None:
        raise ValueError("calibration-report-and-source-root-required-together")
    path = Path(path)
    local_root(path.parent)
    local_root(source_root)
    before = spec.file_record(path)
    verdict = calibration_replay.replay(path, source_root)
    if verdict["outcome"] != "PASS" or verdict["source_files_checked"] is not True:
        raise ValueError("calibration-replay-not-passed")
    report = read_json(path, maximum=8 * 1024**2)
    if spec.file_record(path) != before:
        raise ValueError("calibration-report-changed-during-replay")
    replay_sha = spec.digest(verdict)
    c = Calibration(engine_identity=report["backend_assets"]["engine_identity"],
        target_sha256=report["models"]["target"]["sha256"],
        draft_sha256=report["models"]["draft"]["sha256"],
        settings_sha256=settings_identity(report["configuration"]),
        report_sha256=before["sha256"], replay_sha256=replay_sha,
        paired_total=verdict["paired_total"], paired_text_equal=verdict["paired_text_equal"],
        median_wall_ratio=verdict["median_paired_wall_speed_ratio"],
        speculation_observed=verdict["speculation_observed"])
    return c, {"report": before, "source_root": str(Path(source_root).resolve(strict=True)),
               "replay": verdict, "replay_sha256": replay_sha}


def choose(prepared, models, config, snapshot, *, policy, expected_output_tokens, calibration):
    # Estimates are deliberately explicit. Admission is neither RAM reservation
    # nor a measured peak. Snapshot is refreshed after all expensive hashing.
    budget = ResourceBudget(ram_available_bytes=snapshot["available_bytes"],
        ram_headroom_bytes=min(4 * 1024**3, snapshot["available_bytes"]))
    caps = BackendCapabilities(backend="llama.cpp", engine_identity=prepared["engine_identity"],
        declared_modes=("none", "draft"), declared_placements=("cpu",))
    plans = {}
    for mode in ("none", "draft") if "draft" in models else ("none",):
        demand = ExecutionDemand(engine_identity=prepared["engine_identity"],
            target_sha256=models["target"]["sha256"], target_bytes=models["target"]["bytes"],
            configuration_sha256=spec.digest({"configuration_sha256": spec.digest(config), "mode": mode}),
            mode=mode, draft_sha256=models["draft"]["sha256"] if mode == "draft" else None,
            draft_bytes=models["draft"]["bytes"] if mode == "draft" else 0,
            host_overhead_bytes=2 * 1024**3, context_workspace_bytes=512 * 1024**2)
        plans[mode] = plan_admission(caps, demand, budget)
    return StartupChoice(policy=policy, expected_output_tokens=expected_output_tokens,
        maximum_output_tokens=128, settings_sha256=settings_identity(config),
        baseline=plans["none"], draft=plans.get("draft"), calibration=calibration)


def status_exchange(port):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/v1/status", headers={"Connection": "close"})
        response = conn.getresponse()
        raw = response.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024 or response.getheader("Content-Type", "").split(";", 1)[0] != "application/json":
            raise ValueError("status-response-bounds-or-type")
        return {"status": response.status, "body": _load(raw),
                "raw_response": raw.decode("utf-8"), "request": None}
    finally:
        conn.close()


def validate_inputs(prepared, models, hashes):
    if source_hashes() != hashes:
        raise ValueError("adaptive-source-pin-mismatch")
    profiles = dict(zip(("target", "draft"), model_profiles(models)))
    for kind, model in models.items():
        if checked_model(model["path"], profiles[kind]) != model:
            raise ValueError("adaptive-model-pin-mismatch:" + kind)
    if assets.prepare_assets(Path(prepared["directory"]).parent, download=False) != prepared:
        raise ValueError("adaptive-assets-pin-mismatch")


def smoke_errors(phase):
    """Producer observations only; independent replay remains a separate step."""
    errors = []
    rows = ([phase["warmup"]] if phase["warmup"] else []) + phase["rows"]
    if len(rows) != 5 or any(row["response"]["status"] != 200
            or row["backend_exchange_count"] != 1 or not row["backend_receipt"]
            or row["backend_receipt"]["outcome"] != "ANSWERED"
            or row["response"]["body"].get("cellivane", {}).get("execution") != "computed" for row in rows):
        errors.append("computed-response-coverage")
    state = phase["state"] or {}
    if (state.get("closed") is not True or state.get("poisoned") is not False
            or state.get("backend_calls") != 5 or state.get("computed") != 5 or state.get("rejected") != 1
            or any(state.get(k) != 0 for k in ("active", "inflight", "waiters", "cache_hits", "coalesced", "failures", "timeouts"))):
        errors.append("final-state")
    if not phase["oversize_response"] or phase["oversize_response"]["status"] != 400:
        errors.append("oversize-not-rejected")
    if not phase["status_response"] or phase["status_response"]["status"] != 200:
        errors.append("status-unavailable")
    if phase["mode"] == "none" and (phase["speculation_observed"] or {}).get("draft_attempt_observed"):
        errors.append("unexpected-draft")
    return errors


def run_selected(args, root, evidence, prepared, models, config, hashes, calibration):
    directory = local_root(evidence / "worker")
    ports = (args.port, args.worker_port)
    phase = {"phase_id": None, "round": 0, "mode": None, "identity": None,
        "capabilities": None, "budget": None, "demand": None, "admission": None,
        "memory_snapshot": None, "command": None, "environment": None,
        "audit_directory": None, "warmup": None, "rows": [], "state": None,
        "cleanup": None, "ports_closed": {}, "worker_log": None, "speculation_observed": None,
        "owned_memory": None, "source_unchanged_during_run": False,
        "models_unchanged_during_run": False, "backend_assets_unchanged_during_run": False,
        "status_response": None, "oversize_response": None, "audit_status": None,
        "outcome": "NOT_STARTED", "error": None}
    child, engine, server, thread, audit, choice = None, None, None, None, None, None
    try:
        for port in ports:
            lab.port_free(port)
        validate_inputs(prepared, models, hashes)
        target_profile, draft_profile = model_profiles(models)
        # This is the final mutable-host observation before the owned launch.
        snapshot = spec.memory_snapshot()
        choice = choose(prepared, models, config, snapshot, policy=args.policy,
            expected_output_tokens=args.expected_output_tokens, calibration=calibration)
        mode, reason = choice.decision()
        phase["memory_snapshot"] = snapshot
        phase["mode"] = mode
        if choice.selected is None:
            raise ValueError("adaptive-start-blocked:" + reason)
        admission = choice.selected.require_admitted()
        cmd = spec.command(prepared, models, config, mode, args.worker_port)
        env = spec.cpu_environment(root, Path(prepared["server"]).parent)
        phase.update(phase_id="adaptive-" + mode, identity=admission.sha256,
            capabilities=admission.capabilities.record(), budget=admission.budget.record(),
            demand=admission.demand.record(), admission=admission.record(), command=cmd,
            environment={k: env[k] for k in ("CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES",
                "ROCR_VISIBLE_DEVICES", "GGML_VK_VISIBLE_DEVICES")})
        with ExitStack() as stack:
            child = lab.OwnedProcess(stack, cmd, env, directory, "worker")
            if lab.ready(child.process, args.worker_port, "/health", seconds=120) != {"status": "ok"}:
                raise ValueError("adaptive-worker-not-healthy")
            try:
                audit = Audit(root)
                phase["audit_directory"] = str(audit.directory)
                audit.append("adaptive_configuration", {"choice_sha256": choice.sha256,
                    "configuration_sha256": spec.digest(config), "mode": mode,
                    "admission_sha256": admission.sha256})
                backend = Backend(EngineConfig("llama-cpp-tensor", "openai",
                    f"http://127.0.0.1:{args.worker_port}/v1", config["alias"],
                    timeout_s=90, max_tokens=128), audit)
                engine = BoundedSeedEngine(backend, choice=choice, model=config["alias"],
                    target_profile=target_profile, draft_profile=draft_profile,
                    identity=admission.sha256, cache_entries=0, timeout_s=90,
                    event_sink=lambda event: audit.append("scheduler", event))
                server = SeedHTTPServer(("127.0.0.1", args.port), engine, audit)
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05})
                thread.start()
                print(json.dumps({"ready": True, "url": f"http://127.0.0.1:{args.port}/v1",
                    "mode": mode, "reason": reason, "choice_sha256": choice.sha256,
                    "maximum_output_tokens": 128, "audit": audit.status(),
                    "profiles": profile_records(target_profile, draft_profile),
                    "run_directory": str(evidence)}, ensure_ascii=False), flush=True)
                if args.mode == "smoke":
                    phase["warmup"] = spec.request_record(args.port, config, "Reply with only OK.", audit)
                    for case in spec.CASES:
                        row = {"case": case["id"], **spec.request_record(args.port, config, case["prompt"], audit)}
                        phase["rows"].append(row)
                        spec.file_record(directory / "worker.log")
                        print(json.dumps({"case": case["id"], "status": row["response"]["status"]}), flush=True)
                    oversized = {**spec.payload(config, "This must be rejected before backend dispatch."), "max_tokens": 129}
                    phase["oversize_response"] = spec.exchange(args.port, "/v1/chat/completions", oversized)
                else:
                    until = time.monotonic() + args.serve_seconds if args.serve_seconds else None
                    try:
                        while until is None or time.monotonic() < until:
                            if child.process.poll() is not None:
                                raise RuntimeError("adaptive-owned-worker-exited")
                            if audit.status()["poisoned"] or engine.status()["poisoned"]:
                                raise RuntimeError("adaptive-serving-unavailable")
                            time.sleep(.1)
                    except KeyboardInterrupt:
                        pass
                phase["status_response"] = status_exchange(args.port)
                phase["owned_memory"] = spec.owned_memory_snapshot(child.process)
                phase["outcome"] = "COMPLETED"
            finally:
                # Wait for synchronous handlers before destroying their owned worker.
                spec.shutdown_seed(engine, server, thread, audit, phase)
                if audit is not None:
                    phase["audit_status"] = audit.status()
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
            phase["source_unchanged_during_run"] = source_hashes() == hashes
            current_profiles = dict(zip(("target", "draft"), model_profiles(models)))
            phase["models_unchanged_during_run"] = all(checked_model(m["path"], current_profiles[k]) == m for k, m in models.items())
            phase["backend_assets_unchanged_during_run"] = assets.prepare_assets(
                Path(prepared["directory"]).parent, download=False) == prepared
            if not all(phase[k] for k in ("source_unchanged_during_run", "models_unchanged_during_run",
                                         "backend_assets_unchanged_during_run")):
                raise ValueError("adaptive-inputs-changed-during-run")
            if (directory / "worker.log").is_file():
                phase["worker_log"] = spec.file_record(directory / "worker.log")
                phase["speculation_observed"] = spec.observe_speculation((directory / "worker.log").read_bytes())
                if phase["speculation_observed"]["errors"]:
                    raise ValueError("adaptive-worker-log-error")
            if child is not None and (not child.cleanup.get("job_cleanup_confirmed") or not all(phase["ports_closed"].values())):
                raise ValueError("adaptive-cleanup-unconfirmed")
            if args.mode == "smoke" and phase["outcome"] == "COMPLETED":
                errors = smoke_errors(phase)
                if errors:
                    raise ValueError("adaptive-recording-incomplete:" + ",".join(errors))
        except Exception as exc:
            phase["outcome"] = "FAIL"
            phase["error"] = (phase["error"] + ";" if phase["error"] else "") + type(exc).__name__ + ":" + str(exc)[:512]
    return choice, phase


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("plan", "smoke", "serve"), required=True)
    parser.add_argument("--policy", choices=("auto", "none", "draft"), default="auto")
    parser.add_argument("--expected-output-tokens", type=int, default=128)
    parser.add_argument("--asset-root", required=True)
    parser.add_argument("--target-file", required=True)
    parser.add_argument("--target-profile", choices=PROFILE_NAMES, default="4b")
    parser.add_argument("--draft-file")
    parser.add_argument("--calibration-file")
    parser.add_argument("--calibration-source-root")
    parser.add_argument("--storage-root", default=str(lab.default_data_root() / "adaptive"))
    parser.add_argument("--port", type=int, default=18961)
    parser.add_argument("--worker-port", type=int, default=18962)
    parser.add_argument("--serve-seconds", type=int, default=0, help="0 serves until Ctrl+C")
    args = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("owned adaptive launcher currently requires Windows Job Objects")
    if (args.port == args.worker_port or any(not 1024 <= p <= 65535 for p in (args.port, args.worker_port))
            or not 1 <= args.expected_output_tokens <= 128 or not 0 <= args.serve_seconds <= 86400
            or bool(args.calibration_file) != bool(args.calibration_source_root)):
        parser.error("distinct ports 1024..65535, tokens 1..128, seconds 0..86400, paired calibration inputs required")
    root = local_root(args.storage_root)
    evidence = local_root(root / "adaptive-runs") / str(uuid4())
    evidence.mkdir(exist_ok=False)
    hashes = source_hashes()
    summary = {"schema_version": 2, "experiment": "cellivane-seed-adaptive", "run_id": evidence.name,
        "profiles": None,
        "configuration": spec.configuration(1, 128), "configuration_sha256": None,
        "models": None, "backend_assets": None, "source_sha256": hashes,
        "source_snapshot": str(evidence / "source-snapshot"), "calibration_validation": None,
        "choice": None, "choice_sha256": None, "phase": None,
        "launch_mode": args.mode, "outcome": "NOT_STARTED", "error": None}
    try:
        snapshot_sources(evidence / "source-snapshot", hashes)
        config = summary["configuration"]
        summary["configuration_sha256"] = spec.digest(config)
        prepared = assets.prepare_assets(args.asset_root, download=False)
        summary["backend_assets"] = prepared
        target_profile = get_profile(args.target_profile)
        draft_profile = get_profile("0.6b") if args.draft_file else None
        validate_pair(target_profile, draft_profile)
        summary["profiles"] = profile_records(target_profile, draft_profile)
        models = {"target": checked_model(args.target_file, target_profile)}
        if args.draft_file:
            models["draft"] = checked_model(args.draft_file, draft_profile)
        summary["models"] = models
        calibration, summary["calibration_validation"] = load_calibration(args.calibration_file, args.calibration_source_root)
        if args.mode == "plan":
            validate_inputs(prepared, models, hashes)
            choice = choose(prepared, models, config, spec.memory_snapshot(), policy=args.policy,
                expected_output_tokens=args.expected_output_tokens, calibration=calibration)
            summary["choice"], summary["choice_sha256"] = choice.record(), choice.sha256
            summary["outcome"] = "PLANNED" if choice.selected else "BLOCKED"
            print(encoded({"choice": choice.record(), "choice_sha256": choice.sha256,
                           "backend_dispatched": False}).decode("utf-8"), flush=True)
        else:
            summary["backend_assets"] = {**prepared, "inspection": spec.inspect_backend(prepared, root, evidence)}
            choice, phase = run_selected(args, root, evidence, prepared, models, config, hashes, calibration)
            summary["choice"] = choice.record() if choice else None
            summary["choice_sha256"] = choice.sha256 if choice else None
            summary["phase"] = phase
            summary["outcome"], summary["error"] = phase["outcome"], phase["error"]
    except BaseException as exc:
        summary["outcome"] = "FAIL"
        summary["error"] = type(exc).__name__ + ":" + str(exc)[:512]
    lab.save(evidence / "adaptive.json", summary)
    print(encoded({"outcome": summary["outcome"], "summary": str(evidence / "adaptive.json"),
                   "error": summary["error"], "independent_verdict": False}).decode("utf-8"), flush=True)
    return 0 if summary["outcome"] in ("COMPLETED", "PLANNED") else 2


if __name__ == "__main__":
    raise SystemExit(main())
