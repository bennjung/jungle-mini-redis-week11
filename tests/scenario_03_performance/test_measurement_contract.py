"""시나리오 03 M3 계약 검증.

집계·기록 검산·실행 입력·진행 표식처럼 JMeter 없이 검증 가능한 계약과,
실제 JMeter·격리 시험 서버가 있을 때만 수행하는 도구 검증을 구분한다.

실제 JMeter 검증은 M3_RUN_JMETER=1과 JMeter 실행 파일이 모두 있을 때만 실행한다.
도구가 없을 때 통과로 꾸미지 않고 skip 사유를 남긴다.
"""

import csv
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from threading import Lock, Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree

import pytest

from tests.scenario_03_performance import run_performance as rp


# ---------------------------------------------------------------------------
# 공통 보조: 기록 한 건과 실행 산출물
# ---------------------------------------------------------------------------


def make_call(thread="M3 Threads 1-1", seq=1, elapsed_ms=3.0,
              outcome="success", unknown=False, phase="measure",
              start_ns=0, decision_ns=None, return_ns=None, late=False,
              code="200", affected=None, error="", run_id="run-1"):
    """정규화 호출 기록 한 건을 만든다. 시각 단위는 나노초, 지연은 밀리초 입력."""
    if start_ns == 0:
        start_ns = 1_000_000_000 + (seq - 1) * 10_000_000
    elapsed_ns = int(round(elapsed_ms * 1_000_000))
    if decision_ns is None:
        decision_ns = start_ns + elapsed_ns
    if return_ns is None:
        return_ns = decision_ns
    return {
        "run_id": run_id,
        "phase": phase,
        "thread_name": thread,
        "request_sequence": seq,
        "start_ns": start_ns,
        "deadline_ns": start_ns + 5_000_000_000,
        "decision_ns": decision_ns,
        "observed_decision_ns": decision_ns,
        "return_ns": return_ns,
        "outcome": outcome,
        "processing_unknown": unknown,
        "client_elapsed_ns": elapsed_ns if outcome == "success" else None,
        "observed_elapsed_ns": elapsed_ns,
        "late_return": late,
        "http_code": code,
        "jdbc_affected": affected,
        "error": error,
    }


def write_calls(path, calls):
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rp.CALL_FIELDS)
        writer.writeheader()
        for call in calls:
            writer.writerow({name: call.get(name, "") for name in rp.CALL_FIELDS})


def write_jtl(path, calls, duplicate=None, omit=None, label="m3-http-incr"):
    """정규화 기록과 대응하는 JTL 측정 행을 만든다. omit은 (thread, seq)."""
    columns = [
        "timeStamp", "elapsed", "label", "responseCode", "responseMessage",
        "threadName", "dataType", "success", "failureMessage", "bytes",
        "sentBytes", "grpThreads", "allThreads", "URL", "Latency",
        "IdleTime", "Connect",
    ] + list(rp.SAMPLE_VARIABLES)
    rows = []
    for call in calls:
        key = (call["thread_name"], int(call["request_sequence"]))
        if omit is not None and key == tuple(omit):
            continue
        rows.append(jtl_row(call, label=label))
    if duplicate is not None:
        rows.append(jtl_row(duplicate, label=label))
    for index in range(1, rp.WARMUP_TOTAL + 1):
        warmup = make_call(thread=f"warmup-thread-{index % 10 + 1}",
                           seq=index, phase="warmup")
        rows.append(jtl_row(warmup, label="m3-warmup-http-incr"))
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def jtl_row(call, label="m3-http-incr"):
    row = {
        "timeStamp": 1_700_000_000_000,
        "elapsed": int(round((call.get("observed_elapsed_ns") or 0) / 1_000_000)),
        "label": label,
        "responseCode": call.get("http_code") or "",
        "responseMessage": "OK",
        "threadName": call["thread_name"],
        "dataType": "text",
        "success": "true" if call["outcome"] == "success" else "false",
        "failureMessage": call.get("error") or "",
        "bytes": 30,
        "sentBytes": 40,
        "grpThreads": 1,
        "allThreads": 1,
        "URL": "http://127.0.0.1/kv/incr",
        "Latency": 1,
        "IdleTime": 0,
        "Connect": 0,
    }
    row.update({
        "m3_run_id": call["run_id"],
        "m3_phase": call["phase"],
        "m3_thread_name": call["thread_name"],
        "m3_request_sequence": call["request_sequence"],
        "m3_start_ns": call["start_ns"],
        "m3_deadline_ns": call["deadline_ns"],
        "m3_decision_ns": call["decision_ns"],
        "m3_return_ns": call["return_ns"],
        "m3_outcome": call["outcome"],
        "m3_processing_unknown": "true" if call["processing_unknown"] else "false",
        "m3_client_elapsed_ns": call["client_elapsed_ns"] or "",
        "m3_observed_elapsed_ns": call["observed_elapsed_ns"],
        "m3_http_code": call.get("http_code") or "",
        "m3_jdbc_affected": "" if call.get("jdbc_affected") is None else call["jdbc_affected"],
        "m3_late_return": "true" if call["late_return"] else "false",
        "m3_error": call.get("error") or "",
    })
    return row


def make_completion(calls, planned_n, final_value, *,
                    concurrency=1, target="http", normal=True,
                    warmup_expected=100, warmup_completed=100,
                    stop_reason="", limit_hit=False, prep_failed=False):
    success = sum(1 for call in calls if call["outcome"] == "success")
    rejected = sum(1 for call in calls if call["outcome"] == "rejected")
    timeout = sum(1 for call in calls if call["outcome"] == "timeout")
    other = sum(1 for call in calls if call["outcome"] == "other_failure")
    unfinished = sum(1 for call in calls if call["outcome"] == "unfinished")
    unknown = sum(1 for call in calls if call["processing_unknown"])
    decided = sum(1 for call in calls if call["outcome"])
    starts = [call["start_ns"] for call in calls]
    ends = [call["decision_ns"] for call in calls if call["decision_ns"]]
    return {
        "normal": normal,
        "run_id": calls[0]["run_id"] if calls else "run-1",
        "target": target,
        "concurrency": concurrency,
        "planned_n": planned_n,
        "attempts": len(calls),
        "decided": decided,
        "success": success,
        "rejected": rejected,
        "timeout": timeout,
        "other_failure": other,
        "unfinished": unfinished,
        "processing_unknown": unknown,
        "max_in_flight": 1,
        "warmup_expected": warmup_expected,
        "warmup_completed": warmup_completed,
        "warmup_per_thread": warmup_expected // concurrency,
        "start_ns": min(starts) if starts else None,
        "end_ns": max(ends) if ends else None,
        "final_read_done": not prep_failed,
        "final_read_ok": normal,
        "final_value": final_value,
        "stop_reason": stop_reason,
        "limit_hit": limit_hit,
        "prep_failed": prep_failed,
        "records_written": len(calls),
        "measurement_complete": True,
    }


def make_run_dir(tmp_path, calls, completion, jtl_kwargs=None, name="run-1"):
    run_dir = Path(tmp_path) / name
    run_dir.mkdir()
    write_calls(run_dir / "calls.csv", calls)
    write_jtl(run_dir / "result.jtl", calls, **(jtl_kwargs or {}))
    (run_dir / "completion.json").write_text(
        json.dumps(completion, ensure_ascii=False), encoding="utf-8"
    )
    return run_dir


# ---------------------------------------------------------------------------
# M3-11·12: 성공 지연 백분위수와 표본 없음
# ---------------------------------------------------------------------------


def test_m3_11_percentile_uses_success_samples_only():
    successes = [float(value) for value in range(1, 101)]
    assert rp.percentile(successes, 0.50) == pytest.approx(50.5)
    assert rp.percentile(successes, 0.95) == pytest.approx(95.05)
    assert rp.percentile(successes, 0.99) == pytest.approx(99.01)

    calls = [make_call(seq=index, elapsed_ms=float(index))
             for index in range(1, 101)]
    calls.append(make_call(seq=101, elapsed_ms=1000.0,
                           outcome="timeout", unknown=True))
    metrics = rp.round_metrics(calls, planned_n=101, final_value=100,
                               duration_ns=1_000_000_000)
    assert metrics["success_count"] == 100
    assert metrics["timeout_count"] == 1
    assert metrics["p50_ms"] == pytest.approx(50.5)
    assert metrics["p95_ms"] == pytest.approx(95.05)
    assert metrics["p99_ms"] == pytest.approx(99.01)
    assert metrics["passed"] is False


def test_m3_12_no_success_and_single_success_latency():
    empty = rp.round_metrics([], planned_n=1, final_value=None,
                             duration_ns=1_000_000_000)
    assert empty["p50_ms"] is None
    assert empty["p95_ms"] is None
    assert empty["p99_ms"] is None
    assert empty["success_rate"] is None
    assert empty["throughput_per_second"] == pytest.approx(0.0)

    one = rp.round_metrics([make_call(elapsed_ms=3.0)], planned_n=1,
                           final_value=1, duration_ns=1_000_000_000)
    assert one["p50_ms"] == 3.0
    assert one["p95_ms"] == 3.0
    assert one["p99_ms"] == 3.0


# ---------------------------------------------------------------------------
# M3-13: 회차 집계와 실패 회차 포함 시 정상 5회 요약 거절
# ---------------------------------------------------------------------------


