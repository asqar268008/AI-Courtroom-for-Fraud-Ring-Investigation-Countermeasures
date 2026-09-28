import glob
import json
import re
from pathlib import Path

def transform_judge(text: str, verdict_dict: dict = None) -> str:
    if not text or not isinstance(text, str):
        return text
    if text.startswith("The Court finds"):
        return text

    m_conf = re.search(r"confidence\s+([\d]+(?:\.[\d]+)?)", text)
    conf = float(m_conf.group(1)) if m_conf else 0.92
    conf_pct = int(round(conf * 100)) if conf <= 1.0 else int(round(conf))

    actions = re.findall(r"([A-Za-z0-9_-]+):\s*Tier\s*(\d+)", text)
    t4 = [acc for acc, tier in actions if tier == "4"]
    t3 = [acc for acc, tier in actions if tier == "3"]
    t2 = [acc for acc, tier in actions if tier == "2"]

    if not actions and verdict_dict:
        nv_list = verdict_dict.get("node_verdicts", [])
        t4 = [n["node_id"] for n in nv_list if n.get("tier") == 4]
        t3 = [n["node_id"] for n in nv_list if n.get("tier") == 3]
        t2 = [n["node_id"] for n in nv_list if n.get("tier") == 2]

    upheld = t2
    s1 = f"The Court finds substantial and verified evidence establishing an organized laundering syndicate with {conf_pct}% judicial confidence."
    if upheld:
        s2 = f"The Prosecution's indictment is sustained for coordinated layering nodes, while the Defense's exculpatory proofs are upheld for verified account(s) ({', '.join(upheld)}) to prevent collateral commercial harm."
    else:
        s2 = "The Prosecution's forensic money trail demonstrates deliberate evasion and velocity that overcomes the Defense's objections."

    orders = []
    if t4:
        orders.append(f"Tier 4 hard freezes and STR filings are ordered for core extraction nodes ({', '.join(t4)})")
    if t3:
        orders.append(f"Tier 3 outbound holds are placed on pass-through intermediaries ({', '.join(t3)})")
    if t2:
        orders.append(f"defended accounts ({', '.join(t2)}) are remanded to Tier 2 step-up verification")

    order_str = "; ".join(orders) if orders else "enhanced monitoring is instituted"
    s3 = f"Final Order: {order_str}."
    return f"{s1} {s2} {s3}"


def transform_prosecutor(text: str, ring_nodes: list = None, roles: dict = None) -> str:
    if not text or not isinstance(text, str):
        return text
    if text.startswith("The Prosecution indicts"):
        return text

    m_ring = re.search(r"against ring\s+([^.]+?)\s+based on", text)
    if m_ring:
        ring = [x.strip() for x in m_ring.group(1).split(",") if x.strip()]
    elif ring_nodes:
        ring = ring_nodes
    else:
        ring = []

    roles = roles or {}
    origins = [a for a in ring if roles.get(a) == "origin" or a.startswith("A") or a.startswith("R0") or a == (ring[0] if ring else "")]
    cashouts = [a for a in ring if roles.get(a) == "cashout" or (a.startswith("M") and a not in origins)]

    origin_str = ", ".join(origins[:2]) if origins else (ring[0] if ring else "seed")
    cash_str = f" and {len(cashouts)} cash-out endpoint(s) ({', '.join(cashouts)})" if cashouts else ""
    core_targets = (cashouts + origins)[:4] or ring[:3]

    s1 = f"The Prosecution indicts an organized laundering syndicate centered on origin account {origin_str}{cash_str} across {len(ring)} coordinated accounts."
    s2 = "Forensic analysis confirms structured fund layering substantiated by rapid fund velocity and co-located telemetry designed to bypass velocity controls and rapidly extract capital."
    s3 = f"We petition the Court for immediate Tier 4 hard freezes and STR filings against primary coordinators ({', '.join(core_targets)}), with Tier 3 outbound holds on pass-through mule intermediaries."
    return f"{s1} {s2} {s3}"


def transform_defense(text: str, ring_count: int = None) -> str:
    if not text or not isinstance(text, str):
        return text
    if text.startswith("The Defense"):
        return text

    m_def = re.search(r"legitimate accounts\s*\(([^)]+)\)", text)
    innocent_str = m_def.group(1).strip() if m_def else ""
    has_innocent = innocent_str and innocent_str != "none"

    if has_innocent:
        s1 = f"The Defense conducted an exculpatory audit across all flagged accounts and identified legitimate commercial activity for {innocent_str} demonstrating established operating history and verified counterparties."
        s2 = "Imposing punitive Tier 4 blocking on these accounts would cause severe collateral damage to innocent commercial operations."
        s3 = f"We move to dismiss all punitive freezes against defended accounts ({innocent_str}), restricting any intervention to Tier 2 step-up verification."
        return f"{s1} {s2} {s3}"
    else:
        count = ring_count or 12
        s1 = f"The Defense scrutinized all {count} flagged accounts for benign explanations, distinguishing unwitting pass-through counterparties from culpable ring coordinators."
        s2 = "While intermediary accounts exhibit rapid fund transmission, the state has not demonstrated that account holders acted with criminal intent rather than being compromised or coerced mules."
        s3 = "We request the Court mandate Tier 2 step-up authentication or temporary Tier 3 soft holds rather than irreversible Tier 4 hard freezes, allowing legitimate customers due process to verify transactions."
        return f"{s1} {s2} {s3}"


