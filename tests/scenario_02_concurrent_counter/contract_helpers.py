"""확장 계약 테스트의 JSON 비교와 실제 저장소 연결 도구."""

from copy import deepcopy

from fastapi.testclient import TestClient

from app import create_app
from storage import MiniRedis
from tests.scenario_01_storage.storage_helpers import snapshot
from tests.response_helpers import (
    assert_json as assert_json_value,
    check_response,
)


def assert_json(actual, expected):
    """counter·JSON 계약: int와 float를 JSON 숫자로 함께 본다."""
    assert_json_value(actual, expected, numbers="json")


def check(response, status, expected):
    return check_response(response, status, expected, numbers="json")


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
