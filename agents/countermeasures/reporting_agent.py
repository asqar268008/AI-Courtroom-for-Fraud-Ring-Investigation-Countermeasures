"""Suspicious Activity Report: a structured JSON dossier plus a readable PDF."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.config import RUNTIME_DIR
from agents.investigation.network_agent import IST_OFFSET, inr

SAR_DIR = RUNTIME_DIR / "sar"
SAR_DIR.mkdir(parents=True, exist_ok=True)

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/Nirmala.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def _font() -> str:
    """Register a TTF that has the rupee sign; fall back to Helvetica."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    if "SARFont" in pdfmetrics.getRegisteredFontNames():
        return "SARFont"
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            pdfmetrics.registerFont(TTFont("SARFont", candidate))
            return "SARFont"
    return "Helvetica"


def pdf_safe(text: str, font: str) -> str:
    return text if font != "Helvetica" else text.replace("₹", "Rs. ").replace("→", "->").replace("…", "...")


def build_sar(case_id: str, state: dict, transactions: list[dict], audit_hash: str) -> dict:
    verdict = state["verdict"]
    subjects = [nv.model_dump() for nv in verdict.node_verdicts if nv.tier >= 3]
    return {
        "report_type": "Suspicious Transaction Report (draft for FIU-IND / internal compliance review)",
        "case_id": case_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": verdict.summary,
        "alert": {"seed_account": state["seed"], "reason": state.get("alert_reason", ""),
                  "capital_at_risk": state.get("capital_at_risk", 0)},
        "subjects": subjects,
        "not_reported": [nv.node_id for nv in verdict.node_verdicts if nv.tier < 3],
        "transactions": transactions,
        "evidence": [e.model_dump() for e in state.get("evidence", [])],
        "confidence": verdict.confidence,
        "counterfactual_ok": verdict.counterfactual_ok,
        "audit_hash": audit_hash,
        "note": "Draft generated automatically. A compliance officer must review before regulatory filing.",
    }


def write_sar(sar: dict) -> tuple[Path, Path | None]:
    json_path = SAR_DIR / f"{sar['case_id']}.json"
    json_path.write_text(json.dumps(sar, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        pdf_path = _write_pdf(sar)
    except Exception:  # PDF is a convenience; the JSON dossier is the record
        pdf_path = None
    return json_path, pdf_path


def _write_pdf(sar: dict) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font = _font()

    def t(text: str) -> str:
        text = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return text if font != "Helvetica" else text.replace("₹", "Rs. ").replace("→", "->")

    styles = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=styles["BodyText"], fontName=font, fontSize=9.5, leading=13)
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontName=font, fontSize=16)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontName=font, fontSize=12, spaceBefore=8)

    path = SAR_DIR / f"{sar['case_id']}.pdf"
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm, title=f"STR {sar['case_id']}")
    story = [Paragraph(t(f"Suspicious Transaction Report (draft) · {sar['case_id']}"), h1),
             Paragraph(t(sar["report_type"]), body),
             Paragraph(t(f"Generated {sar['generated_at']} · confidence {sar['confidence']:.2f} · "
                         f"audit hash {sar['audit_hash'][:16]}…"), body),
             Spacer(1, 6), Paragraph("Summary", h2), Paragraph(t(sar["summary"]), body),
             Paragraph("Subjects", h2)]

    def table(rows, widths):
        tb = Table([[Paragraph(t(c), body) for c in r] for r in rows], colWidths=widths, repeatRows=1)
        tb.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E4DED1")),
                                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#B3AC9E")),
                                ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        return tb

    rows = [["Account", "Role", "P(fraud)", "Action", "Evidence"]]
    for s in sar["subjects"]:
        rows.append([s["node_id"], s["role"], f"{s['p_fraud']:.2f}", s["action"], ", ".join(s["cited_evidence"])])
    story.append(table(rows, [22 * mm, 24 * mm, 18 * mm, 45 * mm, 65 * mm]))
    if sar["not_reported"]:
        story.append(Paragraph(t("Traced but not reported (lower tier): " + ", ".join(sar["not_reported"])), body))

    story.append(Paragraph("Transactions", h2))
    rows = [["Time (IST)", "From", "To", "Amount", "Txn ID"]]
    for tx in sar["transactions"]:
        when = datetime.fromtimestamp(tx["ts"] + IST_OFFSET, timezone.utc).strftime("%d %b %H:%M:%S")
        rows.append([when, tx["src"], tx["dst"], inr(tx["amount"]), tx["txn_id"]])
    story.append(table(rows, [32 * mm, 22 * mm, 22 * mm, 32 * mm, 40 * mm]))

    story.append(Paragraph("Evidence", h2))
    rows = [["ID", "Finding", "Source"]]
    for e in sar["evidence"]:
        rows.append([e["id"], e["text"], e["source_tool"]])
    story.append(table(rows, [28 * mm, 110 * mm, 36 * mm]))
    story += [Spacer(1, 8), Paragraph(t(sar["note"]), body)]
    doc.build(story)
    return path
