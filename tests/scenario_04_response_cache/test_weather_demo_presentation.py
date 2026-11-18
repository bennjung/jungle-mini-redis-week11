import sys

import httpx
from fastapi.testclient import TestClient

from app import create_app as create_storage_app
from demo.weather_app import create_app
from storage import MiniRedis


def test_cache_expires_after_one_minute_and_lock_ttl_stays_unchanged(capsys, monkeypatch):
    now = [1_000_000]
    storage = TestClient(create_storage_app(MiniRedis(clock=lambda: now[0])))
    requests = []
    origins = []

    def transport(request):
        if request.url.host == "127.0.0.1":
            requests.append(request)
            return storage.request(
                request.method, str(request.url), content=request.content,
                headers=dict(request.headers),
            )
        origins.append(request)
        return httpx.Response(200, json={"current": {"temperature_2m": 22}})

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        client = TestClient(create_app(client=http))
        params = {"latitude": 37.566, "longitude": 126.978}
        assert client.get("/weather", params=params).status_code == 200
        now[0] += 59_999
        assert client.get("/weather", params=params).status_code == 200
        assert len(origins) == 1
        now[0] += 1
        assert client.get("/weather", params=params).status_code == 200
        assert len(origins) == 2

    import json
    assert all(json.loads(r.content)["ttl"] == 60_000 for r in requests if r.method == "PUT")
    assert all(json.loads(r.content)["ttl"] == 10_000 for r in requests if r.url.path.endswith("/acquire"))
    output = capsys.readouterr().out
    assert "\033[36m[Mini Redis] GET" in output
    assert "\033[33m[Mini Redis] LOCK ACQUIRE" in output
    assert "\033[32m[Mini Redis] PUT" in output
    assert "\033[0m" in output


def test_terminal_log_shows_request_steps_and_cache_hit(capsys):
    storage = TestClient(create_storage_app(MiniRedis()))

    def transport(request):
        if request.url.host == "127.0.0.1":
            return storage.request(
                request.method, str(request.url), content=request.content,
                headers=dict(request.headers),
            )
        return httpx.Response(200, json={"current": {"temperature_2m": 22}})

    http = httpx.Client(transport=httpx.MockTransport(transport))
    client = TestClient(create_app(client=http))
    params = {"latitude": 37.566, "longitude": 126.978}
    assert client.get("/weather", params=params).status_code == 200
    first = capsys.readouterr().out
    for text in ("━━━ 날씨 조회", "①", "GET", "②", "LOCK ACQUIRE",
                 "③", "OPEN-METEO", "④", "PUT", "⑤", "LOCK RELEASE", "━━━ 조회 종료"):
        assert text in first
    assert client.get("/weather", params=params).status_code == 200
    second = capsys.readouterr().out
    assert "①" in second and "GET" in second and "재사용" in second
    assert "③" not in second and "④" not in second
