"""Scenario scorecard: generate many fresh cases of every typology and grade the court.

    python scripts/evaluate_scenarios.py --per-kind 20
Writes runtime/reports/scenario-scorecard.html and .csv (open the HTML in a browser).
"""
import argparse
import asyncio
import csv
import os
import random
import sys
import time
from pathlib import Path

os.environ.setdefault("LLM_PROVIDER", "mock")  # grading does not need the LLM; decisions are code
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.ibm_service import replay_and_judge  # noqa: E402
from services.report_service import REPORTS_DIR, scorecard_html, write_report  # noqa: E402
from core.scenarios import FRAUD_KINDS, INNOCENT_KINDS, build_scenario  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-kind", type=int, default=20)
    ap.add_argument("--seed", type=int, default=None, help="fix for a reproducible scorecard")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    rows = []
    start = time.perf_counter()
    for kind in FRAUD_KINDS + INNOCENT_KINDS:
        for _ in range(args.per_kind):
            rows.append(await replay_and_judge(build_scenario(kind, rng.randrange(1, 10**6))))
        rs = [r for r in rows if r["kind"] == kind]
        f = sum(r["fraud_accounts"] for r in rs)
        print(f"{kind:<20} alerts {sum(r['alerted'] for r in rs):>3}/{len(rs)}  stopped "
              f"{(100 * sum(r['caught'] for r in rs) / f) if f else 0:5.1f}%  wrongful {sum(r['wrongful'] for r in rs):>3}  "
              f"auto {sum(r['route'] == 'act' for r in rs):>3}  review {sum(r['route'] == 'review' for r in rs):>3}")
    path = write_report("scenario-scorecard.html", scorecard_html(rows))
    with open(REPORTS_DIR / "scenario-scorecard.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) + ["seed_correct"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"\n{len(rows)} cases in {time.perf_counter() - start:.1f}s -> {path}")


if __name__ == "__main__":
    asyncio.run(main())
