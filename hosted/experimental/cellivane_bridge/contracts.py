"""Bounded application contracts, independent of the public AIOS wire schemas."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from urllib.parse import urlsplit


def endpoint(value: str) -> tuple[str, int, str]:
    if type(value) is not str or len(value) > 256:
        raise ValueError("endpoint")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("endpoint") from exc
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username is not None or parsed.password is not None
            or port is None or not 1 <= port <= 65535
            or parsed.query or parsed.fragment or parsed.path not in ("", "/v1")):
        raise ValueError("endpoint")
    return "127.0.0.1", port, parsed.path


def number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


@dataclass(frozen=True)
class EngineConfig:
    name: str
    kind: str
    base_url: str
    model: str
    role: str = "candidate"
    timeout_s: float = 60.0
    max_tokens: int = 128

    def __post_init__(self):
        for value in (self.name, self.model):
            if type(value) is not str or not 1 <= len(value) <= 128 or not value.isprintable():
                raise ValueError("engine-identity")
        if self.kind not in ("ollama", "openai", "strata") or self.role not in ("reference", "candidate", "reviewer"):
            raise ValueError("engine-kind-role")
        endpoint(self.base_url)
        if self.kind == "ollama" and endpoint(self.base_url)[2]:
            raise ValueError("ollama-base-path")
        if not number(self.timeout_s, 0.1, 120):
            raise ValueError("engine-timeout")
        if type(self.max_tokens) is not int or not 1 <= self.max_tokens <= 512:
            raise ValueError("engine-token-budget")

    def record(self):
        return asdict(self)


@dataclass(frozen=True)
class EngineResult:
    name: str
    kind: str
    model: str
    outcome: str
    text: str | None
    elapsed_s: float
    request_sha256: str
    response_sha256: str | None
    raw_response: dict | None
    error: str | None
    usage: dict | None
    request_body: str | None = None
    response_body: str | None = None
    http_status: int | None = None

    def record(self):
        return asdict(self)
