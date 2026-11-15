"""시나리오 03 — 서버 스택·무효화 성능 비교 실행 담당.

- S3-02 카운터: 기존 counter.jmx·counter.groovy·run_performance.run_single을 그대로 쓴다.
- S3-03A 캐시 수명주기: invalidation.jmx의 cache_lifecycle 분기.
- S3-03B 콜드 키 만료 백로그: invalidation.jmx의 expiry_backlog 분기.
- 서버 프로세스는 이 스크립트가 시작·종료하고, 측정 구간에는 포함하지 않는다.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

import run_performance as base

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FASTAPI_PYTHON = str(PROJECT_ROOT / ".venv" / "bin" / "python")
FLASK_PYTHON = "/tmp/opencode/mini-redis-test-env/bin/python"
SOURCE_ROOT = Path("/tmp/opencode/m3-stack-source")

CACHE_VALUE = '{"temperature":22,"observed_at":"2026-11-14T12:00:00Z"}'
HOT_VALUE = '{"temperature":19,"observed_at":"2026-11-14T12:00:00Z"}'

CACHE_OPS = ("get_hit", "delete", "get_miss", "put_refill")
EXPIRY_OPS = ("get_hot",)

CACHE_MEASURE_LABELS = (
    "m3-cache-hit-get", "m3-cache-delete", "m3-cache-miss-get", "m3-cache-refill-put",
)
CACHE_WARMUP_LABELS = (
    "m3-warmup-cache-put", "m3-warmup-cache-get",
    "m3-warmup-cache-delete", "m3-warmup-cache-get-miss",
)
EXPIRY_MEASURE_LABELS = ("m3-expiry-hot-get",)
EXPIRY_WARMUP_LABELS = ("m3-warmup-expiry-hot-get",)

OP_LABELS = {
    "get_hit": "m3-cache-hit-get",
    "delete": "m3-cache-delete",
    "get_miss": "m3-cache-miss-get",
    "put_refill": "m3-cache-refill-put",
    "get_hot": "m3-expiry-hot-get",
}

SERVERS = {
    "flask": {
        "label": "Flask 3.1.3 / Werkzeug 3.1.9",
        "cwd": SOURCE_ROOT / "flask",
        "python": FLASK_PYTHON,
    },
    "fastapi_ce427": {
        "label": "FastAPI 0.162.7 / uvicorn 0.54.0 @ ce427a2",
        "cwd": SOURCE_ROOT / "fastapi-ce427",
        "python": FASTAPI_PYTHON,
    },
    "fastapi_head": {
        "label": "FastAPI 0.162.7 / uvicorn 0.54.0 @ HEAD TTL",
        "cwd": SOURCE_ROOT / "fastapi-head",
        "python": FASTAPI_PYTHON,
    },
}

SUMMARY_FIELDS = ("throughput_per_second", "p50_ms", "p95_ms", "p99_ms", "success_rate")
OP_FIELDS = ("throughput_per_second", "p50_ms", "p95_ms", "p99_ms")


# ---------------------------------------------------------------------------
# 실행 입력
# ---------------------------------------------------------------------------


def _read_environment(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise base.InputError("환경 기록은 JSON 객체여야 한다")
    return payload


def _source_hashes(server_id):
    server = SERVERS[server_id]
    return {
        name: base_hash(server["cwd"] / name)
        for name in ("app.py", "storage.py")
    }


def base_hash(path):
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _server_command(server_id):
    server = SERVERS[server_id]
    return [server["python"], "app.py"]


def _make_settings(args):
    return {
        "results_dir": args.results_dir,
        "state_dir": args.state_dir,
        "environment": _read_environment(args.environment_file),
        "n": args.n,
        "request_timeout_ms": args.request_timeout_ms,
        "total_limit_ms": args.total_limit_ms,
        "prep_limit_ms": args.prep_limit_ms,
        "finish_limit_ms": args.finish_limit_ms,
        "jmeter_bin": args.jmeter or os.environ.get("M3_JMETER_BIN"),
        "java_home": args.java_home or os.environ.get("M3_JAVA_HOME")
        or os.environ.get("JAVA_HOME"),
        "jmx_counter": str(Path(__file__).with_name("counter.jmx")),
        "jmx_invalidation": str(Path(__file__).with_name("invalidation.jmx")),
        "properties_counter": str(Path(__file__).with_name("jmeter.properties")),
        "properties_invalidation": str(Path(__file__).with_name("invalidation.properties")),
        "http": {
            "host": args.http_host,
            "port": args.http_port,
            "scheme": args.http_scheme,
        },
        "jdbc": {"url": "", "user": "", "password": None, "password_file": None},
        "cache_k": args.cache_k,
        "cache_ttl_ms": args.cache_ttl_ms,
        "cache_value": args.cache_value,
        "cold_k": args.cold_k,
        "cold_ttl_ms": args.cold_ttl_ms,
        "cold_value": args.cold_value,
        "hot_key": args.hot_key,
        "hot_value": args.hot_value,
        "manage_server": not args.no_manage_server,
        "campaign_id": args.run_id or time.strftime("%Y%m%dT%H%M%S"),
    }


def validate_invalidation_inputs(settings, scenario, c):
    errors = []
    for name in ("results_dir", "state_dir", "n", "request_timeout_ms",
                 "total_limit_ms", "prep_limit_ms", "finish_limit_ms",
                 "jmeter_bin", "java_home", "environment"):
        if settings.get(name) in (None, ""):
            errors.append(f"{name}: 필수 입력 없음")
    for name in ("n", "request_timeout_ms", "total_limit_ms",
                 "prep_limit_ms", "finish_limit_ms"):
        value = settings.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            errors.append(f"{name}: 1 이상의 정수 필요")
    if scenario not in ("cache_lifecycle", "expiry_backlog"):
        errors.append(f"scenario: 알 수 없음 {scenario!r}")
    if c not in base.CONCURRENCIES:
        errors.append(f"c: {base.CONCURRENCIES} 중 하나여야 함")
    if scenario == "cache_lifecycle":
        k = settings.get("cache_k")
        if not isinstance(k, int) or k <= 0 or k % 2:
            errors.append("cache_k: 2 이상의 짝수 정수 필요")
        elif settings.get("n") != (k * 5) // 2:
            errors.append("n: cache_lifecycle은 cache_k의 2.5배여야 함")
        elif (k % c) or ((k // 2) % c):
            errors.append("cache_k와 cache_k/2가 동시성 C로 나누어져야 함")
    if scenario == "expiry_backlog":
        cold_k = settings.get("cold_k")
        if not isinstance(cold_k, int) or cold_k <= 0:
            errors.append("cold_k: 1 이상의 정수 필요")
        if settings.get("n") % c:
            errors.append("n이 동시성 C로 나누어져야 함")
    if settings.get("n") and settings.get("total_limit_ms"):
        if settings["total_limit_ms"] <= settings["request_timeout_ms"]:
            errors.append("total_limit_ms는 request_timeout_ms보다 커야 함")
    if errors:
        raise base.InputError("입력이 불완전하다:\n- " + "\n- ".join(errors))


# ---------------------------------------------------------------------------
# 서버 시작·종료
# ---------------------------------------------------------------------------


def _port_free(host="127.0.0.1", port=5000):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) != 0


def _wait_ready(timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", 5000, timeout=1)
            connection.request("GET", "/kv?key=health")
            response = connection.getresponse()
            response.read()
            connection.close()
            if response.status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.1)
    return False


def start_server(server_id, log_path, timeout=30.0):
    if not _port_free():
        raise base.InputError("포트 5000이 이미 사용 중이다")
    server = SERVERS[server_id]
    log = Path(log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        _server_command(server_id), cwd=server["cwd"],
        stdout=handle, stderr=subprocess.STDOUT, text=True,
        start_new_session=True,
    )
    if not _wait_ready(timeout):
        stop_server(process)
        handle.close()
        detail = log.read_text(encoding="utf-8", errors="ignore")[-2000:]
        raise base.InputError(f"서버 준비 실패: {server_id}\n{detail}")
    return process, handle


def stop_server(process):
    if process is None:
        return
    if process.poll() is None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
    deadline = time.time() + 10
    while time.time() < deadline and not _port_free():
        time.sleep(0.1)


# ---------------------------------------------------------------------------
# 속성·결과 읽기
# ---------------------------------------------------------------------------


def _write_properties(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for name, value in values.items():
                handle.write(f"{name}={base._properties_escape(value)}\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def invalidation_properties(settings, *, scenario, server_id, c, round_index,
                            run_dir):
    warmup_per_thread = 4 if scenario == "cache_lifecycle" else 100 // c
    warmup_total = warmup_per_thread * c
    cache = scenario == "cache_lifecycle"
    expiry = scenario == "expiry_backlog"
    return {
        "m3.run_id": settings.get("run_id"),
        "m3.target": "http",
        "m3.scenario": scenario,
        "m3.server_id": server_id,
        "m3.run_dir": str(run_dir),
        "m3.calls_file": str(Path(run_dir) / "calls.csv"),
        "m3.completion_file": str(Path(run_dir) / "completion.json"),
        "m3.c": c,
        "m3.n": settings["n"],
        "m3.warmup_total": warmup_total,
        "m3.warmup_per_thread": warmup_per_thread,
        "m3.request_timeout_ms": settings["request_timeout_ms"],
        "m3.total_limit_ms": settings["total_limit_ms"],
        "m3.prep_limit_ms": settings["prep_limit_ms"],
        "m3.finish_limit_ms": settings["finish_limit_ms"],
        "m3.http.host": settings["http"]["host"],
        "m3.http.port": settings["http"]["port"],
        "m3.http.scheme": settings["http"]["scheme"],
        "m3.http.connect_timeout_ms": settings["request_timeout_ms"],
        "m3.http.response_timeout_ms": settings["request_timeout_ms"],
        "m3.scenario.cache": "true" if cache else "false",
        "m3.scenario.expiry": "true" if expiry else "false",
        "m3.cache.k": settings["cache_k"],
        "m3.cache.half": settings["cache_k"] // 2,
        "m3.cache.phase1_loops": settings["cache_k"] // c,
        "m3.cache.phase2_loops": (settings["cache_k"] // 2) // c,
        "m3.cache.value": settings["cache_value"],
        "m3.cache.ttl_ms": settings["cache_ttl_ms"],
        "m3.expiry.cold_k": settings["cold_k"],
        "m3.expiry.cold_ttl_ms": settings["cold_ttl_ms"],
        "m3.expiry.cold_value": settings["cold_value"],
        "m3.expiry.hot_key": settings["hot_key"],
        "m3.expiry.hot_value": settings["hot_value"],
        "m3.expiry.loops": settings["n"] // c,
    }


def read_jtl_measure(path, labels):
    rows = base.read_jtl_all_rows(path)
    result = []
    for row in rows:
        if row.get("label") not in labels:
            continue
        if row.get("m3_phase") != "measure":
            continue
        try:
            row["m3_request_sequence"] = base._as_int(row.get("m3_request_sequence"))
            row["m3_start_ns"] = base._as_int(row.get("m3_start_ns"))
            row["m3_decision_ns"] = base._as_int(row.get("m3_decision_ns"))
            row["m3_success"] = base._as_bool(row.get("success"))
            row["m3_unknown"] = base._as_bool(row.get("m3_processing_unknown"))
        except (TypeError, ValueError) as error:
            raise base.IncompleteResultError(
                "JTL 측정 행의 정수·불리언 값이 계약과 다르다",
                {"path": str(path), "error": str(error)},
            ) from error
        result.append(row)
    return result


def _is_success(record):
    return record.get("outcome") == "success"


def _latencies_ms(records):
    return [
        int(record["client_elapsed_ns"]) / 1_000_000
        for record in records
        if _is_success(record) and record.get("client_elapsed_ns") not in (None, "")
    ]


def round_metrics(records, planned_n, duration_ns, scenario, completion, settings):
    attempts = len(records)
    success = sum(1 for record in records if _is_success(record))
    duration_seconds = duration_ns / 1_000_000_000 if duration_ns else None
    throughput = success / duration_seconds if duration_seconds else None
    per_operation = {}
    ops = CACHE_OPS if scenario == "cache_lifecycle" else EXPIRY_OPS
    for op in ops:
        subset = [record for record in records if record.get("operation") == op]
        op_success = sum(1 for record in subset if _is_success(record))
        op_starts = [int(record["start_ns"]) for record in subset
                     if record.get("start_ns") is not None]
        op_decisions = [int(record["decision_ns"]) for record in subset
                        if record.get("decision_ns") is not None]
        op_duration = (max(op_decisions) - min(op_starts)
                       if op_starts and op_decisions else None)
        op_seconds = op_duration / 1_000_000_000 if op_duration else None
        latencies = _latencies_ms(subset)
        per_operation[op] = {
            "attempts": len(subset),
            "success_count": op_success,
            "success_rate": op_success / len(subset) if subset else None,
            "throughput_per_second": op_success / op_seconds if op_seconds else None,
            "p50_ms": base.percentile(latencies, 0.50),
            "p95_ms": base.percentile(latencies, 0.95),
            "p99_ms": base.percentile(latencies, 0.99),
        }
    return {
        "attempts": attempts,
        "planned_n": planned_n,
        "success_count": success,
        "success_rate": success / attempts if attempts else None,
        "throughput_per_second": throughput,
        "p50_ms": base.percentile(_latencies_ms(records), 0.50),
        "p95_ms": base.percentile(_latencies_ms(records), 0.95),
        "p99_ms": base.percentile(_latencies_ms(records), 0.99),
        "final_value": completion.get("final_value"),
        "duration_ns": duration_ns,
        "per_operation": per_operation,
        "passed": (
            attempts == planned_n
            and success == planned_n
            and completion.get("normal") is True
        ),
    }


def summarize_rounds(rounds, scenario):
    if len(rounds) != base.ROUNDS:
        raise base.IncompleteResultError(
            f"5회 기본 비교가 아니다: {len(rounds)}회", {"rounds": len(rounds)}
        )
    failed = [index for index, item in enumerate(rounds, start=1)
              if not item.get("passed")]
    if failed:
        raise base.IncompleteResultError(
            "실패 회차가 있어 5회 요약을 만들지 않는다: "
            + ", ".join(str(index) for index in failed),
            {"failed_rounds": failed},
        )
    summary = {"rounds": len(rounds)}
    for name in SUMMARY_FIELDS:
        values = [item[name] for item in rounds if item.get(name) is not None]
        summary[name] = None if not values else {
            "median": statistics.median(values),
            "min": min(values),
            "max": max(values),
        }
    ops = CACHE_OPS if scenario == "cache_lifecycle" else EXPIRY_OPS
    summary["operations"] = {}
    for op in ops:
        op_summary = {}
        for name in OP_FIELDS:
            values = [item["per_operation"][op][name] for item in rounds
                      if item["per_operation"].get(op, {}).get(name) is not None]
            op_summary[name] = None if not values else {
                "median": statistics.median(values),
                "min": min(values),
                "max": max(values),
            }
        summary["operations"][op] = op_summary
    return summary


# ---------------------------------------------------------------------------
# 산출물 검산
# ---------------------------------------------------------------------------


def validate_invalidation_artifacts(run_dir, expected, settings):
    run_dir = Path(run_dir)
    scenario = expected["scenario"]
    c = expected["concurrency"]
    n = expected["planned_n"]
    completion = base.read_completion(run_dir)
    records = base.read_calls_csv(run_dir / "calls.csv")
    labels = CACHE_MEASURE_LABELS if scenario == "cache_lifecycle" else EXPIRY_MEASURE_LABELS
    warmup_labels = CACHE_WARMUP_LABELS if scenario == "cache_lifecycle" else EXPIRY_WARMUP_LABELS
    jtl_rows = read_jtl_measure(run_dir / "result.jtl", labels)
    all_rows = base.read_jtl_all_rows(run_dir / "result.jtl")
    problems = []

    for name in ("run_id", "planned_n", "concurrency", "target", "scenario"):
        if completion.get(name) != expected[name]:
            problems.append(
                f"완료 표식 {name} 불일치: {completion.get(name)!r} != {expected[name]!r}"
            )
    if completion.get("normal") is not True:
        problems.append("완료 표식이 정상 완료가 아니다")
    if completion.get("prep_failed"):
        problems.append("준비 실패 표식이 있다")
    if completion.get("limit_hit"):
        problems.append("전체 한도 도달 표식이 있다")
    if completion.get("stop_reason"):
        problems.append(f"중단 사유가 있다: {completion['stop_reason']}")

    computed = {name: 0 for name in base.OUTCOMES}
    op_counts = {op: 0 for op in (CACHE_OPS if scenario == "cache_lifecycle" else EXPIRY_OPS)}
    for record in records:
        outcome = record.get("outcome")
        if outcome not in computed:
            problems.append(f"알 수 없는 결과 분류: {outcome!r}")
            continue
        computed[outcome] += 1
        operation = record.get("operation")
        if operation not in op_counts:
            problems.append(f"알 수 없는 operation: {operation!r}")
        else:
            op_counts[operation] += 1
        if record.get("run_id") != expected["run_id"]:
            problems.append("호출 기록 run_id 불일치")
        if record.get("phase") != "measure":
            problems.append("측정 외 단계 기록이 정규화 결과에 있다")
        start = record.get("start_ns")
        deadline = record.get("deadline_ns")
        decision = record.get("decision_ns")
        observed = record.get("observed_decision_ns")
        returned = record.get("return_ns")
        observed_elapsed = record.get("observed_elapsed_ns")
        client_elapsed = record.get("client_elapsed_ns")
        late = record.get("late_return")
        if start is None or decision is None:
            problems.append("시작 또는 결정 시각이 없는 호출 기록이 있다")
            continue
        if deadline is None or deadline <= start:
            problems.append("요청 마감이 시작보다 늦지 않다")
        if decision < start:
            problems.append("결정 시각이 시작보다 이르다")
        if observed is not None and observed < decision:
            problems.append("관찰된 결정 시각이 결정 시각보다 이르다")
        if outcome == "success":
            if returned is None:
                problems.append("성공 기록에 반환 시각이 없다")
            else:
                if decision != returned:
                    problems.append("성공 결정 시각이 반환 시각과 다르다")
                if observed != returned:
                    problems.append("성공 관찰 결정 시각이 반환 시각과 다르다")
                if client_elapsed != returned - start:
                    problems.append("성공 지연이 반환-시작과 다르다")
                if client_elapsed is None or client_elapsed <= 0:
                    problems.append("성공 지연이 양수가 아니다")
            if record.get("http_code") != 200:
                problems.append("HTTP 성공 기록의 상태 코드가 200이 아니다")
            if record.get("processing_unknown") is not False:
                problems.append("성공 기록의 처리 불명확 표시가 false가 아니다")
            if late is not False:
                problems.append("성공 기록의 늦은 반환 표시가 false가 아니다")
        else:
            problems.append(f"성공이 아닌 기록이 있다: {outcome!r}")
        if observed_elapsed is not None and returned is not None:
            if observed_elapsed != returned - start:
                problems.append("관찰 경과가 반환-시작과 다르다")

    expected_ops = (
        {
            "get_hit": settings["cache_k"],
            "delete": settings["cache_k"] // 2,
            "get_miss": settings["cache_k"] // 2,
            "put_refill": settings["cache_k"] // 2,
        }
        if scenario == "cache_lifecycle"
        else {"get_hot": n}
    )
    for op, expected_count in expected_ops.items():
        if op_counts.get(op) != expected_count:
            problems.append(
                f"operation {op} 건수 불일치: {op_counts.get(op)} != {expected_count}"
            )

    if completion.get("attempts") != len(records):
        problems.append("완료 표식 시도 수와 정규화 기록 수가 다르다")
    for name in base.OUTCOMES:
        if completion.get(name) != computed[name]:
            problems.append(f"완료 표식 {name} 건수 불일치")
    if completion.get("decided") != len(records):
        problems.append("완료 표식 결정 건수와 기록 수가 다르다")
    if completion.get("success") != n or computed["success"] != n:
        problems.append("성공 수가 계획 N과 다르다")
    if len(records) != n:
        problems.append("시도 수가 계획 N과 다르다")
    if completion.get("processing_unknown"):
        problems.append("처리 여부 불명확 건수가 있다")

    warmup_total = 4 * c if scenario == "cache_lifecycle" else 100
    if completion.get("warmup_completed") != warmup_total:
        problems.append("워밍업 완료 건수가 계약과 다르다")
    if completion.get("warmup_expected") != warmup_total:
        problems.append("워밍업 기대 건수가 계약과 다르다")
    warmup_rows = [
        row for row in all_rows
        if row.get("label") in warmup_labels and row.get("m3_phase") == "warmup"
    ]
    if len(warmup_rows) != warmup_total:
        problems.append("JTL 워밍업 행 수가 계약과 다르다")

    expected_final = settings["cache_k"] if scenario == "cache_lifecycle" else 1
    if completion.get("final_read_done") is not True:
        problems.append("최종 상태 확인 미완료")
    if completion.get("final_read_ok") is not True:
        problems.append("최종 상태 확인 실패")
    if completion.get("final_value") != expected_final:
        problems.append(
            f"최종 수치 불일치: {completion.get('final_value')!r} != {expected_final!r}"
        )

    call_keys = [(record.get("thread_name"), record.get("request_sequence"))
                 for record in records]
    if len(call_keys) != len(set(call_keys)):
        problems.append("정규화 기록에 (실행 주체, 순번) 중복이 있다")
    jtl_keys = [(row.get("m3_thread_name"), row.get("m3_request_sequence"))
                for row in jtl_rows]
    if len(jtl_keys) != len(set(jtl_keys)):
        problems.append("JTL 측정 행에 (실행 주체, 순번) 중복이 있다")
    if set(call_keys) != set(jtl_keys):
        problems.append("JTL과 정규화 기록의 요청 집합이 다르다")
    if len(jtl_rows) != len(records):
        problems.append("JTL 측정 행 수와 정규화 기록 수가 다르다")

    by_key = {(row.get("m3_thread_name"), row.get("m3_request_sequence")): row
              for row in jtl_rows}
    for record in records:
        row = by_key.get((record.get("thread_name"), record.get("request_sequence")))
        if row is None:
            continue
        if row.get("m3_outcome") != record.get("outcome"):
            problems.append("JTL과 정규화 기록의 결과 분류가 다르다")
        if row.get("m3_operation") != record.get("operation"):
            problems.append("JTL과 정규화 기록의 operation이 다르다")
        if row.get("m3_success") != _is_success(record):
            problems.append("JTL 성공 표시가 정규화 결과와 다르다")
        if row.get("m3_start_ns") != record.get("start_ns"):
            problems.append("JTL과 정규화 기록의 시작 시각이 다르다")
        if row.get("m3_decision_ns") != record.get("decision_ns"):
            problems.append("JTL과 정규화 기록의 결정 시각이 다르다")
        expected_label = OP_LABELS.get(record.get("operation"))
        if expected_label is not None and row.get("label") != expected_label:
            problems.append("JTL 라벨이 operation과 다르다")

    peak = base._max_in_flight(records)
    if peak > c:
        problems.append(f"최대 진행 수 {peak} > 동시성 {c}")
    if completion.get("max_in_flight") is not None and completion["max_in_flight"] > c:
        problems.append("완료 표식 최대 진행 수가 동시성을 넘었다")
    for thread, thread_peak in base._per_thread_peak(records).items():
        if thread_peak > 1:
            problems.append(f"실행 주체 {thread}의 최대 진행 수가 {thread_peak}이다")

    start_ns = completion.get("start_ns")
    end_ns = completion.get("end_ns")
    duration_ns = None
    if (not isinstance(start_ns, int) or isinstance(start_ns, bool)
            or not isinstance(end_ns, int) or isinstance(end_ns, bool)):
        problems.append("완료 표식의 전체 시작·종료 시각이 정수가 아니다")
    else:
        duration_ns = end_ns - start_ns
        if duration_ns <= 0:
            problems.append("전체 실행 구간이 양수가 아니다")
        starts = [record["start_ns"] for record in records]
        decisions = [record["decision_ns"] for record in records]
        if starts and start_ns > min(starts):
            problems.append("전체 시작 시각이 첫 호출 시작보다 늦다")
        if decisions and end_ns != max(decisions):
            problems.append("전체 종료 시각이 마지막 결정 시각과 다르다")
        if decisions and any(decision < start_ns or decision > end_ns
                             for decision in decisions):
            problems.append("호출 결정 시각이 전체 구간 밖에 있다")
    if completion.get("measurement_complete") is not True:
        problems.append("측정 결정 완료 표식이 없다")
    if completion.get("records_written") != len(records):
        problems.append("기록 완료 건수와 정규화 기록 수가 다르다")

    if problems:
        raise base.IncompleteResultError(
            "실행 산출물 검산 실패: " + "; ".join(problems[:12]),
            {
                "problems": problems,
                "run_dir": str(run_dir),
                "attempts": len(records),
                "completion": completion,
            },
        )
    metrics = round_metrics(records, n, duration_ns, scenario, completion, settings)
    return {
        "run_id": expected["run_id"],
        "scenario": scenario,
        "server_id": expected["server_id"],
        "target": "http",
        "concurrency": c,
        "attempts": len(records),
        "success": computed["success"],
        "final_value": completion.get("final_value"),
        "max_in_flight": peak,
        "duration_ns": duration_ns,
        "completion": completion,
        "metrics": metrics,
    }


# ---------------------------------------------------------------------------
# 실행 단위
# ---------------------------------------------------------------------------


def _run_id(settings, scenario, server_id, c, round_index):
    return f"{settings['campaign_id']}-{scenario}-{server_id}-c{c}-r{round_index}"


def run_invalidation_single(settings, *, scenario, server_id, c, round_index,
                            manage_server=True):
    validate_invalidation_inputs(settings, scenario, c)
    results_dir = Path(settings["results_dir"]).resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    run_id = settings.get("run_id") or _run_id(settings, scenario, server_id, c, round_index)
    run_dir = results_dir / run_id
    if run_dir.exists():
        raise base.IncompleteResultError("결과 폴더를 덮어쓰지 않는다", {"run_dir": str(run_dir)})
    run_dir.mkdir()
    store = base.RunStateStore(Path(settings["state_dir"]))
    store.begin(run_id, endpoint=f"http://{settings['http']['host']}:{settings['http']['port']}")
    process = None
    handle = None
    try:
        (run_dir / "run_meta.json").write_text(
            json.dumps({
                "run_id": run_id,
                "scenario": scenario,
                "server_id": server_id,
                "server_label": SERVERS[server_id]["label"],
                "source_hashes": _source_hashes(server_id),
                "server_command": _server_command(server_id),
                "server_cwd": str(SERVERS[server_id]["cwd"]),
                "concurrency": c,
                "round": round_index,
                "settings": {
                    key: value for key, value in settings.items()
                    if key not in ("environment", "jdbc")
                },
                "environment": settings["environment"],
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        values = invalidation_properties(
            settings, scenario=scenario, server_id=server_id, c=c,
            round_index=round_index, run_dir=run_dir,
        )
        values["m3.run_id"] = run_id
        _write_properties(run_dir / "run.properties", values)
        if manage_server:
            process, handle = start_server(server_id, run_dir / "server.log")
        child = dict(settings)
        child.update({
            "run_id": run_id,
            "jmx": settings["jmx_invalidation"],
            "properties": settings["properties_invalidation"],
            "runtime_properties_path": str(run_dir / "run.properties"),
        })
        outcome = base.default_runner(child, run_dir)
        if handle is not None:
            handle.close()
            handle = None
        (run_dir / "jmeter.stdout.log").write_text(
            (outcome.get("stdout") or "") + (outcome.get("stderr") or ""),
            encoding="utf-8",
        )
        if outcome.get("timed_out"):
            raise base.IncompleteResultError(
                "JMeter 프로세스가 종료 한도 안에 끝나지 않았다",
                {"run_dir": str(run_dir), "timed_out": True},
            )
        if outcome.get("returncode") != 0:
            raise base.IncompleteResultError(
                f"JMeter 종료 코드 {outcome.get('returncode')}",
                {"run_dir": str(run_dir), "returncode": outcome.get("returncode")},
            )
        result = validate_invalidation_artifacts(run_dir, {
            "run_id": run_id,
            "scenario": scenario,
            "server_id": server_id,
            "target": "http",
            "planned_n": settings["n"],
            "concurrency": c,
        }, settings)
        store.complete(run_id)
        return result
    except Exception as error:
        store.fail(run_id, str(error))
        raise
    finally:
        if handle is not None:
            handle.close()
        stop_server(process)


def run_counter_single(settings, *, server_id, c, round_index):
    """기존 S3-02 카운터 실행을 서버 시작·종료와 함께 수행한다."""
    results_dir = Path(settings["results_dir"]).resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    run_id = settings.get("run_id") or _run_id(settings, "counter", server_id, c, round_index)
    run_dir = results_dir / run_id
    if run_dir.exists():
        raise base.IncompleteResultError("결과 폴더를 덮어쓰지 않는다", {"run_dir": str(run_dir)})
    server_log = results_dir / f".{run_id}-server.log"
    process = None
    handle = None
    try:
        process, handle = start_server(server_id, server_log)
        child = dict(settings)
        child.update({
            "run_id": run_id,
            "jmx": settings["jmx_counter"],
            "properties": settings["properties_counter"],
        })
        base.validate_single_inputs(child, target="http")
        result = base.run_single(child, target="http", concurrency=c,
                                 round_index=round_index)
        stop_server(process)
        process = None
        if handle is not None:
            handle.close()
            handle = None
        if server_log.exists():
            server_log.replace(run_dir / "server.log")
        meta_path = run_dir / "run_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.update({
            "scenario": "counter",
            "server_id": server_id,
            "server_label": SERVERS[server_id]["label"],
            "source_hashes": _source_hashes(server_id),
            "server_command": _server_command(server_id),
            "server_cwd": str(SERVERS[server_id]["cwd"]),
        })
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return {**result, "scenario": "counter", "server_id": server_id}
    finally:
        if handle is not None:
            handle.close()
        stop_server(process)
        if server_log.exists() and not (run_dir / "server.log").exists():
            server_log.replace(run_dir / "server.log")


# ---------------------------------------------------------------------------
# 묶음 실행
# ---------------------------------------------------------------------------


def rotate(values, offset):
    if not values:
        return []
    offset %= len(values)
    return list(values[offset:]) + list(values[:offset])


def run_campaign(settings, *, scenario, servers, c_list, rounds):
    results_dir = Path(settings["results_dir"]).resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    campaign_id = settings["campaign_id"]
    plan = []
    for c in c_list:
        for round_index in range(1, rounds + 1):
            for server_id in rotate(servers, round_index - 1):
                plan.append({
                    "scenario": scenario,
                    "server_id": server_id,
                    "concurrency": c,
                    "round": round_index,
                })
    records = []
    stopped = None
    for index, item in enumerate(plan):
        try:
            if scenario == "counter":
                result = run_counter_single(
                    settings, server_id=item["server_id"],
                    c=item["concurrency"], round_index=item["round"])
            else:
                result = run_invalidation_single(
                    settings, scenario=scenario, server_id=item["server_id"],
                    c=item["concurrency"], round_index=item["round"])
        except (base.IncompleteResultError, base.PendingRunError, base.InputError) as error:
            stopped = {
                "index": index,
                "item": item,
                "error": str(error),
                "attempts": getattr(error, "details", {}).get("attempts"),
            }
            records.append({**item, "status": "failed", "error": str(error)})
            for remaining in plan[index + 1:]:
                records.append({**remaining, "status": "not_run"})
            break
        records.append({**item, "status": "completed",
                        "run_id": result["run_id"],
                        "metrics": result["metrics"]})
    summary = {
        "campaign_id": campaign_id,
        "scenario": scenario,
        "planned": len(plan),
        "executed": sum(1 for record in records if record["status"] == "completed"),
        "stopped": stopped,
        "runs": records,
    }
    if stopped is None and len(records) == len(plan):
        groups = {}
        for record in records:
            key = (record["server_id"], record["concurrency"])
            groups.setdefault(key, []).append(record["metrics"])
        summary["summaries"] = {}
        for (server_id, c), metrics in sorted(groups.items()):
            if scenario == "counter":
                summary["summaries"][f"{server_id}-c{c}"] = base.summarize_rounds(metrics)
            else:
                summary["summaries"][f"{server_id}-c{c}"] = summarize_rounds(metrics, scenario)
    (results_dir / f"campaign-{scenario}-{campaign_id}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parser():
    parser = argparse.ArgumentParser(description="시나리오 03 서버 스택·무효화 비교")
    parser.add_argument("--scenario", required=True,
                        choices=("counter", "cache_lifecycle", "expiry_backlog"))
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--environment-file", required=True)
    parser.add_argument("--n", type=int, default=10000)
    parser.add_argument("--c", type=int, default=None,
                        help="단일 실행 동시성. 없으면 묶음")
    parser.add_argument("--round", type=int, default=1)
    parser.add_argument("--rounds", type=int, default=base.ROUNDS)
    parser.add_argument("--server-id", default=None,
                        help="단일 실행 서버 ID")
    parser.add_argument("--servers", default=None,
                        help="묶음 서버 ID 쉼표 목록")
    parser.add_argument("--campaign", action="store_true",
                        help="묶음 실행")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--request-timeout-ms", type=int, default=5000)
    parser.add_argument("--total-limit-ms", type=int, default=360000)
    parser.add_argument("--prep-limit-ms", type=int, default=30000)
    parser.add_argument("--finish-limit-ms", type=int, default=15000)
    parser.add_argument("--cache-k", type=int, default=4000)
    parser.add_argument("--cache-ttl-ms", type=int, default=600000)
    parser.add_argument("--cache-value", default=CACHE_VALUE)
    parser.add_argument("--cold-k", type=int, default=10000)
    parser.add_argument("--cold-ttl-ms", type=int, default=5000)
    parser.add_argument("--cold-value", default=CACHE_VALUE)
    parser.add_argument("--hot-key", default="weather:hot")
    parser.add_argument("--hot-value", default=HOT_VALUE)
    parser.add_argument("--http-host", default="127.0.0.1")
    parser.add_argument("--http-port", type=int, default=5000)
    parser.add_argument("--http-scheme", default="http")
    parser.add_argument("--jmeter", default=None)
    parser.add_argument("--java-home", default=None)
    parser.add_argument("--no-manage-server", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    settings = _make_settings(args)
    if args.campaign:
        servers = (args.servers.split(",") if args.servers
                   else ("flask", "fastapi_ce427", "fastapi_head"))
        unknown = [server for server in servers if server not in SERVERS]
        if unknown:
            raise base.InputError(f"알 수 없는 서버: {unknown}")
        c_list = [args.c] if args.c else list(base.CONCURRENCIES)
        summary = run_campaign(
            settings, scenario=args.scenario, servers=servers,
            c_list=c_list, rounds=args.rounds)
        print(json.dumps({
            "campaign_id": summary["campaign_id"],
            "scenario": summary["scenario"],
            "planned": summary["planned"],
            "executed": summary["executed"],
            "stopped": summary["stopped"],
        }, ensure_ascii=False, indent=2))
        return 0 if summary["stopped"] is None else 1
    if args.server_id is None or args.c is None:
        raise base.InputError("단일 실행은 --server-id와 --c가 필요하다")
    if args.scenario == "counter":
        result = run_counter_single(
            settings, server_id=args.server_id, c=args.c, round_index=args.round)
    else:
        result = run_invalidation_single(
            settings, scenario=args.scenario, server_id=args.server_id,
            c=args.c, round_index=args.round,
            manage_server=settings["manage_server"])
    print(json.dumps({
        "run_id": result["run_id"],
        "scenario": result.get("scenario"),
        "server_id": result.get("server_id"),
        "attempts": result["attempts"],
        "success": result["success"],
        "final_value": result["final_value"],
        "metrics": result["metrics"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
