"""날씨 데모의 실제 파이프라인 이벤트를 모니터 구독자에게 전달한다."""

import asyncio
import threading


class EventRecorder:
    """앱 인스턴스별 이벤트 순번과 SSE 구독 큐를 소유한다.

    이벤트는 구독 이후에만 전달하며 과거 이력을 보관하지 않는다.
    """

    def __init__(self):
        self._sequence = 0
        self._subscribers = set()
        self._loop = None
        self._lock = threading.Lock()

    def cursor(self):
        with self._lock:
            return self._sequence

    def subscribe(self, loop):
        queue = asyncio.Queue()
        with self._lock:
            self._subscribers.add(queue)
            self._loop = loop
        return queue

    def unsubscribe(self, queue):
        with self._lock:
            self._subscribers.discard(queue)
            if not self._subscribers:
                self._loop = None

    def publish(
        self,
        *,
        terminal,
        kind,
        message,
        request_id,
        elapsed_ms,
        step=None,
        target=None,
        status=None,
        calls=None,
    ):
        with self._lock:
            self._sequence += 1
            event = {
                "seq": self._sequence,
                "request_id": request_id,
                "kind": kind,
                "step": step,
                "message": message,
                "target": target,
                "elapsed_ms": elapsed_ms,
                "status": status,
                "calls": calls,
            }
            subscribers = tuple(self._subscribers)
            loop = self._loop

        if loop is not None:
            for queue in subscribers:
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
                except RuntimeError:
                    # 구독자가 닫힌 루프에 남아 있어도 날씨 요청은 계속 처리한다.
                    pass

        print(terminal, flush=True)
        return event
