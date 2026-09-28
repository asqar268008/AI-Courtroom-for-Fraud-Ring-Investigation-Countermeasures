"""Case dossier and report generator.

Every live case is saved as a JSON dossier (runtime/cases/<case_id>.json). All reports are rendered
from that dossier, so they can be regenerated at any time and never disagree with each other.

Report types (GET /api/reports/<case_id>/<kind>):
  investigation   full investigation report (HTML, printable): story, network, evidence, arguments,
                  verdict per account, actions, customer notices, audit trail, LLM provenance
  executive       one-page executive summary (PDF)
  review          analyst review packet (HTML) â€” why the court was unsure and what to check
  str             Suspicious Transaction Report draft (PDF/JSON, only when accounts were frozen)
  notices         customer notices per account group (HTML), incl. the no-tipping-off rule
  audit           audit trail with hash chain (CSV)
  dossier         the raw dossier (JSON) for other systems
Portfolio reports (not per case): moot-court training report, scenario scorecard, dataset validation.
"""
from __future__ import annotations

import csv
import html
import io
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import RUNTIME_DIR
from agents.investigation.network_agent import IST_OFFSET, inr, lakh

CASES_DIR = RUNTIME_DIR / "cases"
REPORTS_DIR = RUNTIME_DIR / "reports"
for _d in (CASES_DIR, REPORTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

TIER_NAME = {4: "Hard freeze + STR", 3: "Soft freeze", 2: "Step-up check", 1: "Alert only", 0: "Cleared"}
TIER_COLOR = {4: "#B3261E", 3: "#D9822B", 2: "#0F766E", 1: "#64748B", 0: "#2E7D32"}
STATUS_TEXT = {"neutralized": "Ring neutralised automatically", "cleared": "Cleared â€” no ring proven",
               "awaiting_review": "Awaiting analyst review", "approved": "Approved by analyst and executed",
               "dismissed": "Dismissed by analyst"}
e = html.escape


def ist(ts: float, fmt: str = "%d %b %Y, %H:%M:%S IST") -> str:
    return datetime.fromtimestamp(ts + IST_OFFSET, timezone.utc).strftime(fmt)


# ----------------------------------------------------------------------------------------
# Dossier
# ----------------------------------------------------------------------------------------
def _dump(obj):
    return obj.model_dump() if hasattr(obj, "model_dump") else obj


def build_dossier(final: dict, ctx, scenario_meta: Optional[dict] = None, status: Optional[str] = None) -> dict:
    rec = ctx.recording
    verdict = _dump(final.get("verdict")) or {}
    tiers = {v["node_id"]: v["tier"] for v in verdict.get("node_verdicts", [])}
    trace = ctx.cache.get("structure", {}).get("trace", {})
    net: dict[str, float] = {}
    for t in trace.get("edges", []):
        net[t["dst"]] = net.get(t["dst"], 0) + t["amount"]
        net[t["src"]] = net.get(t["src"], 0) - t["amount"]
    if final.get("seed"):
        net[final["seed"]] = net.get(final["seed"], 0) + trace.get("capital", 0)
    held = sum(max(0.0, net.get(a, 0)) for a, t in tiers.items() if t >= 3)
    sources = {}
    for ev in rec:
        if ev["type"] == "agent.message":
            sources[ev["payload"]["agent"]] = ev["payload"].get("source", "code")
    closed = next((ev["payload"] for ev in reversed(rec) if ev["type"] == "case.closed"), {})
    if status is None:
        status = closed.get("status", "awaiting_review" if final.get("route") == "review" else "neutralized")
    llm_before = ctx.cache.get("llm_before", {})
    llm_now = ctx.llm.describe()
    llm = {k: (llm_now[k] - llm_before.get(k, 0)) if isinstance(llm_now.get(k), (int, float)) and k in llm_before
           else llm_now[k] for k in llm_now}
    truth = (scenario_meta or {}).get("ground_truth") or ctx.ground_truth or {}
    grading = None
    if truth:
        grading = {
            "missed": [a for a, f in truth.items() if f and tiers.get(a, 0) <= 2],
            "wrongful": [a for a, f in truth.items() if not f and tiers.get(a, 0) >= 3],
            "correct": [a for a, f in truth.items() if (f and tiers.get(a, 0) >= 3) or (not f and tiers.get(a, 0) <= 2)],
            "truth": truth,
        }
    meta = {k: v for k, v in (scenario_meta or {}).items() if k not in ("ground_truth",)}
    return {
        "case_id": final["case_id"], "generated_at": time.time(), "status": status,
        "scenario": meta, "route": final.get("route"),
        "alert": {"seed": final.get("seed"), "time": final.get("alert_time"), "reason": final.get("alert_reason", ""),
                  "capital": final.get("capital_at_risk", 0.0)},
        "duration_s": closed.get("duration_s", ctx.elapsed()),
        "ring": final.get("ring", []), "roles": final.get("roles", {}), "sources": final.get("sources", []),
        "risk": final.get("risk", {}), "genome": {k: v for k, v in (final.get("genome") or {}).items() if k != "vector"},
        "evidence": [_dump(x) for x in final.get("evidence", [])],
        "prosecution": _dump(final.get("prosecution")), "defense": _dump(final.get("defense")), "verdict": verdict,
        "text_sources": sources, "actions": final.get("actions", []), "sar_path": final.get("sar_path", ""),
        "funds_held": round(held, 2),
        "customer_messages": [ev["payload"] for ev in rec if ev["type"] == "customer.message"],
        "timeline": [{"t": ev["t"], "type": ev["type"], "payload": _brief_payload(ev)} for ev in rec
                     if ev["type"] in ("case.opened", "stage.changed", "agent.message", "verdict.issued",
                                       "action.executed", "case.closed")],
        "graph": final.get("display_graph", {}), "llm": llm,
        "audit": ctx.audit.for_case(final["case_id"]) if ctx.audit else [],
        "grading": grading, "history": [],
    }


def _brief_payload(ev: dict) -> dict:
    p = ev["payload"]
    if ev["type"] == "agent.message":
        return {"agent": p["agent"], "text": p["text"][:280], "source": p.get("source", "code")}
    if ev["type"] == "verdict.issued":
        return {"confidence": p.get("confidence"), "route": p.get("route")}
    return {k: v for k, v in p.items() if k in ("stage", "seed", "reason", "endpoint", "accounts", "status", "tier",
                                              "capital_label", "detail")}


def save_dossier(d: dict) -> Path:
    path = CASES_DIR / f"{d['case_id']}.json"
    path.write_text(json.dumps(d, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return path


def load_dossier(case_id: str) -> Optional[dict]:
    path = CASES_DIR / f"{Path(case_id).name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def list_dossiers(limit: int = 50) -> list[dict]:
    out = []
    for p in sorted(CASES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        d = json.loads(p.read_text(encoding="utf-8"))
        v = d.get("verdict") or {}
        capital = (d.get("alert") or {}).get("capital", 0.0) or d.get("capital_at_risk", 0.0) or d.get("funds_held", 0.0)
        out.append({"case_id": d["case_id"], "status": d["status"], "title": d["scenario"].get("title", ""),
                    "kind": d["scenario"].get("kind", ""), "seed": d["alert"]["seed"], "capital": capital,
                    "confidence": v.get("confidence"), "frozen": sum(1 for n in v.get("node_verdicts", []) if n.get("tier", 0) >= 3),
                    "accounts": len(d.get("ring", [])), "generated_at": d["generated_at"],
                    "duration_s": d.get("duration_s", 0.0)})
    return out


def available(d: dict) -> list[dict]:
    kinds = [("investigation", "Investigation report", "html"), ("executive", "Executive summary", "pdf"),
             ("notices", "Customer notices", "html"), ("audit", "Audit trail", "csv"), ("dossier", "Case dossier", "json")]
    if d["status"] in ("awaiting_review", "dismissed", "approved") or d["route"] == "review":
        kinds.insert(1, ("review", "Analyst review packet", "html"))
    if any(n["tier"] >= 3 for n in (d.get("verdict") or {}).get("node_verdicts", [])) and d.get("sar_path"):
        kinds.insert(1, ("str", "Suspicious Transaction Report (draft)", "pdf"))
    return [{"kind": k, "title": t, "format": f, "url": f"/api/reports/{d['case_id']}/{k}"} for k, t, f in kinds]


def render(d: dict, kind: str) -> tuple[bytes, str, str]:
    """Returns (content, media_type, filename)."""
    cid = d["case_id"]
    if kind == "investigation":
        return investigation_html(d).encode(), "text/html", f"{cid}-investigation.html"
    if kind == "review":
        return review_html(d).encode(), "text/html", f"{cid}-review-packet.html"
    if kind == "notices":
        return notices_html(d).encode(), "text/html", f"{cid}-customer-notices.html"
    if kind == "executive":
        try:
            return executive_pdf(d), "application/pdf", f"{cid}-executive-summary.pdf"
        except Exception:  # reportlab missing or font trouble: fall back to printable HTML
            return investigation_html(d, executive=True).encode(), "text/html", f"{cid}-executive-summary.html"
    if kind == "audit":
        return audit_csv(d).encode(), "text/csv", f"{cid}-audit-trail.csv"
    if kind == "dossier":
        return json.dumps(d, ensure_ascii=False, indent=1, default=str).encode(), "application/json", f"{cid}.json"
    if kind == "str":
        path = Path(d.get("sar_path") or "")
        if path.exists():
            return path.read_bytes(), "application/pdf" if path.suffix == ".pdf" else "application/json", path.name
        raise FileNotFoundError("No STR for this case")
    raise KeyError(kind)


# ----------------------------------------------------------------------------------------
# Shared HTML pieces
# ----------------------------------------------------------------------------------------
CSS = """
:root{--ink:#1c1b19;--muted:#6b6760;--line:#dcd7cc;--paper:#fbfaf6;--red:#B3261E;--teal:#0F766E;--navy:#1F3A5F}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 Georgia,'Times New Roman',serif}
main{max-width:980px;margin:0 auto;padding:36px 28px 60px}h1{font-size:28px;margin:0 0 4px}h2{font-size:19px;margin:34px 0 10px;
padding-bottom:6px;border-bottom:1px solid var(--line)}h3{font-size:15px;margin:14px 0 4px}.sub{color:var(--muted);font-size:13px}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{font-weight:600;color:var(--muted);font-size:12px;
text-transform:uppercase;letter-spacing:.04em}.kpis{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0}
.kpi{flex:1 1 170px;margin:0 6px 6px 0;border:1px solid var(--line);border-radius:8px;padding:10px 12px;background:#fff}.kpi b{display:block;font-size:22px}
.kpi span{font-size:12px;color:var(--muted)}.pill{display:inline-block;white-space:nowrap;border-radius:999px;padding:1px 9px;font-size:12px;color:#fff}
.box{border:1px solid var(--line);border-left:4px solid var(--navy);border-radius:6px;background:#fff;padding:10px 14px;margin:8px 0}
.box.pros{border-left-color:var(--red)}.box.def{border-left-color:var(--teal)}.src{float:right;font:11px ui-monospace,monospace;color:var(--muted)}
.status{font-size:14px;padding:6px 12px;border-radius:6px;display:inline-block;margin-top:8px;background:#EEF3F8;color:var(--navy)}
.warn{background:#FFF4E5;border:1px solid #F0C27B;border-radius:6px;padding:10px 14px}.ok{color:#2E7D32}.bad{color:var(--red)}
svg{max-width:100%;height:auto;background:#fff;border:1px solid var(--line);border-radius:8px}
footer{margin-top:40px;color:var(--muted);font-size:12px}
@media print{body{background:#fff}main{padding:0}.noprint{display:none}}

"""


def page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>{e(title)}</title><style>{CSS}</style></head>"
            f"<body><main>{body}<footer>Generated by the Autonomous Fraud Courtroom on "
            f"{ist(time.time(), '%d %b %Y %H:%M IST')}. Synthetic data unless stated otherwise. Drafts require review "
            f"by a compliance officer before any regulatory filing.</footer></main></body></html>")


def tier_pill(tier: int) -> str:
    return f"<span class='pill' style='background:{TIER_COLOR.get(tier, '#888')}'>{e(TIER_NAME.get(tier, str(tier)))}</span>"


def graph_svg(d: dict) -> str:
    g = d.get("graph") or {}
    if not g.get("nodes"):
        return ""
    tiers = {v["node_id"]: v["tier"] for v in (d.get("verdict") or {}).get("node_verdicts", [])}
    pos = {n["id"]: (n["x"], n["y"]) for n in g["nodes"]}
    parts = [f"<svg viewBox='0 0 {g.get('width', 900)} {g.get('height', 520) + 20}' role='img' "
             f"aria-label='Money network'><defs><marker id='ar' viewBox='0 0 10 10' refX='9' refY='5' "
             f"markerWidth='6' markerHeight='6' orient='auto-start-reverse'><path d='M0,0L10,5L0,10z' fill='#999'/>"
             f"</marker></defs>"]
    for ed in g.get("edges", []):
        src = ed.get("source") or ed.get("from")
        dst = ed.get("target") or ed.get("to")
        if not src or not dst or src not in pos or dst not in pos:
            continue
        (x1, y1), (x2, y2) = pos[src], pos[dst]
        kind = ed.get("kind", "ring")
        color = {"cycle": "#B3261E", "ring": "#555", "source": "#999", "legit": "#2E7D32", "shared": "#999"}.get(kind, "#999")
        dash = " stroke-dasharray='4 3'" if kind in ("shared", "legit") else ""
        marker = "" if kind == "shared" else " marker-end='url(#ar)'"
        amt = ed.get("amount")
        title = f"<title>{e(src)} -> {e(dst)}: {inr(amt)}</title>" if amt else ""
        parts.append(f"<line x1='{x1}' y1='{y1}' x2='{x2}' y2='{y2}' stroke='{color}' stroke-width='1.4'{dash}{marker}>{title}</line>")
    for n in g["nodes"]:
        x, y = pos[n["id"]]
        label = e(n.get("label") or n["id"])
        if n.get("group") == "attr":
            w = max(58, len(label) * 8 + 14)
            parts.append(f"<g transform='translate({x},{y})'><rect x='{-w / 2}' y='-11' width='{w}' height='22' rx='4' "
                         f"fill='#F3F1EC' stroke='#999'/><text text-anchor='middle' dy='4' font-size='11' "
                         f"font-family='monospace'>{label}</text></g>")
            continue
        t = tiers.get(n["id"])
        fill = TIER_COLOR.get(t, "#fff") if t is not None and t >= 2 else ("#E8F0E8" if n.get("group") == "legit" else "#fff")
        text_color = "#fff" if t is not None and t >= 2 else "#1c1b19"
        r = 19 if n.get("group") == "ring" else 15
        tag = TIER_NAME.get(t, "") if t is not None else n.get("tag", n.get("group", ""))
        parts.append(f"<g transform='translate({x},{y})'><circle r='{r}' fill='{fill}' stroke='#555'/>"
                     f"<text text-anchor='middle' dy='4' font-size='11' font-family='monospace' fill='{text_color}'>{label}</text>"
                     f"<text text-anchor='middle' y='{r + 12}' font-size='9.5' fill='#6b6760'>{e(tag)}</text></g>")
    parts.append("</svg>")
    return "".join(parts)


def _kpis(d: dict) -> str:
    v = d.get("verdict") or {}
    nv = v.get("node_verdicts", [])
    frozen = sum(1 for n in nv if n["tier"] >= 3)
    return (f"<div class='kpis'><div class='kpi'><span>Money at risk</span><b>{lakh(d['alert']['capital'])}</b></div>"
            f"<div class='kpi'><span>Funds stopped in frozen accounts</span><b>{lakh(d['funds_held'])}</b></div>"
            f"<div class='kpi'><span>Accounts frozen / examined</span><b>{frozen} / {len(nv)}</b></div>"
            f"<div class='kpi'><span>Decision time</span><b>{float(d['duration_s']):.1f} s</b></div></div>")


def _verdict_table(d: dict) -> str:
    rows = []
    risk = d.get("risk", {})
    for n in sorted((d.get("verdict") or {}).get("node_verdicts", []), key=lambda n: (-n["tier"], n["node_id"])):
        rows.append(f"<tr><td class='mono'>{e(n['node_id'])}</td><td>{e(n['role'])}</td><td>{risk.get(n['node_id'], 0):.2f}</td>"
                    f"<td>{n['p_fraud']:.2f}</td><td>{tier_pill(n['tier'])}</td><td>{e(n['rationale'])}</td>"
                    f"<td class='mono'>{e(', '.join(n['cited_evidence']))}</td></tr>")
    return ("<table><tr><th>Account</th><th>Role</th><th>Risk</th><th>P(fraud) after court</th><th>Decision</th>"
            "<th>Why</th><th>Evidence</th></tr>" + "".join(rows) + "</table>")


# ----------------------------------------------------------------------------------------
# 1. Investigation report
# ----------------------------------------------------------------------------------------
def investigation_html(d: dict, executive: bool = False) -> str:
    sc, v = d.get("scenario") or {}, d.get("verdict") or {}
    src = d.get("text_sources", {})
    status = STATUS_TEXT.get(d["status"], d["status"])
    body = [f"<p class='sub mono'>Case {e(d['case_id'])} Â· {e(sc.get('title', 'Live alert'))}</p>",
            f"<h1>Investigation report</h1>",
            f"<p>Alert on <b class='mono'>{e(d['alert']['seed'] or '')}</b> at {ist(d['alert']['time'] or 0)} â€” "
            f"{e(d['alert']['reason'])}</p><span class='status'>{e(status)} Â· confidence {v.get('confidence', 0):.2f}</span>",
            _kpis(d)]
    if sc.get("description"):
        body.append(f"<div class='box'><b>Scenario.</b> {e(sc['description'])}</div>")
    body.append(f"<h2>Court's decision</h2><div class='box'><span class='src'>{e(src.get('judge', ''))}</span>{e(v.get('summary', ''))}</div>")
    body.append(_verdict_table(d))
    if executive:
        return page(f"Executive summary {d['case_id']}", "".join(body))
    body.append("<h2>Money network</h2>" + graph_svg(d) +
                "<p class='sub'>Filled circles show the court's decision; dashed green lines are legitimate relationships "
                "the counterfactual protected; grey boxes are shared devices or IP addresses.</p>")
    g = d.get("genome") or {}
    if g.get("match", {}).get("id"):
        body.append(f"<p>Typology: <b>{e(g.get('typology', ''))}</b>; nearest fraud genome <span class='mono'>"
                    f"{e(g['match']['id'])}</span> ({e(g['match'].get('label', ''))}) at {g['match']['similarity']:.2f}.</p>")
    body.append("<h2>Evidence registry</h2><p class='sub'>Computed by code. The agents could cite only these IDs.</p><table>"
                "<tr><th>ID</th><th>Side</th><th>Finding</th><th>Tool</th></tr>" +
                "".join(f"<tr><td class='mono'>{e(x['id'])}</td><td>{e(x['side'])}</td><td>{e(x['text'])}</td>"
                        f"<td class='mono'>{e(x['source_tool'])}</td></tr>" for x in d["evidence"]) + "</table>")
    body.append("<h2>Arguments</h2>")
    for key, cls, name in (("prosecution", "pros", "Prosecutor"), ("defense", "def", "Defense")):
        b = d.get(key) or {}
        who = "prosecutor" if key == "prosecution" else "defense"
        body.append(f"<div class='box {cls}'><span class='src'>{e(src.get(who, ''))}</span><h3>{name}</h3>{e(b.get('summary', ''))}"
                    f"<div class='sub mono'>cites {e(', '.join(b.get('cited_evidence', [])) or 'â€”')}</div></div>")
    if d.get("grading"):
        gr = d["grading"]
        n = len(gr["truth"])
        body.append(f"<h2>Scorecard against ground truth</h2><p>Synthetic scenario, so the right answer is known. "
                    f"<b class='{'ok' if not gr['missed'] and not gr['wrongful'] else 'bad'}'>{len(gr['correct'])} of {n} "
                    f"accounts decided correctly.</b> Missed (ring accounts not frozen): {e(', '.join(gr['missed']) or 'none')}. "
                    f"Wrongful freezes: {e(', '.join(gr['wrongful']) or 'none')}.</p>")
        if sc.get("expected"):
            body.append(f"<p class='sub'>Expected: {e(sc['expected'])}</p>")
    body.append("<h2>Actions taken</h2><table><tr><th>Endpoint</th><th>Accounts</th><th>Status</th></tr>" +
                "".join(f"<tr><td class='mono'>{e(a.get('endpoint', ''))}</td><td class='mono'>{e(', '.join(a.get('accounts', [])))}</td>"
                        f"<td>{e(str(a.get('status', '')))}</td></tr>" for a in d["actions"]) + "</table>")
    body.append("<h2>Timeline</h2><table><tr><th>+s</th><th>Event</th><th>Detail</th></tr>" +
                "".join(f"<tr><td class='mono'>{t['t']:.1f}</td><td>{e(t['type'])}</td><td>{e(_timeline_text(t))}</td></tr>"
                        for t in d["timeline"]) + "</table>")
    llm = d.get("llm", {})
    body.append(f"<h2>AI provenance</h2><p>Language model: <b>{e(str(llm.get('provider')))}</b> "
                f"(<span class='mono'>{e(str(llm.get('model')))}</span>, judge <span class='mono'>{e(str(llm.get('judge_model')))}</span>). "
                f"Calls {llm.get('calls', 0)}, cache hits {llm.get('cache_hits', 0)}, failures {llm.get('failures', 0)}, "
                f"tokens {llm.get('input_tokens', 0)}+{llm.get('output_tokens', 0)}. Every decision above was computed by code; "
                f"the model only wrote the arguments, and each cited evidence ID was validated. Account IDs were masked "
                f"before any text left the bank.</p>")
    body.append("<h2>Audit trail</h2><table><tr><th>Step</th><th>Hash (SHA-256, chained)</th></tr>" +
                "".join(f"<tr><td>{e(a['kind'])}</td><td class='mono'>{e(a['hash'][:32])}â€¦</td></tr>" for a in d["audit"]) + "</table>")
    for h in d.get("history", []):
        body.append(f"<p class='sub'>Analyst action: {e(json.dumps(h, ensure_ascii=False))}</p>")
    return page(f"Investigation {d['case_id']}", "".join(body))


def _timeline_text(t: dict) -> str:
    p = t["payload"]
    if t["type"] == "agent.message":
        return f"{p['agent']}: {p['text']}"
    if t["type"] == "stage.changed":
        return f"stage â†’ {p.get('stage')}"
    if t["type"] == "action.executed":
        return f"{p.get('endpoint')} {', '.join(p.get('accounts', []))} {p.get('detail', '')}"
    return ", ".join(f"{k}={v}" for k, v in p.items())


# ----------------------------------------------------------------------------------------
# 2. Analyst review packet
# ----------------------------------------------------------------------------------------
def review_html(d: dict) -> str:
    v = d.get("verdict") or {}
    nv = v.get("node_verdicts", [])
    unsure = [n for n in nv if 0.35 <= n["p_fraud"] <= 0.85]
    reasons = []
    if v.get("confidence", 1) < 0.85:
        reasons.append(f"Overall confidence {v.get('confidence', 0):.2f} is below the 0.85 autonomy threshold.")
    if not v.get("counterfactual_ok", True):
        reasons.append("The counterfactual shows that a proposed freeze would cut legitimate relationships.")
    checks = []
    for n in unsure:
        checks.append(f"<tr><td class='mono'>{e(n['node_id'])}</td><td>{n['p_fraud']:.2f}</td><td>{tier_pill(n['tier'])}</td>"
                      f"<td>{e(n['rationale'])}</td><td>Call the customer or check KYC and device history; confirm whether "
                      f"the payment from the ring was expected.</td></tr>")
    body = [f"<p class='sub mono'>Case {e(d['case_id'])}</p><h1>Analyst review packet</h1>",
            f"<p class='warn'><b>The court did not act on its own.</b> {' '.join(e(r) for r in reasons) or 'Sent for review.'} "
            f"Nothing has been frozen yet. Approve to execute the recommendation below, or dismiss it.</p>", _kpis(d),
            "<h2>Recommendation</h2>", f"<div class='box'>{e(v.get('summary', ''))}</div>", _verdict_table(d),
            "<h2>What to check (the uncertain accounts)</h2>"]
    body.append("<table><tr><th>Account</th><th>P(fraud)</th><th>Proposed</th><th>Signals</th><th>Suggested check</th></tr>"
                + ("".join(checks) or "<tr><td colspan='5'>No borderline accounts.</td></tr>") + "</table>")
    body.append("<h2>How to decide</h2><p>In the dashboard, use <b>Approve recommendation</b> or <b>Dismiss</b> "
                f"(API: <span class='mono'>POST /api/review/{e(d['case_id'])}</span>). Your decision is written to the "
                "hash-chained audit log, and a dismissal is stored as a precedent the moot court learns from.</p>")
    body.append("<h2>Money network</h2>" + graph_svg(d))
    return page(f"Review {d['case_id']}", "".join(body))


# ----------------------------------------------------------------------------------------
# 3. Customer notices
# ----------------------------------------------------------------------------------------
def notices_html(d: dict) -> str:
    nv = (d.get("verdict") or {}).get("node_verdicts", [])
    body = [f"<p class='sub mono'>Case {e(d['case_id'])}</p><h1>Customer notices</h1>"]
    msgs = {m["tier"]: m for m in d.get("customer_messages", [])}
    for tier, title in ((2, "Step-up verification"), (3, "Outgoing transfers paused")):
        accs = [n["node_id"] for n in nv if n["tier"] == tier]
        if not accs:
            continue
        text = msgs.get(tier, {}).get("text", "")
        body.append(f"<h2>{title} â€” {len(accs)} account(s)</h2><p class='mono'>{e(', '.join(accs))}</p>"
                    f"<div class='box'>{e(text) or '(message not sent)'}</div>")
    hard = [n["node_id"] for n in nv if n["tier"] == 4]
    if hard:
        body.append(f"<h2>Not notified â€” {len(hard)} account(s)</h2><p class='mono'>{e(', '.join(hard))}</p><p>These "
                    "accounts are subject of a Suspicious Transaction Report. Telling them would be tipping-off, so no "
                    "message is sent; branch staff see a neutral 'account under review' status.</p>")
    cleared = [n["node_id"] for n in nv if n["tier"] <= 1]
    if cleared:
        body.append(f"<h2>No customer impact â€” {len(cleared)} account(s)</h2><p class='mono'>{e(', '.join(cleared))}</p>"
                    "<p>No message: these accounts remain fully active.</p>")
    return page(f"Notices {d['case_id']}", "".join(body))


# ----------------------------------------------------------------------------------------
# 4. Audit CSV
# ----------------------------------------------------------------------------------------
def audit_csv(d: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["case_id", "at_ist", "step", "sha256", "payload"])
    for a in d["audit"]:
        w.writerow([d["case_id"], ist(a["at"]), a["kind"], a["hash"], json.dumps(a["payload"], ensure_ascii=False)[:2000]])
    return buf.getvalue()


# ----------------------------------------------------------------------------------------
# 5. Executive summary PDF
# ----------------------------------------------------------------------------------------
def executive_pdf(d: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from agents.countermeasures.reporting_agent import _font, pdf_safe  # the same rupee-capable font the STR uses

    font = _font()

    def e(text):  # noqa: F811 - escape for reportlab paragraphs, rupee-safe
        return pdf_safe(html.escape(str(text)), font)

    styles = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=styles["BodyText"], fontName=font, fontSize=9.5, leading=13)
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontName=font, fontSize=17, leading=21, alignment=0)
    h2 = ParagraphStyle("h2", parent=styles["Heading3"], fontName=font, fontSize=11.5, spaceBefore=8)
    v = d.get("verdict") or {}
    sc = d.get("scenario") or {}
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=14 * mm)
    story = [Paragraph(f"Executive summary Â· Case {e(d['case_id'])}", h1),
             Paragraph(f"{e(sc.get('title', 'Live alert'))} Â· {e(STATUS_TEXT.get(d['status'], d['status']))} Â· "
                       f"alert {ist(d['alert']['time'] or 0)}", body), Spacer(1, 6)]
    k = [["Money at risk", "Funds stopped", "Accounts frozen", "Decision time", "Confidence"],
         [pdf_safe(lakh(d["alert"]["capital"]), font), pdf_safe(lakh(d["funds_held"]), font),
          f"{sum(1 for n in v.get('node_verdicts', []) if n['tier'] >= 3)} of {len(v.get('node_verdicts', []))}",
          f"{float(d['duration_s']):.1f} s", f"{v.get('confidence', 0):.2f}"]]
    t = Table(k, hAlign="LEFT")
    t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), font, 9), ("FONTSIZE", (0, 1), (-1, 1), 12),
                           ("TEXTCOLOR", (0, 0), (-1, 0), colors.grey), ("BOX", (0, 0), (-1, -1), 0.5, colors.lightgrey),
                           ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey), ("TOPPADDING", (0, 0), (-1, -1), 5)]))
    story += [t, Paragraph("What happened", h2), Paragraph(e(sc.get("description") or d["alert"]["reason"]), body),
              Paragraph("The court's decision", h2), Paragraph(e(v.get("summary", "")), body), Paragraph("Per account", h2)]
    rows = [["Account", "Role", "P(fraud)", "Decision", "Why"]]
    for n in sorted(v.get("node_verdicts", []), key=lambda n: (-n["tier"], n["node_id"])):
        rows.append([n["node_id"], n["role"], f"{n['p_fraud']:.2f}", TIER_NAME[n["tier"]], Paragraph(e(n["rationale"]), body)])
    t = Table(rows, colWidths=[18 * mm, 22 * mm, 18 * mm, 32 * mm, 88 * mm], repeatRows=1)
    style = [("FONT", (0, 0), (-1, -1), font, 8.5), ("TEXTCOLOR", (0, 0), (-1, 0), colors.grey),
             ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey), ("VALIGN", (0, 0), (-1, -1), "TOP")]
    for i, n in enumerate(sorted(v.get("node_verdicts", []), key=lambda n: (-n["tier"], n["node_id"])), 1):
        style.append(("TEXTCOLOR", (3, i), (3, i), colors.HexColor(TIER_COLOR[n["tier"]])))
    t.setStyle(TableStyle(style))
    story.append(t)
    if d.get("grading"):
        gr = d["grading"]
        story += [Paragraph("Scorecard (synthetic ground truth)", h2),
                  Paragraph(f"{len(gr['correct'])} of {len(gr['truth'])} accounts decided correctly; missed: "
                            f"{', '.join(gr['missed']) or 'none'}; wrongful freezes: {', '.join(gr['wrongful']) or 'none'}.", body)]
    llm = d.get("llm", {})
    story += [Paragraph("Controls", h2), Paragraph(
        f"Decisions computed by code; language model {e(str(llm.get('provider')))} ({e(str(llm.get('model')))}) wrote the "
        f"arguments only, with every cited evidence ID validated and account IDs masked. Autonomy threshold 0.85 plus "
        f"counterfactual check. {len(d['audit'])} steps recorded in a SHA-256 hash-chained audit log.", body)]
    doc.build(story)
    return buf.getvalue()


