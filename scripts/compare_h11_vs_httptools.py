"""동일 환경(Python 3.14 + asyncio + MiniRedis)에서 uvicorn http='h11' vs 'httptools' 비교 측정."""

import asyncio
import json
import socket
import statistics
import sys
import threading
import time
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app
from scripts.profile_deep_dive import (
    DeepASGIMiddleware,
    DeepStorageProxy,
    build_raw_http,
    free_port,
    make_large_json_payload,
    send_http_raw_keepalive,
    summarize_us,
)
from storage import MiniRedis


def start_server(http_impl: str):
    raw_store = MiniRedis()
    raw_store.set("k:small", "hello")
    raw_store.set("k:cnt", 0)
    large_obj = make_large_json_payload(16384)
    raw_store.set("k:large_read", large_obj)
    proxy = DeepStorageProxy(raw_store)
    mw = DeepASGIMiddleware(create_app(proxy), proxy)

    port = free_port()
    config = uvicorn.Config(
        mw,
        host="127.0.0.1",
        port=port,
        http=http_impl,
        loop="asyncio",
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    sock = None
    for _ in range(100):
        try:
            sock = socket.create_connection(("127.0.0.1", port))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            break
        except OSError:
            time.sleep(0.02)
    return server, thread, mw, sock, port, large_obj


def bench_single_c1(http_impl: str, iterations=1000, warmup=100):
    server, thread, mw, sock, _port, large_obj = start_server(http_impl)
    put_small = json.dumps({"key": "k:small", "value": "hello"}).encode("utf-8")
    incr_body = json.dumps({"key": "k:cnt", "amount": 1}).encode("utf-8")
    put_large = json.dumps({"key": "k:large", "value": large_obj}, separators=(",", ":")).encode("utf-8")

    cases = [
        ("GET small", build_raw_http("GET", "/kv?key=k:small")),
        ("PUT small", build_raw_http("PUT", "/kv", put_small)),
        ("INCR cnt", build_raw_http("POST", "/kv/incr", incr_body)),
        ("PUT large (~16KB)", build_raw_http("PUT", "/kv", put_large)),
    ]

    results = {}
    for label, raw_req in cases:
        mw.enabled = False
        for _ in range(warmup):
            send_http_raw_keepalive(sock, raw_req)

        mw.records.clear()
        mw.enabled = True
        rtts = []
        for _ in range(iterations):
            t0 = time.perf_counter_ns()
            send_http_raw_keepalive(sock, raw_req)
            t1 = time.perf_counter_ns()
            rtts.append(t1 - t0)
        mw.enabled = False

        rtt = summarize_us(rtts)
        tot_asgi = summarize_us([r["asgi_total"] for r in mw.records])
        route = summarize_us([r["route"] for r in mw.records])
        recv_body = summarize_us([r["recv_body"] for r in mw.records])
        parse_valid = summarize_us([r["parse_valid"] for r in mw.records])
        storage = summarize_us([r["storage"] for r in mw.records])
        resp_build = summarize_us([r["resp_build"] for r in mw.records])
        asgi_send = summarize_us([r["asgi_send"] for r in mw.records])
        outside_asgi = max(0.0, rtt["p50"] - tot_asgi["p50"])
        results[label] = {
            "rtt": rtt,
            "outside_asgi_p50": outside_asgi,
            "route_p50": route["p50"],
            "recv_p50": recv_body["p50"],
            "parse_p50": parse_valid["p50"],
            "storage_p50": storage["p50"],
            "resp_build_p50": resp_build["p50"],
            "asgi_send_p50": asgi_send["p50"],
        }

    sock.close()
    server.should_exit = True
    thread.join(timeout=5.0)
    return results


def bench_concurrent_c50(http_impl: str, concurrency=50, per_worker=100):
    """동시 연결 C=50, 총 5,000건(GET small) keep-alive 처리량·지연 비교."""
    server, thread, mw, sock, port, _ = start_server(http_impl)
    sock.close()
    mw.enabled = False

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
    t_start = time.perf_counter()
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    elapsed = time.perf_counter() - t_start

    server.should_exit = True
    thread.join(timeout=5.0)
    s = summarize_us(all_rtts)
    rps = (concurrency * per_worker) / elapsed
    return {"rps": rps, "elapsed": elapsed, "rtt": s}


def main():
    r_h11 = bench_single_c1("h11")
    r_ht = bench_single_c1("httptools")

    print("\n=== [C=1] 단건 요청 비교: h11 vs httptools (둘 다 asyncio 루프) ===")
    for label in r_h11:
        a = r_h11[label]
        b = r_ht[label]
        diff = b["rtt"]["p50"] - a["rtt"]["p50"]
        pct = diff / a["rtt"]["p50"] * 100.0
        print(f"\n  [{label}]")
        print(
            f"    RTT p50: h11={a['rtt']['p50']:6.1f} us -> httptools={b['rtt']['p50']:6.1f} us "
            f"({diff:+.1f} us, {pct:+.1f}%) | p99: {a['rtt']['p99']:6.1f} -> {b['rtt']['p99']:6.1f} us"
        )
        print(
            f"    1) TCP+uvicorn+parser(ASGI 밖): {a['outside_asgi_p50']:6.1f} -> {b['outside_asgi_p50']:6.1f} us"
        )
        print(
            f"    7) uvicorn 응답쓰기(ASGI send) : {a['asgi_send_p50']:6.1f} -> {b['asgi_send_p50']:6.1f} us"
        )
        print(
            f"    (참고) FastAPI route: {a['route_p50']:.1f}->{b['route_p50']:.1f} | "
            f"store: {a['storage_p50']:.1f}->{b['storage_p50']:.1f} | "
            f"resp_build: {a['resp_build_p50']:.1f}->{b['resp_build_p50']:.1f} us"
        )

    c50_h11 = bench_concurrent_c50("h11")
    c50_ht = bench_concurrent_c50("httptools")
    print("\n=== [C=50, N=5,000] GET small 동시 처리량·지연: h11 vs httptools ===")
    for name, res in [("h11", c50_h11), ("httptools", c50_ht)]:
        r = res["rtt"]
        print(
            f"  {name:10s} | RPS={res['rps']:8.1f}/s | p50={r['p50']/1000:6.2f} ms | "
            f"p95={r['p95']/1000:6.2f} ms | p99={r['p99']/1000:6.2f} ms"
        )
    print(f"  -> RPS 변화: {(c50_ht['rps']/c50_h11['rps'] - 1)*100:+.1f}% ({c50_ht['rps']/c50_h11['rps']:.2f}배)")


if __name__ == "__main__":
    main()
