"""Hypothesis property 테스트: 입력 예시가 아니라 불변 조건을 검증한다.

실행: .venv/bin/python -m pytest tests/scenario_01_storage/test_ttl_properties.py -q
"""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from storage import MiniRedis
from tests.scenario_01_storage.storage_helpers import snapshot
from tests.scenario_01_storage.ttl_clock_helpers import FakeClock
from tests.scenario_01_storage.ttl_state_helpers import (
    assert_ttl_invariants,
    ttl_snapshot,
)
from tests.scenario_01_storage.ttl_strategies import (
    AMOUNT,
    EXPIRY_TTL,
    KEY,
    TTL,
    VALUE,
)

SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def new_store():
    clock = FakeClock()
    return MiniRedis(clock=clock), clock


def assert_empty(store):
    assert snapshot(store) == {}
    assert ttl_snapshot(store) == ({}, [], {})
    assert_ttl_invariants(store)


@given(key=KEY, value=VALUE)
@SETTINGS
def test_set_then_get_returns_same_value(key, value):
    store, _ = new_store()
    assert store.set(key, value) == {"ok": True}
    assert store.get(key) == {"found": True, "value": value}
    assert snapshot(store) == {key: value}
    assert_ttl_invariants(store)


@given(key=KEY, value=VALUE, ttl=TTL)
@SETTINGS
def test_ttl_boundary_and_same_now(key, value, ttl):
    store, clock = new_store()
    assert store.set(key, value, ttl=ttl) == {"ok": True}
    assert store.ttl(key) == {"found": True, "ttl": ttl}

    clock.advance(ttl - 1)
    assert store.ttl(key) == {"found": True, "ttl": 1}
    assert store.get(key) == {"found": True, "value": value}

    clock.advance(1)
    assert store.get(key) == {"found": False}
    assert store.ttl(key) == {"found": False}
    assert_empty(store)


@given(key=KEY, value=VALUE)
@SETTINGS
def test_delete_returns_previous_value_and_is_idempotent(key, value):
    store, _ = new_store()
    assert store.set(key, value) == {"ok": True}
    assert store.delete(key) == {"found": True, "value": value}
    assert store.get(key) == {"found": False}
    assert store.delete(key) == {"found": False}
    assert_empty(store)


@given(key=KEY, value=VALUE, other=VALUE, ttl=TTL)
@SETTINGS
def test_set_without_ttl_clears_existing_ttl(key, value, other, ttl):
    store, clock = new_store()
    assert store.set(key, value, ttl=ttl) == {"ok": True}
    assert store.ttl(key) == {"found": True, "ttl": ttl}

    assert store.set(key, other) == {"ok": True}
    assert store.ttl(key) == {"found": True, "ttl": -1}
    clock.advance(ttl)
    assert store.get(key) == {"found": True, "value": other}
    assert_ttl_invariants(store)


@given(key=KEY, value=VALUE, ttl=EXPIRY_TTL)
@SETTINGS
def test_expire_nonpositive_removes_immediately(key, value, ttl):
    store, _ = new_store()
    assert store.set(key, value) == {"ok": True}
    if ttl <= 0:
        assert store.expire(key, ttl) == {"found": True, "ttl": 0}
        assert_empty(store)
    else:
        assert store.expire(key, ttl) == {"found": True, "ttl": ttl}
        assert store.get(key) == {"found": True, "value": value}
        assert_ttl_invariants(store)


@given(key=KEY, value=VALUE, ttl=TTL)
@SETTINGS
def test_persist_keeps_value_and_removes_ttl(key, value, ttl):
    store, clock = new_store()
    assert store.set(key, value, ttl=ttl) == {"ok": True}
    assert store.persist(key) == {"found": True, "persisted": True}
    assert store.ttl(key) == {"found": True, "ttl": -1}
    assert store.get(key) == {"found": True, "value": value}

    clock.advance(ttl)
    assert store.get(key) == {"found": True, "value": value}
    assert_ttl_invariants(store)


@given(
    key=KEY,
    initial=st.integers(min_value=-1_000, max_value=1_000),
    amount=AMOUNT,
    ttl=TTL,
)
@SETTINGS
def test_incr_decr_preserve_live_ttl(key, initial, amount, ttl):
    store, clock = new_store()
    assert store.set(key, initial, ttl=ttl) == {"ok": True}
    assert store.incr(key, amount) == {"ok": True, "value": initial + amount}
    assert store.ttl(key) == {"found": True, "ttl": ttl}
    assert store.decr(key, amount) == {"ok": True, "value": initial}
    assert store.ttl(key) == {"found": True, "ttl": ttl}

    clock.advance(ttl)
    assert store.get(key) == {"found": False}
    assert_ttl_invariants(store)
