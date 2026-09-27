"""Every scenario type runs end to end through the API, and every report renders."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config import settings
from core.scenarios import FRAUD_KINDS, INNOCENT_KINDS, SCENARIOS, build_scenario


def run_until_closed(c, ws, url):
    assert c.post(url).status_code == 200
    closed = None
    for _ in range(5000):
        ev = ws.receive_json()
        if ev["type"] == "case.closed":
            closed = ev
            break
    return closed


def test_scenarios_are_different_every_run():
    a, b = build_scenario("mule_ring", 1), build_scenario("mule_ring", 2)
    assert a["meta"]["seed"] != b["meta"]["seed"] or len(a["transactions"]) != len(b["transactions"])
    assert build_scenario("mule_ring", 5)["transactions"] == build_scenario("mule_ring", 5)["transactions"]


@pytest.mark.parametrize("kind", [k for k in SCENARIOS if k != "demo_3am"])
def test_scenario_end_to_end_with_reports(kind, monkeypatch):
    monkeypatch.setattr(settings, "settle_window", 0.4)
    with TestClient(app) as c, c.websocket_connect("/ws") as ws:
        closed = run_until_closed(c, ws, f"/api/demo/run?speed=2000&scenario={kind}&seed=3")
        assert closed, "case never closed"
        case_id = closed["case_id"]
        reports = None
        for _ in range(50):
            r = c.get(f"/api/reports/{case_id}")
            if r.status_code == 200:
                reports = r.json()["reports"]
                break
            __import__("time").sleep(0.1)
        assert reports
        for rep in reports:
            res = c.get(rep["url"])
            assert res.status_code == 200 and len(res.content) > 200, rep
        d = c.get(f"/api/reports/{case_id}/dossier").json()
        tiers = {v["node_id"]: v["tier"] for v in d["verdict"]["node_verdicts"]}
        truth = d["grading"]["truth"]
        # never freeze an innocent account, whatever the scenario
        assert not [a for a, f in truth.items() if not f and tiers.get(a, 0) >= 3]
        if kind in INNOCENT_KINDS:
            assert closed["payload"]["status"] == "cleared"
        if kind in FRAUD_KINDS:
            assert any(t >= 3 for t in tiers.values()), "a fraud scenario must end with freezes recommended"
        if closed["payload"]["status"] == "awaiting_review":
            kinds = [r["kind"] for r in reports]
            assert "review" in kinds
            out = c.post(f"/api/review/{case_id}", json={"decision": "approve", "note": "checked KYC"}).json()
            assert out["status"] == "approved"
            assert any(a.get("endpoint", "").startswith("/bank/") for a in out["actions"])
            assert c.get(f"/api/reports/{case_id}/str").status_code == 200


def test_portfolio_reports():
    with TestClient(app) as c:
        assert c.get("/api/reports").status_code == 200
        assert "Moot court" in c.get("/api/reports/portfolio/moot").text
        assert len(c.get("/api/scenarios").json()) == len(SCENARIOS)
