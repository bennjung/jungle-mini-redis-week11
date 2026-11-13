# JSON 자료형 확장·시나리오 02 테스트 케이스

## 기준과 상태

- 기준: [통합 결론](02-concurrent-counter-integrated-review.md), [함수 흐름](02-top-down-functions.md), [REST API 8절](../../rest-api.md).
- 이 문서는 합의한 행동을 정확한 입력과 관찰로 구체화한 테스트 명세 초안이다. 아래 구체적인 표본은 계약을 검증하기 위한 입력이며 새 기능·정책을 추가하지 않는다. 새 테스트의 정확한 표본을 사용자 검토 없이 동결된 계약이라고 주장하지 않는다.
- 구현 전 테스트 실패를 확인한 뒤 `app.py`·`storage.py`에 계약을 구현했다. 기존 S1-10-01~04는 승인된 JSON 확장에 따라 같은 입력의 저장 성공으로 전환했으며, bytes·UTF-8 인코딩 불가능 문자열의 거절은 유지한다. 구현에 맞추어 새 계약을 완화하지 않는다.
- #1 테스트: `tests/scenario_01_storage/test_json_types_extension.py`. 저장소는 기존 `MiniRedis.set/get/delete`, HTTP는 `create_app(MiniRedis()).test_client()`의 실제 저장소 연결 경로를 사용한다.
- 증감 연결 테스트: `tests/scenario_02_concurrent_counter/test_counter_http.py`. 승인된 외부 요청을 실제 저장소와 연결해 검증한다. 범위별 단위 테스트와 함께 유지한다.
- 범위별 독립 테스트: 자료형 저장은 J1-01~04의 `storage` 매개변수와 직접 입력 거절 테스트, HTTP 경계는 `test_counter_http_boundary.py`, 저장소 증감은 `test_counter_storage.py`를 사용한다. 독립 테스트의 입력·호출·판정은 아래 A·C 절을 따른다.
- 실제 TCP 동시 요청: `tests/scenario_02_concurrent_counter/test_counter_concurrent.py`. 단일 프로세스·`threaded=False` 서버를 사용한다. 반복 횟수와 완료 한도는 미정이므로 환경 변수 `COUNTER_TEST_REPEAT`·`COUNTER_TEST_TIMEOUT`을 명시한 실행만 수행하며 기본값을 임의로 정하지 않는다. 이는 테스트 실행 입력이지 서버 설정이나 확정된 수용 기준이 아니다. 미지정 시 건너뛰고 미검증으로 기록한다.
- HTTP 시험 클라이언트 테스트는 실제 TCP·외부 장치 검증을 대체하지 않는다. 모든 테스트는 새 저장소에서 실행하고 서버는 종료한다. 실패를 통과시키기 위한 `xfail`이나 자동 재시도는 사용하지 않는다.

## 공통 절차와 관찰

- 저장·증감의 사전 상태는 반드시 `set` 또는 `PUT /kv`로 만든다. 숫자를 `_data`에 직접 넣지 않는다.
- #1의 보조 키는 `sample:other`에 문자열 `"keep"`이다. 대상 키는 `sample:json`이다.
- 증감의 보조 키는 `counter:other`에 문자열 `"keep"`, 대상은 `counter:one`이다. 없는 대상은 `counter:missing`이다.
- 각 요청 완료 직후 HTTP 상태·JSON 본문 전체와 타입을 검사한다. 객체 필드 순서는 강제하지 않는다. 불리언과 숫자는 구분하고, JSON의 숫자 타입 안에서 정수·소수의 표기만으로 실패시키지는 않는다.
- 정상·거절 후 대상과 보조 키를 조회하고, 조회를 반복해 상태 유지도 확인한다. 저장소 연결 시험에서는 테스트 전용 `snapshot`의 깊은 사본으로 전체 상태를 비교해 거절 시 상태 보존을 확인한다. 실행 코드에 관찰용 함수를 추가하지 않는다.
- 실패하면 다음 단계로 진행해 통과시키지 않는다. 사전 상태 준비에서 실패한 경우와 증감 응답에서 실패한 경우를 실행 기록에서 구분한다.

## J1-01. JSON 자료형의 저장·조회·삭제

각 행은 저장소 직접 호출과 실제 저장소 연결 HTTP에서 각각 독립 실행한다.

