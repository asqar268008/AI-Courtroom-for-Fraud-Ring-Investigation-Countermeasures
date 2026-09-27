"""Mock core-banking API: freeze, soft-freeze, step-up and unfreeze.

The Countermeasure agent calls these functions directly; the same functions are exposed over HTTP
under /bank/* so the endpoints can be demonstrated or called by other systems.
"""
from __future__ import annotations

import time
from threading import Lock

STATUS_BY_TIER = {2: "step_up_pending", 3: "soft_frozen", 4: "hard_frozen"}
ENDPOINT_BY_TIER = {2: "/bank/step-up", 3: "/bank/soft-freeze", 4: "/bank/freeze"}


class MockBank:
    def __init__(self) -> None:
        self._lock = Lock()
        self.status: dict[str, dict] = {}
        self.log: list[dict] = []

    def apply(self, tier: int, accounts: list[str], case_id: str, reason: str = "") -> dict:
        with self._lock:
            now = time.time()
            for acc in accounts:
                self.status[acc] = {"status": STATUS_BY_TIER[tier], "tier": tier, "case_id": case_id, "at": now}
            entry = {"endpoint": ENDPOINT_BY_TIER[tier], "accounts": accounts, "status": 200,
                     "case_id": case_id, "reason": reason, "at": now}
            self.log.append(entry)
            return entry

    def unfreeze(self, account: str, by: str = "analyst") -> dict:
        with self._lock:
            self.status[account] = {"status": "active", "tier": 0, "case_id": None, "at": time.time(), "by": by}
            return {"endpoint": "/bank/unfreeze", "accounts": [account], "status": 200}

    def get(self, account: str) -> dict:
        return self.status.get(account, {"status": "active", "tier": 0})

    def reset(self) -> None:
        with self._lock:
            self.status.clear()
            self.log.clear()


BankService = MockBank
