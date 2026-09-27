"""Step 1 — (re)generate the synthetic demo dataset into data/demo/.

    python scripts/generate_data.py            # default seed 7 (matches the mockup)
    python scripts/generate_data.py --seed 42  # a different background population
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from app.config import DATA_DIR  # noqa: E402
from core.simulator import build_demo_scenario, write_scenario  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=DATA_DIR / "demo")
    args = ap.parse_args()
    scenario = build_demo_scenario(seed=args.seed)
    write_scenario(scenario, args.out)
    (args.out / "meta.json").write_text(json.dumps(scenario["meta"], indent=1), encoding="utf-8")
    print(f"Wrote {len(scenario['accounts'])} accounts, {len(scenario['transactions'])} transactions, "
          f"{len(scenario['logins'])} logins to {args.out}")


if __name__ == "__main__":
    main()
