# 1요청 성능 구간 분석 및 개선 기록 (`perf/request-breakdown`)

이 문서는 `perf/request-breakdown` 분석 브랜치에서 수행한 **단일 요청(1 request) 성능 구간 분해**, **변경 이유 및 트레이드오프**, **전/후 실측 비교**를 기록한다. 기존 [실행 기록](03-performance-test-worklog.md)과 [스택 비교 결과 JSON](03-stack-comparison-results.json)의 확정 수치는 수정하지 않는다.

> 측정은 `dev`의 TTL 정리 개선·락 기능을 병합하기 전 구성에서 수행했다. 아래 수치를 병합 후 서버의 재측정 결과로 해석하지 않는다.

---

## 1. 분석 목적 및 방법

- **목적**: 현재 Mini Redis([app.py](../../app.py) + [storage.py](../../storage.py))에서 HTTP 1요청이 처리되는 동안 어느 구간이 시간을 얼마나 차지하는지 분리·계측하고, 기존 HTTP/JSON 계약을 해치지 않는 범위에서 비효율 구간을 확인·개선한다.
- **측정 스크립트**:
  - [profile_request.py](../../scripts/profile_request.py): 3-Tier 경계 소거법(직접 호출 / ASGI 인메모리 / 실제 `uvicorn` TCP) 및 `cProfile`
  - [profile_deep_dive.py](../../scripts/profile_deep_dive.py): `httpx` 클라이언트 비용을 제외한 Raw Socket keep-alive(`TCP_NODELAY`) 기반 7구간 분해, 서버 이벤트루프 전용 `cProfile`, `MiniRedis.set` 크기별 스케일링(1KB~65KB)
  - [compare_http_vs_resp.py](../../scripts/compare_http_vs_resp.py): 동일 Python 3.14 + `asyncio` + `MiniRedis` 위 최소 RESP2 대조 측정
  - [compare_h11_vs_httptools.py](../../scripts/compare_h11_vs_httptools.py): `http="h11"` vs `http="httptools"`(`loop="asyncio"`) 단건(`C=1`) 및 동시(`C=50`, `N=5,000`) 비교

---

## 2. 초기 병목 진단 결과

1. **작은 요청(`GET small`, `PUT small`, `INCR cnt`)**:
   - Raw Socket keep-alive RTT(`p50 = 207.8~231.5 µs`, 순수 TCP 루프백 echo `22.56 µs`) 중 **약 97~99%**가 `uvicorn` + `h11` + `FastAPI` + JSON 입출력 구간이었고, `MiniRedis` 저장소 자체는 **`2.4~7.1 µs` (1.1~3.1%)**에 불과했다.
   - 서버 전용 `cProfile`에서 최상위 CPU 소비처는 Pure-Python HTTP/1.1 파서·검증기인 `h11`(`normalize_and_validate`, `validate`, `_process_event`)이었다.
   - 참고 대조([compare_http_vs_resp.py](../../scripts/compare_http_vs_resp.py)): 동일 `asyncio` + `MiniRedis` 위 최소 RESP2(`28.5~35.6 µs`) 대비 현재 HTTP/FastAPI 스택은 `6.5~7.5배`가량 지연이 크며, 이는 HTTP/1.1 파싱·라우팅·JSON 직렬화라는 프로토콜/API 계약 선택에 따른 비용으로 확인됐다.
2. **큰 JSON 요청(`PUT large`, ~16KB)**:
   - 초기 `PUT large` RTT(`p50 = 820.4 µs`)에서는 `MiniRedis.set`이 **`435.7 µs` (53.1%)**로 최대 구간이었다.
   - 단계별 측정 결과 `dict` 저장과 TTL 정리는 크기와 무관하게 `3.56 µs`(`O(1)`) 고정이었으나, Python 재귀 순회인 `_json_content_supported`가 `MiniRedis.set` 시간의 **약 70%**(`16KB` 기준 `287.33 µs`, `65KB` 기준 `1,144.57 µs`)를 소비하고 있었다.

---

## 3. 변경 1: `_json_content_supported` 재귀 검증 제거

