"""Shared runtime and dependency injection providers."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from app.config import DATA_DIR, FRONTEND_DIR, settings
from core.events import EventBus
from core.llm import LLM
from database.graph_store import GraphStore
from security.audit import AuditLog
from services.alert_service import AlertTrigger
from services.bank_service import MockBank
from agents.moot_court.precedent_memory import Memory
from core.simulator import build_demo_scenario, write_scenario

DEMO_DIR = DATA_DIR / "demo"


class Runtime:
    """Shared state container across API requests and background workers."""

    def __init__(self) -> None:
        self.bus = EventBus()
        self.bank = MockBank()
        self.audit = AuditLog()
        self.memory = Memory()
        self.llm = LLM(settings)
        self.trigger = AlertTrigger()
        self.meta: dict = {}
        self.live_txns: list[dict] = []
        self.store: GraphStore = GraphStore()
        self.cases: dict[str, dict] = {}
        self.demo_task: Optional[asyncio.Task] = None
        self.moot_task: Optional[asyncio.Task] = None
        self.case_tasks: set[asyncio.Task] = set()
        self.watch: dict[str, float] = {}  # account -> data time until which it belongs to an open case
        self.finals: dict[str, tuple] = {}  # case_id -> (final state, context), for analyst review
        self.stats = {"monitored": 0, "suspicious": 0, "held": 0}

    def linked_to_open_case(self, account: str, ts: float) -> bool:
        """True if the account is already inside (or directly downstream of) an open investigation."""
        if self.watch.get(account, -1) >= ts:
            return True
        for t in self.store.in_txns(account, ts - 3600, ts):
            if self.watch.get(t["src"], -1) >= ts:
                self.watch[account] = ts + 3600
                return True
        return False

    def load_demo(self) -> None:
        if not (DEMO_DIR / "transactions.csv").exists():
            scenario = build_demo_scenario()
            write_scenario(scenario, DEMO_DIR)
            (DEMO_DIR / "meta.json").write_text(json.dumps(scenario["meta"], indent=1))
        self.meta = json.loads((DEMO_DIR / "meta.json").read_text())
        live_start = self.meta["live_start"]
        self.store = GraphStore.from_csv(DEMO_DIR, max_ts=live_start)
        full = GraphStore.from_csv(DEMO_DIR)
        self.live_txns = sorted((t for t in full.txns.values() if t["ts"] >= live_start), key=lambda t: t["ts"])
        self.trigger.reset()
        self.watch.clear()
        self.bank.reset()

    def load_scenario(self, sc: dict) -> None:
        self.meta = sc["meta"]
        live_start = self.meta["live_start"]
        history = [t for t in sc["transactions"] if float(t["timestamp"]) < live_start]
        self.store = GraphStore.from_records(sc["accounts"], history, sc["logins"])
        full = GraphStore.from_records(sc["accounts"], sc["transactions"], sc["logins"])
        self.live_txns = sorted((t for t in full.txns.values() if t["ts"] >= live_start), key=lambda t: t["ts"])
        self.trigger.reset()
        self.watch.clear()
        self.bank.reset()


rt = Runtime()


def get_runtime() -> Runtime:
    return rt
