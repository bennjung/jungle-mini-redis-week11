# FastAPI·asyncio 정리 — 주기 TTL 정리를 위한 배경

작성일: 2026-11-14

목적: Flask/Werkzeug에서 FastAPI/ASGI로 옮길 때 필요한 async 개념과, TTL active 만료를 주기적으로 실행하는 방법을 처음 보는 사람도 이해할 수 있게 정리한다.

이 문서는 결정 문서가 아니다. FastAPI 전환 검토 이슈(#6)의 배경 자료이며, 전환 여부를 확정하지 않는다.

## 1. 출발점: TTL 정리는 왜 어려운가

TTL 만료는 두 갈래로 처리한다.

- **Lazy 만료** — `get`/`set`/`delete` 할 때 만료 시각을 확인하고, 지났으면 "없음"으로 응답하면서 삭제한다.
- **Active 만료** — 아무도 다시 찾지 않는 콜드 키를 주기적으로 찾아 메모리에서 회수한다.

Lazy만으로는 접근되지 않는 키가 영원히 남는다. 그래서 active 만료가 필요한데, 이건 **요청과 무관하게 주기적으로** 실행되어야 한다.

Redis는 자체 이벤트 루프에 `serverCron`이라는 주기 작업을 등록해 이 문제를 푼다. `hz=10`이면 약 100ms마다 깨어나 `activeExpireCycle`을 실행한다. 별도 스레드가 아니라 명령 처리와 같은 스레드다.

현재 Flask `threaded=False` + `app.run()`에는 이런 주기 훅이 없다. 그래서 다음 선택지가 나온다.

- `socketserver.service_actions` — 단일 스레드·무락이지만 Werkzeug 개발 서버 전용
- 백그라운드 스레드 + 락 — 이식성은 좋지만 동시 접근이 생김
- 외부 cron + 정리 엔드포인트 — 락은 없지만 운영 요소가 추가됨
- FastAPI/ASGI lifespan + asyncio 주기 태스크 — 이벤트 루프 하나에서 유휴 정리까지 가능

## 2. async/await 기본

### 2.1 `async def` — 코루틴 함수

`async def`는 일반 함수가 아니라 **코루틴 함수**를 정의한다.

```python
async def hello():
    print("본문 실행")
    return "hi"
```

가장 중요한 차이는 **호출해도 본문이 바로 실행되지 않는다**는 점이다.

```python
c = hello()          # 아무것도 출력되지 않음
print(type(c))       # <class 'coroutine'>
```

`hello()`는 결과가 아니라 **나중에 실행할 수 있는 코루틴 객체**를 돌려준다. 본문은 이벤트 루프가 구동할 때 실행된다.

```python
import asyncio

async def hello():
    print("본문 실행")
    return "hi"

c = hello()
print("호출 후")
asyncio.run(c)
```

출력:

```
호출 후
본문 실행
```

이것은 `snprintf`처럼 결과를 미리 버퍼에 만들어 두는 것과 다르다. **계산 자체를 미루고, 중간에 멈췄다 이어갈 수 있는 상태**를 만드는 것이다.

- `def` = 바로 요리해서 접시를 내놓는 것
- `async def` = 레시피와 재료를 준비해 둔 상태
- `await` = 요리를 시작하고, 중간에 멈출 수 있는 지점

`async def`라고 해서 자동으로 병렬 실행되거나 별도 스레드에서 도는 것은 아니다.

### 2.2 `await` — 제어를 넘기고 기다리기

`await`는 `async def` 안에서만 쓸 수 있다.

```python
async def main():
    print("A")
    await asyncio.sleep(1)
    print("B")
```

- `await asyncio.sleep(1)`에서 현재 코루틴이 멈추고 이벤트 루프에 제어를 넘긴다.
- 1초 뒤 이벤트 루프가 다시 이 코루틴을 이어서 실행한다.
- `time.sleep(1)`은 스레드 전체를 멈춘다. `asyncio.sleep(1)`은 다른 작업에 양보한다.

중요한 규칙:

- `await`는 **멈출 수 있는 지점**이다. 그 사이에 다른 코루틴이 실행될 수 있다.
- 저장소의 읽기-수정-쓰기 같은 임계 구역 안에는 `await`를 넣으면 안 된다.
- `await`가 없는 동기 코드는 한 코루틴이 끝날 때까지 다른 코루틴이 끼어들지 않는다.

### 2.3 이벤트 루프

이벤트 루프는 **코루틴과 타이머를 번갈아 실행하는 스케줄러**다.

```python
async def fetch(name):
    print("start", name)
    await asyncio.sleep(1)
    print("end", name)

async def main():
    await asyncio.gather(fetch("A"), fetch("B"))

asyncio.run(main())
```

출력:

```
start A
start B
(1초 후)
end A
end B
```

- 스레드는 하나다.
- A가 `await sleep`에서 양보하면 B가 시작된다.
- 1초 뒤 둘 다 이어서 끝난다.
- 병렬이 아니라 **동시성**이다. CPU 코어를 여러 개 쓰는 것이 아니다.

### 2.4 태스크와 Future

- **코루틴**은 아직 실행되지 않은 작업이다.
- **태스크**는 이벤트 루프에 등록된 코루틴이다. `asyncio.create_task(coro())`.
- **Future**는 나중에 채워질 결과의 자리다. `await future`로 기다린다.

```python
task = asyncio.create_task(cleaner())
...
task.cancel()
```

### 2.5 `asyncio.run`

`asyncio.run(coro)`은 새 이벤트 루프를 만들고, 코루틴을 끝까지 실행한 뒤 루프를 닫는다.

FastAPI/uvicorn에서는 uvicorn이 이미 루프를 소유하므로 `asyncio.run`을 직접 호출하지 않는다.

## 3. FastAPI에서

### 3.1 `async def` 라우트 vs `def` 라우트

- `async def` 라우트 → **이벤트 루프**에서 실행된다.
- `def` 라우트 → Starlette가 **스레드풀**에서 실행한다. 여러 요청이 동시에 저장소를 만질 수 있다.

저장소를 단일 스레드로 보호하려면 저장소를 만지는 라우트는 `async def`여야 한다.

### 3.2 lifespan — 시작과 종료

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(cleaner())
    yield
    task.cancel()
```

- `yield` 전이 startup, 후가 shutdown이다.
- 주기 태스크를 여기서 시작하고 종료한다.

### 3.3 주기 TTL 정리

```python
async def cleaner():
    while True:
        await asyncio.sleep(1)        # 1초 동안 다른 요청에 양보
        store.active_expire_step(20)  # await 없이 짧게 실행
```

- `await asyncio.sleep(1)`이 타이머 역할을 한다.
- 요청이 없어도 이벤트 루프가 깨어나 cleaner를 실행한다.
- `active_expire_step` 안에 `await`가 없으면, 그 20개 확인은 중간에 끼어들지 않고 끝난다.
- 이 구조가 Redis의 `serverCron → activeExpireCycle`과 가장 비슷하다.

### 3.4 단일 스레드를 지키는 조건

1. uvicorn 워커 1개
2. 저장소를 만지는 라우트는 `async def`
3. 저장소 호출 사이에 `await` 금지
4. 블로킹 호출 금지 (`time.sleep`, 디스크, DB)
5. 테스트도 이 모델을 깨지 않게 작성

조건을 지키면 요청 처리와 TTL 정리가 하나의 이벤트 루프에서 직렬로 실행된다. 그래서 락이 필요 없다.

### 3.5 기존 HTTP 계약 유지

FastAPI는 검증 실패를 기본 **422**로 반환한다. 현재 계약은 **400 `INVALID_INPUT`** 이다.

```python
from fastapi.exceptions import RequestValidationError

@app.exception_handler(RequestValidationError)
async def invalid_input(request, exc):
    return JSONResponse(status_code=400, content={"error": "INVALID_INPUT"})
```

또는 본문을 `dict`로 받고 저장소에서 직접 검증해 422를 피할 수 있다.

## 4. 무엇에 묶이는가

| 방식 | 유휴 정리 | 추가 스레드 | 락 | 서버 독립 |
| --- | --- | --- | --- | --- |
| 요청 트리거 | 안 됨 | 0 | 불필요 | 됨 |
| `service_actions` | 됨 | 0 | 불필요 | Werkzeug만 |
| 백그라운드 스레드 | 됨 | 1 | 필요 | 됨 |
| 외부 cron | 됨 | 0 | 불필요 | 됨 |
| FastAPI lifespan | 됨 | 0 | 불필요 | ASGI만 |

- FastAPI에 묶이는 것은 **주기 실행 트리거**다.
- 저장소/TTL 로직은 프레임워크와 무관하게 유지할 수 있다.

## 5. 용어

- **코루틴** — `async def`로 만든, 중간에 멈췄다 이어질 수 있는 작업.
- **이벤트 루프** — 코루틴과 타이머를 번갈아 실행하는 스케줄러.
- **태스크** — 이벤트 루프에 등록된 코루틴.
- **Future** — 나중에 채워질 결과의 자리.
- **ASGI** — 비동기 Python 웹 서버 인터페이스. FastAPI가 사용한다.
- **uvicorn** — ASGI 서버.
- **lifespan** — ASGI 앱의 시작/종료 훅.
- **Starlette 스레드풀** — `def` 라우트를 실행하는 별도 스레드 묶음.

## 6. 참고

- `app.py`, `storage.py`
- `resc/scenario_04_response_cache/ldc-scenario-template.md`
- `AGENTS.md`
- GitHub issue #6: FastAPI 마이그레이션 검토
