"""Reports and Audit queries."""
from __future__ import annotations

import json
from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse
from app.dependencies import rt
from agents.countermeasures.reporting_agent import SAR_DIR
from agents.moot_court.grader import load_metrics
from services.report_service import load_dossier, list_dossiers, render, moot_report_html, available

router = APIRouter()


@router.get("/reports")
def get_portfolio_reports():
    return {
        "reports": [
            {"kind": "moot", "title": "Moot Court Training Report", "url": "/api/reports/portfolio/moot"},
            {"kind": "scorecard", "title": "Scenario Evaluation Scorecard", "url": "/api/reports/portfolio/scorecard"},
        ]
    }


@router.get("/reports/portfolio/moot")
def get_moot_portfolio_report():
    m = load_metrics()
    if not m:
        return HTMLResponse("<h1>Moot court training report</h1><p>No training runs yet.</p>")
    return HTMLResponse(moot_report_html(m))


@router.get("/reports/{case_id}")
def get_case_reports_list(case_id: str):
    d = load_dossier(case_id)
    if not d:
        raise HTTPException(status_code=404, detail="case dossier not found")
    return {"case_id": case_id, "reports": available(d)}


@router.get("/reports/{case_id}/{kind}")
def get_report(case_id: str, kind: str):
    d = load_dossier(case_id)
    if not d:
        raise HTTPException(status_code=404, detail="case dossier not found")
    data, ctype, filename = render(d, kind)
    if ctype.startswith("text/html"):
        return HTMLResponse(data.decode("utf-8"))
    if ctype.startswith("application/json"):
        return JSONResponse(content=json.loads(data.decode("utf-8")) if isinstance(data, (bytes, str)) else data)
    return Response(content=data, media_type=ctype, headers={"Content-Disposition": f'inline; filename="{filename}"'})


@router.get("/dossiers")
def get_dossiers(limit: int = 50):
    return list_dossiers(limit=limit)


@router.get("/audit")
def get_audit(limit: int = 100):
    entries = rt.audit.read_all()[-limit:]
    chain_ok, broken = rt.audit.verify_chain()
    return {"chain_ok": chain_ok, "broken_at": broken, "count": len(entries), "entries": entries}


@router.get("/audit/verify")
def get_audit_verify():
    chain_ok, broken = rt.audit.verify_chain()
    return {"chain_intact": chain_ok, "broken_at": broken}
