"""Graph Investigation Agent: executes the Scout step, layout calculation, and topology extraction."""
from __future__ import annotations

import asyncio
import math
import random
import statistics
from app.config import settings
from database.graph_store import GraphStore
from database.vector_index import VectorIndex
from agents.agent_helpers import ctx_for, D
from agents.investigation.network_agent import (
    trace_ring, roles_from_depth, ego_entity_count, inr, ist_clock,
    detect_cycles, shared_attributes, dwell_times, account_features
)
from core.simulator import make_evasion_case

GENOME_CENTER = [0.35, 0.4, 0.5, 0.5, 0.5, 0.4, 0.5, 0.5, 0.6, 0.3, 0.1]
GENOME_FIELDS = ["size", "depth", "has_cycle", "frac_fast", "mean_fan", "max_fan",
                 "frac_shared", "frac_new", "mean_burst", "amount_cv", "frac_merchant"]


def structure_of(store: GraphStore, seed: str, alert_time: float, max_hop_gap_s: float | None = None,
                 trace_depth: int = 4, alert_window_s: float = 600, **kwargs) -> dict:
    gap = max_hop_gap_s if max_hop_gap_s is not None else kwargs.get("max_gap_s", 600.0)
    trace = trace_ring(store, seed, alert_time, window_s=alert_window_s, max_hop_gap_s=gap, max_depth=trace_depth)
    end = alert_time + gap * trace_depth
    cycles = detect_cycles(store, trace["ring"], trace["window_start"], end)
    shared = shared_attributes(store, trace["ring"])
    dwell = dwell_times(store, trace)
    feats = account_features(store, trace, alert_time, cycles, shared, dwell, fast_s=kwargs.get("fast_s", 60))
    roles = roles_from_depth(trace["depth"])
    return {
        "trace": trace, "cycles": cycles, "shared": shared, "dwell": dwell, "features": feats,
        "ring": trace["ring"], "depth": trace["depth"], "roles": roles,
        "parents": trace["parents"], "edges": trace["edges"], "back_edges": trace.get("back_edges", []),
        "sources": trace["sources"], "capital": trace.get("capital", 0.0),
        "arrival": trace["arrival"], "exposure": trace["exposure"],
        "scanned_entities": ego_entity_count(store, trace["ring"], alert_time - 3600, alert_time + 3600),
    }


def genome_vector(s: dict) -> list[float]:
    trace = s.get("trace", s)
    feats = s.get("features", {})
    ring = trace.get("ring", [])
    n = max(len(ring), 1)
    out_deg: dict[str, set] = {}
    edges = trace.get("edges", [])
    for t in edges:
        out_deg.setdefault(t["src"], set()).add(t["dst"])
    fans = [len(v) for v in out_deg.values()] or [0]
    amounts = [t["amount"] for t in edges] or [0.0]
    cv = (statistics.pstdev(amounts) / statistics.mean(amounts)) if (amounts and statistics.mean(amounts)) else 0.0
    f = list(feats.values()) if feats else []
    depth = trace.get("depth", {})
    max_d = max(depth.values()) if depth else 0
    return [
        min(n / 20.0, 1.0),
        max_d / 4.0 if depth else 0.0,
        1.0 if s.get("cycles") else 0.0,
        sum(x.get("fast_flow", 0) for x in f) / n if f else 0.5,
        min(statistics.mean(fans) / 4.0, 1.0),
        min(max(fans) / 8.0, 1.0),
        sum(x.get("shared_attr", 0) for x in f) / n if f else 0.5,
        sum(x.get("new_account", 0) for x in f) / n if f else 0.5,
        sum(x.get("burst_ratio", 0) for x in f) / n if f else 0.5,
        min(cv / 2.0, 1.0),
        sum(x.get("is_merchant", 0) for x in f) / n if f else 0.0,
    ]


