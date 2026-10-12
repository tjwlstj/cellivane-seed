"""Synthetic CPU speculation guards; no model, subprocess, or network download.

HTTP tests use a local in-process fixture server. Archive tests use tiny local
ZIPs, and scheduler observations are explicit synthetic pinned-log examples.
"""
from contextlib import contextmanager, redirect_stderr
from copy import deepcopy
from hashlib import sha256
import http.server
import io
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import cellivane_llama_assets as assets
import cellivane_seed_speculate as spec


def slot_line(accepted=2, generated=4, ratio="0.50000", task=1):
    return (f"slot print_timing: id 0 | task {task} | draft acceptance = {ratio} "
            f"({accepted:5d} accepted / {generated:5d} generated), mean len =  1.50\n")


def common_line(accepted=2, generated=4):
    return ("spec common_specu: statistics     draft-simple: #calls(b,g,a) =    1      2      3, "
            f"#gen drafts =      2, #acc drafts =     1, #gen tokens = {generated:6d}, "
            f"#acc tokens = {accepted:5d}, mean len = 1.50, dur(b,g,a) = 0.1 0.2 0.3\n")


def synthetic_probe_context():
    return ["Template application failed: ", "------------",
        "While executing CallExpression at line 41, column 134 in source:",
        r"...'\n</think>\n\n' + content.lstrip('\n') }}...",
        "                                           ^",
        "Error: Callee is not a function: got Undefined (hint: 'lstrip')",
        "\x1b[1m\x1b[38;5;214mcompare_reasoning_scope: Template application failed",
        "\x1b[0m\x1b[1m\x1b[38;5;214mPhase 2: Content analysis",
        "\x1b[0m\x1b[1m\x1b[38;5;214mPhase 3: Tool call analysis"]


@contextmanager
def fixture_http(raw=b'{"status":"ok"}', *, status=200,
                 content_type="application/json", encoding="identity"):
    captured = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()
        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            captured.append((self.path, self.rfile.read(size)))
            self.respond()
        def respond(self):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Encoding", encoding)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield server.server_port, captured
    finally:
        server.shutdown()
        thread.join(2)
        server.server_close()


def good_row(case=None):
    row = {"response": {"status": 200, "body": {
               "cellivane": {"execution": "computed"},
               "usage": {"completion_tokens": 8}},
               "raw_response": "synthetic", "request": {}},
           "wall_s": 1.0, "text": "synthetic answer",
           "backend_receipt": {"outcome": "ANSWERED"}, "backend_exchange_count": 1}
    if case is not None:
        row["case"] = case
    return row


def good_summary():
    phases = []
    for mode in ("none", "draft"):
        phases.append({"phase_id": "round-0-" + mode, "round": 0, "mode": mode,
            "outcome": "COMPLETED", "warmup": good_row(),
            "rows": [good_row(case["id"]) for case in spec.CASES],
            "state": {"closed": True, "poisoned": False, "backend_calls": 5, "computed": 5,
                      **dict.fromkeys(("active", "inflight", "waiters", "cache_hits", "coalesced", "failures", "timeouts"), 0)},
            "cleanup": {"job_cleanup_confirmed": True},
            "ports_closed": {"18951": True, "18952": True},
            "source_unchanged_during_run": True, "models_unchanged_during_run": True,
            "backend_assets_unchanged_during_run": True,
            "speculation_observed": spec.observe_speculation(b"synthetic healthy baseline\n")})
    return {"configuration": spec.configuration(1), "phases": phases}


