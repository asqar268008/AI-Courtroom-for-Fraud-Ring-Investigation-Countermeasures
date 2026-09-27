"""Behavior Investigation Agent: extracts account profiling, dwell times, and velocity features."""
from __future__ import annotations

from agents.investigation.network_agent import (
    dwell_times,
    account_features,
    fast_score,
    precedent_vector,
)

compute_dwell_times = dwell_times
extract_account_features = account_features

__all__ = [
    "dwell_times",
    "compute_dwell_times",
    "account_features",
    "extract_account_features",
    "fast_score",
    "precedent_vector",
]
