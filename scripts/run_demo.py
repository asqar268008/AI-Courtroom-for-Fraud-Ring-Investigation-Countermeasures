"""Step 2 â€” run the golden case in the terminal (no server, no browser).

    python scripts/run_demo.py
Prints evidence, each agent's argument, the per-account verdict table and the audit check.
"""
import asyncio
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

os.environ.setdefault("FC_STAGE_DELAY", "0")
os.environ.setdefault("FC_TOKEN_DELAY", "0")
os.environ.setdefault("FC_SETTLE_WINDOW", "0")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from security.audit import AuditLog  # noqa: E402
from core.events import EventBus  # noqa: E402
from database.graph_store import GraphStore  # noqa: E402
from agents.moot_court.precedent_memory import Memory  # noqa: E402
from core.simulator import build_demo_scenario  # noqa: E402
from core.workflow import run_case  # noqa: E402


async def main() -> None:
    sc = build_demo_scenario()
    store = GraphStore.from_records(sc["accounts"], sc["transactions"], sc["logins"])
    bus = EventBus()
    audit = AuditLog()
    final, ctx = await run_case(store, Memory(), "A", sc["meta"]["t0"] + 70, reason="rapid pass-through",
                                bus=bus, audit=audit, ground_truth=sc["meta"]["ground_truth"])

    print(f"\n=== Case {final['case_id']} ===")
    print("\nEvidence registry")
    for e in final["evidence"]:
        print(f"  {e.id:<11} {e.text}")
    for side in ("prosecution", "defense"):
        b = final[side]
        print(f"\n{side.upper()} (cites {', '.join(b.cited_evidence)})\n  {b.summary}")
    v = final["verdict"]
    print(f"\nJUDGE  confidence {v.confidence:.2f}  counterfactual_ok={v.counterfactual_ok}  route={final['route']}")
    print(f"  {v.summary}\n")
    print(f"  {'acct':<5} {'role':<10} {'p':>5}  tier  action")
    for nv in sorted(v.node_verdicts, key=lambda n: (-n.tier, n.node_id)):
        print(f"  {nv.node_id:<5} {nv.role:<10} {nv.p_fraud:>5.2f}  {nv.tier:>4}  {nv.action}")
    print(f"\nActions: {[(a.get('endpoint'), a.get('accounts', a.get('reason', ''))) for a in final['actions']]}")
    print(f"SAR: {final.get('sar_path')}")
    print(f"Audit chain intact: {audit.verify()}   pipeline time: {ctx.elapsed():.2f} s")


if __name__ == "__main__":
    asyncio.run(main())
