"""SSE (Server-Sent Events) 이벤트 브로커."""

import asyncio
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class EventBroker:
    """SSE 이벤트를 여러 클라이언트에 브로드캐스트."""

    def __init__(self, max_history: int = 100):
        self._subscribers: list[asyncio.Queue] = []
        self._history: list[dict] = []
        self._max_history = max_history

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def publish(self, event_type: str, data: dict):
        entry = {
            "type": event_type,
            "data": data,
            "timestamp": datetime.now().isoformat(),
        }
        self._history.append(entry)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.remove(q)

    @property
    def history(self) -> list[dict]:
        return self._history[-50:]

    def format_sse(self, entry: dict) -> str:
        return f"event: {entry['type']}\ndata: {json.dumps(entry, ensure_ascii=False)}\n\n"


# 싱글톤
broker = EventBroker()
