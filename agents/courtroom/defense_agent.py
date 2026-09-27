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
    template = f"Defense brief defending legitimate accounts ({', '.join([k for k,v in positions.items() if v=='innocent']) or 'none'})."
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
    return {"defense": brief, "evidence": exculpatory_ev}


__all__ = ["defense", "DEFENSE", "exculpatory", "established"]
