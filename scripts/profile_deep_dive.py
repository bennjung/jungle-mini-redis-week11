"""1(작은 요청) · 2(큰 JSON 요청) 세부 구간 분석 스크립트."""

import asyncio
import cProfile
import io
import json
import pstats
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
import storage as storage_mod
from storage import MiniRedis

_json_content_supported = getattr(storage_mod, "_json_content_supported", None)


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


def make_large_json_payload(target_bytes):
    items = []
    i = 0
    while len(json.dumps({"items": items}, separators=(",", ":")).encode("utf-8")) < target_bytes:
        items.append(
            {"id": i, "name": f"item-{i:04d}", "active": (i % 2 == 0), "score": i * 1.5}
        )
        i += 1
    return {"items": items}


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DeepStorageProxy:
    def __init__(self, target):
        self._target = target
        self.t_store_in = 0
        self.t_store_out = 0

    def _wrap(self, name):
        fn = getattr(self._target, name)

        def wrapper(*args, **kwargs):
            self.t_store_in = time.perf_counter_ns()
            res = fn(*args, **kwargs)
            self.t_store_out = time.perf_counter_ns()
            return res

        return wrapper

    def __getattr__(self, name):
        if name in {"set", "get", "delete", "expire", "ttl", "persist", "incr", "decr"}:
            return self._wrap(name)
        return getattr(self._target, name)


class DeepASGIMiddleware:
    """receive/store/send 경계를 모두 분리해 1요청 내부 6개 구간을 기록한다."""

    def __init__(self, app, proxy: DeepStorageProxy):
        self.app = app
        self.proxy = proxy
        self.enabled = False
        self.records = []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        t_asgi_in = time.perf_counter_ns()
        t_recv_first = 0
        t_recv_last = 0
        t_send_first = 0
        t_send_last = 0

        async def wrapped_receive():
            nonlocal t_recv_first, t_recv_last
            if t_recv_first == 0:
                t_recv_first = time.perf_counter_ns()
            msg = await receive()
            t_recv_last = time.perf_counter_ns()
            return msg

        async def wrapped_send(msg):
            nonlocal t_send_first, t_send_last
            if t_send_first == 0:
                t_send_first = time.perf_counter_ns()
            await send(msg)
            if msg["type"] == "http.response.body" and not msg.get("more_body", False):
                t_send_last = time.perf_counter_ns()

        await self.app(scope, wrapped_receive, wrapped_send)

        t_si = self.proxy.t_store_in
        t_so = self.proxy.t_store_out
        if t_recv_first == 0:
            # GET 등 body를 읽지 않는 요청
            route_ns = t_si - t_asgi_in
            recv_ns = 0
            parse_ns = 0
        else:
            route_ns = t_recv_first - t_asgi_in
            recv_ns = t_recv_last - t_recv_first
            parse_ns = t_si - t_recv_last

        self.records.append(
            {
                "asgi_total": t_send_last - t_asgi_in,
                "route": route_ns,
                "recv_body": recv_ns,
                "parse_valid": parse_ns,
                "storage": t_so - t_si,
                "resp_build": t_send_first - t_so,
                "asgi_send": t_send_last - t_send_first,
            }
        )


