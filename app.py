"""문서 01의 HTTP 요청을 전달받은 저장소 호출에 연결한다."""

from flask import Flask, request
from werkzeug.exceptions import BadRequest


def create_app(storage):
    """세 API가 같은 저장소를 사용하는 Flask 앱을 생성한다."""
    app = Flask(__name__)

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

    return app
