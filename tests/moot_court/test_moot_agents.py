"""Tests for Moot Court: Red Team mutations, grading, and precedent memory."""
import random
import pytest
from agents.moot_court.precedent_memory import Memory, genome_vector
from agents.moot_court.red_team_agent import RedTeam
from agents.moot_court.grader import benchmark_cases, run_moot


def test_red_team_adversary():
    rng = random.Random(42)
    red = RedTeam(rng)
    params = red.sample()
    assert "layers" in params
    assert "hop_delay_s" in params
    red.record(params, evaded=True)
    assert len(red.successes) == 1
    mutated = red.sample()
    assert mutated is not None


def test_precedent_memory(tmp_path):
    mem = Memory(tmp_path / "test-memory.json")
    assert len(mem.data["genomes"]) >= 5
    vec = [0.5] * 11
    pid = mem.add_precedent("defense", vec, "Test lesson", "framing", 1, "ACC_TEST")
    assert pid.startswith("P-")
    found = mem.find_precedent(vec, "defense", min_sim=0.9)
    assert found is not None
    assert found["id"] == pid


def test_benchmark_suite():
    cases = benchmark_cases(10)
    assert len(cases) == 10
    evasion = [c for c in cases if c["meta"]["kind"] == "evasion"]
    assert len(evasion) == 5
