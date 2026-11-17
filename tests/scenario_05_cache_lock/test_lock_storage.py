"""시나리오 05: 저장소 키별 원자적 락 계약 테스트 (L1-01 ~ L1-11)."""

import pytest

from storage import MiniRedis
from tests.scenario_01_storage.ttl_clock_helpers import (
    BASE_MS,
    FakeClock,
    FirstKSampler,
    FixedMonotonic,
    StepMonotonic,
)


def assert_lock_invariants(store: MiniRedis) -> None:
    """_locks, _lock_keys, _lock_pos의 O(1) swap-pop 불변 조건을 검증한다."""
    assert set(store._locks) == set(store._lock_keys) == set(store._lock_pos)
    assert len(store._locks) == len(store._lock_keys) == len(store._lock_pos)
    for key, idx in store._lock_pos.items():
        assert store._lock_keys[idx] == key


def test_l1_01_acquire_success_and_data_untouched():
    clock = FakeClock(BASE_MS)
    store = MiniRedis(clock=clock)

    res = store.acquire("weather:seoul", "t1", 5000)

    assert res == {"acquired": True}
    assert store._locks["weather:seoul"] == ("t1", BASE_MS + 5000)
    assert store._data == {}
    assert store._expires == {}
    assert_lock_invariants(store)


def test_l1_02_acquire_busy_returns_retry_after_ms():
    clock = FakeClock(BASE_MS)
    store = MiniRedis(clock=clock)
    assert store.acquire("weather:seoul", "t1", 5000) == {"acquired": True}

    clock.advance(800)
    res = store.acquire("weather:seoul", "t2", 5000)

    assert res == {"error": "LOCK_BUSY", "retry_after_ms": 4200}
    assert store._locks["weather:seoul"] == ("t1", BASE_MS + 5000)
    assert_lock_invariants(store)


def test_l1_03_acquire_same_token_extends_ttl():
    clock = FakeClock(BASE_MS)
    store = MiniRedis(clock=clock)
    assert store.acquire("weather:seoul", "t1", 5000) == {"acquired": True}

    clock.advance(3000)
    res = store.acquire("weather:seoul", "t1", 5000)

    assert res == {"acquired": True}
    assert store._locks["weather:seoul"] == ("t1", BASE_MS + 8000)
    assert_lock_invariants(store)


def test_l1_04_locks_are_independent_per_key():
    clock = FakeClock(BASE_MS)
    store = MiniRedis(clock=clock)
    assert store.acquire("weather:seoul", "t1", 5000) == {"acquired": True}

    res = store.acquire("weather:busan", "t2", 5000)

    assert res == {"acquired": True}
    assert store._locks["weather:seoul"] == ("t1", BASE_MS + 5000)
    assert store._locks["weather:busan"] == ("t2", BASE_MS + 5000)
    assert_lock_invariants(store)


def test_l1_05_acquire_at_exact_expiry_boundary_succeeds():
    clock = FakeClock(BASE_MS)
    store = MiniRedis(clock=clock)
    assert store.acquire("weather:seoul", "t1", 5000) == {"acquired": True}

    clock.advance(5000)
    res = store.acquire("weather:seoul", "t2", 3000)

    assert res == {"acquired": True}
    assert store._locks["weather:seoul"] == ("t2", BASE_MS + 8000)
    assert_lock_invariants(store)


def test_l1_06_release_by_owner_allows_next_acquire():
    clock = FakeClock(BASE_MS)
    store = MiniRedis(clock=clock)
    assert store.acquire("weather:seoul", "t1", 5000) == {"acquired": True}

    rel = store.release("weather:seoul", "t1")
    acq = store.acquire("weather:seoul", "t2", 5000)

    assert rel == {"released": True}
    assert acq == {"acquired": True}
    assert store._locks["weather:seoul"] == ("t2", BASE_MS + 5000)
    assert_lock_invariants(store)


def test_l1_07_release_by_other_token_is_rejected():
    clock = FakeClock(BASE_MS)
    store = MiniRedis(clock=clock)
    assert store.acquire("weather:seoul", "t1", 5000) == {"acquired": True}

    rel = store.release("weather:seoul", "t2")

    assert rel == {"released": False}
    assert store._locks["weather:seoul"] == ("t1", BASE_MS + 5000)
    assert_lock_invariants(store)


