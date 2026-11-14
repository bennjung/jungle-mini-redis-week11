# 01 TTL 테스트 계약 (초안)

## 기준과 상태

- 기준: [01 TTL 계약](01-ttl-contract.md), [04 날씨 TTL/LDC 결정 로그](../scenario_04_response_cache/04-weather-ttl-decision-log.md), [REST API 9절](../../rest-api.md).
- 이 문서는 확정된 TTL 행동을 정확한 입력·관찰로 구체화한 테스트 명세 초안이다. 아래 표본은 계약을 검증하기 위한 입력이며 새 기능·정책을 추가하지 않는다. 정확한 표본을 사용자 검토 없이 동결된 계약이라고 주장하지 않는다.
- 저장소 코어 테스트: `tests/scenario_01_storage/test_ttl_storage.py`. Active 만료 테스트: `tests/scenario_01_storage/test_ttl_active.py`. HTTP 표면 테스트: `tests/scenario_01_storage/test_ttl_http.py`. 타이머 task 테스트: `tests/scenario_01_storage/test_ttl_timer.py`(후속 timer 브랜치). 날씨 소비자 기능 테스트: `tests/scenario_04_response_cache/test_weather_cache.py`.
- 테스트 helper: `tests/scenario_01_storage/ttl_state_helpers.py`, `tests/scenario_01_storage/ttl_clock_helpers.py`.
- 구현 전에 테스트를 실행해 실패 원인을 확인하고, 구현 뒤 같은 테스트를 다시 실행한다. 테스트에 맞추어 TTL 계약을 완화하지 않는다.
- `[미정]` 항목은 기대 결과를 임의로 만들지 않는다. 결정 전에는 해당 테스트를 작성하지 않거나 `[미검증]`으로 남긴다.
- 모든 테스트는 새 저장소에서 실행한다. 실패를 통과시키기 위한 `xfail`·자동 재시도는 사용하지 않는다.

## 공통 절차와 관찰

- 사전 상태는 `set`·`expire`로 만든다. `_data`·`_expires`에 직접 값을 넣지 않는다. 만료된 사전 상태는 `set(..., ttl=...)` 뒤 가짜 시계를 만료 시각 이상으로 진행해 만든다.
- 가짜 벽시계 기준값: `BASE_MS = 1_700_000_000_000`. `advance(ms)`는 현재 값을 `ms`만큼 증가시킨다.
- `[제안]` 테스트 주입 인터페이스: `MiniRedis(clock=clock, sampler=sampler)`.
  - `clock()`은 정수 epoch ms를 반환한다. 기본값은 실제 벽시계다.
  - `sampler(keys, k)`는 `keys`에서 최대 `k`개를 반환한다. 기본값은 중복 없는 무작위 표본이다.
  - `MiniRedis()`는 기존처럼 동작해야 한다. 이 인터페이스는 테스트 코드 작성 전에 승인한다.
- `[확정]` active tick은 테스트에서 `_maybe_active_expire()` 또는 `_active_expire_cycle(now)`를 직접 호출해 재현한다. 실제 100ms 타이머를 기다리지 않는다. 타이머 task의 주기·유휴 실행은 T1-19(후속 timer 브랜치)에서 확인한다.
- `snapshot(store)`는 기존처럼 `_data`의 사본을 반환한다. `ttl_snapshot(store)`는 `(_expires.copy(), list(_ttl_keys), _ttl_pos.copy())`를 반환한다. 테스트 전용이며 실행 코드의 메서드가 아니다.
- 불변 조건 검사: `set(_expires) == set(_ttl_keys) == set(_ttl_pos)`, 모든 키의 위치 일치, 세 길이 일치, `_ttl_keys` 중복 없음.
- 각 호출 직후 반환값 전체와 `_data`·TTL 구조를 확인한다. 반환값의 필드 구성과 타입을 비교하고, 딕셔너리 순서는 강제하지 않는다.
- HTTP 테스트는 `fastapi.testclient.TestClient`로 `create_app(MiniRedis(clock=...))`를 감싼 클라이언트를 사용한다. 실제 TCP 통합 테스트는 별도로 둔다.
- 사전 상태 준비 실패와 검증 실패를 실행 기록에서 구분한다. 실패하면 다음 단계로 진행해 통과시키지 않는다.

