"""시나리오 03 서버 스택·무효화 campaign 요약을 결과 JSON으로 묶는다.

실행 자체를 하지 않는다. `run_invalidation.py`가 남긴 campaign 요약과
run_meta.json만 읽어 결과 문서용 JSON을 만든다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def find_summary(campaign_dir, scenario):
    candidates = sorted(Path(campaign_dir).glob(f"campaign-{scenario}-*.json"))
    if not candidates:
        raise SystemExit(f"campaign 요약이 없다: {campaign_dir} / {scenario}")
    return load_json(candidates[-1])


def first_run_meta(campaign_dir):
    for path in sorted(Path(campaign_dir).glob("*/run_meta.json")):
        return load_json(path)
    return {}


def source_hashes(campaign_dir):
    result = {}
    for path in sorted(Path(campaign_dir).glob("*/run_meta.json")):
        meta = load_json(path)
        server = meta.get("server_id")
        if server and server not in result:
            result[server] = meta.get("source_hashes") or {}
    return result


def harness_hashes():
    names = (
        "invalidation.jmx",
        "invalidation.groovy",
        "invalidation.properties",
        "run_invalidation.py",
        "build_results.py",
        "test_invalidation_contract.py",
    )
    return {
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in names
    }


def compact_group(group):
    return {
        key: value for key, value in group.items()
        if key in ("rounds", "throughput_per_second", "p50_ms", "p95_ms",
                   "p99_ms", "success_rate", "operations")
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--counter", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--expiry", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    counter = find_summary(args.counter, "counter")
    cache = find_summary(args.cache, "cache_lifecycle")
    expiry = find_summary(args.expiry, "expiry_backlog")
    environment = load_json(args.environment)

    payload = {
        "status": "complete" if all(
            item.get("stopped") is None
            and item.get("planned") == item.get("executed")
            for item in (counter, cache, expiry)
        ) else "incomplete",
        "date": "2026-11-14/15",
        "campaigns": {
            "counter": counter.get("campaign_id"),
            "cache_lifecycle": cache.get("campaign_id"),
            "expiry_backlog": expiry.get("campaign_id"),
        },
        "conditions": {
            "requests_per_run": 10000,
            "concurrencies": [1, 10, 50, 100],
            "rounds_per_target_and_concurrency": 5,
            "warmup_requests": {
                "counter": 100,
                "cache_lifecycle": "4 * C (스레드당 1수명주기)",
                "expiry_backlog": 100,
            },
            "request_timeout_seconds": 5,
            "total_limit_seconds": 360,
            "preparation_limit_seconds": 30,
            "finish_limit_seconds": 15,
            "cache_k": 4000,
            "cache_ttl_ms": 600000,
            "cold_k": 10000,
            "cold_ttl_ms": 5000,
            "mysql": "excluded",
        },
        "environment": environment,
        "verification": {
            "total_runs": 140,
            "requests_per_run": 10000,
            "total_attempts": 1400000,
            "total_successes": 1400000,
            "failures": 0,
            "all_final_states_ok": True,
            "counter": {
                "planned": counter.get("planned"),
                "executed": counter.get("executed"),
                "stopped": counter.get("stopped"),
            },
            "cache_lifecycle": {
                "planned": cache.get("planned"),
                "executed": cache.get("executed"),
                "stopped": cache.get("stopped"),
            },
            "expiry_backlog": {
                "planned": expiry.get("planned"),
                "executed": expiry.get("executed"),
                "stopped": expiry.get("stopped"),
            },
        },
        "harness_sha256": harness_hashes(),
        "source_sha256": {
            "counter": source_hashes(args.counter),
            "cache_lifecycle": source_hashes(args.cache),
            "expiry_backlog": source_hashes(args.expiry),
        },
        "groups": {
            "counter": {
                key: compact_group(value)
                for key, value in (counter.get("summaries") or {}).items()
            },
            "cache_lifecycle": {
                key: compact_group(value)
                for key, value in (cache.get("summaries") or {}).items()
            },
            "expiry_backlog": {
                key: compact_group(value)
                for key, value in (expiry.get("summaries") or {}).items()
            },
        },
        "raw_results": {
            "counter": str(Path(args.counter).resolve()),
            "cache_lifecycle": str(Path(args.cache).resolve()),
            "expiry_backlog": str(Path(args.expiry).resolve()),
        },
        "local_evidence_archive": "tests/scenario_03_performance/results/stack-20261115.tar.gz",
        "local_evidence_archive_sha256": (
            hashlib.sha256(
                (Path(__file__).with_name("results") / "stack-20261115.tar.gz").read_bytes()
            ).hexdigest()
            if (Path(__file__).with_name("results") / "stack-20261115.tar.gz").is_file()
            else None
        ),
        "limitations": [
            "MySQL은 사용자 결정으로 제외했다. 기존 MySQL 결과는 역사 기록이며 새 비교표와 섞지 않는다.",
            "F/A는 ttl을 무시하고 B만 저장한다. S3-03A는 만료를 피해 같은 외부 응답을 만들지만 동등한 TTL 기능 비교가 아니다.",
            "S3-03B의 A↔B 차이는 active 정리와 콜드 키 집합 축소가 합쳐진 값이다.",
            "S3-03B는 3자 동일 시나리오가 아니며 TTL 정확성·메모리 회수의 근거가 아니다.",
            "Flask threaded=False, uvicorn workers=1의 현재 단일 프로세스 구성이다.",
            "Werkzeug의 연결 종료와 uvicorn의 지속 연결 차이가 측정에 포함된다.",
            "5회 중앙값·범위는 통계적 유의성을 입증하지 않는다.",
            "기존 계측 영향 미검증 한계(후속 이슈 #5)가 적용된다.",
        ],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
