"""Run with python -m cellivane_bridge --config ... --prompt ... ."""
from __future__ import annotations

import argparse
import json
import sys

from .runtime import load_config, run


def main(argv=None):
    parser = argparse.ArgumentParser(description="Cellivane reference/subengine bridge (local experiment)")
    parser.add_argument("--config", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    try:
        config, engines = load_config(args.config)
        report = run(config, engines, args.prompt)
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"outcome": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(f"Cellivane bridge: {report['outcome']}")
        print(f"Reference: {report['reference_engine']}")
        print(report["answer"] or "Reference answer unavailable; no substitute was selected.")
        for name, result in report["results"].items():
            if name != report["reference_engine"]:
                print(f"\nSubengine {name} [{result['outcome']}, {result['elapsed_s']:.2f}s]")
                print(result["text"] or result["error"])
        print(f"\nEvidence: {report['run_directory']}")
    return 0 if report["outcome"] == "COMPLETE" else 1
