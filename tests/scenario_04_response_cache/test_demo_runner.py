import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def require_free_demo_ports():
    for port in (5000, 8000):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                pytest.skip(f"데모 포트 {port}가 이미 사용 중입니다")


def test_demo_runner_starts_and_stops_both_servers():
    runner = ROOT / "demo" / "run.py"
    assert runner.exists(), "데모 실행 스크립트가 필요합니다"
    process = subprocess.Popen(
        [sys.executable, str(runner)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        pending = {
            "http://127.0.0.1:5000/kv?key=demo-runner-test",
            "http://127.0.0.1:8000/",
        }
        while pending and time.monotonic() < deadline and process.poll() is None:
            for url in pending.copy():
                try:
                    with urllib.request.urlopen(url, timeout=0.5) as response:
                        if response.status == 200:
                            pending.remove(url)
                except (urllib.error.URLError, TimeoutError):
                    pass
            time.sleep(0.05)
        assert not pending, f"서버가 시작되지 않았습니다: {pending}"
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        output, _ = process.communicate(timeout=10)
    assert process.returncode == 0, output
    for url in ("http://127.0.0.1:5000/", "http://127.0.0.1:8000/"):
        try:
            urllib.request.urlopen(url, timeout=1).close()
        except urllib.error.URLError:
            continue
        raise AssertionError(f"종료 후에도 서버가 응답합니다: {url}")


def test_demo_runner_stops_other_server_when_port_is_busy():
    with socket.socket() as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied.bind(("127.0.0.1", 8000))
        occupied.listen()
        result = subprocess.run(
            [sys.executable, str(ROOT / "demo" / "run.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.returncode != 0
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", 5000)) != 0
