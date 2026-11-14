"""J1-01~04: 신규 JSON 자료형 계약. 기존 숫자 거절 테스트와 분리한다."""

import json

import pytest

from storage import MiniRedis
from tests.scenario_02_concurrent_counter.contract_helpers import (
    assert_json, check, connected_client, put, state,
)


VALUES = [
    ("string", "hello"), ("empty", ""), ("number-text", "1"),
    ("null-text", "null"), ("integer", 1), ("negative", -3),
    ("real", 1.0), ("fraction", 0.25), ("true", True), ("false", False),
    ("null", None), ("array-empty", []), ("object-empty", {}),
    ("array", [1, "가", True, None]),
    ("object", {"a": 1, "b": "가", "c": False, "d": None}),
    ("nested", {"a": [1, {"b": [True, None, "가"]}]}),
]


@pytest.fixture(params=["storage", "http"])
def exchange(request):
    """같은 외부 행동을 기존 저장소 호출과 HTTP에서 각각 검증한다."""
    if request.param == "storage":
        store = MiniRedis()

        def call(method, key, expected, *, value=None, status=200):
            operation = {"PUT": store.set, "GET": store.get, "DELETE": store.delete}[method]
            actual = operation(key, value) if method == "PUT" else operation(key)
            assert_json(actual, expected)
    else:
        client, store = connected_client()

        def call(method, key, expected, *, value=None, status=200):
            if method == "PUT":
                response = client.put("/kv", json={"key": key, "value": value})
            else:
                response = client.request(method, "/kv", params={"key": key})
            check(response, status, expected)

    return call, store


@pytest.mark.parametrize("case_id,value", VALUES, ids=[item[0] for item in VALUES])
def test_j1_01_round_trip(exchange, case_id, value):
    call, _ = exchange
    call("PUT", "sample:other", {"ok": True}, value="keep")
    call("PUT", "sample:json", {"ok": True}, value=value)
    for _ in range(2):
        call("GET", "sample:json", {"found": True, "value": value})
    call("DELETE", "sample:json", {"found": True, "value": value})
    for _ in range(2):
        call("GET", "sample:json", {"found": False})
    call("DELETE", "sample:json", {"found": False})
    call("GET", "sample:other", {"found": True, "value": "keep"})


def test_j1_02_overwrite_types(exchange):
    call, _ = exchange
    call("PUT", "sample:other", {"ok": True}, value="keep")
    for value in ("1", 1, True, [1], {"a": 1}, None):
        call("PUT", "sample:json", {"ok": True}, value=value)
        for _ in range(2):
            call("GET", "sample:json", {"found": True, "value": value})
        call("GET", "sample:other", {"found": True, "value": "keep"})


BOUNDARIES = [
    ("string", lambda n: "a" * n, 65_536),
    ("array", lambda n: ["a" * n], 65_532),
    ("object", lambda n: {"a": "a" * n}, 65_528),
    ("unicode-array", lambda n: ["가" * n], 21_844),
    ("escaped-array", lambda n: ['"' * n], 32_766),
    ("object-key", lambda n: {"a" * n: 0}, 65_530),
]


@pytest.mark.parametrize("case_id,make_value,boundary", BOUNDARIES,
                         ids=[item[0] for item in BOUNDARIES])
@pytest.mark.parametrize("extra", [0, 1], ids=["boundary", "over"])
def test_j1_03_size(exchange, case_id, make_value, boundary, extra):
    call, store = exchange
    call("PUT", "sample:other", {"ok": True}, value="keep")
    call("PUT", "sample:json", {"ok": True}, value="before")
    before = state(store)
    value = make_value(boundary + extra)
    if extra:
        call("PUT", "sample:json", {"error": "INVALID_INPUT"}, value=value, status=400)
        assert_json(state(store), before)
        call("GET", "sample:json", {"found": True, "value": "before"})
        call("PUT", "sample:json", {"ok": True}, value="after")
        expected = "after"
    else:
        call("PUT", "sample:json", {"ok": True}, value=value)
        expected = value
    for _ in range(2):
        call("GET", "sample:json", {"found": True, "value": expected})
    call("GET", "sample:other", {"found": True, "value": "keep"})


def test_j1_03_request_format_does_not_count():
    client, _ = connected_client()
    value = {"a": "가" * 21_842}
    compact = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    assert len(compact.encode("utf-8")) == 65_534
    raw = json.dumps({"key": "sample:json", "value": value}, ensure_ascii=True, indent=4)
    assert len(raw.encode("utf-8")) > 65_536
    check(
        client.put("/kv", content=raw, headers={"Content-Type": "application/json"}),
        200, {"ok": True},
    )
    check(client.get("/kv", params={"key": "sample:json"}),
          200, {"found": True, "value": value})


def cyclic_list():
    value = []
    value.append(value)
    return value


@pytest.mark.parametrize("make_value", [
    lambda: float("nan"), lambda: float("inf"), lambda: float("-inf"),
    lambda: [float("nan")], lambda: {"a": float("inf")},
    lambda: (1, 2), lambda: {1: "a"}, cyclic_list,
], ids=["nan", "inf", "negative-inf", "nested-nan", "nested-inf",
        "tuple", "non-string-key", "cycle"])
def test_j1_04_invalid_python_content(make_value):
    store = MiniRedis()
    assert store.set("sample:other", "keep") == {"ok": True}
    assert store.set("sample:json", "before") == {"ok": True}
    before = state(store)
    assert_json(store.set("sample:json", make_value()), {"error": "INVALID_INPUT"})
    assert_json(state(store), before)