# ----------------------------------------------------------------------------------------
# Portfolio reports
# ----------------------------------------------------------------------------------------
def line_chart_svg(series: dict[str, list[float]], labels: list[str], colors: dict[str, str], ymax: float,
                   title: str = "") -> str:
    W, H, L, R, T, B = 640, 280, 46, 16, 20, 34
    n = max(len(labels), 2)

    def x(i):
        return L + i * (W - L - R) / (n - 1)

    def y(v):
        return T + (1 - v / ymax) * (H - T - B)

    out = [f"<svg viewBox='0 0 {W} {H}' role='img' aria-label='{e(title)}'>"]
    for g in range(0, 5):
        val = ymax * g / 4
        out.append(f"<line x1='{L}' x2='{W - R}' y1='{y(val)}' y2='{y(val)}' stroke='#eee'/>"
                   f"<text x='{L - 6}' y='{y(val) + 4}' font-size='10' text-anchor='end' fill='#888'>{val:.0f}</text>")
    for i, lab in enumerate(labels):
        out.append(f"<text x='{x(i)}' y='{H - 12}' font-size='10' text-anchor='middle' fill='#888'>{e(lab)}</text>")
    for name, vals in series.items():
        pts = " ".join(f"{x(i)},{y(v)}" for i, v in enumerate(vals))
        out.append(f"<polyline points='{pts}' fill='none' stroke='{colors[name]}' stroke-width='2.2'/>")
        out += [f"<circle cx='{x(i)}' cy='{y(v)}' r='3' fill='{colors[name]}'><title>{e(name)}: {v}</title></circle>"
                for i, v in enumerate(vals)]
    out.append("</svg>")
    legend = " Â· ".join(f"<span style='color:{colors[k]}'>â– </span> {e(k)}" for k in series)
    return "".join(out) + f"<p class='sub'>{legend}</p>"


