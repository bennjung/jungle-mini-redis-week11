# 01 TTL 탑다운 함수 계약 (3인 분업 초안)

## 기준과 상태

- 기준: [01 TTL 계약](01-ttl-contract.md), [01 TTL 테스트 계약](01-ttl-test-cases.md), [REST API 9절](../../rest-api.md), [04 날씨 TTL/LDC 결정 로그](../scenario_04_response_cache/04-weather-ttl-decision-log.md).
- 이 문서는 TTL 기능을 기존 `app.py` → `storage.py` 흐름에 배치하고, 테스트 작성부터 구현까지 3인 분업 경계를 정하는 설계 초안이다.
- `[확정]` 2026-11-14 구현 범위: 01 TTL 전체(저장소 코어·active tick·HTTP 표면·`incr`/`decr` lazy)와 04 날씨 소비자 테스트(T1-21)까지를 한 조각으로 구현한다. 타이머 task와 T1-19는 후속 timer 브랜치에서 한다. 03 성능 재측정·대량 무효화는 제외하고, 원본 API 실패 응답은 MiniRedis 범위가 아니므로 다루지 않는다.
- 함수 이름·인자·내부 helper 이름은 2026-11-14에 동결했다. 이후 변경은 계약 수정과 사용자 확인을 거친다.
- `[미정]` 항목은 구현으로 확정하지 않는다. 담당 범위의 테스트를 작성하기 전에 사용자 확인을 받는다.
- 새 계층·큐·잠금·별도 저장소를 만들지 않는다. 기존 `create_app(storage)`와 단일 `MiniRedis` 인스턴스, 단일 이벤트 루프·`uvicorn workers=1` 서버를 유지한다.

## 1. 전체 호출 구조

```text
HTTP — app.py
  PUT    /kv          → storage.set(key, value, ttl=선택)
  POST   /kv/expire   → storage.expire(key, ttl)
  GET    /kv/ttl      → storage.ttl(key)
  POST   /kv/persist  → storage.persist(key)

lifespan 타이머 task — app.py (후속 timer 브랜치)
  100ms마다           → storage._maybe_active_expire()  (C: tick 진입점)

저장소 — storage.py
  public 메서드
    → 입력 검증·lazy 만료           (B: TTL 코어)
    → _set_ttl / _clear_ttl / _drop_key (B: TTL 구조 변경)

  _maybe_active_expire()
    → _active_expire_cycle(now)     (C: 표본·만료 확인)
    → _drop_key(key)                (B: 네 구조에서 제거)
```

- `_maybe_active_expire`는 lifespan 타이머 task가 100ms마다 호출한다. public 메서드는 active를 직접 호출하지 않는다(D-24). 타이머 task 구현은 후속 브랜치에서 한다.
- `_drop_key`는 TTL 구조를 바꾸는 유일한 제거 경로다. B가 소유하고 C는 호출만 한다. C가 `_expires`·`_ttl_keys`·`_ttl_pos`를 직접 수정하지 않는다.
- `_set_ttl`·`_clear_ttl`은 B만 호출한다. C는 active 만료에서 `_drop_key`만 쓴다.
- HTTP는 저장 상태를 미리 조회하거나 TTL을 계산하지 않는다. 저장소는 HTTP 상태 코드를 반환하지 않는다.

### 1.1 TTL API 표면과 기존 명령의 TTL 영향

- **API 표면** — TTL 파라미터나 새 엔드포인트가 필요한 명령은 `PUT /kv`(`set`의 선택 `ttl`)와 `POST /kv/expire`·`GET /kv/ttl`·`POST /kv/persist`뿐이다. `GET /kv`·`DELETE /kv`의 요청·응답 형식은 바뀌지 않는다.
- **기존 명령의 TTL 영향** — `get`·`delete`는 만료된 키를 없는 키로 취급하는 lazy 검사가 필요하다(D-13). 새 명령이 아니라 기존 동작에 붙는 조건 검사다. 응답 형식은 그대로다.
- **active 트리거** — D-24에서 요청 트리거 대신 lifespan의 100ms 주기 타이머로 확정했다. 요청 처리 메서드는 active를 호출하지 않고 lazy만 한다. 타이머는 요청이 없어도 돈다.
- **A의 수정 범위** — `put_kv`와 새 세 엔드포인트만. `get_kv`·`delete_kv`는 수정하지 않는다.
- **B의 수정 범위** — `set`과 새 메서드, 그리고 `get`·`delete`의 lazy 한 줄. `get`·`delete`의 응답 형식은 바꾸지 않는다.

