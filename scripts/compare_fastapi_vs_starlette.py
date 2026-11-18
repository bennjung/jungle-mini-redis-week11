"""동일 uvicorn(httptools+asyncio) + MiniRedis 조건에서 FastAPI vs Starlette vs 순수 ASGI 비교."""

import asyncio
import json
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import parse_qs

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse as StarletteJSONResponse
from starlette.routing import Route

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import CONDITION_FIELD, ERROR_STATUS, _INVALID_BODY, _timer_loop, create_app
from scripts.profile_deep_dive import (
    build_raw_http,
    free_port,
    send_http_raw_keepalive,
    summarize_us,
)
from storage import MiniRedis


def create_starlette_app(storage):
    """app.py와 동일한 8개 엔드포인트·lifespan·500 핸들러를 순수 Starlette로 구성."""

    async def _json_body(request: StarletteRequest):
        try:
            return await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _INVALID_BODY

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(_timer_loop(storage))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def internal_server_error(request, error):
        return StarletteJSONResponse({"error": "INTERNAL_SERVER_ERROR"}, status_code=500)

    async def put_kv(request: StarletteRequest):
        body = await _json_body(request)
        if body is _INVALID_BODY or not isinstance(body, dict) or "key" not in body or "value" not in body:
            return StarletteJSONResponse({"error": "INVALID_INPUT"}, status_code=400)
        result = storage.set(body["key"], body["value"], ttl=body.get("ttl"))
        status = 400 if result == {"error": "INVALID_INPUT"} else 200
        return StarletteJSONResponse(result, status_code=status)

    async def get_kv(request: StarletteRequest):
        key = request.query_params.get("key")
        if key is None:
            return StarletteJSONResponse({"error": "INVALID_INPUT"}, status_code=400)
        result = storage.get(key)
        status = 400 if result == {"error": "INVALID_INPUT"} else 200
        return StarletteJSONResponse(result, status_code=status)

    async def delete_kv(request: StarletteRequest):
        key = request.query_params.get("key")
        if key is None:
            return StarletteJSONResponse({"error": "INVALID_INPUT"}, status_code=400)
        result = storage.delete(key)
        status = 400 if result == {"error": "INVALID_INPUT"} else 200
        return StarletteJSONResponse(result, status_code=status)

    async def expire_kv(request: StarletteRequest):
        body = await _json_body(request)
        if body is _INVALID_BODY or not isinstance(body, dict) or "key" not in body or "ttl" not in body:
            return StarletteJSONResponse({"error": "INVALID_INPUT"}, status_code=400)
        result = storage.expire(body["key"], body["ttl"])
        status = 400 if result == {"error": "INVALID_INPUT"} else 200
        return StarletteJSONResponse(result, status_code=status)

    async def ttl_kv(request: StarletteRequest):
        key = request.query_params.get("key")
        if key is None:
            return StarletteJSONResponse({"error": "INVALID_INPUT"}, status_code=400)
        result = storage.ttl(key)
        status = 400 if result == {"error": "INVALID_INPUT"} else 200
        return StarletteJSONResponse(result, status_code=status)

    async def persist_kv(request: StarletteRequest):
        body = await _json_body(request)
        if body is _INVALID_BODY or not isinstance(body, dict) or "key" not in body:
            return StarletteJSONResponse({"error": "INVALID_INPUT"}, status_code=400)
        result = storage.persist(body["key"])
        status = 400 if result == {"error": "INVALID_INPUT"} else 200
        return StarletteJSONResponse(result, status_code=status)

    async def counter_kv(request: StarletteRequest, operation: str):
        body = await _json_body(request)
        condition = CONDITION_FIELD[operation]
        if (
            body is _INVALID_BODY
            or not isinstance(body, dict)
            or "key" not in body
            or "amount" not in body
            or not set(body) <= {"key", "amount", condition}
        ):
            return StarletteJSONResponse({"error": "INVALID_INPUT"}, status_code=400)
        if condition in body:
            result = getattr(storage, operation)(
                body["key"], body["amount"], **{condition: body[condition]}
            )
        else:
            result = getattr(storage, operation)(body["key"], body["amount"])
        return StarletteJSONResponse(result, status_code=ERROR_STATUS.get(result.get("error"), 200))

    async def incr_kv(request: StarletteRequest):
        return await counter_kv(request, "incr")

    async def decr_kv(request: StarletteRequest):
        return await counter_kv(request, "decr")

    routes = [
        Route("/kv", put_kv, methods=["PUT"]),
        Route("/kv", get_kv, methods=["GET"]),
        Route("/kv", delete_kv, methods=["DELETE"]),
        Route("/kv/expire", expire_kv, methods=["POST"]),
        Route("/kv/ttl", ttl_kv, methods=["GET"]),
        Route("/kv/persist", persist_kv, methods=["POST"]),
        Route("/kv/incr", incr_kv, methods=["POST"]),
        Route("/kv/decr", decr_kv, methods=["POST"]),
    ]
    return Starlette(
        routes=routes,
        lifespan=lifespan,
        exception_handlers={Exception: internal_server_error},
    )


