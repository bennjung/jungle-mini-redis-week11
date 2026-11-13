"""문서 01의 HTTP 요청과 증감 요청을 전달받은 저장소 호출에 연결한다."""

from flask import Flask, request
from werkzeug.exceptions import BadRequest

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


def create_app(storage):
    """모든 API가 같은 저장소를 사용하는 Flask 앱을 생성한다."""
    app = Flask(__name__)

    @app.errorhandler(500)
    def internal_server_error(error):
        """예상하지 못한 내부 오류의 상세를 노출하지 않는다."""
        return {"error": "INTERNAL_SERVER_ERROR"}, 500

    @app.put("/kv")
    def put_kv():
        try:
            body = request.get_json()
        except BadRequest:
            return {"error": "INVALID_INPUT"}, 400

        if not isinstance(body, dict) or "key" not in body or "value" not in body:
            return {"error": "INVALID_INPUT"}, 400

        result = storage.set(body["key"], body["value"])
        status = 400 if result == {"error": "INVALID_INPUT"} else 200
        return result, status

    @app.get("/kv")
    def get_kv():
        if "key" not in request.args:
            return {"error": "INVALID_INPUT"}, 400

        result = storage.get(request.args["key"])
        status = 400 if result == {"error": "INVALID_INPUT"} else 200
        return result, status

    @app.delete("/kv")
    def delete_kv():
        if "key" not in request.args:
            return {"error": "INVALID_INPUT"}, 400

        result = storage.delete(request.args["key"])
        status = 400 if result == {"error": "INVALID_INPUT"} else 200
        return result, status

    def counter_kv(operation):
        """증감 요청 하나를 검증한 뒤 해당 저장소 연산을 한 번 호출한다."""
        try:
            body = request.get_json()
        except BadRequest:
            return {"error": "INVALID_INPUT"}, 400

        condition = CONDITION_FIELD[operation]
        if (
            not isinstance(body, dict)
            or "key" not in body
            or "amount" not in body
            or not set(body) <= {"key", "amount", condition}
        ):
            return {"error": "INVALID_INPUT"}, 400

        if condition in body:
            # 실제 null도 생략과 구분해 그대로 전달한다. 타입 판정은 저장소 책임이다.
            result = getattr(storage, operation)(
                body["key"], body["amount"], **{condition: body[condition]}
            )
        else:
            result = getattr(storage, operation)(body["key"], body["amount"])
        return result, ERROR_STATUS.get(result.get("error"), 200)

    @app.post("/kv/incr")
    def incr_kv():
        return counter_kv("incr")

    @app.post("/kv/decr")
    def decr_kv():
        return counter_kv("decr")

    return app


if __name__ == "__main__":
    from storage import MiniRedis

    # 한 저장소를 요청 간 공유한다. 동시 호출·운영 배포를 위한 서버 설정은 아니다.
    create_app(MiniRedis()).run(
        host="127.0.0.1", port=5000, threaded=False, use_reloader=False
    )