class SpeculationGuardTests(unittest.TestCase):
    def test_configuration_exact_numeric_types_and_ranges(self):
        for repeat, cap in ((True, 128), (0, 128), (4, 128), (1, True), (1, 63), (1, 129)):
            with self.subTest(repeat=repeat, cap=cap), self.assertRaises(ValueError):
                spec.configuration(repeat, cap)
        value = spec.configuration(3, 128)
        self.assertEqual((value["threads"], value["slots"], value["cache_entries"]), (4, 1, 0))
        self.assertEqual(value["no_think"], {"enable_thinking": False})

    def test_interleaved_fresh_phase_order(self):
        self.assertEqual(spec.phase_order(3), [(0, "none"), (0, "draft"),
            (1, "draft"), (1, "none"), (2, "none"), (2, "draft")])

    def test_cpu_command_disables_thinking_and_prompt_reuse(self):
        prepared = {"server": "fixture-server.exe"}
        models = {"target": {"path": "target.gguf"}, "draft": {"path": "draft.gguf"}}
        args = spec.command(prepared, models, spec.configuration(), "none", 18952)
        self.assertEqual(args[args.index("--spec-type") + 1], "none")
        self.assertNotIn("--spec-draft-model", args)
        for name, expected in (("--host", "127.0.0.1"), ("--gpu-layers", "0"),
                ("--reasoning", "off"), ("--chat-template-kwargs", '{"enable_thinking":false}'),
                ("--reasoning-format", "auto"), ("--cache-ram", "0"), ("--fit", "off")):
            self.assertEqual(args[args.index(name) + 1], expected)
        self.assertIn("--no-cache-prompt", args)

    def test_draft_command_uses_pinned_new_flags_and_greedy_cpu(self):
        args = spec.command({"server": "server"}, {"target": {"path": "t"},
            "draft": {"path": "d"}}, spec.configuration(), "draft", 18952)
        self.assertEqual(args[args.index("--spec-type") + 1], "draft-simple")
        self.assertEqual(args[args.index("--spec-draft-model") + 1], "d")
        self.assertEqual(args[args.index("--spec-draft-sampling") + 1], "greedy")
        self.assertEqual(args[args.index("--spec-draft-ngl") + 1], "0")
        self.assertNotIn("--draft-max", args)
        for mode, port in (("mtp", 18952), ("draft", True), ("draft", 80)):
            with self.assertRaises(ValueError):
                spec.command({"server": "s"}, {}, {}, mode, port)

    def test_explicit_model_paths_required_before_prepare(self):
        with patch.object(assets, "prepare_assets") as prepare, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                spec.main(["--asset-root", "fixture", "--storage-root", "fixture"])
        prepare.assert_not_called()

    def test_source_closure_is_fixed_and_excludes_unrelated_engines(self):
        self.assertEqual(len(spec.SOURCE_PATHS), 13)
        self.assertEqual(len(set(spec.SOURCE_PATHS)), 13)
        self.assertFalse(any("ollama" in p or "placement" in p or "compare" in p for p in spec.SOURCE_PATHS))
        self.assertTrue(all((spec.REPO / p).is_file() for p in spec.SOURCE_PATHS))

    def test_native_per_request_and_cumulative_counters_are_distinct(self):
        raw = (slot_line() + common_line() + slot_line(1, 4, "0.25000", 2)
               + common_line(3, 8)).encode()
        observed = spec.observe_speculation(raw)
        self.assertEqual((observed["offered_tokens"], observed["accepted_tokens"]), (8, 3))
        self.assertEqual([r["generated_tokens"] for r in observed["implementation_counters"]], [4, 8])
        self.assertTrue(observed["actual_speculation_observed"])
        self.assertFalse(observed["native_mtp"])
        self.assertFalse(observed["actual_backend_qualification"])

    def test_missing_or_only_common_counters_never_prove_verification(self):
        for raw in (b"--spec-type draft-simple\n", common_line().encode()):
            observed = spec.observe_speculation(raw)
            self.assertEqual(observed["offered_tokens"], 0)
            self.assertFalse(observed["actual_speculation_observed"])

    def test_zero_accepted_is_honest_attempt_without_acceptance_claim(self):
        observed = spec.observe_speculation(slot_line(0, 4, "0.00000").encode())
        self.assertTrue(observed["draft_attempt_observed"])
        self.assertFalse(observed["accepted_token_observed"])
        self.assertFalse(observed["actual_speculation_observed"])

    def test_counter_tampering_and_pinned_format_rejected(self):
        for raw in (slot_line(5, 4, "1.25000"), slot_line(0, 0, "0.00000"),
                    slot_line(2, 4, "0.90000"), slot_line().replace("mean len", "bad len"),
                    common_line(5, 4)):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                spec.observe_speculation(raw.encode())

    def test_fatal_after_valid_stats_blocks_observed_success(self):
        observed = spec.observe_speculation((slot_line() + "error: runner failed\n").encode())
        self.assertEqual(observed["accepted_tokens"], 2)
        self.assertTrue(observed["errors"])
        self.assertFalse(observed["actual_speculation_observed"])

    def test_worker_log_bounds_and_exact_bytes_type(self):
        for raw in ("text", b"x" * (spec.MAX_LOG_BYTES + 1)):
            with self.assertRaises(ValueError):
                spec.observe_speculation(raw)

    def test_partial_utf8_token_pieces_preserve_ascii_native_observations(self):
        raw = b"token piece: '\xec'\r\n" + slot_line().encode() + b"token piece: '\x84\xb8'\n" + common_line().encode()
        observed = spec.observe_speculation(raw)
        self.assertEqual(observed["parsing_policy"], {"decode": "utf-8", "errors": "replace",
            "counter_lines": "ASCII-only-full-line", "raw_bytes_preserved": True,
            "template_probe_errors": "bounded-context-warning"})
        self.assertEqual((observed["offered_tokens"], observed["accepted_tokens"]), (4, 2))
        self.assertEqual(observed["request_counters"][0]["line"], 2)
        self.assertEqual(observed["implementation_counters"][0]["line"], 4)
        self.assertTrue(observed["actual_speculation_observed"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.log"
            path.write_bytes(raw)
            self.assertEqual(spec.file_record(path)["sha256"], sha256(raw).hexdigest())
            self.assertEqual(path.read_bytes(), raw)

    def test_corrupt_or_unicode_counter_line_cannot_become_valid_proof(self):
        for raw in (slot_line().encode().replace(b"0.50000", b"0.50\xff000"),
                    slot_line().replace("    2 accepted", "    ٢ accepted").encode(),
                    (common_line().rstrip() + ", token=한글\n").encode()):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                spec.observe_speculation(raw)

    def test_fatal_scan_survives_unrelated_invalid_utf8_fragments(self):
        raw = b"token: \xec\n" + slot_line().encode() + b"error: backend failed \xff\n"
        observed = spec.observe_speculation(raw)
        self.assertTrue(observed["errors"])
        self.assertFalse(observed["actual_speculation_observed"])

    def test_exact_bounded_nonfatal_probe_preserved_as_warning(self):
        context = synthetic_probe_context()
        raw = ("unrelated prefix\n" + "\n".join(context) + "\n" + slot_line()).encode()
        observed = spec.observe_speculation(raw)
        self.assertEqual(observed["errors"], [])
        self.assertEqual(observed["warnings"], [{"line": 7, "raw": context[5],
            "classification": "template-analysis-reasoning-probe-fallback"}])
        self.assertTrue(observed["actual_speculation_observed"])

    def test_bare_different_and_missing_probe_context_remain_errors(self):
        candidates = [[synthetic_probe_context()[5]]]
        for index in range(9):
            context = synthetic_probe_context()
            if index == 5:
                context[index] = context[index].replace("'lstrip'", "'rstrip'")
            else:
                context[index] = "wrong-context"
            candidates.append(context)
        candidates.append(synthetic_probe_context()[:-1])
        for context in candidates:
            with self.subTest(context=context):
                observed = spec.observe_speculation(("\n".join(context) + "\n" + slot_line()).encode())
                self.assertTrue(observed["errors"])
                self.assertEqual(observed["warnings"], [])
                self.assertFalse(observed["actual_speculation_observed"])

    def test_nonfatal_probe_does_not_hide_later_fatal(self):
        for fatal in ("error: actual runner failed", "GGML_ASSERT(buffer)", "CUDA error: 2", "out of memory"):
            raw = ("\n".join(synthetic_probe_context()) + "\n" + slot_line() + fatal + "\n").encode()
            with self.subTest(fatal=fatal):
                observed = spec.observe_speculation(raw)
                self.assertEqual(len(observed["warnings"]), 1)
                self.assertTrue(observed["errors"])
                self.assertFalse(observed["actual_speculation_observed"])

    def test_reasoning_parser_choice_changes_configuration_identity(self):
        current = spec.configuration(1)
        self.assertEqual(current["reasoning_format"], "auto")
        previous = deepcopy(current)
        del previous["reasoning_format"]
        self.assertNotEqual(spec.digest(current), spec.digest(previous))

    def test_file_record_hashes_raw_crlf_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.log"
            raw = b"line1\r\nline2\r\n"
            path.write_bytes(raw)
            self.assertEqual(spec.file_record(path)["sha256"], sha256(raw).hexdigest())
            self.assertEqual(spec.file_record(path)["bytes"], len(raw))
            with self.assertRaises(ValueError):
                spec.file_record(path, maximum=1)

    def test_http_fixture_records_exact_canonical_request(self):
        body = spec.payload(spec.configuration(), "fixture only")
        with fixture_http() as (port, captured):
            reply = spec.exchange(port, "/v1/chat/completions", body)
        self.assertEqual(reply["status"], 200)
        self.assertEqual(captured, [("/v1/chat/completions", spec.encoded(body))])

    def test_http_duplicate_nonfinite_and_bad_headers_rejected(self):
        cases = [(b'{"x":1,"x":2}', "application/json", "identity"),
                 (b'{"x":NaN}', "application/json", "identity"),
                 (b'{"x":1}', "text/plain", "identity"),
                 (b'{"x":1}', "application/json", "gzip")]
        for raw, content_type, encoding in cases:
            with self.subTest(raw=raw, content_type=content_type, encoding=encoding):
                with fixture_http(raw, content_type=content_type, encoding=encoding) as (port, _):
                    with self.assertRaises(ValueError):
                        spec.exchange(port, "/health")

    def test_http_bound_and_endpoint_guard_no_connection(self):
        with patch.object(spec.http.client, "HTTPConnection") as connect:
            for port, path in ((True, "/health"), (80, "/health"), (18952, "/redirect")):
                with self.assertRaises(ValueError):
                    spec.exchange(port, path)
            connect.assert_not_called()
        with fixture_http(b"x" * (256 * 1024 + 1)) as (port, _):
            with self.assertRaisesRegex(ValueError, "response-size"):
                spec.exchange(port, "/health")

    def test_http_redirect_status_not_followed_or_promoted(self):
        with fixture_http(status=302) as (port, _):
            self.assertEqual(spec.exchange(port, "/health")["status"], 302)

    def test_seed_shutdown_still_closes_socket_after_persistence_failure(self):
        engine, server, thread, audit = Mock(), Mock(), Mock(), Mock()
        engine.close.side_effect = OSError("fixture persistence failure")
        engine.status.return_value = {"closed": True, "poisoned": True}
        thread.is_alive.side_effect = [True, False]
        phase = {}
        with self.assertRaisesRegex(OSError, "persistence"):
            spec.shutdown_seed(engine, server, thread, audit, phase)
        server.shutdown.assert_called_once()
        server.server_close.assert_called_once()
        thread.join.assert_called_once_with(10)
        self.assertTrue(phase["state"]["closed"])

    def test_server_created_but_thread_not_started_does_not_deadlock_shutdown(self):
        engine, server, thread = Mock(), Mock(), Mock()
        thread.is_alive.return_value = False
        engine.status.return_value = {"closed": True}
        spec.shutdown_seed(engine, server, thread, None, {})
        server.shutdown.assert_not_called()
        server.server_close.assert_called_once()

    def test_insufficient_memory_blocks_before_owned_process(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            models = {"target": {"path": "t", **spec.MODELS["target"]},
                      "draft": {"path": "d", **spec.MODELS["draft"]}}
            snapshot = {"source": "synthetic", "available_bytes": 4 * 1024**3 + 1,
                        "total_bytes": 32 * 1024**3, "scope": "synthetic"}
            with patch.object(spec, "memory_snapshot", return_value=snapshot), \
                    patch.object(spec, "source_hashes", return_value={}), \
                    patch.object(spec, "cpu_environment", return_value=dict.fromkeys((
                        "CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "GGML_VK_VISIBLE_DEVICES"), "-1")), \
                    patch.object(spec.lab, "port_free"), \
                    patch.object(spec.lab, "OwnedProcess") as spawn, \
                    patch.object(assets, "file_sha256", side_effect=lambda p: models["target" if str(p) == "t" else "draft"]["sha256"]):
                phase = spec.run_phase(base, base, {"engine_identity": "a" * 64, "server": "fixture"},
                    models, spec.configuration(1), 0, "draft", (18951, 18952), expected_sources={})
            spawn.assert_not_called()
            self.assertEqual(phase["admission"]["outcome"], "BLOCKED")
            self.assertEqual(phase["outcome"], "FAIL")
            self.assertIsNone(phase["cleanup"])

    def test_operational_completion_does_not_require_speedup_or_acceptance(self):
        summary = good_summary()
        summary["phases"][1]["speculation_observed"] = spec.observe_speculation(slot_line(0, 4, "0.00000").encode())
        for row in summary["phases"][1]["rows"]:
            row["wall_s"] = 3.0
            row["text"] = "different valid output"
        self.assertEqual(spec.recording_errors(summary), [])
        pairs, metrics = spec.summarize(summary["phases"], 1)
        self.assertEqual(metrics["pairs"], {"total": 4, "both_answered": 4, "text_equal": 0})
        self.assertTrue(all(not row["text_equal"] for row in pairs))
        self.assertGreater(metrics["draft"]["median_wall_s"], metrics["none"]["median_wall_s"])

    def test_uncertain_receipt_cache_hit_missing_warmup_and_cleanup_fail_gate(self):
        for change in ("receipt", "cache", "warmup", "cleanup", "port", "source", "assets", "state", "coverage"):
            with self.subTest(change=change):
                summary = good_summary()
                phase = summary["phases"][0]
                if change == "receipt": phase["rows"][0]["backend_receipt"] = None
                if change == "cache": phase["rows"][0]["response"]["body"]["cellivane"]["execution"] = "cache"
                if change == "warmup": phase["warmup"] = None
                if change == "cleanup": phase["cleanup"]["job_cleanup_confirmed"] = False
                if change == "port": phase["ports_closed"]["18951"] = False
                if change == "source": phase["source_unchanged_during_run"] = False
                if change == "assets": phase["backend_assets_unchanged_during_run"] = False
                if change == "state": phase["state"]["backend_calls"] = 4
                if change == "coverage": summary["phases"].pop()
                self.assertTrue(spec.recording_errors(summary))

    def test_baseline_with_observed_draft_is_rejected(self):
        summary = good_summary()
        summary["phases"][0]["speculation_observed"] = spec.observe_speculation(slot_line().encode())
        self.assertIn("round-0-none:worker-log-error-or-unexpected-draft", spec.recording_errors(summary))


class PhaseIntegrityTests(unittest.TestCase):
    def fixtures(self, base):
        prepared = {"directory": str(base / "asset"), "server": "fixture-server",
                    "engine_identity": "a" * 64}
        models = {kind: {"path": kind, **pin} for kind, pin in spec.MODELS.items()}
        sources = {"fixture-source": "b" * 64}
        snapshot = {"source": "synthetic", "available_bytes": 32 * 1024**3,
                    "total_bytes": 32 * 1024**3, "scope": "synthetic"}
        environment = dict.fromkeys(("CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES",
            "ROCR_VISIBLE_DEVICES", "GGML_VK_VISIBLE_DEVICES"), "-1")
        return prepared, models, sources, snapshot, environment

    def test_changed_source_model_or_asset_between_phases_blocks_launch(self):
        for changed in ("source", "model", "asset"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                prepared, models, sources, snapshot, environment = self.fixtures(base)
                current_sources = {} if changed == "source" else sources
                def checked(path, kind):
                    if changed == "model": raise ValueError("synthetic-model-tamper")
                    return models[kind]
                with patch.object(spec, "memory_snapshot", return_value=snapshot), \
                        patch.object(spec, "source_hashes", return_value=current_sources), \
                        patch.object(spec, "checked_model", side_effect=checked), \
                        patch.object(assets, "prepare_assets", return_value={} if changed == "asset" else prepared), \
                        patch.object(spec, "cpu_environment", return_value=environment), \
                        patch.object(spec.lab, "port_free"), \
                        patch.object(spec.lab, "OwnedProcess") as spawn:
                    phase = spec.run_phase(base, base, prepared, models, spec.configuration(1),
                        0, "draft", (18951, 18952), expected_sources=sources)
                spawn.assert_not_called()
                self.assertEqual(phase["outcome"], "FAIL")
                self.assertIsNone(phase["cleanup"])

    def test_changed_source_or_asset_during_phase_fails_and_preserves_raw_log(self):
        for changed in ("source", "asset"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                prepared, models, sources, snapshot, environment = self.fixtures(base)
                created = []
                class Child:
                    def __init__(self, stack, command, env, folder, label):
                        self.process = Mock()
                        self.cleanup = None
                        (folder / "worker.log").write_bytes(b"synthetic token fragment: \xec\n")
                        created.append(self)
                        stack.callback(self.close)
                    def close(self):
                        self.cleanup = {"job_cleanup_confirmed": True}
                engine, server, thread, audit = Mock(), Mock(), Mock(), Mock()
                engine.status.return_value = deepcopy(good_summary()["phases"][0]["state"])
                thread.is_alive.side_effect = [True, False]
                audit.directory = base / "audit-fixture"
                source_values = [sources, {} if changed == "source" else sources]
                asset_values = [prepared, {} if changed == "asset" else prepared]
                with patch.object(spec, "memory_snapshot", return_value=snapshot), \
                        patch.object(spec, "source_hashes", side_effect=source_values), \
                        patch.object(spec, "checked_model", side_effect=lambda path, kind: models[kind]), \
                        patch.object(assets, "prepare_assets", side_effect=asset_values), \
                        patch.object(spec, "cpu_environment", return_value=environment), \
                        patch.object(spec.lab, "port_free"), \
                        patch.object(spec.lab, "OwnedProcess", Child), \
                        patch.object(spec.lab, "ready", return_value={"status": "ok"}), \
                        patch.object(spec, "Audit", return_value=audit), \
                        patch.object(spec, "Backend"), patch.object(spec, "SeedEngine", return_value=engine), \
                        patch.object(spec, "SeedHTTPServer", return_value=server), \
                        patch.object(spec.threading, "Thread", return_value=thread), \
                        patch.object(spec, "request_record", side_effect=lambda *args: good_row()), \
                        patch.object(spec, "owned_memory_snapshot", return_value={"available": False}), \
                        patch("builtins.print"):
                    phase = spec.run_phase(base, base, prepared, models, spec.configuration(1),
                        0, "draft", (18951, 18952), expected_sources=sources)
                self.assertEqual(len(created), 1)
                self.assertEqual(phase["outcome"], "FAIL")
                self.assertTrue(phase["cleanup"]["job_cleanup_confirmed"])
                self.assertIsNotNone(phase["worker_log"])
                self.assertIsNotNone(phase["speculation_observed"])
                self.assertFalse(phase["source_unchanged_during_run"] if changed == "source"
                                 else phase["backend_assets_unchanged_during_run"])
                server.server_close.assert_called_once()

    def test_main_stops_before_second_phase_after_integrity_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            failed = good_summary()["phases"][0]
            failed["outcome"] = "FAIL"
            failed["backend_assets_unchanged_during_run"] = False
            with patch.object(spec, "source_hashes", return_value={}), \
                    patch.object(assets, "prepare_assets", return_value={}), \
                    patch.object(spec, "inspect_backend", return_value={}), \
                    patch.object(spec, "checked_model", return_value={}), \
                    patch.object(spec, "run_phase", return_value=failed) as run, \
                    patch("builtins.print"):
                result = spec.main(["--asset-root", directory, "--storage-root", directory,
                    "--target-file", "target", "--draft-file", "draft", "--repeat", "3"])
            self.assertEqual(result, 1)
            run.assert_called_once()


class AssetGuardTests(unittest.TestCase):
    def package(self, names):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as package:
            for name in names:
                entry = name
                if type(name) is str:
                    entry = zipfile.ZipInfo(name)
                    # ZipInfo normalizes native backslashes on Windows. Restore
                    # the raw hostile filename so this fixture reaches parser.
                    entry.filename = name
                package.writestr(entry, b"synthetic binary fixture")
        stream.seek(0)
        return zipfile.ZipFile(stream)

    def test_archive_rejects_windows_and_traversal_aliases(self):
        for name in ("../escape.exe", "/absolute.exe", "x/../escape.exe",
                     "C:/escape.exe", "x/alias. /bad.exe", "x/alias./bad.exe", "x//bad.exe"):
            with self.subTest(name=name), self.package(["llama-server.exe", name]) as package:
                with self.assertRaises(ValueError):
                    assets.archive_members(package)

    def test_archive_raw_backslash_metadata_rejected(self):
        # Windows' stdlib ZipInfo constructor normalizes native separators
        # while reading, so exercise the raw metadata boundary explicitly.
        entry = zipfile.ZipInfo("llama-server.exe")
        entry.filename = "x\\llama-server.exe"
        package = Mock()
        package.infolist.return_value = [entry]
        with self.assertRaisesRegex(ValueError, "unsafe-archive"):
            assets.archive_members(package)

    def test_archive_casefold_duplicate_missing_server_and_symlink_rejected(self):
        for names in (["llama-server.exe", "LIB.dll", "lib.DLL"], ["other.exe"]):
            with self.package(names) as package, self.assertRaises(ValueError):
                assets.archive_members(package)
        entry = zipfile.ZipInfo("llama-server.exe")
        entry.external_attr = 0o120777 << 16
        with self.package([entry]) as package, self.assertRaises(ValueError):
            assets.archive_members(package)

    def test_archive_expanded_bound_without_allocation(self):
        entry = zipfile.ZipInfo("llama-server.exe")
        entry.file_size = assets.MAX_EXPANDED_BYTES + 1
        package = Mock()
        package.infolist.return_value = [entry]
        with self.assertRaisesRegex(ValueError, "expanded-size"):
            assets.archive_members(package)

    def tiny_cache(self, directory):
        downloads = Path(directory) / "downloads"
        downloads.mkdir()
        path = downloads / "fixture.zip"
        with zipfile.ZipFile(path, "w") as package:
            package.writestr("llama-server.exe", b"fixture server, never executed")
            package.writestr("ggml.dll", b"fixture library")
        raw = path.read_bytes()
        return {"ASSET_NAME": path.name, "ASSET_BYTES": len(raw), "ASSET_SHA256": sha256(raw).hexdigest()}

    def test_tiny_cached_archive_all_binary_hashes_verified_each_use(self):
        with tempfile.TemporaryDirectory() as directory:
            values = self.tiny_cache(directory)
            with patch.multiple(assets, **values), patch.object(assets, "_fetch") as fetch:
                prepared = assets.prepare_assets(directory)
                self.assertEqual(set(prepared["binary_sha256"]), {"llama-server.exe", "ggml.dll"})
                self.assertEqual(assets.prepare_assets(directory), prepared)
                fetch.assert_not_called()
                Path(prepared["directory"], "ggml.dll").write_bytes(b"tampered")
                with self.assertRaisesRegex(ValueError, "member-digest"):
                    assets.prepare_assets(directory)

    def test_cached_extra_binary_and_manifest_tampering_rejected(self):
        for change in ("extra", "manifest", "archive"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                values = self.tiny_cache(directory)
                with patch.multiple(assets, **values):
                    prepared = assets.prepare_assets(directory)
                    if change == "extra": Path(prepared["directory"], "injected.exe").write_bytes(b"injected")
                    if change == "manifest": Path(prepared["directory"], "cellivane-assets.json").write_bytes(b"{}")
                    if change == "archive": Path(directory, "downloads", "fixture.zip").write_bytes(b"tampered")
                    with self.assertRaises(ValueError):
                        assets.prepare_assets(directory)

    def test_missing_archive_never_downloads_without_explicit_flag(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(assets, "_fetch") as fetch:
            with self.assertRaisesRegex(ValueError, "not-prepared"):
                assets.prepare_assets(directory)
            fetch.assert_not_called()
            with self.assertRaises(ValueError):
                assets.prepare_assets(directory, download=1)


if __name__ == "__main__":
    unittest.main()
