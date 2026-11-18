"""발표용 날씨 데모의 HTTP 동작: 실제 외부망 없이 Mini Redis HTTP API를 사용한다."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import create_app as create_storage_app
from storage import MiniRedis
from demo.weather_app import create_app


@pytest.fixture
def demo(capsys):
    storage = TestClient(create_storage_app(MiniRedis()))
    calls = []

    def transport(request):
        if request.url.host == "127.0.0.1":
            return storage.request(
                request.method, str(request.url), content=request.content,
                headers=dict(request.headers),
            )
        calls.append(request)
        return httpx.Response(200, json={"current": {"temperature_2m": 22.0}})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        yield TestClient(create_app(client=client)), storage, calls, capsys


def test_first_lookup_stores_and_second_reuses(demo):
    client, storage, calls, capsys = demo
    params = {"latitude": 37.5664, "longitude": 126.9784}
    first = client.get("/weather", params=params)
    second = client.get("/weather", params=params)
    assert first.status_code == second.status_code == 200
    assert first.json()["current"]["temperature_2m"] == 22.0
    assert second.json() == first.json()
    assert len(calls) == 1
    assert calls[0].url.params["latitude"] == "37.566"
    assert calls[0].url.params["longitude"] == "126.978"
    stored = storage.get("/kv", params={"key": "weather:37.566:126.978"}).json()
    assert json.loads(stored["value"]) == first.json()
    ttl = storage.get("/kv/ttl", params={"key": "weather:37.566:126.978"}).json()
    assert ttl["found"] and 0 < ttl["ttl"] <= 600000
    output = capsys.readouterr().out
    assert "저장 성공" in output and "재사용" in output and "원본 호출: 1회" in output


def test_distinct_locations_are_independent(demo):
    client, _, calls, _ = demo
    assert client.get("/weather", params={"latitude": 35.179, "longitude": 129.076}).status_code == 200
    assert client.get("/weather", params={"latitude": 37.566, "longitude": 126.978}).status_code == 200
    assert len(calls) == 2


def test_storage_failure_is_not_success(capsys):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as http:
        client = TestClient(create_app(client=http))
        response = client.get("/weather", params={"latitude": 37.566, "longitude": 126.978})
    assert response.status_code == 502
    assert "실패" in capsys.readouterr().out


def test_origin_failure_does_not_store():
    calls = []

    def transport(request):
        calls.append(request)
        if request.url.host == "127.0.0.1":
            return httpx.Response(200, json={"found": False})
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        client = TestClient(create_app(client=http))
        assert client.get("/weather", params={"latitude": 37.566, "longitude": 126.978}).status_code == 502
    assert not any(r.method == "PUT" for r in calls)


def test_store_failure_is_reported(capsys):
    def transport(request):
        if request.url.host != "127.0.0.1":
            return httpx.Response(200, json={"current": {"temperature_2m": 22}})
        if request.method == "GET":
            return httpx.Response(200, json={"found": False})
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        response = TestClient(create_app(client=http)).get(
            "/weather", params={"latitude": 37.566, "longitude": 126.978}
        )
    assert response.status_code == 502
    output = capsys.readouterr().out
    assert "실패" in output
    assert "저장 성공" not in output


def test_invalid_coordinates_rejected_without_external_calls(demo):
    client, _, calls, _ = demo
    assert client.get("/weather", params={"latitude": 91, "longitude": 127}).status_code == 422
    assert not calls


def test_page_has_location_controls(demo):
    client, _, _, _ = demo
    html = client.get("/").text
    for name in ("현재 위치", "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종"):
        assert name in html
