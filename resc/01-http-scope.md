# 문서 01의 HTTP 담당 범위

## 근거와 상태

- 원본: [01-storage-scenario.md](01-storage-scenario.md). 행동 계약과 테스트의 사전 상태·입력·순서·검증 시점·기대 결과·유지 조건은 원본을 따른다.
- 담당: 찬. 범위는 문서 01 전체 중 HTTP 요청 해석·JSON 및 본문 구조·필수 필드 검사·저장소 호출·HTTP 응답 생성이다. S1-01 하나로 한정하지 않는다.
- 구현 언어는 Python, 웹 프레임워크는 Flask로 선정했다. Flask로 현재 계약을 충족할 수 있고 팀원이 사용 경험을 갖고 있어, 현재 요구를 위해 다른 프레임워크를 도입할 이유가 없다.
- 아래는 기존 테스트에서 HTTP 담당 검증을 분리한 문서다. 새 행동 계약이나 저장소 내부 설계를 추가하지 않는다.
- 테스트 도구는 pytest다. HTTP 앱은 `app.py`의 `create_app(storage)`로 저장소를 전달받으며 테스트는 [tests/test_http.py](../tests/test_http.py)에 작성했다. 원격 병합으로 실제 저장소는 `storage.py`의 `MiniRedis()`로 확인되었다. 서버 실행 진입점은 아직 없다.
- 실행 상태: 원격 병합 후 HTTP 경계 테스트 41개와 저장소 단위 테스트 54개, 총 95개가 모두 통과했다. 양쪽 구현과 테스트의 입력·기대 결과는 바꾸지 않았다. 실제 저장소를 연결한 HTTP 통합·외부 네트워크 기능 테스트는 미실행이다.

## HTTP가 지켜야 할 기존 계약

| 요청 | 해석·호출 | 응답 |
| --- | --- | --- |
| `PUT /kv` | JSON 본문의 `key`·`value`를 `set(key, value)`에 전달 | `{"ok": True}`는 HTTP 200과 `{"ok":true}` |
| `GET /kv?key=...` | URL 해석 후 키를 `get(key)`에 전달 | 정상 반환을 HTTP 200과 JSON으로 전달 |
| `DELETE /kv?key=...` | URL 해석 후 키를 `delete(key)`에 전달 | 정상 반환을 HTTP 200과 JSON으로 전달 |
| JSON·본문 구조·필수 필드 오류 | HTTP가 검사하고 저장소를 호출하지 않음 | HTTP 400과 `{"error":"INVALID_INPUT"}` |
| 위 호출의 입력 거절 반환 | `{"error": "INVALID_INPUT"}` 수신 | HTTP 400과 `{"error":"INVALID_INPUT"}` |

- JSON 문자열을 추가 해석하거나 숫자로 변환하지 않는다. 빈 문자열, 문자열 `"null"`, 실제 `null`을 구분한다. 실제 `null`은 Python `None`에 대응한다.
- 조회·삭제의 `found=False`는 HTTP 200이며 `value` 필드를 추가하지 않는다. `found=True`인 `None`은 `value:null`로 표현한다.
- 키·문자열 `value`의 UTF-8 바이트 제한 검증은 저장소 책임이다. HTTP에서 요청 전체 길이나 글자 수로 대체 판정하지 않는다.
- 삭제 결과를 만들기 위해 HTTP가 먼저 `get`을 호출하지 않는다. 삭제 직전 `value`와 존재 여부는 `delete` 반환을 따른다.
- 같은 저장소를 사용하는 요청은 공유 상태를 참조한다. 요청마다 새 저장소를 만들지 않는다. 실제 저장소는 `MiniRedis()`로 생성할 수 있고 HTTP 연결 지점은 `create_app(storage)`다. 서버 실행 진입점과 프로세스·스레드 조건은 아직 미정이다.
- 예상하지 못한 예외를 일괄적으로 `INVALID_INPUT`으로 변환하지 않는다.
- HTTP는 PUT 본문이 해석 가능한 JSON 객체인지, `key`·`value` 필드가 존재하는지 검사한다. GET·DELETE는 쿼리 `key` 존재 여부를 검사한다. 오류는 저장소 호출 없이 HTTP 400과 `{"error":"INVALID_INPUT"}`으로 응답한다. 필드가 존재하는 `null`·빈 문자열·다른 타입은 HTTP가 걸러내지 않고 저장소에 전달한다.

