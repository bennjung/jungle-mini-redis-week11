"""딕셔너리를 사용하는 Mini Redis 저장소."""

import json
import math
import random
import time


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

    def __init__(self, clock=None, sampler=None):
        self._data = {}
        self._expires = {}
        self._ttl_keys = []
        self._ttl_pos = {}
        self._clock = clock if clock is not None else lambda: int(time.time() * 1000)
        self._sampler = sampler if sampler is not None else random.sample

    def _now_ms(self):
        """현재 시각을 정수 epoch ms로 반환한다."""
        return int(self._clock())

    def _set_ttl(self, key, expiry_ms):
        """절대 만료 시각을 기록한다. TTL이 없던 키면 인덱스에 추가한다."""
        if key not in self._expires:
            self._ttl_pos[key] = len(self._ttl_keys)
            self._ttl_keys.append(key)
        self._expires[key] = expiry_ms

    def _clear_ttl(self, key):
        """TTL 구조에서만 제거하고 value는 남긴다."""
        if key not in self._expires:
            return
        index = self._ttl_pos.pop(key)
        last = self._ttl_keys.pop()
        if index < len(self._ttl_keys):
            self._ttl_keys[index] = last
            self._ttl_pos[last] = index
        del self._expires[key]

    def _drop_key(self, key):
        """네 구조에서 키를 제거한다. TTL 구조 제거의 유일한 경로다."""
        self._data.pop(key, None)
        self._clear_ttl(key)

    def _lazy_expire(self, key, now_ms=None):
        """그 키가 만료됐으면 제거하고, 만료 여부를 반환한다.

        now_ms를 넘기면 그 시각으로 판정한다. 호출자가 남은 시간 계산에도
        같은 시각을 쓰려고 넘길 수 있다.
        """
        if key not in self._expires:
            return False
        now = self._now_ms() if now_ms is None else now_ms
        if now < self._expires[key]:
            return False
        self._drop_key(key)
        return True

    def _maybe_active_expire(self):
        """타이머 tick. 같은 시점으로 active 사이클을 최대 2회 실행한다."""
        now = self._now_ms()
        expired, size = self._active_expire_cycle(now)
        if size > 0 and expired * 4 >= size:
            self._active_expire_cycle(now)

    def _active_expire_cycle(self, now_ms):
        """TTL 키에서 최대 20개를 뽑아 만료된 키를 제거한다."""
        keys = self._ttl_keys
        k = min(20, len(keys))
        if k == 0:
            return 0, 0
        if self._sampler is not None:
            sample = self._sampler(keys, k)
        else:
            sample = random.sample(keys, k)

        expired = 0
        for key in sample:
            expiry_ms = self._expires.get(key)
            # 표본을 뽑은 뒤 TTL이 갱신됐거나 키가 사라졌으면 삭제하지 않는다.
            if expiry_ms is None or now_ms < expiry_ms:
                continue
            self._drop_key(key)
            expired += 1
        return expired, len(sample)

    def set(self, key, value, ttl=None):
        """검증을 마친 뒤 지정한 키를 덮어쓰고 선택한 TTL을 적용한다."""
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
        if ttl is not None:
            # bool은 int의 하위 타입이므로 먼저 걸러낸다.
            if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
                return {"error": "INVALID_INPUT"}

        now = self._now_ms()
        self._lazy_expire(key, now)
        self._data[key] = value
        if ttl is None:
            self._clear_ttl(key)
        else:
            self._set_ttl(key, now + ttl)
        return {"ok": True}

    def get(self, key):
        """키의 존재 여부와 보관한 value를 반환한다. 만료된 키는 없는 키다."""
        if not isinstance(key, str):
            return {"error": "INVALID_INPUT"}
        try:
            key_size = len(key.encode("utf-8"))
        except UnicodeEncodeError:
            return {"error": "INVALID_INPUT"}
        if not 1 <= key_size <= 256:
            return {"error": "INVALID_INPUT"}

        self._lazy_expire(key)
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

        self._lazy_expire(key)
        if key not in self._data:
            return {"found": False}
        value = self._data[key]
        self._drop_key(key)
        return {"found": True, "value": value}

    def expire(self, key, ttl):
        """TTL을 설정·갱신한다. ttl이 0 이하면 키를 즉시 삭제한다."""
        if not isinstance(key, str):
            return {"error": "INVALID_INPUT"}
        try:
            key_size = len(key.encode("utf-8"))
        except UnicodeEncodeError:
            return {"error": "INVALID_INPUT"}
        if not 1 <= key_size <= 256:
            return {"error": "INVALID_INPUT"}

        now = self._now_ms()
        self._lazy_expire(key, now)
        if key not in self._data:
            return {"found": False}
        if isinstance(ttl, bool) or not isinstance(ttl, int):
            return {"error": "INVALID_INPUT"}

        if ttl <= 0:
            self._drop_key(key)
            return {"found": True, "ttl": 0}
        self._set_ttl(key, now + ttl)
        return {"found": True, "ttl": ttl}

    def ttl(self, key):
        """남은 TTL을 ms로 반환한다. TTL이 없으면 -1이다."""
        if not isinstance(key, str):
            return {"error": "INVALID_INPUT"}
        try:
            key_size = len(key.encode("utf-8"))
        except UnicodeEncodeError:
            return {"error": "INVALID_INPUT"}
        if not 1 <= key_size <= 256:
            return {"error": "INVALID_INPUT"}

        now = self._now_ms()
        self._lazy_expire(key, now)
        if key not in self._data:
            return {"found": False}
        if key not in self._expires:
            return {"found": True, "ttl": -1}
        return {"found": True, "ttl": self._expires[key] - now}

    def persist(self, key):
        """TTL만 제거하고 value는 유지한다."""
        if not isinstance(key, str):
            return {"error": "INVALID_INPUT"}
        try:
            key_size = len(key.encode("utf-8"))
        except UnicodeEncodeError:
            return {"error": "INVALID_INPUT"}
        if not 1 <= key_size <= 256:
            return {"error": "INVALID_INPUT"}

        self._lazy_expire(key)
        if key not in self._data:
            return {"found": False}
        if key not in self._expires:
            return {"found": True, "persisted": False}
        self._clear_ttl(key)
        return {"found": True, "persisted": True}

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

        self._lazy_expire(key)
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
