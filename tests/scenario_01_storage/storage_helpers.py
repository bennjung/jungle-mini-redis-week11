"""실행 코드와 분리한 저장소 상태 관찰 도구."""


def snapshot(store):
    """테스트 대상의 전체 키·value 상태를 독립된 사본으로 반환한다."""
    return store._data.copy()
