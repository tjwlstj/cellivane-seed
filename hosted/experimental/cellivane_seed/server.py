"""Bounded loopback Seed HTTP serving. Tensor execution belongs to the backend.

The experimental bridge's original strict transport is reused as a library.
No tools import, cloud endpoint, retry, native MTP or cross-process KV sharing.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import threading
from uuid import uuid4

from cellivane_bridge.adapters import _load, probe, request
from cellivane_bridge.contracts import EngineConfig
from cellivane_bridge.storage import encoded, local_root
from .core import SeedEngine, InvalidRequest, BusyError, EngineUnavailable, BackendFailure


class Audit:
    """Serialized, exclusive per-record fsync; poison on any persistence failure.

    This is an integrity chain, not authentication or power-loss recovery proof.
    Raw wire records can contain user text; storage stays in the local run folder.
    Limits and the 80% warning apply to this session; no automatic rotation.
    """
    RECORD_LIMIT = 10000
    BYTE_LIMIT = 64 * 1024 * 1024
    WARNING_THRESHOLD_PERCENT = 80

    def __init__(self, root):
        self.session_id = str(uuid4())
        self.directory = local_root(local_root(root) / "seed-runs") / self.session_id
        self.directory.mkdir()
        self.lock = threading.Lock()
        self.sequence, self.previous, self.poisoned = 0, None, False
        self.bytes = 0

    def status(self):
        """Path-free committed usage; warn at >=80% of either per-session limit.

        Bytes count encoded records, not filesystem allocation. This snapshot
        neither reserves append capacity nor rotates or recovers an old run.
        """
        with self.lock:
            return {"session_id": self.session_id,
                    "records": self.sequence, "bytes": self.bytes,
                    "record_limit": self.RECORD_LIMIT, "byte_limit": self.BYTE_LIMIT,
                    "remaining_records": max(0, self.RECORD_LIMIT - self.sequence),
                    "remaining_bytes": max(0, self.BYTE_LIMIT - self.bytes),
                    "warning_threshold_percent": self.WARNING_THRESHOLD_PERCENT,
                    "near_capacity": (
                        self.sequence * 100 >= self.RECORD_LIMIT * self.WARNING_THRESHOLD_PERCENT
                        or self.bytes * 100 >= self.BYTE_LIMIT * self.WARNING_THRESHOLD_PERCENT),
                    "poisoned": self.poisoned}

    def append(self, kind, value):
        with self.lock:
            if self.poisoned:
                raise OSError("audit-poisoned")
            record = {"seq": self.sequence + 1, "previous_sha256": self.previous,
                      "kind": kind, "value": value}
            raw = encoded(record)
            try:
                if self.sequence >= self.RECORD_LIMIT or self.bytes + len(raw) > self.BYTE_LIMIT:
                    raise OSError("audit-capacity-exhausted")
                path = self.directory / (f"{self.sequence + 1:06d}.json")
                with path.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                self.poisoned = True
                raise
            self.sequence += 1
            self.bytes += len(raw)
            self.previous = sha256(raw).hexdigest()


class Backend:
    def __init__(self, config, audit):
        self.config, self.audit = config, audit

    def __call__(self, payload):
        config = EngineConfig(self.config.name, "openai", self.config.base_url,
                              self.config.model, timeout_s=self.config.timeout_s,
                              max_tokens=payload["max_tokens"])
        result = request(config, payload["messages"], threading.Event())
        self.audit.append("backend_exchange", result.record())
        if result.outcome != "ANSWERED":
            raise BackendFailure("backend-" + result.outcome.lower())
        return result.raw_response


class SeedHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = False
    request_queue_size = 8

    def __init__(self, address, engine, audit, max_connections=12):
        if address[0] != "127.0.0.1":
            raise ValueError("seed-loopback-only")
        self.engine, self.audit = engine, audit
        self.connections = threading.BoundedSemaphore(max_connections)
        super().__init__(address, Handler)

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def process_request(self, connection, address):
        connection.settimeout(5)
        if not self.connections.acquire(blocking=False):
            try:
                connection.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
            finally:
                self.shutdown_request(connection)
            return
        try:
            super().process_request(connection, address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, connection, address):
        try:
            super().process_request_thread(connection, address)
        finally:
            self.connections.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "CellivaneSeed/0.1.0"

    def log_message(self, *_args):
        pass

    def reply(self, status, value):
        raw = encoded(value)
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
        except (OSError, socket.timeout):
            # A disconnected subscriber does not own other subscribers' compute.
            # A completed model receipt remains completed; no backend cancel claim.
            pass

    def allowed(self):
        host = self.headers.get_all("Host", [])
        expected = "127.0.0.1:" + str(self.server.server_port)
        if host != [expected] or self.headers.get("Origin") is not None:
            self.reply(403, {"error": {"message": "loopback-host-required-no-browser-origin"}})
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        state = self.server.engine.status()
        if self.path == "/health":
            okay = (not state["closed"] and not state["poisoned"]
                    and not self.server.audit.status()["poisoned"])
            self.reply(200 if okay else 503, {"status": "ok" if okay else "unavailable",
                        "service": "cellivane-seed", "scope": "serving-admission-only"})
        elif self.path == "/v1/models":
            self.reply(200, {"object": "list", "data": [{"id": state["model"],
                       "object": "model", "owned_by": "local-backend"}]})
        elif self.path == "/v1/status":
            self.reply(200, {**state, "tensor_backend": "external-openai-subset",
                        "token_mtp": False, "kv_sharing": False,
                        "canonical_main_binding": False,
                        "audit": self.server.audit.status()})
        else:
            self.reply(404, {"error": {"message": "route-not-supported"}})

    def do_POST(self):
        if not self.allowed():
            return
        if self.path != "/v1/chat/completions":
            self.reply(404, {"error": {"message": "route-not-supported"}})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1
                or len(lengths[0]) > 5 or not lengths[0].isascii() or not lengths[0].isdigit()
                or not 1 <= int(lengths[0]) <= 32 * 1024
                or self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json"):
            self.reply(400, {"error": {"message": "bounded-json-body-required"}})
            return
        try:
            raw = self.rfile.read(int(lengths[0]))
            if len(raw) != int(lengths[0]):
                raise InvalidRequest("truncated-body")
            payload = _load(raw)
            self.reply(200, self.server.engine.complete(payload))
        except (InvalidRequest, ValueError, UnicodeError, RecursionError):
            self.reply(400, {"error": {"message": "invalid-request"}})
        except BusyError:
            self.reply(429, {"error": {"message": "compute-capacity-full"}})
        except EngineUnavailable:
            self.reply(503, {"error": {"message": "engine-unavailable"}})
        except BackendFailure:
            self.reply(502, {"error": {"message": "backend-not-completed"}})
        except (OSError, socket.timeout):
            self.reply(408, {"error": {"message": "request-io-timeout"}})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18941)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--identity", required=True, help="Artifact/config identity; not an attestation")
    parser.add_argument("--storage-root", required=True)
    parser.add_argument("--slots", type=int, default=1)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be 1024..65535")
    config = EngineConfig("seed-tensor-worker", "openai", args.backend_url,
                          args.model, timeout_s=90, max_tokens=128)
    qualification = probe(config)
    if qualification.get("outcome") != "CONFIRMED":
        raise RuntimeError("seed-backend-not-qualified: " + str(qualification))
    audit = Audit(args.storage_root)
    audit.append("configuration", {"engine": "cellivane-seed", "version": "0.1.0",
                 "model": args.model, "identity": args.identity, "slots": args.slots,
                 "backend": config.record(), "qualification": qualification})
    engine = SeedEngine(Backend(config, audit), model=args.model, identity=args.identity,
                        slots=args.slots, event_sink=lambda event: audit.append("scheduler", event))
    server = SeedHTTPServer(("127.0.0.1", args.port), engine, audit)
    print(json.dumps({"ready": True, "url": f"http://127.0.0.1:{args.port}/v1",
                      "audit_directory": str(audit.directory)}), flush=True)
    try:
        server.serve_forever(poll_interval=.1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            engine.close()
        finally:
            server.server_close()
        audit.append("shutdown", engine.status())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
