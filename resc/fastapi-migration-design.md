# Flask → FastAPI 마이그레이션 설계

## 1. 목적과 상태

- 목적: HTTP 호스팅 계층을 Flask/WSGI에서 FastAPI/ASGI로 전환한다. 배경은 active cleanup의 주기 실행을 서버 내부 훅이 아니라 앱 lifecycle에서 소유하기 위해서다.
- 이 문서는 마이그레이션의 **설계 계약**이다. 승인 전 코드 변경은 시험이다.
- 기준 코드: `git show HEAD:app.py`(Flask), `storage.py`, 기존 테스트, [REST API 명세](../rest-api.md), [HTTP 담당 범위](scenario_01_storage/01-http-scope.md).
- 원칙: **저장소 로직·도메인 계약·확정 HTTP 계약은 동일**하다. 프레임워크, 테스트 fixture, 실행·문서의 구현 서술만 필요한 만큼 바꾼다.
- 이번 마이그레이션은 TTL·active cleanup의 실제 동작을 구현하지 않는다. FastAPI lifespan에서 maintenance task를 소유할 자리만 마련하고, 실제 연결은 TTL 구현 단계에서 한다.

## 2. 기준 코드의 동작

기준은 Flask `app.py`다. 아래 동작은 마이그레이션 후에도 같아야 한다.

| 요청 | HTTP 검증 | 저장소 호출 | 응답 |
| --- | --- | --- | --- |
| `PUT /kv` | JSON 해석. 해석 불가 → 400 `INVALID_INPUT`. 본문이 객체가 아니거나 `key`·`value` 누락 → 400 | `set(key, value)` | `INVALID_INPUT` → 400, 그 외 → 200 |
| `GET /kv` | 쿼리 `key` 필수. 누락 → 400 | `get(key)` | `INVALID_INPUT` → 400, 그 외 → 200 |
| `DELETE /kv` | 쿼리 `key` 필수. 누락 → 400 | `delete(key)` | `INVALID_INPUT` → 400, 그 외 → 200 |
| `POST /kv/incr` | JSON 해석. 객체·`key`·`amount` 필수, 허용 필드는 `{key, amount, maximum}`. 조건이 있으면 `None`도 그대로 전달 | `incr(key, amount)` 또는 `incr(key, amount, maximum=...)` | `ERROR_STATUS.get(error, 200)` |
| `POST /kv/decr` | 위와 같고 허용 필드는 `{key, amount, minimum}` | `decr(key, amount)` 또는 `decr(key, amount, minimum=...)` | 같은 매핑 |
| 처리되지 않은 내부 예외 | — | — | 500 `{"error":"INTERNAL_SERVER_ERROR"}` |

- `create_app(storage)`는 전달받은 저장소 하나를 모든 경로가 공유한다. 저장소를 새로 만들지 않는다.
- `ERROR_STATUS`는 `INVALID_INPUT` 400, `TYPE_MISMATCH` 400, `NEGATIVE_AMOUNT` 400, `NOT_FOUND` 404, `OUT_OF_RANGE` 422, `CONDITION_NOT_MET` 409다.
- HTTP는 요청 형식·필수 필드만 검사하고, 키·`value` 타입·크기·연산 입력은 저장소가 검사한다. HTTP가 저장 상태를 미리 조회하지 않는다.
- 입력 거절은 상태를 바꾸지 않는다. 정상 응답은 `ok`·`found`·`value` 등 기존 JSON 필드를 유지한다.
- 실행 진입점은 `127.0.0.1:5000`, 단일 저장소, 단일 프로세스다. Flask에서는 `threaded=False`, `use_reloader=False`였다.

## 3. 변경 범위

### 바뀌는 것