def measure_raw_tcp_echo(iterations=1000, warmup=100):
    """순수 TCP loopback (keep-alive 소켓 send/recv 왕복) 기준선 측정."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def server_loop():
        conn, _ = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        while True:
            data = conn.recv(65536)
            if not data:
                break
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n{\"ok\":true}")
        conn.close()
        srv.close()

    t = threading.Thread(target=server_loop, daemon=True)
    t.start()

    cli = socket.create_connection(("127.0.0.1", port))
    cli.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    req = b"GET /kv?key=k:small HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"

    for _ in range(warmup):
        cli.sendall(req)
        cli.recv(4096)

    samples = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        cli.sendall(req)
        cli.recv(4096)
        t1 = time.perf_counter_ns()
        samples.append(t1 - t0)

    cli.close()
    t.join(timeout=2.0)
    return summarize_us(samples)


def send_http_raw_keepalive(sock, raw_req_bytes):
    """클라이언트 프레임워크(httpx) 없이 원시 HTTP/1.1 요청 1건을 송수신한다."""
    sock.sendall(raw_req_bytes)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            raise RuntimeError("EOF")
        buf += chunk
    hdr, rest = buf.split(b"\r\n\r\n", 1)
    cl = 0
    for line in hdr.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            cl = int(line.split(b":", 1)[1].strip())
            break
    while len(rest) < cl:
        rest += sock.recv(65536)
    return rest[:cl]


def build_raw_http(method, path, body_bytes=None):
    if body_bytes is None:
        return (
            f"{method} {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1\r\n"
            f"Connection: keep-alive\r\n\r\n"
        ).encode("ascii")
    return (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"Connection: keep-alive\r\n\r\n"
    ).encode("ascii") + body_bytes


def run_part1_server_breakdown(iterations=1000, warmup=100):
    print("\n================ [1] 작은 요청 & uvicorn 서버 내부 세부 분석 ================")
    echo_s = measure_raw_tcp_echo(iterations=iterations, warmup=warmup)
    print(f"  [기준선] 순수 TCP 루프백 echo RTT (keep-alive, TCP_NODELAY): p50={echo_s['p50']:.2f} us (p95={echo_s['p95']:.2f})")

    raw_store = MiniRedis()
    raw_store.set("k:small", "hello")
    raw_store.set("k:cnt", 0)
    large_obj = make_large_json_payload(16384)
    raw_store.set("k:large_read", large_obj)
    proxy = DeepStorageProxy(raw_store)
    mw = DeepASGIMiddleware(create_app(proxy), proxy)

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(mw, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
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

    put_small_body = json.dumps({"key": "k:small", "value": "hello"}).encode("utf-8")
    incr_body = json.dumps({"key": "k:cnt", "amount": 1}).encode("utf-8")
    put_large_body = json.dumps({"key": "k:large", "value": large_obj}, separators=(",", ":")).encode("utf-8")

    cases = [
        ("GET small", build_raw_http("GET", "/kv?key=k:small")),
        ("PUT small", build_raw_http("PUT", "/kv", put_small_body)),
        ("INCR cnt", build_raw_http("POST", "/kv/incr", incr_body)),
        (f"GET large (~{len(put_large_body)}B)", build_raw_http("GET", "/kv?key=k:large_read")),
        (f"PUT large (~{len(put_large_body)}B)", build_raw_http("PUT", "/kv", put_large_body)),
    ]

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
        uvicorn_h11_tcp = max(0.0, rtt["p50"] - tot_asgi["p50"])
        tot = rtt["p50"]

        print(f"\n  [{label}] Raw Socket RTT p50={tot:.1f} us (p95={rtt['p95']:.1f}, p99={rtt['p99']:.1f})")
        print(f"    1) TCP + uvicorn + h11 요청수신/파싱 (ASGI 밖) : {uvicorn_h11_tcp:6.1f} us ({uvicorn_h11_tcp/tot*100:5.1f}%)")
        print(f"    2) FastAPI/Starlette 라우팅 → handler/recv     : {route['p50']:6.1f} us ({route['p50']/tot*100:5.1f}%)")
        print(f"    3) ASGI receive() 바디 읽기 (h11->body)        : {recv_body['p50']:6.1f} us ({recv_body['p50']/tot*100:5.1f}%)")
        print(f"    4) app.py json.loads + dict/키 1차 검증        : {parse_valid['p50']:6.1f} us ({parse_valid['p50']/tot*100:5.1f}%)")
        print(f"    5) MiniRedis 저장소 연산                       : {storage['p50']:6.1f} us ({storage['p50']/tot*100:5.1f}%)")
        print(f"    6) JSONResponse 생성 + json.dumps 직렬화       : {resp_build['p50']:6.1f} us ({resp_build['p50']/tot*100:5.1f}%)")
        print(f"    7) uvicorn/h11 응답 쓰기 (ASGI send 내부)      : {asgi_send['p50']:6.1f} us ({asgi_send['p50']/tot*100:5.1f}%)")

    sock.close()
    server.should_exit = True
    thread.join(timeout=5.0)


def run_server_cprofile(iterations=1000):
    print(f"\n================ [1-B] 서버 전용 cProfile (uvicorn 이벤트루프 내, N={iterations}) ================")
    raw_store = MiniRedis()
    raw_store.set("k:small", "hello")
    raw_store.set("k:cnt", 0)
    app = create_app(raw_store)

    # uvicorn 내부에서 프로파일링하도록 서버 스레드를 직접 실행하고,
    # 별도 프로파일러 wrapper로 HTTP 요청 구간만 enable/disable한다.
    pr = cProfile.Profile()

    class ProfiledApp:
        def __init__(self, inner):
            self.inner = inner
            self.active = False

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and self.active:
                pr.enable()
                try:
                    await self.inner(scope, receive, send)
                finally:
                    pr.disable()
            else:
                await self.inner(scope, receive, send)

    p_app = ProfiledApp(app)
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(p_app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
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

    for label, req in [
        ("GET small (서버 ASGI 내부)", build_raw_http("GET", "/kv?key=k:small")),
        ("PUT small (서버 ASGI 내부)", build_raw_http("PUT", "/kv", json.dumps({"key": "k:small", "value": "hello"}).encode())),
    ]:
        p_app.active = False
        for _ in range(100):
            send_http_raw_keepalive(sock, req)

        pr.clear()
        p_app.active = True
        for _ in range(iterations):
            send_http_raw_keepalive(sock, req)
        p_app.active = False

        s = io.StringIO()
        pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(12)
        print(f"\n  --- {label} (top 12 by tottime) ---")
        for line in s.getvalue().splitlines()[4:19]:
            print("  " + line)

    sock.close()
    server.should_exit = True
    thread.join(timeout=5.0)


def run_part2_large_json_breakdown(iterations=500, warmup=50):
    print("\n================ [2] 큰 JSON 요청: MiniRedis.set 내부 & 크기별 스케일링 ================")
    store = MiniRedis()

    for target in [1024, 4096, 16384, 65000]:
        payload = make_large_json_payload(target)
        actual_bytes = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        )
        key = "k:scale"

        def bench(fn):
            for _ in range(warmup):
                fn()
            samples = []
            for _ in range(iterations):
                t0 = time.perf_counter_ns()
                fn()
                t1 = time.perf_counter_ns()
                samples.append(t1 - t0)
            return summarize_us(samples)

        s_total = bench(lambda: store.set(key, payload))
        s_key = bench(lambda: len(key.encode("utf-8")))
        s_check = (
            bench(lambda: _json_content_supported(payload, set()))
            if _json_content_supported is not None
            else {"p50": 0.0}
        )
        s_size = bench(
            lambda: len(
                json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")
            )
        )
        s_loads = bench(lambda b=json.dumps(payload).encode("utf-8"): json.loads(b))

        def write_only():
            now = store._now_ms()
            store._lazy_expire(key, now)
            store._data[key] = payload
            store._clear_ttl(key)

        s_write = bench(write_only)

        tot = s_total["p50"]
        print(f"\n  [Payload ~{actual_bytes:,d} B] MiniRedis.set p50={tot:7.2f} us (p95={s_total['p95']:.2f}, p99={s_total['p99']:.2f})")
        print(f"    1) key UTF-8 길이 검증                   : {s_key['p50']:7.2f} us ({s_key['p50']/tot*100:5.1f}%)")
        print(f"    2) _json_content_supported(재귀 순회)     : {s_check['p50']:7.2f} us ({s_check['p50']/tot*100:5.1f}%)")
        print(f"    3) json.dumps + encode('utf-8')(크기측정) : {s_size['p50']:7.2f} us ({s_size['p50']/tot*100:5.1f}%)")
        print(f"    4) _now_ms + _lazy_expire + dict + ttl   : {s_write['p50']:7.2f} us ({s_write['p50']/tot*100:5.1f}%)")
        print(f"    [참고] HTTP 요청 파싱 json.loads 비용    : {s_loads['p50']:7.2f} us")


def main():
    run_part1_server_breakdown()
    run_server_cprofile()
    run_part2_large_json_breakdown()


if __name__ == "__main__":
    main()
