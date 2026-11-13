"""resc/scenario_01_storage/01-storage-scenario.md의 저장소 행동 계약. HTTP 테스트는 별도다."""

import pytest

from tests.scenario_01_storage.storage_helpers import snapshot


def assert_mapping(actual, expected):
    """전체 필드와 타입을 확인한다. True를 1로 반환해도 통과시키지 않는다."""
    assert type(actual) is dict
    assert actual.keys() == expected.keys()
    for key, value in expected.items():
        assert type(actual[key]) is type(value), key
        assert actual[key] == value, key


def assert_state(store, expected):
    assert_mapping(snapshot(store), expected)


def assert_step(store, result, expected_result, expected_state):
    """각 호출 직후 반환과 전체 상태를 확인하고 다음 호출로 넘어간다."""
    assert_mapping(result, expected_result)
    assert_state(store, expected_state)


@pytest.fixture
def store():
    # 구현이 없어도 케이스는 수집하며, 실행 시 미정의 의존성을 오류로 보고한다.
    from storage import MiniRedis

    instance = MiniRedis()
    assert_state(instance, {})
    return instance


def prepare(store, entries):
    """내부 상태를 직접 주입하지 않고 승인된 set으로 사전 상태를 준비한다."""
    state = {}
    for key, value in entries.items():
        state[key] = value
        assert_step(store, store.set(key, value), {"ok": True}, state)


def test_s1_01_store_and_read_twice(store):
    state = {"sample:one": "hello"}
    assert_step(store, store.set("sample:one", "hello"), {"ok": True}, state)
    for _ in range(2):
        assert_step(
            store, store.get("sample:one"),
            {"found": True, "value": "hello"}, state,
        )


def test_s1_02_delete_and_read_twice(store):
    prepare(store, {"sample:one": "hello"})
    assert_step(
        store, store.delete("sample:one"),
        {"found": True, "value": "hello"}, {},
    )
    for _ in range(2):
        assert_step(store, store.get("sample:one"), {"found": False}, {})


def test_s1_03_delete_preserves_other_key(store):
    prepare(store, {"sample:one": "hello", "sample:two": "world"})
    state = {"sample:two": "world"}
    assert_step(
        store, store.delete("sample:one"),
        {"found": True, "value": "hello"}, state,
    )
    assert_step(
        store, store.get("sample:two"),
        {"found": True, "value": "world"}, state,
    )


def test_s1_04_overwrite_preserves_other_key(store):
    prepare(store, {"sample:one": "hello", "sample:two": "world"})
    state = {"sample:one": "updated", "sample:two": "world"}
    assert_step(store, store.set("sample:one", "updated"), {"ok": True}, state)
    for _ in range(2):
        assert_step(
            store, store.get("sample:one"),
            {"found": True, "value": "updated"}, state,
        )
        assert_step(
            store, store.get("sample:two"),
            {"found": True, "value": "world"}, state,
        )


def test_s1_05_missing_keys_and_repeated_delete(store):
    state = {"sample:one": "hello"}
    prepare(store, state)
    assert_step(store, store.get("sample:missing"), {"found": False}, state)
    assert_step(store, store.delete("sample:missing"), {"found": False}, state)
    assert_step(
        store, store.delete("sample:one"),
        {"found": True, "value": "hello"}, {},
    )
    assert_step(store, store.delete("sample:one"), {"found": False}, {})
    assert_step(store, store.get("sample:one"), {"found": False}, {})
    assert_step(store, store.get("sample:missing"), {"found": False}, {})


def test_s1_06_empty_string_and_json_text(store):
    json_text = '{"a":1}'
    assert_step(
        store, store.set("sample:empty", ""),
        {"ok": True}, {"sample:empty": ""},
    )
    state = {"sample:empty": "", "sample:text": json_text}
    assert_step(store, store.set("sample:text", json_text), {"ok": True}, state)
    assert_step(
        store, store.get("sample:empty"), {"found": True, "value": ""}, state,
    )
    assert_step(
        store, store.get("sample:text"),
        {"found": True, "value": json_text}, state,
    )
    assert_step(store, store.get("sample:missing"), {"found": False}, state)
    state = {"sample:text": json_text}
    assert_step(
        store, store.delete("sample:empty"), {"found": True, "value": ""}, state,
    )
    assert_step(store, store.delete("sample:empty"), {"found": False}, state)
    assert_step(
        store, store.get("sample:text"),
        {"found": True, "value": json_text}, state,
    )


