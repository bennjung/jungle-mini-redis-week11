"""실제 MiniRedis와 로컬 TCP HTTP를 연결하여 문서 01을 검증한다."""

import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

import pytest
from flask import Flask
from werkzeug.serving import make_server

from app import create_app
from storage import MiniRedis
from tests.scenario_01_storage.storage_helpers import snapshot


@pytest.fixture
def http():
    """각 시나리오마다 빈 실제 저장소와 순차 처리 서버를 새로 만든다."""
    store = MiniRedis()
    server = make_server("127.0.0.1", 0, create_app(store), threaded=False)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    opener = build_opener(ProxyHandler({}))

    def request_and_check(method, key, expected, *, value=None, status=200):
        url = f"http://127.0.0.1:{server.port}/kv"
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
            assert response.status == status
            assert response.headers.get_content_type() == "application/json"
            body = json.loads(response.read())
        assert type(body) is dict
        assert body == expected
        for field, content in expected.items():
            assert type(body[field]) is type(content)

    try:
        yield request_and_check, store
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
        assert not thread.is_alive()


def test_script_entrypoint_connects_real_storage(monkeypatch):
    """python app.py의 실행 분기가 실제 저장소와 순차 서버를 연결한다."""
    started = []

    def record_run(app, **options):
        started.append((app, options))

    monkeypatch.setattr(Flask, "run", record_run)
    runpy.run_path(str(Path(__file__).resolve().parents[2] / "app.py"), run_name="__main__")
    assert len(started) == 1
    app, options = started[0]
    assert options == {
        "host": "127.0.0.1",
        "port": 5000,
        "threaded": False,
        "use_reloader": False,
    }
    client = app.test_client()
    stored = client.put("/kv", json={"key": "sample:one", "value": "hello"})
    assert stored.status_code == 200
    assert stored.get_json() == {"ok": True}
    found = client.get("/kv", query_string={"key": "sample:one"})
    assert found.status_code == 200
    assert found.get_json() == {"found": True, "value": "hello"}


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
                if "Running on http://127.0.0.1:5000" in line:
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
            assert response.status == status
            assert response.headers.get_content_type() == "application/json"
            actual = json.loads(response.read())
        assert type(actual) is dict
        assert actual == expected
        for field, content in expected.items():
            assert type(actual[field]) is type(content)

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
    request("GET", "sample:one", {"found": True, "value": "hello"})


def test_s1_02_delete_and_read_twice(http):
    request, _ = http
    request("PUT", "sample:one", {"ok": True}, value="hello")
    request("DELETE", "sample:one", {"found": True, "value": "hello"})
    request("GET", "sample:one", {"found": False})
    request("GET", "sample:one", {"found": False})


def test_s1_03_other_key_preserved(http):
    request, _ = http
    request("PUT", "sample:one", {"ok": True}, value="hello")
    request("PUT", "sample:two", {"ok": True}, value="world")
    request("DELETE", "sample:one", {"found": True, "value": "hello"})
    request("GET", "sample:two", {"found": True, "value": "world"})


def test_s1_04_overwrite(http):
    request, _ = http
    request("PUT", "sample:one", {"ok": True}, value="hello")
    request("PUT", "sample:two", {"ok": True}, value="world")
    request("PUT", "sample:one", {"ok": True}, value="updated")
    for _ in range(2):
        request("GET", "sample:one", {"found": True, "value": "updated"})
        request("GET", "sample:two", {"found": True, "value": "world"})


def test_s1_05_missing_and_repeated_delete(http):
    request, _ = http
    request("PUT", "sample:one", {"ok": True}, value="hello")
    request("GET", "sample:missing", {"found": False})
    request("DELETE", "sample:missing", {"found": False})
    request("DELETE", "sample:one", {"found": True, "value": "hello"})
    request("DELETE", "sample:one", {"found": False})
    request("GET", "sample:one", {"found": False})
    request("GET", "sample:missing", {"found": False})


def test_s1_06_empty_and_json_text(http):
    request, _ = http
    request("PUT", "sample:empty", {"ok": True}, value="")
    request("PUT", "sample:text", {"ok": True}, value='{"a":1}')
    request("GET", "sample:empty", {"found": True, "value": ""})
    request("GET", "sample:text", {"found": True, "value": '{"a":1}'})
    request("GET", "sample:missing", {"found": False})
    request("DELETE", "sample:empty", {"found": True, "value": ""})
    request("DELETE", "sample:empty", {"found": False})
    request("GET", "sample:text", {"found": True, "value": '{"a":1}'})


def test_s1_06_01_number_and_null_text(http):
    request, _ = http
    request("PUT", "sample:number", {"ok": True}, value="10")
    request("PUT", "sample:word", {"ok": True}, value="null")
    request("GET", "sample:number", {"found": True, "value": "10"})
    request("GET", "sample:word", {"found": True, "value": "null"})


