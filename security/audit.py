"""Append-only, hash-chained audit log in SQLite. Tampering with any row breaks the chain."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from threading import Lock

from app.config import RUNTIME_DIR


class AuditLog:
    def __init__(self, path=None) -> None:
        self.path = str(path or (RUNTIME_DIR / "audit.db"))
        self._lock = Lock()
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, at REAL, case_id TEXT, "
                "kind TEXT, payload TEXT, prev_hash TEXT, hash TEXT)"
            )

    def append(self, case_id: str, kind: str, payload: dict) -> str:
        with self._lock, sqlite3.connect(self.path) as db:
            row = db.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            prev = row[0] if row else "GENESIS"
            body = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
            digest = hashlib.sha256(f"{prev}|{case_id}|{kind}|{body}".encode()).hexdigest()
            db.execute(
                "INSERT INTO audit (at, case_id, kind, payload, prev_hash, hash) VALUES (?,?,?,?,?,?)",
                (time.time(), case_id, kind, body, prev, digest),
            )
            return digest

    def log(self, kind: str, payload: dict, case_id: str = "SYSTEM") -> str:
        return self.append(case_id, kind, payload)

    def verify(self) -> bool:
        with sqlite3.connect(self.path) as db:
            prev = "GENESIS"
            for case_id, kind, body, prev_hash, digest in db.execute(
                "SELECT case_id, kind, payload, prev_hash, hash FROM audit ORDER BY id"
            ):
                expected = hashlib.sha256(f"{prev}|{case_id}|{kind}|{body}".encode()).hexdigest()
                if prev_hash != prev or digest != expected:
                    return False
                prev = digest
        return True

    def verify_chain(self) -> tuple[bool, int | None]:
        with sqlite3.connect(self.path) as db:
            prev = "GENESIS"
            for row_id, case_id, kind, body, prev_hash, digest in db.execute(
                "SELECT id, case_id, kind, payload, prev_hash, hash FROM audit ORDER BY id"
            ):
                expected = hashlib.sha256(f"{prev}|{case_id}|{kind}|{body}".encode()).hexdigest()
                if prev_hash != prev or digest != expected:
                    return False, row_id
                prev = digest
        return True, None

    def read_all(self) -> list[dict]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT id, at, case_id, kind, payload, prev_hash, hash FROM audit ORDER BY id")
            return [
                {
                    "id": r[0],
                    "at": r[1],
                    "case_id": r[2],
                    "kind": r[3],
                    "payload": json.loads(r[4]),
                    "prev_hash": r[5],
                    "hash": r[6],
                }
                for r in rows
            ]

    def for_case(self, case_id: str) -> list[dict]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT at, kind, payload, hash FROM audit WHERE case_id=? ORDER BY id", (case_id,))
            return [{"at": a, "kind": k, "payload": json.loads(p), "hash": h} for a, k, p, h in rows]
