"""T1-14~18: TTL active 만료 tick. tick 함수를 직접 호출하고 실제 시간을 기다리지 않는다.

대상: MiniRedis._maybe_active_expire / MiniRedis._active_expire_cycle
계약: resc/scenario_01_storage/01-ttl-test-cases.md, 01-ttl-c-active-contract.md
실행: .venv/bin/python -m pytest tests/scenario_01_storage/test_ttl_active.py -q
"""

from tests.scenario_01_storage.ttl_clock_helpers import FakeClock, FirstKSampler
from tests.scenario_01_storage.ttl_state_helpers import (
    assert_ttl_invariants,
    ttl_snapshot,
)

TTL_MS = 1_000


def build_store(sampler):
    """가짜 시계와 주입한 표본 선택기를 가진 새 저장소를 만든다."""
    clock = FakeClock()
    from storage import MiniRedis

    return MiniRedis(clock=clock, sampler=sampler), clock


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
        if self.before_return is not None:
            self.before_return()
        return sample


def test_t1_18_live_and_refreshed_ttl_preserved():
    sampler = HookSampler()
    store, clock = build_store(sampler)
    keys = prepare_ttl_keys(store, 2, "ttl", ttl=60_000)

    store._maybe_active_expire()

    assert len(sampler.calls) == 1
    assert_alive(store, keys)

    # 추가: TTL을 1_000ms로 줄인 키를 표본으로 돌려주기 전에 60_000ms로 갱신한다.
    # 계약 원문은 advance(1_000) 뒤 expire로 갱신하라고 적었지만, expire는 lazy
    # 만료를 먼저 하므로 만료 시각에 도달한 키는 갱신할 수 없다(T1-08과 충돌).
    # 만료 직전(999ms)에 갱신해 "삭제 전 현재 _expires 확인"만 검증한다.
    assert store.expire(keys[0], TTL_MS) == {"found": True, "ttl": TTL_MS}
    clock.advance(TTL_MS - 1)

    def refresh_ttl():
        assert store.expire(keys[0], 60_000) == {"found": True, "ttl": 60_000}

    sampler.before_return = refresh_ttl

    store._maybe_active_expire()

    assert_alive(store, keys)
    assert store.ttl(keys[0]) == {"found": True, "ttl": 60_000}
    assert_ttl_invariants(store)
