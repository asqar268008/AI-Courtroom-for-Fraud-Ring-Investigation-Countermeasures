"""Persistent learned memory: fraud genomes, precedents (lessons from mistakes) and learned policy."""
from __future__ import annotations

import json
import threading

from app.config import RUNTIME_DIR, settings
from agents.investigation.graph_investigation_agent import GENOME_CENTER, seed_library
from database.vector_index import VectorIndex

PRECEDENT_CENTER = [0.3, 0.3, 0.5, 0.4, 0.5, 0.5, 0.2, 0.2, 0.3, 0.3, 0.5]


class Memory:
    def __init__(self, path=None) -> None:
        self.path = path or (RUNTIME_DIR / "memory.json")
        self._lock = threading.Lock()
        self.data: dict = {}
        self.load()

    # ---------- persistence ----------
    def load(self) -> None:
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.reset()

    def save(self) -> None:
        with self._lock:
            self.path.write_text(json.dumps(self.data, indent=1), encoding="utf-8")

    def reset(self) -> None:
        self.data = {
            "policy": {"max_hop_gap_s": settings.default_max_hop_gap_s},
            "genomes": seed_library(),
            "precedents": [],
            "next_precedent": 31,
        }
        self.save()

    # ---------- policy ----------
    @property
    def max_hop_gap_s(self) -> float:
        return float(self.data["policy"]["max_hop_gap_s"])

    def widen_hop_gap(self, needed_s: float) -> bool:
        new = min(settings.max_hop_gap_cap_s, max(self.max_hop_gap_s, needed_s * 1.1))
        if new > self.max_hop_gap_s:
            self.data["policy"]["max_hop_gap_s"] = round(new)
            return True
        return False

    # ---------- genomes ----------
    def match_genome(self, vec: list[float]) -> dict:
        idx = VectorIndex(GENOME_CENTER)
        for g in self.data["genomes"]:
            idx.add(g["id"], g["vector"])
        hits = idx.search(vec, k=1)
        if not hits:
            return {"id": None, "label": "", "similarity": 0.0}
        gid, sim = hits[0]
        g = next(x for x in self.data["genomes"] if x["id"] == gid)
        return {"id": gid, "label": g["label"], "similarity": round(max(0.0, sim), 3)}

    def add_genome(self, gid: str, label: str, vec: list[float], source: str) -> None:
        self.data["genomes"].append({"id": gid, "label": label, "vector": vec, "source": source})

    # ---------- precedents ----------
    def find_precedent(self, vec: list[float], side: str, min_sim: float = 0.93) -> dict | None:
        items = [p for p in self.data["precedents"] if p["side"] == side]
        if not items:
            return None
        idx = VectorIndex(PRECEDENT_CENTER)
        for p in items:
            idx.add(p["id"], p["vector"])
        pid, sim = idx.search(vec, k=1)[0]
        if sim < min_sim:
            return None
        p = dict(next(x for x in items if x["id"] == pid))
        p["similarity"] = round(sim, 3)
        return p

    def add_precedent(self, side: str, vec: list[float], lesson: str, case_type: str, round_no: int,
                      example: str) -> str:
        pid = f"P-{self.data['next_precedent']:03d}"
        self.data["next_precedent"] += 1
        self.data["precedents"].append({"id": pid, "side": side, "vector": vec, "lesson": lesson,
                                        "case_type": case_type, "round": round_no, "example": example})
        return pid

    def precedents(self) -> list[dict]:
        return [{k: v for k, v in p.items() if k != "vector"} for p in self.data["precedents"]]


from agents.investigation.graph_investigation_agent import genome_vector

__all__ = ["Memory", "genome_vector"]