### 1.2 타이머 실행 구조

- `[확정]` active 만료는 FastAPI `lifespan`이 소유하는 100ms 주기 task가 실행한다(D-24). Redis의 serverCron·activeExpireCycle과 같은 시간 기준이다.
- 타이머는 같은 이벤트 루프에서 돈다. tick의 저장소 호출은 동기 함수로 끝나므로 사이클 중간에 요청 처리가 끼어들지 않는다. 별도 스레드·큐·잠금을 만들지 않는다.
- 요청이 없어도 실행되므로 유휴 상태의 콜드 키가 회수된다. 앱이 종료되면 task를 취소하고 대기한다.
- 테스트는 실제 100ms를 기다리지 않는다. `FakeClock`과 주입한 표본 선택기로 tick 함수를 직접 호출해 재현하고, 타이머 task의 시작·주기·종료는 별도 통합 테스트에서 확인한다.

### 1.3 함수 역할 한눈에

**저장소 public 메서드**

| 함수 | 소유 | 역할 | 반환 |
| --- | --- | --- | --- |
| `MiniRedis(clock=None, sampler=None)` | B | 저장소 생성. `_data`·`_expires`·`_ttl_keys`·`_ttl_pos`와 시계·표본 선택기를 준비한다 | 인스턴스 |
| `set(key, value, ttl=None)` | B | `value`를 저장한다. `ttl`이 있으면 만료 시각을 설정하고, 없으면 기존 TTL을 제거한다 | `{"ok": True}` 또는 `{"error": "INVALID_INPUT"}` |
| `get(key)` | B | `value`를 조회한다. 만료된 키는 없는 키로 취급한다 | `{"found": True, "value": ...}` 또는 `{"found": False}` |
| `delete(key)` | B | 키를 삭제하고 삭제 직전 `value`를 돌려준다 | `{"found": ...}` (+ `value`) |
| `expire(key, ttl)` | B | TTL을 설정·갱신한다. `ttl<=0`이면 키를 즉시 삭제한다 | `{"found": True, "ttl": ...}` 또는 `{"found": False}` |
| `ttl(key)` | B | 남은 TTL을 ms로 돌려준다. TTL이 없으면 `-1`이다 | `{"found": True, "ttl": ...}` 또는 `{"found": False}` |
| `persist(key)` | B | TTL만 제거하고 `value`는 유지한다 | `{"found": True, "persisted": ...}` 또는 `{"found": False}` |
| `incr`·`decr`·`_adjust` | B | 카운터 증감. lazy 만료 검사를 추가하고, 살아 있는 키의 TTL은 유지한다 | 기존 계약 + `{"error": "NOT_FOUND"}` |

**내부 helper**

| 함수 | 소유 | 역할 | 반환 |
| --- | --- | --- | --- |
| `_now_ms()` | B | 현재 시각을 정수 epoch ms로 돌려준다. 테스트는 `FakeClock`을 주입한다 | `int` |
| `_drop_key(key)` | B | `_data`·`_expires`·`_ttl_keys`·`_ttl_pos`에서 키를 제거한다. TTL 구조 제거의 유일한 경로다 | 없음 |
| `_set_ttl(key, expiry_ms)` | B | `_expires`에 절대 만료 시각을 기록한다. TTL이 없던 키면 `_ttl_keys`에 추가한다 | 없음 |
| `_clear_ttl(key)` | B | TTL 구조에서만 제거하고 `value`는 남긴다 | 없음 |
| `_lazy_expire(key)` | B | 그 키가 만료됐는지 확인하고, 만료면 `_drop_key`를 호출한다 | 만료 여부 |
| `_maybe_active_expire()` | C | 타이머가 100ms마다 호출하는 tick 진입점. active 사이클을 최대 2회 실행한다 | 없음 |
| `_active_expire_cycle(now_ms)` | C | TTL 키에서 최대 20개를 뽑아 만료된 키를 제거한다 | `(expired_count, sample_size)` |

