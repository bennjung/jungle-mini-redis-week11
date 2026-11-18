"""Mini Redis HTTP API를 사용하는 발표용 날씨 서비스."""

import json
import math
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

REDIS_URL = "http://127.0.0.1:5000"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
TTL_MS = 600_000


def create_app(client=None):
    app = FastAPI()
    http = client or httpx.Client(timeout=5.0)
    origin_calls = 0

    @app.get("/", response_class=HTMLResponse)
    def index():
        return Path(__file__).with_name("index.html").read_text(encoding="utf-8")

    @app.get("/weather")
    def weather(latitude: float = Query(ge=-90, le=90), longitude: float = Query(ge=-180, le=180)):
        nonlocal origin_calls
        if not math.isfinite(latitude) or not math.isfinite(longitude):
            raise HTTPException(status_code=422, detail="좌표가 올바르지 않습니다")
        lat = f"{latitude:.3f}"
        lon = f"{longitude:.3f}"
        # 음수 0과 양수 0은 같은 장소로 취급한다.
        lat = "0.000" if lat == "-0.000" else lat
        lon = "0.000" if lon == "-0.000" else lon
        key = f"weather:{lat}:{lon}"
        try:
            result = http.get(f"{REDIS_URL}/kv", params={"key": key})
            result.raise_for_status()
            cached = result.json()
            if cached["found"]:
                data = json.loads(cached["value"])
                print(f"[Mini Redis] 조회 성공: {key} → 재사용 | 원본 호출: {origin_calls}회", flush=True)
                return data
            print(f"[Mini Redis] 저장된 응답 없음: {key}", flush=True)
            origin_calls += 1
            print(f"[Open-Meteo] 원본 호출: {origin_calls}회 ({lat}, {lon})", flush=True)
            response = http.get(WEATHER_URL, params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,weather_code", "timezone": "Asia/Seoul",
            })
            response.raise_for_status()
            data = response.json()
            if not isinstance(data.get("current"), dict) or "temperature_2m" not in data["current"]:
                raise ValueError("날씨 원본 응답 형식 오류")
            stored = http.put(f"{REDIS_URL}/kv", json={
                "key": key, "value": json.dumps(data, ensure_ascii=False), "ttl": TTL_MS,
            })
            stored.raise_for_status()
            if stored.json() != {"ok": True}:
                raise ValueError("저장 응답 오류")
            print(f"[Mini Redis] 저장 성공: {key} | TTL: {TTL_MS}ms", flush=True)
            return data
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            print(f"[데모] 조회 또는 저장 실패: {exc}", flush=True)
            raise HTTPException(status_code=502, detail="날씨를 가져오지 못했습니다") from exc

    return app


app = create_app()
