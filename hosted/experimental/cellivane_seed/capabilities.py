"""Immutable backend declarations and separately labelled observations.

A caller supplies declarations/evidence labels; this module never probes an
engine. Neither a declaration nor an evidence digest authenticates hardware or
qualifies an engine. No token acceleration or tensor residency is implemented.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re

__all__ = ["BackendCapabilities", "UnsupportedCapability", "UnknownCapability"]

_BACKENDS = {
    "ollama": frozenset(("none",)),
    "llamafile": frozenset(("none",)),
    "llama.cpp": frozenset(("none", "draft", "ngram")),
}
_PLACEMENTS = frozenset(("cpu", "hybrid", "auto"))
_FIELDS = {"schema_version", "backend", "engine_identity", "declared_modes", "declared_placements",
           "observed_modes", "observed_placements", "evidence_sha256", "nonstream_chat", "scope",
           "observation_identity", "actual_backend_qualification"}


class UnsupportedCapability(ValueError):
    """A requested feature is outside this adapter's declared support."""


class UnknownCapability(ValueError):
    """The caller required an observation that has not been supplied."""


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _choices(value, allowed, name, *, empty=True):
    if (type(value) is not tuple or len(value) > len(allowed) or (not empty and not value)
            or any(type(item) is not str or item not in allowed for item in value)
            or len(set(value)) != len(value)):
        raise ValueError("capability-" + name)


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendCapabilities:
    backend: str
    engine_identity: str
    declared_modes: tuple[str, ...]
    declared_placements: tuple[str, ...]
    observed_modes: tuple[str, ...] = ()
    observed_placements: tuple[str, ...] = ()
    evidence_sha256: str | None = None
    observation_identity: str | None = None  # ExecutionDemand.sha256 for the observed configuration.
    nonstream_chat: bool = True

    def __post_init__(self):
        BackendCapabilities._validate(self)

    def _validate(self):
        if type(self) is not BackendCapabilities:
            raise ValueError("capability-exact-type")
        if type(self.backend) is not str or self.backend not in _BACKENDS:
            raise ValueError("capability-backend")
        if not _digest(self.engine_identity):
            raise ValueError("capability-engine-identity")
        _choices(self.declared_modes, _BACKENDS[self.backend], "declared-modes", empty=False)
        _choices(self.declared_placements, _PLACEMENTS, "declared-placements", empty=False)
        _choices(self.observed_modes, _BACKENDS[self.backend], "observed-modes")
        _choices(self.observed_placements, _PLACEMENTS, "observed-placements")
        if not set(self.observed_modes) <= set(self.declared_modes) or not set(self.observed_placements) <= set(self.declared_placements):
            raise ValueError("capability-observation-not-declared")
        has_observations = bool(self.observed_modes or self.observed_placements)
        if has_observations != (self.evidence_sha256 is not None) or (has_observations and not _digest(self.evidence_sha256)):
            raise ValueError("capability-observation-evidence")
        if has_observations != (self.observation_identity is not None) or (has_observations and not _digest(self.observation_identity)):
            raise ValueError("capability-observation-identity")
        if type(self.nonstream_chat) is not bool:
            raise ValueError("capability-nonstream-chat")

    def require(self, mode, placement, *, require_observed=False):
        BackendCapabilities._validate(self)
        if type(require_observed) is not bool:
            raise ValueError("capability-require-observed")
        if type(mode) is not str or mode not in ("none", "draft", "ngram"):
            raise UnsupportedCapability("capability-mode-unknown")
        if type(placement) is not str or placement not in _PLACEMENTS:
            raise UnsupportedCapability("capability-placement-unknown")
        if not self.nonstream_chat:
            raise UnsupportedCapability("capability-nonstream-chat-unsupported")
        if mode not in self.declared_modes:
            raise UnsupportedCapability("capability-mode-unsupported")
        if placement not in self.declared_placements:
            raise UnsupportedCapability("capability-placement-unsupported")
        if require_observed and (mode not in self.observed_modes or placement not in self.observed_placements):
            raise UnknownCapability("capability-required-observation-missing")

    def record(self):
        BackendCapabilities._validate(self)
        return {"schema_version": 1, "backend": self.backend, "engine_identity": self.engine_identity,
            "declared_modes": sorted(self.declared_modes), "declared_placements": sorted(self.declared_placements),
            "observed_modes": sorted(self.observed_modes), "observed_placements": sorted(self.observed_placements),
            "evidence_sha256": self.evidence_sha256, "nonstream_chat": self.nonstream_chat,
            "observation_identity": self.observation_identity,
            "scope": "caller-declarations-and-evidence-labels", "actual_backend_qualification": False}

    @property
    def sha256(self):
        return sha256(_encoded(BackendCapabilities.record(self))).hexdigest()

    @classmethod
    def from_record(cls, value):
        if cls is not BackendCapabilities or type(value) is not dict or set(value) != _FIELDS:
            raise ValueError("capability-record-fields")
        if (type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["scope"] != "caller-declarations-and-evidence-labels" or value["actual_backend_qualification"] is not False):
            raise ValueError("capability-record-scope")
        lists = ("declared_modes", "declared_placements", "observed_modes", "observed_placements")
        if any(type(value[name]) is not list for name in lists):
            raise ValueError("capability-record-list")
        return cls(backend=value["backend"], engine_identity=value["engine_identity"],
            nonstream_chat=value["nonstream_chat"], evidence_sha256=value["evidence_sha256"],
            observation_identity=value["observation_identity"],
            **{name: tuple(value[name]) for name in lists})
