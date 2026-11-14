# Mini Redis REST API 명세

외부 프로그램이 키에 대응하는 JSON `value`를 저장·조회·삭제하고, 숫자를 증감하고, 키의 TTL(보관 기간)을 설정·조회·제거하기 위한 HTTP API다.

## 명세 적용 상태

- 1~7절은 저장·조회·삭제와 공통 사용 범위, 8절은 JSON 자료형과 증감의 상세 계약, 9절은 TTL 표면이다.
- JSON 자료형 저장과 `POST /kv/incr`·`POST /kv/decr`를 구현했다. 기존 숫자·불리언·배열·객체 거절 테스트는 승인된 타입 지원에 따라 같은 입력의 저장 성공 검증으로 전환했다.
- TTL 표면은 9절에 추가했다. 저장소 행동 계약은 [TTL 계약](resc/scenario_01_storage/01-ttl-contract.md)을 따른다. TTL은 아직 구현하지 않았다.
- 확장 설계의 행동 근거는 [시나리오 02 통합 결론](resc/scenario_02_concurrent_counter/02-concurrent-counter-integrated-review.md), 함수 흐름은 [탑다운 함수 초안](resc/scenario_02_concurrent_counter/02-top-down-functions.md)을 따른다.
- 확장 계약의 구체적인 검증 입력·순서와 미검증 항목은 [테스트 케이스](resc/scenario_02_concurrent_counter/02-test-cases.md)를 따른다.

## 1. 접속과 공통 규칙

- 기본 주소: 서버 운영자가 안내한 주소를 사용한다.
- 현재 로컬 실행 주소: `http://127.0.0.1:5000`.
- 현재 실행 경로는 로컬 개발용 uvicorn 서버(`workers=1`, 단일 이벤트 루프)다. 다른 기기에서 접속할 수 있는 공개 서버 주소는 제공하지 않는다.
- 통신은 HTTP, 요청·응답 데이터는 JSON을 사용한다. HTTPS는 현재 제공하지 않는다.
- 저장·증감 요청에는 `Content-Type: application/json`을 지정한다.
- 조회·삭제는 JSON 본문이 아니라 쿼리 매개변수 `key`를 사용한다.
- 쿼리의 키는 URL 인코딩하여 전달한다. 아래 요청 예시의 주소는 로컬 실행 기준이다.

| 기능 | 메서드 | 경로 | 입력 위치 | 정상 상태 코드 |
| --- | --- | --- | --- | --- |
| 저장·덮어쓰기(+선택 TTL) | PUT | `/kv` | JSON 본문 | 200 |
| 조회 | GET | `/kv` | `key` 쿼리 | 200 |
| 삭제 | DELETE | `/kv` | `key` 쿼리 | 200 |
| 증가 | POST | `/kv/incr` | JSON 본문 | 200 |
| 감소 | POST | `/kv/decr` | JSON 본문 | 200 |
| TTL 설정·변경·즉시 삭제 | POST | `/kv/expire` | JSON 본문 | 200 |
| 남은 TTL 조회 | GET | `/kv/ttl` | `key` 쿼리 | 200 |
| TTL 제거 | POST | `/kv/persist` | JSON 본문 | 200 |

## 2. 입력 제한

| 항목 | 허용 타입 | 크기 | 필수 여부 |
| --- | --- | --- | --- |
| `key` | 문자열 | UTF-8 기준 1~256바이트 | 모든 요청에서 필수 |
| `value` | 모든 JSON 자료형과 중첩 내용 | 8.2절 기준 0~65,536바이트 | PUT에서 필수 |
| `ttl` | 정수(밀리초) | `set`: 1 이상, `expire`: 정수(0·음수는 즉시 삭제) | `set`에서 선택, `expire`에서 필수 |