## T1-01. set으로 TTL 저장·조회

- 사전 상태: 없음.
- 입력: `set("ttl:one", "hello", ttl=600_000)` → `ttl("ttl:one")` → `get("ttl:one")`.
- 검증 시점: 각 호출 직후.
- 기대:
  - `set` → `{"ok": True}`.
  - `ttl` → `{"found": True, "ttl": 600_000}`.
  - `get` → `{"found": True, "value": "hello"}`.
- 유지: 불변 조건. 보조 키가 있으면 보존한다.

## T1-02. 만료 경계

- 사전 상태: `set("ttl:one", "hello", ttl=600_000)`.
- 입력·기대:
  - `advance(599_999)` → `ttl` = `{"found": True, "ttl": 1}`, `get` = `{"found": True, "value": "hello"}`.
  - `advance(1)` → `get` = `{"found": False}`, `ttl` = `{"found": False}`, `delete` = `{"found": False}`.
- 유지: 경계 도달 시 물리적 제거 여부와 무관하게 논리 결과가 같다. `ttl_snapshot`에서 `_expires`·`_ttl_keys`·`_ttl_pos`가 제거됐는지 확인한다.

## T1-03. TTL 없는 키

- 사전 상태: `set("plain:one", "hello")`.
- 입력: `ttl("plain:one")` → `get("plain:one")`.
- 기대: `ttl` = `{"found": True, "ttl": -1}`, `get` = `{"found": True, "value": "hello"}`.
- 유지: TTL 구조에 키가 없다.

## T1-04. TTL 없는 덮어쓰기

- 사전 상태: `set("ttl:one", "before", ttl=600_000)`.
- 입력: `set("ttl:one", "after")` → `ttl("ttl:one")` → `advance(600_000)` → `get("ttl:one")`.
- 기대: `set` = `{"ok": True}`, `ttl` = `{"found": True, "ttl": -1}`, `get` = `{"found": True, "value": "after"}`.
- 유지: 이전 TTL이 제거되고 새 `value`는 만료되지 않는다.

## T1-05. TTL 갱신

- 사전 상태: `set("ttl:one", "hello", ttl=1_000)`.
- 입력: `advance(500)` → `set("ttl:one", "hello2", ttl=2_000)` → `ttl` → `advance(1_999)` → `get` → `advance(1)` → `get`.
- 기대:
  - 갱신 직후 `ttl` = `{"found": True, "ttl": 2_000}`.
  - 만료 1ms 전 `get` = `{"found": True, "value": "hello2"}`.
  - 만료 시각 `get` = `{"found": False}`.
- 유지: 갱신은 이전 만료 시각을 연장하지 않고 새 기간을 적용한다.

## T1-06. set의 TTL 입력 거절

- 사전 상태: `set("ttl:one", "before")`와 보조 키 `set("other:one", "keep")`.
- 입력: 각각 `set("ttl:one", "after", ttl=0)`, `ttl=-1`, `ttl=True`, `ttl=1.5`, `ttl="1000"`.
- 기대: 모두 `{"error": "INVALID_INPUT"}`. `get("ttl:one")`은 `"before"`, 보조 키는 `"keep"`, TTL 구조는 변하지 않는다.
- 유지: 입력 거절은 상태를 변경하지 않는다.

## T1-07. 만료된 키의 덮어쓰기

