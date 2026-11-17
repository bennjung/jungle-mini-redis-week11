"""핵심 TTL 코드에 수동 변이를 적용하고 TTL 테스트가 잡는지 확인한다.

각 변이는 storage.py에 잠깐 적용한 뒤 원본으로 되돌린다.
실행: .venv/bin/python tools/mutation_check.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORAGE = ROOT / "storage.py"
TEST_FILES = [
    "tests/scenario_01_storage/test_ttl_storage.py",
    "tests/scenario_01_storage/test_ttl_active.py",
    "tests/scenario_01_storage/test_ttl_http.py",
    "tests/scenario_01_storage/test_ttl_timer.py",
]

MUTANTS = [
    {
        "name": "lazy-expire-boundary",
        "old": "        if now < self._expires[key]:\n            return False",
        "new": "        if now <= self._expires[key]:\n            return False",
    },
    {
        "name": "expire-zero-not-delete",
        "old": '        if ttl <= 0:\n            self._drop_key(key)\n            return {"found": True, "ttl": 0}',
        "new": '        if ttl < 0:\n            self._drop_key(key)\n            return {"found": True, "ttl": 0}',
    },
    {
        "name": "set-keeps-old-ttl",
        "old": "        if ttl is None:\n            self._clear_ttl(key)",
        "new": "        if ttl is None:\n            pass",
    },
    {
        "name": "incr-clears-ttl",
        "old": '        self._data[key] = result\n        return {"ok": True, "value": result}',
        "new": '        self._clear_ttl(key)\n        self._data[key] = result\n        return {"ok": True, "value": result}',
    },
    {
        "name": "swap-pop-position-stale",
        "old": "            self._ttl_pos[last] = index",
        "new": "            pass",
    },
    {
        "name": "active-25-boundary",
        "old": "            if size == 0 or expired * 4 < size:",
        "new": "            if size == 0 or expired * 4 <= size:",
    },
    {
        "name": "active-exact-expiry-skipped",
        "old": "            if expiry_ms is None or now_ms < expiry_ms:",
        "new": "            if expiry_ms is None or now_ms <= expiry_ms:",
    },
    {
        "name": "ttl-remaining-off-by-one",
        "old": '        return {"found": True, "ttl": self._expires[key] - now}',
        "new": '        return {"found": True, "ttl": self._expires[key] - now + 1}',
    },
    {
        "name": "expire-expiry-off-by-one",
        "old": '        self._set_ttl(key, now + ttl)\n        return {"found": True, "ttl": ttl}',
        "new": '        self._set_ttl(key, now + ttl - 1)\n        return {"found": True, "ttl": ttl}',
    },
    {
        "name": "ttl-without-ttl-zero",
        "old": '            return {"found": True, "ttl": -1}',
        "new": '            return {"found": True, "ttl": 0}',
    },
    {
        "name": "persist-inverted",
        "old": '            return {"found": True, "persisted": False}',
        "new": '            return {"found": True, "persisted": True}',
    },
    {
        "name": "delete-keeps-ttl",
        "old": "        value = self._data[key]\n        self._drop_key(key)",
        "new": "        value = self._data[key]\n        self._data.pop(key)",
    },
    {
        "name": "active-max-cycles-one",
        "old": "    _ACTIVE_MAX_CYCLES = 50",
        "new": "    _ACTIVE_MAX_CYCLES = 1",
    },
    {
        "name": "active-budget-zero",
        "old": "    _ACTIVE_TIME_BUDGET_S = 0.001",
        "new": "    _ACTIVE_TIME_BUDGET_S = 0.0",
    },
]


def run_tests():
    command = [
        sys.executable,
        "-m",
        "pytest",
        *TEST_FILES,
        "-q",
        "--tb=no",
        "-p",
        "no:cacheprovider",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "TIMEOUT"
    output = (result.stdout or "") + (result.stderr or "")
    failure = next(
        (line.strip() for line in output.splitlines() if line.startswith("FAILED")),
        "",
    )
    return result.returncode, failure, output


def main():
    original = STORAGE.read_text(encoding="utf-8")
    if STORAGE.read_text(encoding="utf-8") != original:
        raise SystemExit("storage.py 읽기 불일치")
    results = []
    try:
        for mutant in MUTANTS:
            mutated = original.replace(mutant["old"], mutant["new"], 1)
            if mutated == original:
                results.append((mutant["name"], "NOT_APPLIED", mutant["old"][:50]))
                continue
            STORAGE.write_text(mutated, encoding="utf-8")
            code, failure, output = run_tests()
            if code == 0:
                status = "SURVIVED"
            elif code == 124:
                status = "TIMEOUT"
            else:
                status = "KILLED"
            results.append((mutant["name"], status, failure))
    finally:
        STORAGE.write_text(original, encoding="utf-8")

    print("\nMutation results")
    print("-" * 90)
    for name, status, detail in results:
        print(f"{name:32} {status:12} {detail}")
    killed = sum(1 for _, status, _ in results if status == "KILLED")
    survived = sum(1 for _, status, _ in results if status == "SURVIVED")
    print("-" * 90)
    print(f"killed={killed} survived={survived} total={len(results)}")
    print(f"storage.py restored: {STORAGE.read_text(encoding='utf-8') == original}")

    if any(status in {"SURVIVED", "NOT_APPLIED", "TIMEOUT"} for _, status, _ in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
