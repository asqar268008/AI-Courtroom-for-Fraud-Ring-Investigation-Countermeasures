"""Tests for API routes, FastAPI application, and WebSocket connections."""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_status_endpoint(client):
    res = client.get("/api/status")
    assert res.status_code == 200
    data = res.json()
    assert "status" in data
    assert data["status"] == "ready"


def test_scenarios_catalog(client):
    res = client.get("/api/scenarios")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)


def test_transaction_ingestion(client):
    res = client.post(
        "/api/txns",
        json={
            "txn_id": "TEST_TXN_001",
            "timestamp": 1726522500.0,
            "from_account": "ACC_SENDER",
            "to_account": "ACC_RECEIVER",
            "amount": 25000.0,
            "rail": "UPI",
        },
    )
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_audit_endpoint(client):
    res = client.get("/api/audit")
    assert res.status_code == 200
    data = res.json()
    assert "chain_ok" in data
    assert "entries" in data
