"""Bounded adaptive facade and calibration-loader fixtures, without model execution.

HTTP is owned loopback only. Model byte/digest constants label pure admission
fixtures; no weights, backend binary, process, real calibration or OS RAM probe
is opened. Mocked replay exercises loader trust boundaries, not engine proof.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import copy
from dataclasses import FrozenInstanceError, replace
import hashlib
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "hosted"))
sys.path.insert(0, str(ROOT / "hosted" / "experimental"))
import cellivane_seed_adaptive as launcher
from cellivane_seed.adaptive import Calibration, StartupChoice, settings_identity
from cellivane_seed.capabilities import BackendCapabilities
from cellivane_seed.profiles import get_profile
from cellivane_seed.resources import AdmissionPlan, ExecutionDemand, ResourceBudget
from cellivane_seed.server import Audit, SeedHTTPServer


ENGINE = "a" * 64


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def declared_choice(*, available=1000, maximum=128):
    capabilities = BackendCapabilities(backend="llama.cpp", engine_identity=ENGINE,
        declared_modes=("none",), declared_placements=("cpu",))
    demand = ExecutionDemand(engine_identity=ENGINE, target_sha256="b" * 64,
        target_bytes=400, configuration_sha256="c" * 64,
        host_overhead_bytes=100, context_workspace_bytes=200)
    baseline = AdmissionPlan(capabilities=capabilities, demand=demand,
        budget=ResourceBudget(ram_available_bytes=available, ram_headroom_bytes=100))
    return StartupChoice(policy="none", expected_output_tokens=maximum,
        maximum_output_tokens=maximum, settings_sha256="d" * 64, baseline=baseline)


def completion():
    return {"model": "fixture", "choices": [{"index": 0,
        "message": {"role": "assistant", "content": "fixture answer"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}


@contextmanager
def serving(choice=None, *, target_profile=None, draft_profile=None):
    with tempfile.TemporaryDirectory(prefix="cellivane-adaptive-http-fixture-") as folder:
        audit = Audit(folder)
        backend = mock.Mock(side_effect=lambda _: completion())
        chosen = declared_choice() if choice is None else choice
        engine = launcher.BoundedSeedEngine(backend, choice=chosen,
            target_profile=target_profile, draft_profile=draft_profile, model="fixture",
            identity=chosen.selected.sha256, event_sink=lambda value: audit.append("scheduler", value))
        server = SeedHTTPServer(("127.0.0.1", 0), engine, audit)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        try:
            yield server, engine, audit, backend, chosen
        finally:
            engine.close()
            server.shutdown()
            server.server_close()
            thread.join(2)


def exchange(server, path, value=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET" if value is None else "POST", path,
            body=None if value is None else encoded(value),
            headers={"Connection": "close", **({"Content-Type": "application/json"} if value is not None else {})})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def request(tokens):
    return {"model": "fixture", "messages": [{"role": "user", "content": "fixture question"}],
            "max_tokens": tokens, "temperature": 0, "stream": False}


def pinned_inputs():
    # Reading constants is not opening or qualifying the pinned model assets.
    return {"engine_identity": ENGINE}, copy.deepcopy(launcher.spec.MODELS), launcher.spec.configuration(1, 128)


def pinned_calibration(config):
    return Calibration(engine_identity=ENGINE,
        target_sha256=launcher.spec.MODELS["target"]["sha256"],
        draft_sha256=launcher.spec.MODELS["draft"]["sha256"], settings_sha256=settings_identity(config),
        report_sha256="1" * 64, replay_sha256="2" * 64, paired_total=8,
        paired_text_equal=8, median_wall_ratio=1.2, speculation_observed=True)


def profile_models(target_name, *, draft=False):
    target = get_profile(target_name)
    result = {"target": {"bytes": target.bytes, "sha256": target.sha256}}
    if draft:
        small = get_profile("0.6b")
        result["draft"] = {"bytes": small.bytes, "sha256": small.sha256}
    return result


@contextmanager
def fake_pinned_metadata(path, profile, *, changed_after_hash=False, reparse=False, digest=None):
    """Keep a real tiny regular file; fake only its known-size/hash observations.

    The tiny fixture is not a valid GGUF. No positive artifact qualification is
    claimed by these isolated checker control-flow tests.
    """
    original_stat, original_lstat = Path.stat, Path.lstat
    hashed = False

    def metadata(original, candidate, *args, **kwargs):
        info = original(candidate, *args, **kwargs)
        if candidate != path:
            return info
        return SimpleNamespace(st_mode=info.st_mode, st_size=profile.bytes,
            st_mtime_ns=info.st_mtime_ns + (1 if changed_after_hash and hashed else 0),
            st_ino=info.st_ino, st_file_attributes=0x400 if reparse else 0)

    def file_sha(candidate):
        nonlocal hashed
        if candidate != path:
            raise AssertionError("fixture checker hashed an unexpected path")
        hashed = True
        return profile.sha256 if digest is None else digest

    with mock.patch.object(Path, "stat", lambda candidate, *args, **kwargs:
            metadata(original_stat, candidate, *args, **kwargs)), \
            mock.patch.object(Path, "lstat", lambda candidate, *args, **kwargs:
                metadata(original_lstat, candidate, *args, **kwargs)), \
            mock.patch.object(launcher.assets, "file_sha256", side_effect=file_sha) as hash_reader:
        yield hash_reader


def replay_result(**changes):
    return {"outcome": "PASS", "source_files_checked": True, "paired_total": 8,
            "paired_text_equal": 8, "median_paired_wall_speed_ratio": 1.2,
            "speculation_observed": True, "scope": "synthetic-replay-loader-fixture", **changes}


@contextmanager
def calibration_files():
    with tempfile.TemporaryDirectory(prefix="cellivane-calibration-loader-fixture-") as folder:
        base = Path(folder)
        source = base / "source-snapshot"
        source.mkdir()
        path = base / "report.json"
        prepared, models, config = pinned_inputs()
        report = {"backend_assets": prepared, "models": models, "configuration": config,
                  "fixture_note": "before",
                  # These untrusted assertions must not replace replayed results.
                  "paired_total": 4096, "paired_text_equal": 4096,
                  "median_paired_wall_speed_ratio": 99,
                  "independent_verdict": {"outcome": "PASS", "speculation_observed": False}}
        path.write_bytes(encoded(report))
        yield path, source, report


class AdaptiveFacadeTests(unittest.TestCase):
    def test_http_129_is_rejected_before_cache_lease_or_dispatch_but_128_computes(self):
        with serving() as (server, engine, audit, backend, _):
            cache_reads = []
            class CountCacheReads(OrderedDict):
                def __contains__(self, key):
                    cache_reads.append(key)
                    return super().__contains__(key)
            with mock.patch.object(engine, "_cache", CountCacheReads()):
                status, rejected = exchange(server, "/v1/chat/completions", request(129))
            self.assertEqual(cache_reads, [])
            self.assertEqual(status, 400)
            self.assertNotIn("cellivane", rejected)
            self.assertEqual(rejected["error"]["message"], "invalid-request")
            state = engine.status()
            for field in ("backend_calls", "active", "inflight", "waiters", "computed", "cache_entries", "cache_hits"):
                self.assertEqual(state[field], 0, field)
            backend.assert_not_called()
            events = [json.loads(path.read_bytes())["value"] for path in audit.directory.iterdir()]
            self.assertEqual([event["kind"] for event in events], ["reject"])
            self.assertIsNone(events[0]["key"])
            self.assertIsNone(events[0]["execution"])
            status, answered = exchange(server, "/v1/chat/completions", request(128))
            self.assertEqual(status, 200)
            self.assertEqual(answered["cellivane"]["execution"], "computed")
            self.assertEqual(answered["choices"][0]["message"]["content"], "fixture answer")
            backend.assert_called_once()
            self.assertEqual(backend.call_args.args[0]["max_tokens"], 128)
            state = engine.status()
            self.assertEqual((state["backend_calls"], state["computed"], state["active"], state["inflight"]), (1, 1, 0, 0))
            self.assertEqual(exchange(server, "/v1/chat/completions", request(129))[0], 400)
            self.assertEqual(engine.status()["cache_hits"], 0)
            backend.assert_called_once()

    def test_public_status_has_bound_adaptive_identity_and_path_free_audit(self):
        with serving() as (server, engine, audit, _, chosen):
            status, state = exchange(server, "/v1/status")
            self.assertEqual(status, 200)
            self.assertEqual(state["adaptive"], engine.status()["adaptive"])
            self.assertEqual(state["adaptive"]["choice_sha256"], chosen.sha256)
            self.assertEqual((state["adaptive"]["policy"], state["adaptive"]["mode"]), ("none", "none"))
            self.assertEqual(state["adaptive"]["maximum_output_tokens"], 128)
            self.assertIs(state["adaptive"]["per_request_switching"], False)
            self.assertEqual(state["audit"], audit.status())
            self.assertNotIn("audit_directory", state)
            self.assertNotIn(str(audit.directory.parent), json.dumps(state))
            with self.assertRaises(FrozenInstanceError):
                chosen.maximum_output_tokens = 512
            state["adaptive"]["mode"] = "draft"
            self.assertEqual(engine.status()["adaptive"]["mode"], "none")
            self.assertEqual(engine.status()["adaptive"]["choice_sha256"], chosen.sha256)

    def test_blocked_or_wrong_type_choice_cannot_construct_serving_engine(self):
        backend = mock.Mock()
        for chosen in (declared_choice(available=799), None, {}, mock.Mock(spec=StartupChoice)):
            with self.subTest(chosen=chosen), self.assertRaisesRegex(ValueError, "adaptive-start-blocked"):
                launcher.BoundedSeedEngine(backend, choice=chosen, model="fixture", identity="fixture-only")
        backend.assert_not_called()

    def test_facade_honors_immutable_choice_ceiling_below_default_core_limit(self):
        with serving(declared_choice(maximum=64)) as (server, engine, _, backend, _):
            self.assertEqual(exchange(server, "/v1/chat/completions", request(65))[0], 400)
            backend.assert_not_called()
            self.assertEqual(exchange(server, "/v1/chat/completions", request(64))[0], 200)
            backend.assert_called_once()
            self.assertEqual(engine.status()["adaptive"]["maximum_output_tokens"], 64)


class AdaptiveChooseTests(unittest.TestCase):
    def test_each_explicit_target_keeps_its_own_demand_instead_of_silently_substituting(self):
        prepared, _, config = pinned_inputs()
        for name in ("0.6b", "1.7b", "4b"):
            with self.subTest(name=name):
                target = get_profile(name)
                chosen = launcher.choose(prepared, profile_models(name), config,
                    {"available_bytes": 32 * 1024**3}, policy="auto", expected_output_tokens=128, calibration=None)
                self.assertEqual(chosen.decision()[0], "none")
                self.assertEqual((chosen.selected.demand.target_sha256, chosen.selected.demand.target_bytes),
                                 (target.sha256, target.bytes))
        blocked = launcher.choose(prepared, profile_models("4b"), config,
            {"available_bytes": 9 * 1024**3}, policy="auto", expected_output_tokens=128, calibration=None)
        self.assertIsNone(blocked.selected)
        self.assertEqual(blocked.baseline.demand.target_sha256, get_profile("4b").sha256)
        fits = launcher.choose(prepared, profile_models("1.7b"), config,
            {"available_bytes": 9 * 1024**3}, policy="auto", expected_output_tokens=128, calibration=None)
        self.assertEqual(fits.decision()[0], "none")

    def test_four_b_calibration_does_not_enable_new_one_point_seven_auto_draft(self):
        prepared, _, config = pinned_inputs()
        chosen = launcher.choose(prepared, profile_models("1.7b", draft=True), config,
            {"available_bytes": 32 * 1024**3}, policy="auto", expected_output_tokens=128,
            calibration=pinned_calibration(config))
        self.assertEqual(chosen.decision(), ("none", "calibration-identity-mismatch"))
        self.assertEqual(chosen.selected.demand.target_sha256, get_profile("1.7b").sha256)
        explicit = replace(chosen, policy="draft", calibration=None)
        self.assertEqual(explicit.decision()[0], "draft")
        self.assertEqual(explicit.selected.demand.draft_sha256, get_profile("0.6b").sha256)

    def test_pinned_file_bytes_and_explicit_workspace_are_reserved_without_opening_models(self):
        prepared, models, config = pinned_inputs()
        available = 4 * 1024**3 + models["target"]["bytes"] + models["draft"]["bytes"] + 2 * 1024**3 + 512 * 1024**2
        chosen = launcher.choose(prepared, models, config, {"available_bytes": available}, policy="auto",
            expected_output_tokens=128, calibration=pinned_calibration(config))
        self.assertEqual(chosen.decision()[0], "draft")
        self.assertEqual(chosen.baseline.budget, chosen.draft.budget)
        self.assertEqual(chosen.baseline.budget.ram_headroom_bytes, 4 * 1024**3)
        self.assertEqual(chosen.draft.ram_required_bytes, models["target"]["bytes"] + models["draft"]["bytes"]
                         + 2 * 1024**3 + 512 * 1024**2)
        self.assertEqual(chosen.draft.demand.context_tokens, 2048)
        self.assertEqual(chosen.draft.vram_required_bytes, 0)
        self.assertEqual(chosen.maximum_output_tokens, 128)
        self.assertFalse(chosen.draft.record()["measured_peak_verified"])

    def test_fresh_ram_snapshot_changes_auto_to_none_but_explicit_draft_is_blocked(self):
        prepared, models, config = pinned_inputs()
        enough = 4 * 1024**3 + models["target"]["bytes"] + models["draft"]["bytes"] + 2 * 1024**3 + 512 * 1024**2
        selected = []
        for available in (enough, enough - 1):
            chosen = launcher.choose(prepared, models, config, {"available_bytes": available}, policy="auto",
                expected_output_tokens=128, calibration=pinned_calibration(config))
            self.assertEqual(chosen.baseline.outcome, "ADMITTED")
            selected.append(chosen.decision()[0])
            if available < enough:
                self.assertEqual((chosen.draft.outcome, chosen.draft.reason), ("BLOCKED", "ram-budget-insufficient"))
                self.assertIsNone(replace(chosen, policy="draft").selected)
        self.assertEqual(selected, ["draft", "none"])

    def test_fresh_baseline_insufficient_ram_blocks_all_policies(self):
        prepared, models, config = pinned_inputs()
        minimum = 4 * 1024**3 + models["target"]["bytes"] + 2 * 1024**3 + 512 * 1024**2
        for policy in ("none", "auto", "draft"):
            for available in (minimum - 1, 1024**3):
                with self.subTest(policy=policy, available=available):
                    chosen = launcher.choose(prepared, models, config, {"available_bytes": available}, policy=policy,
                        expected_output_tokens=128, calibration=pinned_calibration(config))
                    self.assertEqual(chosen.baseline.outcome, "BLOCKED")
                    self.assertIsNone(chosen.selected)
                    self.assertEqual(chosen.record()["outcome"], "BLOCKED")
                    self.assertLessEqual(chosen.baseline.budget.ram_headroom_bytes, available)

    def test_target_only_auto_selects_none_and_explicit_draft_cannot_launch(self):
        prepared, models, config = pinned_inputs()
        del models["draft"]
        for policy, expected in (("auto", "none"), ("draft", None)):
            chosen = launcher.choose(prepared, models, config, {"available_bytes": 32 * 1024**3},
                policy=policy, expected_output_tokens=128, calibration=None)
            self.assertEqual(chosen.decision()[0], expected)
            self.assertIsNone(chosen.draft)

    def test_choose_rejects_non_integer_ram_and_over_profile_output_expectations(self):
        prepared, models, config = pinned_inputs()
        for available in (True, 1.0, float("nan"), float("inf"), -1):
            with self.subTest(available=available), self.assertRaises(ValueError):
                launcher.choose(prepared, models, config, {"available_bytes": available},
                    policy="auto", expected_output_tokens=128, calibration=None)
        with self.assertRaises(ValueError):
            launcher.choose(prepared, models, config, {"available_bytes": 32 * 1024**3},
                policy="auto", expected_output_tokens=129, calibration=None)


class AdaptiveProfileFileTests(unittest.TestCase):
    def test_checker_accepts_exact_declared_metadata_for_each_profile_without_reading_real_weights(self):
        for name in ("0.6b", "1.7b", "4b"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                profile = get_profile(name)
                path = Path(folder) / profile.filename
                path.write_bytes(b"tiny metadata-only fixture, not a GGUF")
                with fake_pinned_metadata(path, profile) as hash_reader:
                    checked = launcher.checked_model(path, profile)
                    hash_reader.assert_called_once_with(path)
                self.assertEqual(checked, {"path": str(path.resolve(strict=True)),
                                          "bytes": profile.bytes, "sha256": profile.sha256})

    def test_wrong_filename_or_size_is_rejected_before_hash_reader(self):
        profile = get_profile("1.7b")
        with tempfile.TemporaryDirectory() as folder:
            for filename in (profile.filename, get_profile("0.6b").filename):
                path = Path(folder) / filename
                path.write_bytes(b"not a model")
                with self.subTest(filename=filename), mock.patch.object(launcher.assets, "file_sha256") as hash_reader:
                    with self.assertRaisesRegex(ValueError, "model-profile-file"):
                        launcher.checked_model(path, profile)
                    hash_reader.assert_not_called()

    def test_wrong_digest_reparse_or_posthash_metadata_change_is_rejected(self):
        profile = get_profile("0.6b")
        for options, reason in (({"digest": "9" * 64}, "pin-mismatch"),
                                ({"reparse": True}, "pin-mismatch"),
                                ({"changed_after_hash": True}, "changed-during-hash")):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / profile.filename
                path.write_bytes(b"not a model")
                with fake_pinned_metadata(path, profile, **options) as hash_reader:
                    with self.assertRaisesRegex(ValueError, reason):
                        launcher.checked_model(path, profile)
                    if options.get("reparse"):
                        hash_reader.assert_not_called()

    def test_checker_requires_exact_profile_and_revalidates_forced_pin_mutation(self):
        for profile in (None, "4b", {}, True):
            with self.subTest(profile=profile), self.assertRaisesRegex(ValueError, "exact-type"):
                launcher.checked_model("unused.gguf", profile)
        profile = replace(get_profile("4b"))
        object.__setattr__(profile, "sha256", "9" * 64)
        with self.assertRaisesRegex(ValueError, "model-profile-pin"):
            launcher.checked_model("unused.gguf", profile)

    def test_model_profile_derivation_rejects_unknown_or_unsupported_pairs(self):
        for target in ("0.6b", "1.7b", "4b"):
            self.assertEqual(launcher.model_profiles(profile_models(target)), (get_profile(target), None))
        self.assertEqual(launcher.model_profiles(profile_models("1.7b", draft=True)),
                         (get_profile("1.7b"), get_profile("0.6b")))
        for models in (profile_models("0.6b", draft=True),
                       {"target": profile_models("4b")["target"], "draft": profile_models("1.7b")["target"]},
                       {"target": {"bytes": 1, "sha256": "9" * 64}}):
            with self.subTest(models=models), self.assertRaises(ValueError):
                launcher.model_profiles(models)

    def test_public_profile_status_is_demand_bound_and_path_free(self):
        prepared, _, config = pinned_inputs()
        chosen = launcher.choose(prepared, profile_models("1.7b", draft=True), config,
            {"available_bytes": 32 * 1024**3}, policy="auto", expected_output_tokens=128, calibration=None)
        target, draft = get_profile("1.7b"), get_profile("0.6b")
        with serving(chosen, target_profile=target, draft_profile=draft) as (server, _, audit, backend, _):
            status, state = exchange(server, "/v1/status")
            self.assertEqual(status, 200)
            self.assertEqual(state["profiles"], {"target": target.record(), "draft": draft.record()})
            self.assertEqual(state["adaptive"]["mode"], "none")
            self.assertNotIn(str(audit.directory.parent), json.dumps(state))
            self.assertNotIn("audit_directory", state)
            backend.assert_not_called()

    def test_profile_facade_cannot_rebind_target_or_omit_declared_draft(self):
        prepared, _, config = pinned_inputs()
        baseline = launcher.choose(prepared, profile_models("1.7b"), config,
            {"available_bytes": 32 * 1024**3}, policy="none", expected_output_tokens=128, calibration=None)
        with_draft = launcher.choose(prepared, profile_models("1.7b", draft=True), config,
            {"available_bytes": 32 * 1024**3}, policy="none", expected_output_tokens=128, calibration=None)
        backend = mock.Mock()
        for chosen, target, draft in ((baseline, get_profile("4b"), None),
                                      (baseline, get_profile("1.7b"), get_profile("0.6b")),
                                      (with_draft, get_profile("1.7b"), None),
                                      (with_draft, None, get_profile("0.6b"))):
            with self.subTest(target=target, draft=draft), self.assertRaises(ValueError):
                launcher.BoundedSeedEngine(backend, choice=chosen, target_profile=target,
                    draft_profile=draft, model="fixture", identity=chosen.selected.sha256)
        backend.assert_not_called()

    def test_profiles_extend_only_adaptive_source_closure(self):
        profile_source = "hosted/experimental/cellivane_seed/profiles.py"
        self.assertIn(profile_source, launcher.SOURCE_PATHS)
        self.assertEqual(len(launcher.SOURCE_PATHS), 19)
        self.assertNotIn(profile_source, launcher.spec.SOURCE_PATHS)


class CalibrationLoaderTests(unittest.TestCase):
    def test_calibration_inputs_are_optional_only_as_a_pair(self):
        with mock.patch.object(launcher.calibration_replay, "replay") as replay:
            self.assertEqual(launcher.load_calibration(None, None), (None, None))
            for path, source in (("report.json", None), (None, "sources")):
                with self.subTest(path=path, source=source), self.assertRaisesRegex(ValueError, "required-together"):
                    launcher.load_calibration(path, source)
            replay.assert_not_called()

    def test_replay_is_source_bound_and_overrides_untrusted_provided_verdict_values(self):
        with calibration_files() as (path, source, report):
            raw = path.read_bytes()
            verdict = replay_result()
            with mock.patch.object(launcher.calibration_replay, "replay", return_value=verdict) as replay:
                calibration, metadata = launcher.load_calibration(path, source)
            replay.assert_called_once_with(path, source)
            self.assertEqual(calibration.engine_identity, report["backend_assets"]["engine_identity"])
            self.assertEqual(calibration.target_sha256, report["models"]["target"]["sha256"])
            self.assertEqual(calibration.draft_sha256, report["models"]["draft"]["sha256"])
            self.assertEqual(calibration.settings_sha256, settings_identity(report["configuration"]))
            self.assertEqual((calibration.paired_total, calibration.paired_text_equal,
                              calibration.median_wall_ratio, calibration.speculation_observed), (8, 8, 1.2, True))
            self.assertEqual(calibration.report_sha256, hashlib.sha256(raw).hexdigest())
            expected_replay_sha = hashlib.sha256(encoded(verdict)).hexdigest()
            self.assertEqual(calibration.replay_sha256, expected_replay_sha)
            self.assertEqual(metadata["replay_sha256"], expected_replay_sha)
            self.assertEqual(metadata["replay"], verdict)
            self.assertEqual(metadata["source_root"], str(source.resolve(strict=True)))

    def test_failed_replay_or_non_exact_source_checked_flag_cannot_create_calibration(self):
        with calibration_files() as (path, source, _):
            for changes in ({"outcome": "FAIL"}, {"outcome": "pass"}, {"outcome": True},
                            {"source_files_checked": False}, {"source_files_checked": 1},
                            {"source_files_checked": None}):
                with self.subTest(changes=changes), mock.patch.object(launcher.calibration_replay, "replay",
                        return_value=replay_result(**changes)), self.assertRaisesRegex(ValueError, "not-passed"):
                    launcher.load_calibration(path, source)

    def test_independent_replay_failure_is_not_overridden_by_raw_pass_assertion(self):
        with calibration_files() as (path, source, _):
            with mock.patch.object(launcher.calibration_replay, "replay",
                    side_effect=ValueError("fixture-independent-source-mismatch")):
                with self.assertRaisesRegex(ValueError, "independent-source-mismatch"):
                    launcher.load_calibration(path, source)

    def test_report_byte_or_same_length_hash_change_during_replay_is_rejected(self):
        for same_length in (False, True):
            with self.subTest(same_length=same_length), calibration_files() as (path, source, _):
                raw = path.read_bytes()
                changed = raw.replace(b'"fixture_note":"before"', b'"fixture_note":"after!"') if same_length else raw + b"\n"
                self.assertNotEqual(changed, raw)
                if same_length:
                    self.assertEqual(len(changed), len(raw))
                def replay(_path, _source):
                    path.write_bytes(changed)
                    return replay_result()
                with mock.patch.object(launcher.calibration_replay, "replay", side_effect=replay), \
                        self.assertRaisesRegex(ValueError, "changed-during-replay"):
                    launcher.load_calibration(path, source)

    def test_replay_record_hash_is_canonical_independent_of_dict_insertion_order(self):
        with calibration_files() as (path, source, _):
            hashes = []
            for verdict in (replay_result(), dict(reversed(list(replay_result().items())))):
                with mock.patch.object(launcher.calibration_replay, "replay", return_value=verdict):
                    calibration, metadata = launcher.load_calibration(path, source)
                hashes.append((calibration.replay_sha256, metadata["replay_sha256"]))
            self.assertEqual(hashes[0], hashes[1])

    def test_invalid_replayed_counts_ratios_or_observation_are_not_trusted(self):
        with calibration_files() as (path, source, _):
            for changes in ({"paired_total": True}, {"paired_text_equal": 9},
                            {"median_paired_wall_speed_ratio": float("nan")},
                            {"median_paired_wall_speed_ratio": float("inf")}, {"speculation_observed": 1}):
                with self.subTest(changes=changes), mock.patch.object(launcher.calibration_replay, "replay",
                        return_value=replay_result(**changes)), self.assertRaises(ValueError):
                    launcher.load_calibration(path, source)

    def test_standalone_user_pass_json_is_not_a_speculation_report(self):
        with calibration_files() as (path, source, _):
            path.write_bytes(encoded(replay_result()))
            # Use the real independent parser here: a fabricated PASS object
            # has no raw experiment, source closure, audit or model bindings.
            with self.assertRaises((KeyError, ValueError)):
                launcher.load_calibration(path, source)


class AdaptivePlanCLITests(unittest.TestCase):
    def test_explicit_target_profiles_are_recorded_in_schema_two_without_downgrade(self):
        prepared, _, _ = pinned_inputs()
        for name in ("0.6b", "1.7b", "4b"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                target = get_profile(name)
                models = profile_models(name)
                with mock.patch.object(launcher, "os", SimpleNamespace(name="nt")), \
                        mock.patch.object(launcher, "source_hashes", return_value={}), \
                        mock.patch.object(launcher, "snapshot_sources"), \
                        mock.patch.object(launcher, "validate_inputs"), \
                        mock.patch.object(launcher.assets, "prepare_assets", return_value=prepared), \
                        mock.patch.object(launcher, "checked_model", return_value=models["target"]) as checker, \
                        mock.patch.object(launcher.spec, "memory_snapshot", return_value={"available_bytes": 32 * 1024**3}), \
                        mock.patch.object(launcher.lab, "OwnedProcess") as owned, \
                        mock.patch("builtins.print") as printed:
                    target_file = Path(folder) / target.filename
                    code = launcher.main(["--mode", "plan", "--target-profile", name, "--asset-root", folder,
                        "--target-file", str(target_file), "--storage-root", folder])
                self.assertEqual(code, 0)
                checker.assert_called_once_with(str(target_file), target)
                owned.assert_not_called()
                receipt = json.loads(printed.call_args.args[0])
                saved = json.loads(Path(receipt["summary"]).read_bytes())
                self.assertEqual(saved["schema_version"], 2)
                self.assertEqual(saved["profiles"], {"target": target.record(), "draft": None})
                self.assertEqual(saved["choice"]["selected_mode"], "none")
                self.assertEqual(saved["choice"]["baseline"]["demand"]["target_sha256"], target.sha256)
                self.assertEqual(saved["choice"]["baseline"]["demand"]["target_bytes"], target.bytes)

    def test_wrong_file_pin_fails_smoke_before_inspection_or_owned_process(self):
        prepared, _, _ = pinned_inputs()
        with tempfile.TemporaryDirectory() as folder:
            target = get_profile("1.7b")
            target_file = Path(folder) / target.filename
            target_file.write_bytes(b"intentionally wrong size, not a GGUF")
            with mock.patch.object(launcher, "os", SimpleNamespace(name="nt")), \
                    mock.patch.object(launcher, "source_hashes", return_value={}), \
                    mock.patch.object(launcher, "snapshot_sources"), \
                    mock.patch.object(launcher.assets, "prepare_assets", return_value=prepared), \
                    mock.patch.object(launcher.assets, "file_sha256") as hash_reader, \
                    mock.patch.object(launcher.spec, "inspect_backend") as inspection, \
                    mock.patch.object(launcher.lab, "OwnedProcess") as owned, \
                    mock.patch.object(launcher, "run_selected") as run_selected, \
                    mock.patch("builtins.print") as printed:
                code = launcher.main(["--mode", "smoke", "--target-profile", "1.7b", "--asset-root", folder,
                    "--target-file", str(target_file), "--storage-root", folder])
            self.assertEqual(code, 2)
            hash_reader.assert_not_called()
            inspection.assert_not_called()
            owned.assert_not_called()
            run_selected.assert_not_called()
            receipt = json.loads(printed.call_args.args[0])
            self.assertEqual(receipt["outcome"], "FAIL")
            self.assertIn("model-profile-file-pin-mismatch:1.7b", receipt["error"])

    def test_same_point_six_target_and_draft_fail_before_checker_or_child(self):
        prepared, _, _ = pinned_inputs()
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(launcher, "os", SimpleNamespace(name="nt")), \
                    mock.patch.object(launcher, "source_hashes", return_value={}), \
                    mock.patch.object(launcher, "snapshot_sources"), \
                    mock.patch.object(launcher.assets, "prepare_assets", return_value=prepared), \
                    mock.patch.object(launcher, "checked_model") as checker, \
                    mock.patch.object(launcher.spec, "inspect_backend") as inspection, \
                    mock.patch.object(launcher.lab, "OwnedProcess") as owned, \
                    mock.patch.object(launcher, "run_selected") as run_selected, \
                    mock.patch("builtins.print") as printed:
                code = launcher.main(["--mode", "smoke", "--target-profile", "0.6b", "--asset-root", folder,
                    "--target-file", "not-opened-target.gguf", "--draft-file", "not-opened-draft.gguf", "--storage-root", folder])
            self.assertEqual(code, 2)
            checker.assert_not_called()
            inspection.assert_not_called()
            owned.assert_not_called()
            run_selected.assert_not_called()
            self.assertIn("draft-pair-unsupported", json.loads(printed.call_args.args[0])["error"])

    def test_plan_uses_fresh_ram_and_never_dispatches_owned_process_for_admitted_or_blocked_choice(self):
        prepared, models, _ = pinned_inputs()
        for available, expected_outcome, expected_code in ((32 * 1024**3, "PLANNED", 0), (1024**3, "BLOCKED", 2)):
            with self.subTest(outcome=expected_outcome), tempfile.TemporaryDirectory() as folder:
                with mock.patch.object(launcher, "os", SimpleNamespace(name="nt")), \
                        mock.patch.object(launcher, "source_hashes", return_value={}), \
                        mock.patch.object(launcher, "snapshot_sources"), \
                        mock.patch.object(launcher, "validate_inputs"), \
                        mock.patch.object(launcher, "load_calibration", return_value=(None, None)), \
                        mock.patch.object(launcher.assets, "prepare_assets", return_value=prepared), \
                        mock.patch.object(launcher, "checked_model", return_value=models["target"]), \
                        mock.patch.object(launcher.spec, "memory_snapshot", return_value={"available_bytes": available}) as snapshot, \
                        mock.patch.object(launcher.lab, "OwnedProcess") as owned, \
                        mock.patch.object(launcher.spec, "inspect_backend") as inspect_backend, \
                        mock.patch.object(launcher, "run_selected") as run_selected, \
                        mock.patch("builtins.print") as printed:
                    # Replace only launcher's os binding, not global os.name;
                    # tempfile, Path and local_root keep native Linux semantics.
                    code = launcher.main(["--mode", "plan", "--policy", "none", "--asset-root", folder,
                        "--target-file", str(Path(folder) / "not-opened.gguf"), "--storage-root", folder])
                self.assertEqual(code, expected_code)
                snapshot.assert_called_once_with()
                owned.assert_not_called()
                inspect_backend.assert_not_called()
                run_selected.assert_not_called()
                outputs = [json.loads(call.args[0]) for call in printed.call_args_list]
                self.assertIs(outputs[0]["backend_dispatched"], False)
                self.assertEqual(outputs[-1]["outcome"], expected_outcome)
                saved = json.loads(Path(outputs[-1]["summary"]).read_bytes())
                self.assertEqual(saved["schema_version"], 2)
                self.assertEqual(saved["profiles"], {"target": get_profile("4b").record(), "draft": None})
                self.assertEqual(saved["outcome"], expected_outcome)
                self.assertEqual(saved["choice"]["baseline"]["budget"]["ram_available_bytes"], available)
                self.assertIsNone(saved["phase"])


if __name__ == "__main__":
    unittest.main()
