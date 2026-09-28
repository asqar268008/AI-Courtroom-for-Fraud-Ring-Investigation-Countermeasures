"""Prosecutor Agent: argues the state's case for fraud based on strictly cited evidence."""
from __future__ import annotations

import re
from app.schemas import Brief, Evidence
from agents.agent_helpers import ctx_for, _source, _masker, _llm_text, PROSECUTOR


def build_prosecutor_rubric(ring: list[str], roles: dict, ev_list: list) -> str:
    origins = [a for a in ring if roles.get(a) == "origin"]
    cashouts = [a for a in ring if roles.get(a) == "cashout"]
    intermediaries = [a for a in ring if roles.get(a) == "intermediary"]

    ev_types = set()
    for e in ev_list:
        k = getattr(e, "kind", "") if hasattr(e, "kind") else (e.get("kind") if isinstance(e, dict) else "")
        if k == "shared_device": ev_types.add("shared device hardware fingerprints")
        elif k == "shared_ip": ev_types.add("co-located VPN/IP endpoints")
        elif k == "dwell": ev_types.add("rapid sub-minute pass-through velocity")
        elif k == "cycle": ev_types.add("closed circular fund routing")
        elif k == "fan_out": ev_types.add("structured multi-account smurfing")
        elif k == "burst": ev_types.add("anomalous inflow volume spikes")
        elif k == "genome_match": ev_types.add("syndicate pattern archetype match")

    ev_str = ", ".join(list(ev_types)[:3]) if ev_types else "multi-hop transactional velocity"
    origin_str = ", ".join(origins) if origins else (ring[0] if ring else "seed")
    cash_str = f" and {len(cashouts)} cash-out endpoint(s) ({', '.join(cashouts)})" if cashouts else ""
    core_targets = (cashouts + origins)[:4] or ring[:3]

    s1 = f"The Prosecution indicts an organized laundering syndicate centered on origin account {origin_str}{cash_str} across {len(ring)} coordinated accounts."
    s2 = f"Forensic analysis confirms structured fund layering substantiated by {ev_str} designed to bypass velocity controls and rapidly extract capital."
    s3 = f"We petition the Court for immediate Tier 4 hard freezes and STR filings against primary coordinators ({', '.join(core_targets)}), with Tier 3 outbound holds on pass-through mule intermediaries."
    return f"{s1} {s2} {s3}"


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
    template = build_prosecutor_rubric(ring, state.get("roles", {}), ev_list)
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
