"""TTL·증감 계약을 검증하는 참조 모델.

property/state-machine 테스트와 저장소 랜덤 fuzz가 같은 모델을 쓴다.
"""

_MISSING = object()


def _json_type(value):
    """TYPE_MISMATCH의 actual_type에 쓸 JSON 타입 이름."""
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


class ReferenceModel:
    """MiniRedis의 TTL·증감 계약을 단순화한 참조 모델."""

    def __init__(self, clock):
        self._clock = clock
        self.data = {}
        self.expires = {}

    def _now(self):
        return int(self._clock())

    def _lazy(self, key, now=None):
        now = self._now() if now is None else now
        if key in self.expires and now >= self.expires[key]:
            self.data.pop(key, None)
            self.expires.pop(key, None)

    def set(self, key, value, ttl=None):
        now = self._now()
        self._lazy(key, now)
        self.data[key] = value
        if ttl is None:
            self.expires.pop(key, None)
        else:
            self.expires[key] = now + ttl
        return {"ok": True}

    def get(self, key):
        now = self._now()
        self._lazy(key, now)
        if key not in self.data:
            return {"found": False}
        return {"found": True, "value": self.data[key]}

    def delete(self, key):
        now = self._now()
        self._lazy(key, now)
        if key not in self.data:
            return {"found": False}
        value = self.data.pop(key)
        self.expires.pop(key, None)
        return {"found": True, "value": value}

    def expire(self, key, ttl):
        now = self._now()
        self._lazy(key, now)
        if key not in self.data:
            return {"found": False}
        if ttl <= 0:
            self.data.pop(key, None)
            self.expires.pop(key, None)
            return {"found": True, "ttl": 0}
        self.expires[key] = now + ttl
        return {"found": True, "ttl": ttl}

    def ttl(self, key):
        now = self._now()
        self._lazy(key, now)
        if key not in self.data:
            return {"found": False}
        if key not in self.expires:
            return {"found": True, "ttl": -1}
        return {"found": True, "ttl": self.expires[key] - now}

    def persist(self, key):
        now = self._now()
        self._lazy(key, now)
        if key not in self.data:
            return {"found": False}
        had_ttl = key in self.expires
        self.expires.pop(key, None)
        return {"found": True, "persisted": had_ttl}

    def _adjust(self, key, amount, bound, field, sign):
        self._lazy(key)
        if key not in self.data:
            return {"error": "NOT_FOUND"}
        current = self.data[key]
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            return {
                "error": "TYPE_MISMATCH",
                "field": "value",
                "actual_type": _json_type(current),
            }
        result = current + amount if sign == 1 else current - amount
        if bound is not _MISSING:
            if sign == 1 and result > bound:
                return {"error": "CONDITION_NOT_MET", "value": current}
            if sign == -1 and result < bound:
                return {"error": "CONDITION_NOT_MET", "value": current}
        self.data[key] = result
        return {"ok": True, "value": result}

    def incr(self, key, amount, *, maximum=_MISSING):
        return self._adjust(key, amount, maximum, "maximum", 1)

    def decr(self, key, amount, *, minimum=_MISSING):
        return self._adjust(key, amount, minimum, "minimum", -1)