**HTTP 처리 함수**

| 함수 | 소유 | 역할 |
| --- | --- | --- |
| `put_kv` | A | `PUT /kv` 본문을 해석하고 `set`을 한 번 호출해 200/400으로 연결한다 |
| `expire_kv` | A | `POST /kv/expire` 본문을 해석하고 `expire`를 호출한다 |
| `ttl_kv` | A | `GET /kv/ttl`의 쿼리 `key`를 읽고 `ttl`을 호출한다 |
| `persist_kv` | A | `POST /kv/persist` 본문을 해석하고 `persist`를 호출한다 |

**테스트 전용 helper**

| 함수 | 소유 | 역할 |
| --- | --- | --- |
| `ttl_snapshot(store)` | B | `_expires`·`_ttl_keys`·`_ttl_pos`의 사본을 돌려준다 |
| `assert_ttl_invariants(store)` | B | 세 구조의 키 집합·위치·길이·중복 없음 조건을 검사한다 |
| `FakeClock` | C | 가짜 벽시계. `advance(ms)`로 시간을 진행한다 |
| `FirstKSampler` | C | 현재 `keys`의 앞에서부터 최대 `k`개를 돌려준다 |
| `snapshot(store)` | 기존 | `_data`의 사본을 돌려준다. 기존 의미를 유지한다 |

## 2. 3인 분업표

| 역할 | 수정 파일 | 담당 함수 | 테스트 | 선행 조건 |
| --- | --- | --- | --- | --- |
| **A. HTTP 경계·소비자** | `app.py`, `tests/scenario_01_storage/test_ttl_http.py`, `tests/scenario_04_response_cache/test_weather_cache.py` | `put_kv`, `expire_kv`, `ttl_kv`, `persist_kv` | T1-20, T1-21 | B의 저장소 인터페이스, C의 `FakeClock` |
| **B. TTL 코어** | `storage.py`의 TTL 코어·public 메서드, `tests/scenario_01_storage/test_ttl_storage.py`, `tests/scenario_01_storage/ttl_state_helpers.py` | `__init__`, `_now_ms`, `_drop_key`, `_set_ttl`, `_clear_ttl`, `_lazy_expire`, `set`, `get`, `delete`, `expire`, `ttl`, `persist`, `incr`, `decr`, `_adjust` | T1-01~13, T1-23 | 없음 |
| **C. Active 만료** | `storage.py`의 active 구간, `tests/scenario_01_storage/test_ttl_active.py`, `tests/scenario_01_storage/ttl_clock_helpers.py` | `_maybe_active_expire`, `_active_expire_cycle` | T1-14~18 | B의 `_now_ms`·`_drop_key`·TTL 구조 |
| **통합** | `storage.py` 병합, 전체 테스트 실행 | 함수 단위 병합·회귀 확인 | T1-01~18, T1-20~21 | A·B·C |

- B가 `storage.py` 코어의 주 담당이다. C의 active 함수는 같은 파일에 들어가므로 B가 함수 단위로 병합한다. 어느 한쪽의 파일 전체로 다른 쪽을 덮어쓰지 않는다.
- A는 `storage.py`를 수정하지 않는다. C는 `app.py`를 수정하지 않는다.
- 테스트 파일은 담당별로 분리한다. 테스트 계약 문서의 파일 목록은 이 분담에 맞춰 `test_ttl_storage.py`(B)·`test_ttl_active.py`(C)·`test_ttl_http.py`·`test_weather_cache.py`(A)로 갱신한다. lifespan 타이머 task와 T1-19는 후속 timer 브랜치의 `test_ttl_timer.py`로 둔다.
- B가 가장 긴 경로다. A와 C는 B의 인터페이스가 동결된 뒤 테스트를 먼저 작성하고, 구현은 각자 범위에서 병렬로 진행할 수 있다.

