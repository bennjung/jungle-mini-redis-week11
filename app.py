"""문서 01의 HTTP 요청과 증감 요청을 전달받은 저장소 호출에 연결한다."""

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# 저장소가 반환한 오류 코드를 승인된 HTTP 상태로 연결한다.
ERROR_STATUS = {
    "INVALID_INPUT": 400,
    "TYPE_MISMATCH": 400,
    "NEGATIVE_AMOUNT": 400,
    "NOT_FOUND": 404,
    "OUT_OF_RANGE": 422,
    "CONDITION_NOT_MET": 409,
}

# 증감 연산별로 허용하는 선택 조건 필드 하나를 정한다.
CONDITION_FIELD = {"incr": "maximum", "decr": "minimum"}

_INVALID_BODY = object()


async def _json_body(request):
    """JSON 본문을 해석한다. 형식 오류는 저장소 호출 없이 거절한다."""
    try:
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _INVALID_BODY


def create_app(storage):
    """모든 API가 같은 저장소를 사용하는 FastAPI 앱을 생성한다."""

    @asynccontextmanager
    async def lifespan(app):
        """TTL active cleanup maintenance task를 소유할 자리다.

        아직 TTL과 정리 루프를 구현하지 않았으므로 시작하는 task가 없다.
        후속 TTL 단계에서 이 블록에서 주기 task를 만들고, yield 이후 취소·대기한다.
        lifespan은 요청 처리 경로가 아니므로 저장소 호출이나 HTTP 응답에 관여하지 않는다.
        """
        yield

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(Exception)
    async def internal_server_error(request, error):
        """예상하지 못한 내부 오류의 상세를 노출하지 않는다."""
        return JSONResponse({"error": "INTERNAL_SERVER_ERROR"}, status_code=500)

    @app.put("/kv")
    async def put_kv(request: Request):
        body = await _json_body(request)
        if (
            body is _INVALID_BODY
            or not isinstance(body, dict)
            or "key" not in body
            or "value" not in body
        ):
            return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)

        result = storage.set(body["key"], body["value"])
        status = 400 if result == {"error": "INVALID_INPUT"} else 200
        return JSONResponse(result, status_code=status)

    @app.get("/kv")
    async def get_kv(request: Request):
        key = request.query_params.get("key")
        if key is None:
            return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)

        result = storage.get(key)
        status = 400 if result == {"error": "INVALID_INPUT"} else 200
        return JSONResponse(result, status_code=status)

    @app.delete("/kv")
    async def delete_kv(request: Request):
        key = request.query_params.get("key")
        if key is None:
            return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)

        result = storage.delete(key)
        status = 400 if result == {"error": "INVALID_INPUT"} else 200
        return JSONResponse(result, status_code=status)

    async def counter_kv(request, operation):
        """증감 요청 하나를 검증한 뒤 해당 저장소 연산을 한 번 호출한다."""
        body = await _json_body(request)
        condition = CONDITION_FIELD[operation]
        if (
            body is _INVALID_BODY
            or not isinstance(body, dict)
            or "key" not in body
            or "amount" not in body
            or not set(body) <= {"key", "amount", condition}
        ):
            return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)

        if condition in body:
            # 실제 null도 생략과 구분해 그대로 전달한다. 타입 판정은 저장소 책임이다.
            result = getattr(storage, operation)(
                body["key"], body["amount"], **{condition: body[condition]}
            )
        else:
            result = getattr(storage, operation)(body["key"], body["amount"])
        return JSONResponse(
            result, status_code=ERROR_STATUS.get(result.get("error"), 200)
        )

    @app.post("/kv/incr")
    async def incr_kv(request: Request):
        return await counter_kv(request, "incr")

    @app.post("/kv/decr")
    async def decr_kv(request: Request):
        return await counter_kv(request, "decr")

    return app


if __name__ == "__main__":
    import uvicorn

    from storage import MiniRedis

    # 한 저장소를 요청 간 공유한다. 단일 프로세스·단일 이벤트 루프로 실행한다.
    uvicorn.run(
        create_app(MiniRedis()),
        host="127.0.0.1",
        port=5000,
        workers=1,
    )
