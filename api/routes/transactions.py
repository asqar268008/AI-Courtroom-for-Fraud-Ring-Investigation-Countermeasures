"""Transactions and Simulation Routes."""
from __future__ import annotations

import asyncio
import random
from typing import Optional
from fastapi import APIRouter, HTTPException

from app.config import settings
from app.dependencies import rt
from app.schemas import TxnIn
from agents.investigation.transaction_agent import score_txn
from agents.investigation.network_agent import inr, ist_clock
from core.scenarios import SCENARIOS, build_scenario, catalog, random_scenario

router = APIRouter()


async def ingest(txn: dict, ambient: bool = False) -> Optional[str]:
    base = {
        "txn_id": txn["txn_id"], "src": txn["src"], "dst": txn["dst"], "amount": txn["amount"],
        "amount_label": inr(txn["amount"]), "clock": ist_clock(txn["ts"]), "rail": txn.get("rail", "UPI"),
        "ambient": ambient
    }
    if rt.bank.get(txn["src"])["tier"] >= 3:
        rt.stats["held"] += 1
        await rt.bus.publish({"case_id": None, "type": "txn.ingested", "t": 0, "payload": {
            **base, "risk": 1.0, "reasons": ["sender's outbound transfers are on hold"], "status": "held"}})
        return None
    risk, reasons = score_txn(rt.store, {"src": txn["src"], "dst": txn["dst"], "amount": txn["amount"], "ts": txn["ts"]})
    stored = rt.store.add_txn(txn["txn_id"], txn["ts"], txn["src"], txn["dst"], txn["amount"], txn.get("rail", "UPI"))
    rt.stats["monitored"] += 1
    fired = rt.trigger.check(rt.store, stored)
    linked = rt.watch.get(stored["src"], -1) >= stored["ts"] or rt.watch.get(stored["dst"], -1) >= stored["ts"]
    opens = bool(fired and not rt.linked_to_open_case(fired[0], stored["ts"]))
    status = "alert" if opens else "investigating" if linked else "flagged" if risk >= 0.6 else "allowed"
    if risk >= 0.6 or opens:
        rt.stats["suspicious"] += 1
    await rt.bus.publish({"case_id": None, "type": "txn.ingested", "t": 0, "payload": {
        **base, "risk": risk, "reasons": reasons, "status": status}})
    if opens:
        seed, reason = fired
        rt.watch[seed] = stored["ts"] + 3600
        await rt.bus.publish({"case_id": None, "type": "alert.raised", "t": 0, "payload": {
            "seed": seed, "reason": reason, "risk": max(risk, 0.9 if "velocity" in reason else risk),
            "txn": {**base, "risk": risk}}})
        from api.routes.investigations import investigate
        task = asyncio.create_task(investigate(seed, stored["ts"], reason))
        rt.case_tasks.add(task)
        task.add_done_callback(rt.case_tasks.discard)
        return seed
    return None


@router.post("/txns")
@router.post("/transactions/ingest")
async def ingest_endpoint(t: TxnIn):
    seed = await ingest({
        "txn_id": t.txn_id, "ts": t.timestamp, "src": t.from_account,
        "dst": t.to_account, "amount": t.amount, "rail": t.rail,
    })
    return {"status": "ok", "alert_opened_for": seed, "opened_case": seed is not None, "seed": seed}


@router.get("/scenarios")
def get_scenarios():
    return catalog()


async def _run_demo(speed: float, scenario_name: str, seed: Optional[int] = None):
    rng = random.Random(seed) if seed is not None else None
    if scenario_name == "random":
        sc = random_scenario(rng)
    elif scenario_name in SCENARIOS:
        sc = build_scenario(scenario_name, rng=rng)
    else:
        rt.load_demo()
        sc = None
    if sc:
        rt.load_scenario(sc)
    rt.stats = {"monitored": 0, "suspicious": 0, "held": 0}
    await rt.bus.publish({"case_id": None, "type": "demo.loaded", "t": 0, "payload": {
        "meta": rt.meta, "txns_count": len(rt.live_txns)}})
    if not rt.live_txns:
        return
    t0 = rt.live_txns[0]["ts"]
    wall0 = asyncio.get_event_loop().time()
    for txn in list(rt.live_txns):
        dt = (txn["ts"] - t0) / speed
        target = wall0 + dt
        now = asyncio.get_event_loop().time()
        if target > now:
            await asyncio.sleep(target - now)
        await ingest(txn)
    if rt.case_tasks:
        await asyncio.gather(*list(rt.case_tasks), return_exceptions=True)
    await rt.bus.publish({"case_id": None, "type": "demo.finished", "t": 0, "payload": {}})


@router.post("/demo/run")
async def demo_run(speed: float = 40.0, scenario: str = "demo_3am", seed: Optional[int] = None):
    if rt.demo_task and not rt.demo_task.done():
        rt.demo_task.cancel()
    rt.demo_task = asyncio.create_task(_run_demo(speed, scenario, seed))
    return {"status": "started", "speed": speed, "scenario": scenario}


@router.post("/demo/reset")
async def demo_reset():
    if rt.demo_task and not rt.demo_task.done():
        rt.demo_task.cancel()
    rt.bank.reset()
    rt.load_demo()
    await rt.bus.publish({"case_id": None, "type": "demo.reset", "t": 0, "payload": {}})
    return {"status": "reset"}
