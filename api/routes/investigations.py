"""Investigations management routes."""
from __future__ import annotations

import asyncio
from typing import Optional
from fastapi import APIRouter, HTTPException

from app.config import settings
from app.dependencies import rt
from app.schemas import InvestigateIn
from api.websocket import load_recording
from core.workflow import run_case
from services.report_service import list_dossiers

router = APIRouter()


async def investigate(seed: str, alert_time: float, reason: str = "manual investigation") -> dict:
    try:
        final, ctx = await run_case(
            rt.store, rt.memory, seed, alert_time,
            mode="live", reason=reason, bus=rt.bus, bank=rt.bank, audit=rt.audit,
            settings=settings, llm=rt.llm, scenario_meta=rt.meta,
        )
        cid = final["case_id"]
        rt.cases[cid] = final
        rt.finals[cid] = (final, ctx)
        return final
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise


@router.post("/investigate")
async def manual_investigation(body: InvestigateIn):
    now = body.alert_time or (rt.live_txns[0]["ts"] if rt.live_txns else 0.0)
    task = asyncio.create_task(investigate(body.seed, now, body.reason))
    rt.case_tasks.add(task)
    task.add_done_callback(rt.case_tasks.discard)
    return {"status": "investigation_started", "seed": body.seed}


@router.get("/investigations")
def get_investigations():
    return list_dossiers()


@router.get("/investigations/{case_id}/events")
def get_investigation_events(case_id: str):
    return load_recording(case_id)
