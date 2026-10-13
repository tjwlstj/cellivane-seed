"""Pinned profile registry/pair policy only; no model files or engines are opened."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "hosted" / "experimental"))
sys.path.insert(0, str(ROOT / "tools" / "hosted"))
from cellivane_seed.profiles import ModelProfile, PROFILE_NAMES, get_profile, identify_profile, validate_pair
import cellivane_seed_assets as asset_catalog
import cellivane_seed_compare as comparison
import cellivane_seed_speculate as historical_speculation
import verify_cellivane_comparison as comparison_verifier


PINS = {
    "0.6b": ("Qwen3-0.6B-Q8_0.gguf", 639446688,
             "9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031"),
    "1.7b": ("Qwen3-1.7B-Q8_0.gguf", 1834426016,
             "061b54daade076b5d3362dac252678d17da8c68f07560be70818cace6590cb1a"),
    "4b": ("Qwen3-4B-Q8_0.gguf", 4280404704,
           "8c2f07f26af9747e41988551106f149b03eb9b5cb6df636027b6bf6278473300"),
}


class ProfileTests(unittest.TestCase):
    def test_profile_names_are_an_immutable_explicit_allowlist(self):
        self.assertIs(type(PROFILE_NAMES), tuple)
        self.assertEqual(set(PROFILE_NAMES), set(PINS))

    def test_exact_named_pins_are_shared_with_existing_asset_and_comparison_catalogs(self):
        for name, expected in PINS.items():
            with self.subTest(name=name):
                profile = get_profile(name)
                self.assertIs(type(profile), ModelProfile)
                self.assertEqual(profile.name, name)
                self.assertEqual((profile.filename, profile.bytes, profile.sha256), expected)
                asset = asset_catalog.CATALOG[name]
                self.assertEqual((asset["filename"], asset["size"], asset["sha256"]), expected)
                self.assertEqual(comparison.MODELS[name], expected)
                self.assertEqual(comparison_verifier.MODELS[name], expected[1:])

    def test_identification_requires_matching_size_and_digest_together(self):
        for name, (_filename, size, digest) in PINS.items():
            with self.subTest(name=name):
                self.assertEqual(identify_profile(size, digest), get_profile(name))
        for size, digest in ((True, PINS["0.6b"][2]), (PINS["1.7b"][1], PINS["4b"][2]),
                             (PINS["4b"][1] + 1, PINS["4b"][2]), (PINS["4b"][1], "a" * 64),
                             (float("nan"), PINS["4b"][2])):
            with self.subTest(size=size, digest=digest), self.assertRaises(ValueError):
                identify_profile(size, digest)

    def test_well_typed_but_rebound_artifact_fields_do_not_create_arbitrary_profiles(self):
        original = get_profile("4b")
        for changes in ({"bytes": original.bytes + 1}, {"sha256": "9" * 64},
                        {"filename": PINS["1.7b"][0]}, {"name": "1.7b"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(original, **changes)

    def test_existing_speculation_v1_target_and_draft_pins_are_preserved(self):
        for role, name in (("target", "4b"), ("draft", "0.6b")):
            with self.subTest(role=role):
                profile = get_profile(name)
                old = historical_speculation.MODELS[role]
                self.assertEqual((profile.bytes, profile.sha256), (old["bytes"], old["sha256"]))

    def test_names_are_exact_not_aliases_parameter_guesses_or_boolean_indices(self):
        for name in (None, True, False, 0, 1.7, "", "4B", "1.7B", "qwen3", "0.6", "7b", "../4b"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                get_profile(name)

    def test_profile_is_frozen_and_record_mutation_does_not_rewrite_registry_pin(self):
        profile = get_profile("1.7b")
        for field, value in (("name", "4b"), ("filename", "other.gguf"),
                             ("bytes", 1), ("sha256", "a" * 64)):
            with self.subTest(field=field), self.assertRaises(FrozenInstanceError):
                setattr(profile, field, value)
        record = profile.record()
        self.assertEqual(record["scope"], "pinned-model-artifact-not-backend-qualification")
        for field in ("name", "filename", "bytes", "sha256"):
            self.assertEqual(record[field], getattr(profile, field))
        record["bytes"] = 1
        self.assertEqual(get_profile("1.7b").bytes, PINS["1.7b"][1])
        self.assertEqual(profile.record()["bytes"], PINS["1.7b"][1])

    def test_profile_fields_reject_boolean_nonfinite_and_invalid_artifact_labels(self):
        original = get_profile("4b")
        cases = [{"bytes": value} for value in (True, False, 0, -1, 1.0, float("nan"), float("inf"), 2**63)]
        cases += [{"sha256": value} for value in (None, "A" * 64, "a" * 63, "a" * 65)]
        cases += [{"filename": value} for value in (None, "../model.gguf", "folder/model.gguf", "folder\\model.gguf")]
        cases += [{"name": value} for value in (None, True, "unknown")]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(original, **changes)
        with self.assertRaises(TypeError):
            ModelProfile(name="4b", filename=original.filename, bytes=original.bytes,
                         sha256=original.sha256, arbitrary_model_allowed=True)

    def test_target_only_is_supported_for_every_known_profile(self):
        for name in PINS:
            with self.subTest(name=name):
                validate_pair(get_profile(name), None)

    def test_only_fixed_point_six_draft_below_one_point_seven_or_four_is_supported(self):
        allowed = {("1.7b", "0.6b"), ("4b", "0.6b")}
        for target in PINS:
            for draft in PINS:
                with self.subTest(target=target, draft=draft):
                    if (target, draft) in allowed:
                        validate_pair(get_profile(target), get_profile(draft))
                    else:
                        with self.assertRaises(ValueError):
                            validate_pair(get_profile(target), get_profile(draft))

    def test_pair_rejects_strings_dicts_or_missing_target_instead_of_inferring_a_profile(self):
        for target, draft in ((None, None), ("4b", None), ({"name": "4b"}, None),
                              (get_profile("4b"), "0.6b"), (get_profile("4b"), {}),
                              (True, get_profile("0.6b"))):
            with self.subTest(target=target, draft=draft), self.assertRaises(ValueError):
                validate_pair(target, draft)

    def test_forced_mutation_is_revalidated_without_corrupting_the_registry(self):
        for field, invalid in (("bytes", True), ("sha256", "bad"), ("name", "unknown")):
            profile = replace(get_profile("4b"))
            object.__setattr__(profile, field, invalid)
            with self.subTest(field=field), self.assertRaises(ValueError):
                profile.record()
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_pair(profile, get_profile("0.6b"))
        self.assertEqual(get_profile("4b").bytes, PINS["4b"][1])


if __name__ == "__main__":
    unittest.main()
