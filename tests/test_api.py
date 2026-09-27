"""API smoke test: replay the live demo over HTTP and watch the events arrive on the WebSocket."""
from fastapi.testclient import TestClient

from app.main import app, rt
from app.config import settings


def test_live_demo_over_websocket(monkeypatch):
    # let the downstream hops (up to 150 s of data time) land before the court sits
    monkeypatch.setattr(settings, "settle_window", 0.5)
    with TestClient(app) as c:
        st = c.get("/api/status").json()
        assert st["llm_provider"] == "mock"
        with c.websocket_connect("/ws") as ws:
            r = c.post("/api/demo/run?speed=1000")
            assert r.status_code == 200
            seen = set()
            for _ in range(2000):
                ev = ws.receive_json()
                seen.add(ev["type"])
                if ev["type"] == "case.closed":
                    break
        assert "txn.ingested" in seen
        assert {"case.opened", "evidence.added", "verdict.issued", "action.executed", "case.closed"} <= seen
        case = next(iter(rt.cases.values()))
        assert case["route"] == "act", case
        assert c.get("/api/audit/verify").json()["chain_intact"] is True
        assert c.get("/").status_code == 200


def test_external_ingestion_feed(monkeypatch):
    """What scripts/stream_transactions.py does: reset to history, then POST live transactions."""
    monkeypatch.setattr(settings, "settle_window", 0.3)
    with TestClient(app) as c:
        assert c.post("/api/demo/reset").status_code == 200
        live = list(rt.live_txns)
        opened = []
        for t in live:
            r = c.post("/transactions/ingest", json={"txn_id": t["txn_id"], "timestamp": t["ts"],
                                                     "from_account": t["src"], "to_account": t["dst"],
                                                     "amount": t["amount"], "rail": t["rail"]}).json()
            if r["alert_opened_for"]:
                opened.append(r["alert_opened_for"])
        assert opened == ["A"], "exactly one case, opened on the collector account"


def test_analyst_override_creates_precedent(monkeypatch):
    monkeypatch.setattr(settings, "settle_window", 0.3)
    with TestClient(app) as c:
        with c.websocket_connect("/ws") as ws:
            c.post("/api/demo/run?speed=1000")
            case_id = None
            for _ in range(2000):
                ev = ws.receive_json()
                if ev["type"] == "case.closed":
                    case_id = ev["case_id"]
                    break
        r = c.post(f"/api/override/{case_id}", json={"account": "C4", "decision": "release", "note": "salary account"})
        assert r.status_code == 200, r.text
        assert r.json()["precedent"]
        assert c.get("/bank/status/C4").json()["status"] == "active"
        assert c.get("/api/audit/verify").json()["chain_intact"] is True