def round_metric(value, passed=True):
    return {"p50_ms": value, "passed": passed, "success_count": 1,
            "attempts": 1, "planned_n": 1}


def test_m3_13_five_round_summary_and_failed_round_refusal():
    rounds = [round_metric(value) for value in (10, 20, 30, 40, 50)]
    summary = rp.summarize_rounds(rounds)
    assert summary["p50_ms"] == {"median": 30, "min": 10, "max": 50}
    assert summary["rounds"] == 5

    failed = rounds[:4] + [round_metric(50, passed=False)]
    with pytest.raises(rp.IncompleteResultError) as caught:
        rp.summarize_rounds(failed)
    assert "5회" in str(caught.value)
    with pytest.raises(rp.IncompleteResultError):
        rp.summarize_rounds(rounds[:4])


# ---------------------------------------------------------------------------
# M3-16: JTL·정규화 기록 누락/중복이면 기록 불완전
# ---------------------------------------------------------------------------


def test_m3_16_matching_artifacts_pass(tmp_path):
    calls = [make_call(seq=1), make_call(seq=2)]
    completion = make_completion(calls, planned_n=2, final_value=2)
    run_dir = make_run_dir(tmp_path, calls, completion)
    result = rp.validate_run_artifacts(run_dir, expected={
        "run_id": "run-1", "planned_n": 2, "concurrency": 1, "target": "http",
    })
    assert result["attempts"] == 2
    assert result["success"] == 2
    assert result["final_value"] == 2


@pytest.mark.parametrize("mode", ["omit", "duplicate"])
def test_m3_16_missing_or_duplicate_row_is_incomplete(tmp_path, mode):
    calls = [make_call(seq=1), make_call(seq=2)]
    completion = make_completion(calls, planned_n=2, final_value=2)
    kwargs = {"omit": ("M3 Threads 1-1", 2)} if mode == "omit" else {
        "duplicate": make_call(seq=2)
    }
    run_dir = make_run_dir(tmp_path, calls, completion, jtl_kwargs=kwargs)
    with pytest.raises(rp.IncompleteResultError):
        rp.validate_run_artifacts(run_dir, expected={
            "run_id": "run-1", "planned_n": 2, "concurrency": 1,
            "target": "http",
        })


def test_m3_16_missing_completion_keeps_unknown_count(tmp_path):
    calls = [make_call(seq=1), make_call(seq=2)]
    run_dir = Path(tmp_path) / "run-1"
    run_dir.mkdir()
    write_calls(run_dir / "calls.csv", calls)
    write_jtl(run_dir / "result.jtl", calls)
    with pytest.raises(rp.IncompleteResultError) as caught:
        rp.validate_run_artifacts(run_dir, expected={
            "run_id": "run-1", "planned_n": 2, "concurrency": 1,
            "target": "http",
        })
    # 실제 시도 수를 모를 때 추정하여 채우지 않는다.
    assert caught.value.details.get("attempts") is None


# ---------------------------------------------------------------------------
# M3-14·15·18 보조: 기록/설정 정적 계약
# ---------------------------------------------------------------------------


def test_sample_variables_cover_required_call_fields():
    required = {
        "run_id", "phase", "thread_name", "request_sequence", "start_ns",
        "deadline_ns", "decision_ns", "return_ns", "outcome",
        "processing_unknown", "client_elapsed_ns",
    }
    assert required <= set(rp.CALL_FIELDS)
    assert required <= {name.removeprefix("m3_") for name in rp.SAMPLE_VARIABLES}
    assert "m3_observed_elapsed_ns" in rp.SAMPLE_VARIABLES


# ---------------------------------------------------------------------------
# M3-17: 진행 표식 fail closed
# ---------------------------------------------------------------------------


def test_m3_17_pending_marker_blocks_next_batch(tmp_path):
    store = rp.RunStateStore(tmp_path)
    store.begin("run-1")
    assert store.pending() == ["run-1"]
    with pytest.raises(rp.PendingRunError):
        store.begin("run-2")
    store.fail("run-1", "late response unknown")
    assert store.pending() == ["run-1"]
    with pytest.raises(rp.PendingRunError):
        store.begin("run-2")


def test_m3_17_completed_marker_allows_next_batch(tmp_path):
    store = rp.RunStateStore(tmp_path)
    store.begin("run-1")
    store.complete("run-1")
    assert store.pending() == []
    store.begin("run-2")
    assert store.pending() == ["run-2"]


def test_run_single_writes_marker_before_launch(tmp_path):
    observed = {}

    def fake_runner(settings, run_dir):
        observed["pending"] = rp.RunStateStore(settings["state_dir"]).pending()
        return {"returncode": 1, "timed_out": False}

    with pytest.raises(rp.IncompleteResultError):
        rp.run_single(base_settings(tmp_path), target="http", concurrency=1,
                      round_index=1, runner=fake_runner)
    assert observed["pending"] == [rp_run_id(tmp_path)]


def rp_run_id(tmp_path):
    # run_single의 실행 식별자는 결과 폴더에서 확인한다.
    dirs = [path.name for path in (Path(tmp_path) / "runs").iterdir()
            if path.is_dir() and not path.name.startswith(".")]
    assert len(dirs) == 1
    return dirs[0]


# ---------------------------------------------------------------------------
# 입력 계약: 누락 거부, 기본값으로 본 측정 수치 확정 금지
# ---------------------------------------------------------------------------


def base_settings(tmp_path):
    return {
        "results_dir": str(Path(tmp_path) / "runs"),
        "state_dir": str(Path(tmp_path) / "state"),
        "run_id": "run-1",
        "n": 1000,
        "request_timeout_ms": 5000,
        "transport_timeout_ms": 5000,
        "total_limit_ms": 120000,
        "prep_limit_ms": 60000,
        "finish_limit_ms": 30000,
        "environment": {
            "jmeter_version": "5.6.3", "jdk_version": "17.0.21",
            "mysql_version": "8.4", "connector_version": "9.2.0",
            "python_version": "3.14.10", "flask_version": "3.1.3",
            "werkzeug_version": "3.1.9",
        },
        "http": {"host": "127.0.0.1", "port": 5000, "scheme": "http"},
        "jdbc": {"url": "jdbc:mysql://127.0.0.1:3306/bench",
                 "user": "benchmark", "password": "not-in-argv"},
        "jmeter_bin": "/opt/jmeter/bin/jmeter",
        "java_home": "/opt/jdk",
        "jmx": "/abs/counter.jmx",
        "properties": "/abs/jmeter.properties",
    }


def test_campaign_requires_every_measurement_input(tmp_path):
    with pytest.raises(rp.InputError) as caught:
        rp.validate_baseline_inputs({})
    message = str(caught.value)
    for name in ("n", "request_timeout_ms", "total_limit_ms",
                 "prep_limit_ms", "finish_limit_ms", "environment",
                 "results_dir", "state_dir"):
        assert name in message


def test_pilot_refuses_non_candidate_n(tmp_path):
    settings = base_settings(tmp_path)
    settings["n"] = 2000
    with pytest.raises(rp.InputError):
        rp.validate_pilot_inputs(settings)
    for n in (1000, 10000):
        settings["n"] = n
        assert rp.validate_pilot_inputs(settings)["n"] == n


def test_campaign_refuses_n_outside_agreed_range(tmp_path):
    settings = base_settings(tmp_path)
    for n in (999, 10001):
        settings["n"] = n
        with pytest.raises(rp.InputError):
            rp.validate_baseline_inputs(settings)
    settings["n"] = 5000
    assert rp.validate_baseline_inputs(settings)["n"] == 5000


def test_missing_jdbc_secret_is_refused_without_default(tmp_path):
    settings = base_settings(tmp_path)
    settings["jdbc"] = {"url": "jdbc:mysql://127.0.0.1/bench",
                        "user": "benchmark"}
    with pytest.raises(rp.InputError):
        rp.validate_baseline_inputs(settings)


def test_credentials_never_appear_in_jmeter_argv(tmp_path):
    settings = base_settings(tmp_path)
    props = Path(tmp_path) / "run.properties"
    rp.write_runtime_properties(props, settings, run_dir=Path(tmp_path))
    assert props.stat().st_mode & 0o777 == 0o600
    command = rp.build_jmeter_command(settings, jmx_path=Path("/x/counter.jmx"),
                                     props_path=props,
                                     jtl_path=Path("/x/result.jtl"),
                                     log_path=Path("/x/jmeter.log"))
    joined = " ".join(command)
    assert "not-in-argv" not in joined
    assert "-n" in command and "-t" in command and "-q" in command
    assert "-l" in command and "-j" in command


# ---------------------------------------------------------------------------
# 실제 JMeter 도구 검증 (M3-01·02·03·06·09·15·18)
# ---------------------------------------------------------------------------


JMETER = os.environ.get("M3_JMETER_BIN", "")
JAVA_HOME = os.environ.get("M3_JAVA_HOME", "")
RUN_JMETER = os.environ.get("M3_RUN_JMETER") == "1"


def jmeter_available():
    if not RUN_JMETER:
        return False, "M3_RUN_JMETER=1 미지정"
    if not JMETER or not Path(JMETER).is_file():
        return False, "M3_JMETER_BIN 미지정 또는 파일 없음"
    if not JAVA_HOME or not Path(JAVA_HOME).is_dir():
        return False, "M3_JAVA_HOME 미지정 또는 디렉터리 없음"
    try:
        out = subprocess.run([JMETER, "--version"], capture_output=True, text=True,
                             timeout=60, env={**os.environ, "JAVA_HOME": JAVA_HOME})
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"JMeter 실행 실패: {error}"
    return (out.returncode == 0 and "5.6.3" in out.stdout + out.stderr,
            (out.stdout + out.stderr)[-300:])