@pytest.mark.parametrize(
    "key,value,accepted",
    [
        pytest.param(None, "new", False, id="S1-07-01"),
        pytest.param("", "new", False, id="S1-07-02"),
        pytest.param("a", "hello", True, id="S1-07-03"),
        pytest.param("a" * 256, "hello", True, id="S1-07-04"),
        pytest.param("a" * 257, "hello", False, id="S1-07-05"),
        pytest.param("한" * 85 + "a", "hello", True, id="S1-07-06"),
        pytest.param("한" * 86, "hello", False, id="S1-07-07"),
        pytest.param("sample:one", "a" * 65_536, True, id="S1-07-08"),
        pytest.param("sample:one", "a" * 65_537, False, id="S1-07-09"),
        pytest.param("sample:one", "한" * 21_845 + "a", True, id="S1-07-10"),
        pytest.param("sample:one", "한" * 21_845 + "aa", False, id="S1-07-11"),
    ],
)
def test_s1_07_input_boundaries_and_state(http, key, value, accepted):
    request, store = http
    request("PUT", "sample:one", {"ok": True}, value="hello")
    request("PUT", "sample:two", {"ok": True}, value="world")
    expected_state = snapshot(store)
    result = {"ok": True} if accepted else {"error": "INVALID_INPUT"}
    request("PUT", key, result, value=value, status=200 if accepted else 400)
    if accepted:
        expected_state[key] = value
    # HTTP 응답 외 관찰은 기존 테스트 전용 snapshot으로 명시적으로 구분한다.
    assert snapshot(store) == expected_state
    request("GET", "sample:two", {"found": True, "value": "world"})
    if accepted and key != "sample:one":
        request("GET", key, {"found": True, "value": "hello"})
    expected_value = value if accepted and key == "sample:one" else "hello"
    request("GET", "sample:one", {"found": True, "value": expected_value})


def test_s1_08_null_value(http):
    request, _ = http
    request("PUT", "sample:null", {"ok": True}, value=None)
    request("GET", "sample:null", {"found": True, "value": None})
    request("GET", "sample:missing", {"found": False})
    request("DELETE", "sample:null", {"found": True, "value": None})
    request("DELETE", "sample:null", {"found": False})
    request("GET", "sample:null", {"found": False})


def test_s1_08_01_overwrite_null_and_text(http):
    request, _ = http
    request("PUT", "sample:one", {"ok": True}, value="hello")
    request("PUT", "sample:two", {"ok": True}, value="world")
    request("PUT", "sample:one", {"ok": True}, value=None)
    request("GET", "sample:one", {"found": True, "value": None})
    request("PUT", "sample:one", {"ok": True}, value="updated")
    request("GET", "sample:one", {"found": True, "value": "updated"})
    request("GET", "sample:two", {"found": True, "value": "world"})


@pytest.mark.parametrize(
    "key",
    [
        pytest.param(None, id="S1-09-01-set"),
        pytest.param("", id="S1-09-02-set"),
        pytest.param(1, id="S1-09-03-set"),
        pytest.param(True, id="S1-09-04-set"),
        pytest.param([], id="S1-09-05-set"),
        pytest.param({}, id="S1-09-06-set"),
        pytest.param("a" * 257, id="S1-09-08-set"),
        pytest.param("\ud800", id="S1-09-09-set"),
    ],
)
def test_s1_09_invalid_json_key_preserves_state(http, key):
    """원문의 키를 JSON으로 표현할 수 있는 PUT 조합을 실제 HTTP로 검증한다."""
    request, store = http
    request("PUT", "sample:one", {"ok": True}, value="hello")
    request("PUT", "sample:two", {"ok": True}, value="world")
    before = snapshot(store)
    assert before == {"sample:one": "hello", "sample:two": "world"}

    request("PUT", key, {"error": "INVALID_INPUT"}, value="new", status=400)

    assert snapshot(store) == before
    request("GET", "sample:one", {"found": True, "value": "hello"})
    request("GET", "sample:two", {"found": True, "value": "world"})
    assert snapshot(store) == before


@pytest.mark.parametrize("method", ["GET", "DELETE"])
@pytest.mark.parametrize(
    "key",
    [
        pytest.param("", id="S1-09-02"),
        pytest.param("a" * 257, id="S1-09-08"),
    ],
)
def test_s1_09_invalid_query_key_preserves_state(http, method, key):
    """원문과 같은 문자열로 URL 쿼리에 전달 가능한 키를 검사한다."""
    request, store = http
    request("PUT", "sample:one", {"ok": True}, value="hello")
    request("PUT", "sample:two", {"ok": True}, value="world")
    before = snapshot(store)
    assert before == {"sample:one": "hello", "sample:two": "world"}

    request(method, key, {"error": "INVALID_INPUT"}, status=400)

    assert snapshot(store) == before
    request("GET", "sample:one", {"found": True, "value": "hello"})
    request("GET", "sample:two", {"found": True, "value": "world"})
    assert snapshot(store) == before


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("\ud800", id="S1-10-06"),
    ],
)
def test_s1_10_invalid_json_value_preserves_state(http, value):
    """bytes를 제외한 원문의 value를 실제 JSON 요청으로 전달한다."""
    request, store = http
    request("PUT", "sample:one", {"ok": True}, value="hello")
    request("PUT", "sample:two", {"ok": True}, value="world")
    before = snapshot(store)
    assert before == {"sample:one": "hello", "sample:two": "world"}

    request("PUT", "sample:one", {"error": "INVALID_INPUT"}, value=value, status=400)

    assert snapshot(store) == before
    request("GET", "sample:one", {"found": True, "value": "hello"})
    request("GET", "sample:two", {"found": True, "value": "world"})
    assert snapshot(store) == before


@pytest.mark.parametrize("value", [
    pytest.param(1, id="S1-10-01"),
    pytest.param(True, id="S1-10-02"),
    pytest.param([], id="S1-10-03"),
    pytest.param({}, id="S1-10-04"),
])
def test_s1_10_json_value_replaces_target_only(http, value):
    request, store = http
    request("PUT", "sample:one", {"ok": True}, value="hello")
    request("PUT", "sample:two", {"ok": True}, value="world")
    request("PUT", "sample:one", {"ok": True}, value=value)
    assert snapshot(store) == {"sample:one": value, "sample:two": "world"}
    for _ in range(2):
        request("GET", "sample:one", {"found": True, "value": value})
        request("GET", "sample:two", {"found": True, "value": "world"})
