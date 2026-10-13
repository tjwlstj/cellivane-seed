# Cellivane Seed · 셀리베인 시드

셀리베인 시드는 로컬 모델 서버 앞에서 요청 수를 제한하고, 같은 요청의 완료
결과를 재사용하며, 동시에 들어온 동일 요청을 하나의 backend 호출로 합치는
실험용 서빙 계층입니다. Python 표준 라이브러리로 구현했으며 텐서 계산은
별도 엔진이 담당합니다. 상태는 **PARTIAL / experimental**입니다.

독립 AIOS/Cellivane 실험 트리에서 출발했습니다. 모델 가중치와 엔진 바이너리는
포함하지 않으며, native 커널·Strata fork·독립 텐서 추론 구현으로 배포하지 않습니다.

- [운영 가이드](docs/OPERATIONS.md): 세션 한도, 상태 확인, 중단과 재시작
- [구조](docs/ARCHITECTURE.md) · [외부 출처·모델 해시·라이선스](docs/UPSTREAM.md)
- [모델 크기·CPU/GPU 배치 비교](docs/BENCHMARKS.md) · [CPU draft/target 실험](docs/SPECULATION.md)
- [개발·검증 가이드](docs/DEVELOPMENT_GUIDE.md): 계약, 예산, 독립 재판독, CI 이력

## 현재 구현

- Loopback 전용 비스트리밍 OpenAI subset: `/health`, `/v1/models`,
  `/v1/status`, `/v1/chat/completions`.
- 기본 compute slot 1개, 동일 요청 waiter 최대 8개, 완료 결과 LRU 최대
  32개/1 MiB. 다른 요청의 대기열은 없으며 사용 중이면 HTTP 429를 반환합니다.
- backend 원시 응답 증거, 세션별 hash-chain audit, 별도 stdlib verifier.
- pinned 엔진을 쓰는 Windows CPU lab과 Ollama `cpu`/`hybrid`/`auto` 비교 경로.
  프로세스 정리는 retained handle과 Windows Job Object를 사용합니다.

결과 캐시·요청 병합과 token speculation은 별개입니다. Native MTP, 학습,
엔진 간 KV 공유, 도구 호출, 스트리밍, native Kernel Room 권한 연결은 없습니다.
CPU 및 단일 GPU의 제한된 실측은 아래 각 실험의 범위로 해석해야 합니다.

## 준비와 fixture 테스트

CPython 3.11을 사용합니다. 저장소 소스에는 `pip install`이 필요하지 않습니다.
모델 실행에는 별도로 준비한 외부 엔진·가중치가 필요합니다. 저장소 루트에서:

```sh
python -B -m unittest discover -s tools/hosted/tests -p "test_cellivane_*.py" -v
```

Windows에서는 `py -3`, Linux에서는 필요하면 `python3`을 사용합니다.
테스트는 synthetic data와 소유한 loopback/process fixture를 쓰며 모델을 다운로드하거나
실행하지 않습니다. Windows Job/archive 관련 테스트는 다른 OS에서 건너뜁니다.

