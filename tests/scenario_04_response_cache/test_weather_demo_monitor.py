"""날씨 데모 실행 모니터: SSE 이벤트와 모니터 화면 계약.

계약: 연결 이후 실제 이벤트만 한 줄씩 전달하고, 끊긴 동안의 내역은 복구하지 않는다.
실제 Open-Meteo 대신 가짜 원본을 쓰고, Mini Redis는 HTTP API로 호출한다.
"""

import json
import re
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from app import create_app as create_storage_app
from demo.weather_app import create_app
from storage import MiniRedis

PARAMS = {"latitude": 37.5664, "longitude": 126.9784}
KEY = "weather:37.566:126.978"
ORIGIN_BODY = {"current": {"temperature_2m": 22.0}}


class StorageProxy:
    """로컬 Mini Redis 요청을 실제 앱으로 보내고 LOCK_BUSY를 관찰한다."""

    def __init__(self, store):
        self.store = store
        self.storage = TestClient(create_storage_app(store))
        self.busy = threading.Event()

    def __call__(self, request):
        response = self.storage.request(
            request.method,
            str(request.url),
            content=request.content,
            headers=dict(request.headers),
        )
        if request.url.path == "/kv/lock/acquire" and response.status_code == 409:
            self.busy.set()
        return response


class BlockingOrigin:
    """첫 원본 호출을 붙잡아 두 요청이 실제로 겹치게 만든다."""

    def __init__(self):
        self.calls = 0
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, request):
        with self._lock:
            self.calls += 1
            call = self.calls
        if call == 1:
            self.first_started.set()
            if not self.release_first.wait(5):
                raise AssertionError("테스트가 첫 원본 호출을 해제하지 않았습니다")
        return httpx.Response(200, json=ORIGIN_BODY)


def make_http(proxy, origin):
    def transport(request):
        if request.url.host == "127.0.0.1":
            return proxy(request)
        return origin(request)

    return httpx.Client(transport=httpx.MockTransport(transport))


def read_sse_block(lines):
    """SSE 블록 하나를 읽어 (event 이름, data)를 반환한다."""
    event = None
    data_lines = []
    for line in lines:
        if line == "":
            if event is None and not data_lines:
                continue
            data = json.loads("\n".join(data_lines)) if data_lines else None
            return event, data
        if line.startswith("event: "):
            event = line[len("event: "):]
        elif line.startswith("data: "):
            data_lines.append(line[len("data: "):])
    raise AssertionError("SSE 스트림이 이벤트를 보내기 전에 끝났습니다")


def start_weather(client, results, errors, label):
    def run():
        try:
            results[label] = client.get("/weather", params=PARAMS)
        except BaseException as exc:  # 스레드 예외를 테스트 스레드로 전달한다.
            errors[label] = exc

    thread = threading.Thread(target=run, name=label)
    thread.start()
    return thread


def join_all(*threads):
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive(), f"{thread.name} 스레드가 끝나지 않았습니다"


def collect_until_end(lines):
    events = []
    while True:
        event, data = read_sse_block(lines)
        assert event is None, f"예상하지 않은 SSE event: {event}"
        events.append(data)
        if data["kind"] == "END":
            return events


@pytest.fixture
def demo():
    store = MiniRedis()
    proxy = StorageProxy(store)
    origin = BlockingOrigin()
    with make_http(proxy, origin) as http:
        yield TestClient(create_app(client=http)), store, proxy, origin


def test_monitor_page_is_a_live_log():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as http:
        client = TestClient(create_app(client=http))
        response = client.get("/monitor")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    for text in ("실행 모니터", "연결됨", "new EventSource", "/monitor/events",
                 "요청 ID", "aria-live", "자동"):
        assert text in html


