"""WebSocket live event streaming endpoint."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import RUNTIME_DIR
from app.dependencies import rt

router = APIRouter()
RECORDINGS_DIR = RUNTIME_DIR / "recordings"


def load_recording(case_id: str) -> list[dict]:
    p = RECORDINGS_DIR / f"{case_id}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    q = rt.bus.subscribe()
    active = getattr(rt.bus, "_active_case", None)
    if active:
        for ev in load_recording(active):
            await ws.send_json(ev)
    try:
        while True:
            ev = await q.get()
            await ws.send_json(ev)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        rt.bus.unsubscribe(q)