- 키의 빈 문자열 `""`·실제 `null`은 허용하지 않는다.
- 문자열·숫자·불리언·실제 `null`·배열·객체를 허용하고 자료형을 그대로 보관한다. 객체 필드 이름은 문자열이다.
- 키·문자열 value는 해석된 내용의 UTF-8 크기, 실제 null은 0바이트, 나머지는 8.2절의 JSON 표현 기준으로 계산한다. 요청 전체 길이나 실제 메모리 사용량이 아니며 상한을 포함한다.
- `ttl`은 정수 밀리초만 허용한다. 소수·문자열·불리언은 허용하지 않는다. `set`의 `ttl`은 생략하거나 `null`이면 TTL 없음이고, `expire`의 `ttl`은 필수이며 `null`은 허용하지 않는다.
- UTF-8로 표현할 수 없는 키·문자열 `value`는 거절한다.
- 문자열 `"10"`·`"null"`·`"{\"a\":1}"`은 각각 문자열로 보관한다. 숫자·실제 `null`·객체로 다시 해석하지 않는다.
- 빈 문자열 `value`, 실제 `null`인 `value`, 문자열 `"null"`, 키 없음은 서로 다르다.

## 3. 저장·덮어쓰기

### 요청

`PUT /kv`

```http
Content-Type: application/json
```

```json
{"key":"sample:one","value":"hello"}
```

`key`와 `value`는 필수이고 `ttl`은 선택이다. 같은 키가 이미 있으면 새 `value`로 덮어쓴다. 다른 키는 변경하지 않는다.

`ttl`은 1 이상의 정수 밀리초이며, 저장과 동시에 TTL을 건다.

```json
{"key":"weather:37.566:126.978","value":{"temperature":22},"ttl":600000}
```

`ttl`을 생략하거나 `null`이면 TTL 없이 저장하고, 기존 TTL이 있으면 제거한다. TTL을 유지하면서 값을 덮어쓰려면 `ttl`을 명시해야 한다. `ttl`이 0·음수·소수·문자열·불리언이면 400 `INVALID_INPUT`이다.

### 성공 응답

HTTP **200**, `Content-Type: application/json`:

```json
{"ok":true}
```

빈 문자열과 실제 `null`도 정상 저장할 수 있다.

```json
{"key":"sample:empty","value":""}
```

```json
{"key":"sample:null","value":null}
```

`value:null`은 키 삭제가 아니다. `value` 필드 자체의 누락은 오류다.

### 요청 예시

```bash
curl --request PUT 'http://127.0.0.1:5000/kv' \
  --header 'Content-Type: application/json' \
  --data '{"key":"sample:one","value":"hello"}'
```

## 4. 조회

### 요청

`GET /kv?key=sample%3Aone`

`key` 쿼리가 필수다. 조회는 보관 상태를 변경하지 않는다.

`GET /kv`는 TTL을 반환하지 않는다. 남은 TTL은 `GET /kv/ttl`로 조회한다. 만료된 키는 없는 키로 취급해 `{"found":false}`를 반환한다.

### 키가 있는 경우

HTTP **200**, `Content-Type: application/json`:

```json
{"found":true,"value":"hello"}
```

`value`는 저장한 타입·내용 그대로 반환한다. 빈 문자열이면 `""`, 실제 `null`이면 `null`이다.

### 키가 없는 경우

HTTP **200**, `Content-Type: application/json`:

```json
{"found":false}
```

키 없음은 404 오류가 아니다. `value` 필드는 생략한다. `{"found":true,"value":null}`은 키가 있고 실제 `null`이 저장된 상태다.

### 요청 예시

```bash
curl --get 'http://127.0.0.1:5000/kv' \
  --data-urlencode 'key=sample:one'
```

## 5. 삭제

### 요청

`DELETE /kv?key=sample%3Aone`

`key` 쿼리가 필수다. 한 요청은 한 키만 삭제한다. 삭제 후 같은 키의 조회 결과는 키 없음이다.

삭제는 TTL도 함께 제거한다. 만료된 키는 없는 키로 취급해 `{"found":false}`를 반환한다.