### 승인된 입력 검증 책임과 함수 계약

- 이 문서는 원본 저장 시나리오를 대체하지 않는 HTTP 담당 상세 문서다. 원본의 S1 입력·순서·기대 결과는 유지하고, HTTP 기술 선택·책임 분리 상세·구현·테스트·실행 이력은 이 문서에 모은다.
- HTTP는 JSON 해석·객체 형태의 본문·필수 필드 존재 여부를 확인한다. PUT의 `key`·`value`, GET·DELETE 쿼리의 `key`가 필수다. 필드 누락과 실제 `null`을 구분하며, 타입·크기를 검사하거나 임의로 변환하지 않는다.
- 저장소는 HTTP 없이 직접 호출되어도 키·`value` 타입·크기를 상태 변경 전에 검사한다. 키는 문자열 1~256 UTF-8 바이트, `value`는 문자열 0~65,536 UTF-8 바이트 또는 실제 `None`이다. 바이트 수는 JSON·URL 해석 후 문자열 기준이며 실제 `None`을 문자열 `"null"`로 변환하지 않는다.
- `set`·`get`·`delete`의 타입·크기 제한 위반은 `{"error":"INVALID_INPUT"}` 반환으로 전달하고, 기존 항목·다른 키를 보존하며 새 항목을 만들지 않는다. 입력 거절 전용 예외는 도입하지 않는다.
- HTTP 형식 오류와 저장소의 타입·크기 오류는 외부에서 같은 HTTP 400·`{"error":"INVALID_INPUT"}`으로 응답한다. 오류에 `ok`·`found`를 섞지 않는다. 정상 응답은 기존 `ok`·`found`·`value` 계약을 그대로 따른다.
- S1-07의 필드는 모두 존재하므로 거절 행도 `set`에 전달한다. HTTP 분리 테스트는 저장소 반환 연결을 확인하며 실제 크기 판정·거절 전 상태 보존·무효 항목 비생성은 실제 저장소 단위 테스트에서 별도로 확인한다.
- 형식 오류의 조기 거절과 저장소 호출 경로는 구분한다. 저장소 호출을 포함한 전체 예외를 입력 오류로 바꾸지 않는다. 동시 호출 허용 여부·안전성은 이번 책임 분리로 확정하지 않는다.
- 미지원 콘텐츠 유형·메서드·경로, 중복 쿼리·중복 JSON 필드 및 HTTP 통신 단계의 잘못된 UTF-8 입력 처리 정책은 미정이다. 저장소 함수의 UTF-8 인코딩 불가능 문자열 거절은 원격 S1-09·10에서 확정되었고 해당 단위 테스트를 보존한다. JSON으로 해석할 수 없는 본문은 확정한 JSON 오류 계약을 따른다. 공백·제어문자·대소문자·유니코드 정규화, 총 저장량·자원 부족, 입력 거절 외 요청 실패·응답 유실 보장은 원본의 미정 항목으로 유지한다.

## 기존 테스트별 HTTP 검증

### 공통 판정

- 각 케이스와 S1-07의 각 행은 독립 실행한다. HTTP 경계 테스트는 pytest 준비 함수 `exchange`에서 앱과 시험용 저장소를 새로 만든다. 실제 저장소의 사전 상태 준비 방법을 정한 것은 아니다.
- 원본 입력과 요청 순서를 유지하고, 각 응답을 받은 직후 다음 요청 전에 상태 코드와 본문을 확인한다.
- 아래의 `set`·`get`·`delete`는 원본에서 확정한 호출 이름이다. HTTP 연결 지점은 `create_app(storage)`이며 실제 저장소는 `storage.py`의 `MiniRedis()`다. HTTP 분리 테스트는 기존 시험용 저장소를 유지한다.
- 조회·삭제는 `found`의 타입, `value` 필드 존재 여부·타입·내용까지 확인한다. 저장 성공·거절 응답도 생략하지 않는다.
- HTTP만 분리한 검증에서는 전달한 인자와 반환 결과의 응답 연결을 확인한다. 실제 저장 상태의 변화·보존은 저장소 테스트와 실제 저장소를 연결한 외부 기능 테스트에서 확인한다.