- 사전 상태: `set("ttl:one", "before", ttl=1_000)` → `advance(1_000)`.
- 입력 A: `set("ttl:one", "after")` → `ttl` → `advance(1_000)` → `get`.
- 기대 A: `ttl` = `{"found": True, "ttl": -1}`, 이후 `get` = `{"found": True, "value": "after"}`.
- 입력 B: 같은 사전 상태에서 `set("ttl:one", "after", ttl=2_000)` → `ttl` → `advance(1_999)` → `get`.
- 기대 B: `ttl` = `{"found": True, "ttl": 2_000}`, `get` = `{"found": True, "value": "after"}`.
- 유지: 지난 TTL을 물려주지 않는다.

## T1-08. expire

- 사전 상태: `set("ttl:one", "hello")`와 보조 키.
- 입력·기대:
  - `expire("ttl:one", 60_000)` → `{"found": True, "ttl": 60_000}`. `ttl` = 60_000.
  - `expire("ttl:one", 30_000)` → `{"found": True, "ttl": 30_000}`. `ttl` = 30_000.
  - `advance(30_000)` → `get` = `{"found": False}`.
  - 없는 키 `expire("ttl:missing", 1_000)` → `{"found": False}`.
  - 만료된 키 `expire("ttl:one", 1_000)` → `{"found": False}`. `get` = `{"found": False}`.
  - `expire("ttl:one", 0)`와 `expire("ttl:one", -1)`은 각각 새 사전 상태에서 `{"found": True, "ttl": 0}`. `get` = `{"found": False}`. TTL 구조에서 제거된다.
- 유지: `expire`는 `value`를 바꾸지 않는다. 즉시 삭제는 `value`와 TTL을 함께 제거한다.

## T1-09. expire 입력 거절

- 사전 상태: `set("ttl:one", "before")`.
- 입력: `expire("ttl:one", None)`, `True`, `1.5`, `"1000"`.
- 기대: 모두 `{"error": "INVALID_INPUT"}`. `get("ttl:one")`은 `"before"`, TTL 구조는 변하지 않는다.

## T1-10. ttl 조회

- 사전 상태: `set("ttl:one", "hello", ttl=60_000)`, `set("plain:one", "hello")`.
- 입력·기대:
  - `ttl("ttl:one")` = `{"found": True, "ttl": 60_000}`.
  - `ttl("plain:one")` = `{"found": True, "ttl": -1}`.
  - `ttl("missing")` = `{"found": False}`.
  - `advance(60_000)` → `ttl("ttl:one")` = `{"found": False}`.

## T1-11. persist

- 사전 상태: `set("ttl:one", "hello", ttl=60_000)`, `set("plain:one", "hello")`.
- 입력·기대:
  - `persist("ttl:one")` = `{"found": True, "persisted": True}`. `ttl` = -1. `value` 유지.
  - `persist("plain:one")` = `{"found": True, "persisted": False}`.
  - `persist("missing")` = `{"found": False}`.
  - `advance(60_000)` → `get("ttl:one")` = `{"found": True, "value": "hello"}`.

## T1-12. delete·get의 lazy 만료

- 사전 상태: `set("ttl:one", "hello", ttl=1_000)` → `advance(1_000)`.
- 입력·기대:
  - `get("ttl:one")` = `{"found": False}`. `ttl_snapshot`에서 제거됨.
  - 같은 사전 상태에서 `delete("ttl:one")` = `{"found": False}`. `ttl_snapshot`에서 제거됨.
- 유지: 만료된 키와 처음부터 없던 키의 결과 필드 구성이 같다.

## T1-13. 불변 조건

- T1-01~12의 각 연산 직후 불변 조건을 검사한다.
- 추가로 `set` → `expire` → `persist` → `set`(TTL 없음) → `delete` 순서와, TTL 키와 TTL 없는 키를 섞은 상태에서 각 연산 후 검사한다.
- `ttl_snapshot`의 사본을 수정해도 저장소 상태가 변하지 않는지 확인한다. `snapshot(store)`는 `_data`만 반환하는 기존 의미를 유지한다.

## T1-14. active 만료 — 기본 표본

