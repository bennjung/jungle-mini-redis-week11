"""현재 프로젝트(HTTP/1.1 + FastAPI + JSON) vs 최소 RESP2(asyncio TCP) + MiniRedis 실측 비교."""

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
        "p50": percentile(us, 50),
        "p95": percentile(us, 95),
        "p99": percentile(us, 99),
        "mean": statistics.fmean(us),
    }


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- 1. 최소 RESP2 서버 (동일 Python 3.14 asyncio + MiniRedis) ---
class MiniRESPProtocol(asyncio.Protocol):
    """단일 RESP2 Array-of-BulkStrings 명령을 MiniRedis로 연결하는 최소 프로토콜."""

    def __init__(self, store: MiniRedis):
        self.store = store
        self.buf = bytearray()
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        sock = transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def data_received(self, data):
        self.buf.extend(data)
        while True:
            parsed = self._try_parse_command()
            if parsed is None:
                break
            args, consumed = parsed
            del self.buf[:consumed]
            self.transport.write(self._dispatch(args))

    def _try_parse_command(self):
        buf = self.buf
        if not buf:
            return None
        if buf[0:1] != b"*":
            return None
        nl = buf.find(b"\r\n")
        if nl == -1:
            return None
        argc = int(buf[1:nl])
        pos = nl + 2
        args = []
        for _ in range(argc):
            if pos >= len(buf) or buf[pos : pos + 1] != b"$":
                return None
            nl2 = buf.find(b"\r\n", pos)
            if nl2 == -1:
                return None
            length = int(buf[pos + 1 : nl2])
            start = nl2 + 2
            end = start + length
            if len(buf) < end + 2:
                return None
            args.append(bytes(buf[start:end]))
            pos = end + 2
        return args, pos

    def _dispatch(self, args):
        cmd = args[0].upper()
        if cmd == b"GET" and len(args) == 2:
            res = self.store.get(args[1].decode("utf-8"))
            if "error" in res:
                return f"-ERR {res['error']}\r\n".encode("ascii")
            if not res["found"] or res["value"] is None:
                return b"$-1\r\n"
            val = res["value"]
            raw = val.encode("utf-8") if isinstance(val, str) else json.dumps(val, separators=(",", ":")).encode("utf-8")
            return f"${len(raw)}\r\n".encode("ascii") + raw + b"\r\n"

        if cmd == b"SET" and len(args) == 3:
            res = self.store.set(args[1].decode("utf-8"), args[2].decode("utf-8"))
            if "error" in res:
                return f"-ERR {res['error']}\r\n".encode("ascii")
            return b"+OK\r\n"

        if cmd == b"INCRBY" and len(args) == 3:
            res = self.store.incr(args[1].decode("utf-8"), int(args[2]))
            if "error" in res:
                return f"-ERR {res['error']}\r\n".encode("ascii")
            return f":{res['value']}\r\n".encode("ascii")

        return b"-ERR UNKNOWN_COMMAND\r\n"


def encode_resp_cmd(*parts: bytes) -> bytes:
    out = [f"*{len(parts)}\r\n".encode("ascii")]
    for p in parts:
        out.append(f"${len(p)}\r\n".encode("ascii"))
        out.append(p)
        out.append(b"\r\n")
    return b"".join(out)


def send_resp_raw(sock, raw_req):
    sock.sendall(raw_req)
    buf = b""
    while b"\r\n" not in buf:
        buf += sock.recv(65536)
    prefix = buf[0:1]
    if prefix in (b"+", b"-", b":"):
        return buf
    if prefix == b"$":
        nl = buf.find(b"\r\n")
        length = int(buf[1:nl])
        if length == -1:
            return None
        need = nl + 2 + length + 2
        while len(buf) < need:
            buf += sock.recv(65536)
        return buf[nl + 2 : nl + 2 + length]
    raise RuntimeError(f"unexpected resp: {buf[:20]!r}")


# --- 2. HTTP/1.1 클라이언트 helper ---
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


def send_http_raw(sock, raw_req):
    sock.sendall(raw_req)
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += sock.recv(65536)
    hdr, rest = buf.split(b"\r\n\r\n", 1)
    cl = 0
    for line in hdr.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            cl = int(line.split(b":", 1)[1].strip())
            break
    while len(rest) < cl:
        rest += sock.recv(65536)
    return rest[:cl]


