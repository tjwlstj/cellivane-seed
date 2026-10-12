"""Pure bounded backend fixtures; no model, network, or tensor qualification."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from uuid import UUID

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "hosted/experimental"))
from cellivane_seed import BackendFailure, BusyError, EngineUnavailable, InvalidRequest, SeedEngine


def request(text="private user prompt", **changes):
    return {"model": "tiny", "messages": [{"role": "user", "content": text}], **changes}


def answer(text="fixture answer", **changes):
    return {"model": "tiny", "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
        "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}, **changes}


def until(condition, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(.005)
    return bool(condition())


class SeedCoreTests(unittest.TestCase):
    def engine(self, backend=None, **kwargs):
        return SeedEngine(backend or (lambda _payload: answer()), model="tiny", identity="artifact-sha256:fixture", **kwargs)

    def launch(self, engine, payload):
        result = []
        def execute():
            try:
                result.append(engine.complete(payload))
            except BaseException as exc:
                result.append(exc)
        thread = threading.Thread(target=execute, daemon=True)
        thread.start()
        return thread, result

    def test_exact_cache_normalized_defaults_unique_uuid_and_deepcopy(self):
        raw, seen = answer(), []
        def backend(payload):
            seen.append(payload)
            return raw
        engine = self.engine(backend)
        first = engine.complete(request())
        first["choices"][0]["message"]["content"] = "caller mutation"
        first["usage"]["total_tokens"] = 99
        raw["choices"][0]["message"]["content"] = "backend mutation"
        second = engine.complete(request(temperature=0.0, stream=False, max_tokens=128))
        self.assertEqual(second["choices"][0]["message"]["content"], "fixture answer")
        self.assertEqual(second["usage"]["total_tokens"], 5)
        self.assertEqual(second["cellivane"]["execution"], "cache")
        self.assertNotEqual(first["id"], second["id"])
        UUID(first["id"])
        UUID(second["id"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["temperature"], 0)
        self.assertEqual(engine.status()["backend_calls"], 1)

    def test_invalid_input_is_rejected_before_a_warm_cache(self):
        calls = []
        engine = self.engine(lambda payload: calls.append(payload) or answer())
        engine.complete(request())
        invalid = [None, [], request(model="other"), request(extra=1), request(stream=0), request(stream=True),
            request(max_tokens=True), request(max_tokens=0), request(max_tokens=513), request(temperature=False),
            request(temperature=1), request(temperature=float("nan")), request(temperature=10**1000),
            request(messages=[]), request(messages=[{"role": "tool", "content": "x"}]),
            request(messages=[{"role": "system", "content": "x"}]),
            request(messages=[{"role": "assistant", "content": "x"}]),
            request(messages=[{"role": True, "content": "x"}]), request(messages=[{"role": "user", "content": []}]),
            request(messages=[{"role": "user", "content": "x", "name": "n"}]),
            request(messages=[{"role": "user", "content": "\x00"}]), request("한" * 6000),
            request(messages=[{"role": "user", "content": "x"}] * 33), request("\ud800")]
        for payload in invalid:
            with self.subTest(payload=str(payload)[:60]), self.assertRaises(InvalidRequest):
                engine.complete(payload)
        self.assertEqual(len(calls), 1)
        self.assertEqual(engine.status()["cache_hits"], 0)

    def test_inference_fields_role_order_and_identity_change_keys(self):
        engine = self.engine()
        variants = [request(), request("different"), request(max_tokens=64),
            request(messages=[{"role": "assistant", "content": "earlier"},
                              {"role": "user", "content": "private user prompt"}]),
            request(messages=[{"role": "system", "content": "a"}, {"role": "user", "content": "b"}]),
            request(messages=[{"role": "user", "content": "b"}, {"role": "system", "content": "a"}])]
        keys = {engine.complete(payload)["cellivane"]["key"] for payload in variants}
        self.assertEqual(len(keys), len(variants))
        other = SeedEngine(lambda _p: answer(), model="tiny", identity="different-artifact")
        self.assertNotIn(other.complete(request())["cellivane"]["key"], keys)

    def test_identical_concurrency_computes_once_and_assigns_fresh_ids(self):
        gate, entered = threading.Event(), threading.Event()
        def backend(_payload):
            entered.set()
            gate.wait(2)
            return answer()
        engine = self.engine(backend)
        owner, owner_result = self.launch(engine, request())
        self.assertTrue(entered.wait(1))
        waiter, waiter_result = self.launch(engine, request())
        try:
            self.assertTrue(until(lambda: engine.status()["waiters"] == 1))
        finally:
            gate.set()
            owner.join(2)
            waiter.join(2)
        self.assertFalse(owner.is_alive() or waiter.is_alive())
        self.assertEqual(owner_result[0]["cellivane"]["execution"], "computed")
        self.assertEqual(waiter_result[0]["cellivane"]["execution"], "coalesced")
        self.assertNotEqual(owner_result[0]["id"], waiter_result[0]["id"])
        self.assertEqual(engine.status()["backend_calls"], 1)
        self.assertEqual(engine.status()["coalesced"], 1)
        self.assertEqual(engine.status()["active"], 0)

    def test_distinct_overload_and_waiter_limit_have_no_queue_or_dispatch(self):
        gate = threading.Event()
        engine = self.engine(lambda _p: gate.wait(2) and answer(), max_waiters=1)
        owner, _ = self.launch(engine, request())
        self.assertTrue(until(lambda: engine.status()["active"] == 1))
        waiter, _ = self.launch(engine, request())
        try:
            self.assertTrue(until(lambda: engine.status()["waiters"] == 1))
            with self.assertRaises(BusyError):
                engine.complete(request("distinct"))
            with self.assertRaises(BusyError):
                engine.complete(request())
            self.assertEqual(engine.status()["backend_calls"], 1)
            self.assertEqual(engine.status()["inflight"], 1)
        finally:
            gate.set()
            owner.join(2)
            waiter.join(2)

    def test_waiter_timeout_releases_only_waiter_and_does_not_cancel_owner(self):
        gate = threading.Event()
        engine = self.engine(lambda _p: gate.wait(2) and answer(), timeout_s=.03)
        owner, result = self.launch(engine, request())
        self.assertTrue(until(lambda: engine.status()["active"] == 1))
        with self.assertRaisesRegex(BackendFailure, "waiter-timeout"):
            engine.complete(request())
        self.assertEqual(engine.status()["waiters"], 0)
        self.assertEqual(engine.status()["active"], 1)
        self.assertTrue(owner.is_alive())
        gate.set()
        owner.join(2)
        self.assertIsInstance(result[0], BackendFailure)  # Owner itself returned past its deadline.
        self.assertEqual(engine.status()["cache_entries"], 0)
        self.assertEqual(engine.status()["backend_calls"], 1)

    def test_two_slots_allow_two_distinct_callers_without_hidden_executor(self):
        barrier, threads = threading.Barrier(2), []
        def backend(_payload):
            threads.append(threading.get_ident())
            barrier.wait(2)
            return answer()
        engine = self.engine(backend, slots=2)
        one, first = self.launch(engine, request("one"))
        two, second = self.launch(engine, request("two"))
        one.join(2)
        two.join(2)
        self.assertIsInstance(first[0], dict)
        self.assertIsInstance(second[0], dict)
        self.assertEqual(set(threads), {one.ident, two.ident})
        self.assertEqual(engine.status()["backend_calls"], 2)

    def test_lru_promotes_hits_and_evicts_oldest(self):
        engine = self.engine(cache_entries=2)
        for text in ("a", "b", "a", "c", "b"):
            engine.complete(request(text))
        self.assertEqual(engine.status()["backend_calls"], 4)
        self.assertEqual(engine.status()["cache_hits"], 1)
        self.assertEqual(engine.status()["cache_entries"], 2)

    def test_cache_byte_bound_and_disabled_cache(self):
        engine = self.engine(cache_bytes=400)
        for text in ("a", "b", "a"):
            engine.complete(request(text))
            self.assertLessEqual(engine.status()["cache_bytes"], 400)
        self.assertEqual(engine.status()["backend_calls"], 3)
        self.assertEqual(engine.status()["cache_entries"], 1)
        disabled = self.engine(cache_entries=0)
        disabled.complete(request())
        disabled.complete(request())
        self.assertEqual(disabled.status()["cache_entries"], 0)
        self.assertEqual(disabled.status()["backend_calls"], 2)

    def test_invalid_outputs_are_never_cached_and_capacity_recovers(self):
        choice = answer()["choices"][0]
        bad = [None, answer(model="wrong"), answer(choices=[]), answer(choices=[choice, choice]),
            answer(choices=[{**choice, "index": True}]), answer(choices=[{**choice, "finish_reason": "length"}]),
            answer(choices=[{**choice, "message": {"role": "assistant", "content": ""}}]),
            answer(choices=[{**choice, "message": {"role": "assistant", "content": "<think>secret</think>ok"}}]),
            answer(choices=[{**choice, "message": {"role": "assistant", "content": "ok", "tool_calls": []}}]),
            answer(reasoning="hidden"), answer(usage={"prompt_tokens": True, "completion_tokens": 2, "total_tokens": 3}),
            answer(usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 6}),
            answer(usage={"prompt_tokens": -1, "completion_tokens": 2, "total_tokens": 1}),
            answer("x" * (256 * 1024)), answer("\ud800"), answer("\x00")]
        for raw in bad:
            with self.subTest(raw=str(raw)[:70]):
                results = iter((raw, answer()))
                engine = self.engine(lambda _p: next(results))
                with self.assertRaises(BackendFailure):
                    engine.complete(request())
                self.assertEqual(engine.status()["active"], 0)
                self.assertEqual(engine.status()["cache_entries"], 0)
                self.assertEqual(engine.complete(request())["cellivane"]["execution"], "computed")
                self.assertEqual(engine.status()["backend_calls"], 2)

    def test_real_llamafile_known_cached_token_detail_is_checked_then_normalized(self):
        usage = {**answer()["usage"], "prompt_tokens_details": {"cached_tokens": 0}}
        engine = self.engine(lambda _p: answer(usage=usage))
        self.assertEqual(engine.complete(request())["usage"], answer()["usage"])
        for details in ({"cached_tokens": True}, {"cached_tokens": -1}, {"cached_tokens": 4},
                        {"cached_tokens": 0, "other": 0}, {}):
            bad = self.engine(lambda _p: answer(usage={**usage, "prompt_tokens_details": details}))
            with self.subTest(details=details), self.assertRaises(BackendFailure):
                bad.complete(request())

    def test_entire_serialized_response_fits_bridge_256kib_for_computed_and_cache(self):
        text = "x" * (256 * 1024 - 1400)
        engine = self.engine(lambda _p: answer(text), cache_bytes=1024 * 1024)
        for response in (engine.complete(request()), engine.complete(request())):
            raw = json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            self.assertLessEqual(len(raw), 256 * 1024)
            self.assertEqual(response["choices"][0]["message"]["content"], text)
        # JSON escaping, not just raw text bytes, counts toward the bound.
        escaped = self.engine(lambda _p: answer("\n" * (128 * 1024) + "ok"))
        with self.assertRaisesRegex(BackendFailure, "backend-size"):
            escaped.complete(request())
        self.assertEqual(escaped.status()["cache_entries"], 0)

    def test_backend_failure_fans_out_but_next_attempt_can_recover(self):
        gate, calls = threading.Event(), []
        def backend(_payload):
            calls.append(1)
            if len(calls) == 1:
                gate.wait(2)
                raise RuntimeError("private user prompt")
            return answer()
        events = []
        engine = self.engine(backend, event_sink=events.append)
        owner, first = self.launch(engine, request())
        self.assertTrue(until(lambda: engine.status()["active"] == 1))
        waiter, second = self.launch(engine, request())
        self.assertTrue(until(lambda: engine.status()["waiters"] == 1))
        gate.set()
        owner.join(2)
        waiter.join(2)
        self.assertIsInstance(first[0], BackendFailure)
        self.assertIsInstance(second[0], BackendFailure)
        self.assertEqual(engine.status()["active"], 0)
        self.assertEqual(engine.status()["waiters"], 0)
        self.assertNotIn("private user prompt", json.dumps(events))
        self.assertEqual(engine.complete(request())["cellivane"]["execution"], "computed")

    def test_start_sink_failure_poison_prevents_backend_call_and_future_cache(self):
        calls = []
        def fail(_event):
            raise OSError("disk-full")
        engine = self.engine(lambda p: calls.append(p) or answer(), event_sink=fail)
        with self.assertRaises(EngineUnavailable):
            engine.complete(request())
        self.assertFalse(calls)
        status = engine.status()
        self.assertTrue(status["closed"] and status["poisoned"])
        self.assertEqual(status["backend_calls"], 0)
        self.assertEqual(status["active"], 0)
        with self.assertRaises(EngineUnavailable):
            engine.complete(request())

    def test_finish_sink_failure_never_returns_or_caches_valid_answer(self):
        def sink(event):
            if event["kind"] == "finish":
                raise OSError("disk-full")
        engine = self.engine(event_sink=sink)
        with self.assertRaises(EngineUnavailable):
            engine.complete(request())
        self.assertTrue(engine.status()["poisoned"])
        self.assertEqual(engine.status()["cache_entries"], 0)
        self.assertEqual(engine.status()["computed"], 0)

    def test_cache_hit_sink_failure_clears_existing_cache_and_blocks_answer(self):
        def sink(event):
            if event["kind"] == "cache_hit":
                raise OSError("disk-full")
        engine = self.engine(event_sink=sink)
        engine.complete(request())
        with self.assertRaises(EngineUnavailable):
            engine.complete(request())
        self.assertTrue(engine.status()["poisoned"])
        self.assertEqual(engine.status()["cache_entries"], 0)
        self.assertEqual(engine.status()["cache_hits"], 0)

    def test_join_sink_failure_wakes_owner_and_releases_waiters_without_answer(self):
        gate = threading.Event()
        def sink(event):
            if event["kind"] == "join":
                raise OSError("disk-full")
        engine = self.engine(lambda _p: gate.wait(2) and answer(), event_sink=sink)
        owner, result = self.launch(engine, request())
        self.assertTrue(until(lambda: engine.status()["active"] == 1))
        with self.assertRaises(EngineUnavailable):
            engine.complete(request())
        self.assertEqual(engine.status()["waiters"], 0)
        gate.set()
        owner.join(2)
        self.assertIsInstance(result[0], EngineUnavailable)
        self.assertEqual(engine.status()["active"], 0)

    def test_events_are_monotonic_private_and_response_digest_is_replayable(self):
        events = []
        engine = self.engine(event_sink=events.append)
        first, second = engine.complete(request()), engine.complete(request())
        self.assertEqual([event["seq"] for event in events], [1, 2, 3])
        self.assertEqual([event["kind"] for event in events], ["start", "finish", "cache_hit"])
        self.assertEqual(events[0]["backend_calls"], 1)
        normalized = {key: first[key] for key in ("model", "choices", "usage")}
        digest = hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(events[1]["response_sha256"], digest)
        self.assertEqual(events[2]["response_sha256"], digest)
        self.assertNotIn("private user prompt", json.dumps(events))
        self.assertNotIn("fixture answer", json.dumps(events))
        self.assertEqual(first["cellivane"]["key"], second["cellivane"]["key"])

    def test_close_blocks_new_admission_but_existing_owner_finishes_without_cache(self):
        gate = threading.Event()
        engine = self.engine(lambda _p: gate.wait(2) and answer())
        owner, result = self.launch(engine, request())
        self.assertTrue(until(lambda: engine.status()["active"] == 1))
        engine.close()
        engine.close()
        with self.assertRaises(EngineUnavailable):
            engine.complete(request())
        gate.set()
        owner.join(2)
        self.assertIsInstance(result[0], dict)
        self.assertEqual(engine.status()["active"], 0)
        self.assertEqual(engine.status()["cache_entries"], 0)

    def test_constructor_rejects_bool_nonfinite_or_unbounded_limits(self):
        for kwargs in ({"slots": True}, {"slots": 0}, {"slots": 33}, {"cache_bytes": -1},
                {"cache_entries": 4097}, {"max_waiters": True}, {"timeout_s": True},
                {"timeout_s": float("inf")}, {"timeout_s": 10**1000}, {"timeout_s": 0}):
            with self.subTest(kwargs=str(kwargs)[:60]), self.assertRaises(ValueError):
                self.engine(**kwargs)


if __name__ == "__main__":
    unittest.main()
