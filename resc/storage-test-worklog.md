# 저장소 단위 테스트 작업 기록

## 코딩 전 명세 대조

아래는 초기 작성 시점의 기록이다. 최신 상태 관찰 방식은 문서 끝의 ‘스냅샷 관찰 도구 분리’를 따른다.

- 기준: `01-storage-scenario.md`의 확정 계약과 행동 테스트 케이스. 기존 입력·순서·반환·검증 시점을 유지한다.
- 사용자 합의: `pytest`, 테스트마다 새 `MiniRedis()`, `storage.py`의 `MiniRedis`, `tests/test_storage.py`에 테스트 배치.
- 호출 대상: 저장 `set(key, value)`, 조회 `get(key)`, 삭제 `delete(key)`. HTTP 없이 순차 실행한다.
- 사전 상태는 `set` 성공을 확인하며 준비한다. 전체 상태는 `_snapshot()`이 반환하는 독립된 `키 → value` 사본으로 확인한다. 관찰용 딕셔너리가 저장소의 내부 구현을 뜻하지는 않는다.
- 각 요청 반환 직후 결과의 필드·타입·내용을 확인하고, 다음 요청 전에 전체 상태를 비교한다. 아래 사전 상태와 입력의 실제 `null`은 Python `None`이다.

| ID | 사전 상태·정확한 입력·호출 순서 | 기대 결과·유지 조건 |
| --- | --- | --- |
| S1-01 | 빈 저장소; `set("sample:one", "hello")`, 같은 키 `get` 2회 | 저장 성공; 두 조회 `hello`; 상태 유지 |
| S1-02 | `sample:one=hello`; `delete`, 같은 키 `get` 2회 | 삭제 직전 `hello` 반환; 두 조회 없음; 재생성 없음 |
| S1-03 | `sample:one=hello`, `sample:two=world`; one 삭제, two 조회 | 삭제 `hello`; two는 `world` 유지 |
| S1-04 | 위 두 키; one에 `updated` 저장, one·two 순서로 조회 2회 | one=`updated`, two=`world`; 이전 내용 복원 없음 |
| S1-05 | one=`hello`, missing은 저장 이력 없음; missing 조회·삭제, one 삭제 2회·조회, missing 재조회 | missing은 항상 없음; one 첫 삭제만 `hello`; 이후 없음 |
| S1-06 | 빈 저장소; empty에 `""`, text에 문자열 `{"a":1}` 저장; empty·text·missing 조회; empty 삭제 2회; text 재조회 | 문자열 타입·내용 보존; 빈 문자열과 없음 구분; text 유지 |
| S1-06-01 | 빈 저장소; number에 `"10"`, word에 `"null"` 저장 후 각각 조회 | 숫자·실제 null로 변환하지 않음 |
| S1-07-01~11 | 매 행 one=`hello`, two=`world`; 아래 정확한 입력으로 `set` 후 명세의 유효한 키 조회 | 성공·거절 반환 확인; 전체 상태와 후속 조회로 항목 추가·삭제·변경 검증 |
| S1-08 | 빈 저장소; null 키에 `None` 저장·조회, missing 조회, null 키 삭제 2회·조회 | 존재하는 None과 없음 구분; 첫 삭제는 None 반환 |
| S1-08-01 | one=`hello`, two=`world`; one에 None 저장·조회, `updated` 저장·조회, two 조회 | 두 저장 성공; one은 None에서 문자열로 변경; two 유지 |

### S1-07의 정확한 입력

각 행은 독립 실행한다. 잘못된 키에 추가 `get`을 호출하지 않는다. 모든 행에서 `sample:two`를, 명세에 따라 새 유효 키와 `sample:one`을 조회한다.

| ID 끝번호 | key | value | 저장 결과 |
| --- | --- | --- | --- |
| 01 | `None` | `"new"` | INVALID_INPUT |
| 02 | `""` | `"new"` | INVALID_INPUT |
| 03 | `"a"` | `"hello"` | 성공 |
| 04 | `"a" * 256` | `"hello"` | 성공 |
| 05 | `"a" * 257` | `"hello"` | INVALID_INPUT |
| 06 | `"한" * 85 + "a"` | `"hello"` | 성공 |
| 07 | `"한" * 86` | `"hello"` | INVALID_INPUT |
| 08 | `"sample:one"` | `"a" * 65_536` | 성공 |
| 09 | `"sample:one"` | `"a" * 65_537` | INVALID_INPUT |
| 10 | `"sample:one"` | `"한" * 21_845 + "a"` | 성공 |
| 11 | `"sample:one"` | `"한" * 21_845 + "aa"` | INVALID_INPUT |

## 테스트 작성 단계 실행 기록

