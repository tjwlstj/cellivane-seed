"""Pure smoke-verdict regression fixtures; no engine or model is launched."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "hosted"))
import cellivane_seed_lab as lab


def response(execution, *, status=200, content="fixture answer"):
    return {"status": status, "body": {"model": lab.ALIAS,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            "cellivane": {"execution": execution}}}


def observations():
    # This handcrafted evidence tests classification only, not actual inference.
    result = [{"name": name, "cold": response("computed"), "warm": response("cache"),
               "backend_calls_delta": 1} for name in ("arithmetic", "control", "korean")]
    result.append({"name": "coalescing-and-overload", "responses": [response("computed"), response("coalesced")],
                   "overload": {"status": 429, "body": {"error": {"message": "compute-capacity-full"}}},
                   "backend_calls_delta": 1})
    result.append({"name": "bridge", "report": {"outcome": "COMPLETE", "dispatch_count": 2,
                   "output_allowed": True, "results": {"reference": {"outcome": "ANSWERED"},
                   "candidate": {"outcome": "ANSWERED"}}}})
    return result


class SeedLabVerdictTests(unittest.TestCase):
    def test_valid_fixture_observations_pass_mechanical_smoke_checks(self):
        self.assertEqual(lab.smoke_checks(observations()), [])

    def test_cold_or_warm_http_failure_cannot_be_hidden_by_success_body(self):
        for case in range(3):
            for phase in ("cold", "warm"):
                with self.subTest(case=case, phase=phase):
                    value = observations()
                    value[case][phase]["status"] = 503
                    self.assertIn("cold-warm-contract:" + value[case]["name"], lab.smoke_checks(value))

    def test_forged_cache_execution_or_backend_count_is_rejected(self):
        for change in ("cold-cache", "warm-computed", "backend-count"):
            with self.subTest(change=change):
                value = observations()
                if change == "backend-count":
                    value[0]["backend_calls_delta"] = 2
                else:
                    phase, execution = change.split("-")
                    value[0][phase]["body"]["cellivane"]["execution"] = execution
                self.assertIn("cold-warm-contract:arithmetic", lab.smoke_checks(value))

    def test_warm_response_must_match_computed_response(self):
        value = observations()
        value[1]["warm"]["body"]["choices"][0]["message"]["content"] = "different"
        self.assertIn("cold-warm-contract:control", lab.smoke_checks(value))

    def test_coalescing_requires_one_computation_one_join_and_one_backend_call(self):
        changes = [("execution", "computed"), ("execution", "cache"), ("count", 2), ("status", 502), ("content", "different")]
        for field, replacement in changes:
            with self.subTest(field=field, replacement=replacement):
                value = observations()
                shared = value[3]
                if field == "count":
                    shared["backend_calls_delta"] = replacement
                elif field == "status":
                    shared["responses"][1]["status"] = replacement
                elif field == "execution":
                    shared["responses"][1]["body"]["cellivane"]["execution"] = replacement
                else:
                    shared["responses"][1]["body"]["choices"][0]["message"]["content"] = replacement
                self.assertIn("coalescing-or-overload-contract", lab.smoke_checks(value))

    def test_overload_requires_429_and_does_not_accept_success_or_unavailability(self):
        for status in (200, 400, 502, 503):
            with self.subTest(status=status):
                value = observations()
                value[3]["overload"]["status"] = status
                self.assertIn("coalescing-or-overload-contract", lab.smoke_checks(value))

    def test_bridge_incomplete_cancelled_or_output_fenced_cannot_pass(self):
        cases = [("outcome", "INCOMPLETE"), ("outcome", "CANCELLED"),
                 ("output_allowed", False), ("dispatch_count", 1), ("dispatch_count", 3)]
        for key, replacement in cases:
            with self.subTest(key=key, replacement=replacement):
                value = observations()
                value[4]["report"][key] = replacement
                self.assertIn("bridge-contract", lab.smoke_checks(value))

    def test_missing_or_uncertain_bridge_child_result_cannot_pass(self):
        for outcome in (None, "UNKNOWN", "NOT_STARTED"):
            with self.subTest(outcome=outcome):
                value = observations()
                if outcome is None:
                    del value[4]["report"]["results"]["candidate"]
                else:
                    value[4]["report"]["results"]["candidate"]["outcome"] = outcome
                self.assertIn("bridge-contract", lab.smoke_checks(value))

    def test_multiple_failures_are_reported_without_promoting_fixture_to_actual_proof(self):
        value = copy.deepcopy(observations())
        value[0]["cold"]["status"] = 502
        value[3]["overload"]["status"] = 200
        value[4]["report"]["outcome"] = "INCOMPLETE"
        self.assertEqual(lab.smoke_checks(value), ["cold-warm-contract:arithmetic",
                         "coalescing-or-overload-contract", "bridge-contract"])


if __name__ == "__main__":
    unittest.main()
