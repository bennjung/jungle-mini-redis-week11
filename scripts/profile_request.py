"""1요청 성능 구간 분석 스크립트 (Tier 소거법 + ASGI/Storage 래퍼 계측 + cProfile)."""

import asyncio
import cProfile
import io
import json
import pstats
import socket
import statistics
import threading
import time
from pathlib import Path
import sys

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app
from storage import MiniRedis


def percentile(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    idx = (len(sorted_vals) - 1) * p / 100.0
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def summarize_us(ns_list):
    us = sorted(v / 1000.0 for v in ns_list)
    return {
        "n": len(us),
        "p50": percentile(us, 50),
        "p95": percentile(us, 95),
        "p99": percentile(us, 99),
        "mean": statistics.fmean(us),
    }


class StorageProfilerProxy:
    """MiniRedis를 감싸 메서드 실행 시간(ns)을 기록하는 무침습 프록시."""

    def __init__(self, target):
        self._target = target
        self.last_store_in_ns = 0
        self.last_store_out_ns = 0

    def _wrap(self, name):
        fn = getattr(self._target, name)

        def wrapper(*args, **kwargs):
            t0 = time.perf_counter_ns()
            res = fn(*args, **kwargs)
            t1 = time.perf_counter_ns()
            self.last_store_in_ns = t0
            self.last_store_out_ns = t1
            return res

        return wrapper

    def __getattr__(self, name):
        if name in {"set", "get", "delete", "expire", "ttl", "persist", "incr", "decr"}:
            return self._wrap(name)
        return getattr(self._target, name)


class ProfilerASGIMiddleware:
    """ASGI 경계에서 pre-storage / storage / post-storage 구간(ns)을 기록한다."""

    def __init__(self, app, proxy: StorageProfilerProxy):
        self.app = app
        self.proxy = proxy
        self.records = []
        self.enabled = False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        t_asgi_in = time.perf_counter_ns()
        t_resp_done = t_asgi_in

        async def wrapped_send(message):
            nonlocal t_resp_done
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                t_resp_done = time.perf_counter_ns()

        await self.app(scope, receive, wrapped_send)
        t_in = self.proxy.last_store_in_ns
        t_out = self.proxy.last_store_out_ns
        self.records.append(
            {
                "total_asgi_ns": t_resp_done - t_asgi_in,
                "pre_storage_ns": t_in - t_asgi_in,
                "storage_ns": t_out - t_in,
                "post_storage_ns": t_resp_done - t_out,
            }
        )


def make_large_json_payload(target_bytes=16384):
    items = []
    i = 0
    while len(json.dumps({"items": items}, separators=(",", ":")).encode("utf-8")) < target_bytes:
        items.append({"id": i, "name": f"item-{i:04d}", "active": (i % 2 == 0), "score": i * 1.5})
        i += 1
    return {"items": items}


LARGE_OBJ = make_large_json_payload(16384)
LARGE_OBJ_BYTES = len(json.dumps(LARGE_OBJ, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def run_tier1_storage(iterations=2000, warmup=200):
    print("\n=== [Tier 1] MiniRedis 직접 호출 (순수 저장소 비용) ===")
    store = MiniRedis()
    store.set("k:small", "hello")
    store.set("k:large", LARGE_OBJ)
    store.set("k:cnt", 0)

    cases = [
        ("GET small", lambda: store.get("k:small")),
        ("PUT small", lambda: store.set("k:small", "hello")),
        (f"PUT large (~{LARGE_OBJ_BYTES}B)", lambda: store.set("k:large", LARGE_OBJ)),
        ("INCR cnt", lambda: store.incr("k:cnt", 1)),
    ]

    results = {}
    for label, fn in cases:
        for _ in range(warmup):
            fn()
        samples = []
        for _ in range(iterations):
            t0 = time.perf_counter_ns()
            fn()
            t1 = time.perf_counter_ns()
            samples.append(t1 - t0)
        s = summarize_us(samples)
        results[label] = s
        print(f"  {label:24s} | p50={s['p50']:8.2f} us | p95={s['p95']:8.2f} us | p99={s['p99']:8.2f} us | mean={s['mean']:8.2f} us")
    return results


async def run_tier2_asgi(iterations=1000, warmup=100):
    print("\n=== [Tier 2] ASGI 인메모리 (FastAPI + JSON + Storage, TCP/uvicorn 제외) ===")
    raw_store = MiniRedis()
    raw_store.set("k:small", "hello")
    raw_store.set("k:cnt", 0)
    proxy = StorageProfilerProxy(raw_store)
    wrapped_app = ProfilerASGIMiddleware(create_app(proxy), proxy)

    transport = httpx.ASGITransport(app=wrapped_app)
    cases = [
        ("GET small", "GET", "/kv?key=k:small", None),
        ("PUT small", "PUT", "/kv", {"key": "k:small", "value": "hello"}),
        (f"PUT large (~{LARGE_OBJ_BYTES}B)", "PUT", "/kv", {"key": "k:large", "value": LARGE_OBJ}),
        ("INCR cnt", "POST", "/kv/incr", {"key": "k:cnt", "amount": 1}),
    ]

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for label, method, url, payload in cases:
            wrapped_app.enabled = False
            for _ in range(warmup):
                resp = await client.request(method, url, json=payload)
                assert resp.status_code == 200

            wrapped_app.records.clear()
            wrapped_app.enabled = True
            client_samples = []
            for _ in range(iterations):
                t0 = time.perf_counter_ns()
                resp = await client.request(method, url, json=payload)
                t1 = time.perf_counter_ns()
                assert resp.status_code == 200
                client_samples.append(t1 - t0)
            wrapped_app.enabled = False

            c_s = summarize_us(client_samples)
            pre_s = summarize_us([r["pre_storage_ns"] for r in wrapped_app.records])
            sto_s = summarize_us([r["storage_ns"] for r in wrapped_app.records])
            post_s = summarize_us([r["post_storage_ns"] for r in wrapped_app.records])
            tot_s = summarize_us([r["total_asgi_ns"] for r in wrapped_app.records])
            p50_tot = tot_s["p50"]
            print(
                f"  {label:24s} | client_p50={c_s['p50']:7.1f} us | asgi_p50={p50_tot:7.1f} us "
                f"[pre={pre_s['p50']:6.1f} ({pre_s['p50']/p50_tot*100:4.1f}%), "
                f"store={sto_s['p50']:6.1f} ({sto_s['p50']/p50_tot*100:4.1f}%), "
                f"post={post_s['p50']:6.1f} ({post_s['p50']/p50_tot*100:4.1f}%)]"
            )


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_tier3_uvicorn(iterations=1000, warmup=100, access_log=False):
    tag = "access_log=True" if access_log else "access_log=False"
    print(f"\n=== [Tier 3] 실제 uvicorn TCP 루프백 ({tag}, keep-alive) ===")
    raw_store = MiniRedis()
    raw_store.set("k:small", "hello")
    raw_store.set("k:cnt", 0)
    proxy = StorageProfilerProxy(raw_store)
    wrapped_app = ProfilerASGIMiddleware(create_app(proxy), proxy)

    port = _free_port()
    config = uvicorn.Config(
        wrapped_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=access_log,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 5.0
    with httpx.Client(base_url=base_url, timeout=5.0) as client:
        while time.time() < deadline:
            try:
                r = client.get("/kv?key=k:small")
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.02)

        cases = [
            ("GET small", "GET", "/kv?key=k:small", None),
            ("PUT small", "PUT", "/kv", {"key": "k:small", "value": "hello"}),
            (f"PUT large (~{LARGE_OBJ_BYTES}B)", "PUT", "/kv", {"key": "k:large", "value": LARGE_OBJ}),
            ("INCR cnt", "POST", "/kv/incr", {"key": "k:cnt", "amount": 1}),
        ]
        if access_log:
            cases = cases[:1]

        for label, method, url, payload in cases:
            wrapped_app.enabled = False
            for _ in range(warmup):
                resp = client.request(method, url, json=payload)
                assert resp.status_code == 200

            wrapped_app.records.clear()
            wrapped_app.enabled = True
            rtt_samples = []
            for _ in range(iterations):
                t0 = time.perf_counter_ns()
                resp = client.request(method, url, json=payload)
                t1 = time.perf_counter_ns()
                assert resp.status_code == 200
                rtt_samples.append(t1 - t0)
            wrapped_app.enabled = False

            rtt_s = summarize_us(rtt_samples)
            asgi_s = summarize_us([r["total_asgi_ns"] for r in wrapped_app.records])
            pre_s = summarize_us([r["pre_storage_ns"] for r in wrapped_app.records])
            sto_s = summarize_us([r["storage_ns"] for r in wrapped_app.records])
            post_s = summarize_us([r["post_storage_ns"] for r in wrapped_app.records])
            net_h11_p50 = max(0.0, rtt_s["p50"] - asgi_s["p50"])
            tot = rtt_s["p50"]
            print(
                f"  {label:24s} | RTT_p50={tot:7.1f} us (p95={rtt_s['p95']:7.1f}, p99={rtt_s['p99']:7.1f})\n"
                f"    -> TCP+uvicorn+h11={net_h11_p50:6.1f} us ({net_h11_p50/tot*100:4.1f}%) | "
                f"pre={pre_s['p50']:6.1f} us ({pre_s['p50']/tot*100:4.1f}%) | "
                f"store={sto_s['p50']:6.1f} us ({sto_s['p50']/tot*100:4.1f}%) | "
                f"post={post_s['p50']:6.1f} us ({post_s['p50']/tot*100:4.1f}%)"
            )

    server.should_exit = True
    thread.join(timeout=5.0)


async def run_cprofile_asgi(iterations=500):
    print(f"\n=== [Method C] cProfile 상위 CPU 함수 (ASGI 인메모리, N={iterations}) ===")
    raw_store = MiniRedis()
    raw_store.set("k:small", "hello")
    app = create_app(raw_store)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for label, method, url, payload in [
            ("GET small", "GET", "/kv?key=k:small", None),
            (f"PUT large (~{LARGE_OBJ_BYTES}B)", "PUT", "/kv", {"key": "k:large", "value": LARGE_OBJ}),
        ]:
            for _ in range(50):
                await client.request(method, url, json=payload)

            pr = cProfile.Profile()
            pr.enable()
            for _ in range(iterations):
                resp = await client.request(method, url, json=payload)
                assert resp.status_code == 200
            pr.disable()

            s = io.StringIO()
            ps = pstats.Stats(pr, stream=s).sort_stats("tottime")
            ps.print_stats(12)
            print(f"\n  --- {label} (top 12 by tottime) ---")
            for line in s.getvalue().splitlines()[4:19]:
                print("  " + line)


def main():
    run_tier1_storage()
    asyncio.run(run_tier2_asgi())
    run_tier3_uvicorn(access_log=False)
    run_tier3_uvicorn(access_log=True, iterations=500, warmup=50)
    asyncio.run(run_cprofile_asgi())


if __name__ == "__main__":
    main()