- `app.py`: Flask 앱 → FastAPI 앱. `create_app(storage)` 이름과 인자는 유지한다. `uvicorn.run(..., host="127.0.0.1", port=5000, workers=1)`로 실행한다.
- JSON 본문 해석: Flask `request.get_json()` → `Request.json()` + 형식 오류 처리. **Pydantic 모델을 쓰지 않는다.** 자동 검증이 422를 만들거나 기존 400 계약을 바꾸지 않게 하기 위해서다.
- HTTP 테스트: Flask `test_client` → FastAPI `TestClient`/`httpx`. `make_server`/`threaded=False` fixture → uvicorn/ASGI 서버 또는 `TestClient`.
- `requirements-dev.txt`: Flask 제거, `fastapi`·`uvicorn`·`httpx` 추가.
- 실행·문서의 구현 서술: Flask, `threaded=False`, `request.get_json`, `request.args`, `test_client`, `make_server`를 FastAPI/uvicorn/TestClient 기준으로 바꾼다. **행동 계약 문장은 바꾸지 않는다.**
- FastAPI `lifespan`: maintenance task를 소유할 자리를 만든다. 실제 TTL active cleanup 연결은 후속이다.

### 바뀌지 않는 것

- `storage.py`의 `MiniRedis.set/get/delete/incr/decr` 동작, 오류 우선순위, 상태 보존.
- 키 1~256 UTF-8 바이트, `value` 타입·크기, JSON 타입 보존, 실제 `null`과 키 없음의 구분.
- `rest-api.md`의 확정 HTTP 계약: 경로, 상태 코드, JSON 필드, 오류 코드, HTTP/저장소 검증 분리.
- 저장소 단위 테스트와 02의 동시 증가 관찰 계약(최종 값, 증가분 보존, 조건부 실패).
- 04의 응답 재사용·만료·단일 키 무효화 계약.

### 명시적 비목표

- TTL active cleanup의 실제 구현과 active 테스트.
- Pydantic/OpenAPI 기반으로 요청·응답 계약을 재정의하는 것.
- 다중 워커·다중 프로세스·공유 저장소.
- Flask 기본 404/405/Content-Type 정책의 동등 구현. 이 정책은 `rest-api.md`에서 미정이다.
- 03 성능 비교의 재측정. 서버 스택이 바뀌므로 기존 Flask 수치를 FastAPI 수치로 대체하지 않는다.

## 4. FastAPI 매핑

| Flask 기준 | FastAPI 구현 |
| --- | --- |
| `request.get_json()` + `BadRequest` | `await request.json()` + `json.JSONDecodeError`/`UnicodeDecodeError` → 400 |
| `request.args["key"]` 존재 확인 | `request.query_params.get("key")`가 `None`이면 400 |
| `return result, status` | `JSONResponse(result, status_code=status)` |
| `@app.errorhandler(500)` | `@app.exception_handler(Exception)` → 500 JSON |
| `create_app(storage)` | 같은 이름·인자. 내부 프레임워크만 FastAPI |
| `app.run(threaded=False, use_reloader=False)` | `uvicorn.run(app, host, port, workers=1)` |
| Flask test client | `fastapi.testclient.TestClient` |
| `make_server(..., threaded=False)` | uvicorn `Server` in thread 또는 ASGI transport |

- `JSONResponse`는 기존처럼 `application/json`을 반환한다. 필드 순서는 계약이 아니다.
- `request.json()`은 Content-Type을 강제하지 않는다. 이 차이는 6절의 미정 정책에 포함한다.
- 500 테스트는 `TestClient(..., raise_server_exceptions=False)`로 응답을 관찰하고, 예외 전파를 확인하는 테스트는 기본 `TestClient`를 쓴다.

## 5. 필요한 변경

| 영역 | 변경 | 이유 |
| --- | --- | --- |
| `app.py` | FastAPI 앱, 수동 JSON 해석, 500 handler, uvicorn 진입점 | 프레임워크 전환 |
| `requirements-dev.txt` | Flask → fastapi·uvicorn·httpx | 의존성 |
| `test_http.py` | TestClient, 응답 접근자, 예외 전파 fixture | 클라이언트 교체 |
| `test_http_integration.py` | uvicorn 서버 fixture, script log 문자열, `uvicorn.run` monkeypatch | 실제 TCP/진입점 시험 |
| `test_json_types_extension.py`·`contract_helpers.py` | TestClient, `response.json()`/headers | 클라이언트 교체 |
| `test_counter_http.py`·`test_counter_http_boundary.py` | TestClient, 500용 `raise_server_exceptions=False` | 클라이언트 교체 |
| `test_counter_concurrent.py` | uvicorn/ASGI 서버, `threaded=False` 가정 제거 | 서버 모델 |
| `readme.md`, `rest-api.md`, `01-http-scope.md` | 실행·서버 설명 갱신 | 구현 서술 |
| TTL 문서 | `threaded=False` → 단일 이벤트 루프·workers=1 | 구현 서술 |
| `AGENTS.md` | Flask 선택/실행 결정 기록 갱신 | 결정 동기화 |

