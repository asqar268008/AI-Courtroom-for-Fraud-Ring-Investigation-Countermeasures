"""Tests for Countermeasures: BankService actions, customer notices, and reporting."""
import pytest
from services.bank_service import BankService
from agents.countermeasures.countermeasure_agent import customer_template
from agents.countermeasures.reporting_agent import build_sar, write_sar


def test_bank_service_tiers():
    bank = BankService()
    res4 = bank.apply(4, ["ACC_01"], "CASE_1")
    assert res4["status"] == 200
    assert bank.get("ACC_01")["status"] == "hard_frozen"

    res3 = bank.apply(3, ["ACC_02"], "CASE_1")
    assert bank.get("ACC_02")["status"] == "soft_frozen"

    res2 = bank.apply(2, ["ACC_03"], "CASE_1")
    assert bank.get("ACC_03")["status"] == "step_up_pending"

    unf = bank.unfreeze("ACC_01")
    assert unf["status"] == 200
    assert bank.get("ACC_01")["status"] == "active"


def test_customer_templates():
    tpl4 = customer_template(4, 150000.0)
    assert "paused" in tpl4.lower() or "blocked" in tpl4.lower() or "transfers" in tpl4.lower()

    tpl2 = customer_template(2, 50000.0)
    assert "confirm" in tpl2.lower() or "checking" in tpl2.lower()


def test_sar_generation(golden):
    state = golden["state"]
    case_id = state.get("case_id", "FR-TEST-GOLD")
    sar = build_sar(case_id, state, [], "test_audit_hash")
    assert sar["case_id"] == case_id
    assert "summary" in sar
    json_path, pdf_path = write_sar(sar)
    assert json_path.exists()
