"""Counterfactual collateral check and expected-utility choice of countermeasure tier.

EU(tier) = p * exposure * effectiveness(tier) - (1 - p) * (friction(tier) + collateral_weight(tier) * legit_volume)

* effectiveness: share of the money at risk that the tier actually protects
* friction: cost to an innocent customer of being challenged or frozen
* collateral: legitimate business that a freeze would cut off (from the counterfactual)
"""
from __future__ import annotations

from database.graph_store import DAY, GraphStore
from app.schemas import TIER_ACTIONS

# Step-up stops account takeover but not a willing mule, who can pass the challenge himself.
EFFECTIVENESS = {1: 0.0, 2: 0.25, 3: 0.9, 4: 1.0}
FRICTION = {1: 0.0, 2: 2000.0, 3: 50000.0, 4: 200000.0}
COLLATERAL_WEIGHT = {1: 0.0, 2: 0.0, 3: 0.6, 4: 1.0}
ROLE_CAP = {"controller": 4, "layering": 4, "mule": 3}
HARD_FREEZE_MIN_P = 0.85
FREEZE_MIN_P = 0.50  # never freeze on less-than-even odds
COLLATERAL_LIMIT_VOLUME = 50000.0
COLLATERAL_LIMIT_CPS = 3


def counterfactual(store: GraphStore, account: str, ring: set[str], sources: set[str], alert_time: float) -> dict:
    """Simulate removing `account` from the graph: which legitimate relationships would break?"""
    cps = store.counterparties(account, alert_time - 30 * DAY, alert_time - 600)
    legit = {c: v for c, v in cps.items() if c not in ring and c not in sources}
    return {
        "account": account,
        "legit_counterparties": len(legit),
        "legit_volume": round(sum(legit.values()), 2),
        "examples": sorted(legit, key=lambda c: -legit[c])[:4],
    }


def expected_utility(p: float, exposure: float, collateral_volume: float, tier: int) -> float:
    return p * exposure * EFFECTIVENESS[tier] - (1 - p) * (FRICTION[tier] + COLLATERAL_WEIGHT[tier] * collateral_volume)


def choose_tier(p: float, role: str, exposure: float, cf: dict, max_tier: int,
                cap_override: int | None = None) -> tuple[int, dict]:
    """Pick the tier with the highest expected utility, within role, confidence and kill-switch caps."""
    if p < 0.15:
        return 0, {}
    cap = min(ROLE_CAP.get(role, 2), max_tier)
    if cap_override is not None:
        cap = min(cap, cap_override)
    if p < HARD_FREEZE_MIN_P:
        cap = min(cap, 3)
    if p < FREEZE_MIN_P:
        cap = min(cap, 2)
    scores = {t: round(expected_utility(p, exposure, cf["legit_volume"], t), 1) for t in range(1, cap + 1)}
    best = max(scores, key=lambda t: (scores[t], -t))
    return best, scores


def collateral_blocks(cf: dict) -> bool:
    return cf["legit_volume"] > COLLATERAL_LIMIT_VOLUME or cf["legit_counterparties"] > COLLATERAL_LIMIT_CPS


def action_name(tier: int) -> str:
    return TIER_ACTIONS[tier]
