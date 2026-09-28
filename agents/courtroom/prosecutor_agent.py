"""Prosecutor Agent: argues the state's case for fraud based on strictly cited evidence."""
from __future__ import annotations

import re
from app.schemas import Brief, Evidence
from agents.agent_helpers import ctx_for, _source, _masker, _llm_text, PROSECUTOR


async def prosecutor(state: dict) -> dict:
    ctx = ctx_for(state)
    await ctx.pause()
    await ctx.emit("stage.changed", {"stage": "prosecutor"})
    ring, feats, ev_list = state["ring"], state["features"], state["evidence"]
    cited_ids = [e.id for e in ev_list]
    positions = {a: "fraud" for a in ring}

    facts = {
        "ring": ring, "roles": state["roles"],
        "evidence": [e.model_dump() for e in ev_list],
    }
    template = f"Indictment against ring {', '.join(ring)} based on {len(cited_ids)} verified evidentiary facts."
    summary, parsed, fallback = await _llm_text(ctx, PROSECUTOR, facts, template, _masker(state), Brief)

    if not fallback:
        # Check if LLM cited hallucinated evidence IDs
        found_ev = re.findall(r"\b(?:EV-[\w-]+|[A-Z]{3}-\d+|DWL-[\w-]+|EXC-[\w-]+)\b", summary)
        for cid in found_ev:
            if cid not in cited_ids:
                fallback = True
                summary = template
                break

    brief = Brief(
        side="prosecution",
        summary=summary,
        positions=positions,
        cited_evidence=cited_ids,
        llm_fallback=fallback,
    )
    await ctx.emit("brief.filed", brief.model_dump())
    await ctx.emit("agent.message", {
        "agent": "prosecutor", "stage": "prosecution",
        "text": summary, "source": _source(ctx, fallback),
        "evidence_ids": cited_ids,
    })
    return {"prosecution": brief}


__all__ = ["prosecutor", "PROSECUTOR"]
