"""The real-LLM code path, exercised against a fake provider (no network, no key)."""
import asyncio
import copy
import re

import pytest

from app.config import settings as base
from core.llm import LLM, LLMError, Masker
from agents.moot_court.precedent_memory import Memory
from core.workflow import run_case
from fake_llm import FakeProvider


def make(provider="groq", **kw):
    s = copy.copy(base)
    s.llm_provider, s.llm_api_key, s.llm_cache, s.llm_retries = provider, "test-key", False, 2
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def run(store, alert_time, llm, tmp_path, s):
    return asyncio.run(run_case(store, Memory(tmp_path / "m.json"), "A", alert_time, llm=llm, settings=s,
                                case_id=f"FR-LLM-{id(llm) % 10000}"))


@pytest.mark.parametrize("provider,path", [("groq", "/openai/v1/chat/completions"), ("gemini", "/chat/completions"),
                                           ("anthropic", "/v1/messages")])
def test_agents_use_llm_text_with_valid_citations(store, alert_time, tmp_path, provider, path):
    fake = FakeProvider()
    s = make(provider)
    llm = LLM(s, transport=fake.transport())
    final, ctx = run(store, alert_time, llm, tmp_path, s)
    assert fake.requests and fake.requests[0]["url"].endswith(path)
    assert final["prosecution"].summary.startswith("[fake-llm]")
    assert final["verdict"].summary.startswith("[fake-llm]")
    assert not final["prosecution"].llm_fallback
    known = {e.id for e in final["evidence"]}
    assert set(final["prosecution"].cited_evidence) <= known
    # the verdict itself is still decided by code
    tiers = {v.node_id: v.tier for v in final["verdict"].node_verdicts}
    assert tiers["C3"] == 2 and tiers["A"] == 4
    sources = [e["payload"].get("source", "") for e in ctx.recording if e["type"] == "agent.message"]
    assert any(src.startswith("LLM") for src in sources)


def test_account_ids_never_leave_the_process(store, alert_time, tmp_path):
    fake = FakeProvider()
    s = make()
    run(store, alert_time, LLM(s, transport=fake.transport()), tmp_path, s)
    sent = " ".join(str(r["body"]) for r in fake.requests)
    for acc in ["B1", "B2", "B3", "C3", "C6", "D-4471"][:5]:
        assert not re.search(rf"(?<![A-Za-z0-9_-]){acc}(?![A-Za-z0-9_-])", sent), acc


def test_rate_limit_is_retried(store, alert_time, tmp_path):
    fake = FakeProvider(fail_first=2)
    s = make()
    llm = LLM(s, transport=fake.transport())
    final, _ = run(store, alert_time, llm, tmp_path, s)
    assert llm.stats["retries"] >= 2
    assert final["prosecution"].summary.startswith("[fake-llm]")


def test_invented_evidence_falls_back_to_template(store, alert_time, tmp_path):
    fake = FakeProvider(invent_ids=True)
    s = make()
    final, _ = run(store, alert_time, LLM(s, transport=fake.transport()), tmp_path, s)
    assert final["prosecution"].llm_fallback
    assert "EV-XXX-999" not in final["prosecution"].summary
    assert {v.node_id: v.tier for v in final["verdict"].node_verdicts}["C3"] == 2


def test_cache_makes_second_call_free(tmp_path, monkeypatch):
    import core.llm as L
    monkeypatch.setattr(L, "CACHE_DIR", tmp_path / "cache")
    fake = FakeProvider()
    s = make(llm_cache=True)
    llm = LLM(s, transport=fake.transport())
    a = asyncio.run(llm.json("Role: PROSECUTOR", '{"x": "EV-CYC-001"}'))
    b = asyncio.run(llm.json("Role: PROSECUTOR", '{"x": "EV-CYC-001"}'))
    assert a == b and len(fake.requests) == 1 and llm.stats["cache_hits"] == 1


def test_mock_is_disabled_and_masker_roundtrip():
    assert not LLM(make("mock")).enabled
    with pytest.raises(LLMError):
        asyncio.run(LLM(make("mock")).json("x", "y"))
    m = Masker(["A", "B1", "C3"])
    assert m.unmask(m.mask("A sent to B1 and C3")) == "A sent to B1 and C3"
