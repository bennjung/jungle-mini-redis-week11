"""T1-21: 날씨 응답 캐시 소비자. KV HTTP API와 호출 횟수를 세는 가짜 원본을 쓴다.

/weather 엔드포인트나 별도 앱 프로세스는 만들지 않는다. 테스트 하네스가
캐시 조회 → 원본 호출 → TTL 저장 순서를 직접 수행하고, 원본 호출 수로
재사용과 재조회를 구분한다.
실행: .venv/bin/python -m pytest tests/scenario_04_response_cache/test_weather_cache.py -q
"""

import pytest
from fastapi.testclient import TestClient

from app import create_app
from storage import MiniRedis

BASE_MS = 1_700_000_000_000
TTL_MS = 600_000
TARGET_KEY = "weather:37.566:126.978"
OTHER_KEY = "weather:37.502:127.025"
ORIGIN_RESPONSE = '{"temperature":22,"observed_at":"2026-11-14T12:00:00Z"}'
OTHER_RESPONSE = '{"temperature":19,"observed_at":"2026-11-14T12:00:00Z"}'


class FakeClock:
    """정수 epoch ms를 돌려주는 가짜 벽시계. advance(ms)로 시간을 진행한다."""

    def __init__(self, now=BASE_MS):
        self._now = now

    def __call__(self):
        return self._now

    def advance(self, ms):
        self._now += ms


class FakeOrigin:
    """원본 날씨 API 대신 같은 응답을 돌려주고 호출 횟수와 키를 기록한다."""

    def __init__(self, response=ORIGIN_RESPONSE):
        self._response = response
        self.requests = []

    @property
    def calls(self):
        return len(self.requests)

    def fetch(self, key):
        self.requests.append(key)
        return self._response


@pytest.fixture
def cache():
    """새 저장소·가짜 시계·가짜 원본을 가진 KV HTTP 클라이언트를 만든다."""
    clock = FakeClock()
    client = TestClient(create_app(MiniRedis(clock=clock)))
    return client, FakeOrigin(), clock


def read_through(client, origin, key):
    """캐시에 있으면 재사용하고, 없으면 원본을 한 번 호출해 TTL과 함께 저장한다."""
    cached = client.get("/kv", params={"key": key})
    assert cached.status_code == 200
    body = cached.json()
    if body["found"]:
        return body["value"]

    response = origin.fetch(key)
    stored = client.put("/kv", json={"key": key, "value": response, "ttl": TTL_MS})
    assert stored.status_code == 200
    assert stored.json() == {"ok": True}
    return response


def test_weather_cache_reuse_expiry_and_manual_invalidation(cache):
    client, origin, clock = cache

    # 사전 상태: 대상 키는 없고, 다른 키에는 별도 응답이 저장되어 있다.
    stored_other = client.put("/kv", json={"key": OTHER_KEY, "value": OTHER_RESPONSE})
    assert stored_other.status_code == 200
    assert stored_other.json() == {"ok": True}
    assert client.get("/kv", params={"key": TARGET_KEY}).json() == {"found": False}

    # 최초 요청 → 원본 1회, TTL 600000ms와 함께 응답 저장.
    assert read_through(client, origin, TARGET_KEY) == ORIGIN_RESPONSE
    assert origin.calls == 1
    assert origin.requests == [TARGET_KEY]
    assert client.get("/kv", params={"key": TARGET_KEY}).json() == {
        "found": True,
        "value": ORIGIN_RESPONSE,
    }
    assert client.get("/kv/ttl", params={"key": TARGET_KEY}).json() == {
        "found": True,
        "ttl": TTL_MS,
    }

    # TTL 안 재요청 → 원본 1회 유지, 저장 응답 재사용.
    assert read_through(client, origin, TARGET_KEY) == ORIGIN_RESPONSE
    assert origin.calls == 1

    # 만료 시각까지 진행 후 요청 → 만료된 키는 없음으로 보이고 원본 2회, 새 응답 저장.
    clock.advance(TTL_MS)
    assert client.get("/kv", params={"key": TARGET_KEY}).json() == {"found": False}
    assert read_through(client, origin, TARGET_KEY) == ORIGIN_RESPONSE
    assert origin.calls == 2
    assert origin.requests == [TARGET_KEY, TARGET_KEY]
    assert client.get("/kv", params={"key": TARGET_KEY}).json() == {
        "found": True,
        "value": ORIGIN_RESPONSE,
    }
    assert client.get("/kv/ttl", params={"key": TARGET_KEY}).json() == {
        "found": True,
        "ttl": TTL_MS,
    }

    # 수동 DELETE 후 요청 → 원본 3회.
    deleted = client.delete("/kv", params={"key": TARGET_KEY})
    assert deleted.status_code == 200
    assert deleted.json() == {"found": True, "value": ORIGIN_RESPONSE}
    assert read_through(client, origin, TARGET_KEY) == ORIGIN_RESPONSE
    assert origin.calls == 3
    assert origin.requests == [TARGET_KEY, TARGET_KEY, TARGET_KEY]

    # 다른 키의 캐시는 영향을 받지 않는다.
    assert client.get("/kv", params={"key": OTHER_KEY}).json() == {
        "found": True,
        "value": OTHER_RESPONSE,
    }