| 하위 ID | 정확한 value |
| --- | --- |
| string | `"hello"` |
| empty | `""` |
| number-text | `"1"` |
| null-text | `"null"` |
| integer | `1` |
| negative | `-3` |
| real | `1.0` |
| fraction | `0.25` |
| true | `true` |
| false | `false` |
| null | 실제 `null` |
| array-empty | `[]` |
| object-empty | `{}` |
| array | `[1, "가", true, null]` |
| object | `{"a":1,"b":"가","c":false,"d":null}` |
| nested | `{"a":[1,{"b":[true,null,"가"]}]}` |

- 사전 상태: 보조 키만 저장되어 있고 대상 키는 없음.
- 행동: 대상에 행의 value 저장 → 두 번 조회 → 삭제 → 두 번 조회 → 반복 삭제.
- 기대: 저장 `{"ok":true}`, 조회·첫 삭제 `{"found":true,"value":행의 내용}`, 이후 조회·삭제 `{"found":false}`. HTTP 정상 상태는 모두 200.
- 유지: 자료형과 내용 및 보조 키 보존. 실제 `null`도 키 존재로 표현한다.

## J1-02. 다른 타입으로 덮어쓰기

- 독립된 저장소·HTTP 경로에서 대상에 `"1"` → `1` → `true` → `[1]` → `{"a":1}` → 실제 `null` 순서로 저장한다.
- 각 저장 직후 두 번 조회해 정확한 타입·내용을 확인한다. 매번 성공이며 보조 키는 유지한다.

## J1-03. 64KiB 경계와 초과

아래 Python 표현은 JSON value를 만드는 정확한 입력 정의다. `n`은 오른쪽 열의 수치다. 각 행은 저장소·HTTP에서 각각 실행한다.

| 하위 ID | 입력 정의 | 경계 n / 초과 n |
| --- | --- | --- |
| string | `"a" * n` | 65,536 / 65,537 |
| array | `["a" * n]` | 65,532 / 65,533 |
| object | `{"a": "a" * n}` | 65,528 / 65,529 |
| unicode-array | `["가" * n]` | 21,844 / 21,845 |
| escaped-array | `['"' * n]` | 32,766 / 32,767 |
| object-key | `{"a" * n: 0}` | 65,530 / 65,531 |

- 사전 상태: 대상은 `"before"`, 보조 키는 `"keep"`.
- 경계 입력: 저장 성공, 직후 조회가 입력과 동일. 상위 요청의 키·필드·공백 길이는 value 크기에 포함하지 않는다.
- 초과 입력: `INVALID_INPUT`·HTTP 400 또는 저장소의 같은 오류, 전체 상태는 요청 전 그대로. 이어 정상 `"after"` 저장이 성공하는지 확인한다.
- 별도 HTTP 사례: value `{"a":"가" * 21842}`는 정확히 65,534바이트다. ASCII 이스케이프와 들여쓰기를 사용한 요청 본문이 65,536바이트를 넘어도 해석된 value 기준으로 성공해야 한다.

## J1-04. 지원하지 않는 Python 내용 거절

- 저장소 직접 호출에만 적용한다. 대상 `"before"`와 보조 키를 준비한다.
- 각 입력: `float("nan")`, `float("inf")`, `float("-inf")`, `[float("nan")]`, `{"a":float("inf")}`, `(1,2)`, `{1:"a"}`, 자기 자신을 원소로 포함한 리스트.
- 기대: `{"error":"INVALID_INPUT"}`. 정상 응답이 아닌 거절이어야 하며 전체 상태는 보존한다.
- HTTP로 순환 참조나 튜플을 임의 표현해 보내지 않는다. 직렬화기가 문자열 키나 배열로 자동 변환하는 것을 저장소의 타입 지원으로 취급하지 않는다.

## S2-01. 단일 증감과 정상 경계

모든 행의 사전 상태는 PUT으로 `counter:one`에 초기 수치를 저장한 상태다. 표의 추가 필드가 없으면 조건을 실제로 생략한다. POST 본문은 `{"key":"counter:one","amount":증감량,...추가필드}`다.