### S1-01. 보관한 value 조회

- 원본 사전 상태: `sample:one` 없음.
- `PUT /kv` 본문 `{"key":"sample:one","value":"hello"}` → `set("sample:one", "hello")` → HTTP 200과 `{"ok":true}`.
- 이어서 `GET /kv`의 쿼리 `key=sample:one` 두 번 → 각각 `get("sample:one")` → 매번 HTTP 200과 `{"found":true,"value":"hello"}`.
- 조회로 실제 보관 내용이 바뀌지 않는지는 HTTP 분리 검증만으로 증명하지 않는다.

### S1-02. 사용자가 value 폐기

- 원본 사전 상태: `sample:one`에 `"hello"` 보관.
- `DELETE /kv`의 `key=sample:one` → `delete("sample:one")` → HTTP 200과 `{"found":true,"value":"hello"}`.
- 같은 키의 GET 두 번 → 각각 `get("sample:one")` → HTTP 200과 `{"found":false}`, `value` 생략.
- 실제 삭제와 이후 조회의 비생성 보장은 저장소·외부 기능 검증에 남긴다.

### S1-03. 다른 키에 영향을 주지 않음

- 원본 사전 상태: `sample:one`에 `"hello"`, `sample:two`에 `"world"` 보관.
- `sample:one` DELETE → `delete("sample:one")` → HTTP 200과 `{"found":true,"value":"hello"}`.
- `sample:two` GET → `get("sample:two")` → HTTP 200과 `{"found":true,"value":"world"}`.
- 서로 다른 요청의 키가 뒤바뀌지 않는지 확인한다. 실제 다른 키 보존은 저장소·외부 기능 검증에 남긴다.

### S1-04. 같은 키 덮어쓰기

- 원본 사전 상태: `sample:one`에 `"hello"`, `sample:two`에 `"world"` 보관.
- PUT 본문 `{"key":"sample:one","value":"updated"}` → `set("sample:one", "updated")` → HTTP 200과 `{"ok":true}`.
- GET 순서: `sample:one` → `sample:two` → `sample:one` → `sample:two`.
- 각각 해당 키로 `get`을 호출하고, HTTP 200과 `found:true` 및 `"updated"`·`"world"`·`"updated"`·`"world"`를 순서대로 응답한다.
- 실제 덮어쓰기·이전 내용 비복원·다른 키 보존은 저장소·외부 기능 검증에 남긴다.

### S1-05. 없는 키 조회·삭제와 반복 삭제

- 원본 사전 상태: `sample:missing`은 저장 이력이 없고 `sample:one`에 `"hello"` 보관.
- 호출 순서: `get("sample:missing")` → `delete("sample:missing")` → `delete("sample:one")` → `delete("sample:one")` → `get("sample:one")` → 유지 조건 확인용 `get("sample:missing")`.
- 모두 HTTP 200. 세 번째 요청만 `{"found":true,"value":"hello"}`이며 나머지는 `{"found":false}`로 `value`를 생략한다.
- HTTP에서 없는 키·반복 삭제를 404나 오류로 바꾸지 않는다.

### S1-06. 빈 문자열 value와 글자 그대로 보존

- 원본 사전 상태: `sample:empty`·`sample:text`·`sample:missing` 없음.
- PUT으로 `set("sample:empty", "")`, `set("sample:text", '{"a":1}')` 순서대로 호출한다. 둘 다 HTTP 200과 `{"ok":true}`.
- GET 순서: `sample:empty` → `sample:text` → `sample:missing`. HTTP 200과 각각 `{"found":true,"value":""}`, `{"found":true,"value":"{\"a\":1}"}`, `{"found":false}`.
- `sample:empty` DELETE 두 번 → HTTP 200과 각각 `{"found":true,"value":""}`, `{"found":false}`.
- 마지막 `sample:text` GET도 처음과 같은 문자열 응답을 확인한다. 문자열 내부의 JSON을 추가 해석하지 않는다.

