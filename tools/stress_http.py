"""장시간 HTTP soak: 실제 uvicorn 서버에 랜덤 요청을 보내고 지표를 기록한다.

서버는 별도 subprocess로 실행하고, 이 프로세스는 클라이언트 역할만 한다.
기본값은 짧은 스모크용이다. 본 실행은 --duration 30m --concurrency 20 --rate 100.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import socket
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.stress.stress_common import (  # noqa: E402
    INVALID_RATIO,
    OPERATION_WEIGHTS,
    cache_keys,
    choose_http_key,
    counter_keys,
    percentile,
    plain_keys,
    random_amount,
    random_bound,
    random_ttl,
    random_value,
    weighted_choice,
)
STABLE_KEY = "oracle:http:stable"
STABLE_NUM = "oracle:http:counter"
COUNTER_ERRORS = {
    "INVALID_INPUT",
    "TYPE_MISMATCH",
    "NEGATIVE_AMOUNT",
    "NOT_FOUND",
    "OUT_OF_RANGE",
    "CONDITION_NOT_MET",
}
INVALID_ERRORS = {
    "INVALID_INPUT",
    "TYPE_MISMATCH",
    "NEGATIVE_AMOUNT",
    "OUT_OF_RANGE",
}


def parse_duration(text):
    """2m, 30m, 2h, 10s, 90 같은 기간 문자열을 초로 바꾼다."""
    text = text.strip().lower()
    if not text:
        raise argparse.ArgumentTypeError("duration이 비었습니다")
    unit = text[-1]
    if unit in {"s", "m", "h"}:
        number = text[:-1]
        factor = {"s": 1, "m": 60, "h": 3600}[unit]
    else:
        number = text
        factor = 1
    try:
        value = float(number) * factor
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"잘못된 duration: {text}") from error
    if value <= 0:
        raise argparse.ArgumentTypeError("duration은 0보다 커야 합니다")
    return value


def free_port():
    """로컬에서 사용 가능한 TCP 포트를 하나 고른다."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_valid_request(rng, operation, keyspace, ttl_ratio):
    """정상 요청 하나를 만든다. 연산별 hot 키 풀을 사용한다."""
    key = choose_http_key(rng, operation, keyspace)
    if operation == "get":
        return {
            "invalid": False,
            "op": "get",
            "method": "GET",
            "path": "/kv",
            "params": {"key": key},
        }
    if operation == "ttl":
        return {
            "invalid": False,
            "op": "ttl",
            "method": "GET",
            "path": "/kv/ttl",
            "params": {"key": key},
        }
    if operation == "set":
        body = {"key": key, "value": random_value(rng)}
        if rng.random() < ttl_ratio:
            body["ttl"] = random_ttl(rng)
        return {
            "invalid": False,
            "op": "set",
            "method": "PUT",
            "path": "/kv",
            "json": body,
        }
    if operation == "delete":
        return {
            "invalid": False,
            "op": "delete",
            "method": "DELETE",
            "path": "/kv",
            "params": {"key": key},
        }
    if operation == "expire":
        return {
            "invalid": False,
            "op": "expire",
            "method": "POST",
            "path": "/kv/expire",
            "json": {"key": key, "ttl": rng.choice([-1, 0, 1_000, 3_000, 10_000])},
        }
    if operation == "persist":
        return {
            "invalid": False,
            "op": "persist",
            "method": "POST",
            "path": "/kv/persist",
            "json": {"key": key},
        }

    amount = random_amount(rng)
    body = {"key": key, "amount": amount}
    if rng.random() < 0.2:
        body["maximum" if operation == "incr" else "minimum"] = random_bound(
            rng, 1 if operation == "incr" else -1
        )
    return {
        "invalid": False,
        "op": operation,
        "method": "POST",
        "path": "/kv/incr" if operation == "incr" else "/kv/decr",
        "json": body,
    }


