"""Moot court: nightly self-play.

Each round, the Red-team agent generates labelled cases of two kinds:
  * evasion  - real rings disguised to look normal   (tests the prosecution and the Scout)
  * framing  - innocent activity that looks like fraud (tests the defense)
The same LangGraph courtroom tries every case. The grader compares verdicts with the truth and turns
each mistake into a precedent (or a policy change), which future trials retrieve. The Red team then
mutates whatever fooled the courtroom.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import random
import time
from typing import Callable, Optional

from agents.investigation import network_agent as D
from app.config import RUNTIME_DIR, settings as base_settings
from agents.investigation.graph_investigation_agent import genome_vector, structure_of
from database.graph_store import GraphStore
from agents.moot_court.precedent_memory import Memory
from agents.investigation.evidence_aggregation_agent import node_risk
from core.simulator import FRAMING_KINDS, default_evasion_params, make_evasion_case, make_framing_case

METRICS_PATH = RUNTIME_DIR / "moot_metrics.json"

FRAMING_LESSONS = {
    "payroll": "salary payouts to long-standing staff are not mule transfers",
    "family_phone": "family members sharing one phone are not a ring",
    "festival": "a shop paying suppliers after festival sales is normal trade",
    "kirana": "a kirana store paying suppliers from UPI takings is normal trade",
}


class RedTeam:
    """Keeps the evasion tricks that worked and mutates them to push harder."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.successes: list[dict] = []

    def sample(self) -> dict:
        if self.successes and self.rng.random() < 0.6:
            p = copy.deepcopy(self.rng.choice(self.successes))
            p["hop_delay_s"] = min(5400, p["hop_delay_s"] * self.rng.uniform(1.0, 1.35))
            p["mule_age_days"] = min(900, p["mule_age_days"] * self.rng.uniform(1.0, 1.3))
            p["shared_prob"] = max(0.0, p["shared_prob"] * self.rng.uniform(0.4, 1.0))
            p["decoy_ratio"] = min(0.45, p["decoy_ratio"] + self.rng.uniform(0, 0.08))
            p["cycle"] = p["cycle"] and self.rng.random() < 0.7
            return p
        return default_evasion_params(self.rng)

    def record(self, params: dict, evaded: bool) -> None:
        if evaded:
            self.successes = (self.successes + [params])[-20:]

    def difficulty(self) -> dict:
        pool = self.successes or [{"hop_delay_s": 0, "mule_age_days": 0, "decoy_ratio": 0, "layers": 0, "fan": 0}]
        return {
            "max_hop_delay_min": round(max(p["hop_delay_s"] for p in pool) / 60, 1),
            "max_mule_age_days": round(max(p["mule_age_days"] for p in pool)),
            "max_decoy_pct": round(100 * max(p["decoy_ratio"] for p in pool)),
            "max_layers": max(p["layers"] for p in pool) + 1 if self.successes else 0,
            "framing_kinds": FRAMING_KINDS,
        }


def _load_metrics() -> dict:
    if METRICS_PATH.exists():
        return json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    return {"rounds": [], "log": [], "difficulty": {}, "policy": {}}


def load_metrics() -> dict:
    data = _load_metrics()
    return data