### S1-06-01. 숫자 표현과 null 글자 보존

- 원본 사전 상태: `sample:number`·`sample:word` 없음.
- PUT으로 `set("sample:number", "10")`, `set("sample:word", "null")` 순서대로 호출하고 각 HTTP 200과 `{"ok":true}`를 확인한다.
- 두 키를 GET하여 HTTP 200과 각각 `{"found":true,"value":"10"}`, `{"found":true,"value":"null"}`을 확인한다. 숫자나 실제 `null`로 변환하지 않는다.

### S1-07. 키·value 크기 경계와 입력 거절

- 원본의 각 행을 독립 실행한다. 매번 `sample:one`에 `"hello"`, `sample:two`에 `"world"`가 있고, 크기 경계용 다른 키는 없다.
- 아래 입력을 PUT 본문에 넣어 해석 후 그대로 `set`에 전달한다. 반복 횟수는 원본과 같으며 따옴표·설명은 입력에 포함하지 않는다.

| 원본 ID | `set`에 전달할 키 | `set`에 전달할 `value` | 저장소 반환 → HTTP |
| --- | --- | --- | --- |
| S1-07-01 | `None` | `"new"` | `INVALID_INPUT` 반환 → 400 |
| S1-07-02 | `""` | `"new"` | `INVALID_INPUT` 반환 → 400 |
| S1-07-03 | `"a"` | `"hello"` | `ok=True` 반환 → 200 |
| S1-07-04 | `a` 256회 | `"hello"` | `ok=True` 반환 → 200 |
| S1-07-05 | `a` 257회 | `"hello"` | `INVALID_INPUT` 반환 → 400 |
| S1-07-06 | `한` 85회 뒤 `a` 1회 | `"hello"` | `ok=True` 반환 → 200 |
| S1-07-07 | `한` 86회 | `"hello"` | `INVALID_INPUT` 반환 → 400 |
| S1-07-08 | `"sample:one"` | `a` 65,536회 | `ok=True` 반환 → 200 |
| S1-07-09 | `"sample:one"` | `a` 65,537회 | `INVALID_INPUT` 반환 → 400 |
| S1-07-10 | `"sample:one"` | `한` 21,845회 뒤 `a` 1회 | `ok=True` 반환 → 200 |
| S1-07-11 | `"sample:one"` | `한` 21,845회 뒤 `a` 2회 | `INVALID_INPUT` 반환 → 400 |

- 성공 응답 본문은 `{"ok":true}`, 거절 응답 본문은 `{"error":"INVALID_INPUT"}`이다.
- 각 PUT 응답 확인 후 원본에서 지정한 유효한 키만 GET한다. 원본은 후속 조회 간 전체 순서를 추가로 확정하지 않았으므로 여기서 새 순서를 정하지 않는다.
- 모든 행에서 `sample:two`는 `{"found":true,"value":"world"}`.
- S1-07-03·04·06은 새 키 GET에서 `{"found":true,"value":"hello"}`.
- S1-07-08·10은 `sample:one` GET에서 `found:true`와 입력 문자열의 정확한 타입·내용을 전달한다. 나머지 행의 `sample:one`은 `{"found":true,"value":"hello"}`.
- 모든 정상 GET은 HTTP 200이다. URL 인코딩·해석 후 키가 원래 문자열과 같은지 확인한다.
- 무효한 키에 대한 GET을 임의로 추가하지 않는다. 거절 전 검증·실제 상태 보존·무효 항목 비생성은 HTTP 분리 검증으로 증명하지 않는다.

### S1-08. 실제 null인 value 보관·조회·삭제

- 원본 사전 상태: `sample:null`·`sample:missing` 없음.
- PUT 본문 `{"key":"sample:null","value":null}` → `set("sample:null", None)` → HTTP 200과 `{"ok":true}`.
- 이후 호출 순서: `get("sample:null")` → `get("sample:missing")` → `delete("sample:null")` → `delete("sample:null")` → `get("sample:null")`.
- 모두 HTTP 200. 첫 조회와 첫 삭제는 `{"found":true,"value":null}`, 나머지는 `{"found":false}`로 `value`를 생략한다.
- HTTP에서 `None`을 문자열 `"null"`로 변환하거나 존재하지 않는 것으로 처리하지 않는다.

