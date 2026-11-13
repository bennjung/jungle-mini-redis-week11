"""사용자가 추가 승인한 S1-09·S1-10 입력 거절 계약."""

import pytest

from storage import MiniRedis
from tests.storage_helpers import snapshot


@pytest.fixture
def store():
    instance = MiniRedis()
    assert instance.set("sample:one", "hello") == {"ok": True}
    assert instance.set("sample:two", "world") == {"ok": True}
    assert snapshot(instance) == {"sample:one": "hello", "sample:two": "world"}
    return instance


def assert_rejected_and_unchanged(store, response, before):
    assert response == {"error": "INVALID_INPUT"}
    assert snapshot(store) == before
    assert before == {"sample:one": "hello", "sample:two": "world"}
    for key, value in before.items():
        assert store.get(key) == {"found": True, "value": value}
        assert snapshot(store) == before


@pytest.mark.parametrize("operation", ["set", "get", "delete"])
@pytest.mark.parametrize(
    "key",
    [
        pytest.param(None, id="S1-09-01"),
        pytest.param("", id="S1-09-02"),
        pytest.param(1, id="S1-09-03"),
        pytest.param(True, id="S1-09-04"),
        pytest.param([], id="S1-09-05"),
        pytest.param({}, id="S1-09-06"),
        pytest.param(b"key", id="S1-09-07"),
        pytest.param("a" * 257, id="S1-09-08"),
        pytest.param("\ud800", id="S1-09-09"),
    ],
)
def test_s1_09_reject_invalid_keys_without_state_changes(store, operation, key):
    before = snapshot(store)
    if operation == "set":
        response = store.set(key, "new")
    else:
        response = getattr(store, operation)(key)
    assert_rejected_and_unchanged(store, response, before)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(1, id="S1-10-01"),
        pytest.param(True, id="S1-10-02"),
        pytest.param([], id="S1-10-03"),
        pytest.param({}, id="S1-10-04"),
        pytest.param(b"hello", id="S1-10-05"),
        pytest.param("\ud800", id="S1-10-06"),
    ],
)
def test_s1_10_reject_invalid_values_without_state_changes(store, value):
    before = snapshot(store)
    response = store.set("sample:one", value)
    assert_rejected_and_unchanged(store, response, before)
