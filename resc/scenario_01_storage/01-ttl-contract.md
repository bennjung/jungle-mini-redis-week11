# 01 TTL 계약 (MiniRedis 저장소)

## 상태와 근거

- 이 문서는 MiniRedis의 일반 TTL 동작 계약을 정의한다. 04의 날씨 응답 캐시는 이 계약을 사용하는 소비자이며, TTL 자체의 계약을 04가 중복 정의하지 않는다.
- 결정 근거: [04 날씨 TTL/LDC 결정 로그](../scenario_04_response_cache/04-weather-ttl-decision-log.md)의 D-03~D-22.
- 기존 저장 계약: [1. 외부에서 값 보관·조회·폐기](01-storage-scenario.md).
- HTTP 표면: [REST API 명세](../../rest-api.md).
- `[확정]`은 사용자와 합의한 계약, `[미정]`은 아직 결정하지 않은 항목, `[제안]`은 검토 후보다. `[미정]`·`[제안]`은 구현 계약이 아니다.
- 이 문서는 TTL의 저장소 동작을 소유한다. HTTP 메서드·경로·JSON·상태 코드는 `rest-api.md`가 소유한다.

## 1. 범위와 책임

- TTL은 API 응답 캐시 전용 기능이 아니다. 세션·레이트 리밋·임시 데이터도 같은 저장소 TTL을 쓸 수 있다.
- 저장소(`MiniRedis`)가 키별 만료 시각을 소유하고 판정한다. 앱은 TTL 기간을 전달하는 소비자다.
- HTTP는 JSON 본문·쿼리 구조와 필수 필드를 검사하고, 저장소는 `key`·`value`·TTL 타입·범위와 상태 변경을 담당한다. 기존 01의 경계를 따른다.
- 영속화(재시작 후 복구)는 이 계약의 범위가 아니다. `phase.md`의 선택 요구사항과 구분한다.
- 다중 스레드·다중 프로세스 동시 호출은 이 계약의 범위가 아니다. 현재 서버는 단일 이벤트 루프·`uvicorn workers=1`로 요청을 하나씩 처리한다.

## 2. 자료구조

| 구조 | 내용 | 역할 |
| --- | --- | --- |
| `_data` | `key → value` | 전체 키·`value`. 기존 저장소와 같다. |
| `_expires` | `key → 만료 시각(정수 epoch ms)` | TTL이 있는 키만 담는다. TTL의 진실원이다. |
| `_ttl_keys` | `list[key]` | TTL 키 배열. 무작위 표본 선택용 보조 인덱스다. |
| `_ttl_pos` | `key → _ttl_keys에서의 위치` | O(1) 추가·삭제용 위치 인덱스다. |

### 2.1 불변 조건

- `set(_expires) == set(_ttl_keys) == set(_ttl_pos)`
- 모든 `key`에 대해 `_ttl_keys[_ttl_pos[key]] == key`
- `len(_expires) == len(_ttl_keys) == len(_ttl_pos)`
- `_ttl_keys`에 중복 키가 없다.
- `_ttl_keys`·`_ttl_pos`는 TTL의 두 번째 저장소가 아니다. `_expires`를 가리키는 표본 선택용 보조 인덱스다.

### 2.2 갱신 규칙

| 연산 | `_data` | `_expires` | `_ttl_keys` / `_ttl_pos` |
| --- | --- | --- | --- |
| `set(key, value, ttl)` — 새 TTL | `value` 저장 | 추가 | 목록 끝에 추가, 위치 기록 |
| `set(key, value, ttl)` — 기존 TTL 갱신 | `value` 저장 | 만료 시각 갱신 | 변화 없음 |
| `set(key, value)` — TTL 없음(D-04) | `value` 저장 | 있으면 제거 | 있으면 swap-pop 제거 |
| `expire(key, ttl > 0)` | 변화 없음 | 추가·갱신 | 없으면 목록 끝에 추가 |
| `expire(key, ttl <= 0)` | 키 제거 | 제거 | swap-pop 제거 |
| `persist(key)` | 변화 없음 | 있으면 제거 | 있으면 swap-pop 제거 |
| `delete(key)` | 키 제거 | 있으면 제거 | 있으면 swap-pop 제거 |
| lazy 만료 | 키 제거 | 제거 | swap-pop 제거 |
| active 만료 | 키 제거 | 제거 | swap-pop 제거 |

- swap-pop 순서: `i = _ttl_pos.pop(key)`, `last = _ttl_keys.pop()`, `i < len(_ttl_keys)`이면 `_ttl_keys[i] = last`, `_ttl_pos[last] = i`, 마지막에 `del _expires[key]`. `key`가 마지막 원소이면 `i == len(_ttl_keys)`가 되어 swap을 건너뛴다.
- 모든 연산은 시작 시 lazy 만료 검사를 적용한다. 단, 입력 검증이 실패하면 상태를 변경하지 않고 lazy 정리도 하지 않는다. 기존 01의 "입력 거절 시 상태 보존" 규칙을 따른다.

