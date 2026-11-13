"""딕셔너리를 사용하는 Mini Redis 저장소."""

import json
import math


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


_MISSING = object()


def _json_type(value):
    """TYPE_MISMATCH의 actual_type에 쓸 JSON 타입 이름을 돌려준다."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


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

    def incr(self, key, amount, *, maximum=_MISSING):
        """현재 수치에 증감량을 더한 뒤 상한을 확인하고 저장한다."""
        return self._adjust(key, amount, maximum, "maximum", 1)

    def decr(self, key, amount, *, minimum=_MISSING):
        """현재 수치에서 증감량을 뺀 뒤 하한을 확인하고 저장한다."""
        return self._adjust(key, amount, minimum, "minimum", -1)

    def _adjust(self, key, amount, bound, field, sign):
        """키 → 증감량 → 조건 → 대상 → 결과 순서로 검증한 뒤 상태를 바꾼다."""
        if not isinstance(key, str):
            return {"error": "INVALID_INPUT"}
        try:
            key_size = len(key.encode("utf-8"))
        except UnicodeEncodeError:
            return {"error": "INVALID_INPUT"}
        if not 1 <= key_size <= 256:
            return {"error": "INVALID_INPUT"}

        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            return {"error": "TYPE_MISMATCH", "field": "amount",
                    "actual_type": _json_type(amount)}
        if isinstance(amount, float) and not math.isfinite(amount):
            return {"error": "OUT_OF_RANGE"}
        if amount < 0:
            return {"error": "NEGATIVE_AMOUNT"}

        if bound is not _MISSING:
            if isinstance(bound, bool) or not isinstance(bound, (int, float)):
                return {"error": "TYPE_MISMATCH", "field": field,
                        "actual_type": _json_type(bound)}
            if isinstance(bound, float) and not math.isfinite(bound):
                return {"error": "OUT_OF_RANGE"}

        if key not in self._data:
            return {"error": "NOT_FOUND"}
        current = self._data[key]
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            return {"error": "TYPE_MISMATCH", "field": "value",
                    "actual_type": _json_type(current)}

        try:
            # 큰 정수와 소수의 혼합 연산은 계산 자체에서 OverflowError가 날 수 있다.
            result = current + amount if sign == 1 else current - amount
            size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":"),
                                  allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError, OverflowError):
            return {"error": "OUT_OF_RANGE"}
        if size > 65_536:
            return {"error": "OUT_OF_RANGE"}

        if bound is not _MISSING:
            if sign == 1 and result > bound:
                return {"error": "CONDITION_NOT_MET", "value": current}
            if sign == -1 and result < bound:
                return {"error": "CONDITION_NOT_MET", "value": current}

        self._data[key] = result
        return {"ok": True, "value": result}
