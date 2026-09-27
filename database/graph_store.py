"""In-memory graph of accounts, transfers, devices and IPs.

Kept behind this small class so it can be swapped for Neo4j later without touching the agents.
Timestamps are epoch seconds (float).
"""
from __future__ import annotations

import bisect
import csv
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Optional

import networkx as nx

DAY = 86400.0


class GraphStore:
    def __init__(self) -> None:
        self.accounts: dict[str, dict] = {}
        self.txns: dict[str, dict] = {}
        self._out: dict[str, list[tuple[float, str]]] = defaultdict(list)  # acc -> sorted (ts, txn_id)
        self._in: dict[str, list[tuple[float, str]]] = defaultdict(list)
        self.devices_of: dict[str, set[str]] = defaultdict(set)
        self.ips_of: dict[str, set[str]] = defaultdict(set)
        self.accounts_on_device: dict[str, set[str]] = defaultdict(set)
        self.accounts_on_ip: dict[str, set[str]] = defaultdict(set)
        self.vpn_ips: set[str] = set()

    # ---------- loading ----------
    def add_account(self, account_id: str, created_at: Optional[float] = None, is_merchant: bool = False,
                    gst_registered: bool = False, kyc_level: str = "full", name: str = "") -> None:
        self.accounts[account_id] = {
            "account_id": account_id, "created_at": created_at, "is_merchant": bool(is_merchant),
            "gst_registered": bool(gst_registered), "kyc_level": kyc_level, "name": name or account_id,
        }

    def ensure_account(self, account_id: str) -> None:
        if account_id not in self.accounts:
            self.add_account(account_id)

    def add_txn(self, txn_id: str, ts: float, src: str, dst: str, amount: float, rail: str = "UPI") -> dict:
        self.ensure_account(src)
        self.ensure_account(dst)
        txn = {"txn_id": txn_id, "ts": float(ts), "src": src, "dst": dst, "amount": float(amount), "rail": rail}
        self.txns[txn_id] = txn
        bisect.insort(self._out[src], (txn["ts"], txn_id))
        bisect.insort(self._in[dst], (txn["ts"], txn_id))
        return txn

    def add_login(self, account_id: str, device_id: str, ip: str, is_vpn: bool = False) -> None:
        self.ensure_account(account_id)
        if device_id:
            self.devices_of[account_id].add(device_id)
            self.accounts_on_device[device_id].add(account_id)
        if ip:
            self.ips_of[account_id].add(ip)
            self.accounts_on_ip[ip].add(account_id)
            if is_vpn:
                self.vpn_ips.add(ip)

    @classmethod
    def from_records(cls, accounts: Iterable[dict], transactions: Iterable[dict], logins: Iterable[dict]) -> "GraphStore":
        store = cls()
        for a in accounts:
            store.add_account(a["account_id"], _num(a.get("created_at")), _bool(a.get("is_merchant")),
                              _bool(a.get("gst_registered")), a.get("kyc_level", "full"), a.get("name", ""))
        for t in transactions:
            store.add_txn(t["txn_id"], float(t["timestamp"]), t["from_account"], t["to_account"],
                          float(t["amount"]), t.get("rail", "UPI"))
        for l in logins:
            store.add_login(l["account_id"], l.get("device_id", ""), l.get("ip_address", ""), _bool(l.get("is_vpn")))
        return store

    @classmethod
    def from_csv(cls, folder: Path, max_ts: Optional[float] = None) -> "GraphStore":
        folder = Path(folder)
        accounts = _read(folder / "accounts.csv")
        txns = _read(folder / "transactions.csv")
        if max_ts is not None:
            txns = [t for t in txns if float(t["timestamp"]) < max_ts]
        logins = _read(folder / "logins.csv")
        return cls.from_records(accounts, txns, logins)

    # ---------- queries ----------
    def out_txns(self, acc: str, start: float = float("-inf"), end: float = float("inf")) -> list[dict]:
        return self._slice(self._out.get(acc, []), start, end)

    def in_txns(self, acc: str, start: float = float("-inf"), end: float = float("inf")) -> list[dict]:
        return self._slice(self._in.get(acc, []), start, end)

    def _slice(self, idx: list[tuple[float, str]], start: float, end: float) -> list[dict]:
        lo = bisect.bisect_left(idx, (start, ""))
        hi = bisect.bisect_right(idx, (end, "\uffff"))
        return [self.txns[tid] for _, tid in idx[lo:hi]]

    def age_days(self, acc: str, now: float) -> Optional[float]:
        created = self.accounts.get(acc, {}).get("created_at")
        return None if created is None else max(0.0, (now - created) / DAY)

    def counterparties(self, acc: str, start: float, end: float) -> dict[str, float]:
        """Counterparty -> total volume (both directions) in [start, end]."""
        vol: dict[str, float] = defaultdict(float)
        for t in self.out_txns(acc, start, end):
            vol[t["dst"]] += t["amount"]
        for t in self.in_txns(acc, start, end):
            vol[t["src"]] += t["amount"]
        return dict(vol)

    def transfer_digraph(self, accounts: Iterable[str], start: float, end: float) -> nx.DiGraph:
        """Directed graph of transfers among the given accounts within a time window."""
        accs = set(accounts)
        g = nx.DiGraph()
        g.add_nodes_from(accs)
        for a in accs:
            for t in self.out_txns(a, start, end):
                if t["dst"] in accs:
                    if g.has_edge(a, t["dst"]):
                        g[a][t["dst"]]["amount"] += t["amount"]
                    else:
                        g.add_edge(a, t["dst"], amount=t["amount"], ts=t["ts"])
        return g

    def latest_ts(self) -> float:
        return max((t["ts"] for t in self.txns.values()), default=0.0)


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _num(v) -> Optional[float]:
    if v in (None, ""):
        return None
    return float(v)


def _bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y")