### 변경 이유
- 실제 서비스 경로([app.py](../../app.py))는 `await request.json()`(`json.loads`)으로 파싱된 객체만 저장소에 전달하므로 `tuple`, 비문자열 `dict` 키, 순환 참조가 구조적으로 만들어지지 않는다.
- 기존에도 [storage.py](../../storage.py)의 `MiniRedis.set`은 `json.dumps(..., allow_nan=False)`와 `except (UnicodeEncodeError, TypeError, ValueError)`를 통해 `NaN`, `Infinity`, `-Infinity`, 순환 참조, 직렬화 불가 객체를 `INVALID_INPUT`으로 거절하고 있었다.
- 즉 `_json_content_supported`는 오직 Python에서 `store.set()`을 직접 호출해 `(1, 2)`나 `{1: "a"}`를 넘기는 단위 테스트([02-test-cases.md](../scenario_02_concurrent_counter/02-test-cases.md) `J1-04` 일부)만을 위해 HTTP `PUT` 핫패스에서 매번 Python 재귀 순회를 수행하고 있었다.
- 사용자 합의에 따라 직접 호출 전용 `tuple`·비문자열 키 거절 계약을 제외하고, 관련 테스트와 명세 문서를 함께 동기화했다.

### 변경 범위
- [storage.py](../../storage.py): `_json_content_supported` 정의 및 `MiniRedis.set` 내 호출 제거
- [test_json_types_extension.py](../../tests/scenario_01_storage/test_json_types_extension.py): `test_j1_04_invalid_python_content`에서 `tuple`, `non-string-key` 파라미터 제외(`nan`, `inf`, `negative-inf`, `nested-nan`, `nested-inf`, `cycle` 유지)
- 명세 동기화: [01-storage-scenario.md](../scenario_01_storage/01-storage-scenario.md), [02-test-cases.md](../scenario_02_concurrent_counter/02-test-cases.md), [02-top-down-functions.md](../scenario_02_concurrent_counter/02-top-down-functions.md)

### 실측 효과 ([profile_deep_dive.py](../../scripts/profile_deep_dive.py))
- **`PUT large (~16,467 B)` Raw Socket HTTP RTT**:
  - `p50`: `820.4 µs → 511.7 µs` (**-308.7 µs, -37.6%**)
  - `p95`: `1,031.0 µs → 599.7 µs` (**-41.8%**)
  - `p99`: `1,548.3 µs → 781.3 µs` (**-49.5%**)
  - `MiniRedis.set` 구간: `435.7 µs (53.1%) → 150.8 µs (29.5%)`
- **`MiniRedis.set` 직접 호출 (`p50`)**:
  - ~1,067 B: `32.06 µs → 17.81 µs` (-44.4%)
  - ~4,124 B: `113.98 µs → 40.37 µs` (-64.6%)
  - ~16,441 B: `408.44 µs → 127.04 µs` (-68.9%)
  - ~65,012 B: `1,614.74 µs → 474.93 µs` (-70.6%)

---

## 4. 변경 2: HTTP 파서 `h11` → `httptools` 교체 (`uvloop` 미적용)

### 변경 이유 및 트레이드오프
- **코드·계약 무수정 C 확장 대체**: 작은 요청에서 `cProfile` 1위 병목이었던 Pure-Python `h11` 파서를 C(`llhttp`) 기반 `httptools`로 교체했다.
- **`httptools`의 이중 효과**:
  1. 요청 파싱(`feed_data`, `parse_url`)을 C 상태 머신으로 처리한다.
  2. `uvicorn`이 `httptools_impl` 모드를 사용하면 응답 쓰기(`send`)에서도 `h11.Response`·`h11.Data` 객체 생성과 `h11` 상태 머신 전이 없이 바이트 리스트 조립으로 직접 소켓에 쓴다.
