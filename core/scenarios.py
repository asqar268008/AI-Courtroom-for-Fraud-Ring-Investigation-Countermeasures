"""Scenario library — a different case every run.

Each generator takes a random seed and returns a scenario dict (accounts, transactions, logins, meta)
in exactly the same shape as the demo, so the API, the court, the tests and the reports all work
unchanged. Account IDs, amounts, ring sizes, timings, devices and innocent bystanders are all
randomised; `meta` carries the ground truth and what a correct court should do.

Fraud typologies
  mule_ring          victims -> collector -> layering -> mules, optional round-trip, a kirana in the path
  smurfing_fan_in    30-45 small UPI payments from scam victims, forwarded to mules on one VPN
  gather_scatter     big inflows scattered to 6-10 mules who cash out through an old, busy gold dealer
  layered_chain      a deep, slower chain (2-7 min hops) with aged mule accounts and decoy history
Innocent look-alikes (the Defense must win these)
  payroll_run        employer receives a client payment and pays 8-14 staff within two minutes
  wholesale_festival Diwali distributor collects from retailers and pays suppliers at once
  family_gold_sale   gold sale proceeds split between family members who share one phone

Use:  build_scenario("smurfing_fan_in", seed=42)   or   random_scenario(seed=None)
"""
from __future__ import annotations

import random
from typing import Callable, Optional

from core.simulator import DAY, DEMO_T0, ScenarioBuilder, build_demo_scenario

L = 100000.0  # one lakh


