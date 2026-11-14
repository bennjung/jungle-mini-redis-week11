# 01 TTL — C. Active 만료·타이머 동결 계약

## 기준과 상태

- 기준: [TTL 계약](01-ttl-contract.md) 6절, [TTL 테스트 계약](01-ttl-test-cases.md), [탑다운 분업](01-ttl-top-down-functions.md), [04 결정 로그](../scenario_04_response_cache/04-weather-ttl-decision-log.md) D-24.
- 2026-11-14 동결. 변경은 계약 수정과 사용자 확인을 거친다.
- 이 문서는 C의 작업 범위만 담는다. HTTP 표면은 A, TTL 코어는 B의 문서를 따른다.

## 담당 범위

| 항목 | 내용 |
| --- | --- |
| 수정 파일 | `storage.py`의 active 구간, `app.py`의 lifespan 타이머 블록, `tests/scenario_01_storage/test_ttl_active.py`, `tests/scenario_01_storage/ttl_clock_helpers.py`, `tests/scenario_01_storage/test_ttl_timer.py` |
| 담당 함수 | `_maybe_active_expire`, `_active_expire_cycle`, `_timer_loop` |
| 테스트 helper | `FakeClock`, `FirstKSampler` |
| 테스트 | T1-14~19 |

## 실행 구조

- Active 만료는 FastAPI `lifespan`이 소유하는 100ms 주기 타이머가 실행한다(D-24). Redis의 serverCron·activeExpireCycle과 같은 계열이다.
- **C가 타이머 task를 구현한다.** `app.py`의 lifespan 블록에서 task를 만들고 종료 시 취소·대기한다. C는 lifespan 블록만 수정하고 HTTP handler는 건드리지 않는다(A 소유).
- public 메서드는 active를 호출하지 않는다. TTL 코어는 lazy 만료만 한다.
- tick 안에 100ms 게이트를 두지 않는다. 주기는 타이머가 지킨다. tick은 호출될 때마다 사이클을 실행한다.
- 타이머 task와 저장소 호출은 같은 이벤트 루프에서 실행된다. tick의 저장소 호출은 동기로 끝나므로 사이클 중간에 요청이 끼어들지 않는다. 별도 스레드·큐·잠금을 만들지 않는다.

```python
async def _timer_loop(storage):
    while True:
        await asyncio.sleep(0.1)
        storage._maybe_active_expire()

@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(_timer_loop(storage))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
```

- `[미정]` 첫 tick을 시작 직후에 실행할지, 100ms 뒤에 실행할지.
- `[미정]` tick에서 예상하지 못한 예외가 났을 때 task를 계속 돌릴지, 예외를 그대로 두고 task를 끝낼지.
- `[미정]` T1-19에서 100ms를 실제로 기다리지 않고 주기를 제어하는 방법(간격 주입·짧은 간격·loop 본문 직접 호출).

## tick 계약

### `_maybe_active_expire()`

- `now = self._now_ms()`를 읽는다.
- `_active_expire_cycle(now)`를 최대 2회 실행한다.
- 두 사이클은 **같은 `now`**를 쓴다. 한 tick은 하나의 논리적 시점이다.
- 첫 사이클의 만료 비율이 25% 미만이면 1회에서 멈춘다. 25% 이상이면 1회 더 실행한다.
- 비율은 `expired_count * 4 >= sample_size`로 비교한다. 25%가 정확히 1/4이라 부동소수 없이 판정된다.
- 두 번째 사이클의 결과로 세 번째를 실행하지 않는다. 상한은 2회다.
- 반환값은 없다.

### `_active_expire_cycle(now_ms)`

- `_ttl_keys`에서 최대 20개를 뽑는다. `k = min(20, len(_ttl_keys))`.
- `[확정]` TTL 키가 없으면 `_active_expire_cycle`이 `(0, 0)`을 반환한다. `_maybe_active_expire`는 항상 사이클을 한 번 호출하고, tick에서 빈 목록을 따로 검사하지 않는다.
- `[미정]` 표본 순회: `list(sample)`로 복사한다 / 샘플러가 새 리스트를 반환하도록 계약한다.
- `self._sampler`가 있으면 그것을 쓰고, 없으면 `random.sample`로 중복 없이 뽑는다.
- 각 표본 키에 대해 삭제 전에 현재 `_expires`와 키 존재를 다시 확인한다. 표본을 뽑은 뒤 TTL이 갱신됐거나 키가 사라졌으면 삭제하지 않는다.
- 만료된 키는 `_drop_key(key)`로만 제거한다. `_expires`·`_ttl_keys`·`_ttl_pos`를 직접 수정하지 않는다.
- `(expired_count, sample_size)`를 반환한다.
- 만료 비율은 `expired_count * 4 >= sample_size`로 판정한다. `sample_size`가 0이면 반복하지 않는다.

## 테스트

- 동결된 입력과 기대값은 [TTL 테스트 계약](01-ttl-test-cases.md)의 T1-14~18을 따른다. tick 함수를 직접 호출하고, 실제 100ms를 기다리지 않는다.
- T1-19(유휴 상태의 타이머)는 C가 타이머 task와 함께 작성한다. 테스트에서 주기를 제어하는 방법은 `[미정]`이다.
- `FakeClock`은 기준 ms와 `advance(ms)`를 제공한다. `FirstKSampler`는 현재 `keys`의 앞에서부터 최대 `k`개를 돌려준다.
- 각 테스트 뒤 `assert_ttl_invariants(store)`로 네 구조의 일관성을 확인한다.

## 금지

- `app.py`의 HTTP handler 수정. C는 lifespan 타이머 블록만 수정한다.
- public 메서드 본문 수정.
- TTL 구조 직접 변경. 제거는 `_drop_key`만 쓴다.
- 별도 스레드·큐·잠금 도입.
- T1-14~18에서 실제 시간을 기다리는 테스트.

## 선행과 완료

- 선행: B의 `_now_ms`, `_drop_key`, TTL 구조, `_sampler` 필드.
- 순서: helper와 테스트를 먼저 작성하고, B의 코어가 준비된 뒤 구현한다. B가 함수 단위로 병합한다.
- 완료: T1-14~19 통과. 불변 조건 유지. 타이머 시작·종료가 앱 수명주기와 맞고, 기존 01·02·04 테스트 회귀 통과.
