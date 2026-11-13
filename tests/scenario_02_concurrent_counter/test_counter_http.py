"""S2-01, 04~08: 승인된 HTTP 계약과 실제 MiniRedis 연결 검증."""

import json

import pytest

from app import create_app
from tests.scenario_02_concurrent_counter.contract_helpers import (
    assert_json, check, connected_client, put, read_twice, state,
)


@pytest.fixture
def connected():
    return connected_client()


def prepare(client, value):
    put(client, "counter:other", "keep")
    put(client, "counter:one", value)


SUCCESS_CASES = [
    ("incr", "incr", 0, 1, {}, 1),
    ("decr", "decr", 10, 1, {}, 9),
    ("existing", "incr", 10, 2, {}, 12),
    ("negative-incr", "incr", -3, 2, {}, -1),
    ("negative-decr", "decr", -3, 2, {}, -5),
    ("zero-incr", "incr", 10, 0, {}, 10),
    ("zero-decr", "decr", 10, 0, {}, 10),
    ("fraction-incr", "incr", 0.25, 0.5, {}, 0.75),
    ("fraction-decr", "decr", 0.75, 0.5, {}, 0.25),
    ("real-amount", "incr", 1, 1.0, {}, 2),
    ("upper-equal", "incr", 9, 1, {"maximum": 10}, 10),
    ("lower-equal", "decr", 1, 1, {"minimum": 0}, 0),
    ("upper-inside", "incr", 1, 2, {"maximum": 10}, 3),
    ("lower-inside", "decr", 5, 2, {"minimum": 0}, 3),
    ("no-lower", "decr", 0, 1, {}, -1),
]


@pytest.mark.parametrize("case_id,operation,initial,amount,bound,expected", SUCCESS_CASES,
                         ids=[item[0] for item in SUCCESS_CASES])
def test_s2_01_success(connected, case_id, operation, initial, amount, bound, expected):
    client, store = connected
    prepare(client, initial)
    body = {"key": "counter:one", "amount": amount, **bound}
    check(client.post(f"/kv/{operation}", json=body), 200, {"ok": True, "value": expected})
    read_twice(client, "counter:one", {"found": True, "value": expected})
    read_twice(client, "counter:other", {"found": True, "value": "keep"})
    assert_json(state(store), {"counter:one": expected, "counter:other": "keep"})


CONDITION_CASES = [
    ("upper-over", "incr", 9, 2, {"maximum": 10}),
    ("lower-under", "decr", 1, 2, {"minimum": 0}),
    ("already-above", "incr", 12, 0, {"maximum": 10}),
    ("already-below", "decr", -2, 0, {"minimum": 0}),
    ("floating-bound", "incr", 0.1, 0.2, {"maximum": 0.3}),
]


@pytest.mark.parametrize("case_id,operation,initial,amount,bound", CONDITION_CASES,
                         ids=[item[0] for item in CONDITION_CASES])
def test_s2_04_condition_rejected(connected, case_id, operation, initial, amount, bound):
    client, store = connected
    prepare(client, initial)
    before = state(store)
    response = client.post(f"/kv/{operation}", json={
        "key": "counter:one", "amount": amount, **bound,
    })
    check(response, 409, {"error": "CONDITION_NOT_MET", "value": initial})
    assert_json(state(store), before)
    read_twice(client, "counter:one", {"found": True, "value": initial})
    read_twice(client, "counter:other", {"found": True, "value": "keep"})
    check(client.post(f"/kv/{operation}", json={"key": "counter:one", "amount": 0}),
          200, {"ok": True, "value": initial})


def test_s2_04_floating_result(connected):
    client, _ = connected
    prepare(client, 0.1)
    check(client.post("/kv/incr", json={"key": "counter:one", "amount": 0.2}),
          200, {"ok": True, "value": 0.30000000000000004})
    read_twice(client, "counter:one", {"found": True, "value": 0.30000000000000004})


NON_NUMBERS = [("string", "1"), ("boolean", True), ("null", None),
               ("array", []), ("object", {})]
TARGET_VALUES = NON_NUMBERS + [("string", item) for item in
                              ("01", "+1", "1.0", " 1 ", "", "null")] + [("boolean", False)]


def reject_and_preserve(client, store, operation, body, status, expected, existing=True):
    before = state(store)
    check(client.post(f"/kv/{operation}", json=body), status, expected)
    assert_json(state(store), before)
    for key, value in before.items():
        read_twice(client, key, {"found": True, "value": value})
    if not existing:
        read_twice(client, "counter:missing", {"found": False})


@pytest.mark.parametrize("operation", ["incr", "decr"])
@pytest.mark.parametrize("actual_type,value", TARGET_VALUES)
def test_s2_05_target_type(connected, operation, actual_type, value):
    client, store = connected
    prepare(client, value)
    reject_and_preserve(client, store, operation, {"key": "counter:one", "amount": 1},
                        400, {"error": "TYPE_MISMATCH", "field": "value",
                              "actual_type": actual_type})


@pytest.mark.parametrize("operation", ["incr", "decr"])
@pytest.mark.parametrize("field", ["amount", "bound"])
@pytest.mark.parametrize("actual_type,value", NON_NUMBERS)
def test_s2_05_input_type(connected, operation, field, actual_type, value):
    client, store = connected
    prepare(client, 10)
    field = ("maximum" if operation == "incr" else "minimum") if field == "bound" else field
    body = {"key": "counter:one", "amount": 1, field: value}
    reject_and_preserve(client, store, operation, body, 400,
                        {"error": "TYPE_MISMATCH", "field": field, "actual_type": actual_type})