## 3. 시간과 만료 경계

- `[확정]` 벽시계의 절대 epoch 밀리초(`epoch ms`)를 쓴다.
- `_expires[key] = now_ms + ttl_ms`. `ttl_ms`는 호출자가 전달한 기간이다.
- `[확정]` `now_ms >= _expires[key]`이면 만료다. `now_ms < _expires[key]`이면 유효하다.
- `ttl(key)`의 남은 시간은 `_expires[key] - now_ms`다. 유효한 키의 남은 시간은 1ms 이상이다. `now_ms == _expires[key]`이면 이미 만료다.
- `[확정]` 테스트는 가짜 벽시계를 주입한다. 가짜 시계가 lazy 만료와 active 만료 모두를 제어한다.
- `[확정]` 운영에서는 실제 벽시계를 쓴다. NTP 등으로 시계가 뒤로 가면 TTL이 늘어나고, 앞으로 가면 일찍 만료된다. 이 비용은 향후 영속화 확장성을 위해 받아들인다.
- 영속화는 이 계약의 범위가 아니다. 다만 `_expires`에 절대 만료 시각을 저장해 두므로, 영속화를 도입할 때 그대로 복원할 수 있다. 도입 시 로드하면서 이미 만료된 키를 거르는 처리가 별도로 필요하다.

## 4. 저장소 함수 계약

기존 01의 키·`value` 검증은 그대로 적용한다. TTL 검증도 상태 변경 전에 수행한다. 모든 반환은 기존처럼 예외가 아니라 반환값으로 전달한다.

### 4.1 `set(key, value, ttl=None)`

- 키·`value` 검증은 기존 01을 따른다.
- `ttl`이 `None`이거나 생략되면 TTL을 두지 않는다. 기존 TTL이 있으면 제거한다(D-04).
- `ttl`은 `bool`이 아닌 정수여야 한다.
  - `ttl > 0`: `_expires[key] = now_ms + ttl`. 기존 TTL이 있으면 만료 시각을 갱신하고, 없으면 목록에 추가한다.
  - `ttl <= 0`: `INVALID_INPUT`.
  - `bool`·`float`·문자열·그 밖의 타입: `INVALID_INPUT`.
- 키가 만료됐지만 물리적으로 남아 있으면 lazy 만료로 먼저 제거하고, 새 키로 취급한다. 지난 TTL을 물려주지 않는다.
- 성공 반환은 `{"ok": True}`다. 다른 키는 변경하지 않는다.
- HTTP `"ttl": null`은 생략으로 취급한다(D-12).

### 4.2 `expire(key, ttl)`

- 키 검증과 lazy 만료 검사를 먼저 한다.
- 키가 없거나 만료됐으면 `{"found": False}`.
- `ttl`은 필수이며 `bool`이 아닌 정수여야 한다. `None`·`bool`·`float`·문자열은 `INVALID_INPUT`.
- `ttl > 0`: 기존 `value`는 그대로 두고 TTL을 설정하거나 갱신한다. TTL이 없던 키면 목록에 추가한다. 반환은 `{"found": True, "ttl": ttl}`.
- `ttl <= 0`: 키와 `value`를 즉시 삭제한다. 반환은 `{"found": True, "ttl": 0}`.
- 삭제 시 `_data`·`_expires`·`_ttl_keys`·`_ttl_pos`에서 모두 제거한다.

### 4.3 `ttl(key)`

- 키 검증과 lazy 만료 검사를 먼저 한다.
- 키가 없거나 만료됐으면 `{"found": False}`.
- TTL이 있으면 `{"found": True, "ttl": _expires[key] - now_ms}`.
- TTL이 없으면 `{"found": True, "ttl": -1}`.
- `ttl`은 참고값이다. 만료 판정의 최종 기준은 `get` 등 실제 연산의 lazy 검사다.

### 4.4 `persist(key)`

- 키 검증과 lazy 만료 검사를 먼저 한다.
- 키가 없거나 만료됐으면 `{"found": False}`.
- TTL이 있으면 `_expires`·`_ttl_keys`·`_ttl_pos`에서 제거하고 `{"found": True, "persisted": True}`.
- TTL이 없으면 `{"found": True, "persisted": False}`.
- `value`는 변경하지 않는다.

### 4.5 `get(key)`·`delete(key)`

