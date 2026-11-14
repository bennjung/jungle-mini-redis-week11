# 01 TTL — A. HTTP 경계·소비자 동결 계약

## 기준과 상태

- 기준: [TTL 계약](01-ttl-contract.md), [TTL 테스트 계약](01-ttl-test-cases.md), [탑다운 분업](01-ttl-top-down-functions.md), [REST API 9절](../../rest-api.md).
- 2026-11-14 동결. 변경은 계약 수정과 사용자 확인을 거친다.
- 이 문서는 A의 작업 범위만 담는다. TTL 저장소 동작은 B, active tick은 C의 문서를 따른다.

## 담당 범위

| 항목 | 내용 |
| --- | --- |
| 수정 파일 | `app.py`, `tests/scenario_01_storage/test_ttl_http.py`, `tests/scenario_04_response_cache/test_weather_cache.py` |
| 담당 함수 | `put_kv`, `expire_kv`, `ttl_kv`, `persist_kv` |
| 테스트 | T1-20, T1-21 |

## 공통 계약

- FastAPI async handler와 `JSONResponse`를 쓴다. Pydantic 자동 검증을 쓰지 않는다. 기존 `400 {"error":"INVALID_INPUT"}` 계약을 유지한다.
- HTTP는 JSON 해석·본문 구조·필수 필드 존재만 검사한다. 키·`value` 타입과 크기, TTL 타입·범위는 저장소가 판정한다.
- 저장소 호출은 요청당 정확히 한 번이다. 저장 상태를 미리 조회하거나 TTL을 계산하지 않는다.

## 엔드포인트

### `PUT /kv`

- 본문은 JSON 객체이고 `key`·`value`가 필수다. `ttl`은 선택이다.
- `ttl`이 없거나 `null`이면 `None`으로 전달한다. 저장소는 기존 TTL을 제거한다(D-04, D-12).
- `storage.set(key, value, ttl=ttl)`을 한 번 호출한다.
- 결과가 `{"error": "INVALID_INPUT"}`이면 400, 그 밖의 정상 결과는 200이다.
- 계약 필드 외 추가 필드는 무시한다.

### `POST /kv/expire`

- 본문은 JSON 객체이고 `key`·`ttl`이 필수다. `ttl`이 없으면 저장소를 호출하지 않고 400 `INVALID_INPUT`을 반환한다.
- `ttl: null`은 그대로 저장소에 전달하고, 저장소가 `INVALID_INPUT`으로 거절한다.
- `storage.expire(key, ttl)`을 한 번 호출한다. `INVALID_INPUT` → 400, 정상 → 200.
- 계약 필드 외 추가 필드는 무시한다(D-26).

### `GET /kv/ttl`

- 쿼리 `key`가 필수다. 없으면 400 `INVALID_INPUT`이다.
- `storage.ttl(key)`를 한 번 호출한다. `INVALID_INPUT` → 400, 정상 → 200.

### `POST /kv/persist`

- 본문은 JSON 객체이고 `key`가 필수다. 없으면 400 `INVALID_INPUT`이다.
- `storage.persist(key)`를 한 번 호출한다. `INVALID_INPUT` → 400, 정상 → 200.
- 계약 필드 외 추가 필드는 무시한다(D-26).

- 정상 응답의 JSON 필드와 상태 코드는 [REST API 9절](../../rest-api.md)과 [테스트 계약](01-ttl-test-cases.md)의 T1-20을 따른다.
- 키 없음·만료는 404가 아니라 200과 `{"found": false}`다.

## 소비자 테스트 (T1-21)

- `/weather` 엔드포인트와 별도 날씨 앱 프로세스를 만들지 않는다(D-15).
- 테스트 하네스가 KV HTTP API를 직접 호출하고, 원본은 호출 횟수를 세는 가짜 함수로 대체한다.
- 최초 요청 → 원본 1회·응답 저장. TTL 안 재요청 → 원본 1회 유지. 만료 후 → 원본 2회. 수동 `DELETE` 후 → 원본 3회.
- 다른 키의 캐시는 영향을 받지 않는다.
- 원본 API 실패·MiniRedis 실패 처리는 이 계약의 범위가 아니다(D-27). 실패 주입 테스트를 만들지 않는다.
- 테스트는 `MiniRedis(clock=...)`로 가짜 시계를 주입한다.

## 금지

- `storage.py` 수정.
- TTL 타입·범위 판정, 저장 상태 사전 조회, TTL 계산.
- `/weather` 엔드포인트·별도 앱 프로세스 추가.
- 테스트를 통과시키기 위해 기대 결과를 완화하거나 `skip`·`xfail`로 돌리는 것.

## 선행과 완료

- 선행: B의 public 시그니처(`set`, `expire`, `ttl`, `persist`), C의 `FakeClock`.
- 완료: T1-20·21 통과. 기존 `test_http.py`·`test_counter_http*.py`의 회귀 통과. 저장소 호출 횟수 계약 유지.