requires_jmeter = pytest.mark.skipif(
    not jmeter_available()[0], reason=jmeter_available()[1]
)


class FakeCounter:
    """격리된 시험 대상. 성공 응답과 지연·연결 종료 같은 실패 상황을 만든다."""

    def __init__(self, *, incr_delay_ms=0, incr_delay_after=0,
                 incr_delay_ms_after=0, incr_status=200, incr_body=None,
                 close_connection=False, prep_delay_ms=0, final_delay_ms=0,
                 block_body_ms=0, warmup_block_at=0, warmup_block_ms=0,
                 incr_block_after=0, incr_block_ms=0, bad_after=0,
                 measurement_body_delay_ms=0, final_value_override=None):
        self.value = 0
        self.lock = Lock()
        self.incr_count = 0
        self.connections = 0
        self.incr_delay_ms = incr_delay_ms
        self.incr_delay_after = incr_delay_after
        self.incr_delay_ms_after = incr_delay_ms_after
        self.incr_status = incr_status
        self.incr_body = incr_body
        self.close_connection = close_connection
        self.prep_delay_ms = prep_delay_ms
        self.final_delay_ms = final_delay_ms
        self.block_body_ms = block_body_ms
        self.warmup_block_at = warmup_block_at
        self.warmup_block_ms = warmup_block_ms
        self.incr_block_after = incr_block_after
        self.incr_block_ms = incr_block_ms
        self.bad_after = bad_after
        self.measurement_body_delay_ms = measurement_body_delay_ms
        self.final_value_override = final_value_override
        self.warmup_total_for_override = 100
        self.after_count = 0
        self.after_incr_count = None
        self.after_ns = None
        self.last_measurement_response_ns = None
        self.requests = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                with server.lock:
                    server.connections += 1

            def _read_json(self):
                length = int(self.headers.get("Content-Length", "0"))
                return json.loads(self.rfile.read(length) or b"{}")

            def _send(self, status, body, body_delay_ms=0):
                data = body.encode("utf-8") if isinstance(body, str) else body
                import time
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if server.close_connection:
                    self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.flush()
                if server.block_body_ms:
                    time.sleep(server.block_body_ms / 1000)
                if body_delay_ms:
                    time.sleep(body_delay_ms / 1000)
                self.wfile.write(data)

            def do_PUT(self):
                self._read_json()
                import time
                if server.prep_delay_ms:
                    time.sleep(server.prep_delay_ms / 1000)
                with server.lock:
                    server.value = 0
                self._send(200, '{"ok":true}')

            def do_GET(self):
                import time
                parsed = urlparse(self.path)
                if parsed.path == "/after":
                    with server.lock:
                        server.after_count += 1
                        server.after_incr_count = server.incr_count
                        server.after_ns = time.monotonic_ns()
                    self._send(200, '{"ok":true}')
                    return
                if server.final_delay_ms:
                    time.sleep(server.final_delay_ms / 1000)
                query = parse_qs(parsed.query)
                key = query.get("key", [""])[0]
                with server.lock:
                    value = server.value
                    measured = server.incr_count > server.warmup_total_for_override
                if key == "counter:one":
                    if measured and server.final_value_override is not None:
                        value = server.final_value_override
                    self._send(200, json.dumps({"found": True, "value": value}))
                else:
                    self._send(200, '{"found":false}')

            def do_POST(self):
                import time
                self._read_json()
                with server.lock:
                    server.incr_count += 1
                    index = server.incr_count
                    target = server.value
                    if index == server.warmup_block_at and server.warmup_block_ms:
                        block_ms = server.warmup_block_ms
                    elif server.incr_block_after and index > server.incr_block_after:
                        block_ms = server.incr_block_ms
                    elif server.incr_delay_after and index > server.incr_delay_after:
                        block_ms = server.incr_delay_ms_after
                    else:
                        block_ms = server.incr_delay_ms
                if block_ms:
                    time.sleep(block_ms / 1000)
                if server.incr_body is not None and index > server.bad_after:
                    self._send(server.incr_status, server.incr_body)
                    if index > 100:
                        with server.lock:
                            server.last_measurement_response_ns = time.monotonic_ns()
                    return
                with server.lock:
                    server.value += 1
                    value = server.value
                    target = value
                delay = (server.measurement_body_delay_ms
                         if index > 100 else 0)
                self._send(200, json.dumps({"ok": True, "value": target}),
                           body_delay_ms=delay)
                if index > 100:
                    with server.lock:
                        server.last_measurement_response_ns = time.monotonic_ns()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.httpd.server_address

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=10)


@pytest.fixture
def fake_counter():
    server = FakeCounter()
    try:
        yield server
    finally:
        server.close()


def run_fake_jmeter(tmp_path, server, *, c=1, n=7, request_timeout_ms=2000,
                    total_limit_ms=60000, prep_limit_ms=30000,
                    finish_limit_ms=30000, transport_timeout_ms=None,
                    extra_props=None):
    """격리 HTTP 대상에 실제 JMeter를 실행하고 산출물 경로를 돌려준다."""
    run_id = f"m3-http-c{c}-n{n}"
    run_dir = Path(tmp_path) / run_id
    run_dir.mkdir()
    settings = {
        "results_dir": str(tmp_path),
        "run_id": run_id,
        "n": n,
        "request_timeout_ms": request_timeout_ms,
        "transport_timeout_ms": transport_timeout_ms or request_timeout_ms,
        "total_limit_ms": total_limit_ms,
        "prep_limit_ms": prep_limit_ms,
        "finish_limit_ms": finish_limit_ms,
        "environment": {"jmeter_version": "5.6.3"},
        "http": {"host": server.host, "port": server.port, "scheme": "http"},
        "jdbc": {"url": "jdbc:mysql://127.0.0.1:1/unused",
                 "user": "unused", "password": "unused"},
        "jmeter_bin": JMETER,
        "java_home": JAVA_HOME,
        "jmx": str(Path(__file__).with_name("counter.jmx")),
        "properties": str(Path(__file__).with_name("jmeter.properties")),
    }
    props = run_dir / "run.properties"
    rp.write_runtime_properties(props, settings, run_dir=run_dir,
                                target="http", concurrency=c, round_index=1)
    if extra_props:
        with props.open("a", encoding="utf-8") as handle:
            for name, value in extra_props.items():
                handle.write(f"{name}={value}\n")
    command = rp.build_jmeter_command(
        settings, jmx_path=Path(settings["jmx"]), props_path=props,
        jtl_path=run_dir / "result.jtl", log_path=run_dir / "jmeter.log",
        cwd=Path(__file__).parent,
    )
    process = subprocess.run(command, cwd=Path(__file__).parent,
                             env={**os.environ, "JAVA_HOME": JAVA_HOME},
                             capture_output=True, text=True,
                             timeout=180)
    (run_dir / "stdout.log").write_text(process.stdout + process.stderr,
                                        encoding="utf-8")
    return run_dir, process, server


def measurement_rows(run_dir):
    labels = set(rp.MEASUREMENT_LABELS)
    with (Path(run_dir) / "result.jtl").open(encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle)
                if row.get("label") in labels and row.get("m3_phase") == "measure"]


def completion_of(run_dir):
    path = Path(run_dir) / "completion.json"
    assert path.is_file(), (run_dir / "stdout.log").read_text(
        encoding="utf-8")[-3000:]
    return json.loads(path.read_text(encoding="utf-8"))


@requires_jmeter
def test_m3_01_single_thread_exact_global_quota(tmp_path, fake_counter):
    run_dir, process, server = run_fake_jmeter(tmp_path, fake_counter, c=1, n=7)
    assert process.returncode == 0, (run_dir / "stdout.log").read_text()[-3000:]
    completion = completion_of(run_dir)
    assert completion["attempts"] == 7
    assert completion["success"] == 7
    assert completion["max_in_flight"] == 1
    assert completion["warmup_completed"] == 100
    assert server.incr_count == 107
    result = rp.validate_run_artifacts(run_dir, expected={
        "run_id": "m3-http-c1-n7", "planned_n": 7, "concurrency": 1,
        "target": "http",
    })
    assert result["success"] == 7


@requires_jmeter
def test_m3_02_global_quota_with_many_threads(tmp_path, fake_counter):
    run_dir, process, server = run_fake_jmeter(tmp_path, fake_counter,
                                               c=10, n=1003,
                                               request_timeout_ms=5000,
                                               total_limit_ms=120000)
    assert process.returncode == 0, (run_dir / "stdout.log").read_text()[-3000:]
    completion = completion_of(run_dir)
    assert completion["attempts"] == 1003
    assert completion["success"] == 1003
    assert completion["max_in_flight"] <= 10
    assert server.incr_count == 1103
    rp.validate_run_artifacts(run_dir, expected={
        "run_id": "m3-http-c10-n1003", "planned_n": 1003,
        "concurrency": 10, "target": "http",
    })


