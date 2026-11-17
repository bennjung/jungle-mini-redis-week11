# Mini Redis 랜덤 스트레스 테스트 계획

## 상태와 범위

- 작성일: 2026-11-17
- 목적: 현재 구현된 KV·JSON, TTL, counter, HTTP 경로를 대상으로 정확성·soak 문제를 찾는다.
- 성능 비교가 아니다. 처리량·지연은 관찰 지표이며, 03 JMeter 결과를 대체하지 않는다.
- 영속화와 자료구조별 연산은 이번 범위에 넣지 않는다. 구현 후 별도로 확장한다.
- 기본 pytest에서는 실행하지 않는다. `STRESS=1` 또는 `tools/stress_http.py`로 수동 실행한다.

## 테스트 층

| 층 | 실행 | 검증 |
| --- | --- | --- |
| 저장소 fuzz | `tests/stress/test_storage_fuzz.py` | 참조 모델, TTL 구조 불변 조건, 입력 거절 시 상태 보존 |
| HTTP soak | `tools/stress_http.py` | 실제 API 상태 코드·응답 형식, 5xx/타임아웃, oracle 키, RSS·지연 |

동시성은 HTTP soak의 클라이언트 동시성으로 포함한다. 별도 동시성 테스트는 기존 시나리오 02를 따른다.

## 판정 기준

- 저장소 fuzz: 매 연산을 참조 모델과 비교하고, 매 연산 후 `assert_ttl_invariants`를 검사한다.
- HTTP soak: 계약에 맞는 상태 코드·JSON 형식을 확인한다. 5xx·타임아웃·연결 실패·JSON 파싱 실패는 실패다.
- `incr`/`decr`의 400·404·409·422는 입력·상태에 따라 정상일 수 있으므로 예상된 4xx로 집계한다.
- HTTP에서 TTL 내부 구조를 직접 검증하지 않는다. 정확한 TTL 구조 검증은 저장소 fuzz가 담당한다.
- 별도 디버그 상태 엔드포인트는 두지 않는다.

## 입력과 분포

- 전체 요청의 90%는 정상, 10%는 잘못된 입력이다.
- 저장소 fuzz는 hot 100개와 cold 10,000개를 80/20으로 쓴다.
- HTTP soak는 hot 키를 세 풀로 나눈다.
  - `hot:counter:*` — `incr`/`decr` 전용. 100개를 숫자 0으로 seed한다.
  - `hot:cache:*` — `set`/`get`/`ttl`/`persist` 중심. 100개를 `"cached"`로 seed한다.
  - `hot:plain:*` — `delete`/`expire` 중심. 100개를 `"plain"`로 seed한다.
  - cold `chaos:*` 키는 10,000개 공간에서 고른다.
- TTL 쓰기는 정상 연산의 약 25%이며, 1~10초 80%·30~60초 20%를 쓴다.
- 정상 연산 비중: `get` 25, `ttl` 10, `set` 25, `delete` 10, `expire` 10, `persist` 5, `incr` 10, `decr` 5.
- 경계값과 형식 오류는 고정 코퍼스로 낮은 확률에 주입한다.
- `oracle:*` 키는 최종값 검증에 쓴다.

## 실행

```bash
# 저장소 fuzz (기본 pytest에서는 skip)
STRESS=1 STRESS_OPS=200000 STRESS_SEED=42 \
  .venv/bin/python -m pytest tests/stress/test_storage_fuzz.py -q

# HTTP 스모크
.venv/bin/python tools/stress_http.py \
  --duration 2m --concurrency 10 --rate 50 --keyspace 10000 --seed 42

# HTTP 본 실행
.venv/bin/python tools/stress_http.py \
  --duration 30m --concurrency 20 --rate 100 --keyspace 10000 --seed 42
```

- 서버는 `tools/stress_server.py`가 빈 포트에서 별도 프로세스로 실행한다.
- 부하 생성기는 `tools/stress_http.py` 프로세스에서 실행한다.
- 결과는 기본적으로 `stress-results/<timestamp>-seed<seed>/`에 저장하며 Git에 커밋하지 않는다.
- `summary.json`·`summary.md`·`requests.jsonl`·`server.log`를 남긴다.
- 아직 구현하지 않은 TTL burst(active 정리 포화)와 영속화 재시작은 후속 범위다.

## 산출물

`summary.json`에는 실행 시각, seed, 설정, 실제 RPS, 성공/예상 4xx/예상 밖 오류, 5xx·타임아웃·연결 실패, p50/p95/p99, RSS 시작·종료·최대, oracle 결과, 서버 생존 여부, pass/fail을 기록한다.

`requests.jsonl`에는 요청별 op·invalid 여부·status·latency·outcome·detail을 기록한다.

## 현재 한계

- 저장소 fuzz는 active 만료를 직접 호출하지 않는다. active 만료는 기존 TTL 테스트와 HTTP soak의 TTL 쓰기로 관찰한다.
- HTTP soak의 TTL 쓰기만으로 active 정리 처리량을 포화시키지 않는다. 별도 TTL burst가 필요하다.
- 랜덤 테스트는 정확성 증명이 아니라 탐색이다. 실패는 seed와 요청 로그로 재현한다.
- 영속화·자료구조·다중 프로세스 안전성은 이 계획의 범위가 아니다.
