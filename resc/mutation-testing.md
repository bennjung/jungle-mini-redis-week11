# Mutation testing 기록 — TTL 핵심 로직

## 목적

테스트가 실제 코드 결함에 민감한지 확인한다. `tools/mutation_check.py`가 `storage.py`에 작은 변이를 하나씩 적용하고 TTL 테스트를 실행한 뒤 원본을 복원한다.

## 실행

```bash
.venv/bin/python tools/mutation_check.py
```

대상 테스트:

- `tests/scenario_01_storage/test_ttl_storage.py`
- `tests/scenario_01_storage/test_ttl_active.py`
- `tests/scenario_01_storage/test_ttl_http.py`
- `tests/scenario_01_storage/test_ttl_timer.py`

## 결과 (2026-11-17)

### 수동 변이

- 변이 14개 중 **14개 killed** (`survived=0`)
- `storage.py` 원본 복원 확인: **True**

발견한 테스트 구멍:

- `expire(key, 0)`을 `ttl < 0`으로 바꾸면 `_data`와 TTL 구조가 즉시 제거되지 않고, `get()`의 lazy 만료로만 사라지는 변이가 살아남았다.
- 기존 T1-08 즉시 삭제 테스트가 `expire` 호출 직후가 아니라 `get` 이후에 TTL 구조를 확인했기 때문이다.
- `test_t1_08_expire_immediate_delete`에 `expire` 호출 직후 `_data`·TTL 구조 검사를 추가해 변이를 잡도록 보강했다.
- 테스트 계약 T1-08의 기대 문장에도 “호출 직후 확인, `get`의 lazy 만료에 기대지 않음”을 명시했다.

### mutmut 전체 변이

`setup.cfg`의 `[mutmut]` 설정으로 `storage.py`를 대상으로 실행하고, `app.py`는 테스트 import용으로만 복사했다(`only_mutate = storage.py`).

- 생성된 변이: **570**
- killed: **526**
- survived: **44**
- timeout: **0**
- mutation score: **92.3%**

추가로 발견해 보강한 테스트:

- `expire`·`ttl`·`persist`의 잘못된 키 거절과 상태 보존
- `set(ttl=1)`과 `expire(key, 1)`의 1ms 경계
- 만료된 키에 대한 `persist(found:false)`와 `expire(found:false)`
- 1바이트·256바이트 키 경계 (`get`·`delete`·`expire`·`ttl`·`persist`, counter)
- 중첩 Unicode JSON의 65,536바이트 경계(`ensure_ascii`·separator)
- 기본 sampler 경로와 live→expired 표본에서 cycle 지속

남은 44개는 수동 검토상 대부분 **도달 불가능한 분기·반환값 미사용·동작이 같은 equivalent mutant**다.

- `_json_content_supported`의 cycle guard 11개: 현재 호출 경로에서는 `json.dumps`가 cycle을 거절해 외부 결과가 같다.
- `_json_type`의 `number`/`unknown` 문자열 4개: `_adjust`가 숫자 타입에는 `_json_type`을 호출하지 않아 도달하지 않는다.
- `_active_expire_cycle`의 `random.sample` fallback 7개: `_sampler`가 생성자에서 항상 채워져 fallback이 실행되지 않는다.
- `set`의 `json.dumps` 옵션·`_lazy_expire` 인자 7개: 현재 값·시계 경로에서 결과가 같다.
- `_adjust`의 65,536 숫자 크기 분기 6개: Python 정수 문자열 변환 한도 때문에 해당 분기에 도달하기 어렵다.
- `_lazy_expire` 반환값 3개, `_maybe_active_expire` `break`→`return` 2개, `expire`의 같은 시각 재읽기 2개, `__init__` sampler fallback 1개, `_drop_key` 1개: 반환값 미사용·동작 동일.

이는 “44개가 모두 안전하다”는 증명이 아니다. 새 기능·호출 경로가 생기면 다시 실행해야 한다.

## 수동 변이 목록

- lazy 만료 경계 `now < expiry` → `now <= expiry`
- `expire(key, 0)` 즉시 삭제 → TTL 0 설정
- TTL 없는 `set`이 기존 TTL 유지
- `incr`/`decr`가 TTL 삭제
- swap-pop 위치 갱신 누락
- active 25% 경계 `>=` → `>`
- active 만료 경계 `now < expiry` → `now <= expiry`
- `ttl` 남은 시간 +1
- `expire` 만료 시각 -1
- TTL 없는 키의 `ttl`을 -1 대신 0
- `persist` 반환값 반전
- `delete`가 TTL 구조를 남김
- active 최대 cycles 50 → 1
- active 시간 예산 1ms → 0

## 한계

- TTL 코어·active 구간만 대상으로 했다. HTTP 경계·카운터·JSON 검증은 별도 변이가 필요하다.
- 동작이 같은 equivalent mutant는 killed로 만들 수 없다.
- 변이 스크립트는 `storage.py`를 잠깐 덮어쓰므로 실행 중 다른 테스트·서버와 동시에 돌리지 않는다.
- 14개는 전체 코드의 포괄적 mutation score가 아니며, 핵심 TTL 로직의 민감도 확인이다.
