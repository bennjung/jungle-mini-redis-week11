# 키별 원자적 락 계약 (`ACQUIRE` / `RELEASE`)

## 1. 배경과 목적

외부 날씨 API처럼 응답이 느리거나 호출 제한이 있는 원본을 캐싱할 때, 캐시가 비거나 만료된 순간(Cache Miss) 여러 앱 서버가 동시에 원본을 호출하는 **중복 요청 폭주(Cache Stampede)**가 발생합니다.

이 계약은 **각 캐시 키(`key`)마다 한 번에 하나의 클라이언트만 갱신 권한을 갖도록** 하는 TTL 기반 원자적 락의 저장소 동작과 HTTP API를 정의합니다.

- **적용 대상**: [storage.py](../../storage.py) (`MiniRedis.acquire`, `MiniRedis.release`) 및 [app.py](../../app.py) (`POST /kv/lock/acquire`, `POST /kv/lock/release`)
- **선행 계약**: [TTL 계약](../scenario_01_storage/01-ttl-contract.md), [REST API 명세](../../rest-api.md)

---

## 2. 범위와 한계

### 포함
- 키별 독립 락 획득 (`acquire`), 실패 시 남은 락 만료 시간(`retry_after_ms`) 반환
- 같은 소유 토큰(`token`)으로 다시 `acquire` 시 TTL 연장(재진입)
- 소유 토큰 일치 여부를 검사하는 원자적 락 해제 (`release`)
- lazy 만료 + 기존 TTL과 일관된 100ms 주기 active 만료 정리

### 제외 (한계)
- 대기 클라이언트 큐, Pub-Sub 갱신 완료 알림
- **Fencing Token 기반 `PUT /kv` 차단**: 락을 얻은 서버가 락 TTL보다 오래 지연된 뒤 뒤늦게 `PUT /kv`를 보내면, 이미 다른 서버가 채운 최신 캐시를 덮어쓸 수 있습니다. *(단, `RELEASE`는 소유 토큰을 검사하므로 다른 서버의 새 락을 지우지는 못합니다.)*

---

## 3. 자료구조와 불변 조건

일반 캐시(`_data`, `_expires`)와 키 공간이 충돌하지 않도록 **락 전용 구조**를 분리하고, TTL 정리와 동일한 **O(1) swap-pop 표본 인덱스**를 사용합니다.

| 구조 | 내용 | 역할 |
| :--- | :--- | :--- |
| `_locks` | `key → (token, expires_at_ms)` | 유효한 락의 진실원 (`token`: 소유 토큰, `expires_at_ms`: 절대 만료 시각) |
| `_lock_keys` | `list[key]` | 락 키 배열 (active 정리용 무작위 표본 선택) |
| `_lock_pos` | `key → _lock_keys에서의 위치` | O(1) 삽입·swap-pop 삭제용 위치 인덱스 |

### 불변 조건
- `set(_locks) == set(_lock_keys) == set(_lock_pos)`
- 모든 `key`에 대해 `_lock_keys[_lock_pos[key]] == key`
- `len(_locks) == len(_lock_keys) == len(_lock_pos)`
- `PUT /kv`, `GET /kv`, `DELETE /kv`는 `_locks`를 건드리지 않으며, 락 연산도 `_data`·`_expires`를 건드리지 않는다.

---

## 4. 저장소 메서드 계약 (`MiniRedis`)

### 입력 제한
- `key`: UTF-8 기준 `1~256`바이트 문자열 (`""`·`null`·비문자열·UTF-8 인코딩 불가 시 `INVALID_INPUT`)
- `token`: UTF-8 기준 `1~256`바이트 문자열 (`""`·`null`·비문자열·UTF-8 인코딩 불가 시 `INVALID_INPUT`)
- `ttl`: `1` 이상의 정수 밀리초 (`bool`·`null`·소수·`0`·음수 시 `INVALID_INPUT`)

### `acquire(key, token, ttl)`
1. `key`, `token`, `ttl` 순으로 입력을 검증한다. (실패 시 기존 `_locks` 상태 보존, `{"error": "INVALID_INPUT"}`)
2. 한 번만 `_now_ms()`를 읽어 `now`를 고정하고, `key`가 만료 상태(`now >= expires_at_ms`)면 `_locks`·`_lock_keys`·`_lock_pos`에서 제거한다(lazy 만료).
3. **락이 없는 경우**: `(token, now + ttl)`을 저장하고 `{"acquired": True}`를 반환한다.
4. **같은 `token`이 이미 락을 점유 중인 경우**: 만료 시각을 `now + ttl`로 연장하고 `{"acquired": True}`를 반환한다.
5. **다른 `token`이 유효한 락을 점유 중인 경우**: 상태를 바꾸지 않고 `{"error": "LOCK_BUSY", "retry_after_ms": expires_at_ms - now}`를 반환한다. (`expires_at_ms > now`이므로 항상 `1` 이상)

### `release(key, token)`
1. `key`, `token` 순으로 입력을 검증한다. (실패 시 `{"error": "INVALID_INPUT"}`)
2. `now = _now_ms()` 기준으로 `key`가 만료됐으면 제거한다(lazy 만료).
3. 락이 없거나, 남아 있는 락의 소유 토큰이 `token`과 다르면 상태를 바꾸지 않고 `{"released": False}`를 반환한다.
4. 소유 토큰이 일치하면 `_locks`·`_lock_keys`·`_lock_pos`에서 O(1) swap-pop으로 제거하고 `{"released": True}`를 반환한다.

