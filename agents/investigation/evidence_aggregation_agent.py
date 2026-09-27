"""Evidence Aggregation Agent: aggregates deterministic evidence and scores node risk."""
from __future__ import annotations

import math
from app.schemas import Evidence
from agents.agent_helpers import ctx_for, _source, D, FEATURE_WORDS
from agents.investigation.graph_investigation_agent import trace_ring, genome_vector
from agents.investigation.network_agent import (
    detect_cycles, shared_attributes, dwell_times, account_features, precedent_vector, inr
)


def feature_vector(features: dict, genome_similarity: float) -> list[float]:
    age_days = features.get("age_days") or 0.0
    return [
        1.0 if features.get("in_cycle") else 0.0,
        1.0 if features.get("shared_attr") else 0.0,
        float(features.get("fast_pass_through") or 0.0),
        float(genome_similarity or 0.0),
        min(1.0, float(features.get("hop_gap_s") or 0.0) / 600.0),
        min(1.0, float(features.get("outflow_ratio") or 0.0)),
        0.0 if age_days > 365 else 1.0,
        0.0 if features.get("gst") else 1.0,
    ]


FEATURE_NAMES = [
    "in_cycle", "shared_attr", "fast_flow", "genome_sim",
    "hop_gap", "outflow_ratio", "new_account", "unregistered",
]


def fit_logistic(X, y, l2: float = 1.0, iters: int = 3000, lr: float = 0.5) -> dict:
    weights = {name: 0.3 for name in FEATURE_NAMES}
    bias = -0.5
    if X and y:
        try:
            import numpy as np
            X_arr = np.array(X, dtype=float)
            y_arr = np.array(y, dtype=float)
            w = np.zeros(X_arr.shape[1])
            b = 0.0
            for _ in range(min(iters, 500)):
                logits = X_arr @ w + b
                p = 1.0 / (1.0 + np.exp(-np.clip(logits, -20, 20)))
                err = p - y_arr
                grad_w = (X_arr.T @ err) / len(y_arr) + l2 * w / len(y_arr)
                grad_b = float(np.mean(err))
                w -= lr * grad_w
                b -= lr * grad_b
            weights = {FEATURE_NAMES[i]: round(float(w[i]), 3) for i in range(min(len(FEATURE_NAMES), len(w)))}
            bias = round(float(b), 3)
        except Exception:
            pass
    return {"weights": weights, "bias": bias, "intercept": bias}


def node_risk(features: dict, genome_similarity: float, unavailable: tuple = (), model: dict | None = None) -> float:
    if model and "weights" in model:
        vec = feature_vector(features, genome_similarity)
        w_vals = list(model["weights"].values()) if isinstance(model["weights"], dict) else model["weights"]
        bias = model.get("bias", model.get("intercept", 0.0))
        z = sum(v * w for v, w in zip(vec, w_vals)) + bias
        return 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, z))))

    score = 0.05
    if features.get("in_cycle"):
        score += 0.35
    if features.get("shared_attr"):
        score += 0.25
    if features.get("fast_flow") or features.get("fast_pass_through"):
        score += 0.20
    if genome_similarity >= 0.7:
        score += 0.15
    if features.get("age_days") is not None and features.get("age_days") < 90:
        score += 0.10
    if features.get("burst_ratio", 0) >= 0.8:
        score += 0.15
    if features.get("depth", 99) <= 1:
        score += 0.15
    if features.get("amount_consistent") is False:
        score += 0.10
    return min(0.99, max(0.01, score))


def explain(features: dict, genome_similarity: float) -> dict[str, float]:
    exp = {}
    if features.get("in_cycle"):
        exp["cycle"] = 0.35
    if features.get("shared_attr"):
        exp["infrastructure"] = 0.25
    if features.get("fast_flow") or features.get("fast_pass_through"):
        exp["velocity"] = 0.20
    if genome_similarity >= 0.7:
        exp["genome"] = 0.15
    return exp


explain_risk = explain


async def pattern_detector(state: dict) -> dict:
    ctx = ctx_for(state)
    await ctx.pause()
    await ctx.emit("stage.changed", {"stage": "pattern"})
    seed, ring, t0 = state["seed"], state["ring"], state["alert_time"]
    max_gap = ctx.memory.max_hop_gap_s
    s = trace_ring(ctx.store, seed, t0, window_s=ctx.settings.alert_window_s, max_hop_gap_s=max_gap, max_depth=ctx.settings.trace_depth)

    cycles = detect_cycles(ctx.store, ring, t0 - 3600, t0 + 3600)
    shared = shared_attributes(ctx.store, ring)
    dwells = dwell_times(ctx.store, s)
    feats = account_features(ctx.store, s, t0, cycles, shared, dwells, fast_s=ctx.settings.fast_dwell_s)

    vec = genome_vector({"trace": s, "cycles": cycles, "shared": shared, "dwell": dwells, "features": feats})
    genome_match = ctx.memory.match_genome(vec)

    ev_list: list[Evidence] = []
    # 1. Cycles
    for idx, c in enumerate(cycles):
        ev_list.append(Evidence(
            id=f"CYC-{idx+1:02d}", kind="cycle", nodes=c,
            text=f"Circular fund movement detected across {' -> '.join(c)}",
            source_tool="detect_cycles",
        ))
    # 2. Shared infrastructure
    for idx, s_item in enumerate(shared):
        kind = "shared_device" if s_item["kind"] == "shared_device" else "shared_ip"
        members = s_item.get("accounts", s_item.get("nodes", []))
        ev_list.append(Evidence(
            id=f"INF-{idx+1:02d}", kind=kind, nodes=members,
            text=f"Shared {s_item['kind']} ({s_item['attr']}) across {', '.join(members)}",
            source_tool="shared_attributes",
        ))
    # 3. High velocity dwell
    for a, d in dwells.items():
        if d is not None and d < 60:
            ev_list.append(Evidence(
                id=f"DWL-{a}", kind="dwell", nodes=[a],
                text=f"Rapid dwell time: forwarded funds in {d:.0f} seconds",
                value=d, source_tool="dwell_times",
            ))

    # Calculate risk scores per node
    risk_scores = {}
    for a in ring:
        risk_scores[a] = node_risk(feats[a], genome_match.get("similarity", 0.0))

    for ev in ev_list:
        await ctx.emit("evidence.added", ev.model_dump())

    return {
        "evidence": ev_list, "features": feats,
        "risk": risk_scores, "genome": {"vector": vec, "match": genome_match, **genome_match},
    }


__all__ = ["pattern_detector", "node_risk", "explain", "explain_risk", "feature_vector", "fit_logistic"]
