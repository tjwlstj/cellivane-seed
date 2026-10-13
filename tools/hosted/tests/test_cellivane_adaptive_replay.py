"""Self-contained synthetic replay attacks, without runtime/producer imports.

The full fixture uses tiny, explicitly synthetic model/binary bytes. Immutable
external artifact pins are substituted only inside the test; ZIP members,
physical files, source closure, HTTP bytes, chained audit and all semantics are
still read by the real independent replay. This is not engine qualification.
"""
from __future__ import annotations

import ast
import copy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import zipfile

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
MODULE = importlib.util.spec_from_file_location("independent_adaptive", TOOLS / "verify_cellivane_adaptive.py")
v = importlib.util.module_from_spec(MODULE)
MODULE.loader.exec_module(v)
GIB = 1024**3
COUNTERS = ("backend_calls", "computed", "cache_hits", "coalesced", "failures", "rejected", "timeouts")
CONFIG = {"schema_version": 1, "alias": "cellivane-spec-target", "profile": "cpu", "threads": 4,
    "context": 2048, "slots": 1, "cache_entries": 0, "max_tokens": 128, "temperature": 0,
    "seed": 42, "no_think": {"enable_thinking": False}, "reasoning_format": "auto", "repeat": 1,
    "draft_max": 4, "draft_min": 0, "draft_sampling": "greedy", "cache_prompt": False}
SETTINGS = ("profile", "threads", "context", "slots", "cache_entries", "max_tokens", "temperature",
    "seed", "no_think", "reasoning_format", "draft_max", "draft_min", "draft_sampling", "cache_prompt")
SYSTEM = "Follow the requested output format. Give only the requested answer. /no_think"
CASES = [
    ("fixed-list", "Write exactly the numbers 1 through 20 in order, separated by commas. No title or explanation."),
    ("short-code", "Write only a Python function named square_sum(xs) that returns the sum of the squares in xs. Use exactly three short lines, no Markdown or explanation."),
    ("korean", "고양이를 처음 돌보는 사람에게 세 가지 조언을 한국어로 써 줘. 조언마다 한 문장, 모두 합쳐 80자 이내. 제목이나 맺음말 없이 세 줄만 써 줘."),
    ("copy-lines", "Copy the next sentence exactly four times, one copy per line, then stop. No title or explanation. Sentence: The quiet garden is bright today."),
]


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode("utf-8")


def digest(value):
    return sha256(encode(value)).hexdigest()


def file_record(path):
    raw = Path(path).read_bytes()
    return {"path": str(path), "bytes": len(raw), "sha256": sha256(raw).hexdigest()}


def cleanup(pid=1234):
    return {"name": "synthetic-owned-worker", "pid": pid, "method": "owned-job-terminate-and-close", "returncode": 1,
        **dict.fromkeys(("created_suspended", "assigned_before_resume", "primary_thread_closed", "process_handle_closed",
            "process_exited", "job_assigned", "job_terminated", "job_closed", "job_cleanup_confirmed"), True),
        "job_active_processes": 0, "job_error": None, "process_error": None,
        "backend_stop_api_confirmed": False, "kv_release_confirmed": False}


def exchange(body, status=200, request=None):
    return {"status": status, "body": body, "raw_response": encode(body).decode(), "request": request}


class SyntheticAssets:
    def __init__(self, root):
        self.root = Path(root)
        directory = self.root / "backend"
        downloads = self.root / "downloads"
        directory.mkdir()
        downloads.mkdir()
        members = {"llama-server.exe": b"SYNTHETIC NOT AN EXECUTABLE", "ggml.dll": b"SYNTHETIC NOT A LIBRARY",
            "synthetic-padding.dat": b""}
        archive = downloads / "llama-b11429-bin-win-cpu-x64.zip"
        with zipfile.ZipFile(archive, "w") as package:
            for name, raw in members.items():
                package.writestr(name, raw)
        # Keep the immutable reader's archive-size contract while substituting
        # a synthetic SHA. No external asset is downloaded or executed.
        members["synthetic-padding.dat"] = b"\0" * (19398918 - archive.stat().st_size)
        with zipfile.ZipFile(archive, "w") as package:
            for name, raw in members.items():
                package.writestr(name, raw)
                (directory / name).write_bytes(raw)
        assert archive.stat().st_size == 19398918
        self.archive_sha = sha256(archive.read_bytes()).hexdigest()
        hashes = {name: sha256(raw).hexdigest() for name, raw in members.items()}
        binaries = {name: value for name, value in hashes.items() if Path(name).suffix in (".exe", ".dll")}
        self.engine = digest({"backend": "llama.cpp", "commit": "d81235049384534c167caea52b85a694f6103d14",
            "archive_sha256": self.archive_sha, "binary_sha256": binaries})
        (directory / "cellivane-assets.json").write_bytes(b'{"synthetic":true}')
        help_path, version_path = self.root / "help.log", self.root / "version.log"
        help_path.write_text("--spec-type none,draft-simple --no-cache-prompt --offline --spec-draft-model --spec-draft-sampling --chat-template-kwargs", encoding="utf-8")
        version_path.write_text("version: 0.6.0-dev (build 11429, commit d81235049)", encoding="utf-8")
        server = str(directory / "llama-server.exe")
        self.record = {"schema_version": 1, "backend": "llama.cpp", "stable_release": "v0.6.0",
            "release_tag": "b11429", "source_commit": "d81235049384534c167caea52b85a694f6103d14",
            "archive_bytes": 19398918, "archive_sha256": self.archive_sha, "release_id": 403936548,
            "asset_id": 613152134, "asset_name": archive.name,
            "asset_url": "https://github.com/ggml-org/llama.cpp/releases/download/b11429/" + archive.name,
            "stable_pointer_sha256": "5677c5a4561fad44f2a58fc14187dd9cde253874584e958b37094c723ea8e8a5",
            "directory": str(directory), "server": server, "member_sha256": hashes, "binary_sha256": binaries,
            "binary_tree_sha256": digest(binaries), "engine_identity": self.engine,
            "inspection": {"help": file_record(help_path), "version": file_record(version_path),
                "commands": [[server, "--help"], [server, "--version"]], "cleanup": [cleanup(1), cleanup(2)]}}
        self.models, self.pins = {}, {}
        for kind in ("target", "draft"):
            path = self.root / (kind + ".gguf")
            path.write_bytes(b"SYNTHETIC GGUF FIXTURE " + kind.encode())
            self.models[kind] = file_record(path)
            self.pins[kind] = (self.models[kind]["bytes"], self.models[kind]["sha256"])
        self.profile_models, self.catalog = {}, {}
        for name, filename in (("0.6b", "Qwen3-0.6B-Q8_0.gguf"), ("1.7b", "Qwen3-1.7B-Q8_0.gguf"),
                ("4b", "Qwen3-4B-Q8_0.gguf")):
            path = self.root / filename
            path.write_bytes(b"SYNTHETIC NAMED PROFILE " + name.encode())
            record = file_record(path)
            self.profile_models[name] = record
            self.catalog[name] = (filename, record["bytes"], record["sha256"])

    def profile(self, name):
        filename, size, model_sha = self.catalog[name]
        return {"schema_version": 1, "name": name, "filename": filename, "bytes": size, "sha256": model_sha,
            "scope": "pinned-model-artifact-not-backend-qualification"}


