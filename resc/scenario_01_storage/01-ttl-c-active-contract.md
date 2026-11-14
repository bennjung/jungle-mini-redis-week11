# 01 TTL — C. Active tick 동결 계약

## 기준과 상태

- 기준: [TTL 계약](01-ttl-contract.md) 6절, [TTL 테스트 계약](01-ttl-test-cases.md), [탑다운 분업](01-ttl-top-down-functions.md), [04 결정 로그](../scenario_04_response_cache/04-weather-ttl-decision-log.md) D-24.
- 2026-11-14 동결. 변경은 계약 수정과 사용자 확인을 거친다.
- 이 문서는 C의 작업 범위만 담는다. HTTP 표면은 A, TTL 코어는 B의 문서를 따른다.

## 담당 범위

| 항목 | 내용 |
| --- | --- |
| 수정 파일 | `storage.py`의 active 구간, `tests/scenario_01_storage/test_ttl_active.py`, `tests/scenario_01_storage/ttl_clock_helpers.py` |
| 담당 함수 | `_maybe_active_expire`, `_active_expire_cycle` |
| 테스트 helper | `FakeClock`, `FirstKSampler` |
| 테스트 | T1-14~18 |

## 실행 구조

- Active 만료는 FastAPI `lifespan`이 소유하는 100ms 주기 타이머가 실행한다(D-24). Redis의 serverCron·activeExpireCycle과 같은 계열이다.
- 타이머 task는 후속 timer 브랜치에서 만든다. C는 `app.py`를 수정하지 않고, 타이머가 호출할 tick 함수만 구현한다.
- public 메서드는 active를 호출하지 않는다. TTL 코어는 lazy 만료만 한다.
- tick 안에 100ms 게이트를 두지 않는다. 주기는 타이머가 지킨다. tick은 호출될 때마다 사이클을 실행한다.
- 타이머 task와 저장소 호출은 같은 이벤트 루프에서 실행된다. tick의 저장소 호출은 동기로 끝나므로 사이클 중간에 요청이 끼어들지 않는다. 별도 스레드·큐·잠금을 만들지 않는다.

## tick 계약

### `_maybe_active_expire()`

- `now = self._now_ms()`를 읽는다.
- `_active_expire_cycle(now)`를 최대 2회 실행한다.
- 첫 사이클의 만료 비율이 25% 미만이면 1회에서 멈춘다. 25% 이상이면 1회 더 실행한다.
- 반환값은 없다.

### `_active_expire_cycle(now_ms)`

- `_ttl_keys`에서 최대 20개를 뽑는다. `k = min(20, len(_ttl_keys))`.
- `self._sampler`가 있으면 그것을 쓰고, 없으면 `random.sample`로 중복 없이 뽑는다.
- 각 표본 키에 대해 삭제 전에 현재 `_expires`와 키 존재를 다시 확인한다. 표본을 뽑은 뒤 TTL이 갱신됐거나 키가 사라졌으면 삭제하지 않는다.
- 만료된 키는 `_drop_key(key)`로만 제거한다. `_expires`·`_ttl_keys`·`_ttl_pos`를 직접 수정하지 않는다.
- `(expired_count, sample_size)`를 반환한다.
- 만료 비율 계산은 `expired_count / sample_size`다. `sample_size`가 0이면 추가 반복 판정을 하지 않는다.

## 테스트

- 동결된 입력과 기대값은 [TTL 테스트 계약](01-ttl-test-cases.md)의 T1-14~18을 따른다. tick 함수를 직접 호출하고, 실제 100ms를 기다리지 않는다.
- T1-19(유휴 상태의 타이머)는 타이머 task와 함께 후속 브랜치에서 작성한다. 이번 범위가 아니다.
- `FakeClock`은 기준 ms와 `advance(ms)`를 제공한다. `FirstKSampler`는 현재 `keys`의 앞에서부터 최대 `k`개를 돌려준다.
- 각 테스트 뒤 `assert_ttl_invariants(store)`로 네 구조의 일관성을 확인한다.

## 금지

- `app.py` 수정(타이머 task는 후속 브랜치).
- public 메서드 본문 수정.
- TTL 구조 직접 변경. 제거는 `_drop_key`만 쓴다.
- 별도 스레드·큐·잠금 도입.
- 실제 시간을 기다리는 테스트.

## 선행과 완료

- 선행: B의 `_now_ms`, `_drop_key`, TTL 구조, `_sampler` 필드.
- 순서: helper와 테스트를 먼저 작성하고, B의 코어가 준비된 뒤 구현한다. B가 함수 단위로 병합한다.
- 완료: T1-14~18 통과. 불변 조건 유지. 기존 01·02·04 테스트 회귀 통과.
