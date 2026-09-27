"""Deterministic graph detectors. These produce the facts; LLM agents only interpret them.

All functions are pure: they read the GraphStore and return plain data.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Optional

import networkx as nx

from database.graph_store import DAY, GraphStore


# ----------------------------------------------------------------------------------------
# 1. Money-trail tracing (Graph Scout)
# ----------------------------------------------------------------------------------------
def trace_ring(store: GraphStore, seed: str, alert_time: float, window_s: float,
               max_hop_gap_s: float, max_depth: int = 4) -> dict:
    """Follow the money forward in time from the seed account.

    A transfer is followed when it leaves an account within `max_hop_gap_s` of the money
    arriving there and is not a tiny spend (at least 2% of what arrived, capped at ₹20,000). Returns ring members, their depth,
    who sent them ring money, the ring transfers, back-edges (cycles) and the source accounts.
    """
    window_start = alert_time - window_s
    inflows = store.in_txns(seed, window_start, alert_time)
    sources = sorted({t["src"] for t in inflows})
    capital = sum(t["amount"] for t in inflows)
    first_arrival = min((t["ts"] for t in inflows), default=window_start)

    depth = {seed: 0}
    arrival = {seed: first_arrival}
    received = defaultdict(float)
    received[seed] = capital
    parents: dict[str, list[str]] = defaultdict(list)
    edges: list[dict] = []
    back_edges: list[dict] = []
    frontier = [seed]
    level = 0
    while frontier and level < max_depth:
        nxt = []
        for node in frontier:
            outs = store.out_txns(node, arrival[node], arrival[node] + max_hop_gap_s)
            min_amount = min(0.02 * received[node], 20000.0) if received[node] else 0.0
            for t in outs:
                if t["amount"] < min_amount:
                    continue
                dst = t["dst"]
                if dst in sources and dst not in depth:
                    continue  # money going back to a victim is a refund, not laundering
                edges.append(t)
                if dst in depth:
                    back_edges.append(t)
                    if node not in parents[dst]:
                        parents[dst].append(node)
                    continue
                depth[dst] = level + 1
                arrival[dst] = t["ts"]
                received[dst] += t["amount"]
                parents[dst].append(node)
                nxt.append(dst)
        frontier = nxt
        level += 1
    ring = sorted(depth, key=lambda a: (depth[a], a))
    exposure = {a: 0.0 for a in ring}
    exposure[seed] = capital
    for t in edges:
        if t["dst"] != seed:
            exposure[t["dst"]] += t["amount"]
    return {
        "ring": ring, "depth": depth, "parents": {k: v for k, v in parents.items()},
        "edges": edges, "back_edges": back_edges, "sources": sources, "capital": capital,
        "arrival": arrival, "exposure": exposure, "window_start": window_start,
    }


def roles_from_depth(depth: dict[str, int]) -> dict[str, str]:
    return {a: ("controller" if d == 0 else "layering" if d == 1 else "mule") for a, d in depth.items()}


def ego_entity_count(store: GraphStore, ring: list[str], since: float, until: float) -> int:
    """How many distinct accounts, devices and IPs the Scout looked at (2-hop neighbourhood)."""
    seen: set[str] = set(ring)
    frontier = set(ring)
    for _ in range(2):
        nxt: set[str] = set()
        for a in frontier:
            nxt.update(store.counterparties(a, since, until).keys())
        nxt -= seen
        seen |= nxt
        frontier = nxt
    attrs = set()
    for a in ring:
        attrs |= {f"D:{d}" for d in store.devices_of.get(a, set())}
        attrs |= {f"I:{i}" for i in store.ips_of.get(a, set())}
    return len(seen) + len(attrs)


# ----------------------------------------------------------------------------------------
# 2. Structural detectors
# ----------------------------------------------------------------------------------------
def detect_cycles(store: GraphStore, ring: list[str], start: float, end: float, max_len: int = 6) -> list[list[str]]:
    g = store.transfer_digraph(ring, start, end)
    cycles = []
    for cyc in nx.simple_cycles(g, length_bound=max_len):
        if len(cyc) >= 2:
            cycles.append(cyc)
        if len(cycles) >= 5:
            break
    return cycles


def shared_attributes(store: GraphStore, ring: list[str]) -> list[dict]:
    """Devices or IPs used by two or more ring accounts."""
    ring_set = set(ring)
    found = []
    for device, accs in store.accounts_on_device.items():
        members = sorted(accs & ring_set)
        if len(members) >= 2:
            found.append({"kind": "shared_device", "attr": device, "accounts": members})
    for ip, accs in store.accounts_on_ip.items():
        members = sorted(accs & ring_set)
        if len(members) >= 2:
            found.append({"kind": "shared_ip", "attr": ip, "accounts": members, "vpn": ip in store.vpn_ips})
    return found


def dwell_times(store: GraphStore, trace: dict) -> dict[str, float]:
    """Seconds between money arriving at an account and the first ring transfer leaving it."""
    out_by_src = defaultdict(list)
    for t in trace["edges"]:
        out_by_src[t["src"]].append(t["ts"])
    dwell = {}
    for acc, arr in trace["arrival"].items():
        outs = [ts for ts in out_by_src.get(acc, []) if ts >= arr]
        if outs:
            dwell[acc] = min(outs) - arr
    return dwell


def fast_score(d: Optional[float], fast_s: float = 60) -> float:
    if d is None:
        return 0.0
    # decays to zero over max(20 min, fast_s) beyond the limit, so slower data sets scale sensibly
    return 1.0 if d <= fast_s else max(0.0, 1.0 - (d - fast_s) / max(1200.0, fast_s))


# ----------------------------------------------------------------------------------------
# 3. Per-account features (used by risk score, defense, precedents)
# ----------------------------------------------------------------------------------------
def account_features(store: GraphStore, trace: dict, alert_time: float, cycles: list[list[str]],
                     shared: list[dict], dwell: dict[str, float], fast_s: float = 60) -> dict[str, dict]:
    ring = trace["ring"]
    ring_set = set(ring)
    sources = set(trace["sources"])
    in_cycle = {a for c in cycles for a in c}
    shared_with = defaultdict(set)
    for s in shared:
        for a in s["accounts"]:
            shared_with[a] |= set(s["accounts"]) - {a}
    feats = {}
    horizon = alert_time + 3600
    for acc in ring:
        info = store.accounts.get(acc, {})
        age = store.age_days(acc, alert_time)
        own = dwell.get(acc)
        parent_d = [dwell[p] for p in trace["parents"].get(acc, []) if p in dwell]
        flow_d = own if own is not None else (min(parent_d) if parent_d else None)
        ring_in = trace["exposure"].get(acc, 0.0)
        past_in = [t["amount"] for t in store.in_txns(acc, alert_time - 90 * DAY, trace["window_start"])]
        in_30d = sum(t["amount"] for t in store.in_txns(acc, alert_time - 30 * DAY, horizon))
        cps = store.counterparties(acc, alert_time - 30 * DAY, trace["window_start"])
        legit_cps = {c: v for c, v in cps.items() if c not in ring_set and c not in sources}
        ring_cps = set(trace["parents"].get(acc, [])) | {t["dst"] for t in trace["edges"] if t["src"] == acc}
        older = store.counterparties(acc, alert_time - 90 * DAY, alert_time - DAY)
        recurring = (len([c for c in ring_cps if c in older]) / len(ring_cps)) if ring_cps else 0.0
        typical = statistics.median(past_in) if past_in else 0.0
        p95 = sorted(past_in)[int(0.95 * (len(past_in) - 1))] if past_in else 0.0
        feats[acc] = {
            "account": acc,
            "depth": trace["depth"][acc],
            "traced": 1.0,
            "in_cycle": 1.0 if acc in in_cycle else 0.0,
            "shared_attr": 1.0 if shared_with.get(acc) else 0.0,
            "shared_with": sorted(shared_with.get(acc, set())),
            "dwell_s": own,
            "fast_flow": fast_score(flow_d, fast_s),
            "age_days": age,
            "new_account": 1.0 if (age is not None and age < 90) else (0.5 if age is None else 0.0),
            "burst_ratio": min(1.0, ring_in / in_30d) if in_30d else 1.0,
            "ring_in": ring_in,
            "is_merchant": 1.0 if info.get("is_merchant") else 0.0,
            "gst": 1.0 if info.get("gst_registered") else 0.0,
            "legit_cp": len(legit_cps),
            "legit_volume_30d": sum(legit_cps.values()),
            "recurring_ratio": recurring,
            "typical_ticket": typical,
            "amount_consistent": 1.0 if (past_in and ring_in <= max(3 * typical, p95)) else 0.0,
        }
    return feats


def precedent_vector(f: dict) -> list[float]:
    """Compact, normalised description of one account, used to match precedents."""
    age_years = min((f["age_days"] or 0) / 365.0, 5.0) / 5.0
    return [
        f["in_cycle"], f["shared_attr"], f["fast_flow"], f["new_account"], f["burst_ratio"],
        age_years, f["is_merchant"], f["gst"], min(f["legit_cp"], 10) / 10.0,
        f["recurring_ratio"], min(f["depth"], 3) / 3.0,
    ]


# ----------------------------------------------------------------------------------------
# 4. Formatting helpers
# ----------------------------------------------------------------------------------------
def inr(amount: float) -> str:
    """Indian digit grouping: 4000000 -> ₹40,00,000"""
    n = int(round(amount))
    s = str(n)
    if len(s) <= 3:
        return "₹" + s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return "₹" + ",".join(parts) + "," + tail


def lakh(amount: float) -> str:
    return f"₹{amount / 100000:.1f} L"


IST_OFFSET = 19800  # seconds (UTC+5:30)


def ist_clock(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts + IST_OFFSET, timezone.utc).strftime("%H:%M:%S")


trace_money_trail = trace_ring
find_shared_infrastructure = shared_attributes
