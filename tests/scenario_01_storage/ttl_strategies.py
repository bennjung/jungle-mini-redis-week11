"""Hypothesis가 쓰는 TTL·KV 입력 전략.

키와 값은 저장 계약을 통과하는 범위로 제한한다. TTL·증감량·조건은
경계와 음수·0을 포함한다.
"""

from hypothesis import strategies as st

_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    max_size=20,
)

KEY = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=1,
    max_size=40,
)

_SCALAR = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**9), max_value=10**9),
    st.floats(allow_nan=False, allow_infinity=False),
    _TEXT,
)

VALUE = st.recursive(
    _SCALAR,
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(keys=_TEXT, values=children, max_size=3),
    ),
    max_leaves=6,
)

TTL = st.integers(min_value=1, max_value=10_000)
EXPIRY_TTL = st.integers(min_value=-1_000, max_value=10_000)
AMOUNT = st.integers(min_value=0, max_value=10)
BOUND = st.integers(min_value=-20, max_value=20)