def run_migration():
    cases_updated = 0
    recordings_updated = 0

    # 1. Update runtime/cases/*.json
    for path in glob.glob("runtime/cases/*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            ring = data.get("ring", [])
            roles = data.get("roles", {})
            verdict = data.get("verdict", {})

            changed = False

            if "prosecution" in data and isinstance(data["prosecution"], dict):
                old = data["prosecution"].get("summary")
                new = transform_prosecutor(old, ring, roles)
                if new != old:
                    data["prosecution"]["summary"] = new
                    changed = True

            if "defense" in data and isinstance(data["defense"], dict):
                old = data["defense"].get("summary")
                new = transform_defense(old, len(ring))
                if new != old:
                    data["defense"]["summary"] = new
                    changed = True

            if "verdict" in data and isinstance(data["verdict"], dict):
                old = data["verdict"].get("summary")
                new = transform_judge(old, verdict)
                if new != old:
                    data["verdict"]["summary"] = new
                    changed = True

            # Also check timeline
            for item in data.get("timeline", []):
                p = item.get("payload")
                if isinstance(p, dict):
                    if p.get("agent") == "judge" and "text" in p:
                        p["text"] = transform_judge(p["text"], verdict)
                        changed = True
                    elif p.get("agent") == "prosecutor" and "text" in p:
                        p["text"] = transform_prosecutor(p["text"], ring, roles)
                        changed = True
                    elif p.get("agent") == "defense" and "text" in p:
                        p["text"] = transform_defense(p["text"], len(ring))
                        changed = True

            if changed:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                cases_updated += 1
        except Exception as e:
            print(f"Error updating case {path}: {e}")

    # 2. Update runtime/recordings/*.json
    for path in glob.glob("runtime/recordings/*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                events = json.load(f)

            if not isinstance(events, list):
                continue

            changed = False
            # Find ring / roles / verdict in events if available
            ring = []
            roles = {}
            verdict_dict = {}

            for ev in events:
                p = ev.get("payload", {})
                if not isinstance(p, dict):
                    continue
                if ev.get("type") == "verdict.issued":
                    verdict_dict = p
                if ev.get("type") == "case.opened":
                    if "seed" in p:
                        ring.append(p["seed"])

            for ev in events:
                p = ev.get("payload", {})
                if not isinstance(p, dict):
                    continue
                t = ev.get("type")

                if t == "agent.message":
                    ag = p.get("agent")
                    if ag == "judge" and "text" in p:
                        new_t = transform_judge(p["text"], verdict_dict)
                        if new_t != p["text"]:
                            p["text"] = new_t
                            changed = True
                    elif ag == "prosecutor" and "text" in p:
                        new_t = transform_prosecutor(p["text"], ring, roles)
                        if new_t != p["text"]:
                            p["text"] = new_t
                            changed = True
                    elif ag == "defense" and "text" in p:
                        new_t = transform_defense(p["text"], len(ring))
                        if new_t != p["text"]:
                            p["text"] = new_t
                            changed = True

                elif t == "brief.filed":
                    side = p.get("side")
                    if side == "prosecution" and "summary" in p:
                        new_s = transform_prosecutor(p["summary"], ring, roles)
                        if new_s != p["summary"]:
                            p["summary"] = new_s
                            changed = True
                    elif side == "defense" and "summary" in p:
                        new_s = transform_defense(p["summary"], len(ring))
                        if new_s != p["summary"]:
                            p["summary"] = new_s
                            changed = True

                elif t == "verdict.issued":
                    if "summary" in p:
                        new_s = transform_judge(p["summary"], verdict_dict)
                        if new_s != p["summary"]:
                            p["summary"] = new_s
                            changed = True

            has_def_msg = any(ev.get("type") == "agent.message" and ev.get("payload", {}).get("agent") == "defense" for ev in events)
            if not has_def_msg:
                for idx, ev in enumerate(list(events)):
                    if ev.get("type") == "brief.filed" and ev.get("payload", {}).get("side") == "defense":
                        bp = ev.get("payload", {})
                        summary = bp.get("summary", "")
                        msg_ev = {
                            "case_id": ev.get("case_id"),
                            "type": "agent.message",
                            "t": ev.get("t", 0.0) + 0.05,
                            "payload": {
                                "agent": "defense",
                                "stage": "defense",
                                "text": summary,
                                "source": "deterministic-rubric" if bp.get("llm_fallback") else "LLM",
                                "evidence_ids": bp.get("cited_evidence", []),
                            }
                        }
                        events.insert(idx + 1, msg_ev)
                        changed = True
                        break

            if changed:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(events, f, indent=2)
                recordings_updated += 1
        except Exception as e:
            print(f"Error updating recording {path}: {e}")

    print(f"Migration completed: {cases_updated} cases updated, {recordings_updated} recordings updated.")

if __name__ == "__main__":
    run_migration()
