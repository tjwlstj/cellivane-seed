"""Pure capability contracts; supplied observation labels do not qualify engines."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "hosted/experimental"))
from cellivane_seed.capabilities import BackendCapabilities, UnknownCapability, UnsupportedCapability


def capabilities(**changes):
    values = dict(backend="llama.cpp", engine_identity="a" * 64, declared_modes=("none", "draft", "ngram"),
                  declared_placements=("cpu", "hybrid", "auto"))
    values.update(changes)
    return BackendCapabilities(**values)


class CapabilityTests(unittest.TestCase):
    def test_none_draft_ngram_are_declared_without_implied_observation(self):
        caps = capabilities()
        for mode in ("none", "draft", "ngram"):
            caps.require(mode, "cpu")
            with self.assertRaises(UnknownCapability):
                caps.require(mode, "cpu", require_observed=True)
        self.assertEqual(caps.record()["observed_modes"], [])
        self.assertIs(caps.record()["actual_backend_qualification"], False)

    def test_ollama_and_llamafile_basic_modes_are_conservative(self):
        for backend in ("ollama", "llamafile"):
            with self.subTest(backend=backend):
                caps = capabilities(backend=backend, declared_modes=("none",))
                caps.require("none", "cpu")
                with self.assertRaises(UnsupportedCapability):
                    caps.require("draft", "cpu")
                with self.assertRaises(ValueError):
                    capabilities(backend=backend, declared_modes=("none", "draft"))

    def test_unknown_backends_modes_placements_and_fields_rejected(self):
        for values in ({"backend": "mystery"}, {"declared_modes": ("mtp",)}, {"declared_placements": ("multi-gpu",)},
                       {"declared_modes": ()}, {"declared_placements": ()}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                capabilities(**values)
        with self.assertRaises(TypeError):
            capabilities(unsupported=True)
        with self.assertRaises(UnsupportedCapability):
            capabilities().require("mtp", "cpu")

    def test_false_chat_or_missing_declaration_blocks_requested_feature(self):
        with self.assertRaisesRegex(UnsupportedCapability, "nonstream-chat"):
            capabilities(nonstream_chat=False).require("none", "cpu")
        with self.assertRaisesRegex(UnsupportedCapability, "placement-unsupported"):
            capabilities(declared_placements=("cpu",)).require("draft", "auto")

    def test_observations_need_both_evidence_and_bound_demand_identity(self):
        for changes in ({"observed_modes": ("draft",)}, {"evidence_sha256": "b" * 64},
                        {"observed_modes": ("draft",), "evidence_sha256": "b" * 64},
                        {"observed_modes": ("draft",), "evidence_sha256": "b" * 64, "observation_identity": "bad"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                capabilities(**changes)
        caps = capabilities(observed_modes=("draft",), observed_placements=("cpu",), evidence_sha256="b" * 64,
                            observation_identity="c" * 64)
        caps.require("draft", "cpu", require_observed=True)
        with self.assertRaises(UnknownCapability):
            caps.require("none", "cpu", require_observed=True)

    def test_observed_feature_cannot_exceed_declarations(self):
        with self.assertRaisesRegex(ValueError, "observation-not-declared"):
            capabilities(declared_modes=("none",), observed_modes=("draft",), evidence_sha256="b" * 64, observation_identity="c" * 64)

    def test_exact_booleans_tuples_strings_and_digests_required(self):
        for changes in ({"nonstream_chat": 1}, {"declared_modes": ["none"]}, {"declared_modes": ("none", "none")},
                        {"declared_modes": (False,)}, {"engine_identity": "A" * 64}, {"engine_identity": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                capabilities(**changes)
        with self.assertRaises(ValueError):
            capabilities().require("none", "cpu", require_observed=1)

    def test_frozen_contract_and_fresh_record_lists(self):
        caps = capabilities()
        with self.assertRaises(FrozenInstanceError):
            caps.backend = "ollama"
        record = caps.record()
        record["declared_modes"].clear()
        self.assertEqual(len(caps.declared_modes), 3)

    def test_record_roundtrip_is_json_safe_and_hash_order_is_canonical(self):
        caps = capabilities()
        roundtrip = BackendCapabilities.from_record(json.loads(json.dumps(caps.record(), allow_nan=False)))
        self.assertEqual(roundtrip.sha256, caps.sha256)
        self.assertEqual(replace(caps, declared_modes=tuple(reversed(caps.declared_modes))).sha256, caps.sha256)
        raw = json.dumps(caps.record(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        self.assertEqual(caps.sha256, hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(replace(caps, engine_identity="b" * 64).sha256, caps.sha256)

    def test_record_unknown_keys_wrong_schema_or_qualification_promotion_rejected(self):
        for name, value in (("extra", True), ("schema_version", True), ("actual_backend_qualification", True),
                            ("scope", "actual-runtime-proof"), ("declared_modes", "none")):
            with self.subTest(name=name):
                record = capabilities().record()
                record[name] = value
                with self.assertRaises(ValueError):
                    BackendCapabilities.from_record(record)

    def test_subclasses_and_object_setattr_tamper_are_revalidated(self):
        class Derived(BackendCapabilities):
            pass
        with self.assertRaises(ValueError):
            Derived(backend="llama.cpp", engine_identity="a" * 64, declared_modes=("none",), declared_placements=("cpu",))
        caps = capabilities()
        object.__setattr__(caps, "nonstream_chat", 1)
        with self.assertRaises(ValueError):
            caps.record()
        with self.assertRaises(ValueError):
            caps.require("none", "cpu")


if __name__ == "__main__":
    unittest.main()