### 키가 있는 경우

HTTP **200**, `Content-Type: application/json`:

```json
{"found":true,"value":"hello"}
```

`value`는 삭제 직전 보관된 내용이다. 실제 `null`을 삭제하면 `{"found":true,"value":null}`이다.

### 키가 없거나 이미 삭제한 경우

HTTP **200**, `Content-Type: application/json`:

```json
{"found":false}
```

반복 삭제는 오류가 아니다. 다른 키는 변경하지 않는다.

### 요청 예시

```bash
curl --request DELETE --get 'http://127.0.0.1:5000/kv' \
  --data-urlencode 'key=sample:one'
```

## 6. 입력 오류

아래 입력 오류는 HTTP **400**, `Content-Type: application/json`으로 응답한다.

```json
{"error":"INVALID_INPUT"}
```

| 오류 | 예시 |
| --- | --- |
| 잘못된 JSON | PUT 본문이 `{"key":`처럼 불완전함 |
| PUT 본문이 JSON 객체가 아님 | `[]`, `"hello"`, `10`, `true`, `null` |
| 필수 필드 누락 | PUT에서 `key`·`value` 누락, GET·DELETE에서 쿼리 `key` 누락, `POST /kv/expire`에서 `ttl` 누락, `POST /kv/persist`에서 `key` 누락, `GET /kv/ttl`에서 쿼리 `key` 누락 |
| 키 타입·내용·크기 오류 | PUT의 `key:null`, 빈 키, 문자열 외 키, UTF-8로 표현 불가능한 키, 256바이트 초과 키 |
| `value` 타입·크기 오류 | 지원하지 않는 자료형·중첩 내용, UTF-8로 표현 불가능한 문자열, 자료형별 65,536바이트 초과 |
| TTL 타입·범위 오류 | `set`의 `ttl`이 0·음수·소수·문자열·불리언, `expire`의 `ttl`이 `null`·소수·문자열·불리언 |

- 입력 오류로 거절된 요청은 기존 `value`와 다른 키를 변경하거나 새 항목을 추가하지 않는다.
- 오류 응답에는 `ok`·`found`·`value` 필드를 넣지 않는다.
- GET·DELETE의 쿼리는 문자열이다. `?key=null`은 실제 `null`이 아니라 문자열 `"null"`이라는 키를 조회·삭제하는 요청이다.
- 예상하지 못한 내부 오류는 `INVALID_INPUT`으로 바꾸지 않고 실제 서버에서 500과 `{"error":"INTERNAL_SERVER_ERROR"}`로 연결한다. 아래 미정 정책이나 통신 실패까지 모두 JSON으로 응답한다고 가정하지 않는다.

## 7. 사용 범위와 미정 정책

- 데이터는 현재 서버 프로세스 메모리에만 보관한다. 서버 종료·재시작 후 보존하지 않으며 TTL 상태도 함께 사라진다.
- 현재 실행 서버는 단일 이벤트 루프·`uvicorn workers=1` 개발용이다. 이 실행 구조에서 동시 외부 요청의 정확성을 검증하며, 저장소 직접 다중 스레드 호출 안전성·운영 배포·성능 우위로 확대하지 않는다.
- 여러 키 일괄 삭제·장애 복구는 이번 API 범위가 아니다. TTL은 9절을 따른다.
- TTL active 만료는 저장소의 주기 타이머로 동작한다. 유휴 상태에서도 실행되며, API 응답의 만료 판정은 물리적 제거 여부와 무관하다.
- 미지원 `Content-Type`·메서드·경로, 중복 JSON 필드·쿼리, HTTP 통신 단계의 잘못된 UTF-8, 키 정규화와 자원 부족의 세부 정책은 아직 확정하지 않았다. 이를 증감 오류 매핑으로 일괄 변경하지 않는다.
- 응답 유실·연결 종료 시 요청이 적용됐는지를 이 명세만으로 보장하지 않는다.

