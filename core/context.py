"""Per-case runtime context.

Heavy or non-serialisable objects (the graph, memory, event bus, bank) live here instead of in the
LangGraph state, so the state stays small and checkpointable. Nodes look the context up by case_id.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from app.schemas import Evidence
from core.events import save_recording

if TYPE_CHECKING:
    from security.audit import AuditLog
    from services.bank_service import MockBank, BankService
    from app.config import Settings
    from core.events import EventBus
    from database.graph_store import GraphStore
    from core.llm import LLM
    from agents.moot_court.precedent_memory import Memory

KIND_CODES = {
    "burst": "BUR", "cycle": "CYC", "shared_device": "DEV", "shared_ip": "IP", "fan_out": "FAN",
    "dwell": "DWL", "genome_match": "GEN", "account_profile": "PRF", "counterfactual": "CF", "precedent": "PRC", "relationship": "REL",
}


@dataclass
class CaseContext:
    case_id: str
    store: Any
    memory: Any
    settings: Any
    llm: Any
    mode: str = "live"
    bus: Optional[Any] = None
    bank: Optional[Any] = None
    audit: Optional[Any] = None
    ground_truth: dict = field(default_factory=dict)
    use_llm: bool = True
    cache: dict[str, Any] = field(default_factory=dict)
    evidence_counts: dict[str, int] = field(default_factory=dict)
    recorded_events: list[dict] = field(default_factory=list)
    t0_wall: float = field(default_factory=time.time)

    @property
    def recording(self) -> list[dict]:
        return self.recorded_events

    def elapsed(self) -> float:
        return round(time.time() - self.t0_wall, 3)

    def next_evidence_id(self, kind: str) -> str:
        self.evidence_counts[kind] = self.evidence_counts.get(kind, 0) + 1
        code = KIND_CODES.get(kind, "EV")
        return f"{code}-{self.evidence_counts[kind]:02d}"

    def new_evidence(self, kind: str, nodes: list[str], text: str, value: float | None = None,
                     side: str = "prosecution", tool: str = "") -> Evidence:
        code = self.next_evidence_id(kind)
        return Evidence(id=code, kind=kind, nodes=nodes, text=text,
                        value=value, side=side, source_tool=tool)

    async def emit(self, event_type: str, payload: dict) -> None:
        event = {
            "case_id": self.case_id,
            "type": event_type,
            "t": self.elapsed(),
            "payload": payload,
        }
        self.recorded_events.append(event)
        if self.audit:
            self.audit.append(self.case_id, event_type, payload)
        if self.bus is not None:
            await self.bus.publish(event)
        if event_type == "case.closed":
            save_recording(self.case_id, self.recorded_events)

    async def pause(self) -> None:
        delay = self.settings.stage_delay if self.mode == "live" else 0.0
        if delay > 0:
            await asyncio.sleep(delay)

    async def say(self, agent: str, text: str, stream: bool = True, source: str = "code") -> None:
        """Stream an agent's text word by word, then send the complete message."""
        await self.emit("agent.started", {"agent": agent})
        if stream and self.settings.token_delay > 0:
            words = text.split(" ")
            for i in range(0, len(words), 2):
                await self.emit("agent.token", {"agent": agent, "text": " ".join(words[i:i + 2]) + " "})
                await asyncio.sleep(self.settings.token_delay)
        await self.emit("agent.message", {"agent": agent, "text": text, "source": source})

    async def tool(self, name: str, args: dict, result: str) -> None:
        """Make the agent's work visible: which tool it called, with what, and what came back."""
        await self.emit("agent.tool", {"tool": name, "args": args, "result": result,
                                       "clock": time.strftime("%H:%M:%S")})

    def log(self, kind: str, payload: dict) -> str:
        if self.audit:
            return self.audit.append(self.case_id, kind, payload)
        return ""

    def finish_recording(self) -> None:
        if self.recorded_events:
            save_recording(self.case_id, self.recorded_events)


CONTEXTS: dict[str, CaseContext] = {}


def ctx_for(state: dict) -> CaseContext:
    cid = state.get("case_id")
    if cid and cid in CONTEXTS:
        return CONTEXTS[cid]
    try:
        from app.dependencies import rt
        if cid and cid in rt.finals:
            return rt.finals[cid][1]
    except Exception:
        pass
    raise KeyError(f"CaseContext for {cid!r} not found. Available: {list(CONTEXTS.keys())}")