> T1-14~18은 tick 함수를 직접 호출하고 `keys[:k]` 고정 표본 선택기를 쓰는 조건부 초안이다. T1-19는 후속 timer 브랜치의 테스트다.

- 사전 상태: TTL 1_000ms인 키 25개를 `set`으로 만든다. `advance(1_000)`.
- 입력: 표본 선택기는 현재 `_ttl_keys`의 앞에서부터 최대 20개를 반환한다. `_maybe_active_expire()`를 호출한다.
- 기대: 첫 사이클에서 앞 20개가 제거되고, 만료 비율 100%이므로 같은 트리거에서 남은 5개를 한 번 더 실행해 모두 제거한다. 표본 선택기 호출은 2회다.
- 유지: TTL 없는 키와 TTL이 남은 키는 보존한다. 불변 조건.

## T1-15. active 만료 — 25% 미만이면 반복하지 않음

- 사전 상태: TTL 1_000ms인 키 4개를 먼저 `set`하고 `advance(1_000)`로 만료시킨다. 이어 TTL 1_000ms인 키 16개를 `set`한다. `_ttl_keys`에는 만료 4개가 앞에, 살아 있는 16개가 뒤에 있다.
- 입력: 표본 선택기가 20개를 모두 반환하게 하고 `_maybe_active_expire()`를 호출한다.
- 기대: 만료 4개만 제거되고, 4/20 = 20% < 25%이므로 표본 선택기는 1회만 호출된다. 살아 있는 16개는 보존된다.
- 유지: 불변 조건.

## T1-16. active 만료 — 25% 이상이면 1회 더

- 사전 상태: TTL 1_000ms인 키 5개를 먼저 `set`하고 `advance(1_000)`로 만료시킨다. 이어 TTL 1_000ms인 키 15개를 `set`한다. `_ttl_keys`에는 만료 5개가 앞에, 살아 있는 15개가 뒤에 있다.
- 입력: 표본 선택기가 20개를 모두 반환하게 하고 `_maybe_active_expire()`를 호출한다.
- 기대: 5/20 = 25%이므로 표본 선택기가 2회 호출된다. 두 번째 표본에서 남은 만료 키가 없으면 추가 반복은 없다. 만료 5개가 제거되고 15개가 보존된다.
- 유지: 최대 2회. 불변 조건.

## T1-17. active tick — 호출마다 실행

- 사전 상태: TTL 60_000ms인 키 1개.
- 입력: `_maybe_active_expire()`를 두 번 호출한다.
- 기대: 표본 선택기 호출은 2회다. tick 안에 100ms 게이트를 두지 않는다.
- 유지: 100ms 주기는 타이머 task가 지킨다. task의 주기·유휴 실행은 T1-19(후속 timer 브랜치)에서 확인한다.

## T1-18. active 만료 — 살아 있는 키와 갱신된 TTL 보존

- 사전 상태: TTL 60_000ms인 키 2개.
- 입력: 표본 선택기가 두 키를 모두 반환하게 하고 `_maybe_active_expire()`를 호출한다.
- 기대: 둘 다 만료되지 않았으므로 제거되지 않는다.
- 추가: 키 하나의 TTL을 1_000ms로 줄이고 `advance(1_000)` 뒤 같은 키를 표본으로 반환하기 전에 `expire`로 TTL을 60_000ms로 갱신한다. 트리거 후 그 키는 삭제되지 않는다. 삭제 전 현재 `_expires`를 확인하는 동작을 검증한다.

## T1-19. 타이머 — 유휴 상태 (후속 timer 브랜치)

- 사전 상태: TTL 1_000ms인 키 1개. `advance(1_000)`.
- 입력: 요청을 보내지 않고 lifespan 타이머 task를 100ms 이상 실행한다.
- 기대: 요청이 없어도 tick이 실행되어 만료 키가 제거된다.
- 유지: 타이머 task 구현과 함께 후속 브랜치에서 작성한다. 이번 조각에는 포함하지 않는다.

