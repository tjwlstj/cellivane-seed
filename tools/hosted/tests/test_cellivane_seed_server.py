"""Owned loopback HTTP fixtures; these tests never execute a tensor/model backend.

Bridge CONFIRMED/ANSWERED below describes fixture protocol compatibility only.
It does not qualify an actual model, weights, engine process, or native binding.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import http.client
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "hosted" / "experimental"))

from cellivane_bridge import adapters
from cellivane_bridge.contracts import EngineConfig
from cellivane_seed import server as seed_server
from cellivane_seed.core import EngineUnavailable, SeedEngine
from cellivane_seed.server import Audit, SeedHTTPServer


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def payload(content="fixture question", **changes):
    return {"model": "tiny", "messages": [{"role": "user", "content": content}],
            "stream": False, "max_tokens": 16, "temperature": 0, **changes}


def completion(**changes):
    return {"model": "tiny", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": "fixture answer"},
            "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            **changes}


@contextmanager
def serving(backend=None, *, slots=1, max_connections=12):
    with tempfile.TemporaryDirectory(prefix="cellivane-seed-http-fixture-") as folder:
        audit = Audit(Path(folder))
        engine = SeedEngine(backend or (lambda _: completion()), model="tiny",
                            identity="fixture-only-not-actual-model", slots=slots,
                            event_sink=lambda event: audit.append("scheduler", event))
        server = SeedHTTPServer(("127.0.0.1", 0), engine, audit,
                                max_connections=max_connections)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        try:
            yield server, engine, audit
        finally:
            engine.close()
            server.shutdown()
            server.server_close()
            thread.join(2)


def wire(server, method="GET", path="/health", body=None, *, headers=(), defaults=True):
    rows = []
    if defaults:
        rows.append(("Host", "127.0.0.1:" + str(server.server_port)))
        if body is not None:
            rows.extend((("Content-Type", "application/json"),
                         ("Content-Length", str(len(body)))))
    rows.extend(headers)
    request = (method + " " + path + " HTTP/1.1\r\n"
               + "".join(key + ": " + value + "\r\n" for key, value in rows)
               + "Connection: close\r\n\r\n").encode("ascii") + (body or b"")
    with socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as connection:
        connection.sendall(request)
        # A short-body test must expose EOF rather than wait for the server's
        # independent receive timeout. Read-side remains open for its response.
        connection.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(connection)
        try:
            response.begin()
            raw = response.read()
            return response.status, dict(response.getheaders()), json.loads(raw) if raw else None
        finally:
            response.close()


def post(server, value=None, **options):
    return wire(server, "POST", "/v1/chat/completions",
                encoded(payload() if value is None else value), **options)


class SeedServerTests(unittest.TestCase):
    def assert_error(self, response, status):
        actual, headers, body = response
        self.assertEqual(actual, status)
        self.assertEqual(headers["Connection"], "close")
        self.assertIn("application/json", headers["Content-Type"])
        self.assertIsInstance(body["error"]["message"], str)

    def test_health_models_status_and_non_attestation_boundaries(self):
        with serving() as (server, engine, audit):
            status, headers, health = wire(server)
            self.assertEqual(status, 200)
            self.assertEqual(health["status"], "ok")
            self.assertEqual(health["scope"], "serving-admission-only")
            self.assertEqual(headers["Cache-Control"], "no-store")
            status, _, listed = wire(server, path="/v1/models")
            self.assertEqual(status, 200)
            self.assertEqual(listed["object"], "list")
            self.assertEqual([row["id"] for row in listed["data"]], ["tiny"])
            status, _, state = wire(server, path="/v1/status")
            self.assertEqual(status, 200)
            self.assertEqual(state["identity"], "fixture-only-not-actual-model")
            self.assertEqual(state["tensor_backend"], "external-openai-subset")
            self.assertFalse(state["token_mtp"])
            self.assertFalse(state["kv_sharing"])
            self.assertFalse(state["canonical_main_binding"])
            self.assertEqual(state["backend_calls"], 0)
            self.assertEqual(Path(state["audit_directory"]), audit.directory)

    def test_actual_bridge_transport_and_parser_accept_fixture_protocol(self):
        calls = []
        def backend(value):
            calls.append(copy.deepcopy(value))
            return completion()
        with serving(backend) as (server, engine, _):
            config = EngineConfig("seed-fixture", "openai",
                                  f"http://127.0.0.1:{server.server_port}/v1",
                                  "tiny", "reference", 2, 16)
            qualification = adapters.probe(config)
            self.assertEqual(qualification["outcome"], "CONFIRMED")
            self.assertIsNone(qualification["server_version"])
            self.assertIsNone(qualification["model_digest"])
            self.assertEqual([row["endpoint"] for row in qualification["responses"]],
                             ["/health", "/v1/models"])
            result = adapters.request(config, payload()["messages"], threading.Event())
            self.assertEqual(result.outcome, "ANSWERED")
            self.assertEqual(result.text, "fixture answer")
            self.assertEqual(result.raw_response["model"], "tiny")
            self.assertEqual(result.raw_response["choices"][0]["finish_reason"], "stop")
            self.assertEqual(result.usage, completion()["usage"])
            self.assertEqual(result.response_sha256,
                             hashlib.sha256(result.response_body.encode("utf-8")).hexdigest())
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["messages"], payload()["messages"])

    def test_host_must_be_single_exact_loopback_port_and_origin_is_forbidden(self):
        with serving() as (server, engine, _):
            good = "127.0.0.1:" + str(server.server_port)
            cases = [[], [("Host", "localhost:" + str(server.server_port))],
                     [("Host", good), ("Host", good)],
                     [("Host", "127.0.0.1:1")],
                     [("Host", good), ("Origin", "null")],
                     [("Host", good), ("Origin", "https://example.invalid")]]
            for headers in cases:
                with self.subTest(headers=headers):
                    self.assert_error(wire(server, headers=headers, defaults=False), 403)
            self.assertEqual(engine.status()["backend_calls"], 0)

    def test_paths_are_exact_and_no_backend_dispatch_occurs(self):
        with serving() as (server, engine, _):
            for path in ("/", "/health?x=1", "/v1/models/", "/v1/chat/completions/"):
                with self.subTest(path=path):
                    self.assert_error(wire(server, path=path), 404)
            self.assert_error(wire(server, "POST", "/api/chat", encoded(payload())), 404)
            self.assert_error(wire(server, "POST", "/v1/chat/completions/", encoded(payload())), 404)
            self.assertEqual(engine.status()["backend_calls"], 0)

    def test_duplicate_content_length_and_transfer_encoding_rejected(self):
        with serving() as (server, engine, _):
            raw = encoded(payload())
            for headers in ([("Content-Length", str(len(raw)))],
                            [("Content-Length", "1")],
                            [("Transfer-Encoding", "chunked")],
                            [("Transfer-Encoding", "identity")]):
                with self.subTest(headers=headers):
                    self.assert_error(wire(server, "POST", "/v1/chat/completions", raw,
                                           headers=headers), 400)
            self.assertEqual(engine.status()["backend_calls"], 0)

    def test_missing_invalid_zero_and_oversized_content_length_rejected(self):
        with serving() as (server, engine, _):
            host = [("Host", "127.0.0.1:" + str(server.server_port)),
                    ("Content-Type", "application/json")]
            for length in (None, "0", "-1", "+1", "1,1", "32769", "999999999999"):
                headers = host + ([] if length is None else [("Content-Length", length)])
                with self.subTest(length=length):
                    self.assert_error(wire(server, "POST", "/v1/chat/completions", b"{}",
                                           headers=headers, defaults=False), 400)
            self.assertEqual(engine.status()["backend_calls"], 0)

    def test_non_json_content_type_and_truncated_body_rejected(self):
        with serving() as (server, engine, _):
            host = [("Host", "127.0.0.1:" + str(server.server_port))]
            self.assert_error(wire(server, "POST", "/v1/chat/completions", b"{}",
                                   headers=host + [("Content-Length", "2"),
                                                   ("Content-Type", "text/plain")], defaults=False), 400)
            self.assert_error(wire(server, "POST", "/v1/chat/completions", b"{",
                                   headers=host + [("Content-Length", "8"),
                                                   ("Content-Type", "application/json")], defaults=False), 400)
            self.assertEqual(engine.status()["backend_calls"], 0)

    def test_overlong_digit_content_length_is_rejected_without_integer_conversion_failure(self):
        with serving() as (server, engine, _):
            headers = [("Host", "127.0.0.1:" + str(server.server_port)),
                       ("Content-Type", "application/json"), ("Content-Length", "9" * 5000)]
            self.assert_error(wire(server, "POST", "/v1/chat/completions", b"{}",
                                   headers=headers, defaults=False), 400)
            self.assertEqual(engine.status()["backend_calls"], 0)

    def test_malformed_utf8_duplicate_keys_nonfinite_and_nonobject_json_rejected(self):
        with serving() as (server, engine, _):
            cases = [b"{", b"\xff", b"[]", b"null", b'{"model":"tiny","model":"tiny"}',
                     b'{"temperature":NaN}', b'{"temperature":Infinity}',
                     b'{"temperature":1e999}', b'{"x":' + b"[" * 20 + b"0" + b"]" * 20 + b"}"]
            for raw in cases:
                with self.subTest(raw=raw[:80]):
                    self.assert_error(wire(server, "POST", "/v1/chat/completions", raw), 400)
            self.assertEqual(engine.status()["backend_calls"], 0)

    def test_unknown_fields_stream_temperature_model_and_token_budget_rejected(self):
        with serving() as (server, engine, _):
            cases = [payload(extra=True), payload(stream=True), payload(stream=0),
                     payload(temperature=1), payload(temperature=True), payload(model="other"),
                     payload(max_tokens=0), payload(max_tokens=513), payload(max_tokens=True),
                     payload(messages=[]), payload(messages=[{"role": "tool", "content": "x"}]),
                     payload(messages=[{"role": "system", "content": "x"}]),
                     payload(messages=[{"role": "assistant", "content": "x"}]),
                     payload(messages=[{"role": "user", "content": "x", "extra": 1}])]
            for value in cases:
                with self.subTest(value=value):
                    self.assert_error(post(server, value), 400)
            self.assertEqual(engine.status()["backend_calls"], 0)

    def test_invalid_backend_responses_are_502_and_never_answered(self):
        base = completion()
        cases = [completion(model="other"), completion(usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 6}),
                 completion(usage={"prompt_tokens": True, "completion_tokens": 3, "total_tokens": 4}),
                 completion(choices=[{**base["choices"][0], "finish_reason": "length"}]),
                 completion(choices=[{**base["choices"][0], "message": {"role": "assistant", "content": ""}}]),
                 completion(choices=[{**base["choices"][0], "message": {"role": "assistant", "content": "x", "tool_calls": []}}])]
        for value in cases:
            with self.subTest(value=value), serving(lambda _, value=value: value) as (server, engine, _):
                self.assert_error(post(server), 502)
                self.assertEqual(engine.status()["computed"], 0)

    def test_backend_exception_is_502_without_exception_text_disclosure(self):
        def backend(_):
            raise RuntimeError("private-backend-detail")
        with serving(backend) as (server, _, _):
            response = post(server)
            self.assert_error(response, 502)
            self.assertNotIn("private-backend-detail", json.dumps(response[2]))

    def test_distinct_request_exhausting_compute_slot_returns_429_without_queue(self):
        entered, release = threading.Event(), threading.Event()
        responses = []
        def backend(_):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("fixture-gate-timeout")
            return completion()
        with serving(backend) as (server, engine, _):
            worker = threading.Thread(target=lambda: responses.append(post(server, payload("first"))))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assert_error(post(server, payload("different request")), 429)
                self.assertEqual(engine.status()["backend_calls"], 1)
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(responses[0][0], 200)

    def test_connection_admission_is_bounded_and_recovers_after_idle_client_closes(self):
        with serving(max_connections=1) as (server, engine, _):
            first = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
            try:
                until = time.monotonic() + 2
                while server.connections._value != 0 and time.monotonic() < until:
                    time.sleep(.005)
                self.assertEqual(server.connections._value, 0)
                # Connection admission happens before request parsing. Do not
                # race a full request write against the server's immediate
                # close: Windows may reset unread inbound bytes after sending
                # its rejection. Read the pre-parser rejection directly.
                with socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as rejected:
                    response = http.client.HTTPResponse(rejected)
                    try:
                        response.begin()
                        self.assertEqual(response.status, 503)
                        self.assertEqual(response.read(), b"")
                        self.assertEqual(response.getheader("Connection"), "close")
                    finally:
                        response.close()
                self.assertEqual(engine.status()["backend_calls"], 0)
            finally:
                first.close()
            until = time.monotonic() + 2
            while server.connections._value != 1 and time.monotonic() < until:
                time.sleep(.005)
            self.assertEqual(server.connections._value, 1)
            self.assertEqual(wire(server)[0], 200)

    def test_closed_engine_is_503_for_health_and_generation(self):
        with serving() as (server, engine, _):
            engine.close()
            self.assertEqual(wire(server)[0], 503)
            self.assert_error(post(server), 503)
            self.assertEqual(engine.status()["backend_calls"], 0)

    def test_audit_fsync_failure_poison_is_503_before_backend_and_future_admission(self):
        with serving() as (server, engine, audit):
            with mock.patch("cellivane_seed.server.os.fsync", side_effect=OSError("fixture-disk-failure")):
                self.assert_error(post(server), 503)
            self.assertTrue(audit.poisoned)
            self.assertTrue(engine.status()["poisoned"])
            self.assertTrue(engine.status()["closed"])
            self.assertEqual(engine.status()["backend_calls"], 0)
            self.assert_error(post(server, payload("next")), 503)
            self.assertEqual(wire(server)[0], 503)

    def test_audit_record_and_byte_capacity_poison_before_another_file_is_written(self):
        for exhausted in ("records", "bytes"):
            with self.subTest(exhausted=exhausted), tempfile.TemporaryDirectory() as folder:
                audit = Audit(folder)
                audit.append("fixture", {"actual_model_qualification": False})
                files = set(audit.directory.iterdir())
                if exhausted == "records":
                    audit.sequence = 10000
                else:
                    audit.bytes = 64 * 1024 * 1024
                sequence, size, previous = audit.sequence, audit.bytes, audit.previous
                with self.assertRaisesRegex(OSError, "audit-capacity-exhausted"):
                    audit.append("fixture", {"next": True})
                self.assertTrue(audit.poisoned)
                self.assertEqual((audit.sequence, audit.bytes, audit.previous), (sequence, size, previous))
                self.assertEqual(set(audit.directory.iterdir()), files)
                with self.assertRaisesRegex(OSError, "audit-poisoned"):
                    audit.append("fixture", {})

    def test_audit_capacity_rejection_fences_http_backend_admission(self):
        with serving() as (server, engine, audit):
            audit.bytes = 64 * 1024 * 1024
            self.assert_error(post(server), 503)
            self.assertTrue(audit.poisoned)
            self.assertTrue(engine.status()["poisoned"])
            self.assertEqual(engine.status()["backend_calls"], 0)

    def test_standalone_main_closes_http_server_even_if_engine_close_raises(self):
        engine = mock.Mock()
        engine.close.side_effect = EngineUnavailable("fixture-event-persistence-failed")
        engine.status.return_value = {"closed": True, "poisoned": True}
        server = mock.Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(seed_server, "probe", return_value={"outcome": "CONFIRMED"}), \
                    mock.patch.object(seed_server, "SeedEngine", return_value=engine), \
                    mock.patch.object(seed_server, "SeedHTTPServer", return_value=server), \
                    mock.patch("builtins.print"):
                with self.assertRaises(EngineUnavailable):
                    seed_server.main(["--port", "18941", "--backend-url", "http://127.0.0.1:18942/v1",
                                      "--model", "tiny", "--identity", "fixture-only", "--storage-root", folder])
        server.server_close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