## 3. 공통 계약

### 3.1 자료구조와 불변 조건

| 구조 | 내용 | 소유 |
| --- | --- | --- |
| `_data` | `key → value` | B |
| `_expires` | `key → 만료 시각(정수 epoch ms)` | B |
| `_ttl_keys` | `list[key]` | B |
| `_ttl_pos` | `key → _ttl_keys 위치` | B |
| `_clock` | `clock()` epoch ms | B가 생성자에서 보관, C가 `_now_ms`로 사용 |
| `_sampler` | `sampler(keys, k) → list[key]` 또는 `None` | B가 생성자에서 보관, C가 사용 |

- `set(_expires) == set(_ttl_keys) == set(_ttl_pos)`
- 모든 `key`에 대해 `_ttl_keys[_ttl_pos[key]] == key`
- `len(_expires) == len(_ttl_keys) == len(_ttl_pos)`
- `_ttl_keys`에 중복 키가 없다.
- `_expires`가 TTL의 진실원이다. `_ttl_keys`·`_ttl_pos`는 표본 선택용 보조 인덱스다.

### 3.2 생성자·시계·표본

`[확정]` 기존 `MiniRedis()` 호출을 유지하면서 테스트 주입을 허용한다.

```python
class MiniRedis:
    def __init__(self, clock=None, sampler=None):
        # _data, _expires, _ttl_keys, _ttl_pos
        # _clock, _sampler
        ...
```

- `clock()`은 정수 epoch ms를 반환한다. `None`이면 실제 벽시계를 쓴다.
- `sampler(keys, k)`는 현재 TTL 키에서 최대 `k`개를 반환한다. `None`이면 C의 active 코드가 기본 표본을 쓴다.
- `MiniRedis()`는 기존 동작과 호환되어야 한다.
- 이 인터페이스는 테스트 코드 작성 전에 동결했다(2026-11-14).

### 3.3 내부 helper 계약

| helper | 소유 | 입력 | 책임 | 호출자 |
| --- | --- | --- | --- | --- |
| `_now_ms()` | B | 없음 | 현재 epoch ms | B, C |
| `_drop_key(key)` | B | 키 | `_data`·`_expires`·`_ttl_keys`·`_ttl_pos`에서 제거 | B, C |
| `_set_ttl(key, expiry_ms)` | B | 키, 절대 만료 ms | TTL 추가·갱신. 없으면 목록 끝에 추가 | B |
| `_clear_ttl(key)` | B | 키 | TTL만 제거하고 `value`는 유지 | B |
| `_lazy_expire(key)` | B | 키 | 만료 확인·필요 시 `_drop_key`; 만료 여부 반환 | B의 public 메서드 |
| `_maybe_active_expire()` | C | 없음 | 타이머 tick. 사이클 최대 2회 | lifespan 타이머 task(후속) |
| `_active_expire_cycle(now_ms)` | C | 현재 ms | 표본·만료 확인·`_drop_key`; `(expired_count, sample_size)` 반환 | C |

- `_drop_key`는 swap-pop으로 O(1) 제거한다. C는 이 함수만 호출한다.
- `_lazy_expire`는 입력 검증 뒤에 호출한다. 입력 거절 시에는 lazy 정리도 하지 않아 기존 상태 보존 규칙을 지킨다.
- `_maybe_active_expire`는 타이머가 호출할 때마다 현재 시각을 읽고 `_active_expire_cycle`을 실행한다. 100ms 주기는 lifespan 타이머 task가 지킨다(후속 브랜치). `[확정]` D-24.
- `_active_expire_cycle`은 `self._sampler`가 있으면 그것을, 없으면 중복 없는 기본 표본을 쓴다. 표본의 25% 이상이 만료면 `_maybe_active_expire`가 한 번 더 호출한다. 최대 2회다. `[확정]` 표본 중복 정책은 중복 없음이다.

