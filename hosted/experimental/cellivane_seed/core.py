"""Bounded caller-thread serving with exact-result LRU and in-flight coalescing.

No tensor execution, executor, native cancellation, or hidden work queue lives
here. The supplied backend must enforce its own I/O deadline. timeout_s bounds
joiners and rejects an owner result returned after that deadline; Python cannot
interrupt a blocking callable without introducing another execution lifetime.
Cache byte accounting covers canonical result bytes plus the ASCII cache key,
not Python allocator overhead, tensor weights, or a backend's KV cache.
Event sinks run synchronously under the scheduler lock and must not call back
into the engine. Events contain digests/counters, never prompts or answer text.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
from dataclasses import dataclass, field
import hashlib
import json
import math
import threading
import time
from uuid import uuid4

VERSION = "0.1.0"
MAX_INPUT_BYTES = 16 * 1024
MAX_TEXT_BYTES = 256 * 1024
MAX_RESULT_BYTES = MAX_TEXT_BYTES - 1024  # Space for the per-caller envelope.


class InvalidRequest(ValueError):
    pass


class BusyError(RuntimeError):
    pass


class EngineUnavailable(RuntimeError):
    pass


class BackendFailure(RuntimeError):
    pass


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _text(value):
    return (type(value) is str and bool(value.strip())
            and not any(ord(c) < 32 and c not in "\n\t" for c in value))


def _integer(value):
    return type(value) is int and 0 <= value < 2**63


@dataclass
class _Flight:
    event: threading.Event = field(default_factory=threading.Event)
    waiters: int = 0
    done: bool = False
    value: dict | None = None
    error: Exception | None = None


class SeedEngine:
    def __init__(self, backend, *, model: str, identity: str, slots: int = 1,
                 cache_entries: int = 32, cache_bytes: int = 1048576,
                 max_waiters: int = 8, timeout_s: float = 90, event_sink=None):
        if not callable(backend) or (event_sink is not None and not callable(event_sink)):
            raise ValueError("seed-callback")
        if (type(model) is not str or not 1 <= len(model) <= 128 or not model.isprintable()
                or type(identity) is not str or not 1 <= len(identity) <= 512 or not identity.isprintable()):
            raise ValueError("seed-identity")
        for name, value, low, high in (("slots", slots, 1, 32),
                ("cache_entries", cache_entries, 0, 4096), ("cache_bytes", cache_bytes, 0, 64 * 1024**2),
                ("max_waiters", max_waiters, 0, 1024)):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("seed-" + name)
        if type(timeout_s) not in (int, float) or not 0 < timeout_s <= 3600 or not math.isfinite(timeout_s):
            raise ValueError("seed-timeout")
        self._backend, self._model, self._identity, self._sink = backend, model, identity, event_sink
        self._slots, self._entry_limit, self._byte_limit = slots, cache_entries, cache_bytes
        self._waiter_limit, self._timeout = max_waiters, float(timeout_s)
        self._lock = threading.Lock()
        self._cache, self._flights = OrderedDict(), {}
        self._bytes, self._active, self._waiters, self._seq = 0, 0, 0, 0
        self._closed, self._poisoned = False, False
        self._counts = {name: 0 for name in
            ("backend_calls", "computed", "cache_hits", "coalesced", "failures", "rejected", "timeouts")}

    def _snapshot(self):
        return {"engine": "seed", "version": VERSION, "model": self._model, "identity": self._identity,
            "closed": self._closed, "poisoned": self._poisoned, "slots": self._slots,
            "active": self._active, "inflight": len(self._flights), "waiters": self._waiters,
            "max_waiters": self._waiter_limit, "cache_entries": len(self._cache), "cache_bytes": self._bytes,
            "cache_entries_limit": self._entry_limit, "cache_bytes_limit": self._byte_limit,
            **self._counts, "event_seq": self._seq}

    def status(self) -> dict:
        with self._lock:
            return self._snapshot()

    def _emit(self, kind, key=None, *, execution=None, reason=None, value=None):
        self._seq += 1
        event = {"seq": self._seq, "kind": kind, "key": key, "execution": execution,
            "reason": reason, "response_sha256": _digest(value) if value is not None else None,
            "active": self._active, "waiters": self._waiters, **self._counts,
            "cache_entries": len(self._cache), "cache_bytes": self._bytes}
        if self._sink is not None:
            try:
                self._sink(copy.deepcopy(event))
            except BaseException as exc:
                self._poisoned, self._closed = True, True
                self._cache.clear()
                self._bytes = 0
                for flight in self._flights.values():
                    flight.error = EngineUnavailable("event-persistence-failed")
                    flight.done = True
                    flight.event.set()
                raise EngineUnavailable("event-persistence-failed") from exc

    def _request(self, payload):
        allowed = {"model", "messages", "max_tokens", "temperature", "stream"}
        if type(payload) is not dict or len(payload) > 5 or set(payload) - allowed or not {"model", "messages"} <= set(payload):
            raise InvalidRequest("request-fields")
        if type(payload["model"]) is not str or payload["model"] != self._model:
            raise InvalidRequest("request-model")
        messages = payload["messages"]
        if type(messages) is not list or not 1 <= len(messages) <= 32:
            raise InvalidRequest("request-messages")
        copied = []
        for message in messages:
            if (type(message) is not dict or len(message) != 2 or set(message) != {"role", "content"}
                    or type(message["role"]) is not str or message["role"] not in ("system", "user", "assistant")
                    or type(message["content"]) is not str or len(message["content"]) > MAX_INPUT_BYTES
                    or not _text(message["content"])):
                raise InvalidRequest("request-messages")
            copied.append({"role": message["role"], "content": message["content"]})
        if not any(message["role"] == "user" for message in copied):
            raise InvalidRequest("request-user-required")
        tokens, temperature, stream = payload.get("max_tokens", 128), payload.get("temperature", 0), payload.get("stream", False)
        if type(tokens) is not int or not 1 <= tokens <= 512:
            raise InvalidRequest("request-max-tokens")
        if type(temperature) not in (int, float) or temperature != 0 or not math.isfinite(temperature):
            raise InvalidRequest("request-temperature")
        if stream is not False:
            raise InvalidRequest("request-stream")
        normalized = {"model": self._model, "messages": copied, "max_tokens": tokens,
                      "temperature": 0, "stream": False}
        try:
            if len(_encoded(normalized)) > MAX_INPUT_BYTES:
                raise InvalidRequest("request-size")
            key = _digest({"identity": self._identity, "inference": normalized})
        except (UnicodeError, OverflowError, RecursionError) as exc:
            raise InvalidRequest("request-encoding") from exc
        return normalized, key

    def _answer(self, value):
        if type(value) is not dict or type(value.get("model")) is not str or value["model"] != self._model:
            raise BackendFailure("backend-model")
        if any(key in value for key in ("error", "thinking", "reasoning", "reasoning_content", "tool_calls")):
            raise BackendFailure("backend-unsupported")
        choices, usage = value.get("choices"), value.get("usage")
        if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
            raise BackendFailure("backend-choices")
        choice = choices[0]
        if (set(choice) - {"index", "message", "finish_reason", "logprobs"}
                or type(choice.get("index")) is not int or choice["index"] != 0
                or type(choice.get("finish_reason")) is not str or choice["finish_reason"] != "stop"
                or choice.get("logprobs") is not None):
            raise BackendFailure("backend-finish")
        message = choice.get("message")
        if (type(message) is not dict or set(message) - {"role", "content", "refusal"}
                or type(message.get("role")) is not str or message["role"] != "assistant" or message.get("refusal") is not None
                or type(message.get("content")) is not str or len(message["content"]) > MAX_TEXT_BYTES
                or not _text(message.get("content"))):
            raise BackendFailure("backend-message")
        text = message["content"]
        try:
            if len(text.encode("utf-8")) > MAX_TEXT_BYTES or any(marker in text.lower() for marker in ("<think>", "</think>")):
                raise BackendFailure("backend-text")
        except UnicodeError as exc:
            raise BackendFailure("backend-text") from exc
        usage_keys = {"prompt_tokens", "completion_tokens", "total_tokens"}
        if (type(usage) is not dict or len(usage) not in (3, 4)
                or not usage_keys <= set(usage) or set(usage) - usage_keys - {"prompt_tokens_details"}
                or not all(_integer(usage[key]) for key in usage_keys)
                or usage["prompt_tokens"] + usage["completion_tokens"] != usage["total_tokens"]):
            raise BackendFailure("backend-usage")
        if "prompt_tokens_details" in usage:
            details = usage["prompt_tokens_details"]
            if (type(details) is not dict or set(details) != {"cached_tokens"}
                    or not _integer(details["cached_tokens"]) or details["cached_tokens"] > usage["prompt_tokens"]):
                raise BackendFailure("backend-usage")
        result = {"model": self._model, "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                "finish_reason": "stop"}], "usage": {key: usage[key] for key in usage_keys}}
        if len(_encoded(result)) > MAX_RESULT_BYTES:
            raise BackendFailure("backend-size")
        return result

    def _envelope(self, value, key, execution):
        return {"id": str(uuid4()), "object": "chat.completion", "created": int(time.time()),
            **copy.deepcopy(value), "cellivane": {"engine": "seed", "version": VERSION,
            "execution": execution, "key": key, "backend_calls": self._counts["backend_calls"]}}

    def _cache_put(self, key, value):
        size = len(_encoded(value)) + len(key)
        if self._closed or not self._entry_limit or size > self._byte_limit:
            return
        while self._cache and (len(self._cache) >= self._entry_limit or self._bytes + size > self._byte_limit):
            _old_key, (_old_value, old_size) = self._cache.popitem(last=False)
            self._bytes -= old_size
        self._cache[key] = (copy.deepcopy(value), size)
        self._bytes += size

    def _retire(self, key, flight):
        if self._flights.get(key) is flight:
            del self._flights[key]
            self._active -= 1

    def complete(self, payload) -> dict:
        try:
            normalized, key = self._request(payload)
        except InvalidRequest:
            with self._lock:
                self._counts["rejected"] += 1
                if not self._poisoned:
                    self._emit("reject", reason="invalid-request")
            raise
        with self._lock:
            if self._closed:
                self._counts["rejected"] += 1
                if not self._poisoned:
                    self._emit("reject", key, reason="engine-closed")
                raise EngineUnavailable("engine-unavailable")
            if key in self._cache:
                value, _size = self._cache[key]
                self._counts["cache_hits"] += 1
                try:
                    self._emit("cache_hit", key, execution="cache", value=value)
                except EngineUnavailable:
                    self._counts["cache_hits"] -= 1
                    raise
                self._cache.move_to_end(key)
                return self._envelope(value, key, "cache")
            flight = self._flights.get(key)
            if flight is not None:
                if self._waiters >= self._waiter_limit:
                    self._counts["rejected"] += 1
                    self._emit("reject", key, reason="waiter-capacity")
                    raise BusyError("waiter-capacity")
                flight.waiters += 1
                self._waiters += 1
                try:
                    self._emit("join", key, execution="coalesced")
                except EngineUnavailable:
                    flight.waiters -= 1
                    self._waiters -= 1
                    raise
                owner = False
            else:
                if self._active >= self._slots:
                    self._counts["rejected"] += 1
                    self._emit("reject", key, reason="slot-capacity")
                    raise BusyError("slot-capacity")
                flight = _Flight()
                self._flights[key] = flight
                self._active += 1
                self._counts["backend_calls"] += 1
                try:
                    self._emit("start", key, execution="computed")
                except EngineUnavailable:
                    self._counts["backend_calls"] -= 1
                    self._retire(key, flight)
                    raise
                owner = True
        if not owner:
            return self._joined(key, flight)
        began = time.monotonic()
        try:
            try:
                raw = self._backend(copy.deepcopy(normalized))
            except Exception as exc:
                # A backend's exception message can contain prompt material.
                raise BackendFailure("backend-error") from exc
            if time.monotonic() - began > self._timeout:
                raise BackendFailure("backend-deadline")
            value = self._answer(raw)
        except BaseException as exc:
            with self._lock:
                self._retire(key, flight)
                flight.done = True
                flight.error = EngineUnavailable("event-persistence-failed") if self._poisoned else BackendFailure(
                    str(exc) if isinstance(exc, BackendFailure) else "backend-error")
                self._counts["failures"] += 1
                try:
                    if not self._poisoned:
                        self._emit("failure", key, execution="computed", reason=str(flight.error))
                except EngineUnavailable as persistence:
                    flight.error = persistence
                finally:
                    flight.event.set()
                error = flight.error
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise error from exc
        with self._lock:
            self._retire(key, flight)
            if self._poisoned:
                flight.error, flight.done = EngineUnavailable("event-persistence-failed"), True
                flight.event.set()
                raise flight.error
            self._counts["computed"] += 1
            self._cache_put(key, value)
            try:
                self._emit("finish", key, execution="computed", value=value)
                flight.value, flight.done = value, True
                return self._envelope(value, key, "computed")
            except EngineUnavailable as exc:
                self._counts["computed"] -= 1
                self._counts["failures"] += 1
                flight.error, flight.done = exc, True
                raise
            finally:
                flight.event.set()

    def _joined(self, key, flight):
        completed = flight.event.wait(self._timeout)
        with self._lock:
            flight.waiters -= 1
            self._waiters -= 1
            if self._poisoned:
                raise EngineUnavailable("event-persistence-failed")
            if not completed or not flight.done:
                self._counts["timeouts"] += 1
                self._counts["failures"] += 1
                self._emit("failure", key, execution="coalesced", reason="waiter-timeout")
                raise BackendFailure("waiter-timeout")
            if flight.error is not None:
                self._counts["failures"] += 1
                self._emit("failure", key, execution="coalesced", reason=str(flight.error))
                raise type(flight.error)(str(flight.error))
            self._counts["coalesced"] += 1
            try:
                self._emit("finish", key, execution="coalesced", value=flight.value)
            except EngineUnavailable:
                self._counts["coalesced"] -= 1
                raise
            return self._envelope(flight.value, key, "coalesced")

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._cache.clear()
                self._bytes = 0
                self._emit("close", reason="engine-closed")
