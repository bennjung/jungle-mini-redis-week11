# Mini redis 

기술스택 : 

구현 스코프 : 

최종 산출물 : 

## HTTP 서버 실행

프로젝트 루트에서 실행한다.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python app.py
```

- 접속 주소: `http://127.0.0.1:5000`
- 종료: 실행 중인 터미널에서 `Ctrl+C`
- 현재는 같은 컴퓨터에서만 접속할 수 있는 순차 처리 개발 서버다.
- 데이터는 메모리에 보관하므로 서버 종료·재시작 시 사라진다.
- 요청 방법은 [REST API 명세](rest-api.md)를 참고한다.

## 저장소 단위 테스트

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/
```

가상환경 활성화는 `source .venv/bin/activate`로 한다.

테스트 계약은 [저장소 시나리오](resc/scenario_01_storage/01-storage-scenario.md), 명세 대조와 실행 결과는 [작업 기록](resc/scenario_01_storage/storage-test-worklog.md)을 참고한다. HTTP 구현·분리 테스트·실제 로컬 HTTP 검증은 [HTTP 담당 범위](resc/scenario_01_storage/01-http-scope.md)를 참고한다.

### JSON 자료형·증감 테스트

[테스트 케이스](resc/scenario_02_concurrent_counter/02-test-cases.md)를 참고한다. JSON 자료형 저장과 증감을 구현했으며, 기존 숫자·불리언·배열·객체 거절 테스트도 승인된 계약에 따라 같은 입력의 저장 성공으로 전환했다.

```sh
.venv/bin/python -m pytest tests/scenario_01_storage/test_json_types_extension.py tests/scenario_02_concurrent_counter -q
```

실제 TCP 동시 요청의 반복 횟수와 완료 한도는 아직 확정하지 않았다. `COUNTER_TEST_REPEAT`·`COUNTER_TEST_TIMEOUT`을 실행자가 지정한 경우에만 해당 테스트를 실행하며, 미지정 시 건너뛴다. 건너뛴 테스트를 검증 완료로 세지 않는다.

## 저장소 사용

```python
from storage import MiniRedis

redis = MiniRedis()
redis.set("sample:one", "hello")  # {"ok": True}
redis.get("sample:one")  # {"found": True, "value": "hello"}
redis.delete("sample:one")  # {"found": True, "value": "hello"}
redis.get("sample:one")  # {"found": False}
```

각 객체는 독립된 Python 딕셔너리를 사용한다. 전체 키·value 관찰은 테스트 전용 `tests/scenario_01_storage/storage_helpers.py`의 `snapshot(store)`로 수행한다. 실행 코드에는 스냅샷 메서드가 없다.
