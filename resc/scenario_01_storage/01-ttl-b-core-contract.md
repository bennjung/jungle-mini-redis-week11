# 01 TTL — B. TTL 코어 동결 계약

## 기준과 상태

- 기준: [TTL 계약](01-ttl-contract.md), [TTL 테스트 계약](01-ttl-test-cases.md), [탑다운 분업](01-ttl-top-down-functions.md).
- 2026-11-14 동결. 변경은 계약 수정과 사용자 확인을 거친다.
- 이 문서는 B의 작업 범위만 담는다. HTTP 표면은 A, active tick은 C의 문서를 따른다.

## 담당 범위

| 항목 | 내용 |
| --- | --- |
| 수정 파일 | `storage.py`의 TTL 코어·public 메서드, `tests/scenario_01_storage/test_ttl_storage.py`, `tests/scenario_01_storage/ttl_state_helpers.py` |
| 담당 함수 | `__init__`, `_now_ms`, `_drop_key`, `_set_ttl`, `_clear_ttl`, `_lazy_expire`, `set`, `get`, `delete`, `expire`, `ttl`, `persist`, `incr`, `decr`, `_adjust` |
| 테스트 | T1-01~13, T1-23 |

## 자료구조와 불변 조건

| 구조 | 내용 | 역할 |
| --- | --- | --- |
| `_data` | `key → value` | 전체 키·`value` |
| `_expires` | `key → 만료 시각(정수 epoch ms)` | TTL의 진실원. TTL이 있는 키만 담는다 |
| `_ttl_keys` | `list[key]` | 표본 선택용 보조 인덱스 |
| `_ttl_pos` | `key → _ttl_keys 위치` | O(1) 추가·삭제용 위치 인덱스 |

- `set(_expires) == set(_ttl_keys) == set(_ttl_pos)`
- 모든 `key`에 대해 `_ttl_keys[_ttl_pos[key]] == key`
- `len(_expires) == len(_ttl_keys) == len(_ttl_pos)`
- `_ttl_keys`에 중복 키가 없다.

## 생성자·시계

- `MiniRedis(clock=None, sampler=None)`로 생성한다. 기존 `MiniRedis()` 호출과 호환된다.
- `_clock()`은 정수 epoch ms를 반환한다. `None`이면 실제 벽시계를 쓴다.
- `_sampler(keys, k)`는 C가 쓰는 표본 선택기다. 기본값은 `random.sample`이다.
- 필드: `_data`, `_expires`, `_ttl_keys`, `_ttl_pos`, `_clock`, `_sampler`.

## 내부 helper

| helper | 역할 |
| --- | --- |
| `_now_ms()` | 현재 시각을 정수 epoch ms로 반환한다 |
| `_drop_key(key)` | 네 구조에서 키를 제거한다. TTL 구조 제거의 **유일한 경로**다. `_ttl_keys`는 swap-pop으로 O(1)에 지운다 |
| `_set_ttl(key, expiry_ms)` | `_expires`에 절대 만료 시각을 쓴다. TTL이 없던 키면 `_ttl_keys` 끝에 추가하고 `_ttl_pos`를 기록한다 |
| `_clear_ttl(key)` | TTL 구조에서만 제거하고 `value`는 남긴다 |
| `_lazy_expire(key)` | 그 키가 만료됐는지 확인하고, 만료면 `_drop_key`를 호출한다. 만료 여부를 반환한다 |

- 입력 검증이 실패하면 상태를 바꾸지 않고 lazy 정리도 하지 않는다.

## public 메서드

모든 메서드는 입력 검증 → lazy 만료 → 상태 변경 순서다. 만료 판정은 `now_ms >= _expires[key]`다. active는 호출하지 않는다(D-24).

### `set(key, value, ttl=None)`

- 기존 키·`value` 검증을 그대로 적용한다.
- `ttl`이 `None`이면 TTL을 두지 않고, 기존 TTL이 있으면 제거한다(D-04).
- `ttl`은 `bool`이 아닌 정수여야 한다. `ttl > 0`이면 `_expires[key] = now + ttl`. 기존 TTL은 갱신하고, 없으면 인덱스에 추가한다. `ttl <= 0`·`bool`·`float`·문자열은 `INVALID_INPUT`.
- 만료된 키를 덮어쓰면 이전 TTL을 물려주지 않고 새 키로 취급한다.
- 성공 반환은 `{"ok": True}`다.

### `get(key)`·`delete(key)`

- 키 검증 → lazy 만료 → 기존 결과.
- 만료된 키는 없는 키로 취급한다. `get`은 `{"found": False}`, `delete`는 `{"found": False}`.
- `delete`는 살아 있는 키의 `value`를 삭제 직전 내용으로 반환한다.

### `expire(key, ttl)`

- 키 검증 → lazy 만료 → 키 없음·만료면 `{"found": False}`.
- `ttl`은 필수이고 `bool`이 아닌 정수다. `None`·`bool`·`float`·문자열은 `INVALID_INPUT`.
- `ttl > 0`이면 TTL을 설정·갱신하고 `{"found": True, "ttl": ttl}`.
- `ttl <= 0`이면 키와 `value`를 즉시 삭제하고 `{"found": True, "ttl": 0}`.

### `ttl(key)`

- 키 검증 → lazy 만료 → 키 없음·만료면 `{"found": False}`.
- TTL이 있으면 `{"found": True, "ttl": _expires[key] - now_ms}`.
- TTL이 없으면 `{"found": True, "ttl": -1}`.

### `persist(key)`

- 키 검증 → lazy 만료 → 키 없음·만료면 `{"found": False}`.
- TTL이 있으면 `_clear_ttl` 후 `{"found": True, "persisted": True}`.
- TTL이 없으면 `{"found": True, "persisted": False}`.

### `incr`·`decr`·`_adjust`

- 기존 키·증감량·조건 검증 순서를 유지한다.
- 검증을 통과한 뒤 `_lazy_expire(key)`를 호출한다. 만료된 키는 없는 키로 취급해 `NOT_FOUND`다.
- 살아 있는 키의 TTL은 유지한다. `_adjust`는 `_data`만 바꾸고 TTL 구조는 건드리지 않는다.
- 없는 키의 `NOT_FOUND`는 기존 02 계약을 유지한다.

## 테스트

- 동결된 입력과 기대값은 [TTL 테스트 계약](01-ttl-test-cases.md)의 T1-01~13, T1-23을 따른다.
- 각 연산 뒤 `ttl_snapshot(store)`와 `assert_ttl_invariants(store)`로 구조를 확인한다.
- 기존 `test_storage.py`·`test_storage_invalid_input.py`와 02 저장소 테스트가 계속 통과해야 한다.

## 금지

- `app.py` 수정.
- `_maybe_active_expire`·`_active_expire_cycle` 본문 작성.
- `_drop_key` 밖에서 TTL 구조 직접 변경.
- 계약에 없는 동작(예: TTL 제거·자동 생성) 추가.

## 선행과 완료

- 선행: 없음. B의 구조와 시그니처가 C·A의 선행 조건이다.
- 완료: T1-01~13, T1-23 통과. 기존 저장소·02 회귀 통과. 불변 조건 유지.
