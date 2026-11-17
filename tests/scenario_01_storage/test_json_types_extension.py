"""J1-01~04: 신규 JSON 자료형 계약. 기존 숫자 거절 테스트와 분리한다."""

import json

import pytest

from storage import MiniRedis
from tests.scenario_02_concurrent_counter.contract_helpers import (
    assert_json, check, connected_client, state,
)


VALUES = [
    ("string", "hello"),
    ("empty", ""),
    ("integer", 1),
    ("fraction", 0.25),
    ("true", True),
    ("null", None),
    ("nested", {"a": [1, {"b": [True, None, "가"]}]}),
]


@pytest.mark.parametrize("case_id,value", VALUES, ids=[item[0] for item in VALUES])
def test_j1_01_round_trip(case_id, value):
    store = MiniRedis()
    assert_json(store.set("sample:other", "keep"), {"ok": True})
    assert_json(store.set("sample:json", value), {"ok": True})
    assert_json(store.get("sample:json"), {"found": True, "value": value})
    assert_json(store.delete("sample:json"), {"found": True, "value": value})
    assert_json(store.get("sample:json"), {"found": False})
    assert_json(store.delete("sample:json"), {"found": False})
    assert_json(store.get("sample:other"), {"found": True, "value": "keep"})


def test_j1_01_http_round_trip():
    client, _ = connected_client()
    value = {"a": [1, {"b": [True, None, "가"]}]}
    check(client.put("/kv", json={"key": "sample:json", "value": value}), 200, {"ok": True})
    check(client.get("/kv", params={"key": "sample:json"}), 200, {"found": True, "value": value})
    check(client.delete("/kv", params={"key": "sample:json"}), 200, {"found": True, "value": value})
    check(client.get("/kv", params={"key": "sample:json"}), 200, {"found": False})


def test_j1_02_overwrite_types():
    store = MiniRedis()
    assert_json(store.set("sample:other", "keep"), {"ok": True})
    for value in ("1", 1, True, [1], {"a": 1}, None):
        assert_json(store.set("sample:json", value), {"ok": True})
        assert_json(store.get("sample:json"), {"found": True, "value": value})
        assert_json(store.get("sample:other"), {"found": True, "value": "keep"})


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
def test_j1_03_size(case_id, make_value, boundary, extra):
    store = MiniRedis()
    assert_json(store.set("sample:other", "keep"), {"ok": True})
    assert_json(store.set("sample:json", "before"), {"ok": True})
    before = state(store)
    value = make_value(boundary + extra)
    if extra:
        assert_json(store.set("sample:json", value), {"error": "INVALID_INPUT"})
        assert_json(state(store), before)
        assert_json(store.get("sample:json"), {"found": True, "value": "before"})
        # 거절 뒤 정상 저장이 다시 성공하는지 확인한다.
        assert_json(store.set("sample:json", "after"), {"ok": True})
        assert_json(store.get("sample:json"), {"found": True, "value": "after"})
    else:
        assert_json(store.set("sample:json", value), {"ok": True})
        assert_json(store.get("sample:json"), {"found": True, "value": value})
    assert_json(store.get("sample:other"), {"found": True, "value": "keep"})


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
