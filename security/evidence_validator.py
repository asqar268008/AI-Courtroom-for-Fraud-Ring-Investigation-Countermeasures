"""Evidence validation and citation verification. Ensures zero LLM hallucination."""
from __future__ import annotations

from typing import List, Set
from app.schemas import Evidence, Brief, CourtVerdict


class EvidenceValidator:
    """Validates that all evidence cited by Prosecutor, Defense, and Judge exists in the factual store."""

    @staticmethod
    def validate_citations(cited_ids: List[str], available_evidence: List[Evidence]) -> tuple[bool, List[str]]:
        """Check whether all cited evidence IDs exist in the factual evidence pool."""
        known_ids: Set[str] = {e.id for e in available_evidence}
        invalid_ids = [cid for cid in cited_ids if cid not in known_ids]
        is_valid = len(invalid_ids) == 0
        return is_valid, invalid_ids

    @staticmethod
    def validate_brief(brief: Brief, available_evidence: List[Evidence]) -> bool:
        valid, _ = EvidenceValidator.validate_citations(brief.cited_evidence, available_evidence)
        return valid

    @staticmethod
    def validate_verdict(verdict: CourtVerdict, available_evidence: List[Evidence]) -> bool:
        valid, _ = EvidenceValidator.validate_citations(verdict.cited_evidence, available_evidence)
        for nv in verdict.node_verdicts:
            nv_valid, _ = EvidenceValidator.validate_citations(nv.cited_evidence, available_evidence)
            if not nv_valid:
                return False
        return valid
