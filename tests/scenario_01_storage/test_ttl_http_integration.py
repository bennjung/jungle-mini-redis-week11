"""실제 uvicorn TCP로 TTL HTTP 표면과 04 무효화 시나리오를 검증한다.

TestClient가 아닌 실제 소켓 요청을 사용하고, 가짜 시계를 서버와 공유한다.
실행: .venv/bin/python -m pytest tests/scenario_01_storage/test_ttl_http_integration.py -q
"""

import json
import time
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

import pytest
import uvicorn

from app import create_app
from storage import MiniRedis

BASE_MS = 1_700_000_000_000
TTL_MS = 600_000


class FakeClock:
    """정수 epoch ms를 돌려주는 가짜 벽시계. advance(ms)로 시간을 진행한다."""

    def __init__(self, now=BASE_MS):
        self._now = now

    def __call__(self):
        return self._now

    def advance(self, ms):
        self._now += ms


@pytest.fixture
def server():
    """가짜 시계를 공유하는 실제 uvicorn 서버를 띄우고 호출 도구를 돌려준다."""
    clock = FakeClock()
    store = MiniRedis(clock=clock)
    config = uvicorn.Config(
        create_app(store), host="127.0.0.1", port=0, log_level="error"
    )
    instance = uvicorn.Server(config)
    thread = Thread(target=instance.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5
    while not instance.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert instance.started, "uvicorn 시작 시간 초과"
    port = instance.servers[0].sockets[0].getsockname()[1]
    opener = build_opener(ProxyHandler({}))

    def call(method, path, *, params=None, body=None):
        url = f"http://127.0.0.1:{port}{path}"
        if params:
            url += "?" + urlencode(params)
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            response = opener.open(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return (
                response.status,
                response.headers.get_content_type(),
                json.loads(response.read()),
            )

    try:
        yield call, clock
    finally:
        instance.should_exit = True
        thread.join(timeout=3)
        assert not thread.is_alive()


def test_ttl_endpoints_over_real_http(server):
    """PUT ttl → GET ttl → 만료 → expire/persist가 실제 TCP에서 동작한다."""
    call, clock = server

    status, content_type, body = call(
        "PUT", "/kv", body={"key": "ttl:one", "value": "hello", "ttl": TTL_MS}
    )
    assert (status, content_type, body) == (200, "application/json", {"ok": True})
    assert call("GET", "/kv/ttl", params={"key": "ttl:one"})[2] == {
        "found": True,
        "ttl": TTL_MS,
    }
    assert call("GET", "/kv", params={"key": "ttl:one"})[2] == {
        "found": True,
        "value": "hello",
    }

    clock.advance(TTL_MS)
    assert call("GET", "/kv", params={"key": "ttl:one"})[2] == {"found": False}
    assert call("GET", "/kv/ttl", params={"key": "ttl:one"})[2] == {"found": False}

    assert call(
        "PUT", "/kv", body={"key": "ttl:two", "value": "hello"}
    )[2] == {"ok": True}
    assert call("POST", "/kv/expire", body={"key": "ttl:two", "ttl": 1_000})[2] == {
        "found": True,
        "ttl": 1_000,
    }
    assert call("GET", "/kv/ttl", params={"key": "ttl:two"})[2] == {
        "found": True,
        "ttl": 1_000,
    }
    assert call("POST", "/kv/persist", body={"key": "ttl:two"})[2] == {
        "found": True,
        "persisted": True,
    }
    assert call("GET", "/kv/ttl", params={"key": "ttl:two"})[2] == {
        "found": True,
        "ttl": -1,
    }


def test_weather_invalidation_over_real_http(server):
    """04 소비자: 재사용 → 만료 후 재조회 → 수동 DELETE 후 재조회를 실제 TCP로 확인한다."""
    call, clock = server
    key = "weather:37.566:126.978"
    origin_calls = []
    response_text = '{"temperature":22,"observed_at":"2026-11-14T12:00:00Z"}'

    def read_through():
        status, _, body = call("GET", "/kv", params={"key": key})
        assert status == 200
        if body["found"]:
            return body["value"]
        origin_calls.append(key)
        assert call(
            "PUT", "/kv", body={"key": key, "value": response_text, "ttl": TTL_MS}
        )[2] == {"ok": True}
        return response_text

    assert read_through() == response_text
    assert len(origin_calls) == 1

    assert read_through() == response_text
    assert len(origin_calls) == 1

    clock.advance(TTL_MS)
    assert read_through() == response_text
    assert len(origin_calls) == 2

    assert call("DELETE", "/kv", params={"key": key})[2]["found"] is True
    assert read_through() == response_text
    assert len(origin_calls) == 3