def test_stream_sends_pipeline_events_after_ready(demo, capsys):
    client, store, _, origin = demo
    origin.release_first.set()
    results, errors = {}, {}

    with client.stream("GET", "/monitor/events") as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        lines = stream.iter_lines()
        event, ready = read_sse_block(lines)
        assert event == "ready"
        assert "cursor" in ready

        thread = start_weather(client, results, errors, "first")
        events = collect_until_end(lines)
        join_all(thread)

    assert errors == {}
    assert results["first"].status_code == 200
    assert store.get(KEY)["found"] is True

    kinds = [item["kind"] for item in events]
    assert kinds[0] == "REQUEST"
    for kind in ("GET", "LOCK ACQUIRE", "OPEN-METEO", "PUT", "LOCK RELEASE", "END"):
        assert kind in kinds
    assert kinds.count("GET") >= 2
    assert len({item["request_id"] for item in events}) == 1
    assert all(item["elapsed_ms"] >= 0 for item in events)
    origin_events = [item for item in events if item["kind"] == "OPEN-METEO"]
    assert origin_events and origin_events[-1]["calls"] == 1
    assert events[-1]["status"] == 200

    output = capsys.readouterr().out
    for text in ("━━━ 날씨 조회", "①", "②", "③", "④", "⑤", "━━━ 조회 종료"):
        assert text in output


def test_stream_does_not_replay_events_from_before_the_connection(demo, capsys):
    client, _, _, origin = demo
    origin.release_first.set()
    first = client.get("/weather", params=PARAMS)
    assert first.status_code == 200
    first_id = re.search(r"요청 ([0-9a-f]{8})", capsys.readouterr().out).group(1)

    results, errors = {}, {}
    with client.stream("GET", "/monitor/events") as stream:
        lines = stream.iter_lines()
        event, _ = read_sse_block(lines)
        assert event == "ready"
        thread = start_weather(client, results, errors, "second")
        events = collect_until_end(lines)
        join_all(thread)

    assert errors == {}
    assert results["second"].status_code == 200
    assert {item["request_id"] for item in events} == {events[0]["request_id"]}
    assert first_id not in {item["request_id"] for item in events}


def test_stream_includes_repeated_get_while_waiting_for_lock(demo):
    client, store, proxy, origin = demo
    results, errors = {}, {}

    with client.stream("GET", "/monitor/events") as stream:
        lines = stream.iter_lines()
        event, _ = read_sse_block(lines)
        assert event == "ready"

        owner = start_weather(client, results, errors, "owner")
        assert origin.first_started.wait(5), "소유 요청이 원본 호출을 시작하지 않았습니다"
        waiter = start_weather(client, results, errors, "waiter")
        assert proxy.busy.wait(5), "대기 요청이 LOCK_BUSY를 관찰하지 않았습니다"
        origin.release_first.set()

        events = []
        while len([item for item in events if item["kind"] == "END"]) < 2:
            event, data = read_sse_block(lines)
            assert event is None
            events.append(data)
        join_all(owner, waiter)

    assert errors == {}
    assert results["owner"].status_code == 200
    assert results["waiter"].status_code == 200
    assert store._locks == {}

    waiter_id = next(
        item["request_id"]
        for item in events
        if item["kind"] == "LOCK ACQUIRE" and "경합" in item["message"]
    )
    waiter_gets = [
        item for item in events
        if item["request_id"] == waiter_id and item["kind"] == "GET"
    ]
    assert len(waiter_gets) >= 2
    assert any("대기 후" in item["message"] for item in waiter_gets)
    assert [item["kind"] for item in events].count("END") == 2


def test_stream_reports_error_and_failed_end():
    def transport(request):
        if request.url.host == "127.0.0.1":
            return httpx.Response(200, json={"found": False})
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        client = TestClient(create_app(client=http))
        results = {}

        with client.stream("GET", "/monitor/events") as stream:
            lines = stream.iter_lines()
            event, _ = read_sse_block(lines)
            assert event == "ready"
            results["response"] = client.get("/weather", params=PARAMS)
            events = collect_until_end(lines)

    assert results["response"].status_code == 502
    errors = [item for item in events if item["kind"] == "ERROR"]
    assert errors and "실패" in errors[0]["message"]
    assert events[-1]["status"] == 502
