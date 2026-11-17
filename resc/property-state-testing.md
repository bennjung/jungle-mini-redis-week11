# Property·State-machine 테스트 기록

## 목적

예시 테스트가 놓치는 입력 공간과 연속 상태 전이를 탐색한다.

- Property: 특정 입력 하나가 아니라 **모든 입력에서 성립해야 하는 규칙**을 검증한다.
- State machine: `set → expire → get → delete → incr …` 같은 연속 연산을 참조 모델과 비교한다.
- Hypothesis가 실패 입력·연산 시퀀스를 최소 케이스로 축소한다.

## 파일

- `tests/scenario_01_storage/ttl_strategies.py` — 키·JSON 값·TTL·증감량 전략
- `tests/scenario_01_storage/ttl_reference.py` — TTL·증감 참조 모델
- `tests/scenario_01_storage/test_ttl_properties.py` — 불변 조건 7개
- `tests/scenario_01_storage/test_ttl_state_machine.py` — 상태 기계 1개

## 실행

```bash
.venv/bin/python -m pytest \
  tests/scenario_01_storage/test_ttl_properties.py \
  tests/scenario_01_storage/test_ttl_state_machine.py -q
```

## 검증 범위

Property:

- `set` 후 `get`은 같은 `value`
- TTL 경계와 같은 `now` 사용
- `delete`는 이전 `value` 반환, 반복 삭제는 없음
- TTL 없는 `set`은 기존 TTL 제거
- `expire` 0·음수는 즉시 삭제
- `persist`는 `value` 유지, TTL 제거
- `incr`/`decr`는 살아 있는 TTL 유지

State machine:

- `set/get/delete/expire/ttl/persist/incr/decr`
- 가짜 시계 진행
- 매 단계 `_data`·`_expires`·TTL 보조 구조를 참조 모델과 비교

## 결과 (2026-11-17)

- Property 7개: **passed**
- State machine 1개: **passed** (`max_examples=50`, `stateful_step_count=50`)
- 전체 pytest: **668 passed, 39 skipped, 1 warning**

## 한계

- State machine은 active 만료를 호출하지 않는다. active는 기존 TTL 테스트와 HTTP soak가 담당한다.
- 참조 모델이 틀리면 테스트도 같이 틀린다.
- Property/state-machine도 넓게 보면 랜덤 탐색이다. 정확성 증명이 아니다.
- 영속화·자료구조는 아직 범위에 넣지 않았다.

## 관련

- [Mutation testing](mutation-testing.md)
- [랜덤 스트레스 테스트 계획](stress-test-plan.md)
- [TTL 테스트 계약](scenario_01_storage/01-ttl-test-cases.md)