@requires_jmeter
@pytest.mark.parametrize("c", [1, 10, 50, 100])
def test_m3_03_warmup_total_and_start_after_reset(tmp_path, fake_counter, c):
    run_dir, process, _ = run_fake_jmeter(tmp_path, fake_counter, c=c, n=20,
                                          request_timeout_ms=5000,
                                          total_limit_ms=120000)
    assert process.returncode == 0, (run_dir / "stdout.log").read_text()[-3000:]
    completion = completion_of(run_dir)
    assert completion["warmup_completed"] == 100
    assert completion["warmup_per_thread"] == 100 // c
    assert completion["final_value"] == 20
    rows = list(csv.DictReader((run_dir / "result.jtl").open(encoding="utf-8")))
    warmup = [row for row in rows if row.get("label") in rp.WARMUP_LABELS
              and row.get("m3_phase") == "warmup"]
    measure = measurement_rows(run_dir)
    assert len(warmup) == 100
    assert len(measure) == 20


@requires_jmeter
def test_m3_06_logical_deadline_keeps_late_success_failed(tmp_path):
    server = FakeCounter(incr_delay_after=100, incr_delay_ms_after=2000)
    try:
        run_dir, process, server = run_fake_jmeter(
            tmp_path, server, c=1, n=1, request_timeout_ms=1000,
            total_limit_ms=10000, prep_limit_ms=30000,
            finish_limit_ms=30000, transport_timeout_ms=5000)
    finally:
        server.close()
    completion = completion_of(run_dir)
    assert completion["success"] == 0
    assert completion["timeout"] == 1
    assert completion["attempts"] == 1
    assert completion["normal"] is False
    assert completion["stop_reason"]
    measure = measurement_rows(run_dir)
    assert len(measure) == 1
    assert measure[0]["success"] == "false"
    assert measure[0]["m3_outcome"] == "timeout"


@requires_jmeter
@pytest.mark.parametrize("status,body,expected_outcome,unknown", [
    (400, '{"error":"INVALID_INPUT"}', "rejected", "false"),
    (500, '{"error":"INTERNAL_SERVER_ERROR"}', "other_failure", "true"),
    (200, '{"ok":"true","value":1}', "other_failure", "true"),
])
def test_m3_09_bad_response_is_not_success(tmp_path, status, body,
                                           expected_outcome, unknown):
    server = FakeCounter(incr_delay_after=100, incr_status=status,
                         incr_body=body, bad_after=100)
    try:
        run_dir, _, _ = run_fake_jmeter(tmp_path, server, c=1, n=1,
                                        request_timeout_ms=2000)
    finally:
        server.close()
    completion = completion_of(run_dir)
    assert completion["success"] == 0
    assert completion["normal"] is False
    measure = measurement_rows(run_dir)
    assert len(measure) == 1
    assert measure[0]["m3_outcome"] == expected_outcome
    assert measure[0]["m3_processing_unknown"] == unknown


@requires_jmeter
def test_m3_18_prep_and_final_delay_not_in_measurement(tmp_path):
    server = FakeCounter(prep_delay_ms=1000, final_delay_ms=1000)
    try:
        run_dir, _, _ = run_fake_jmeter(tmp_path, server, c=1, n=7,
                                        request_timeout_ms=5000)
    finally:
        server.close()
    completion = completion_of(run_dir)
    assert completion["success"] == 7
    duration_ms = (completion["end_ns"] - completion["start_ns"]) / 1e6
    assert duration_ms < 1000
    measure = measurement_rows(run_dir)
    assert len(measure) == 7
    assert all(float(row["elapsed"]) < 1000 for row in measure)


@requires_jmeter
def test_m3_15_closed_connections_are_counted(tmp_path):
    server = FakeCounter(close_connection=True)
    try:
        run_dir, _, _ = run_fake_jmeter(tmp_path, server, c=1, n=7,
                                        request_timeout_ms=5000)
    finally:
        server.close()
    completion = completion_of(run_dir)
    assert completion["success"] == 7
    # 워밍업 100건 + 측정 7건 + 준비/초기화/최종 조회가 새 연결을 만든다.
    assert server.connections >= 107


@requires_jmeter
def test_m3_04_blocked_warmup_starts_no_measurement(tmp_path):
    server = FakeCounter(warmup_block_at=50, warmup_block_ms=5000)
    try:
        run_dir, _, _ = run_fake_jmeter(tmp_path, server, c=10, n=20,
                                        request_timeout_ms=2000,
                                        prep_limit_ms=2000,
                                        total_limit_ms=30000)
    finally:
        server.close()
    completion = completion_of(run_dir)
    assert completion["prep_failed"] is True
    assert completion["normal"] is False
    assert completion["attempts"] == 0
    assert completion["start_ns"] is None
    assert measurement_rows(run_dir) == []


@requires_jmeter
def test_m3_05_delayed_body_is_not_success_before_body(tmp_path):
    server = FakeCounter(measurement_body_delay_ms=1200)
    try:
        run_dir, _, _ = run_fake_jmeter(tmp_path, server, c=1, n=1,
                                        request_timeout_ms=5000)
    finally:
        server.close()
    completion = completion_of(run_dir)
    assert completion["success"] == 1
    rows = measurement_rows(run_dir)
    assert len(rows) == 1
    # 헤더만 먼저 받고 본문이 도착하기 전에 성공으로 결정했다면 1.2초보다 짧다.
    assert float(rows[0]["elapsed"]) >= 1100


@requires_jmeter
def test_m3_08_total_limit_marks_unfinished_without_duplicates(tmp_path):
    server = FakeCounter(incr_block_after=100, incr_block_ms=4000)
    try:
        run_dir, _, _ = run_fake_jmeter(tmp_path, server, c=10, n=100,
                                        request_timeout_ms=3000,
                                        transport_timeout_ms=3000,
                                        total_limit_ms=1000,
                                        finish_limit_ms=10000)
    finally:
        server.close()
    completion = completion_of(run_dir)
    assert completion["limit_hit"] is True
    assert completion["normal"] is False
    assert completion["attempts"] < 100
    assert completion["unfinished"] >= 1
    assert completion["attempts"] == completion["unfinished"]
    calls = list(csv.DictReader((run_dir / "calls.csv").open(encoding="utf-8")))
    keys = [(row["thread_name"], row["request_sequence"]) for row in calls]
    assert len(keys) == len(set(keys))
    assert all(row["outcome"] == "unfinished" for row in calls)


# ---------------------------------------------------------------------------
# 산출물 구조: JMX·속성·SQL·실행 담당의 정적 계약
# ---------------------------------------------------------------------------


def jmx_root():
    return ElementTree.parse(Path(__file__).with_name("counter.jmx")).getroot()


def test_jmx_uses_setup_main_teardown_and_global_quota():
    root = jmx_root()
    tags = [element.tag for element in root.iter()]
    assert tags.count("SetupThreadGroup") == 1
    assert tags.count("ThreadGroup") == 1
    assert tags.count("PostThreadGroup") == 1
    controllers = [element for element in root.iter("ThroughputController")]
    assert len(controllers) == 1
    controller = controllers[0]
    props = {child.get("name"): (child.text or "")
             for child in controller if child.tag.endswith("Prop")}
    assert props["ThroughputController.style"] == "0"
    assert props["ThroughputController.perThread"] == "false"
    assert props["ThroughputController.maxThroughput"] == "${__P(m3.n)}"
    assert not list(root.iter("SynchronizingTimer"))


def test_jmx_measurement_controller_has_one_sampler_per_active_target():
    root = jmx_root()
    controller = next(root.iter("ThroughputController"))
    # hashTree 안에서 ThroughputController 다음의 형제 hashTree가 자식 범위다.
    parent_tree = None
    for tree in root.iter("hashTree"):
        children = list(tree)
        if controller in children:
            parent_tree = tree
            break
    assert parent_tree is not None
    index = list(parent_tree).index(controller)
    subtree = list(parent_tree)[index + 1]
    assert subtree.tag == "hashTree"
    samplers = [element for element in subtree.iter()
                if element.tag in ("HTTPSamplerProxy", "JDBCSampler")]
    assert sorted(element.get("testname") for element in samplers) == [
        "m3-http-incr", "m3-jdbc-incr",
    ]
    assert not list(subtree.iter("Timer"))


def test_jmx_http_and_jdbc_paths_match_contract():
    root = jmx_root()
    http = next(root.iter("HTTPSamplerProxy"))
    http_props = {child.get("name"): (child.text or "")
                  for child in http if child.tag.endswith("Prop")}
    assert http_props["HTTPSampler.implementation"] == "HttpClient4"
    assert http_props["HTTPSampler.follow_redirects"] == "false"
    assert http_props["HTTPSampler.auto_redirects"] == "false"
    data_source = next(root.iter("JDBCDataSource"))
    jdbc_props = {child.get("name"): (child.text or "")
                  for child in data_source if child.tag.endswith("Prop")}
    assert jdbc_props["autocommit"] == "true"
    assert jdbc_props["poolMax"] == "0"
    assert jdbc_props["dbUrl"] == "${__P(m3.jdbc.url)}"


def test_jmx_scripts_are_file_based_and_have_stages():
    root = jmx_root()
    elements = [element for element in root.iter()
                if element.tag in ("JSR223Sampler", "JSR223PreProcessor",
                                   "JSR223PostProcessor")]
    assert elements
    for element in elements:
        props = {child.get("name"): (child.text or "")
                 for child in element if child.tag.endswith("Prop")}
        assert props["filename"] == "counter.groovy"
        assert props["parameters"]
        assert props["script"] == ""


