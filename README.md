# Cellivane Seed · 셀리베인 시드

**A small local serving layer with bounded admission, exact-result caching,
in-flight request coalescing, and independent evidence replay.**

셀리베인 시드는 로컬 모델 서버 앞에서 요청 수를 제한하고, 같은 요청의 완료
결과를 재사용하며, 동시에 들어온 동일 요청을 하나의 backend 호출로 합칩니다.
Python 표준 라이브러리로 구현한 실험용 프로젝트입니다. 텐서 계산은 별도
backend가 담당합니다.

This standalone export originated in the AIOS/Cellivane experimental hosted
tree. It is not the native kernel, a Strata fork, or an independent tensor
inference implementation. Model weights and engine binaries are not bundled.

Read the [architecture](docs/ARCHITECTURE.md),
[upstream references and model hashes](docs/UPSTREAM.md), and
[bounded model/placement comparison](docs/BENCHMARKS.md).
The [development guide](docs/DEVELOPMENT_GUIDE.md) owns the workflow for
contracts, resource budgets and new actual-run acceptance.

## What is implemented / 구현 범위

- Loopback-only, nonstreaming OpenAI subset: `/health`, `/v1/models`,
  `/v1/status`, and `/v1/chat/completions`.
- Fixed compute slots, bounded in-flight joiners and HTTP connections, an
  exact-result LRU cache, and fail-closed persistence errors.
- Raw backend wire evidence, hash-chained local audit records, and separate
  stdlib verifiers for Seed smoke and reference/subengine bridge journals.
- An owned Windows CPU lab using pinned llamafile and an isolated Ollama
  candidate. Process cleanup uses retained handles and Windows Job Objects.
- An owned Windows Ollama comparison path with `cpu`, `hybrid`, and `auto`
  placement plans, separate observed-layer evidence, and a fixed small corpus.

No native token MTP, training, cross-process KV sharing, tool calls, streaming, or
automatic native Kernel Room binding is implemented. Cache/coalescing avoids
duplicate requests; it is not speculative token decoding. CPU and one-GPU
placement were observed in bounded local runs. General GPU qualification and
cross-hardware performance support need separate measurements.

## Requirements and tests / 준비와 테스트

Use CPython 3.11. No `pip install` is needed for this source checkout.
Run commands from the repository root:

```sh
python -B -m unittest discover -s tools/hosted/tests -p "test_cellivane_*.py" -v
```

