"""resc/scenario_01_storage/01-ttl-test-cases.md의 T1-01~13, T1-23~24 저장소 계약.

가짜 벽시계와 주입한 표본 선택기로 TTL 경계를 정확히 검증한다.
실행: .venv/bin/python -m pytest tests/scenario_01_storage/test_ttl_storage.py -q
"""

import pytest

from storage import MiniRedis
from tests.scenario_01_storage.storage_helpers import snapshot
from tests.scenario_01_storage.ttl_state_helpers import (
    assert_ttl_invariants,
    ttl_snapshot,
)

BASE_MS = 1_700_000_000_000


class FakeClock:
    """정수 epoch ms를 돌려주는 가짜 벽시계. advance(ms)로 시간을 진행한다."""

    def __init__(self, now=BASE_MS):
        self._now = now

    def __call__(self):
        return self._now

    def advance(self, ms):
        self._now += ms


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store(clock):
    """각 테스트는 빈 저장소와 기준 시각의 가짜 벽시계로 시작한다."""
    instance = MiniRedis(clock=clock)
    assert_ttl_invariants(instance)
    return instance


def assert_mapping(actual, expected):
    """전체 필드와 타입을 확인한다. True를 1로 반환해도 통과시키지 않는다."""
    assert type(actual) is dict
    assert actual.keys() == expected.keys()
    for key, value in expected.items():
        assert type(actual[key]) is type(value), key
        assert actual[key] == value, key


def assert_data(store, expected):
    assert_mapping(snapshot(store), expected)


def assert_ttl_state(store, expected_expires, expected_keys, expected_pos):
    """TTL 구조의 정확한 내용과 불변 조건을 함께 확인한다."""
    assert ttl_snapshot(store) == (expected_expires, expected_keys, expected_pos)
    assert_ttl_invariants(store)


def test_t1_01_set_with_ttl(store):
    assert_mapping(store.set("ttl:one", "hello", ttl=600_000), {"ok": True})
    assert_ttl_state(store, {"ttl:one": BASE_MS + 600_000}, ["ttl:one"],
                     {"ttl:one": 0})
    assert_data(store, {"ttl:one": "hello"})

    assert_mapping(
        store.ttl("ttl:one"), {"found": True, "ttl": 600_000},
    )
    assert_mapping(
        store.get("ttl:one"), {"found": True, "value": "hello"},
    )
    assert_data(store, {"ttl:one": "hello"})
    assert_ttl_state(store, {"ttl:one": BASE_MS + 600_000}, ["ttl:one"],
                     {"ttl:one": 0})


def test_t1_02_expiry_boundary(store, clock):
    assert_mapping(
        store.set("ttl:one", "hello", ttl=600_000), {"ok": True},
    )

    clock.advance(599_999)
    assert_mapping(store.ttl("ttl:one"), {"found": True, "ttl": 1})
    assert_mapping(store.get("ttl:one"), {"found": True, "value": "hello"})
    assert_ttl_state(store, {"ttl:one": BASE_MS + 600_000}, ["ttl:one"],
                     {"ttl:one": 0})

    clock.advance(1)
    assert_mapping(store.get("ttl:one"), {"found": False})
    assert_ttl_state(store, {}, [], {})
    assert_mapping(store.ttl("ttl:one"), {"found": False})
    assert_mapping(store.delete("ttl:one"), {"found": False})
    assert_data(store, {})


def test_t1_03_key_without_ttl(store):
    assert_mapping(store.set("plain:one", "hello"), {"ok": True})
    assert_ttl_state(store, {}, [], {})

    assert_mapping(store.ttl("plain:one"), {"found": True, "ttl": -1})
    assert_mapping(store.get("plain:one"), {"found": True, "value": "hello"})
    assert_ttl_state(store, {}, [], {})


def test_t1_04_overwrite_without_ttl_removes_ttl(store, clock):
    assert_mapping(
        store.set("ttl:one", "before", ttl=600_000), {"ok": True},
    )
    assert_ttl_state(store, {"ttl:one": BASE_MS + 600_000}, ["ttl:one"],
                     {"ttl:one": 0})

    assert_mapping(store.set("ttl:one", "after"), {"ok": True})
    assert_data(store, {"ttl:one": "after"})
    assert_ttl_state(store, {}, [], {})
    assert_mapping(store.ttl("ttl:one"), {"found": True, "ttl": -1})

    clock.advance(600_000)
    assert_mapping(store.get("ttl:one"), {"found": True, "value": "after"})
    assert_ttl_state(store, {}, [], {})


