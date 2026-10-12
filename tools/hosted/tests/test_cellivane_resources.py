"""Declared-byte planner tests: no allocation, model, process or hardware probes."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "hosted/experimental"))
from cellivane_seed.capabilities import BackendCapabilities
from cellivane_seed.resources import AdmissionPlan, AdmissionRejected, ExecutionDemand, ResourceBudget, plan_admission


def caps(**changes):
    values = dict(backend="llama.cpp", engine_identity="a" * 64, declared_modes=("none", "draft", "ngram"),
                  declared_placements=("cpu", "hybrid", "auto"))
    values.update(changes)
    return BackendCapabilities(**values)


def demand(**changes):
    values = dict(engine_identity="a" * 64, target_sha256="b" * 64, target_bytes=400,
                  configuration_sha256="c" * 64, host_overhead_bytes=100, context_workspace_bytes=200)
    values.update(changes)
    return ExecutionDemand(**values)


def budget(**changes):
    values = dict(ram_available_bytes=1000, ram_headroom_bytes=100)
    values.update(changes)
    return ResourceBudget(**values)


class ResourceTests(unittest.TestCase):
    def test_cpu_counts_full_target_host_workspace_and_zero_vram(self):
        plan = plan_admission(caps(), demand(), budget())
        self.assertEqual(plan.outcome, "ADMITTED")
        self.assertEqual(plan.ram_required_bytes, 700)
        self.assertEqual(plan.vram_required_bytes, 0)
        self.assertIs(plan.require_admitted(), plan)
        self.assertEqual(plan.record()["capabilities"]["observed_modes"], [])
        for field in ("os_memory_limit_enforced", "tensor_residency_managed", "measured_peak_verified", "actual_backend_qualification"):
            self.assertIs(plan.record()[field], False)

    def test_real_size_labels_count_draft_without_claiming_model_execution(self):
        d = demand(target_sha256="8c2f07f26af9747e41988551106f149b03eb9b5cb6df636027b6bf6278473300", target_bytes=4280404704,
            mode="draft", draft_sha256="9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031", draft_bytes=639446688,
            host_overhead_bytes=1 << 30, context_workspace_bytes=1 << 30)
        plan = plan_admission(caps(), d, budget(ram_available_bytes=8 << 30, ram_headroom_bytes=1 << 30))
        self.assertEqual(plan.ram_required_bytes, 7067335040)
        self.assertEqual(plan.outcome, "ADMITTED")

    def test_exact_fit_and_one_byte_short_are_distinct(self):
        fits = plan_admission(caps(), demand(), budget(ram_available_bytes=800))
        short = plan_admission(caps(), demand(), budget(ram_available_bytes=799))
        self.assertEqual(fits.outcome, "ADMITTED")
        self.assertEqual((short.outcome, short.reason), ("BLOCKED", "ram-budget-insufficient"))

    def test_ngram_has_no_draft_weight_and_draft_mode_requires_artifact(self):
        self.assertEqual(plan_admission(caps(), demand(mode="ngram"), budget()).ram_required_bytes, 700)
        for changes in ({"mode": "draft"}, {"mode": "draft", "draft_sha256": "d" * 64},
                        {"draft_sha256": "d" * 64, "draft_bytes": 10}, {"mode": "ngram", "draft_bytes": 10}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                demand(**changes)

    def test_insufficient_budget_or_unsupported_capability_prevents_backend_call(self):
        backend = Mock()
        plans = [plan_admission(caps(), demand(), budget(ram_available_bytes=600)),
            plan_admission(caps(declared_modes=("none",)), demand(mode="ngram"), budget()),
            plan_admission(caps(nonstream_chat=False), demand(), budget()),
            plan_admission(caps(declared_placements=("cpu",)), demand(placement="auto"), budget())]
        for plan in plans:
            with self.subTest(reason=plan.reason), self.assertRaises(AdmissionRejected):
                plan.require_admitted()
                backend()
        backend.assert_not_called()

    def test_gpu_without_backend_estimate_is_unknown_even_with_huge_vram(self):
        for placement in ("hybrid", "auto"):
            plan = plan_admission(caps(), demand(placement=placement), budget(vram_available_bytes=100000000000))
            self.assertEqual((plan.outcome, plan.reason), ("UNKNOWN", "backend-gpu-memory-estimate-missing"))
            self.assertIsNone(plan.vram_required_bytes)
            with self.assertRaises(AdmissionRejected):
                plan.require_admitted()

    def test_gpu_estimate_still_reserves_all_weights_in_host_ram(self):
        d = demand(placement="hybrid", gpu_required_bytes=300, gpu_estimate_sha256="d" * 64)
        plan = plan_admission(caps(), d, budget(vram_available_bytes=400, vram_headroom_bytes=100))
        self.assertEqual((plan.ram_required_bytes, plan.vram_required_bytes, plan.outcome), (700, 300, "ADMITTED"))
        self.assertEqual(plan_admission(caps(), d, budget(ram_available_bytes=500, vram_available_bytes=100000)).reason, "ram-budget-insufficient")
        self.assertEqual(plan_admission(caps(), d, budget(vram_available_bytes=399, vram_headroom_bytes=100)).reason, "vram-budget-insufficient")

    def test_gpu_estimate_requires_provenance_cpu_rejects_gpu_allocation(self):
        for changes in ({"gpu_required_bytes": 1}, {"gpu_required_bytes": False}, {"gpu_estimate_sha256": "d" * 64},
                        {"placement": "hybrid", "gpu_required_bytes": 10},
                        {"placement": "auto", "gpu_estimate_sha256": "d" * 64},
                        {"placement": "hybrid", "gpu_required_bytes": 0, "gpu_estimate_sha256": "d" * 64}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                demand(**changes)
        self.assertEqual(plan_admission(caps(), demand(gpu_required_bytes=0), budget()).vram_required_bytes, 0)

    def test_observation_requirement_stays_unknown_until_exact_demand_is_bound(self):
        d = demand()
        initial = plan_admission(caps(), d, budget(), require_observed=True)
        self.assertEqual(initial.outcome, "UNKNOWN")
        observed = caps(observed_modes=("none",), observed_placements=("cpu",), evidence_sha256="d" * 64, observation_identity=d.sha256)
        self.assertEqual(plan_admission(observed, d, budget(), require_observed=True).outcome, "ADMITTED")
        changed = replace(d, context_tokens=4096)
        stale = plan_admission(observed, changed, budget(), require_observed=True)
        self.assertEqual((stale.outcome, stale.reason), ("UNKNOWN", "capability-observation-demand-mismatch"))

    def test_model_engine_configuration_draft_and_context_bind_identity(self):
        d = demand()
        for changes in ({"target_sha256": "d" * 64}, {"engine_identity": "d" * 64},
                        {"configuration_sha256": "d" * 64}, {"context_tokens": 4096}, {"context_workspace_bytes": 201}):
            with self.subTest(changes=changes):
                self.assertNotEqual(replace(d, **changes).sha256, d.sha256)
        self.assertEqual(plan_admission(caps(), replace(d, engine_identity="d" * 64), budget()).reason, "engine-identity-mismatch")
        draft = demand(mode="draft", draft_bytes=10, draft_sha256="d" * 64)
        self.assertNotEqual(draft.sha256, replace(draft, draft_sha256="e" * 64).sha256)

    def test_available_budget_changes_plan_but_not_demand_identity(self):
        d = demand()
        first = plan_admission(caps(), d, budget())
        second = plan_admission(caps(), d, budget(ram_available_bytes=1100))
        self.assertEqual(first.demand.sha256, second.demand.sha256)
        self.assertNotEqual(first.budget.sha256, second.budget.sha256)
        self.assertNotEqual(first.sha256, second.sha256)

    def test_strict_integer_bounds_no_bool_float_nonfinite_or_overflow(self):
        for invalid in (False, -1, 1.0, float("nan"), float("inf"), 2**63):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                budget(ram_available_bytes=invalid)
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                demand(host_overhead_bytes=invalid)
        for invalid in (True, 0, -1, 1.0, 2**20 + 1):
            with self.subTest(context=invalid), self.assertRaises(ValueError):
                demand(context_tokens=invalid)
        with self.assertRaisesRegex(ValueError, "total-overflow"):
            demand(target_bytes=2**63 - 1)
        with self.assertRaisesRegex(ValueError, "headroom-exceeds"):
            budget(ram_headroom_bytes=1001)

    def test_unknown_mode_placement_fields_and_invalid_identities_rejected(self):
        for changes in ({"mode": "mtp"}, {"placement": "ssd"}, {"target_sha256": "bad"}, {"configuration_sha256": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                demand(**changes)
        with self.assertRaises(TypeError):
            demand(unsupported=True)
        with self.assertRaises(ValueError):
            plan_admission(caps(), demand(), budget(), require_observed=1)

    def test_frozen_contract_records_are_fresh_and_sha_is_canonical(self):
        plan = plan_admission(caps(), demand(), budget())
        for instance, field, value in ((plan, "require_observed", True), (plan.demand, "target_bytes", 1), (plan.budget, "ram_available_bytes", 1)):
            with self.subTest(field=field), self.assertRaises(FrozenInstanceError):
                setattr(instance, field, value)
        record = plan.record()
        record["demand"]["target_bytes"] = 1
        self.assertEqual(plan.demand.target_bytes, 400)
        raw = json.dumps(plan.record(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        self.assertEqual(plan.sha256, hashlib.sha256(raw).hexdigest())

    def test_subclass_and_mutated_objects_cannot_bypass_readmission(self):
        class Derived(ResourceBudget):
            pass
        with self.assertRaises(ValueError):
            Derived(ram_available_bytes=1000, ram_headroom_bytes=100)
        for field, invalid in (("target_bytes", True), ("mode", "mtp")):
            d = demand()
            plan = plan_admission(caps(), d, budget())
            object.__setattr__(d, field, invalid)
            with self.subTest(field=field), self.assertRaises(ValueError):
                plan.require_admitted()
        with self.assertRaises(ValueError):
            AdmissionPlan(capabilities={}, demand=demand(), budget=budget())


if __name__ == "__main__":
    unittest.main()