- 테스트 입력·기대 결과·저장소 호출 횟수는 바꾸지 않는다. 클라이언트·fixture·서버 구동만 바꾼다.
- 테스트를 통과시키기 위해 기대 결과를 완화하거나 `skip`·`xfail`로 돌리지 않는다.
- `incr`·`decr`·`get`·`delete`의 저장소 동작은 건드리지 않는다.

## 6. 동작 차이와 미정

| 항목 | 기준 | FastAPI | 처리 |
| --- | --- | --- | --- |
| 잘못된 JSON | 400 `INVALID_INPUT` | `Request.json()` 예외 → 400 | 동일하게 구현·검증 |
| 본문 구조·필수 필드 | 400 | 수동 검사 → 400 | 동일 |
| 저장소 `INVALID_INPUT` | 400 | `JSONResponse` 400 | 동일 |
| 500 | 500 JSON | exception handler | 동일. 테스트 client 옵션만 다름 |
| 404/405 | Flask 기본 HTML | FastAPI 기본 JSON `detail` | **미정 정책**. 기존 계약 아님. 필요하면 별도 handler |
| Content-Type 불일치 | Flask 정책에 따름 | `request.json()`은 강제하지 않음 | **미정 정책**. 기존 계약 아님 |
| 중복 쿼리 키 | Flask 해석 | Starlette 해석 | **미정 정책**. 기존 계약 아님 |
| 동시 요청 | `threaded=False` 순차 | 단일 이벤트 루프, async endpoint | 관찰 계약(유실 없음) 유지. `await request.json()` 지점에서 요청이 겹칠 수 있음 |
| 테스트 예외 전파 | `PROPAGATE_EXCEPTIONS` | `raise_server_exceptions` | 테스트 전용 차이 |

- 04의 동시성 범위는 계속 순차 요청만이다. FastAPI 전환을 이유로 stampede·single-flight를 추가하지 않는다.
- 02의 동시 요청 테스트는 클라이언트 요청 동시성과 최종 값 보존을 검증한다. 서버가 요청을 직렬화한다는 구현 설명은 "단일 이벤트 루프, 저장소 호출 원자성"으로 바꾸고, 테스트 데이터·기대는 유지한다.
- `await request.json()`에서 요청이 겹칠 수 있지만, 저장소 호출(`set`/`get`/`delete`/`incr`/`decr`)은 동기 함수로 한 번에 끝난다. 요청 간 저장소 호출이 겹쳐 실행되지는 않는다.

## 7. 테스트 포팅 계약

| 파일 | 바꾸는 것 | 유지하는 것 |
| --- | --- | --- |
| `test_http.py` | TestClient, `response.json()`, headers, 예외 fixture | Mock 호출, 상태 코드, JSON, 입력 경계 |
| `test_http_integration.py` | uvicorn fixture, log 문자열, `uvicorn.run` monkeypatch | 실제 TCP 요청, 저장소 snapshot, 모든 S1 입력 |
| `test_json_types_extension.py` | `connected_client`, `check`, 응답 접근자 | J1 값·경계·상태 보존 |
| `test_counter_http.py` | TestClient, 응답 접근자 | S2-01·04~08 요청·기대 |
| `test_counter_http_boundary.py` | TestClient, 500 fixture | 필드·타입·오류 우선순위 |
| `test_counter_concurrent.py` | uvicorn/ASGI 서버 fixture, `threaded=False` 설명 | S2-02·03·09~14 명령·최종 값 |
| `test_storage.py`·`test_storage_invalid_input.py` | 없음 | 전체 |
| `test_ttl_*.py`(예정) | FastAPI TestClient fixture | TTL 계약·입력·기대 |

- 테스트의 호출 대상·입력·검증 시점이 기존 계약과 다르면 작업을 멈추고 사용자에게 확인한다.
- 실제 TCP 통합 테스트는 유지한다. `TestClient`만으로 실제 TCP 검증을 대체하지 않는다.

## 8. 문서 영향

