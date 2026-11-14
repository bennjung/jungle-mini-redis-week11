"""TTL 구조를 실행 코드와 분리해 관찰하는 테스트 전용 도구."""


def ttl_snapshot(store):
    """`_expires`·`_ttl_keys`·`_ttl_pos`의 독립된 사본을 반환한다."""
    return (store._expires.copy(), list(store._ttl_keys), store._ttl_pos.copy())


def assert_ttl_invariants(store):
    """TTL 구조의 키 집합·위치·길이·중복 없음 불변 조건을 검사한다."""
    expires = set(store._expires)
    ttl_keys = list(store._ttl_keys)

    assert expires == set(ttl_keys) == set(store._ttl_pos)
    assert len(store._expires) == len(ttl_keys) == len(store._ttl_pos)
    assert len(ttl_keys) == len(set(ttl_keys))
    for key in ttl_keys:
        assert store._ttl_keys[store._ttl_pos[key]] == key
