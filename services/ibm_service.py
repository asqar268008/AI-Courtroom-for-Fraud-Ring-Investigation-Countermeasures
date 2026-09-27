"""Held-out validation on the IBM "Transactions for Anti Money Laundering (AML)" dataset.

Dataset: Altman et al., "Realistic Synthetic Financial Transactions for Anti-Money Laundering Models"
(NeurIPS 2023 Datasets & Benchmarks), Kaggle: ealtman2019/ibm-transactions-for-anti-money-laundering-aml.
Files used: <NAME>_Trans.csv (transactions + Is Laundering label) and <NAME>_Patterns.txt (every
laundering attempt, with its typology: FAN-OUT, FAN-IN, CYCLE, GATHER-SCATTER, SCATTER-GATHER, STACK,
BIPARTITE, RANDOM). The engine is never tuned on this data.

Why this is fast
* the CSV is parsed once with pandas and cached as a compact .npz (int32 account ids, int64 seconds,
  float32 USD amounts): HI-Small (~5 M rows) loads in seconds on the second run
* accounts are indexed CSR-style (one sorted slice per account for outflows and inflows), so a case
  pulls only the transactions it needs with binary search — the full graph is never built in Python
* each case gets a small GraphStore: the seed's money trail up to 4 hops plus 30 days of history for
  every account on it, with very busy accounts capped

Why this is honest
* no look-ahead: a case only sees transactions up to alert time + `horizon` (default 24 h)
* the seed and alert time come from the transactions, not from the answer key
* negatives: clean accounts with the same kind of activity, judged the same way
* a simple baseline (freeze the seed and every account it paid in the window) is scored alongside
* IBM has no devices, IPs, KYC dates or merchant flags, so those evidence types cannot fire here
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

DAY = 86400

# Approximate USD value of one unit of each currency in the dataset (Sept 2022). Only used so that
# amounts in different currencies are comparable; the court's decisions do not depend on exact rates.
USD_RATE = {"US Dollar": 1.0, "Euro": 1.0, "Yuan": 0.145, "Yen": 0.0070, "UK Pound": 1.16, "Rupee": 0.0125,
            "Ruble": 0.0165, "Swiss Franc": 1.04, "Australian Dollar": 0.68, "Canadian Dollar": 0.75,
            "Mexican Peso": 0.050, "Brazil Real": 0.19, "Shekel": 0.29, "Saudi Riyal": 0.266, "Bitcoin": 20000.0}

PATTERN_RE = re.compile(r"BEGIN LAUNDERING ATTEMPT - ([A-Z\-]+)")


def _acct(bank, account) -> str:
    bank = str(bank).strip()
    try:
        bank = str(int(bank))
    except ValueError:
        pass
    return f"{bank}-{str(account).strip()}"


# ----------------------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------------------
@dataclass
class TxnTable:
    names: np.ndarray            # account index -> "bank-account"
    src: np.ndarray              # int32
    dst: np.ndarray              # int32
    ts: np.ndarray               # int64 seconds (UTC)
    amount: np.ndarray           # float32, USD
    fmt: np.ndarray              # int16 index into formats
    formats: np.ndarray
    label: np.ndarray            # bool
    index: dict = field(default_factory=dict)
    _out: tuple = ()
    _in: tuple = ()

    @property
    def n(self) -> int:
        return len(self.src)

    def build_index(self) -> None:
        self.index = {name: i for i, name in enumerate(self.names.tolist())}
        n_acc = len(self.names)
        for attr, key in (("_out", self.src), ("_in", self.dst)):
            order = np.lexsort((self.ts, key))  # by account, then time
            counts = np.bincount(key, minlength=n_acc)
            offsets = np.concatenate([[0], np.cumsum(counts)])
            setattr(self, attr, (order, offsets))

    def _slice(self, which: str, acc: int, start: float, end: float, cap: int) -> np.ndarray:
        order, offsets = self._out if which == "out" else self._in
        rows = order[offsets[acc]:offsets[acc + 1]]
        t = self.ts[rows]
        lo, hi = np.searchsorted(t, start, "left"), np.searchsorted(t, end, "right")
        rows = rows[lo:hi]
        return rows[-cap:] if len(rows) > cap else rows

    def out_rows(self, acc: int, start: float, end: float, cap: int = 400) -> np.ndarray:
        return self._slice("out", acc, start, end, cap)

    def in_rows(self, acc: int, start: float, end: float, cap: int = 400) -> np.ndarray:
        return self._slice("in", acc, start, end, cap)


def load_transactions(path: Path, max_rows: Optional[int] = None, use_cache: bool = True) -> TxnTable:
    """Parse an IBM *_Trans.csv (or its cache). Columns are taken by position because the file has two
    columns both called "Account"."""
    path = Path(path)
    cache = path.with_suffix(path.suffix + (f".{max_rows}" if max_rows else "") + ".fc2.npz")
    if use_cache and cache.exists() and cache.stat().st_mtime >= path.stat().st_mtime:
        z = np.load(cache, allow_pickle=True)
        t = TxnTable(z["names"], z["src"], z["dst"], z["ts"], z["amount"], z["fmt"], z["formats"], z["label"])
        t.build_index()
        return t
    import pandas as pd

    df = pd.read_csv(path, header=0, nrows=max_rows, dtype=str, usecols=range(11), engine="c")
    df.columns = ["Timestamp", "FromBank", "FromAcc", "ToBank", "ToAcc", "AmtRecv", "CurRecv", "AmtPaid",
                  "CurPaid", "Format", "Label"]
    dt = pd.to_datetime(df["Timestamp"], format="%Y/%m/%d %H:%M")
    # resolution-independent (pandas 2 uses ns, pandas 3 may use us/s): seconds since the epoch, UTC
    ts = ((dt - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).to_numpy()
    src_names = _norm_bank(df["FromBank"]) + "-" + df["FromAcc"].str.strip()
    dst_names = _norm_bank(df["ToBank"]) + "-" + df["ToAcc"].str.strip()
    codes, names = pd.factorize(pd.concat([src_names, dst_names], ignore_index=True))
    n = len(df)
    rate = df["CurPaid"].map(USD_RATE).fillna(1.0).to_numpy()
    amount = (pd.to_numeric(df["AmtPaid"], errors="coerce").fillna(0).to_numpy() * rate).astype("float32")
    fmt_codes, formats = pd.factorize(df["Format"])
    t = TxnTable(np.asarray(names, dtype=object), codes[:n].astype("int32"), codes[n:].astype("int32"), ts.astype("int64"),
                 amount, fmt_codes.astype("int16"), np.asarray(formats, dtype=object), (df["Label"].str.strip() == "1").to_numpy())
    if use_cache:
        np.savez(cache, names=t.names, src=t.src, dst=t.dst, ts=t.ts, amount=t.amount, fmt=t.fmt,
                 formats=t.formats, label=t.label)
    t.build_index()
    return t


def _norm_bank(col):
    num = col.str.strip().str.lstrip("0")
    return num.where(num != "", "0")


@dataclass
class Attempt:
    kind: str
    rows: list[tuple]            # (ts, src_name, dst_name, amount_usd)

    @property
    def accounts(self) -> set[str]:
        return {r[1] for r in self.rows} | {r[2] for r in self.rows}


def load_patterns(path: Path) -> list[Attempt]:
    from datetime import datetime, timezone
    attempts, current = [], None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            m = PATTERN_RE.match(line)
            if m:
                current = Attempt(m.group(1), [])
                continue
            if line.startswith("END LAUNDERING ATTEMPT"):
                if current and current.rows:
                    attempts.append(current)
                current = None
                continue
            if current is not None and line:
                p = line.split(",")
                if len(p) < 11:
                    continue
                ts = datetime.strptime(p[0], "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc).timestamp()
                amt = float(p[7]) * USD_RATE.get(p[8], 1.0)
                current.rows.append((ts, _acct(p[1], p[2]), _acct(p[3], p[4]), amt))
    return attempts


# ----------------------------------------------------------------------------------------
# Per-case subgraph
# ----------------------------------------------------------------------------------------
@dataclass
class Profile:
    """Time scales for IBM's slower, minute-resolution laundering (the live demo uses seconds)."""
    alert_window_s: int = 1 * DAY       # inflows to the seed counted as the money under investigation
    max_hop_gap_s: int = 1 * DAY        # follow money that leaves within a day of arriving
    fast_dwell_s: int = 6 * 3600        # "fast pass-through" if forwarded within 6 hours
    horizon_s: int = 1 * DAY            # no look-ahead beyond alert + horizon
    history_s: int = 30 * DAY
    trace_depth: int = 4
    hub_cap: int = 300                  # at most this many txns per account per direction (busy accounts)


