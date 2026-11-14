"""범위 A: 저장소 구현과 독립적으로 호출·반환·HTTP 오류 연결을 검증한다."""

from copy import deepcopy
import json
from unittest.mock import Mock, call

import pytest
from fastapi.testclient import TestClient

from app import create_app
from tests.scenario_02_concurrent_counter.contract_helpers import check
from tests.scenario_02_concurrent_counter.test_counter_http import BAD_BODIES


@pytest.fixture
def boundary():
    storage = Mock(spec_set=["set", "get", "delete", "incr", "decr"])
    app = create_app(storage)
    # 미처리 예외도 실제 서버처럼 500 응답으로 관찰한다.
    return TestClient(app, raise_server_exceptions=False), storage


@pytest.mark.parametrize("operation", ["incr", "decr"])
@pytest.mark.parametrize("condition", ["omitted", "number", "null"])
def test_a01_exactly_one_call_and_condition_presence(boundary, operation, condition):
    client, storage = boundary
    body = {"key": "counter:one", "amount": 0.5}
    kwargs = {}
    field = "maximum" if operation == "incr" else "minimum"
    if condition != "omitted":
        body[field] = 20 if condition == "number" else None
        kwargs[field] = body[field]
    result = ({"error": "TYPE_MISMATCH", "field": field, "actual_type": "null"}
              if condition == "null" else {"ok": True, "value": 10.5})
    getattr(storage, operation).return_value = deepcopy(result)
    response = client.post(f"/kv/{operation}", json=body)
    assert storage.mock_calls == [getattr(call, operation)("counter:one", 0.5, **kwargs)]
    check(response, 400 if condition == "null" else 200, result)


@pytest.mark.parametrize("operation", ["incr", "decr"])
@pytest.mark.parametrize("amount", ["1", None, True, [], {}, -1, 0])
def test_a02_pass_amount_without_conversion(boundary, operation, amount):
    client, storage = boundary
    result = {"error": "NEGATIVE_AMOUNT"}
    getattr(storage, operation).return_value = result
    response = client.post(f"/kv/{operation}", json={"key": "counter:one", "amount": amount})
    assert storage.mock_calls == [getattr(call, operation)("counter:one", amount)]
    args = getattr(storage, operation).call_args.args
    assert type(args[1]) is type(amount)
    check(response, 400, result)


ERRORS = [
    (400, {"error": "INVALID_INPUT"}),
    (400, {"error": "TYPE_MISMATCH", "field": "value", "actual_type": "boolean"}),
    (400, {"error": "NEGATIVE_AMOUNT"}),
    (404, {"error": "NOT_FOUND"}),
    (422, {"error": "OUT_OF_RANGE"}),
    (409, {"error": "CONDITION_NOT_MET", "value": 9}),
]


@pytest.mark.parametrize("operation", ["incr", "decr"])
@pytest.mark.parametrize("status,result", ERRORS)
def test_a03_error_mapping(boundary, operation, status, result):
    client, storage = boundary
    getattr(storage, operation).return_value = deepcopy(result)
    response = client.post(f"/kv/{operation}", json={"key": "counter:one", "amount": 1})
    assert storage.mock_calls == [getattr(call, operation)("counter:one", 1)]
    check(response, status, result)


@pytest.mark.parametrize("operation", ["incr", "decr"])
@pytest.mark.parametrize("body", BAD_BODIES[:9])
def test_a04_bad_shape_or_fields_do_not_call_storage(boundary, operation, body):
    client, storage = boundary
    response = client.post(f"/kv/{operation}", content=json.dumps(body),
                           headers={"Content-Type": "application/json"})
    check(response, 400, {"error": "INVALID_INPUT"})
    assert storage.mock_calls == []


@pytest.mark.parametrize("operation", ["incr", "decr"])
def test_a04_wrong_condition_does_not_call_storage(boundary, operation):
    client, storage = boundary
    wrong = "minimum" if operation == "incr" else "maximum"
    response = client.post(f"/kv/{operation}", json={"key": "counter:one", "amount": 1, wrong: 0})
    check(response, 400, {"error": "INVALID_INPUT"})
    assert storage.mock_calls == []


@pytest.mark.parametrize("operation", ["incr", "decr"])
def test_a04_malformed_json_does_not_call_storage(boundary, operation):
    client, storage = boundary
    response = client.post(f"/kv/{operation}", content=b'{"key":',
                           headers={"Content-Type": "application/json"})
    check(response, 400, {"error": "INVALID_INPUT"})
    assert storage.mock_calls == []


@pytest.mark.parametrize("operation", ["incr", "decr"])
@pytest.mark.parametrize("key", [None, "", 1, "a" * 257])
def test_a05_key_validation_is_storage_responsibility(boundary, operation, key):
    client, storage = boundary
    getattr(storage, operation).return_value = {"error": "INVALID_INPUT"}
    response = client.post(f"/kv/{operation}", json={"key": key, "amount": 1})
    assert storage.mock_calls == [getattr(call, operation)(key, 1)]
    check(response, 400, {"error": "INVALID_INPUT"})


@pytest.mark.parametrize("operation", ["incr", "decr"])
def test_a06_unexpected_exception_is_500(boundary, operation):
    client, storage = boundary
    getattr(storage, operation).side_effect = RuntimeError("private detail")
    response = client.post(f"/kv/{operation}", json={"key": "counter:one", "amount": 1})
    assert storage.mock_calls == [getattr(call, operation)("counter:one", 1)]
    check(response, 500, {"error": "INTERNAL_SERVER_ERROR"})


@pytest.mark.parametrize("value", [1, -3, 0.25, True, None, [1, "가"], {"a": [True, None]}])
def test_a07_json_set_and_return_types(boundary, value):
    client, storage = boundary
    storage.set.return_value = {"ok": True}
    check(client.put("/kv", json={"key": "sample:json", "value": value}), 200, {"ok": True})
    assert storage.mock_calls == [call.set("sample:json", value)]
    assert type(storage.set.call_args.args[1]) is type(value)
    for method, operation in (("GET", "get"), ("DELETE", "delete")):
        storage.reset_mock()
        result = {"found": True, "value": deepcopy(value)}
        getattr(storage, operation).return_value = result
        check(client.request(method, "/kv", params={"key": "sample:json"}), 200, result)
        assert storage.mock_calls == [getattr(call, operation)("sample:json")]
