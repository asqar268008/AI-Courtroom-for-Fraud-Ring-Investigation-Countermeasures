"""Step 4 (optional) — act like the bank's core system: POST transactions to a running server.

    uvicorn fraudcourt.api:app --port 8000      # terminal 1
    python scripts/stream_transactions.py      # terminal 2 (open http://localhost:8000 first)

It pushes the demo's live transactions one by one to /transactions/ingest; the AlertTrigger
fires on its own and the dashboard shows the investigation. Use --speed to go faster.
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--speed", type=float, default=20.0, help="data seconds per real second")
    ap.add_argument("--data", type=Path, default=ROOT / "data" / "demo")
    args = ap.parse_args()
    meta = json.loads((args.data / "meta.json").read_text())
    with open(args.data / "transactions.csv", newline="") as f:
        live = [r for r in csv.DictReader(f) if float(r["timestamp"]) >= meta["live_start"]]
    live.sort(key=lambda r: float(r["timestamp"]))
    prev = meta["live_start"]
    with httpx.Client(base_url=args.url, timeout=10) as c:
        c.post("/api/demo/reset").raise_for_status()  # history only; live txns come from us
        for r in live:
            ts = float(r["timestamp"])
            time.sleep(max(0.0, (ts - prev) / args.speed))
            prev = ts
            body = {"txn_id": r["txn_id"], "timestamp": ts, "from_account": r["from_account"],
                    "to_account": r["to_account"], "amount": float(r["amount"]), "rail": r["rail"]}
            res = c.post("/transactions/ingest", json=body).json()
            flag = f"  -> ALERT, case opened on {res['alert_opened_for']}" if res.get("alert_opened_for") else ""
            print(f"{r['from_account']:>4} -> {r['to_account']:<4} ₹{float(r['amount']):>12,.0f}{flag}")
    print("done — watch the dashboard", file=sys.stderr)


if __name__ == "__main__":
    main()