| 하위 ID | 경로 | 초기 | 증감량 | 추가 필드 | 기대 value |
| --- | --- | --- | --- | --- | --- |
| incr | `/kv/incr` | 0 | 1 | 없음 | 1 |
| decr | `/kv/decr` | 10 | 1 | 없음 | 9 |
| existing | `/kv/incr` | 10 | 2 | 없음 | 12 |
| negative-incr | `/kv/incr` | -3 | 2 | 없음 | -1 |
| negative-decr | `/kv/decr` | -3 | 2 | 없음 | -5 |
| zero-incr | `/kv/incr` | 10 | 0 | 없음 | 10 |
| zero-decr | `/kv/decr` | 10 | 0 | 없음 | 10 |
| fraction-incr | `/kv/incr` | 0.25 | 0.5 | 없음 | 0.75 |
| fraction-decr | `/kv/decr` | 0.75 | 0.5 | 없음 | 0.25 |
| real-amount | `/kv/incr` | 1 | 1.0 | 없음 | 2 |
| upper-equal | `/kv/incr` | 9 | 1 | `maximum:10` | 10 |
| lower-equal | `/kv/decr` | 1 | 1 | `minimum:0` | 0 |
| upper-inside | `/kv/incr` | 1 | 2 | `maximum:10` | 3 |
| lower-inside | `/kv/decr` | 5 | 2 | `minimum:0` | 3 |
| no-lower | `/kv/decr` | 0 | 1 | 없음 | -1 |

- 기대: HTTP 200과 `{"ok":true,"value":기대수치}`. 직후 GET 두 번으로 같은 수치를 확인하고, 보조 키와 대상 외 상태를 보존한다.
- 표의 없음은 조건을 전송하지 않는 의미이며 실제 `null`을 보내지 않는다.

## S2-04. 조건 불충족과 소수 경계

S2-01과 같은 준비·입력 형태로 각 행을 독립 실행한다.

| 하위 ID | 경로 | 초기 | 증감량 | 조건 | 기대 |
| --- | --- | --- | --- | --- | --- |
| upper-over | `/kv/incr` | 9 | 2 | `maximum:10` | 409, 현재 value 9 |
| lower-under | `/kv/decr` | 1 | 2 | `minimum:0` | 409, 현재 value 1 |
| already-above | `/kv/incr` | 12 | 0 | `maximum:10` | 409, 현재 value 12 |
| already-below | `/kv/decr` | -2 | 0 | `minimum:0` | 409, 현재 value -2 |
| floating-bound | `/kv/incr` | 0.1 | 0.2 | `maximum:0.3` | 409, 현재 value 0.1 |
| floating-result | `/kv/incr` | 0.1 | 0.2 | 없음 | 200, value 0.30000000000000004 |

- 409 본문은 `{"error":"CONDITION_NOT_MET","value":초기수치}`다. 전체 상태를 보존하고 이후 조건 없이 증감량 0의 정상 요청을 같은 경로에 보내 200을 확인한다.

## S2-05. 숫자가 아닌 타입

- 타입별 입력: `string`은 `"1"`, `boolean`은 `true`, `null`은 실제 `null`, `array`는 `[]`, `object`는 `{}`. 대상 value는 추가로 `"01"`, `"+1"`, `"1.0"`, `" 1 "`, `""`, `"null"`, `false`도 검증한다.
- `value`: 각 타입의 value를 대상에 PUT하고 amount 1로 증가·감소를 각각 보낸다.
- `amount`: 대상 value 10을 준비하고 각 타입을 amount로 증가·감소를 각각 보낸다.
- 조건: 대상 value 10, amount 1에서 증가의 `maximum` 또는 감소의 `minimum`에 각 타입을 보낸다.
- 기대: 400과 `{"error":"TYPE_MISMATCH","field":해당필드,"actual_type":해당JSON타입}`. 전체 상태를 보존하고 대상과 보조 키를 반복 조회한다.

## S2-06. 없음·음수 증감량·오류 우선순위

증가·감소에서 각 행을 독립 실행한다. 기본 대상 value는 10이다.

