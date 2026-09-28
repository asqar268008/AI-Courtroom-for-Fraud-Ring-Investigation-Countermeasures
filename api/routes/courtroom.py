"""Courtroom Review, Overrides and Decision routes."""
from __future__ import annotations

import time
from fastapi import APIRouter, HTTPException

from app.config import settings
from app.dependencies import rt
from app.schemas import ReviewIn, OverrideIn
from agents.countermeasures.countermeasure_agent import countermeasure
from services.report_service import build_dossier, save_dossier, load_dossier
from agents.investigation.network_agent import precedent_vector

router = APIRouter()


@router.post("/review/{case_id}")
@router.post("/cases/{case_id}/review")
async def review_case(case_id: str, body: ReviewIn):
    final = None
    ctx = None
    if case_id in rt.finals:
        final, ctx = rt.finals[case_id]

    declined = []
    approved_tiers = set(body.approve_tiers) if body.approve_tiers is not None else {2, 3, 4}
    exclude = set(body.exclude)

    if final and ctx:
        ctx.cache["employee_note"] = body.note
        ctx.cache["employee_exclude"] = exclude

        proposed = {v.node_id: v.tier for v in final["verdict"].node_verdicts}
        for acc, t in proposed.items():
            if t >= 2:
                if acc in exclude:
                    declined.append({"account": acc, "proposed_tier": t, "reason": "excluded_by_employee"})
                elif t not in approved_tiers:
                    declined.append({"account": acc, "proposed_tier": t, "reason": "tier_not_approved"})

        if body.decision == "approve":
            res = await countermeasure(final, approve_tiers=approved_tiers, exclude=exclude)
            final["actions"] = res.get("actions", [])
            final["sar_path"] = res.get("sar_path", "")
            save_dossier(build_dossier(final, ctx, rt.meta, status="approved"))
            await rt.bus.publish({
                "case_id": case_id, "type": "case.closed", "t": time.time(),
                "payload": {
                    "case_id": case_id, "actions": final["actions"], "status": "approved",
                    "note": body.note, "declined": declined,
                },
            })
            rt.finals.pop(case_id, None)
            return {"status": "approved", "actions": final["actions"], "declined": declined, "case_id": case_id}
        else:
            save_dossier(build_dossier(final, ctx, rt.meta, status="dismissed"))
            await rt.bus.publish({
                "case_id": case_id, "type": "case.closed", "t": time.time(),
                "payload": {"case_id": case_id, "actions": [], "status": "dismissed", "note": body.note},
            })
            rt.finals.pop(case_id, None)
            return {"status": "dismissed", "actions": [], "declined": declined, "case_id": case_id}

    # Case is not in memory; fall back to loading from persisted dossier on disk
    d = load_dossier(case_id)
    if not d:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

    node_verdicts = (d.get("verdict") or {}).get("node_verdicts", [])
    proposed = {nv.get("node_id"): nv.get("tier", 0) for nv in node_verdicts}

    for acc, t in proposed.items():
        if t >= 2:
            if acc in exclude:
                declined.append({"account": acc, "proposed_tier": t, "reason": "excluded_by_employee"})
            elif t not in approved_tiers:
                declined.append({"account": acc, "proposed_tier": t, "reason": "tier_not_approved"})

    actions = []
    if body.decision == "approve":
        for tier in (4, 3, 2):
            if tier not in approved_tiers:
                continue
            accounts = [nv["node_id"] for nv in node_verdicts if nv.get("tier") == tier and nv["node_id"] not in exclude]
            if not accounts:
                continue
            res = rt.bank.apply(tier, accounts, case_id, reason=f"Court order: Tier {tier} ({TIER_ACTIONS.get(tier, 'Action')})")
            rt.audit.append(case_id, f"COUNTERMEASURE_TIER_{tier}", {"accounts": accounts, "bank_response": res})
            act_entry = {
                "tier": tier, "action": TIER_ACTIONS.get(tier, "Action"), "accounts": accounts,
                "endpoint": res.get("endpoint", ""), "status": res.get("status", "ok"),
            }
            actions.append(act_entry)
            await rt.bus.publish({"case_id": case_id, "type": "action.executed", "t": time.time(), "payload": act_entry})

        d["status"] = "approved"
        d["actions"] = actions
        d["approval"] = {"decision": "approve", "note": body.note, "declined": declined}
        save_dossier(d)

        await rt.bus.publish({
            "case_id": case_id, "type": "case.closed", "t": time.time(),
            "payload": {
                "case_id": case_id, "actions": actions, "status": "approved",
                "note": body.note, "declined": declined,
            },
        })
        rt.finals.pop(case_id, None)
        return {"status": "approved", "actions": actions, "declined": declined, "case_id": case_id}
    else:
        d["status"] = "dismissed"
        d["actions"] = []
        d["approval"] = {"decision": "dismiss", "note": body.note, "declined": []}
        save_dossier(d)
        rt.audit.append(case_id, "CASE_DISMISSED", {"note": body.note})

        await rt.bus.publish({
            "case_id": case_id, "type": "case.closed", "t": time.time(),
            "payload": {"case_id": case_id, "actions": [], "status": "dismissed", "note": body.note},
        })
        rt.finals.pop(case_id, None)
        return {"status": "dismissed", "actions": [], "declined": declined, "case_id": case_id}


@router.post("/override/{case_id}")
@router.post("/override")
async def manual_override(body: OverrideIn, case_id: str = "manual"):
    acc, dec = body.account, body.decision
    if dec == "release":
        res = rt.bank.unfreeze(acc, by="analyst_override")
        rt.audit.log("OVERRIDE_RELEASE", {"account": acc, "case_id": case_id, "note": body.note})
    else:
        res = rt.bank.apply(4, [acc], case_id, reason=body.note or "analyst confirmed suspicion")
        rt.audit.log("OVERRIDE_CONFIRM", {"account": acc, "case_id": case_id, "note": body.note})
    
    # Precedent generation
    vec = [0.3] * 11
    d = load_dossier(case_id)
    if d and "features" in d and acc in d["features"]:
        try:
            vec = precedent_vector(d["features"][acc])
        except Exception:
            pass
            
    pid = rt.memory.add_precedent(
        side="defense" if dec == "release" else "prosecution",
        vec=vec,
        lesson=body.note or f"analyst override {dec}",
        case_type="analyst_override",
        round_no=0,
        example=f"{acc}: {dec}",
    )
    rt.memory.save()
    
    await rt.bus.publish({
        "case_id": case_id, "type": "account.overridden", "t": time.time(),
        "payload": {"account": acc, "decision": dec, "note": body.note, "status": rt.bank.get(acc)},
    })
    return {"status": "ok", "account": acc, "decision": dec, "bank": res, "precedent": pid}
