"""문서 01의 HTTP 경계 검증. 실제 저장·삭제·상태 보존은 검증하지 않는다.

대상: app.create_app(storage)가 반환하는 실제 Flask 앱.
저장소는 합의된 set/get/delete 호출을 기록하고 요청별 지정 결과만 반환한다.
실행: python3 -m pytest tests/scenario_01_storage/test_http.py -v
"""

from copy import deepcopy
from unittest.mock import Mock, call

import pytest


@pytest.fixture
def exchange():
    # 구현 전에도 테스트 목록은 수집할 수 있도록 앱은 준비 단계에서 가져온다.
    from app import create_app

    storage = Mock(spec_set=["set", "get", "delete"])
    app = create_app(storage)
    app.config["TESTING"] = True
    client = app.test_client()

    def request_and_check(method, key, result, *, value=None, status=200):
        """각 요청 완료 직후 호출 및 응답을 확인하고 다음 요청으로 넘어간다."""
        storage.reset_mock(return_value=True, side_effect=True)
        operation = {"PUT": "set", "GET": "get", "DELETE": "delete"}[method]
        getattr(storage, operation).return_value = deepcopy(result)

        if method == "PUT":
            response = client.put("/kv", json={"key": key, "value": value})
            expected_call = call.set(key, value)
        else:
            # 쿼리 인코딩은 클라이언트에 맡기고 해석된 키의 전달을 검증한다.
            response = client.open("/kv", method=method, query_string={"key": key})
            expected_call = getattr(call, operation)(key)

        # 삭제 전 사전 조회 등 추가 호출도 거절한다.
        assert storage.mock_calls == [expected_call]
        assert response.status_code == status
        assert response.is_json
        body = response.get_json()
        assert type(body) is dict
        assert body == result  # found=False일 때 value 필드 생략도 확인한다.
        for field, expected in result.items():
            # Python에서는 True == 1이므로 내용 비교 외에 JSON 타입도 확인한다.
            assert type(body[field]) is type(expected)

    return request_and_check


def test_s1_01_store_and_read_twice(exchange):
    """S1-01: sample:one 없음에 해당하는 호출·반환을 준비한다."""
    exchange("PUT", "sample:one", {"ok": True}, value="hello")
    exchange("GET", "sample:one", {"found": True, "value": "hello"})
    exchange("GET", "sample:one", {"found": True, "value": "hello"})


def test_s1_02_delete_and_read_twice(exchange):
    """S1-02: sample:one에 hello가 있는 상황의 저장소 반환을 준비한다."""
    exchange("DELETE", "sample:one", {"found": True, "value": "hello"})
    exchange("GET", "sample:one", {"found": False})
    exchange("GET", "sample:one", {"found": False})


def test_s1_03_delete_one_and_read_other_key(exchange):
    """S1-03: one=hello, two=world 상황. 실제 다른 키 보존은 별도 검증."""
    exchange("DELETE", "sample:one", {"found": True, "value": "hello"})
    exchange("GET", "sample:two", {"found": True, "value": "world"})


def test_s1_04_overwrite_and_read_both_keys_twice(exchange):
    """S1-04: one=hello, two=world 상황에서 updated 전달·응답을 확인한다."""
    exchange("PUT", "sample:one", {"ok": True}, value="updated")
    exchange("GET", "sample:one", {"found": True, "value": "updated"})
    exchange("GET", "sample:two", {"found": True, "value": "world"})
    exchange("GET", "sample:one", {"found": True, "value": "updated"})
    exchange("GET", "sample:two", {"found": True, "value": "world"})


def test_s1_05_missing_key_and_repeated_delete(exchange):
    """S1-05: missing은 저장 이력 없음, one=hello에 해당하는 반환."""
    exchange("GET", "sample:missing", {"found": False})
    exchange("DELETE", "sample:missing", {"found": False})
    exchange("DELETE", "sample:one", {"found": True, "value": "hello"})
    exchange("DELETE", "sample:one", {"found": False})
    exchange("GET", "sample:one", {"found": False})
    exchange("GET", "sample:missing", {"found": False})


def test_s1_06_empty_and_json_text(exchange):
    """S1-06: empty·text·missing 없음. JSON 모양 문자열을 재해석하지 않는다."""
    exchange("PUT", "sample:empty", {"ok": True}, value="")
    exchange("PUT", "sample:text", {"ok": True}, value='{"a":1}')
    exchange("GET", "sample:empty", {"found": True, "value": ""})
    exchange("GET", "sample:text", {"found": True, "value": '{"a":1}'})
    exchange("GET", "sample:missing", {"found": False})
    exchange("DELETE", "sample:empty", {"found": True, "value": ""})
    exchange("DELETE", "sample:empty", {"found": False})
    exchange("GET", "sample:text", {"found": True, "value": '{"a":1}'})


def test_s1_06_01_number_and_null_text(exchange):
    """S1-06-01: number·word 없음. 숫자와 null 글자는 문자열로 전달한다."""
    exchange("PUT", "sample:number", {"ok": True}, value="10")
    exchange("PUT", "sample:word", {"ok": True}, value="null")
    exchange("GET", "sample:number", {"found": True, "value": "10"})
    exchange("GET", "sample:word", {"found": True, "value": "null"})


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
def test_s1_07_input_boundaries(exchange, key, value, accepted):
    """S1-07: 각 행 one=hello, two=world. 제한 검증 자체는 저장소 책임."""
    # 합격 여부를 HTTP가 판정하는 것이 아니라 저장소 반환을 연결해야 한다.
    result = {"ok": True} if accepted else {"error": "INVALID_INPUT"}
    exchange("PUT", key, result, value=value, status=200 if accepted else 400)

    # 원본이 요구하는 유효한 키만 조회한다. 조회 간 순서는 구현 계약이 아니다.
    exchange("GET", "sample:two", {"found": True, "value": "world"})
    if accepted and key != "sample:one":
        exchange("GET", key, {"found": True, "value": "hello"})
    expected_value = value if accepted and key == "sample:one" else "hello"
    exchange("GET", "sample:one", {"found": True, "value": expected_value})