def test_properties_disable_retry_and_export_measurement_variables():
    path = Path(__file__).with_name("jmeter.properties")
    text = path.read_text(encoding="utf-8")
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip()
    assert values["httpclient4.retrycount"] == "0"
    assert values["httpclient4.request_sent_retry_enabled"] == "false"
    assert values["jmeter.save.saveservice.autoflush"] == "false"
    assert values["jmeter.save.saveservice.output_format"] == "csv"
    exported = set(values["sample_variables"].split(","))
    assert set(rp.SAMPLE_VARIABLES) <= exported


def test_mysql_setup_only_creates_test_table():
    text = Path(__file__).with_name("mysql_setup.sql").read_text(encoding="utf-8")
    statements = "\n".join(line for line in text.splitlines()
                            if not line.strip().startswith("--"))
    assert "CREATE TABLE benchmark_counter" in statements
    assert "counter_key VARBINARY(256) PRIMARY KEY" in text
    assert "value BIGINT NOT NULL" in text
    for forbidden in ("DROP TABLE", "TRUNCATE", "DELETE FROM", "ALTER TABLE"):
        assert forbidden not in statements.upper()


def test_groovy_uses_high_resolution_clock_and_watchdog():
    text = Path(__file__).with_name("counter.groovy").read_text(encoding="utf-8")
    assert "System.nanoTime()" in text
    assert "ScheduledThreadPoolExecutor" in text
    assert "JMeterStopThreadException" in text
    assert "processing_unknown" in text
    assert "writeAtomic" in text


def test_orchestrator_does_not_generate_load():
    text = Path(__file__).with_name("run_performance.py").read_text(encoding="utf-8")
    for forbidden in ("import http.client", "from http.client", "import urllib.request",
                      "from urllib.request", "import socket"):
        assert forbidden not in text
    assert "subprocess.run" in text


def test_cli_exposes_all_execution_paths(capsys):
    with pytest.raises(SystemExit):
        rp.main(["--help"])
    help_text = capsys.readouterr().out
    for command in ("unit", "test", "single", "pilot", "campaign"):
        assert command in help_text


@pytest.mark.skipif(not os.environ.get("M3_JDBC_PASSWORD_FILE"),
                    reason="비밀 파일 미지정")
def test_secret_never_written_into_repository_files():
    secret = Path(os.environ["M3_JDBC_PASSWORD_FILE"]).read_text(encoding="utf-8").strip()
    if not secret:
        pytest.skip("빈 비밀 파일")
    for path in Path(__file__).parent.iterdir():
        if path.is_file():
            assert secret not in path.read_text(encoding="utf-8", errors="ignore"), path.name


def test_run_single_keeps_secret_out_of_results_and_cleans_temporary_file(tmp_path):
    settings = base_settings(tmp_path)
    observed = {}

    def fake_runner(child_settings, run_dir):
        secret_path = Path(child_settings["runtime_properties_path"])
        observed["secret_path"] = secret_path
        observed["secret_mode"] = secret_path.stat().st_mode & 0o777
        observed["secret_text"] = secret_path.read_text(encoding="utf-8")
        observed["public_text"] = (Path(run_dir) / "run.properties").read_text(
            encoding="utf-8")
        return {"returncode": 1, "timed_out": False}

    with pytest.raises(rp.IncompleteResultError):
        rp.run_single(settings, target="http", concurrency=1, round_index=1,
                      runner=fake_runner)
    assert observed["secret_mode"] == 0o600
    assert "not-in-argv" in observed["secret_text"]
    assert "not-in-argv" not in observed["public_text"]
    assert "m3.jdbc.user" not in observed["public_text"]
    assert "m3.jdbc.url=None" not in observed["public_text"]
    assert not observed["secret_path"].exists()
    for path in (Path(settings["results_dir"]) / "run-1").iterdir():
        if path.is_file():
            assert "not-in-argv" not in path.read_text(encoding="utf-8",
                                                       errors="ignore")


def test_jdbc_auto_reconnect_is_refused(tmp_path):
    for suffix in ("?autoReconnect=true", "?maxReconnects=3"):
        settings = base_settings(tmp_path)
        settings["jdbc"]["url"] += suffix
        with pytest.raises(rp.InputError):
            rp.validate_baseline_inputs(settings)


def test_run_id_cannot_escape_results_directory(tmp_path):
    settings = base_settings(tmp_path)
    settings["run_id"] = "../escape"
    with pytest.raises(rp.InputError):
        rp.run_single(settings, target="http", concurrency=1, round_index=1,
                      runner=lambda *_: {"returncode": 1, "timed_out": False})


def jdbc_smoke_settings(tmp_path):
    return {
        "results_dir": str(tmp_path),
        "state_dir": str(Path(tmp_path) / "state"),
        "run_id": "jdbc-smoke",
        "n": 3,
        "request_timeout_ms": 5000,
        "transport_timeout_ms": 5000,
        "total_limit_ms": 60000,
        "prep_limit_ms": 30000,
        "finish_limit_ms": 30000,
        "environment": {
            "jmeter_version": "5.6.3", "jdk_version": "17",
            "mysql_version": "8.4", "connector_version": "9.2.0",
            "python_version": "3.14", "flask_version": "3.1",
            "werkzeug_version": "3.1",
        },
        "jdbc": {
            "url": os.environ.get("M3_JDBC_URL"),
            "user": os.environ.get("M3_JDBC_USER"),
            "password_file": os.environ.get("M3_JDBC_PASSWORD_FILE"),
        },
        "jmeter_bin": os.environ.get("M3_JMETER_BIN", ""),
        "java_home": os.environ.get("M3_JAVA_HOME", ""),
        "jmx": str(Path(__file__).with_name("counter.jmx")),
        "properties": str(Path(__file__).with_name("jmeter.properties")),
    }


@pytest.mark.skipif(os.environ.get("M3_RUN_JDBC") != "1",
                    reason="M3_RUN_JDBC=1과 시험 전용 MySQL 환경 필요")
def test_m3_jdbc_single_smoke_after_table_ready(tmp_path):
    """시험 전용 테이블이 준비된 뒤 JDBC 경로를 실제 JMeter로 확인한다.

    M3-07·10·14 전체를 대신하지 않는다. 여기서는 JDBC 성공·최종 수치만 본다.
    """
    settings = jdbc_smoke_settings(tmp_path)
    missing = [name for name in ("M3_JDBC_URL", "M3_JDBC_USER",
                                 "M3_JDBC_PASSWORD_FILE", "M3_JMETER_BIN",
                                 "M3_JAVA_HOME") if not os.environ.get(name)]
    if missing:
        pytest.skip("환경 변수 없음: " + ", ".join(missing))
    result = rp.run_single(settings, target="jdbc", concurrency=1, round_index=1)
    assert result["attempts"] == 3
    assert result["success"] == 3
    assert result["final_value"] == 3


@pytest.mark.parametrize("mode", ["calls_omit", "calls_duplicate"])
def test_m3_16_normalized_record_mismatch_is_incomplete(tmp_path, mode):
    calls = [make_call(seq=1), make_call(seq=2)]
    completion = make_completion(calls, planned_n=2, final_value=2)
    run_dir = make_run_dir(tmp_path, calls, completion)
    if mode == "calls_omit":
        write_calls(run_dir / "calls.csv", calls[:1])
    else:
        write_calls(run_dir / "calls.csv", [calls[0], calls[0]])
    with pytest.raises(rp.IncompleteResultError):
        rp.validate_run_artifacts(run_dir, expected={
            "run_id": "run-1", "planned_n": 2, "concurrency": 1,
            "target": "http",
        })


# ---------------------------------------------------------------------------
# 코드리뷰 회귀: 반환 관찰과 watchdog 경쟁
# ---------------------------------------------------------------------------


@requires_jmeter
def test_return_before_deadline_is_not_timeout_during_slow_classification(tmp_path):
    """반환 시각이 마감 전이면 분류가 늦어도 watchdog가 timeout으로 바꾸지 않는다."""
    server = FakeCounter()
    try:
        run_dir, _, _ = run_fake_jmeter(
            tmp_path, server, c=1, n=1, request_timeout_ms=1000,
            total_limit_ms=10000, prep_limit_ms=30000,
            finish_limit_ms=30000,
            extra_props={"m3.test.classification_delay_ms": "1500"})
    finally:
        server.close()
    completion = completion_of(run_dir)
    assert completion["success"] == 1
    assert completion["timeout"] == 0
    assert completion["normal"] is True
    rows = measurement_rows(run_dir)
    assert len(rows) == 1
    assert rows[0]["m3_outcome"] == "success"
    assert rows[0]["m3_late_return"] == "false"


@requires_jmeter
def test_late_return_after_deadline_stays_timeout_with_delayed_classification(tmp_path):
    """분류 지연이 있어도 마감 이후 반환은 여전히 실패다."""
    server = FakeCounter(incr_delay_after=100, incr_delay_ms_after=2000)
    try:
        run_dir, _, _ = run_fake_jmeter(
            tmp_path, server, c=1, n=1, request_timeout_ms=1000,
            total_limit_ms=10000, prep_limit_ms=30000,
            finish_limit_ms=30000, transport_timeout_ms=5000,
            extra_props={"m3.test.classification_delay_ms": "1500"})
    finally:
        server.close()
    completion = completion_of(run_dir)
    assert completion["success"] == 0
    assert completion["timeout"] == 1
    assert completion["normal"] is False


