# Mini Redis REST API 명세

외부 프로그램이 키에 대응하는 `value`를 저장·조회·삭제하기 위한 HTTP API다.

## 1. 접속과 공통 규칙

- 기본 주소: 서버 운영자가 안내한 주소를 사용한다.
- 현재 로컬 실행 주소: `http://127.0.0.1:5000`.
- 현재 실행 경로는 로컬 개발 서버다. 다른 기기에서 접속할 수 있는 공개 서버 주소는 제공하지 않는다.
- 통신은 HTTP, 요청·응답 데이터는 JSON을 사용한다. HTTPS는 현재 제공하지 않는다.
- 저장 요청에는 `Content-Type: application/json`을 지정한다.
- 조회·삭제는 JSON 본문이 아니라 쿼리 매개변수 `key`를 사용한다.
- 쿼리의 키는 URL 인코딩하여 전달한다. 아래 요청 예시의 주소는 로컬 실행 기준이다.

| 기능 | 메서드 | 경로 | 입력 위치 | 정상 상태 코드 |
| --- | --- | --- | --- | --- |
| 저장·덮어쓰기 | PUT | `/kv` | JSON 본문 | 200 |
| 조회 | GET | `/kv` | `key` 쿼리 | 200 |
| 삭제 | DELETE | `/kv` | `key` 쿼리 | 200 |

## 2. 입력 제한

| 항목 | 허용 타입 | 크기 | 필수 여부 |
| --- | --- | --- | --- |
| `key` | 문자열 | UTF-8 기준 1~256바이트 | 모든 요청에서 필수 |
| `value` | 문자열 또는 실제 `null` | 문자열은 UTF-8 기준 0~65,536바이트 | PUT에서 필수 |

- 키의 빈 문자열 `""`·실제 `null`은 허용하지 않는다.
- 문자열·실제 `null` 이외의 `value`는 허용하지 않는다. 숫자·불리언·배열·객체를 자동 변환하지 않는다.
- 크기는 JSON·URL을 해석한 뒤 얻은 문자열의 UTF-8 바이트 수다. JSON 본문 전체 길이·이스케이프 표기 길이·글자 수가 아니다. 상한을 포함한다.
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

두 필드 모두 필수다. 같은 키가 이미 있으면 새 `value`로 덮어쓴다. 다른 키는 변경하지 않는다.

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
| 필수 필드 누락 | PUT에서 `key`·`value` 누락, GET·DELETE에서 쿼리 `key` 누락 |
| 키 타입·내용·크기 오류 | PUT의 `key:null`, 빈 키, 문자열 외 키, UTF-8로 표현 불가능한 키, 256바이트 초과 키 |
| `value` 타입·크기 오류 | 숫자·불리언·배열·객체, UTF-8로 표현 불가능한 문자열, 65,536바이트 초과 문자열 |

- 입력 오류로 거절된 요청은 기존 `value`와 다른 키를 변경하거나 새 항목을 추가하지 않는다.
- 오류 응답에는 `ok`·`found`·`value` 필드를 넣지 않는다.
- GET·DELETE의 쿼리는 문자열이다. `?key=null`은 실제 `null`이 아니라 문자열 `"null"`이라는 키를 조회·삭제하는 요청이다.
- 예상하지 못한 내부 오류는 `INVALID_INPUT`으로 보장하지 않는다. 아래 미정 정책을 포함한 모든 실패가 위 JSON으로 반환된다고 가정하면 안 된다.

## 7. 사용 범위와 미정 정책

- 데이터는 현재 서버 프로세스 메모리에만 보관한다. 서버 종료·재시작 후 보존하지 않는다.
- 현재 실행 서버는 순차 처리 개발용이다. 동시 호출 안전성·운영 배포·성능 우위를 보장하지 않는다.
- 시간 만료·여러 키 일괄 삭제·장애 복구는 이번 API 범위가 아니다.
- 미지원 `Content-Type`·메서드·경로, 중복 JSON 필드·쿼리, HTTP 통신 단계의 잘못된 UTF-8, 키 정규화, 자원 부족 및 예상하지 못한 내부 오류의 세부 응답 정책은 아직 확정하지 않았다.
- 응답 유실·연결 종료 시 요청이 적용됐는지를 이 명세만으로 보장하지 않는다.

이 문서는 [저장소 시나리오](01-storage-scenario.md)와 [HTTP 담당 범위](01-http-scope.md)의 확정 계약을 외부 사용 관점에서 정리한 문서다. 새로운 입력·오류 정책을 추가하지 않는다.
