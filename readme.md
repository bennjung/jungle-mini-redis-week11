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
- 현재는 같은 컴퓨터에서만 접속할 수 있는 단일 이벤트 루프 개발 서버다. `app.py`가 uvicorn을 `workers=1`로 실행한다.
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

### 성능 비교 설계

[03 성능 비교 시나리오](resc/scenario_03_performance/03-performance-scenario.md)와 [JMeter 테스트 설계](resc/scenario_03_performance/03-jmeter-test-design.md)를 참고한다. 도구는 Apache JMeter이며 기존 pytest 기능 테스트와 구분한다. 실행 계획·조율·집계와 측정 도구 검증을 `tests/scenario_03_performance/`에 구현했다. 실제 실행 범위·실패·분석은 [실행 기록](resc/scenario_03_performance/03-performance-test-worklog.md)에 남긴다.

- 실행 환경: JMeter 5.6.3, Java 17, MySQL 8.4 및 Connector/J. JMeter `lib/`에 Connector/J를 준비한다. 이 도구는 Python에서 HTTP 부하를 직접 만들지 않고 JMeter를 실행한다.
- 시험 전용 스키마에만 `tests/scenario_03_performance/mysql_setup.sql`을 적용한다. 기존 테이블을 자동 삭제하거나 변경하지 않는다.
- `run_performance.py --help`에서 `single`·`pilot`·`campaign`의 필수 입력을 확인한다. 총 요청 수와 시간 한도는 파일럿 후 본 측정 전에 고정한다. 프로그램이 본 측정 수치를 자동 선택하지 않는다.
- 접속 정보는 `M3_HTTP_HOST`·`M3_HTTP_PORT`·`M3_JDBC_URL`·`M3_JDBC_USER`, 도구 위치는 `M3_JMETER_BIN`·`M3_JAVA_HOME`으로 전달할 수 있다. 비밀번호는 `M3_JDBC_PASSWORD_FILE`로 사용자 전용 파일 경로를 전달하며 명령행·JMX·저장소에 넣지 않는다. JDBC URL에 비밀번호나 자동 재연결을 넣으면 거절한다.
- 실행에는 비밀 없는 버전 기록 JSON(`--environment-file`), 새 결과 위치(`--results-dir`), 같은 실제 대상 환경에서 계속 공유할 상태 위치(`--state-dir`)가 필요하다. 결과 위치가 바뀌어도 상태 위치는 바꾸지 않는다. 불명확한 실패 후 표식이나 잠금을 지워 자동 재실행하지 않는다.
- 새 측정 도구의 기본 pytest 실행은 외부 도구 검증을 건너뛴다. 실제 HTTP/JMeter 검증에는 `M3_RUN_JMETER=1`, 실제 시험용 MySQL 검증에는 추가로 `M3_RUN_JDBC=1`과 접속 정보가 필요하다. 건너뛴 항목을 통과로 세지 않는다.
- `campaign` 전에는 양쪽 S3-01 통과와 N·시간 한도 확정을 실행자가 확인해야 한다. 현재 CLI가 이 승인 절차 자체를 자동 검증하지는 않는다.
- 현재 사용자가 확정한 본 측정 조건은 N=10,000, 동시성1·10·50·100, 요청별5초·전체360초·준비30초·종료15초, 대상별5회 교차 반복이다. 예비 전체120초 중단 자료는 새 본 측정과 분리해 보존한다.
- 실제 결과: 측정 도구83개 검증 통과, 본 측정40개·400,000건 모두 성공. 현재 구성의 처리량 중앙값은 Mini Redis 약1,594~2,582건/초, MySQL 약67~83건/초였다. 양쪽의 내구성·통신 경로 차이와 세부 분석은 [실행 기록](resc/scenario_03_performance/03-performance-test-worklog.md), 수치와 범위는 [집계 JSON](resc/scenario_03_performance/03-performance-results.json)을 따른다. 로컬 원자료는 Git에서 제외한 `tests/scenario_03_performance/results/final-20261114.tar.gz`에 보존한다.
- 계측 코드 자체의 영향과 별도 측정 도구와의 대조는 미검증이다. 위 수치는 현재 계측 구성의 관찰이며 저장소 자체의 속도 차이나 서버 포화를 확정하지 않는다. 추가 진단·개선 논의는 [이슈 #5](https://github.com/bennjung/jungle-mini-redis-week11/issues/5)로 보류했다.

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
