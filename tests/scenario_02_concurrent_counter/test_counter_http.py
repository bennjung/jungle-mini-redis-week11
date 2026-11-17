"""S2-01, 04~08: 승인된 HTTP 계약과 실제 MiniRedis 연결 검증."""

import json

import pytest
from fastapi.testclient import TestClient

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


def reject_and_preserve(client, store, operation, body, status, expected, existing=True):
    before = state(store)
    check(client.post(f"/kv/{operation}", json=body), status, expected)
    assert_json(state(store), before)
    for key, value in before.items():
        read_twice(client, key, {"found": True, "value": value})
    if not existing:
        read_twice(client, "counter:missing", {"found": False})


@pytest.mark.parametrize("case_id,operation,initial,amount,bound,expected", [
    ("incr", "incr", 0, 1, {}, 1),
    ("decr", "decr", 10, 1, {}, 9),
    ("upper-equal", "incr", 9, 1, {"maximum": 10}, 10),
    ("lower-equal", "decr", 1, 1, {"minimum": 0}, 0),
], ids=["incr", "decr", "upper-equal", "lower-equal"])
def test_s2_01_success(connected, case_id, operation, initial, amount, bound, expected):
    client, store = connected
    prepare(client, initial)
    body = {"key": "counter:one", "amount": amount, **bound}
    check(client.post(f"/kv/{operation}", json=body), 200, {"ok": True, "value": expected})
    read_twice(client, "counter:one", {"found": True, "value": expected})
    read_twice(client, "counter:other", {"found": True, "value": "keep"})
    assert_json(state(store), {"counter:one": expected, "counter:other": "keep"})


@pytest.mark.parametrize("case_id,operation,initial,amount,bound", [
    ("upper-over", "incr", 9, 2, {"maximum": 10}),
    ("lower-under", "decr", 1, 2, {"minimum": 0}),
], ids=["upper-over", "lower-under"])
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


@pytest.mark.parametrize("operation,actual_type,value", [
    ("incr", "string", "1"),
    ("decr", "boolean", True),
])
def test_s2_05_target_type(connected, operation, actual_type, value):
    client, store = connected
    prepare(client, value)
    reject_and_preserve(client, store, operation, {"key": "counter:one", "amount": 1},
                        400, {"error": "TYPE_MISMATCH", "field": "value",
                              "actual_type": actual_type})


@pytest.mark.parametrize("operation,field,actual_type,value", [
    ("incr", "amount", "string", "1"),
    ("decr", "minimum", "null", None),
])
def test_s2_05_input_type(connected, operation, field, actual_type, value):
    client, store = connected
    prepare(client, 10)
    body = {"key": "counter:one", "amount": 1, field: value}
    reject_and_preserve(client, store, operation, body, 400,
                        {"error": "TYPE_MISMATCH", "field": field, "actual_type": actual_type})


@pytest.mark.parametrize("operation,case_id", [
    ("incr", "missing"),
    ("decr", "missing-negative"),
    ("incr", "key-and-amount"),
])
def test_s2_06_rejection_priority(connected, operation, case_id):
    client, store = connected
    prepare(client, 10)
    body = {"key": "counter:one", "amount": 1}
    missing = case_id in ("missing", "missing-negative")
    if missing:
        body["key"] = "counter:missing"
    if case_id == "missing":
        status, expected = 404, {"error": "NOT_FOUND"}
    elif case_id == "missing-negative":
        body["amount"] = -1
        status, expected = 400, {"error": "NEGATIVE_AMOUNT"}
    else:
        body.update(key="", amount="1")
        status, expected = 400, {"error": "INVALID_INPUT"}
    reject_and_preserve(client, store, operation, body, status, expected, existing=not missing)


@pytest.mark.parametrize("body", [
    [],
    {},
    {"key": "counter:one"},
    {"key": "", "amount": 1},
])
def test_s2_07_bad_body(connected, body):
    client, store = connected
    prepare(client, "before")
    before = state(store)
    response = client.post("/kv/incr", content=json.dumps(body),
                           headers={"Content-Type": "application/json"})
    check(response, 400, {"error": "INVALID_INPUT"})
    assert_json(state(store), before)


def test_s2_07_malformed_json(connected):
    client, store = connected
    prepare(client, "before")
    before = state(store)
    check(client.post("/kv/incr", content=b'{"key":',
                      headers={"Content-Type": "application/json"}),
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

    client = TestClient(create_app(BrokenStorage()), raise_server_exceptions=False)
    response = client.get("/kv", params={"key": "counter:one"})
    check(response, 500, {"error": "INTERNAL_SERVER_ERROR"})


def test_s2_15_unrepresentable_result(connected):
    client, store = connected
    prepare(client, 1e308)
    reject_and_preserve(client, store, "incr",
                        {"key": "counter:one", "amount": 1e308, "maximum": 0},
                        422, {"error": "OUT_OF_RANGE"})


def test_s2_15_unrepresentable_bound_literal(connected):
    client, store = connected
    prepare(client, 10)
    before = state(store)
    raw = '{"key":"counter:one","amount":1,"maximum":1e400}'
    check(client.post("/kv/incr", content=raw,
                      headers={"Content-Type": "application/json"}),
          422, {"error": "OUT_OF_RANGE"})
    assert_json(state(store), before)
