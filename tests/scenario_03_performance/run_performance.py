"""시나리오 03 성능 실행 담당.

부하를 직접 만들지 않는다. 한 대상 실행마다 JMeter 프로세스 하나를 시작하고,
JMeter가 남긴 JTL·정규화 기록·완료 표식을 검산한 뒤 회차 지표를 계산한다.

- 측정 수치(N·시간 한도)의 기본값을 두지 않는다. 입력이 없으면 시작하지 않는다.
- 비밀은 실행용 속성 파일(0600)에만 쓰고 명령행 인자·결과·로그에 남기지 않는다.
- 진행 중 표식이 남아 있으면 새 실행을 거절한다(fail closed).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

WARMUP_TOTAL = 100
CONCURRENCIES = (1, 10, 50, 100)
ROUNDS = 5
PILOT_N = (1000, 10000)
BASELINE_N_RANGE = range(1000, 10001)
TARGETS = ("http", "jdbc")

MEASUREMENT_LABELS = ("m3-http-incr", "m3-jdbc-incr")
WARMUP_LABELS = ("m3-warmup-http-incr", "m3-warmup-jdbc-incr")

CALL_FIELDS = (
    "run_id",
    "phase",
    "thread_name",
    "request_sequence",
    "start_ns",
    "deadline_ns",
    "decision_ns",
    "observed_decision_ns",
    "return_ns",
    "outcome",
    "processing_unknown",
    "client_elapsed_ns",
    "observed_elapsed_ns",
    "late_return",
    "http_code",
    "jdbc_affected",
    "error",
)

SAMPLE_VARIABLES = (
    "m3_run_id",
    "m3_phase",
    "m3_thread_name",
    "m3_request_sequence",
    "m3_start_ns",
    "m3_deadline_ns",
    "m3_decision_ns",
    "m3_observed_decision_ns",
    "m3_return_ns",
    "m3_outcome",
    "m3_processing_unknown",
    "m3_client_elapsed_ns",
    "m3_observed_elapsed_ns",
    "m3_late_return",
    "m3_http_code",
    "m3_jdbc_affected",
    "m3_error",
    "m3_connection_id",
)

OUTCOMES = ("success", "rejected", "timeout", "other_failure", "unfinished")
BLOCKING_STATES = ("pending", "failed-unknown")


class InputError(ValueError):
    """본 측정을 시작할 수 없을 만큼 입력이 불완전하다."""


class IncompleteResultError(RuntimeError):
    """실행 산출물이 정상 완료를 입증하지 못한다."""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


class PendingRunError(RuntimeError):
    """사람 확인 없이 지나갈 수 없는 진행 중 표식이 있다."""


# ---------------------------------------------------------------------------
# 집계
# ---------------------------------------------------------------------------


def percentile(values, p):
    """정렬 표본에서 (m-1)*p 위치를 선형 보간한다. 표본이 없으면 None."""
    if not 0.0 <= p <= 1.0:
        raise ValueError("p는 0과 1 사이여야 한다")
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * p
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    fraction = position - low
    return ordered[low] + (ordered[high] - ordered[low]) * fraction


def _success_latencies_ms(records):
    values = []
    for record in records:
        if _is_success(record) and record.get("client_elapsed_ns") not in (None, ""):
            values.append(int(record["client_elapsed_ns"]) / 1_000_000)
    return values


def _is_success(record):
    return record.get("outcome") == "success"


def _is_unknown(record):
    value = record.get("processing_unknown")
    return value is True or value == "true" or value == "True"


def round_metrics(records, planned_n, final_value, duration_ns):
    """회차 하나의 지표를 계산한다. 실패·시간 초과는 성공 지연에 넣지 않는다."""
    records = [record for record in records if record.get("phase", "measure") == "measure"]
    attempts = len(records)
    outcome_counts = {name: 0 for name in OUTCOMES}
    for record in records:
        outcome = record.get("outcome")
        if outcome in outcome_counts:
            outcome_counts[outcome] += 1
    success = outcome_counts["success"]
    latencies = _success_latencies_ms(records)
    duration_seconds = None
    if duration_ns is not None and duration_ns > 0:
        duration_seconds = duration_ns / 1_000_000_000
    throughput = None if duration_seconds is None else success / duration_seconds
    success_rate = None if attempts == 0 else success / attempts
    passed = (
        attempts == planned_n
        and success == planned_n
        and outcome_counts["rejected"] == 0
        and outcome_counts["timeout"] == 0
        and outcome_counts["other_failure"] == 0
        and outcome_counts["unfinished"] == 0
        and sum(1 for record in records if _is_unknown(record)) == 0
        and final_value == planned_n
    )
    return {
        "attempts": attempts,
        "planned_n": planned_n,
        "success_count": success,
        "rejected_count": outcome_counts["rejected"],
        "timeout_count": outcome_counts["timeout"],
        "other_failure_count": outcome_counts["other_failure"],
        "unfinished_count": outcome_counts["unfinished"],
        "unknown_count": sum(1 for record in records if _is_unknown(record)),
        "success_rate": success_rate,
        "throughput_per_second": throughput,
        "p50_ms": percentile(latencies, 0.50),
        "p95_ms": percentile(latencies, 0.95),
        "p99_ms": percentile(latencies, 0.99),
        "final_value": final_value,
        "duration_ns": duration_ns,
        "passed": passed,
    }


SUMMARY_FIELDS = (
    "p50_ms",
    "p95_ms",
    "p99_ms",
    "throughput_per_second",
    "success_rate",
)


def summarize_rounds(rounds):
    """정확히 5회이고 모두 통과일 때만 중앙값·최솟값·최댓값을 만든다."""
    if len(rounds) != ROUNDS:
        raise IncompleteResultError(
            f"5회 기본 비교가 아니다: {len(rounds)}회",
            {"rounds": len(rounds), "observations": rounds},
        )
    failed = [index for index, item in enumerate(rounds, start=1) if not item.get("passed")]
    if failed:
        raise IncompleteResultError(
            "실패 회차가 있어 5회 기본 비교 요약을 만들지 않는다: "
            + ", ".join(str(index) for index in failed),
            {"failed_rounds": failed, "observations": rounds},
        )
    summary = {"rounds": len(rounds)}
    for name in SUMMARY_FIELDS:
        values = [round_[name] for round_ in rounds if round_.get(name) is not None]
        if not values:
            summary[name] = None
        else:
            summary[name] = {
                "median": statistics.median(values),
                "min": min(values),
                "max": max(values),
            }
    return summary


# ---------------------------------------------------------------------------
# 입력 계약
# ---------------------------------------------------------------------------


def _positive_int(settings, name, errors):
    value = settings.get(name)
    if value is None:
        errors.append(f"{name}: 필수 입력 없음")
    elif isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        errors.append(f"{name}: 1 이상의 정수 필요")


def _environment_errors(environment, errors):
    if not isinstance(environment, dict):
        errors.append("environment: 필수 입력 없음")
        return
    for name in (
        "jmeter_version",
        "jdk_version",
        "mysql_version",
        "connector_version",
        "python_version",
        "flask_version",
        "werkzeug_version",
    ):
        if not environment.get(name):
            errors.append(f"environment.{name}: 필수 입력 없음")


_JDBC_MANAGED_PARAMS = {"connecttimeout", "sockettimeout", "autoreconnect", "maxreconnects"}


def prepare_jdbc_url(url, transport_timeout_ms):
    """Connector/J 전송 한도를 URL에 맞추고 서버 취소·재시도 설정을 검증한다.

    JDBC 쿼리 제한(Statement.setQueryTimeout)은 사용하지 않는다. 연결·소켓 한도만
    transport deadline에 맞춘다. 비밀번호는 URL에 넣지 않는다.
    """
    if not isinstance(url, str) or not url:
        raise InputError("JDBC URL이 없다")
    if isinstance(transport_timeout_ms, bool) or not isinstance(transport_timeout_ms, int) \
            or transport_timeout_ms <= 0:
        raise InputError("JDBC 전송 한도는 1 이상의 정수 밀리초여야 한다")
    base, separator, query = url.partition("?")
    parts = [part for part in query.split("&") if part]
    seen = {}
    kept = []
    for part in parts:
        key, equals, value = part.partition("=")
        lowered = key.lower()
        if lowered == "password":
            raise InputError("JDBC URL에 비밀번호를 넣지 않는다")
        if lowered in _JDBC_MANAGED_PARAMS:
            if lowered in seen:
                raise InputError(f"JDBC URL에 중복된 {key} 설정이 있다")
            seen[lowered] = value
        else:
            kept.append(part)
    if seen.get("autoreconnect", "").lower() == "true":
        raise InputError("JDBC autoReconnect=true는 허용하지 않는다")
    if "maxreconnects" in seen:
        raise InputError("JDBC URL에 maxReconnects를 지정하지 않는다(autoReconnect=false)")
    for name in ("connecttimeout", "sockettimeout"):
        if name in seen:
            try:
                configured = int(seen[name])
            except ValueError as error:
                raise InputError(f"JDBC {name} 값이 정수가 아니다") from error
            if configured != transport_timeout_ms:
                raise InputError(
                    f"JDBC {name}={configured}가 전송 한도 {transport_timeout_ms}와 다르다"
                )
    # Connector/J 9.2.0은 maxReconnects=0을 허용하지 않는다. autoReconnect=false로
    # 재시도를 끄고 maxReconnects는 명시하지 않는다(기본값은 autoReconnect=false라 비활성).
    kept.extend([
        f"connectTimeout={transport_timeout_ms}",
        f"socketTimeout={transport_timeout_ms}",
        "autoReconnect=false",
    ])
    return base + "?" + "&".join(kept)


def endpoint_key(settings, target):
    """같은 실제 대상 그룹인지 식별한다. 상태 디렉터리 공유 오용을 막는다."""
    if target == "http":
        http = settings.get("http") or {}
        return f"http:{http.get('scheme')}://{http.get('host')}:{http.get('port')}"
    transport = (settings.get("transport_timeout_ms")
                 or settings.get("request_timeout_ms") or 1)
    return "jdbc:" + prepare_jdbc_url((settings.get("jdbc") or {}).get("url", ""), transport)


def _target_errors(settings, errors):
    http = settings.get("http")
    if not isinstance(http, dict) or not all(
        http.get(name) for name in ("host", "port", "scheme")
    ):
        errors.append("http: host·port·scheme 필수 입력 없음")
    jdbc = settings.get("jdbc")
    if not isinstance(jdbc, dict) or not jdbc.get("url") or not jdbc.get("user"):
        errors.append("jdbc: url·user 필수 입력 없음")
    elif not jdbc.get("password") and not jdbc.get("password_file"):
        errors.append("jdbc: 비밀번호 입력 없음(password_file 또는 환경)")
    else:
        transport = settings.get("transport_timeout_ms") or settings.get("request_timeout_ms")
        try:
            prepare_jdbc_url(jdbc["url"], transport)
        except InputError as error:
            errors.append(str(error))


def validate_baseline_inputs(settings):
    """기본 비교 입력을 검사한다. 누락·범위 위반은 InputError로 거부한다."""
    errors = []
    for name in (
        "results_dir",
        "state_dir",
        "n",
        "request_timeout_ms",
        "total_limit_ms",
        "prep_limit_ms",
        "finish_limit_ms",
        "environment",
    ):
        if settings.get(name) in (None, ""):
            errors.append(f"{name}: 필수 입력 없음")
    for name in ("n", "request_timeout_ms", "total_limit_ms",
                 "prep_limit_ms", "finish_limit_ms"):
        _positive_int(settings, name, errors)
    if isinstance(settings.get("n"), int) and not isinstance(settings.get("n"), bool):
        if settings["n"] not in BASELINE_N_RANGE:
            errors.append("n: 본 측정은 1,000~10,000 범위에서 합의해야 함")
    _environment_errors(settings.get("environment"), errors)
    _target_errors(settings, errors)
    if settings.get("total_limit_ms") and settings.get("request_timeout_ms"):
        if settings["total_limit_ms"] <= settings["request_timeout_ms"]:
            errors.append("total_limit_ms: 요청 한도보다 커야 함")
    if errors:
        raise InputError("본 측정 입력이 불완전하다:\n- " + "\n- ".join(errors))
    return settings


def validate_pilot_inputs(settings):
    """파일럿은 후보 N과 입력 한도를 명시해야 한다. 본 측정 수치를 자동 선택하지 않는다."""
    errors = []
    for name in ("results_dir", "state_dir", "n", "request_timeout_ms",
                 "total_limit_ms", "prep_limit_ms", "finish_limit_ms",
                 "environment"):
        if settings.get(name) in (None, ""):
            errors.append(f"{name}: 필수 입력 없음")
    for name in ("n", "request_timeout_ms", "total_limit_ms",
                 "prep_limit_ms", "finish_limit_ms"):
        _positive_int(settings, name, errors)
    if isinstance(settings.get("n"), int) and settings.get("n") not in PILOT_N:
        errors.append(f"n: 파일럿 후보는 {PILOT_N[0]} 또는 {PILOT_N[1]}만 허용")
    _environment_errors(settings.get("environment"), errors)
    _target_errors(settings, errors)
    if errors:
        raise InputError("파일럿 입력이 불완전하다:\n- " + "\n- ".join(errors))
    return settings


def validate_single_inputs(settings, target=None):
    """단일 실행은 도구 검증에도 쓰므로 N 범위는 강제하지 않지만 한도는 필수다.

    단일 실행은 선택한 대상의 접속 정보만 요구한다. 기본 비교·파일럿은 양쪽을
    모두 요구하므로 이 완화를 묶음 실행에 적용하지 않는다.
    """
    errors = []
    for name in ("results_dir", "state_dir", "n", "request_timeout_ms",
                 "total_limit_ms", "prep_limit_ms", "finish_limit_ms",
                 "environment"):
        if settings.get(name) in (None, ""):
            errors.append(f"{name}: 필수 입력 없음")
    for name in ("n", "request_timeout_ms", "total_limit_ms",
                 "prep_limit_ms", "finish_limit_ms"):
        _positive_int(settings, name, errors)
    _environment_errors(settings.get("environment"), errors)
    if target == "http":
        http = settings.get("http")
        if not isinstance(http, dict) or not all(
            http.get(name) for name in ("host", "port", "scheme")
        ):
            errors.append("http: host·port·scheme 필수 입력 없음")
    elif target == "jdbc":
        jdbc = settings.get("jdbc")
        if not isinstance(jdbc, dict) or not jdbc.get("url") or not jdbc.get("user"):
            errors.append("jdbc: url·user 필수 입력 없음")
        elif not jdbc.get("password") and not jdbc.get("password_file"):
            errors.append("jdbc: 비밀번호 입력 없음(password_file 또는 환경)")
        else:
            transport = (settings.get("transport_timeout_ms")
                         or settings.get("request_timeout_ms"))
            try:
                prepare_jdbc_url(jdbc["url"], transport)
            except InputError as error:
                errors.append(str(error))
    else:
        _target_errors(settings, errors)
    if errors:
        raise InputError("단일 실행 입력이 불완전하다:\n- " + "\n- ".join(errors))
    return settings


# ---------------------------------------------------------------------------
# 결과 읽기·검산
# ---------------------------------------------------------------------------


def _as_int(value):
    if value in (None, "", "null"):
        return None
    if isinstance(value, bool):
        raise ValueError("불리언은 정수 시각이 아니다")
    return int(str(value))


def _as_bool(value):
    if value is True or value == "true" or value == "True":
        return True
    if value is False or value == "false" or value == "False":
        return False
    if value in (None, "", "null"):
        return None
    raise ValueError(f"불리언이 아니다: {value!r}")


def read_calls_csv(path):
    path = Path(path)
    if not path.is_file():
        raise IncompleteResultError("정규화 호출 기록이 없다", {"path": str(path)})
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not set(CALL_FIELDS) <= set(reader.fieldnames):
            raise IncompleteResultError(
                "정규화 호출 기록의 열이 계약과 다르다",
                {"columns": reader.fieldnames},
            )
        records = []
        for line, row in enumerate(reader, start=2):
            record = dict(row)
            try:
                for name in ("request_sequence", "start_ns", "deadline_ns",
                             "decision_ns", "observed_decision_ns", "return_ns",
                             "client_elapsed_ns", "observed_elapsed_ns"):
                    record[name] = _as_int(record.get(name))
                for name in ("http_code", "jdbc_affected"):
                    record[name] = _as_int(record.get(name))
                record["processing_unknown"] = _as_bool(record.get("processing_unknown"))
                record["late_return"] = _as_bool(record.get("late_return"))
            except (TypeError, ValueError) as error:
                raise IncompleteResultError(
                    "정규화 호출 기록의 정수·불리언 값이 계약과 다르다",
                    {"path": str(path), "line": line, "error": str(error)},
                ) from error
            records.append(record)
    return records


def read_jtl_csv(path):
    path = Path(path)
    if not path.is_file():
        raise IncompleteResultError("JTL 결과 파일이 없다", {"path": str(path)})
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise IncompleteResultError("JTL 헤더가 없다", {"path": str(path)})
        fields = set(reader.fieldnames)
        required = {"timeStamp", "elapsed", "label", "success", "threadName"}
        missing = required - fields
        if missing:
            raise IncompleteResultError(
                "JTL 필수 열이 없다", {"missing": sorted(missing),
                                       "columns": reader.fieldnames}
            )
        rows = []
        for line, row in enumerate(reader, start=2):
            # 같은 thread 변수를 물려받은 조율 Sampler 행을 측정 행으로 오인하지 않는다.
            if row.get("label") not in MEASUREMENT_LABELS:
                continue
            if row.get("m3_phase") != "measure":
                continue
            try:
                row["m3_request_sequence"] = _as_int(row.get("m3_request_sequence"))
                row["m3_start_ns"] = _as_int(row.get("m3_start_ns"))
                row["m3_decision_ns"] = _as_int(row.get("m3_decision_ns"))
                row["m3_success"] = _as_bool(row.get("success"))
                row["m3_unknown"] = _as_bool(row.get("m3_processing_unknown"))
            except (TypeError, ValueError) as error:
                raise IncompleteResultError(
                    "JTL 측정 행의 정수·불리언 값이 계약과 다르다",
                    {"path": str(path), "line": line, "error": str(error)},
                ) from error
            rows.append(row)
    return rows


def read_jtl_all_rows(path):
    path = Path(path)
    if not path.is_file():
        raise IncompleteResultError("JTL 결과 파일이 없다", {"path": str(path)})
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_completion(run_dir):
    path = Path(run_dir) / "completion.json"
    if not path.is_file():
        raise IncompleteResultError(
            "완료 표식이 없다. 파일 존재나 종료 코드만으로 정상 완료를 인정하지 않는다",
            {"path": str(path), "attempts": None},
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise IncompleteResultError(
            "완료 표식을 읽을 수 없다", {"path": str(path), "error": str(error)}
        ) from error


def _call_key(record_or_row, thread_key, seq_key):
    return (record_or_row.get(thread_key), record_or_row.get(seq_key))


def _max_in_flight(records):
    events = []
    for record in records:
        start = record.get("start_ns")
        end = record.get("decision_ns")
        if start is None or end is None:
            continue
        events.append((int(start), 1))
        events.append((int(end), -1))
    events.sort(key=lambda item: (item[0], item[1]))
    active = 0
    peak = 0
    for _, delta in events:
        active += delta
        peak = max(peak, active)
    return peak


def _per_thread_peak(records):
    intervals = {}
    for record in records:
        start = record.get("start_ns")
        end = record.get("decision_ns")
        if start is None or end is None:
            continue
        intervals.setdefault(record.get("thread_name"), []).append((int(start), int(end)))
    peaks = {}
    for thread, spans in intervals.items():
        events = []
        for start, end in spans:
            events.append((start, 1))
            events.append((end, -1))
        events.sort(key=lambda item: (item[0], item[1]))
        active = 0
        peak = 0
        for _, delta in events:
            active += delta
            peak = max(peak, active)
        peaks[thread] = peak
    return peaks


def validate_run_artifacts(run_dir, expected):
    """완료 표식·정규화 기록·JTL을 서로 대조한다. 하나라도 어긋나면 정상 완료가 아니다."""
    run_dir = Path(run_dir)
    completion = read_completion(run_dir)
    records = read_calls_csv(run_dir / "calls.csv")
    jtl_rows = read_jtl_csv(run_dir / "result.jtl")
    all_rows = read_jtl_all_rows(run_dir / "result.jtl")
    problems = []

    for name in ("run_id", "planned_n", "concurrency", "target"):
        if expected.get(name) is not None and completion.get(name) != expected[name]:
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

    computed = {name: 0 for name in OUTCOMES}
    target = expected.get("target")
    for record in records:
        outcome = record.get("outcome")
        if outcome not in computed:
            problems.append(f"알 수 없는 결과 분류: {outcome!r}")
            continue
        computed[outcome] += 1
        if record.get("run_id") != expected.get("run_id", record.get("run_id")):
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
        if decision is None:
            problems.append("결과가 결정되지 않은 호출 기록이 있다")
        if start is None:
            problems.append("시작 시각이 없는 호출 기록이 있다")
        if not isinstance(record.get("processing_unknown"), bool):
            problems.append("처리 불명확 표시가 불리언이 아니다")
        if not isinstance(late, bool):
            problems.append("늦은 반환 표시가 불리언이 아니다")
        if start is not None and deadline is not None and deadline <= start:
            problems.append("요청 마감이 시작보다 늦지 않다")
        if start is not None and decision is not None and decision < start:
            problems.append("결정 시각이 시작보다 이르다")
        if decision is not None and observed is not None and observed < decision:
            problems.append("관찰된 결정 시각이 결정 시각보다 이르다")
        if returned is not None and start is not None:
            if returned < start:
                problems.append("반환 시각이 시작보다 이르다")
            if observed_elapsed != returned - start:
                problems.append("관찰 경과가 반환-시작과 다르다")
        elif observed_elapsed is not None:
            problems.append("반환이 없는데 관찰 경과가 있다")
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
            if target == "http" and record.get("http_code") != 200:
                problems.append("HTTP 성공 기록의 상태 코드가 200이 아니다")
            if target == "jdbc" and record.get("jdbc_affected") != 1:
                problems.append("JDBC 성공 기록의 변경 행 수가 1이 아니다")
        else:
            if client_elapsed not in (None, ""):
                problems.append("성공이 아닌 기록에 성공 지연이 있다")
            if outcome == "timeout":
                if decision != deadline:
                    problems.append("시간 초과 결정 시각이 마감과 다르다")
                if returned is not None:
                    if returned <= deadline:
                        problems.append("마감 전 반환이 시간 초과로 결정됐다")
                    if not late:
                        problems.append("늦은 반환 표시가 없다")
                elif late:
                    problems.append("반환이 없는데 늦은 반환 표시가 있다")
            if outcome in ("rejected", "other_failure") and returned is not None:
                if decision != returned:
                    problems.append("완료 응답의 결정 시각이 반환 시각과 다르다")
            if outcome == "unfinished" and returned is not None and not late:
                problems.append("미완료 기록의 반환 표시가 늦은 반환이 아니다")
    if completion.get("attempts") != len(records):
        problems.append("완료 표식 시도 수와 정규화 기록 수가 다르다")
    for name in OUTCOMES:
        if completion.get(name) != computed[name]:
            problems.append(f"완료 표식 {name} 건수 불일치")
    if completion.get("decided") != len(records):
        problems.append("완료 표식 결정 건수와 기록 수가 다르다")

    call_keys = [_call_key(record, "thread_name", "request_sequence") for record in records]
    if len(call_keys) != len(set(call_keys)):
        problems.append("정규화 기록에 (실행 주체, 순번) 중복이 있다")
    jtl_keys = [_call_key(row, "m3_thread_name", "m3_request_sequence") for row in jtl_rows]
    if len(jtl_keys) != len(set(jtl_keys)):
        problems.append("JTL 측정 행에 (실행 주체, 순번) 중복이 있다")
    if set(call_keys) != set(jtl_keys):
        missing = sorted(set(call_keys) - set(jtl_keys))
        extra = sorted(set(jtl_keys) - set(call_keys))
        problems.append(f"JTL과 정규화 기록의 요청 집합이 다르다(누락 {missing}, 추가 {extra})")
    if len(jtl_rows) != len(records):
        problems.append("JTL 측정 행 수와 정규화 기록 수가 다르다")

    by_key = {_call_key(row, "m3_thread_name", "m3_request_sequence"): row
              for row in jtl_rows}
    for record in records:
        row = by_key.get(_call_key(record, "thread_name", "request_sequence"))
        if row is None:
            continue
        if row.get("m3_outcome") != record.get("outcome"):
            problems.append("JTL과 정규화 기록의 결과 분류가 다르다")
        if row.get("m3_success") != (record.get("outcome") == "success"):
            problems.append("JTL 성공 표시가 정규화 결과와 다르다")
        if row.get("m3_unknown") != _is_unknown(record):
            problems.append("JTL 처리 불명확 표시가 정규화 결과와 다르다")
        if row.get("m3_start_ns") != record.get("start_ns"):
            problems.append("JTL과 정규화 기록의 시작 시각이 다르다")
        if row.get("m3_decision_ns") != record.get("decision_ns"):
            problems.append("JTL과 정규화 기록의 결정 시각이 다르다")

    peak = _max_in_flight(records)
    concurrency = expected.get("concurrency")
    if concurrency is not None and peak > concurrency:
        problems.append(f"최대 진행 수 {peak} > 동시성 {concurrency}")
    if (concurrency is not None
            and completion.get("max_in_flight") is not None
            and completion["max_in_flight"] > concurrency):
        problems.append("완료 표식 최대 진행 수가 동시성을 넘었다")
    for thread, thread_peak in _per_thread_peak(records).items():
        if thread_peak > 1:
            problems.append(f"실행 주체 {thread}의 최대 진행 수가 {thread_peak}이다")

    warmup_rows = [row for row in all_rows
                   if row.get("label") in WARMUP_LABELS
                   and row.get("m3_phase") == "warmup"]
    if completion.get("warmup_completed") != WARMUP_TOTAL:
        problems.append("워밍업 완료 건수가 100이 아니다")
    if len(warmup_rows) != WARMUP_TOTAL:
        problems.append("JTL 워밍업 행 수가 100이 아니다")

    planned_n = expected.get("planned_n")
    if planned_n is not None:
        if len(records) != planned_n:
            problems.append("시도 수가 계획 N과 다르다")
        if computed["success"] != planned_n:
            problems.append("성공 수가 계획 N과 다르다")
    if completion.get("final_read_done") is not True:
        problems.append("최종 조회 미완료")
    if completion.get("final_read_ok") is not True:
        problems.append("최종 조회가 성공하지 않았다")
    if planned_n is not None and completion.get("final_value") != planned_n:
        problems.append("최종 수치가 계획 N과 다르다")
    if completion.get("processing_unknown"):
        problems.append("처리 여부 불명확 건수가 있다")
    if completion.get("records_written") != len(records):
        problems.append("기록 완료 건수와 정규화 기록 수가 다르다")

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
        starts = [record["start_ns"] for record in records
                  if record.get("start_ns") is not None]
        decisions = [record["decision_ns"] for record in records
                     if record.get("decision_ns") is not None]
        if starts and start_ns > min(starts):
            problems.append("전체 시작 시각이 첫 호출 시작보다 늦다")
        if decisions and end_ns != max(decisions):
            problems.append("전체 종료 시각이 마지막 결정 시각과 다르다")
        if decisions and any(decision < start_ns or decision > end_ns
                             for decision in decisions):
            problems.append("호출 결정 시각이 전체 구간 밖에 있다")
    if completion.get("measurement_complete") is not True:
        problems.append("측정 결정 완료 표식이 없다")
    if problems:
        raise IncompleteResultError(
            "실행 산출물 검산 실패: " + "; ".join(problems[:12]),
            {
                "problems": problems,
                "run_dir": str(run_dir),
                "attempts": len(records),
                "completion": completion,
            },
        )
    metrics = round_metrics(records, planned_n=planned_n, final_value=completion.get("final_value"),
                            duration_ns=duration_ns)
    return {
        "run_id": expected.get("run_id"),
        "target": expected.get("target"),
        "concurrency": concurrency,
        "attempts": len(records),
        "success": computed["success"],
        "final_value": completion.get("final_value"),
        "max_in_flight": peak,
        "duration_ns": duration_ns,
        "completion": completion,
        "metrics": metrics,
    }


# ---------------------------------------------------------------------------
# 진행 표식
# ---------------------------------------------------------------------------


class RunStateStore:
    """실제 대상 그룹이 공유하는 상태 위치에 실행 상태를 남긴다.

    결과 폴더가 바뀌어도 같은 state_dir를 쓰면 미해결 표식이 새 실행을 막는다.
    begin은 O_EXCL 잠금 파일을 먼저 잡아 동시 시작 경쟁을 막는다. 잠금이나
    표식이 남아 있으면 정상 완료로 간주하지 않는다(fail closed).
    """

    def __init__(self, root):
        self.root = Path(root)
        self.state_dir = self.root / ".state"
        self.lock_path = self.state_dir / "active.lock"

    def _path(self, run_id):
        safe = "".join(char if char.isalnum() or char in "-_." else "_" for char in run_id)
        return self.state_dir / f"{safe}.json"

    def _write(self, run_id, status, **extra):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {"run_id": run_id, "status": status, "updated_at": time.time(), **extra}
        target = self._path(run_id)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, target)

    def _acquire_lock(self, run_id):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise PendingRunError(
                "다른 실행이 진행 중이거나 잠금 표식이 남아 있다: "
                f"{self.lock_path}"
            ) from error
        try:
            os.write(descriptor, run_id.encode("utf-8"))
        finally:
            os.close(descriptor)

    def _release_lock(self):
        try:
            self.lock_path.unlink()
        except OSError:
            # 해제하지 못한 잠금은 다음 실행을 막는 fail-closed 표식으로 남긴다.
            pass

    def active(self):
        if not self.lock_path.is_file():
            return None
        try:
            return self.lock_path.read_text(encoding="utf-8").strip() or self.lock_path.name
        except OSError:
            return self.lock_path.name

    def _blocking_payloads(self):
        if not self.state_dir.is_dir():
            return []
        blocking = []
        for path in sorted(self.state_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {"run_id": path.stem, "status": "pending", "corrupt": True}
            if payload.get("status") in BLOCKING_STATES:
                blocking.append(payload)
        return blocking

    def begin(self, run_id, endpoint=None):
        self._acquire_lock(run_id)
        try:
            blocking = self._blocking_payloads()
            if blocking:
                names = ", ".join(str(payload.get("run_id") or "?") for payload in blocking)
                if endpoint is not None:
                    mismatched = [payload for payload in blocking
                                  if payload.get("endpoint") not in (None, endpoint)]
                    unknown = [payload for payload in blocking
                               if payload.get("endpoint") is None]
                    if mismatched or unknown:
                        raise PendingRunError(
                            "같은 state_dir에 다른/불명 대상의 미해결 표식이 있다: " + names
                        )
                raise PendingRunError(
                    "진행 중이거나 불명확하게 끝난 실행 표식이 있어 새 실행을 시작하지 않는다: "
                    + names
                )
            if self._path(run_id).exists():
                raise PendingRunError(f"이미 실행 기록이 있는 run_id다: {run_id}")
            self._write(run_id, "pending", endpoint=endpoint)
        except Exception:
            self._release_lock()
            raise

    def complete(self, run_id):
        existing = self.get(run_id) or {}
        self._write(run_id, "completed", endpoint=existing.get("endpoint"))
        self._release_lock()

    def fail(self, run_id, reason):
        existing = self.get(run_id) or {}
        self._write(run_id, "failed-unknown", reason=str(reason),
                    endpoint=existing.get("endpoint"))
        self._release_lock()

    def get(self, run_id):
        path = self._path(run_id)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def pending(self):
        return [str(payload.get("run_id") or "?")
                for payload in self._blocking_payloads()]

    def completed(self):
        if not self.state_dir.is_dir():
            return []
        completed = []
        for path in sorted(self.state_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("status") == "completed":
                completed.append(payload.get("run_id") or path.stem)
        return completed


# ---------------------------------------------------------------------------
# 실행 속성·명령
# ---------------------------------------------------------------------------


def _properties_escape(value):
    text = str(value)
    if "\n" in text or "\r" in text:
        raise InputError("속성 값에 줄바꿈을 넣을 수 없다")
    text = text.replace("\\", "\\\\")
    if text[:1] in (" ", "#", "!"):
        text = "\\" + text
    return text


def _jdbc_password(settings):
    jdbc = settings.get("jdbc") or {}
    password = jdbc.get("password")
    if password is None and jdbc.get("password_file"):
        try:
            password = Path(jdbc["password_file"]).read_text(
                encoding="utf-8").rstrip("\n")
        except OSError as error:
            raise InputError(f"JDBC 비밀번호 파일을 읽을 수 없다: {error}") from error
    return password


def _redact_password(run_dir, password):
    if not password:
        return
    for path in Path(run_dir).iterdir():
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if password in text:
            path.write_text(text.replace(password, "***"), encoding="utf-8")


def runtime_properties(settings, *, target, concurrency, round_index, run_dir,
                       include_secret=True):
    """JMeter에 넘길 속성. 비밀은 include_secret인 파일에만 둔다."""
    transport = settings.get("transport_timeout_ms") or settings["request_timeout_ms"]
    request_timeout = int(settings["request_timeout_ms"])
    warmup_per_thread = WARMUP_TOTAL // int(concurrency)
    if WARMUP_TOTAL % int(concurrency):
        raise InputError("동시성은 워밍업 100건을 나누어떨어져야 한다")
    http = settings.get("http") or {}
    jdbc = settings.get("jdbc") or {}
    password = _jdbc_password(settings)
    if target == "jdbc" and password is None:
        raise InputError("jdbc 비밀번호 입력 없음")
    password = password or ""
    jdbc_url = jdbc.get("url") or ""
    jdbc_user = jdbc.get("user") or ""
    if target == "jdbc" and (not jdbc_url or not jdbc_user):
        raise InputError("jdbc url·user 입력 없음")
    if target == "jdbc":
        jdbc_url = prepare_jdbc_url(jdbc_url, int(transport))
    properties = {
        "m3.run_id": settings["run_id"],
        "m3.target": target,
        "m3.target.http": "true" if target == "http" else "false",
        "m3.target.jdbc": "true" if target == "jdbc" else "false",
        "m3.round": round_index,
        "m3.n": settings["n"],
        "m3.c": concurrency,
        "m3.warmup_total": WARMUP_TOTAL,
        "m3.warmup_per_thread": warmup_per_thread,
        "m3.request_timeout_ms": request_timeout,
        "m3.transport_timeout_ms": transport,
        "m3.total_limit_ms": settings["total_limit_ms"],
        "m3.prep_limit_ms": settings["prep_limit_ms"],
        "m3.finish_limit_ms": settings["finish_limit_ms"],
        "m3.http.host": http.get("host", ""),
        "m3.http.port": http.get("port", ""),
        "m3.http.scheme": http.get("scheme", ""),
        "m3.http.connect_timeout_ms": transport,
        "m3.http.response_timeout_ms": transport,
        "m3.jdbc.url": jdbc_url,
        "m3.jdbc.user": jdbc_user,
        "m3.jdbc.password": password,
        # 서버 측 Statement.setQueryTimeout은 사용하지 않는다. 전송 한도는 URL에 둔다.
        "m3.jdbc.query_timeout_s": 0,
        "m3.prep_jdbc_timeout_s": 0,
        "m3.finish_jdbc_timeout_s": 0,
        "m3.run_dir": str(Path(run_dir).resolve()),
        "m3.calls_file": str((Path(run_dir) / "calls.csv").resolve()),
        "m3.completion_file": str((Path(run_dir) / "completion.json").resolve()),
    }
    if not include_secret:
        properties.pop("m3.jdbc.password", None)
        properties.pop("m3.jdbc.user", None)
    return properties


def write_runtime_properties(path, settings, *, run_dir, target="http",
                             concurrency=1, round_index=1, include_secret=True):
    """속성 파일을 0600으로 쓴다. 같은 경로를 덮어쓸 때도 먼저 권한을 좁힌다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    properties = runtime_properties(
        settings, target=target, concurrency=concurrency,
        round_index=round_index, run_dir=run_dir,
        include_secret=include_secret,
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for name, value in properties.items():
                handle.write(f"{name}={_properties_escape(value)}\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    return path


def build_jmeter_command(settings, *, jmx_path, props_path, jtl_path,
                         log_path, cwd=None):
    """JMeter 명령행. 비밀은 포함하지 않는다(속성 파일에서 읽는다)."""
    java_home = settings.get("java_home")
    del java_home  # 환경 변수는 subprocess env로 전달한다.
    command = [
        str(settings["jmeter_bin"]),
        "-n",
        "-t", str(jmx_path),
    ]
    # 고정 옵션을 먼저, 실행별 속성을 나중에 읽어 실행 값이 이기게 한다.
    if settings.get("properties"):
        command.extend(["-q", str(settings["properties"])])
    command.extend([
        "-q", str(props_path),
        "-l", str(jtl_path),
        "-j", str(log_path),
    ])
    return command


def _sanitized_settings(settings):
    copy = json.loads(json.dumps(settings, default=str))
    if isinstance(copy.get("jdbc"), dict):
        copy["jdbc"].pop("password", None)
        copy["jdbc"].pop("password_file", None)
        copy["jdbc"].pop("user", None)
    return copy


def _process_timeout_seconds(settings):
    total = (int(settings["prep_limit_ms"]) + int(settings["total_limit_ms"])
             + int(settings["finish_limit_ms"])) / 1000
    return max(30.0, total + 60.0)


def default_runner(settings, run_dir):
    """자식 JMeter 하나만 시작하고 자기 자식만 종료한다."""
    run_dir = Path(run_dir)
    jtl = run_dir / "result.jtl"
    log = run_dir / "jmeter.log"
    props = Path(settings.get("runtime_properties_path")
                 or (run_dir / "run.properties"))
    command = build_jmeter_command(
        settings, jmx_path=Path(settings["jmx"]), props_path=props,
        jtl_path=jtl, log_path=log,
    )
    environment = dict(os.environ)
    if settings.get("java_home"):
        environment["JAVA_HOME"] = str(settings["java_home"])
    timeout = _process_timeout_seconds(settings)
    try:
        process = subprocess.run(
            command, cwd=Path(settings["jmx"]).parent, env=environment,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        return {
            "returncode": None,
            "timed_out": True,
            "stdout": (error.stdout or "") if isinstance(error.stdout, str) else "",
            "stderr": (error.stderr or "") if isinstance(error.stderr, str) else "",
            "command": command,
        }
    return {
        "returncode": process.returncode,
        "timed_out": False,
        "stdout": process.stdout,
        "stderr": process.stderr,
        "command": command,
    }


def _validate_run_id(run_id):
    if not isinstance(run_id, str) or not run_id:
        raise InputError("run_id는 비어 있지 않은 문자열이어야 한다")
    if any(char in run_id for char in ("/", "\\", "..")) or run_id.startswith("."):
        raise InputError("run_id에 경로 문자를 넣을 수 없다")
    return run_id


def run_single(settings, *, target, concurrency, round_index, runner=None):
    """대상·동시성·회차 하나를 실행하고 정상 완료까지 검산한다."""
    if target not in TARGETS:
        raise InputError(f"대상은 {TARGETS} 중 하나여야 한다")
    if concurrency not in CONCURRENCIES:
        raise InputError(f"동시성은 {CONCURRENCIES} 중 하나여야 한다")
    if not isinstance(round_index, int) or round_index <= 0:
        raise InputError("회차는 1 이상의 정수여야 한다")
    if not settings.get("state_dir"):
        raise InputError("state_dir: 실제 대상 그룹이 공유하는 상태 위치가 필요하다")
    runner = runner or default_runner
    results_dir = Path(settings["results_dir"]).resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    state_root = Path(settings["state_dir"]).resolve()
    run_id = settings.get("run_id") or (
        f"{time.strftime('%Y%m%dT%H%M%S')}-{target}-c{concurrency}-r{round_index}"
    )
    _validate_run_id(run_id)
    settings = dict(settings)
    settings["run_id"] = run_id
    store = RunStateStore(state_root)
    store.begin(run_id, endpoint=endpoint_key(settings, target))
    run_dir = results_dir / run_id
    try:
        if run_dir.exists():
            raise IncompleteResultError(
                "결과 폴더를 덮어쓰지 않는다", {"run_dir": str(run_dir)})
        run_dir.mkdir()
        (run_dir / "run_meta.json").write_text(
            json.dumps({
                "run_id": run_id,
                "target": target,
                "concurrency": concurrency,
                "round": round_index,
                "settings": _sanitized_settings(settings),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 결과 폴더에는 비밀 없는 실행 속성만 남긴다.
        write_runtime_properties(
            run_dir / "run.properties", settings, run_dir=run_dir,
            target=target, concurrency=concurrency, round_index=round_index,
            include_secret=False,
        )
        # 비밀은 결과 밖의 사용자 전용 임시 파일로만 JMeter에 전달하고 지운다.
        descriptor, secret_path = tempfile.mkstemp(prefix="m3-secret-", suffix=".properties")
        os.close(descriptor)
        try:
            write_runtime_properties(
                secret_path, settings, run_dir=run_dir, target=target,
                concurrency=concurrency, round_index=round_index,
                include_secret=True,
            )
            child_settings = dict(settings)
            child_settings["runtime_properties_path"] = secret_path
            outcome = runner(child_settings, run_dir)
        finally:
            try:
                os.unlink(secret_path)
            except OSError:
                pass
        (run_dir / "stdout.log").write_text(
            (outcome.get("stdout") or "") + (outcome.get("stderr") or ""),
            encoding="utf-8",
        )
        _redact_password(run_dir, _jdbc_password(settings))
        if outcome.get("timed_out"):
            raise IncompleteResultError(
                "JMeter 프로세스가 종료 한도 안에 끝나지 않았다. 자기 자식만 종료했다",
                {"run_dir": str(run_dir), "timed_out": True},
            )
        if outcome.get("returncode") != 0:
            raise IncompleteResultError(
                f"JMeter 종료 코드 {outcome.get('returncode')}",
                {"run_dir": str(run_dir), "returncode": outcome.get("returncode")},
            )
        result = validate_run_artifacts(run_dir, expected={
            "run_id": run_id,
            "planned_n": settings["n"],
            "concurrency": concurrency,
            "target": target,
        })
        store.complete(run_id)
        return result
    except Exception as error:
        store.fail(run_id, str(error))
        raise


# ---------------------------------------------------------------------------
# 묶음 실행
# ---------------------------------------------------------------------------


def campaign_plan(targets=TARGETS, rounds=ROUNDS):
    """C 1·10·50·100, 회차 홀수는 HTTP→JDBC, 짝수는 JDBC→HTTP."""
    plan = []
    for concurrency in CONCURRENCIES:
        for round_index in range(1, rounds + 1):
            order = targets if round_index % 2 == 1 else tuple(reversed(targets))
            for target in order:
                plan.append({
                    "target": target,
                    "concurrency": concurrency,
                    "round": round_index,
                })
    return plan


def _run_plan(settings, plan, *, stage, runner=None):
    results_dir = Path(settings["results_dir"]).resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    campaign_id = _validate_run_id(settings.get("run_id")
                                   or time.strftime("%Y%m%dT%H%M%S"))
    records = []
    stopped = None
    for index, item in enumerate(plan):
        run_id = f"{campaign_id}-{item['target']}-c{item['concurrency']}-r{item['round']}"
        child = dict(settings)
        child["run_id"] = run_id
        try:
            result = run_single(child, target=item["target"],
                                concurrency=item["concurrency"],
                                round_index=item["round"], runner=runner)
        except (IncompleteResultError, PendingRunError, InputError) as error:
            stopped = {
                "index": index,
                "item": item,
                "error": str(error),
                "attempts": getattr(error, "details", {}).get("attempts"),
            }
            records.append({**item, "run_id": run_id, "status": "failed",
                            "error": str(error)})
            for remaining in plan[index + 1:]:
                records.append({**remaining,
                                "run_id": (f"{campaign_id}-{remaining['target']}"
                                           f"-c{remaining['concurrency']}-r{remaining['round']}"),
                                "status": "not_run"})
            break
        records.append({**item, "run_id": run_id, "status": "completed",
                        "metrics": result["metrics"]})
    summary = {
        "stage": stage,
        "campaign_id": campaign_id,
        "planned": len(plan),
        "executed": sum(1 for record in records if record["status"] == "completed"),
        "stopped": stopped,
        "runs": records,
    }
    if stage == "campaign" and stopped is None and len(records) == len(plan):
        groups = {}
        for record in records:
            key = (record["target"], record["concurrency"])
            groups.setdefault(key, []).append(record["metrics"])
        summary["summaries"] = {}
        for key, metrics in groups.items():
            summary["summaries"][f"{key[0]}-c{key[1]}"] = summarize_rounds(metrics)
    (results_dir / f"{stage}-{campaign_id}-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def run_pilot(settings, runner=None):
    validate_pilot_inputs(settings)
    plan = [{"target": target, "concurrency": concurrency, "round": 1}
            for concurrency in CONCURRENCIES for target in TARGETS]
    return _run_plan(settings, plan, stage="pilot", runner=runner)


def run_campaign(settings, runner=None):
    validate_baseline_inputs(settings)
    return _run_plan(settings, campaign_plan(), stage="campaign", runner=runner)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_environment(path):
    path = Path(path)
    if not path.is_file():
        raise InputError(f"환경 기록 파일이 없다: {path}")
    try:
        environment = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise InputError(f"환경 기록 파일을 읽을 수 없다: {error}") from error
    if not isinstance(environment, dict):
        raise InputError("환경 기록은 JSON 객체여야 한다")
    return environment


def _jdbc_settings(args):
    password = None
    password_file = args.jdbc_password_file or os.environ.get("M3_JDBC_PASSWORD_FILE")
    if password_file:
        try:
            password = Path(password_file).read_text(encoding="utf-8").rstrip("\n")
        except OSError as error:
            raise InputError(f"JDBC 비밀번호 파일을 읽을 수 없다: {error}") from error
    elif os.environ.get("M3_JDBC_PASSWORD"):
        password = os.environ["M3_JDBC_PASSWORD"]
    return {
        "url": args.jdbc_url or os.environ.get("M3_JDBC_URL"),
        "user": args.jdbc_user or os.environ.get("M3_JDBC_USER"),
        "password": password,
        "password_file": password_file,
    }


def _http_settings(args):
    port = args.http_port or os.environ.get("M3_HTTP_PORT")
    if port is not None:
        try:
            port = int(port)
        except ValueError as error:
            raise InputError("http 포트는 정수여야 한다") from error
    return {
        "host": args.http_host or os.environ.get("M3_HTTP_HOST"),
        "port": port,
        "scheme": args.http_scheme or os.environ.get("M3_HTTP_SCHEME", "http"),
    }


def _settings(args):
    jmeter = args.jmeter or os.environ.get("M3_JMETER_BIN")
    if not jmeter:
        raise InputError("JMeter 실행 파일 경로가 없다(--jmeter 또는 M3_JMETER_BIN)")
    java_home = args.java_home or os.environ.get("M3_JAVA_HOME") or os.environ.get("JAVA_HOME")
    if not java_home:
        raise InputError("JAVA_HOME이 없다(--java-home 또는 M3_JAVA_HOME)")
    jmx = Path(args.jmx) if args.jmx else Path(__file__).with_name("counter.jmx")
    properties = (Path(args.jmeter_properties) if args.jmeter_properties
                  else Path(__file__).with_name("jmeter.properties"))
    return {
        "results_dir": args.results_dir,
        "state_dir": args.state_dir,
        "run_id": args.run_id,
        "n": args.n,
        "request_timeout_ms": args.request_timeout_ms,
        "transport_timeout_ms": args.transport_timeout_ms or args.request_timeout_ms,
        "total_limit_ms": args.total_limit_ms,
        "prep_limit_ms": args.prep_limit_ms,
        "finish_limit_ms": args.finish_limit_ms,
        "environment": _load_environment(args.environment_file),
        "http": _http_settings(args),
        "jdbc": _jdbc_settings(args),
        "jmeter_bin": jmeter,
        "java_home": java_home,
        "jmx": str(jmx),
        "properties": str(properties),
    }


def _add_measurement_arguments(parser):
    parser.add_argument("--results-dir", required=True,
                        help="결과를 저장할 새 폴더(묶음별)")
    parser.add_argument("--state-dir", required=True,
                        help="같은 실제 대상 그룹이 공유하는 미해결 상태 위치")
    parser.add_argument("--n", type=int, required=True,
                        help="전체 시도 요청 수(본 측정은 1,000~10,000 합의값)")
    parser.add_argument("--request-timeout-ms", type=int, required=True,
                        help="요청별 논리적 마감(ms)")
    parser.add_argument("--total-limit-ms", type=int, required=True,
                        help="전체 실행 한도(ms)")
    parser.add_argument("--prep-limit-ms", type=int, required=True,
                        help="준비·워밍업·시작 조율 한도(ms)")
    parser.add_argument("--finish-limit-ms", type=int, required=True,
                        help="종료 확인·최종 조회 한도(ms)")
    parser.add_argument("--environment-file", required=True,
                        help="JMeter·JDK·MySQL·드라이버·Python·Flask·Werkzeug 버전 JSON")
    parser.add_argument("--run-id", default=None, help="묶음/실행 식별자(선택)")
    parser.add_argument("--jmeter", default=None, help="JMeter 실행 파일")
    parser.add_argument("--java-home", default=None, help="JDK 경로")
    parser.add_argument("--jmx", default=None, help="counter.jmx 경로")
    parser.add_argument("--jmeter-properties", default=None,
                        help="jmeter.properties 경로")
    parser.add_argument("--http-host", default=None, help="Mini Redis 호스트")
    parser.add_argument("--http-port", type=int, default=None, help="Mini Redis 포트")
    parser.add_argument("--http-scheme", default=None, help="http 또는 https")
    parser.add_argument("--jdbc-url", default=None, help="시험 전용 JDBC URL")
    parser.add_argument("--jdbc-user", default=None, help="시험 전용 DB 계정")
    parser.add_argument("--jdbc-password-file", default=None,
                        help="DB 비밀번호 파일(명령행에 비밀번호를 쓰지 않는다)")
    parser.add_argument("--transport-timeout-ms", type=int, default=None,
                        help="통신 차단 한도(기본: 요청 한도와 동일)")


def build_parser():
    parser = argparse.ArgumentParser(
        description="시나리오 03 성능 비교 실행 담당(JMeter 단일 프로세스, 집계만 수행)")
    sub = parser.add_subparsers(dest="command", required=True)

    unit = sub.add_parser("unit", help="JMeter 없이 집계·요약 계약 자체 점검")
    unit.set_defaults(handler=_handle_unit)

    test = sub.add_parser("test", help="pytest 계약·도구 검증 실행")
    test.add_argument("--pytest-arg", action="append", default=[],
                      help="pytest에 추가로 전달할 인자(여러 번 사용 가능)")
    test.set_defaults(handler=_handle_test)

    single = sub.add_parser("single", help="대상·동시성·회차 하나 실행")
    _add_measurement_arguments(single)
    single.add_argument("--target", choices=TARGETS, required=True)
    single.add_argument("--concurrency", type=int, choices=CONCURRENCIES,
                        required=True)
    single.add_argument("--round", type=int, required=True, dest="round_index")
    single.set_defaults(handler=_handle_single)

    pilot = sub.add_parser("pilot", help="N 후보 1,000/10,000 예비 실행")
    _add_measurement_arguments(pilot)
    pilot.set_defaults(handler=_handle_pilot)

    campaign = sub.add_parser("campaign", help="본 비교 5회×동시성 4×대상 2")
    _add_measurement_arguments(campaign)
    campaign.set_defaults(handler=_handle_campaign)
    return parser


def _handle_unit(args):
    try:
        _self_check()
    except AssertionError as error:
        print(f"UNIT FAIL: {error}", file=sys.stderr)
        return 1
    print("UNIT PASS: percentile·round_metrics·summarize_rounds 계약")
    return 0


def _self_check():
    samples = [float(value) for value in range(1, 101)]
    assert abs(percentile(samples, 0.50) - 50.5) < 1e-9
    assert abs(percentile(samples, 0.95) - 95.05) < 1e-9
    assert abs(percentile(samples, 0.99) - 99.01) < 1e-9
    assert percentile([], 0.5) is None
    one = round_metrics([
        {"phase": "measure", "outcome": "success", "client_elapsed_ns": 3_000_000,
         "processing_unknown": False, "decision_ns": 1, "start_ns": 0}
    ], planned_n=1, final_value=1, duration_ns=1_000_000_000)
    assert one["p50_ms"] == 3.0 and one["p95_ms"] == 3.0 and one["p99_ms"] == 3.0
    rounds = [{"p50_ms": value, "passed": True} for value in (10, 20, 30, 40, 50)]
    summary = summarize_rounds(rounds)
    assert summary["p50_ms"] == {"median": 30, "min": 10, "max": 50}
    try:
        summarize_rounds(rounds[:4])
    except IncompleteResultError:
        pass
    else:
        raise AssertionError("5회가 아닌 요약을 거절하지 않았다")


def _handle_test(args):
    command = [sys.executable, "-m", "pytest",
               str(Path(__file__).with_name("test_measurement_contract.py")),
               *args.pytest_arg]
    return subprocess.call(command)


def _handle_single(args):
    settings = _settings(args)
    validate_single_inputs(settings, target=args.target)
    result = run_single(settings, target=args.target,
                        concurrency=args.concurrency,
                        round_index=args.round_index)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _handle_pilot(args):
    settings = _settings(args)
    summary = run_pilot(settings)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["stopped"] is None else 1


def _handle_campaign(args):
    settings = _settings(args)
    summary = run_campaign(settings)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["stopped"] is None and "summaries" in summary else 1


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (InputError, IncompleteResultError, PendingRunError) as error:
        print(f"실행 거절: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