class Fixture:
    def __init__(self, root, assets, mode="none", policy="none", reason="explicit-none", with_draft=True,
                 target_profile=None):
        self.root, self.assets = Path(root), assets
        snapshot = self.root / "source-snapshot"
        self.sources = {}
        for name in v.SOURCE_PATHS if target_profile is None else v.SOURCE_PATHS_V2:
            path = snapshot / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"# synthetic source closure fixture: " + name.encode())
            self.sources[name] = sha256(path.read_bytes()).hexdigest()
        models = copy.deepcopy(assets.models if with_draft else {"target": assets.models["target"]})
        if target_profile is not None:
            models = {"target": copy.deepcopy(assets.profile_models[target_profile])}
            if with_draft:
                models["draft"] = copy.deepcopy(assets.profile_models["0.6b"])
        self.summary = {"schema_version": 1 if target_profile is None else 2,
            "experiment": "cellivane-seed-adaptive", "run_id": str(uuid4()),
            "configuration": copy.deepcopy(CONFIG), "configuration_sha256": digest(CONFIG), "models": models,
            "backend_assets": copy.deepcopy(assets.record), "source_sha256": self.sources,
            "source_snapshot": str(snapshot), "calibration_validation": None, "choice": None,
            "choice_sha256": None, "phase": None, "launch_mode": "smoke", "outcome": "COMPLETED", "error": None}
        if target_profile is not None:
            self.summary["profiles"] = {"target": assets.profile(target_profile),
                "draft": assets.profile("0.6b") if with_draft else None}
        caps = {"schema_version": 1, "backend": "llama.cpp", "engine_identity": assets.engine,
            "declared_modes": ["draft", "none"], "declared_placements": ["cpu"], "observed_modes": [],
            "observed_placements": [], "evidence_sha256": None, "nonstream_chat": True,
            "observation_identity": None, "scope": "caller-declarations-and-evidence-labels", "actual_backend_qualification": False}
        budget = {"schema_version": 1, "ram_available_bytes": 16 * GIB, "ram_headroom_bytes": 4 * GIB,
            "vram_available_bytes": 0, "vram_headroom_bytes": 0, "scope": "caller-supplied-available-byte-snapshot"}

        def plan(selected):
            draft = models.get("draft") if selected == "draft" else None
            demand = {"schema_version": 1, "engine_identity": assets.engine,
                "target_sha256": models["target"]["sha256"], "target_bytes": models["target"]["bytes"],
                "draft_sha256": draft["sha256"] if draft else None, "draft_bytes": draft["bytes"] if draft else 0,
                "configuration_sha256": digest({"configuration_sha256": digest(CONFIG), "mode": selected}),
                "mode": selected, "placement": "cpu", "context_tokens": 2048, "host_overhead_bytes": 2 * GIB,
                "context_workspace_bytes": 512 * 1024**2, "gpu_required_bytes": None, "gpu_estimate_sha256": None,
                "scope": "declared-files-and-total-runtime-workspace-estimate"}
            return {"schema_version": 1, "outcome": "ADMITTED", "reason": None, "capabilities": copy.deepcopy(caps),
                "demand": demand, "budget": copy.deepcopy(budget), "require_observed": False,
                "ram_required_bytes": sum(demand[k] for k in ("target_bytes", "draft_bytes", "host_overhead_bytes", "context_workspace_bytes")),
                "vram_required_bytes": 0, "scope": "declared-budget-preflight-only", "os_memory_limit_enforced": False,
                "tensor_residency_managed": False, "measured_peak_verified": False, "actual_backend_qualification": False}
        self.summary["choice"] = {"schema_version": 1, "policy": policy, "selected_mode": mode, "outcome": "ADMITTED",
            "reason": reason, "expected_output_tokens": 128, "settings_sha256": digest({k: CONFIG[k] for k in SETTINGS}),
            "maximum_output_tokens": 128, "baseline": plan("none"), "draft": plan("draft") if with_draft else None,
            "calibration": None, "scope": "startup-selection-only", "per_request_switching": False,
            "automatic_retry": False, "current_host_speedup_guaranteed": False}
        self.summary["choice_sha256"] = digest(self.summary["choice"])
        selected = self.summary["choice"]["baseline" if mode == "none" else "draft"]
        audit = self.root / "seed-runs" / str(uuid4())
        audit.mkdir(parents=True)
        log = self.root / "worker.log"
        log.write_bytes(b"synthetic worker log; no engine executed\n")
        self.phase = {"phase_id": "adaptive-" + mode, "round": 0, "mode": mode, "identity": digest(selected),
            **{k: copy.deepcopy(selected[k]) for k in ("capabilities", "budget", "demand")}, "admission": selected,
            "memory_snapshot": {"source": "GlobalMemoryStatusEx", "available_bytes": 16 * GIB, "total_bytes": 32 * GIB,
                "scope": "host-available-physical-memory-snapshot"}, "command": self.command(mode),
            "environment": {k: "-1" for k in ("CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "GGML_VK_VISIBLE_DEVICES")},
            "audit_directory": str(audit), "warmup": None, "rows": [], "state": None, "cleanup": cleanup(),
            "ports_closed": {"18961": True, "18962": True}, "worker_log": file_record(log),
            "speculation_observed": v.spec.read_speculation(log.read_bytes()),
            "owned_memory": {"scope": "owned-process-working-set-only", "available": False},
            "source_unchanged_during_run": True, "models_unchanged_during_run": True,
            "backend_assets_unchanged_during_run": True, "status_response": None,
            "oversize_response": exchange({"error": {"message": "invalid-request"}}, 400,
                self.request("This must be rejected before backend dispatch.", 129)),
            "audit_status": None, "outcome": "COMPLETED", "error": None}
        for index, prompt in enumerate(["Reply with only OK.", *[case[1] for case in CASES]]):
            request = self.request(prompt)
            raw = {"model": CONFIG["alias"], "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "SYNTHETIC ANSWER"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10,
                    "prompt_tokens_details": {"cached_tokens": 0}}}
            receipt = {"name": "llama-cpp-tensor", "kind": "openai", "model": CONFIG["alias"], "outcome": "ANSWERED",
                "text": "SYNTHETIC ANSWER", "elapsed_s": .05, "request_body": encode(request).decode(),
                "response_body": encode(raw).decode(), "request_sha256": digest(request), "response_sha256": digest(raw),
                "raw_response": raw, "error": None, "usage": copy.deepcopy(raw["usage"]), "http_status": 200}
            row = {"response": {"request": request}, "wall_s": .1, "text": "SYNTHETIC ANSWER",
                "backend_receipt": receipt, "backend_exchange_count": 1}
            if index:
                row["case"] = CASES[index - 1][0]
                self.phase["rows"].append(row)
            else:
                self.phase["warmup"] = row
        self.summary["phase"] = self.phase
        self.path = self.root / "adaptive.json"
        self.reseal()

    def request(self, prompt, tokens=128):
        return {"model": CONFIG["alias"], "messages": [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt}], "temperature": 0, "max_tokens": tokens, "stream": False}

    def command(self, mode):
        models = self.summary["models"]
        result = [self.assets.record["server"], "--model", models["target"]["path"], "--alias", CONFIG["alias"],
            "--host", "127.0.0.1", "--port", "18962", "--threads", "4", "--threads-batch", "4", "--threads-http", "2",
            "--parallel", "1", "--ctx-size", "2048", "--batch-size", "512", "--ubatch-size", "128", "--gpu-layers", "0",
            "--fit", "off", "--temp", "0", "--seed", "42", "--n-predict", "128", "--jinja", "--reasoning", "off",
            "--reasoning-format", "auto", "--chat-template-kwargs", '{"enable_thinking":false}', "--no-cache-prompt",
            "--cache-ram", "0", "--no-cache-idle-slots", "--no-context-shift", "--offline", "--no-webui", "--metrics",
            "--log-colors", "off", "--log-verbosity", "5", "--no-log-prefix", "--no-log-timestamps", "--spec-type",
            "none" if mode == "none" else "draft-simple"]
        if mode == "draft":
            result += ["--spec-draft-model", models["draft"]["path"], "--spec-draft-threads", "4",
                "--spec-draft-threads-batch", "4", "--spec-draft-ngl", "0", "--spec-draft-n-max", "4",
                "--spec-draft-n-min", "0", "--spec-draft-sampling", "greedy"]
        return result

    def metadata(self):
        c = self.summary["choice"]
        return {"policy": c["policy"], "mode": c["selected_mode"], "reason": c["reason"],
            "expected_output_tokens": c["expected_output_tokens"], "maximum_output_tokens": 128,
            "choice_sha256": self.summary["choice_sha256"], "per_request_switching": False}

    def reseal(self):
        s, p = self.summary, self.phase
        s["choice_sha256"] = digest(s["choice"])
        counts, seq = dict.fromkeys(COUNTERS, 0), 0
        self.records = [{"kind": "adaptive_configuration", "value": {"choice_sha256": s["choice_sha256"],
            "configuration_sha256": s["configuration_sha256"], "mode": p["mode"], "admission_sha256": p["identity"]}}]

        def event(kind, key=None, value=None):
            nonlocal seq
            seq += 1
            self.records.append({"kind": "scheduler", "value": {**counts, "seq": seq, "kind": kind, "key": key,
                "execution": "computed" if kind in ("start", "finish") else None,
                "reason": {"reject": "invalid-request", "close": "engine-closed"}.get(kind),
                "response_sha256": digest(value) if value else None, "active": 1 if kind == "start" else 0,
                "waiters": 0, "cache_entries": 0, "cache_bytes": 0}})

        for row in [p["warmup"], *p["rows"]]:
            request, receipt = row["response"]["request"], row["backend_receipt"]
            receipt.update(request_body=encode(request).decode(), request_sha256=digest(request),
                response_body=encode(receipt["raw_response"]).decode(), response_sha256=digest(receipt["raw_response"]),
                usage=copy.deepcopy(receipt["raw_response"]["usage"]))
            raw = receipt["raw_response"]
            value = {"model": raw["model"], "choices": copy.deepcopy(raw["choices"]),
                "usage": {k: raw["usage"][k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")}}
            key = digest({"identity": p["identity"], "inference": request})
            counts["backend_calls"] += 1
            event("start", key)
            self.records.append({"kind": "backend_exchange", "value": copy.deepcopy(receipt)})
            counts["computed"] += 1
            event("finish", key, value)
            body = {**value, "id": str(uuid4()), "object": "chat.completion", "created": 0,
                "cellivane": {"engine": "seed", "version": "0.1.0", "execution": "computed",
                    "key": key, "backend_calls": counts["backend_calls"]}}
            row["response"] = exchange(body, 200, request)
        counts["rejected"] += 1
        event("reject")
        state = {"engine": "seed", "version": "0.1.0", "model": CONFIG["alias"], "identity": p["identity"],
            "closed": False, "poisoned": False, "slots": 1, "active": 0, "inflight": 0, "waiters": 0,
            "max_waiters": 8, "cache_entries": 0, "cache_bytes": 0, "cache_entries_limit": 0,
            "cache_bytes_limit": 1048576, **counts, "event_seq": seq, "adaptive": self.metadata()}
        if s["schema_version"] == 2:
            state["profiles"] = copy.deepcopy(s["profiles"])
        self.status_state = copy.deepcopy(state)
        event("close")
        state.update(closed=True, event_seq=seq)
        p["state"] = state
        self.records.append({"kind": "shutdown", "value": copy.deepcopy(state)})
        self.write_chain()

    def write_chain(self, update_status=True):
        previous, full = None, []
        for seq, item in enumerate(self.records, 1):
            record = {"seq": seq, "previous_sha256": previous, **item}
            raw = encode(record)
            (Path(self.phase["audit_directory"]) / f"{seq:06d}.json").write_bytes(raw)
            previous = sha256(raw).hexdigest()
            full.append(raw)
        if update_status:
            def usage(rows):
                size = sum(map(len, rows))
                return {"session_id": Path(self.phase["audit_directory"]).name, "records": len(rows), "bytes": size,
                    "record_limit": 10000, "byte_limit": 67108864, "remaining_records": 10000 - len(rows),
                    "remaining_bytes": 67108864 - size, "warning_threshold_percent": 80,
                    "near_capacity": False, "poisoned": False}
            body = {**self.status_state, "tensor_backend": "external-openai-subset", "token_mtp": False,
                "kv_sharing": False, "canonical_main_binding": False, "audit": usage(full[:17])}
            self.phase["status_response"] = exchange(body)
            self.phase["audit_status"] = usage(full)
        self.save()

    def save(self):
        self.path.write_bytes(encode(self.summary))

    def replay(self):
        self.save()
        return v.replay(self.path)


class AdaptiveReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory()
        cls.assets = SyntheticAssets(cls.shared.name)

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive_patch = patch.object(v.spec, "ARCHIVE_SHA", self.assets.archive_sha)
        self.models_patch = patch.object(v.spec, "MODEL_PINS", self.assets.pins)
        self.archive_patch.start()
        self.models_patch.start()
        self.addCleanup(self.archive_patch.stop)
        self.addCleanup(self.models_patch.stop)
        self.f = Fixture(self.temp.name, self.assets)

    def test_full_synthetic_smoke_replays_real_physical_and_semantic_readers(self):
        result = self.f.replay()
        self.assertEqual(result["outcome"], "PASS")
        self.assertEqual((result["source_file_count"], result["computed_responses"], result["audit_records"]), (18, 5, 19))
        for key in ("native_mtp", "per_request_switching", "current_host_speedup_guaranteed", "endpoint_pid_attestation"):
            self.assertIs(result[key], False)

    def test_explicit_draft_can_pass_without_claiming_observed_acceptance(self):
        f = Fixture(Path(self.temp.name) / "draft", self.assets, "draft", "draft", "explicit-draft")
        result = f.replay()
        self.assertEqual(result["selected_mode"], "draft")
        self.assertFalse(result["actual_speculation_observed"])

    def test_auto_missing_draft_is_none_and_does_not_need_calibration(self):
        f = Fixture(Path(self.temp.name) / "none", self.assets, policy="auto", reason="draft-missing", with_draft=False)
        self.assertEqual(f.replay()["reason"], "draft-missing")

    def test_short_profile_gate_is_before_missing_calibration(self):
        c = self.f.summary["choice"]
        c.update(policy="auto", expected_output_tokens=63, reason="short-output-profile")
        self.f.reseal()
        self.assertEqual(self.f.replay()["reason"], "short-output-profile")
        c.update(expected_output_tokens=64)
        self.f.reseal()
        with self.assertRaisesRegex(ValueError, "choice-independent"):
            self.f.replay()

    def test_resealed_uncalibrated_auto_draft_is_rejected(self):
        c = self.f.summary["choice"]
        c.update(policy="auto", selected_mode="draft", reason="calibrated-advisory-benefit")
        self.f.reseal()
        with self.assertRaisesRegex(ValueError, "choice-independent"):
            self.f.replay()

    def calibrated_choice(self):
        calibration = {"schema_version": 1, "engine_identity": self.assets.engine,
            "target_sha256": self.assets.models["target"]["sha256"],
            "draft_sha256": self.assets.models["draft"]["sha256"],
            "settings_sha256": digest({k: CONFIG[k] for k in SETTINGS}),
            "report_sha256": "a" * 64, "replay_sha256": "b" * 64,
            "paired_total": 8, "paired_text_equal": 8, "median_wall_ratio": 1.05,
            "speculation_observed": True,
            "scope": "advisory-historical-calibration-not-current-host-qualification"}
        self.f.summary["choice"].update(policy="auto", selected_mode="draft",
            reason="calibrated-advisory-benefit", calibration=calibration)
        return calibration

    def test_exact_eight_equal_pairs_and_1_05_ratio_are_advisory_boundary(self):
        calibration = self.calibrated_choice()
        self.f.summary["choice_sha256"] = digest(self.f.summary["choice"])
        result = v.verify_choice(self.f.summary, self.assets.engine, calibration)
        self.assertEqual(result["demand"]["mode"], "draft")

    def test_resealed_calibration_needs_eight_all_equal_observed_beneficial_pairs(self):
        original = self.calibrated_choice()
        for changes in ({"paired_total": 7, "paired_text_equal": 7}, {"paired_text_equal": 7},
                {"speculation_observed": False}, {"median_wall_ratio": 1.049999}):
            with self.subTest(changes=changes):
                calibration = {**original, **changes}
                self.f.summary["choice"]["calibration"] = calibration
                self.f.summary["choice_sha256"] = digest(self.f.summary["choice"])
                with self.assertRaisesRegex(ValueError, "choice-independent"):
                    v.verify_choice(self.f.summary, self.assets.engine, calibration)

    def test_resealed_calibration_must_match_engine_models_and_settings(self):
        original = self.calibrated_choice()
        for name in ("engine_identity", "target_sha256", "draft_sha256", "settings_sha256"):
            with self.subTest(name=name):
                calibration = {**original, name: "f" * 64}
                self.f.summary["choice"]["calibration"] = calibration
                self.f.summary["choice_sha256"] = digest(self.f.summary["choice"])
                with self.assertRaisesRegex(ValueError, "choice-independent"):
                    v.verify_choice(self.f.summary, self.assets.engine, calibration)

    def test_fresh_ram_can_block_draft_even_with_matching_good_calibration(self):
        calibration = self.calibrated_choice()
        choice = self.f.summary["choice"]
        available = choice["baseline"]["ram_required_bytes"] + 4 * GIB
        for plan in (choice["baseline"], choice["draft"]):
            plan["budget"]["ram_available_bytes"] = available
        choice["draft"].update(outcome="BLOCKED", reason="ram-budget-insufficient")
        choice.update(selected_mode="none", reason="draft-blocked")
        self.f.summary["choice_sha256"] = digest(choice)
        self.assertEqual(v.verify_choice(self.f.summary, self.assets.engine, calibration)["demand"]["mode"], "none")
        choice.update(selected_mode="draft", reason="calibrated-advisory-benefit")
        self.f.summary["choice_sha256"] = digest(choice)
        with self.assertRaisesRegex(ValueError, "choice-independent"):
            v.verify_choice(self.f.summary, self.assets.engine, calibration)

    def test_boolean_workload_and_maximum_cannot_pass_numeric_equality(self):
        for field in ("expected_output_tokens", "maximum_output_tokens"):
            with self.subTest(field=field):
                old = self.f.summary["choice"][field]
                self.f.summary["choice"][field] = True
                self.f.reseal()
                with self.assertRaises(ValueError):
                    self.f.replay()
                self.f.summary["choice"][field] = old

    def test_resealed_plan_cannot_reduce_workspace_or_headroom(self):
        for section, field in (("demand", "host_overhead_bytes"), ("budget", "ram_headroom_bytes")):
            with self.subTest(field=field):
                plan = copy.deepcopy(self.f.summary["choice"]["baseline"])
                plan[section][field] = 0
                self.f.summary["choice"]["baseline"] = plan
                self.f.reseal()
                with self.assertRaises(ValueError):
                    self.f.replay()

    def test_resealed_declared_observation_is_rejected(self):
        c = self.f.summary["choice"]["baseline"]["capabilities"]
        c.update(observed_modes=["none"], observed_placements=["cpu"], evidence_sha256="a" * 64,
            observation_identity="b" * 64)
        self.f.reseal()
        with self.assertRaisesRegex(ValueError, "capability-declaration"):
            self.f.replay()

    def test_memory_snapshot_cannot_drift_from_admitted_budget(self):
        self.f.phase["memory_snapshot"]["available_bytes"] -= 1
        with self.assertRaisesRegex(ValueError, "budget-snapshot"):
            self.f.replay()

    def test_source_snapshot_content_and_missing_family_fail(self):
        path = Path(self.f.summary["source_snapshot"]) / "hosted/experimental/cellivane_seed/adaptive.py"
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "source-content"):
            self.f.replay()
        del self.f.summary["source_sha256"]["hosted/experimental/cellivane_seed/adaptive.py"]
        with self.assertRaisesRegex(ValueError, "source-closure"):
            self.f.replay()

    def test_models_and_binary_physical_mutation_are_checked(self):
        path = Path(self.assets.models["target"]["path"])
        old = path.read_bytes()
        try:
            path.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "model-file-changed"):
                self.f.replay()
        finally:
            path.write_bytes(old)
        path = Path(self.assets.record["server"])
        old = path.read_bytes()
        try:
            path.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "backend-member-changed"):
                self.f.replay()
        finally:
            path.write_bytes(old)

    def test_unknown_backend_receipt_cannot_be_presented_as_answered(self):
        self.f.phase["rows"][0]["backend_receipt"]["outcome"] = "UNKNOWN"
        self.f.reseal()
        with self.assertRaises(ValueError):
            self.f.replay()

    def test_resealed_backend_model_or_finish_cannot_pass(self):
        for field, value in (("model", "other-model"), ("finish_reason", "length")):
            with self.subTest(field=field):
                raw = self.f.phase["rows"][0]["backend_receipt"]["raw_response"]
                if field == "model":
                    raw["model"] = value
                else:
                    raw["choices"][0][field] = value
                self.f.reseal()
                with self.assertRaises(ValueError):
                    self.f.replay()

    def test_prompt_kv_reuse_is_rejected_even_when_receipt_and_chain_resealed(self):
        self.f.phase["rows"][0]["backend_receipt"]["raw_response"]["usage"]["prompt_tokens_details"]["cached_tokens"] = 1
        self.f.reseal()
        with self.assertRaisesRegex(ValueError, "usage-or-prompt-cache"):
            self.f.replay()

    def test_raw_http_duplicate_key_cannot_hide_semantic_body(self):
        response = self.f.phase["rows"][0]["response"]
        response["raw_response"] = '{"model":"evil",' + response["raw_response"][1:]
        with self.assertRaisesRegex(ValueError, "duplicate-json-key"):
            self.f.replay()

    def test_raw_backend_wire_hash_is_not_a_normalized_response_hash(self):
        self.f.phase["rows"][0]["backend_receipt"]["response_body"] += " "
        with self.assertRaises(ValueError):
            self.f.replay()

    def test_no_extra_backend_dispatch_can_hide_behind_400(self):
        self.f.records.insert(17, {"kind": "backend_exchange", "value": copy.deepcopy(self.f.phase["warmup"]["backend_receipt"])})
        self.f.write_chain()
        with self.assertRaisesRegex(ValueError, "smoke-audit-coverage"):
            self.f.replay()

    def test_reject_key_and_counter_must_prove_invalid_admission(self):
        for field, value in (("key", "a" * 64), ("backend_calls", 6), ("rejected", True)):
            with self.subTest(field=field):
                old = self.f.records[16]["value"][field]
                self.f.records[16]["value"][field] = value
                self.f.write_chain()
                with self.assertRaisesRegex(ValueError, "scheduler-transition"):
                    self.f.replay()
                self.f.records[16]["value"][field] = old

    def test_oversize_400_must_bind_actual_129_token_request(self):
        for value in (128, True):
            self.f.phase["oversize_response"]["request"]["max_tokens"] = value
            with self.assertRaisesRegex(ValueError, "oversize-before-dispatch"):
                self.f.replay()

    def test_status_body_and_audit_byte_usage_are_independently_recomputed(self):
        status = self.f.phase["status_response"]
        status["body"]["audit"]["bytes"] += 1
        status["raw_response"] = encode(status["body"]).decode()
        with self.assertRaisesRegex(ValueError, "status-before-shutdown"):
            self.f.replay()

    def test_final_audit_capacity_claim_cannot_be_resealed(self):
        self.f.phase["audit_status"]["record_limit"] += 1
        with self.assertRaisesRegex(ValueError, "audit-committed-usage"):
            self.f.replay()

    def test_audit_chain_missing_or_noncanonical_record_fails(self):
        path = Path(self.f.phase["audit_directory"]) / "000005.json"
        raw = path.read_bytes()
        path.write_bytes(raw + b"\n")
        with self.assertRaisesRegex(ValueError, "audit-chain"):
            self.f.replay()
        path.unlink()
        with self.assertRaises(ValueError):
            self.f.replay()

    def test_close_cleanup_port_and_mutation_flags_are_not_optional(self):
        for container, key, value in ((self.f.phase["cleanup"], "job_cleanup_confirmed", False),
                (self.f.phase["ports_closed"], "18962", False), (self.f.phase, "backend_assets_unchanged_during_run", False)):
            with self.subTest(key=key):
                old = container[key]
                container[key] = value
                with self.assertRaises(ValueError):
                    self.f.replay()
                container[key] = old

    def test_fatal_after_native_success_cannot_pass(self):
        path = Path(self.f.phase["worker_log"]["path"])
        path.write_bytes(b"synthetic normal line\nerror: failed to load model\n")
        self.f.phase["worker_log"] = file_record(path)
        self.f.phase["speculation_observed"] = v.spec.read_speculation(path.read_bytes())
        with self.assertRaisesRegex(ValueError, "worker-log-recompute"):
            self.f.replay()

    def test_serve_or_planned_is_not_complete_smoke_evidence(self):
        for value in ("serve", "plan"):
            self.f.summary["launch_mode"] = value
            with self.assertRaisesRegex(ValueError, "adaptive-smoke-incomplete"):
                self.f.replay()

    def test_claims_and_unknown_fields_cannot_promote_scope(self):
        self.f.summary["choice"]["current_host_speedup_guaranteed"] = True
        self.f.reseal()
        with self.assertRaisesRegex(ValueError, "choice-independent"):
            self.f.replay()
        self.f.summary["current_host_qualified"] = True
        with self.assertRaisesRegex(ValueError, "summary-fields"):
            self.f.replay()

    def test_import_graph_excludes_runtime_and_producer(self):
        tree = ast.parse((TOOLS / "verify_cellivane_adaptive.py").read_text(encoding="utf-8"))
        imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertFalse(any(name and (name.startswith("cellivane_") or name.startswith("hosted")) for name in imported))


