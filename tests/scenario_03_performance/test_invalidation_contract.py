"""시나리오 03 무효화 하네스의 집계·검증 단위 계약."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import run_invalidation as inv
import run_performance as base


def _record(operation, sequence, start_ns, elapsed_ns=1_000_000):
    return {
        "operation": operation,
        "request_sequence": sequence,
        "thread_name": "m3-measure 1-1",
        "outcome": "success",
        "client_elapsed_ns": elapsed_ns,
        "start_ns": start_ns,
        "decision_ns": start_ns + elapsed_ns,
    }


def _cache_records():
    records = []
    sequence = 0
    for operation, count in (
        ("get_hit", 4), ("delete", 2), ("get_miss", 2), ("put_refill", 2)
    ):
        for _ in range(count):
            sequence += 1
            records.append(_record(operation, sequence, sequence * 10_000_000))
    return records


def _round(passed=True, throughput=1000.0, p50=1.0, p95=2.0, p99=3.0):
    return {
        "passed": passed,
        "throughput_per_second": throughput,
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
        "success_rate": 1.0,
        "per_operation": {
            operation: {
                "throughput_per_second": throughput,
                "p50_ms": p50,
                "p95_ms": p95,
                "p99_ms": p99,
            }
            for operation in inv.CACHE_OPS
        },
    }


def test_round_metrics_counts_per_operation():
    records = _cache_records()
    completion = {"normal": True, "final_value": 4}
    settings = {"cache_k": 4}
    metrics = inv.round_metrics(
        records, planned_n=10, duration_ns=100_000_000,
        scenario="cache_lifecycle", completion=completion, settings=settings,
    )
    assert metrics["attempts"] == 10
    assert metrics["success_count"] == 10
    assert metrics["passed"] is True
    assert metrics["per_operation"]["get_hit"]["attempts"] == 4
    assert metrics["per_operation"]["delete"]["attempts"] == 2
    assert metrics["per_operation"]["get_miss"]["attempts"] == 2
    assert metrics["per_operation"]["put_refill"]["attempts"] == 2


def test_summarize_rounds_rejects_failed_round():
    rounds = [_round() for _ in range(4)] + [_round(passed=False)]
    with pytest.raises(base.IncompleteResultError):
        inv.summarize_rounds(rounds, "cache_lifecycle")


def test_summarize_rounds_uses_median_and_range():
    rounds = [_round(throughput=value) for value in (10.0, 20.0, 30.0, 40.0, 50.0)]
    summary = inv.summarize_rounds(rounds, "cache_lifecycle")
    assert summary["throughput_per_second"]["median"] == 30.0
    assert summary["throughput_per_second"]["min"] == 10.0
    assert summary["throughput_per_second"]["max"] == 50.0
    assert summary["operations"]["get_hit"]["throughput_per_second"]["median"] == 30.0


def test_invalidation_properties_cache_phase_loops():
    settings = {
        "n": 10_000,
        "request_timeout_ms": 5000,
        "total_limit_ms": 360_000,
        "prep_limit_ms": 30_000,
        "finish_limit_ms": 15_000,
        "http": {"host": "127.0.0.1", "port": 5000, "scheme": "http"},
        "cache_k": 4000,
        "cache_ttl_ms": 600_000,
        "cache_value": "V",
        "cold_k": 10_000,
        "cold_ttl_ms": 5000,
        "cold_value": "V",
        "hot_key": "weather:hot",
        "hot_value": "H",
    }
    values = inv.invalidation_properties(
        settings, scenario="cache_lifecycle", server_id="flask", c=100,
        round_index=1, run_dir="/tmp/example",
    )
    assert values["m3.warmup_per_thread"] == 4
    assert values["m3.warmup_total"] == 400
    assert values["m3.cache.phase1_loops"] == 40
    assert values["m3.cache.phase2_loops"] == 20
    assert values["m3.scenario.cache"] == "true"
    assert values["m3.scenario.expiry"] == "false"


def test_invalidation_properties_expiry_loops():
    settings = {
        "n": 10_000,
        "request_timeout_ms": 5000,
        "total_limit_ms": 360_000,
        "prep_limit_ms": 30_000,
        "finish_limit_ms": 15_000,
        "http": {"host": "127.0.0.1", "port": 5000, "scheme": "http"},
        "cache_k": 4000,
        "cache_ttl_ms": 600_000,
        "cache_value": "V",
        "cold_k": 10_000,
        "cold_ttl_ms": 5000,
        "cold_value": "V",
        "hot_key": "weather:hot",
        "hot_value": "H",
    }
    values = inv.invalidation_properties(
        settings, scenario="expiry_backlog", server_id="fastapi_head", c=100,
        round_index=1, run_dir="/tmp/example",
    )
    assert values["m3.warmup_per_thread"] == 1
    assert values["m3.warmup_total"] == 100
    assert values["m3.expiry.loops"] == 100
    assert values["m3.scenario.cache"] == "false"
    assert values["m3.scenario.expiry"] == "true"
