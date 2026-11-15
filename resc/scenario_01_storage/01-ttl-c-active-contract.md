# 01 TTL — C. Active 만료·타이머 동결 계약

## 기준과 상태

- 기준: [TTL 계약](01-ttl-contract.md) 6절, [TTL 테스트 계약](01-ttl-test-cases.md), [탑다운 분업](01-ttl-top-down-functions.md), [04 결정 로그](../scenario_04_response_cache/04-weather-ttl-decision-log.md) D-24·D-29.
- 2026-11-14 동결. 2026-11-15 개정: 시간 예산·hard cap·tick 예외 backoff. 변경은 계약 수정과 사용자 확인을 거친다.
- 이 문서는 C의 작업 범위만 담는다. HTTP 표면은 A, TTL 코어는 B의 문서를 따른다.

## 담당 범위

| 항목 | 내용 |
| --- | --- |
| 수정 파일 | `storage.py`의 active 구간, `app.py`의 lifespan·타이머 블록, `tests/scenario_01_storage/test_ttl_active.py`, `tests/scenario_01_storage/ttl_clock_helpers.py`, `tests/scenario_01_storage/test_ttl_timer.py` |
| 담당 함수 | `_maybe_active_expire`, `_active_expire_cycle`, `_timer_loop` |
| 테스트 helper | `FakeClock`, `FirstKSampler`, `FixedMonotonic`, `StepMonotonic` |
| 테스트 | T1-14~19, T1-25~29 |

## 실행 구조

- Active 만료는 FastAPI `lifespan`이 소유하는 100ms 주기 타이머가 실행한다(D-24). Redis의 serverCron·activeExpireCycle과 같은 계열이다.
- **C가 타이머 task를 구현한다.** `app.py`의 lifespan 블록에서 task를 만들고 종료 시 취소·대기한다. C는 lifespan 블록만 수정하고 HTTP handler는 건드리지 않는다(A 소유).
- public 메서드는 active를 호출하지 않는다. TTL 코어는 lazy 만료만 한다.
- tick 안에 100ms 게이트를 두지 않는다. 주기는 타이머가 지킨다. tick은 호출될 때마다 active 사이클을 실행한다.
- 타이머 task와 저장소 호출은 같은 이벤트 루프에서 실행된다. tick에는 await이 없고 저장소 호출이 동기로 끝나므로 사이클 중간에 요청이 끼어들지 않는다. 별도 스레드·큐·잠금을 만들지 않는다.

### active 파라미터

| 항목 | 값 | 역할 |
| --- | --- | --- |
| 표본 크기 | 20 | 한 cycle이 확인할 TTL 키 수 |
| 시간 예산 | 1ms (`0.001s`) | tick이 이벤트 루프를 붙잡는 상한 |
| hard cap | 50 cycles | 시간 예산이 소진되지 않아도 반복을 끝내는 상한 |
| 25% 규칙 | `expired_count * 4 >= sample_size` | 같은 tick에서 계속할지 판단 |
| 시간 측정 | `time.monotonic` | 기본값. 테스트는 `monotonic`을 주입한다 |

