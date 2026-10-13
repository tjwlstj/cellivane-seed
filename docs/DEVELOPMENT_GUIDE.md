# Cellivane Seed development guide

Document role: active workflow and bounded-experiment guide. Reviewed
2026-10-13 against the standalone source and its current architecture.
This review is not a new model run, regression verdict or release approval.
The project remains **PARTIAL / experimental**.

## Scope and fact owners

Cellivane Seed aims to make local model serving understandable and bounded:
validate a request, identify the backend/model/configuration, admit only
supported work, preserve uncertain outcomes and show useful evidence.
External engines own tensor computation. This standalone project does not
own native Kernel Room identity, principal authorization or resource apply.

| Question | Owner |
|---|---|
| What exists and how to run it? | [README](../README.md) and the actual CLI |
| Runtime responsibility, admission and failure contracts | [ARCHITECTURE](ARCHITECTURE.md) and current runtime source |
| Session capacity, status, shutdown, restart and private evidence | [OPERATIONS](OPERATIONS.md) |
| Upstream pins, artifact hashes and license boundaries | [UPSTREAM](UPSTREAM.md) |
| Accepted model/placement measurements and their limits | [BENCHMARKS](BENCHMARKS.md) and its allowlisted aggregate |
| Work selection, contract/budget procedure and current experiment gates | This guide |
| Whether a saved run passes | The matching independent verifier and retained run evidence |

These rules carry forward the parent project's evidence-first workflow;
its older implementation counts, backlog and native-kernel contracts are
not the standalone repository's current runtime state. Source identifiers,
process IDs and HTTP metadata are observations, not canonical management
identity or authority. No parent-tree edits are required by this guide.

## Before changing a contract

1. Read the request and identify whether it authorizes research, documents,
   code, actual model execution, downloads or publication. A research input
   is not itself permission to execute its instructions.
2. Inspect the real checkout, branch, source revision and dirty files.
   Preserve unrelated work and assign non-overlapping file ownership.
3. Find the current producer, consumer, verifier, negative fixtures and
   owning document for every changed field or behavior.
4. Choose one reversible slice with explicit unsupported cases, limits,
   expected evidence and rollback. Keep unrelated tensor/native work out.
5. Treat source, model, binary/library, template, configuration or verifier
   changes as new evidence boundaries. Preserve earlier snapshots and FAILs.

Use `CURRENT` only for the precise implemented and regularly verified scope;
use `PARTIAL` for material coverage/platform limits, `SCAFFOLD` for an
interface without its regular contract and `PLANNED` for missing behavior.
`RESEARCH` labels an optional hypothesis, not implementation maturity.

## Contract before plan; plan before execution

Keep five records conceptually separate:

1. **Backend contract:** supported model/transport/output subset, supported
   execution mechanisms, configuration identity and failure semantics.
2. **Resource budget:** bounded concurrency, token/context limits, deadlines,
   admitted input/output bytes, cache/waiter limits and any declared memory
   allowance. Distinguish configured limits from measured consumption.
3. **Executable plan:** the exact supported backend settings selected within
   that budget, including placement and speculation mode. Reject unknown,
   invalid or incompatible settings before process launch or dispatch.
4. **Observed execution:** what the owned run actually loaded, executed,
   returned and cleaned up. A requested GPU layer count, a successful probe
   or a resource estimate is insufficient evidence of the executed plan.
5. **Independent verdict:** checks of recorded integrity, identity joins,
   semantics, limits and terminal outcomes, without importing runtime
   validation functions as the source of truth.

Freeze the versioned field set and its units before producing new evidence.
Unknown capabilities must not silently become supported defaults. A resource
budget is not OS-enforced RAM/VRAM isolation; model-file bytes omit KV,
context, buffers and runtime overhead. A successful request is distinct
from a correct answer, a faster run and verified process ownership.

