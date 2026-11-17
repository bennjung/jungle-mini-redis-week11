"""랜덤 스트레스 테스트가 공유하는 입력 생성기와 설정.

저장소 fuzz와 HTTP soak가 같은 연산 비중·키 분포·TTL 분포를 쓰게 한다.
실제 입력값은 seed 기반으로 생성하고, 경계값은 테스트 파일에서 별도로 주입한다.
"""

import random

# 전체 요청의 90%는 정상, 10%는 잘못된 입력으로 보낸다.
INVALID_RATIO = 0.10

# 정상 연산의 상대 비중. get/ttl은 읽기, set/delete/expire/persist는 쓰기,
# incr/decr는 카운터다.
OPERATION_WEIGHTS = {
    "get": 25,
    "ttl": 10,
    "set": 25,
    "delete": 10,
    "expire": 10,
    "persist": 5,
    "incr": 10,
    "decr": 5,
}

HOT_KEY_COUNT = 100
HOT_KEY_RATIO = 0.80
TTL_SHORT_MS = (1_000, 3_000, 10_000)
TTL_LONG_MS = (30_000, 60_000)
TTL_SHORT_RATIO = 0.80
TTL_WRITE_RATIO = 0.25

_VALUE_KINDS = ("string", "empty_string", "int", "float", "bool", "null",
                "list", "object")


def weighted_choice(rng, weights):
    """{이름: 가중치}에서 하나를 고른다. 가중치는 양수여야 한다."""
    names = list(weights)
    values = [weights[name] for name in names]
    return rng.choices(names, weights=values, k=1)[0]


def hot_keys():
    """저장소 fuzz가 쓰는 일반 hot 키 목록."""
    return [f"hot:{index:03d}" for index in range(HOT_KEY_COUNT)]


def counter_keys():
    """HTTP soak의 counter 전용 hot 키. set/delete/expire 대상이 아니다."""
    return [f"hot:counter:{index:03d}" for index in range(HOT_KEY_COUNT)]


def cache_keys():
    """HTTP soak의 cache/get/TTL hot 키."""
    return [f"hot:cache:{index:03d}" for index in range(HOT_KEY_COUNT)]


def plain_keys():
    """HTTP soak의 delete/expire hot 키."""
    return [f"hot:plain:{index:03d}" for index in range(HOT_KEY_COUNT)]


def choose_key(rng, keyspace):
    """저장소 fuzz용: 80%는 일반 hot 키, 20%는 넓은 cold 키 공간."""
    if rng.random() < HOT_KEY_RATIO:
        return rng.choice(hot_keys())
    return f"chaos:{rng.randrange(keyspace)}"


def choose_http_key(rng, operation, keyspace):
    """HTTP soak용: 연산에 맞는 hot 키 풀을 고른다.

    counter 키는 incr/decr 전용으로 남겨 성공 카운터 경로를 충분히 누른다.
    """
    if operation in {"incr", "decr"}:
        if rng.random() < 0.9:
            return rng.choice(counter_keys())
        return f"chaos:{rng.randrange(keyspace)}"
    if operation in {"set", "get", "ttl", "persist"}:
        if rng.random() < 0.8:
            return rng.choice(cache_keys())
        return f"chaos:{rng.randrange(keyspace)}"
    if operation in {"delete", "expire"}:
        if rng.random() < 0.8:
            return rng.choice(plain_keys())
        return f"chaos:{rng.randrange(keyspace)}"
    return choose_key(rng, keyspace)


def random_value(rng):
    """저장 계약을 통과하는 작은 JSON 값을 만든다."""
    kind = rng.choice(_VALUE_KINDS)
    if kind == "string":
        return "v" + "".join(rng.choice("abcdefghij") for _ in range(rng.randrange(0, 16)))
    if kind == "empty_string":
        return ""
    if kind == "int":
        return rng.randrange(-1_000_000, 1_000_000)
    if kind == "float":
        return rng.choice([0.0, -0.0, 1.5, -3.25, 100.0])
    if kind == "bool":
        return rng.choice([True, False])
    if kind == "null":
        return None
    if kind == "list":
        return [random_value(rng) for _ in range(rng.randrange(0, 4))]
    return {f"f{index}": random_value(rng) for index in range(rng.randrange(0, 4))}


def random_ttl(rng):
    """짧은 TTL 80%, 긴 TTL 20%."""
    if rng.random() < TTL_SHORT_RATIO:
        return rng.choice(TTL_SHORT_MS)
    return rng.choice(TTL_LONG_MS)


def random_amount(rng):
    """증감량은 0 이상의 작은 정수."""
    return rng.randrange(0, 6)


def random_bound(rng, sign):
    """incr의 maximum 또는 decr의 minimum으로 쓸 작은 정수."""
    if sign > 0:
        return rng.randrange(0, 20)
    return rng.randrange(-20, 1)


def percentile(values, ratio):
    """정렬된 표본에서 백분위수를 계산한다. 값이 없으면 None."""
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * ratio) - 1))
    return ordered[index]
