"""WDL-01~11: 날씨 데모의 키별 갱신 락 사용 계약.

WDL-05·06은 test_weather_demo.py의 순차 재사용·실패 해제 회귀 테스트가 담당한다.
계약: resc/scenario_05_cache_lock/05-weather-demo-lock-usage.md
실제 Open-Meteo 대신 가짜 원본을 쓰고, Mini Redis는 HTTP API로 호출한다.
"""

import json
import re
import threading

import httpx
from fastapi.testclient import TestClient

from app import create_app as create_storage_app
from demo.weather_app import create_app
from storage import MiniRedis
from tests.scenario_01_storage.ttl_clock_helpers import BASE_MS, FakeClock

KEY = "weather:37.566:126.978"
PARAMS = {"latitude": 37.5664, "longitude": 126.9784}
ORIGIN_BODY = {"current": {"temperature_2m": 22.0}}

# 승인된 계약 상수. 구현 전에는 이 값으로 기대 결과를 고정한다.
EXPECTED_LOCK_TTL_MS = 10_000
EXPECTED_LOCK_POLL_INTERVAL_S = 0.1
EXPECTED_LOCK_WAIT_LIMIT_S = 10.0


class StorageProxy:
    """로컬 요청을 실제 Mini Redis HTTP 앱으로 보내고 락 요청을 기록한다."""

    def __init__(self, store, before=None, after=None):
        self.store = store
        self.client = TestClient(create_storage_app(store))
        self.records = []
        self.busy = threading.Event()
        self._records_lock = threading.Lock()
        self._before = before
        self._after = after

    def __call__(self, request):
        if self._before is not None:
            self._before(request)
        response = self.client.request(
            request.method,
            str(request.url),
            content=request.content,
            headers=dict(request.headers),
        )
        record = {
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
        }
        if request.url.path in ("/kv/lock/acquire", "/kv/lock/release"):
            record["body"] = json.loads(request.content)
        with self._records_lock:
            self.records.append(record)
        if request.url.path == "/kv/lock/acquire" and response.status_code == 409:
            self.busy.set()
        if self._after is not None:
            self._after(request, response)
        return response

    def lock_acquires(self):
        with self._records_lock:
            return [record for record in self.records if record["path"] == "/kv/lock/acquire"]

    def lock_releases(self):
        with self._records_lock:
            return [record for record in self.records if record["path"] == "/kv/lock/release"]


class FakeMonotonic:
    """데모의 경합 대기 시간을 테스트가 직접 진행시키는 가짜 초 단위 시계."""

    def __init__(self, now=0.0):
        self.now = now
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


class FakeOrigin:
    """호출 횟수를 세고, 필요하면 첫 호출을 붙잡거나 실패시키는 가짜 원본."""

    def __init__(self, fail_first=False, block_first=True):
        self.fail_first = fail_first
        self.block_first = block_first
        self.calls = 0
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self._calls_lock = threading.Lock()

    def __call__(self, request):
        with self._calls_lock:
            index = self.calls
            self.calls += 1
        if index == 0:
            if self.block_first:
                self.first_started.set()
                if not self.release_first.wait(5):
                    raise AssertionError("테스트가 첫 원본 호출을 해제하지 않았습니다")
            if self.fail_first:
                return httpx.Response(503)
        return httpx.Response(200, json=ORIGIN_BODY)


def make_http(proxy, origin):
    def transport(request):
        if request.url.host == "127.0.0.1":
            return proxy(request)
        return origin(request)

    return httpx.Client(transport=httpx.MockTransport(transport))


def start_weather(client, results, errors, label):
    def run():
        try:
            results[label] = client.get("/weather", params=PARAMS)
        except BaseException as exc:  # 스레드에서 난 예외를 테스트 스레드로 전달한다.
            errors[label] = exc

    thread = threading.Thread(target=run, name=label)
    thread.start()
    return thread


def join_all(*threads):
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive(), f"{thread.name} 스레드가 끝나지 않았습니다"