def moot_report_html(metrics: dict) -> str:
    base = metrics.get("baseline")
    rounds = metrics.get("rounds", [])
    if not base or not rounds:
        return page("Moot court report", "<h1>Moot court report</h1><p>Run the moot court first.</p>")
    labels = ["base"] + [f"R{r['round']}" for r in rounds]
    missed = [base["missed_per_100"]] + [r["benchmark"]["missed_per_100"] for r in rounds]
    wrong = [base["wrongful_per_100"]] + [r["benchmark"]["wrongful_per_100"] for r in rounds]
    chart = line_chart_svg({"Missed rings per 100": missed, "Wrongful freezes per 100": wrong}, labels,
                           {"Missed rings per 100": "#B3261E", "Wrongful freezes per 100": "#0F766E"},
                           max(10, max(missed + wrong) * 1.15), "Moot court learning curves")
    rows = "".join(f"<tr><td>R{r['round']}</td><td>{r['benchmark']['missed_per_100']}</td><td>{r['benchmark']['wrongful_per_100']}</td>"
                   f"<td>{r['red_team_success_pct']}%</td><td>{r['sent_to_court']}</td><td>{r['precedents_added']}</td>"
                   f"<td>{r['max_hop_gap_min']} min</td></tr>" for r in rounds)
    log = "".join(f"<tr><td>R{x['round']}</td><td>{e(x['type'])}</td><td>{e(x['what'])}</td><td>{e(x['stored'])}</td>"
                  f"<td>{'fixed from R' + str(x['fixed_round']) if x.get('fixed_round') else 'open'}</td></tr>"
                  for x in metrics.get("log", [])[-25:])
    body = (f"<h1>Moot court training report</h1><p>Every night the red team attacks both sides of the court: evasion cases "
            f"test the Prosecutor, framing cases test the Defense. Mistakes become precedents. Progress is measured on a "
            f"fixed 30-case benchmark the court never learns from.</p>"
            f"<div class='kpis'><div class='kpi'><span>Missed rings</span><b>{missed[0]} â†’ {missed[-1]}</b></div>"
            f"<div class='kpi'><span>Wrongful freezes</span><b>{wrong[0]} â†’ {wrong[-1]}</b></div>"
            f"<div class='kpi'><span>Rounds</span><b>{len(rounds)}</b></div>"
            f"<div class='kpi'><span>Learned trace window</span><b>{rounds[-1]['max_hop_gap_min']} min</b></div></div>"
            f"<h2>Learning curves</h2>{chart}<h2>Per round</h2><table><tr><th>Round</th><th>Missed/100</th><th>Wrongful/100</th>"
            f"<th>Red-team wins</th><th>Cases</th><th>New precedents</th><th>Trace window</th></tr>{rows}</table>"
            f"<h2>Precedent log (latest 25)</h2><table><tr><th>Round</th><th>Type</th><th>What went wrong</th>"
            f"<th>Stored lesson</th><th>Status</th></tr>{log}</table>")
    return page("Moot court report", body)