# ---------------------------------------------------------------------------
# 코드리뷰 회귀: 전체 한도 타이머 수명
# ---------------------------------------------------------------------------


@requires_jmeter
def test_total_deadline_blocks_new_calls_and_late_return_is_unfinished(tmp_path):
    """총한도 callback이 늦어도 총한도 이후 시작·반환을 성공으로 만들지 않는다."""
    server = FakeCounter(incr_delay_after=100, incr_delay_ms_after=2000)
    try:
        run_dir, _, _ = run_fake_jmeter(
            tmp_path, server, c=1, n=2, request_timeout_ms=5000,
            transport_timeout_ms=5000, total_limit_ms=1000,
            prep_limit_ms=30000, finish_limit_ms=30000,
            extra_props={"m3.test.total_limit_callback_delay_ms": "2500"})
    finally:
        server.close()
    completion = completion_of(run_dir)
    total_deadline = completion["start_ns"] + 1000 * 1_000_000
    calls = rp.read_calls_csv(run_dir / "calls.csv")
    assert completion["success"] == 0
    assert completion["attempts"] == 1
    assert completion["unfinished"] == 1
    assert completion["limit_hit"] is True
    assert completion["normal"] is False
    assert completion["end_ns"] == max(call["decision_ns"] for call in calls)
    assert len(calls) == 1
    call = calls[0]
    assert call["start_ns"] < total_deadline
    assert call["return_ns"] > total_deadline
    assert call["outcome"] == "unfinished"
    assert call["decision_ns"] == total_deadline
    assert call["decision_ns"] >= call["start_ns"]
    assert call["late_return"] is True
    assert call["processing_unknown"] is True


@requires_jmeter
def test_return_before_total_is_success_when_callback_fires_during_classification(
        tmp_path):
    """반환이 총한도 전이면 분류가 늦고 총한도 callback이 와도 성공으로 보존한다."""
    server = FakeCounter()
    try:
        run_dir, _, _ = run_fake_jmeter(
            tmp_path, server, c=1, n=1, request_timeout_ms=5000,
            total_limit_ms=500, prep_limit_ms=30000, finish_limit_ms=30000,
            extra_props={"m3.test.classification_delay_ms": "1500"})
    finally:
        server.close()
    completion = completion_of(run_dir)
    calls = rp.read_calls_csv(run_dir / "calls.csv")
    assert completion["success"] == 1
    assert completion["unfinished"] == 0
    assert completion["limit_hit"] is True
    assert completion["normal"] is False
    assert len(calls) == 1
    assert calls[0]["outcome"] == "success"
    assert calls[0]["decision_ns"] == calls[0]["return_ns"]


@requires_jmeter
def test_total_deadline_before_request_deadline_marks_unfinished(tmp_path):
    """총한도가 요청 한도보다 먼저면 반환 전에도 unfinished로 결정한다."""
    server = FakeCounter(incr_delay_after=100, incr_delay_ms_after=4000)
    try:
        run_dir, _, _ = run_fake_jmeter(
            tmp_path, server, c=1, n=1, request_timeout_ms=1500,
            transport_timeout_ms=5000, total_limit_ms=1000,
            prep_limit_ms=30000, finish_limit_ms=30000,
            extra_props={"m3.test.total_limit_callback_delay_ms": "5000"})
    finally:
        server.close()
    completion = completion_of(run_dir)
    total_deadline = completion["start_ns"] + 1000 * 1_000_000
    calls = rp.read_calls_csv(run_dir / "calls.csv")
    assert completion["success"] == 0
    assert completion["timeout"] == 0
    assert completion["unfinished"] == 1
    assert completion["limit_hit"] is True
    assert completion["normal"] is False
    assert len(calls) == 1
    call = calls[0]
    assert call["start_ns"] < total_deadline
    assert call["return_ns"] > total_deadline
    assert call["decision_ns"] == total_deadline
    assert call["outcome"] == "unfinished"
    assert call["late_return"] is True
    assert call["processing_unknown"] is True


@requires_jmeter
def test_total_timer_cancelled_before_slow_final_read(tmp_path):
    """모든 측정 결정 뒤에는 최종 조회가 전체 한도보다 느려도 normal을 유지한다."""
    server = FakeCounter(final_delay_ms=2000)
    try:
        run_dir, _, _ = run_fake_jmeter(
            tmp_path, server, c=1, n=1, request_timeout_ms=5000,
            total_limit_ms=1000, prep_limit_ms=30000,
            finish_limit_ms=30000)
    finally:
        server.close()
    completion = completion_of(run_dir)
    assert completion["normal"] is True
    assert completion["limit_hit"] is False
    assert completion["success"] == 1
    assert completion["final_value"] == 1


# ---------------------------------------------------------------------------
# 코드리뷰 회귀: JDBC 서버 취소·전송 한도
# ---------------------------------------------------------------------------


def test_jdbc_query_timeout_is_zero_and_transport_timeouts_are_url_params(tmp_path):
    settings = base_settings(tmp_path)
    settings["transport_timeout_ms"] = 3500
    properties = rp.runtime_properties(
        settings, target="jdbc", concurrency=1, round_index=1,
        run_dir=Path(tmp_path) / "run")
    assert properties["m3.jdbc.query_timeout_s"] == 0
    assert properties["m3.prep_jdbc_timeout_s"] == 0
    assert properties["m3.finish_jdbc_timeout_s"] == 0
    url = properties["m3.jdbc.url"]
    assert "connectTimeout=3500" in url
    assert "socketTimeout=3500" in url
    assert "autoReconnect=false" in url
    assert "maxReconnects=" not in url


def test_jdbc_url_conflicting_transport_timeout_is_refused():
    with pytest.raises(rp.InputError):
        rp.prepare_jdbc_url(
            "jdbc:mysql://127.0.0.1/db?socketTimeout=100", 5000)
    with pytest.raises(rp.InputError):
        rp.prepare_jdbc_url(
            "jdbc:mysql://127.0.0.1/db?connectTimeout=0", 5000)
    with pytest.raises(rp.InputError):
        rp.prepare_jdbc_url(
            "jdbc:mysql://127.0.0.1/db?autoReconnect=true", 5000)
    with pytest.raises(rp.InputError):
        rp.prepare_jdbc_url(
            "jdbc:mysql://127.0.0.1/db?maxReconnects=3", 5000)
    with pytest.raises(rp.InputError):
        rp.prepare_jdbc_url(
            "jdbc:mysql://127.0.0.1/db?maxReconnects=0", 5000)


def test_jdbc_url_preserves_other_parameters_and_matching_timeouts():
    url = rp.prepare_jdbc_url(
        "jdbc:mysql://127.0.0.1/db?useSSL=false&socketTimeout=5000", 5000)
    assert "useSSL=false" in url
    assert url.count("socketTimeout=") == 1
    assert "socketTimeout=5000" in url


def test_jdbc_query_timeout_jmx_does_not_request_server_cancel():
    root = jmx_root()
    samplers = list(root.iter("JDBCSampler"))
    assert samplers
    for sampler in samplers:
        props = {child.get("name"): (child.text or "")
                 for child in sampler if child.tag.endswith("Prop")}
        assert props["queryTimeout"] in (
            "${__P(m3.jdbc.query_timeout_s)}",
            "${__P(m3.prep_jdbc_timeout_s)}",
            "${__P(m3.finish_jdbc_timeout_s)}",
        )


# ---------------------------------------------------------------------------
# 코드리뷰 회귀: 최종 수치 정확 비교
# ---------------------------------------------------------------------------


@requires_jmeter
def test_fractional_final_value_is_not_accepted_as_success(tmp_path):
    server = FakeCounter(final_value_override=1.9)
    try:
        run_dir, _, _ = run_fake_jmeter(
            tmp_path, server, c=1, n=1, request_timeout_ms=5000,
            total_limit_ms=60000)
    finally:
        server.close()
    completion = completion_of(run_dir)
    assert completion["success"] == 1
    assert completion["final_read_ok"] is False
    assert completion["normal"] is False


# ---------------------------------------------------------------------------
# 코드리뷰 회귀: 나노초 정밀도
# ---------------------------------------------------------------------------


def test_as_int_preserves_integer_nanoseconds_exactly():
    assert rp._as_int("9007199254740993") == 9007199254740993
    with pytest.raises(ValueError):
        rp._as_int("123.4")
    with pytest.raises(ValueError):
        rp._as_int("1e3")


def test_calls_csv_rejects_fractional_nanoseconds(tmp_path):
    calls = [make_call(seq=1)]
    completion = make_completion(calls, planned_n=1, final_value=1)
    run_dir = make_run_dir(tmp_path, calls, completion)
    text = (run_dir / "calls.csv").read_text(encoding="utf-8")
    text = text.replace(str(calls[0]["start_ns"]), "123.4", 1)
    (run_dir / "calls.csv").write_text(text, encoding="utf-8")
    with pytest.raises(rp.IncompleteResultError):
        rp.read_calls_csv(run_dir / "calls.csv")


# ---------------------------------------------------------------------------
# 코드리뷰 회귀: 산출물 시간 계약
# ---------------------------------------------------------------------------


def rewrite_run(run_dir, calls, completion):
    write_calls(run_dir / "calls.csv", calls)
    write_jtl(run_dir / "result.jtl", calls)
    (run_dir / "completion.json").write_text(
        json.dumps(completion, ensure_ascii=False), encoding="utf-8")


