"""Alert trigger: cheap rules that decide WHEN to open an investigation (not whether it is fraud)."""
from __future__ import annotations

from typing import Optional

from database.graph_store import GraphStore

LAKH = 100000.0


class AlertTrigger:
    def __init__(self, min_inflow: float = 10 * LAKH, burst_outflows: int = 3, burst_window_s: float = 120,
                 inflow_window_s: float = 600, cooldown_s: float = 3600) -> None:
        self.min_inflow = min_inflow
        self.burst_outflows = burst_outflows
        self.burst_window_s = burst_window_s
        self.inflow_window_s = inflow_window_s
        self.cooldown_s = cooldown_s
        self._last_alert: dict[str, float] = {}

    def reset(self) -> None:
        self._last_alert.clear()

    def check(self, store: GraphStore, txn: dict) -> Optional[tuple[str, str]]:
        """Called after every ingested transaction. Returns (seed_account, reason) when a rule fires."""
        acc, now = txn["src"], txn["ts"]
        if now - self._last_alert.get(acc, -1e18) < self.cooldown_s:
            return None
        inflow = sum(t["amount"] for t in store.in_txns(acc, now - self.inflow_window_s, now))
        outs = store.out_txns(acc, now - self.burst_window_s, now)
        if inflow >= self.min_inflow and len(outs) >= self.burst_outflows:
            self._last_alert[acc] = now
            return acc, f"outflow velocity: {len(outs)} transfers in {self.burst_window_s:.0f} s after a large inflow"
        recent_in = store.in_txns(acc, now - 60, now)
        amount_in = sum(t["amount"] for t in recent_in)
        amount_out = sum(t["amount"] for t in store.out_txns(acc, now - 60, now))
        if amount_in >= 5 * LAKH and amount_out >= 0.8 * amount_in:
            self._last_alert[acc] = now
            return acc, "rapid pass-through: 80% of a large inflow forwarded within 60 s"
        return None


AlertDispatcher = AlertTrigger
