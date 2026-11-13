"""확장 계약 테스트의 JSON 비교와 실제 저장소 연결 도구."""

from copy import deepcopy

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
        "HTTP 상태 불일치", response.status_code, status, response.get_data(as_text=True)[:300],
    )
    assert response.is_json, ("JSON 응답 아님", response.content_type)
    assert_json(response.get_json(), expected)


def state(store):
    return deepcopy(snapshot(store))


def connected_client():
    store = MiniRedis()
    app = create_app(store)
    app.config["TESTING"] = True
    return app.test_client(), store


def put(client, key, value):
    response = client.put("/kv", json={"key": key, "value": value})
    assert response.status_code == 200, (
        "사전 상태 준비 PUT 실패", key, response.status_code, response.get_json()
    )
    check(response, 200, {"ok": True})


def read_twice(client, key, expected):
    for _ in range(2):
        check(client.get("/kv", query_string={"key": key}), 200, expected)
