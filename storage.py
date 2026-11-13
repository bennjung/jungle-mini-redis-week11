"""딕셔너리를 사용하는 Mini Redis 저장소."""

import json


def _json_content_supported(value, path):
    """JSON으로 보관할 수 있는 Python 값인지 확인한다. 객체 필드 이름은 문자열만 허용한다."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return True
    if isinstance(value, list):
        if id(value) in path:
            return False
        path.add(id(value))
        supported = all(_json_content_supported(item, path) for item in value)
        path.discard(id(value))
        return supported
    if isinstance(value, dict):
        if id(value) in path:
            return False
        path.add(id(value))
        supported = all(
            isinstance(field, str) and _json_content_supported(item, path)
            for field, item in value.items()
        )
        path.discard(id(value))
        return supported
    return False


class MiniRedis:
    """각 인스턴스가 독립된 키·value 상태를 소유한다."""

    def __init__(self):
        self._data = {}

    def set(self, key, value):
        """검증을 마친 뒤 지정한 키만 추가하거나 덮어쓴다."""
        if not isinstance(key, str):
            return {"error": "INVALID_INPUT"}
        if not _json_content_supported(value, set()):
            return {"error": "INVALID_INPUT"}
        try:
            key_size = len(key.encode("utf-8"))
            if isinstance(value, str):
                value_size = len(value.encode("utf-8"))
            elif value is None:
                value_size = 0
            else:
                compact = json.dumps(
                    value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                )
                value_size = len(compact.encode("utf-8"))
        except (UnicodeEncodeError, TypeError, ValueError):
            return {"error": "INVALID_INPUT"}
        if not 1 <= key_size <= 256 or value_size > 65_536:
            return {"error": "INVALID_INPUT"}

        self._data[key] = value
        return {"ok": True}

    def get(self, key):
        """키의 존재 여부와 보관한 value를 상태 변경 없이 반환한다."""
        if not isinstance(key, str):
            return {"error": "INVALID_INPUT"}
        try:
            key_size = len(key.encode("utf-8"))
        except UnicodeEncodeError:
            return {"error": "INVALID_INPUT"}
        if not 1 <= key_size <= 256:
            return {"error": "INVALID_INPUT"}

        if key not in self._data:
            return {"found": False}
        return {"found": True, "value": self._data[key]}

    def delete(self, key):
        """항목이 있으면 제거하고 삭제 직전의 value를 반환한다."""
        if not isinstance(key, str):
            return {"error": "INVALID_INPUT"}
        try:
            key_size = len(key.encode("utf-8"))
        except UnicodeEncodeError:
            return {"error": "INVALID_INPUT"}
        if not 1 <= key_size <= 256:
            return {"error": "INVALID_INPUT"}

        if key not in self._data:
            return {"found": False}
        value = self._data.pop(key)
        return {"found": True, "value": value}