| 하위 ID | 입력 차이 | 기대 |
| --- | --- | --- |
| missing | 대상 없는 `counter:missing`, amount 1 | 404, `{"error":"NOT_FOUND"}`, 키를 만들지 않음 |
| negative | amount -1, 조건 생략 | 400, `{"error":"NEGATIVE_AMOUNT"}` |
| negative-conditioned | amount -1, 증가의 maximum 20 또는 감소의 minimum 0 | 동일하게 음수 증감량 거절 |
| missing-negative | 대상 없음, amount -1 | `NEGATIVE_AMOUNT`가 키 없음에 우선 |
| value-and-amount | value `"1"`, amount `"1"` | amount의 `TYPE_MISMATCH` |
| key-and-amount | key `""`、amount `"1"` | `INVALID_INPUT` |
| amount-and-bound | amount -1, 해당 조건이 null | `NEGATIVE_AMOUNT` |
| bound-and-missing | 대상 없음, 해당 조건이 null, amount 1 | 조건의 `TYPE_MISMATCH` |

- 정상적인 거절은 전체 상태를 유지한다. 없는 대상은 GET으로 `{"found":false}`를 확인한다.

## S2-07. HTTP 형식·필수·허용 필드

증가·감소의 각 경로에 매번 새 저장소로 다음을 보낸다. 사전 상태는 대상 `"before"`, 보조 키 `"keep"`이다.

- 생본문 `{"key":`.
- JSON 본문: `[]`, `"hello"`, `10`, `true`, 실제 `null`.
- 필드 누락: `{}`, `{"key":"counter:one"}`, `{"amount":1}`.
- 미지원 필드: `{"key":"counter:one","amount":1,"extra":0}`.
- 반대 조건: 증가에 minimum 0, 감소에 maximum 20.
- key: 실제 null, 빈 문자열, 숫자 1, `"a" * 257`.
- 기대: 400과 `{"error":"INVALID_INPUT"}`. 전체 상태를 유지한다.

## S2-08. 내부 오류의 HTTP 경계

- 시험용 저장소의 `get(key)`가 `RuntimeError("private detail")`를 던지게 하고 `GET /kv?key=counter:one`을 보낸다. 실제 Flask 앱을 사용하고 예외의 외부 재전파를 끈다.
- 기대: HTTP 500과 `{"error":"INTERNAL_SERVER_ERROR"}`만 반환. 입력 오류로 바꾸지 않고 예외 상세를 반환하지 않는다.
- HTTP 오류 경계만 검증하며 500에서 저장 상태 불변을 주장하지 않는다.

## S2-02·03·09~14. 실제 TCP 동시 요청

환경 변수의 반복 횟수만큼 매번 새 저장소·순차 처리 서버로 실행한다. 요청을 재시도하지 않는다. 각 요청과 배치 전체의 완료 대기에 환경 변수로 지정한 초 단위 한도를 적용한다. 완료된 응답과 미완료를 모두 구분하고 실패·시간 초과를 제외해 통과시키지 않는다. 준비는 HTTP로 하고 선행 PUT 성공을 반드시 확인한다.

| ID | 초기 상태와 동시 입력 | 모든 응답 완료 후 기대 |
| --- | --- | --- |
| S2-02 | counter:one=0, amount 1의 INCR 100건 | 모두 200, 응답 value가 1~100 각 1회, 최종 100 |
| S2-03 | counter:one=10, amount 1의 INCR 100건 | 모두 200, 응답 value가 11~110 각 1회, 최종 110 |
| S2-09 | counter:one=0, INCR amount 1 50건과 amount -1 50건 | 입력별 성공 50건·NEGATIVE_AMOUNT 50건, 성공 value가 1~50 각 1회, 최종 50 |
| S2-10 | stock:one=1, amount 1·minimum 0의 DECR 2건 | 성공 value 0 1건, CONDITION_NOT_MET·value 0 1건, 최종 0 |
| S2-11 | counter:one=0, counter:two=10, 각 키에 amount 1의 INCR 50건 | 키별 성공 value가 1~50과 11~60, 최종 50과 60 |
| S2-12 | counter:one=10, INCR amount 1과 PUT value 20 | 증가 응답 11이면 최종 20, 증가 응답 21이면 최종 21. PUT은 성공 |
| S2-13 | counter:one=10, INCR amount 1과 DELETE | 증가 성공 11·삭제 value 11, 또는 삭제 value 10·증가 NOT_FOUND. 최종 없음 |
| S2-14 | counter:one=10, INCR amount 1과 GET | 증가 성공 11, GET은 10 또는 11, 최종 11 |