On Windows, `py -3` may replace `python`; on Linux, use `python3` if needed.
These tests use synthetic data, owned loopback HTTP servers and Python-only
process fixtures. They do not download or execute a model. Windows Job and
archive tests are skipped on other systems. The initial public beta source
[5f19faf](https://github.com/tjwlstj/cellivane-seed/commit/5f19faf9479ff487be041d94695d4b78fe07816d)
passed [Windows and Ubuntu CI](https://github.com/tjwlstj/cellivane-seed/actions/runs/38191543178):
175 tests on each platform, with 15 platform skips on Ubuntu. That historical
run does not validate the newer comparison/profile additions; their final CI
must be checked at the corresponding published source revision.

The `59c9bcd` comparison/profile snapshot passed its final local Windows suite:
280 tests, zero skips, 28.110 seconds, including 45 comparison-verifier
positive/negative fixtures. Actual model results and their separate replay
are described in [BENCHMARKS.md](docs/BENCHMARKS.md).

The newer contract/budget/speculation slice passed 385 local Windows tests,
zero skips, in 29.307 seconds. Check terminal fixture-workflow results for the
exact published revision; local regression and actual-run replay are separate
from GitHub CI.

## Attach a local backend / 기존 로컬 backend 연결

Start a backend separately on `127.0.0.1`. It must expose JSON `/health`,
`/v1/models` with the exact configured model alias, and nonstreaming
`/v1/chat/completions`. Successful answers require a single assistant choice,
`finish_reason: "stop"`, and valid prompt/completion/total token counts.
Truncated, unsupported or uncertain backend results are rejected.

PowerShell:

```powershell
$env:PYTHONPATH = Join-Path $PWD 'hosted/experimental'
py -3 -B -m cellivane_seed --backend-url http://127.0.0.1:8080/v1 --model my-local-model --identity local-model-v1 --storage-root "$env:LOCALAPPDATA/Cellivane/attached"
```

Linux/macOS shell (attached mode; the owned lab is Windows only):

```sh
PYTHONPATH=hosted/experimental python3 -B -m cellivane_seed \
  --backend-url http://127.0.0.1:8080/v1 --model my-local-model \
  --identity local-model-v1 --storage-root "$HOME/.local/share/Cellivane/attached"
```

The Seed endpoint defaults to `http://127.0.0.1:18941/v1`. Use the same alias
(`my-local-model` in the example) when sending requests. `--identity` labels
the configured backend/cache namespace; it is not an attestation. Backend
discovery confirms HTTP metadata only. `/health` reports serving admission,
not ongoing model or hardware qualification.

## Owned Windows CPU lab / 소형 모델 실험

The lab uses llamafile 0.10.5 thin and Qwen3-0.6B Q8_0. Prepare the following
files in an asset cache of your choice, using the upstream distributions:

| File | Required SHA-256 |
|---|---|
| `llamafile-0.10.5-thin.exe` | `55c69c1be9d6ad2172e2d1c0acc677a60ea8ff60232009a8c8170f5bcb917611` |
| `Qwen3-0.6B-Q8_0.gguf` | `9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031` |

Upstream sources: [llamafile 0.10.5](https://github.com/mozilla-ai/llamafile/releases/tag/0.10.5),
[ggml-org Qwen3-0.6B GGUF](https://huggingface.co/ggml-org/Qwen3-0.6B-GGUF),
and [Ollama 0.40.2](https://github.com/ollama/ollama/releases/tag/v0.40.2).
The lab checks asset hashes and rejects changed cached files.

```powershell
$cache = Join-Path $env:LOCALAPPDATA 'Cellivane/asset-cache'
$assets = Join-Path $env:LOCALAPPDATA 'Cellivane/bridge-lab'
py -3 -B tools/hosted/cellivane_bridge_lab.py --profile subengine-smoke --prepare-only --asset-cache $cache --storage-root $assets
powershell -File tools/hosted/Start-CellivaneSeed.ps1 -Mode smoke -AssetRoot $assets -AssetCache $cache
```

Preparation copies verified model/binary files into the chosen local asset
root. If absent, it also downloads the pinned Ollama Windows archive
(1,468,085,195 bytes) and validates all extracted members. This is an explicit
asset preparation step, not part of the unit tests. The smoke starts owned
CPU workers and compares a Seed reference with an Ollama candidate; it does
not establish improved answer quality or general GPU support.

Run `-Mode serve` instead of `smoke` to keep Seed serving with its owned
llamafile worker. Defaults use `%LOCALAPPDATA%/Cellivane/{seed-lab,bridge-lab,asset-cache}`.
Override `-StorageRoot`, `-AssetRoot` and `-AssetCache` to use another local
fixed disk. UNC/network and reparse-point storage are rejected by the Windows
lab. The retained smoke verifier expects the default ports 18941, 18942 and
18932; other profiles require their own validation.

## Compare model size and placement / 모델 크기·배치 비교

The separate comparison path uses pinned Ollama v0.40.2 and official dense
Qwen3 Q8_0 files. The asset downloader only obtains/verifies GGUF bytes; it
does not install an engine or run a model. Keep the existing verified engine
cache (`--asset-root`), model directory (`--model-file`) and private output
directory (`--storage-root`) distinct. Example local D: paths below can be
changed to another supported local fixed disk:

```powershell
$modelRoot = 'D:/AIOS/cellivane-seed-models-20261012'
$engineAssets = 'D:/AIOS/cellivane-bridge-20261009'
py -3 -B tools/hosted/cellivane_seed_assets.py --model 1.7b --storage-root $modelRoot
py -3 -B tools/hosted/cellivane_seed_assets.py --model 4b --storage-root $modelRoot
py -3 -B tools/hosted/cellivane_seed_compare.py --model 1.7b --model-file "$modelRoot/models/Qwen3-1.7B-Q8_0.gguf" --asset-root $engineAssets --storage-root $modelRoot --profile cpu --repeat 2
py -3 -B tools/hosted/cellivane_seed_compare.py --model 4b --model-file "$modelRoot/models/Qwen3-4B-Q8_0.gguf" --asset-root $engineAssets --storage-root $modelRoot --profile hybrid --layers 14 --repeat 2
py -3 -B tools/hosted/cellivane_seed_compare.py --model 1.7b --model-file "$modelRoot/models/Qwen3-1.7B-Q8_0.gguf" --asset-root $engineAssets --storage-root $modelRoot --profile auto --repeat 2
```

The engine cache still needs the separately verified original 0.6B/llamafile
assets described above; the comparison reuses the lab's complete engine-cache
validation. `--model 0.6b` is also available in the downloader and comparison
tool. CPU requests zero GPU layers; hybrid requests an explicit count; auto
lets the backend choose. A requested profile is not proof of actual offload.
The comparison executes models, starts owned processes and stores private
raw evidence. Read `acceptance`, observed placement, source/model stability
and cleanup results in the printed report.

Five accepted local runs used twelve fixed exact-format cases twice, with
temperature 0, maximum 64 tokens, context 2048 and no Seed result cache.
CPU scores were 6/24 for 0.6B, 14/24 for 1.7B and 22/24 for 4B. The same
scores held for the tested 1.7B auto and 4B hybrid runs, which observed
29/29 and 14/37 GPU layers respectively on one RTX 5050. All five exercised
Seed-to-bridge compatibility and passed separate recorded-evidence replay.
This small corpus is not a general accuracy benchmark, and backend-local
prompt/KV reuse may affect timing. See [BENCHMARKS.md](docs/BENCHMARKS.md)
and its [allowlisted aggregate JSON](docs/benchmarks/2026-10-12-summary.json)
for timing scope, provenance, run hashes and limits. Raw prompts, answers,
logs, local paths and process IDs are kept outside the public repository.
Those five runs belong to the
[59c9bcd source snapshot](https://github.com/tjwlstj/cellivane-seed/commit/59c9bcd).
The current comparison launcher now declares an explicit sixteen-file source
closure; its changed bytes and the new planner need their own validation.

## Current development slice / 현재 고도화 방향

The capability/resource planner and the non-executing
[plan CLI](tools/hosted/cellivane_seed_plan.py) are **PARTIAL**: 26 pure
contract/budget fixtures and six CLI fixtures passed. Caller-supplied byte
budgets and workspace estimates are mandatory; absent GPU estimates or
required observations stay `UNKNOWN`. `ADMITTED` is declared preflight,
not measured memory fit or engine qualification. The specified CPU speculative
OFF/ON experiment and its independent recorded-evidence replay passed, along
with the 385-test local Windows suite described in the
[development guide](docs/DEVELOPMENT_GUIDE.md#current-slice-contract-budget-and-speculation).
Existing model/placement results remain evidence for their original source.
Token speculation, router prefetch and quantized computation are separate
mechanisms; result caching is not any of those three.

The separate [CPU speculation launcher](tools/hosted/cellivane_seed_speculate.py)
uses the pinned llama.cpp b11429 Windows CPU binary, official Qwen3-4B Q8_0
target and Qwen3-0.6B Q8_0 draft. It alternates fresh-server `none` and
`draft-simple` phases with greedy draft sampling, no Seed result cache and
disabled backend prompt reuse. It requires explicit local asset/storage
roots and both model files. The accepted repeat-three run completed six phases,
24 timed responses and six warmups. All 12 OFF/ON text pairs matched; independent
replay passed and observed 444 offered / 366 accepted draft tokens including
warmup. The median per-pair wall-time ratio was 1.1974 (OFF/ON), an advisory
small-corpus result under TRACE collection. Both earlier failed diagnostics
remain preserved. Check terminal CI for the exact published revision. Use the
[launcher and verifier instructions](docs/DEVELOPMENT_GUIDE.md#cpu-speculation-launcher-and-replay)
and [SPECULATION.md](docs/SPECULATION.md) for this experiment. This backend
draft-model path does not implement Strata's native MTP, establish answer
correctness or promise production speedup.

## Evidence and limits / 증거와 한계

Use a summary path printed by the smoke:

```sh
python -B tools/hosted/verify_cellivane_seed.py /absolute/path/to/lab-summary.json
python -B tools/hosted/verify_cellivane_bridge.py --run-directory /absolute/path/to/bridge/run
python -B tools/hosted/verify_cellivane_comparison.py /absolute/path/to/comparison.json --source-root /absolute/path/to/matching-source
```

The verifiers do not import runtime validators. They check recorded integrity
and semantic joins. Unkeyed hashes and cleanup receipts do not independently
authenticate an engine, prove OS process liveness, or prove model quality.
Changing sources creates a new evidence boundary; an earlier PASS does not
validate later code.

Comparison replay additionally checks the requested plan/configuration
identity, raw backend joins, corpus scores/timings, observed placement and
the separate bridge journal. Use the exact source tree matching the report.
It validates recorded evidence rather than rerunning a model or proving
which process owned an HTTP endpoint.

Audit files and engine logs can contain prompts, responses and absolute local
paths. Keep them private and outside Git. The included `.gitignore` excludes
local assets, generated evidence and secrets; it intentionally keeps source
and configuration JSON visible. Closing a client connection does not confirm
backend cancellation, process shutdown or KV release.

## License

This project's source is available under the [MIT License](LICENSE),
copyright © 2026 tjwlstj. External engines, model weights and other upstream
artifacts retain their own licenses and distribution terms.
