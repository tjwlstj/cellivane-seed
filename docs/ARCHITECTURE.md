# Cellivane Seed architecture

Status: **PARTIAL / experimental**. Reviewed 2026-10-12. This standalone
Python project owns local request admission, completed-result reuse and
reference/subengine comparison. An external engine owns token computation.
It does not implement a native kernel, tensor kernels or model training.

## Responsibility flow

```mermaid
flowchart LR
    Client[Local client] --> HTTP[Bounded loopback HTTP admission]
    HTTP --> Key[Validated request and artifact/config identity]
    Key --> Cache{Exact completed result?}
    Cache -->|hit| Result[Per-caller response envelope]
    Cache -->|miss| Flight{Identical request in flight?}
    Flight -->|join within bound| Wait[Wait for the existing result]
    Flight -->|new and slot available| Backend[External tensor backend]
    Flight -->|distinct and all slots occupied| Busy[HTTP 429]
    Backend --> Valid[Validate completed plain-text answer]
    Valid --> LRU[Bounded in-memory LRU]
    LRU --> Result
    Wait --> Result
    HTTP -.-> Audit[Serialized local evidence records]
    Backend -.-> Audit
```

The result cache stores a normalized completed answer, not model weights or
KV tensors. A cache hit gets a fresh response envelope and reports its cache
execution kind. Reusing a completed answer is different from accelerating
the model's token-generation kernel.

| Component | Concrete responsibility | Boundary |
|---|---|---|
| [Seed core](../hosted/experimental/cellivane_seed/core.py) | Strict request validation; artifact/config-keyed result LRU; bounded same-key joins; distinct compute slots | No executor, hidden generation queue or tensor execution |
| [Seed HTTP server](../hosted/experimental/cellivane_seed/server.py) | Loopback admission, connection bounds, backend wire records, local scheduler audit | Health means admission availability; metadata is not endpoint/PID attestation |
| [Ollama translation](../hosted/experimental/cellivane_seed/ollama.py) | Translate a completed Ollama response and logical token counts into Seed's plain completion contract | External Ollama owns tensors and any backend-local prompt/KV behavior |
| [Placement plan and observation](../hosted/experimental/cellivane_seed/placement.py) | Separate requested CPU/GPU placement from bounded run-log observations; bind configuration digests | Observed offload does not independently qualify a GPU or authenticate a process |
| [Bridge runtime](../hosted/experimental/cellivane_bridge/runtime.py) | Qualify reference before generation; reference first, then qualified subengines | Maximum three engines, child parallelism two, turn deadline 180 seconds; no automatic replacement |
| [Independent Seed replay](../tools/hosted/verify_cellivane_seed.py) | Recheck saved wire, identity joins, scheduler counters and terminal zero-active state without importing the runtime | Unkeyed hashes are integrity checks, not hostile-source authentication or OS liveness proof |

## Current serving contract

The HTTP surface is a nonstreaming OpenAI subset: `GET /health`,
`GET /v1/models`, `GET /v1/status` and `POST /v1/chat/completions`.
It accepts literal `127.0.0.1` and an explicit port, checks the Host header,
and rejects browser Origin requests. It is a local application interface,
not an authenticated multiuser network service.

The current request contract accepts 1–32 plain `system`, `user` or
`assistant` messages, including at least one `user` message. The normalized
request must fit 16 KiB; the HTTP body has its separate 32 KiB bound.
`temperature` must be zero, `stream` must be false, and `max_tokens` is
1–512. The backend's raw response and Seed's outgoing envelope each have
their own 256 KiB check. Unsupported tool/thinking output, incomplete
completion, unknown fields at the constrained contract boundary and
invalid token counts do not become successful fallback answers.

Default Seed capacity is one distinct compute slot, at most eight same-key
waiters, and an LRU of at most 32 completed entries and 1 MiB of accounted
result/key bytes. Python allocator overhead, weights and backend KV memory
are outside that byte counter. A different request arriving while all slots
are occupied receives 429; there is no hidden distinct-request queue.

The key includes the configured artifact identity and the normalized
model/messages/generation settings. Backend binary/library changes, model
hash changes and hardware-plan changes need a new identity. Temperature
zero makes exact-result memoization an application policy; it does not
guarantee identical floating-point results across CPU/GPU implementations.
The cache is ephemeral process memory and can contain sensitive answer text.
Raw audit evidence can also contain user input and output; keep it local.

A waiting subscriber can time out without owning cancellation of the
shared backend computation. A disconnected HTTP client does not cancel
other subscribers. The backend must bound its own I/O. Seed rejects an
owner result returned after its deadline, but Python cannot interrupt an
arbitrary blocked callable. Logical usage copied into a cached completion
does not mean those tokens were recomputed on that hit.