def benchmark_cases(n: int = 30, seed: int = 777) -> list[dict]:
    """A fixed exam: the same cases every round, never used for learning. Measures real improvement."""
    rng = random.Random(seed)
    cases = []
    for i in range(n):
        if i % 2 == 0:
            cases.append(make_evasion_case(default_evasion_params(rng), rng))
        else:
            cases.append(make_framing_case(FRAMING_KINDS[(i // 2) % len(FRAMING_KINDS)], rng))
    return cases


async def evaluate_benchmark(memory: Memory, cases: list[dict]) -> dict:
    from core.workflow import run_case
    s = copy.copy(base_settings)
    s.stage_delay = s.token_delay = s.settle_window = 0
    missed = wrongful = 0
    for case in cases:
        meta = case["meta"]
        store = GraphStore.from_records(case["accounts"], case["transactions"], case["logins"])
        final, _ = await run_case(store, memory, meta["seed"], meta["alert_time"], mode="moot",
                                  ground_truth=meta["ground_truth"], settings=s, settle=False)
        missed += bool(final["grade"]["missed"])
        wrongful += bool(final["grade"]["wrongful"])
    n = len(cases)
    return {"missed_per_100": round(100 * missed / n, 1), "wrongful_per_100": round(100 * wrongful / n, 1)}


async def run_round(memory: Memory, round_no: int, n_cases: int, rng: random.Random, red: RedTeam,
                    metrics: dict, progress: Optional[Callable] = None) -> dict:
    from core.workflow import run_case
    s = copy.copy(base_settings)
    s.stage_delay = s.token_delay = s.settle_window = 0
    evasion_n = framing_n = missed_cases = wrongful_cases = sent = added = 0
    for i in range(n_cases):
        if i % 2 == 0:
            params = red.sample()
            case = make_evasion_case(params, rng)
            evasion_n += 1
        else:
            case = make_framing_case(rng.choice(FRAMING_KINDS), rng)
            framing_n += 1
        meta = case["meta"]
        store = GraphStore.from_records(case["accounts"], case["transactions"], case["logins"])

        # Pre-screen: cheap detectors only; clearly harmless cases never reach the (LLM) courtroom.
        st = structure_of(store, meta["seed"], meta["alert_time"], memory.max_hop_gap_s)
        sim = memory.match_genome(genome_vector(st))["similarity"]
        max_risk = max((node_risk(f, sim) for f in st["features"].values()), default=0.0)
        if max_risk < 0.35:
            grade = {"missed": [a for a, f in meta["ground_truth"].items() if f],
                     "untraced": [], "wrongful": [], "tiers": {}}
            final = {"evidence": []}
        else:
            sent += 1
            final, _ = await run_case(store, memory, meta["seed"], meta["alert_time"], mode="moot",
                                      ground_truth=meta["ground_truth"], settings=s, settle=False)
            grade = final["grade"]

        kind = meta["kind"]
        used = {e.text.split(" ")[1] for e in final.get("evidence", []) if e.kind == "precedent"}
        correct = not grade["missed"] and not grade["wrongful"]
        if correct:
            for p in memory.data["precedents"]:
                if p["id"] in used and not p.get("fixed_round"):
                    p["fixed_round"] = round_no
            for p in memory.data["precedents"]:
                if (p["side"] == "policy" and not p.get("fixed_round") and kind == "evasion"
                        and p["old_gap"] < meta["max_hop_gap_s"] <= memory.max_hop_gap_s):
                    p["fixed_round"] = round_no

        if kind == "evasion":
            evaded = bool(grade["missed"])
            red.record(meta["params"], evaded)
            if evaded:
                missed_cases += 1
                if grade["untraced"] or len(grade["missed"]) == len(meta["ground_truth"]):
                    old = memory.max_hop_gap_s
                    if memory.widen_hop_gap(meta["max_hop_gap_s"]):
                        pid = memory.add_precedent("policy", [], f"extend the money-trail window to "
                                                   f"{memory.max_hop_gap_s / 60:.0f} min", kind, round_no, meta["seed"])
                        memory.data["precedents"][-1]["old_gap"] = old
                        added += 1
                        metrics["log"].append({"round": round_no, "type": "Evasion", "precedent": pid,
                                               "what": f"Ring waiting {meta['max_hop_gap_s'] / 60:.0f} min between hops was not traced",
                                               "stored": f"{pid} · Scout window now {memory.max_hop_gap_s / 60:.0f} min"})
                    if st["trace"]["ring"] and len(st["trace"]["ring"]) > 2:
                        memory.add_genome(f"F-MOOT-R{round_no}-{i:02d}", "Evasive ring learned in moot court",
                                          genome_vector(st), "moot court")
                for acc in [a for a in grade["missed"] if a not in grade["untraced"]][:2]:
                    feats = st["features"].get(acc)
                    if feats is None:
                        continue
                    vec = D.precedent_vector(feats)
                    if memory.find_precedent(vec, "prosecution", 0.97):
                        continue
                    pid = memory.add_precedent("prosecution", vec, "aged, quiet accounts that pass ring money on "
                                               "are still mules", kind, round_no, acc)
                    added += 1
                    age = feats["age_days"] or 0
                    hop = meta["params"]["hop_delay_s"] / 60
                    metrics["log"].append({"round": round_no, "type": "Evasion", "precedent": pid,
                                           "what": f"Mule {acc} ({age:.0f}-day-old account, {hop:.0f}-min hops) "
                                                   f"was only challenged, not frozen",
                                           "stored": f"{pid} · prosecution"})
        else:
            if grade["wrongful"]:
                wrongful_cases += 1
                for acc in grade["wrongful"][:2]:
                    feats = st["features"].get(acc)
                    if feats is None:
                        continue
                    vec = D.precedent_vector(feats)
                    if memory.find_precedent(vec, "defense", 0.97):
                        continue
                    pid = memory.add_precedent("defense", vec, FRAMING_LESSONS[kind], kind, round_no, acc)
                    added += 1
                    metrics["log"].append({"round": round_no, "type": "Framing", "precedent": pid,
                                           "what": f"{kind.replace('_', ' ').capitalize()} case: {acc} was frozen wrongly",
                                           "stored": f"{pid} · defense"})
        if progress:
            await progress({"round": round_no, "case": i + 1, "of": n_cases})

    memory.save()
    return {
        "round": round_no, "cases": n_cases, "evasion_cases": evasion_n, "framing_cases": framing_n,
        "red_team_success_pct": round(100 * missed_cases / max(evasion_n, 1), 1),
        "framing_success_pct": round(100 * wrongful_cases / max(framing_n, 1), 1),
        "sent_to_court": sent, "precedents_added": added, "max_hop_gap_min": round(memory.max_hop_gap_s / 60, 1),
    }


async def run_moot(rounds: int = 8, cases_per_round: int = 20, seed: int = 11, reset: bool = False,
                   memory: Optional[Memory] = None, progress: Optional[Callable] = None) -> dict:
    memory = memory or Memory()
    if reset:
        memory.reset()
        metrics = {"rounds": [], "log": [], "difficulty": {}, "policy": {}, "baseline": None}
    else:
        metrics = _load_metrics()
    rng = random.Random(seed + len(metrics["rounds"]))
    red = RedTeam(rng)
    start_round = len(metrics["rounds"]) + 1
    started = time.time()
    bench = benchmark_cases()
    if not metrics.get("baseline"):
        metrics["baseline"] = await evaluate_benchmark(memory, bench)
    for r in range(start_round, start_round + rounds):
        result = await run_round(memory, r, cases_per_round, rng, red, metrics, progress)
        result["benchmark"] = await evaluate_benchmark(memory, bench)
        metrics["rounds"].append(result)
        metrics["difficulty"] = red.difficulty()
        metrics["policy"] = memory.data["policy"]
        fixed = {p["id"]: p.get("fixed_round") for p in memory.data["precedents"]}
        for entry in metrics["log"]:
            entry["fixed_round"] = fixed.get(entry["precedent"])
        METRICS_PATH.write_text(json.dumps(metrics, indent=1), encoding="utf-8")
    metrics["elapsed_s"] = round(time.time() - started, 1)
    return metrics


def main() -> None:
    ap = argparse.ArgumentParser(description="Run moot-court self-play rounds")
    ap.add_argument("--rounds", type=int, default=8)
    ap.add_argument("--cases", type=int, default=20)
    ap.add_argument("--reset", action="store_true", help="forget all precedents and learned policy first")
    args = ap.parse_args()
    m = asyncio.run(run_moot(args.rounds, args.cases, reset=args.reset))
    b = m["baseline"]
    print("Fixed benchmark (same 30 cases every round, never learned from):")
    print(f"{'round':>5} {'missed/100':>11} {'wrongful/100':>13} | {'red-team wins':>13} {'to court':>9} "
          f"{'new prec.':>9} {'window':>7}")
    print(f"{'base':>5} {b['missed_per_100']:>11} {b['wrongful_per_100']:>13} |")
    for r in m["rounds"][-args.rounds:]:
        print(f"{r['round']:>5} {r['benchmark']['missed_per_100']:>11} {r['benchmark']['wrongful_per_100']:>13} | "
              f"{r['red_team_success_pct']:>12}% {r['sent_to_court']:>9} {r['precedents_added']:>9} "
              f"{r['max_hop_gap_min']:>6}m")
    print(f"done in {m['elapsed_s']} s; precedents log entries: {len(m['log'])}")


if __name__ == "__main__":
    main()


async def moot_grader(state: dict) -> dict:
    from agents.agent_helpers import ctx_for
    ctx = ctx_for(state)
    await ctx.pause()
    await ctx.emit("stage.changed", {"stage": "grade"})
    verdict = state.get("verdict")
    tiers = {v.node_id: v.tier for v in (verdict.node_verdicts if verdict else [])}
    truth = ctx.ground_truth or {}
    ring = set(state.get("ring", []))

    missed = [a for a, f in truth.items() if f and tiers.get(a, 0) <= 2]
    wrongful = [a for a, f in truth.items() if not f and tiers.get(a, 0) >= 3]
    untraced = [a for a, f in truth.items() if f and a not in ring]
    correct = [a for a, f in truth.items() if (f and tiers.get(a, 0) >= 3) or (not f and tiers.get(a, 0) <= 2)]

    grade = {
        "missed": missed,
        "wrongful": wrongful,
        "untraced": untraced,
        "correct": correct,
        "tiers": tiers,
    }
    await ctx.emit("case.closed", {
        "case_id": ctx.case_id,
        "status": "graded",
        "grade": grade,
    })
    return {"grade": grade}

__all__ = ["moot_grader", "run_moot", "benchmark_cases", "load_metrics", "RedTeam", "FRAMING_KINDS", "FRAMING_LESSONS"]