def bench_resp(iterations=1000, warmup=100):
    store = MiniRedis()
    store.set("k:small", "hello")
    store.set("k:cnt", 0)
    large_str = "x" * 16384
    store.set("k:large", large_str)

    port = free_port()
    ready = threading.Event()
    stop_holder = {}

    def run_loop():
        async def main_coro():
            loop = asyncio.get_running_loop()
            srv = await loop.create_server(lambda: MiniRESPProtocol(store), "127.0.0.1", port)
            stop_event = asyncio.Event()
            stop_holder["loop"] = loop
            stop_holder["stop"] = stop_event
            ready.set()
            await stop_event.wait()
            srv.close()
            await srv.wait_closed()

        asyncio.run(main_coro())

    t = threading.Thread(target=run_loop, daemon=True)
    t.start()
    ready.wait()

    sock = socket.create_connection(("127.0.0.1", port))
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    cases = [
        ("GET small", encode_resp_cmd(b"GET", b"k:small")),
        ("SET small", encode_resp_cmd(b"SET", b"k:small", b"hello")),
        ("INCR cnt", encode_resp_cmd(b"INCRBY", b"k:cnt", b"1")),
        ("GET large (16KB str)", encode_resp_cmd(b"GET", b"k:large")),
        ("SET large (16KB str)", encode_resp_cmd(b"SET", b"k:large", large_str.encode("ascii"))),
    ]

    out = {}
    for label, req in cases:
        for _ in range(warmup):
            send_resp_raw(sock, req)
        samples = []
        for _ in range(iterations):
            t0 = time.perf_counter_ns()
            send_resp_raw(sock, req)
            t1 = time.perf_counter_ns()
            samples.append(t1 - t0)
        out[label] = summarize_us(samples)

    sock.close()
    stop_holder["loop"].call_soon_threadsafe(stop_holder["stop"].set)
    t.join(timeout=3.0)
    return out


def bench_http(iterations=1000, warmup=100):
    store = MiniRedis()
    store.set("k:small", "hello")
    store.set("k:cnt", 0)
    large_str = "x" * 16384
    store.set("k:large", large_str)

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(store), host="127.0.0.1", port=port, log_level="warning", access_log=False
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

    cases = [
        ("GET small", build_raw_http("GET", "/kv?key=k:small")),
        ("PUT small", build_raw_http("PUT", "/kv", json.dumps({"key": "k:small", "value": "hello"}).encode())),
        ("INCR cnt", build_raw_http("POST", "/kv/incr", json.dumps({"key": "k:cnt", "amount": 1}).encode())),
        ("GET large (16KB str)", build_raw_http("GET", "/kv?key=k:large")),
        ("PUT large (16KB str)", build_raw_http("PUT", "/kv", json.dumps({"key": "k:large", "value": large_str}).encode())),
    ]

    out = {}
    for label, req in cases:
        for _ in range(warmup):
            send_http_raw(sock, req)
        samples = []
        for _ in range(iterations):
            t0 = time.perf_counter_ns()
            send_http_raw(sock, req)
            t1 = time.perf_counter_ns()
            samples.append(t1 - t0)
        out[label] = summarize_us(samples)

    sock.close()
    server.should_exit = True
    t.join(timeout=5.0)
    return out


def main():
    resp = bench_resp()
    http = bench_http()

    print("\n=== 동일 Python 3.14 + 동일 MiniRedis 기준: 최소 RESP2 vs 현재 HTTP/FastAPI ===")
    pairs = [
        ("GET small", "GET small", "GET small"),
        ("SET/PUT small", "SET small", "PUT small"),
        ("INCR cnt", "INCR cnt", "INCR cnt"),
        ("GET large (16KB str)", "GET large (16KB str)", "GET large (16KB str)"),
        ("SET/PUT large (16KB str)", "SET large (16KB str)", "PUT large (16KB str)"),
    ]
    for title, r_key, h_key in pairs:
        r = resp[r_key]
        h = http[h_key]
        ratio = h["p50"] / r["p50"]
        print(
            f"  {title:25s} | RESP2 p50={r['p50']:6.1f} us (p99={r['p99']:6.1f}) "
            f"| HTTP p50={h['p50']:6.1f} us (p99={h['p99']:6.1f}) "
            f"| HTTP/RESP={ratio:4.2f}x (+{h['p50']-r['p50']:5.1f} us)"
        )


if __name__ == "__main__":
    main()
