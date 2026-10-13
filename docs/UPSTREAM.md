# Upstream references and artifact provenance

Checked 2026-10-12. This page separates Cellivane's original source,
external tensor-engine binaries, model weights and architecture references.
Repository licensing does not change the licenses of external artifacts.
No Strata source code or third-party tensor kernels are vendored here.

## Code and dependency licenses

| Material | Relationship to this project | License checked at source |
|---|---|---|
| Cellivane Seed / bridge / local tools | Original implementation distributed in this repository | [MIT](../LICENSE) |
| [Strata v0.1.43](https://github.com/Niko1221/Strata/releases/tag/v0.1.43) | Architecture research only; no imported code, weights or binary | [MIT, source 7695859](https://github.com/Niko1221/Strata/blob/7695859aa13afb248bdb4d36f9b2e95707db079d/LICENSE) |
| [llama.cpp v0.6.0](https://github.com/ggml-org/llama.cpp/releases/tag/v0.6.0) | Reference for CPU/GPU offload and current backend controls | [MIT, source d812350](https://github.com/ggml-org/llama.cpp/blob/d81235049384534c167caea52b85a694f6103d14/LICENSE) |
| [llamafile 0.10.5](https://github.com/mozilla-ai/llamafile/releases/tag/0.10.5) | External pinned CPU tensor worker in the original smoke; not bundled | [Apache-2.0 project license](https://github.com/mozilla-ai/llamafile/blob/0.10.5/LICENSE); underlying components retain their notices |
| [Ollama v0.40.2](https://github.com/ollama/ollama/releases/tag/v0.40.2) | External local subengine / dense-model backend; not bundled | [MIT, source b061384](https://github.com/ollama/ollama/blob/b061384d90ff455462bc32745dd0de479de717a3/LICENSE) |
| Official Qwen3 GGUF weights below | Separate external model artifacts; not repository source or release assets | Official model-card metadata: `apache-2.0` |

These are directly inspected project licenses or model metadata, not a
complete per-file legal/redistribution audit of binary packages and all
transitive components. An upstream tag or moving documentation URL can
change; immutable source and model revisions below identify this research.

## Strata: useful reference, different model contract

The current research pin is
`7695859aa13afb248bdb4d36f9b2e95707db079d` (v0.1.43). The older
`fb58e0dbc8399662c0e47c76578c6e878b14f6cf` (v0.1.41) identifies earlier
local research and must not be relabeled as current-source evidence.

The [pinned README](https://github.com/Niko1221/Strata/blob/7695859aa13afb248bdb4d36f9b2e95707db079d/README.md)
describes a Qwen3.8-Flash-Next 125B model path with 12 GB or more VRAM,
32 GB RAM minimum and roughly 80 GB storage. The
[installation guide](https://github.com/Niko1221/Strata/blob/7695859aa13afb248bdb4d36f9b2e95707db079d/docs/INSTALL.md)
also discusses an 8 GB slow path. Those statements are not an 8 GB hard
impossibility proof. The stronger tiny-model limitation is architecture:
the [model layout](https://github.com/Niko1221/Strata/blob/7695859aa13afb248bdb4d36f9b2e95707db079d/include/strata/core/layout.hpp)
and [inspector](https://github.com/Niko1221/Strata/blob/7695859aa13afb248bdb4d36f9b2e95707db079d/tools/strata_inspect.py)
target the model-specific `qwen4exp` MoE layout, not a general `qwen3`
dense GGUF such as 0.6B, 1.7B or 4B.

[Multi-GPU design](https://github.com/Niko1221/Strata/blob/7695859aa13afb248bdb4d36f9b2e95707db079d/docs/MULTI_GPU.md):
contiguous layer stages, per-stage expert caches and a shared host expert
arena. CPU workers calculate experts absent from the GPU caches. Pinned
host-memory handoffs do not require NVLink; this is layer/pipeline
parallelism, not tensor parallelism. Optional stage trimming changes dense
weight residency. Windows/WDDM's documented 8 GiB pinned expert-arena cap
is a host-memory rule, not an 8 GB VRAM qualification result. An added slow
GPU may reduce throughput. These are upstream claims, not Cellivane tests.

The [secondary-GPU expert cache](https://github.com/Niko1221/Strata/blob/7695859aa13afb248bdb4d36f9b2e95707db079d/docs/SECOND_GPU.md)
is another model-specific ownership scheme. Optional token MTP in
[DETAILS.md](https://github.com/Niko1221/Strata/blob/7695859aa13afb248bdb4d36f9b2e95707db079d/docs/DETAILS.md)
is not Seed's exact-result cache or in-flight coalescing. Cellivane adopts
the general idea of explicit responsibility/placement reporting, while
leaving tensor placement to an established dense-model backend.

Three upstream mechanisms need separate names. Strata's
[MTP contract](https://github.com/Niko1221/Strata/blob/7695859aa13afb248bdb4d36f9b2e95707db079d/include/strata/core/mtp.hpp#L3-L17)
drafts future token candidates for target verification. Its
[routing-aware prefetch contract](https://github.com/Niko1221/Strata/blob/7695859aa13afb248bdb4d36f9b2e95707db079d/include/strata/core/expert_source.hpp#L240-L262)
predicts expert identifiers to prepare data before a layer needs it. Its
[quantized formats](https://github.com/Niko1221/Strata/blob/7695859aa13afb248bdb4d36f9b2e95707db079d/include/strata/artifact/dequant.hpp#L68-L94)
define how stored low-bit codes and scales are interpreted for computation.
MTP does not predict decompressed model weights. Grouped token verification
can share work or amortize expert movement; that connection does not make
token speculation an SSD decompressor. Quantized MMQ may compute without
expanding a whole weight matrix to FP16, as described in the pinned
[DETAILS.md](https://github.com/Niko1221/Strata/blob/7695859aa13afb248bdb4d36f9b2e95707db079d/docs/DETAILS.md).

Cellivane's new contract/budget and actual speculation experiment follow the
[development guide](DEVELOPMENT_GUIDE.md). The pure declared-budget planner
is `PARTIAL`; the specified CPU OFF/ON run and its independent recorded-evidence
replay passed, and that historical slice's local Windows suite passed 385 tests with zero
skips in 29.307 seconds. Publication CI must be checked for the exact published
revision, independently of those local results.
Reading these upstream mechanisms does not import code or qualify a backend;
the local result does not establish general performance support.

## CPU/GPU backend references

The [llama.cpp server controls at d812350](https://github.com/ggml-org/llama.cpp/blob/d81235049384534c167caea52b85a694f6103d14/tools/server/README.md)
document GPU-layer offload, device selection and `none`, `layer`, `row`
and experimental `tensor` split modes. `--tensor-split` is a ratio among
GPUs; it is not a CPU/GPU percentage. The
[build guide](https://github.com/ggml-org/llama.cpp/blob/d81235049384534c167caea52b85a694f6103d14/docs/build.md)
documents Windows/CUDA builds and distinguishes `--device none` from
merely requesting zero GPU layers. The stable v0.6.0 release points to
[nightly b11429](https://github.com/ggml-org/llama.cpp/releases/tag/b11429);
the latest nightly observed during this review was
[b11559](https://github.com/ggml-org/llama.cpp/releases/tag/b11559).
The first review installed neither binary. The new experiment selects the
b11429 Windows CPU archive through the stable v0.6.0 pointer and the
[pinned asset helper](../tools/hosted/cellivane_llama_assets.py); b11559 is
only a research reference. Preparing verified bytes is not actual-model
qualification. The new OFF/ON path has two preserved failed diagnostics and a
separate accepted repeat-three CPU run with independent source-checked replay.

| New experimental binary | Official fixed distribution | Bytes | SHA-256 |
|---|---|---:|---|
| llama.cpp b11429 Windows CPU x64 | [ZIP](https://github.com/ggml-org/llama.cpp/releases/download/b11429/llama-b11429-bin-win-cpu-x64.zip) | 19398918 | `1283323272b04cd07905816a597a0da810918102de958f4ff6f7bbaa70ed2efe` |

The source commit is `d81235049384534c167caea52b85a694f6103d14`.
The helper also pins release/asset identity, the stable pointer content
hash, and every extracted binary/member; extra or changed files fail closed.
No upstream source is copied into the repository. Archive bytes are not
RAM/VRAM requirements; binary integrity does not attest an HTTP endpoint.

The [CPU experiment](../tools/hosted/cellivane_seed_speculate.py) checks that
this binary exposes `none` and `draft-simple`, greedy draft controls and the
required no-prompt-cache/reasoning flags. It uses the official 4B target and
0.6B draft pins below. This is an external draft-model mechanism, distinct
from Strata's model-native MTP and expert I/O prefetch. Current usage and
failure/acceptance boundaries are in the
[development guide](DEVELOPMENT_GUIDE.md#cpu-speculation-launcher-and-replay);
[SPECULATION.md](SPECULATION.md) owns the paired metrics: all twelve text pairs
matched, and draft acceptance was observed in the specified CPU/model run.
A flag declaration or downloaded binary alone does not establish acceptance.

[llamafile 0.10.6](https://github.com/mozilla-ai/llamafile/releases/tag/0.10.6)
was the newer release observed during review; it does not replace the
original smoke's 0.10.5 pin. Its
[hardware support documentation](https://docs.mozilla.ai/llamafile/reference/support)
describes Windows NVIDIA support, explicit GPU selection, CPU fallback and
best-effort platform coverage. A loaded CUDA library or requested offload
flag alone is not a completed GPU inference test. The
[pinned README](https://github.com/mozilla-ai/llamafile/blob/017cdeb4b7217c0fb44cc7b61f44bd8ab2749dd2/README.md)
notes Windows' executable-size restriction; using a thin executable with
an external GGUF avoids packaging large weights into that executable.

Ollama's [GGUF import contract](https://docs.ollama.com/modelfile),
[chat API](https://docs.ollama.com/api/chat),
[loaded-model API](https://docs.ollama.com/api/ps),
[CPU/GPU placement reporting](https://docs.ollama.com/faq#how-can-i-tell-if-my-model-was-loaded-onto-the-gpu)
and [hardware support](https://docs.ollama.com/gpu) inform the new dense
experiment. These moving docs may describe features newer than 0.40.2;
actual pinned-run responses/logs remain authoritative for compatibility.
Chat streaming defaults on upstream; Cellivane explicitly requests a
nonstreaming, no-thinking plain answer. A zero keep-alive unload is not
request cancellation. `size_vram`, model digest and native offload logs
are observations with distinct meanings.

NVIDIA lists RTX 5050 as compute capability 12.0 in its
[official GPU table](https://developer.nvidia.com/cuda/gpus).
The [Blackwell compatibility guide](https://docs.nvidia.com/cuda/blackwell-compatibility-guide/index.html)
explains cubin/PTX compatibility. A CUDA toolkit version alone does not
prove this exact binary works on this GPU. Record driver, loaded backend,
actual layers and a completed generation. With one installed GPU,
multi-GPU execution remains `NOT_RUN`.

## Immutable official Qwen3 model artifacts

These are the official Qwen organization repositories. Revision identifies
the repository snapshot; SHA-256 is the LFS file's content digest, not a
Git blob ID or Xet storage hash. Byte size is file size, not total RAM/VRAM
required by weights, KV cache, context buffers and runtime overhead.

| Model / quantization | Official fixed artifact | Bytes | GiB | SHA-256 |
|---|---|---:|---:|---|
| Qwen3 0.6B Q8_0 | [GGUF](https://huggingface.co/Qwen/Qwen3-0.6B-GGUF/resolve/23749fefcc72300e3a2ad315e1317431b06b590a/Qwen3-0.6B-Q8_0.gguf) | 639446688 | 0.596 | `9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031` |
| Qwen3 1.7B Q8_0 | [GGUF](https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/resolve/90862c4b9d2787eaed51d12237eafdfe7c5f6077/Qwen3-1.7B-Q8_0.gguf) | 1834426016 | 1.708 | `061b54daade076b5d3362dac252678d17da8c68f07560be70818cace6590cb1a` |
| Qwen3 4B Q8_0 | [GGUF](https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/bc640142c66e1fdd12af0bd68f40445458f3869b/Qwen3-4B-Q8_0.gguf) | 4280404704 | 3.986 | `8c2f07f26af9747e41988551106f149b03eb9b5cb6df636027b6bf6278473300` |
| Qwen3 8B Q8_0, optional future CPU/partial-offload axis | [GGUF](https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/7c41481f57cb95916b40956ab2f0b139b296d974/Qwen3-8B-Q8_0.gguf) | 8709518112 | 8.111 | `408b955510e196121c1c375201744783b5c9a43c7956d73fc78df54c66e883d6` |
| Qwen3 8B Q4_K_M, optional separate quantization axis | [GGUF](https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/7c41481f57cb95916b40956ab2f0b139b296d974/Qwen3-8B-Q4_K_M.gguf) | 5027783488 | 4.682 | `d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785` |

The pinned model cards for
[0.6B](https://huggingface.co/Qwen/Qwen3-0.6B-GGUF/blob/23749fefcc72300e3a2ad315e1317431b06b590a/README.md),
[1.7B](https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/blob/90862c4b9d2787eaed51d12237eafdfe7c5f6077/README.md),
[4B](https://huggingface.co/Qwen/Qwen3-4B-GGUF/blob/bc640142c66e1fdd12af0bd68f40445458f3869b/README.md)
and [8B](https://huggingface.co/Qwen/Qwen3-8B-GGUF/blob/7c41481f57cb95916b40956ab2f0b139b296d974/README.md)
declare Apache-2.0 metadata. Model source/weight terms remain separate from
Cellivane's MIT code. The 8B Q8_0 file alone exceeds 8 GiB; do not promise
full residency on an 8 GB GPU. Partial offload or CPU execution is a
different question. An 8B Q4 comparison also changes quantization, so it
cannot isolate parameter count by itself.

## Reproduce artifact acquisition without running a model

The [asset tool](../tools/hosted/cellivane_seed_assets.py) only accepts the
three pinned Q8_0 rows 0.6B/1.7B/4B. It reads no token, requires no model
account, uses no arbitrary URL, verifies exact byte count and streaming
SHA-256, and publishes without replacing an existing artifact. Failed
exclusive `.part` files and mismatched pre-existing files are preserved.
Receipts go under the selected storage root's `downloads/` directory.
File fsync is not a power-loss qualification. The destination must support
same-volume hard links; failure preserves the verified partial for review.

```powershell
$modelRoot = Join-Path $env:LOCALAPPDATA 'Cellivane/seed-models'
py -3 -B tools/hosted/cellivane_seed_assets.py --model 1.7b --storage-root $modelRoot
py -3 -B tools/hosted/cellivane_seed_assets.py --model 4b --storage-root $modelRoot
```

Set `$modelRoot` to another local fixed disk when needed, including D:.
Asset download/storage and engine preparation are explicit operations;
[OPERATIONS](OPERATIONS.md) explains private evidence and accumulated disk use.

Downloading and verifying weights does not execute or qualify them. This
tool does not install llamafile, Ollama, CUDA or any driver. The older
owned smoke still needs its separately verified 0.6B/llamafile/Ollama cache
described in the [repository README](../README.md). New comparison runs
take that verified engine cache as `--asset-root` and the new GGUF as
`--model-file`; the two storage roots have different roles.

For current responsibility and maturity boundaries, see
[ARCHITECTURE.md](ARCHITECTURE.md).

The e32e224 adaptive startup path used only the existing b11429 CPU
binary and official 4B target / optional 0.6B draft pins above. No new
upstream code or artifact pin is introduced. Auto baseline and calibrated
draft functional smoke and matching-source independent replay passed. The
historical speculation result is advisory calibration,
not current-host qualification. The [new aggregate](benchmarks/2026-10-13-adaptive-summary.json)
records the eighteen runtime/launcher and four verifier hashes separately.
See [OPERATIONS](OPERATIONS.md#adaptive-startup-selection).

The explicit-target follow-up is **source CURRENT / bounded actual and replay PASS / product PARTIAL**.
It reuses the same official 0.6B/1.7B/4B Q8_0 pins already catalogued above
and the same b11429 CPU binary. No new upstream artifact or code import is
introduced for these profiles. Only 1.7B/4B targets may pair with pinned 0.6B
draft; a fixed HTTP alias is not model identity. Source/schema joins and new
actual evidence are separate from acquisition and the historical 4B result.
The calibration loader still supports only that fixed historical 4B/0.6B
speculation format, not matching 1.7B calibration generation/ingestion.
The [profile aggregate](benchmarks/2026-10-13-profiles-summary.json) records
four matching-source functional smoke/replay PASS results, not general model
qualification or improved performance. Its 1.7B explicit draft was slower
than baseline on the recorded sequential four-task smoke despite matching text.
See [profile usage](OPERATIONS.md#explicit-target-profiles).