class ProfileReplayTests(unittest.TestCase):
    """V2 model selection is independently pinned; historical V1 stays strict."""

    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory()
        cls.assets = SyntheticAssets(cls.shared.name)

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, value in (("ARCHIVE_SHA", self.assets.archive_sha), ("MODEL_PINS", self.assets.pins)):
            context = patch.object(v.spec, name, value)
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(v, "CATALOG", self.assets.catalog)
        context.start()
        self.addCleanup(context.stop)
        self.f = Fixture(self.temp.name, self.assets, with_draft=False, target_profile="1.7b")

    def test_all_three_v2_target_profiles_have_full_physical_replay(self):
        for name in ("0.6b", "1.7b", "4b"):
            with self.subTest(name=name):
                fixture = Fixture(Path(self.temp.name) / name, self.assets, with_draft=False, target_profile=name)
                result = fixture.replay()
                self.assertEqual(result["outcome"], "PASS")
                self.assertEqual(result["source_file_count"], 19)
                self.assertEqual(result["selected_mode"], "none")

    def test_explicit_draft_for_1_7b_and_4b_has_independent_dynamic_plans(self):
        for name in ("1.7b", "4b"):
            with self.subTest(name=name):
                fixture = Fixture(Path(self.temp.name) / name, self.assets, "draft", "draft", "explicit-draft",
                    target_profile=name)
                self.assertEqual(fixture.replay()["selected_mode"], "draft")
                required = fixture.phase["admission"]["ram_required_bytes"]
                self.assertEqual(required, self.assets.catalog[name][1] + self.assets.catalog["0.6b"][1]
                    + 2 * GIB + 512 * 1024**2)

    def test_v1_remains_18_files_and_rejects_v2_profile_extension(self):
        fixture = Fixture(Path(self.temp.name) / "v1", self.assets)
        result = fixture.replay()
        self.assertEqual(result["source_file_count"], 18)
        fixture.summary["profiles"] = {"target": self.assets.profile("4b"), "draft": self.assets.profile("0.6b")}
        with self.assertRaisesRegex(ValueError, "summary-fields"):
            fixture.replay()

    def test_v2_requires_profiles_and_exact_19_file_registry_source_family(self):
        registry = "hosted/experimental/cellivane_seed/profiles.py"
        digest_value = self.f.summary["source_sha256"].pop(registry)
        with self.assertRaisesRegex(ValueError, "source-closure"):
            self.f.replay()
        self.f.summary["source_sha256"][registry] = digest_value
        del self.f.summary["profiles"]
        with self.assertRaisesRegex(ValueError, "summary-fields"):
            self.f.replay()

    def test_resealed_profile_swap_cannot_keep_old_model_record(self):
        self.f.summary["profiles"]["target"] = self.assets.profile("0.6b")
        self.f.reseal()
        with self.assertRaisesRegex(ValueError, "model-pin"):
            self.f.replay()

    def test_resealed_profile_and_model_swap_cannot_keep_old_admission(self):
        self.f.summary["profiles"]["target"] = self.assets.profile("0.6b")
        self.f.summary["models"]["target"] = copy.deepcopy(self.assets.profile_models["0.6b"])
        self.f.reseal()
        with self.assertRaisesRegex(ValueError, "demand-configuration-or-workspace"):
            self.f.replay()

    def test_profile_metadata_is_exact_and_does_not_confer_qualification(self):
        original = copy.deepcopy(self.f.summary["profiles"]["target"])
        for changes in ({"schema_version": True}, {"bytes": True}, {"sha256": "f" * 64},
                {"filename": "different.gguf"}, {"scope": "qualified-model"}, {"actual_backend_qualification": True}):
            with self.subTest(changes=changes):
                self.f.summary["profiles"]["target"] = {**original, **changes}
                self.f.reseal()
                with self.assertRaisesRegex(ValueError, "profile-catalog-binding"):
                    self.f.replay()

    def test_pinned_content_under_wrong_filename_is_rejected(self):
        old_path = Path(self.f.summary["models"]["target"]["path"])
        renamed = Path(self.temp.name) / "Qwen3-4B-Q8_0.gguf"
        renamed.write_bytes(old_path.read_bytes())
        self.f.summary["models"]["target"] = file_record(renamed)
        self.f.phase["command"][2] = str(renamed)
        self.f.reseal()
        with self.assertRaisesRegex(ValueError, "profile-path-filename"):
            self.f.replay()

    def test_only_0_6b_draft_and_distinct_target_are_supported_pairs(self):
        fixture = Fixture(Path(self.temp.name) / "same", self.assets, target_profile="0.6b")
        with self.assertRaisesRegex(ValueError, "unsupported-profile-pair"):
            fixture.replay()
        fixture = Fixture(Path(self.temp.name) / "larger-draft", self.assets, target_profile="4b")
        fixture.summary["profiles"]["draft"] = self.assets.profile("1.7b")
        fixture.summary["models"]["draft"] = copy.deepcopy(self.assets.profile_models["1.7b"])
        fixture.reseal()
        with self.assertRaisesRegex(ValueError, "unsupported-profile-pair"):
            fixture.replay()

    def test_same_model_demand_is_rejected_after_plan_and_audit_resealing(self):
        fixture = Fixture(Path(self.temp.name) / "draft", self.assets, "draft", "draft", "explicit-draft",
            target_profile="1.7b")
        plan = fixture.summary["choice"]["draft"]
        plan["demand"].update(draft_sha256=plan["demand"]["target_sha256"], draft_bytes=plan["demand"]["target_bytes"])
        plan["ram_required_bytes"] = sum(plan["demand"][name] for name in
            ("target_bytes", "draft_bytes", "host_overhead_bytes", "context_workspace_bytes"))
        fixture.phase.update(admission=plan, identity=digest(plan), demand=copy.deepcopy(plan["demand"]))
        fixture.reseal()
        with self.assertRaisesRegex(ValueError, "demand-configuration-or-workspace"):
            fixture.replay()

    def test_v2_status_and_shutdown_must_join_summary_profiles(self):
        status = self.f.phase["status_response"]
        status["body"]["profiles"]["target"] = self.assets.profile("4b")
        status["raw_response"] = encode(status["body"]).decode()
        with self.assertRaisesRegex(ValueError, "status-before-shutdown"):
            self.f.replay()
        self.f.reseal()
        self.f.phase["state"]["profiles"]["target"] = self.assets.profile("4b")
        self.f.records[-1]["value"] = copy.deepcopy(self.f.phase["state"])
        self.f.write_chain()
        with self.assertRaisesRegex(ValueError, "shutdown-state-or-counters"):
            self.f.replay()

    def test_4b_historical_calibration_does_not_qualify_1_7b_auto_draft(self):
        fixture = Fixture(Path(self.temp.name) / "calibration", self.assets, policy="auto",
            reason="calibration-identity-mismatch", target_profile="1.7b")
        report = {"backend_assets": {"engine_identity": self.assets.engine},
            "models": {"target": self.assets.profile_models["4b"], "draft": self.assets.profile_models["0.6b"]},
            "configuration": copy.deepcopy(CONFIG)}
        report_path = Path(self.temp.name) / "synthetic-historical-v1.json"
        report_path.write_bytes(encode(report))
        verdict = {"outcome": "PASS", "source_files_checked": True, "paired_total": 12,
            "paired_text_equal": 12, "median_paired_wall_speed_ratio": 1.2, "speculation_observed": True}
        validation = {"report": file_record(report_path), "source_root": self.temp.name,
            "replay": verdict, "replay_sha256": digest(verdict)}
        fixture.summary["calibration_validation"] = validation
        fixture.summary["choice"]["calibration"] = {"schema_version": 1, "engine_identity": self.assets.engine,
            "target_sha256": self.assets.profile_models["4b"]["sha256"],
            "draft_sha256": self.assets.profile_models["0.6b"]["sha256"],
            "settings_sha256": digest({name: CONFIG[name] for name in SETTINGS}),
            "report_sha256": validation["report"]["sha256"], "replay_sha256": validation["replay_sha256"],
            "paired_total": 12, "paired_text_equal": 12, "median_wall_ratio": 1.2, "speculation_observed": True,
            "scope": "advisory-historical-calibration-not-current-host-qualification"}
        fixture.reseal()
        # The old reader boundary is isolated here; the complete physical/wire
        # replay above and all adaptive identity decisions remain real readers.
        with patch.object(v.spec, "replay", return_value=verdict):
            self.assertEqual(fixture.replay()["reason"], "calibration-identity-mismatch")
            fixture.summary["choice"].update(selected_mode="draft", reason="calibrated-advisory-benefit")
            fixture.reseal()
            with self.assertRaisesRegex(ValueError, "choice-independent"):
                fixture.replay()

    def test_v1_cannot_be_relabelled_v2_without_registry_and_metadata(self):
        fixture = Fixture(Path(self.temp.name) / "v1", self.assets)
        fixture.summary["schema_version"] = 2
        fixture.summary["profiles"] = {"target": self.assets.profile("4b"), "draft": self.assets.profile("0.6b")}
        with self.assertRaisesRegex(ValueError, "source-closure"):
            fixture.replay()


class CalibrationReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.report = {"backend_assets": {"engine_identity": "a" * 64}, "models": {
            "target": {"sha256": "b" * 64}, "draft": {"sha256": "c" * 64}}, "configuration": copy.deepcopy(CONFIG)}
        self.path = root / "synthetic-historical-report.json"
        self.path.write_bytes(encode(self.report))
        self.verdict = {"outcome": "PASS", "source_files_checked": True, "paired_total": 12,
            "paired_text_equal": 12, "median_paired_wall_speed_ratio": 1.2, "speculation_observed": True}
        self.validation = {"report": file_record(self.path), "source_root": str(root),
            "replay": copy.deepcopy(self.verdict), "replay_sha256": digest(self.verdict)}

    def test_replays_independent_reader_and_binds_returned_verdict(self):
        with patch.object(v.spec, "replay", return_value=self.verdict) as replay:
            result = v.verify_calibration(self.validation)
        replay.assert_called_once_with(str(self.path), self.temp.name)
        self.assertEqual(result["paired_total"], 12)
        self.assertEqual(result["report_sha256"], self.validation["report"]["sha256"])
        self.assertIn("not-current-host", result["scope"])

    def test_resealed_supplied_pass_cannot_replace_actual_replay(self):
        self.validation["replay"]["median_paired_wall_speed_ratio"] = 9.0
        self.validation["replay_sha256"] = digest(self.validation["replay"])
        with patch.object(v.spec, "replay", return_value=self.verdict):
            with self.assertRaisesRegex(ValueError, "calibration-independent-replay"):
                v.verify_calibration(self.validation)

    def test_changed_report_raw_bytes_are_not_a_valid_saved_pass(self):
        self.path.write_bytes(self.path.read_bytes() + b" ")
        with patch.object(v.spec, "replay", return_value=self.verdict) as replay:
            with self.assertRaisesRegex(ValueError, "artifact-content-changed"):
                v.verify_calibration(self.validation)
        replay.assert_not_called()

    def test_unsourced_or_boolean_calibration_result_is_rejected(self):
        for field, value in (("source_files_checked", False), ("paired_total", True), ("median_paired_wall_speed_ratio", True)):
            with self.subTest(field=field):
                verdict = {**self.verdict, field: value}
                self.validation.update(replay=verdict, replay_sha256=digest(verdict))
                with patch.object(v.spec, "replay", return_value=verdict):
                    with self.assertRaises(ValueError):
                        v.verify_calibration(self.validation)


if __name__ == "__main__":
    unittest.main()
