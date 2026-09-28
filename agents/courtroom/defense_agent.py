"""Defense Agent: provides vigorous advocacy, exculpatory arguments, and objections."""
from __future__ import annotations

from app.schemas import Brief, Evidence
from agents.agent_helpers import ctx_for, _source, _masker, _llm_text, DEFENSE


def exculpatory(f: dict) -> bool:
    return (
        (f.get("age_days") or 0) >= 365
        and f.get("legit_cp", 0) >= 3
        and not f.get("shared_attr")
        and not f.get("in_cycle")
        and bool(f.get("amount_consistent", True))
    )


def established(f: dict) -> bool:
    return (
        (f.get("age_days") or 0) >= 365
        and f.get("recurring_ratio", 0.0) >= 0.8
        and not f.get("in_cycle")
    )


def build_defense_rubric(ring: list[str], positions: dict) -> str:
    innocent = [k for k, v in positions.items() if v == "innocent"]
    if innocent:
        innocent_str = ", ".join(innocent)
        s1 = f"The Defense conducted an exculpatory audit across all {len(ring)} accounts and identified legitimate commercial activity for {innocent_str} ({len(innocent)} account(s) demonstrating established operating history and verified counterparties)."
        s2 = "Imposing punitive Tier 4 blocking on these accounts would cause severe collateral damage to innocent commercial operations."
        s3 = f"We move to dismiss all punitive freezes against defended accounts ({innocent_str}), restricting any intervention to Tier 2 step-up verification."
        return f"{s1} {s2} {s3}"
    else:
        s1 = f"The Defense scrutinized all {len(ring)} flagged accounts for benign explanations, distinguishing unwitting pass-through counterparties from culpable ring coordinators."
        s2 = "While intermediary accounts exhibit rapid fund transmission, the state has not demonstrated that account holders acted with criminal intent rather than being compromised or coerced mules."
        s3 = "We request the Court mandate Tier 2 step-up authentication or temporary Tier 3 soft holds rather than irreversible Tier 4 hard freezes, allowing legitimate customers due process to verify transactions."
        return f"{s1} {s2} {s3}"


async def defense(state: dict) -> dict:
    ctx = ctx_for(state)
    await ctx.pause()
    await ctx.emit("stage.changed", {"stage": "defense"})
    ring, feats, ev_list = state["ring"], state["features"], state["evidence"]

    positions = {}
    objections = {}
    cited_ids = []
    exculpatory_ev: list[Evidence] = []

    for a in ring:
        f = feats.get(a, {})
        if exculpatory(f) or established(f):
            positions[a] = "innocent"
            ev = Evidence(
                id=f"EXC-{a}", kind="account_profile", nodes=[a],
                text=f"Account {a} is an established legitimate business ({f.get('age_days', 0):.0f} days old) with verified counterparties.",
                side="defense", source_tool="account_features",
            )
            exculpatory_ev.append(ev)
            cited_ids.append(ev.id)
            objections[a] = [e.id for e in ev_list if a in e.nodes]
        else:
            positions[a] = "unsure"

    for ev in exculpatory_ev:
        await ctx.emit("evidence.added", ev.model_dump())


    facts = {
        "ring": ring, "positions": positions,
        "exculpatory_evidence": [e.model_dump() for e in exculpatory_ev],
    }
    template = build_defense_rubric(ring, positions)
    summary, _, fallback = await _llm_text(ctx, DEFENSE, facts, template, _masker(state), Brief)

    brief = Brief(
        side="defense",
        summary=summary,
        positions=positions,
        cited_evidence=cited_ids,
        objections=objections,
        llm_fallback=fallback,
    )
    await ctx.emit("brief.filed", brief.model_dump())
    await ctx.emit("agent.message", {
        "agent": "defense", "stage": "defense",
        "text": summary, "source": _source(ctx, fallback),
        "evidence_ids": cited_ids,
    })
    return {"defense": brief, "evidence": exculpatory_ev}


__all__ = ["defense", "DEFENSE", "exculpatory", "established"]