def test_wdl_01_overlapping_misses_use_one_origin_call(capsys):
    store = MiniRedis()
    proxy = StorageProxy(store)
    origin = FakeOrigin()
    results, errors = {}, {}

    with make_http(proxy, origin) as http:
        client = TestClient(create_app(client=http))
        owner = start_weather(client, results, errors, "owner")
        assert origin.first_started.wait(5), "소유 요청이 원본 호출을 시작하지 않았습니다"
        waiter = start_weather(client, results, errors, "waiter")
        assert proxy.busy.wait(5), "대기 요청이 LOCK_BUSY를 관찰하지 않았습니다"
        origin.release_first.set()
        join_all(owner, waiter)

    assert errors == {}
    assert results["owner"].status_code == 200
    assert results["waiter"].status_code == 200
    assert results["owner"].json() == results["waiter"].json() == ORIGIN_BODY
    assert origin.calls == 1
    assert store._locks == {}

    stored = store.get(KEY)
    assert stored["found"] is True
    assert json.loads(stored["value"]) == ORIGIN_BODY
    ttl = proxy.client.get("/kv/ttl", params={"key": KEY}).json()
    assert ttl["found"] is True and 0 < ttl["ttl"] <= 60_000

    acquires = proxy.lock_acquires()
    assert acquires
    assert {record["body"]["ttl"] for record in acquires} == {EXPECTED_LOCK_TTL_MS}
    assert len({record["body"]["token"] for record in acquires}) >= 2
    owner_token = next(record["body"]["token"] for record in acquires if record["status"] == 200)
    assert sum(1 for record in acquires if record["body"]["token"] == owner_token) == 1

    output = capsys.readouterr().out
    for text in ("락 획득", "락 경합 대기", "조회 성공(대기 후)", "재사용", "락 해제", "원본 호출: 1회"):
        assert text in output
    request_ids = set(re.findall(r"\(요청 ([0-9a-f]{8})\)", output))
    assert len(request_ids) >= 2
    assert {token[:8] for token in {record["body"]["token"] for record in acquires}} <= request_ids


def test_wdl_02_waiter_takes_over_after_owner_failure(capsys):
    store = MiniRedis()
    proxy = StorageProxy(store)
    origin = FakeOrigin(fail_first=True)
    results, errors = {}, {}

    with make_http(proxy, origin) as http:
        client = TestClient(create_app(client=http))
        owner = start_weather(client, results, errors, "owner")
        assert origin.first_started.wait(5), "소유 요청이 원본 호출을 시작하지 않았습니다"
        waiter = start_weather(client, results, errors, "waiter")
        assert proxy.busy.wait(5), "대기 요청이 LOCK_BUSY를 관찰하지 않았습니다"
        origin.release_first.set()
        join_all(owner, waiter)

    assert errors == {}
    assert results["owner"].status_code == 502
    assert results["waiter"].status_code == 200
    assert results["waiter"].json() == ORIGIN_BODY
    assert origin.calls == 2
    assert store._locks == {}
    assert store.get(KEY)["found"] is True

    output = capsys.readouterr().out
    for text in ("락 획득", "락 경합 대기", "실패", "락 해제", "락 획득(대기 후)", "저장 성공"):
        assert text in output


def test_wdl_03_wait_limit_returns_502_without_origin_call(capsys):
    store = MiniRedis()
    assert store.acquire(KEY, "holder", 60_000) == {"acquired": True}
    proxy = StorageProxy(store)
    origin = FakeOrigin(block_first=False)

    class FakeWait:
        """가짜 시간: sleep이 호출될 때마다 monotonic이 함께 진행된다."""

        def __init__(self):
            self.now = 0.0
            self.sleeps = []

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds

    fake = FakeWait()
    with make_http(proxy, origin) as http:
        client = TestClient(create_app(client=http, sleep=fake.sleep, monotonic=fake.monotonic))
        response = client.get("/weather", params=PARAMS)

    assert response.status_code == 502
    assert origin.calls == 0
    assert store._locks[KEY][0] == "holder"
    assert fake.now >= EXPECTED_LOCK_WAIT_LIMIT_S
    assert fake.now < EXPECTED_LOCK_WAIT_LIMIT_S + EXPECTED_LOCK_POLL_INTERVAL_S
    assert fake.sleeps and all(0 < seconds <= EXPECTED_LOCK_POLL_INTERVAL_S for seconds in fake.sleeps)
    assert fake.sleeps[0] == EXPECTED_LOCK_POLL_INTERVAL_S

    output = capsys.readouterr().out
    assert "락 경합 대기" in output
    assert "대기 한도 초과" in output
    assert "원본 호출" not in output
    assert store.release(KEY, "holder") == {"released": True}