TEMPLATES = [
    ("F-2023-MULE-07", "Mule pass-through ring with round-trip",
     {"layers": 1, "fan": 3, "hop_delay_s": 35, "mule_age_days": 12, "shared_prob": 0.6, "cycle": True,
      "decoy_ratio": 0.0, "total_lakh": 40}),
    ("F-2024-SMURF-12", "Single-hop smurfing fan-out",
     {"layers": 0, "fan": 8, "hop_delay_s": 50, "mule_age_days": 20, "shared_prob": 0.2, "cycle": False,
      "decoy_ratio": 0.0, "total_lakh": 20}),
    ("F-2022-SLOWCHAIN-03", "Slow layered chain with aged accounts",
     {"layers": 2, "fan": 2, "hop_delay_s": 420, "mule_age_days": 250, "shared_prob": 0.0, "cycle": False,
      "decoy_ratio": 0.2, "total_lakh": 15}),
    ("F-2025-BUSTOUT-19", "Bust-out loop through older accounts",
     {"layers": 1, "fan": 2, "hop_delay_s": 90, "mule_age_days": 300, "shared_prob": 0.3, "cycle": True,
      "decoy_ratio": 0.1, "total_lakh": 30}),
    ("F-2023-MULE-02", "Deep mule chain sharing one handset",
     {"layers": 2, "fan": 2, "hop_delay_s": 30, "mule_age_days": 15, "shared_prob": 0.9, "cycle": False,
      "decoy_ratio": 0.0, "total_lakh": 25}),
]


def seed_library(max_hop_gap_s: float = 600) -> list[dict]:
    rng = random.Random(2023)
    library = []
    for gid, label, params in TEMPLATES:
        case = make_evasion_case(params, rng)
        store = GraphStore.from_records(case["accounts"], case["transactions"], case["logins"])
        st = structure_of(store, case["meta"]["seed"], case["meta"]["alert_time"], max(max_hop_gap_s, 900))
        library.append({"id": gid, "label": label, "vector": genome_vector(st), "source": "seed library"})
    return library


def _layout_row(ids: list[str], y: float, width: float = 900, margin: float = 80, max_gap: float = 150) -> dict:
    n = len(ids)
    if n == 0:
        return {}
    if n == 1:
        return {ids[0]: (width / 2.0, y)}
    usable = width - 2 * margin
    gap = min(max_gap, usable / (n - 1))
    start_x = (width - gap * (n - 1)) / 2.0
    return {i: (start_x + idx * gap, y) for idx, i in enumerate(ids)}


def build_display_graph(ctx, s: dict, alert_time: float) -> dict:
    ring, depth, roles = s["ring"], s["depth"], s["roles"]
    by_layer: dict[int, list[str]] = {}
    for a in ring:
        by_layer.setdefault(depth.get(a, 0), []).append(a)
    y_map = {0: 90, 1: 220, 2: 350, 3: 470}
    pos: dict[str, tuple[float, float]] = {}
    for d, ids in by_layer.items():
        pos.update(_layout_row(ids, y_map.get(d, 350 + d * 100)))

    nodes = []
    for a in ring:
        info = ctx.store.accounts.get(a, {})
        ins = ctx.store.in_txns(a, alert_time - 3600, alert_time + 3600)
        outs = ctx.store.out_txns(a, alert_time - 3600, alert_time + 3600)
        dwell = (outs[0]["ts"] - ins[0]["ts"]) if ins and outs and outs[0]["ts"] >= ins[0]["ts"] else 0
        x, y = pos.get(a, (450, 220))
        nodes.append({
            "id": a, "label": info.get("name", a), "role": roles.get(a, "mule"),
            "group": "ring",
            "x": x, "y": y, "depth": depth.get(a, 0),
            "stats": {
                "received": sum(t["amount"] for t in ins),
                "forwarded": sum(t["amount"] for t in outs),
                "dwell_s": round(dwell, 1),
                "fan_out": len(outs),
                "devices": len(info.get("devices", [])),
                "ips": len(info.get("ips", [])),
            },
        })

    cycle_pairs = set()
    for cyc in s.get("cycles", []):
        for i in range(len(cyc)):
            cycle_pairs.add((cyc[i], cyc[(i + 1) % len(cyc)]))

    edges = []
    seen = set()
    for t in ctx.store.txns.values():
        if t["src"] in ring and t["dst"] in ring:
            src, dst = t["src"], t["dst"]
            eid = t.get("txn_id") or f"E-{src}-{dst}"
            if eid in seen:
                continue
            seen.add(eid)
            knd = "cycle" if (src, dst) in cycle_pairs else "ring"
            edges.append({
                "id": eid,
                "source": src, "target": dst,
                "from": src, "to": dst,
                "amount": t["amount"],
                "kind": knd,
                "label": D.inr(t["amount"]),
                "time": D.ist_clock(t["ts"]),
                "txns": [{"txn_id": t.get("txn_id"), "amount": t["amount"], "time": D.ist_clock(t["ts"])}],
            })

    for sh in s.get("shared", []):
        attr = sh["attr"]
        sh_accs = sh.get("accounts") or sh.get("nodes") or []
        if attr not in pos:
            shared_nodes = [n for n in nodes if n["id"] in sh_accs]
            avg_x = sum(n["x"] for n in shared_nodes) / len(shared_nodes) if shared_nodes else 450
            avg_y = sum(n["y"] for n in shared_nodes) / len(shared_nodes) if shared_nodes else 100
            pos[attr] = (avg_x, max(20, avg_y - 60))
            knd = "device" if "device" in sh.get("kind", "") else "ip"
            nodes.append({
                "id": attr, "label": attr, "group": "attr",
                "kind": knd,
                "vpn": sh.get("vpn", "VPN" in attr),
                "x": pos[attr][0], "y": pos[attr][1],
                "depth": 0, "stats": {},
            })
        for acc in sh_accs:
            if acc in ring:
                edges.append({
                    "id": f"SH-{attr}-{acc}",
                    "source": attr, "target": acc,
                    "from": attr, "to": acc,
                    "amount": 0, "kind": "shared",
                    "label": "", "time": "",
                })

    return {"nodes": nodes, "edges": edges, "width": 900, "height": max(520, max((n['y'] for n in nodes), default=520) + 40)}


