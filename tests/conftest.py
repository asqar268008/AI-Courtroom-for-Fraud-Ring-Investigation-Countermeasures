"""Shared fixtures. Env vars are set BEFORE the application is imported so tests are fast,
offline (mock LLM) and never touch the developer's runtime/ folder."""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="fc-test-")
os.environ.update({
    "LLM_PROVIDER": "mock", "FC_STAGE_DELAY": "0", "FC_TOKEN_DELAY": "0",
    "FC_SETTLE_WINDOW": "0", "FC_RUNTIME_DIR": _TMP,
    "FC_APPROVAL_MODE": "auto", "FC_AMBIENT_TPS": "0",
})

import asyncio  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from security.audit import AuditLog  # noqa: E402
from database.graph_store import GraphStore  # noqa: E402
from agents.moot_court.precedent_memory import Memory  # noqa: E402
from core.simulator import build_demo_scenario  # noqa: E402
from core.workflow import run_case  # noqa: E402

RING = ["A", "B1", "B2", "B3", "C1", "C2", "C3", "C4", "C5", "C6"]


@pytest.fixture(scope="session")
def scenario():
    return build_demo_scenario()


@pytest.fixture(scope="session")
def store(scenario):
    return GraphStore.from_records(scenario["accounts"], scenario["transactions"], scenario["logins"])


@pytest.fixture(scope="session")
def alert_time(scenario):
    return scenario["meta"]["t0"] + 70


@pytest.fixture(scope="session")
def golden(store, alert_time, scenario):
    """Run the whole courtroom pipeline once on the golden demo case."""
    tmp = Path(_TMP)
    audit = AuditLog(tmp / "golden-audit.db")
    memory = Memory(tmp / "golden-memory.json")
    final, ctx = asyncio.run(run_case(store, memory, "A", alert_time, reason="golden test",
                                      audit=audit, ground_truth=scenario["meta"]["ground_truth"],
                                      case_id="FR-TEST-GOLD"))
    return {"state": final, "ctx": ctx, "audit": audit}
