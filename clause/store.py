"""SQLite document store for customers, contracts, invoices, emails and the decision ledger,
plus an in-process event bus that feeds the live UI."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KINDS = ("customer", "contract", "invoice", "email", "decision", "outbox", "meta")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS docs (kind TEXT NOT NULL, id TEXT NOT NULL, seq INTEGER, "
            "created TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (kind, id))"
        )
        self._db.commit()

    def put(self, kind: str, doc_id: str, data: dict[str, Any]) -> dict[str, Any]:
        assert kind in KINDS, kind
        with self._lock:
            row = self._db.execute("SELECT seq, created FROM docs WHERE kind=? AND id=?", (kind, doc_id)).fetchone()
            if row:
                seq, created = row
            else:
                seq = (self._db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM docs").fetchone()[0])
                created = utcnow()
            doc = {**data, "id": doc_id}
            self._db.execute(
                "INSERT OR REPLACE INTO docs (kind, id, seq, created, data) VALUES (?, ?, ?, ?, ?)",
                (kind, doc_id, seq, created, json.dumps(doc, default=str)),
            )
            self._db.commit()
        return doc

    def get(self, kind: str, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM docs WHERE kind=? AND id=?", (kind, doc_id)).fetchone()
        return json.loads(row[0]) if row else None

    def update(self, kind: str, doc_id: str, **fields: Any) -> dict[str, Any]:
        doc = self.get(kind, doc_id)
        if doc is None:
            raise KeyError(f"{kind} {doc_id} not found")
        doc.update(fields)
        return self.put(kind, doc_id, doc)

    def all(self, kind: str, **match: Any) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM docs WHERE kind=? ORDER BY seq", (kind,)).fetchall()
        docs = [json.loads(r[0]) for r in rows]
        return [d for d in docs if all(d.get(k) == v for k, v in match.items())]

    def meta(self, key: str, default: Any = None) -> Any:
        doc = self.get("meta", key)
        return doc["value"] if doc else default

    def set_meta(self, key: str, value: Any) -> None:
        self.put("meta", key, {"value": value})

    def reset(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM docs")
            self._db.commit()


class Bus:
    """Fan-out of UI events to every connected SSE client."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait({"event": event, "data": data})
            except asyncio.QueueFull:
                pass
