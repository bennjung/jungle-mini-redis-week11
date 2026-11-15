"""T1-14~18, T1-25~27: TTL active 만료 tick. tick 함수를 직접 호출하고 실제 시간을 기다리지 않는다.

대상: MiniRedis._maybe_active_expire / MiniRedis._active_expire_cycle
계약: resc/scenario_01_storage/01-ttl-test-cases.md, 01-ttl-c-active-contract.md
실행: .venv/bin/python -m pytest tests/scenario_01_storage/test_ttl_active.py -q
"""

from tests.scenario_01_storage.ttl_clock_helpers import (
    FakeClock,
    FirstKSampler,
    FixedMonotonic,
    StepMonotonic,
)
from tests.scenario_01_storage.ttl_state_helpers import (
    assert_ttl_invariants,
    ttl_snapshot,
)

TTL_MS = 1_000


def build_store(sampler, monotonic=None):
    """가짜 벽시계·표본 선택기·시간 예산 시계를 가진 새 저장소를 만든다."""
    clock = FakeClock()
    from storage import MiniRedis

    return MiniRedis(
        clock=clock,
        sampler=sampler,
        monotonic=FixedMonotonic() if monotonic is None else monotonic,
    ), clock


def prepare_ttl_keys(store, count, prefix, ttl=TTL_MS):
    """승인된 set으로 TTL 키를 만든다. 만들어진 키 이름을 순서대로 돌려준다."""
    keys = []
    for index in range(count):
        key = f"{prefix}:{index:02d}"
        assert store.set(key, "hello", ttl=ttl) == {"ok": True}, key
        keys.append(key)
    return keys


def assert_removed(store, keys):
    for key in keys:
        assert store.get(key) == {"found": False}, key


def assert_alive(store, keys):
    for key in keys:
        assert store.get(key) == {"found": True, "value": "hello"}, key


def test_t1_14_active_expire_samples_20_then_remaining_5():
    sampler = FirstKSampler()
    store, clock = build_store(sampler)
    assert store.set("plain:keep", "hello") == {"ok": True}
    keys = prepare_ttl_keys(store, 25, "ttl")
    clock.advance(TTL_MS)

    store._maybe_active_expire()

    # 첫 사이클 20개(만료 비율 100%) → 같은 트리거에서 남은 5개까지 제거.
    assert [len(sample) for sample in sampler.calls] == [20, 5]
    assert_removed(store, keys)
    assert store.get("plain:keep") == {"found": True, "value": "hello"}
    assert ttl_snapshot(store) == ({}, [], {})
    assert_ttl_invariants(store)


def test_t1_15_under_25_percent_stops_after_one_cycle():
    sampler = FirstKSampler()
    store, clock = build_store(sampler)
    expired = prepare_ttl_keys(store, 4, "expired")
    clock.advance(TTL_MS)
    alive = prepare_ttl_keys(store, 16, "alive")

    store._maybe_active_expire()

    # 4/20 = 20% < 25%이므로 표본 선택기는 1회만 호출된다.
    assert len(sampler.calls) == 1
    assert len(sampler.calls[0]) == 20
    # get()의 lazy 만료와 구분해, cycle이 TTL 구조에서 실제로 제거했는지 본다.
    expires, ttl_keys, _ = ttl_snapshot(store)
    assert set(expires) == set(alive)
    assert set(ttl_keys) == set(alive)
    assert_removed(store, expired)
    assert_alive(store, alive)
    assert_ttl_invariants(store)


def test_t1_16_25_percent_runs_one_more_cycle():
    sampler = FirstKSampler()
    store, clock = build_store(sampler)
    expired = prepare_ttl_keys(store, 5, "expired")
    clock.advance(TTL_MS)
    alive = prepare_ttl_keys(store, 15, "alive")

    store._maybe_active_expire()

    # 5/20 = 25%이므로 한 번 더 실행되고, 두 번째 표본에는 만료 키가 없다.
    assert len(sampler.calls) == 2
    assert len(sampler.calls[1]) == 15
    assert_removed(store, expired)
    assert_alive(store, alive)
    assert_ttl_invariants(store)