관련 명세는 [저장소 시나리오](resc/scenario_01_storage/01-storage-scenario.md), [TTL 계약](resc/scenario_01_storage/01-ttl-contract.md), [HTTP 담당 범위](resc/scenario_01_storage/01-http-scope.md), 시나리오 02 통합 결론을 따른다. 과거 실행 기록의 숫자 거절과 현재의 JSON 타입 허용 계약을 구분한다.

## 8. JSON 자료형·증감 확장 계약

### 8.1. 경로와 입력

| 행동 | 요청 | 입력 | 성공 응답 |
| --- | --- | --- | --- |
| 저장·덮어쓰기(+선택 TTL) | `PUT /kv` | JSON 본문의 `key`, `value`, 선택 `ttl` | HTTP 200, `{"ok":true}` |
| 조회 | `GET /kv` | 쿼리 `key` | 기존 HTTP 200과 `found`·`value` |
| 삭제 | `DELETE /kv` | 쿼리 `key` | 기존 HTTP 200과 `found`·`value` |
| 증가 | `POST /kv/incr` | JSON 본문의 `key`, `amount`, 선택적 `maximum` | HTTP 200, `{"ok":true,"value":변경직후수치}` |
| 감소 | `POST /kv/decr` | JSON 본문의 `key`, `amount`, 선택적 `minimum` | HTTP 200, `{"ok":true,"value":변경직후수치}` |

- TTL 표면(`POST /kv/expire`, `GET /kv/ttl`, `POST /kv/persist`)은 9절을 따른다.
- 저장·증감에는 `Content-Type: application/json`을 지정한다. 증감의 `key`·`amount`는 필수이고 기본 증감량을 자동으로 넣지 않는다.
- 증감 본문은 JSON 객체다. 증가에는 `key`·`amount`·`maximum`만, 감소에는 `key`·`amount`·`minimum`만 허용한다. 반대 방향의 조건이나 알 수 없는 필드는 `INVALID_INPUT`으로 거절한다. 조건이 조용히 무시되는 것을 막기 위한 규칙이며 기존 PUT의 추가 필드 정책까지 변경하지 않는다.
- 증감량은 정수 또는 소수이며 0 이상이다. 불리언은 숫자가 아니다. 대상 `value`는 음수일 수 있다. 숫자처럼 보이는 문자열은 숫자로 재해석하지 않는다.
- 조건 생략은 허용하지만 명시적 `null`은 `TYPE_MISMATCH`다. 조건도 숫자여야 한다.
- 증가 결과는 `maximum` 이하, 감소 결과는 `minimum` 이상이어야 한다. 먼저 결과를 계산하고 저장 전에 조건을 확인한다. 조건을 생략하면 해당 상·하한 조건 없이 처리한다.
- 증감 전체는 단일 이벤트 루프 서버의 한 요청 처리 안에서 저장소 호출 한 번으로 끝낸다. 성공 응답은 해당 요청 직후의 결과이며 응답 수신 시점에도 최신이라는 보장은 아니다.

```http
POST /kv/incr
Content-Type: application/json

{"key":"counter:one","amount":2,"maximum":10}
```

현재 `value`가 1이면 HTTP 200과 `{"ok":true,"value":3}`을 반환한다.

```http
POST /kv/decr
Content-Type: application/json

{"key":"stock:one","amount":1,"minimum":0}
```

현재 `value`가 1이면 HTTP 200과 `{"ok":true,"value":0}`을 반환한다.

### 8.2. 저장 자료형과 크기