def test_s1_06_01_number_and_null_text_remain_strings(store):
    assert_step(
        store, store.set("sample:number", "10"),
        {"ok": True}, {"sample:number": "10"},
    )
    state = {"sample:number": "10", "sample:word": "null"}
    assert_step(store, store.set("sample:word", "null"), {"ok": True}, state)
    assert_step(
        store, store.get("sample:number"),
        {"found": True, "value": "10"}, state,
    )
    assert_step(
        store, store.get("sample:word"),
        {"found": True, "value": "null"}, state,
    )


@pytest.mark.parametrize(
    "key,value,accepted",
    [
        pytest.param(None, "new", False, id="S1-07-01"),
        pytest.param("", "new", False, id="S1-07-02"),
        pytest.param("a", "hello", True, id="S1-07-03"),
        pytest.param("a" * 256, "hello", True, id="S1-07-04"),
        pytest.param("a" * 257, "hello", False, id="S1-07-05"),
        pytest.param("한" * 85 + "a", "hello", True, id="S1-07-06"),
        pytest.param("한" * 86, "hello", False, id="S1-07-07"),
        pytest.param("sample:one", "a" * 65_536, True, id="S1-07-08"),
        pytest.param("sample:one", "a" * 65_537, False, id="S1-07-09"),
        pytest.param("sample:one", "한" * 21_845 + "a", True, id="S1-07-10"),
        pytest.param("sample:one", "한" * 21_845 + "aa", False, id="S1-07-11"),
    ],
)
def test_s1_07_size_limits_and_state_preservation(store, key, value, accepted):
    initial = {"sample:one": "hello", "sample:two": "world"}
    prepare(store, initial)
    before = snapshot(store)
    assert_mapping(before, initial)
    expected = initial.copy()
    if accepted:
        expected[key] = value
    response = {"ok": True} if accepted else {"error": "INVALID_INPUT"}

    assert_step(store, store.set(key, value), response, expected)
    # 저장 후에도 이전 스냅샷 내용이 바뀌지 않아야 한다.
    assert_mapping(before, initial)
    if not accepted:
        assert_mapping(snapshot(store), before)

    assert_step(
        store, store.get("sample:two"),
        {"found": True, "value": "world"}, expected,
    )
    if accepted and key != "sample:one":
        assert_step(
            store, store.get(key), {"found": True, "value": "hello"}, expected,
        )
    assert_step(
        store, store.get("sample:one"),
        {"found": True, "value": expected["sample:one"]}, expected,
    )


def test_s1_08_actual_null_store_read_delete(store):
    state = {"sample:null": None}
    assert_step(store, store.set("sample:null", None), {"ok": True}, state)
    assert_step(
        store, store.get("sample:null"), {"found": True, "value": None}, state,
    )
    assert_step(store, store.get("sample:missing"), {"found": False}, state)
    assert_step(
        store, store.delete("sample:null"), {"found": True, "value": None}, {},
    )
    assert_step(store, store.delete("sample:null"), {"found": False}, {})
    assert_step(store, store.get("sample:null"), {"found": False}, {})


def test_s1_08_01_overwrite_between_string_and_actual_null(store):
    prepare(store, {"sample:one": "hello", "sample:two": "world"})
    state = {"sample:one": None, "sample:two": "world"}
    assert_step(store, store.set("sample:one", None), {"ok": True}, state)
    assert_step(
        store, store.get("sample:one"), {"found": True, "value": None}, state,
    )
    state = {"sample:one": "updated", "sample:two": "world"}
    assert_step(store, store.set("sample:one", "updated"), {"ok": True}, state)
    assert_step(
        store, store.get("sample:one"),
        {"found": True, "value": "updated"}, state,
    )
    assert_step(
        store, store.get("sample:two"),
        {"found": True, "value": "world"}, state,
    )


def test_snapshot_is_an_independent_copy(store):
    """합의한 관찰 메서드 계약: 사본 수정으로 저장소를 바꿀 수 없다."""
    prepare(store, {"sample:one": "hello"})
    state_copy = snapshot(store)
    state_copy["sample:one"] = "updated"
    state_copy["sample:two"] = None
    assert_state(store, {"sample:one": "hello"})
    state_copy.clear()
    assert_step(
        store, store.get("sample:one"),
        {"found": True, "value": "hello"}, {"sample:one": "hello"},
    )