def build_invalid_request(rng):
    """HTTP 경계에서 거절되어야 하는 요청 하나를 만든다."""
    choice = rng.randrange(19)
    if choice == 0:
        return {
            "invalid": True,
            "op": "invalid-json",
            "method": "PUT",
            "path": "/kv",
            "content": b'{"key":',
            "headers": {"content-type": "application/json"},
        }
    if choice == 1:
        return {"invalid": True, "op": "invalid-body", "method": "PUT", "path": "/kv", "json": []}
    if choice == 2:
        return {"invalid": True, "op": "invalid-body", "method": "PUT", "path": "/kv", "json": "hello"}
    if choice == 3:
        return {
            "invalid": True,
            "op": "put-missing-key",
            "method": "PUT",
            "path": "/kv",
            "json": {"value": "v"},
        }
    if choice == 4:
        return {
            "invalid": True,
            "op": "put-missing-value",
            "method": "PUT",
            "path": "/kv",
            "json": {"key": "bad:missing-value"},
        }
    if choice == 5:
        return {"invalid": True, "op": "put-empty", "method": "PUT", "path": "/kv", "json": {}}
    if choice == 6:
        return {
            "invalid": True,
            "op": "put-null-key",
            "method": "PUT",
            "path": "/kv",
            "json": {"key": None, "value": "v"},
        }
    if choice == 7:
        return {
            "invalid": True,
            "op": "put-empty-key",
            "method": "PUT",
            "path": "/kv",
            "json": {"key": "", "value": "v"},
        }
    if choice == 8:
        return {
            "invalid": True,
            "op": "put-long-key",
            "method": "PUT",
            "path": "/kv",
            "json": {"key": "a" * 257, "value": "v"},
        }
    if choice == 9:
        return {
            "invalid": True,
            "op": "put-zero-ttl",
            "method": "PUT",
            "path": "/kv",
            "json": {"key": STABLE_KEY, "value": "v", "ttl": 0},
        }
    if choice == 10:
        return {
            "invalid": True,
            "op": "put-float-ttl",
            "method": "PUT",
            "path": "/kv",
            "json": {"key": STABLE_KEY, "value": "v", "ttl": 1.5},
        }
    if choice == 11:
        return {
            "invalid": True,
            "op": "expire-missing-ttl",
            "method": "POST",
            "path": "/kv/expire",
            "json": {"key": STABLE_KEY},
        }
    if choice == 12:
        return {
            "invalid": True,
            "op": "expire-null-ttl",
            "method": "POST",
            "path": "/kv/expire",
            "json": {"key": STABLE_KEY, "ttl": None},
        }
    if choice == 13:
        return {
            "invalid": True,
            "op": "persist-missing-key",
            "method": "POST",
            "path": "/kv/persist",
            "json": {},
        }
    if choice == 14:
        return {"invalid": True, "op": "get-missing-key", "method": "GET", "path": "/kv"}
    if choice == 15:
        return {"invalid": True, "op": "ttl-missing-key", "method": "GET", "path": "/kv/ttl"}
    if choice == 16:
        return {"invalid": True, "op": "delete-missing-key", "method": "DELETE", "path": "/kv"}
    if choice == 17:
        return {
            "invalid": True,
            "op": "incr-negative",
            "method": "POST",
            "path": "/kv/incr",
            "json": {"key": STABLE_NUM, "amount": -1},
        }
    return {
        "invalid": True,
        "op": "incr-bad-amount",
        "method": "POST",
        "path": "/kv/incr",
        "json": {"key": STABLE_NUM, "amount": "1"},
    }


def _validate_found_body(body, extra_field, extra_type):
    if not isinstance(body, dict) or not isinstance(body.get("found"), bool):
        return "found 응답 형식 오류"
    if body["found"]:
        if extra_field not in body or not isinstance(body[extra_field], extra_type):
            return f"found=true인데 {extra_field}가 없거나 타입이 다름"
    elif extra_field in body:
        return f"found=false인데 {extra_field}가 포함됨"
    return None