### active 만료 정리 (`_maybe_active_expire` 일관 방식)
- `_timer_loop`(100ms 주기, 예외 시 지수 backoff · 연속 5회 실패 시 정리만 중단)가 호출하는 `_maybe_active_expire()` 안에서, 일반 TTL 키 정리(`_ttl_keys`)와 **동일한 규칙과 시간 예산(1ms)**을 공유해 `_lock_keys`도 정리한다.
- 각 사이클은 `_lock_keys`에서 최대 `20`개 표본(`_sampler` 주입 가능)을 뽑아 `now >= expires_at_ms`인 락을 제거한다.
- 표본의 `25%` 이상이 만료되면 같은 `now`로 반복하되, 전체 틱의 **1ms 시간 예산**과 **최대 50 cycles** 상한을 넘지 않는다.

---

## 5. HTTP API 계약 (`app.py`)

### `POST /kv/lock/acquire`
- **요청**: `Content-Type: application/json`, 본문 `{"key": str, "token": str, "ttl": int}` (필수 3필드 외 알 수 없는 필드가 있으면 `400 INVALID_INPUT`)
- **응답**:
  - 획득 또는 동일 토큰 TTL 연장: `HTTP 200`, `{"acquired": true}`
  - 다른 토큰이 점유 중: `HTTP 409`, `{"error": "LOCK_BUSY", "retry_after_ms": <정수>}`
  - JSON/필드/타입/크기 오류: `HTTP 400`, `{"error": "INVALID_INPUT"}`

### `POST /kv/lock/release`
- **요청**: `Content-Type: application/json`, 본문 `{"key": str, "token": str}` (필수 2필드 외 알 수 없는 필드가 있으면 `400 INVALID_INPUT`)
- **응답**:
  - 해제 성공: `HTTP 200`, `{"released": true}`
  - 락 없음 · 만료됨 · 토큰 불일치: `HTTP 200`, `{"released": false}`
  - JSON/필드/타입/크기 오류: `HTTP 400`, `{"error": "INVALID_INPUT"}`

---

## 6. 테스트 케이스 (구현 전 동결 대상)

| ID | 대상 | 사전 상태 → 입력 · 행동 → 검증 시점 | 기대 결과 및 유지 조건 |
| :--- | :--- | :--- | :--- |
| **L1-01** | 저장소·HTTP | 락 없음 → `acquire("weather:seoul", "t1", 5000)` → 즉시 | `{"acquired": True}` / `200`, `_locks`에 기록, `_data` 무변경 |
| **L1-02** | 저장소·HTTP | `"t1"` 점유 중 (`ttl=5000`), `t=800ms` 경과 → `"t2"`가 `acquire("weather:seoul", "t2", 5000)` | `{"error": "LOCK_BUSY", "retry_after_ms": 4200}` / `409`, 락 소유자 `"t1"` 유지 |
| **L1-03** | 저장소·HTTP | `"t1"` 점유 중 (`ttl=5000`), `t=3000ms` 경과 → 같은 `"t1"`이 `acquire("weather:seoul", "t1", 5000)` | `{"acquired": True}` / `200`, 만료 시각이 `3000 + 5000 = 8000ms`로 연장됨 |
| **L1-04** | 저장소·HTTP | `"t1"`이 `"weather:seoul"` 점유 중 → `"t2"`가 `acquire("weather:busan", "t2", 5000)` | `{"acquired": True}` / `200` (키별 독립 락) |
| **L1-05** | 저장소 | `"t1"` 점유 (`ttl=5000`), `now == 5000ms` 경계 → `"t2"`가 `acquire("weather:seoul", "t2", 3000)` | `{"acquired": True}`, 기존 만료 락 제거 후 `"t2"`가 새로 점유 |
| **L1-06** | 저장소·HTTP | `"t1"` 점유 중 → `release("weather:seoul", "t1")` 후 `"t2"`가 `acquire` | `{"released": True}` / `200` → 이어진 `acquire`는 `{"acquired": True}` |
| **L1-07** | 저장소·HTTP | `"t1"` 점유 중 → `"t2"`가 `release("weather:seoul", "t2")` | `{"released": False}` / `200`, `"t1"` 락 그대로 유지 |
| **L1-08** | 저장소·HTTP | 락 없음 또는 `now >= expires_at_ms` 만료 상태 → `release("weather:seoul", "t1")` | `{"released": False}` / `200`, 만료 락이면 구조에서 제거됨 |
| **L1-09** | 저장소·HTTP | `"t1"`이 `"weather:seoul"` 락 점유 중 → `PUT /kv`로 캐시 저장 및 `GET /kv`·`DELETE /kv` | 락과 캐시 간 덮어쓰기·삭제 충돌 없음 (`_locks` 유지) |
| **L1-10** | 저장소·HTTP | 잘못된 `key`·`token` (`""`, `null`, 257B), 잘못된 `ttl` (`0`, `-1`, `True`, `"10"`, `1.5`), 알 수 없는 필드 | `400 INVALID_INPUT`, 기존 락 상태 보존 |
| **L1-11** | active 정리 | 만료된 락 20개 이상 + `sampler`·`monotonic` 주입 → `_maybe_active_expire()` | `25%` 규칙·`1ms` 예산·`50` cycles 안에서 만료 락 정리, 불변 조건 유지 |
| **L1-12** | HTTP 동시성 | `app.py` 단일 이벤트 루프에서 같은 키에 `ACQUIRE` 20건 동시 전송 | 정확히 1건만 `200 {"acquired": true}`, 19건은 `409 LOCK_BUSY` |