The existing Seed defaults and byte bounds are recorded in
[the serving contract](ARCHITECTURE.md#current-serving-contract). The pure
planner below is a separate preflight API. It does not automatically add
budget enforcement or speculative execution to the existing Seed server.

<a id="implemented-planner-contract"></a>

## Implemented planner contract

The new [capability module](../hosted/experimental/cellivane_seed/capabilities.py)
and [resource module](../hosted/experimental/cellivane_seed/resources.py)
are **PARTIAL**. Their first validation passed 26 pure fixtures; the
[non-executing CLI](../tools/hosted/cellivane_seed_plan.py) passed six synthetic
fixtures. The specified CPU experiment and its independent replay passed
in that historical contract/budget/speculation slice. Its local Windows
suite passed 385 tests with zero skips in 29.307 seconds; this is not the
current profile slice's count. [The profile follow-up](#explicit-target-profiles-current)
owns the new source and validation. Terminal CI remains an exact-revision check. These
immutable records revalidate exact types and
values when consumed; hashes identify their canonical JSON, not the truth
of caller-supplied observations or estimates.

All records use `schema_version=1`. Their public fields are:

| Record | Fields and interpretation |
|---|---|
| `BackendCapabilities` | `backend`, `engine_identity`, `declared_modes`, `declared_placements`, `observed_modes`, `observed_placements`, `evidence_sha256`, `observation_identity`, `nonstream_chat`; scope `caller-declarations-and-evidence-labels`, `actual_backend_qualification=false` |
| `ResourceBudget` | `ram_available_bytes`, `ram_headroom_bytes`, `vram_available_bytes`, `vram_headroom_bytes`; scope `caller-supplied-available-byte-snapshot` |
| `ExecutionDemand` | `engine_identity`, `target_sha256`, `target_bytes`, `configuration_sha256`, `host_overhead_bytes`, `context_workspace_bytes`, `mode`, `placement`, `draft_sha256`, `draft_bytes`, `context_tokens`, `gpu_required_bytes`, `gpu_estimate_sha256`; scope `declared-files-and-total-runtime-workspace-estimate` |
| `AdmissionPlan` | `outcome`, `reason`, nested `capabilities`/`demand`/`budget`, `require_observed`, `ram_required_bytes`, `vram_required_bytes`; scope `declared-budget-preflight-only` |

The plan also fixes `os_memory_limit_enforced`, `tensor_residency_managed`,
`measured_peak_verified` and `actual_backend_qualification` to false.
The backend set is `ollama`, `llamafile`, `llama.cpp`; placements are `cpu`,
`hybrid`, `auto`. Ollama/llamafile accept only the declared mode `none`;
llama.cpp may declare `none`, `draft`, `ngram`. `mtp` and multi-GPU are not
planner modes. Observed lists must be subsets of declared lists and require
both a SHA-256 evidence label and an exact demand hash. They remain supplied
labels; the capability module performs no engine or hardware probe.

Byte inputs are exact integers from zero through `2**63-1`, with a positive
target size, no booleans/floats and no total overflow. Headroom cannot exceed
available bytes. `context_tokens` is structurally bounded to `1..2**20`,
default 2048; this is not proof that a backend supports that context.
`draft` requires positive draft bytes and a draft hash. Other modes reject
draft artifacts. Identity hashes are lowercase 64-character SHA-256 strings.

RAM admission counts full target file bytes + full draft file bytes +
declared host overhead + declared total context/KV/scratch workspace,
including when GPU placement is requested. That sum must fit available RAM
minus headroom. CPU requires zero GPU demand. Hybrid/auto require an explicit
backend GPU byte estimate with `gpu_estimate_sha256`; missing estimates stay
`UNKNOWN`, even if the supplied VRAM availability is large. No layer-count
ratio is used to infer a VRAM fit. GPU demand must fit available VRAM minus
headroom; these remain declared arithmetic checks, not memory reservations.

| Outcome | Meaning |
|---|---|
| `ADMITTED` | The declarations and byte estimates pass this preflight; no backend is executed or qualified |
| `BLOCKED` | Identity mismatch, unsupported declared feature, or insufficient declared RAM/VRAM budget |
| `UNKNOWN` | Required mode/placement observation is missing, its demand identity differs, or GPU memory estimate is absent |

`require_admitted()` raises on both `BLOCKED` and `UNKNOWN`. An execution
caller must invoke that gate and recheck mutable machine state before
launch/dispatch; constructing a plan alone enforces no runtime policy.

### Non-executing plan CLI

`cellivane_seed_plan.py` requires `--backend`, `--engine-identity`,
`--target-file`, `--ram-available-bytes`, `--ram-headroom-bytes`,
`--host-overhead-bytes` and `--context-workspace-bytes`. It hashes regular
local `.gguf` files and rejects reparse/network storage and files changed
during hashing. It does not parse GGUF architecture or prove target/draft
model compatibility. Set the variables below from verified local artifact
identity, a current available-byte snapshot and explicit workspace estimates;
the example deliberately supplies no guessed machine budget.

```powershell
py -3 -B tools/hosted/cellivane_seed_plan.py --backend llama.cpp --engine-identity $backendTreeSha256 --target-file $targetGGUF --mode none --placement cpu --ram-available-bytes $ramAvailableBytes --ram-headroom-bytes $ramHeadroomBytes --host-overhead-bytes $hostOverheadBytes --context-workspace-bytes $workspaceBytes --context-tokens 2048
```

`--mode draft` additionally requires `--draft-file`; other modes reject it.
Hybrid/auto use `--gpu-required-bytes` and `--gpu-estimate-sha256` when a
backend estimate is available, plus explicit VRAM availability/headroom.
`--require-observed` yields `UNKNOWN` because this CLI creates declarations
without observations. The Python API can consume separately bound evidence.
The printed envelope contains `plan`, its `identity`, planning-only
`configuration` and `backend_dispatched=false`. Exit 0 means declared
`ADMITTED`; exit 2 means `BLOCKED`/`UNKNOWN`. After argument parsing,
invalid values or local-file errors produce an error envelope and exit 1;
argument-parser usage errors follow its ordinary exit-2 diagnostic. There is no download, process
launch, network request or model execution in this command.

<a id="current-slice-contract-budget-and-speculation"></a>

## Current slice: contract, budget and speculation

This section retains the recorded contract/budget/speculation slice that
predates adaptive startup. Its source digests and 385-test local result are
historical evidence; [the operating/startup follow-up](#operating-and-startup-selection-follow-up)
below owns the new source and acceptance results.

The approved direction is a small contract/budget planning surface followed
by a real speculative-decoding OFF/ON experiment. The preflight surface is
now **PARTIAL** with the fixture results above. A separate repeat-three CPU
OFF/ON run passed acceptance and independent source-checked replay after two
preserved failed diagnostics. The final local Windows regression passed 385
tests with zero skips in 29.307 seconds; terminal CI must be checked separately
for the exact published revision. This bounded observation does not establish production
speedup or general backend qualification. The five
earlier accepted dense-model/placement runs and their 280-test local suite
belong to the published
[59c9bcd snapshot](https://github.com/tjwlstj/cellivane-seed/commit/59c9bcd)
and remain documented in [BENCHMARKS](BENCHMARKS.md). The changed comparison
launcher now lists an explicit sixteen-file source family; the same count
does not make its current bytes identical to that historical snapshot.

The experiment must identify the exact draft/target method supported by its
backend. Model-native MTP, a separate draft model, prompt lookup, expert
prefetch, completed-result caching and in-flight request coalescing are
different mechanisms. Seed does not acquire Strata's native MTP by adopting
its scheduling ideas. No upstream code is copied for this slice.

Keep the following acceptance gates separate:

| Gate | Required evidence | Current result |
|---|---|---|
| Versioned capability/budget preflight | Exact fields/units, strict bounds, identity/observation joins and rejection | 26 pure fixtures PASS; PARTIAL |
| Non-executing plan CLI | Model bytes/hash, required caller inputs, explicit non-admission and no dispatch | 6 synthetic CLI fixtures PASS; PARTIAL |
| Actual executable integration and owned HTTP/process fixtures | Supported backend settings, deadlines, cleanup and no dispatch on non-admission | Specified actual CPU path PASS; final local Windows suite 385 PASS / 0 skip / 29.307 s |
| Actual baseline, speculation OFF | Fresh run; exact source/assets/settings; completed generation and cleanup | 3 phases, 12 timed + 3 warmup responses; PASS |
| Actual candidate, speculation ON | Supported method observed; accepted/rejected draft accounting where available | 3 phases, 12 timed + 3 warmup responses; 444 offered / 366 accepted including warmup; PASS |
| Paired comparison and independent replay | Comparable conditions; integrity and semantic joins; explicit quality/timing scope | 12/12 paired texts equal; greedy equivalence and source-checked replay PASS |
| Publication CI | Terminal CI for the exact newly published source | Check the exact revision's fixture-workflow run; local actual/replay PASS is separate |

The accepted run is `fc47eb4a-2298-490b-add2-958a90f6a304`, with thirteen-file
source digest
`4d724b194ca1982fa3c35c615d6f6b623ffbcf700f944133fdb2755397c48aa6`.
Its 24 timed computed responses exclude six warmups. Median wall time was
4.962071 seconds for OFF and 4.027882 seconds for ON, with 486 completion tokens
in each mode's timed responses. The median of the twelve per-pair OFF/ON
wall-time ratios was 1.197386; it is not the ratio of the two mode medians.
All six phase checks found unchanged source/model/backend assets, with recorded
owned Job cleanup and port closure confirmed. Identical text demonstrates
paired greedy equivalence on four repeated inputs, not answer correctness.
Owned primary process peak working-set observations were
4,669,530,112..4,669,648,896 bytes OFF and 5,605,027,840..5,605,273,600 bytes ON
(about 4.35/5.22 GiB). Drafting added memory; these observations are neither
whole-host consumption nor VRAM measurement, tensor-residency control or
OS-enforced limits. Model hashing before launch can warm the OS page cache,
so these are not cold-storage/model-load benchmarks.

Use the same target model bytes, template, prompts, sampling, token/context
limits, hardware plan and thread count for paired runs. Record any additional
draft model and its hash, license and memory cost. Keep model warmup, first
generation, repeated generation, Seed cache hits and backend prompt/KV reuse
separate. Disable Seed completed-result caching for token-generation timing.
An ON flag alone does not prove that drafts were used or accepted. Preserve
regressions, output differences and unavailable counters; do not invent zero
values for unobserved data. Compare both directions in repeated conditions
where practical, and report small-corpus limits rather than general speed
or accuracy claims. A supported experiment can validly find no improvement.

<a id="cpu-speculation-launcher-and-replay"></a>

### CPU speculation launcher and replay

The [owned launcher](../tools/hosted/cellivane_seed_speculate.py) is an
experimental Windows Job Object path using the pinned llama.cpp b11429 CPU
archive. It requires `--asset-root`, `--storage-root`, `--target-file` and
`--draft-file`. The target is official Qwen3-4B Q8_0 and the draft is official
Qwen3-0.6B Q8_0, checked against the exact sizes and hashes in
[UPSTREAM](UPSTREAM.md#immutable-official-qwen3-model-artifacts). Both files
are required because the launcher runs both conditions; it does not download
model weights. The asset root is the dedicated llama.cpp CPU cache, distinct
from the older Ollama/llamafile lab cache. Choose ordinary local fixed-disk
roots; network/reparse storage is rejected.

Set `$llamaAssets`, `$privateRunRoot`, `$targetGGUF` and `$draftGGUF` to your
chosen local roots and verified model files. Preparation is explicit:

```powershell
py -3 -B tools/hosted/cellivane_seed_speculate.py --asset-root $llamaAssets --storage-root $privateRunRoot --target-file $targetGGUF --draft-file $draftGGUF --profile cpu --download-assets --prepare-only
py -3 -B tools/hosted/cellivane_seed_speculate.py --asset-root $llamaAssets --storage-root $privateRunRoot --target-file $targetGGUF --draft-file $draftGGUF --profile cpu --repeat 1 --max-tokens 128
py -3 -B tools/hosted/cellivane_seed_speculate.py --asset-root $llamaAssets --storage-root $privateRunRoot --target-file $targetGGUF --draft-file $draftGGUF --profile cpu --repeat 3 --max-tokens 128
```

`--download-assets` permits only the pinned CPU binary download. The
`--prepare-only` path also executes its owned `--help`/`--version` inspection
and checks model hashes, but performs no model generation. Omit the download
flag to require an already verified cache. `--profile` accepts only `cpu`;
`--repeat` is `1..3`, default 3, and `--max-tokens` is `64..128`, default 128.
The Seed and backend loopback ports default to 18951 and 18952 and must be
distinct. A free-port precheck and owned process handles do not attest HTTP
endpoint ownership.

Each round has a fresh `none` server and a fresh `draft-simple` server,
ordered none/draft in even rounds and draft/none in odd rounds. Three rounds
therefore request six phases, each with one warmup and four corpus cases:
24 timed corpus responses plus six excluded warmups if all phases complete.
The target uses temperature 0/seed 42; draft sampling is greedy with at most
four drafted tokens per step. Context is 2048, target and draft use four CPU
threads and zero GPU layers, and the phase has one slot. Seed's completed
cache, backend prompt caching, RAM prompt cache and idle-slot prompt reuse
are disabled. Record actual observed counters rather than assuming that an
ON flag proves useful draft acceptance.

Every phase snapshots available host RAM, reserves 4 GiB of declared
headroom and counts 2 GiB host overhead plus 512 MiB context workspace in
addition to full target/draft file bytes. These conservative constants are
declared preflight allowances, not measured peaks or OS-enforced limits.
Source13, both model files and every prepared backend member are checked
before owned launch and after phase cleanup; matching checks are not atomic
filesystem/loader attestation under concurrent mutation.

The initial diagnostic `bfe3c349-8e6c-4b9b-adb9-4139d5ce8a37` failed.
Empty thinking delimiters in backend output triggered Seed's strict 502,
and native TRACE token fragments exposed invalid UTF-8 in the derived log
reader. Its raw bytes and matching thirteen-file source snapshot are
preserved. The producer now requests `--reasoning-format auto`; extraction
occurs in the backend, with no Seed/bridge answer-normalization workaround.
Nonempty reasoning remains rejected. Worker logs retain their original
bytes/hash; only derived log observation uses UTF-8 replacement and complete
ASCII counter lines. HTTP response JSON keeps strict UTF-8 validation.
This correction does not relabel the original failure as PASS.

The second diagnostic `00038cd8-f0f1-4f39-98c8-94e5784f331c` also remains
FAIL with its original source and raw logs. It returned ten HTTP 200 responses,
matched all four OFF/ON texts and recorded 148 offered / 122 accepted draft
tokens, but the observation parser classified a nonfatal differential template
probe as fatal. The final parser recognizes only the pinned nine-line context
from five lines before that `lstrip` error through three lines after it, retains
it as a warning and still rejects other error/fatal records. The accepted run
uses its own new source/run identity; neither diagnostic was relabeled.

Set `$summaryJson` to the printed `speculation.json` and `$matchingSourceRoot`
to its exact retained source before independent replay:

```powershell
py -3 -B tools/hosted/verify_cellivane_speculation.py $summaryJson --source-root $matchingSourceRoot
```

The [independent verifier](../tools/hosted/verify_cellivane_speculation.py)
does not import runtime or producer validators. Supplying `--source-root`
checks all thirteen source files and the physical model files; omitting it
must not be reported as source-file replay. It also checks prepared assets,
bound plans, HTTP/audit joins, phase order, raw log counters, cleanup and
recomputed timing/text pairs. Exit 0/PASS is recorded-run integrity and
semantic replay, separate from `greedy_equivalence`, observed speculation
and advisory speed ratios. `wall_s` spans the HTTP request, prefill and
generation with verbosity-5 TRACE collection enabled. It is not decoder-only
tokens/second or production speedup. Timed corpus rows exclude warmup;
native offered/accepted totals include warmup. The verifier allows at most
five per-request counter rows per phase and applies conservative total-token
bounds with one final draft-block/EOS allowance per request. Those bounds
are not request-to-native-task attestation. Missing acceptance, unequal text, zero
accepted tokens and a slowdown must retain their respective meanings.
The results and public allowlisted aggregate belong to
[SPECULATION.md](SPECULATION.md). The specified actual run and independent
replay are PASS, with public provenance in the
[allowlisted aggregate](benchmarks/2026-10-12-speculation-summary.json).
The [final local Windows suite](../evidence/full-regression-20261012-final.log)
passed 385 tests with zero skips in 29.307 seconds. Check terminal CI for the
exact published revision separately. The project remains **PARTIAL** for this CPU/model pair, with
`actual_backend_qualification=false`, no endpoint-PID attestation, native MTP,
managed tensor residency or general quality claim. Private raw reports remain
outside publication.

## Validation and publication

Start with pure contract/planner/verifier negative tests, then owned bounded
HTTP/process fixtures, then the explicitly authorized real model experiment.
Run the repository's affected regression suite from the checkout root:

```sh
python -B -m unittest discover -s tools/hosted/tests -p "test_cellivane_*.py" -v
```

Record the actual collected/executed/skipped counts and platform reasons.
Tests do not download or execute a real model. Timeout, unsupported, skipped,
infrastructure failure, unknown completion and runtime failure remain
distinct. A clean process exit and a producer's completed status do not by
themselves satisfy acceptance. Retain raw evidence privately and replay it
using the exact matching source; independent replay checks recorded evidence,
not hostile-process attestation, native execution or model quality.

Update the owning document first and keep README/architecture/upstream
mirrors short. Check relative links, anchors, stale status wording and
`git diff --check`. A content-review date does not refresh runtime evidence.
Document-only changes do not require native QEMU tests. Do not modify the
parent project's resource manifest or rerun its guard when neither changes.

Publish only the intended reviewed files after the relevant local gates.
Use beta first and check terminal CI for the exact published revision;
historical green CI does not cover later source. Moving main requires the
user's separate explicit instruction and the same verified beta revision.

## Historical CI and current-source validation

The following terminal GitHub runs describe their exact source revisions,
not the working tree after them. The jobs use synthetic/loopback/process
fixtures; they do not execute the real model experiments.

| Published revision | Terminal CI evidence | Interpretation |
|---|---|---|
| [5f19faf](https://github.com/tjwlstj/cellivane-seed/commit/5f19faf9479ff487be041d94695d4b78fe07816d) | [Initial public CI](https://github.com/tjwlstj/cellivane-seed/actions/runs/38191543178): Windows and Ubuntu success, 175 collected each; Ubuntu 15 skipped | Original smoke/export only |
| [c22a29d](https://github.com/tjwlstj/cellivane-seed/commit/c22a29d90fcf26a1378ad92c1fb31f044b1a33c1) | [CI 38203515952](https://github.com/tjwlstj/cellivane-seed/actions/runs/38203515952): Windows success; Ubuntu failure | A synthetic lifecycle test entered the Windows-only launcher gate |
| [e763cec](https://github.com/tjwlstj/cellivane-seed/commit/e763cec58686511c35ac1039917902db50f2cc07) | [CI 38203636107](https://github.com/tjwlstj/cellivane-seed/actions/runs/38203636107): both success, 385 collected each; Windows 0 skipped, Ubuntu 15 skipped | Fixture selects the Windows branch with a local mock without changing pathlib's host OS |
| [e32e224](https://github.com/tjwlstj/cellivane-seed/commit/e32e22419836add56b9fc70a6d91223ac90f59c8) | [CI 38333193360](https://github.com/tjwlstj/cellivane-seed/actions/runs/38333193360): both success, 467 collected each; Windows 0 skipped, Ubuntu 15 skipped | First fixed-4B adaptive startup and audit/status slice; not new target profiles |

The c22a29d failure and e763cec fix concern a mocked control-flow test, not
Ubuntu model execution. The local 385-test Windows run and the accepted CPU
speculation report remain their own evidence records. New audit/status or
startup-selector code requires new affected regressions and exact-revision
CI after publication; none of these historical links validates later bytes.
Do not predict a future commit's CI success in its own source documentation.

## Operating and startup-selection follow-up

The results below belong to the published e32e224 source. Its terminal CI
now appears in the historical table above; it does not cover the new target
profile contract in the next subsection.

[OPERATIONS](OPERATIONS.md) owns audit limits, capacity monitoring, deadline
semantics and new-session handling. Session restart does not restore cache
or recover an old audit, and per-session limits do not cap accumulated disk use.
The path-free audit status snapshot is implemented in current source, with
per-session limits, the shared append/snapshot lock and an 80% warning.
Its failure/threshold regressions passed 24 tests, separate from the
historical CI above.

The [adaptive policy](../hosted/experimental/cellivane_seed/adaptive.py) and
[launcher](../tools/hosted/cellivane_seed_adaptive.py) are **CURRENT** as source
implementation, with 24 pure policy fixtures passed. Actual auto baseline and
calibrated draft functional smoke both passed independent recorded-evidence
replay against eighteen runtime/launcher source files and four frozen verifier
files. Their source snapshots remain separate from the historical calibration.
The [allowlisted aggregate](benchmarks/2026-10-13-adaptive-summary.json) owns the
new report/verdict/source hashes and supported claims. Launcher fixtures passed
18 tests; independent replay fixtures passed 35. That e32e224 slice's local Windows suite
passed **467 tests / zero skips / 42.739 seconds**. Its raw regression log stays
private. These results do not establish new Ubuntu actual-model execution or
terminal CI success for a future publication. Check the matching commit in the
[beta workflow](https://github.com/tjwlstj/cellivane-seed/actions/workflows/tests.yml?query=branch%3Abeta).
That snapshot's first supported profile is fixed official Qwen3-4B Q8_0 target with optional
0.6B Q8_0 draft, Windows CPU b11429, context 2048, four threads, one slot,
cache zero and a 128-token HTTP cap. Do not expand this to generic GGUF support.

Independently replay raw calibration and its retained matching source before
choosing the startup mode, then refresh declared RAM admission before one owned
server launch. The auto policy chooses baseline when calibration is absent.
Expected output length is a caller's workload hint. Historical speed ratio,
text equality or speculation counters are advisory evidence for that recorded
model/source configuration, not a current-host guarantee. The pinned backend
does not permit per-request switching. Record fallback to `none`, rejection,
source snapshot, status, cleanup and new independent results explicitly.
[OPERATIONS](OPERATIONS.md#adaptive-startup-selection) owns the runnable
baseline/draft commands and the precise CLI and policy bounds.

Each independently replayed smoke had one warmup plus four computed HTTP
responses, 129-token HTTP rejection before additional dispatch, nineteen audit
records and recorded zero-active owned Job/port cleanup. Calibrated draft had
148 offered / 122 accepted tokens including warmup; uncalibrated baseline had
zero of both. This is functional startup selection, not a new paired-speed
or quality comparison. A supplemental thirty-second serve run completed an
external HTTP request and cleanup, outside the five-request smoke replay
contract. A short-output plan selected baseline without child dispatch.

Use Python CLI as the primary invocation. The optional PowerShell wrapper
passed static AST checks but `-File` was blocked by the test host's existing
execution policy; it is not a tested policy-independent execution path.
Do not change system execution policy or add a blanket bypass to make a
documentation command appear verified.

<a id="explicit-target-profiles-current"></a>

### Explicit target profiles: current verified slice

**Source implementation CURRENT / bounded actual and independent replay PASS / product PARTIAL.**
The launcher has explicit `0.6b`, `1.7b`, `4b` target profiles, default 4B,
and a pinned [profile registry](../hosted/experimental/cellivane_seed/profiles.py).
It reuses the asset downloader's
immutable Q8_0 pins; bind each target profile and optional draft to recorded
artifact metadata and execution identity. Fixed alias `cellivane-spec-target`
must not substitute for that identity. Never automatically downgrade targets.

Only pinned 0.6B may be a draft for the 1.7B/4B targets. A 0.6B target rejects
drafting, including a same-size draft. Keep CPU context/thread/slot/cache/output
bounds unchanged. The calibration loader still replays only the fixed historical
4B/0.6B speculation format. Generation/ingestion of matching 1.7B calibration
is not implemented, so current 1.7B auto stays baseline. Historical 4B benefit
cannot qualify it. Explicit 1.7B draft is experimental opt-in; functional smoke
is separate from repeated/interleaved speed or general equivalence evidence.

The summary is schema 2 with profile/artifact joins and a nineteen-file
source closure. The verifier source preserves old schema-1/eighteen-file replay with its own source
snapshots, hashes and verifier rules. New count/schema support is a contract
change requiring profile-pair/type/identity rejection fixtures, new actual
acceptance and independent replay. The old aggregate remains unchanged; the
new [allowlisted profile aggregate](benchmarks/2026-10-13-profiles-summary.json)
records four actual independent PASS runs against nineteen source files and
four frozen verifier files. The final local Windows suite passed
**505 tests / zero skips / 56.439 seconds**. Historical v1/18-file compatibility
replayed both earlier runs with unchanged saved verdict results. New terminal CI
must still be checked for the exact source after publication. Do not extend
e32e224's 467-test local/CI PASS to changed bytes. Runnable usage contracts are in
[OPERATIONS](OPERATIONS.md#explicit-target-profiles); metadata, actual runs,
regressions and CI remain separate gates. Source checks confirm
path-free profile records in status/CLI ready, plus exact basename/size/hash
enforcement. The optional wrapper adds `-TargetProfile`; its current source
passed static AST checks. Python remains primary: AST success does not establish
execution under the host's PowerShell policy.
Treat `profiles.draft` as candidate artifact metadata, even when selected
mode is baseline. Read `choice.selected_mode` / `adaptive.mode` for the
mechanism rather than claiming every provided draft was loaded.

The four smoke runs covered 0.6B auto baseline, 1.7B auto with mismatched
historical 4B calibration, explicit 1.7B draft, and 4B auto with matching
calibration. Each had one warmup plus four computed responses, one 129-token
HTTP rejection before extra dispatch, nineteen audit records and recorded
owned cleanup. The same-model 0.6B target/draft negative CLI retained its
expected FAIL before inspection/model launch; it is not an actual generation PASS.

Report the 1.7B observation candidly: four texts matched, but the one-pass
four-task HTTP total was 8.152742 seconds baseline versus 11.247127 seconds
draft. Drafting was slower and used a larger owned-primary peak working set.
These sequential single-pass observations are not a repeated/interleaved
performance study or general output/quality proof. OS primary-worker counters
exclude Python, other processes and GPU; they do not establish total RAM use,
reservation or enforced limits. Keep the full measurement scope beside the
[operating results](OPERATIONS.md#profile-smoke-results).

## Upstream and public evidence boundary

Original code is MIT; external binaries and models keep their own licenses.
Project-level license discovery does not complete per-file redistribution
or compatibility review. Source references are not code-import approval.
Keep immutable pins, artifact size/hash, notices and update ownership
explicit; do not silently upgrade a dependency while refreshing prose.

Public documentation may include official source URLs, repository-relative
paths, allowlisted aggregate metrics, run identifiers and content hashes.
Keep credentials, personal absolute paths, process IDs, raw prompts/answers,
environment dumps, engine logs and private raw reports outside publication.
The original raw failures and their matching source snapshots remain private
and unchanged. Native MTP, cross-engine KV sharing, multi-GPU execution,
OS-enforced resource isolation and Kernel Room binding require their own
implementation and evidence; this workflow does not qualify them.
