"""실제 MiniRedis와 로컬 TCP HTTP를 연결하여 문서 01을 검증한다."""

import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

import pytest
import uvicorn
from fastapi.testclient import TestClient

from app import create_app
from storage import MiniRedis
from tests.response_helpers import check_raw_response
from tests.scenario_01_storage.storage_helpers import snapshot


@pytest.fixture
def http():
    """각 시나리오마다 빈 실제 저장소와 단일 이벤트 루프 서버를 새로 만든다."""
    store = MiniRedis()
    config = uvicorn.Config(
        create_app(store), host="127.0.0.1", port=0, log_level="error"
    )
    server = uvicorn.Server(config)
    thread = Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "uvicorn 시작 시간 초과"
    port = server.servers[0].sockets[0].getsockname()[1]
    opener = build_opener(ProxyHandler({}))

    def request_and_check(method, key, expected, *, value=None, status=200):
        url = f"http://127.0.0.1:{port}/kv"
        data = None
        headers = {}
        if method == "PUT":
            data = json.dumps({"key": key, "value": value}).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            url += "?" + urlencode({"key": key})

        request = Request(url, data=data, headers=headers, method=method)
        try:
            response = opener.open(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            check_raw_response(
                response.status,
                response.headers.get_content_type(),
                json.loads(response.read()),
                status,
                expected,
            )

    try:
        yield request_and_check, store
    finally:
        server.should_exit = True
        thread.join(timeout=3)
        assert not thread.is_alive()


def test_script_entrypoint_connects_real_storage(monkeypatch):
    """python app.py의 실행 분기가 실제 저장소와 단일 이벤트 루프 서버를 연결한다."""
    started = []

    def record_run(app, **options):
        started.append((app, options))

    monkeypatch.setattr(uvicorn, "run", record_run)
    runpy.run_path(str(Path(__file__).resolve().parents[2] / "app.py"), run_name="__main__")
    assert len(started) == 1
    app, options = started[0]
    assert options == {
        "host": "127.0.0.1",
        "port": 5000,
        "workers": 1,
    }
    client = TestClient(app)
    stored = client.put("/kv", json={"key": "sample:one", "value": "hello"})
    assert stored.status_code == 200
    assert stored.json() == {"ok": True}
    found = client.get("/kv", params={"key": "sample:one"})
    assert found.status_code == 200
    assert found.json() == {"found": True, "value": "hello"}


def test_script_server_responds_over_http():
    """실제 python app.py 프로세스에 요청하고 성공·실패 모두 서버를 정리한다."""
    root = Path(__file__).resolve().parents[2]
    ready = Event()
    startup_finished = Event()
    logs = []
    process = subprocess.Popen(
        [sys.executable, "app.py"],
        cwd=root,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    def read_logs():
        try:
            for line in process.stdout:
                logs.append(line)
                if "Uvicorn running on http://127.0.0.1:5000" in line:
                    ready.set()
                    startup_finished.set()
        finally:
            startup_finished.set()

    reader = Thread(target=read_logs, daemon=True)
    reader.start()
    opener = build_opener(ProxyHandler({}))

    def check(method, path, expected, *, body=None, raw=None, status=200):
        data = raw
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if data is not None else {}
        request = Request(
            "http://127.0.0.1:5000" + path, data=data, headers=headers, method=method
        )
        try:
            response = opener.open(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            check_raw_response(
                response.status,
                response.headers.get_content_type(),
                json.loads(response.read()),
                status,
                expected,
            )

    try:
        assert startup_finished.wait(5), "서버 시작 시간 초과:\n" + "".join(logs)
        # 다른 프로세스가 5000 포트를 쓰면 이 서버의 준비를 확인하지 못해 실패한다.
        assert ready.is_set(), "실행한 서버가 시작되지 않음:\n" + "".join(logs)
        key_path = "/kv?key=sample%3Aone"
        found = {"found": True, "value": "hello"}
        invalid = {"error": "INVALID_INPUT"}

        check("PUT", "/kv", {"ok": True}, body={"key": "sample:one", "value": "hello"})
        check("GET", key_path, found)
        check("GET", key_path, found)
        check(
            "PUT", "/kv", invalid,
            body={"key": "sample:one", "value": "a" * 65_537}, status=400,
        )
        check("GET", key_path, found)
        check("PUT", "/kv", invalid, body={"key": "sample:one"}, status=400)
        check("PUT", "/kv", invalid, raw=b'{"key":', status=400)
        check("GET", "/kv", invalid, status=400)
        check("GET", key_path, found)
        check("DELETE", key_path, found)
        check("GET", key_path, {"found": False})
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        reader.join(timeout=3)
        process.stdout.close()
        assert not reader.is_alive()


def test_s1_01_store_and_read_twice(http):
    request, _ = http
    request("PUT", "sample:one", {"ok": True}, value="hello")
    request("GET", "sample:one", {"found": True, "value": "hello"})
    request("DELETE", "sample:one", {"found": True, "value": "hello"})
    request("GET", "sample:one", {"found": False})


@pytest.mark.parametrize(
    "key,value,accepted",
    [
        pytest.param("a" * 256, "hello", True, id="S1-07-04"),
        pytest.param("a" * 257, "hello", False, id="S1-07-05"),
    ],
)
def test_s1_07_input_boundaries_and_state(http, key, value, accepted):
    request, store = http
    request("PUT", "sample:one", {"ok": True}, value="hello")
    expected_state = snapshot(store)
    result = {"ok": True} if accepted else {"error": "INVALID_INPUT"}
    request("PUT", key, result, value=value, status=200 if accepted else 400)
    if accepted:
        expected_state[key] = value
    assert snapshot(store) == expected_state
    request("GET", "sample:one", {"found": True, "value": "hello"})


def test_s1_08_null_value(http):
    request, _ = http
    request("PUT", "sample:null", {"ok": True}, value=None)
    request("GET", "sample:null", {"found": True, "value": None})
    request("DELETE", "sample:null", {"found": True, "value": None})
    request("GET", "sample:null", {"found": False})
