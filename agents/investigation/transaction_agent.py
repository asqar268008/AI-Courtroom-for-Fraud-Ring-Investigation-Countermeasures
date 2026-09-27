"""Transaction Investigation Agent: scores velocity, amount anomalies, and seeds orchestrator flow."""
from __future__ import annotations

import statistics
from typing import Optional
from database.graph_store import GraphStore
from agents.agent_helpers import ctx_for, _source, D

DAY = 86400.0
LAKH = 100000.0


def score_txn(store: GraphStore, txn: dict) -> tuple[float, list[str]]:
    """Real-time transaction risk scoring based on velocity, amount, and sender profile."""
    src, dst, amt, now = txn["src"], txn["dst"], txn["amount"], txn["ts"]
    score, why = 0.03, []
    inflow = sum(t["amount"] for t in store.in_txns(src, now - 600, now))
    if inflow >= 0.5 * amt and amt >= LAKH:
        s = 0.35 * min(1.0, inflow / max(amt, 1.0))
        score += s
        why.append("forwards money received minutes ago")
    outs = store.out_txns(src, now - 120, now)
    if len(outs) >= 3:
        score += 0.2
        why.append(f"{len(outs)} transfers in 2 minutes")
    elif len(outs) == 2:
        score += 0.08
    prior = [t for t in store.out_txns(src, now - 90 * DAY, now - 1) if t["dst"] == dst]
    if not prior:
        score += 0.08
        why.append("first payment to this recipient")
    age = store.age_days(src, now)
    if age is not None and age < 90:
        score += 0.15
        why.append(f"sender account {age:.0f} days old")
    history = [t["amount"] for t in store.out_txns(src, now - 90 * DAY, now - 600)]
    typical = statistics.median(history) if history else 0.0
    if amt >= LAKH and (not history or amt > 5 * typical):
        score += 0.15
        why.append("far above the sender's usual amount")
    if amt >= 5 * LAKH:
        score += 0.05
    return round(min(0.99, score), 2), why


async def orchestrator(state: dict) -> dict:
    ctx = ctx_for(state)
    await ctx.pause()
    await ctx.emit("stage.changed", {"stage": "orchestrator"})
    seed = state["seed"]
    capital = sum(t["amount"] for t in ctx.store.out_txns(seed, state["alert_time"] - 3600, state["alert_time"] + 3600))
    await ctx.emit("agent.tool", {
        "agent": "orchestrator", "tool": "seed_triage",
        "input": {"seed": seed, "alert_time": state["alert_time"], "reason": state.get("alert_reason", "")},
        "output": {"capital_at_risk": capital, "capital_at_risk_label": D.inr(capital)},
    })
    return {"capital_at_risk": capital}


__all__ = ["orchestrator", "score_txn"]