## T1-20. HTTP TTL 표면

`TestClient(create_app(MiniRedis(clock=...)))`로 각 행을 독립 실행한다. 사전 상태는 `PUT /kv`로 만든다.

| 하위 ID | 사전 상태 | 요청 | 기대 |
| --- | --- | --- | --- |
| put-ttl | 없음 | `PUT /kv` `{"key":"ttl:one","value":"hello","ttl":600000}` | 200, `{"ok":true}` |
| put-no-ttl | 없음 | `PUT /kv` `{"key":"ttl:one","value":"hello"}` | 200, `{"ok":true}`. `GET /kv/ttl`은 `ttl:-1` |
| put-null-ttl | `PUT /kv` ttl 600000 | `PUT /kv` `{"key":"ttl:one","value":"hello","ttl":null}` | 200, `{"ok":true}`. `GET /kv/ttl`은 `ttl:-1` |
| ttl-get | `PUT /kv` ttl 600000 | `GET /kv/ttl?key=ttl%3Aone` | 200, `{"found":true,"ttl":600000}` |
| ttl-missing | 없음 | `GET /kv/ttl?key=missing` | 200, `{"found":false}` |
| expire-set | `PUT /kv` TTL 없음 | `POST /kv/expire` `{"key":"ttl:one","ttl":60000}` | 200, `{"found":true,"ttl":60000}` |
| expire-delete | `PUT /kv` ttl 600000 | `POST /kv/expire` `{"key":"ttl:one","ttl":0}` | 200, `{"found":true,"ttl":0}`. `GET /kv`는 `found:false` |
| expire-missing | 없음 | `POST /kv/expire` `{"key":"missing","ttl":1000}` | 200, `{"found":false}` |
| persist | `PUT /kv` ttl 600000 | `POST /kv/persist` `{"key":"ttl:one"}` | 200, `{"found":true,"persisted":true}`. `GET /kv/ttl`은 `ttl:-1` |
| persist-no-ttl | `PUT /kv` TTL 없음 | `POST /kv/persist` `{"key":"ttl:one"}` | 200, `{"found":true,"persisted":false}` |
| persist-missing | 없음 | `POST /kv/persist` `{"key":"missing"}` | 200, `{"found":false}` |
| get-hides-ttl | `PUT /kv` ttl 600000 | `GET /kv?key=ttl%3Aone` | `{"found":true,"value":"hello"}`. TTL 필드 없음 |
| put-invalid-ttl | `PUT /kv` ttl 600000, value `"before"` | `PUT /kv` `{"key":"ttl:one","value":"after","ttl":0·-1·1.5·"1000"·true}` | 400, `{"error":"INVALID_INPUT"}`. `value`·TTL 보존 |
| expire-invalid-ttl | `PUT /kv` ttl 600000, value `"before"` | `POST /kv/expire` `{"key":"ttl:one","ttl":누락·null·1.5·"1000"·true}` | 400, `{"error":"INVALID_INPUT"}`. `value`·TTL 보존 |
| expire-boundary | `PUT /kv` ttl 600000 | 만료 시각까지 가짜 시계 진행, `GET /kv/ttl?key=ttl%3Aone` | 200, `{"found":false}` |

- HTTP 응답 본문은 객체 필드 순서를 강제하지 않는다. `true`·`false`·`null`의 JSON 표기를 구분한다.
- 만료된 키와 없는 키의 `GET /kv` 결과는 `{"found":false}`로 같다.
- `GET /kv/ttl`의 `ttl`은 남은 시간이고, `expire` 응답의 `ttl`은 방금 설정한 TTL이다.

## T1-21. 날씨 소비자 기능 테스트

