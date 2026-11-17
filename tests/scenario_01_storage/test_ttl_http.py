"""T1-20: TTL HTTP 표면. 실제 MiniRedis와 가짜 벽시계를 HTTP 경계에 연결한다.

대상: app.create_app(MiniRedis(clock=...))가 반환하는 FastAPI 앱.
각 행은 새 저장소에서 독립 실행하고, 사전 상태는 PUT /kv로 만든다.
실행: .venv/bin/python -m pytest tests/scenario_01_storage/test_ttl_http.py -q
"""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import create_app
from storage import MiniRedis
from tests.response_helpers import check_response

BASE_MS = 1_700_000_000_000
TTL_MS = 600_000

_ABSENT = object()


class FakeClock:
    """정수 epoch ms를 돌려주는 가짜 벽시계. advance(ms)로 시간을 진행한다."""

    def __init__(self, now=BASE_MS):
        self._now = now

    def __call__(self):
        return self._now

    def advance(self, ms):
        self._now += ms


@pytest.fixture
def api():
    """각 테스트마다 빈 저장소와 가짜 시계를 가진 HTTP 클라이언트를 만든다."""
    clock = FakeClock()
    return TestClient(create_app(MiniRedis(clock=clock))), clock


def put(client, key, value, *, ttl=_ABSENT):
    """ttl 생략과 JSON null을 구분해 PUT /kv 본문을 만든다."""
    body = {"key": key, "value": value}
    if ttl is not _ABSENT:
        body["ttl"] = ttl
    return client.put("/kv", json=body)


def get_ttl(client, key):
    return client.get("/kv/ttl", params={"key": key})


def test_put_ttl(api):
    """put-ttl: TTL과 함께 저장한다."""
    client, _ = api
    check_response(put(client, "ttl:one", "hello", ttl=TTL_MS), 200, {"ok": True})


def test_put_no_ttl(api):
    """put-no-ttl: ttl 생략은 TTL 없이 저장한다."""
    client, _ = api
    check_response(put(client, "ttl:one", "hello"), 200, {"ok": True})
    check_response(get_ttl(client, "ttl:one"), 200, {"found": True, "ttl": -1})


def test_put_null_ttl(api):
    """put-null-ttl: JSON null ttl은 생략과 같게 기존 TTL을 제거한다."""
    client, _ = api
    check_response(put(client, "ttl:one", "hello", ttl=TTL_MS), 200, {"ok": True})
    check_response(put(client, "ttl:one", "hello", ttl=None), 200, {"ok": True})
    check_response(get_ttl(client, "ttl:one"), 200, {"found": True, "ttl": -1})


def test_ttl_get(api):
    """ttl-get: 남은 TTL을 조회한다."""
    client, _ = api
    check_response(put(client, "ttl:one", "hello", ttl=TTL_MS), 200, {"ok": True})
    check_response(get_ttl(client, "ttl:one"), 200, {"found": True, "ttl": TTL_MS})


def test_ttl_missing(api):
    """ttl-missing: 없는 키는 404가 아니라 found:false다."""
    client, _ = api
    check_response(get_ttl(client, "missing"), 200, {"found": False})


def test_expire_set(api):
    """expire-set: TTL 없는 키에 TTL을 설정한다."""
    client, _ = api
    check_response(put(client, "ttl:one", "hello"), 200, {"ok": True})
    check_response(
        client.post("/kv/expire", json={"key": "ttl:one", "ttl": 60_000}),
        200,
        {"found": True, "ttl": 60_000},
    )


def test_expire_delete(api):
    """expire-delete: ttl 0은 키를 즉시 삭제한다."""
    client, _ = api
    check_response(put(client, "ttl:one", "hello", ttl=TTL_MS), 200, {"ok": True})
    check_response(
        client.post("/kv/expire", json={"key": "ttl:one", "ttl": 0}),
        200,
        {"found": True, "ttl": 0},
    )
    check_response(client.get("/kv", params={"key": "ttl:one"}), 200, {"found": False})


def test_expire_missing(api):
    """expire-missing: 없는 키는 found:false다."""
    client, _ = api
    check_response(
        client.post("/kv/expire", json={"key": "missing", "ttl": 1_000}),
        200,
        {"found": False},
    )


def test_persist(api):
    """persist: TTL만 제거하고 value는 유지한다."""
    client, _ = api
    check_response(put(client, "ttl:one", "hello", ttl=TTL_MS), 200, {"ok": True})
    check_response(
        client.post("/kv/persist", json={"key": "ttl:one"}),
        200,
        {"found": True, "persisted": True},
    )
    check_response(get_ttl(client, "ttl:one"), 200, {"found": True, "ttl": -1})