- `value`는 문자열·숫자·불리언·실제 `null`·배열·객체와 중첩 내용을 허용하고 저장·조회·삭제 시 JSON 타입을 유지한다. 객체 필드 이름은 문자열이다. `NaN`·무한대·순환 참조는 표준 JSON 자료형 지원에 포함하지 않는다.
- 키는 기존처럼 문자열 1~256 UTF-8 바이트다. 항목 하나의 `value` 상한은 65,536바이트이며 경계를 포함한다. 요청 전체 크기나 실제 메모리 사용량의 제한은 아니다.
- 최상위 문자열은 문자열 내용 자체의 UTF-8 바이트 수, 최상위 실제 `null`은 0바이트를 적용한다.
- 나머지 자료형은 `json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)` 결과의 UTF-8 바이트 수를 적용한다. 배열·객체의 내부 내용·필드 이름·괄호·구분 기호·필수 이스케이프를 포함한다. 크기 측정용 변환이며 문자열로 바꿔 저장하지 않는다.
- 조회·삭제의 키 없음은 기존 HTTP 200과 `{"found":false}`를 유지한다. 실제 `null` 저장과 키 없음을 구분한다.

### 8.3. 오류 코드·상태와 연결 이유

| 오류 코드 | HTTP 상태 | 발생 조건 | 이 코드·상태를 연결한 이유 |
| --- | --- | --- | --- |
| `INVALID_INPUT` | 400 | 잘못된 JSON, 객체가 아닌 본문, 필수 필드 누락, 잘못된 키, 증감의 미지원 필드·반대 조건, PUT의 지원하지 않는 `value`·크기 초과, TTL 타입·범위 위반 | 요청 형식이나 입력 계약 자체가 잘못됐음을 뜻한다. 기존 저장의 입력 거절 계약도 유지한다. |
| `TYPE_MISMATCH` | 400 | 증감 대상 `value`·`amount`·`maximum`·`minimum`이 숫자가 아님 | 숫자 연산에 필요한 타입과 실제 타입이 다르다. 사용자가 대상이나 입력 타입을 수정할 수 있도록 형식·입력 계열인 400으로 구분한다. |
| `NEGATIVE_AMOUNT` | 400 | 숫자인 증감량이 음수 | 타입은 맞지만 증감량의 입력 규칙인 0 이상을 위반했다. 명령의 방향을 뒤집는 입력을 명확히 거절한다. |
| `NOT_FOUND` | 404 | 유효한 증감 요청의 대상 키가 없음 | 요청은 유효하지만 변경할 대상이 없다. 조회·삭제의 정상적인 없음 결과와 구분하며 자동 생성하지 않는다. |
| `OUT_OF_RANGE` | 422 | 증감의 숫자 표현 범위 위반 또는 계산 결과의 64KiB 초과 | 요청 형식을 이해했지만 숫자나 결과를 허용 범위에서 다룰 수 없다. 사용자가 지정한 상·하한과 저장소의 표현 한계를 구분한다. 구체적 숫자 범위는 후속 검토 대상이다. |
| `CONDITION_NOT_MET` | 409 | 표현 가능한 계산 결과가 지정한 상한·하한을 위반 | 입력 자체보다는 현재 저장 상태에 해당 연산을 적용한 결과와 요청 조건이 충돌한다. 같은 요청도 현재 상태가 달라지면 성공할 수 있다. |
| `INTERNAL_SERVER_ERROR` | 500 | 예상하지 못한 서버 내부 오류 | 사용자 입력 오류로 숨기지 않고 서버 문제임을 알린다. 내부 예외 상세는 노출하지 않으며 상태 미변경을 보장하는 거절과 구분한다. |

- 위 상태 분류는 이 API의 계약이다. 같은 원인에 반드시 이 이름을 사용해야 한다는 HTTP 표준 규칙은 아니다.
- 입력 검증·조건 불충족 등 정상적인 거절은 저장 상태를 변경하지 않는다. 500이나 응답 유실만으로 실제 변경이 없었다고 판단하지 않는다.
- 저장 크기 초과는 기존 호환성을 위해 `INVALID_INPUT`·400, 증감 계산 결과의 크기 초과는 `OUT_OF_RANGE`·422로 구분한다.
- 표준 JSON 숫자로 표현할 수 없는 NaN·무한대인 증감량·조건과, 계산 자체에서 표현 오류가 발생하는 경우도 `OUT_OF_RANGE`로 거절한다. 이는 임의의 정수 범위를 도입하는 것이 아니다.