def basic_valid_run(tmp_path):
    calls = [make_call(seq=1, elapsed_ms=3.0)]
    completion = make_completion(calls, planned_n=1, final_value=1)
    return make_run_dir(tmp_path, calls, completion), calls, completion


@pytest.mark.parametrize("mutation", [
    "zero_duration", "end_mismatch", "start_before_global",
    "elapsed_mismatch", "missing_return", "decision_before_start",
    "thread_overlap", "wrong_http_code",
])
def test_artifact_timestamp_contract_mutations_are_rejected(tmp_path, mutation):
    run_dir, calls, completion = basic_valid_run(tmp_path)
    if mutation == "zero_duration":
        completion = dict(completion)
        completion["end_ns"] = completion["start_ns"]
    elif mutation == "end_mismatch":
        completion = dict(completion)
        completion["end_ns"] = max(call["decision_ns"] for call in calls) - 1
    elif mutation == "start_before_global":
        completion = dict(completion)
        completion["start_ns"] = min(call["start_ns"] for call in calls) + 1
    elif mutation == "elapsed_mismatch":
        calls = [dict(calls[0])]
        calls[0]["client_elapsed_ns"] = calls[0]["client_elapsed_ns"] + 1
    elif mutation == "missing_return":
        calls = [dict(calls[0])]
        calls[0]["return_ns"] = None
        calls[0]["observed_elapsed_ns"] = None
    elif mutation == "decision_before_start":
        calls = [dict(calls[0])]
        calls[0]["decision_ns"] = calls[0]["start_ns"] - 1
        calls[0]["observed_decision_ns"] = calls[0]["decision_ns"]
        calls[0]["return_ns"] = calls[0]["decision_ns"]
        calls[0]["client_elapsed_ns"] = -1
    elif mutation == "thread_overlap":
        calls = [make_call(seq=1, start_ns=1_000_000_000,
                           decision_ns=1_100_000_000),
                 make_call(seq=2, start_ns=1_050_000_000,
                           decision_ns=1_150_000_000)]
    elif mutation == "wrong_http_code":
        calls = [dict(calls[0])]
        calls[0]["http_code"] = 500
    if mutation not in ("zero_duration", "end_mismatch", "start_before_global"):
        completion = make_completion(calls, planned_n=len(calls), final_value=len(calls))
    rewrite_run(run_dir, calls, completion)
    with pytest.raises(rp.IncompleteResultError):
        rp.validate_run_artifacts(run_dir, expected={
            "run_id": "run-1", "planned_n": len(calls),
            "concurrency": 1, "target": "http",
        })


def test_valid_timestamp_artifacts_still_pass(tmp_path):
    run_dir, _, _ = basic_valid_run(tmp_path)
    result = rp.validate_run_artifacts(run_dir, expected={
        "run_id": "run-1", "planned_n": 1, "concurrency": 1,
        "target": "http",
    })
    assert result["attempts"] == 1


# ---------------------------------------------------------------------------
# 코드리뷰 회귀: 결과 폴더와 분리된 공유 상태
# ---------------------------------------------------------------------------


def test_state_dir_blocks_new_results_folder_after_unknown_failure(tmp_path):
    settings = base_settings(tmp_path)
    rp.RunStateStore(settings["state_dir"]).begin("old-run")
    settings["results_dir"] = str(Path(tmp_path) / "different-results")
    settings["run_id"] = "new-run"
    called = []
    with pytest.raises(rp.PendingRunError):
        rp.run_single(settings, target="http", concurrency=1, round_index=1,
                      runner=lambda *_: called.append(True))
    assert called == []


def test_state_dir_requires_absent_atomic_lock(tmp_path):
    store = rp.RunStateStore(tmp_path / "state")
    lock = Path(tmp_path) / "state" / ".state" / "active.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("external", encoding="utf-8")
    with pytest.raises(rp.PendingRunError):
        store.begin("run-1")


def test_state_dir_shared_by_two_store_instances(tmp_path):
    first = rp.RunStateStore(tmp_path / "state")
    second = rp.RunStateStore(tmp_path / "state")
    first.begin("run-1")
    with pytest.raises(rp.PendingRunError):
        second.begin("run-2")
    first.complete("run-1")
    second.begin("run-2")
    assert second.pending() == ["run-2"]


def test_single_input_requires_state_dir(tmp_path):
    settings = base_settings(tmp_path)
    del settings["state_dir"]
    with pytest.raises(rp.InputError):
        rp.validate_single_inputs(settings, target="http")


# ---------------------------------------------------------------------------
# 실제 MySQL 격리 검증 (M3-07·10·14). 테이블·계정은 메인이 준비한다.
# ---------------------------------------------------------------------------


def real_jdbc_ready():
    names = ("M3_RUN_JDBC", "M3_JDBC_URL", "M3_JDBC_USER",
             "M3_JDBC_PASSWORD_FILE", "M3_JMETER_BIN", "M3_JAVA_HOME")
    return all(os.environ.get(name) for name in names)


requires_real_jdbc = pytest.mark.skipif(
    not real_jdbc_ready(),
    reason="M3_RUN_JDBC=1과 실제 MySQL·JMeter 환경 필요",
)


def real_environment():
    return {
        "jmeter_version": "5.6.3", "jdk_version": "17.0.21",
        "mysql_version": "8.4", "connector_version": "9.2.0",
        "python_version": "3.14.10", "flask_version": "3.1.3",
        "werkzeug_version": "3.1.9",
    }


def real_jdbc_settings(tmp_path, *, run_id, jmx_path, c=1, n=1,
                       request_timeout_ms=5000, transport_timeout_ms=3000,
                       total_limit_ms=60000, prep_limit_ms=30000,
                       finish_limit_ms=30000):
    return {
        "results_dir": str(Path(tmp_path) / "results"),
        "state_dir": str(Path(tmp_path) / "state"),
        "run_id": run_id,
        "n": n,
        "request_timeout_ms": request_timeout_ms,
        "transport_timeout_ms": transport_timeout_ms,
        "total_limit_ms": total_limit_ms,
        "prep_limit_ms": prep_limit_ms,
        "finish_limit_ms": finish_limit_ms,
        "environment": real_environment(),
        "jdbc": {
            "url": os.environ["M3_JDBC_URL"],
            "user": os.environ["M3_JDBC_USER"],
            "password_file": os.environ["M3_JDBC_PASSWORD_FILE"],
        },
        "jmeter_bin": os.environ["M3_JMETER_BIN"],
        "java_home": os.environ["M3_JAVA_HOME"],
        "jmx": str(jmx_path),
        "properties": str(Path(__file__).with_name("jmeter.properties")),
    }


def mysql_parts():
    url = os.environ["M3_JDBC_URL"]
    match = re.match(r"jdbc:mysql://([^/:]+)(?::(\d+))?/([^?;]+)", url)
    if match is None:
        raise AssertionError("M3_JDBC_URL에서 host·port·database를 해석하지 못했다")
    return match.group(1), match.group(2) or "3306", match.group(3)


