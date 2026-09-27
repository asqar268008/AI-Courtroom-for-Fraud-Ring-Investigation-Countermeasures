"""Data contracts shared by every layer: evidence, briefs, verdicts and the LangGraph state."""
from __future__ import annotations

import operator
from typing import Annotated, Literal, Optional, TypedDict

from pydantic import BaseModel, Field

EvidenceKind = Literal[
    "burst", "cycle", "shared_device", "shared_ip", "fan_out", "dwell",
    "genome_match", "account_profile", "counterfactual", "precedent", "relationship",
]
Side = Literal["prosecution", "defense", "judge"]


class Evidence(BaseModel):
    """A fact computed by deterministic code. Agents may only cite these IDs."""
    id: str
    kind: EvidenceKind
    nodes: list[str]
    text: str
    value: Optional[float] = None
    side: Side = "prosecution"
    source_tool: str


class Brief(BaseModel):
    side: Literal["prosecution", "defense"]
    summary: str
    positions: dict[str, Literal["fraud", "innocent", "unsure"]] = Field(default_factory=dict)
    cited_evidence: list[str] = Field(default_factory=list)
    objections: dict[str, list[str]] = Field(default_factory=dict)  # defense: node -> evidence ids
    llm_fallback: bool = False


class NodeVerdict(BaseModel):
    node_id: str
    role: str
    p_fraud: float
    tier: int
    action: str
    rationale: str
    cited_evidence: list[str]


class CourtVerdict(BaseModel):
    summary: str
    node_verdicts: list[NodeVerdict]
    confidence: float
    counterfactual_ok: bool
    upheld_objections: list[str] = Field(default_factory=list)
    cited_evidence: list[str] = Field(default_factory=list)
    llm_fallback: bool = False


class FraudInvestigationState(TypedDict, total=False):
    case_id: str
    mode: Literal["live", "moot"]
    seed: str
    alert_reason: str
    alert_time: float
    capital_at_risk: float
    sources: list[str]
    ring: list[str]
    roles: dict[str, str]
    depth: dict[str, int]
    parents: dict[str, list[str]]
    scanned_entities: int
    display_graph: dict
    evidence: Annotated[list[Evidence], operator.add]
    features: dict[str, dict]
    risk: dict[str, float]
    genome: dict
    prosecution: Brief
    defense: Brief
    verdict: CourtVerdict
    route: str
    actions: Annotated[list[dict], operator.add]
    sar_path: str
    grade: dict


TIER_ACTIONS = {
    0: "Observe",
    1: "Alert analyst",
    2: "Step-up challenge",
    3: "Soft freeze, outbound blocked",
    4: "Hard freeze and STR",
}


class TxnIn(BaseModel):
    txn_id: str
    timestamp: float
    from_account: str
    to_account: str
    amount: float
    rail: str = "UPI"


class InvestigateIn(BaseModel):
    seed: str
    alert_time: Optional[float] = None
    reason: str = "manual investigation"


class ReviewIn(BaseModel):
    decision: str  # "approve" or "dismiss"
    note: str = ""
    approve_tiers: Optional[list[int]] = None  # e.g. [4, 3] approves freezes but not step-ups; None = all
    exclude: list[str] = []  # accounts the employee keeps unrestricted


class OverrideIn(BaseModel):
    account: str
    decision: str  # "release" or "confirm"
    note: str = ""


class BankIn(BaseModel):
    accounts: list[str]
    case_id: str = "manual"