- 실행 환경: Python 3.14.3, 프로젝트 `.venv`, pytest 9.2.0. Python 지원 버전을 새로 확정한 것은 아니다.
- `.venv/bin/python -m pytest tests/test_storage.py --collect-only -q`: 성공, 21개 수집. S1 문서의 20개 실행 케이스와 스냅샷 사본 독립성 1개다.
- `.venv/bin/python -m pytest tests/test_storage.py -q --tb=short`: 준비 단계 오류 21개. `storage.py`가 없어 `from storage import MiniRedis`에서 `ModuleNotFoundError`가 발생했다. 기대 결과 assertion 실패가 아니며, 저장소 행동의 통과·실패는 아직 미검증이다.
- 명세 대조: S1-01~08과 하위 케이스의 입력·호출 순서·반복 횟수·응답 확인 시점을 유지했다. S1-07은 초과 길이 키에 `get`을 호출하지 않으며 전후 전체 상태로 무효한 항목 미추가를 검증하도록 작성했다.
- `_snapshot()` 결과와 반환 딕셔너리는 키 집합·타입·내용을 검사한다. 실제 `None`, 문자열 `"null"`, 빈 문자열, 키 없음은 서로 구분한다.
- 코드에는 스킵·xfail·대체 저장소를 넣지 않았다. 실제 저장소 구현과 HTTP 기능·동시성·TTL·성능·장애 복구·충돌·리사이즈 검증은 이번 작업 범위가 아니다.
- 당시 단계의 다음 작업은 저장소 구현 후 동일 명령 재실행이었다. 테스트 작성 단계에는 통과한 동작 테스트가 없었으며, 이후 구현 결과는 아래에 기록한다.

## 딕셔너리 기반 저장소 구현 작업

- 사용자 승인: `MiniRedis` 인스턴스의 내부 저장소는 Python `dict`를 사용한다. 생성 시 새 빈 딕셔너리를 만들며, `_snapshot()`은 그 전체 사본을 반환한다.
- 구현 전 같은 명령으로 실행: 기존 21개 모두 `storage` 모듈 부재로 준비 단계에서 중단됐다. assertion 실패가 아니다.
- 기존 테스트 코드 SHA-256: `664ae2fb515586c57b30d14ffea2ddbf268495b350b3b838a3d4ab5895248e21`. 구현에 맞춰 기존 계약 테스트를 변경하지 않는다.
- 명세 대조: 위 ID별 호출 대상·사전 상태·입력·검증 시점·반환·유지 조건을 구현 기준으로 사용한다. 입력 제한 검증은 상태 변경 전에 끝낸다. 존재 여부는 `None` 비교 대신 키 포함 여부로 판정한다.
- 추가 사용자 승인: 잘못된 키에 대한 조회·삭제와 입력 타입·UTF-8 인코딩 실패는 모두 `INVALID_INPUT`으로 거절하고 기존 상태를 보존한다.
- 구현 전 추가 명세: 문서의 S1-09는 9개 무효 키를 `set`·`get`·`delete`에 각각 넣는 27개 케이스다. S1-10은 6개 무효 value를 `sample:one`에 저장하려는 케이스다. 정확한 입력은 해당 표를 따르며, 각 실행 전 두 정상 키를 준비하고 거절 반환 직후 전체 상태와 후속 두 키 조회를 검사한다.
- 위 추가 검증은 `tests/test_storage_invalid_input.py`에 작성한다. 기존 S1-01~08 케이스는 수정하지 않는다.

### 추가 테스트 작성 후, 동작 구현 전 실행

- `.venv/bin/python -m pytest tests/ -q --tb=no`: 21 failed, 33 errors. 생성자와 스냅샷만 존재하는 상태에서 `set`·`get`·`delete`가 미정의여서 `AttributeError`로 중단됐다. pytest의 failed 표시는 기대 결과 assertion 실패와 구분한다. 추가 33개는 사전 상태 준비 단계에서 중단됐다.

### 구현 후 실행 및 명세 재대조

- `.venv/bin/python -m pytest tests/ -q`: **54 passed**. 기존 21개와 추가 33개를 실제 `MiniRedis` 구현으로 실행했다.
- 기존 `tests/test_storage.py`의 SHA-256이 구현 전과 동일하다. 기존 테스트의 호출 대상·정확한 입력·순서·기대 결과를 변경하지 않았다.
- 구현: 인스턴스별 `self._data` 딕셔너리, 검증 후 저장·덮어쓰기, 순수 조회, 삭제 직전 value 반환, 전체 사본 스냅샷. 입력 거절 시 딕셔너리를 변경하지 않는다. 별도 해시 테이블은 구현하지 않았다.
- 추가 거절 테스트: 세 함수의 무효 키 27개 조합과 set의 무효 value 6개 모두 반환·전후 전체 상태·두 정상 키 조회를 확인했다.
- 미검증: HTTP 기능·동시 카운터·TTL·성능·장애 복구·총 저장량 한도·자원 부족·입력 누락. 54개 통과를 이 범위의 완료로 확장하지 않는다.

## 스냅샷 관찰 도구 분리

- 사용자 요청: 실행 코드의 `_snapshot()` 메서드를 제거하고 테스트 전용 모듈로 분리한다.
- 변경 전 `.venv/bin/python -m pytest tests/ -q`: 54개 통과.
- 변경 범위: `tests/storage_helpers.py`의 `snapshot(store)`가 `store._data.copy()`를 반환한다. 저장소 생성·set·get·delete 호출 대상, 모든 테스트의 사전 상태·입력·순서·기대 결과·검증 시점은 유지한다. 각 호출 직후와 거절 전후 전체 상태 관찰의 위치도 유지한다.
- 실행 코드가 테스트 모듈을 참조하거나 테스트 도구를 호출하지 않도록 한다. 기존 스냅샷 독립성 테스트는 같은 사본 수정·추가·삭제를 새 관찰 함수에 적용한다.
- 변경 후 `.venv/bin/python -m pytest tests/ -q`: **54개 모두 통과**. 기존 입력과 기대 결과를 유지했으며 변경한 것은 상태 관찰 함수의 위치와 호출 방식이다.
