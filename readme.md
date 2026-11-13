# Mini redis 

기술스택 : 

구현 스코프 : 

최종 산출물 : 

## 저장소 단위 테스트

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/
```

가상환경 활성화는 `source .venv/bin/activate`로 한다.

테스트 계약은 [저장소 시나리오](resc/01-storage-scenario.md), 명세 대조와 실행 결과는 [작업 기록](resc/storage-test-worklog.md)을 참고한다. 저장소 단위 테스트 54개가 통과했으며 HTTP 서버는 아직 구현하지 않았다.

## 저장소 사용

```python
from storage import MiniRedis

redis = MiniRedis()
redis.set("sample:one", "hello")  # {"ok": True}
redis.get("sample:one")  # {"found": True, "value": "hello"}
redis.delete("sample:one")  # {"found": True, "value": "hello"}
redis.get("sample:one")  # {"found": False}
```

각 객체는 독립된 Python 딕셔너리를 사용한다. 전체 키·value 관찰은 테스트 전용 `tests/storage_helpers.py`의 `snapshot(store)`로 수행한다. 실행 코드에는 스냅샷 메서드가 없다.