```python
logger = logging.getLogger(__name__)

TTL_MAX_CONSECUTIVE_FAILURES = 5

async def _timer_loop(storage, interval=0.1, sleep=asyncio.sleep):
    failures = 0
    while True:
        delay = interval if failures == 0 else min(interval * 2 ** failures, 5.0)
        await sleep(delay)
        try:
            storage._maybe_active_expire()
            failures = 0
        except Exception:
            failures += 1
            logger.exception(
                "TTL active cleanup failed (%d/%d)",
                failures,
                TTL_MAX_CONSECUTIVE_FAILURES,
            )
            if failures >= TTL_MAX_CONSECUTIVE_FAILURES:
                logger.error(
                    "TTL active cleanup stopped after %d consecutive failures",
                    failures,
                )
                return

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

- `[확정]` 타이머는 시작 후 100ms를 기다렸다가 첫 tick을 실행한다. 시작 직후에는 실행하지 않는다.
- `[확정]` tick에서 예상하지 못한 예외가 나면 `logger.exception`으로 남기고, 실패 횟수를 증가시킨다. 실패 횟수가 5 미만이면 다음 sleep 간격을 `min(interval * 2 ** failures, 5.0)`초로 늘려 재시도한다. 5회 연속 실패면 `logger.error`를 남기고 task를 끝낸다. 앱은 계속 동작하고, active 정리만 멈춘다.
- `[확정]` tick 성공 시 실패 횟수는 0으로 돌아간다. 따라서 5회는 **연속 실패** 기준이다.
- `[확정]` `CancelledError`는 `Exception`이 아니므로 잡지 않는다. 종료 취소는 그대로 전파된다.
- `[확정]` `sleep`은 `try` 밖에 둔다. sleep 자체의 예외는 tick 예외로 취급하지 않는다.
- `[확정]` `_timer_loop(storage, interval=0.1, sleep=asyncio.sleep)`로 sleep을 주입한다. 테스트는 가짜 sleep으로 tick 횟수와 backoff 간격을 제어하고 실제 100ms를 기다리지 않는다.

## tick 계약

### `_maybe_active_expire()`

- `now = self._now_ms()`를 읽는다.
- `deadline = self._monotonic() + self._ACTIVE_TIME_BUDGET_S`를 계산한다.
- `_active_expire_cycle(now)`를 반복한다. 각 cycle은 다음 순서다.
  1. cycle을 실행한다.
  2. `sample_size == 0`이면 중단한다.
  3. `expired_count * 4 < sample_size`이면 중단한다. (25% 미만)
  4. `self._monotonic() >= deadline`이면 중단한다.
  5. `cycles >= self._ACTIVE_MAX_CYCLES`이면 중단한다.
- 두 사이클 이상은 **같은 `now`**를 쓴다. 한 tick은 하나의 논리적 시점이다.
- **최소 1회 cycle**을 실행한다. 시작 시점에 이미 예산이 지났더라도 첫 cycle은 실행하고, 그 뒤에 예산을 확인한다.
- 시간 예산은 cycle 중간을 자르지 않는다. cycle이 끝난 뒤에 확인하므로 한 cycle은 예산을 조금 넘길 수 있다.
- hard cap은 시간 예산이 소진되지 않는 경우(테스트의 고정 monotonic 등)에도 무한 반복을 막는다.
- 반환값은 없다.

### `_active_expire_cycle(now_ms)`

- `_ttl_keys`에서 최대 20개를 뽑는다. `k = min(20, len(_ttl_keys))`.
- `[확정]` TTL 키가 없으면 `_active_expire_cycle`이 `(0, 0)`을 반환한다. `_maybe_active_expire`는 항상 사이클을 한 번 호출하고, tick에서 빈 목록을 따로 검사하지 않는다.
- `[확정]` sampler는 새 리스트를 반환한다. cycle은 표본을 복사하지 않고 그대로 순회한다.
- `self._sampler`가 있으면 그것을 쓰고, 없으면 `random.sample`로 중복 없이 뽑는다. sampler는 새 리스트를 반환한다.
- 각 표본 키에 대해 삭제 전에 현재 `_expires`와 키 존재를 다시 확인한다. 표본을 뽑은 뒤 TTL이 갱신됐거나 키가 사라졌으면 삭제하지 않는다. 현재 tick은 동기라 요청이 사이에 끼어들지 않는다. 이 확인은 주입한 sampler가 표본 반환 중 상태를 바꾸는 경우와, 향후 tick이 비동기가 되는 경우를 방어한다.
- 만료된 키는 `_drop_key(key)`로만 제거한다. `_expires`·`_ttl_keys`·`_ttl_pos`를 직접 수정하지 않는다.
- `(expired_count, sample_size)`를 반환한다.
- 만료 비율은 `expired_count * 4 >= sample_size`로 판정한다. `sample_size`가 0이면 반복하지 않는다.

## 테스트

- 동결된 입력과 기대값은 [TTL 테스트 계약](01-ttl-test-cases.md)의 T1-14~19, T1-25~29를 따른다. tick 함수를 직접 호출하고, 실제 100ms를 기다리지 않는다.
- T1-14~18·25는 `FixedMonotonic()`으로 시간 예산이 중간에 끊지 않게 한다.
- T1-26은 `StepMonotonic(step_s=0.0002)`으로 1ms 예산 소진을, T1-27은 `FixedMonotonic()`과 2,000개 키로 hard cap 50 cycles를 검증한다.
- T1-19(유휴 상태의 타이머)는 C가 작성한다. `_timer_loop`에 가짜 sleep을 주입해 tick 횟수를 제어하고 실제 100ms를 기다리지 않는다.
- T1-28은 일시적 tick 예외 후 backoff 재시도와 실패 횟수 초기화를, T1-29는 연속 5회 실패 후 중단을 검증한다.
- `FakeClock`은 기준 ms와 `advance(ms)`를 제공한다. `FirstKSampler`는 현재 `keys`의 앞에서부터 최대 `k`개를 돌려준다. `FixedMonotonic`은 고정 초, `StepMonotonic`은 호출마다 증가하는 초를 돌려준다.
- 각 테스트 뒤 `assert_ttl_invariants(store)`로 네 구조의 일관성을 확인한다.

## 금지

- `app.py`의 HTTP handler 수정. C는 lifespan·타이머 블록만 수정한다.
- public 메서드 본문 수정.
- TTL 구조 직접 변경. 제거는 `_drop_key`만 쓴다.
- 별도 스레드·큐·잠금 도입.
- T1-14~18, T1-26~27에서 실제 시간을 기다리는 테스트.
- tick 예외를 무한 재시도하거나, `CancelledError`를 잡아 종료를 막는 구현.

## 선행과 완료

- 선행: B의 `_now_ms`, `_drop_key`, TTL 구조, `_sampler`, `_monotonic` 필드.
- 순서: helper와 테스트를 먼저 작성하고, B의 코어가 준비된 뒤 구현한다. B가 함수 단위로 병합한다.
- 완료: T1-14~19, T1-25~29 통과. 불변 조건 유지. 타이머 시작·종료가 앱 수명주기와 맞고, 기존 01·02·04 테스트 회귀 통과.
