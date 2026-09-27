"""Countermeasures and Core-Banking routes."""
from __future__ import annotations

from fastapi import APIRouter
from app.dependencies import rt
from app.schemas import BankIn

router = APIRouter()


@router.get("/bank/status/{account}")
def bank_status(account: str):
    return rt.bank.get(account)


@router.post("/bank/step-up")
def bank_step_up(b: BankIn):
    return rt.bank.apply(2, b.accounts, b.case_id)


@router.post("/bank/soft-freeze")
def bank_soft_freeze(b: BankIn):
    return rt.bank.apply(3, b.accounts, b.case_id)


@router.post("/bank/freeze")
def bank_hard_freeze(b: BankIn):
    return rt.bank.apply(4, b.accounts, b.case_id)


@router.post("/bank/unfreeze")
def bank_unfreeze(b: BankIn):
    for acc in b.accounts:
        rt.bank.unfreeze(acc)
    return {"status": 200, "accounts": b.accounts}