Audit writes are serialized and poison admission on persistence failure.
They use exclusive records and file fsync. This is not power-loss or
filesystem crash-recovery qualification. Owned Windows launchers use Job
Objects and retained process handles; checking an empty port and observing
an owned process start still does not independently bind that process to
every response received at the endpoint.

## A small hardware-plan surface

The dense-model experiment asks an existing tensor backend to divide a
fixed model's layers between CPU and a GPU. Cellivane records the plan and
observations; it does not move tensors itself. The current profile mapping
targets Ollama and should not be treated as a universal backend CLI.

| Requested profile | Ollama plan | Required interpretation |
|---|---|---|
| `cpu` | `num_gpu=0` | Confirm CPU placement in the run's logs; a request alone is not proof |
| `hybrid` | Explicit bounded GPU-layer count, default 14 | Record actual offloaded/total layers and backend; the backend may clamp or reject a request |
| `auto` | `num_gpu=-1` | Backend chooses placement; record its actual outcome and any CPU fallback |

The plan fixes context 2048, four CPU threads, one slot and temperature
zero. Its identity joins model SHA-256, verified backend-tree digest and
plan digest. Numeric offload records and CPU/CUDA/Vulkan buffer/runtime
records are observations. Missing, conflicting or failed logs remain
`UNKNOWN` or `FAILED`; a GPU mentioned in discovery is insufficient.
Five local Windows runs compared Qwen3 0.6B/1.7B/4B on CPU, 4B with
14/37 layers observed on CUDA, and 1.7B auto with 29/29 layers observed on
CUDA using an NVIDIA RTX 5050. Their operational acceptance and separate
recorded-evidence replay passed. Exact-format scores and timing scopes are
reported in [BENCHMARKS.md](BENCHMARKS.md); they do not establish general
GPU qualification or a portable quality/speed guarantee. A single installed
GPU cannot provide an actual multi-GPU experiment.

Backend-specific distinctions matter. Current llama.cpp supports a GPU-layer
count and several split modes; its `--tensor-split` divides work among GPUs,
not between CPU and GPU. Current llamafile documents explicit GPU selection
and possible CPU fallback. Flags from a newer backend must not be copied
blindly into the previously qualified llamafile 0.10.5 launcher. See the
fixed upstream references in [UPSTREAM.md](UPSTREAM.md).

## Comparison and evidence boundaries

