# 날씨 데모 실행 모니터 계약

## 1. 상태와 범위

2026-11-18 사용자 승인으로 날씨 데모에 별도 브라우저 탭 실행 모니터를 추가했다.

- 유지: 기존 날씨 화면, 두 프로세스 구성, `GET`·`LOCK ACQUIRE`·`PUT` 터미널 로그와 색상, 락 TTL(`10_000`ms), 응답 캐시 TTL(`60_000`ms), 기존 동결 테스트(WDL-01~11, 날씨 데모·표현 테스트).
- 추가: `demo/weather_app.py`의 실제 파이프라인 이벤트를 단일 원천으로 기록하고 `GET /monitor`·`GET /monitor/events`로 제공.
- 제외: Mini Redis 서버 내부 처리 계측, 이벤트 영속화, 끊긴 구간 복구, 다중 워커 간 이벤트 공유, 모니터에서의 날씨 조회 조작.

## 2. 이벤트 원천과 터미널 로그

- `weather()` 처리 중 실제로 발생한 지점에서 `emit()`을 한 번 호출한다. 같은 이벤트가 구조화 레코드와 기존 터미널 줄을 함께 만든다.
- 기존 터미널 문자열·ANSI 색상·단계 번호(①~⑤)·요청 ID 표기는 바꾸지 않는다. `test_weather_demo_presentation.py`와 WDL 로그 검증이 그대로 통과해야 한다.
- 이벤트 종류: `REQUEST`, `GET`, `LOCK ACQUIRE`, `OPEN-METEO`, `PUT`, `LOCK RELEASE`, `ERROR`, `END`.
- 각 이벤트는 `seq`, `request_id`, `kind`, `step`, `message`, `target`, `elapsed_ms`, `status`, `calls` 필드를 가진다. `calls`는 `OPEN-METEO`의 프로세스 누적 원본 호출 횟수이고 나머지는 `null`이다.
- 캐시 조회는 실제 호출마다 기록한다. 락 대기 중 100ms 간격의 반복 GET과 락 획득 후 double-check GET도 각각 한 줄이다.
- 요청 ID는 기존과 같이 락 토큰 앞 8자다. 좌표 검증(HTTP 422)처럼 요청 ID가 생기기 전에 끝나는 경로는 모니터 이벤트에 포함하지 않는다.

## 3. 전달과 연결 경계

- `GET /monitor/events`는 `text/event-stream` SSE로 즉시 전송한다. 첫 블록은 `event: ready`와 현재 순번이다.
- 구독 등록 이후 발생한 이벤트만 각 구독 큐로 전달한다. 과거 이벤트 이력은 보관하지 않으며 `Last-Event-ID` 기반 재생도 하지 않는다.
- 브라우저 새로고침은 DOM을 새로 만들므로 이전 목록이 사라진다. 연결이 끊긴 동안 발생한 이벤트는 재연결 후 복구하지 않는다.
- 이벤트는 앱 인스턴스 메모리에만 있다. `demo/run.py`의 단일 워커 구성을 전제로 하며, 다중 워커에서는 요청을 처리한 워커의 이벤트만 보일 수 있다.

## 4. 화면 계약

- `GET /monitor`는 별도 탭에서 직접 연다. 기존 `/` 화면에는 링크를 추가하지 않는다.
- 큰 글씨, 자동 아래 이동, 요청 ID, 연결 상태를 표시한다.
- 이벤트는 실제 도착 순서대로 append한다. 도착 전 예상 단계를 미리 그리지 않는다.
- `ERROR` 이벤트와 `END`의 200/502 상태를 표시한다. 연결 오류는 별도 연결 상태로 표시한다.
- 이벤트가 없을 때는 빈 상태 안내만 보여 준다.

## 5. 동결한 테스트 케이스

| ID | 대상 | 검증 시점 | 기대 결과 및 유지 조건 |
| :--- | :--- | :--- | :--- |
| **MD-01** | 모니터 화면 | `GET /monitor` 응답 후 | HTML 200, 실행 모니터·연결 상태·요청 ID·SSE 경로·`aria-live`·자동 이동 안내가 있다 |
| **MD-02** | 단일 요청 이벤트 | SSE 연결 후 `/weather` 1회 | `ready` 다음에 `REQUEST`부터 `END`까지 실제 순서로 오고, GET 반복·락·원본·저장·해제가 포함된다. 요청 ID는 하나이고 최종 상태는 200이다. 기존 터미널 단계 로그도 유지된다 |
| **MD-03** | 연결 이후만 수신 | 연결 전 `/weather` 1회, 연결 후 1회 | 연결 후 이벤트에 이전 요청 ID가 없고, 새 요청 ID만 보인다 |
| **MD-04** | 락 대기 중 반복 GET | 두 요청을 겹치고 대기 요청이 409를 받은 뒤 소유 요청 완료 | 대기 요청 ID의 GET이 2회 이상이고 `대기 후` 재사용이 보이며, 두 요청 모두 200이고 락이 남지 않는다 |
| **MD-05** | 오류 표시 | 원본 503으로 `/weather` 1회 | `ERROR` 이벤트에 `실패`가 있고 `END` 상태는 502다 |

## 6. 검증 경계

- 자동 테스트는 가짜 원본과 로컬 Mini Redis HTTP API를 사용한다. 실제 Open-Meteo 네트워크, 실제 브라우저의 EventSource 재연결, 다중 워커·다중 프로세스는 검증하지 않는다.
- SSE 연결 종료·재연결의 브라우저별 타이밍과 프록시 버퍼링은 검증하지 않는다.
- 이벤트 큐는 구독자별 메모리 큐다. 모니터 탭이 오랫동안 이벤트를 소비하지 못하는 상황의 백프레셔는 다루지 않는다.

## 7. 검증 기록

2026-11-18 구현 후 확인한 범위다.

- `tests/scenario_04_response_cache/test_weather_demo_monitor.py`: 5 passed.
- `tests/scenario_04_response_cache`: 24 passed, 2 skipped.
- 전체 pytest: 363 passed, 30 skipped, 1 deselected. `test_http_integration.py::test_script_server_responds_over_http`는 당시 사용자가 실행 중이던 Mini Redis 서버가 5000 포트를 점유해 시작하지 못했으므로 선택 해제했다. 이 테스트의 계약이나 코드는 변경하지 않았다.
- 실제 Open-Meteo 네트워크, 실제 브라우저의 EventSource 재연결, 다중 워커는 실행하지 않았다.

## 8. 관련 문서

- [날씨 데모 안내](../../demo/README.md)
- [락 사용 계약](../scenario_05_cache_lock/05-weather-demo-lock-usage.md)
- [04 응답 캐시 시나리오](04-response-cache-scenario.md)
