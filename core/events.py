"""In-process event bus. Agents publish events; every WebSocket client receives them.

For multi-process deployments replace this class with Redis pub/sub; the interface stays the same.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.config import RUNTIME_DIR

RECORDINGS = RUNTIME_DIR / "recordings"
RECORDINGS.mkdir(parents=True, exist_ok=True)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def publish(self, event: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


def save_recording(case_id: str, events: list[dict]) -> Path:
    path = RECORDINGS / f"{case_id}.json"
    path.write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    (RECORDINGS / "latest.json").write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    return path
