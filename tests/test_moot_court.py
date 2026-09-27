"""The moot court must make the system measurably better on a fixed exam it never learns from."""
import asyncio

from agents.moot_court.precedent_memory import Memory
from agents.moot_court.grader import run_moot


def test_moot_court_learns(tmp_path):
    memory = Memory(tmp_path / "moot-memory.json")
    m = asyncio.run(run_moot(rounds=3, cases_per_round=20, reset=True, memory=memory))
    base, last = m["baseline"], m["rounds"][-1]["benchmark"]
    assert last["missed_per_100"] < base["missed_per_100"], "missed rings should fall"
    assert last["wrongful_per_100"] <= base["wrongful_per_100"], "wrongful freezes must not rise"
    assert memory.data["precedents"], "mistakes should be stored as precedents"
    assert memory.data["policy"]["max_hop_gap_s"] > 600, "scout window should widen"
