"""LangGraph wiring: Orchestrator -> Scout -> Pattern -> (Prosecutor || Defense) -> Judge -> Action/Review/Grade."""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.config import Settings, settings as default_settings
from app.schemas import FraudInvestigationState
from core.context import CONTEXTS, CaseContext
from core.events import EventBus
from core.llm import LLM
from database.graph_store import GraphStore
from security.audit import AuditLog
from services.bank_service import BankService, MockBank
from services.report_service import build_dossier, save_dossier
from agents.investigation.transaction_agent import orchestrator
from agents.investigation.graph_investigation_agent import scout
from agents.investigation.evidence_aggregation_agent import pattern_detector
from agents.courtroom.prosecutor_agent import prosecutor
from agents.courtroom.defense_agent import defense
from agents.courtroom.judge_agent import judge, route_after_judge
from agents.countermeasures.countermeasure_agent import countermeasure, human_review
from agents.moot_court.grader import moot_grader
from agents.moot_court.precedent_memory import Memory


def build_app(checkpoint: bool = True):
    g = StateGraph(FraudInvestigationState)
    g.add_node("orchestrator", orchestrator)
    g.add_node("scout", scout)
    g.add_node("pattern", pattern_detector)
    g.add_node("prosecutor", prosecutor)
    g.add_node("defense", defense)
    g.add_node("judge", judge)
    g.add_node("countermeasure", countermeasure)
    g.add_node("human_review", human_review)
    g.add_node("moot_grader", moot_grader)

    g.add_edge(START, "orchestrator")
    g.add_edge("orchestrator", "scout")
    g.add_edge("scout", "pattern")
    g.add_edge("pattern", "prosecutor")
    g.add_edge("pattern", "defense")
    g.add_edge(["prosecutor", "defense"], "judge")
    g.add_conditional_edges(
        "judge",
        route_after_judge,
        {"act": "countermeasure", "review": "human_review", "grade": "moot_grader"},
    )
    for end in ("countermeasure", "human_review", "moot_grader"):
        g.add_edge(end, END)
    return g.compile(checkpointer=MemorySaver() if checkpoint else None)


_LIVE_APP = None
_MOOT_APP = None


def get_app(mode: str):
    global _LIVE_APP, _MOOT_APP
    if mode == "live":
        if _LIVE_APP is None:
            _LIVE_APP = build_app(checkpoint=True)
        return _LIVE_APP
    if _MOOT_APP is None:
        _MOOT_APP = build_app(checkpoint=False)
    return _MOOT_APP


async def run_case(
    store: GraphStore,
    memory: Memory,
    seed: str,
    alert_time: float,
    *,
    mode: str = "live",
    reason: str = "",
    bus: Optional[EventBus] = None,
    bank: Optional[MockBank] = None,
    audit: Optional[AuditLog] = None,
    ground_truth: Optional[dict] = None,
    settings: Settings = default_settings,
    llm: Optional[LLM] = None,
    settle: bool = True,
    case_id: Optional[str] = None,
    scenario_meta: Optional[dict] = None,
) -> tuple[dict, CaseContext]:
    case_id = case_id or f"FR-{time.strftime('%m%d')}-{uuid.uuid4().hex[:4].upper()}"
    ctx = CaseContext(
        case_id=case_id,
        store=store,
        memory=memory,
        settings=settings,
        llm=llm or LLM(settings),
        mode=mode,
        bus=bus,
        bank=bank or MockBank(),
        audit=audit,
        ground_truth=ground_truth or {},
        use_llm=(mode == "live" or settings.moot_use_llm),
    )
    ctx.cache["settle"] = settle
    ctx.cache["llm_before"] = dict(ctx.llm.describe())
    CONTEXTS[case_id] = ctx
    try:
        app = get_app(mode)
        inflows = store.in_txns(seed, alert_time - 3600, alert_time + 3600)
        outflows = store.out_txns(seed, alert_time - 3600, alert_time + 3600)
        capital = sum(t["amount"] for t in inflows) or sum(t["amount"] for t in outflows) or 0.0
        initial = {
            "case_id": case_id,
            "mode": mode,
            "seed": seed,
            "alert_time": alert_time,
            "alert_reason": reason,
            "capital_at_risk": capital,
            "evidence": [],
            "actions": [],
        }
        await ctx.emit("case.opened", {
            "case_id": case_id,
            "seed": seed,
            "alert_time": alert_time,
            "reason": reason,
            "capital": capital,
        })
        if settle and ctx.settings.settle_window > 0:
            await asyncio.sleep(ctx.settings.settle_window)
        config = {"configurable": {"thread_id": case_id}, "recursion_limit": 30}
        final = await app.ainvoke(initial, config=config)
        if mode == "live":
            final["dossier_path"] = str(save_dossier(build_dossier(final, ctx, scenario_meta)))
        return final, ctx
    finally:
        CONTEXTS.pop(case_id, None)
