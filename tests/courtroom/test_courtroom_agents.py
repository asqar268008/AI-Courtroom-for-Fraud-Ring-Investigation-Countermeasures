"""Tests for Courtroom Agents: Prosecutor, Defense, Judge, and Decision Logic."""
import pytest
from agents.courtroom.defense_agent import exculpatory, established
from core.decision import counterfactual, choose_tier, action_name


def test_golden_case_verdicts(golden):
    state = golden["state"]
    verdict = state["verdict"]
    assert verdict.confidence >= 0.8
    assert verdict.counterfactual_ok is True

    tiers = {v.node_id: v.tier for v in verdict.node_verdicts}
    assert tiers["A"] == 4   # Hard freeze
    assert tiers["B1"] == 4  # Hard freeze
    assert tiers["C3"] == 2  # Step up (legitimate business defended)


def test_defense_exculpatory_helpers():
    f_legit = {
        "age_days": 1500,
        "legit_cp": 5,
        "shared_attr": False,
        "in_cycle": False,
        "amount_consistent": True,
        "recurring_ratio": 0.9,
    }
    assert exculpatory(f_legit) is True

    f_mule = {
        "age_days": 10,
        "legit_cp": 0,
        "shared_attr": True,
        "in_cycle": True,
        "amount_consistent": False,
        "recurring_ratio": 0.0,
    }
    assert exculpatory(f_mule) is False


def test_decision_choose_tier():
    cf_clean = {"legit_volume": 0, "legit_counterparties": 0}
    t4, _ = choose_tier(p=0.95, role="controller", exposure=500000.0, cf=cf_clean, max_tier=4)
    assert t4 == 4

    cf_noisy = {"legit_volume": 100000.0, "legit_counterparties": 10}
    t_low, _ = choose_tier(p=0.2, role="mule", exposure=50000.0, cf=cf_noisy, max_tier=4)
    assert t_low in (0, 1, 2)