def scorecard_html(results: list[dict], title: str = "Scenario scorecard") -> str:
    """results: one row per case from scripts/evaluate_scenarios.py"""
    kinds = sorted({r["kind"] for r in results}, key=lambda k: (r_innocent(results, k), k))
    rows = []
    for k in kinds:
        rs = [r for r in results if r["kind"] == k]
        fraud_accounts = sum(r["fraud_accounts"] for r in rs)
        innocent_accounts = sum(r["innocent_accounts"] for r in rs)
        caught = sum(r["caught"] for r in rs)
        wrongful = sum(r["wrongful"] for r in rs)
        rows.append(
            f"<tr><td>{e(k)}</td><td>{len(rs)}</td><td>{sum(r['alerted'] for r in rs)}/{len(rs)}</td>"
            f"<td>{(100 * caught / fraud_accounts) if fraud_accounts else float('nan'):.0f}%</td>"
            f"<td>{wrongful} / {innocent_accounts}</td><td>{sum(r['route'] == 'act' for r in rs)}</td>"
            f"<td>{sum(r['route'] == 'review' for r in rs)}</td>"
            f"<td>{statistics.mean(r['seconds'] for r in rs):.2f}s</td></tr>")
    all_f = sum(r["fraud_accounts"] for r in results)
    all_i = sum(r["innocent_accounts"] for r in results)
    body = (f"<h1>{e(title)}</h1><p>{len(results)} freshly generated cases (random seeds), replayed through the alert "
            f"trigger and the full court exactly as in the live demo. Synthetic data: ground truth is known.</p>"
            f"<div class='kpis'><div class='kpi'><span>Ring accounts stopped</span><b>"
            f"{100 * sum(r['caught'] for r in results) / max(all_f, 1):.1f}%</b></div>"
            f"<div class='kpi'><span>Innocent accounts frozen</span><b>{sum(r['wrongful'] for r in results)} of {all_i}</b></div>"
            f"<div class='kpi'><span>Decided autonomously</span><b>{sum(r['route'] == 'act' for r in results)}/{len(results)}</b></div>"
            f"<div class='kpi'><span>Median decision time</span><b>{statistics.median(r['seconds'] for r in results):.2f}s</b></div></div>"
            f"<table><tr><th>Scenario</th><th>Cases</th><th>Alert fired</th><th>Ring accounts stopped</th>"
            f"<th>Innocent frozen</th><th>Auto</th><th>Review</th><th>Avg time</th></tr>{''.join(rows)}</table>"
            f"<p class='sub'>'Stopped' means soft or hard freeze (tier 3+). Cases sent to review took no action until an "
            f"analyst approves, so their ring accounts count as stopped only if the court recommended a freeze.</p>")
    return page(title, body)


def r_innocent(results: list[dict], kind: str) -> bool:
    return any(r["kind"] == kind and r["fraud_accounts"] == 0 for r in results)


def write_report(name: str, content: str) -> Path:
    path = REPORTS_DIR / name
    path.write_text(content, encoding="utf-8")
    return path


generate_html_report = investigation_html
generate_case_brief = review_html
