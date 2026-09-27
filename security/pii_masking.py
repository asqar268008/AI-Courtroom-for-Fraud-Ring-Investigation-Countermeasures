"""PII Masking and anonymization engine for LLM prompts and logging."""
from __future__ import annotations

import re
from typing import Dict


class Masker:
    """Consistently pseudonymises account IDs and human names in text sent to the LLM."""

    def __init__(self, name_map: Dict[str, str] | None = None):
        self.name_map = dict(name_map or {})
        self.alias_to_real: Dict[str, str] = {v: k for k, v in self.name_map.items()}

    def mask(self, text: str) -> str:
        out = text
        for real, alias in self.name_map.items():
            out = re.sub(rf"\b{re.escape(real)}\b", alias, out)
        out = re.sub(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", "[PAN_MASKED]", out)
        out = re.sub(r"\b(?:\+91|0)?[6-9]\d{9}\b", "[PHONE_MASKED]", out)
        out = re.sub(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", "[EMAIL_MASKED]", out)
        return out

    def unmask(self, text: str) -> str:
        out = text
        for alias, real in self.alias_to_real.items():
            out = re.sub(rf"\b{re.escape(alias)}\b", real, out)
        return out


def mask_pan(pan: str) -> str:
    if len(pan) == 10:
        return pan[:2] + "XXXXX" + pan[-3:]
    return "[MASKED]"


def mask_account(acc: str) -> str:
    if len(acc) >= 4:
        return "*" * (len(acc) - 4) + acc[-4:]
    return "****"
