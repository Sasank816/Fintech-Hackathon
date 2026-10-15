"""Shared customer memory. The contract, billing and collections agents never message each
other: what one learns about a customer, it remembers here, and the others recall it.

CogneeMemory is the real backend (one Cognee dataset per customer, all four lifecycle calls).
LocalMemory is an in-process stand-in with the same interface, used by the test suite.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger("clause.memory")


@dataclass
class MemoryHit:
    text: str
    source: str  # "cognee:graph", "cognee:chunk", "local"


@dataclass
class MemoryEvent:
    op: str  # remember | recall | improve | forget
    customer: str
    detail: str


class Memory(Protocol):
    backend: str
    events: list[MemoryEvent]

    async def remember(self, customer: str, kind: str, ref: str, text: str) -> None: ...
    async def recall(self, customer: str, question: str) -> list[MemoryHit]: ...
    async def improve(self, customer: str, feedback: str) -> None: ...
    async def forget(self, customer: str) -> None: ...


def dataset_for(customer: str) -> str:
    return "customer_" + re.sub(r"[^a-z0-9_]", "_", customer.lower())


def memory_note(kind: str, ref: str, text: str) -> str:
    return f"[{kind} {ref}] {text.strip()}"


class LocalMemory:
    backend = "local"

    def __init__(self) -> None:
        self.notes: dict[str, list[str]] = {}
        self.events: list[MemoryEvent] = []

    async def remember(self, customer: str, kind: str, ref: str, text: str) -> None:
        self.notes.setdefault(customer, []).append(memory_note(kind, ref, text))
        self.events.append(MemoryEvent("remember", customer, f"{kind} {ref}"))

    async def recall(self, customer: str, question: str) -> list[MemoryHit]:
        # crude stemming: "promises" should find "Promised", "disputes" should find "disputed"
        words = {w[:6] for w in re.findall(r"[a-z0-9£.]+", question.lower()) if len(w) > 3}
        notes = self.notes.get(customer, [])
        scored = []
        for i, note in enumerate(notes):
            score = sum(1 for w in words if w in note.lower())
            if score:
                scored.append((score, i, note))
        scored.sort(key=lambda t: (-t[0], -t[1]))  # best match first, newest first on ties
        self.events.append(MemoryEvent("recall", customer, question))
        if not words:  # no usable keywords ("?"): show the latest notes
            return [MemoryHit(n, "local") for n in notes[-5:][::-1]]
        return [MemoryHit(n, "local") for _, _, n in scored[:5]]

    async def improve(self, customer: str, feedback: str) -> None:
        await self.remember(customer, "feedback", "human", feedback)
        self.events.append(MemoryEvent("improve", customer, feedback[:80]))

    async def forget(self, customer: str) -> None:
        self.notes.pop(customer, None)
        self.events.append(MemoryEvent("forget", customer, "dataset erased"))


def configure_cognee_env(data_dir: Path, anthropic_key: str, model: str) -> None:
    """Cognee reads its configuration from the environment at import time."""
    root = data_dir / "cognee"
    defaults = {
        "LLM_PROVIDER": "anthropic",
        "LLM_MODEL": model,
        "LLM_API_KEY": anthropic_key,
        "EMBEDDING_PROVIDER": "fastembed",
        "EMBEDDING_MODEL": "sentence-transformers/all-MiniLM-L6-v2",
        "EMBEDDING_DIMENSIONS": "384",
        "SYSTEM_ROOT_DIRECTORY": str(root / "system"),
        "DATA_ROOT_DIRECTORY": str(root / "data"),
        "ENABLE_BACKEND_ACCESS_CONTROL": "false",
        "TELEMETRY_DISABLED": "true",
        "FASTEMBED_CACHE_PATH": str(data_dir / "fastembed_cache"),
    }
    for key, value in defaults.items():
        if value and not os.environ.get(key):
            os.environ[key] = value


class CogneeMemory:
    backend = "cognee"

    def __init__(self) -> None:
        import cognee  # imported late: env must be configured first

        self._cognee = cognee
        self.events: list[MemoryEvent] = []
        self._lock = asyncio.Lock()  # cognee's local graph store prefers one writer

    async def remember(self, customer: str, kind: str, ref: str, text: str) -> None:
        note = memory_note(kind, ref, text)
        async with self._lock:
            # self-improvement runs explicitly in improve() (on human approvals), not on every write
            await self._cognee.remember(note, dataset_name=dataset_for(customer), node_set=[customer, kind],
                                        self_improvement=False)
        self.events.append(MemoryEvent("remember", customer, f"{kind} {ref}"))

    async def recall(self, customer: str, question: str) -> list[MemoryHit]:
        from cognee import SearchType

        self.events.append(MemoryEvent("recall", customer, question))
        try:
            results = await self._cognee.recall(
                question, query_type=SearchType.GRAPH_COMPLETION, datasets=[dataset_for(customer)], top_k=8)
        except Exception as exc:  # an empty dataset raises; treat as no memory
            log.warning("recall failed for %s: %s", customer, exc)
            return []
        hits = []
        for r in results:
            text = getattr(r, "text", None) or getattr(r, "content", None) or str(r)
            if text:
                hits.append(MemoryHit(str(text), f"cognee:{getattr(r, 'source', 'graph')}"))
        return hits

    async def improve(self, customer: str, feedback: str) -> None:
        await self.remember(customer, "feedback", "human", feedback)
        async with self._lock:
            try:
                await self._cognee.improve(dataset=dataset_for(customer))
            except Exception as exc:
                log.warning("improve failed for %s: %s", customer, exc)
        self.events.append(MemoryEvent("improve", customer, feedback[:80]))

    async def forget(self, customer: str) -> None:
        async with self._lock:
            await self._cognee.forget(dataset=dataset_for(customer))
        self.events.append(MemoryEvent("forget", customer, "dataset erased"))