@pytest.mark.parametrize("operation", ["incr", "decr"])
@pytest.mark.parametrize("case_id", ["missing", "negative", "negative-conditioned",
                                   "missing-negative", "value-and-amount", "key-and-amount",
                                   "amount-and-bound", "bound-and-missing"])
def test_s2_06_rejection_priority(connected, operation, case_id):
    client, store = connected
    prepare(client, "1" if case_id == "value-and-amount" else 10)
    body = {"key": "counter:one", "amount": 1}
    bound = "maximum" if operation == "incr" else "minimum"
    status, expected = 400, {"error": "NEGATIVE_AMOUNT"}
    missing = case_id in ("missing", "missing-negative", "bound-and-missing")
    if missing:
        body["key"] = "counter:missing"
    if case_id == "missing":
        status, expected = 404, {"error": "NOT_FOUND"}
    elif case_id in ("negative", "negative-conditioned", "missing-negative", "amount-and-bound"):
        body["amount"] = -1
        if case_id == "negative-conditioned":
            body[bound] = 20 if operation == "incr" else 0
        elif case_id == "amount-and-bound":
            body[bound] = None
    elif case_id == "value-and-amount":
        body["amount"] = "1"
        expected = {"error": "TYPE_MISMATCH", "field": "amount", "actual_type": "string"}
    elif case_id == "key-and-amount":
        body.update(key="", amount="1")
        expected = {"error": "INVALID_INPUT"}
    elif case_id == "bound-and-missing":
        body[bound] = None
        expected = {"error": "TYPE_MISMATCH", "field": bound, "actual_type": "null"}
    reject_and_preserve(client, store, operation, body, status, expected, existing=not missing)


BAD_BODIES = [[], "hello", 10, True, None, {}, {"key": "counter:one"}, {"amount": 1},
              {"key": "counter:one", "amount": 1, "extra": 0}]
BAD_BODIES += [{"key": key, "amount": 1} for key in (None, "", 1, "a" * 257)]


@pytest.mark.parametrize("operation", ["incr", "decr"])
@pytest.mark.parametrize("body", BAD_BODIES)
def test_s2_07_bad_body(connected, operation, body):
    client, store = connected
    prepare(client, "before")
    # json=None 생략이 아니라 실제 JSON null을 전송한다.
    before = state(store)
    response = client.post(f"/kv/{operation}", data=json.dumps(body),
                           content_type="application/json")
    check(response, 400, {"error": "INVALID_INPUT"})
    assert_json(state(store), before)
    read_twice(client, "counter:one", {"found": True, "value": "before"})
    read_twice(client, "counter:other", {"found": True, "value": "keep"})


@pytest.mark.parametrize("operation", ["incr", "decr"])
def test_s2_07_malformed_json(connected, operation):
    client, store = connected
    prepare(client, "before")
    before = state(store)
    check(client.post(f"/kv/{operation}", data=b'{"key":', content_type="application/json"),
          400, {"error": "INVALID_INPUT"})
    assert_json(state(store), before)


@pytest.mark.parametrize("operation", ["incr", "decr"])
def test_s2_07_wrong_direction(connected, operation):
    client, store = connected
    prepare(client, "before")
    field, value = ("minimum", 0) if operation == "incr" else ("maximum", 20)
    reject_and_preserve(client, store, operation,
                        {"key": "counter:one", "amount": 1, field: value},
                        400, {"error": "INVALID_INPUT"})


def test_s2_08_internal_error_is_json_not_input_error():
    class BrokenStorage:
        def get(self, key):
            raise RuntimeError("private detail")

    app = create_app(BrokenStorage())
    app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
    response = app.test_client().get("/kv", query_string={"key": "counter:one"})
    check(response, 500, {"error": "INTERNAL_SERVER_ERROR"})


@pytest.mark.parametrize("operation,initial,amount,bound", [
    ("incr", 10 ** 309, 1.0, {}),
    ("decr", 10 ** 309, 1.0, {}),
    ("incr", 1e308, 1e308, {"maximum": 0}),
    ("decr", -1e308, 1e308, {"minimum": 0}),
])
def test_s2_15_unrepresentable_result(connected, operation, initial, amount, bound):
    client, store = connected
    prepare(client, initial)
    reject_and_preserve(client, store, operation,
                        {"key": "counter:one", "amount": amount, **bound},
                        422, {"error": "OUT_OF_RANGE"})


@pytest.mark.parametrize("operation", ["incr", "decr"])
def test_s2_15_unrepresentable_bound_literal(connected, operation):
    client, store = connected
    prepare(client, 10)
    before = state(store)
    field = "maximum" if operation == "incr" else "minimum"
    # 1e400은 JSON 숫자 표기지만 현재 Python float로 유한하게 표현할 수 없다.
    raw = '{"key":"counter:one","amount":1,"' + field + '":1e400}'
    check(client.post(f"/kv/{operation}", data=raw, content_type="application/json"),
          422, {"error": "OUT_OF_RANGE"})
    assert_json(state(store), before)