### 3.4 HTTP → 저장소 호출

| HTTP | 저장소 호출 | 정상 응답 |
| --- | --- | --- |
| `PUT /kv` + 선택 `ttl` | `set(key, value, ttl=ttl 또는 None)` | `{"ok": True}` → 200 |
| `POST /kv/expire` | `expire(key, ttl)` | `{"found": True, "ttl": ...}` 또는 `{"found": False}` → 200 |
| `GET /kv/ttl` | `ttl(key)` | `{"found": True, "ttl": ...}` 또는 `{"found": False}` → 200 |
| `POST /kv/persist` | `persist(key)` | `{"found": True, "persisted": ...}` 또는 `{"found": False}` → 200 |

- `PUT /kv`에서 `ttl`이 없거나 `null`이면 `ttl=None`으로 전달한다. 저장소는 기존 TTL을 제거한다(D-04).
- `POST /kv/expire`의 `ttl`은 필수다. HTTP는 필드 존재만 확인하고 타입·범위는 저장소가 판정한다.
- `GET /kv/ttl`은 쿼리 `key`가 필수다. `POST /kv/persist`는 본문 `key`가 필수다.
- `INVALID_INPUT`은 HTTP 400, 나머지 정상 반환은 200으로 연결한다. 키 없음은 200과 `{"found": false}`다.

### 3.5 테스트 helper

| helper | 파일 | 소유 | 내용 |
| --- | --- | --- | --- |
| `ttl_snapshot(store)` | `ttl_state_helpers.py` | B | `_expires`·`_ttl_keys`·`_ttl_pos`의 사본 |
| `assert_ttl_invariants(store)` | `ttl_state_helpers.py` | B | 네 불변 조건 검사 |
| `FakeClock` | `ttl_clock_helpers.py` | C | 기준 ms, `advance(ms)` |
| `FirstKSampler` | `ttl_clock_helpers.py` | C | 현재 `keys`의 앞에서부터 k개 반환 |

- 기존 `tests/scenario_01_storage/storage_helpers.py`의 `snapshot(store)`는 `_data` 사본 의미를 유지한다. 수정하지 않는다.
- A의 T1-21은 C의 `FakeClock`을 사용한다. C가 A의 테스트 착수 전에 helper를 제공한다.

## 4. 함수 계약

### 4.1 `app.py` — A

```python
def put_kv():
    # JSON 해석 → 객체·key·value 존재 확인
    # ttl = body.get("ttl")  (없음·null 모두 None)
    # storage.set(key, value, ttl=ttl)
    # 결과를 200/400으로 연결
    ...

def expire_kv():
    # POST /kv/expire
    # JSON 해석 → 객체·key·ttl 필수 확인
    # storage.expire(key, ttl)
    # 결과를 200/400으로 연결
    ...

def ttl_kv():
    # GET /kv/ttl?key=...
    # key 쿼리 필수 확인
    # storage.ttl(key)
    ...

def persist_kv():
    # POST /kv/persist
    # JSON 해석 → 객체·key 필수 확인
    # storage.persist(key)
    ...
```

- A는 TTL 타입·범위를 직접 판정하지 않는다. `storage`가 반환한 `INVALID_INPUT`을 400으로 연결한다.
- `expire`의 `ttl` 필드가 없으면 저장소를 호출하지 않고 400 `INVALID_INPUT`을 반환한다. `ttl: null`은 저장소에 전달하고 저장소가 거절한다.
- `expire`·`persist`는 계약 필드 외 추가 필드를 무시한다. `PUT /kv`와 같다.
- 기존 `put_kv`의 `key`·`value` 필수 검사와 `ERROR_STATUS` 매핑을 유지한다.
- A는 `storage` 내부 상태를 조회하지 않는다.

### 4.2 `storage.py` 코어 — B

