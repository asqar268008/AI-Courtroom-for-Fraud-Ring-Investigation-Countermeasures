"""Judge Agent: weighs Prosecution vs Defense, evaluates counterfactuals, and rules on Tier actions."""
from __future__ import annotations

import re
from app.schemas import CourtVerdict, NodeVerdict, Evidence, Brief, TIER_ACTIONS
from agents.agent_helpers import ctx_for, _source, _masker, _llm_text, JUDGE
from core.decision import counterfactual, choose_tier, action_name, collateral_blocks


def route_after_judge(state: dict) -> str:
    verdict: CourtVerdict = state["verdict"]
    if state.get("mode") == "moot":
        return "grade"
    if ctx_for(state).settings.approval_mode == "employee":
        return "review"
    if verdict.confidence >= 0.85 and verdict.counterfactual_ok:
        return "act"
    return "review"


async def judge(state: dict) -> dict:
    ctx = ctx_for(state)
    await ctx.pause()
    await ctx.emit("stage.changed", {"stage": "judge"})

    ring = state["ring"]
    pros: Brief = state["prosecution"]
    defn: Brief = state["defense"]
    feats = state["features"]
    risk = state["risk"]
    roles = state["roles"]
    ev_list = state["evidence"]

    upheld = [a for a, pos in defn.positions.items() if pos == "innocent"]
    seed = state.get("seed")
    is_innocent_cluster = bool(ring) and (
        len(upheld) == len(ring)
        or (seed in upheld and len(upheld) >= max(1, len(ring) - 1))
    )

    all_cf_ok = True
    node_verdicts: list[NodeVerdict] = []
    for a in ring:
        p = risk.get(a, 0.5)
        role = roles.get(a, "mule")
        cf = counterfactual(ctx.store, a, set(ring), set(state.get("sources", [state["seed"]])), state["alert_time"])
        has_collateral = collateral_blocks(cf)

        if is_innocent_cluster:
            tier = 0
            p = min(p, 0.05)
        elif a in upheld:
            tier = 2
            p = min(p, 0.40)
        else:
            tier, _ = choose_tier(p=p, role=role, exposure=state.get("capital_at_risk", 100000.0), cf=cf, max_tier=ctx.settings.max_tier)

        if tier >= 3 and has_collateral:
            all_cf_ok = False

        cited = [e.id for e in ev_list if a in e.nodes]
        nv = NodeVerdict(
            node_id=a, role=role, p_fraud=round(p, 2),
            tier=tier, action=TIER_ACTIONS.get(tier, "Observe"),
            rationale=f"Role {role} assigned Tier {tier} ({TIER_ACTIONS.get(tier)}) based on risk p={p:.2f}.",
            cited_evidence=cited,
        )
        node_verdicts.append(nv)

    all_cited = list(set(pros.cited_evidence + defn.cited_evidence))
    confidence = 0.95 if is_innocent_cluster else (0.92 if not defn.objections else (0.88 if len(upheld) <= 1 else 0.75))
    template = f"Court verdict reached with confidence {confidence:.2f}. Actions: {', '.join([f'{nv.node_id}: Tier {nv.tier}' for nv in node_verdicts])}."

    facts = {
        "verdict": [nv.model_dump() for nv in node_verdicts],
        "prosecution": pros.summary,
        "defense": defn.summary,
        "upheld": upheld,
    }
    summary, _, fallback = await _llm_text(ctx, JUDGE, facts, template, _masker(state), judge=True)

    verdict = CourtVerdict(
        summary=summary,
        node_verdicts=node_verdicts,
        confidence=confidence,
        counterfactual_ok=all_cf_ok,
        upheld_objections=upheld,
        cited_evidence=all_cited,
        llm_fallback=fallback,
    )
    await ctx.emit("verdict.issued", verdict.model_dump())
    await ctx.emit("agent.message", {
        "agent": "judge", "stage": "verdict",
        "text": summary, "source": _source(ctx, fallback, judge=True),
        "evidence_ids": all_cited,
    })
    route = route_after_judge({**state, "verdict": verdict})
    return {"verdict": verdict, "route": route}


__all__ = ["judge", "route_after_judge", "JUDGE"]