def extract_store(t: TxnTable, seed: int, alert_time: float, prof: Profile):
    """Money trail from the seed (forward, depth-limited, no look-ahead) + history of every account on it."""
    from database.graph_store import GraphStore
    start, end = alert_time - prof.alert_window_s, alert_time + prof.horizon_s
    rows: set[int] = set()
    rows.update(t.in_rows(seed, start, alert_time, prof.hub_cap).tolist())
    frontier, seen = {seed}, {seed}
    for _ in range(prof.trace_depth):
        nxt = set()
        for a in frontier:
            out = t.out_rows(a, start, end, prof.hub_cap)
            rows.update(out.tolist())
            for d in t.dst[out].tolist():
                if d not in seen:
                    seen.add(d)
                    nxt.add(d)
        frontier = nxt
        if len(seen) > 400:
            break
    hist_start = alert_time - prof.history_s
    for a in seen:  # history: needed for account profiles, counterfactuals and precedents
        rows.update(t.out_rows(a, hist_start, end, 60).tolist())
        rows.update(t.in_rows(a, hist_start, end, 60).tolist())
    idx = np.fromiter(rows, dtype=np.int64)
    idx = idx[t.ts[idx] <= end]
    store = GraphStore()
    for acc in set(t.src[idx].tolist()) | set(t.dst[idx].tolist()) | {seed}:
        store.add_account(t.names[acc])  # IBM has no account-opening dates or merchant flags
    for i in idx[np.argsort(t.ts[idx], kind="stable")].tolist():
        store.add_txn(f"IBM-{i}", float(t.ts[i]), t.names[t.src[i]], t.names[t.dst[i]], float(t.amount[i]),
                      str(t.formats[t.fmt[i]]))
    return store


