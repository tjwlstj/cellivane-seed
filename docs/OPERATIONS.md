# Cellivane Seed 운영 가이드

상태: **PARTIAL / 로컬 실험용**. 검토일 2026-10-13.
이 문서는 실행 중인 세션의 한도·상태 확인·중단·private 증거 보존을 설명합니다.
설치·기동 명령은 [README](../README.md), 입력 계약은
[ARCHITECTURE](ARCHITECTURE.md#current-serving-contract), 변경·검증 절차는
[DEVELOPMENT_GUIDE](DEVELOPMENT_GUIDE.md)가 담당합니다.

## 한 세션의 한도

Seed는 localhost의 단일 사용자 실험을 위한 서빙 계층입니다. 인증된 multiuser
서비스가 아니므로 public address로 중계하거나 browser Origin 검사를 우회하지 마세요.
모델 파일 크기·KV·Python allocator·backend 메모리는 다음 카운터와 별개입니다.

| 대상 | 기본 한도와 동작 |
|---|---|
| 서로 다른 생성 요청 | compute slot 1개; 사용 중이면 429, 숨은 생성 대기열 없음 |
| 같은 in-flight 요청의 subscriber | waiter 최대 8개; 가득 차면 429 |
| 완료 결과 LRU | 32개와 accounted key/result 1 MiB; 프로세스 메모리에만 존재 |
| Audit | 세션당 최대 10,000 record 및 64 MiB serialized record bytes |
| 연결 | HTTP 동시 연결 기본 12개, 수신 I/O timeout 5초 |

Audit record 수는 사용자 요청 수가 아닙니다. configuration, scheduler event,
backend exchange와 shutdown도 record를 사용합니다. 한 요청이 여러 record를
만들 수 있어 10,000번 생성 요청을 보장하지 않습니다.

Audit는 record를 exclusive 생성하고 file flush/fsync 후 hash chain을 잇습니다.
**다음 append가 record 또는 byte 한도를 넘으려 하거나 persistence가 실패하면
audit가 poisoned가 됩니다.** Scheduler event 저장 실패는 Seed admission도 닫고,
성공으로 반환·cache할 수 없는 결과를 성공으로 대체하지 않습니다. 이미 한도에
닿은 세션에서는 shutdown record도 저장되지 못할 수 있습니다.
File fsync와 exclusive 생성은 power-loss/crash recovery 검증이 아닙니다.

## 상태와 용량 경고

현재 `/health`는 Seed의 admission 상태이며 backend 모델의 지속적인 생존·GPU
자격을 검사하지 않습니다. `/v1/status`의 scheduler 값으로 active/inflight/waiter,
cache, closed/poisoned를 확인합니다. 상태 GET은 생성 요청을 만들지 않습니다.

현재 소스의 audit snapshot 계약은 다음과 같습니다. **구현 CURRENT / audit·상태
회귀 24 tests PASS**입니다. 새 실제 smoke의 독립 재판독과 최종 Windows 회귀도
아래 별도 범위로 통과했습니다. 이전 배포의 `/v1/status`에는
`audit_directory`가 포함될 수 있습니다.

| `audit` field | 의미 |
|---|---|
| `session_id` | 현재 audit 세션 식별자; 인증 또는 복구 token이 아님 |
| `records`, `bytes` | 성공적으로 저장한 record 수와 serialized bytes |
| `record_limit`, `byte_limit` | 10,000 및 67,108,864 |
| `remaining_records`, `remaining_bytes` | 위 한도까지 남은 record와 byte 예산 |
| `warning_threshold_percent` | 80 |
| `near_capacity` | record 수 또는 bytes가 각각의 한도 80% 이상이면 true |
| `poisoned` | audit persistence 또는 한도 초과 시도가 실패 상태를 만들었는지 |

Append와 snapshot은 같은 lock으로 직렬화합니다. 80% 경고는 남은
시간이나 처리 가능한 요청 수의 예측이 아닙니다. Snapshot을 읽는 사이에도 다른
요청이 남은 용량을 사용할 수 있으며 shutdown 여유를 예약하지 않습니다.
현재 status 응답은 기본 절대경로를 제거했습니다. 운영자가 보는 CLI ready 출력과
private lab report에는 audit 경로를 유지합니다. 이 변경 이전 CI는 새 응답의 검증이 아닙니다.

<a id="local-status-and-request"></a>

## 로컬 상태 확인과 요청

다음은 기본 포트로 실행한 Seed에 보내는 PowerShell 예시입니다. 실제 실행 중인
model alias로 `$model`을 설정하세요. 명령을 실행하면 실제 모델 생성이 발생합니다.

```powershell
$seedBase = 'http://127.0.0.1:18941'
$model = 'my-local-model'
Invoke-RestMethod -Uri "$seedBase/health"
Invoke-RestMethod -Uri "$seedBase/v1/status"
$body = @{model=$model; messages=@(@{role='user'; content='Reply with exactly CELLIVANE_OK'}); temperature=0; stream=$false; max_tokens=64} | ConvertTo-Json -Depth 6 -Compress
Invoke-RestMethod -Method Post -Uri "$seedBase/v1/chat/completions" -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

| HTTP 상태 | 이 경로에서의 해석 |
|---|---|
| 200 completion | 계약에 맞게 완료된 답변; 정답 여부는 별도 |
| 400 | body·message·설정 등의 입력 계약 거부 |
| 403 | loopback Host/Origin 조건 불일치 |
| 429 | slot 또는 같은 요청 waiter 용량 부족 |
| 502 | backend가 유효하게 완료되지 않았거나 owner/waiter deadline 실패 |
| 503 | admission closed/poisoned 또는 연결 용량 부족 |
| 408 | bounded HTTP 수신 I/O 실패/timeout |

다시 보낸 요청은 별도의 호출입니다. 이전 요청의 완료 여부를 모르면 자동 재시도를
성공·취소 증거로 취급하지 마세요. Cache hit는 `cellivane.execution` 구분과 논리 token usage를
반환하며, 그 hit에서 token 계산을 다시 했다는 뜻이 아닙니다.

## Deadline과 중단

Core의 `timeout_s`는 waiter의 대기를 제한하고 늦게 돌아온 owner 결과를 거부합니다.
임의의 Python callable이 계속 block하면 core가 이를 선점하거나 모델을 종료할 수
없습니다. 그런 callable을 연결하는 사람은 backend 자체의 deadline을 구현해야 합니다.

기본 attached CLI는 strict HTTP transport에 90초 deadline을 전달합니다.
Transport는 bounded body와 남은 wall-clock 시간으로 I/O를 제한합니다. 이는 임의
callable에 대한 보장과 다르며 OS가 모든 종료를 정확히 90초에 강제한다는 뜻도 아닙니다.
5초 HTTP 수신 timeout, backend transport deadline, subscriber 대기, 프로세스 cleanup은
각자 다른 경계입니다.

Subscriber timeout 또는 client disconnect는 shared backend 계산의 소유권을 주지
않습니다. 연결을 닫아도 다른 subscriber의 생성이나 모델 자체가 정지했다고 할 수
없습니다. Attached backend는 별도 프로세스이므로 Seed 종료가 그 모델 서버의 종료를
의미하지 않습니다. 소유 Windows lab은 Ctrl+C/finalizer에서 retained handle과 Job으로
자신이 기동한 프로세스를 정리하며, 실제 cleanup receipt를 확인해야 합니다.
긴 blocking callable은 server의 worker join을 지연시킬 수 있습니다.

## 용량에 가까워졌거나 poisoned일 때

1. `near_capacity`가 표시되면 새 생성 요청을 줄이고 active/inflight/waiter와 남은
   용량을 확인합니다. 큰 응답·추가 event를 위한 정확한 reserve는 보장되지 않습니다.
2. 세션을 종료할 때 새 요청 전송을 멈추고 기존 작업의 결과·실패·cleanup을 확인합니다.
   소유 backend와 attached backend의 종료 책임을 구분합니다.
3. CLI가 출력한 audit 경로와 private run report, 원본 log·source snapshot을 보존합니다.
   Poisoned 세션에 기록을 덧붙이거나 기존 파일을 편집해 복구하지 않습니다.
4. 원인을 해결한 뒤 같은 local storage root로 새 프로세스를 기동할 수 있습니다.
   Audit는 새로운 `seed-runs/<session-id>` 디렉터리를 만듭니다.

**새 세션 시작은 복구가 아닙니다.** 이전 audit를 replay해 실행을 이어가지 않으며
완료 cache·in-flight 상태를 복원하지 않습니다. 첫 요청은 새 계산일 수 있습니다.
Poisoned 세션은 이전 실패 상태로 보존합니다. 새 session ID가 이전 결과의 검증을
갱신하거나 실패를 PASS로 바꾸지 않습니다.

10k/64 MiB는 **한 세션의 상한**입니다. 반복 재시작으로 쌓이는 모든 세션과 engine log,
model asset의 누적 디스크 사용량에는 자동 상한·rotation·retention 정책이 없습니다.
운영자가 디스크 여유와 보존 정책을 관리해야 합니다. 이 프로젝트는 원본 evidence를
자동 삭제해 여유를 확보하지 않습니다.

<a id="private-evidence-and-replay"></a>

## Private 증거와 재판독

CLI ready 출력과 lab summary가 알려 준 경로를 로컬에서 선택합니다. 다음 대화형
예시는 경로를 저장소에 넣지 않으며 verifier는 모델을 재실행하지 않습니다.

```powershell
$summary = Read-Host 'Seed lab-summary.json path'
py -3 -B tools/hosted/verify_cellivane_seed.py $summary
$bridgeRun = Read-Host 'Matching bridge run directory'
py -3 -B tools/hosted/verify_cellivane_bridge.py --run-directory $bridgeRun
$comparison = Read-Host 'comparison.json path'
$matchingSource = Read-Host 'Exact matching source checkout or snapshot directory'
py -3 -B tools/hosted/verify_cellivane_comparison.py $comparison --source-root $matchingSource
```

Speculative CPU report에는 별도
[launcher/verifier 절차](DEVELOPMENT_GUIDE.md#cpu-speculation-launcher-and-replay)를 사용합니다.
서로 다른 report 형식의 경로를 섞지 마세요. 독립 verifier는 runtime validator를
import하지 않고 저장된 hash·semantic join을 검사합니다. Unkeyed hash는 hostile source
인증이나 endpoint/PID attestation이 아닙니다. Public aggregate만으로 private replay를
재구성할 수 없으며 raw report·audit·log와 matching source가 필요합니다.

Audit, raw wire, engine log에는 prompt·answer·절대경로가 포함될 수 있습니다.
Private evidence와 자산은 Git 밖에 보존합니다. `.gitignore`는 generated evidence·asset·secret을
제외하지만 모든 개인정보를 자동 판별하지 않습니다. 공개에는 reviewed source,
official URL, allowlisted aggregate와 해시를 사용합니다.

<a id="adaptive-startup-selection"></a>

## CPU 시작 모드 선택

[정책](../hosted/experimental/cellivane_seed/adaptive.py)과
[launcher](../tools/hosted/cellivane_seed_adaptive.py)의 아래 첫 구현·실측은
**e32e224 snapshot의 역사적 검증 범위**입니다. 새 target profile 절과 구분합니다.
정책 fixture 24 tests와 audit·상태 회귀 24 tests가 통과했습니다.
Auto baseline과 calibrated draft의 실제 기능 smoke 및 독립 기록 재판독은 **PASS**입니다.
해당 e32e224 slice의 로컬 Windows 회귀는 **467 PASS / 0 skip / 42.739초**였습니다.
현재 profile 확장의 505-test 결과는 아래 별도 절에 있으며 프로젝트는 PARTIAL입니다.
Launcher fixture 18개와 독립 replay fixture 35개도 통과했습니다. 공개 revision의 CI는
[beta fixture workflow](https://github.com/tjwlstj/cellivane-seed/actions/workflows/tests.yml?query=branch%3Abeta)에서
해당 commit의 terminal 결과로 확인해야 하며 이전 e763cec CI와 구분합니다.

그 snapshot의 첫 지원 범위는 pinned llama.cpp b11429, 공식 Qwen3-4B Q8_0 target과 선택적
Qwen3-0.6B Q8_0 draft입니다. CPU, context 2048, thread 4, slot 1, cache 0,
greedy generation 최대 128 token을 고정합니다. Input 계약은 기존 Seed subset을
유지하되 HTTP `max_tokens > 128`은 backend dispatch 전에 400으로 거부합니다.
다른 모델·GPU·native MTP의 범용 지원을 의미하지 않습니다.

`--mode plan|smoke|serve`는 필수이고 `--policy auto|none|draft`의 기본은 `auto`입니다.
`--expected-output-tokens`는 `1..128`, 기본 128이며 호출자가 정한 workload hint입니다.
다음 요청의 실제 길이나 속도를 예측하는 값이 아닙니다. `auto`의 선택 순서는 다음과 같습니다.

1. Baseline declared RAM admission이 안 되면 기동을 차단합니다.
2. Draft model·admission이 없거나 예상 출력이 64 token 미만이면 `none`을 선택합니다.
3. Calibration이 없거나 engine/model/settings identity가 다르면 `none`을 선택합니다.
4. 모든 paired text가 같고 pair가 8개 이상이며 실제 speculation 관측과 paired wall
   ratio 1.05 이상을 기록한 matching calibration이면 `draft`를 선택합니다.
   그 외에는 `none`입니다. 과거 host 측정은 현재 host의 성능 보장이 아닙니다.

명시적 `--policy none`은 baseline을, `--policy draft`는 draft admission을 요구합니다.
명시적 draft 선택은 자동 benefit 조건을 요구하지 않으므로 가속 검증과 구분해야 합니다.
선택은 기동 전에 한 번 수행합니다. 한 개의 소유 backend server를 기동하며 요청
중간 `none`/`draft` 변경이나 generation 자동 재시도를 하지 않습니다.

RAM snapshot은 Windows `GlobalMemoryStatusEx`에서 읽습니다. Planner는 full target/draft
file bytes와 선언한 host overhead 2 GiB·workspace 512 MiB, headroom 최대 4 GiB로
검사합니다. 실행 직전 snapshot을 다시 읽어 선택합니다. 이는 RAM 예약·OS 강제 한도나
실측 peak 보장이 아닙니다. Source/model/binary가 바뀌면 기존 선택·증거를 재사용하지 마세요.

README의 baseline 예시는 공식 자산 준비와 `plan`/`serve`를 포함합니다. 같은 shell에서
선택적 draft와 calibration을 준비하려면:

```powershell
py -3 -B tools/hosted/cellivane_seed_assets.py --model 0.6b --storage-root $taskModels
$taskDraft = Join-Path $taskModels 'models/Qwen3-0.6B-Q8_0.gguf'
$taskCalibration = Read-Host 'Retained raw speculation.json path'
$taskCalibrationSource = Read-Host 'Matching retained source-snapshot directory'
py -3 -B tools/hosted/cellivane_seed_adaptive.py --mode plan --policy auto --expected-output-tokens 128 --asset-root $taskAssets --target-file $taskTarget --draft-file $taskDraft --calibration-file $taskCalibration --calibration-source-root $taskCalibrationSource
py -3 -B tools/hosted/cellivane_seed_adaptive.py --mode smoke --policy auto --expected-output-tokens 128 --asset-root $taskAssets --target-file $taskTarget --draft-file $taskDraft --calibration-file $taskCalibration --calibration-source-root $taskCalibrationSource
```

Calibration의 두 인자는 함께 지정해야 합니다. Public aggregate나 user-supplied PASS
파일 대신 원래 raw `speculation.json`, matching source snapshot, model·asset·audit·log를
보존한 private evidence가 필요합니다. Launcher가 독립 speculation verifier를 재실행하고
source-file replay PASS를 요구합니다. 파일·재판독 실패는 임의의 baseline fallback으로
숨기지 않습니다. 인자 자체를 생략하는 auto baseline과 구분하세요.

`--asset-root`·`--target-file`은 필수이며 adaptive는 자산을 다운로드하지 않습니다.
`--storage-root`는 기본 `%LOCALAPPDATA%/Cellivane/adaptive`이고 D: 등의 local fixed disk로
변경할 수 있습니다. Seed/backend 포트는 기본 18961/18962입니다.
`--serve-seconds`는 정수 `0..86400`; 0은 Ctrl+C까지 실행하고 양수는 해당 시간 이후
정리합니다. 모델 로딩 시간과 HTTP 요청 자체의 deadline은 별도입니다.
`plan`은 backend generation 없이 파일·RAM을 검사하고 `adaptive-runs/<id>/adaptive.json`
및 source snapshot을 기록합니다. `smoke`는 warmup 1개와 4개 task, 129-token 부정 요청,
status·cleanup을 기록합니다. Producer COMPLETED와 독립 PASS는 같은 verdict가 아닙니다.
## e32e224 기능 smoke의 검증 범위

[Allowlisted 집계](benchmarks/2026-10-13-adaptive-summary.json)의 두 실행은 각각
warmup 1개 + corpus 4개가 HTTP 200 computed로 끝났고 독립 replay PASS였습니다.
Matching runtime/launcher source 18개와 별도 verifier 4개 snapshot/hash를 확인했습니다.
각 실행의 129-token 요청은 dispatch 전에 400이었으며 추가 backend dispatch는 0입니다.
Backend calls 5, rejected 1, final audit records 19, owned Job active 0과 port closure를
기록했습니다. Source/model/asset 안정성과 recorded cleanup은 endpoint/PID 인증이 아닙니다.

| Auto 경로 | 선택 이유 | Draft offered / accepted | 독립 결과 |
|---|---|---|---|
| Matching raw calibration + draft | `calibrated-advisory-benefit` → `draft` | 148 / 122 | PASS |
| Draft present, calibration 없음 | `calibration-missing` → `none` | 0 / 0 | PASS |

Draft counter는 warmup을 포함한 run-log 합계이며 request/native task의 직접 attestation이
아닙니다. 이 기능 smoke를 새 paired speed/quality 비교나 현재 host의 가속 보장으로
해석하지 않습니다. 공개 집계 SHA-256은
`b0dec2c9a1b14cca5ed91a5ecbffa952cd0da353c727777122eec62a74c8bf17`입니다.

보충 `serve --serve-seconds 30` 실행은 외부 HTTP 요청 1개를 완료하고 source/model 안정성,
종료·cleanup을 기록했습니다. 이는 5-request smoke용 독립 verifier의 적용 범위 밖입니다.
Draft가 있는 `plan --expected-output-tokens 32`도 `short-output-profile`로 `none`을
선택했으며 child dispatch는 없었습니다. 계획의 `PLANNED` outcome은 모델 실행이
없다는 뜻이며 해당 선택 기능의 미구현 상태를 뜻하지 않습니다.

선택적으로 [PowerShell wrapper](../tools/hosted/Start-CellivaneAdaptive.ps1)를
사용할 수 있습니다. 기본 `-Mode plan`은 모델을 기동하지 않습니다.
**Python CLI가 기본 경로**입니다. Wrapper는 AST 검사만 통과했고 검사 host에서는 기존
실행 정책이 `powershell -File`을 차단했습니다. Wrapper 실행은 허용된 로컬 정책이
있는 환경에서만 사용하며, 이 가이드는 시스템 정책 변경이나 일괄 bypass를 안내하지 않습니다.

```powershell
powershell -File tools/hosted/Start-CellivaneAdaptive.ps1 -AssetRoot $taskAssets -TargetFile $taskTarget
powershell -File tools/hosted/Start-CellivaneAdaptive.ps1 -Mode serve -Policy auto -AssetRoot $taskAssets -TargetFile $taskTarget -ServeSeconds 60
```

[독립 adaptive verifier](../tools/hosted/verify_cellivane_adaptive.py)는 저장된
`adaptive.json`과 matching source snapshot으로 재판독합니다. 위의 두 기능 smoke는
실제 독립 PASS이며 각 run의 원본 verdict와 verifier snapshot을 private evidence에 보존합니다.

```powershell
$taskReport = Read-Host 'Saved adaptive.json path'
$taskSource = Read-Host 'Matching adaptive source-snapshot directory'
py -3 -B tools/hosted/verify_cellivane_adaptive.py $taskReport --source-root $taskSource
```

이 verifier의 PASS 범위는 recorded startup integrity와 semantic replay이며 endpoint/PID
인증·실제 backend 자격·OS 메모리 강제·현재 host의 가속 보장이 아닙니다.

<a id="explicit-target-profiles"></a>

## 명시적 target profile 확장

**소스 구현 CURRENT / 기능 smoke·독립 replay PASS / 제품 PARTIAL:** 이 slice는 사용자가
`--target-profile 0.6b|1.7b|4b`로 target을 고릅니다. 기본값은 기존 호환 `4b`입니다.
동일한 CPU b11429/context 2048/thread 4/slot 1/cache 0/max 128 계약을 유지합니다.
엔진이 RAM이나 요청 내용에 따라 더 작은 target으로 자동 교체하지 않습니다.
선택한 model의 정답률이나 품질을 보증하지 않습니다.

| Target profile | 정확한 공식 Q8_0 artifact | 허용 draft | 새 검증 상태 |
|---|---|---|---|
| `0.6b` | Qwen3-0.6B, 639,446,688 bytes | 없음; 동일 0.6B draft도 거부 | Auto baseline 독립 PASS |
| `1.7b` | Qwen3-1.7B, 1,834,426,016 bytes | Pinned Qwen3-0.6B만 | Auto baseline·명시적 draft 독립 PASS |
| `4b` | Qwen3-4B, 4,280,404,704 bytes | Pinned Qwen3-0.6B만 | Schema 2/source 19의 auto calibrated draft 독립 PASS |

Artifact의 immutable revision·SHA는 [UPSTREAM](UPSTREAM.md#immutable-official-qwen3-model-artifacts)가
담당합니다. Target profile은 정확한 pinned basename·byte 수·SHA 모두와 일치해야 합니다.
파일명만으로 모델을 추정하거나 다른 파일을 조용히 선택하지 않습니다.
지원 목록 밖의 draft, 같은 크기 또는 더 큰 draft는
기동 전에 거부하는 계약입니다. `--target-profile`은 tensor backend 자체의 임의
모델 지원을 선언하는 인자가 아닙니다.

HTTP alias는 모든 profile에서 `cellivane-spec-target`으로 유지합니다.
`/v1/models`의 alias만 보고 0.6B/1.7B/4B를 추정하지 마세요. 선택 profile과 artifact
metadata·hash·계획의 identity로 실제 모델을 구분합니다. Schema-2 `/v1/status`와 CLI
ready 출력의 top-level `profiles`는 `target` record와 `draft` record 또는 null을 제공합니다.
각 record는 `schema_version`, `name`, `filename`, `bytes`, `sha256`, `scope`이며
scope는 `pinned-model-artifact-not-backend-qualification`입니다. 절대경로는 포함하지 않습니다.
`profiles.draft`는 입력으로 제공한 후보 artifact입니다. Baseline `none`을 선택하면
이 값이 있어도 draft model을 로드하지 않을 수 있습니다. Summary `choice.selected_mode`와
status `adaptive.mode`가 선택한 실행 방식을 나타내며 metadata만으로 loaded instance를 추정하지 않습니다.
Model 크기는 다른데 같은
alias를 사용하는 것이 동일 weight 또는 calibration이라는 뜻은 아닙니다.

기본 `auto`는 matching calibration이 없으면 baseline입니다. 과거 4B calibration은
1.7B target의 근거가 될 수 없으며 identity가 다르면 `none`을 선택합니다.
현재 loader는 기존 고정 4B target/0.6B draft raw speculation 형식만 독립 재판독합니다.
1.7B matching calibration을 생성하거나 입력하는 경로는 아직 구현하지 않았으므로
현재 1.7B `auto`는 baseline입니다. 새 profile smoke의 `adaptive.json`이나 public aggregate를
calibration으로 대신 넣을 수 없습니다. 미지원 형식의 입력은 거부하며 자동 fallback으로 숨기지 않습니다.
1.7B + 0.6B 명시적 `--policy draft`는 calibration benefit 조건을 요구하지 않는
실험 opt-in 경로입니다. 기능 smoke를 일반적인 동일출력·가속 보장으로 확장하지 않습니다.

README의 0.6B/1.7B 준비·plan·serve 예시는 완전한 변수와 공식 catalog를 사용합니다.
기존 4B target은 새 CLI에서 명시적으로 다음처럼 선택할 수 있습니다.

```powershell
py -3 -B tools/hosted/cellivane_seed_adaptive.py --mode plan --target-profile 4b --policy auto --asset-root $taskAssets --target-file $taskTarget
py -3 -B tools/hosted/cellivane_seed_adaptive.py --mode smoke --target-profile 1.7b --policy draft --asset-root $taskAssets --target-file $taskSmall --draft-file $taskTiny
```

후속 raw summary는 schema 2의 profile/artifact identity join과 19-file source closure를
기록합니다. 독립 verifier의 소스도 역사적 schema 1/18-file 증거를 원래 계약으로
읽도록 확장했습니다. 기존 두 v1 run의 matching snapshot replay도 이전 verdict와
동일했습니다. 이전 aggregate·source hash를 새 값으로 덮어쓰지 않았습니다.
Schema·file count 증가 자체는 실행 성공이 아니므로 새 actual·회귀와 역사 CI를 구분합니다.

<a id="profile-smoke-results"></a>

## 현재 profile smoke 결과

[공개 집계](benchmarks/2026-10-13-profiles-summary.json)의 네 schema-2 run은 동일하게
동결된 19-file runtime/launcher source에서 실행했고 독립 replay PASS였습니다.
원본 report/verdict와 verifier 4개 snapshot은 private evidence에 보존합니다.
총 computed 응답은 20개(4 warmup + 16 corpus), 각 run은 HTTP 200 응답 5개,
129-token 요청의 HTTP 400 사전 거부 1개, 추가 backend dispatch 0, audit records 19개와
소유 Job active 0·port closure를 확인했습니다. 최종 Windows 전체 회귀는
**505 PASS / 0 skip / 56.439초**입니다. 이 변경의 공개 revision CI는 게시 후
[beta workflow](https://github.com/tjwlstj/cellivane-seed/actions/workflows/tests.yml?query=branch%3Abeta)에서
해당 commit의 terminal 결과로 확인해야 합니다.

| Target / policy | 선택 mode | 4-task HTTP 합계 | 소유 primary peak working set | 독립 replay |
|---|---|---:|---:|---|
| 0.6B / auto | none | 4.517928초 | 0.885 GiB | PASS |
| 1.7B / auto + 기존 4B calibration | none | 8.152742초 | 2.001 GiB | PASS |
| 1.7B / 명시적 draft | draft | 11.247127초 | 2.872 GiB | PASS |
| 4B / auto + matching calibration | draft | 17.128861초 | 5.220 GiB | PASS |

0.6B는 `draft-missing`, 1.7B auto는 `calibration-identity-mismatch`, 4B auto는
`calibrated-advisory-benefit`로 선택했습니다. 1.7B auto가 baseline인 것은 현재
calibration 계약의 결과이며, 이 smoke의 속도를 측정해 자동 mode를 바꾼 것이 아닙니다.
1.7B의 두 실행은 네 task의 텍스트가 4/4 같았으나 draft HTTP 합계가 더 컸습니다.
1.7B draft counter는 188 offered / 97 accepted, 4B는 148 / 122이며 warmup을 포함합니다.

시간은 각 profile의 **한 번의 순차 4-task HTTP 합계**로 prefill·generation·동기 audit를
포함하고 startup·warmup은 제외합니다. 반복·교차 순서 비교나 decoder-only throughput이
아니므로 새로운 일반 속도·정확도·품질 보장으로 확장하지 않습니다. 메모리는 OS가 읽은
**소유 primary worker의 peak working set**만입니다. Python·다른 프로세스·GPU를 제외하며
전체 RAM 요구량, tensor residency 또는 예약·강제 상한을 뜻하지 않습니다.

동일 0.6B target/draft 조합의 부정 CLI 입력은 inspection·model launch 전에 거부됐고
원본 producer FAIL과 source를 보존했습니다. 기대된 미지원 거부를 성공 모델 실행으로
승격하지 않습니다. 공개 집계 SHA-256은
`32a8b15ddaad4124ef802dc42ea0ba9c5e9c55fa52ef2dae910656214b908303`입니다.

## 아직 지원하지 않는 운영 기능

자동 audit rotation, crash recovery, persistent cache, arbitrary callable 선점,
모델 중단 API, per-request speculative mode switching은 구현된 운영 기능으로
간주하지 않습니다. 역사 CI와 벤치마크는 해당 source snapshot에 한정됩니다.