def validate_response(spec, status, body):
    """요청 하나의 상태 코드와 JSON 본문을 계약과 대조한다."""
    if spec["invalid"]:
        if status != 400:
            return "unexpected", f"invalid 요청이 {status}"
        if not isinstance(body, dict) or body.get("error") not in INVALID_ERRORS:
            return "unexpected", f"invalid 오류 본문: {body!r}"
        return "expected_4xx", None

    operation = spec["op"]
    if operation == "set":
        if status == 200 and body == {"ok": True}:
            return "ok", None
        return "unexpected", f"set 응답: {status} {body!r}"

    if operation in {"get", "delete"}:
        if status != 200:
            return "unexpected", f"{operation} 상태: {status}"
        problem = _validate_found_body(body, "value", object)
        return ("unexpected", problem) if problem else ("ok", None)

    if operation == "expire":
        if status != 200:
            return "unexpected", f"expire 상태: {status}"
        problem = _validate_found_body(body, "ttl", int)
        if problem:
            return "unexpected", problem
        if body["found"] and body["ttl"] < 0:
            return "unexpected", "expire ttl이 음수"
        return "ok", None

    if operation == "ttl":
        if status != 200:
            return "unexpected", f"ttl 상태: {status}"
        problem = _validate_found_body(body, "ttl", int)
        return ("unexpected", problem) if problem else ("ok", None)

    if operation == "persist":
        if status != 200:
            return "unexpected", f"persist 상태: {status}"
        problem = _validate_found_body(body, "persisted", bool)
        return ("unexpected", problem) if problem else ("ok", None)

    # incr/decr는 4xx가 정상일 수 있다. 없는 키·비숫자·조건 불충족은 예상된 결과다.
    if status == 200:
        if (
            isinstance(body, dict)
            and body.get("ok") is True
            and isinstance(body.get("value"), (int, float))
            and not isinstance(body.get("value"), bool)
        ):
            return "ok", None
        return "unexpected", f"{operation} 성공 응답: {body!r}"
    if status in {400, 404, 409, 422}:
        if isinstance(body, dict) and body.get("error") in COUNTER_ERRORS:
            return "expected_4xx", None
        return "unexpected", f"{operation} 오류 본문: {body!r}"
    return "unexpected", f"{operation} 상태: {status}"


class RateLimiter:
    """전체 요청 속도를 제한한다. rate<=0이면 제한하지 않는다."""

    def __init__(self, rate):
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        if self.interval <= 0:
            return
        async with self.lock:
            now = time.monotonic()
            if self.next_at < now:
                self.next_at = now
            delay = self.next_at - now
            self.next_at += self.interval
        if delay > 0:
            await asyncio.sleep(delay)


async def _send(client, spec):
    kwargs = {
        "method": spec["method"],
        "url": spec["path"],
        "params": spec.get("params"),
        "headers": spec.get("headers"),
    }
    if "json" in spec:
        kwargs["json"] = spec["json"]
    if "content" in spec:
        kwargs["content"] = spec["content"]
    return await client.request(**kwargs)


async def worker(client, rng, deadline, limiter, keyspace, ttl_ratio, stats, records):
    while True:
        if time.monotonic() >= deadline:
            return
        await limiter.wait()
        if time.monotonic() >= deadline:
            return

        invalid = rng.random() < INVALID_RATIO
        spec = build_invalid_request(rng) if invalid else build_valid_request(
            rng, weighted_choice(rng, OPERATION_WEIGHTS), keyspace, ttl_ratio
        )
        stats["total"] += 1
        stats["by_op"][spec["op"]] += 1
        started = time.perf_counter()
        try:
            response = await _send(client, spec)
        except httpx.TimeoutException as error:
            stats["timeouts"] += 1
            stats["unexpected"] += 1
            records.append(
                {
                    "op": spec["op"],
                    "invalid": invalid,
                    "status": None,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "outcome": "timeout",
                    "detail": repr(error),
                }
            )
            continue
        except httpx.TransportError as error:
            stats["transport_errors"] += 1
            stats["unexpected"] += 1
            records.append(
                {
                    "op": spec["op"],
                    "invalid": invalid,
                    "status": None,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "outcome": "transport_error",
                    "detail": repr(error),
                }
            )
            continue

        latency_ms = (time.perf_counter() - started) * 1000
        stats["latencies"].append(latency_ms)
        if response.status_code >= 500:
            stats["status_5xx"] += 1
            body = None
            outcome, detail = "unexpected", f"HTTP {response.status_code}"
        else:
            try:
                body = response.json()
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                stats["invalid_json"] += 1
                outcome, detail = "unexpected", f"JSON 아님: {error!r}"
            else:
                outcome, detail = validate_response(spec, response.status_code, body)

        if outcome == "ok":
            stats["ok"] += 1
        elif outcome == "expected_4xx":
            stats["expected_4xx"] += 1
        else:
            stats["unexpected"] += 1
            if len(stats["errors"]) < 50:
                stats["errors"].append(
                    {"op": spec["op"], "status": response.status_code, "detail": detail}
                )

        records.append(
            {
                "op": spec["op"],
                "invalid": invalid,
                "status": response.status_code,
                "latency_ms": round(latency_ms, 3),
                "outcome": outcome,
                "detail": detail,
            }
        )