def choose_seed(att: Attempt) -> tuple[str, float]:
    """The account whose outgoing burst a velocity rule would flag first, and the time it would fire:
    the account with most outgoing pattern transfers; alert after its 3rd (or last) one."""
    outs: dict[str, list[float]] = {}
    for ts, s, _d, _a in att.rows:
        outs.setdefault(s, []).append(ts)
    seed = max(outs, key=lambda a: (len(outs[a]), -min(outs[a])))
    times = sorted(outs[seed])
    return seed, times[min(2, len(times) - 1)] + 60


# ----------------------------------------------------------------------------------------
# Evaluation
# ----------------------------------------------------------------------------------------
async def judge_case(t: TxnTable, seed_name: str, alert_time: float, prof: Profile, memory, settings) -> dict:
    from core.workflow import run_case
    seed = t.index[seed_name]
    t0 = time.perf_counter()
    store = extract_store(t, seed, alert_time, prof)
    extract_s = time.perf_counter() - t0
    final, _ = await run_case(store, memory, seed_name, alert_time, mode="moot", reason="IBM validation",
                              ground_truth={}, settings=settings, settle=False)
    tiers = {v.node_id: v.tier for v in final["verdict"].node_verdicts}
    from agents.investigation.evidence_aggregation_agent import feature_vector
    sim = final["genome"]["match"]["similarity"] if final["genome"]["match"]["similarity"] >= 0.6 else 0.0
    vectors = {a: feature_vector(f, sim) for a, f in final["features"].items()}
    # baseline: freeze the seed and everyone it paid in the alert window
    paid = t.out_rows(seed, alert_time - prof.alert_window_s, alert_time, 10_000)
    baseline = {seed_name} | {t.names[d] for d in t.dst[paid].tolist()}
    return {"flagged": sorted(a for a, tr in tiers.items() if tr >= 3), "traced": sorted(tiers),
            "baseline": sorted(baseline), "seconds": time.perf_counter() - t0, "extract_s": extract_s,
            "store_txns": len(store.txns), "confidence": final["verdict"].confidence,
            "challenged": sorted(a for a, tr in tiers.items() if tr >= 2), "vectors": vectors}


def negative_seeds(t: TxnTable, laundering: set[str], n: int, rng: np.random.Generator,
                   min_out: int = 3) -> list[tuple[str, float]]:
    """Clean accounts with a burst of >= 3 outgoing transfers in a day (what a velocity rule would flag)."""
    order, offsets = t._out
    counts = np.diff(offsets)
    candidates = np.flatnonzero(counts >= min_out)
    rng.shuffle(candidates)
    out = []
    for acc in candidates:
        name = t.names[acc]
        if name in laundering:
            continue
        rows = order[offsets[acc]:offsets[acc + 1]]
        ts = t.ts[rows]
        for i in range(min_out - 1, len(ts)):
            if ts[i] - ts[i - min_out + 1] <= DAY:
                out.append((name, float(ts[i]) + 60))
                break
        if len(out) >= n:
            break
    return out


def score(rows: list[dict], laundering: set[str]) -> dict:
    """Aggregate account-level metrics for the court and the baseline."""
    def agg(key: str) -> dict:
        tp = fp = pattern_hit = pattern_total = 0
        for r in rows:
            flagged = set(r[key])
            tp += len(flagged & laundering)
            fp += len(flagged - laundering)
            if r["positive"]:
                pattern_hit += len(flagged & r["pattern_accounts"])
                pattern_total += len(r["pattern_accounts"])
        neg = [r for r in rows if not r["positive"]]
        return {"precision": tp / (tp + fp) if tp + fp else 0.0,
                "pattern_recall": pattern_hit / pattern_total if pattern_total else 0.0,
                "flagged": tp + fp, "wrongful_accounts": fp,
                "negatives_with_freeze": sum(1 for r in neg if r[key]) / len(neg) if neg else 0.0}
    return {"court": agg("flagged"), "baseline": agg("baseline")}
