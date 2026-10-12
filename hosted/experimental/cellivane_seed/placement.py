"""Small requested-placement plans, separate from observed tensor execution.

Only Ollama's num_ctx, num_thread, num_gpu and temperature subset is emitted.
The pinned v0.40.2 api/types.go declares the runner options and uses num_gpu=-1
for dynamic selection: https://github.com/ollama/ollama/blob/v0.40.2/api/types.go
No upstream implementation is imported. A plan does not prove GPU support,
allocation, model qualification, multi-GPU execution, or a VRAM budget.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import re

__all__ = ["PlacementPlan", "build_plan", "artifact_identity", "observe_placement"]

MAX_LOG_BYTES = 1024 * 1024
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


@dataclass(frozen=True, slots=True)
class PlacementPlan:
    profile: str
    num_gpu: int
    backend: str = "ollama"
    num_ctx: int = 2048
    num_thread: int = 4
    slots: int = 1
    temperature: int = 0
    schema_version: int = 1

    def __post_init__(self):
        self._validate()

    def _validate(self):
        if type(self.profile) is not str or self.profile not in ("cpu", "hybrid", "auto"):
            raise ValueError("placement-profile")
        if type(self.backend) is not str or self.backend != "ollama":
            raise ValueError("placement-backend")
        for field, expected in (("num_ctx", 2048), ("num_thread", 4), ("slots", 1),
                                ("temperature", 0), ("schema_version", 1)):
            value = getattr(self, field)
            if type(value) is not int or value != expected:
                raise ValueError("placement-" + field)
        if type(self.num_gpu) is not int:
            raise ValueError("placement-num_gpu")
        valid = ((self.profile == "cpu" and self.num_gpu == 0)
                 or (self.profile == "hybrid" and 1 <= self.num_gpu <= 128)
                 or (self.profile == "auto" and self.num_gpu == -1))
        if not valid:
            raise ValueError("placement-num_gpu")

    def record(self):
        """Return a fresh JSON-safe requested plan; no observed result is inferred."""
        self._validate()
        return {"schema_version": self.schema_version, "backend": self.backend,
                "profile": self.profile, "num_ctx": self.num_ctx,
                "num_thread": self.num_thread, "slots": self.slots,
                "num_gpu": self.num_gpu, "temperature": self.temperature,
                "scope": "requested-placement-only"}

    @property
    def sha256(self):
        return sha256(_encoded(self.record())).hexdigest()

    def ollama_options(self):
        """Safe parameter values for an API options/parameters object or Modelfile.

        The caller owns JSON/Modelfile serialization, one runner slot and device
        visibility. No model path, shell text, device mask or observed placement
        is hidden in this mapping.
        """
        self._validate()
        return {"num_ctx": self.num_ctx, "num_thread": self.num_thread,
                "num_gpu": self.num_gpu, "temperature": self.temperature}


def build_plan(profile, layers=14):
    """Build cpu(0), bounded hybrid(layers), or dynamic auto(-1).

    layers is an exact integer in 1..128, used only by hybrid. Validate it even
    for other profiles so an ignored bool/nonfinite value cannot pass silently.
    """
    if type(profile) is not str or profile not in ("cpu", "hybrid", "auto"):
        raise ValueError("placement-profile")
    if type(layers) is not int or not 1 <= layers <= 128:
        raise ValueError("placement-layers")
    return PlacementPlan(profile, 0 if profile == "cpu" else -1 if profile == "auto" else layers)


def artifact_identity(*, model_sha256, backend_tree_sha256, plan_sha256):
    """Bind caller-verified artifact/config digests; this does not hash files.

    A caller must verify the model and the complete backend tree, including
    loaded libraries, and supply the plan hash. No digest authenticates source.
    """
    fields = {"model_sha256": model_sha256, "backend_tree_sha256": backend_tree_sha256,
              "plan_sha256": plan_sha256}
    if any(type(value) is not str or _HEX.fullmatch(value) is None for value in fields.values()):
        raise ValueError("placement-artifact-digest")
    return sha256(_encoded({"schema_version": 1, **fields})).hexdigest()


_OFFLOAD = re.compile(r"^(?:llama_model_)?load_tensors:\s+offloaded ([0-9]+)/([0-9]+) layers to GPU\s*$")
_LOADED = re.compile(r"^load_backend:\s+loaded (CPU|CUDA|Vulkan) backend\b", re.I)
_COMPUTE = re.compile(r'^time=\S+\s+level=INFO\s+source=\S+\s+msg="inference compute".*'
                      r'\blibrary=(cpu|cuda|vulkan)(?:\s|$)', re.I)
_STRUCTURED = re.compile(r'^time=\S+\s+level=(?:INFO|DEBUG)\s+source=\S+\s+'
                         r'msg="offload to (cpu|cuda|vulkan)"(?:\s|$)', re.I)
_BUFFER = re.compile(r"^(?:load_tensors|llama_model_load_tensors|llama_context|llama_kv_cache|sched_reserve):"
                     r"\s+(CPU|CUDA[0-9]*|Vulkan[0-9]*)\s+(model|KV|compute|output) buffer size\s*=\s*"
                     r"([0-9]+(?:\.[0-9]+)?) MiB\s*$", re.I)
_ERROR = re.compile(r"\blevel=(?:ERROR|FATAL)\b|\bCUDA error\b|\bout of memory\b|"
                    r"\bfailed to (?:allocate|load|initialize)\b|\berror loading model\b|"
                    r"\bfatal error\b|\brunner process has terminated\b", re.I)
_UNAVAILABLE = re.compile(r"no usable GPU found|no compatible GPUs|no CUDA-capable device|"
                          r"falling back to CPU", re.I)
_PROBE_NO_DEVICE = re.compile(r'^time=\S+\s+level=DEBUG\s+source=native_probe\.go:[0-9]+\s+'
                              r'msg="native GPU discovery failed"\s+.*'
                              r'stderr="ggml_cuda_init: failed to initialize CUDA: '
                              r'no CUDA-capable device is detected(?:\\r|\\n|\\"|"|$)')


def observe_placement(text):
    """Parse a bounded, run-scoped log; fail closed on errors/conflicting loads.

    Numeric native offload records and loaded runtime/buffer records are
    observations only. Discovery text, config flags and quoted examples are
    insufficient. GPU_OFFLOAD_OBSERVED never means qualified model inference.
    Keep logs from separate model loads/runs separate; contradictory counts are
    not assigned to whichever load happened to appear last.
    """
    if type(text) is not str:
        raise ValueError("placement-log-type")
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("placement-log-utf8") from exc
    if len(raw) > MAX_LOG_BYTES:
        raise ValueError("placement-log-size")
    runtimes, counts, buffers, errors, warnings = set(), set(), [], set(), set()
    native_count, structured_count = False, False
    for line in text.splitlines():
        if _PROBE_NO_DEVICE.match(line):
            # A masked/absent device failed in the discovery subprocess, not in
            # the model runner. Never treat its quoted stderr as runtime proof.
            warnings.update(("gpu-discovery-unavailable", "gpu-unavailable"))
            continue
        if _ERROR.search(line):
            errors.add("runtime-error")
        if _UNAVAILABLE.search(line):
            warnings.add("gpu-unavailable")
        match = _OFFLOAD.fullmatch(line)
        if match:
            fields = match.groups()
            if any(len(value) > 4 for value in fields):
                errors.add("offload-count-range")
            else:
                offloaded, total = map(int, fields)
                if not 1 <= total <= 4096 or not 0 <= offloaded <= total:
                    errors.add("offload-count-range")
                else:
                    counts.add((offloaded, total))
                    native_count = True
        elif re.match(r"^(?:llama_model_)?load_tensors:\s+offloaded\b", line):
            errors.add("offload-count-format")
        if _STRUCTURED.match(line):
            fields = re.findall(r'(?:^|\s)layers\.(offload|model)=(\S+)', line)
            if len(fields) != 2 or {name for name, _value in fields} != {"offload", "model"}:
                errors.add("structured-offload-fields")
            elif any(re.fullmatch(r"[0-9]{1,4}", value) is None for _name, value in fields):
                errors.add("offload-count-format")
            else:
                values = {name: int(value) for name, value in fields}
                offloaded, total = values["offload"], values["model"]
                if not 1 <= total <= 4096 or not 0 <= offloaded <= total:
                    errors.add("offload-count-range")
                else:
                    counts.add((offloaded, total))
                    structured_count = True
        runtime = _LOADED.match(line) or _COMPUTE.search(line)
        if runtime:
            runtimes.add(runtime.group(1).upper())
        match = _BUFFER.fullmatch(line)
        if match:
            device, kind, size = match.groups()
            size = float(size)
            if not math.isfinite(size) or not 0 <= size <= 2**40:
                errors.add("buffer-size-range")
            else:
                runtime = re.sub(r"[0-9]+$", "", device).upper()
                runtimes.add(runtime)
                buffers.append({"runtime": runtime, "device": device.upper(),
                                "kind": kind.lower(), "mib": size})
    if len(counts) > 1:
        errors.add("conflicting-offload-counts")
    offloaded, total = next(iter(counts)) if len(counts) == 1 else (None, None)
    positive = offloaded is not None and offloaded > 0
    if positive and "gpu-unavailable" in warnings:
        errors.add("offload-unavailable-conflict")
    gpu_model_buffer = any(row["runtime"] in ("CUDA", "VULKAN")
                           and row["kind"] == "model" and row["mib"] > 0 for row in buffers)
    # A structured scheduling estimate needs an actual model buffer as well.
    gpu = bool(positive and {"CUDA", "VULKAN"} & runtimes and (native_count or gpu_model_buffer)
               and not errors and not warnings)
    status = ("FAILED" if errors else "GPU_OFFLOAD_OBSERVED" if gpu
              else "CPU_OBSERVED" if "CPU" in runtimes and not positive else "UNKNOWN")
    return {"schema_version": 1, "scope": "run-log-observation-only", "status": status,
            "offloaded_layers": offloaded, "total_layers": total,
            "offload_count_source": "native" if native_count else "structured" if structured_count else "none",
            "runtimes": sorted(runtimes), "buffers": buffers,
            "gpu_offload_observed": gpu, "gpu_qualified": False,
            "multi_gpu_qualified": False, "errors": sorted(errors), "warnings": sorted(warnings),
            "log_sha256": sha256(raw).hexdigest()}