- 파일: `tests/scenario_04_response_cache/test_weather_cache.py`.
- 하네스: KV HTTP API를 직접 호출하고, 원본 호출 수를 세는 가짜 함수/클래스를 둔다. `/weather` 엔드포인트는 만들지 않는다.
- 사전 상태: `weather:37.566:126.978`이 없다. 다른 키 `weather:37.502:127.025`에는 별도 응답을 저장해 둔다. 가짜 원본 응답 문자열은 `{"temperature":22,"observed_at":"2026-11-14T12:00:00Z"}`. TTL 예시는 600,000ms.
- 입력·기대:
  - 최초 요청 → 원본 1회, 저장소에 응답 저장.
  - TTL 안 재요청 → 원본 1회 유지, 저장 응답 재사용.
  - 만료 시각까지 진행 후 요청 → 원본 2회, 새 응답 저장.
  - 수동 `DELETE` 후 요청 → 원본 3회.
  - 다른 키 `weather:37.502:127.025`는 영향을 받지 않는다.
- 유지: 응답 내용이 같아도 원본 호출 수로 재사용·재조회를 구분한다.
- 미정: 원본 실패 시 동작, 정확한 TTL 값, 원본 응답 스키마. 실패 주입 테스트는 결정 후 추가한다.

## T1-23. incr·decr의 lazy 만료와 TTL 유지

- 사전 상태: `set("count:ttl", 10, ttl=60_000)`, `set("count:plain", 10)`.
- 입력·기대:
  - `incr("count:ttl", 5)` → `{"ok": True, "value": 15}`. `ttl("count:ttl")` = `{"found": True, "ttl": 60_000}`.
  - `decr("count:ttl", 3)` → `{"ok": True, "value": 12}`. TTL은 그대로 60_000이다.
  - `advance(60_000)` → `incr("count:ttl", 1)` = `{"error": "NOT_FOUND"}`. `get("count:ttl")` = `{"found": False}`.
  - `incr("count:plain", 1)` = `{"ok": True, "value": 11}`. TTL 구조에 키가 없다.
- 유지: 만료된 키는 증감으로 되살아나지 않는다. TTL 없는 키의 동작은 바뀌지 않는다. 입력 거절 시에는 lazy 정리도 하지 않는다.

## T1-22. 미정 항목 (테스트 작성 보류)

- `[확정]` active 표본은 중복 없이 뽑는다(`random.sample`). `T1-14`~`T1-18`의 고정 표본 선택기는 이 조건과 맞는다.
- `[확정]` active 만료는 lifespan의 100ms 주기 타이머가 실행한다(D-24). `T1-14`~`T1-18`은 tick 함수를 직접 호출한다. `T1-19`는 후속 timer 브랜치에서 작성한다.
- `[확정]` `incr`·`decr`는 lazy 만료 검사를 하고, 만료된 키는 `NOT_FOUND`, 살아 있는 키의 TTL은 유지한다(T1-23).
- `[미정]` `POST /kv/expire`·`POST /kv/persist`의 알 수 없는 추가 필드 정책. 결정 전에는 해당 입력을 테스트하지 않는다.
- `[미정]` 원본 실패·MiniRedis 실패 시 사용자 응답. 04 문서의 미정 항목과 함께 결정한다.

## 실행·구현 순서

1. 이 문서의 테스트 ID와 입력을 사용자 검토로 동결한다.
2. 저장소 테스트를 먼저 작성하고, 구현 전에 실행해 실패 원인을 기록한다. 미정 항목은 작성하지 않는다.
3. HTTP 표면 테스트와 날씨 소비자 테스트를 작성한다.
4. `storage.py`의 TTL 저장·조회·삭제·lazy/active를 구현하고 같은 테스트를 다시 실행한다.
5. `app.py`의 TTL 엔드포인트를 구현하고 HTTP 테스트를 다시 실행한다.
6. 테스트의 호출 대상·입력·검증 시점이 이 문서와 일치하는지 다시 대조한다. 통과·실패·미검증을 구분해 기록한다.
- 구현 전 테스트가 도구 미설치·미정의 함수로 중단된 경우와 기대 결과 assertion이 실패한 경우를 구분한다.