이 변경 이전의 [e763cec](https://github.com/tjwlstj/cellivane-seed/commit/e763cec58686511c35ac1039917902db50f2cc07)은
[Windows·Ubuntu CI](https://github.com/tjwlstj/cellivane-seed/actions/runs/38203636107)를
통과했습니다. 각각 385개를 수집했고 Ubuntu는 15개를 건너뛰었습니다.
이 역사적 결과는 이후 운영·선택 기능 변경을 검증하지 않습니다. 최신 소스는
해당 revision의 실제 회귀 결과와 terminal CI를 따로 확인해야 합니다.

[e32e224 snapshot](https://github.com/tjwlstj/cellivane-seed/commit/e32e22419836add56b9fc70a6d91223ac90f59c8)의
운영·adaptive 로컬 Windows 회귀는 **467 PASS / 0 skip / 42.739초**입니다.
[해당 revision의 CI](https://github.com/tjwlstj/cellivane-seed/actions/runs/38333193360)도
Windows·Ubuntu 모두 467개를 수집해 통과했고 Ubuntu는 15개를 건너뛰었습니다.
이 이력은 아래 새 target-profile 변경을 검증하지 않습니다.
새 profile 변경의 로컬 Windows 전체 회귀는 **505 PASS / 0 skip / 56.439초**입니다.
새 공개 revision의 Ubuntu·Windows CI는
[beta fixture workflow](https://github.com/tjwlstj/cellivane-seed/actions/workflows/tests.yml?query=branch%3Abeta)에서
해당 commit과 terminal 결과를 확인하세요. 로컬 PASS를 새 CI 성공으로 표시하지 않습니다.

## 기존 로컬 backend 연결

backend를 `127.0.0.1`에서 별도로 시작합니다. JSON `/health`, 정확한 model alias를
내보내는 `/v1/models`, 비스트리밍 `/v1/chat/completions`가 필요합니다.
완료 응답은 assistant choice 1개, `finish_reason: "stop"`, 유효한 token count를
가져야 합니다. 잘리거나 지원하지 않는 출력은 성공 응답으로 대체하지 않습니다.

PowerShell:

```powershell
$env:PYTHONPATH = Join-Path $PWD 'hosted/experimental'
$storageRoot = Join-Path $env:LOCALAPPDATA 'Cellivane/attached'
py -3 -B -m cellivane_seed --backend-url http://127.0.0.1:8080/v1 --model my-local-model --identity local-model-v1 --storage-root $storageRoot
```

Linux/macOS attached mode 예시입니다. 소유 프로세스를 기동하는 lab은 Windows 전용입니다.

```sh
PYTHONPATH=hosted/experimental python3 -B -m cellivane_seed \
  --backend-url http://127.0.0.1:8080/v1 --model my-local-model \
  --identity local-model-v1 --storage-root "$HOME/.local/share/Cellivane/attached"
```

Seed 기본 주소는 `http://127.0.0.1:18941/v1`이며 요청에도 같은 model alias를 사용합니다.
`--identity`는 backend/cache namespace이며 인증 증거가 아닙니다. `/health`의 성공은
서빙 admission 상태입니다. [상태 확인·요청 예시](docs/OPERATIONS.md#local-status-and-request)와
[정확한 입력 계약](docs/ARCHITECTURE.md#current-serving-contract)을 참고하세요.

## Windows 소형 모델 lab

원래 CPU lab은 llamafile 0.10.5 thin과 Qwen3-0.6B Q8_0를 사용합니다.
공식 배포처에서 다음 파일을 asset cache에 준비합니다.

| 파일 | 필수 SHA-256 |
|---|---|
| `llamafile-0.10.5-thin.exe` | `55c69c1be9d6ad2172e2d1c0acc677a60ea8ff60232009a8c8170f5bcb917611` |
| `Qwen3-0.6B-Q8_0.gguf` | `9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031` |

출처는 [llamafile 0.10.5](https://github.com/mozilla-ai/llamafile/releases/tag/0.10.5),
[ggml-org Qwen3 GGUF](https://huggingface.co/ggml-org/Qwen3-0.6B-GGUF),
[Ollama 0.40.2](https://github.com/ollama/ollama/releases/tag/v0.40.2)입니다.

```powershell
$cache = Join-Path $env:LOCALAPPDATA 'Cellivane/asset-cache'
$engineAssets = Join-Path $env:LOCALAPPDATA 'Cellivane/bridge-lab'
py -3 -B tools/hosted/cellivane_bridge_lab.py --profile subengine-smoke --prepare-only --asset-cache $cache --storage-root $engineAssets
py -3 -B tools/hosted/cellivane_seed_lab.py --mode smoke --asset-root $engineAssets --asset-cache $cache
```

준비 단계는 파일 해시를 검사해 복사하고, 없으면 pinned Ollama Windows archive
(1,468,085,195 bytes)를 다운로드해 추출 파일을 검증합니다. Smoke는 실제 CPU 모델을
실행하고 Seed reference와 Ollama candidate의 API 호환을 비교합니다.
답변 품질 향상을 증명하는 테스트는 아닙니다.

계속 서빙하려면 `--mode serve`를 사용합니다. 기본 저장 위치는
`%LOCALAPPDATA%/Cellivane/{seed-lab,bridge-lab,asset-cache}`입니다.
`--storage-root`, `--asset-root`, `--asset-cache`로 D: 등 다른 로컬 고정 디스크를
선택할 수 있습니다. Windows lab은 UNC/network·reparse 저장소를 거부합니다.
원래 smoke verifier는 기본 포트 18941/18942/18932를 전제로 합니다.

## 모델 크기·배치 비교

별도 비교 경로는 pinned Ollama v0.40.2와 공식 Qwen3 Q8_0를 사용합니다.
위에서 준비한 엔진 cache, 새 모델 디렉터리와 private 결과 디렉터리를 구분합니다.

```powershell
$modelRoot = Join-Path $env:LOCALAPPDATA 'Cellivane/seed-models'
$engineAssets = Join-Path $env:LOCALAPPDATA 'Cellivane/bridge-lab'
py -3 -B tools/hosted/cellivane_seed_assets.py --model 1.7b --storage-root $modelRoot
py -3 -B tools/hosted/cellivane_seed_assets.py --model 4b --storage-root $modelRoot
py -3 -B tools/hosted/cellivane_seed_compare.py --model 1.7b --model-file "$modelRoot/models/Qwen3-1.7B-Q8_0.gguf" --asset-root $engineAssets --storage-root $modelRoot --profile cpu --repeat 2
py -3 -B tools/hosted/cellivane_seed_compare.py --model 4b --model-file "$modelRoot/models/Qwen3-4B-Q8_0.gguf" --asset-root $engineAssets --storage-root $modelRoot --profile hybrid --layers 14 --repeat 2
py -3 -B tools/hosted/cellivane_seed_compare.py --model 1.7b --model-file "$modelRoot/models/Qwen3-1.7B-Q8_0.gguf" --asset-root $engineAssets --storage-root $modelRoot --profile auto --repeat 2
```

Asset tool은 파일만 다운로드·검증하며 모델을 실행하지 않습니다. 비교 명령은 실제
모델·소유 프로세스를 실행합니다. 기존 cache의 0.6B/llamafile 자산도 검증 대상입니다.
`0.6b` 비교 역시 지원합니다. `cpu`는 GPU layer 0을 요청하고, `hybrid`는 명시한
개수를 요청하며, `auto`는 backend에 선택을 맡깁니다. 요청한 profile 자체는
offload 성공 증거가 아닙니다.

[59c9bcd snapshot](https://github.com/tjwlstj/cellivane-seed/commit/59c9bcd)의
5개 승인된 로컬 실행은 **12문항을 두 번** 반복했습니다. CPU exact-format 점수는
0.6B 6/24, 1.7B 14/24, 4B 22/24였습니다. 1.7B auto와 4B hybrid도 같은 점수였고
단일 RTX 5050에서 각각 29/29, 14/37 CUDA layer를 관측했습니다.
일반 정확도 벤치마크가 아니며 backend prompt/KV 재사용이 시간에 영향을 줄 수 있습니다.
[결과·조건·원본 해시](docs/BENCHMARKS.md)와 [공개 집계](docs/benchmarks/2026-10-12-summary.json)를
함께 읽으세요. 이전 PASS는 수정된 현재 source를 검증하지 않습니다.

## CPU draft/target 실험과 다음 단계

별도 [launcher](tools/hosted/cellivane_seed_speculate.py)는 pinned llama.cpp b11429에서
Qwen3-4B target / 0.6B draft를 CPU로 실행했습니다. **4개 task × 3회 × OFF/ON**으로
24개 측정 응답과 별도 warmup 6개를 얻었고, 12쌍의 텍스트가 일치했습니다.
독립 기록 재판독이 통과했으며 paired OFF/ON HTTP wall 비율의 중앙값은 1.1974였습니다.
TRACE 수집을 켠 작은 corpus의 관측으로, GPU 결과·decoder-only 속도·정답률 향상이나
production speedup을 뜻하지 않습니다. 결과 캐시와 backend prompt reuse는 껐으며,
draft 444 offered / 366 accepted는 warmup을 포함합니다.

[실행·verifier 사용법](docs/DEVELOPMENT_GUIDE.md#cpu-speculation-launcher-and-replay)과
[SPECULATION](docs/SPECULATION.md)에 조건과 두 번의 보존된 실패 진단이 있습니다.
계약·예산 planner는 PARTIAL이며, 사전 `ADMITTED`는 실제 메모리 fit이나 엔진 자격의
증거가 아닙니다. 이 실험의 기록과 현재 adaptive 실행 검증은 서로 다릅니다.

## CPU 시작 모드 선택과 서빙

[Adaptive launcher](tools/hosted/cellivane_seed_adaptive.py)의 e32e224 선택·기동 구현과
auto baseline/calibrated draft 기능 smoke의 독립 기록 재판독은 **PASS**였습니다.
제품은 고정된 실험 범위의 PARTIAL입니다. 이 역사적 첫 지원 profile은
공식 Qwen3-4B Q8_0 target과 선택적 Qwen3-0.6B Q8_0 draft만 사용합니다.
Windows CPU, context 2048, thread 4, slot 1, cache 0, 최대 128 token으로 고정합니다.
일반 GGUF·GPU profile이나 요청 중간 mode 변경을 지원한다는 뜻은 아닙니다.

다음은 calibration 없이 baseline을 선택하는 실행 가능한 PowerShell 예시입니다.
준비 명령은 공식 pinned 자산을 다운로드하며, `serve`는 실제 모델을 기동합니다.

```powershell
$taskAssets = Join-Path $env:LOCALAPPDATA 'Cellivane/llama-assets'
$taskModels = Join-Path $env:LOCALAPPDATA 'Cellivane/seed-models'
$taskTarget = Join-Path $taskModels 'models/Qwen3-4B-Q8_0.gguf'
py -3 -B tools/hosted/cellivane_seed_assets.py --model 4b --storage-root $taskModels
py -3 -B tools/hosted/cellivane_llama_assets.py --asset-root $taskAssets --download-assets
py -3 -B tools/hosted/cellivane_seed_adaptive.py --mode plan --policy auto --expected-output-tokens 128 --asset-root $taskAssets --target-file $taskTarget
py -3 -B tools/hosted/cellivane_seed_adaptive.py --mode serve --policy auto --expected-output-tokens 128 --asset-root $taskAssets --target-file $taskTarget
```

`plan`은 모델을 기동하지 않고 private 계획·source snapshot을 기록합니다. `serve`는
기본 `http://127.0.0.1:18961/v1`에서 Ctrl+C까지 실행하며 alias는
`cellivane-spec-target`입니다. 이 profile은 HTTP `max_tokens > 128`을 dispatch 전에
400으로 거부합니다. Calibration이 없으면 `auto`는 `none`을 선택합니다.
[선택 조건·선택적 draft/calibration·smoke 절차](docs/OPERATIONS.md#adaptive-startup-selection)를
참고하세요. 과거 calibration은 현재 host의 속도 향상을 보장하지 않습니다.

[공개 집계](docs/benchmarks/2026-10-13-adaptive-summary.json)는 두 smoke의 matching
source 18개, 각 5개 computed 응답(1 warmup + 4 corpus), 129-token 요청의
dispatch 전 거부, audit 19개 및 소유 프로세스 cleanup을 기록합니다.
이 결과는 시작 모드 선택·서빙 기능 검증이며 새 paired speed/quality 비교가 아닙니다.
Python 명령이 기본 실행 경로입니다. 선택적 PowerShell wrapper는 AST 검사만 통과했고
검사 host의 기존 실행 정책에서 `-File`이 차단됐습니다. 시스템 정책을 바꾸지 않습니다.

## 명시적 target profile 확장

**소스 구현 CURRENT / 고정 CPU profile 기능·독립 replay PASS / 제품 PARTIAL:** 새 adaptive CLI는
`--target-profile 0.6b|1.7b|4b`를 제공합니다(기본 `4b`).
각 profile은 공식 Qwen3 Q8_0의 정확한 byte/hash pin을 사용합니다. Model file과
profile을 맞춰 지정하며, 자동 target 교체나 품질 하향 선택은 하지 않습니다.
HTTP alias `cellivane-spec-target`은 고정 호환 이름이므로 실제 model 크기의 증거가 아닙니다.
CPU/context/thread/slot/cache/128-token 한도는 위와 동일합니다.

아래는 새 CLI의 실행 예시입니다. 4B를 먼저 다운로드하지 않아도 이 블록부터
준비할 수 있습니다. 준비 명령은 모델 실행 없이 공식 파일을 다운로드·검증합니다.
두 `serve` 명령 중 원하는 모델 하나를 선택하세요. 다른 모델로 바꾸려면 Ctrl+C로 종료한 뒤 실행합니다.

```powershell
$taskAssets = Join-Path $env:LOCALAPPDATA 'Cellivane/llama-assets'
$taskModels = Join-Path $env:LOCALAPPDATA 'Cellivane/seed-models'
py -3 -B tools/hosted/cellivane_llama_assets.py --asset-root $taskAssets --download-assets
py -3 -B tools/hosted/cellivane_seed_assets.py --model 0.6b --storage-root $taskModels
py -3 -B tools/hosted/cellivane_seed_assets.py --model 1.7b --storage-root $taskModels
$taskTiny = Join-Path $taskModels 'models/Qwen3-0.6B-Q8_0.gguf'
$taskSmall = Join-Path $taskModels 'models/Qwen3-1.7B-Q8_0.gguf'
py -3 -B tools/hosted/cellivane_seed_adaptive.py --mode plan --target-profile 0.6b --policy auto --asset-root $taskAssets --target-file $taskTiny
py -3 -B tools/hosted/cellivane_seed_adaptive.py --mode plan --target-profile 1.7b --policy auto --asset-root $taskAssets --target-file $taskSmall
py -3 -B tools/hosted/cellivane_seed_adaptive.py --mode serve --target-profile 0.6b --policy auto --asset-root $taskAssets --target-file $taskTiny
py -3 -B tools/hosted/cellivane_seed_adaptive.py --mode serve --target-profile 1.7b --policy auto --asset-root $taskAssets --target-file $taskSmall
```

0.6B target에는 draft를 허용하지 않습니다. 1.7B/4B target의 draft 후보는 pinned
0.6B만이며 같거나 큰 draft는 거부합니다. 현재 calibration loader는 고정된 기존
4B target/0.6B draft speculation 형식만 받으며, 1.7B matching calibration의 생성·입력은
구현하지 않았습니다. 따라서 현재 1.7B `auto`는 `none`이고, 명시적 draft는 실험 opt-in입니다.
`profiles.draft`는 제공한 후보 artifact이며 실제 로드를 뜻하지 않습니다.
선택된 실행 방식은 summary의 `choice.selected_mode`와 status의 `adaptive.mode`로 구분합니다.
[profile 계약·증거 구분](docs/OPERATIONS.md#explicit-target-profiles)을 참고하세요.

[새 공개 집계](docs/benchmarks/2026-10-13-profiles-summary.json)는 0.6B auto baseline,
1.7B auto baseline·명시적 draft, 4B auto calibrated draft의 네 독립 replay PASS를
기록합니다. 각 실행은 source 19개, computed 5개(1 warmup + 4 corpus), 129-token
요청의 dispatch 전 거부와 cleanup을 확인했습니다. 1.7B의 네 답변 텍스트는 두 실행에서
같았지만, 한 번의 순차 4-task HTTP 합계는 baseline 8.152742초, draft 11.247127초로
**draft가 더 느렸습니다**. 반복·교차 통계 벤치마크나 품질 향상 증거는 아닙니다.

## 증거와 공개 범위

Smoke·bridge·comparison의 matching-source verifier 사용법은
[운영 가이드](docs/OPERATIONS.md#private-evidence-and-replay)에 모았습니다.
Verifier는 저장된 무결성과 의미 연결을 검사합니다. 해시·cleanup receipt는
endpoint/PID 인증, OS 생존 증명 또는 모델 품질 보증이 아닙니다.
Audit·엔진 로그의 입력/출력·절대경로는 private 저장소에 보존하고 Git에 넣지 않습니다.
Client 연결 종료는 backend 취소·모델 정지·KV 해제 확인이 아닙니다.

## 라이선스

이 저장소의 원본 코드는 [MIT](LICENSE), copyright © 2026 tjwlstj입니다.
외부 엔진·가중치와 upstream 자산은 각각의 라이선스·배포 조건을 유지합니다.
