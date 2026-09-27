"""Tests for Investigation Agents: Transaction Agent, Network Agent, Behavior Agent, Graph Scout, and Pattern Aggregator."""
import pytest
from database.graph_store import GraphStore
from agents.investigation.transaction_agent import score_txn
from agents.investigation.network_agent import trace_money_trail, detect_cycles, find_shared_infrastructure
from agents.investigation.behavior_agent import compute_dwell_times, extract_account_features, precedent_vector
from agents.investigation.graph_investigation_agent import structure_of, build_display_graph
from agents.investigation.evidence_aggregation_agent import node_risk, explain_risk, feature_vector


def test_money_trail_tracing(store, alert_time):
    trace = trace_money_trail(store, "A", alert_time, 600, 600, 4)
    assert "A" in trace["ring"]
    assert "B1" in trace["ring"]
    assert trace["depth"]["A"] == 0
    assert trace["depth"]["B1"] == 1


def test_cycle_detection(store, alert_time):
    cycles = detect_cycles(store, ["A", "B3", "C6"], alert_time - 3600, alert_time + 3600)
    assert isinstance(cycles, list)


def test_shared_infrastructure(store):
    shared = find_shared_infrastructure(store, ["B3", "C5", "C6"])
    assert any(s["kind"] == "shared_device" for s in shared)


def test_score_txn(store):
    risk, why = score_txn(store, {"src": "A", "dst": "B1", "amount": 1300000.0, "ts": 1726522555.0})
    assert isinstance(risk, float)
    assert isinstance(why, list)
