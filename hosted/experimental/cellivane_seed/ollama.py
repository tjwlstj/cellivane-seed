"""Strict Ollama subengine adapter for Seed's plain completion contract.

Ollama owns tensor execution. The existing bridge owns bounded loopback I/O;
Seed owns admission and result reuse. Raw backend receipts stay in local audit.
"""
from __future__ import annotations

import threading

from cellivane_bridge.adapters import request
from cellivane_bridge.contracts import EngineConfig
from .core import BackendFailure


class OllamaBackend:
    def __init__(self, config, audit):
        if type(config) is not EngineConfig or config.kind != "ollama":
            raise ValueError("ollama-backend-config")
        self.config, self.audit = config, audit

    def __call__(self, payload):
        config = EngineConfig(self.config.name, "ollama", self.config.base_url,
            self.config.model, timeout_s=self.config.timeout_s,
            max_tokens=payload["max_tokens"])
        result = request(config, payload["messages"], threading.Event())
        self.audit.append("backend_exchange", result.record())
        if result.outcome != "ANSWERED":
            raise BackendFailure("ollama-" + result.outcome.lower())
        usage = result.usage
        if type(usage) is not dict or set(usage) != {"prompt_eval_count", "eval_count"}:
            raise BackendFailure("ollama-token-counts-required")
        prompt, completion = usage["prompt_eval_count"], usage["eval_count"]
        if any(type(n) is not int or not 0 <= n < 1 << 62 for n in (prompt, completion)):
            raise BackendFailure("ollama-token-counts-invalid")
        return {"model": config.model, "choices": [{"index": 0,
            "message": {"role": "assistant", "content": result.text},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": prompt,
            "completion_tokens": completion, "total_tokens": prompt + completion}}
