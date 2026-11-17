"""mutation testing이 찾은 경계·기본 경로 보강 테스트.

기존 TTL·저장소 테스트가 놓친 다음을 고정한다.
- 기본 sampler 경로
- live 키 뒤에 expired 키가 오는 표본
- 중첩 Unicode JSON의 65,536바이트 경계
- expire 1ms와 만료 키 persist
- 1바이트·256바이트 키 경계
"""

from storage import MiniRedis
from tests.scenario_01_storage.storage_helpers import snapshot
from tests.scenario_01_storage.ttl_clock_helpers import FakeClock, FixedMonotonic
from tests.scenario_01_storage.ttl_state_helpers import (
    assert_ttl_invariants,
    ttl_snapshot,
)

TTL_MS = 1_000


def new_store():
    clock = FakeClock()
    return MiniRedis(clock=clock, monotonic=FixedMonotonic()), clock


def test_nested_unicode_json_size_boundaries():
    """compact JSON·ensure_ascii=False 기준 65,536바이트 경계를 고정한다."""
    store, _ = new_store()

    exact = ["한" * 21_844]
    assert store.set("unicode:exact", exact) == {"ok": True}
    assert store.get("unicode:exact") == {"found": True, "value": exact}

    # 두 항목이면 comma가 들어간다. separator가 바뀌면 65,536을 넘는다.
    comma_boundary = ["한" * 10_921, "한" * 10_921]
    assert store.set("unicode:comma", comma_boundary) == {"ok": True}
    assert store.get("unicode:comma") == {
        "found": True,
        "value": comma_boundary,
    }


def test_expire_one_ms_and_persist_expired_key():
    store, clock = new_store()
    assert store.set("ttl:one", "hello") == {"ok": True}
    assert store.expire("ttl:one", 1) == {"found": True, "ttl": 1}
    assert store.ttl("ttl:one") == {"found": True, "ttl": 1}
    clock.advance(1)
    assert store.persist("ttl:one") == {"found": False}
    assert snapshot(store) == {}
    assert ttl_snapshot(store) == ({}, [], {})
    assert_ttl_invariants(store)


def test_expire_expired_key_without_prior_get():
    """get의 lazy 만료 없이도 expire가 만료 키를 found:false로 처리한다."""
    store, clock = new_store()
    assert store.set("ttl:one", "hello", ttl=1) == {"ok": True}
    clock.advance(1)
    assert store.expire("ttl:one", 1_000) == {"found": False}
    assert snapshot(store) == {}
    assert ttl_snapshot(store) == ({}, [], {})
    assert_ttl_invariants(store)


def test_key_length_boundaries_for_each_method():
    for key in ("a", "a" * 256):
        store, _ = new_store()
        assert store.set(key, "v") == {"ok": True}
        assert store.get(key) == {"found": True, "value": "v"}
        assert store.ttl(key) == {"found": True, "ttl": -1}
        assert store.persist(key) == {"found": True, "persisted": False}
        assert store.expire(key, 1_000) == {"found": True, "ttl": 1_000}
        assert store.delete(key) == {"found": True, "value": "v"}
        assert_ttl_invariants(store)


def test_counter_key_length_boundaries():
    for key in ("a", "a" * 256):
        store, _ = new_store()
        assert store.set(key, 0) == {"ok": True}
        assert store.incr(key, 1) == {"ok": True, "value": 1}
        assert store.decr(key, 1) == {"ok": True, "value": 0}


def test_active_default_sampler_removes_expired_keys():
    store, clock = new_store()
    keys = [f"cold:{index:02d}" for index in range(20)]
    for key in keys:
        assert store.set(key, "v", ttl=TTL_MS) == {"ok": True}
    clock.advance(TTL_MS)

    store._maybe_active_expire()

    assert ttl_snapshot(store) == ({}, [], {})
    assert snapshot(store) == {}
    assert_ttl_invariants(store)


def test_active_continues_after_live_key_in_sample():
    store, clock = new_store()
    live = "active:live"
    expired = "active:expired"
    assert store.set(live, "v", ttl=60_000) == {"ok": True}
    assert store.set(expired, "v", ttl=TTL_MS) == {"ok": True}
    clock.advance(TTL_MS)

    def sampler(keys, k):
        preferred = [live, expired]
        return [key for key in preferred if key in keys][:k]

    store._sampler = sampler
    store._maybe_active_expire()

    # get의 lazy 만료가 아니라 active cycle이 제거했는지 먼저 확인한다.
    assert expired not in ttl_snapshot(store)[0]
    assert expired not in ttl_snapshot(store)[1]
    assert store.get(expired) == {"found": False}
    assert store.get(live) == {"found": True, "value": "v"}
    assert_ttl_invariants(store)
