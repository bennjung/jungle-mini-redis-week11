"""현재 구현(app.py + storage.py)의 실제 HTTP 처리 속도와 TTL·삭제 동작을 보여주는 데모."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import socket
import time

import uvicorn

from app import create_app
from storage import MiniRedis


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_server(host: str, port: int) -> None:
    uvicorn.run(
        create_app(MiniRedis()),
        host=host,
        port=port,
        workers=1,
        log_level="warning",
    )


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    frac = rank - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * frac


class KeepAliveClient:
    """클라이언트 라이브러리 오버헤드를 줄인 asyncio HTTP/1.1 keep-alive 클라이언트."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port
        )
        sock = self._writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            await self._writer.wait_closed()

    async def request(
        self, method: str, path: str, body: dict | None = None
    ) -> tuple[int, dict]:
        assert self._reader is not None and self._writer is not None
        payload = (
            json.dumps(body, separators=(",", ":")).encode("utf-8")
            if body is not None
            else b""
        )
        headers = [
            f"{method} {path} HTTP/1.1",
            f"Host: {self._host}:{self._port}",
            "Connection: keep-alive",
        ]
        if body is not None:
            headers.append("Content-Type: application/json")
            headers.append(f"Content-Length: {len(payload)}")
        raw_req = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + payload
        self._writer.write(raw_req)
        await self._writer.drain()

        status_line = await self._reader.readline()
        status = int(status_line.split(b" ", 2)[1])
        content_length = 0
        while True:
            line = await self._reader.readline()
            if line in (b"\r\n", b""):
                break
            lower = line.lower()
            if lower.startswith(b"content-length:"):
                content_length = int(line.split(b":", 1)[1].strip())
        resp_bytes = await self._reader.readexactly(content_length)
        return status, json.loads(resp_bytes)


async def _wait_for_server(host: str, port: int, timeout_s: float = 10.0) -> None:
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        client = KeepAliveClient(host, port)
        try:
            await client.connect()
            status, _ = await client.request("GET", "/kv?key=__health__")
            await client.close()
            if status == 200:
                return
        except OSError:
            await asyncio.sleep(0.05)
    raise RuntimeError("Mini Redis server did not start in time")


async def _bench_http(
    host: str,
    port: int,
    *,
    name: str,
    total: int,
    concurrency: int,
    method: str,
    path: str,
    body: dict | None = None,
) -> dict:
    clients = [KeepAliveClient(host, port) for _ in range(concurrency)]
    await asyncio.gather(*(c.connect() for c in clients))

    # Warmup
    for _ in range(min(50, total)):
        await clients[0].request(method, path, body)

    per_worker = total // concurrency
    remainder = total % concurrency
    latencies: list[float] = []

    async def worker(client: KeepAliveClient, count: int) -> None:
        for _ in range(count):
            t0 = time.perf_counter()
            status, _ = await client.request(method, path, body)
            dt = (time.perf_counter() - t0) * 1000.0
            assert status == 200
            latencies.append(dt)

    start = time.perf_counter()
    await asyncio.gather(
        *(
            worker(clients[i], per_worker + (1 if i < remainder else 0))
            for i in range(concurrency)
        )
    )
    elapsed = time.perf_counter() - start
    await asyncio.gather(*(c.close() for c in clients))

    ordered = sorted(latencies)
    return {
        "name": name,
        "total": total,
        "concurrency": concurrency,
        "elapsed_s": elapsed,
        "rps": total / elapsed,
        "p50_ms": _percentile(ordered, 50),
        "p95_ms": _percentile(ordered, 95),
        "p99_ms": _percentile(ordered, 99),
    }


async def run_demo(host: str, port: int) -> tuple[list[dict], dict]:
    ctrl = KeepAliveClient(host, port)
    await ctrl.connect()

    # Setup initial keys
    assert (await ctrl.request("PUT", "/kv", {"key": "demo:item", "value": {"seat": "A1"}}))[0] == 200
    assert (await ctrl.request("PUT", "/kv", {"key": "demo:counter", "value": 0}))[0] == 200

    results = [
        await _bench_http(
            host,
            port,
            name="PUT /kv (저장+TTL 60s)",
            total=10_000,
            concurrency=50,
            method="PUT",
            path="/kv",
            body={"key": "demo:item", "value": {"seat": "A1"}, "ttl": 60_000},
        ),
        await _bench_http(
            host,
            port,
            name="GET /kv (조회 히트)",
            total=10_000,
            concurrency=50,
            method="GET",
            path="/kv?key=demo%3Aitem",
        ),
        await _bench_http(
            host,
            port,
            name="POST /kv/incr (원자적 증가)",
            total=5_000,
            concurrency=50,
            method="POST",
            path="/kv/incr",
            body={"key": "demo:counter", "amount": 1},
        ),
    ]

    # Verify counter exactness after warmup + 5000 benchmark
    _, counter_res = await ctrl.request("GET", "/kv?key=demo%3Acounter")

    # Live TTL expiration & manual DELETE check (real wall-clock 350ms)
    await ctrl.request("PUT", "/kv", {"key": "demo:ttl", "value": "temp", "ttl": 300})
    _, before_exp = await ctrl.request("GET", "/kv?key=demo%3Attl")
    await asyncio.sleep(0.35)
    _, after_exp = await ctrl.request("GET", "/kv?key=demo%3Attl")

    await ctrl.request("PUT", "/kv", {"key": "demo:del", "value": "bye"})
    _, deleted = await ctrl.request("DELETE", "/kv?key=demo%3Adel")
    _, after_del = await ctrl.request("GET", "/kv?key=demo%3Adel")

    await ctrl.close()
    checks = {
        "counter_value": counter_res["value"],
        "ttl_before": before_exp,
        "ttl_after_350ms": after_exp,
        "delete_return": deleted,
        "after_delete": after_del,
    }
    return results, checks


def main() -> None:
    store = MiniRedis()
    store.set("demo:item", {"seat": "A1"})
    t0 = time.perf_counter()
    for _ in range(10_000):
        store.get("demo:item")
    direct_elapsed = time.perf_counter() - t0

    port = _find_free_port()
    proc = multiprocessing.Process(
        target=_run_server, args=("127.0.0.1", port), daemon=True
    )
    proc.start()
    try:
        asyncio.run(_wait_for_server("127.0.0.1", port))
        http_results, checks = asyncio.run(run_demo("127.0.0.1", port))
    finally:
        proc.terminate()
        proc.join(timeout=5)

    print(f"MiniRedis Python 직접 조회 10,000건: {direct_elapsed:.4f}초 ({10_000 / direct_elapsed:,.1f} req/s)\n")
    print("| 작업 (FastAPI HTTP, C=50) | 요청 수 | 소요(초) | 처리량(req/s) | p50(ms) | p95(ms) | p99(ms) |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in http_results:
        print(
            f"| {r['name']} | {r['total']:,} | {r['elapsed_s']:.3f} | "
            f"{r['rps']:,.1f} | {r['p50_ms']:.2f} | {r['p95_ms']:.2f} | {r['p99_ms']:.2f} |"
        )
    print(f"\n실제 동작 확인:")
    print(f"- 카운터 최종값(워밍업 50 + 본 실행 5,000): {checks['counter_value']}")
    print(f"- TTL 300ms: 직후={checks['ttl_before']} -> 350ms 후={checks['ttl_after_350ms']}")
    print(f"- 수동 DELETE: 반환={checks['delete_return']} -> 재조회={checks['after_delete']}")


if __name__ == "__main__":
    main()
