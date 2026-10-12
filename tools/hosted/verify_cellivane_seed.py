#!/usr/bin/env python3
"""Independent replay of a successful Seed smoke bundle; no runtime imports.

Checks raw backend wire hashes, identity-keyed cache/coalescing joins and final
zero-active state. Unkeyed hashes do not authenticate an adversarial artifact.
Cleanup receipts and PID/source labels are not independent OS liveness proof.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from uuid import UUID

SOURCE_PATHS = {"hosted/experimental/cellivane_seed/" + name + ".py" for name in
                ("__init__", "__main__", "core", "server")} | {
                "hosted/experimental/cellivane_bridge/" + name + ".py" for name in
                ("__init__", "__main__", "adapters", "cli", "contracts", "runtime", "storage")} | {
                "tools/hosted/cellivane_seed_lab.py", "tools/hosted/cellivane_bridge_lab.py"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(value):
    return sha256(encoded(value)).hexdigest()


def check(condition, reason):
    if not condition:
        raise ValueError(reason)


def load(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            check(key not in result, "duplicate-json-key")
            result[key] = value
        return result
    def constant(_value):
        raise ValueError("nonfinite-json")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    def bounded(item, depth=0):
        check(depth <= 20, "json-depth")
        if isinstance(item, dict):
            for nested in item.values():
                bounded(nested, depth + 1)
        elif isinstance(item, list):
            for nested in item:
                bounded(nested, depth + 1)
        elif type(item) is float:
            check(math.isfinite(item), "nonfinite-number")
    bounded(value)
    return value


def read(path, limit=16 * 1024 * 1024):
    path = Path(path)
    check(not path.is_symlink() and path.is_file() and path.stat().st_size <= limit, "file-bound")
    return load(path.read_bytes())


def normalized(value, model):
    check(type(value) is dict and value.get("model") == model, "answer-model")
    check(not any(key in value for key in ("error", "thinking", "reasoning", "reasoning_content", "tool_calls")), "answer-unsupported")
    choices, usage = value.get("choices"), value.get("usage")
    check(type(choices) is list and len(choices) == 1, "answer-choices")
    choice = choices[0]
    check(type(choice) is dict and set(choice) <= {"index", "message", "finish_reason", "logprobs"}
          and type(choice.get("index")) is int and choice["index"] == 0
          and choice.get("finish_reason") == "stop" and choice.get("logprobs") is None, "answer-finish")
    message = choice.get("message")
    check(type(message) is dict and message.get("role") == "assistant"
          and set(message) <= {"role", "content", "refusal"} and message.get("refusal") is None, "answer-role")
    text = message.get("content")
    check(type(text) is str and text.strip() and len(text.encode("utf-8")) < 256 * 1024
          and not any(ord(c) < 32 and c not in "\n\t" for c in text)
          and "<think>" not in text.lower() and "</think>" not in text.lower(), "answer-text")
    check(type(usage) is dict and {"prompt_tokens", "completion_tokens", "total_tokens"} <= set(usage)
          and set(usage) <= {"prompt_tokens", "completion_tokens", "total_tokens", "prompt_tokens_details"}, "answer-usage")
    stats = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    check(all(type(v) is int and 0 <= v < 2**63 for v in stats.values())
          and stats["prompt_tokens"] + stats["completion_tokens"] == stats["total_tokens"], "answer-tokens")
    if "prompt_tokens_details" in usage:
        details = usage["prompt_tokens_details"]
        check(type(details) is dict and set(details) == {"cached_tokens"}
              and type(details["cached_tokens"]) is int
              and 0 <= details["cached_tokens"] <= stats["prompt_tokens"], "answer-token-details")
    return {"model": model, "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": text}}], "usage": stats}


def verify(summary_path):
    summary = read(summary_path)
    check(summary.get("outcome") == "PASS" and summary.get("mode") == "smoke", "smoke-not-pass")
    check(summary.get("execution_outcome") == "COMPLETED" and summary.get("errors") == [], "smoke-errors")
    check(summary.get("source_unchanged_during_run") is True
          and set(summary.get("source_sha256", {})) == SOURCE_PATHS
          and all(re.fullmatch("[0-9a-f]{64}", v) for v in summary["source_sha256"].values()), "source-closure")
    directory = Path(summary["audit_directory"])
    check(directory.is_absolute() and not directory.is_symlink(), "audit-directory")
    files = sorted(directory.glob("*.json"))
    check(3 <= len(files) <= 10000 and sum(p.stat().st_size for p in files) <= 64 * 1024**2, "audit-bound")
    records, previous = [], None
    for seq, path in enumerate(files, 1):
        check(path.name == f"{seq:06d}.json", "audit-filename")
        raw = path.read_bytes()
        record = read(path, 1024 * 1024)
        check(set(record) == {"seq", "previous_sha256", "kind", "value"}
              and type(record["seq"]) is int and record["seq"] == seq
              and record["previous_sha256"] == previous and raw == encoded(record), "audit-chain")
        previous = sha256(raw).hexdigest()
        records.append(record)
    check(records[0]["kind"] == "configuration" and records[-1]["kind"] == "shutdown", "audit-boundaries")
    config = records[0]["value"]
    model, identity = config["model"], config["identity"]
    check(type(config["slots"]) is int and config["slots"] == 1 and config["engine"] == "cellivane-seed", "configuration")
    check(re.fullmatch("[0-9a-f]{64}", identity) is not None, "identity-format")
    expected_identity = sha256(json.dumps({"binary": config["binary_sha256"], "model": config["model_sha256"],
        "arguments": config["worker_args"]}, sort_keys=True).encode()).hexdigest()
    check(identity == expected_identity == summary["backend_identity"], "identity-binding")
    check(config["model_sha256"] == summary["model_sha256"]
          and config["binary_sha256"] == summary["binary_sha256"], "artifact-binding")
    pending, wire, values, scheduler, finished, deliveries, rejected_keys = {}, {}, {}, [], set(), [], []
    counts = {key: 0 for key in ("backend_calls", "computed", "cache_hits", "coalesced", "failures", "rejected", "timeouts")}
    active, waiters = 0, 0
    for record in records[1:-1]:
        row = record["value"]
        if record["kind"] == "backend_exchange":
            check(row.get("outcome") == "ANSWERED" and row.get("http_status") == 200, "backend-not-answered")
            for field in ("request", "response"):
                raw = row[field + "_body"].encode("utf-8")
                check(len(raw) <= 256 * 1024 and sha256(raw).hexdigest() == row[field + "_sha256"], "wire-hash")
            request = load(row["request_body"].encode("utf-8"))
            check(set(request) == {"model", "messages", "max_tokens", "temperature", "stream"}
                  and type(request["temperature"]) in (int, float)
                  and request["temperature"] == 0 and request["stream"] is False
                  and type(request["max_tokens"]) is int and 1 <= request["max_tokens"] <= 512
                  and request["model"] == model, "wire-request")
            messages = request["messages"]
            check(type(messages) is list and 1 <= len(messages) <= 32
                  and len(encoded(request)) <= 16384
                  and all(type(m) is dict and set(m) == {"role", "content"}
                      and m["role"] in ("system", "user", "assistant")
                      and type(m["content"]) is str and m["content"].strip() for m in messages)
                  and any(m["role"] == "user" for m in messages), "wire-messages")
            key = digest({"identity": identity, "inference": request})
            check(key in pending and key not in wire, "wire-without-owner")
            response = load(row["response_body"].encode("utf-8"))
            check(response == row["raw_response"], "raw-response-mismatch")
            wire[key] = normalized(response, model)
            check(row["text"] == wire[key]["choices"][0]["message"]["content"], "wire-text")
        elif record["kind"] == "scheduler":
            scheduler.append(row)
            check(type(row["seq"]) is int and row["seq"] == len(scheduler), "scheduler-sequence")
            key, kind, execution = row["key"], row["kind"], row["execution"]
            if kind == "start":
                check(key not in pending and execution == "computed" and active < config["slots"], "start-admission")
                pending[key] = 0
                active += 1
                counts["backend_calls"] += 1
            elif kind == "join":
                check(key in pending and key not in finished and execution == "coalesced" and waiters < 8, "join-owner")
                pending[key] += 1
                waiters += 1
            elif kind == "finish":
                check(key in wire and row["response_sha256"] == digest(wire[key]), "finish-wire-join")
                values[key] = wire[key]
                if execution == "computed":
                    check(key in pending and key not in finished, "finish-owner")
                    finished.add(key)
                    active -= 1
                    counts["computed"] += 1
                elif execution == "coalesced":
                    check(key in finished and key in pending and pending[key] > 0, "finish-waiter")
                    pending[key] -= 1
                    waiters -= 1
                    counts["coalesced"] += 1
                else:
                    raise ValueError("finish-execution")
            elif kind == "cache_hit":
                check(key in values and row["response_sha256"] == digest(values[key]) and execution == "cache", "cache-origin")
                counts["cache_hits"] += 1
            elif kind == "reject":
                check(row["reason"] == "slot-capacity" and active == config["slots"]
                      and key not in pending and key not in values, "reject-capacity")
                rejected_keys.append(key)
                counts["rejected"] += 1
            elif kind == "close":
                check(active == waiters == 0, "close-active")
            else:
                raise ValueError("unexpected-scheduler-event:" + kind)
            check(type(row["active"]) is int and type(row["waiters"]) is int
                  and row["active"] == active and row["waiters"] == waiters, "scheduler-gauges")
            for field, value in counts.items():
                check(type(row[field]) is int and row[field] == value, "scheduler-counter:" + field)
            check(type(row["cache_entries"]) is int and 0 <= row["cache_entries"] <= 32
                  and type(row["cache_bytes"]) is int and 0 <= row["cache_bytes"] <= 1048576, "cache-bounds")
            if kind in ("finish", "cache_hit"):
                deliveries.append((key, execution, row["response_sha256"], row["backend_calls"]))
        else:
            raise ValueError("unexpected-audit-record")
    final = records[-1]["value"]
    check(final == summary["state"] and final["active"] == final["inflight"] == final["waiters"] == 0
          and final["closed"] is True and final["poisoned"] is False
          and final["event_seq"] == len(scheduler) and scheduler[-1]["kind"] == "close", "final-state")
    check(all(type(final[field]) is int and final[field] == value for field, value in counts.items())
          and all(type(final[field]) is int for field in ("active", "inflight", "waiters", "event_seq"))
          and counts["failures"] == 0
          and counts["computed"] == counts["backend_calls"] == len(wire) == 5
          and counts["cache_hits"] == 3 and counts["coalesced"] == counts["rejected"] == 1, "final-counters")
    observations = summary["observations"]
    check(len(observations) == 5, "observations-count")
    responses = []
    for row in observations[:3]:
        cold, warm = row["cold"], row["warm"]
        check(cold["status"] == warm["status"] == 200 and row["backend_calls_delta"] == 1, "cold-warm-status")
        check(cold["body"]["cellivane"]["execution"] == "computed"
              and warm["body"]["cellivane"]["execution"] == "cache", "cold-warm-execution")
        check(normalized(cold["body"], model) == normalized(warm["body"], model), "cache-equality")
        check(type(row["exact"]) is bool and row["exact"] == (cold["body"]["choices"][0]["message"]["content"] == row["expected"]), "quality-label")
        responses.extend((cold, warm))
    shared = observations[3]
    check(shared["backend_calls_delta"] == 1 and shared["overload"]["status"] == 429
          and {r["body"]["cellivane"]["execution"] for r in shared["responses"]} == {"computed", "coalesced"}, "shared-observation")
    overload = shared["overload"]
    check(rejected_keys == [digest({"identity": identity, "inference": overload["request"]})]
          and load(overload["raw_response"].encode()) == overload["body"], "overload-event-binding")
    responses.extend(shared["responses"])
    check(len({r["body"]["id"] for r in responses}) == len(responses), "subscriber-response-id")
    for response in responses:
        body = response["body"]
        check(response["status"] == 200 and load(response["raw_response"].encode()) == body, "response-wire")
        key = digest({"identity": identity, "inference": response["request"]})
        check(body["cellivane"]["key"] == key and normalized(body, model) == values[key], "response-owner-binding")
        check(str(UUID(body["id"])) == body["id"] and body["cellivane"]["engine"] == "seed"
              and body["cellivane"]["version"] == "0.1.0", "subscriber-metadata")
        delivery = (key, body["cellivane"]["execution"], digest(normalized(body, model)), body["cellivane"]["backend_calls"])
        check(type(body["cellivane"]["backend_calls"]) is int and delivery in deliveries, "response-event-binding")
        deliveries.remove(delivery)
    bridge = observations[4]["report"]
    check(bridge["outcome"] == "COMPLETE" and bridge["dispatch_count"] == 2
          and bridge["output_allowed"] is True and len(bridge["results"]) == 2
          and all(r["outcome"] == "ANSWERED" for r in bridge["results"].values()), "bridge-not-complete")
    for result in bridge["results"].values():
        check(result["http_status"] == 200 and result["error"] is None, "bridge-http-complete")
        for field in ("request", "response"):
            check(sha256(result[field + "_body"].encode()).hexdigest() == result[field + "_sha256"], "bridge-wire-hash")
        raw = load(result["response_body"].encode())
        check(raw == result["raw_response"] and raw["model"] == result["model"], "bridge-raw-response")
        if result["kind"] == "openai":
            value = normalized(raw, model)
            request = load(result["request_body"].encode())
            key = digest({"identity": identity, "inference": request})
            check(value == values[key] and result["text"] == value["choices"][0]["message"]["content"], "bridge-seed-value")
            check(raw["cellivane"]["engine"] == "seed" and raw["cellivane"]["version"] == "0.1.0"
                  and raw["cellivane"]["key"] == key and str(UUID(raw["id"])) == raw["id"], "bridge-seed-metadata")
            delivery = (key, raw["cellivane"]["execution"], digest(value), raw["cellivane"]["backend_calls"])
            check(type(raw["cellivane"]["backend_calls"]) is int and delivery in deliveries, "bridge-seed-event")
            deliveries.remove(delivery)
        elif result["kind"] == "ollama":
            check(raw["done"] is True and raw["done_reason"] == "stop"
                  and raw["message"]["role"] == "assistant" and raw["message"]["content"] == result["text"], "bridge-ollama-value")
        else:
            raise ValueError("bridge-kind")
    check(not deliveries, "unjoined-deliveries")
    check(set(summary["ports_closed"]) == {"18932", "18941", "18942"}
          and all(v is True for v in summary["ports_closed"].values()), "port-receipts")
    check(len(summary["cleanup"]) == 3 and all(all(r.get(k) is True for k in (
        "created_suspended", "assigned_before_resume", "primary_thread_closed", "process_handle_closed",
        "process_exited", "job_assigned", "job_closed", "job_cleanup_confirmed"))
        and r["job_active_processes"] == 0 for r in summary["cleanup"]), "cleanup-receipts")
    return {"outcome": "PASS", "scope": "seed-smoke-integrity-and-semantic-replay",
            "backend_calls": counts["backend_calls"], "cache_hits": counts["cache_hits"],
            "coalesced": counts["coalesced"], "records": len(records),
            "actual_backend_qualification": False, "process_liveness_verified": False,
            "token_mtp": False, "quality_improvement_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary")
    args = parser.parse_args(argv)
    try:
        verdict = verify(args.summary)
    except (ValueError, KeyError, TypeError, OSError, RecursionError, OverflowError) as exc:
        verdict = {"outcome": "FAIL", "reason": str(exc)}
    print(json.dumps(verdict, ensure_ascii=False))
    return 0 if verdict["outcome"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