def test_s1_08_null_value_and_repeated_delete(exchange):
    """S1-08: null·missing 없음. 실제 null과 키 없음을 구분한다."""
    exchange("PUT", "sample:null", {"ok": True}, value=None)
    exchange("GET", "sample:null", {"found": True, "value": None})
    exchange("GET", "sample:missing", {"found": False})
    exchange("DELETE", "sample:null", {"found": True, "value": None})
    exchange("DELETE", "sample:null", {"found": False})
    exchange("GET", "sample:null", {"found": False})


def test_s1_08_01_overwrite_text_with_null_and_back(exchange):
    """S1-08-01: one=hello, two=world 상황. None·updated 전달을 확인한다."""
    exchange("PUT", "sample:one", {"ok": True}, value=None)
    exchange("GET", "sample:one", {"found": True, "value": None})
    exchange("PUT", "sample:one", {"ok": True}, value="updated")
    exchange("GET", "sample:one", {"found": True, "value": "updated"})
    exchange("GET", "sample:two", {"found": True, "value": "world"})


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_storage_invalid_input_maps_to_http_400(exchange, method):
    """함수 계약의 오류 반환 연결만 검증한다. 입력 거절 판정은 검증하지 않는다."""
    # sample:one 자체가 무효라는 뜻이 아니다. 저장소 반환만 지정한다.
    exchange(method, "sample:one", {"error": "INVALID_INPUT"}, status=400)


@pytest.fixture
def http_boundary():
    """응답을 검사할 수 있도록 미처리 예외도 실제 서버처럼 500으로 응답한다."""
    from app import create_app

    storage = Mock(spec_set=["set", "get", "delete"])
    app = create_app(storage)
    app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
    return app.test_client(), storage


@pytest.mark.parametrize(
    "body",
    [
        pytest.param('{"key":', id="malformed-json"),
        pytest.param("[]", id="array-body"),
        pytest.param('"hello"', id="string-body"),
        pytest.param("10", id="number-body"),
        pytest.param("true", id="boolean-body"),
        pytest.param("null", id="null-body"),
        pytest.param('{"value":"hello"}', id="missing-key"),
        pytest.param('{"key":"sample:one"}', id="missing-value"),
        pytest.param("{}", id="missing-both-fields"),
    ],
)
def test_put_format_error_rejected_without_storage_call(http_boundary, body):
    """JSON·객체 구조·필수 필드 오류는 HTTP가 400 JSON으로 직접 거절한다."""
    client, storage = http_boundary
    response = client.put("/kv", data=body, content_type="application/json")

    assert storage.mock_calls == []
    assert response.status_code == 400
    assert response.is_json
    assert response.get_json() == {"error": "INVALID_INPUT"}


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_missing_query_key_rejected_without_storage_call(http_boundary, method):
    """쿼리 key 누락은 빈 key와 구분하며 저장소에 전달하지 않는다."""
    client, storage = http_boundary
    response = client.open("/kv", method=method)

    assert storage.mock_calls == []
    assert response.status_code == 400
    assert response.is_json
    assert response.get_json() == {"error": "INVALID_INPUT"}


@pytest.mark.parametrize("field", ["key", "value"])
@pytest.mark.parametrize(
    "invalid_value",
    [
        pytest.param(10, id="number"),
        pytest.param(True, id="boolean"),
        pytest.param([], id="array"),
        pytest.param({"a": 1}, id="object"),
    ],
)
def test_put_type_validation_delegated_to_storage(http_boundary, field, invalid_value):
    """필드가 있으면 HTTP는 타입을 바꾸거나 거절하지 않고 저장소에 전달한다."""
    client, storage = http_boundary
    body = {"key": "sample:one", "value": "hello"}
    body[field] = invalid_value
    storage.set.return_value = {"error": "INVALID_INPUT"}

    response = client.put("/kv", json=body)

    assert storage.mock_calls == [call.set(body["key"], body["value"])]
    args = storage.set.call_args.args
    assert type(args[0]) is type(body["key"])
    assert type(args[1]) is type(body["value"])
    assert response.status_code == 400
    assert response.is_json
    assert response.get_json() == {"error": "INVALID_INPUT"}


@pytest.mark.parametrize(
    "method,operation", [("PUT", "set"), ("GET", "get"), ("DELETE", "delete")]
)
def test_unexpected_storage_error_is_not_invalid_input(http_boundary, method, operation):
    """내부 예외를 입력 오류로 삼키지 않는다. 새 HTTP 오류 정책은 정하지 않는다."""
    client, storage = http_boundary
    client.application.config["PROPAGATE_EXCEPTIONS"] = True
    error = RuntimeError("unexpected storage failure")
    getattr(storage, operation).side_effect = error

    with pytest.raises(RuntimeError) as caught:
        if method == "PUT":
            client.put("/kv", json={"key": "sample:one", "value": "hello"})
        else:
            client.open("/kv", method=method, query_string={"key": "sample:one"})

    assert caught.value is error
    expected_call = (
        call.set("sample:one", "hello")
        if method == "PUT"
        else getattr(call, operation)("sample:one")
    )
    assert storage.mock_calls == [expected_call]
