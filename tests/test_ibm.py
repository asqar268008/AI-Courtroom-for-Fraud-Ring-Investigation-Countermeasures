"""IBM AML pipeline: parsing, indexing, no look-ahead, and an end-to-end validation run on a tiny
synthetic file in the IBM format (the real dataset is not needed for tests)."""
import subprocess
import sys
from pathlib import Path

import numpy as np

from services import ibm_service as ibm

ROOT = Path(__file__).resolve().parent.parent


def make_sample(tmp_path):
    subprocess.run([sys.executable, str(ROOT / "scripts/make_ibm_like_sample.py"), "--rows", "20000", "--accounts",
                    "3000", "--attempts", "6", "--out", str(tmp_path)], check=True, capture_output=True)
    return tmp_path / "SAMPLE_Trans.csv", tmp_path / "SAMPLE_Patterns.txt"


def test_load_index_and_patterns(tmp_path):
    trans, patterns = make_sample(tmp_path)
    t = ibm.load_transactions(trans)
    assert t.n > 20000 and t.label.sum() > 0
    assert (t.ts > 1.6e9).all(), "timestamps must be epoch seconds"
    atts = ibm.load_patterns(patterns)
    assert {a.kind for a in atts} == {"FAN-OUT", "FAN-IN", "CYCLE", "GATHER-SCATTER", "SCATTER-GATHER", "STACK",
                                     "BIPARTITE", "RANDOM"}
    assert all(r[1] in t.index and r[2] in t.index for a in atts for r in a.rows), "pattern accounts match"
    # CSR slices return only that account's outflows, in time order
    acc = int(t.src[0])
    rows = t.out_rows(acc, 0, 2e9, cap=10**6)
    assert (t.src[rows] == acc).all() and (np.diff(t.ts[rows]) >= 0).all()
    t2 = ibm.load_transactions(trans)  # second load comes from the .npz cache
    assert t2.n == t.n and (t2.ts == t.ts).all()


def test_no_look_ahead(tmp_path):
    trans, patterns = make_sample(tmp_path)
    t = ibm.load_transactions(trans)
    att = ibm.load_patterns(patterns)[0]
    seed, at = ibm.choose_seed(att)
    prof = ibm.Profile(horizon_s=3600)
    store = ibm.extract_store(t, t.index[seed], at, prof)
    assert store.txns and max(x["ts"] for x in store.txns.values()) <= at + 3600


def test_validation_script_runs(tmp_path):
    trans, patterns = make_sample(tmp_path)
    out = subprocess.run([sys.executable, str(ROOT / "scripts/validate_ibm.py"), "--trans", str(trans), "--patterns",
                          str(patterns), "--per-pattern", "4", "--negatives", "20"],
                         capture_output=True, text=True, env={**__import__("os").environ, "FC_RUNTIME_DIR": str(tmp_path / "rt")})
    assert out.returncode == 0, out.stderr[-2000:]
    assert "Court, calibrated" in out.stdout
    assert (tmp_path / "rt" / "reports" / "ibm-validation.html").exists()
