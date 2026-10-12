#!/usr/bin/env python3
"""Independently replay an experimental Cellivane bridge journal.

This stdlib-only reader verifies received evidence, not live endpoint/process
identity or backend implementation. An unkeyed hash chain is not authenticity.
It imports neither the bridge runtime nor its serializers and validators.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit
from uuid import UUID

MAX_FILE = 4 * 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024
MAX_WIRE = 256 * 1024
KINDS = {"accepted", "qualification", "dispatch", "result", "final"}
STATS = {"total_duration", "load_duration", "prompt_eval_count", "prompt_eval_cached_count",
         "prompt_eval_duration", "eval_count", "eval_duration"}
ENGINE_KEYS = {"name", "kind", "base_url", "model", "role", "timeout_s", "max_tokens"}
RESULT_KEYS = {"name", "kind", "model", "outcome", "text", "elapsed_s", "request_sha256",
               "response_sha256", "raw_response", "error", "usage", "request_body",
               "response_body", "http_status"}
QUALIFICATION_KEYS = {"schema_version", "name", "kind", "model", "base_url", "outcome",
                      "model_present", "server_version", "model_digest", "responses", "error", "elapsed_s"}
FINAL_KEYS = {"schema_version", "run_id", "run_directory", "outcome", "error", "reference_engine",
              "answer", "qualifications", "results", "dispatch_count", "call_budget", "parallelism",
              "elapsed_s", "output_allowed", "model_training", "token_mtp", "kv_sharing",
              "canonical_main_binding", "directory_fsync", "cancellation_requested", "deadline_exceeded",
              "qualification_scope"}


class Invalid(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Invalid(reason)


def exact(value, keys, reason):
    require(type(value) is dict and set(value) == set(keys), reason)


def same(left, right):
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return set(left) == set(right) and all(same(left[k], right[k]) for k in left)
    if type(left) is list:
        return len(left) == len(right) and all(same(a, b) for a, b in zip(left, right))
    return left == right


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def integer(value, low=0, high=(1 << 63) - 1):
    return type(value) is int and low <= value <= high


def number(value, low=0, high=1 << 63):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def text(value, maximum=None):
    return (type(value) is str and bool(value.strip())
            and not any(ord(c) < 32 and c not in "\n\t" for c in value)
            and (maximum is None or len(value.encode("utf-8")) <= maximum))


def sha(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def decode(raw, maximum=MAX_FILE):
    require(type(raw) is bytes and 0 < len(raw) <= maximum, "json-size")
    def pairs(items):
        row = {}
        for key, value in items:
            require(key not in row, "json-duplicate-key")
            row[key] = value
        return row
    def invalid_constant(_):
        raise Invalid("json-nonfinite")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_constant)
        pending, count = [(value, 0)], 0
        while pending:
            item, depth = pending.pop()
            count += 1
            require(depth <= 24 and count <= 100000, "json-complexity")
            require(type(item) is not float or math.isfinite(item), "json-nonfinite")
            if type(item) is dict:
                pending.extend((child, depth + 1) for child in item.values())
            elif type(item) is list:
                pending.extend((child, depth + 1) for child in item)
            elif type(item) is str:
                item.encode("utf-8")  # Reject lone surrogate strings, including escaped forms.
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError, OverflowError) as exc:
        raise Invalid("json-invalid") from exc


def safe_path(path, *, directory):
    path = Path(path)
    require(path.is_absolute() and not str(path).startswith(("\\\\", "//")), "local-absolute-path")
    for part in (path, *path.parents):
        info = part.lstat()
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
                "path-reparse")
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode), "path-kind")
    return path.resolve(strict=True)


def read_file(path):
    path = safe_path(path, directory=False)
    before = path.stat()
    require(before.st_nlink == 1 and 0 < before.st_size <= MAX_FILE, "file-size-or-links")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(MAX_FILE + 1)
        after = os.fstat(stream.fileno())
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            == (opened.st_dev, opened.st_ino, after.st_size, after.st_mtime_ns)
            and len(raw) == before.st_size, "file-changed")
    return raw


def address(value):
    require(type(value) is str and len(value) <= 256, "engine-endpoint")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        require(parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                and parsed.username is None and parsed.password is None
                and integer(port, 1, 65535) and not parsed.query and not parsed.fragment
                and parsed.path in ("", "/v1"), "engine-endpoint")
    except ValueError as exc:
        raise Invalid("engine-endpoint") from exc
    return parsed.hostname, port, parsed.path


def engine(value):
    exact(value, ENGINE_KEYS, "engine-fields")
    require(all(type(value[k]) is str and 1 <= len(value[k]) <= 128 and value[k].isprintable()
                for k in ("name", "model")), "engine-identity")
    require(value["kind"] in ("ollama", "openai", "strata")
            and value["role"] in ("reference", "candidate", "reviewer"), "engine-kind-role")
    endpoint = address(value["base_url"])
    require(value["kind"] != "ollama" or not endpoint[2], "ollama-base-path")
    require(number(value["timeout_s"], .1, 120) and integer(value["max_tokens"], 1, 512), "engine-budget")
    return endpoint[:2]


def messages(value, prompt):
    require(type(value) is list and len(value) == 2, "accepted-messages")
    expected = [{"role": "system", "content": "Answer the user briefly and accurately. /no_think"},
                {"role": "user", "content": prompt}]
    require(same(value, expected) and len(encoded(value)) <= 16 * 1024, "accepted-messages")


def raw_join(row, *, uncertain=False):
    body = row["response_body"]
    if body is None:
        require(row["raw_response"] is None and
                (row["response_sha256"] is None or uncertain and sha(row["response_sha256"])),
                "response-missing-wire")
        return None
    require(type(body) is str and (uncertain or bool(body)) and len(body.encode("utf-8")) <= MAX_WIRE,
            "response-wire-size")
    require(sha(row["response_sha256"]) and digest(body.encode("utf-8")) == row["response_sha256"],
            "response-sha256")
    try:
        value = decode(body.encode("utf-8"), MAX_WIRE)
    except Invalid:
        require(row["raw_response"] is None, "invalid-response-parsed")
        return None
    if uncertain and type(value) is not dict:
        require(row["raw_response"] is None, "invalid-response-parsed")
        return None
    require(type(value) is dict and same(value, row["raw_response"]), "response-raw-join")
    return value


def qualification(row, config):
    exact(row, QUALIFICATION_KEYS, "qualification-fields")
    require(type(row["schema_version"]) is int and row["schema_version"] == 1, "qualification-schema")
    require(all(same(row[key], config[key]) for key in ("name", "kind", "model", "base_url")),
            "qualification-identity")
    require(row["outcome"] in ("CONFIRMED", "UNKNOWN") and type(row["model_present"]) is bool
            and number(row["elapsed_s"]), "qualification-state")
    require(row["server_version"] is None or text(row["server_version"], 512), "qualification-version")
    require(row["model_digest"] is None or sha(row["model_digest"]), "qualification-digest")
    if config["kind"] == "strata":
        require(row["outcome"] == "UNKNOWN" and row["model_present"] is False
                and row["server_version"] is None and row["model_digest"] is None
                and row["responses"] == [] and row["elapsed_s"] == 0.0
                and row["error"] == "strata-runtime-qualification-not-implemented", "strata-qualification-fence")
        return
    paths = ("/api/version", "/api/tags") if config["kind"] == "ollama" else ("/health", "/v1/models")
    responses = row["responses"]
    require(type(responses) is list and len(responses) <= 2, "qualification-responses")
    values = []
    for index, response in enumerate(responses):
        exact(response, {"endpoint", "status", "response_sha256", "raw_response", "response_body"},
              "qualification-response-fields")
        require(response["endpoint"] == paths[index] and integer(response["status"], 100, 599),
                "qualification-endpoint-status")
        values.append(raw_join(response, uncertain=row["outcome"] == "UNKNOWN"))
    if row["outcome"] == "UNKNOWN":
        require(text(row["error"], 512), "qualification-error")
        return
    require(row["error"] is None and row["model_present"] is True and len(values) == 2
            and all(response["status"] == 200 for response in responses)
            and all(type(value) is dict for value in values), "qualification-confirmed")
    first, listing = values
    if config["kind"] == "ollama":
        exact(first, {"version"}, "qualification-version-fields")
        require(text(first["version"]) and len(first["version"]) <= 128
                and same(first["version"], row["server_version"]), "qualification-version-join")
        models = listing.get("models")
    else:
        require(("status" not in first or first["status"] == "ok") and listing.get("object") == "list",
                "qualification-health-list")
        require(row["server_version"] is None and row["model_digest"] is None, "qualification-openai-metadata")
        models = listing.get("data")
    require(type(models) is list and len(models) <= 128, "qualification-model-list")
    matches = []
    for item in models:
        require(type(item) is dict, "qualification-model")
        keys = ("name", "model") if config["kind"] == "ollama" else ("id",)
        require(any(text(item.get(key)) for key in keys), "qualification-model-id")
        require(all(integer(item[key]) for key in ("size", "created") if key in item), "qualification-model-number")
        if any(item.get(key) == config["model"] for key in keys):
            matches.append(item)
    require(len(matches) == 1, "qualification-model-match")
    if config["kind"] == "ollama":
        require(sha(matches[0].get("digest")) and matches[0]["digest"] == row["model_digest"],
                "qualification-model-digest")


def usage(value):
    if value is None:
        return None
    require(type(value) is dict, "response-usage")
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        require(depth <= 4, "response-usage-depth")
        if type(item) is dict:
            pending.extend((child, depth + 1) for child in item.values())
        else:
            require(item is None or integer(item), "response-usage-number")
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        require(key not in value or integer(value[key]), "response-usage-count")
    if all(key in value for key in ("prompt_tokens", "completion_tokens", "total_tokens")):
        require(value["prompt_tokens"] + value["completion_tokens"] == value["total_tokens"],
                "response-usage-total")
    return value


def answer(value, config):
    require(type(value) is dict and value.get("model") == config["model"], "response-model")
    if config["kind"] == "ollama":
        require(not set(value) - {"model", "created_at", "message", "done", "done_reason"} - STATS,
                "response-ollama-fields")
        require(value.get("done") is True and value.get("done_reason") == "stop", "response-finish")
        require("created_at" not in value or text(value["created_at"]) and len(value["created_at"]) <= 128,
                "response-created-at")
        require(all(integer(value[key]) for key in STATS & set(value)), "response-statistic")
        message = value.get("message")
        observed_usage = {k: value[k] for k in ("prompt_eval_count", "eval_count") if k in value} or None
    else:
        choices = value.get("choices")
        require(type(choices) is list and len(choices) == 1 and type(choices[0]) is dict, "response-choices")
        choice = choices[0]
        require(not set(choice) - {"index", "message", "finish_reason", "logprobs"}
                and type(choice.get("index")) is int and choice["index"] == 0
                and choice.get("finish_reason") == "stop" and choice.get("logprobs") is None,
                "response-choice")
        require("created" not in value or integer(value["created"]), "response-created")
        message = choice.get("message")
        observed_usage = usage(value.get("usage"))
    require(type(message) is dict and not set(message) - {"role", "content", "refusal"}
            and message.get("role") == "assistant" and message.get("refusal") is None
            and text(message.get("content")), "response-message")
    return message["content"], observed_usage


def result(row, config, accepted):
    exact(row, RESULT_KEYS, "result-fields")
    require(all(same(row[key], config[key]) for key in ("name", "kind", "model")), "result-identity")
    require(row["outcome"] in ("ANSWERED", "UNKNOWN", "NOT_STARTED") and number(row["elapsed_s"]),
            "result-state")
    require(type(row["request_body"]) is str and len(row["request_body"].encode("utf-8")) <= 32 * 1024,
            "request-wire-size")
    raw_request = row["request_body"].encode("utf-8")
    request = decode(raw_request, 32 * 1024)
    expected = {"model": config["model"], "messages": accepted["messages"], "stream": False}
    if config["kind"] == "ollama":
        expected.update(think=False, options={"num_predict": config["max_tokens"]})
    else:
        expected.update(max_tokens=config["max_tokens"], temperature=0)
    require(same(request, expected) and raw_request == encoded(expected), "request-contract")
    require(sha(row["request_sha256"]) and row["request_sha256"] == digest(raw_request), "request-sha256")
    require(row["http_status"] is None or integer(row["http_status"], 100, 599), "result-http-status")
    value = raw_join(row, uncertain=row["outcome"] == "UNKNOWN")
    if row["outcome"] == "ANSWERED":
        require(row["http_status"] == 200 and row["error"] is None, "answered-status-error")
        content, observed_usage = answer(value, config)
        require(same(content, row["text"]) and same(observed_usage, row["usage"]), "answered-text-usage-join")
    else:
        require(row["text"] is None and row["usage"] is None and text(row["error"], 512), "uncertain-result")
        if row["outcome"] == "NOT_STARTED":
            require(row["response_body"] is None and row["response_sha256"] is None and row["http_status"] is None
                    and row["error"] == "cancelled-before-dispatch", "not-started-wire")


def replay(run_directory):
    root = safe_path(run_directory, directory=True)
    require(str(UUID(root.name)) == root.name, "run-id")
    files = sorted(root.iterdir(), key=lambda p: p.name)
    require(2 <= len(files) <= 11, "journal-record-count")
    rows, previous, total = [], "0" * 64, 0
    for sequence, path in enumerate(files, 1):
        require(re.fullmatch(r"[0-9]{4}-(accepted|qualification|dispatch|result|final)\.json", path.name),
                "journal-file-name")
        raw = read_file(path)
        total += len(raw)
        require(total <= MAX_TOTAL, "journal-total-size")
        row = decode(raw)
        exact(row, {"schema_version", "sequence", "kind", "previous_sha256", "payload"}, "record-fields")
        require(type(row["schema_version"]) is int and row["schema_version"] == 1
                and type(row["sequence"]) is int and row["sequence"] == sequence
                and type(row["kind"]) is str and row["kind"] in KINDS, "record-schema-sequence")
        require(path.name == f"{sequence:04d}-{row['kind']}.json", "record-file-sequence")
        require(row["previous_sha256"] == previous and sha(previous), "record-hash-chain")
        require(raw == encoded(row), "record-canonical-bytes")
        previous = digest(raw)
        rows.append(row)
    require(rows[0]["kind"] == "accepted" and rows[-1]["kind"] == "final"
            and sum(r["kind"] == "accepted" for r in rows) == 1
            and sum(r["kind"] == "final" for r in rows) == 1, "journal-terminals")
    accepted = rows[0]["payload"]
    exact(accepted, {"prompt", "messages", "engines", "parallelism", "call_budget", "deadline_s",
                     "directory_fsync", "scope", "policy"}, "accepted-fields")
    require(text(accepted["prompt"], 8192), "accepted-prompt")
    messages(accepted["messages"], accepted["prompt"])
    require(integer(accepted["parallelism"], 1, 2) and number(accepted["deadline_s"], 1, 180)
            and type(accepted["directory_fsync"]) is bool
            and accepted["scope"] == "isolated-local-experiment"
            and accepted["policy"] == "reference-is-authoritative", "accepted-policy-budget")
    configs = accepted["engines"]
    require(type(configs) is list and 2 <= len(configs) <= 3, "accepted-engines")
    endpoints = [engine(config) for config in configs]
    require(len(set(endpoints)) == len(endpoints) and len({e["name"] for e in configs}) == len(configs),
            "engine-alias")
    require(type(accepted["call_budget"]) is int and accepted["call_budget"] == len(configs), "call-budget")
    refs = [e for e in configs if e["role"] == "reference"]
    require(len(refs) == 1, "reference-required")
    reference = refs[0]["name"]
    by_name = {e["name"]: e for e in configs}
    ordered = [reference] + [e["name"] for e in configs if e["name"] != reference]
    quals, dispatched, results = {}, {}, {}
    for record in rows[1:-1]:
        kind, payload = record["kind"], record["payload"]
        require(type(payload) is dict, "payload-object")
        if kind == "qualification":
            name = payload.get("name")
            require(not dispatched and len(quals) < len(ordered) and name == ordered[len(quals)]
                    and (not quals or quals[reference]["outcome"] == "CONFIRMED"), "qualification-order")
            qualification(payload, by_name[name])
            quals[name] = payload
        elif kind == "dispatch":
            exact(payload, {"engine", "role", "effective_timeout_s"}, "dispatch-fields")
            name = payload["engine"]
            require(type(name) is str and name in by_name and name not in dispatched
                    and len(dispatched) < accepted["call_budget"], "dispatch-engine-budget")
            config = by_name[name]
            require(payload["role"] == config["role"] and len(quals) == len(configs)
                    and quals[name]["outcome"] == "CONFIRMED", "dispatch-qualification-role")
            require(number(payload["effective_timeout_s"], .001,
                           min(config["timeout_s"], accepted["deadline_s"])), "dispatch-timeout-budget")
            require((not dispatched and name == reference) or
                    (name != reference and reference in results and results[reference]["outcome"] == "ANSWERED"),
                    "reference-dispatch-fence")
            require(len(set(dispatched) - set(results)) < accepted["parallelism"], "dispatch-parallelism-budget")
            dispatched[name] = payload
        elif kind == "result":
            name = payload.get("name")
            require(type(name) is str and name in dispatched and name not in results, "result-dispatch-join")
            result(payload, by_name[name], accepted)
            results[name] = payload
        else:
            raise Invalid("journal-phase-order")
    require(set(dispatched) == set(results), "missing-dispatch-result")
    final = rows[-1]["payload"]
    exact(final, FINAL_KEYS, "final-fields")
    require(type(final["schema_version"]) is int and final["schema_version"] == 1
            and final["run_id"] == root.name and type(final["run_directory"]) is str
            and Path(final["run_directory"]).is_absolute()
            and Path(final["run_directory"]).resolve(strict=True) == root, "final-run-identity")
    require(same(final["qualifications"], quals) and same(final["results"], results), "final-record-join")
    require(final["reference_engine"] == reference and type(final["dispatch_count"]) is int
            and final["dispatch_count"] == len(dispatched)
            and same(final["call_budget"], accepted["call_budget"])
            and same(final["parallelism"], accepted["parallelism"])
            and same(final["directory_fsync"], accepted["directory_fsync"])
            and number(final["elapsed_s"]), "final-budget-join")
    for key in ("output_allowed", "cancellation_requested", "deadline_exceeded"):
        require(type(final[key]) is bool, "final-boolean")
    for key in ("model_training", "token_mtp", "kv_sharing", "canonical_main_binding"):
        require(final[key] is False, "final-scope")
    require(final["qualification_scope"] == "http-model-availability-only", "final-qualification-scope")
    require(final["error"] is None or final["error"] in
            ("reference-not-qualified", "reference-result-uncertain", "turn-cancelled", "deadline", "subengine-incomplete"), "final-error")
    ref_result = results.get(reference)
    output = (not final["cancellation_requested"] and not final["deadline_exceeded"]
              and ref_result is not None and ref_result["outcome"] == "ANSWERED")
    require(final["output_allowed"] is output, "final-output-fence")
    require(same(final["answer"], ref_result["text"] if output else None), "final-reference-answer")
    complete = len(results) == len(configs) and all(r["outcome"] == "ANSWERED" for r in results.values())
    expected_outcome = "CANCELLED" if final["cancellation_requested"] else "COMPLETE" if complete and output else "INCOMPLETE"
    require(final["outcome"] == expected_outcome, "final-outcome")
    if reference in quals and quals[reference]["outcome"] != "CONFIRMED":
        require(not dispatched and final["error"] == "reference-not-qualified", "failed-reference-zero-generation")
    elif ref_result is not None and ref_result["outcome"] != "ANSWERED":
        require(set(dispatched) == {reference} and final["error"] == "reference-result-uncertain",
                "uncertain-reference-zero-child-generation")
    elif final["error"] == "turn-cancelled":
        require(final["cancellation_requested"], "final-cancel-error")
    else:
        require(final["error"] == ("deadline" if final["deadline_exceeded"] else "subengine-incomplete" if not complete else None),
                "final-error-join")
    unseen_wire = sum(row["response_sha256"] is not None and row["response_body"] is None for row in results.values())
    unseen_wire += sum(row["response_sha256"] is not None and row["response_body"] is None
                       for q in quals.values() for row in q["responses"])
    return {"schema_version": 1, "verifier": "cellivane-bridge-journal-v1", "verdict": "PASS",
            "run_id": root.name, "journal_records": len(rows), "journal_tail_sha256": previous,
            "run_outcome": final["outcome"], "dispatch_count": len(dispatched),
            "output_allowed": output, "reference_engine": reference,
            "unverifiable_wire_records": unseen_wire,
            "actual_backend_qualification": False, "process_liveness_verified": False,
            "evidence_scope": "received-journal-integrity-and-contract-only"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-directory", required=True)
    args = parser.parse_args(argv)
    try:
        verdict = replay(args.run_directory)
    except (Invalid, OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        print(json.dumps({"schema_version": 1, "verifier": "cellivane-bridge-journal-v1",
                          "verdict": "FAIL", "reason": str(exc)[:256],
                          "actual_backend_qualification": False, "process_liveness_verified": False}))
        return 1
    print(json.dumps(verdict, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
