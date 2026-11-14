"""T1-19: lifespan 타이머 task. 가짜 sleep으로 tick 횟수를 제어하고 실제 100ms를 기다리지 않는다.

대상: app._timer_loop
계약: resc/scenario_01_storage/01-ttl-c-active-contract.md 실행 구조
실행: .venv/bin/python -m pytest tests/scenario_01_storage/test_ttl_timer.py -q
"""

import asyncio
import logging
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import _timer_loop, create_app
from tests.scenario_01_storage.ttl_clock_helpers import FakeClock
from tests.scenario_01_storage.ttl_state_helpers import (
    assert_ttl_invariants,
    ttl_snapshot,
)


class _StopTimer(Exception):
    """가짜 sleep이 루프를 정상 종료시키기 위해 던지는 테스트 전용 신호."""


def test_t1_19_timer_ticks_on_interval_without_requests():
    """요청이 없어도 sleep 뒤마다 tick이 실행되고, 첫 tick은 sleep 전에 없다."""
    ticks = []
    sleeps = []

    class StubStorage:
        def _maybe_active_expire(self):
            ticks.append(len(sleeps))

    async def fake_sleep(interval):
        sleeps.append(interval)
        if len(sleeps) == 3:
            raise _StopTimer()

    async def scenario():
        task = asyncio.create_task(
            _timer_loop(StubStorage(), interval=0.1, sleep=fake_sleep)
        )
        with pytest.raises(_StopTimer):
            await task

    asyncio.run(scenario())

    assert sleeps == [0.1, 0.1, 0.1]
    # sleep 1·2 뒤에만 tick이 실행됐다. 세 번째 sleep은 tick 전에 루프를 끝낸다.
    assert ticks == [1, 2]


def test_t1_19_idle_tick_expires_expired_key():
    """HTTP 요청 없이 타이머 tick만으로 만료 키가 제거된다."""
    clock = FakeClock()
    from storage import MiniRedis

    store = MiniRedis(clock=clock)
    assert store.set("idle:one", "hello", ttl=1_000) == {"ok": True}
    clock.advance(1_000)

    sleeps = []

    async def fake_sleep(interval):
        sleeps.append(interval)
        if len(sleeps) == 1:
            # 첫 sleep이 끝난 시점에는 아직 tick 전이므로 TTL 구조에 키가 있다.
            assert "idle:one" in ttl_snapshot(store)[0]
            return
        raise _StopTimer()

    async def scenario():
        task = asyncio.create_task(
            _timer_loop(store, interval=0.1, sleep=fake_sleep)
        )
        with pytest.raises(_StopTimer):
            await task

    asyncio.run(scenario())

    assert sleeps == [0.1, 0.1]
    assert store.get("idle:one") == {"found": False}
    assert ttl_snapshot(store) == ({}, [], {})
    assert_ttl_invariants(store)


def test_timer_stops_after_tick_exception(caplog):
    """tick 예외는 로그를 남기고 task를 끝낸다. 앱은 계속 동작한다."""
    ticks = []

    class FailingStorage:
        def _maybe_active_expire(self):
            ticks.append(1)
            raise RuntimeError("boom")

    async def fake_sleep(interval):
        return

    async def scenario():
        await _timer_loop(FailingStorage(), interval=0.1, sleep=fake_sleep)

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())

    assert ticks == [1]
    assert "TTL active cleanup failed" in caplog.text


def test_lifespan_starts_and_cancels_timer(monkeypatch):
    """앱 수명 동안 타이머 task를 만들고, 종료 시 취소·대기한다."""
    started = []
    cancelled = []

    async def fake_timer(storage, interval=0.1, sleep=asyncio.sleep):
        started.append(storage)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr("app._timer_loop", fake_timer)
    storage = Mock()

    with TestClient(create_app(storage)):
        assert len(started) == 1

    assert cancelled == [True]