def create_raw_asgi_app(storage):
    """비교 기준용 최소 순수 ASGI 3.0 앱."""
    timer_task = None

    async def send_json(send, payload, status=200):
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def read_json(receive):
        chunks = []
        while True:
            msg = await receive()
            chunks.append(msg.get("body", b""))
            if not msg.get("more_body", False):
                break
        try:
            return json.loads(b"".join(chunks))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _INVALID_BODY

    async def app(scope, receive, send):
        nonlocal timer_task
        typ = scope["type"]
        if typ == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    timer_task = asyncio.create_task(_timer_loop(storage))
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    if timer_task is not None:
                        timer_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await timer_task
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if typ != "http":
            return

        try:
            method = scope["method"]
            path = scope["path"]
            if path == "/kv" and method == "GET":
                qs = parse_qs(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
                keys = qs.get("key")
                if not keys:
                    await send_json(send, {"error": "INVALID_INPUT"}, 400)
                    return
                res = storage.get(keys[0])
                await send_json(send, res, 400 if res == {"error": "INVALID_INPUT"} else 200)
                return
            if path == "/kv" and method == "PUT":
                body = await read_json(receive)
                if body is _INVALID_BODY or not isinstance(body, dict) or "key" not in body or "value" not in body:
                    await send_json(send, {"error": "INVALID_INPUT"}, 400)
                    return
                res = storage.set(body["key"], body["value"], ttl=body.get("ttl"))
                await send_json(send, res, 400 if res == {"error": "INVALID_INPUT"} else 200)
                return
            if path == "/kv/incr" and method == "POST":
                body = await read_json(receive)
                if (
                    body is _INVALID_BODY
                    or not isinstance(body, dict)
                    or "key" not in body
                    or "amount" not in body
                    or not set(body) <= {"key", "amount", "maximum"}
                ):
                    await send_json(send, {"error": "INVALID_INPUT"}, 400)
                    return
                if "maximum" in body:
                    res = storage.incr(body["key"], body["amount"], maximum=body["maximum"])
                else:
                    res = storage.incr(body["key"], body["amount"])
                await send_json(send, res, ERROR_STATUS.get(res.get("error"), 200))
                return
            await send_json(send, {"detail": "Not Found"}, 404)
        except Exception:
            await send_json(send, {"error": "INTERNAL_SERVER_ERROR"}, 500)

    return app


def start_uvicorn(app_factory):
    store = MiniRedis()
    store.set("k:small", "hello")
    store.set("k:cnt", 0)
    app = app_factory(store)
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            http="httptools",
            loop="asyncio",
            log_level="warning",
            access_log=False,
        )
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    sock = None
    for _ in range(100):
        try:
            sock = socket.create_connection(("127.0.0.1", port))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            break
        except OSError:
            time.sleep(0.02)
    return server, t, sock, port


