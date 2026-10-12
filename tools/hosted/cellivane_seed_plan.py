#!/usr/bin/env python3
"""Print a model/config-bound budget plan; never launch or contact an engine.

Available bytes and workspace estimates are explicit caller inputs. ADMITTED is
a policy preflight, not an OS reservation, measured peak or tensor placement.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "hosted/experimental"))
from cellivane_seed.capabilities import BackendCapabilities
from cellivane_seed.resources import ExecutionDemand, ResourceBudget, plan_admission
from cellivane_bridge.storage import local_root


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def model_file(value):
    path = Path(value)
    if path.suffix.lower() != ".gguf" or not path.is_file():
        raise ValueError("regular-local-gguf-required")
    local_root(path.parent)
    info = path.lstat()
    if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("model-reparse-not-supported")
    if not 1 <= info.st_size < 2**63:
        raise ValueError("model-size")
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024**2), b""):
            digest.update(chunk)
    after = path.stat()
    if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
        raise ValueError("model-changed-during-hash")
    return info.st_size, digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("ollama", "llamafile", "llama.cpp"), required=True)
    parser.add_argument("--engine-identity", required=True, help="Verified backend-tree SHA-256 label")
    parser.add_argument("--target-file", required=True)
    parser.add_argument("--draft-file")
    parser.add_argument("--mode", choices=("none", "draft", "ngram"), default="none")
    parser.add_argument("--placement", choices=("cpu", "hybrid", "auto"), default="cpu")
    parser.add_argument("--ram-available-bytes", type=int, required=True)
    parser.add_argument("--ram-headroom-bytes", type=int, required=True)
    parser.add_argument("--vram-available-bytes", type=int, default=0)
    parser.add_argument("--vram-headroom-bytes", type=int, default=0)
    parser.add_argument("--host-overhead-bytes", type=int, required=True)
    parser.add_argument("--context-workspace-bytes", type=int, required=True)
    parser.add_argument("--context-tokens", type=int, default=2048)
    parser.add_argument("--gpu-required-bytes", type=int)
    parser.add_argument("--gpu-estimate-sha256")
    parser.add_argument("--require-observed", action="store_true")
    args = parser.parse_args(argv)
    try:
        if (args.mode == "draft") != (args.draft_file is not None):
            raise ValueError("draft-file-required-only-for-draft-mode")
        modes = ("none", "draft", "ngram") if args.backend == "llama.cpp" else ("none",)
        caps = BackendCapabilities(backend=args.backend, engine_identity=args.engine_identity,
            declared_modes=modes, declared_placements=("cpu", "hybrid", "auto"))
        target_bytes, target_sha = model_file(args.target_file)
        draft_bytes, draft_sha = model_file(args.draft_file) if args.draft_file else (0, None)
        configuration = {"backend": args.backend, "mode": args.mode, "placement": args.placement,
            "context_tokens": args.context_tokens, "scope": "planning-only-no-generation-settings"}
        demand = ExecutionDemand(engine_identity=args.engine_identity, target_sha256=target_sha,
            target_bytes=target_bytes, draft_sha256=draft_sha, draft_bytes=draft_bytes,
            configuration_sha256=sha256(encoded(configuration)).hexdigest(),
            mode=args.mode, placement=args.placement, context_tokens=args.context_tokens,
            host_overhead_bytes=args.host_overhead_bytes, context_workspace_bytes=args.context_workspace_bytes,
            gpu_required_bytes=args.gpu_required_bytes, gpu_estimate_sha256=args.gpu_estimate_sha256)
        budget = ResourceBudget(ram_available_bytes=args.ram_available_bytes,
            ram_headroom_bytes=args.ram_headroom_bytes, vram_available_bytes=args.vram_available_bytes,
            vram_headroom_bytes=args.vram_headroom_bytes)
        plan = plan_admission(caps, demand, budget, require_observed=args.require_observed)
        value = {"plan": plan.record(), "identity": plan.sha256,
                 "configuration": configuration, "backend_dispatched": False}
        print(encoded(value).decode("utf-8"))
        return 0 if plan.outcome == "ADMITTED" else 2
    except (ValueError, OSError, OverflowError) as exc:
        print(encoded({"outcome": "ERROR", "reason": str(exc), "backend_dispatched": False}).decode("utf-8"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
