"""Cosine-similarity vector index. Uses FAISS when installed, otherwise numpy (same results)."""
from __future__ import annotations

import numpy as np

try:  # optional dependency
    import faiss  # type: ignore
except Exception:  # pragma: no cover
    faiss = None


class VectorIndex:
    def __init__(self, center: list[float] | None = None) -> None:
        self.center = np.array(center, dtype="float32") if center is not None else None
        self.ids: list[str] = []
        self._vecs: list[np.ndarray] = []

    def _prep(self, vec: list[float]) -> np.ndarray:
        v = np.array(vec, dtype="float32")
        if self.center is not None:
            v = v - self.center
        n = np.linalg.norm(v)
        return v / n if n > 0 else v

    def add(self, item_id: str, vec: list[float]) -> None:
        self.ids.append(item_id)
        self._vecs.append(self._prep(vec))

    def search(self, vec: list[float], k: int = 3) -> list[tuple[str, float]]:
        if not self.ids:
            return []
        q = self._prep(vec)
        mat = np.stack(self._vecs)
        if faiss is not None:
            index = faiss.IndexFlatIP(mat.shape[1])
            index.add(mat)
            scores, idx = index.search(q[None, :], min(k, len(self.ids)))
            return [(self.ids[i], float(s)) for s, i in zip(scores[0], idx[0]) if i >= 0]
        scores = mat @ q
        order = np.argsort(-scores)[:k]
        return [(self.ids[i], float(scores[i])) for i in order]
