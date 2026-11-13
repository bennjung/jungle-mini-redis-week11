"""범위 C: HTTP와 분리한 MiniRedis.incr/decr 직접 호출 계약."""

import pytest

from storage import MiniRedis
from tests.scenario_02_concurrent_counter.contract_helpers import assert_json, state
from tests.scenario_02_concurrent_counter.test_counter_http import (
    CONDITION_CASES, NON_NUMBERS, SUCCESS_CASES, TARGET_VALUES,
)


def prepared_store(initial):
    store = MiniRedis()
    assert store.set("counter:other", "keep") == {"ok": True}
    result = store.set("counter:one", initial)
    assert result == {"ok": True}, ("#1 숫자·자료형 저장 선행 조건", result)
    return store


@pytest.mark.parametrize("case_id,operation,initial,amount,bound,expected", SUCCESS_CASES,
                         ids=[item[0] for item in SUCCESS_CASES])
def test_c01_success(case_id, operation, initial, amount, bound, expected):
    store = prepared_store(initial)
    actual = getattr(store, operation)("counter:one", amount, **bound)
    assert_json(actual, {"ok": True, "value": expected})
    for _ in range(2):
        assert_json(store.get("counter:one"), {"found": True, "value": expected})
    assert_json(state(store), {"counter:one": expected, "counter:other": "keep"})


@pytest.mark.parametrize("case_id,operation,initial,amount,bound", CONDITION_CASES,
                         ids=[item[0] for item in CONDITION_CASES])
def test_c02_condition_rejection(case_id, operation, initial, amount, bound):
    store = prepared_store(initial)
    before = state(store)
    actual = getattr(store, operation)("counter:one", amount, **bound)
    assert_json(actual, {"error": "CONDITION_NOT_MET", "value": initial})
    assert_json(state(store), before)
    assert_json(getattr(store, operation)("counter:one", 0), {"ok": True, "value": initial})


def test_c02_floating_result():
    store = prepared_store(0.1)
    assert_json(store.incr("counter:one", 0.2), {"ok": True, "value": 0.30000000000000004})
    assert_json(store.get("counter:one"), {"found": True, "value": 0.30000000000000004})


@pytest.mark.parametrize("operation", ["incr", "decr"])
@pytest.mark.parametrize("actual_type,value", TARGET_VALUES)
def test_c03_target_type(operation, actual_type, value):
    store = prepared_store(value)
    before = state(store)
    actual = getattr(store, operation)("counter:one", 1)
    assert_json(actual, {"error": "TYPE_MISMATCH", "field": "value", "actual_type": actual_type})
    assert_json(state(store), before)


@pytest.mark.parametrize("operation", ["incr", "decr"])
@pytest.mark.parametrize("field", ["amount", "bound"])
@pytest.mark.parametrize("actual_type,value", NON_NUMBERS)
def test_c03_input_type(operation, field, actual_type, value):
    store = prepared_store(10)
    before = state(store)
    field = ("maximum" if operation == "incr" else "minimum") if field == "bound" else field
    amount = value if field == "amount" else 1
    bound = {} if field == "amount" else {field: value}
    actual = getattr(store, operation)("counter:one", amount, **bound)
    assert_json(actual, {"error": "TYPE_MISMATCH", "field": field, "actual_type": actual_type})
    assert_json(state(store), before)


@pytest.mark.parametrize("operation", ["incr", "decr"])
@pytest.mark.parametrize("conditioned", [False, True])
def test_c04_negative_amount(operation, conditioned):
    store = prepared_store(10)
    before = state(store)
    bound = {"maximum": 20} if operation == "incr" else {"minimum": 0}
    actual = getattr(store, operation)("counter:one", -1, **(bound if conditioned else {}))
    assert_json(actual, {"error": "NEGATIVE_AMOUNT"})
    assert_json(state(store), before)


@pytest.mark.parametrize("operation", ["incr", "decr"])
def test_c05_missing_and_input_priority(operation):
    store = MiniRedis()
    assert store.set("counter:other", "keep") == {"ok": True}
    before = state(store)
    command = getattr(store, operation)
    assert_json(command("counter:missing", 1), {"error": "NOT_FOUND"})
    assert_json(command("counter:missing", -1), {"error": "NEGATIVE_AMOUNT"})
    field = "maximum" if operation == "incr" else "minimum"
    assert_json(command("counter:missing", 1, **{field: None}),
                {"error": "TYPE_MISMATCH", "field": field, "actual_type": "null"})
    assert_json(state(store), before)
    assert_json(store.get("counter:missing"), {"found": False})


@pytest.mark.parametrize("operation", ["incr", "decr"])
@pytest.mark.parametrize("key", [None, "", 1, True, [], {}, b"key", "a" * 257, "\ud800"])
def test_c06_key_before_amount(operation, key):
    store = MiniRedis()
    assert store.set("counter:other", "keep") == {"ok": True}
    before = state(store)
    assert_json(getattr(store, operation)(key, "1"), {"error": "INVALID_INPUT"})
    assert_json(state(store), before)


@pytest.mark.parametrize("operation", ["incr", "decr"])
def test_c06_amount_before_bound_and_value(operation):
    store = prepared_store("1")
    before = state(store)
    field = "maximum" if operation == "incr" else "minimum"
    actual = getattr(store, operation)("counter:one", -1, **{field: None})
    assert_json(actual, {"error": "NEGATIVE_AMOUNT"})
    actual = getattr(store, operation)("counter:one", "1")
    assert_json(actual, {"error": "TYPE_MISMATCH", "field": "amount", "actual_type": "string"})
    assert_json(state(store), before)


@pytest.mark.parametrize("operation,initial,amount,bound", [
    ("incr", 10 ** 309, 1.0, {}),
    ("decr", 10 ** 309, 1.0, {}),
    ("incr", 1e308, 1e308, {"maximum": 0}),
    ("decr", -1e308, 1e308, {"minimum": 0}),
])
def test_c07_unrepresentable_calculation_preserves_state(operation, initial, amount, bound):
    store = prepared_store(initial)
    before = state(store)
    assert_json(getattr(store, operation)("counter:one", amount, **bound), {"error": "OUT_OF_RANGE"})
    assert_json(state(store), before)


@pytest.mark.parametrize("operation", ["incr", "decr"])
@pytest.mark.parametrize("field", ["amount", "bound"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")],
                         ids=["nan", "infinity", "negative-infinity"])
def test_c07_nonfinite_input_is_not_json_number(operation, field, value):
    store = prepared_store(10)
    before = state(store)
    bound_name = "maximum" if operation == "incr" else "minimum"
    amount = value if field == "amount" else 1
    bound = {bound_name: value} if field == "bound" else {}
    assert_json(getattr(store, operation)("counter:one", amount, **bound), {"error": "OUT_OF_RANGE"})
    assert_json(state(store), before)