```python
class MiniRedis:
    def __init__(self, clock=None, sampler=None): ...
    def _now_ms(self): ...
    def _drop_key(self, key): ...
    def _set_ttl(self, key, expiry_ms): ...
    def _clear_ttl(self, key): ...
    def _lazy_expire(self, key): ...

    def set(self, key, value, ttl=None): ...
    def get(self, key): ...
    def delete(self, key): ...
    def expire(self, key, ttl): ...
    def ttl(self, key): ...
    def persist(self, key): ...
```

- public 메서드는 active를 직접 호출하지 않는다. active는 lifespan 타이머가 C의 `_maybe_active_expire`를 호출해 실행한다.
- `set`: 기존 키·value 검증 → TTL 타입·범위 검증 → lazy 만료 → 상태 변경. `ttl=None`이면 기존 TTL 제거, 양의 정수면 `_set_ttl(key, now + ttl)`. `ttl<=0`·bool·float·문자열은 `INVALID_INPUT`.
- `get`·`delete`: 키 검증 → lazy 만료 → 기존 결과. 만료된 키는 `{"found": False}`.
- `expire`: 키 검증 → lazy 만료 → 키 없음/만료 `{"found": False}` → TTL 검증 → 양수면 `_set_ttl`, 0·음수면 `_drop_key` 후 `{"found": True, "ttl": 0}`.
- `ttl`: 키 검증 → lazy 만료 → TTL 있으면 남은 ms, 없으면 `-1`, 없거나 만료면 `{"found": False}`.
- `persist`: 키 검증 → lazy 만료 → TTL 있으면 `_clear_ttl` 후 `persisted: True`, 없으면 `persisted: False`, 키 없음/만료면 `{"found": False}`.
- `incr`·`decr`: 키·증감량·조건 검증 → lazy 만료 → 기존 증감. 만료된 키는 `NOT_FOUND`, 살아 있는 키의 TTL은 유지한다. `_adjust`에 lazy 검사 한 줄을 추가한다.
- B는 `_maybe_active_expire`의 본문을 작성하지 않는다. public 메서드에서 호출하지도 않는다.

### 4.3 `storage.py` active — C

```python
def _maybe_active_expire(self):
    # now = self._now_ms()
    # _active_expire_cycle(now)를 최대 2회 실행
    # 첫 사이클의 만료 비율이 25% 미만이면 1회에서 멈춤
    ...

def _active_expire_cycle(self, now_ms):
    # keys = self._ttl_keys
    # k = min(20, len(keys))
    # sample = self._sampler(keys, k) if self._sampler else 기본 표본
    # 각 key: 현재 _expires 확인 → 만료면 self._drop_key(key)
    # return (expired_count, sample_size)
    ...
```

- C는 `_expires`·`_ttl_keys`·`_ttl_pos`를 직접 수정하지 않는다. 제거는 `_drop_key`만 쓴다.
- 표본 크기는 최대 20개다. 표본의 25% 이상이 만료면 1회 더 실행하고 최대 2회다.
- 삭제 전에 현재 `_expires`와 키 존재를 확인한다. 표본을 뽑은 뒤 TTL이 갱신됐거나 키가 사라졌으면 삭제하지 않는다.
- `self._sampler`가 `None`이면 중복 없는 기본 표본을 쓴다. `[확정]` 표본 중복 정책은 중복 없음이다.
- 100ms 주기는 타이머 task가 지킨다. `_maybe_active_expire`는 호출될 때마다 사이클을 실행한다.
- C는 public 메서드 본문·HTTP 코드를 수정하지 않는다. 타이머 task는 후속 timer 브랜치에서 `app.py`에 추가한다.

## 5. 역할별 수정 범위와 금지

### A. HTTP 경계·소비자

