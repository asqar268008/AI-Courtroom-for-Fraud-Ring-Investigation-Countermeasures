"""Synthetic data generator.

* build_demo_scenario(): the exact story used in the live demo (A -> B1..B3 -> C1..C6, merchant C3).
* make_evasion_case() / make_framing_case(): labelled cases for the moot court.

Every scenario is a dict with "accounts", "transactions", "logins" lists (same columns as the CSVs)
plus "meta" (alert seed, alert time, ground truth).
"""
from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

DAY = 86400.0
IST = timezone(timedelta(hours=5, minutes=30))
DEMO_T0 = datetime(2026, 9, 17, 3, 5, 0, tzinfo=IST).timestamp()


class ScenarioBuilder:
    def __init__(self, rng: random.Random, prefix: str = "T") -> None:
        self.rng = rng
        self.prefix = prefix
        self.accounts: list[dict] = []
        self.transactions: list[dict] = []
        self.logins: list[dict] = []
        self._n = 0

    def account(self, acc: str, created_at: float, merchant: bool = False, gst: bool = False, name: str = "") -> str:
        self.accounts.append({"account_id": acc, "created_at": round(created_at), "is_merchant": int(merchant),
                              "gst_registered": int(gst), "kyc_level": "full", "name": name or acc})
        return acc

    def txn(self, ts: float, src: str, dst: str, amount: float, rail: str = "UPI") -> None:
        self._n += 1
        self.transactions.append({"txn_id": f"{self.prefix}-{self._n:06d}", "timestamp": round(ts, 1),
                                  "from_account": src, "to_account": dst, "amount": round(amount, 2), "rail": rail})

    def login(self, acc: str, device: str, ip: str, vpn: bool = False) -> None:
        self.logins.append({"account_id": acc, "device_id": device, "ip_address": ip, "is_vpn": int(vpn)})

    def background(self, n: int, t0: float, days: int = 90, prefix: str = "L", merchants: int = 4,
                   txns_per_account: int = 25) -> tuple[list[str], list[str]]:
        """Legitimate people and shops with ordinary history."""
        rng = self.rng
        people = [self.account(f"{prefix}{i:02d}", t0 - rng.uniform(400, 3000) * DAY) for i in range(1, n + 1)]
        shops = [self.account(f"M{i}", t0 - rng.uniform(900, 4000) * DAY, merchant=True, gst=True,
                              name=f"Merchant {i}") for i in range(1, merchants + 1)]
        for p in people:
            self.login(p, f"DEV-{p}", f"IP-{p}")
        for s in shops:
            self.login(s, f"DEV-{s}", f"IP-{s}")
        for p in people:
            for _ in range(txns_per_account):
                ts = t0 - rng.uniform(0.5, days) * DAY
                if rng.random() < 0.6:
                    self.txn(ts, p, rng.choice(shops), rng.uniform(150, 6000))
                else:
                    self.txn(ts, p, rng.choice(people), rng.uniform(200, 15000))
            self.txn(t0 - rng.uniform(1, 28) * DAY, rng.choice(shops), p, rng.uniform(25000, 90000), rail="NEFT")
        return people, shops

    def scenario(self, meta: dict) -> dict:
        self.transactions.sort(key=lambda t: t["timestamp"])
        return {"accounts": self.accounts, "transactions": self.transactions, "logins": self.logins, "meta": meta}