async def scout(state: dict) -> dict:
    ctx = ctx_for(state)
    await ctx.pause()
    await ctx.emit("stage.changed", {"stage": "scout"})
    seed, t0 = state["seed"], state["alert_time"]
    max_gap = ctx.memory.max_hop_gap_s
    s = structure_of(ctx.store, seed, t0, max_hop_gap_s=max_gap, trace_depth=ctx.settings.trace_depth, alert_window_s=ctx.settings.alert_window_s)
    ring = s["ring"]
    trace = s.get("trace", s)

    await ctx.tool("trace_downstream", {"seed": seed, "depth": ctx.settings.trace_depth,
                                        "max_hop_gap_s": int(ctx.memory.max_hop_gap_s)},
                   f"{len(ring)} accounts on the money trail, {len(trace.get('edges', []))} transfers")
    found: list[str] = list(trace.get("sources", [seed]))
    for hop in range(0, max(trace["depth"].values(), default=0) + 1):
        new = [a for a in ring if trace["depth"][a] == hop]
        found += new
        await ctx.emit("graph.progress", {"hop": hop, "nodes": len(found), "new": new})
        if ctx.mode == "live" and ctx.settings.stage_delay > 0:
            await asyncio.sleep(min(0.2, ctx.settings.stage_delay * 0.2))
    await ctx.tool("detect_cycles", {"accounts": len(ring)}, f"{len(s['cycles'])} money cycle(s)")
    await ctx.tool("find_shared_devices_ips", {"accounts": len(ring)},
                   f"{len(s['shared'])} shared device/IP link(s)" + (": " + ", ".join(x["attr"] for x in s["shared"])
                                                                     if s["shared"] else ""))

    disp = build_display_graph(ctx, s, t0)
    await ctx.emit("graph.snapshot", disp)
    return {
        "sources": s.get("sources", [seed]), "ring": s["ring"], "roles": s["roles"],
        "depth": s["depth"], "parents": s["parents"], "edges": s.get("edges", []),
        "arrival": s.get("arrival", {}), "exposure": s.get("exposure", {}),
        "scanned_entities": s["scanned_entities"], "display_graph": disp,
    }


__all__ = ["scout", "build_display_graph", "structure_of", "genome_vector", "seed_library", "GENOME_CENTER"]

