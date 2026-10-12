"""Subengine translation fixtures; no actual tensor execution."""
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "hosted/experimental"))
from cellivane_bridge.contracts import EngineConfig, EngineResult
from cellivane_seed.core import BackendFailure, SeedEngine
from cellivane_seed.ollama import OllamaBackend


class Audit:
    def __init__(self):
        self.rows = []

    def append(self, kind, value):
        self.rows.append((kind, value))


class OllamaTranslationTests(unittest.TestCase):
    def setUp(self):
        self.config = EngineConfig("fixture", "ollama", "http://127.0.0.1:18952", "tiny:latest")
        self.audit = Audit()
        self.backend = OllamaBackend(self.config, self.audit)
        self.result = EngineResult("fixture", "ollama", "tiny:latest", "ANSWERED", "OK", .1,
            "0" * 64, "1" * 64, {"message": {"content": "OK"}}, None,
            {"prompt_eval_count": 5, "eval_count": 2})

    def test_complete_and_exact_reuse_uses_one_translated_backend_receipt(self):
        engine = SeedEngine(self.backend, model="tiny:latest", identity="fixture")
        with patch("cellivane_seed.ollama.request", return_value=self.result) as call:
            a = engine.complete({"model": "tiny:latest", "messages": [{"role": "user", "content": "hello"}]})
            b = engine.complete({"model": "tiny:latest", "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(call.call_count, 1)
        self.assertEqual(a["usage"], {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})
        self.assertEqual(b["cellivane"]["execution"], "cache")
        self.assertEqual(len(self.audit.rows), 1)

    def test_uncertain_and_missing_counts_fail_after_raw_receipt(self):
        for result in (replace(self.result, outcome="UNKNOWN"), replace(self.result, usage=None),
                replace(self.result, usage={"prompt_eval_count": True, "eval_count": 2})):
            with self.subTest(result=result), patch("cellivane_seed.ollama.request", return_value=result):
                with self.assertRaises(BackendFailure):
                    self.backend({"max_tokens": 64, "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(len(self.audit.rows), 3)

    def test_event_persistence_failure_prevents_answer(self):
        def fail(*_):
            raise OSError("fixture-audit-full")
        self.audit.append = fail
        with patch("cellivane_seed.ollama.request", return_value=self.result):
            with self.assertRaises(OSError):
                self.backend({"max_tokens": 64, "messages": [{"role": "user", "content": "hello"}]})

    def test_openai_and_strata_configs_do_not_enter_ollama_route(self):
        for kind in ("openai", "strata"):
            with self.assertRaises(ValueError):
                OllamaBackend(EngineConfig("fixture", kind, "http://127.0.0.1:18952", "tiny"), self.audit)