### S1-08-01. 문자열과 실제 null 사이의 덮어쓰기

- 원본 사전 상태: `sample:one`에 `"hello"`, `sample:two`에 `"world"` 보관.
- 순서: PUT으로 `set("sample:one", None)` → GET으로 `get("sample:one")` → PUT으로 `set("sample:one", "updated")` → GET으로 `get("sample:one")` → GET으로 `get("sample:two")`.
- 두 PUT은 각각 HTTP 200과 `{"ok":true}`.
- 세 GET은 HTTP 200과 순서대로 `{"found":true,"value":null}`, `{"found":true,"value":"updated"}`, `{"found":true,"value":"world"}`.

## HTTP 구현 구조

아래 앱·함수 배치와 HTTP 형식 오류 처리를 구현했다. HTTP는 JSON·본문 구조·필수 필드를 검사하고, 저장소는 타입·크기를 검사하는 승인된 책임 분리를 따른다.

### 파일과 함수

- `app.py`: Flask 앱 생성과 요청 처리만 둔다. 별도 경로 등록 모듈·서비스 계층·저장소 구현을 추가하지 않는다.
- `create_app(storage)`: Flask 앱을 만들고 아래 세 경로를 등록한 뒤 앱을 반환한다. 전달받은 저장소를 세 처리 함수가 함께 사용한다. 실제 저장소를 생성하거나 가져오는 책임은 두지 않는다.
- `put_kv()`: `PUT /kv` 처리. JSON 본문을 해석하고 `key`·`value`를 `storage.set(key, value)`에 전달한다.
- `get_kv()`: `GET /kv` 처리. 해석된 쿼리의 `key`를 `storage.get(key)`에 전달한다.
- `delete_kv()`: `DELETE /kv` 처리. 해석된 쿼리의 `key`를 `storage.delete(key)`에 전달한다. 사전 조회하지 않는다.
- 위 세 처리 함수는 `create_app` 안에 정의하여 전달받은 저장소를 참조한다. 요청마다 저장소를 만들지 않으며 전역 저장소나 새 저장소 타입은 추가하지 않는다.

### 요청 해석과 응답

- PUT 본문은 Flask의 `request.get_json()`으로 해석한다. 이미 해석된 문자열 내부를 다시 해석하지 않는다.
- GET·DELETE의 키는 Flask가 URL 해석한 `request.args`에서 읽는다. 별도의 재해석이나 문자열 정규화를 하지 않는다.
- 명시적 JSON `null`은 `None`으로 전달한다. 필드 누락은 HTTP가 저장소 호출 없이 400·`INVALID_INPUT`으로 거절하고, 실제 `None`으로 대체하지 않는다.
- HTTP JSON·본문 구조·필수 필드 검사를 통과한 경우에만 해당 저장소 함수를 한 번 호출한다. 타입·크기 검사와 저장소 결과 재구성은 하지 않는다.
- 저장소 결과가 `{"error": "INVALID_INPUT"}`이면 상태 코드 400, 합의된 정상 결과이면 200이다. 결과 사전과 상태 코드를 반환하여 Flask가 JSON 응답을 만들게 한다.
- 정상 결과의 `found`·`value`·`ok`를 새로 계산하거나 필드를 추가하지 않는다. 키 없음과 실제 `None`인 `value`를 구분한다.
- 공통 응답 처리는 세 함수 안의 짧은 동일 처리로 둔다. 이 단계에서 별도 응답 변환 함수·예외 계층은 추가하지 않는다.
- 광범위한 예외 처리는 두지 않는다. JSON 해석 호출에서 발생한 `BadRequest`만 400 JSON으로 바꾸며, 본문 구조·필수 필드는 조건문으로 검사한다. 저장소 호출은 예외 처리 범위 밖에 두어 내부 오류를 입력 오류로 숨기지 않는다.