def test_t1_05_refresh_ttl(store, clock):
    assert_mapping(store.set("ttl:one", "hello", ttl=1_000), {"ok": True})

    clock.advance(500)
    assert_mapping(store.set("ttl:one", "hello2", ttl=2_000), {"ok": True})
    assert_ttl_state(store, {"ttl:one": BASE_MS + 500 + 2_000}, ["ttl:one"],
                     {"ttl:one": 0})
    assert_mapping(store.ttl("ttl:one"), {"found": True, "ttl": 2_000})

    clock.advance(1_999)
    assert_mapping(store.get("ttl:one"), {"found": True, "value": "hello2"})

    clock.advance(1)
    assert_mapping(store.get("ttl:one"), {"found": False})
    assert_ttl_state(store, {}, [], {})


@pytest.mark.parametrize("invalid_ttl", [0, -1, True, 1.5, "1000"])
def test_t1_06_set_rejects_invalid_ttl(store, invalid_ttl):
    assert_mapping(store.set("ttl:one", "before"), {"ok": True})
    assert_mapping(store.set("other:one", "keep"), {"ok": True})
    before = ttl_snapshot(store)

    assert_mapping(
        store.set("ttl:one", "after", ttl=invalid_ttl),
        {"error": "INVALID_INPUT"},
    )
    assert_data(store, {"ttl:one": "before", "other:one": "keep"})
    assert_mapping(store.get("ttl:one"), {"found": True, "value": "before"})
    assert_mapping(store.get("other:one"), {"found": True, "value": "keep"})
    assert ttl_snapshot(store) == before
    assert_ttl_invariants(store)


def test_t1_07a_overwrite_expired_key_without_ttl(store, clock):
    assert_mapping(store.set("ttl:one", "before", ttl=1_000), {"ok": True})
    clock.advance(1_000)

    assert_mapping(store.set("ttl:one", "after"), {"ok": True})
    assert_ttl_state(store, {}, [], {})
    assert_mapping(store.ttl("ttl:one"), {"found": True, "ttl": -1})

    clock.advance(1_000)
    assert_mapping(store.get("ttl:one"), {"found": True, "value": "after"})


def test_t1_07b_overwrite_expired_key_with_new_ttl(store, clock):
    assert_mapping(store.set("ttl:one", "before", ttl=1_000), {"ok": True})
    clock.advance(1_000)

    assert_mapping(store.set("ttl:one", "after", ttl=2_000), {"ok": True})
    assert_ttl_state(store, {"ttl:one": BASE_MS + 1_000 + 2_000}, ["ttl:one"],
                     {"ttl:one": 0})
    assert_mapping(store.ttl("ttl:one"), {"found": True, "ttl": 2_000})

    clock.advance(1_999)
    assert_mapping(store.get("ttl:one"), {"found": True, "value": "after"})


def test_t1_08_expire(store, clock):
    assert_mapping(store.set("ttl:one", "hello"), {"ok": True})
    assert_mapping(store.set("other:one", "keep"), {"ok": True})

    assert_mapping(
        store.expire("ttl:one", 60_000), {"found": True, "ttl": 60_000},
    )
    assert_ttl_state(store, {"ttl:one": BASE_MS + 60_000}, ["ttl:one"],
                     {"ttl:one": 0})
    assert_mapping(store.ttl("ttl:one"), {"found": True, "ttl": 60_000})
    assert_mapping(store.get("ttl:one"), {"found": True, "value": "hello"})

    assert_mapping(
        store.expire("ttl:one", 30_000), {"found": True, "ttl": 30_000},
    )
    assert_ttl_state(store, {"ttl:one": BASE_MS + 30_000}, ["ttl:one"],
                     {"ttl:one": 0})
    assert_mapping(store.ttl("ttl:one"), {"found": True, "ttl": 30_000})

    clock.advance(30_000)
    assert_mapping(store.get("ttl:one"), {"found": False})
    assert_ttl_state(store, {}, [], {})
    assert_data(store, {"other:one": "keep"})

    assert_mapping(store.expire("ttl:missing", 1_000), {"found": False})
    assert_mapping(store.expire("ttl:one", 1_000), {"found": False})
    assert_mapping(store.get("ttl:one"), {"found": False})
    assert_ttl_state(store, {}, [], {})