- **수정 가능:** `app.py`의 TTL 요청 처리, `test_ttl_http.py`, `test_weather_cache.py`.
- **필수:** 본문 구조·필수 필드 검사, 저장소 호출 한 번, `INVALID_INPUT` → 400, 정상 → 200. `PUT /kv`의 `ttl` 생략·null을 `None`으로 전달.
- **금지:** `storage.py` 수정, TTL 타입·범위 판정, 저장 상태 사전 조회, TTL 계산, `/weather` 엔드포인트 추가, 별도 날씨 앱 프로세스.
- **독립 검증:** T1-20은 실제 `MiniRedis` 연결 HTTP, T1-21은 KV API + 가짜 원본이다. 원본 API 실패는 MiniRedis 계약 범위가 아니므로 실패 주입 테스트를 만들지 않는다.

### B. TTL 코어

- **수정 가능:** `storage.py`의 TTL 코어·public 메서드, `test_ttl_storage.py`, `ttl_state_helpers.py`.
- **필수:** 자료구조·불변 조건·`_now_ms`·`_drop_key`·`_set_ttl`·`_clear_ttl`·`_lazy_expire`·public 계약. 입력 검증 전 상태 변경 금지. `set`의 TTL 초기화(D-04), `expire`의 0·음수 삭제, `ttl`의 `-1`, `persist`의 `persisted` 반환. `incr`·`decr`의 lazy 만료와 TTL 유지.
- **금지:** `app.py` 수정, `_maybe_active_expire`·`_active_expire_cycle` 본문 작성, `_drop_key` 밖에서 TTL 구조 직접 변경.
- **독립 검증:** T1-01~13. 각 연산 뒤 `ttl_snapshot`과 불변 조건을 확인한다. 기존 `tests/scenario_01_storage/test_storage.py`·`test_storage_invalid_input.py`가 계속 통과해야 한다.

### C. Active 만료

- **수정 가능:** `storage.py`의 `_maybe_active_expire`·`_active_expire_cycle`, `test_ttl_active.py`, `ttl_clock_helpers.py`.
- **필수:** tick마다 사이클 최대 2회, 최대 20개 표본, 25% 이상 1회 추가, 삭제 전 현재 TTL 확인, `_drop_key`만 사용. `FakeClock`·`FirstKSampler` 제공.
- **금지:** `app.py` 수정(타이머 task는 후속 timer 브랜치), public 메서드 본문 수정, TTL 구조 직접 변경, 별도 스레드·큐 도입.
- **독립 검증:** T1-14~18. tick 함수를 직접 호출해 표본·25%·반복 상한·보존을 확인한다. 100ms 주기와 유휴 실행(T1-19)은 후속 timer 브랜치에서 확인한다.

### 통합

- B가 `storage.py`에서 C의 active 함수를 함수 단위로 병합한다. C의 함수 본문과 A의 타이머 호출을 함께 확인한다.
- A·B·C의 테스트를 모두 실행하고, 기존 01 저장소 테스트와 04 날씨 테스트의 회귀를 확인한다.
- 테스트 계약의 호출 대상·입력·검증 시점이 이 문서와 일치하는지 대조한다. 미정 항목은 `[미검증]`으로 남긴다.
- 테스트를 통과시키기 위해 계약을 완화하거나 `skip`·`xfail`로 돌리지 않는다.

## 6. 의존 관계와 병렬 순서

1. **B의 인터페이스 동결** — `MiniRedis(clock, sampler)`, 자료구조, `_now_ms`, `_drop_key`, `_set_ttl`, `_clear_ttl`, `_lazy_expire`, public 메서드 시그니처.
2. **C의 helper 먼저** — `FakeClock`·`FirstKSampler`와 `_active_expire_cycle` stub. tick의 입력·반환을 고정한다.
3. **A·B·C의 테스트 작성** — A는 T1-20~21, B는 T1-01~13, C는 T1-14~18. 구현 전에 실행해 실패 원인을 기록한다.
4. **B 코어 구현** — T1-01~13 통과. A의 HTTP 테스트가 저장소 경로로 통과할 수 있게 한다.
5. **A의 HTTP·소비자 구현** — T1-20·21 통과.
6. **C의 active 구현** — T1-14~18 통과. B가 병합.
7. **통합 검증** — 전체 TTL 테스트, 기존 01·02 회귀, 04 날씨 경로. 03 성능 재측정은 별도다.