def test_l1_08_release_missing_or_expired_lock_returns_false_and_cleans_up():
    clock = FakeClock(BASE_MS)
    store = MiniRedis(clock=clock)

    assert store.release("weather:seoul", "t1") == {"released": False}

    assert store.acquire("weather:seoul", "t1", 5000) == {"acquired": True}
    clock.advance(5000)
    assert store.release("weather:seoul", "t1") == {"released": False}
    assert "weather:seoul" not in store._locks
    assert_lock_invariants(store)


def test_l1_09_kv_operations_do_not_collide_with_locks():
    clock = FakeClock(BASE_MS)
    store = MiniRedis(clock=clock)
    assert store.acquire("weather:seoul", "t1", 5000) == {"acquired": True}

    assert store.set("weather:seoul", {"temp": 22}, ttl=60_000) == {"ok": True}
    assert store.get("weather:seoul") == {"found": True, "value": {"temp": 22}}
    assert store.delete("weather:seoul") == {"found": True, "value": {"temp": 22}}

    assert store._locks["weather:seoul"] == ("t1", BASE_MS + 5000)
    assert_lock_invariants(store)


@pytest.mark.parametrize(
    ("key", "token", "ttl"),
    [
        ("", "t1", 5000),
        (None, "t1", 5000),
        ("a" * 257, "t1", 5000),
        ("k1", "", 5000),
        ("k1", None, 5000),
        ("k1", "t" * 257, 5000),
        ("k1", "t1", 0),
        ("k1", "t1", -1),
        ("k1", "t1", True),
        ("k1", "t1", "5000"),
        ("k1", "t1", 1.5),
        ("k1", "t1", None),
    ],
)
def test_l1_10_invalid_inputs_preserve_existing_locks(key, token, ttl):
    clock = FakeClock(BASE_MS)
    store = MiniRedis(clock=clock)
    assert store.acquire("k1", "owner", 5000) == {"acquired": True}

    assert store.acquire(key, token, ttl) == {"error": "INVALID_INPUT"}
    assert store._locks == {"k1": ("owner", BASE_MS + 5000)}
    assert_lock_invariants(store)


@pytest.mark.parametrize(
    ("key", "token"),
    [
        ("", "t1"),
        (None, "t1"),
        ("a" * 257, "t1"),
        ("k1", ""),
        ("k1", None),
        ("k1", "t" * 257),
    ],
)
def test_l1_10_release_invalid_inputs_preserve_existing_locks(key, token):
    clock = FakeClock(BASE_MS)
    store = MiniRedis(clock=clock)
    assert store.acquire("k1", "owner", 5000) == {"acquired": True}

    assert store.release(key, token) == {"error": "INVALID_INPUT"}
    assert store._locks == {"k1": ("owner", BASE_MS + 5000)}
    assert_lock_invariants(store)


def test_l1_11_active_expire_cleans_expired_locks_with_25pct_and_budget():
    clock = FakeClock(BASE_MS)
    sampler = FirstKSampler()
    monotonic = FixedMonotonic()
    store = MiniRedis(clock=clock, sampler=sampler, monotonic=monotonic)

    for i in range(45):
        assert store.acquire(f"lock:exp:{i}", f"t{i}", 100) == {"acquired": True}
    for i in range(5):
        assert store.acquire(f"lock:live:{i}", f"t{i}", 10_000) == {"acquired": True}

    clock.advance(100)
    store._maybe_active_expire()

    for i in range(45):
        assert f"lock:exp:{i}" not in store._locks
    for i in range(5):
        assert f"lock:live:{i}" in store._locks
    assert len(sampler.calls) >= 2
    assert_lock_invariants(store)


def test_l1_11_active_expire_stops_on_time_budget_shared_with_ttl():
    clock = FakeClock(BASE_MS)
    sampler = FirstKSampler()
    monotonic = StepMonotonic(step_s=0.0006)
    store = MiniRedis(clock=clock, sampler=sampler, monotonic=monotonic)

    for i in range(30):
        assert store.set(f"ttl:{i}", i, ttl=100) == {"ok": True}
    for i in range(30):
        assert store.acquire(f"lock:{i}", f"t{i}", 100) == {"acquired": True}

    clock.advance(100)
    store._maybe_active_expire()

    # 시간 예산(1ms)을 초과하면 무한 루프 없이 중단되어야 한다.
    assert len(sampler.calls) <= 3
    assert_lock_invariants(store)
