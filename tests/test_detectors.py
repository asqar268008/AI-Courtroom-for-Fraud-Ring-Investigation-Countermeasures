"""Unit tests for the deterministic tools (facts come from code, not LLMs)."""
from agents.investigation import network_agent as D
from app.config import settings
from core.decision import choose_tier
from conftest import RING


def test_trace_ring(store, alert_time):
    tr = D.trace_ring(store, "A", alert_time, settings.alert_window_s, settings.default_max_hop_gap_s)
    members = set(tr["ring"]) if "ring" in tr else set(tr["depth"])
    assert set(RING) <= members
    assert {"V1", "V2", "V3", "V4"} <= set(tr["sources"])


def test_roles_from_depth():
    roles = D.roles_from_depth({"A": 0, "B1": 1, "C1": 2})
    assert len(set(roles.values())) == 3


def test_cycle_detection(store, alert_time):
    cycles = D.detect_cycles(store, RING, alert_time - 600, alert_time + 600)
    assert any({"A", "C6"} <= set(c) for c in cycles)


def test_shared_attributes(store):
    found = {f["attr"]: set(f["accounts"]) for f in D.shared_attributes(store, RING)}
    assert found["D-9001"] == {"A", "B2"}
    assert found["D-4471"] == {"B3", "C5", "C6"}
    assert found["VPN-17"] == {"B1", "C1", "C2"}


def test_indian_formatting():
    assert D.inr(4000000) == "₹40,00,000"
    assert D.inr(784400) == "₹7,84,400"


def test_tier_cap_kill_switch():
    cf = {"legit_counterparties": 0, "legit_volume": 0}
    capped = choose_tier(0.99, "collector", 40_00_000, cf, max_tier=1)
    tier = capped[0] if isinstance(capped, tuple) else capped
    assert tier <= 1