# ----------------------------------------------------------------------------------------
# Demo scenario
# ----------------------------------------------------------------------------------------
def build_demo_scenario(seed: int = 7, t0: float = DEMO_T0) -> dict:
    rng = random.Random(seed)
    b = ScenarioBuilder(rng, prefix="TX")
    people, shops = b.background(40, t0)

    # Merchant C3: 4-year-old GST-registered shop with regular customers and a supplier.
    b.account("C3", t0 - 4.1 * 365 * DAY, merchant=True, gst=True, name="C3 Kirana Store")
    b.login("C3", "DEV-C3", "IP-C3")
    customers = rng.sample(people, 14)
    for c in customers:
        for _ in range(rng.randint(3, 6)):
            b.txn(t0 - rng.uniform(1, 90) * DAY, c, "C3", rng.uniform(300, 45000))
    for w in range(12):
        b.txn(t0 - (7 * w + rng.uniform(0, 2)) * DAY, "C3", shops[0], rng.uniform(40000, 90000), rail="NEFT")

    # Scam victims (legitimate, older accounts).
    victims = ["V1", "V2", "V3", "V4"]
    for v in victims:
        b.account(v, t0 - rng.uniform(1500, 5000) * DAY, name=f"Victim {v}")
        b.login(v, f"DEV-{v}", f"IP-{v}")
        for _ in range(8):
            b.txn(t0 - rng.uniform(2, 60) * DAY, v, rng.choice(shops), rng.uniform(500, 8000))

    # The ring: new accounts.
    b.account("A", t0 - 18 * DAY, name="A")
    for acc, age in [("B1", 16), ("B2", 15), ("B3", 17), ("C1", 11), ("C2", 12), ("C4", 10), ("C5", 9), ("C6", 13)]:
        b.account(acc, t0 - age * DAY, name=acc)
    b.login("A", "D-9001", "IP-A")
    b.login("B2", "D-9001", "IP-B2")
    for acc in ["B3", "C5", "C6"]:
        b.login(acc, "D-4471", f"IP-{acc}")
    for acc in ["B1", "C1", "C2"]:
        b.login(acc, f"DEV-{acc}", "VPN-17", vpn=True)
    for acc in ["C4"]:
        b.login(acc, f"DEV-{acc}", f"IP-{acc}")

    L = 100000.0  # one lakh
    live = [
        (0, "V1", "A", 10 * L), (12, "V2", "A", 10 * L), (25, "V3", "A", 10 * L), (38, "V4", "A", 10 * L),
        (55, "A", "B1", 13 * L), (62, "A", "B2", 14 * L), (70, "A", "B3", 13 * L),
        (90, "B1", "C1", 6.5 * L), (96, "B1", "C2", 6.5 * L),
        (100, "B2", "C3", 38000.0), (104, "B2", "C4", 13.62 * L),
        (108, "B3", "C5", 6.5 * L), (112, "B3", "C6", 6.5 * L),
        (150, "C6", "A", 2 * L),
    ]
    for dt, src, dst, amt in live:
        b.txn(t0 + dt, src, dst, amt, rail="IMPS" if amt > 5 * L else "UPI")

    ring = ["A", "B1", "B2", "B3", "C1", "C2", "C3", "C4", "C5", "C6"]
    truth = {acc: acc != "C3" for acc in ring}
    meta = {"t0": t0, "live_start": t0, "seed": "A", "ground_truth": truth,
            "description": "Scam proceeds from 4 victims laundered through A, 3 layering and 6 mule accounts."}
    return b.scenario(meta)


def write_scenario(scenario: dict, folder: Path) -> None:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("accounts", "transactions", "logins"):
        rows = scenario[name]
        with open(folder / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)


# ----------------------------------------------------------------------------------------
# Moot court: evasion cases (fraud disguised) and framing cases (innocent that looks bad)
# ----------------------------------------------------------------------------------------
def default_evasion_params(rng: random.Random) -> dict:
    return {
        "layers": rng.randint(1, 2),
        "fan": rng.randint(2, 3),
        "hop_delay_s": rng.uniform(20, 900),
        "mule_age_days": rng.uniform(5, 200),
        "shared_prob": rng.uniform(0.0, 1.0),
        "cycle": rng.random() < 0.5,
        "decoy_ratio": rng.uniform(0.0, 0.2),
        "total_lakh": rng.uniform(5, 60),
    }


def make_evasion_case(params: dict, rng: random.Random, t0: float = DEMO_T0) -> dict:
    b = ScenarioBuilder(rng, prefix="EV")
    people, shops = b.background(12, t0, txns_per_account=10)
    L = 100000.0
    total = params["total_lakh"] * L
    victims = [b.account(f"V{i}", t0 - rng.uniform(900, 4000) * DAY) for i in range(1, 4)]
    for v in victims:
        b.login(v, f"DEV-{v}", f"IP-{v}")
    for i, v in enumerate(victims):
        b.txn(t0 + i * 10, v, "R0", total / len(victims))

    ring: list[str] = []
    seed = b.account("R0", t0 - rng.uniform(5, 40) * DAY)
    ring.append(seed)
    b.login(seed, "DEV-R0", "IP-R0")
    fan = params["fan"]
    if params["layers"] >= 2 and fan > 2:
        fan = 2  # keep rings at a readable size (<= 15 accounts)

    shared_device = f"DEV-SHARED-{rng.randint(100, 999)}"
    arrival = {seed: t0 + 10 * (len(victims) - 1)}
    amounts = {seed: total}
    layer_nodes = [seed]
    max_gap = 0.0
    node_i = 0
    for _layer in range(params["layers"] + 1):
        next_nodes = []
        for parent in layer_nodes:
            for j in range(fan):
                gap = params["hop_delay_s"] * rng.uniform(0.8, 1.2) + j * rng.uniform(2, 8)
                max_gap = max(max_gap, gap + (arrival[parent] - t0 if parent == seed else 0))
                node_i += 1
                acc = b.account(f"R{node_i}", t0 - params["mule_age_days"] * rng.uniform(0.7, 1.3) * DAY)
                ring.append(acc)
                if rng.random() < params["shared_prob"]:
                    b.login(acc, shared_device, f"IP-{acc}")
                else:
                    b.login(acc, f"DEV-{acc}", f"IP-{acc}")
                share = amounts[parent] / fan
                amounts[acc] = share
                arrival[acc] = arrival[parent] + gap
                b.txn(arrival[acc], parent, acc, share, rail="IMPS")
                next_nodes.append(acc)
                # decoys: give the mule a small legitimate-looking history
                if rng.random() < params["decoy_ratio"] * 2:
                    for _ in range(rng.randint(3, 6)):
                        b.txn(t0 - rng.uniform(1, 30) * DAY, rng.choice(people), acc, rng.uniform(500, 5000))
                        b.txn(t0 - rng.uniform(1, 30) * DAY, acc, rng.choice(shops), rng.uniform(200, 3000))
        layer_nodes = next_nodes
    if rng.random() < params["shared_prob"]:
        b.login(seed, shared_device, "IP-R0")
    if params["cycle"]:
        last = layer_nodes[-1]
        b.txn(arrival[last] + params["hop_delay_s"], last, seed, amounts[last] * 0.3)

    truth = {acc: True for acc in ring}
    meta = {"t0": t0, "seed": seed, "alert_time": t0 + 10 * len(victims) + 1, "ground_truth": truth, "kind": "evasion",
            "max_hop_gap_s": max_gap, "params": params}
    return b.scenario(meta)


