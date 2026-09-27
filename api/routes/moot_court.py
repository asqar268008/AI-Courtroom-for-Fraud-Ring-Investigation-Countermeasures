"""Moot Court Training and Benchmark routes."""
from __future__ import annotations

import asyncio
from fastapi import APIRouter
from app.dependencies import rt
from agents.moot_court.grader import run_moot

router = APIRouter()


@router.post("/moot/run")
async def start_moot_court(rounds: int = 4, cases: int = 25, reset: bool = False):
    async def _runner():
        async def on_progress(ev: dict):
            await rt.bus.publish({"case_id": None, "type": "moot.progress", "t": 0, "payload": ev})
        await run_moot(rounds=rounds, cases_per_round=cases, reset=reset, progress_cb=on_progress, memory=rt.memory)
        await rt.bus.publish({"case_id": None, "type": "moot.finished", "t": 0, "payload": {}})

    rt.moot_task = asyncio.create_task(_runner())
    return {"status": "started", "rounds": rounds, "cases_per_round": cases}
