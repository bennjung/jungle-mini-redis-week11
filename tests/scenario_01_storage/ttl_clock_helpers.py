"""TTL active tick 테스트가 쓰는 가짜 시계·표본 선택기·시간 예산 시계.

계약: resc/scenario_01_storage/01-ttl-test-cases.md 공통 절차, 01-ttl-c-active-contract.md.
실제 시간을 기다리지 않고 tick의 시점, 표본, 시간 예산을 고정한다.
"""

BASE_MS = 1_700_000_000_000


class FakeClock:
    """정수 epoch ms를 돌려주는 가짜 벽시계. advance(ms)로 시간을 진행한다."""

    def __init__(self, now=BASE_MS):
        self._now = now

    def __call__(self):
        return self._now

    def advance(self, ms):
        self._now += ms


class FirstKSampler:
    """현재 keys의 앞에서부터 최대 k개를 새 리스트로 돌려주는 표본 선택기.

    반환값은 항상 새 리스트이며(계약: sampler는 새 리스트를 반환), 호출마다
    돌려준 표본을 calls에 기록해 사이클 실행 횟수를 검증할 수 있게 한다.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, keys, k):
        sample = list(keys[:k])
        self.calls.append(sample)
        return sample


class FixedMonotonic:
    """항상 같은 초 단위 시간을 돌려주는 monotonic 시계. 예산이 소진되지 않는다."""

    def __init__(self, now=0.0):
        self._now = now

    def __call__(self):
        return self._now


class StepMonotonic:
    """호출될 때마다 step_s초 진행하는 monotonic 시계. 예산 소진 시점을 고정한다."""

    def __init__(self, step_s=0.0002, start_s=0.0):
        self._now = start_s
        self._step = step_s

    def __call__(self):
        value = self._now
        self._now += self._step
        return value
