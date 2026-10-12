# CPU draft/target experiment

Status: **PARTIAL / accepted local experiment with independent replay**.
On 2026-10-12 the fixed Qwen3-4B target and Qwen3-0.6B draft completed a
three-round CPU comparison through the existing Seed HTTP/backend path.
This demonstrates external draft-model speculation for this dense-model
pair. Tensor drafting and target verification belong to pinned llama.cpp;
Cellivane owns the bounded serving, admission, lifetime and evidence path.

The [allowlisted aggregate](benchmarks/2026-10-12-speculation-summary.json)
contains identities, metrics and limits. Raw reports, responses, commands,
local paths, process IDs and logs remain private. See the
[development procedure](DEVELOPMENT_GUIDE.md#cpu-speculation-launcher-and-replay)
for commands and [UPSTREAM](UPSTREAM.md) for official artifacts and licenses.

## Result

| Measurement | Speculation OFF | Draft-simple ON |
|---|---:|---:|
| Scored computed responses | 12/12 | 12/12 |
| Scored completion tokens | 486 | 486 |
| Median request wall time | 4.962071 s | 4.027882 s |
| Owned primary process peak working set, range over phases | 4.3488–4.3489 GiB | 5.2201–5.2203 GiB |
| Draft tokens offered, including separate warmup requests | 0 | 444 |
| Draft tokens accepted, including separate warmup requests | 0 | 366 |

All **12 paired answer texts were byte-identical**. The median of paired
OFF-wall/ON-wall ratios was **1.197386**, approximately **1.20x**. This is a
different statistic from dividing the two aggregate median latencies.
The observed draft acceptance was 366/444, approximately 82.43%, including
warmup. The experiment contains four fixed tasks repeated three times,
not twelve independent task families. It is not a general quality benchmark
or a production speed guarantee.

All six phases completed, with a separate warmup before each: **24 scored
responses plus six warmups**, all computed through one backend exchange each.
The independent verifier returned `PASS`, `greedy_equivalence=PASS` and
`speculation_observed=true`. All owned worker Jobs reported confirmed cleanup,
all local listening ports closed, and source/model/engine artifacts matched
their initial pins at every pre- and post-phase check.

Drafting increased the recorded primary-process working set by about
0.87 GiB. These counters come from the retained owned process handle, cover
that primary process, and are not total machine/Job memory or independent OS
attestation. GPU memory was not measured. The admission plan separately
reserved full target/draft file bytes, 2 GiB declared host overhead and
512 MiB declared context/KV/scratch workspace, while leaving 4 GiB RAM
headroom. Admission estimates are not OS memory limits or measured peaks.

## Fixed protocol and interpretation

- External engine: official llama.cpp CPU archive **b11429**, source
  `d81235049384534c167caea52b85a694f6103d14`, with archive and every member
  verified. Target Qwen3-4B Q8_0 and draft Qwen3-0.6B Q8_0 use the official
  fixed artifact hashes recorded in the aggregate.
- Four CPU threads, one slot, context 2048, maximum 128 completion tokens,
  temperature zero, seed 42, nonstreaming plain answers. Draft-simple uses
  greedy drafting and at most four draft tokens per step.
- Three rounds alternate `none,draft`, `draft,none`, `none,draft`. Each phase
  creates a fresh owned server. Seed result caching and backend prompt reuse
  are disabled; all recorded prompt `cached_tokens` values were zero.
- Model startup and warmup are outside scored wall time. Wall time includes
  Seed HTTP, backend prefill/generation and synchronous evidence writes.
  TRACE verbosity 5 records native counters and adds measurement overhead.
  These values are not decoder-only tokens per second.
- Full model hashing before phases warms the OS file cache. This experiment
  does not measure cold storage loading, storage throughput or weight paging.
- Native log counters include warmup. They are recomputed from raw bytes and
  bounded against phase request/completion budgets, with explicit EOS/final
  draft-block slack. Log task IDs are not authenticated to HTTP request IDs.

The recorded source family contains thirteen explicit runtime/launcher
files. Its canonical manifest digest is
`4d724b194ca1982fa3c35c615d6f6b623ffbcf700f944133fdb2755397c48aa6`.
The full manifest and independent verifier source hashes are in the
aggregate. A matching raw source snapshot is retained privately.

## Diagnostics preserved before acceptance

Two diagnostic failures remain unchanged and are excluded from accepted
metrics:

1. `bfe3c349-8e6c-4b9b-adb9-4139d5ce8a37`: backend
   `--reasoning-format none` left empty template thinking markers in content,
   which the existing Seed output contract rejected. Detailed token logging
   also contained incomplete UTF-8 fragments. The repair uses the backend's
   `auto` parser and a derived UTF-8 replacement view for log analysis.
   HTTP JSON remains strict UTF-8; complete native counter lines must be
   ASCII. No Seed/bridge response-validation relaxation was introduced.
2. `00038cd8-f0f1-4f39-98c8-94e5784f331c`: requests completed, but a recoverable
   differential-template probe was classified as fatal. Pinned upstream
   source confirmed that the reasoning-scope probe catches the exception
   and proceeds to content/tool analysis. The final parser recognizes only
   the exact nine-line `lstrip` diagnostic context as a preserved warning.
   Bare errors, altered/truncated contexts and later fatal errors remain
   failures. This second report was not relabeled PASS.

Raw source snapshots and hashes are preserved for both failures. The final
run uses a new source identity and run ID. Its six phases retained 42 known
template-probe warnings and zero classified worker errors.

## Verification and remaining work

Final accepted run: `fc47eb4a-2298-490b-add2-958a90f6a304`.
Raw report SHA-256:
`33f441f23c9a3ad76a474b37cbc5e04a036c7fbd2af370b7cf909abbf8d4fedf`.
Independent verdict SHA-256:
`c65172bb8f36f0c612a04b57e6559234cde0c29a6b789e6ebd608cc50805a85c`.

The final Windows regression collected and passed **385 tests**, with zero
skips, in 29.307 seconds. It includes pure capability/admission fixtures,
the non-executing plan CLI, owned loopback/lifetime fixtures and independent
wire/audit/log replay negatives. CI runs this fixture suite on Windows and
Ubuntu; inspect the [workflow results](https://github.com/tjwlstj/cellivane-seed/actions/workflows/tests.yml)
for the exact published revision. CI does not execute these real models.

Keep the bridge/Seed contract and explicit admission gate. The next useful
extension is workload-aware selection of an observed backend speculation
path, with opt-in profiles and fallback to ordinary generation when the
supported configuration lacks a demonstrated benefit. That is **PLANNED**.
GPU speculation, other draft/target pairs, model-native MTP, cross-engine KV
sharing, SSD weight/KV prefetch, native tensor kernels and OS-enforced
resource isolation require separate implementation and evidence.