- 기존 01의 동작에 lazy 만료 검사를 추가한다.
- 만료된 키는 없는 키로 취급한다. `get`은 `{"found": False}`, `delete`는 `{"found": False}`.
- 만료된 키를 발견하면 `_data`·`_expires`·`_ttl_keys`·`_ttl_pos`에서 제거한다.
- `delete`는 TTL이 있는 살아 있는 키를 삭제할 때 네 구조에서 모두 제거한다.

### 4.6 `incr(key, amount, ...)`·`decr(key, amount, ...)`

- `[미정]` TTL과 기존 카운터 연산의 관계는 아직 확정하지 않았다.
- `[제안]` lazy 만료 검사를 먼저 하고, 만료된 키는 `NOT_FOUND`로 취급한다. 살아 있는 TTL 키의 TTL은 유지한다(Redis의 `INCR`/`DECR`과 같은 방향). 이 제안은 구현 전에 확인한다.
- TTL을 유지하지 않고 제거하는 쪽으로 정하면, `incr`/`decr`가 `_expires`를 건드리는 규칙을 별도로 추가해야 한다.

## 5. lazy 만료

- `get`·`set`·`delete`·`expire`·`ttl`·`persist`는 시작 시 만료를 확인한다. `incr`·`decr`는 4.6의 미정 항목을 따른다.
- 만료된 키를 만나면 물리적으로 제거하고 없는 키로 응답한다. 제거 대상은 `_data`·`_expires`·`_ttl_keys`·`_ttl_pos`다.
- `set`이 만료된 키를 만나면 이전 TTL을 물려주지 않고 새 키로 취급한다. 새 `ttl`이 있으면 새로 걸고, 없으면 TTL 없이 저장한다.
- 입력 검증이 실패하면 상태를 변경하지 않는다. 만료된 키의 lazy 정리도 하지 않는다.
- lazy 만료는 접근된 키의 정확성을 보장한다. 접근되지 않은 콜드 키의 물리적 제거는 active 만료가 담당한다.

## 6. active 만료

- `[확정]` 시간 기준 + 요청 트리거. 백그라운드 타이머 스레드를 두지 않는다.
- 마지막 사이클에서 100ms 이상 지났으면 요청 처리 시 1회 실행한다. 초당 최대 10회다.
- 1회 사이클은 `_ttl_keys`에서 최대 20개를 뽑아 만료를 확인하고, 만료된 키를 네 구조에서 제거한다.
- 표본의 25% 이상이 만료면 같은 트리거에서 1회 더 실행한다. 최대 2회다. 20개 표본이면 5개 이상이 만료된 경우다.
- 삭제 전에 현재 `_expires`와 키 존재를 확인한다. 표본을 뽑은 뒤 TTL이 갱신됐거나 키가 사라졌으면 삭제하지 않는다. 현재 서버는 단일 이벤트 루프 서버지만, 이 확인으로 갱신·삭제와의 순서 차이를 방어한다.
- `[확정]` 테스트는 시드 고정 RNG 또는 표본 선택기를 주입해 재현한다.
- `[확정]` 유휴 상태에서는 요청이 없으므로 active 만료도 멈춘다. 유휴 상태의 즉시 메모리 회수는 보장하지 않는다.
- `[미정]` 표본을 중복 없이 뽑을지(`random.sample`), 중복을 허용할지.
- `[미정]` active 사이클을 요청 처리 진입 시 실행할지 종료 시 실행할지, 어떤 연산이 트리거할지.
- `[미정]` 첫 요청에서 바로 사이클을 실행할지, 마지막 사이클 시각을 생성 시점으로 초기화해 100ms를 기다릴지.

## 7. HTTP 매핑

정확한 요청·응답 예시는 `rest-api.md`가 소유한다. 이 절은 저장소 함수와 HTTP 표면의 대응만 정한다.

| 동작 | HTTP | 입력 | 정상 응답 |
| --- | --- | --- | --- |
| 저장 + 선택 TTL | `PUT /kv` | JSON `key`, `value`, 선택 `ttl` | 200, `{"ok": true}` |
| TTL 설정·변경·즉시 삭제 | `POST /kv/expire` | JSON `key`, `ttl` | 200, `{"found": true, "ttl": ...}` 또는 `{"found": false}` |
| 남은 TTL 조회 | `GET /kv/ttl` | `key` 쿼리 | 200, `{"found": true, "ttl": ...}` 또는 `{"found": false}` |
| TTL 제거 | `POST /kv/persist` | JSON `key` | 200, `{"found": true, "persisted": ...}` 또는 `{"found": false}` |