def test_wdl_04_waiter_acquires_after_lock_expiry(capsys):
    clock = FakeClock(BASE_MS)
    store = MiniRedis(clock=clock)
    assert store.acquire(KEY, "holder", 1_000) == {"acquired": True}
    proxy = StorageProxy(store)
    origin = FakeOrigin(block_first=False)
    results, errors = {}, {}

    with make_http(proxy, origin) as http:
        client = TestClient(create_app(client=http))
        waiter = start_weather(client, results, errors, "waiter")
        assert proxy.busy.wait(5), "대기 요청이 LOCK_BUSY를 관찰하지 않았습니다"
        clock.advance(1_000)
        join_all(waiter)

    assert errors == {}
    assert results["waiter"].status_code == 200
    assert results["waiter"].json() == ORIGIN_BODY
    assert origin.calls == 1
    assert store._locks == {}
    assert store.get(KEY)["found"] is True

    output = capsys.readouterr().out
    assert "락 경합 대기" in output
    assert "락 획득(대기 후)" in output


def test_wdl_07_cache_hit_does_not_use_lock(capsys):
    store = MiniRedis()
    assert store.set(KEY, json.dumps(ORIGIN_BODY), ttl=600_000) == {"ok": True}
    proxy = StorageProxy(store)
    origin = FakeOrigin(block_first=False)

    with make_http(proxy, origin) as http:
        client = TestClient(create_app(client=http))
        response = client.get("/weather", params=PARAMS)

    assert response.status_code == 200
    assert response.json() == ORIGIN_BODY
    assert origin.calls == 0
    assert proxy.lock_acquires() == []
    assert store._locks == {}

    output = capsys.readouterr().out
    assert "재사용" in output
    assert "락 획득" not in output


def test_wdl_08_slow_cache_then_late_acquire_returns_502_and_releases(capsys):
    store = MiniRedis()
    assert store.acquire(KEY, "holder", 60_000) == {"acquired": True}
    fake = FakeMonotonic()
    state = {"cache_delayed": False, "acquire_delayed": False}
    holder_releases = []

    def before(request):
        if (
            request.method == "GET"
            and request.url.path == "/kv"
            and proxy.busy.is_set()
            and not state["cache_delayed"]
        ):
            state["cache_delayed"] = True
            fake.advance(9.5)
        elif (
            request.method == "POST"
            and request.url.path == "/kv/lock/acquire"
            and proxy.busy.is_set()
            and not state["acquire_delayed"]
        ):
            state["acquire_delayed"] = True
            holder_releases.append(store.release(KEY, "holder"))
            fake.advance(1.0)

    proxy = StorageProxy(store, before=before)
    origin = FakeOrigin(block_first=False)

    with make_http(proxy, origin) as http:
        client = TestClient(create_app(client=http, sleep=fake.sleep, monotonic=fake.monotonic))
        response = client.get("/weather", params=PARAMS)

    assert response.status_code == 502
    assert origin.calls == 0
    assert holder_releases == [{"released": True}]
    assert fake.now >= EXPECTED_LOCK_WAIT_LIMIT_S
    assert store._locks == {}

    late_acquires = [record for record in proxy.lock_acquires() if record["status"] == 200]
    assert late_acquires
    late_token = late_acquires[-1]["body"]["token"]
    assert any(
        record["status"] == 200 and record["body"]["token"] == late_token
        for record in proxy.lock_releases()
    )

    output = capsys.readouterr().out
    assert "락 경합 대기" in output
    assert "락 획득(대기 후)" in output
    assert "락 해제" in output
    assert "대기 한도 초과" in output
    assert "원본 호출" not in output


