"""S2-02·03·09~14: 단일 이벤트 루프 서버에 실제 TCP HTTP 요청을 동시에 진행한다.

반복·완료 한도는 실행자가 COUNTER_TEST_REPEAT·COUNTER_TEST_TIMEOUT으로 지정한다.
미지정 시 보류된 실행 조건을 임의로 확정하지 않고 미검증으로 남긴다.
"""

from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
import json
import math
import os
import time
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

import pytest
import uvicorn

from app import create_app
from storage import MiniRedis
from tests.scenario_02_concurrent_counter.contract_helpers import assert_json


@pytest.fixture
def execution_limits():
    repeat = os.environ.get("COUNTER_TEST_REPEAT")
    timeout = os.environ.get("COUNTER_TEST_TIMEOUT")
    if repeat is None or timeout is None:
        pytest.skip("반복 횟수·완료 한도 미지정: COUNTER_TEST_REPEAT·COUNTER_TEST_TIMEOUT 필요")
    repeat, timeout = int(repeat), float(timeout)
    assert repeat > 0 and math.isfinite(timeout) and timeout > 0
    return repeat, timeout


@contextmanager
def running_server(timeout):
    """단일 이벤트 루프 서버를 스레드에서 띄우고 실제 바인딩된 포트를 읽는다."""
    config = uvicorn.Config(
        create_app(MiniRedis()), host="127.0.0.1", port=0, log_level="error"
    )
    server = uvicorn.Server(config)
    thread = Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + timeout
    while not server.started:
        assert thread.is_alive(), "서버 시작 전 종료"
        assert time.monotonic() < deadline, "서버 시작 시간 초과"
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]

    def request(method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {} if data is None else {"Content-Type": "application/json"}
        outgoing = Request(f"http://127.0.0.1:{port}{path}",
                           data=data, headers=headers, method=method)
        # 연결·쿠키·opener를 작업 스레드끼리 공유하지 않는다. 자동 재시도하지 않는다.
        opener = build_opener(ProxyHandler({}))
        try:
            response = opener.open(outgoing, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            assert response.headers.get_content_type() == "application/json"
            return response.status, json.loads(response.read())

    try:
        yield request
    finally:
        # 앱의 비정상 정지 때문에 테스트 정리까지 무한히 기다리지 않는다.
        server.should_exit = True
        thread.join(timeout=timeout)
        assert not thread.is_alive(), "서버 종료 미완료"


def batch(request, commands, timeout):
    """모든 작업을 준비한 뒤 함께 해제하고 각 입력과 응답의 연결을 유지한다."""
    start = Event()

    def send(command):
        assert start.wait(timeout), "요청 시작 조율 시간 초과"
        return request(*command)

    with ThreadPoolExecutor(max_workers=len(commands)) as pool:
        futures = [pool.submit(send, command) for command in commands]
        start.set()
        done, pending = wait(futures, timeout=timeout)
        errors = [repr(future.exception()) for future in done if future.exception() is not None]
        assert not pending and not errors, {
            "미완료": len(pending), "요청실패": errors, "완료": len(done),
        }
        return [future.result() for future in futures]


def expect(result, status, body):
    assert result[0] == status
    assert_json(result[1], body)


def read(request, key, body):
    for _ in range(2):
        expect(request("GET", "/kv?" + urlencode({"key": key})), 200, body)


def seed(request, key, value):
    result = request("PUT", "/kv", {"key": key, "value": value})
    assert result == (200, {"ok": True}), ("사전 상태 준비 PUT 실패", key, result)


def incr(key="counter:one", amount=1):
    return "POST", "/kv/incr", {"key": key, "amount": amount}


def success_values(results, expected):
    values = []
    for result in results:
        assert result[0] == 200
        assert set(result[1]) == {"ok", "value"}
        assert result[1]["ok"] is True
        assert type(result[1]["value"]) in (int, float)
        values.append(result[1]["value"])
    assert sorted(values) == list(expected)


@pytest.mark.parametrize("case_id", ["S2-02", "S2-03", "S2-09", "S2-10",
                                    "S2-11", "S2-12", "S2-13", "S2-14"])
def test_concurrent_contract(execution_limits, case_id):
    repeat, timeout = execution_limits
    for iteration in range(repeat):
        with running_server(timeout) as request:
            seed(request, "counter:other", "keep")
            initial = 10 if case_id in ("S2-03", "S2-12", "S2-13", "S2-14") else 0
            if case_id == "S2-10":
                seed(request, "stock:one", 1)
                commands = [("POST", "/kv/decr", {
                    "key": "stock:one", "amount": 1, "minimum": 0,
                })] * 2
                results = batch(request, commands, timeout)
                assert sorted(result[0] for result in results) == [200, 409]
                for result in results:
                    expect(result, result[0], {"ok": True, "value": 0} if result[0] == 200
                           else {"error": "CONDITION_NOT_MET", "value": 0})
                read(request, "stock:one", {"found": True, "value": 0})
            else:
                seed(request, "counter:one", initial)
                if case_id in ("S2-02", "S2-03"):
                    results = batch(request, [incr()] * 100, timeout)
                    success_values(results, range(initial + 1, initial + 101))
                    read(request, "counter:one", {"found": True, "value": initial + 100})
                elif case_id == "S2-09":
                    commands = [command for _ in range(50) for command in (incr(), incr(amount=-1))]
                    results = batch(request, commands, timeout)
                    success_values(results[::2], range(1, 51))
                    for result in results[1::2]:
                        expect(result, 400, {"error": "NEGATIVE_AMOUNT"})
                    read(request, "counter:one", {"found": True, "value": 50})
                elif case_id == "S2-11":
                    seed(request, "counter:two", 10)
                    commands = [command for _ in range(50)
                                for command in (incr(), incr("counter:two"))]
                    results = batch(request, commands, timeout)
                    success_values(results[::2], range(1, 51))
                    success_values(results[1::2], range(11, 61))
                    read(request, "counter:one", {"found": True, "value": 50})
                    read(request, "counter:two", {"found": True, "value": 60})
                elif case_id == "S2-12":
                    incremented, stored = batch(request, [incr(), ("PUT", "/kv", {
                        "key": "counter:one", "value": 20,
                    })], timeout)
                    expect(stored, 200, {"ok": True})
                    assert incremented[1].get("value") in (11, 21), iteration
                    value = incremented[1]["value"]
                    expect(incremented, 200, {"ok": True, "value": value})
                    read(request, "counter:one", {"found": True, "value": 20 if value == 11 else 21})
                elif case_id == "S2-13":
                    incremented, deleted = batch(request, [incr(),
                        ("DELETE", "/kv?key=counter%3Aone")], timeout)
                    if incremented[0] == 200:
                        expect(incremented, 200, {"ok": True, "value": 11})
                        expect(deleted, 200, {"found": True, "value": 11})
                    else:
                        expect(incremented, 404, {"error": "NOT_FOUND"})
                        expect(deleted, 200, {"found": True, "value": 10})
                    read(request, "counter:one", {"found": False})
                elif case_id == "S2-14":
                    incremented, found = batch(request, [incr(),
                        ("GET", "/kv?key=counter%3Aone")], timeout)
                    expect(incremented, 200, {"ok": True, "value": 11})
                    assert found[1].get("value") in (10, 11), iteration
                    expect(found, 200, {"found": True, "value": found[1]["value"]})
                    read(request, "counter:one", {"found": True, "value": 11})
            read(request, "counter:other", {"found": True, "value": "keep"})
