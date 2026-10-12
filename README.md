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

## What is implemented / 구현 범위

- Loopback-only, nonstreaming OpenAI subset: `/health`, `/v1/models`,
  `/v1/status`, and `/v1/chat/completions`.
- Fixed compute slots, bounded in-flight joiners and HTTP connections, an
  exact-result LRU cache, and fail-closed persistence errors.
- Raw backend wire evidence, hash-chained local audit records, and separate
  stdlib verifiers for Seed smoke and reference/subengine bridge journals.
- An owned Windows CPU lab using pinned llamafile and an isolated Ollama
  candidate. Process cleanup uses retained handles and Windows Job Objects.

No token MTP, training, cross-process KV sharing, tool calls, streaming, or
automatic native Kernel Room binding is implemented. Cache/coalescing avoids
duplicate requests; it is not speculative token decoding. General GPU and
cross-hardware performance support needs separate measurements.

## Requirements and tests / 준비와 테스트

Use CPython 3.11. No `pip install` is needed for this source checkout.
Run commands from the repository root:

```sh
python -B -m unittest discover -s tools/hosted/tests -p "test_cellivane_*.py" -v
```

On Windows, `py -3` may replace `python`; on Linux, use `python3` if needed.
These tests use synthetic data, owned loopback HTTP servers and Python-only
process fixtures. They do not download or execute a model. Windows Job and
archive tests are skipped on other systems. CI is configured for Windows and
Ubuntu with Python 3.11; a configured workflow is not a completed CI result.

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

## Evidence and limits / 증거와 한계

Use a summary path printed by the smoke:

```sh
python -B tools/hosted/verify_cellivane_seed.py /absolute/path/to/lab-summary.json
python -B tools/hosted/verify_cellivane_bridge.py --run-directory /absolute/path/to/bridge/run
```

The verifiers do not import runtime validators. They check recorded integrity
and semantic joins. Unkeyed hashes and cleanup receipts do not independently
authenticate an engine, prove OS process liveness, or prove model quality.
Changing sources creates a new evidence boundary; an earlier PASS does not
validate later code.

Audit files and engine logs can contain prompts, responses and absolute local
paths. Keep them private and outside Git. The included `.gitignore` excludes
local assets, generated evidence and secrets; it intentionally keeps source
and configuration JSON visible. Closing a client connection does not confirm
backend cancellation, process shutdown or KV release.

## License

This project's source is available under the [MIT License](LICENSE),
copyright © 2026 tjwlstj. External engines, model weights and other upstream
artifacts retain their own licenses and distribution terms.
