"""Bounded reference plus subengine comparison. No RL, tool calls or KV sharing."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import copy
from dataclasses import replace
import threading
import time

from . import adapters
from .contracts import EngineConfig, EngineResult, endpoint, number
from .storage import Journal, read_json


def load_config(path):
    config = read_json(path, 64 * 1024)
    required = {"schema_version", "storage_root", "parallelism", "deadline_s", "engines"}
    if type(config) is not dict or set(config) != required or type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise ValueError("bridge-config")
    if type(config["parallelism"]) is not int or not 1 <= config["parallelism"] <= 2:
        raise ValueError("bridge-parallelism")
    if not number(config["deadline_s"], 1, 180):
        raise ValueError("bridge-deadline")
    rows = config["engines"]
    if type(rows) is not list or not 2 <= len(rows) <= 3:
        raise ValueError("bridge-engine-count")
    engines = []
    for row in rows:
        if type(row) is not dict or set(row) - set(EngineConfig.__dataclass_fields__):
            raise ValueError("bridge-engine-config")
        try:
            engines.append(EngineConfig(**row))
        except TypeError as exc:
            raise ValueError("bridge-engine-config") from exc
    if len({engine.name for engine in engines}) != len(engines):
        raise ValueError("bridge-duplicate-engine")
    if sum(engine.role == "reference" for engine in engines) != 1:
        raise ValueError("bridge-reference-required")
    if len({endpoint(engine.base_url)[:2] for engine in engines}) != len(engines):
        raise ValueError("bridge-endpoint-alias")
    return config, engines


def run(config, engines, prompt, *, cancel=None, request_fn=None, probe_fn=None):
    if type(prompt) is not str or not prompt.strip() or len(prompt.encode("utf-8")) > 8192:
        raise ValueError("bridge-prompt")
    # Revalidate even callers that bypass load_config; immutable engine copies below.
    if type(engines) not in (tuple, list) or any(type(e) is not EngineConfig for e in engines):
        raise ValueError("bridge-engine-config")
    if not 2 <= len(engines) <= 3 or sum(e.role == "reference" for e in engines) != 1:
        raise ValueError("bridge-reference-required")
    if type(config["parallelism"]) is not int or not 1 <= config["parallelism"] <= 2:
        raise ValueError("bridge-parallelism")
    if not number(config["deadline_s"], 1, 180):
        raise ValueError("bridge-deadline")
    engines = tuple(EngineConfig(**EngineConfig.record(e)) for e in engines)
    config = copy.deepcopy(config)
    if len({e.name for e in engines}) != len(engines):
        raise ValueError("bridge-engine-config")
    if len({endpoint(e.base_url)[:2] for e in engines}) != len(engines):
        raise ValueError("bridge-endpoint-alias")
    cancel = cancel if cancel is not None else threading.Event()
    request_fn = request_fn or adapters.request
    probe_fn = probe_fn or (lambda engine: adapters.probe(engine, cancel))
    journal = Journal(config["storage_root"])
    began = time.monotonic()
    deadline = began + config["deadline_s"]
    messages = [{"role": "system", "content": "Answer the user briefly and accurately. /no_think"},
                {"role": "user", "content": prompt}]
    journal.append("accepted", {"prompt": prompt, "messages": messages, "engines": [e.record() for e in engines],
        "parallelism": config["parallelism"], "call_budget": len(engines),
        "deadline_s": config["deadline_s"], "directory_fsync": journal.directory_fsync,
        "scope": "isolated-local-experiment", "policy": "reference-is-authoritative"})
    qualifications = {}
    results = {}
    dispatch_count = 0
    failure = None

    def permitted():
        return not cancel.is_set() and time.monotonic() + 0.1 < deadline

    def bounded(engine):
        return replace(engine, timeout_s=min(engine.timeout_s, max(0.1, deadline - time.monotonic())))

    def save_result(engine, result):
        if type(result) is not EngineResult or (result.name, result.kind, result.model) != (engine.name, engine.kind, engine.model):
            raise ValueError("bridge-result-binding")
        if result.outcome not in ("ANSWERED", "UNKNOWN", "NOT_STARTED"):
            raise ValueError("bridge-result-outcome")
        if result.outcome == "ANSWERED" and (type(result.text) is not str or not result.text.strip()):
            raise ValueError("bridge-result-text")
        results[engine.name] = result.record()
        journal.append("result", result.record())

    try:
        # Reference readiness is required before any generation is submitted.
        ordered = sorted(engines, key=lambda engine: engine.role != "reference")
        for engine in ordered:
            if not permitted():
                break
            if engine.kind == "strata":
                # Upstream defaults to a mock server. HTTP availability is not a
                # real-engine qualification; keep this route fenced until an
                # owned Strata process + supported model are independently bound.
                probe_record = {"schema_version": 1, "name": engine.name,
                    "kind": engine.kind, "model": engine.model, "base_url": engine.base_url,
                    "outcome": "UNKNOWN", "model_present": False,
                    "server_version": None, "model_digest": None, "responses": [],
                    "error": "strata-runtime-qualification-not-implemented", "elapsed_s": 0.0}
            else:
                probe_record = probe_fn(bounded(engine))
            if type(probe_record) is not dict or any(probe_record.get(k) != getattr(engine, k) for k in ("name", "kind", "model", "base_url")):
                raise ValueError("bridge-probe-binding")
            qualifications[engine.name] = probe_record
            journal.append("qualification", probe_record)
            if engine.role == "reference" and probe_record.get("outcome") != "CONFIRMED":
                failure = "reference-not-qualified"
                break
        if permitted() and failure is None and len(qualifications) == len(engines):
            ready = [engine for engine in ordered if qualifications[engine.name].get("outcome") == "CONFIRMED"]
            # Execute the reference first. Uncertain reference result fences all children.
            reference = ready.pop(0)
            effective = bounded(reference)
            journal.append("dispatch", {"engine": reference.name, "role": "reference", "effective_timeout_s": effective.timeout_s})
            dispatch_count += 1
            result = request_fn(effective, copy.deepcopy(messages), cancel)
            save_result(reference, result)
            if result.outcome != "ANSWERED":
                failure = "reference-result-uncertain"
            elif permitted():
                with ThreadPoolExecutor(max_workers=config["parallelism"], thread_name_prefix="cellivane-subengine") as pool:
                    pending = {}
                    try:
                        # No queue: at most parallelism submitted workers at any time.
                        while ready or pending:
                            while ready and len(pending) < config["parallelism"] and permitted():
                                engine = ready.pop(0)
                                effective = bounded(engine)
                                journal.append("dispatch", {"engine": engine.name, "role": engine.role, "effective_timeout_s": effective.timeout_s})
                                future = pool.submit(request_fn, effective, copy.deepcopy(messages), cancel)
                                pending[future] = engine
                                dispatch_count += 1
                            if not permitted():
                                cancel.set()
                                ready.clear()
                            if not pending:
                                break
                            done, _ = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                            for future in done:
                                engine = pending.pop(future)
                                save_result(engine, future.result())
                    except KeyboardInterrupt:
                        cancel.set()
                        failure = "turn-cancelled"
                        # Preserve any complete valid child response even when
                        # Ctrl+C interrupted the parent's collection loop.
                        for future, engine in list(pending.items()):
                            save_result(engine, future.result())
                        pending.clear()
                    except BaseException:
                        # Set before executor.__exit__ waits for owned workers.
                        cancel.set()
                        raise
    except KeyboardInterrupt:
        cancel.set()
        # A reference call interrupted before it returned has no result object.
        # Leave the incomplete journal visibly incomplete, never fabricate one.
        raise
    except BaseException:
        cancel.set()
        # Persistence failure cannot be overwritten with a successful final record.
        raise
    reference = next(e for e in engines if e.role == "reference")
    reference_result = results.get(reference.name)
    complete = (len(results) == len(engines) and all(row["outcome"] == "ANSWERED" for row in results.values()))
    deadline_exceeded = time.monotonic() >= deadline
    output_allowed = (not cancel.is_set() and not deadline_exceeded and reference_result is not None
                      and reference_result["outcome"] == "ANSWERED")
    report = {"schema_version": 1, "run_id": journal.root.name, "run_directory": str(journal.root),
        "outcome": "CANCELLED" if cancel.is_set() else "COMPLETE" if complete and output_allowed else "INCOMPLETE",
        "error": failure or ("deadline" if deadline_exceeded else "subengine-incomplete" if not complete else None), "reference_engine": reference.name,
        "answer": reference_result["text"] if output_allowed else None,
        "qualifications": qualifications, "results": results, "dispatch_count": dispatch_count,
        "call_budget": len(engines), "parallelism": config["parallelism"],
        "elapsed_s": time.monotonic() - began, "output_allowed": output_allowed,
        "cancellation_requested": cancel.is_set(), "deadline_exceeded": deadline_exceeded,
        "qualification_scope": "http-model-availability-only",
        "model_training": False, "token_mtp": False, "kv_sharing": False,
        "canonical_main_binding": False, "directory_fsync": journal.directory_fsync}
    journal.append("final", report)
    return report