### 8.4. 오류 응답 형식

일반 오류는 `error`만 포함한다. `INVALID_INPUT`, `NEGATIVE_AMOUNT`, `NOT_FOUND`, `OUT_OF_RANGE`, `INTERNAL_SERVER_ERROR`가 이에 해당한다.

```json
{"error":"NEGATIVE_AMOUNT"}
```

타입 오류는 잘못된 부분과 실제 타입을 포함한다.

```json
{"error":"TYPE_MISMATCH","field":"amount","actual_type":"string"}
```

- `field`: `value`, `amount`, `maximum`, `minimum` 중 해당 부분.
- `actual_type`: `string`, `number`, `boolean`, `null`, `array`, `object` 중 해당 JSON 타입. 정상적인 JSON 경로에서 숫자는 타입 오류 대상이 아니지만 타입 표기 자체는 이 어휘를 따른다.

조건 불충족은 판단 시점의 현재 `value`를 포함한다.

```json
{"error":"CONDITION_NOT_MET","value":9}
```

예를 들어 현재 수치 9에 증가량 2, 상한 10이면 결과 11을 저장하지 않고 위 응답과 HTTP 409를 반환한다. 요청한 증감량·조건·계산 결과는 반복해 넣지 않는다. 이후 다른 요청에 의해 현재 상태는 바뀔 수 있다.

### 8.5. 오류 우선순위

먼저 발견한 오류 하나만 반환한다.

1. HTTP 요청 형식·필수 입력·허용 필드
2. 키 → 증감량 → 조건 순서의 타입·입력 제한
3. 대상 키의 존재 여부
4. 저장된 `value`의 숫자 타입
5. 계산 결과의 표현 가능 여부와 크기
6. 계산 결과의 상한·하한 충족 여부

- 없는 키와 음수 증감량이 함께 있으면 `NEGATIVE_AMOUNT`·400이다.
- 대상 `value`가 문자열이고 증감량도 문자열이면 먼저 `amount`의 `TYPE_MISMATCH`·400을 반환한다.
- 결과의 표현 범위와 조건을 모두 위반하면 `OUT_OF_RANGE`·422를 반환한다.
- `PUT /kv`와 `POST /kv/expire`는 HTTP 형식 → 키 → `value`(있는 경우) → TTL 타입·범위 순서로 검사한다. `set`은 기존 키 → `value` → TTL 순서를 유지한다.

### 8.6. 실행·통신의 보장 경계

- 기존 단일 프로세스·단일 이벤트 루프 서버(`uvicorn workers=1`)를 사용한다. 직접 명령 대기 큐를 만들거나 가득 찼을 때 별도 JSON 오류를 보장하지 않는다.
- 연결 종료 시 연산 취소·되돌리기를 보장하지 않는다. 응답을 받지 못하면 처리 여부를 알 수 없으며 이번 범위에서는 재시도하지 않는다. 중복 방지도 보장하지 않는다.
- 미지원 `Content-Type`·메서드·경로, 중복 필드·쿼리 등 7절에 남긴 정책을 이번 증감 오류 표로 임의 확정하지 않는다. 앱이 요청을 받지 못하는 통신 오류는 위 JSON 응답을 보장할 수 없다.
- 구체적인 추가 숫자 범위와 배열·객체를 저장소 직접 호출로 주고받을 때의 변경 전파 정책은 후속 계약으로 남긴다. 현재 표현 불가능한 입력·결과 거절과 별개다. 구현한 경로의 검증을 운영 배포나 모든 경합 상황의 증명으로 확대하지 않는다.
- TTL active 만료는 주기 타이머로 동작한다. API 응답은 논리적 만료를 기준으로 하며, 물리적 제거 시점은 응답에 포함하지 않는다.

## 9. TTL

