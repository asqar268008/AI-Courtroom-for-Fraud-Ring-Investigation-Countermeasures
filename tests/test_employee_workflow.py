"""Employee workflow: alerts → background investigation → employee approval (partial) → execution."""
import pytest
from fastapi.testclient import TestClient

from app.main import app, rt
from app.config import settings


@pytest.fixture
def employee_mode(monkeypatch):
    monkeypatch.setattr(settings, "approval_mode", "employee")
    monkeypatch.setattr(settings, "settle_window", 0.3)
    monkeypatch.setattr(settings, "ambient_tps", 0)


def collect_until_closed(ws, n_cases=1):
    events, closed = [], set()
    for _ in range(20000):
        ev = ws.receive_json()
        events.append(ev)
        if ev["type"] == "case.closed":
            closed.add(ev["case_id"])
            if len(closed) >= n_cases:
                break
    return events


def test_policy_holds_restrictive_actions_for_the_employee(employee_mode):
    with TestClient(app) as c, c.websocket_connect("/ws") as ws:
        c.post("/api/demo/run?speed=2000")
        events = collect_until_closed(ws)
        types = [e["type"] for e in events]
        # the dashboard gets: scored transactions, an alert, hop-by-hop discovery and visible tool calls
        assert any(e["type"] == "txn.ingested" and "risk" in e["payload"] for e in events)
        assert "alert.raised" in types and "graph.progress" in types and "agent.tool" in types
        closed = next(e for e in events if e["type"] == "case.closed")
        assert closed["payload"]["status"] == "awaiting_review"
        assert closed["payload"]["reason"].startswith("policy")
        assert not any(e["type"] == "action.executed" and e["payload"].get("endpoint", "").startswith("/bank/")
                       for e in events), "nothing may be restricted before the employee approves"
        case_id = closed["case_id"]
        assert rt.bank.get("A")["tier"] == 0
        # nodes carry what the investigation page shows
        snap = next(e for e in events if e["type"] == "graph.snapshot")["payload"]
        b1 = next(n for n in snap["nodes"] if n["id"] == "B1")
        assert {"received", "forwarded", "dwell_s", "fan_out", "devices", "ips"} <= set(b1["stats"])
        # listing + event stream for page reloads
        assert any(x["case_id"] == case_id for x in c.get("/api/investigations").json())
        assert len(c.get(f"/api/investigations/{case_id}/events").json()) > 20
        # partial approval: freezes yes, but keep C1 unrestricted and skip step-ups
        r = c.post(f"/api/review/{case_id}", json={"decision": "approve", "exclude": ["C1"], "approve_tiers": [4, 3],
                                                   "note": "C1 verified at branch"}).json()
        assert r["status"] == "approved"
        declined = {d["account"]: d["proposed_tier"] for d in r["declined"]}
        # C1 excluded by hand; C3 (and any other step-up) declined because tier 2 was not approved
        assert "C1" in declined and declined.get("C3") == 2
        assert all(t == 2 for a, t in declined.items() if a != "C1")
        assert rt.bank.get("A")["tier"] == 4 and rt.bank.get("C2")["tier"] == 3
        assert rt.bank.get("C1")["tier"] == 0 and rt.bank.get("C3")["tier"] == 0
        assert c.get("/api/audit/verify").json()["chain_intact"]


def test_multiple_simultaneous_alerts(employee_mode):
    with TestClient(app) as c, c.websocket_connect("/ws") as ws:
        c.post("/api/demo/run?speed=3000&scenario=rush_hour&seed=4")
        events = collect_until_closed(ws, n_cases=3)
        alerts = [e for e in events if e["type"] == "alert.raised"]
        assert len(alerts) == 3 and len({a["payload"]["seed"] for a in alerts}) == 3
        statuses = [e["payload"]["status"] for e in events if e["type"] == "case.closed"]
        assert "cleared" in statuses and statuses.count("awaiting_review") == 2


def test_frozen_sender_transfers_are_held():
    with TestClient(app) as c:
        rt.bank.apply(3, ["L01"], "manual")
        with c.websocket_connect("/ws") as ws:
            c.post("/transactions/ingest", json={"txn_id": "T-HOLD", "timestamp": 1.9e9, "from_account": "L01",
                                                 "to_account": "L02", "amount": 5000})
            ev = ws.receive_json()
        assert ev["type"] == "txn.ingested" and ev["payload"]["status"] == "held"
        assert "T-HOLD" not in rt.store.txns
        rt.bank.reset()


def test_command_center_routes_serve_the_app():
    with TestClient(app) as c:
        for path in ("/", "/dashboard", "/investigations", "/investigations/FR-1", "/countermeasures", "/moot"):
            r = c.get(path)
            assert r.status_code == 200 and "Fraud Command Center" in r.text, path
        assert c.get("/app.js").status_code == 200 and c.get("/styles.css").status_code == 200
