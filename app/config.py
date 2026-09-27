"""Central configuration. Every value can be overridden with an environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("FC_DATA_DIR", ROOT / "data"))
RUNTIME_DIR = Path(os.getenv("FC_RUNTIME_DIR", ROOT / "runtime"))
FRONTEND_DIR = ROOT / "frontend"


def _load_dotenv() -> None:
    """Minimal .env loader so the project has no extra dependency."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


@dataclass
class Settings:
    llm_provider: str = os.getenv("LLM_PROVIDER", "mock").lower()
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "")
    llm_judge_model: str = os.getenv("LLM_JUDGE_MODEL", "")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "")          # empty = provider preset
    llm_timeout: float = _float("LLM_TIMEOUT", 25)
    llm_retries: int = int(os.getenv("LLM_RETRIES", "2"))
    llm_concurrency: int = int(os.getenv("LLM_CONCURRENCY", "3"))
    llm_cache: bool = os.getenv("LLM_CACHE", "1") == "1"

    stage_delay: float = _float("FC_STAGE_DELAY", 1.2)
    token_delay: float = _float("FC_TOKEN_DELAY", 0.02)
    settle_window: float = _float("FC_SETTLE_WINDOW", 2.5)
    replay_speed: float = _float("FC_REPLAY_SPEED", 40)

    confidence_threshold: float = _float("FC_CONFIDENCE_THRESHOLD", 0.85)
    # "employee": every restrictive action waits for an employee's approval (default, product mode)
    # "auto": confident, collateral-free decisions execute on their own (the 45-second half-life demo)
    approval_mode: str = os.getenv("FC_APPROVAL_MODE", "employee").lower()
    ambient_tps: float = _float("FC_AMBIENT_TPS", 4)  # background (normal) transactions per second during a demo
    accuse_threshold: float = 0.40
    max_tier: int = int(os.getenv("FC_MAX_TIER", "4"))

    # Detection parameters (max_hop_gap is also learned by the moot court)
    alert_window_s: int = 600
    fast_dwell_s: int = 60
    default_max_hop_gap_s: int = 600
    max_hop_gap_cap_s: int = 3600
    trace_depth: int = 4
    risk_model: dict | None = None   # optional fitted logistic model (scripts/validate_ibm.py --calibrate)
    inr_per_unit: float = 1.0       # data in another currency (IBM validation uses USD): convert for the policy
    unavailable_signals: tuple = ()  # risk signals the data source cannot provide (see risk.node_risk)

    moot_use_llm: bool = os.getenv("FC_MOOT_USE_LLM", "0") == "1"



settings = Settings()
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
