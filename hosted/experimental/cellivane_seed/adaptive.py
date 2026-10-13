"""Choose a startup mode from declared budgets and advisory calibration.

This policy never starts an engine, changes model quality, authenticates a
calibration, or interrupts a request. A launcher must independently replay its
calibration, refresh available memory, and enforce the selected admission plan.
The pinned backend selects speculation at startup, not per request.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import re

from .resources import AdmissionPlan


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def _hex(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def settings_identity(config):
    """Hash inference settings, excluding experiment repetition and alias."""
    fields = ("profile", "threads", "context", "slots", "cache_entries", "max_tokens",
              "temperature", "seed", "no_think", "reasoning_format", "draft_max",
              "draft_min", "draft_sampling", "cache_prompt")
    if type(config) is not dict or any(k not in config for k in fields):
        raise ValueError("adaptive-settings")
    return _digest({k: config[k] for k in fields})


@dataclass(frozen=True, slots=True, kw_only=True)
class Calibration:
    engine_identity: str
    target_sha256: str
    draft_sha256: str
    settings_sha256: str
    report_sha256: str
    replay_sha256: str
    paired_total: int
    paired_text_equal: int
    median_wall_ratio: float
    speculation_observed: bool

    def __post_init__(self):
        self.validate()

    def validate(self):
        if type(self) is not Calibration:
            raise ValueError("adaptive-calibration-type")
        for name in ("engine_identity", "target_sha256", "draft_sha256", "settings_sha256",
                     "report_sha256", "replay_sha256"):
            if not _hex(getattr(self, name)):
                raise ValueError("adaptive-calibration-identity")
        if (type(self.paired_total) is not int or not 1 <= self.paired_total <= 4096
                or type(self.paired_text_equal) is not int
                or not 0 <= self.paired_text_equal <= self.paired_total):
            raise ValueError("adaptive-calibration-pairs")
        if (type(self.median_wall_ratio) not in (int, float)
                or not math.isfinite(self.median_wall_ratio) or not 0 < self.median_wall_ratio <= 100):
            raise ValueError("adaptive-calibration-ratio")
        if type(self.speculation_observed) is not bool:
            raise ValueError("adaptive-calibration-observation")

    def record(self):
        self.validate()
        return {"schema_version": 1, **{k: getattr(self, k) for k in self.__slots__},
                "scope": "advisory-historical-calibration-not-current-host-qualification"}


@dataclass(frozen=True, slots=True, kw_only=True)
class StartupChoice:
    policy: str
    expected_output_tokens: int
    settings_sha256: str
    baseline: AdmissionPlan
    maximum_output_tokens: int = 128
    draft: AdmissionPlan | None = None
    calibration: Calibration | None = None

    def __post_init__(self):
        self.validate()

    def validate(self):
        if (type(self) is not StartupChoice or type(self.policy) is not str
                or self.policy not in ("auto", "none", "draft")):
            raise ValueError("adaptive-policy")
        if (type(self.maximum_output_tokens) is not int or not 1 <= self.maximum_output_tokens <= 512
                or type(self.expected_output_tokens) is not int
                or not 1 <= self.expected_output_tokens <= self.maximum_output_tokens
                or not _hex(self.settings_sha256) or type(self.baseline) is not AdmissionPlan):
            raise ValueError("adaptive-input")
        self.baseline._validate()
        if self.baseline.demand.mode != "none":
            raise ValueError("adaptive-baseline-mode")
        if self.draft is not None:
            if type(self.draft) is not AdmissionPlan:
                raise ValueError("adaptive-draft-type")
            self.draft._validate()
            a, b = self.baseline.demand, self.draft.demand
            if (b.mode != "draft" or any(getattr(a, k) != getattr(b, k) for k in (
                    "engine_identity", "target_sha256", "target_bytes", "placement", "context_tokens",
                    "host_overhead_bytes", "context_workspace_bytes"))
                    or self.baseline.budget != self.draft.budget):
                raise ValueError("adaptive-plan-pair")
        if self.calibration is not None:
            if type(self.calibration) is not Calibration:
                raise ValueError("adaptive-calibration-type")
            self.calibration.validate()

    def decision(self):
        self.validate()
        if self.baseline.outcome != "ADMITTED":
            return None, "baseline-" + self.baseline.outcome.lower()
        if self.policy == "none":
            return "none", "explicit-none"
        if self.draft is None or self.draft.outcome != "ADMITTED":
            reason = "draft-missing" if self.draft is None else "draft-" + self.draft.outcome.lower()
            return (None if self.policy == "draft" else "none"), reason
        if self.policy == "draft":
            return "draft", "explicit-draft"
        if self.expected_output_tokens < 64:
            return "none", "short-output-profile"
        c, d = self.calibration, self.draft.demand
        if c is None:
            return "none", "calibration-missing"
        if ((c.engine_identity, c.target_sha256, c.draft_sha256, c.settings_sha256) != (
                d.engine_identity, d.target_sha256, d.draft_sha256, self.settings_sha256)):
            return "none", "calibration-identity-mismatch"
        if (c.paired_total < 8 or c.paired_text_equal != c.paired_total
                or not c.speculation_observed or c.median_wall_ratio < 1.05):
            return "none", "calibration-benefit-not-established"
        return "draft", "calibrated-advisory-benefit"

    @property
    def selected(self):
        mode, _reason = self.decision()
        return self.baseline if mode == "none" else self.draft if mode == "draft" else None

    def record(self):
        mode, reason = self.decision()
        return {"schema_version": 1, "policy": self.policy, "selected_mode": mode,
                "outcome": "ADMITTED" if mode is not None else "BLOCKED", "reason": reason,
                "expected_output_tokens": self.expected_output_tokens, "settings_sha256": self.settings_sha256,
                "maximum_output_tokens": self.maximum_output_tokens,
                "baseline": self.baseline.record(), "draft": self.draft.record() if self.draft else None,
                "calibration": self.calibration.record() if self.calibration else None,
                "scope": "startup-selection-only", "per_request_switching": False,
                "automatic_retry": False, "current_host_speedup_guaranteed": False}

    @property
    def sha256(self):
        return _digest(self.record())