async def monitor_rss(pid, stop, stats):
    process = psutil.Process(pid)
    while not stop.is_set():
        try:
            stats["rss_samples"].append(process.memory_info().rss)
        except psutil.Error:
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


async def seed(client):
    """hot 키·oracle 키를 준비하고 카운터 기준값을 만든다."""
    async def put(key, value, ttl=None):
        body = {"key": key, "value": value}
        if ttl is not None:
            body["ttl"] = ttl
        response = await client.put("/kv", json=body)
        assert response.status_code == 200, (key, response.status_code, response.text)
        assert response.json() == {"ok": True}, response.text

    for key in counter_keys():
        await put(key, 0)
    for key in cache_keys():
        await put(key, "cached")
    for key in plain_keys():
        await put(key, "plain")
    await put(STABLE_KEY, "stable")
    await put(STABLE_NUM, 0)
    for _ in range(100):
        response = await client.post("/kv/incr", json={"key": STABLE_NUM, "amount": 1})
        assert response.status_code == 200, response.text
        assert response.json()["value"] == _ + 1, response.text


async def check_oracle(client):
    checks = {}
    response = await client.get("/kv", params={"key": STABLE_KEY})
    checks["stable"] = response.status_code == 200 and response.json() == {
        "found": True,
        "value": "stable",
    }
    response = await client.get("/kv", params={"key": STABLE_NUM})
    checks["counter"] = response.status_code == 200 and response.json() == {
        "found": True,
        "value": 100,
    }
    return checks


def wait_for_server(base_url, timeout=10.0):
    deadline = time.monotonic() + timeout
    last_error = None
    with httpx.Client(timeout=1.0) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(f"{base_url}/kv", params={"key": "__health"})
                if response.status_code == 200:
                    return
            except httpx.HTTPError as error:
                last_error = error
            time.sleep(0.05)
    raise RuntimeError(f"서버 준비 시간 초과: {last_error!r}")


def summarize(args, stats, records, started_at, ended_at, rss_start, rss_end, rss_peak, checks, server_alive):
    latencies = stats["latencies"]
    actual_seconds = max(ended_at - started_at, 1e-9)
    summary = {
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_s": round(actual_seconds, 3),
        "config": {
            "seed": args.seed,
            "duration": args.duration,
            "rate": args.rate,
            "concurrency": args.concurrency,
            "keyspace": args.keyspace,
            "ttl_ratio": args.ttl_ratio,
        },
        "total": stats["total"],
        "actual_rps": round(stats["total"] / actual_seconds, 3),
        "ok": stats["ok"],
        "expected_4xx": stats["expected_4xx"],
        "unexpected": stats["unexpected"],
        "status_5xx": stats["status_5xx"],
        "timeouts": stats["timeouts"],
        "transport_errors": stats["transport_errors"],
        "invalid_json": stats["invalid_json"],
        "by_op": dict(stats["by_op"]),
        "latency_ms": {
            "p50": round(percentile(latencies, 0.50), 3) if latencies else None,
            "p95": round(percentile(latencies, 0.95), 3) if latencies else None,
            "p99": round(percentile(latencies, 0.99), 3) if latencies else None,
        },
        "rss_mb": {
            "start": round(rss_start / (1024 * 1024), 2) if rss_start else None,
            "end": round(rss_end / (1024 * 1024), 2) if rss_end else None,
            "peak": round(rss_peak / (1024 * 1024), 2) if rss_peak else None,
        },
        "oracle": checks,
        "server_alive": server_alive,
        "passed": (
            stats["unexpected"] == 0
            and stats["status_5xx"] == 0
            and stats["timeouts"] == 0
            and stats["transport_errors"] == 0
            and stats["invalid_json"] == 0
            and all(checks.values())
            and server_alive
        ),
        "errors": stats["errors"],
        "record_count": len(records),
    }
    return summary