class Namer:
    """Short, unique, readable account IDs (X47, M203 ...) so every run looks different."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.used: set[str] = set()

    def __call__(self, prefix: str) -> str:
        while True:
            name = f"{prefix}{self.rng.randint(10, 99)}"
            if name not in self.used:
                self.used.add(name)
                return name


def _base(rng: random.Random, t0: float, people: int = 30) -> tuple[ScenarioBuilder, list[str], list[str]]:
    b = ScenarioBuilder(rng, prefix="TX")
    ppl, shops = b.background(people, t0, txns_per_account=12)
    return b, ppl, shops


def _new(b: ScenarioBuilder, name: str, age_days: float, t0: float, device: Optional[str] = None,
         ip: Optional[str] = None, vpn: bool = False, merchant: bool = False, gst: bool = False, label: str = "") -> str:
    b.account(name, t0 - age_days * DAY, merchant=merchant, gst=gst, name=label or name)
    b.login(name, device or f"DEV-{name}", ip or f"IP-{name}", vpn=vpn)
    return name


def _history(b: ScenarioBuilder, rng: random.Random, acc: str, t0: float, cps: list[str], n: int,
             lo: float = 300, hi: float = 5000, incoming: bool = True) -> None:
    for _ in range(n):
        ts = t0 - rng.uniform(1, 28) * DAY
        if incoming:
            b.txn(ts, rng.choice(cps), acc, rng.uniform(lo, hi))
        else:
            b.txn(ts, acc, rng.choice(cps), rng.uniform(lo, hi))


def _victims(b: ScenarioBuilder, rng: random.Random, namer: Namer, t0: float, n: int) -> list[str]:
    return [_new(b, namer("V"), rng.uniform(400, 4000), t0) for _ in range(n)]


def _meta(t0: float, seed: str, truth: dict, kind: str, title: str, typology: str, description: str,
          expected: str, **extra) -> dict:
    return {"t0": t0, "live_start": t0, "seed": seed, "ground_truth": truth, "kind": kind, "title": title,
            "typology": typology, "description": description, "expected": expected, **extra}


# ----------------------------------------------------------------------------------------
# Fraud typologies
# ----------------------------------------------------------------------------------------
def mule_ring(seed: int, t0: float = DEMO_T0) -> dict:
    rng = random.Random(seed)
    namer = Namer(rng)
    b, ppl, shops = _base(rng, t0)
    victims = _victims(b, rng, namer, t0, rng.randint(3, 6))
    dev1, dev2, vpn = f"D-{rng.randint(1000, 9999)}", f"D-{rng.randint(1000, 9999)}", f"VPN-{rng.randint(10, 99)}"
    x = _new(b, namer("A"), rng.uniform(8, 40), t0, device=dev1)
    n1 = rng.randint(2, 4)
    layer1 = []
    for i in range(n1):
        acc = namer("B")
        dev = dev1 if i == 0 else (dev2 if i == n1 - 1 else None)
        _new(b, acc, rng.uniform(5, 30), t0, device=dev, ip=vpn if i == 1 else None, vpn=i == 1)
        layer1.append(acc)
    total = 0.0
    for i, v in enumerate(victims):
        amt = rng.uniform(5, 15) * L
        total += amt
        b.txn(t0 + i * rng.uniform(8, 14), v, x, amt, rail="IMPS")
    t = t0 + len(victims) * 12 + rng.uniform(10, 20)
    arrival = {}
    for acc in layer1:
        t += rng.uniform(4, 9)
        b.txn(t, x, acc, total * 0.97 / n1, rail="IMPS")
        arrival[acc] = t
    ring = [x] + layer1
    mules = []
    kirana = None
    if rng.random() < 0.75:  # an innocent shop the ring buys from (the Defense's client)
        kirana = _new(b, namer("K"), rng.uniform(2.5, 7) * 365, t0, merchant=True, gst=True, label="Kirana store")
        _history(b, rng, kirana, t0, ppl, rng.randint(25, 40), 200, 4000)
        b.txn(t0 - rng.uniform(2, 20) * DAY, kirana, rng.choice(shops), rng.uniform(40000, 90000), rail="NEFT")
    for j, parent in enumerate(layer1):
        share = total * 0.97 / n1
        for k in range(rng.randint(1, 3)):
            m = namer("C")
            dev = dev2 if (parent == layer1[-1] and k < 2) else None
            _new(b, m, rng.uniform(3, 25), t0, device=dev, ip=vpn if parent == layer1[min(1, n1 - 1)] else None,
                 vpn=parent == layer1[min(1, n1 - 1)])
            arrival[parent] += rng.uniform(4, 30)
            b.txn(arrival[parent], parent, m, share * rng.uniform(0.3, 0.5), rail="IMPS")
            arrival[m] = arrival[parent]
            mules.append(m)
        if kirana and j == 0:
            arrival[parent] += rng.uniform(3, 8)
            b.txn(arrival[parent], parent, kirana, rng.uniform(15000, 45000))
    ring += mules
    cycle = rng.random() < 0.6
    if cycle:
        last = mules[-1]
        b.txn(arrival[last] + rng.uniform(20, 60), last, x, rng.uniform(1, 3) * L, rail="IMPS")
    truth = {a: True for a in ring}
    if kirana:
        truth[kirana] = False
    return b.scenario(_meta(
        t0, x, truth, "mule_ring", "Mule ring at 3 AM", "Layered mule network" + (" with round-trip" if cycle else ""),
        f"{len(victims)} scam victims pay {x}; the money is layered through {n1} accounts to {len(mules)} mules "
        f"in under three minutes." + (f" {kirana} is a real shop that happened to be paid." if kirana else ""),
        "Freeze the ring; " + (f"protect {kirana} with a step-up only." if kirana else "no innocent account involved.")))


def smurfing_fan_in(seed: int, t0: float = DEMO_T0) -> dict:
    rng = random.Random(seed)
    namer = Namer(rng)
    b, ppl, shops = _base(rng, t0)
    n = rng.randint(30, 45)
    victims = _victims(b, rng, namer, t0, n)
    x = _new(b, namer("A"), rng.uniform(10, 45), t0, device=f"D-{rng.randint(1000, 9999)}")
    vpn = f"VPN-{rng.randint(10, 99)}"
    total = 0.0
    start = t0 - 540
    for i, v in enumerate(victims):
        amt = rng.uniform(22000, 49000)  # small, UPI-sized payments from a task/investment scam
        total += amt
        b.txn(start + i * (520 / n), v, x, amt)
    mules = [_new(b, namer("M"), rng.uniform(3, 20), t0, ip=vpn, vpn=True) for _ in range(rng.randint(3, 5))]
    t = t0 + 5
    for m in mules:
        t += rng.uniform(5, 15)
        b.txn(t, x, m, total * 0.95 / len(mules), rail="IMPS")
    cashout = []
    for m in mules:
        for _ in range(rng.randint(1, 2)):
            c = _new(b, namer("W"), rng.uniform(2, 15), t0, ip=vpn, vpn=True)
            b.txn(t + rng.uniform(20, 90), m, c, total * 0.4 / len(mules), rail="IMPS")
            cashout.append(c)
    truth = {a: True for a in [x] + mules + cashout}
    return b.scenario(_meta(
        t0, x, truth, "smurfing_fan_in", "Smurfing: 40 small payments", "Fan-in smurfing to VPN mules",
        f"{n} people fall for a task scam and send small UPI payments to {x} over nine minutes; {x} forwards "
        f"them to {len(mules)} mules who share a VPN exit, then to {len(cashout)} cash-out wallets.",
        "Freeze the collector and mules; the senders are victims, not suspects."))


def gather_scatter(seed: int, t0: float = DEMO_T0) -> dict:
    rng = random.Random(seed)
    namer = Namer(rng)
    b, ppl, shops = _base(rng, t0)
    victims = _victims(b, rng, namer, t0, rng.randint(4, 6))
    x = _new(b, namer("A"), rng.uniform(15, 60), t0)
    dev = f"D-{rng.randint(1000, 9999)}"
    dealer = _new(b, namer("G"), rng.uniform(5, 12) * 365, t0, merchant=True, gst=True, label="Gold dealer")
    _history(b, rng, dealer, t0, ppl, rng.randint(35, 50), 20000, 90000)
    b.txn(t0 - rng.uniform(2, 20) * DAY, dealer, rng.choice(shops), rng.uniform(2, 6) * L, rail="NEFT")
    total = 0.0
    for i, v in enumerate(victims):
        amt = rng.uniform(3, 9) * L
        total += amt
        b.txn(t0 + i * rng.uniform(10, 20), v, x, amt, rail="IMPS")
    t = t0 + len(victims) * 20 + 10
    mules = []
    for i in range(rng.randint(6, 10)):
        m = _new(b, namer("M"), rng.uniform(3, 30), t0, device=dev if i % 3 == 0 else None)
        mules.append(m)
    share = total * 0.96 / len(mules)
    for m in mules:
        t += rng.uniform(3, 8)
        b.txn(t, x, m, share, rail="IMPS")
        b.txn(t + rng.uniform(25, 70), m, dealer, rng.uniform(0.6, 0.9) * min(share, 60000))
    truth = {a: True for a in [x] + mules}
    truth[dealer] = False
    return b.scenario(_meta(
        t0, x, truth, "gather_scatter", "Gather-scatter into gold", "Gather-scatter with cash-out",
        f"{len(victims)} large payments are gathered in {x} and scattered to {len(mules)} fresh accounts, which "
        f"buy gold from {dealer}, a real dealer with years of customers.",
        f"Freeze {x} and the mules; do not freeze {dealer}."))


def layered_chain(seed: int, t0: float = DEMO_T0) -> dict:
    rng = random.Random(seed)
    namer = Namer(rng)
    b, ppl, shops = _base(rng, t0)
    victims = _victims(b, rng, namer, t0, 3)
    x = _new(b, namer("A"), rng.uniform(60, 200), t0)
    total = 0.0
    for i, v in enumerate(victims):
        amt = rng.uniform(4, 8) * L
        total += amt
        b.txn(t0 + i * 15, v, x, amt, rail="IMPS")
    dev = f"D-{rng.randint(1000, 9999)}"
    frontier = [(x, t0 + 45, total)]
    ring = [x]
    for depth in range(1, 4):
        nxt = []
        for parent, ts, amt in frontier:
            fan = 3 if depth == 1 else 1
            for k in range(fan):
                m = _new(b, namer("L"), rng.uniform(60, 200), t0, device=dev if rng.random() < 0.4 else None)
                _history(b, rng, m, t0, ppl, rng.randint(2, 5))  # decoy history so mules look normal
                # the collector moves fast (that is what trips the alert); every later hop waits 2-7 minutes
                gap = 15 + k * rng.uniform(15, 30) if depth == 1 else rng.uniform(120, 420)
                b.txn(ts + gap, parent, m, amt * 0.97 / fan, rail="IMPS")
                ring.append(m)
                nxt.append((m, ts + gap, amt * 0.97 / fan))
        frontier = nxt
    truth = {a: True for a in ring}
    return b.scenario(_meta(
        t0, x, truth, "layered_chain", "Slow layered chain", "Deep layering with decoys",
        f"Money moves {x} -> three chains of three hops, waiting 2-7 minutes at every hop, through mule "
        f"accounts that are months old and have ordinary-looking history.",
        "Trace all hops despite the delays; freeze the chain.", evasion=True))


# ----------------------------------------------------------------------------------------
# Innocent look-alikes
# ----------------------------------------------------------------------------------------
def payroll_run(seed: int, t0: float = DEMO_T0) -> dict:
    rng = random.Random(seed)
    namer = Namer(rng)
    b, ppl, shops = _base(rng, t0)
    emp = _new(b, namer("E"), rng.uniform(4, 12) * 365, t0, merchant=True, gst=True, label="Employer Pvt Ltd")
    client = _new(b, namer("Q"), rng.uniform(5, 15) * 365, t0, merchant=True, gst=True, label="Client")
    staff = [_new(b, namer("S"), rng.uniform(1, 8) * 365, t0) for _ in range(rng.randint(8, 14))]
    salary = {s: rng.uniform(22000, 85000) for s in staff}
    for s in staff:
        _history(b, rng, s, t0, shops, rng.randint(3, 8), incoming=False)
        for month in (60, 30):
            b.txn(t0 - month * DAY + rng.uniform(0, 3600), emp, s, salary[s], rail="NEFT")
    for _ in range(4):
        b.txn(t0 - rng.uniform(3, 80) * DAY, client, emp, sum(salary.values()) * rng.uniform(1.1, 1.4), rail="NEFT")
    _history(b, rng, emp, t0, shops, 10, 5000, 40000, incoming=False)
    b.txn(t0, client, emp, max(11 * L, sum(salary.values()) * 1.2), rail="IMPS")
    for i, s in enumerate(staff):
        b.txn(t0 + 20 + i * rng.uniform(3, 8), emp, s, salary[s], rail="IMPS")
    truth = {a: False for a in [emp] + staff}
    return b.scenario(_meta(
        t0, emp, truth, "payroll_run", "Month-end payroll", "Innocent: payroll",
        f"{emp} receives a client payment and pays {len(staff)} employees within two minutes — the same "
        f"salaries it paid last month.", "No freeze. The Defense should win every account."))


def wholesale_festival(seed: int, t0: float = DEMO_T0) -> dict:
    rng = random.Random(seed)
    namer = Namer(rng)
    b, ppl, shops = _base(rng, t0)
    dist = _new(b, namer("H"), rng.uniform(4, 15) * 365, t0, merchant=True, gst=True, label="Wholesale distributor")
    retailers = [_new(b, namer("R"), rng.uniform(2, 10) * 365, t0, merchant=True, gst=True)
                 for _ in range(rng.randint(22, 32))]
    suppliers = [_new(b, namer("P"), rng.uniform(6, 20) * 365, t0, merchant=True, gst=True) for _ in range(3)]
    for r in retailers:
        for _ in range(2):
            b.txn(t0 - rng.uniform(5, 80) * DAY, r, dist, rng.uniform(20000, 70000), rail="NEFT")
    for p in suppliers:
        for _ in range(3):
            b.txn(t0 - rng.uniform(5, 80) * DAY, dist, p, rng.uniform(2, 5) * L, rail="NEFT")
        _history(b, rng, p, t0, shops, 6, 10000, 60000)
    total = 0.0
    for i, r in enumerate(retailers):
        amt = rng.uniform(35000, 80000)
        total += amt
        b.txn(t0 - 500 + i * (480 / len(retailers)), r, dist, amt, rail="UPI")
    for i, p in enumerate(suppliers):
        b.txn(t0 + 15 + i * 12, dist, p, total * 0.28, rail="IMPS")
    truth = {a: False for a in [dist] + suppliers}
    return b.scenario(_meta(
        t0, dist, truth, "wholesale_festival", "Diwali wholesale rush", "Innocent: festival trade",
        f"Two days before Diwali, {len(retailers)} shops pay {dist} for stock, and {dist} immediately pays its "
        f"three long-time suppliers.", "No freeze. Suppliers and the distributor are all established."))


def family_gold_sale(seed: int, t0: float = DEMO_T0) -> dict:
    rng = random.Random(seed)
    namer = Namer(rng)
    b, ppl, shops = _base(rng, t0)
    phone, home = f"D-{rng.randint(1000, 9999)}", f"IP-HOME-{rng.randint(10, 99)}"
    head = _new(b, namer("F"), rng.uniform(8, 20) * 365, t0, device=phone, ip=home)
    members = [head] + [_new(b, namer("F"), rng.uniform(3, 15) * 365, t0, device=phone, ip=home)
                        for _ in range(rng.randint(2, 3))]
    for m in members:
        _history(b, rng, m, t0, shops, rng.randint(6, 12), incoming=False)
        _history(b, rng, m, t0, ppl, rng.randint(2, 4))
    for m in members[1:]:
        b.txn(t0 - rng.uniform(5, 50) * DAY, head, m, rng.uniform(5000, 20000))
    jeweller = _new(b, namer("J"), rng.uniform(10, 25) * 365, t0, merchant=True, gst=True, label="Jeweller")
    _history(b, rng, jeweller, t0, ppl, 20, 20000, 200000, incoming=False)
    amount = rng.uniform(6, 9) * L
    b.txn(t0, jeweller, head, amount, rail="IMPS")
    for i, m in enumerate(members[1:]):
        b.txn(t0 + 15 + i * 10, head, m, amount * 0.9 / (len(members) - 1), rail="IMPS")
    truth = {a: False for a in members}
    return b.scenario(_meta(
        t0, head, truth, "family_gold_sale", "Family gold sale", "Innocent: shared family phone",
        f"{head} sells family gold to a jeweller and splits the money between {len(members) - 1} relatives. "
        f"The family uses one phone, so the accounts share a device.",
        "No hard freeze. A shared family phone is not a mule network — the hardest case for the court."))


def demo_3am(seed: int = 7, t0: float = DEMO_T0) -> dict:
    sc = build_demo_scenario(seed=7, t0=t0)
    sc["meta"].update({"kind": "demo_3am", "title": "The 3 AM analyst (fixed demo)", "typology": "Mule ring",
                       "expected": "Freeze A and B1-B3, soft-freeze the mules, step-up only for C3."})
    return sc


def rush_hour(seed: int, t0: float = DEMO_T0) -> dict:
    """Several cases in one feed: two fraud rings and one innocent look-alike, a few minutes apart.
    Shows how the command center handles simultaneous alerts without interrupting the employee."""
    rng = random.Random(seed)
    parts = [(rng.choice(["mule_ring", "smurfing_fan_in"]), 0.0), (rng.choice(INNOCENT_KINDS), 150.0),
             (rng.choice(["gather_scatter", "mule_ring"]), 300.0)]
    used: set[str] = set()
    used_attrs: set[str] = set()
    merged = {"accounts": [], "transactions": [], "logins": []}
    truth, seeds, titles = {}, [], []
    for n, (kind, offset) in enumerate(parts):
        sc = SCENARIOS[kind](seed=rng.randrange(1, 10**6), t0=t0)
        m = sc["meta"]
        rename = {}
        for a in sc["accounts"]:
            old = a["account_id"]
            new = old
            while new in used:  # keep IDs short and readable, just unique across the three stories
                new = f"{old.rstrip('0123456789')}{rng.randint(10, 999)}"
            used.add(new)
            rename[old] = new
            merged["accounts"].append({**a, "account_id": new})
        for t in sc["transactions"]:
            ts = float(t["timestamp"])
            merged["transactions"].append({**t, "txn_id": f"P{n}-{t['txn_id']}", "from_account": rename[t["from_account"]],
                                           "to_account": rename[t["to_account"]],
                                           "timestamp": round(ts + offset, 1) if ts >= m["live_start"] else ts})
        attr_map: dict[str, str] = {}

        def fresh(value: str) -> str:
            if value not in attr_map:
                if value.startswith(("DEV-", "IP-")) and value.split("-", 1)[1] in rename:
                    attr_map[value] = value.split("-", 1)[0] + "-" + rename[value.split("-", 1)[1]]  # personal
                elif value in used_attrs:
                    prefix = value.rsplit("-", 1)[0] if "-" in value else value
                    new = value
                    while new in used_attrs:
                        new = f"{prefix}-{rng.randint(1000, 9999)}" if value.startswith("D-") else f"{prefix}-{rng.randint(10, 99)}"
                    attr_map[value] = new
                else:
                    attr_map[value] = value
                used_attrs.add(attr_map[value])
            return attr_map[value]
        for lg in sc["logins"]:
            merged["logins"].append({**lg, "account_id": rename[lg["account_id"]],
                                     "device_id": fresh(lg["device_id"]), "ip_address": fresh(lg["ip_address"])})
        truth.update({rename[a]: v for a, v in m["ground_truth"].items()})
        seeds.append(rename[m["seed"]])
        titles.append(m["title"])
    meta = _meta(t0, seeds[0], truth, "rush_hour", "Rush hour: 3 alerts in 6 minutes", "Multiple simultaneous cases",
                 "Three stories arrive in one feed a few minutes apart: " + "; ".join(titles) + ".",
                 "Open three investigations without interrupting the employee; freeze the rings, clear the innocent case.",
                 seeds=seeds)
    return meta and {**merged, "meta": meta}


SCENARIOS: dict[str, Callable[..., dict]] = {
    "demo_3am": demo_3am,
    "mule_ring": mule_ring,
    "smurfing_fan_in": smurfing_fan_in,
    "gather_scatter": gather_scatter,
    "layered_chain": layered_chain,
    "payroll_run": payroll_run,
    "wholesale_festival": wholesale_festival,
    "family_gold_sale": family_gold_sale,
    "rush_hour": rush_hour,
}
FRAUD_KINDS = ["mule_ring", "smurfing_fan_in", "gather_scatter", "layered_chain"]
INNOCENT_KINDS = ["payroll_run", "wholesale_festival", "family_gold_sale"]


def catalog() -> list[dict]:
    out = []
    for kind in SCENARIOS:
        m = SCENARIOS[kind](seed=1)["meta"]
        out.append({"kind": kind, "title": m["title"], "typology": m["typology"],
                    "innocent": kind in INNOCENT_KINDS, "randomised": kind != "demo_3am", "multi": kind == "rush_hour"})
    return out


def build_scenario(kind: str, seed: Optional[int] = None, rng: Optional[random.Random] = None) -> dict:
    if kind not in SCENARIOS:
        raise KeyError(f"Unknown scenario {kind!r}; choose from {', '.join(SCENARIOS)}")
    if seed is None and rng is not None:
        seed = rng.randrange(1, 10**6)
    elif seed is None:
        seed = random.randrange(1, 10**6)
    sc = SCENARIOS[kind](seed=seed)
    sc["meta"]["scenario_seed"] = seed
    return sc


def random_scenario(seed: Optional[int | random.Random] = None) -> dict:
    if isinstance(seed, random.Random):
        rng = seed
    else:
        rng = random.Random(seed)
    kind = rng.choice(FRAUD_KINDS * 2 + INNOCENT_KINDS)  # fraud twice as likely as a look-alike
    return build_scenario(kind, rng.randrange(1, 10**6))