요청 전송 순서·응답 도착 순서를 실행 순서로 사용하지 않는다. 위 입력은 허용되는 전체 결과를 열거해 판정할 수 있다. 모든 실행 후 보조 키의 보존을 확인한다. 저장소 직접 다중 스레드 호출의 안전성이나 성능 우위는 검증하지 않는다.

## 범위 A. HTTP 단위 테스트 — A01~07

실제 `create_app(storage)`에 `set/get/delete/incr/decr` 호출만 기록하고 지정한 결과를 반환하는 시험용 저장소를 주입한다. 실제 저장·증감·상태 보존은 이 테스트의 검증 범위가 아니다. 각 사례는 새 앱·시험용 저장소로 실행하고 응답 직후 전체 호출 목록을 검사한다.

- **A01:** 증가·감소 각각 `key="counter:one"`, `amount=0.5`. 조건 생략·숫자 20·실제 null을 독립 입력한다. `maximum` 또는 `minimum`을 생략하면 키워드 인자 없이, 존재하면 내용 그대로 전달한다. 저장소 호출은 해당 연산 정확히 1회이며 사전 get·추가 set은 없어야 한다. 숫자·생략의 지정 반환은 `{"ok":true,"value":10.5}`·200, null의 지정 반환은 해당 조건의 `TYPE_MISMATCH`·400이다.
- **A02:** 각 연산에 amount `"1"`, 실제 null, true, `[]`, `{}`, -1, 0을 보낸다. 타입 검증은 저장소 책임이므로 변환 없이 연산 1회 호출한다. 시험용 반환은 모두 `{"error":"NEGATIVE_AMOUNT"}`로 지정해 HTTP 400 연결만 확인한다. 이 지정 결과가 각 입력의 실제 연산 판정이라는 뜻은 아니다.
- **A03:** key `"counter:one"`, amount 1을 각 연산으로 보내 저장소의 `INVALID_INPUT`·400, `TYPE_MISMATCH`·400(`field=value, actual_type=boolean`), `NEGATIVE_AMOUNT`·400, `NOT_FOUND`·404, `OUT_OF_RANGE`·422, `CONDITION_NOT_MET`·409(`value=9`)를 각각 그대로 연결하는지 확인한다. 호출은 연산 1회다.
- **A04:** S2-07의 잘못된 JSON·객체 아닌 본문·필수 누락·미지원 필드·반대 조건은 `INVALID_INPUT`·400이며 저장소 전체 호출 목록이 비어 있어야 한다. 키의 타입·크기 오류는 A05로 구분한다.
- **A05:** key 실제 null, `""`, 숫자 1, `"a"*257`과 amount 1을 각 연산에 보낸다. 키는 저장소가 검사하므로 그대로 연산 1회에 전달하고, 지정 반환 `INVALID_INPUT`을 400으로 연결한다.
- **A06:** 각 증감 호출에서 `RuntimeError("private detail")`를 던지게 한다. HTTP 500과 `{"error":"INTERNAL_SERVER_ERROR"}`만 반환하고 추가 호출·예외 상세 노출을 금지한다. 상태 불변은 주장하지 않는다.
- **A07:** value `1`, `-3`, `0.25`, true, 실제 null, `[1,"가"]`, `{"a":[true,null]}`을 PUT에 보내 set 1회와 성공 응답을 확인한다. 이어 시험용 저장소가 각 value를 반환하게 해 GET·DELETE 각각 해당 호출 1회와 타입·본문 보존을 확인한다.

## 범위 C. 저장소 증감 단위 테스트 — C01~07

실제 `MiniRedis`를 새로 만들고 `set`으로 보조 키·대상을 준비한다. `incr(key, amount, maximum=...)`·`decr(key, amount, minimum=...)`를 직접 호출한다. HTTP 상태가 아닌 저장소 결과 딕셔너리를 판정하며, 거절 후 전체 상태의 깊은 사본을 비교한다. 조건 생략은 키워드 인자를 전달하지 않고 실제 null은 `None`으로 명시한다.

