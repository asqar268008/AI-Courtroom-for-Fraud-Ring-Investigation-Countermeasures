"""Held-out validation of the court on the IBM AML dataset (or any file in the same format).

    python scripts/validate_ibm.py --trans HI-Small_Trans.csv --patterns HI-Small_Patterns.txt
    python scripts/validate_ibm.py --trans data/ibm_like/SAMPLE_Trans.csv --patterns data/ibm_like/SAMPLE_Patterns.txt

Writes runtime/reports/ibm-validation.html and ibm-validation.csv. See fraudcourt/ibm.py for the protocol.
"""
import argparse
import asyncio
import copy
import csv
import os
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

os.environ.setdefault("LLM_PROVIDER", "mock")  # decisions are code; the LLM would only add cost here
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import ibm_service as ibm  # noqa: E402
from app.config import settings as base  # noqa: E402
from agents.moot_court.precedent_memory import Memory  # noqa: E402
from services.report_service import REPORTS_DIR, e, page, write_report  # noqa: E402
from agents.investigation.evidence_aggregation_agent import fit_logistic  # noqa: E402


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


async def run_set(cases, table, prof, memory, s, laundering):
    rows = []
    for c in cases:
        r = await ibm.judge_case(table, c["seed"], c["at"], prof, memory, s)
        rows.append({**r, **c})
    return rows


