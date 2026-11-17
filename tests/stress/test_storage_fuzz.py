"""저장소 랜덤 fuzz: 참조 모델과 TTL 불변 조건을 함께 검증한다.

기본 pytest에서는 건너뛰고, STRESS=1일 때만 실행한다.
실행: STRESS=1 STRESS_OPS=200000 STRESS_SEED=42 \
  .venv/bin/python -m pytest tests/stress/test_storage_fuzz.py -q
"""

import os
import random

import pytest

from storage import MiniRedis
from tests.scenario_01_storage.storage_helpers import snapshot
from tests.scenario_01_storage.ttl_reference import ReferenceModel
from tests.scenario_01_storage.ttl_state_helpers import (
    assert_ttl_invariants,
    ttl_snapshot,
)
from tests.stress.stress_common import (
    INVALID_RATIO,
    OPERATION_WEIGHTS,
    TTL_WRITE_RATIO,
    choose_key,
    random_amount,
    random_bound,
    random_ttl,
    random_value,
    weighted_choice,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("STRESS") != "1",
    reason="STRESS=1일 때만 실행하는 저장소 랜덤 fuzz",
)

STABLE_TEXT = "oracle:stable:one"
STABLE_NUM = "oracle:stable:num"


class FakeClock:
    """정수 epoch ms를 돌려주는 가짜 벽시계."""

    def __init__(self, now=1_700_000_000_000):
        self._now = now

    def __call__(self):
        return self._now

    def advance(self, ms):
        self._now += ms


def _seed_stable(store, model):
    """잘못된 입력 테스트에 쓸, TTL 없는 고정 키를 준비한다."""
    assert store.set(STABLE_TEXT, "stable") == {"ok": True}
    model.set(STABLE_TEXT, "stable")
    assert store.set(STABLE_NUM, 0) == {"ok": True}
    model.set(STABLE_NUM, 0)


def _assert_unchanged(store, before_data, before_ttl):
    assert snapshot(store) == before_data
    assert ttl_snapshot(store) == before_ttl
    assert_ttl_invariants(store)


def _invalid_key():
    return random.choice([None, 123, True, [], "", "a" * 257, "\ud800"])


def _run_invalid(store, rng):
    """계약상 거절되어야 하는 입력을 하나 실행하고 상태 보존을 확인한다."""
    before_data = snapshot(store)
    before_ttl = ttl_snapshot(store)
    choice = rng.randrange(12)

    if choice == 0:
        result = store.set(_invalid_key(), "value")
    elif choice == 1:
        result = store.set(STABLE_TEXT, b"bytes")
    elif choice == 2:
        result = store.set(STABLE_TEXT, {"a": b"bytes"})
    elif choice == 3:
        result = store.set(STABLE_TEXT, "value", ttl=rng.choice([0, -1, True, 1.5, "1000"]))
    elif choice == 4:
        result = store.expire(STABLE_TEXT, rng.choice([None, True, 1.5, "1000"]))
    elif choice == 5:
        result = store.incr(STABLE_NUM, rng.choice([-1, True, "1", None, float("inf")]))
    elif choice == 6:
        result = store.decr(STABLE_NUM, rng.choice([-1, True, "1", None, float("inf")]))
    elif choice == 7:
        result = store.incr(STABLE_NUM, 1, maximum=rng.choice([True, "1", None, float("inf")]))
    elif choice == 8:
        result = store.decr(STABLE_NUM, 1, minimum=rng.choice([True, "1", None, float("inf")]))
    elif choice == 9:
        result = store.get(_invalid_key())
    elif choice == 10:
        result = store.delete(_invalid_key())
    else:
        result = store.ttl(_invalid_key())

    assert "error" in result, result
    assert result["error"] in {
        "INVALID_INPUT",
        "TYPE_MISMATCH",
        "NEGATIVE_AMOUNT",
        "OUT_OF_RANGE",
    }, result
    _assert_unchanged(store, before_data, before_ttl)


def test_storage_random_fuzz():
    seed = int(os.environ.get("STRESS_SEED", "42"))
    operations = int(os.environ.get("STRESS_OPS", "200000"))
    keyspace = int(os.environ.get("STRESS_KEYSPACE", "10000"))
    rng = random.Random(seed)
    clock = FakeClock()
    store = MiniRedis(clock=clock)
    model = ReferenceModel(clock)
    _seed_stable(store, model)

    for step in range(operations):
        clock.advance(rng.randrange(0, 101))

        if rng.random() < INVALID_RATIO:
            _run_invalid(store, rng)
            continue

        operation = weighted_choice(rng, OPERATION_WEIGHTS)
        key = choose_key(rng, keyspace)

        if operation == "set":
            value = random_value(rng)
            ttl = random_ttl(rng) if rng.random() < TTL_WRITE_RATIO else None
            actual = store.set(key, value, ttl=ttl)
            expected = model.set(key, value, ttl=ttl)
        elif operation == "get":
            actual, expected = store.get(key), model.get(key)
        elif operation == "delete":
            actual, expected = store.delete(key), model.delete(key)
        elif operation == "expire":
            ttl = rng.choice([-1, 0, 1_000, 3_000, 10_000])
            actual, expected = store.expire(key, ttl), model.expire(key, ttl)
        elif operation == "ttl":
            actual, expected = store.ttl(key), model.ttl(key)
        elif operation == "persist":
            actual, expected = store.persist(key), model.persist(key)
        elif operation == "incr":
            amount = random_amount(rng)
            if rng.random() < 0.2:
                bound = random_bound(rng, 1)
                actual = store.incr(key, amount, maximum=bound)
                expected = model.incr(key, amount, maximum=bound)
            else:
                actual = store.incr(key, amount)
                expected = model.incr(key, amount)
        else:
            amount = random_amount(rng)
            if rng.random() < 0.2:
                bound = random_bound(rng, -1)
                actual = store.decr(key, amount, minimum=bound)
                expected = model.decr(key, amount, minimum=bound)
            else:
                actual = store.decr(key, amount)
                expected = model.decr(key, amount)

        assert actual == expected, (step, operation, key, actual, expected)
        assert_ttl_invariants(store)

    assert snapshot(store) == model.data
    expires, ttl_keys, ttl_pos = ttl_snapshot(store)
    assert expires == model.expires
    assert set(ttl_keys) == set(model.expires)
    assert len(ttl_keys) == len(ttl_pos) == len(model.expires)
    assert_ttl_invariants(store)
