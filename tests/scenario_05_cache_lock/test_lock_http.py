"""시나리오 05: HTTP 락 API 및 동시성 계약 테스트 (L1-01 ~ L1-12)."""

import asyncio

import httpx
from fastapi.testclient import TestClient

from app import create_app
from storage import MiniRedis
from tests.scenario_01_storage.ttl_clock_helpers import BASE_MS, FakeClock


def test_l1_http_acquire_extend_busy_release_and_no_kv_collision():
    clock = FakeClock(BASE_MS)
    app = create_app(MiniRedis(clock=clock))
    with TestClient(app) as client:
        r1 = client.post(
            "/kv/lock/acquire",
            json={"key": "weather:seoul", "token": "t1", "ttl": 5000},
        )
        assert r1.status_code == 200
        assert r1.json() == {"acquired": True}

        clock.advance(800)
        r_busy = client.post(
            "/kv/lock/acquire",
            json={"key": "weather:seoul", "token": "t2", "ttl": 5000},
        )
        assert r_busy.status_code == 409
        assert r_busy.json() == {"error": "LOCK_BUSY", "retry_after_ms": 4200}

        # L1-03: 동일 토큰 연장
        r_ext = client.post(
            "/kv/lock/acquire",
            json={"key": "weather:seoul", "token": "t1", "ttl": 5000},
        )
        assert r_ext.status_code == 200
        assert r_ext.json() == {"acquired": True}

        # L1-04: 다른 키 독립 락
        r_other = client.post(
            "/kv/lock/acquire",
            json={"key": "weather:busan", "token": "t2", "ttl": 5000},
        )
        assert r_other.status_code == 200
        assert r_other.json() == {"acquired": True}

        # L1-09: 일반 KV 연산과 비충돌
        assert (
            client.put(
                "/kv",
                json={"key": "weather:seoul", "value": {"temp": 22}, "ttl": 60_000},
            ).status_code
            == 200
        )
        assert client.delete("/kv", params={"key": "weather:seoul"}).status_code == 200

        # L1-07: 타 토큰 해제 거절
        r_rel_other = client.post(
            "/kv/lock/release", json={"key": "weather:seoul", "token": "t2"}
        )
        assert r_rel_other.status_code == 200
        assert r_rel_other.json() == {"released": False}

        # L1-06: 소유 토큰 해제 성공 후 다른 토큰 획득
        r_rel = client.post(
            "/kv/lock/release", json={"key": "weather:seoul", "token": "t1"}
        )
        assert r_rel.status_code == 200
        assert r_rel.json() == {"released": True}

        r_next = client.post(
            "/kv/lock/acquire",
            json={"key": "weather:seoul", "token": "t2", "ttl": 5000},
        )
        assert r_next.status_code == 200
        assert r_next.json() == {"acquired": True}


def test_l1_10_http_invalid_body_and_extra_fields():
    app = create_app(MiniRedis())
    with TestClient(app) as client:
        for path, bad_payload in [
            ("/kv/lock/acquire", {"key": "k", "token": "t"}),
            ("/kv/lock/acquire", {"key": "k", "token": "t", "ttl": 0}),
            ("/kv/lock/acquire", {"key": "k", "token": "t", "ttl": 1000, "extra": 1}),
            ("/kv/lock/release", {"key": "k"}),
            ("/kv/lock/release", {"key": "k", "token": "t", "extra": 1}),
        ]:
            resp = client.post(path, json=bad_payload)
            assert resp.status_code == 400
            assert resp.json() == {"error": "INVALID_INPUT"}

        bad_json = client.post(
            "/kv/lock/acquire",
            content=b"{bad",
            headers={"Content-Type": "application/json"},
        )
        assert bad_json.status_code == 400
        assert bad_json.json() == {"error": "INVALID_INPUT"}


def test_l1_12_concurrent_acquire_allows_exactly_one_winner():
    app = create_app(MiniRedis())

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            tasks = [
                client.post(
                    "/kv/lock/acquire",
                    json={"key": "weather:seoul", "token": f"t{i}", "ttl": 5000},
                )
                for i in range(20)
            ]
            return await asyncio.gather(*tasks)

    responses = asyncio.run(run())
    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == 1
    assert statuses.count(409) == 19
    for r in responses:
        if r.status_code == 409:
            body = r.json()
            assert body["error"] == "LOCK_BUSY"
            assert 1 <= body["retry_after_ms"] <= 5000