The five accepted model/placement runs and the 280-test local suite belong
to the published
[59c9bcd snapshot](https://github.com/tjwlstj/cellivane-seed/commit/59c9bcd).
The current comparison launcher's source collection now explicitly lists its
sixteen-file family. Retaining that count does not preserve the modified
launcher's hash or validate the new optional capability/resource modules.

The original Windows CPU smoke used llamafile 0.10.5 and Qwen3-0.6B Q8_0,
then compared a Seed reference response with an Ollama subengine. Its three
exact-answer samples passed only one of three expected strings. That is
useful serving/compatibility evidence, not a model-quality improvement.
The original 175-test regression count belongs to that earlier source
snapshot; added public modules need their own regression results.

The new [comparison launcher](../tools/hosted/cellivane_seed_compare.py)
uses fixed 0.6B, 1.7B and 4B Q8_0 artifacts and a twelve-case exact-answer
smoke corpus. It disables Seed's completed-result cache for generation
timings. Backend-local prompt/KV reuse may still occur and must be stated
in the timing scope. Successful HTTP completion, exact string score,
format compliance, generation rate and placement observations are separate
measurements. A small fixed corpus is not a general accuracy benchmark.

For larger-model comparisons, keep prompts, template/thinking mode,
sampling settings, context and token budget fixed. Qwen's recommended
sampling profile differs from Seed's required temperature-zero application
profile; report that limitation rather than calling this an official Qwen
benchmark. Cache-hit latency must stay separate from actual generation.

## Strata reference boundary

Strata is a design reference for explicit hardware ownership and bounded
data movement. Its model-specific MoE expert-cache and multi-GPU layer
pipeline are different from this dense-backend profile. No upstream Strata
code is copied here. Seed has no native MTP, live target-token verifier,
cross-engine KV sharing or independently implemented tensor kernel. The new
experimental draft path delegates drafting and target verification to the
pinned external llama.cpp backend; its independent Python verifier replays
recorded evidence rather than generating or verifying model tokens itself.

The bridge's explicit `kind="strata"` remains fail closed because owned
Strata process/model qualification is not implemented. An available HTTP
service named Strata cannot qualify its default mock as a real reference.
An unqualified reference dispatches no generation. Native Kernel Room
management authority, ABI changes, streaming and actual multi-GPU support
remain outside this standalone experimental project.

## Contract, budget and speculation follow-up

The current development slice separates a validated backend contract,
requested resource budget, executable plan, observed execution and
independent verdict. The contract must say what a backend can actually do;
a profile name or resource estimate cannot create that capability. A budget
is a planning/admission constraint, not a claim of OS-enforced memory or
VRAM isolation. Missing measurements remain unknown.

[BackendCapabilities](../hosted/experimental/cellivane_seed/capabilities.py)
and [resource admission](../hosted/experimental/cellivane_seed/resources.py)
are **PARTIAL**, with 26 pure fixture tests passed. `none`, `draft` and
`ngram` are planner modes; this adapter contract permits only `none` for
Ollama/llamafile and all three declarations for llama.cpp. Caller-supplied
observed labels require both evidence and exact demand identity; they do
not authenticate an engine. The plan returns `ADMITTED`, `BLOCKED` or
`UNKNOWN`. Full target/draft file bytes plus declared host overhead and
context workspace are counted in RAM even for GPU placement. GPU demand
requires its own byte estimate and provenance hash; missing estimates stay
`UNKNOWN`, with no file-size/layer-ratio VRAM inference.

The [plan CLI](../tools/hosted/cellivane_seed_plan.py) passed six synthetic
fixtures. It hashes local GGUF bytes and prints a plan without launching or
contacting a backend. It does not inspect the GGUF architecture or provide
model compatibility proof. It creates no observed labels, so
`--require-observed` remains `UNKNOWN`. See the
[field and CLI contract](DEVELOPMENT_GUIDE.md#implemented-planner-contract)
for exact inputs and exit meanings.

The separate [CPU experiment launcher](../tools/hosted/cellivane_seed_speculate.py)
now configures the pinned backend's `none` and `draft-simple` paths, starts a
fresh owned server per phase and alternates their order by round. CPU-only
target/draft execution uses four threads, context 2048, greedy draft sampling
and no prompt/result reuse. Source, both model hashes and all prepared assets
are rechecked before each phase and again after it. This detects changes at
those boundaries; it is not atomic filesystem/loader attestation.

Two diagnostics failed and their original source/logs remain preserved.
The producer now uses backend `--reasoning-format auto` to extract empty
thinking delimiters. Seed/bridge response validation remains unchanged and
rejects nonempty reasoning; no answer normalization bypass was added. Raw
worker log bytes and hashes are retained. Only a derived observation view
uses UTF-8 replacement decoding, and speculative counters require complete
ASCII lines; HTTP JSON still requires strict UTF-8. See the
[experiment procedure](DEVELOPMENT_GUIDE.md#cpu-speculation-launcher-and-replay)
and [results owner](SPECULATION.md).

The second failure classified a pinned backend's nonfatal differential
template probe as fatal. Only its exact nine-line `lstrip` probe context is
now retained as a warning; other error/fatal records still reject acceptance.
Neither original failed run is promoted to PASS.

Experiment wall time includes HTTP, prefill and generation under verbosity-5
TRACE logging; it is not decoder-only throughput or production speedup.
Corpus timings exclude warmup, while phase draft counters include it.
Conservative counter bounds do not bind native task IDs to HTTP requests.

The accepted repeat-three run completed six phases, 24 timed responses and six
warmups. All twelve paired texts matched, and independent source-checked replay
passed. Draft logs recorded 444 offered / 366 accepted tokens including warmup.
The median per-pair OFF/ON wall-time ratio was 1.1974; this is an advisory
small-corpus observation for the specified CPU/model pair. Source, model and
backend-asset checks matched at all six phase boundaries, and recorded owned
Job cleanup and port closure were confirmed.
The owned primary process peak working set increased from about 4.35 GiB OFF
to 5.22 GiB ON; the additional draft model did not save memory. These counters
are not whole-system DRAM/VRAM accounting or enforced memory limits.
Seed's result cache and same-key coalescing do not implement token drafting,
and Strata's model-specific native MTP is not inherited by a dense backend.
Actual external-backend speculation was observed; generalized backend
qualification, endpoint-PID attestation and quality claims remain false.
The final local Windows regression passed 385 tests with zero skips in
29.307 seconds. Verify terminal fixture-workflow results for the exact
published revision separately from these local results;
the [development guide](DEVELOPMENT_GUIDE.md#current-slice-contract-budget-and-speculation)
owns that acceptance boundary. Earlier comparison PASS results do not
validate this changed source or extend this bounded speculation result.
