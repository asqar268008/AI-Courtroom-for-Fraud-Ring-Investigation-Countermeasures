"""Write a SMALL SYNTHETIC dataset in the exact IBM AML file format (…_Trans.csv + …_Patterns.txt).

This is NOT the IBM dataset. It exists so the validation pipeline can be tried and tested without a
Kaggle download. For real numbers, download HI-Small_Trans.csv and HI-Small_Patterns.txt from Kaggle
(ealtman2019/ibm-transactions-for-anti-money-laundering-aml) and point validate_ibm.py at them.

    python scripts/make_ibm_like_sample.py --rows 200000 --out data/ibm_like
"""
import argparse
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

KINDS = ["FAN-OUT", "FAN-IN", "CYCLE", "GATHER-SCATTER", "SCATTER-GATHER", "STACK", "BIPARTITE", "RANDOM"]
CURRENCIES = [("US Dollar", 0.55), ("Euro", 0.2), ("Rupee", 0.08), ("Yuan", 0.07), ("UK Pound", 0.1)]
FORMATS = ["ACH", "Cheque", "Credit Card", "Wire", "Cash", "Reinvestment"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=200_000)
    ap.add_argument("--accounts", type=int, default=20_000)
    ap.add_argument("--attempts", type=int, default=30, help="laundering attempts per pattern type")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=Path("data/ibm_like"))
    args = ap.parse_args()
    rng = random.Random(args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    start = datetime(2022, 9, 1, tzinfo=timezone.utc)
    days = 16
    accts = [(f"{rng.randint(1, 400):03d}", f"{rng.getrandbits(36):09X}") for _ in range(args.accounts)]
    currency = {a: rng.choices([c for c, _ in CURRENCIES], [w for _, w in CURRENCIES])[0] for a in accts}
    friends = {a: rng.sample(accts, rng.randint(2, 8)) for a in accts}  # regular counterparties

    def row(ts, a, b, amt, fmt, label):
        cur = currency[a]
        return (f"{ts.strftime('%Y/%m/%d %H:%M')},{a[0]},{a[1]},{b[0]},{b[1]},{amt:.2f},{cur},{amt:.2f},{cur},"
                f"{fmt},{label}")

    rows = []
    for _ in range(args.rows):
        a = rng.choice(accts)
        b = rng.choice(friends[a]) if rng.random() < 0.8 else rng.choice(accts)
        ts = start + timedelta(minutes=rng.randrange(days * 1440))
        rows.append((ts, row(ts, a, b, rng.lognormvariate(7, 1.4), rng.choice(FORMATS), 0)))

    launderers = rng.sample(accts, min(len(accts), 40 * args.attempts * len(KINDS)))
    pool = iter(launderers)
    patterns = []
    for kind in KINDS:
        for _ in range(args.attempts):
            t = start + timedelta(minutes=rng.randrange((days - 4) * 1440))
            base = rng.lognormvariate(9, 1)
            edges = []
            step = lambda: timedelta(minutes=rng.randint(10, 600))  # noqa: E731

            def take(n):
                return [next(pool) for _ in range(n)]
            if kind == "FAN-OUT":
                hub, leaves = take(1)[0], take(rng.randint(4, 10))
                for l in leaves:
                    t += step(); edges.append((t, hub, l, base))
            elif kind == "FAN-IN":
                hub, leaves = take(1)[0], take(rng.randint(4, 10))
                for l in leaves:
                    t += step(); edges.append((t, l, hub, base))
            elif kind == "CYCLE":
                ring = take(rng.randint(3, 8))
                for i, a in enumerate(ring):
                    t += step(); edges.append((t, a, ring[(i + 1) % len(ring)], base * 0.98 ** i))
            elif kind in ("GATHER-SCATTER", "SCATTER-GATHER"):
                hub, a_side, b_side = take(1)[0], take(rng.randint(3, 6)), take(rng.randint(3, 6))
                if kind == "GATHER-SCATTER":
                    for a in a_side:
                        t += step(); edges.append((t, a, hub, base))
                    for b in b_side:
                        t += step(); edges.append((t, hub, b, base * len(a_side) / len(b_side) * 0.97))
                else:
                    src, dst = hub, take(1)[0]
                    for a in a_side:
                        t += step(); edges.append((t, src, a, base))
                    for a in a_side:
                        t += step(); edges.append((t, a, dst, base * 0.97))
            elif kind == "STACK":
                layers = [take(3), take(3), take(3)]
                for l1, l2 in zip(layers, layers[1:]):
                    for a in l1:
                        for b in rng.sample(l2, 2):
                            t += step(); edges.append((t, a, b, base / 2))
            elif kind == "BIPARTITE":
                a_side, b_side = take(rng.randint(3, 5)), take(rng.randint(3, 5))
                for a in a_side:
                    for b in rng.sample(b_side, 2):
                        t += step(); edges.append((t, a, b, base))
            else:  # RANDOM walk
                cur = take(1)[0]
                for _ in range(rng.randint(4, 8)):
                    nxt = take(1)[0]
                    t += step(); edges.append((t, cur, nxt, base)); cur = nxt
            lines = [row(ts, a, b, amt, rng.choice(["ACH", "Wire", "Cheque"]), 1) for ts, a, b, amt in edges]
            patterns.append((kind, lines))
            rows += [(ts, ln) for (ts, *_), ln in zip(edges, lines)]

    rows.sort(key=lambda r: r[0])
    header = "Timestamp,From Bank,Account,To Bank,Account,Amount Received,Receiving Currency,Amount Paid,Payment Currency,Payment Format,Is Laundering\n"
    (args.out / "SAMPLE_Trans.csv").write_text(header + "\n".join(r[1] for r in rows) + "\n")
    with open(args.out / "SAMPLE_Patterns.txt", "w") as fh:
        for kind, lines in patterns:
            fh.write(f"BEGIN LAUNDERING ATTEMPT - {kind}:  synthetic sample\n" + "\n".join(lines) + f"\nEND LAUNDERING ATTEMPT - {kind}\n\n")
    print(f"{len(rows)} transactions, {len(patterns)} laundering attempts -> {args.out} (synthetic, IBM file format)")


if __name__ == "__main__":
    main()
