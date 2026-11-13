"""딕셔너리를 사용하는 Mini Redis 저장소."""


class MiniRedis:
    """각 인스턴스가 독립된 키·value 상태를 소유한다."""

    def __init__(self):
        self._data = {}

    def set(self, key, value):
        """검증을 마친 뒤 지정한 키만 추가하거나 덮어쓴다."""
        if not isinstance(key, str):
            return {"error": "INVALID_INPUT"}
        if value is not None and not isinstance(value, str):
            return {"error": "INVALID_INPUT"}
        try:
            key_size = len(key.encode("utf-8"))
            value_size = 0 if value is None else len(value.encode("utf-8"))
        except UnicodeEncodeError:
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

