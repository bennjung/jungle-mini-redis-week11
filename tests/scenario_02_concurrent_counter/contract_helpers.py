"""확장 계약 테스트의 JSON 비교와 실제 저장소 연결 도구."""

from copy import deepcopy

from fastapi.testclient import TestClient

from app import create_app
from storage import MiniRedis
from tests.scenario_01_storage.storage_helpers import snapshot


def assert_json(actual, expected):
    """불리언과 숫자를 구분하고 중첩 JSON 내용과 필드 집합을 검사한다."""
    if type(expected) in (int, float):
        assert type(actual) in (int, float), (actual, expected)
        assert actual == expected
    else:
        assert type(actual) is type(expected), (actual, expected)
        if isinstance(expected, dict):
            assert actual.keys() == expected.keys(), (actual, expected)
            for key in expected:
                assert_json(actual[key], expected[key])
        elif isinstance(expected, list):
            assert len(actual) == len(expected)
            for item, wanted in zip(actual, expected):
                assert_json(item, wanted)
        else:
            assert actual == expected


def check(response, status, expected):
    assert response.status_code == status, (
        "HTTP 상태 불일치", response.status_code, status, response.text[:300],
    )
    assert response.headers["content-type"].startswith("application/json"), (
        "JSON 응답 아님", response.headers.get("content-type"),
    )
    assert_json(response.json(), expected)


def state(store):
    return deepcopy(snapshot(store))


def connected_client():
    store = MiniRedis()
    return TestClient(create_app(store)), store


def put(client, key, value):
    response = client.put("/kv", json={"key": key, "value": value})
    assert response.status_code == 200, (
        "사전 상태 준비 PUT 실패", key, response.status_code, response.json()
    )
    check(response, 200, {"ok": True})


def read_twice(client, key, expected):
    for _ in range(2):
        check(client.get("/kv", params={"key": key}), 200, expected)