def test_persist_no_ttl(api):
    """persist-no-ttl: TTL이 없으면 persisted:false다."""
    client, _ = api
    check_response(put(client, "ttl:one", "hello"), 200, {"ok": True})
    check_response(
        client.post("/kv/persist", json={"key": "ttl:one"}),
        200,
        {"found": True, "persisted": False},
    )


def test_persist_missing(api):
    """persist-missing: 없는 키는 found:false다."""
    client, _ = api
    check_response(
        client.post("/kv/persist", json={"key": "missing"}),
        200,
        {"found": False},
    )


def test_get_hides_ttl(api):
    """get-hides-ttl: GET /kv 응답에는 TTL 필드가 없다."""
    client, _ = api
    check_response(put(client, "ttl:one", "hello", ttl=TTL_MS), 200, {"ok": True})
    body = check_response(
        client.get("/kv", params={"key": "ttl:one"}),
        200,
        {"found": True, "value": "hello"},
    )
    assert "ttl" not in body


@pytest.mark.parametrize(
    "ttl",
    [
        pytest.param(0, id="zero"),
        pytest.param("1000", id="string"),
    ],
)
def test_put_invalid_ttl_preserves_value_and_ttl(api, ttl):
    """put-invalid-ttl: 잘못된 ttl은 400이며 value와 TTL을 보존한다."""
    client, _ = api
    check_response(put(client, "ttl:one", "before", ttl=TTL_MS), 200, {"ok": True})

    check_response(
        put(client, "ttl:one", "after", ttl=ttl),
        400,
        {"error": "INVALID_INPUT"},
    )
    check_response(client.get("/kv", params={"key": "ttl:one"}), 200, {"found": True, "value": "before"})
    check_response(get_ttl(client, "ttl:one"), 200, {"found": True, "ttl": TTL_MS})


@pytest.mark.parametrize(
    "ttl",
    [
        pytest.param(_ABSENT, id="missing"),
        pytest.param(None, id="null"),
    ],
)
def test_expire_invalid_ttl_preserves_value_and_ttl(api, ttl):
    """expire-invalid-ttl: 누락·null·잘못된 타입은 400이며 상태를 보존한다."""
    client, _ = api
    check_response(put(client, "ttl:one", "before", ttl=TTL_MS), 200, {"ok": True})

    body = {"key": "ttl:one"}
    if ttl is not _ABSENT:
        body["ttl"] = ttl
    check_response(
        client.post("/kv/expire", json=body),
        400,
        {"error": "INVALID_INPUT"},
    )
    check_response(client.get("/kv", params={"key": "ttl:one"}), 200, {"found": True, "value": "before"})
    check_response(get_ttl(client, "ttl:one"), 200, {"found": True, "ttl": TTL_MS})


def test_expire_boundary(api):
    """expire-boundary: 만료 시각에 도달하면 없는 키로 응답한다."""
    client, clock = api
    check_response(put(client, "ttl:one", "hello", ttl=TTL_MS), 200, {"ok": True})
    clock.advance(TTL_MS)
    check_response(get_ttl(client, "ttl:one"), 200, {"found": False})


def test_expire_key_omitted():
    """expire-key-omitted: key 누락은 저장소 호출 없이 400이다."""
    storage = Mock()
    client = TestClient(create_app(storage))

    check_response(
        client.post("/kv/expire", json={"ttl": TTL_MS}),
        400,
        {"error": "INVALID_INPUT"},
    )
    assert storage.mock_calls == []


def test_ttl_key_omitted():
    """ttl-key-omitted: 쿼리 key 누락은 저장소 호출 없이 400이다."""
    storage = Mock()
    client = TestClient(create_app(storage))

    check_response(client.get("/kv/ttl"), 400, {"error": "INVALID_INPUT"})
    assert storage.mock_calls == []


def test_persist_key_omitted():
    """persist-key-omitted: key 누락은 저장소 호출 없이 400이다."""
    storage = Mock()
    client = TestClient(create_app(storage))

    check_response(client.post("/kv/persist", json={}), 400, {"error": "INVALID_INPUT"})
    assert storage.mock_calls == []


def test_expire_null_ttl_missing_key(api):
    """expire-null-ttl-missing-key: 키가 없으면 ttl null보다 found false가 먼저다."""
    client, _ = api

    check_response(
        client.post("/kv/expire", json={"key": "missing", "ttl": None}),
        200,
        {"found": False},
    )