def write_summary(out_dir, summary, records):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (out_dir / "requests.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    lines = [
        "# Mini Redis HTTP soak",
        "",
        f"- 결과: **{'PASS' if summary['passed'] else 'FAIL'}**",
        f"- 기간: {summary['duration_s']}s",
        f"- 총 요청: {summary['total']} ({summary['actual_rps']} RPS)",
        f"- ok / expected 4xx / unexpected: "
        f"{summary['ok']} / {summary['expected_4xx']} / {summary['unexpected']}",
        f"- 5xx / timeout / transport: "
        f"{summary['status_5xx']} / {summary['timeouts']} / {summary['transport_errors']}",
        f"- p50 / p95 / p99: "
        f"{summary['latency_ms']['p50']} / {summary['latency_ms']['p95']} / "
        f"{summary['latency_ms']['p99']} ms",
        f"- RSS start / end / peak: "
        f"{summary['rss_mb']['start']} / {summary['rss_mb']['end']} / "
        f"{summary['rss_mb']['peak']} MB",
        f"- oracle: {summary['oracle']}",
        f"- server alive: {summary['server_alive']}",
        "",
        "## 설정",
        "",
        f"```json\n{json.dumps(summary['config'], ensure_ascii=False, indent=2)}\n```",
    ]
    if summary["errors"]:
        lines += ["", "## 첫 오류", "", f"```json\n{json.dumps(summary['errors'], ensure_ascii=False, indent=2)}\n```"]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return "\n".join(lines)


def terminate_process(process, timeout=5.0):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def main():
    parser = argparse.ArgumentParser(description="Mini Redis HTTP random soak")
    parser.add_argument("--duration", default="2m", type=parse_duration)
    parser.add_argument("--rate", type=float, default=50.0,
                        help="전체 초당 요청 수. 0이면 제한 없음")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--keyspace", type=int, default=10_000)
    parser.add_argument("--ttl-ratio", type=float, default=0.25)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    if args.concurrency <= 0:
        parser.error("--concurrency는 1 이상이어야 합니다")
    if args.keyspace <= 0:
        parser.error("--keyspace는 1 이상이어야 합니다")
    if not 0 <= args.ttl_ratio <= 1:
        parser.error("--ttl-ratio는 0~1이어야 합니다")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.output) if args.output else ROOT / "stress-results" / f"{stamp}-seed{args.seed}"
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    server_log = out_dir / "server.log"
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_handle = server_log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "tools.stress_server", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    rng = random.Random(args.seed)
    stats = {
        "total": 0,
        "ok": 0,
        "expected_4xx": 0,
        "unexpected": 0,
        "status_5xx": 0,
        "timeouts": 0,
        "transport_errors": 0,
        "invalid_json": 0,
        "by_op": Counter(),
        "latencies": [],
        "rss_samples": [],
        "errors": [],
    }
    records = []

    async def run():
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=args.timeout,
            limits=httpx.Limits(
                max_connections=max(args.concurrency * 2, 10),
                max_keepalive_connections=max(args.concurrency, 5),
            ),
        ) as client:
            await seed(client)
            stop = asyncio.Event()
            monitor = asyncio.create_task(monitor_rss(process.pid, stop, stats))
            limiter = RateLimiter(args.rate)
            started_at = time.time()
            started_mono = time.monotonic()
            deadline = started_mono + args.duration
            tasks = [
                asyncio.create_task(
                    worker(
                        client,
                        rng,
                        deadline,
                        limiter,
                        args.keyspace,
                        args.ttl_ratio,
                        stats,
                        records,
                    )
                )
                for _ in range(args.concurrency)
            ]
            await asyncio.gather(*tasks)
            ended_mono = time.monotonic()
            ended_at = time.time()
            stop.set()
            await monitor
            checks = await check_oracle(client)
            server_alive = process.poll() is None
            try:
                stats["rss_samples"].append(psutil.Process(process.pid).memory_info().rss)
            except psutil.Error:
                pass
            rss_samples = stats["rss_samples"]
            rss_start = rss_samples[0] if rss_samples else 0
            rss_end = rss_samples[-1] if rss_samples else 0
            rss_peak = max(rss_samples) if rss_samples else 0
            return summarize(
                args, stats, records, started_at, ended_at,
                rss_start, rss_end, rss_peak, checks, server_alive,
            )

    try:
        wait_for_server(base_url)
        summary = asyncio.run(run())
    except Exception as error:
        terminate_process(process)
        log_handle.close()
        print(f"스트레스 실행 실패: {error!r}", file=sys.stderr)
        try:
            print(server_log.read_text(encoding="utf-8")[-4000:], file=sys.stderr)
        except OSError:
            pass
        raise SystemExit(1)
    finally:
        terminate_process(process)
        if not log_handle.closed:
            log_handle.close()

    print(write_summary(out_dir, summary, records))
    print(f"\n결과 폴더: {out_dir}")
    raise SystemExit(0 if summary["passed"] else 1)


if __name__ == "__main__":
    main()
