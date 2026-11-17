"""HTTP·JSON 응답의 구조와 타입을 검사하는 공통 도구.

숫자 비교 모드는 호출부에서 정한다.
- numbers="exact": int와 float를 구분한다.
- numbers="json": 둘 다 JSON 숫자로 보고 허용한다. bool은 숫자와 구분한다.
"""


def assert_json(actual, expected, *, numbers="exact"):
    """JSON 값의 타입과 내용을 재귀적으로 확인한다."""
    if numbers not in ("exact", "json"):
        raise ValueError(f"알 수 없는 숫자 비교 모드: {numbers!r}")

    # bool은 int의 하위 타입이므로 숫자보다 먼저 구분한다.
    if isinstance(expected, bool) or expected is None or isinstance(expected, str):
        assert type(actual) is type(expected), (actual, expected)
        assert actual == expected, (actual, expected)
        return

    if isinstance(expected, (int, float)):
        if numbers == "json":
            assert type(actual) in (int, float), (actual, expected)
        else:
            assert type(actual) is type(expected), (actual, expected)
        assert actual == expected, (actual, expected)
        return

    if isinstance(expected, dict):
        assert type(actual) is dict, (actual, expected)
        assert actual.keys() == expected.keys(), (actual, expected)
        for key in expected:
            assert_json(actual[key], expected[key], numbers=numbers)
        return

    if isinstance(expected, list):
        assert type(actual) is list, (actual, expected)
        assert len(actual) == len(expected), (actual, expected)
        for item, wanted in zip(actual, expected):
            assert_json(item, wanted, numbers=numbers)
        return

    assert type(actual) is type(expected), (actual, expected)
    assert actual == expected, (actual, expected)


def check_response(response, status, expected, *, numbers="exact"):
    """TestClient 응답의 상태 코드·Content-Type·JSON 본문을 확인한다."""
    assert response.status_code == status, (
        "HTTP 상태 불일치", response.status_code, status, response.text[:300],
    )
    assert response.headers["content-type"].startswith("application/json"), (
        "JSON 응답 아님", response.headers.get("content-type"),
    )
    body = response.json()
    assert_json(body, expected, numbers=numbers)
    return body


def check_raw_response(
    status, content_type, body, expected_status, expected, *, numbers="exact"
):
    """원시 HTTP 응답의 상태·Content-Type·이미 해석한 JSON 본문을 확인한다."""
    assert status == expected_status, (status, expected_status)
    assert content_type == "application/json", content_type
    assert_json(body, expected, numbers=numbers)
    return body
