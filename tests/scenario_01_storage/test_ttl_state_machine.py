"""Hypothesis state machine: 연속된 KV·TTL·증감 연산을 참조 모델과 비교한다.

실행: .venv/bin/python -m pytest tests/scenario_01_storage/test_ttl_state_machine.py -q
"""

from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)

from storage import MiniRedis
from tests.scenario_01_storage.storage_helpers import snapshot
from tests.scenario_01_storage.ttl_clock_helpers import FakeClock
from tests.scenario_01_storage.ttl_reference import ReferenceModel
from tests.scenario_01_storage.ttl_state_helpers import (
    assert_ttl_invariants,
    ttl_snapshot,
)
from tests.scenario_01_storage.ttl_strategies import (
    AMOUNT,
    BOUND,
    EXPIRY_TTL,
    KEY,
    TTL,
    VALUE,
)

keys = Bundle("keys")


class TTLStateMachine(RuleBasedStateMachine):
    """set/get/delete/expire/ttl/persist/incr/decr를 무작위 순서로 실행한다."""

    def __init__(self):
        super().__init__()
        self.clock = FakeClock()
        self.store = MiniRedis(clock=self.clock)
        self.model = ReferenceModel(self.clock)

    def _check(self):
        assert snapshot(self.store) == self.model.data
        expires, ttl_keys, ttl_pos = ttl_snapshot(self.store)
        assert expires == self.model.expires
        assert set(ttl_keys) == set(self.model.expires)
        assert len(ttl_keys) == len(ttl_pos) == len(self.model.expires)
        assert_ttl_invariants(self.store)

    @invariant()
    def state_matches_model(self):
        self._check()

    @rule(target=keys, key=KEY, value=VALUE, ttl=st.one_of(st.none(), TTL))
    def set_key(self, key, value, ttl):
        actual = self.store.set(key, value, ttl=ttl)
        expected = self.model.set(key, value, ttl=ttl)
        assert actual == expected
        return key

    @rule(key=keys)
    def get_key(self, key):
        assert self.store.get(key) == self.model.get(key)

    @rule(key=keys)
    def delete_key(self, key):
        assert self.store.delete(key) == self.model.delete(key)

    @rule(key=keys, ttl=EXPIRY_TTL)
    def expire_key(self, key, ttl):
        assert self.store.expire(key, ttl) == self.model.expire(key, ttl)

    @rule(key=keys)
    def ttl_key(self, key):
        assert self.store.ttl(key) == self.model.ttl(key)

    @rule(key=keys)
    def persist_key(self, key):
        assert self.store.persist(key) == self.model.persist(key)

    @rule(key=keys, amount=AMOUNT, bound=st.one_of(st.none(), BOUND))
    def incr_key(self, key, amount, bound):
        if bound is None:
            actual = self.store.incr(key, amount)
            expected = self.model.incr(key, amount)
        else:
            actual = self.store.incr(key, amount, maximum=bound)
            expected = self.model.incr(key, amount, maximum=bound)
        assert actual == expected

    @rule(key=keys, amount=AMOUNT, bound=st.one_of(st.none(), BOUND))
    def decr_key(self, key, amount, bound):
        if bound is None:
            actual = self.store.decr(key, amount)
            expected = self.model.decr(key, amount)
        else:
            actual = self.store.decr(key, amount, minimum=bound)
            expected = self.model.decr(key, amount, minimum=bound)
        assert actual == expected

    @rule(ms=st.integers(min_value=0, max_value=2_000))
    def advance_time(self, ms):
        self.clock.advance(ms)


def test_ttl_state_machine():
    run_state_machine_as_test(
        TTLStateMachine,
        settings=hypothesis_settings(
            max_examples=50,
            stateful_step_count=50,
            deadline=None,
        ),
    )