### 승인된 형식 오류 처리

아래 처리 배치를 승인받아 `app.py`에 구현했다. 실패하던 형식 오류 테스트 11개를 포함하여 기존 41개가 모두 통과했다.

- 기존 `create_app(storage)`·`put_kv()`·`get_kv()`·`delete_kv()`를 유지한다. 새 검증 함수·공통 요청 처리 함수·예외 계층은 추가하지 않는다.
- 이유: PUT 전용 JSON·본문 검사는 한 곳에서만 쓰이고, GET·DELETE의 필수 쿼리 검사는 각각 짧은 조건 하나다. 기존 함수 안에서 처리하면 요청부터 저장소 호출까지 흐름을 직접 읽을 수 있다.

| 함수 | 처리 순서 |
| --- | --- |
| `put_kv()` | JSON 해석 시도 → 해석 오류면 400 반환 → 본문이 객체인지 검사 → `key`·`value` 존재 여부 검사 → 구조·누락 오류면 400 반환 → `storage.set(key, value)` → 기존 응답 연결 |
| `get_kv()` | 쿼리 `key` 존재 여부 검사 → 누락이면 400 반환 → `storage.get(key)` → 기존 응답 연결 |
| `delete_kv()` | 쿼리 `key` 존재 여부 검사 → 누락이면 400 반환 → `storage.delete(key)` → 기존 응답 연결 |

- 조기 오류 반환은 모두 HTTP 400과 `{"error":"INVALID_INPUT"}`이다. 반환 전에 저장소를 호출하지 않는다.
- 필드 검사는 존재 여부로 한다. `value`가 `None`·빈 문자열인 경우와 누락을 구분하며, `key`가 존재하되 빈 문자열·`None`·다른 타입인 경우는 저장소로 전달한다. GET·DELETE의 빈 쿼리 키도 누락으로 취급하지 않는다.
- JSON 해석 실패를 다루는 범위는 JSON 해석 호출에만 한정한다. 저장소 호출을 포함한 함수 전체를 광범위한 예외 처리로 감싸지 않는다. 본문 구조와 필드 존재는 명시적인 조건으로 검사한다.
- 저장소의 타입·크기 오류 반환을 HTTP 400으로 연결하는 기존 처리는 유지한다. HTTP에서 타입·크기를 추가 검사하지 않는다.
- 미지원 콘텐츠 유형·메서드·경로 등 아직 정하지 않은 정책을 이 설계에서 새로 확정하지 않는다.
- 검증 기준: 기존 30개 통과 유지, 형식 오류 응답 11개 실패 해소, 총 41개 통과. 실제 저장소 동작·외부 네트워크 검증은 이 기준에 포함하지 않는다.

### 기존 구현 순서 기록

- 기존 `tests/test_http.py`의 `create_app(storage)` 호출과 시험용 저장소를 그대로 사용한다. 테스트 입력·기대 결과를 구현에 맞추어 바꾸지 않는다.
- 앱 생성·세 경로·저장소 호출·응답 연결만 구현한 뒤 기존 20개 테스트를 다시 실행한다.
- 실제 저장소 생성과 서버 실행 진입점은 이번 구현에 포함하지 않는다. 이를 정하지 않아도 시험용 저장소를 전달하는 HTTP 경계 테스트를 실행할 수 있다.

## 테스트 코드 연결과 실행 기록

### 승인된 책임 분리의 추가 테스트 명세

모든 행은 새 앱·시험용 저장소로 독립 실행한다. 실제 저장 상태에 의존하지 않는다. 응답 수신 직후 다음 요청 없이 저장소 호출 기록·상태 코드·JSON 본문을 확인한다.