- **`uvloop`를 교체하지 않은 이유**:
  1. 동일 Python 3.14 표준 `asyncio` 위에서 돌린 RESP2가 순수 TCP(`20.2 µs`) 포함 `28.5 µs`로, 표준 `asyncio` 루프 자체의 단건 오버헤드가 작음을 확인했다.
  2. `uvloop`는 Windows를 공식 지원하지 않아 개발 환경 이식성 제약을 추가한다.
  3. 따라서 표준 `asyncio` 이벤트 루프는 유지하고, 플랫폼 제약 없이 Python 3.14.10 wheel(`httptools==0.9.0`)이 바로 동작하는 `httptools`만 채택했다.
- **성능 비교 기준선 유의사항**: 기존 [03-stack-comparison-plan.md](03-stack-comparison-plan.md)의 측정값은 `h11 + asyncio` 기준이므로, `httptools` 적용 이후 측정값과 혼용하지 않고 조건을 구분한다.

### 변경 범위
- [requirements-dev.txt](../../requirements-dev.txt): `httptools` 추가 (`httptools==0.9.0` 설치, `uvicorn` 기본 `http="auto"`가 `HttpToolsProtocol`을 자동 선택하며 `loop="auto"`는 `asyncio` 유지)
- [app.py](../../app.py), [storage.py](../../storage.py): 수정 없음

### 실측 효과 ([compare_h11_vs_httptools.py](../../scripts/compare_h11_vs_httptools.py), `loop="asyncio"` 동일 조건)

#### (A) 단건 요청 (`C=1`, Raw Socket keep-alive)

| 작업 | `h11` RTT p50 (p99) | `httptools` RTT p50 (p99) | 변화 (p50) | 파싱 밖(`1`) 변화 | 응답 쓰기(`7`) 변화 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `GET small` | 206.0 µs (311.2) | **121.1 µs** (201.8) | **-84.9 µs (-41.2%)** | 93.2 → 48.7 µs | 57.0 → 23.7 µs |
| `PUT small` | 222.0 µs (533.2) | **144.9 µs** (277.9) | **-77.2 µs (-34.8%)** | 98.5 → 55.8 µs | 54.6 → 24.9 µs |
| `INCR cnt` | 226.8 µs (679.1) | **142.5 µs** (294.0) | **-84.3 µs (-37.2%)** | 99.7 → 52.2 µs | 53.4 → 24.9 µs |
| `PUT large (~16KB)` | 515.3 µs (1146.0) | **429.8 µs** (906.4) | **-85.5 µs (-16.6%)** | 121.1 → 74.8 µs | 60.6 → 27.3 µs |

#### (B) 동시 요청 (`C=50`, 총 `N=5,000` `GET small` keep-alive)

| HTTP 파서 | 처리량 (RPS) | p50 | p95 | p99 |
| --- | ---: | ---: | ---: | ---: |
| `h11` | 5,051.9 /s | 8.52 ms | 10.64 ms | 11.33 ms |
| `httptools` | **7,813.3 /s** | **5.62 ms** | **6.81 ms** | **7.72 ms** |

- 동시 `C=50` 처리량이 **+54.7% (1.55배)** 향상됐다.

---

## 5. 누적 효과 요약 (`PUT large ~16KB` 및 `GET small` 기준)

- **`GET small` (`C=1`)**: `206.0 µs → 121.1 µs` (**-41.2%**, `httptools` 적용)
- **`PUT large ~16KB` (`C=1`)**: `820.4 µs → 511.7 µs` (`_json_content_supported` 제거) → **`429.8 µs`** (`httptools` 적용, **누적 -47.6%**)
- **남은 주요 구간**: `FastAPI` 라우팅/`solve_dependencies`(`~38~42 µs`), `httptools` 파싱/응답 쓰기(`~72~102 µs`), 큰 JSON의 경우 `app.py` `json.loads` + `storage.py` 크기 검사용 `json.dumps`(`~270 µs` @16KB).

---

## 6. 테스트 검증 (`httptools==0.9.0`, `asyncio` 유지)

- `.venv/bin/pytest -q`: **623 passed, 38 skipped** (실패 없음; 38건은 외부 JMeter/MySQL 등 조건부 스킵)
- `.venv/bin/pytest tests/scenario_01_storage/test_http_integration.py tests/scenario_01_storage/test_ttl_http_integration.py -v`: **41 passed** (스킵 없음, 실제 `uvicorn` TCP 통합 테스트)
- `.venv/bin/pytest tests/scenario_01_storage/test_json_types_extension.py -v`: **65 passed** (`storage.py`·테스트·명세 동기화 검증)