- 1~2는 공통 선행 조건이다. 3은 1~2 뒤에 병렬로 시작할 수 있다. 4~6은 각자 범위에서 병렬로 진행하되, B의 `storage.py` 병합이 마지막에 필요하다.
- A는 B의 구현을 기다리지 않고 인터페이스에 맞춰 `app.py`를 작성할 수 있다. 테스트는 B의 코어가 준비될 때까지 실패한다.
- C는 B의 `_now_ms`·`_drop_key`·TTL 구조가 준비되기 전에는 active 테스트를 실행할 수 없다. C의 helper와 테스트 작성은 먼저 진행할 수 있다.

## 7. 함수-테스트 대응

| 함수·범위 | 테스트 | 담당 |
| --- | --- | --- |
| `app.py` `put_kv`·`expire_kv`·`ttl_kv`·`persist_kv` | T1-20 | A |
| `test_weather_cache.py` 소비자 경로 | T1-21 | A |
| `MiniRedis.set/get/delete/expire/ttl/persist` 코어 | T1-01~13 | B |
| `_set_ttl`·`_clear_ttl`·`_drop_key`·불변 조건 | T1-01~13 | B |
| `incr`·`decr`의 lazy 만료·TTL 유지 | T1-23 | B |
| `_maybe_active_expire`·`_active_expire_cycle` | T1-14~18 | C |
| `FakeClock`·`FirstKSampler` | T1-14~18, T1-21 | C |
| `ttl_snapshot`·`assert_ttl_invariants` | T1-01~13 | B |
| lifespan 타이머 task | T1-19 (후속 timer 브랜치) | 후속 |

## 8. 동결 결과

- `[확정]` `MiniRedis(clock=None, sampler=None)` 인터페이스. `clock()`은 정수 epoch ms, `sampler(keys, k)`는 최대 `k`개를 반환하고 기본값은 실제 벽시계·`random.sample`이다.
- `[확정]` active 만료는 lifespan의 100ms 주기 타이머가 실행한다(D-24). public 메서드는 active를 호출하지 않는다. 타이머 task 구현과 T1-19는 후속 timer 브랜치에서 하고, 이번 조각은 tick 함수까지다.
- `[확정]` 표본은 중복 없이 뽑는다(`random.sample`). 키가 20개 미만이면 전체를 검사한다.
- `[확정]` 테스트 파일 분담: `test_ttl_storage.py`(B), `test_ttl_active.py`(C), `test_ttl_http.py`·`test_weather_cache.py`(A). 타이머 task 시험은 후속 `test_ttl_timer.py`(A).
- `[확정]` `incr`·`decr`는 lazy 만료 검사를 하고, 만료된 키는 `NOT_FOUND`, 살아 있는 키의 TTL은 유지한다(T1-23).
- `[확정]` `POST /kv/expire`·`POST /kv/persist`는 계약 필드 외 추가 필드를 무시한다. `PUT /kv`와 같고, `incr`/`decr`의 거절 규칙과 구분한다.
- `[확정]` 원본 API 실패는 MiniRedis 계약 범위가 아니다. 소비자 책임이며 이 테스트 범위에 넣지 않는다.

## 9. 이 분업이 만족해야 하는 조건

- 세 사람이 같은 `storage.py`를 수정하더라도 함수 단위 소유가 겹치지 않는다. B는 코어·public 메서드, C는 active 두 함수만 수정한다.
- `_drop_key`가 TTL 구조 제거의 유일한 경로다. B와 C가 각자 제거 로직을 만들지 않는다.
- A는 HTTP 계약만, B는 저장소 계약만, C는 active 계약만 책임진다. 한 사람의 테스트를 다른 사람이 구현에 맞춰 바꾸지 않는다.
- 테스트를 먼저 작성하고 구현 전 실패를 확인한다. 통과·실패·미검증을 구분해 기록한다.
- 미정 항목을 편의상 확정하거나 새 기능으로 추가하지 않는다. 막히면 그 지점만 사용자에게 확인한다.
