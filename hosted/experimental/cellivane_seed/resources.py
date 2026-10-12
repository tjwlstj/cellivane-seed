"""Pure declared-budget admission, not tensor residency or OS enforcement.

Reserve full target and draft quantized-file sizes in host RAM, plus explicit
caller estimates for host overhead and total context/KV/scratch workspace.
These estimates are not measured peaks or guarantees. GPU placement requires
a separately supplied backend estimate and its provenance digest; no layer
ratio is converted into a VRAM fit assertion. The caller must recheck mutable
machine state, own the backend lifetime, and stop on a non-admitted result.
No paging, prefetch, memory allocation, OS limit, MTP or speculation executes.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re

from .capabilities import BackendCapabilities, UnknownCapability, UnsupportedCapability

__all__ = ["ResourceBudget", "ExecutionDemand", "AdmissionPlan", "AdmissionRejected", "plan_admission"]
_MAX_BYTES = 2**63 - 1


class AdmissionRejected(ValueError):
    """BLOCKED and UNKNOWN both prevent a backend call."""


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hex(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _bytes(value, name, low=0):
    if type(value) is not int or not low <= value <= _MAX_BYTES:
        raise ValueError("resource-" + name)


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceBudget:
    ram_available_bytes: int
    ram_headroom_bytes: int
    vram_available_bytes: int = 0
    vram_headroom_bytes: int = 0

    def __post_init__(self):
        ResourceBudget._validate(self)

    def _validate(self):
        if type(self) is not ResourceBudget:
            raise ValueError("resource-budget-exact-type")
        for name in ("ram_available_bytes", "ram_headroom_bytes", "vram_available_bytes", "vram_headroom_bytes"):
            _bytes(getattr(self, name), name)
        if self.ram_headroom_bytes > self.ram_available_bytes or self.vram_headroom_bytes > self.vram_available_bytes:
            raise ValueError("resource-headroom-exceeds-available")

    def record(self):
        ResourceBudget._validate(self)
        return {"schema_version": 1, "ram_available_bytes": self.ram_available_bytes,
            "ram_headroom_bytes": self.ram_headroom_bytes, "vram_available_bytes": self.vram_available_bytes,
            "vram_headroom_bytes": self.vram_headroom_bytes, "scope": "caller-supplied-available-byte-snapshot"}

    @property
    def sha256(self):
        return sha256(_encoded(ResourceBudget.record(self))).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionDemand:
    engine_identity: str
    target_sha256: str
    target_bytes: int
    configuration_sha256: str
    host_overhead_bytes: int
    context_workspace_bytes: int
    mode: str = "none"
    placement: str = "cpu"
    draft_sha256: str | None = None
    draft_bytes: int = 0
    context_tokens: int = 2048
    gpu_required_bytes: int | None = None
    gpu_estimate_sha256: str | None = None

    def __post_init__(self):
        ExecutionDemand._validate(self)

    def _validate(self):
        if type(self) is not ExecutionDemand:
            raise ValueError("resource-demand-exact-type")
        if any(not _hex(getattr(self, name)) for name in ("engine_identity", "target_sha256", "configuration_sha256")):
            raise ValueError("resource-demand-identity")
        if type(self.mode) is not str or self.mode not in ("none", "draft", "ngram"):
            raise ValueError("resource-mode")
        if type(self.placement) is not str or self.placement not in ("cpu", "hybrid", "auto"):
            raise ValueError("resource-placement")
        _bytes(self.target_bytes, "target-bytes", 1)
        for name in ("draft_bytes", "host_overhead_bytes", "context_workspace_bytes"):
            _bytes(getattr(self, name), name)
        if type(self.context_tokens) is not int or not 1 <= self.context_tokens <= 2**20:
            raise ValueError("resource-context-tokens")
        if self.mode == "draft":
            if not _hex(self.draft_sha256) or self.draft_bytes == 0:
                raise ValueError("resource-draft-required")
        elif self.draft_sha256 is not None or self.draft_bytes != 0:
            raise ValueError("resource-unexpected-draft")
        if self.target_bytes + self.draft_bytes + self.host_overhead_bytes + self.context_workspace_bytes > _MAX_BYTES:
            raise ValueError("resource-total-overflow")
        if self.placement == "cpu":
            if self.gpu_required_bytes is not None and (type(self.gpu_required_bytes) is not int or self.gpu_required_bytes != 0):
                raise ValueError("resource-cpu-vram-must-be-zero")
            if self.gpu_estimate_sha256 is not None:
                raise ValueError("resource-cpu-gpu-estimate-unexpected")
        elif self.gpu_required_bytes is None:
            if self.gpu_estimate_sha256 is not None:
                raise ValueError("resource-gpu-estimate-incomplete")
        else:
            _bytes(self.gpu_required_bytes, "gpu-required-bytes", 1 if self.placement == "hybrid" else 0)
            if not _hex(self.gpu_estimate_sha256):
                raise ValueError("resource-gpu-estimate-provenance-required")

    def record(self):
        ExecutionDemand._validate(self)
        return {"schema_version": 1, "engine_identity": self.engine_identity, "target_sha256": self.target_sha256,
            "target_bytes": self.target_bytes, "draft_sha256": self.draft_sha256, "draft_bytes": self.draft_bytes,
            "configuration_sha256": self.configuration_sha256, "mode": self.mode, "placement": self.placement,
            "context_tokens": self.context_tokens, "host_overhead_bytes": self.host_overhead_bytes,
            "context_workspace_bytes": self.context_workspace_bytes, "gpu_required_bytes": self.gpu_required_bytes,
            "gpu_estimate_sha256": self.gpu_estimate_sha256, "scope": "declared-files-and-total-runtime-workspace-estimate"}

    @property
    def sha256(self):
        return sha256(_encoded(ExecutionDemand.record(self))).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class AdmissionPlan:
    capabilities: BackendCapabilities
    demand: ExecutionDemand
    budget: ResourceBudget
    require_observed: bool = False

    def __post_init__(self):
        AdmissionPlan._validate(self)

    def _validate(self):
        if type(self) is not AdmissionPlan or type(self.capabilities) is not BackendCapabilities or type(self.demand) is not ExecutionDemand or type(self.budget) is not ResourceBudget:
            raise ValueError("resource-plan-exact-types")
        if type(self.require_observed) is not bool:
            raise ValueError("resource-require-observed")
        BackendCapabilities._validate(self.capabilities)
        ExecutionDemand._validate(self.demand)
        ResourceBudget._validate(self.budget)

    @property
    def ram_required_bytes(self):
        AdmissionPlan._validate(self)
        d = self.demand
        return d.target_bytes + d.draft_bytes + d.host_overhead_bytes + d.context_workspace_bytes

    @property
    def vram_required_bytes(self):
        AdmissionPlan._validate(self)
        return 0 if self.demand.placement == "cpu" else self.demand.gpu_required_bytes

    def _decision(self):
        AdmissionPlan._validate(self)
        if self.capabilities.engine_identity != self.demand.engine_identity:
            return "BLOCKED", "engine-identity-mismatch"
        try:
            BackendCapabilities.require(self.capabilities, self.demand.mode, self.demand.placement, require_observed=self.require_observed)
        except UnsupportedCapability as exc:
            return "BLOCKED", str(exc)
        except UnknownCapability as exc:
            return "UNKNOWN", str(exc)
        if self.require_observed and self.capabilities.observation_identity != self.demand.sha256:
            return "UNKNOWN", "capability-observation-demand-mismatch"
        if self.ram_required_bytes > self.budget.ram_available_bytes - self.budget.ram_headroom_bytes:
            return "BLOCKED", "ram-budget-insufficient"
        needed = self.vram_required_bytes
        if needed is None:
            return "UNKNOWN", "backend-gpu-memory-estimate-missing"
        if needed > self.budget.vram_available_bytes - self.budget.vram_headroom_bytes:
            return "BLOCKED", "vram-budget-insufficient"
        return "ADMITTED", None

    @property
    def outcome(self):
        return AdmissionPlan._decision(self)[0]

    @property
    def reason(self):
        return AdmissionPlan._decision(self)[1]

    def require_admitted(self):
        outcome, reason = AdmissionPlan._decision(self)
        if outcome != "ADMITTED":
            raise AdmissionRejected(outcome + ":" + reason)
        return self

    def record(self):
        outcome, reason = AdmissionPlan._decision(self)
        return {"schema_version": 1, "outcome": outcome, "reason": reason,
            "capabilities": BackendCapabilities.record(self.capabilities), "demand": ExecutionDemand.record(self.demand),
            "budget": ResourceBudget.record(self.budget), "require_observed": self.require_observed,
            "ram_required_bytes": self.ram_required_bytes,
            "vram_required_bytes": self.vram_required_bytes,
            "scope": "declared-budget-preflight-only", "os_memory_limit_enforced": False,
            "tensor_residency_managed": False, "measured_peak_verified": False, "actual_backend_qualification": False}

    @property
    def sha256(self):
        return sha256(_encoded(AdmissionPlan.record(self))).hexdigest()


def plan_admission(capabilities, demand, budget, *, require_observed=False):
    """Return a bound immutable plan; require_admitted fences UNKNOWN/BLOCKED."""
    return AdmissionPlan(capabilities=capabilities, demand=demand, budget=budget, require_observed=require_observed)
