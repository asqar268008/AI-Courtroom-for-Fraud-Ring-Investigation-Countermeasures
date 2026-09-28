"""Moot Court Training and Benchmark routes."""
from __future__ import annotations

import asyncio
import time
from fastapi import APIRouter
from app.dependencies import rt
from agents.moot_court.grader import run_moot, load_metrics

router = APIRouter()


@router.get("/metrics")
@router.get("/moot/metrics")
def get_moot_metrics():
    metrics = load_metrics()
    metrics["precedents"] = rt.memory.data.get("precedents", [])
    metrics["policy"] = rt.memory.data.get("policy", {})
    return metrics


@router.post("/moot/run")
async def start_moot_court(rounds: int = 4, cases: int = 25, reset: bool = False):
    async def _runner():
        async def on_progress(ev: dict):
            await rt.bus.publish({"case_id": None, "type": "moot.progress", "t": time.time(), "payload": ev})
        try:
            res = await run_moot(rounds=rounds, cases_per_round=cases, reset=reset, progress=on_progress, memory=rt.memory)
            await rt.bus.publish({
                "case_id": None,
                "type": "moot.done",
                "t": time.time(),
                "payload": {"rounds": rounds, "elapsed_s": res.get("elapsed_s", 0)}
            })
        except Exception as exc:
            import traceback
            traceback.print_exc()
            await rt.bus.publish({
                "case_id": None,
                "type": "moot.done",
                "t": time.time(),
                "payload": {"rounds": 0, "elapsed_s": 0, "error": str(exc)}
            })

    rt.moot_task = asyncio.create_task(_runner())
    return {"status": "started", "rounds": rounds, "cases_per_round": cases}