- `PUT /kv`의 `ttl`은 생략 또는 `null`이면 TTL 없음이다. 양의 정수 밀리초만 허용하고, 그 밖의 값은 HTTP 400 `{"error":"INVALID_INPUT"}`으로 거절한다.
- `POST /kv/expire`의 `ttl`은 필수다. 양의 정수면 설정·변경, 0·음수면 즉시 삭제다. 타입·범위 위반은 HTTP 400 `{"error":"INVALID_INPUT"}`이다.
- 키가 없거나 만료된 경우 `expire`·`ttl`·`persist`는 HTTP 200과 `{"found": false}`를 반환한다. 404로 바꾸지 않는다.
- 만료된 키를 `expire`로 되살리지 않는다. `{"found": false}`다.
- HTTP 입력 구조·필수 필드 검사는 HTTP가, TTL 타입·범위는 저장소가 담당한다. 기존 01의 경계를 따른다.

## 8. 테스트 계약 (초안)

정확한 테스트 ID·입력·검증 시점은 별도 테스트 계약에서 확정한다. 아래는 검증 범위의 초안이다.

### 8.1 저장소 단위 테스트

- `tests/scenario_01_storage/test_ttl_*.py`에 둔다.
- 가짜 벽시계를 주입해 경계를 정확히 검증한다.
- 저장·TTL 설정·갱신·제거·삭제·lazy 만료·active 만료 후 `_data`·`_expires`·`_ttl_keys`·`_ttl_pos`의 불변 조건을 확인한다.
- `set`의 `ttl` 생략·`None`·0·음수·`bool`·`float`·문자열을 확인한다.
- `expire`의 양수·0·음수·없는 키·만료된 키·`null`을 확인한다.
- `ttl`의 남은 시간·TTL 없음(`-1`)·없는 키·만료된 키를 확인한다.
- `persist`의 TTL 제거·TTL 없음·없는 키·만료된 키를 확인한다.
- 만료 경계: `now < expiry`, `now == expiry`, `now > expiry`.
- 삭제 후 재추가, TTL 갱신, TTL 없는 덮어쓰기, 만료된 키 덮어쓰기를 확인한다.
- active 만료: 20개 표본, 25% 이상 1회 추가(최대 2회), 100ms 간격, 초당 10회, 시드 고정 RNG.
- 유휴 상태에서 active 만료가 멈추는지 확인한다.
- `ttl_snapshot(store)` 테스트 전용 도구로 `_expires`·`_ttl_keys`·`_ttl_pos`를 관찰한다. 기존 `snapshot(store)`는 `_data`만 보므로 그대로 둔다.

### 8.2 HTTP 기능 테스트

- `tests/scenario_04_response_cache/`에 둔다.
- 테스트 하네스가 KV HTTP API를 직접 호출하고, 원본은 가짜 함수로 대체한다.
- 원본 호출 수로 캐시 재사용과 재조회를 구분한다.
- 최초 요청 → 원본 1회, TTL과 함께 저장. TTL 안 재요청 → 원본 1회 유지. TTL 만료 후 → 원본 2회.
- 수동 `delete` 후 → 원본 재호출.
- 원본 실패 → 오류 응답, 캐시 저장 없음. 다음 요청에서 재시도.
- `ttl`·`persist`·`expire`의 HTTP 응답을 저장소 계약과 대조한다.
- 순차 요청만 다룬다. 동시 요청·stampede·single-flight는 이 범위가 아니다.

### 8.3 성능

- 03 성능 비교는 별도다. TTL 구현 뒤 03을 같은 조건으로 다시 측정하는 일은 04의 기능 테스트와 섞지 않는다.

## 9. 아직 정하지 않은 것

- active 표본을 중복 없이 뽑을지, 중복을 허용할지.
- active 사이클의 실행 시점(요청 진입·종료)과 트리거 연산.
- 첫 active 사이클의 초기 시각.
- `incr`·`decr`의 lazy 만료 검사와 TTL 유지 여부.
- `POST /kv/expire`·`POST /kv/persist`의 알 수 없는 추가 필드 허용·거절 정책.
- `rest-api.md`의 정확한 TTL 요청·응답 예시와 오류 예시.
- 테스트 ID·정확한 입력·검증 시점·기대 결과.

## 10. 관련 문서

- [04 날씨 TTL/LDC 결정 로그](../scenario_04_response_cache/04-weather-ttl-decision-log.md) — 문제 상황과 결정 근거.
- [04 API 응답 재사용·만료·수동 무효화](../scenario_04_response_cache/04-response-cache-scenario.md) — 소비자 시나리오.
- [1. 외부에서 값 보관·조회·폐기](01-storage-scenario.md) — 기존 저장 계약.
- [REST API 명세](../../rest-api.md) — HTTP 표면.
- [phase.md](../phase.md) — 과제 요구사항.
- [2. 같은 카운터의 동시 증가](../scenario_02_concurrent_counter/02-concurrent-counter-scenario.md) — 카운터 동작. TTL과의 관계는 4.6의 미정 항목이다.