@pytest.mark.parametrize("non_positive_ttl", [0, -1])
def test_t1_08_expire_immediate_delete(store, non_positive_ttl):
    assert_mapping(
        store.set("ttl:one", "hello", ttl=600_000), {"ok": True},
    )
    assert_mapping(store.set("other:one", "keep"), {"ok": True})

    assert_mapping(
        store.expire("ttl:one", non_positive_ttl),
        {"found": True, "ttl": 0},
    )
    assert_mapping(store.get("ttl:one"), {"found": False})
    assert_data(store, {"other:one": "keep"})
    assert_ttl_state(store, {}, [], {})


@pytest.mark.parametrize("invalid_ttl", [None, True, 1.5, "1000"])
def test_t1_09_expire_rejects_invalid_ttl(store, invalid_ttl):
    assert_mapping(store.set("ttl:one", "before"), {"ok": True})
    before = ttl_snapshot(store)

    assert_mapping(
        store.expire("ttl:one", invalid_ttl), {"error": "INVALID_INPUT"},
    )
    assert_mapping(store.get("ttl:one"), {"found": True, "value": "before"})
    assert ttl_snapshot(store) == before
    assert_ttl_invariants(store)


def test_t1_10_ttl_lookup(store, clock):
    assert_mapping(store.set("ttl:one", "hello", ttl=60_000), {"ok": True})
    assert_mapping(store.set("plain:one", "hello"), {"ok": True})

    assert_mapping(store.ttl("ttl:one"), {"found": True, "ttl": 60_000})
    assert_mapping(store.ttl("plain:one"), {"found": True, "ttl": -1})
    assert_mapping(store.ttl("missing"), {"found": False})

    clock.advance(60_000)
    assert_mapping(store.ttl("ttl:one"), {"found": False})
    assert_ttl_state(store, {}, [], {})
    assert_data(store, {"plain:one": "hello"})


def test_t1_11_persist(store, clock):
    assert_mapping(store.set("ttl:one", "hello", ttl=60_000), {"ok": True})
    assert_mapping(store.set("plain:one", "hello"), {"ok": True})

    assert_mapping(
        store.persist("ttl:one"), {"found": True, "persisted": True},
    )
    assert_mapping(store.ttl("ttl:one"), {"found": True, "ttl": -1})
    assert_mapping(store.get("ttl:one"), {"found": True, "value": "hello"})
    assert_ttl_state(store, {}, [], {})

    assert_mapping(
        store.persist("plain:one"), {"found": True, "persisted": False},
    )
    assert_mapping(store.persist("missing"), {"found": False})

    clock.advance(60_000)
    assert_mapping(store.get("ttl:one"), {"found": True, "value": "hello"})
    assert_ttl_state(store, {}, [], {})


def test_t1_12_get_lazy_expiry(store, clock):
    assert_mapping(store.set("ttl:one", "hello", ttl=1_000), {"ok": True})
    clock.advance(1_000)

    assert_mapping(store.get("ttl:one"), {"found": False})
    assert_ttl_state(store, {}, [], {})
    assert_data(store, {})


def test_t1_12_delete_lazy_expiry(store, clock):
    assert_mapping(store.set("ttl:one", "hello", ttl=1_000), {"ok": True})
    clock.advance(1_000)

    assert_mapping(store.delete("ttl:one"), {"found": False})
    assert_ttl_state(store, {}, [], {})
    assert_data(store, {})