FRAMING_KINDS = ["payroll", "family_phone", "festival", "kirana"]


def make_framing_case(kind: str, rng: random.Random, t0: float = DEMO_T0) -> dict:
    b = ScenarioBuilder(rng, prefix="FR")
    people, shops = b.background(12, t0, txns_per_account=10)
    truth: dict[str, bool] = {}

    if kind == "payroll":
        emp = b.account("E0", t0 - rng.uniform(800, 3000) * DAY, merchant=True, gst=True, name="Employer")
        b.login(emp, "DEV-E0", "IP-E0")
        client = b.account("CL1", t0 - 2000 * DAY, merchant=True, gst=True)
        n = rng.randint(6, 14)
        staff = []
        for i in range(n):
            acc = b.account(f"S{i:02d}", t0 - rng.uniform(300, 3000) * DAY)
            b.login(acc, f"DEV-{acc}", f"IP-{acc}")
            staff.append(acc)
            # some staff have other relationships, some don't
            for _ in range(rng.choice([0, 0, 1, 4])):
                b.txn(t0 - rng.uniform(2, 28) * DAY, acc, rng.choice(shops), rng.uniform(300, 4000))
        salary = {s: rng.uniform(18000, 60000) for s in staff}
        for month in (60, 30):
            for s in staff:
                b.txn(t0 - month * DAY + rng.uniform(0, 3600), emp, s, salary[s], rail="NEFT")
        for _ in range(3):
            b.txn(t0 - rng.uniform(3, 50) * DAY, client, emp, sum(salary.values()) * 1.2, rail="NEFT")
        b.txn(t0, client, emp, sum(salary.values()) * 1.1, rail="IMPS")
        for i, s in enumerate(staff):
            b.txn(t0 + 25 + i * 3, emp, s, salary[s], rail="IMPS")
        seed = emp
        truth = {a: False for a in [emp] + staff}
        alert_time = t0 + 25 + n * 3 + 1

    elif kind == "family_phone":
        members = []
        for i in range(rng.randint(3, 4)):
            acc = b.account(f"F{i}", t0 - rng.uniform(500, 4000) * DAY)
            b.login(acc, "DEV-FAMILY", "IP-HOME")
            members.append(acc)
            for _ in range(rng.randint(2, 6)):
                b.txn(t0 - rng.uniform(2, 60) * DAY, acc, rng.choice(shops), rng.uniform(200, 5000))
        buyer = b.account("GOLDBUYER", t0 - 3000 * DAY, merchant=True, gst=True)
        head = members[0]
        amount = rng.uniform(4, 9) * 100000
        b.txn(t0, buyer, head, amount, rail="IMPS")
        for i, m in enumerate(members[1:]):
            b.txn(t0 + 30 + i * 8, head, m, amount / len(members), rail="IMPS")
        seed = head
        truth = {a: False for a in members}
        alert_time = t0 + 30 + len(members) * 8

    else:  # festival or kirana: merchant with a burst of small inflows who pays suppliers quickly
        shop = b.account("K0", t0 - rng.uniform(700, 3000) * DAY, merchant=True, gst=True)
        b.login(shop, "DEV-K0", "IP-K0")
        n_in = 40 if kind == "festival" else 25
        total = 0.0
        for i in range(n_in):
            amt = rng.uniform(200, 9000 if kind == "festival" else 2500)
            total += amt
            b.txn(t0 - 540 + i * 12, rng.choice(people), shop, amt)
        for _ in range(30):
            b.txn(t0 - rng.uniform(2, 60) * DAY, rng.choice(people), shop, rng.uniform(100, 3000))
        suppliers = rng.sample(shops, 3)
        for i, s in enumerate(suppliers):
            b.txn(t0 + 10 + i * 6, shop, s, total / 4, rail="IMPS")
        seed = shop
        truth = {shop: False, **{s: False for s in suppliers}}
        alert_time = t0 + 30

    meta = {"t0": t0, "seed": seed, "alert_time": alert_time, "ground_truth": truth, "kind": kind,
            "max_hop_gap_s": 0.0, "params": {"kind": kind}}
    return b.scenario(meta)