def test_wdl_09_slow_acquire_after_deadline_returns_502_and_releases(capsys):
    store = MiniRedis()
    assert store.acquire(KEY, "holder", 60_000) == {"acquired": True}
    fake = FakeMonotonic()
    state = {"acquire_delayed": False}
    holder_releases = []

    def before(request):
        if (
            request.method == "POST"
            and request.url.path == "/kv/lock/acquire"
            and proxy.busy.is_set()
            and not state["acquire_delayed"]
        ):
            state["acquire_delayed"] = True
            holder_releases.append(store.release(KEY, "holder"))
            fake.advance(10.5)

    proxy = StorageProxy(store, before=before)
    origin = FakeOrigin(block_first=False)

    with make_http(proxy, origin) as http:
        client = TestClient(create_app(client=http, sleep=fake.sleep, monotonic=fake.monotonic))
        response = client.get("/weather", params=PARAMS)

    assert response.status_code == 502
    assert origin.calls == 0
    assert holder_releases == [{"released": True}]
    assert fake.now >= EXPECTED_LOCK_WAIT_LIMIT_S
    assert store._locks == {}

    late_acquires = [record for record in proxy.lock_acquires() if record["status"] == 200]
    assert late_acquires
    late_token = late_acquires[-1]["body"]["token"]
    assert any(
        record["status"] == 200 and record["body"]["token"] == late_token
        for record in proxy.lock_releases()
    )

    output = capsys.readouterr().out
    assert "락 경합 대기" in output
    assert "락 획득(대기 후)" in output
    assert "락 해제" in output
    assert "대기 한도 초과" in output
    assert "원본 호출" not in output


def test_wdl_10_late_cache_hit_after_deadline_returns_502(capsys):
    store = MiniRedis()
    assert store.acquire(KEY, "holder", 60_000) == {"acquired": True}
    fake = FakeMonotonic()
    state = {"cache_delayed": False}

    def before(request):
        if (
            request.method == "GET"
            and request.url.path == "/kv"
            and proxy.busy.is_set()
            and not state["cache_delayed"]
        ):
            state["cache_delayed"] = True
            store.set(KEY, json.dumps(ORIGIN_BODY), ttl=600_000)
            fake.advance(10.5)

    proxy = StorageProxy(store, before=before)
    origin = FakeOrigin(block_first=False)

    with make_http(proxy, origin) as http:
        client = TestClient(create_app(client=http, sleep=fake.sleep, monotonic=fake.monotonic))
        response = client.get("/weather", params=PARAMS)

    assert response.status_code == 502
    assert origin.calls == 0
    assert store._locks[KEY][0] == "holder"
    assert proxy.lock_releases() == []

    output = capsys.readouterr().out
    assert "락 경합 대기" in output
    assert "대기 한도 초과" in output
    assert store.release(KEY, "holder") == {"released": True}


def test_wdl_11_double_check_hit_skips_origin_and_releases_lock(capsys):
    store = MiniRedis()
    state = {"acquired": False, "filled": False}

    def before(request):
        if (
            request.method == "GET"
            and request.url.path == "/kv"
            and state["acquired"]
            and not state["filled"]
        ):
            # 락 획득과 double-check 사이에 다른 요청이 캐시를 채운 상황을 재현한다.
            state["filled"] = True
            store.set(KEY, json.dumps(ORIGIN_BODY), ttl=600_000)

    def after(request, response):
        if (
            request.method == "POST"
            and request.url.path == "/kv/lock/acquire"
            and response.status_code == 200
        ):
            state["acquired"] = True

    proxy = StorageProxy(store, before=before, after=after)
    origin = FakeOrigin(block_first=False)

    with make_http(proxy, origin) as http:
        client = TestClient(create_app(client=http))
        response = client.get("/weather", params=PARAMS)

    assert response.status_code == 200
    assert response.json() == ORIGIN_BODY
    assert origin.calls == 0
    assert state["filled"] is True
    assert store._locks == {}

    acquires = [record for record in proxy.lock_acquires() if record["status"] == 200]
    assert len(acquires) == 1
    token = acquires[0]["body"]["token"]
    assert any(
        record["status"] == 200 and record["body"]["token"] == token
        for record in proxy.lock_releases()
    )

    output = capsys.readouterr().out
    assert "락 획득" in output
    assert "재사용" in output
    assert "락 해제" in output
    assert "원본 호출: 0회" in output

