"""Pure startup-policy fixtures; no engine, model, memory probe or calibration replay.

The supplied calibration is an advisory test label, not authenticated execution
evidence or a qualification of a current host's performance.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "hosted/experimental"))
from cellivane_seed.adaptive import Calibration, StartupChoice, settings_identity
from cellivane_seed.capabilities import BackendCapabilities
from cellivane_seed.resources import AdmissionPlan, AdmissionRejected, ExecutionDemand, ResourceBudget


ENGINE, TARGET, DRAFT, SETTINGS = (value * 64 for value in "abcd")


def budget(**changes):
    return ResourceBudget(**{"ram_available_bytes": 1000, "ram_headroom_bytes": 100, **changes})


def plan(mode="none", *, memory=None, demand_changes=None, capability_changes=None, observed=False):
    demand = ExecutionDemand(**{
        "engine_identity": ENGINE, "target_sha256": TARGET, "target_bytes": 400,
        "configuration_sha256": ("e" if mode == "none" else "f") * 64,
        "host_overhead_bytes": 100, "context_workspace_bytes": 200,
        "mode": mode, "draft_sha256": DRAFT if mode == "draft" else None,
        "draft_bytes": 100 if mode == "draft" else 0, **(demand_changes or {})})
    capabilities = BackendCapabilities(**{
        "backend": "llama.cpp", "engine_identity": ENGINE,
        "declared_modes": ("none", "draft"), "declared_placements": ("cpu", "hybrid", "auto"),
        **(capability_changes or {})})
    return AdmissionPlan(capabilities=capabilities, demand=demand,
                         budget=budget() if memory is None else memory, require_observed=observed)


def calibration(**changes):
    return Calibration(**{
        "engine_identity": ENGINE, "target_sha256": TARGET, "draft_sha256": DRAFT,
        "settings_sha256": SETTINGS, "report_sha256": "1" * 64, "replay_sha256": "2" * 64,
        "paired_total": 8, "paired_text_equal": 8, "median_wall_ratio": 1.05,
        "speculation_observed": True, **changes})


def choice(**changes):
    return StartupChoice(**{
        "policy": "auto", "expected_output_tokens": 128, "maximum_output_tokens": 128,
        "settings_sha256": SETTINGS, "baseline": plan(), "draft": plan("draft"),
        "calibration": calibration(), **changes})


def settings(**changes):
    return {"profile": "cpu", "threads": 4, "context": 2048, "slots": 1,
            "cache_entries": 0, "max_tokens": 128, "temperature": 0, "seed": 42,
            "no_think": True, "reasoning_format": "auto", "draft_max": 5,
            "draft_min": 0, "draft_sampling": "greedy", "cache_prompt": False, **changes}


class AdaptiveTests(unittest.TestCase):
    def assert_selected(self, candidate, mode):
        self.assertEqual(candidate.decision()[0], mode)
        selected = candidate.selected
        self.assertEqual(candidate.record()["outcome"], "BLOCKED" if mode is None else "ADMITTED")
        if mode is None:
            self.assertIsNone(selected)
        else:
            self.assertIs(selected, candidate.baseline if mode == "none" else candidate.draft)
            self.assertEqual(selected.demand.mode, mode)
            self.assertIs(selected.require_admitted(), selected)

    def test_separate_baseline_and_draft_admissions_share_target_and_budget(self):
        candidate = choice()
        self.assert_selected(candidate, "draft")
        self.assertIsNot(candidate.baseline, candidate.draft)
        self.assertEqual(candidate.baseline.budget, candidate.draft.budget)
        self.assertEqual(candidate.baseline.demand.target_sha256, candidate.draft.demand.target_sha256)
        self.assertIsNone(candidate.baseline.demand.draft_sha256)
        self.assertEqual(candidate.draft.demand.draft_sha256, DRAFT)
        self.assertEqual(candidate.baseline.ram_required_bytes, 700)
        self.assertEqual(candidate.draft.ram_required_bytes, 800)
        self.assertNotEqual(candidate.baseline.demand.configuration_sha256,
                            candidate.draft.demand.configuration_sha256)

    def test_explicit_none_does_not_promote_even_with_beneficial_calibration(self):
        for evidence in (None, calibration()):
            with self.subTest(evidence=evidence):
                candidate = choice(policy="none", calibration=evidence)
                self.assert_selected(candidate, "none")
                self.assertEqual(candidate.decision()[1], "explicit-none")

    def test_auto_short_output_boundary_is_sixty_four_inclusive(self):
        for expected, mode in ((1, "none"), (63, "none"), (64, "draft"), (128, "draft")):
            with self.subTest(expected=expected):
                self.assert_selected(choice(expected_output_tokens=expected), mode)

    def test_explicit_draft_is_admission_gated_without_historical_benefit_requirement(self):
        candidate = choice(policy="draft", expected_output_tokens=1, calibration=None)
        self.assert_selected(candidate, "draft")
        self.assertEqual(candidate.decision()[1], "explicit-draft")

    def test_auto_requires_all_benefit_conditions_together(self):
        cases = [dict(paired_total=7, paired_text_equal=7), dict(paired_text_equal=7),
                 dict(speculation_observed=False), dict(median_wall_ratio=math.nextafter(1.05, 0))]
        for changes in cases:
            with self.subTest(changes=changes):
                candidate = choice(calibration=calibration(**changes))
                self.assert_selected(candidate, "none")
                self.assertEqual(candidate.decision()[1], "calibration-benefit-not-established")
        self.assert_selected(choice(calibration=calibration(paired_total=8, paired_text_equal=8,
                                                           median_wall_ratio=1.05)), "draft")

    def test_missing_calibration_or_draft_is_a_safe_auto_fallback(self):
        for changes in ({"calibration": None}, {"draft": None}):
            with self.subTest(changes=changes):
                self.assert_selected(choice(**changes), "none")
        self.assert_selected(choice(policy="draft", draft=None), None)

    def test_each_calibration_identity_mismatch_prevents_auto_draft(self):
        for field in ("engine_identity", "target_sha256", "draft_sha256", "settings_sha256"):
            with self.subTest(field=field):
                candidate = choice(calibration=calibration(**{field: "9" * 64}))
                self.assert_selected(candidate, "none")
                self.assertEqual(candidate.decision()[1], "calibration-identity-mismatch")

    def test_one_byte_short_draft_budget_falls_back_but_explicit_draft_is_blocked(self):
        for available, mode in ((900, "draft"), (899, "none")):
            memory = budget(ram_available_bytes=available)
            candidate = choice(baseline=plan(memory=memory), draft=plan("draft", memory=memory))
            with self.subTest(available=available):
                self.assert_selected(candidate, mode)
                self.assertEqual(candidate.baseline.outcome, "ADMITTED")
                explicit = replace(candidate, policy="draft")
                self.assert_selected(explicit, "draft" if available == 900 else None)
                if available == 899:
                    self.assertEqual(candidate.draft.reason, "ram-budget-insufficient")
                    with self.assertRaises(AdmissionRejected):
                        candidate.draft.require_admitted()

    def test_unknown_draft_observation_cannot_be_promoted_by_calibration(self):
        unobserved = plan("draft", observed=True)
        self.assertEqual(unobserved.outcome, "UNKNOWN")
        self.assert_selected(choice(draft=unobserved), "none")
        self.assert_selected(choice(policy="draft", draft=unobserved), None)

    def test_unknown_or_blocked_baseline_prevents_every_policy(self):
        memory = budget(ram_available_bytes=799)
        cases = [(plan(observed=True), plan("draft")),
                 (plan(memory=memory), plan("draft", memory=memory)),
                 (plan(capability_changes={"nonstream_chat": False}), plan("draft"))]
        for baseline, draft in cases:
            for policy in ("none", "auto", "draft"):
                with self.subTest(outcome=baseline.outcome, policy=policy):
                    self.assertIn(baseline.outcome, ("UNKNOWN", "BLOCKED"))
                    candidate = choice(policy=policy, baseline=baseline, draft=draft)
                    self.assert_selected(candidate, None)
                    self.assertEqual(candidate.decision()[1], "baseline-" + baseline.outcome.lower())
                    with self.assertRaises(AdmissionRejected):
                        baseline.require_admitted()

    def test_different_target_engine_workspace_or_placement_cannot_share_a_choice(self):
        for changes in ({"engine_identity": "9" * 64}, {"target_sha256": "9" * 64},
                        {"target_bytes": 401}, {"context_tokens": 4096},
                        {"host_overhead_bytes": 101}, {"context_workspace_bytes": 201},
                        {"placement": "hybrid"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "plan-pair"):
                choice(draft=plan("draft", demand_changes=changes))

    def test_different_available_or_headroom_budget_requires_a_new_shared_snapshot(self):
        for changes in ({"ram_available_bytes": 1001}, {"ram_headroom_bytes": 101},
                        {"vram_available_bytes": 1}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "plan-pair"):
                choice(draft=plan("draft", memory=budget(**changes)))
        self.assert_selected(choice(baseline=plan(memory=budget()),
                                    draft=plan("draft", memory=budget())), "draft")

    def test_baseline_must_be_none_and_optional_draft_must_be_draft(self):
        with self.assertRaisesRegex(ValueError, "baseline-mode"):
            choice(baseline=plan("draft"))
        with self.assertRaisesRegex(ValueError, "plan-pair"):
            choice(draft=plan())

    def test_expected_and_maximum_output_are_strict_bounded_integers(self):
        for field in ("expected_output_tokens", "maximum_output_tokens"):
            for invalid in (True, False, 0, -1, 1.0, float("nan"), float("inf"), 513):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    choice(**{field: invalid})
        with self.assertRaises(ValueError):
            choice(expected_output_tokens=65, maximum_output_tokens=64)
        self.assert_selected(choice(expected_output_tokens=64, maximum_output_tokens=64), "draft")
        self.assert_selected(choice(expected_output_tokens=512, maximum_output_tokens=512), "draft")

    def test_calibration_counts_ratios_and_observation_reject_bool_nonfinite_or_invalid_bounds(self):
        cases = [{"paired_total": value} for value in (True, 0, -1, 8.0, 4097)]
        cases += [{"paired_text_equal": value} for value in (True, -1, 8.0, 9)]
        cases += [{"median_wall_ratio": value} for value in (True, 0, -1, float("nan"), float("inf"), 101)]
        cases += [{"speculation_observed": value} for value in (1, 0, None)]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                calibration(**changes)

    def test_calibration_requires_full_lowercase_digest_identities(self):
        for field in ("engine_identity", "target_sha256", "draft_sha256", "settings_sha256",
                      "report_sha256", "replay_sha256"):
            for invalid in (None, 1, "A" * 64, "f" * 63, "f" * 65):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    calibration(**{field: invalid})

    def test_unknown_policy_fields_and_wrong_contract_types_are_rejected(self):
        for changes in ({"policy": "mtp"}, {"policy": True}, {"settings_sha256": "bad"},
                        {"baseline": {}}, {"draft": {}}, {"calibration": {}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                choice(**changes)
        with self.assertRaises(TypeError):
            choice(automatic_retry=True)
        with self.assertRaises(TypeError):
            calibration(actual_backend_qualification=True)

    def test_frozen_contracts_and_detached_nested_records(self):
        candidate = choice()
        for instance, field, value in ((candidate, "policy", "draft"),
                                        (candidate.calibration, "paired_total", 100),
                                        (candidate.baseline.budget, "ram_available_bytes", 1)):
            with self.subTest(field=field), self.assertRaises(FrozenInstanceError):
                setattr(instance, field, value)
        before = candidate.sha256
        record = candidate.record()
        record["baseline"]["budget"]["ram_available_bytes"] = 1
        record["draft"]["capabilities"]["declared_modes"].clear()
        record["calibration"]["paired_text_equal"] = 0
        self.assertEqual(candidate.sha256, before)
        self.assert_selected(candidate, "draft")

    def test_object_setattr_tamper_is_revalidated_before_selection_or_hashing(self):
        for field, invalid in (("expected_output_tokens", True), ("policy", "mtp")):
            candidate = choice()
            object.__setattr__(candidate, field, invalid)
            for operation in (candidate.decision, candidate.record, lambda: candidate.sha256):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    operation()
        candidate = choice()
        object.__setattr__(candidate.calibration, "median_wall_ratio", float("nan"))
        with self.assertRaises(ValueError):
            candidate.decision()
        candidate = choice()
        object.__setattr__(candidate.draft.demand, "target_bytes", True)
        with self.assertRaises(ValueError):
            candidate.decision()

    def test_subclasses_cannot_replace_strict_calibration_or_choice_contracts(self):
        class DerivedCalibration(Calibration):
            pass
        class DerivedChoice(StartupChoice):
            pass
        values = calibration().record()
        values.pop("schema_version")
        values.pop("scope")
        with self.assertRaises(ValueError):
            DerivedCalibration(**values)
        with self.assertRaises(ValueError):
            DerivedChoice(policy="auto", expected_output_tokens=128, settings_sha256=SETTINGS,
                          baseline=plan(), draft=plan("draft"))

    def test_record_sha_is_canonical_and_binds_advisory_evidence_and_budgets(self):
        candidate = choice()
        raw = json.dumps(candidate.record(), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.assertEqual(candidate.sha256, hashlib.sha256(raw).hexdigest())
        memory = budget(ram_available_bytes=1100)
        changed = [replace(candidate, expected_output_tokens=127),
                   replace(candidate, maximum_output_tokens=129),
                   replace(candidate, settings_sha256="9" * 64),
                   replace(candidate, calibration=replace(candidate.calibration, report_sha256="9" * 64)),
                   replace(candidate, calibration=replace(candidate.calibration, replay_sha256="9" * 64)),
                   replace(candidate, baseline=plan(memory=memory), draft=plan("draft", memory=memory))]
        for changed_candidate in changed:
            with self.subTest(record=changed_candidate.record()):
                self.assertNotEqual(changed_candidate.sha256, candidate.sha256)

    def test_settings_identity_ignores_alias_and_repetition_but_binds_inference_settings(self):
        original = settings_identity(settings())
        self.assertEqual(settings_identity(settings(alias="different", repeats=99)), original)
        self.assertEqual(settings_identity(dict(reversed(list(settings().items())))), original)
        for changes in ({"threads": 8}, {"context": 4096}, {"max_tokens": 64},
                        {"reasoning_format": "none"}, {"draft_max": 6}, {"cache_prompt": True}):
            with self.subTest(changes=changes):
                self.assertNotEqual(settings_identity(settings(**changes)), original)

    def test_settings_identity_rejects_missing_fields_wrong_shape_and_nonfinite_values(self):
        missing = settings()
        del missing["context"]
        for value in (None, [], missing, settings(temperature=float("nan")),
                      settings(temperature=float("inf"))):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings_identity(value)

    def test_record_does_not_promote_advisory_policy_to_current_host_qualification(self):
        record = choice().record()
        self.assertEqual(record["scope"], "startup-selection-only")
        for field in ("per_request_switching", "automatic_retry", "current_host_speedup_guaranteed"):
            self.assertIs(record[field], False)
        self.assertIn("advisory-historical-calibration", record["calibration"]["scope"])
        for field in ("actual_backend_qualification", "measured_peak_verified", "tensor_residency_managed"):
            self.assertIs(record["draft"][field], False)


if __name__ == "__main__":
    unittest.main()