| 문서 | 유지할 계약 | 바꿀 구현 서술 |
| --- | --- | --- |
| `rest-api.md` | 경로·상태·JSON·검증 분리·오류 코드 | Flask 개발 서버, `threaded=False`, 7·8.6절 실행 설명 |
| `01-storage-scenario.md` | 저장소 행동·경계 | "웹 프레임워크 Flask" |
| `01-http-scope.md` | HTTP/저장소 책임, 경로, 상태, 테스트 입력 | Flask 선택, `request.get_json`/`args`, `threaded=False`, `test_client`, 실행 기록 |
| `02-concurrent-counter-*` | 동시 증가·최종 값·조건부 실패 | `threaded=False` 순차 서버 설명, 테스트 fixture |
| `02-top-down-functions.md` | A/B/C 책임 | Flask 코드 골격, `threaded=False` 제약 |
| `03-performance-*` | 비교 목적·정확성 기준 | Flask/Werkzeug 환경, 기존 수치의 기준 서버 |
| `04-response-cache-scenario.md` | 재사용·만료·무효화 | `threaded=False` 문장 |
| `04-weather-ttl-decision-log.md` | TTL/LDC 결정 | `threaded=False` 문장, D-18 구현 설명 |
| `01-ttl-contract.md` | TTL 동작 | `threaded=False` 문장 |
| `01-ttl-test-cases.md` | TTL 테스트 | Flask `test_client` fixture |
| `01-ttl-top-down-functions.md` | A/B/C 분업 | Flask/`threaded=False` 참조 |
| `readme.md` | 사용법 | 실행 명령·로그·서버 설명 |
| `AGENTS.md` | 프로젝트 결정 | Flask 선택·실행 결정 기록 |

- 과거 Flask 실행 기록은 역사로 남긴다. FastAPI 실행 기록을 새로 추가하며, 기존 기록을 FastAPI 결과로 바꾸지 않는다.
- 계약 문장을 바꿔야 하는 필요가 생기면 이 문서 승인과 별도 사용자 확인을 받는다.
- 03의 기존 성능 수치는 Flask 서버 기준이다. FastAPI 전환 후 같은 조건의 재측정은 별도 작업이다.

## 9. 승인·완료 조건

- 이 설계 문서를 사용자가 승인한다.
- 기존 테스트가 FastAPI에서 통과한다. 테스트 입력·기대 결과 변경은 클라이언트·fixture에 한정한다.
- 2절의 기준 동작과 4절의 매핑이 일치한다.
- 저장소 단위 테스트와 02 동시 요청 테스트가 통과한다.
- `rest-api.md`의 확정 계약이 바뀌지 않는다.
- 단일 워커(`workers=1`)와 단일 이벤트 루프 전제가 문서에 명시된다.
- TTL active cleanup은 아직 구현하지 않았음을 명시하고, lifespan 자리만 둔다.
- 04의 순차 요청 범위가 유지된다.

## 10. 리스크·후속

- FastAPI 기본 404/405/422와 Content-Type/중복 쿼리 정책은 기존 확정 계약이 아니다. 필요하면 별도 handler로 정의한다.
- `await request.json()`에서 요청이 겹칠 수 있다. 저장소 호출은 동기·원자적이지만, 다단계 캐시 미스 흐름은 04에서 다루지 않는다.
- uvicorn을 테스트 스레드에서 구동할 때 시작·종료 대기와 포트 회수가 필요하다.
- FastAPI/uvicorn 버전과 Python 3.14 지원을 `requirements-dev.txt`에 기록한다.
- TTL active cleanup 구현 시 `lifespan`에서 `_maybe_active_expire`를 호출하고, 테스트는 fake clock/task 제어를 사용한다.
- 03 성능 재측정은 FastAPI 전환 후 별도로 수행한다.

## 11. 현재 브랜치의 시험 변경

- 브랜치: `feature/fastapi-migration`.
- 시험 변경: `app.py` FastAPI 초안, `requirements-dev.txt` 갱신, `test_http.py`·`test_http_integration.py` 일부 포팅.
- 아직 전체 테스트를 실행하지 않았다. 이 문서 승인 전까지 시험 변경은 확정이 아니다.
- `.venv/`는 로컬 테스트 환경이며 저장소에 커밋하지 않는다.
