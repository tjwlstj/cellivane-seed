# Bounded model and placement smoke comparison

Status: **PARTIAL / five accepted local runs**. All five final reports have
`identity_schema_version=2`, `outcome="COMPLETED"` and `acceptance="PASS"`.
Separate replay against the matching sixteen runtime/launcher source files
passed for all five reports and their bridge journals. Replay checks recorded
integrity and semantic joins; it does not attest endpoint/PID identity.
Earlier diagnostic runs without an acceptance field are preserved locally
and excluded from public metrics.

These measurements belong to the published
[59c9bcd snapshot](https://github.com/tjwlstj/cellivane-seed/commit/59c9bcd).
The current comparison launcher has changed its source enumeration to an
explicit sixteen-file family; that change does not refresh these historical
run hashes or validate new source. The separate draft/target experiment is
documented in [SPECULATION.md](SPECULATION.md).

## Question and scope

The experiment asks whether the same small serving layer can run three
dense Qwen3 Q8_0 artifacts through a fixed Ollama backend, and whether a
small requested placement surface can select CPU, partial GPU offload or
backend-chosen placement on one Windows machine. It measures application
completion, a small exact-answer corpus, latency, backend generation rate,
and observed placement separately. It does not test Strata inference,
multi-GPU execution, MTP, training or general model intelligence.

The final run set is 0.6B CPU, 1.7B CPU, 4B CPU, 4B hybrid with 14 requested
GPU layers, and 1.7B auto. No result is assumed merely from the profile name.
CPU/GPU placement must be observed in the owned run's logs and loaded-model
metadata. See [architecture](ARCHITECTURE.md) and
[official sources and artifact hashes](UPSTREAM.md).

## Fixed protocol

| Setting | Value / interpretation |
|---|---|
| Tensor engine | Pinned external Ollama v0.40.2; Cellivane owns serving, not tensors |
| Models | Qwen3 dense 0.6B, 1.7B, 4B; Q8_0; exact model SHA-256 and byte size checked |
| Corpus | Twelve fixed arithmetic, formatting, Korean, logic, extraction and small code-result prompts |
| Repeats | Two sequential passes over the same twelve prompts: 24 responses, not 24 independent tasks |
| Generation settings | Temperature 0, maximum 64 generated tokens, context 2048, four CPU threads, nonstreaming/no-thinking plain answers |
| Concurrency | One distinct Seed slot; one visible NVIDIA device for hybrid/auto; Vulkan disabled |
| Seed completed-result cache | Disabled; every scored response must be `computed` with one backend exchange |
| Warmup | Separate request before the corpus; warmup wall time is not included in corpus latency |
| Backend prompt/KV cache | May be reused by Ollama; not reset between corpus rows |
| Exact score | `text.strip() == expected`; leading/trailing whitespace is ignored, other content is not repaired |
| Wall latency | End-to-end Seed HTTP request with local backend and synchronous evidence writes |
| Generation rate | Backend `eval_count / eval_duration`; excludes Seed HTTP and audit overhead |
| Identity | Version 2 identity binds the model/backend/plan artifact identity and effective execution configuration |

The corpus and system instruction are versioned in the
[comparison launcher](../tools/hosted/cellivane_seed_compare.py). Corpus and
source digests travel with each private report. Temperature zero is this
application's bounded memoization/comparison profile; it differs from
Qwen's recommended sampling configuration and is not an official Qwen
benchmark.

## Final accepted runs

The host was Windows with one NVIDIA GeForce RTX 5050, reported 8151 MiB
device memory, driver 617.42 and four requested CPU threads. The raw
comparison report does not record the CPU model. A separate current Windows
CIM host inspection identified AMD Ryzen 7 7840HS w/ Radeon 780M Graphics,
eight physical cores and sixteen logical processors; this is host inventory,
not a field recovered from the raw run reports. `auto` means a requested
policy; observed offload determines what ran. All rows below had 24/24
successful computed corpus responses and operational acceptance PASS.

| Model | Requested profile | Accepted responses / exact | Median wall latency | Median backend tokens/s | Observed GPU layers | Loaded-model VRAM |
|---|---|---|---|---|---|---|
| 0.6B Q8_0 | cpu | 24/24; 6/24 exact (25.0%) | 161.408 ms | 94.149 | CPU observed; count absent | 0 B |
| 1.7B Q8_0 | cpu | 24/24; 14/24 exact (58.3%) | 323.687 ms | 39.037 | CPU observed; count absent | 0 B |
| 4B Q8_0 | cpu | 24/24; 22/24 exact (91.7%) | 710.839 ms | 16.699 | CPU observed; count absent | 0 B |
| 4B Q8_0 | hybrid, 14 requested | 24/24; 22/24 exact (91.7%) | 525.971 ms | 21.457 | 14/37, CPU + CUDA | 2024496168 B (1.885 GiB) |
| 1.7B Q8_0 | auto | 24/24; 14/24 exact (58.3%) | 82.447 ms | 150.904 | 29/29, all model layers CUDA | 2115795680 B (1.970 GiB) |

In this fixed corpus, larger CPU models answered more exact-format trials
and generated tokens more slowly. The 4B hybrid run retained the same
22/24 score as 4B CPU while its median wall time was lower; 1.7B auto retained
14/24 and observed complete GPU layer offload. These are observations from
one session per profile, not a causal or portable performance guarantee.
Both repeats had the same pass/fail outcome for each case within each run.

"Loaded-model VRAM" is Ollama `/api/ps` `size_vram`, not peak whole-device
usage or independently measured per-process VRAM. CPU logs supplied no
numeric offloaded-layer record, so the table does not invent a zero/total
layer pair. GPU observations retain `gpu_qualified=false` in the evidence:
completed tasks plus observed offload are narrower than general GPU
qualification.
The auto run also loaded a CPU backend; that availability does not mean
model layers were split onto CPU when all 29/29 were offloaded to CUDA.

The [public aggregate](benchmarks/2026-10-12-summary.json) contains
allowlisted settings, scores, timing statistics, placement observations,
model/backend/source digests, run UUIDs and raw-report SHA-256 values.
It excludes absolute paths, process IDs, environment dumps, raw prompts,
raw answers and raw engine logs. Full evidence remains private. A raw-report
digest is a content reference, not public availability or independent
authentication of that report. Independent replay covers 81 audit records
per run and the separate bridge journal. The frozen comparison verifier's
SHA-256 is
`6e33b097af3624c4f0305805a81bba23cf2b8c16b33462bf5253f1b0965425b4`.
Its 45 positive/negative fixture tests passed. The final local Windows suite
passed 280 tests with zero skips in 28.110 seconds, including those verifier
tests. Unit/fixture regression results are separate from actual model runs
and from the CI result for any later published commit.

| Model / profile | Final run UUID | Private comparison.json SHA-256 |
|---|---|---|
| 0.6B cpu | `d51fd715-d606-4958-a99b-dc2816d962e2` | `14500368f73bdcbe5caafd288961cb85e530937865919e3ee7ab38b5433380c0` |
| 1.7B cpu | `30bc48b4-8dc4-4c37-a102-091a0e803423` | `9e5243cb45c558cb110817b6ece70b78358dd450e781850f3b8fcab8f10a9002` |
| 4B cpu | `ba4960f1-250c-4b1d-a8de-8e8fad05a882` | `f6065a6a83ae9d2d1d5c0cdadfd44e9bd2d45eb745ce4f95cec83c059dd220e8` |
| 4B hybrid | `36797f5a-dc89-42cc-9393-d7554cb7b6ff` | `607cfc31bebd98d7a9e870cd8a535b8f4d57ca69c937a4b0a8b673274d87acde` |
| 1.7B auto | `df7ec805-092d-4b27-af57-d78354e3f900` | `250ad368a869cde8365caf3aadb58e6ca4f9936546685054d3f1956da942b7aa` |

## Acceptance and interpretation

`COMPLETED` describes finishing the orchestration path. Operational
`acceptance="PASS"` additionally requires the complete computed corpus,
successful warmup, bridge completion, resolved placement, terminal zero
active/in-flight/waiter/cache-hit/coalesced/failure/timeout counters, closed
admission, unchanged source/model and owned-process/port cleanup checks.
Hybrid acceptance additionally requires observed partial GPU layer offload.
Answer correctness is reported separately and is not required to fabricate
an operational PASS. An unsupported, truncated or uncertain completion is
not replaced by a fallback answer.

Each run also exercises the bridge with a Seed reference and direct Ollama
candidate. They use the same tensor worker/model. That proves bounded API
compatibility for this run; it does not demonstrate agreement between
independent engines, answer improvement, redundant hardware or shared KV
between separate engines. The report's cleanup receipts and unkeyed hashes
do not independently prove endpoint/PID identity or adversarial authenticity.

## Limits on comparison

- The original 0.6B asset was obtained through ggml-org and reused from the
  verified local cache. Its exact content SHA-256 matches the immutable
  official Qwen 0.6B artifact listed in [UPSTREAM.md](UPSTREAM.md). The newly
  acquired 1.7B and 4B artifacts also use official Qwen pins. All three use
  Q8_0; the original acquisition publisher is a provenance footnote, not
  evidence of a different 0.6B conversion or a publisher confound.
- Twelve hand-selected prompts and two correlated repeats cannot establish
  a general accuracy ranking or statistical confidence. Exact score combines
  correctness with requested output format; correct verbose answers may fail.
  The older three-case smoke used a different scoring comparison and is not
  directly combined with these scores.
- Fixed case order and backend prompt/KV reuse can influence latency. Model
  loading and first warmup/JIT cost are separate from scored task timings.
  Different answer lengths also change wall latency; backend tokens/s and
  end-to-end latency answer different questions.
- Each model/profile has one owned execution session, not a balanced series
  of fresh restarts. Thermal state, background activity, run order, driver,
  backend package and exact hardware can affect results. CPU versus hybrid
  is a local placement experiment, not a portable speed guarantee.
- Configured GPU layers, discovered hardware, loaded-model `size_vram`,
  native layer counts and completed inference are distinct evidence. The
  plan does not implement a universal hardware allocator. One visible GPU
  gives no actual multi-GPU proof. No native Kernel Room ABI or authority is
  changed.

## Reproduce the comparison

Prepare the verified engine asset cache using the repository
[README](../README.md), and acquire a pinned model with
[cellivane_seed_assets.py](../tools/hosted/cellivane_seed_assets.py).
The comparison's `--asset-root` is the engine cache; `--model-file` is the
GGUF path; `--storage-root` is a separate local directory for private runs.

```powershell
py -3 -B tools/hosted/cellivane_seed_assets.py --model 1.7b --storage-root D:/AIOS/cellivane-seed-models-20261012
py -3 -B tools/hosted/cellivane_seed_compare.py --model 1.7b --model-file D:/AIOS/cellivane-seed-models-20261012/models/Qwen3-1.7B-Q8_0.gguf --asset-root D:/AIOS/cellivane-bridge-20261009 --storage-root D:/AIOS/cellivane-seed-models-20261012 --profile cpu --repeat 2
```

Use `--model 4b` with its matching model file for the larger artifact.
`--profile hybrid --layers 14` requests partial offload;
`--profile auto` asks the backend to choose placement. Read the generated
acceptance, placement and cleanup fields rather than treating any profile
argument as a successful GPU test. These commands execute models and write
private evidence; they are not unit tests or the original CPU smoke mode.

To replay the saved report without executing a model:

```powershell
py -3 -B tools/hosted/verify_cellivane_comparison.py /absolute/path/to/comparison.json --source-root /absolute/path/to/matching-source
```

The [comparison verifier](../tools/hosted/verify_cellivane_comparison.py)
uses saved evidence and the separate bridge verifier, without importing
runtime validators. Keep the exact matching source tree and private audit,
bridge and engine-log files together; the sanitized aggregate alone is not
enough to reconstruct that private replay.