def test_t1_13_invariants_across_operations(store, clock):
    assert_ttl_invariants(store)

    assert_mapping(store.set("ttl:one", "hello"), {"ok": True})
    assert_ttl_invariants(store)
    assert_mapping(store.expire("ttl:one", 60_000), {"found": True, "ttl": 60_000})
    assert_ttl_invariants(store)
    assert_mapping(
        store.persist("ttl:one"), {"found": True, "persisted": True},
    )
    assert_ttl_invariants(store)
    assert_mapping(store.set("ttl:one", "after"), {"ok": True})
    assert_ttl_invariants(store)
    assert_mapping(
        store.delete("ttl:one"), {"found": True, "value": "after"},
    )
    assert_ttl_invariants(store)

    # TTL 키와 TTL 없는 키를 섞은 상태에서 각 연산 뒤 불변 조건을 검사한다.
    assert_mapping(store.set("mix:ttl:a", "a", ttl=10_000), {"ok": True})
    assert_mapping(store.set("mix:plain", "p"), {"ok": True})
    assert_ttl_invariants(store)
    assert_mapping(store.set("mix:ttl:b", "b", ttl=20_000), {"ok": True})
    assert_ttl_invariants(store)
    assert_mapping(store.set("mix:ttl:a", "a2", ttl=30_000), {"ok": True})
    assert_ttl_invariants(store)
    assert_mapping(store.expire("mix:plain", 5_000), {"found": True, "ttl": 5_000})
    assert_ttl_invariants(store)
    assert_mapping(store.persist("mix:ttl:b"), {"found": True, "persisted": True})
    assert_ttl_invariants(store)
    assert_mapping(store.delete("mix:ttl:a"), {"found": True, "value": "a2"})
    assert_ttl_invariants(store)

    clock.advance(5_000)
    assert_mapping(store.get("mix:plain"), {"found": False})
    assert_ttl_invariants(store)
    assert_ttl_state(store, {}, [], {})
    assert_data(store, {"mix:ttl:b": "b"})


def test_t1_13_ttl_snapshot_is_a_copy(store):
    assert_mapping(store.set("ttl:one", "hello", ttl=600_000), {"ok": True})
    before = ttl_snapshot(store)

    expires, ttl_keys, ttl_pos = ttl_snapshot(store)
    expires["ttl:one"] = 0
    ttl_keys.clear()
    ttl_pos["ttl:one"] = 99

    assert ttl_snapshot(store) == before
    assert_ttl_invariants(store)
    assert_mapping(store.ttl("ttl:one"), {"found": True, "ttl": 600_000})

    # 기존 snapshot은 `_data`만 반환하는 의미를 유지한다.
    data = snapshot(store)
    data["ttl:one"] = "changed"
    assert_data(store, {"ttl:one": "hello"})


def test_t1_23_incr_decr_lazy_expiry_and_ttl(store, clock):
    assert_mapping(store.set("count:ttl", 10, ttl=60_000), {"ok": True})
    assert_mapping(store.set("count:plain", 10), {"ok": True})
    expiry = BASE_MS + 60_000
    assert_ttl_state(store, {"count:ttl": expiry}, ["count:ttl"],
                     {"count:ttl": 0})

    assert_mapping(
        store.incr("count:ttl", 5), {"ok": True, "value": 15},
    )
    assert_data(store, {"count:ttl": 15, "count:plain": 10})
    assert_ttl_state(store, {"count:ttl": expiry}, ["count:ttl"],
                     {"count:ttl": 0})
    assert_mapping(store.ttl("count:ttl"), {"found": True, "ttl": 60_000})

    assert_mapping(
        store.decr("count:ttl", 3), {"ok": True, "value": 12},
    )
    assert_ttl_state(store, {"count:ttl": expiry}, ["count:ttl"],
                     {"count:ttl": 0})
    assert_mapping(store.ttl("count:ttl"), {"found": True, "ttl": 60_000})

    clock.advance(60_000)
    assert_mapping(store.incr("count:ttl", 1), {"error": "NOT_FOUND"})
    assert_data(store, {"count:plain": 10})
    assert_ttl_state(store, {}, [], {})
    assert_mapping(store.get("count:ttl"), {"found": False})

    assert_mapping(
        store.incr("count:plain", 1), {"ok": True, "value": 11},
    )
    assert_ttl_state(store, {}, [], {})


class StepClock:
    """호출될 때마다 1ms씩 진행하는 시계. 시각을 몇 번 읽는지 드러낸다."""

    def __init__(self, now=BASE_MS):
        self._now = now

    def __call__(self):
        self._now += 1
        return self._now


def test_t1_24_ttl_uses_one_clock_read():
    """T1-24: ttl은 만료 판정과 남은 시간 계산에 같은 시각을 쓴다."""
    clock = StepClock()
    store = MiniRedis(clock=clock)

    assert_mapping(store.set("ttl:one", "hello", ttl=2), {"ok": True})
    assert_mapping(store.ttl("ttl:one"), {"found": True, "ttl": 1})
    assert_mapping(store.get("ttl:one"), {"found": False})