def bench_c1(app_factory, iterations=1000, warmup=100):
    server, t, sock, _ = start_uvicorn(app_factory)
    cases = [
        ("GET small", build_raw_http("GET", "/kv?key=k:small")),
        ("PUT small", build_raw_http("PUT", "/kv", json.dumps({"key": "k:small", "value": "hello"}).encode())),
        ("INCR cnt", build_raw_http("POST", "/kv/incr", json.dumps({"key": "k:cnt", "amount": 1}).encode())),
    ]
    out = {}
    for label, req in cases:
        for _ in range(warmup):
            send_http_raw_keepalive(sock, req)
        samples = []
        for _ in range(iterations):
            t0 = time.perf_counter_ns()
            send_http_raw_keepalive(sock, req)
            t1 = time.perf_counter_ns()
            samples.append(t1 - t0)
        out[label] = summarize_us(samples)
    sock.close()
    server.should_exit = True
    t.join(timeout=5.0)
    return out


def bench_c50(app_factory, concurrency=50, per_worker=100):
    server, t, sock, port = start_uvicorn(app_factory)
    sock.close()
    req = build_raw_http("GET", "/kv?key=k:small")
    barrier = threading.Barrier(concurrency)
    all_rtts = []
    lock = threading.Lock()

    def worker():
        s = socket.create_connection(("127.0.0.1", port))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        for _ in range(10):
            send_http_raw_keepalive(s, req)
        local = []
        barrier.wait()
        for _ in range(per_worker):
            t0 = time.perf_counter_ns()
            send_http_raw_keepalive(s, req)
            t1 = time.perf_counter_ns()
            local.append(t1 - t0)
        s.close()
        with lock:
            all_rtts.extend(local)

    workers = [threading.Thread(target=worker) for _ in range(concurrency)]
    t0 = time.perf_counter()
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    elapsed = time.perf_counter() - t0
    server.should_exit = True
    t.join(timeout=5.0)
    return {"rps": (concurrency * per_worker) / elapsed, "rtt": summarize_us(all_rtts)}


def main():
    targets = [
        ("FastAPI", create_app),
        ("Starlette", create_starlette_app),
        ("Raw ASGI", create_raw_asgi_app),
    ]
    c1_res = {name: bench_c1(fn) for name, fn in targets}
    c50_res = {name: bench_c50(fn) for name, fn in targets}

    print("\n=== [C=1] 단건 요청 비교 (uvicorn + httptools + asyncio) ===")
    for label in ["GET small", "PUT small", "INCR cnt"]:
        fa = c1_res["FastAPI"][label]
        st = c1_res["Starlette"][label]
        ra = c1_res["Raw ASGI"][label]
        print(
            f"  {label:10s} | FastAPI p50={fa['p50']:5.1f} us (p99={fa['p99']:5.1f}) "
            f"| Starlette p50={st['p50']:5.1f} us ({st['p50']-fa['p50']:+.1f} us, p99={st['p99']:5.1f}) "
            f"| Raw ASGI p50={ra['p50']:5.1f} us ({ra['p50']-fa['p50']:+.1f} us, p99={ra['p99']:5.1f})"
        )

    print("\n=== [C=50, N=5,000] GET small 동시 처리량·지연 ===")
    fa_rps = c50_res["FastAPI"]["rps"]
    for name, _ in targets:
        r = c50_res[name]["rtt"]
        rps = c50_res[name]["rps"]
        print(
            f"  {name:10s} | RPS={rps:8.1f}/s ({(rps/fa_rps-1)*100:+5.1f}%) "
            f"| p50={r['p50']/1000:5.2f} ms | p95={r['p95']/1000:5.2f} ms | p99={r['p99']/1000:5.2f} ms"
        )


if __name__ == "__main__":
    main()
