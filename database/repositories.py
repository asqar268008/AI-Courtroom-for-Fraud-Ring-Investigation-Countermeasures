"""Data access and persistence repositories for Graph, Cases, and Precedents."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import RUNTIME_DIR, settings
from database.graph_store import GraphStore
from database.vector_index import VectorIndex


class CaseRepository:
    """Repository for persisting and retrieving case dossiers and state records."""

    def __init__(self, storage_dir: Optional[Path] = None):
        self.storage_dir = storage_dir or (RUNTIME_DIR / "cases")
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def save_case(self, case_id: str, case_data: Dict[str, Any]) -> Path:
        path = self.storage_dir / f"{case_id}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(case_data, f, indent=2, default=str)
        return path

    def get_case(self, case_id: str) -> Optional[Dict[str, Any]]:
        path = self.storage_dir / f"{case_id}.json"
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def list_cases(self) -> List[Dict[str, Any]]:
        cases = []
        for p in sorted(self.storage_dir.glob("*.json"), reverse=True):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    cases.append(json.load(f))
            except Exception:
                continue
        return cases


class PrecedentRepository:
    """Repository for managing moot court precedents and vector similarity indexing."""

    def __init__(self, memory_file: Optional[Path] = None):
        self.memory_file = memory_file or (RUNTIME_DIR / "precedents.json")
        self.vector_index = VectorIndex()
        self._load()

    def _load(self) -> None:
        if self.memory_file.exists():
            try:
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data.get("precedents", []):
                        if "embedding" in item:
                            self.vector_index.add(item["case_id"], item["embedding"], item)
            except Exception:
                pass

    def add_precedent(self, precedent_id: str, embedding: List[float], meta: Dict[str, Any]) -> None:
        self.vector_index.add(precedent_id, embedding, meta)

    def find_similar(self, embedding: List[float], k: int = 3) -> List[Dict[str, Any]]:
        results = self.vector_index.search(embedding, k=k)
        return [r[1] for r in results]