| 테스트 함수 | 정확한 입력 | 기대 결과·유지 조건 |
| --- | --- | --- |
| `test_put_format_error_rejected_without_storage_call` | PUT `/kv`, `Content-Type: application/json`, 본문을 각각 `{"key":`, `[]`, `"hello"`, `10`, `true`, `null`, `{"value":"hello"}`, `{"key":"sample:one"}`, `{}`로 실행 | 9개 각각 400과 `{"error":"INVALID_INPUT"}`, 저장소 모든 함수 무호출 |
| `test_missing_query_key_rejected_without_storage_call` | GET `/kv`, DELETE `/kv`, 쿼리 없음 | 2개 각각 400과 같은 오류 본문, 저장소 모든 함수 무호출 |
| `test_put_type_validation_delegated_to_storage` | PUT `/kv`, 기본 본문 `{"key":"sample:one","value":"hello"}`의 `key` 또는 `value`를 각각 `10`, `true`, `[]`, `{"a":1}`로 대체 | 8개 각각 타입·내용 변환 없이 `set(key, value)` 1회만 호출. 시험용 저장소의 `INVALID_INPUT` 반환을 400과 같은 오류 본문으로 연결 |

- 형식 오류 테스트는 `http_boundary`에서 Flask의 `PROPAGATE_EXCEPTIONS=False`를 사용한다. 미처리 예외를 실제 서버처럼 500 응답으로 관찰하여 기대한 400과 비교하기 위함이며, `app.py`의 예외 처리를 대체하거나 변경하지 않는다.
- 타입 전달 테스트는 HTTP가 타입을 검사하지 않음을 검증한다. 실제 저장소가 해당 타입을 거절하는지는 별도 단위 테스트에서 확인한다. 실제 `null`과 필드 누락의 구분은 기존 S1-08과 새 누락 테스트를 함께 대조한다.

### 기존 테스트 연결과 실행 이력

