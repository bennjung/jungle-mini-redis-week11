"""Mini Redis HTTP API를 사용하는 발표용 날씨 서비스."""

import asyncio
import json
import math
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from .events import EventRecorder

REDIS_URL = "http://127.0.0.1:5000"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
TTL_MS = 60_000
LOCK_TTL_MS = 10_000
LOCK_POLL_INTERVAL_S = 0.1
LOCK_WAIT_LIMIT_S = 10.0
MINI_COLORS = {"GET": "36", "LOCK ACQUIRE": "33", "PUT": "32"}


def mini_terminal(operation, step, message):
    text = f"[Mini Redis] {operation} | {step} {message}"
    color = MINI_COLORS.get(operation)
    if color and sys.stdout.isatty():
        text = f"\033[{color}m{text}\033[0m"
    return text


def create_app(client=None, sleep=time.sleep, monotonic=time.monotonic):
    app = FastAPI()
    http = client or httpx.Client(timeout=5.0)
    origin_calls = 0
    origin_calls_lock = threading.Lock()
    recorder = EventRecorder()

    def emit(kind, message, request_id, started_at, *, terminal, step=None, target=None, status=None, calls=None):
        recorder.publish(
            terminal=terminal,
            kind=kind,
            message=message,
            request_id=request_id,
            elapsed_ms=max(0, int((monotonic() - started_at) * 1000)),
            step=step,
            target=target,
            status=status,
            calls=calls,
        )

    def cache_lookup(key):
        result = http.get(f"{REDIS_URL}/kv", params={"key": key})
        result.raise_for_status()
        cached = result.json()
        if not cached["found"]:
            return None
        return json.loads(cached["value"])

    def cache_hit(key, data, request_id, started_at, waited=False):
        with origin_calls_lock:
            calls = origin_calls
        suffix = "(대기 후)" if waited else ""
        message = f"조회 성공{suffix} → 재사용 · 원본 호출 {calls}회"
        emit(
            "GET",
            message,
            request_id,
            started_at,
            terminal=mini_terminal(
                "GET",
                "①",
                f"조회 성공{suffix}: {key} → 재사용 | 원본 호출: {calls}회 "
                f"(요청 {request_id})",
            ),
            step="①",
            target=key,
        )
        return data

    def wait_timeout(key, request_id, started_at):
        message = f"락 대기 한도 초과 · {int(LOCK_WAIT_LIMIT_S * 1000)}ms"
        emit(
            "ERROR",
            message,
            request_id,
            started_at,
            terminal=(
                f"[데모] 락 대기 한도 초과: {key} "
                f"(요청 {request_id}, {int(LOCK_WAIT_LIMIT_S * 1000)}ms)"
            ),
            target=key,
        )
        raise HTTPException(status_code=502, detail="날씨를 가져오지 못했습니다")

    def acquire_lock(key, token):
        result = http.post(
            f"{REDIS_URL}/kv/lock/acquire",
            json={"key": key, "token": token, "ttl": LOCK_TTL_MS},
        )
        if result.status_code == 409:
            body = result.json()
            if body.get("error") != "LOCK_BUSY":
                raise ValueError("락 경합 응답 오류")
            return False, body.get("retry_after_ms")
        result.raise_for_status()
        if result.json() != {"acquired": True}:
            raise ValueError("락 획득 응답 오류")
        return True, None

    def release_lock(key, token, request_id, started_at):
        try:
            result = http.post(
                f"{REDIS_URL}/kv/lock/release",
                json={"key": key, "token": token},
            )
            result.raise_for_status()
            released = result.json().get("released")
            if not isinstance(released, bool):
                raise ValueError("락 해제 응답 오류")
            emit(
                "LOCK RELEASE",
                f"락 해제 · released={released}",
                request_id,
                started_at,
                terminal=(
                    f"  ⑤ LOCK RELEASE | [Mini Redis] 락 해제: {key} "
                    f"(요청 {request_id}, released={released})"
                ),
                step="⑤",
                target=key,
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            # 저장과 응답이 이미 끝났을 수 있으므로 해제 실패는 성공 응답을 덮어쓰지 않는다.
            emit(
                "ERROR",
                f"락 해제 실패: {exc}",
                request_id,
                started_at,
                terminal=f"[데모] 락 해제 실패: {exc} (요청 {request_id})",
                target=key,
            )

    def fetch_origin(lat, lon, request_id, started_at):
        nonlocal origin_calls
        with origin_calls_lock:
            origin_calls += 1
            calls = origin_calls
        emit(
            "OPEN-METEO",
            f"원본 호출 · 누적 {calls}회",
            request_id,
            started_at,
            terminal=(
                f"  ③ OPEN-METEO | [Open-Meteo] 원본 호출: {calls}회 "
                f"({lat}, {lon}, 요청 {request_id})"
            ),
            step="③",
            target=f"{lat}, {lon}",
            calls=calls,
        )
        result = http.get(WEATHER_URL, params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,weather_code", "timezone": "Asia/Seoul",
        })
        result.raise_for_status()
        data = result.json()
        if not isinstance(data.get("current"), dict) or "temperature_2m" not in data["current"]:
            raise ValueError("날씨 원본 응답 형식 오류")
        return data

    def store(key, data, request_id, started_at):
        stored = http.put(f"{REDIS_URL}/kv", json={
            "key": key, "value": json.dumps(data, ensure_ascii=False), "ttl": TTL_MS,
        })
        stored.raise_for_status()
        if stored.json() != {"ok": True}:
            raise ValueError("저장 응답 오류")
        emit(
            "PUT",
            f"저장 성공 · TTL {TTL_MS:,}ms",
            request_id,
            started_at,
            terminal=mini_terminal(
                "PUT",
                "④",
                f"저장 성공: {key} | TTL: {TTL_MS}ms (요청 {request_id})",
            ),
            step="④",
            target=key,
        )

    @app.get("/", response_class=HTMLResponse)
    def index():
        return Path(__file__).with_name("index.html").read_text(encoding="utf-8")

    @app.get("/monitor", response_class=HTMLResponse)
    def monitor():
        return Path(__file__).with_name("monitor.html").read_text(encoding="utf-8")

    @app.get("/monitor/events")
    async def monitor_events(request: Request):
        async def event_stream():
            queue = recorder.subscribe(asyncio.get_running_loop())
            try:
                yield (
                    "event: ready\n"
                    f"data: {json.dumps({'cursor': recorder.cursor()})}\n\n"
                )
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            finally:
                recorder.unsubscribe(queue)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/weather")
    def weather(latitude: float = Query(ge=-90, le=90), longitude: float = Query(ge=-180, le=180)):
        if not math.isfinite(latitude) or not math.isfinite(longitude):
            raise HTTPException(status_code=422, detail="좌표가 올바르지 않습니다")
        lat = f"{latitude:.3f}"
        lon = f"{longitude:.3f}"
        # 음수 0과 양수 0은 같은 장소로 취급한다.
        lat = "0.000" if lat == "-0.000" else lat
        lon = "0.000" if lon == "-0.000" else lon
        key = f"weather:{lat}:{lon}"
        token = uuid.uuid4().hex
        request_id = token[:8]
        started_at = monotonic()
        status = 200

        emit(
            "REQUEST",
            "조회 시작",
            request_id,
            started_at,
            terminal=f"\n━━━ 날씨 조회 | {key} | 요청 {request_id} ━━━",
            target=key,
        )
        try:
            data = cache_lookup(key)
            if data is not None:
                return cache_hit(key, data, request_id, started_at)

            emit(
                "GET",
                "캐시 없음",
                request_id,
                started_at,
                terminal=mini_terminal(
                    "GET", "①", f"저장된 응답 없음: {key} (요청 {request_id})"
                ),
                step="①",
                target=key,
            )
            acquired, retry_after_ms = acquire_lock(key, token)
            waited = False
            if acquired:
                emit(
                    "LOCK ACQUIRE",
                    "락 획득",
                    request_id,
                    started_at,
                    terminal=mini_terminal(
                        "LOCK ACQUIRE", "②", f"락 획득: {key} (요청 {request_id})"
                    ),
                    step="②",
                    target=key,
                )
            else:
                waited = True
                emit(
                    "LOCK ACQUIRE",
                    f"락 경합 대기 · retry_after_ms={retry_after_ms}",
                    request_id,
                    started_at,
                    terminal=mini_terminal(
                        "LOCK ACQUIRE",
                        "②",
                        f"락 경합 대기: {key} "
                        f"(요청 {request_id}, retry_after_ms={retry_after_ms})",
                    ),
                    step="②",
                    target=key,
                )
                deadline = monotonic() + LOCK_WAIT_LIMIT_S
                while True:
                    now = monotonic()
                    if now >= deadline:
                        wait_timeout(key, request_id, started_at)
                    data = cache_lookup(key)
                    now = monotonic()
                    if now >= deadline:
                        wait_timeout(key, request_id, started_at)
                    if data is not None:
                        return cache_hit(key, data, request_id, started_at, waited=True)
                    emit(
                        "GET",
                        "캐시 없음",
                        request_id,
                        started_at,
                        terminal=mini_terminal(
                            "GET", "①", f"저장된 응답 없음: {key} (요청 {request_id})"
                        ),
                        step="①",
                        target=key,
                    )
                    acquired, retry_after_ms = acquire_lock(key, token)
                    now = monotonic()
                    if acquired:
                        emit(
                            "LOCK ACQUIRE",
                            "락 획득(대기 후)",
                            request_id,
                            started_at,
                            terminal=mini_terminal(
                                "LOCK ACQUIRE",
                                "②",
                                f"락 획득(대기 후): {key} (요청 {request_id})",
                            ),
                            step="②",
                            target=key,
                        )
                        if now >= deadline:
                            release_lock(key, token, request_id, started_at)
                            wait_timeout(key, request_id, started_at)
                        break
                    if now >= deadline:
                        wait_timeout(key, request_id, started_at)
                    sleep(min(LOCK_POLL_INTERVAL_S, deadline - now))

            try:
                # 락 획득 직전에 다른 요청이 저장했을 수 있으므로 원본 호출 전에 다시 확인한다.
                data = cache_lookup(key)
                if data is not None:
                    return cache_hit(key, data, request_id, started_at, waited=waited)
                emit(
                    "GET",
                    "캐시 없음",
                    request_id,
                    started_at,
                    terminal=mini_terminal(
                        "GET", "①", f"저장된 응답 없음: {key} (요청 {request_id})"
                    ),
                    step="①",
                    target=key,
                )
                data = fetch_origin(lat, lon, request_id, started_at)
                store(key, data, request_id, started_at)
                return data
            finally:
                release_lock(key, token, request_id, started_at)
        except HTTPException as exc:
            status = exc.status_code
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            status = 502
            emit(
                "ERROR",
                f"조회 또는 저장 실패: {exc}",
                request_id,
                started_at,
                terminal=f"[데모] 조회 또는 저장 실패: {exc} (요청 {request_id})",
                target=key,
            )
            raise HTTPException(status_code=502, detail="날씨를 가져오지 못했습니다") from exc
        finally:
            emit(
                "END",
                "완료" if status == 200 else "실패",
                request_id,
                started_at,
                terminal=f"━━━ 조회 종료 | 요청 {request_id} ━━━\n",
                status=status,
            )

    return app


app = create_app()