- **C01:** S2-01의 모든 표본을 같은 초기 수치·증감량·조건으로 직접 호출한다. 기대 결과는 `{"ok":true,"value":표의수치}`이고, 두 번 조회와 전체 상태로 대상·보조 키를 확인한다.
- **C02:** S2-04의 거절 5개 표본을 직접 호출해 `CONDITION_NOT_MET`·초기 value와 상태 보존을 확인한다. 이어 조건 없이 증감량 0을 같은 연산에 보내 성공을 확인한다. 별도 0.1+0.2는 0.30000000000000004를 반환·보관해야 한다.
- **C03:** S2-05의 대상·증감량·조건 타입 표본을 같은 내용으로 직접 전달해 정확한 `field`·`actual_type`과 상태 보존을 확인한다.
- **C04:** value 10, amount -1을 각 연산에 조건 없이 또는 증가 maximum 20·감소 minimum 0으로 호출해 `NEGATIVE_AMOUNT`와 상태 보존을 확인한다.
- **C05:** 보조 키만 존재하는 상태에서 각 연산에 없는 대상·amount 1을 보내 `NOT_FOUND`, 같은 대상·amount -1로 `NEGATIVE_AMOUNT`, 없는 대상·amount 1·명시적 null 조건으로 해당 조건의 `TYPE_MISMATCH`를 확인한다. 전체 상태와 키 없음은 유지한다.
- **C06:** 키 실제 null·빈 문자열·숫자 1·true·`[]`·`{}`·`b"key"`·`"a"*257`·`"\ud800"` 각각에 amount `"1"`을 보내 키의 `INVALID_INPUT`이 우선하는지 확인한다. 별도 대상 value `"1"`에서 amount -1·조건 null이면 `NEGATIVE_AMOUNT`, amount `"1"`이면 amount의 `TYPE_MISMATCH`가 우선한다. 모두 전체 상태를 보존한다.
- **C07:** 현재 Python 연산에서 실제 표현 불가능한 결과를 확인한다. 대상 `10**309`에 amount 1.0을 증가·감소하면 정수·소수 혼합 계산의 표현 실패를 `OUT_OF_RANGE`로 거절한다. 대상 1e308에 amount 1e308·maximum 0 증가, 대상 -1e308에 amount 1e308·minimum 0 감소는 결과가 무한대이므로 조건 불충족보다 `OUT_OF_RANGE`가 우선한다. 직접 호출에서 amount 또는 지정 조건에 `float("nan")`, `float("inf")`, `float("-inf")`를 전달한 경우도 `OUT_OF_RANGE`이며 전체 상태를 보존한다. 이는 임의 정수 상한을 정하는 것이 아니라 표준 JSON 숫자로 표현할 수 없는 입력·결과 거절을 검증한다.

## S2-15. 숫자 표현 실패의 HTTP 연결

- C07의 계산 실패 4개 표본을 같은 초기 수치·증감량·조건으로 PUT·POST에 연결해 422와 `{"error":"OUT_OF_RANGE"}`, 전체 상태 보존·반복 조회를 확인한다.
- 대상 value 10, amount 1에서 증가의 maximum 또는 감소의 minimum에 JSON 원문 숫자 `1e400`을 전달한다. JSON 문법상 숫자여도 현재 Python float로 유한하게 표현할 수 없으므로 422와 같은 오류, 전체 상태 보존을 확인한다.

#1이 아직 구현되지 않으면 C의 숫자 사전 상태 준비가 실패할 수 있다. 실패를 우회해 내부 딕셔너리에 쓰지 않으며, 문서화한 입력 거절·없는 키 등 기존 저장 상태로 실행 가능한 사례와 구분한다.

## 보류·미검증

- 구체적인 유한 숫자 범위의 추가 상한은 미정이다. 임의 64비트 경계는 고정하지 않는다. 현재 표현 실패와 조건 불충족의 우선순위는 C07·S2-15로 검증한다.
- 깊은 중첩의 한계, 직접 호출에서 배열·객체의 변경 전파는 미정이다.
- 저장소 직접 호출 테스트와 HTTP 경계 호출 횟수 테스트를 작성했다. 조건 생략의 내부 구현 표현은 강제하지 않는다. 이 검증만으로 모든 실행 순서에서의 안전성을 입증하지 않는다.
- 기존 S1-10-01~04의 기대 변경은 승인된 JSON 타입 계약의 전환이다. 성능 측정, 만료·장애 복구, 재시도 중복 제거는 이번 구현·검증에 포함하지 않는다.