def test_t1_17_tick_runs_on_every_call():
    sampler = FirstKSampler()
    store, _ = build_store(sampler)
    keys = prepare_ttl_keys(store, 1, "ttl", ttl=60_000)

    store._maybe_active_expire()
    store._maybe_active_expire()

    # tick 안에 100ms 게이트가 없다. 호출마다 사이클이 실행된다.
    assert len(sampler.calls) == 2
    assert_alive(store, keys)
    assert_ttl_invariants(store)


class HookSampler:
    """앞에서부터 k개를 뽑고, 반환 직전에 등록한 훅을 실행하는 표본 선택기."""

    def __init__(self):
        self.calls = []
        self.before_return = None

    def __call__(self, keys, k):
        sample = list(keys[:k])
        self.calls.append(sample)
        hook, self.before_return = self.before_return, None
        if hook is not None:
            hook()
        return sample


def test_t1_18_live_and_refreshed_ttl_preserved():
    sampler = HookSampler()
    store, clock = build_store(sampler)
    keys = prepare_ttl_keys(store, 2, "ttl", ttl=60_000)

    store._maybe_active_expire()

    assert len(sampler.calls) == 1
    assert_alive(store, keys)

    # 추가: TTL을 1_000ms로 줄인 키를 만료시킨 뒤, 표본을 돌려주기 직전에
    # set으로 다시 저장한다. set은 lazy 만료로 이전 키를 제거하고 새 TTL로
    # 저장하므로, cycle이 삭제 전에 현재 _expires와 키 존재를 다시 읽는지
    # 확인할 수 있다. expire는 만료된 키를 되살리지 못하므로 쓰지 않는다.
    assert store.expire(keys[0], TTL_MS) == {"found": True, "ttl": TTL_MS}
    clock.advance(TTL_MS)

    def re_add():
        assert store.set(keys[0], "hello", ttl=60_000) == {"ok": True}

    sampler.before_return = re_add

    store._maybe_active_expire()

    assert len(sampler.calls) == 2
    assert_alive(store, keys)
    assert store.ttl(keys[0]) == {"found": True, "ttl": 60_000}
    assert_ttl_invariants(store)


def test_t1_25_empty_ttl_set_tick_does_not_sample():
    """T1-25: TTL 키가 없으면 sampler를 부르지 않고 (0, 0)으로 끝난다."""
    sampler = FirstKSampler()
    store, _ = build_store(sampler)
    assert store.set("plain:keep", "hello") == {"ok": True}

    store._maybe_active_expire()

    assert sampler.calls == []
    assert_alive(store, ["plain:keep"])
    assert_ttl_invariants(store)


def test_t1_26_time_budget_stops_cycles():
    """T1-26: 1ms 예산과 0.2ms 단계 시계에서 5회 cycle 후 멈춘다."""
    sampler = FirstKSampler()
    store, clock = build_store(
        sampler, monotonic=StepMonotonic(step_s=0.0002)
    )
    prepare_ttl_keys(store, 200, "ttl")
    clock.advance(TTL_MS)

    store._maybe_active_expire()

    # cycle 1~5: 각 20개 제거, 5번째 뒤 누적 0.001s 도달로 중단.
    assert [len(sample) for sample in sampler.calls] == [20, 20, 20, 20, 20]
    expires, ttl_keys, _ = ttl_snapshot(store)
    assert len(expires) == 100
    assert set(expires) == set(ttl_keys)
    assert len(store._data) == 100
    assert_ttl_invariants(store)


def test_t1_27_hard_cap_stops_cycles():
    """T1-27: 시간 예산이 남아도 50 cycles에서 중단한다."""
    sampler = FirstKSampler()
    store, clock = build_store(sampler, monotonic=FixedMonotonic())
    prepare_ttl_keys(store, 2_000, "ttl")
    clock.advance(TTL_MS)

    store._maybe_active_expire()

    assert len(sampler.calls) == 50
    assert all(len(sample) == 20 for sample in sampler.calls)
    assert len(store._expires) == 1_000
    assert len(store._data) == 1_000
    assert_ttl_invariants(store)
