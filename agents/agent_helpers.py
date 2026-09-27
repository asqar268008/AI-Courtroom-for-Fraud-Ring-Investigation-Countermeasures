"""Agent Helper Utilities."""
from __future__ import annotations

import asyncio
import copy
import html
import json
import logging
import re
import time
from typing import Optional

from app.config import settings
from app.schemas import Brief, CourtVerdict, Evidence, NodeVerdict, TIER_ACTIONS
from core.context import CONTEXTS, CaseContext, ctx_for
from core.decision import counterfactual, choose_tier, action_name, collateral_blocks
from core.llm import LLMError
from security.pii_masking import Masker
from agents.investigation import network_agent as D

logger = logging.getLogger("agents")

FEATURE_WORDS = {
    "in_cycle": "in a circular fund routing cycle",
    "shared_attr": "shares infrastructure with other suspicious nodes",
    "fast_pass_through": "high velocity rapid pass-through",
    "genome_similarity": "matches topology of known fraud rings",
}

CUSTOMER = """You are drafting a short, professional notice from a bank to its customer.
Explain the action clearly, calmly, and without using technical jargon or tipping off fraud investigations."""

PROSECUTOR = """You are a senior financial intelligence prosecutor at a major bank.
Review the evidence for accounts in a suspicious money-transfer cluster.
Present a structured, factual indictment citing ONLY the provided evidence IDs."""

DEFENSE = """You are a seasoned compliance defense advocate protecting legitimate bank customers from wrongful restriction.
Present exculpatory arguments and objections citing ONLY the provided evidence IDs."""

JUDGE = """You are an impartial financial crimes judge at a major bank.
Weigh the prosecution's indictment against the defense's brief.
Check evidence citations for factual accuracy and evaluate counterfactual business impact.
Assign proportionate risk tiers from 0 (Cleared) to 4 (Hard Freeze + STR)."""


def _evidence_index(state: dict) -> dict:
    return {e.id: e for e in state.get("evidence", [])}


def _source(ctx: CaseContext, fallback: bool, judge: bool = False) -> str:
    if fallback or not ctx.use_llm:
        return "deterministic-rubric"
    model = ctx.settings.llm_judge_model if judge else ctx.settings.llm_model
    return f"LLM ({ctx.settings.llm_provider}:{model or 'default'})"


def _masker(state: dict) -> Masker:
    ring = state.get("ring", [])
    name_map = {}
    for i, a in enumerate(ring):
        name_map[a] = f"ACC_{i+1:02d}"
    for ev in state.get("evidence", []):
        for n in getattr(ev, "nodes", []):
            if n not in name_map:
                name_map[n] = f"ACC_{len(name_map)+1:02d}"
        txt = getattr(ev, "text", "")
        for dev in re.findall(r"\b(?:D-\d+|VPN-\d+|IP-[\w.-]+)\b", txt):
            if dev not in name_map:
                name_map[dev] = f"DEV_{len(name_map)+1:02d}"
    return Masker(name_map)


async def _llm_text(
    ctx: CaseContext, system: str, facts: dict, template: str, masker: Masker,
    schema: type | None = None, judge: bool = False,
) -> tuple[str, dict | None, bool]:
    if not ctx.use_llm or not getattr(ctx.llm, "enabled", False):
        return template, None, True
    try:
        masked_facts = masker.mask(json.dumps(facts, indent=2, default=str))
        user_prompt = f"Case facts:\n{masked_facts}"
        model = ctx.settings.llm_judge_model if judge else ctx.settings.llm_model
        if hasattr(ctx.llm, "json"):
            resp = await ctx.llm.json(system, user_prompt, model=model)
            raw_text = resp.get("summary", "") or resp.get("text", "") or resp.get("rationale", "") or resp.get("indictment", "")
            text = masker.unmask(raw_text) if raw_text else template
            return text, resp, False
        return template, None, True
    except Exception as exc:
        logger.warning(f"LLM fallback triggered: {exc}")
        return template, None, True