def metrics(rows, laundering):
    m = ibm.score(rows, laundering)
    pos = [r for r in rows if r["positive"]]
    total = sum(len(r["pattern_accounts"]) for r in pos) or 1
    m["court"]["traced_recall"] = sum(len(set(r["traced"]) & r["pattern_accounts"]) for r in pos) / total
    m["court"]["challenged_recall"] = sum(len(set(r["challenged"]) & r["pattern_accounts"]) for r in pos) / total
    return m


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trans", type=Path, required=True)
    ap.add_argument("--patterns", type=Path, required=True)
    ap.add_argument("--per-pattern", type=int, default=40, help="laundering attempts sampled per typology")
    ap.add_argument("--negatives", type=int, default=300, help="clean alerts to judge")
    ap.add_argument("--horizon-hours", type=float, default=24, help="how far past the alert the court may look")
    ap.add_argument("--max-rows", type=int, default=None, help="read only the first N rows (quick runs)")
    ap.add_argument("--no-calibrate", action="store_true", help="only score the hand-set (zero-shot) weights")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    t0 = time.perf_counter()
    table = ibm.load_transactions(args.trans, args.max_rows)
    load_s = time.perf_counter() - t0
    attempts = ibm.load_patterns(args.patterns)
    laundering = {table.names[i] for i in np.concatenate([table.src[table.label], table.dst[table.label]]).tolist()}
    print(f"Loaded {table.n:,} transactions, {len(table.names):,} accounts in {load_s:.1f}s; "
          f"{len(attempts)} laundering attempts, {len(laundering):,} laundering accounts")

    prof = ibm.Profile(horizon_s=int(args.horizon_hours * 3600))
    s = copy.copy(base)
    s.stage_delay = s.token_delay = s.settle_window = 0
    s.alert_window_s, s.fast_dwell_s, s.trace_depth = prof.alert_window_s, prof.fast_dwell_s, prof.trace_depth
    s.max_hop_gap_cap_s = prof.max_hop_gap_s
    s.unavailable_signals = ("shared_attr", "new_account")  # IBM has no device/IP logins or opening dates
    s.inr_per_unit = 83.0  # amounts were converted to USD; the freeze policy's costs are in rupees
    memory = Memory(Path(tempfile.mkdtemp()) / "ibm-memory.json")  # throwaway: no precedents from IBM
    memory.data["policy"]["max_hop_gap_s"] = prof.max_hop_gap_s

    rng = np.random.default_rng(args.seed)
    by_kind = defaultdict(list)
    for a in attempts:
        if all(r[1] in table.index for r in a.rows):
            by_kind[a.kind].append(a)
    cases = []
    for kind in sorted(by_kind):
        pick = rng.choice(len(by_kind[kind]), size=min(args.per_pattern, len(by_kind[kind])), replace=False)
        for i in pick.tolist():
            att = by_kind[kind][i]
            seed, at = ibm.choose_seed(att)
            cases.append({"kind": kind, "positive": True, "seed": seed, "at": at, "pattern_accounts": att.accounts})
    for seed, at in ibm.negative_seeds(table, laundering, args.negatives, rng):
        cases.append({"kind": "clean", "positive": False, "seed": seed, "at": at, "pattern_accounts": set()})

    # Time split: calibrate on the earlier half of alerts, test on the later half (no leakage from the future)
    split = float(np.median([c["at"] for c in cases]))
    calib = [c for c in cases if c["at"] < split]
    test = [c for c in cases if c["at"] >= split]
    print(f"{len(cases)} cases: {len(calib)} for calibration (before split), {len(test)} held out for testing")

    zero = await run_set(test, table, prof, memory, s, laundering)
    results = {"zero": metrics(zero, laundering)}
    final_rows, model = zero, None
    if not args.no_calibrate:
        cal_rows = await run_set(calib, table, prof, memory, s, laundering)
        X, y = [], []
        for r in cal_rows:
            for acc, vec in r["vectors"].items():
                X.append(vec)
                y.append(1 if acc in laundering else 0)
        model = fit_logistic(X, y)
        s.risk_model = model
        final_rows = await run_set(test, table, prof, memory, s, laundering)
        results["calibrated"] = metrics(final_rows, laundering)
        print(f"Calibrated on {len(X):,} traced accounts ({sum(y):,} laundering)")
    total_s = time.perf_counter() - t0

    def line(name, m):
        return (f"{name:<28} recall {pct(m['pattern_recall']):>6}  precision {pct(m['precision']):>6}  "
                f"clean alerts with a freeze {pct(m['negatives_with_freeze']):>6}")
    print(line("Baseline (seed + payees)", results["zero"]["baseline"]))
    print(line("Court, hand-set weights", results["zero"]["court"]))
    if model:
        print(line("Court, calibrated", results["calibrated"]["court"]))
    cc = results.get("calibrated", results["zero"])["court"]
    print(f"Court investigation reach: traced {pct(cc['traced_recall'])} of pattern accounts, "
          f"challenged (step-up or stronger) {pct(cc['challenged_recall'])}")
    per_kind = {k: metrics([r for r in final_rows if r["kind"] == k], laundering) for k in sorted(by_kind)}
    print(f"{len(cases) + (len(test) if model else 0)} court runs in {total_s:.1f}s "
          f"(median {statistics.median(r['seconds'] for r in final_rows) * 1000:.0f} ms/case)")

    with open(REPORTS_DIR / "ibm-validation.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["kind", "seed", "positive", "pattern_accounts", "traced", "court_frozen", "court_frozen_correct",
                    "baseline_frozen", "ms"])
        for r in final_rows:
            w.writerow([r["kind"], r["seed"], r["positive"], len(r["pattern_accounts"]), len(r["traced"]),
                        len(r["flagged"]), len(set(r["flagged"]) & laundering), len(r["baseline"]), round(r["seconds"] * 1000)])

    show = results.get("calibrated", results["zero"])
    c, b, z = show["court"], results["zero"]["baseline"], results["zero"]["court"]
    synthetic = "SAMPLE" in args.trans.name

    def trow(name, m, extra=True):
        return (f"<tr><td>{e(name)}</td><td>{pct(m['pattern_recall'])}</td><td>{pct(m['precision'])}</td>"
                f"<td>{pct(m['negatives_with_freeze'])}</td><td>{m['flagged']}</td>"
                f"<td>{pct(m['traced_recall']) if extra and 'traced_recall' in m else 'â€”'}</td></tr>")
    comp = trow("Baseline: freeze the alerted account and everyone it paid", b, False) + trow("Court, hand-set weights (zero-shot)", z)
    if model:
        comp += trow("Court, calibrated on the earlier half", c)
    kinds = "".join(f"<tr><td>{e(k)}</td><td>{sum(1 for r in final_rows if r['kind'] == k)}</td>"
                    f"<td>{pct(v['court']['traced_recall'])}</td><td>{pct(v['court']['pattern_recall'])}</td>"
                    f"<td>{pct(v['court']['precision'])}</td><td>{pct(v['baseline']['pattern_recall'])}</td></tr>"
                    for k, v in per_kind.items())
    weights = ""
    if model:
        weights = ("<h2>Calibrated risk weights</h2><p>Logistic regression fitted only on accounts traced in the "
                   "calibration half. Positive weights raise suspicion.</p><table><tr><th>Signal</th><th>Weight</th></tr>"
                   + "".join(f"<tr><td>{e(k)}</td><td>{v:+.2f}</td></tr>" for k, v in model["weights"].items())
                   + f"<tr><td>bias</td><td>{model['bias']:+.2f}</td></tr></table>")
    body = (
        f"<h1>Held-out validation: {'IBM-format synthetic sample' if synthetic else 'IBM AML dataset'}</h1>"
        f"<p class='sub mono'>{e(args.trans.name)} Â· {table.n:,} transactions Â· {len(table.names):,} accounts Â· "
        f"{len(attempts)} laundering attempts Â· {len(test)} test cases Â· horizon {args.horizon_hours:g} h Â· {total_s:.0f} s</p>"
        + ("<p class='warn'>This run uses a small synthetic file in the IBM format, not the IBM data. Download "
           "HI-Small_Trans.csv and HI-Small_Patterns.txt from Kaggle for real numbers.</p>" if synthetic else "")
        + f"<div class='kpis'><div class='kpi'><span>Laundering accounts frozen</span><b>{pct(c['pattern_recall'])}</b></div>"
        f"<div class='kpi'><span>Freezes that were laundering</span><b>{pct(c['precision'])}</b></div>"
        f"<div class='kpi'><span>Clean alerts with any freeze</span><b>{pct(c['negatives_with_freeze'])}</b></div>"
        f"<div class='kpi'><span>Median time per case</span><b>{statistics.median(r['seconds'] for r in final_rows) * 1000:.0f} ms</b></div></div>"
        f"<h2>Court vs a simple baseline (held-out later half)</h2><table><tr><th></th><th>Recall (pattern accounts frozen)</th>"
        f"<th>Precision</th><th>Clean alerts with a freeze</th><th>Accounts frozen</th><th>Pattern accounts traced</th></tr>{comp}</table>"
        f"<p>The court's investigation reached {pct(c['traced_recall'])} of the laundering accounts and challenged "
        f"{pct(c['challenged_recall'])} of them with at least a step-up; it freezes only when the evidence clears the "
        f"policy, which is why its precision is high and its freeze recall lower.</p>"
        f"<h2>By laundering typology</h2><table><tr><th>Typology</th><th>Cases</th><th>Traced</th><th>Frozen (recall)</th>"
        f"<th>Precision</th><th>Baseline recall</th></tr>{kinds}</table>{weights}"
        "<h2>Protocol</h2><p>For each sampled laundering attempt the alert is raised on the account with the most "
        "outgoing transfers in the attempt, one minute after its third transfer â€” what a velocity rule would do. The "
        "court sees only transactions up to the horizon after the alert (no look-ahead) plus 30 days of history. Clean "
        "alerts are accounts never involved in laundering that made three or more transfers within a day. Cases are "
        "split by time: the earlier half calibrates the risk weights, the later half is the test. 'Frozen' means a "
        "soft or hard freeze (tier 3+); 'traced' means the account was reached by the money-trail search. A frozen "
        "account counts as correct if it takes part in any laundering attempt in the dataset.</p>"
        "<h2>Limits to state when presenting these numbers</h2><p>The IBM data has no devices, IP addresses, "
        "account-opening dates or merchant flags, so those signals are switched off and the remaining weights "
        "renormalised; timestamps are per minute, so dwell thresholds are in hours. FAN-IN and GATHER patterns send "
        "money <i>into</i> the alerted account and the court treats senders as victims by design (it was built for "
        "scam-mule rings), which lowers recall on those types. Amounts were converted to USD with approximate fixed "
        "rates. The baseline freezes something on every alert, so its recall comes with 100% of clean alerts frozen.</p>")
    path = write_report("ibm-validation.html", page("IBM AML validation", body))
    print(f"Report -> {path}")


if __name__ == "__main__":
    asyncio.run(main())