- `tests/test_http.py`의 `test_s1_01`~`test_s1_08_01` 접두사 함수가 해당 원본 ID에 대응한다. S1-07은 `test_s1_07_input_boundaries`의 매개변수 11행으로 나누며 각 행에 원본 ID를 붙인다. 원본 시나리오의 실행 항목은 20개다.
- `test_storage_invalid_input_maps_to_http_400`은 GET·DELETE 두 경우를 검증한다. 요청 키는 기존 `sample:one`을 사용하고 시험용 `get`·`delete` 반환을 `{"error":"INVALID_INPUT"}`으로 설정하여 정확한 호출과 HTTP 400·오류 본문을 확인한다. 이는 해당 키가 무효하다는 뜻이 아니며 저장소의 거절 입력 정책을 정하지 않는다. 원본 S1-07에 길이 초과 키 조회·삭제를 추가한 것이 아니다. 사전 저장 상태에 의존하지 않는 함수 반환 연결 테스트다. 기존 실행 항목은 22개이며 위 추가 19개를 포함한 현재 전체는 41개다.
- `exchange`는 Python 기본 `unittest.mock.Mock`으로 `set`·`get`·`delete`만 제공한다. 저장 상태나 크기 판정 로직을 구현하지 않고 요청마다 합의된 반환을 설정한다.
- 각 요청은 실제 Flask `test_client()`를 거친다. 본문·쿼리 해석 후 저장소 호출 인자와 호출 횟수, 다른 함수의 추가 호출이 없는지 확인한다. 반환에 대한 HTTP 상태 코드·JSON 필드·타입·내용을 다음 요청 전에 검증한다.
- 크기 경계와 상태 관련 반환은 저장소의 올바른 동작을 가정한 HTTP 검증이다. 실제 크기 검사·덮어쓰기·삭제·상태 보존을 증명하지 않는다.
- 최초 기본 환경에서는 pytest 9.0.2를 확인했으며 Flask는 설치되지 않았다. 검증용 임시 가상환경 `/tmp/opencode/mini-redis-http-gm9b_rjm`에 Flask 3.1.3과 pytest 9.2.0을 설치했다. Python은 3.14.10이다. 프로젝트의 버전 고정 결정은 아니다.
- 수집 명령: `python3 -m pytest tests/test_http.py --collect-only -q` → 20개 수집.
- 실행 명령: `python3 -m pytest tests/test_http.py -q --tb=short` → 20개 모두 준비 단계 오류. `from app import create_app`에서 `ModuleNotFoundError: No module named 'app'` 발생. 실제 실행에는 캐시·바이트코드 생성을 끄는 옵션을 추가했다.
- 위 최초 결과는 테스트 통과나 응답 검증 실패가 아니라 구현 전 준비 단계 오류다. Flask를 설치한 임시 가상환경에서도 구현 전에 같은 원인으로 중단됨을 재확인했다.
- 구현 후 실행: `PYTHONDONTWRITEBYTECODE=1 /tmp/opencode/mini-redis-http-gm9b_rjm/bin/python -m pytest tests/test_http.py -v -p no:cacheprovider` → 20개 모두 통과. 기존 테스트의 입력·기대 결과를 바꾸지 않고 실제 Flask 요청 해석·저장소 호출·응답 검증까지 실행했다.
- GET·DELETE 오류 반환 연결 테스트 추가 후 같은 명령으로 재실행 → 22개 모두 통과. `app.py`와 기존 테스트는 변경하지 않았다.
- 이후 승인된 책임 분리: HTTP는 JSON·본문 구조·필수 필드, 저장소는 타입·크기를 검사한다. 이 단계에서는 문서만 수정했다. 기존 22개 테스트는 HTTP 형식 오류의 통일된 응답·저장소 무호출을 검사하지 않는다.
- 위 추가 테스트 작성 후 실행: `PYTHONDONTWRITEBYTECODE=1 /tmp/opencode/mini-redis-http-gm9b_rjm/bin/python -m pytest tests/test_http.py -q --tb=short -p no:cacheprovider` → 30개 통과·11개 검증 실패. 기존 22개와 새 타입 전달 8개는 통과했다. 형식 오류 11개 모두 저장소 무호출 검증은 통과했지만 응답 계약에서 실패했다. 잘못된 JSON·GET/DELETE 쿼리 누락 3개는 400이지만 JSON이 아니며, 객체가 아닌 본문·PUT 필드 누락 8개는 400 대신 500이다. 준비 단계 오류가 아니라 실제 응답에 대한 검증 실패다. `app.py`와 기존 22개 테스트는 변경하지 않았다.
- HTTP 형식 오류 처리 구현 후 실행: `PYTHONDONTWRITEBYTECODE=1 /tmp/opencode/mini-redis-http-gm9b_rjm/bin/python -m pytest tests/test_http.py -q -p no:cacheprovider` → 41개 통과. 테스트 코드·입력·기대 결과는 변경하지 않았다. JSON·본문 구조·필수 필드 오류의 400 JSON 및 저장소 무호출, 기존 정상 흐름과 타입 전달을 확인했다.
- 원격 `28ee569` 병합 충돌 해결 후 실행: `PYTHONDONTWRITEBYTECODE=1 /tmp/opencode/mini-redis-http-gm9b_rjm/bin/python -m pytest tests/ -q -p no:cacheprovider` → 총 95개 통과(HTTP 41개·저장소 54개). S1-09·10과 저장소 테스트를 보존했다. `requirements-dev.txt`에는 기존 pytest 항목과 HTTP 테스트에 필요한 Flask를 함께 둔다. 두 테스트 묶음을 같이 실행한 것이며 실제 저장소를 연결한 HTTP 통합 테스트를 추가한 것은 아니다.

## 아직 결정하지 않은 실행 설계

- Python·Flask 버전.
- 실제 저장소와 HTTP를 연결하는 서버 실행 진입점. 저장소는 `storage.py`의 `MiniRedis()`이며 HTTP에 전달하는 지점은 `create_app(storage)`다.
- 실제 외부 HTTP 테스트의 서버 실행·종료 및 독립 사전 상태 준비 방법.
- 원본의 미정 입력·오류 정책과 동시 호출 조건. Flask 기본값으로 자동 확정하지 않는다.

HTTP 담당 검증 통과, 저장소 단위 테스트 통과, 실제 외부 기능 테스트 통과는 각각 구분하여 보고한다. HTTP 경계 테스트 41개와 원격 저장소 단위 테스트 54개는 서로 다른 검증 범위다. 실제 외부 네트워크 기능 테스트는 미실행이며, 저장소 단위 테스트를 실제 HTTP 통합이나 동시 호출 안전성 검증으로 간주하지 않는다.
