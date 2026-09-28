"""Countermeasures Agent: executes judicial rulings against banking APIs or queues for human review."""
from __future__ import annotations

import time
from app.schemas import CourtVerdict, TIER_ACTIONS
from agents.agent_helpers import ctx_for, CUSTOMER, D
from agents.countermeasures.reporting_agent import build_sar, write_sar


def customer_template(tier: int, amount: float) -> str:
    if tier == 2:
        return (
            f"Hello from your bank. A payment of {D.inr(amount)} reached your account from an account we are "
            "checking. Your account is not blocked. To keep using it normally, please confirm it's you in the "
            "app with your fingerprint or face. Questions? Call your branch."
        )
    return (
        "Hello from your bank. We've paused outgoing transfers from your account while we check some payments "
        "you received. You can still receive money. Please call us or visit your branch with ID so we can help "
        "you quickly."
    )


async def countermeasure(state: dict, approve_tiers: set[int] | None = None, exclude: set[str] | None = None) -> dict:
    ctx = ctx_for(state)
    await ctx.pause()
    await ctx.emit("stage.changed", {"stage": "action"})
    verdict: CourtVerdict = state["verdict"]
    exclude = exclude or set()

    actions = []
    for tier in (4, 3, 2):
        if approve_tiers is not None and tier not in approve_tiers:
            continue
        accounts = [v.node_id for v in verdict.node_verdicts if v.tier == tier and v.node_id not in exclude]
        if not accounts:
            continue
        res = ctx.bank.apply(tier, accounts, ctx.case_id, reason=f"Court order: Tier {tier} ({TIER_ACTIONS.get(tier)})")
        ctx.audit.append(ctx.case_id, f"COUNTERMEASURE_TIER_{tier}", {"accounts": accounts, "bank_response": res})
        act_entry = {
            "tier": tier, "action": TIER_ACTIONS.get(tier), "accounts": accounts,
            "endpoint": res["endpoint"], "status": res["status"],
        }
        actions.append(act_entry)
        await ctx.emit("action.executed", act_entry)

    # File SAR if hard freeze or tier 3+ occurred
    sar_path = ""
    frozen = [v for v in verdict.node_verdicts if v.tier >= 3 and v.node_id not in exclude]
    if frozen:
        sar = build_sar(ctx.case_id, state, [], ctx.audit.verify_chain()[0] and "CHAIN_OK")
        jpath, _ = write_sar(sar)
        sar_path = str(jpath)
        ctx.audit.append(ctx.case_id, "SAR_FILED", {"path": sar_path, "subjects": [v.node_id for v in frozen]})

    await ctx.emit("case.closed", {
        "case_id": ctx.case_id, "status": "neutralized" if actions else "cleared",
        "actions": actions, "sar_path": sar_path,
    })
    return {"actions": actions, "sar_path": sar_path}


async def human_review(state: dict) -> dict:
    ctx = ctx_for(state)
    await ctx.pause()
    await ctx.emit("stage.changed", {"stage": "human_review"})
    verdict: CourtVerdict = state["verdict"]
    actions_needed = any(v.tier >= 2 for v in verdict.node_verdicts)
    status = "awaiting_review" if actions_needed else "cleared"
    reason = "policy: employee mode requires review for restrictive actions" if actions_needed else "cleared: no restrictive tiers"

    await ctx.emit("case.closed", {
        "case_id": ctx.case_id, "status": status,
        "reason": reason, "verdict": verdict.model_dump(),
    })
    return {"actions": []}


__all__ = ["countermeasure", "human_review", "customer_template", "CUSTOMER"]