def mysql_execute(sql):
    host, port, database = mysql_parts()
    password = Path(os.environ["M3_JDBC_PASSWORD_FILE"]).read_text(
        encoding="utf-8").rstrip("\n")
    command = [
        shutil.which("mysql") or "mysql",
        "--batch", "--skip-column-names",
        "-h", host, "-P", port, "-u", os.environ["M3_JDBC_USER"],
        database, "-e", sql,
    ]
    return subprocess.run(
        command, env={**os.environ, "MYSQL_PWD": password},
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout


def copy_jmx_with_changes(tmp_path, *, measurement_query=None,
                          measurement_args=None, measurement_types=None,
                          warm_key=None):
    """원본 JMX/Groovy를 복사하고 측정 명령 또는 격리용 키만 바꾼다."""
    tree = ElementTree.parse(Path(__file__).with_name("counter.jmx"))
    root = tree.getroot()
    for sampler in root.iter("JDBCSampler"):
        name = sampler.get("testname")
        props = {child.get("name"): child for child in sampler
                 if child.tag.endswith("Prop")}
        if name == "m3-jdbc-incr":
            if measurement_query is not None:
                props["query"].text = measurement_query
            if measurement_args is not None:
                props["queryArguments"].text = measurement_args
            if measurement_types is not None:
                props["queryArgumentsTypes"].text = measurement_types
        elif warm_key is not None and "queryArguments" in props:
            value = props["queryArguments"].text or ""
            props["queryArguments"].text = value.replace("counter:one", warm_key)
    target_dir = Path(tmp_path) / "jmx"
    target_dir.mkdir()
    tree.write(target_dir / "counter.jmx", encoding="utf-8", xml_declaration=True)
    shutil.copy(Path(__file__).with_name("counter.groovy"),
                target_dir / "counter.groovy")
    return target_dir / "counter.jmx"


def read_jdbc_run(results_dir, run_id):
    run_dir = Path(results_dir) / run_id
    completion = json.loads((run_dir / "completion.json").read_text(encoding="utf-8"))
    calls = rp.read_calls_csv(run_dir / "calls.csv")
    return run_dir, completion, calls


def run_jdbc_case(settings, *, c=1):
    try:
        rp.run_single(settings, target="jdbc", concurrency=c, round_index=1)
    except rp.IncompleteResultError:
        pass
    return read_jdbc_run(settings["results_dir"], settings["run_id"])


@requires_real_jdbc
def test_m3_10_affected_zero_is_judgment_failure(tmp_path):
    jmx = copy_jmx_with_changes(
        tmp_path,
        measurement_query=("UPDATE benchmark_counter SET value = value + 1 "
                           "WHERE counter_key = ? AND 1 = 0"))
    settings = real_jdbc_settings(tmp_path, run_id="m3-10-zero", jmx_path=jmx)
    _, completion, calls = run_jdbc_case(settings)
    assert completion["success"] == 0
    assert completion["other_failure"] == 1
    assert completion["normal"] is False
    assert calls[0]["outcome"] == "other_failure"
    assert calls[0]["jdbc_affected"] == 0


@requires_real_jdbc
def test_m3_10_affected_two_is_judgment_failure(tmp_path):
    mysql_execute(
        "DROP TABLE IF EXISTS m3_two_rows;"
        "CREATE TABLE m3_two_rows (counter_key VARBINARY(256) PRIMARY KEY,"
        " value BIGINT NOT NULL) ENGINE=InnoDB;"
        "INSERT INTO m3_two_rows (counter_key, value) VALUES ('a', 0), ('b', 0);"
    )
    try:
        jmx = copy_jmx_with_changes(
            tmp_path,
            measurement_query=("UPDATE m3_two_rows SET value = value + 1 "
                               "WHERE counter_key IN (?, ?)"),
            measurement_args="a,b",
            measurement_types="VARBINARY,VARBINARY",
        )
        settings = real_jdbc_settings(tmp_path, run_id="m3-10-two", jmx_path=jmx)
        _, completion, calls = run_jdbc_case(settings)
        assert completion["success"] == 0
        assert completion["other_failure"] == 1
        assert completion["normal"] is False
        assert calls[0]["outcome"] == "other_failure"
        assert calls[0]["jdbc_affected"] == 2
    finally:
        mysql_execute("DROP TABLE IF EXISTS m3_two_rows;")


@requires_real_jdbc
def test_m3_14_each_thread_observes_its_own_jdbc_connection(tmp_path):
    settings = real_jdbc_settings(
        tmp_path, run_id="m3-14-connections",
        jmx_path=Path(__file__).with_name("counter.jmx"), c=10, n=1,
        request_timeout_ms=5000, transport_timeout_ms=5000,
    )
    result = rp.run_single(settings, target="jdbc", concurrency=10, round_index=1)
    completion = read_jdbc_run(settings["results_dir"], "m3-14-connections")[1]
    ids = completion.get("connection_ids") or {}
    after = completion.get("connection_ids_after") or {}
    assert len(ids) == 10
    assert len(set(ids.values())) == 10
    assert completion["distinct_connection_ids"] == 10
    assert ids == after
    assert completion["connection_identity_stable"] is True
    assert result["success"] == 1
    # JMeter 프로세스 종료 뒤 자기 연결이 목록에 남지 않았는지 확인한다.
    # benchmark 사용자는 performance_schema 조회 권한이 없어 SHOW PROCESSLIST를 쓴다.
    processlist = mysql_execute("SHOW PROCESSLIST;")
    live_ids = {line.split("\t", 1)[0] for line in processlist.splitlines()
                if line.strip()}
    assert set(ids.values()).isdisjoint(live_ids)


@requires_real_jdbc
def test_m3_07_locked_row_times_out_without_retry(tmp_path):
    mysql_execute(
        "INSERT IGNORE INTO benchmark_counter (counter_key, value) "
        "VALUES ('counter:one', 0);"
        "UPDATE benchmark_counter SET value = 0 WHERE counter_key = 'counter:one';"
    )
    host, port, database = mysql_parts()
    password = Path(os.environ["M3_JDBC_PASSWORD_FILE"]).read_text(
        encoding="utf-8").rstrip("\n")
    lock = subprocess.Popen(
        [shutil.which("mysql") or "mysql", "--batch", "--skip-column-names",
         "-h", host, "-P", port, "-u", os.environ["M3_JDBC_USER"], database,
         "-e", ("START TRANSACTION;"
                "SELECT value FROM benchmark_counter "
                "WHERE counter_key = 'counter:one' FOR UPDATE;"
                "SELECT SLEEP(8); COMMIT;")],
        env={**os.environ, "MYSQL_PWD": password},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        import time
        time.sleep(1.0)  # 잠금 SELECT가 먼저 실행되도록 잠시 기다린다.
        jmx = copy_jmx_with_changes(tmp_path, warm_key="counter:warm")
        settings = real_jdbc_settings(
            tmp_path, run_id="m3-07-lock", jmx_path=jmx,
            request_timeout_ms=1000, transport_timeout_ms=3000,
            total_limit_ms=10000,
        )
        _, completion, calls = run_jdbc_case(settings)
        assert completion["success"] == 0
        assert completion["timeout"] == 1
        assert completion["processing_unknown"] >= 1
        assert completion["normal"] is False
        assert calls[0]["outcome"] == "timeout"
    finally:
        try:
            lock.wait(timeout=15)
        except subprocess.TimeoutExpired:
            lock.terminate()
            lock.wait(timeout=5)
        mysql_execute(
            "DELETE FROM benchmark_counter WHERE counter_key = 'counter:warm';")


def test_state_dir_endpoint_mismatch_blocks_next_run(tmp_path):
    store = rp.RunStateStore(tmp_path / "state")
    store.begin("old", endpoint="http://127.0.0.1:5000")
    store.fail("old", "unknown")
    with pytest.raises(rp.PendingRunError):
        rp.RunStateStore(tmp_path / "state").begin(
            "new", endpoint="http://127.0.0.1:5001")


def test_different_state_dirs_are_isolated(tmp_path):
    first = rp.RunStateStore(tmp_path / "state-a")
    second = rp.RunStateStore(tmp_path / "state-b")
    first.begin("a-run")
    second.begin("b-run")
    assert first.pending() == ["a-run"]
    assert second.pending() == ["b-run"]


def test_run_single_redacts_password_from_result_logs(tmp_path):
    settings = base_settings(tmp_path)

    def fake_runner(child_settings, run_dir):
        (Path(run_dir) / "jmeter.log").write_text(
            "connection failed password=not-in-argv", encoding="utf-8")
        return {"returncode": 1, "timed_out": False,
                "stdout": "secret not-in-argv", "stderr": ""}

    with pytest.raises(rp.IncompleteResultError):
        rp.run_single(settings, target="http", concurrency=1, round_index=1,
                      runner=fake_runner)
    run_dir = Path(settings["results_dir"]) / "run-1"
    for path in run_dir.iterdir():
        if path.is_file():
            assert "not-in-argv" not in path.read_text(encoding="utf-8",
                                                       errors="ignore"), path.name


@requires_jmeter
def test_after_probe_runs_only_after_all_measurement_decisions(tmp_path):
    server = FakeCounter(incr_delay_after=100, incr_delay_ms_after=200)
    try:
        run_dir, _, _ = run_fake_jmeter(
            tmp_path, server, c=1, n=2, request_timeout_ms=5000,
            total_limit_ms=60000,
            extra_props={
                "m3.test.after_probe": "true",
                "m3.test.after_probe_url": f"http://{server.host}:{server.port}/after",
            })
    finally:
        server.close()
    completion = completion_of(run_dir)
    assert completion["normal"] is True
    assert completion["success"] == 2
    assert server.after_count == 1
    assert server.after_incr_count == 102
    assert server.after_ns > server.last_measurement_response_ns
    assert len(measurement_rows(run_dir)) == 2


@requires_jmeter
def test_after_probe_skipped_when_measurement_fails(tmp_path):
    server = FakeCounter(
        incr_delay_after=100, incr_status=500,
        incr_body='{"error":"INTERNAL_SERVER_ERROR"}', bad_after=100)
    try:
        run_dir, _, _ = run_fake_jmeter(
            tmp_path, server, c=1, n=1, request_timeout_ms=2000,
            total_limit_ms=60000,
            extra_props={
                "m3.test.after_probe": "true",
                "m3.test.after_probe_url": f"http://{server.host}:{server.port}/after",
            })
    finally:
        server.close()
    completion = completion_of(run_dir)
    assert completion["success"] == 0
    assert completion["normal"] is False
    assert server.after_count == 0


def test_connection_after_query_is_behind_completion_gate():
    root = jmx_root()
    parents = {child: parent for parent in root.iter() for child in parent}
    wait = next(element for element in root.iter("JSR223Sampler")
                if element.get("testname") == "m3-wait-complete")
    after = next(element for element in root.iter("JDBCSampler")
                 if element.get("testname") == "m3-connection-id-after")
    allowed = next(element for element in root.iter("IfController")
                   if element.get("testname") == "m3-after-allowed")
    wait_props = {child.get("name"): (child.text or "") for child in wait
                  if child.tag.endswith("Prop")}
    assert wait_props["parameters"] == "wait-complete"
    condition = next(child.text for child in allowed
                     if child.get("name") == "IfController.condition")
    assert condition == "${m3_after_allowed}"
    ordered = list(root.iter())
    assert ordered.index(wait) < ordered.index(allowed) < ordered.index(after)
    # JMX에서 컨트롤러의 자식은 바로 다음 hashTree다.
    scope_parent = parents[allowed]
    siblings = list(scope_parent)
    scope = siblings[siblings.index(allowed) + 1]
    assert scope.tag == "hashTree"
    assert after in list(scope.iter())
    assert any(element.get("testname") == "m3-connection-after-target"
               for element in scope.iter())