TTL의 저장소 행동 계약은 [TTL 계약](resc/scenario_01_storage/01-ttl-contract.md)을 따른다. 이 절은 HTTP 표면만 정의한다.

### 9.1 저장할 때 TTL 설정

`PUT /kv`의 선택 필드 `ttl`로 저장과 동시에 TTL을 건다.

```json
{"key":"weather:37.566:126.978","value":{"temperature":22},"ttl":600000}
```

- `ttl`은 1 이상의 정수 밀리초다.
- 생략하거나 `null`이면 TTL 없이 저장한다. 기존 TTL이 있으면 제거한다.
- 0·음수·소수·문자열·불리언은 HTTP **400**, `{"error":"INVALID_INPUT"}`.
- 응답은 기존과 같은 HTTP **200**, `{"ok":true}`.

### 9.2 TTL 설정·변경·즉시 삭제

`POST /kv/expire`

```json
{"key":"sample:one","ttl":60000}
```

- `ttl`은 필수 정수 밀리초다.
- `ttl > 0`: 기존 `value`를 유지하고 TTL을 설정하거나 변경한다. HTTP **200**, `{"found":true,"ttl":ttl}`.
- `ttl <= 0`: 키와 `value`를 즉시 삭제한다. HTTP **200**, `{"found":true,"ttl":0}`.
- 키가 없거나 만료됐으면 HTTP **200**, `{"found":false}`.
- `ttl` 누락·`null`·소수·문자열·불리언은 HTTP **400**, `{"error":"INVALID_INPUT"}`.

### 9.3 남은 TTL 조회

`GET /kv/ttl?key=sample%3Aone`

- TTL이 있으면 HTTP **200**, `{"found":true,"ttl":남은밀리초}`.
- TTL이 없으면 HTTP **200**, `{"found":true,"ttl":-1}`.
- 키가 없거나 만료됐으면 HTTP **200**, `{"found":false}`.
- `GET /kv`는 TTL을 반환하지 않는다. TTL은 이 경로로만 조회한다.

### 9.4 TTL 제거

`POST /kv/persist`

```json
{"key":"sample:one"}
```

- TTL이 있으면 제거하고 HTTP **200**, `{"found":true,"persisted":true}`.
- TTL이 없으면 HTTP **200**, `{"found":true,"persisted":false}`.
- 키가 없거나 만료됐으면 HTTP **200**, `{"found":false}`.
- `value`는 변경하지 않는다.

### 9.5 만료와 물리적 제거

- 만료된 키는 `GET /kv`·`DELETE /kv`·`GET /kv/ttl`·`POST /kv/expire`·`POST /kv/persist`에서 없는 키로 취급한다.
- `PUT /kv`가 만료된 키를 덮어쓰면 이전 TTL을 물려주지 않고 새 키로 취급한다. `ttl`이 있으면 새로 걸고, 없으면 TTL 없이 저장한다.
- `GET /kv`의 `{"found":false}`는 키가 없거나 만료된 경우다. 두 경우의 필드 구성은 같다.
- 논리적 만료와 물리적 제거 시점은 같지 않다. active 만료는 100ms 주기 타이머로 동작하며, 유휴 상태에서도 실행된다.
- 서버 재시작 시 TTL 상태도 함께 사라진다.

### 9.6 미정

- active 만료의 표본 중복 여부, 실행 시점, 첫 사이클 초기 시각은 저장소 계약의 미정 항목이다. HTTP 표면에는 영향을 주지 않는다.
- `incr`·`decr`와 TTL의 관계는 저장소 계약의 미정 항목이다. 현재 이 명세는 증감이 TTL을 유지하는지 확정하지 않는다.
- `POST /kv/expire`·`POST /kv/persist`의 알 수 없는 추가 필드 허용·거절 정책은 아직 확정하지 않았다. 기존 `PUT /kv`는 추가 필드를 무시하지만, 새 표면에 같은 정책을 자동 적용하지 않는다.