---

## 7. 적용 후 1요청 비중 재측정 및 후속 검토 (`FastAPI` vs `Starlette`)

### (1) 적용 후 1요청 세부 구간 비중 ([profile_deep_dive.py](../../scripts/profile_deep_dive.py), `httptools + asyncio`)
- **`GET small` (p50 `125.9 µs`)**: `TCP+uvicorn+httptools` `75.9 µs` (`60.4%`), `FastAPI+JSONResponse` `47.5 µs` (`37.7%`, 이 중 라우팅 진입 `38.0 µs` `30.2%`), `MiniRedis` `2.4 µs` (`1.9%`)
- **`PUT small` (p50 `142.5 µs`)**: `TCP+uvicorn+httptools` `79.5 µs` (`55.8%`), `FastAPI+JSON` `58.2 µs` (`40.9%`), `MiniRedis` `4.7 µs` (`3.3%`)
- **`INCR cnt` (p50 `153.8 µs`)**: `TCP+uvicorn+httptools` `84.9 µs` (`55.2%`), `FastAPI+JSON` `60.6 µs` (`39.4%`), `MiniRedis` `5.9 µs` (`3.9%`)
- **`GET large ~16KB` (p50 `263.6 µs`)**: `JSONResponse` 직렬화 `133.0 µs` (`50.5%`), `TCP+uvicorn+httptools` `89.1 µs` (`33.8%`), `FastAPI` 라우팅 `40.4 µs` (`15.3%`), `MiniRedis` `2.4 µs` (`0.9%`)
- **`PUT large ~16KB` (p50 `425.1 µs`)**: `MiniRedis.set` `143.7 µs` (`33.8%`), `app.py json.loads` `119.9 µs` (`28.2%`), `TCP+uvicorn+httptools` `103.3 µs` (`24.3%`), `FastAPI+응답` `54.7 µs` (`12.8%`)

### (2) 프레임워크 비교 실측 ([compare_fastapi_vs_starlette.py](../../scripts/compare_fastapi_vs_starlette.py), `uvicorn + httptools + asyncio`)
- **`C=1` 단건 (`p50`)**:
  - `GET small`: `FastAPI` `110.4 µs` → `Starlette` **`78.4 µs`** (-29.1%) / `Raw ASGI` **`60.6 µs`** (-45.2%)
  - `PUT small`: `FastAPI` `123.5 µs` → `Starlette` **`80.7 µs`** (-34.6%) / `Raw ASGI` **`66.5 µs`** (-46.2%)
  - `INCR cnt`: `FastAPI` `128.2 µs` → `Starlette` **`89.0 µs`** (-30.6%) / `Raw ASGI` **`61.7 µs`** (-51.9%)
- **`C=50, N=5,000` (`GET small`)**:
  - `FastAPI` `8,018.0 RPS` → `Starlette` **`12,464.9 RPS`** (+55.5%) / `Raw ASGI` **`15,282.8 RPS`** (+90.6%)

### (3) 등록된 후속 검토 이슈 (미확정)
- 부모 이슈: [#9 성능 개선 제안: 1요청 구간 분석 기반 후속 검토](https://github.com/bennjung/jungle-mini-redis-week11/issues/9) (참고: [#8](https://github.com/bennjung/jungle-mini-redis-week11/issues/8))
  - [#10](https://github.com/bennjung/jungle-mini-redis-week11/issues/10): 웹 프레임워크 경량화 (`FastAPI` → `Starlette` 또는 순수 ASGI)
  - [#11](https://github.com/bennjung/jungle-mini-redis-week11/issues/11): ASGI 서버 교체 (`uvicorn` → `Granian` 등)
  - [#12](https://github.com/bennjung/jungle-mini-redis-week11/issues/12): JSON 직렬화·역직렬화 최적화 (`orjson` 및 큰 JSON 중복 처리 완화)
