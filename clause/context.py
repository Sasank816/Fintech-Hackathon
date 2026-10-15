"""The one object every module receives. It owns the store, the event bus, memory, Stripe, the
LLM and the mailer, and it is the only way modules talk to each other at runtime:

- `emit` pushes a UI event (thread-safe; module code runs in FastAPI's threadpool).
- `remember_later` / `recall` / `improve_later` / `forget` wrap the async memory API for sync code.
- `record_decision` is the audit ledger: every decision is stored, shown and remembered.
- `register_action` / `run_action` let an approval execute a proposal from any module (CC-8).

Each feature module may define `register(ctx)` (called by `build_ctx`) and `startup(ctx)`
(called once by the app after the event loop exists).
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from dataclasses import dataclass, field
from datetime import date
from types import ModuleType
from typing import Any, Callable

from clause.config import Settings
from clause.memory import LocalMemory, Memory, MemoryHit
from clause.store import Bus, Store, new_id, utcnow

log = logging.getLogger("clause.context")

FEATURE_MODULES = ("clause.billing", "clause.collections", "clause.mail", "clause.inbox", "clause.desk")


class StubMailer:
    """Stand-in until clause.mail lands: records nothing, sends nothing."""

    mode = "outbox"

    def send(self, to: str, subject: str, body: str, attachments: list[tuple[str, bytes, str]] = ()) -> str:
        return f"<{new_id('stub')}@clause.local>"

    def fetch_unseen(self) -> list[dict[str, Any]]:
        return []


@dataclass
class Ctx:
    settings: Settings
    store: Store
    bus: Bus
    memory: Memory
    gateway: Any
    llm: Any
    mailer: Any
    loop: asyncio.AbstractEventLoop | None = None
    actions: dict[str, Callable[..., dict]] = field(default_factory=dict)
    memory_log: list[dict[str, Any]] = field(default_factory=list)
    modules: dict[str, ModuleType] = field(default_factory=dict)

    # ---- events -------------------------------------------------------------------------
    def emit(self, event: str, data: dict[str, Any]) -> None:
        loop = self.loop
        if loop is not None and loop.is_running():
            try:
                same_thread = asyncio.get_running_loop() is loop
            except RuntimeError:
                same_thread = False
            if same_thread:
                self.bus.publish(event, data)
            else:
                loop.call_soon_threadsafe(self.bus.publish, event, data)
        else:
            self.bus.publish(event, data)

    # ---- memory -------------------------------------------------------------------------
    def _memory_event(self, op: str, customer: str, detail: str) -> None:
        ev = {"op": op, "customer": customer, "detail": detail[:200], "backend": self.memory.backend, "at": utcnow()}
        self.memory_log.append(ev)
        del self.memory_log[:-300]
        self.emit("memory", ev)

    def _run(self, coro: Any, wait: bool, timeout: float = 25) -> Any:
        loop = self.loop
        if loop is None or not loop.is_running():
            return asyncio.run(coro)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            if wait:
                coro.close()
                raise RuntimeError("blocking memory call from the event loop thread; call it from a sync route")
            return loop.create_task(coro)
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout) if wait else fut

    def remember_later(self, customer: str, kind: str, ref: str, text: str) -> None:
        async def go() -> None:
            try:
                await self.memory.remember(customer, kind, ref, text)
                self._memory_event("remember", customer, f"{kind} {ref}: {text}")
            except Exception as exc:  # memory must never break billing
                log.warning("remember failed for %s: %s", customer, exc)

        self._run(go(), wait=False)

    def recall(self, customer: str, question: str, timeout: float = 25) -> list[MemoryHit]:
        async def go() -> list[MemoryHit]:
            try:
                return await self.memory.recall(customer, question)
            except Exception as exc:
                log.warning("recall failed for %s: %s", customer, exc)
                return []

        try:
            hits = self._run(go(), wait=True, timeout=timeout)
        except Exception as exc:
            log.warning("recall timed out for %s: %s", customer, exc)
            hits = []
        self._memory_event("recall", customer, f"{question} → {len(hits)} hits")
        return hits

    def improve_later(self, customer: str, feedback: str) -> None:
        async def go() -> None:
            try:
                await self.memory.improve(customer, feedback)
                self._memory_event("improve", customer, feedback)
            except Exception as exc:
                log.warning("improve failed for %s: %s", customer, exc)

        self._run(go(), wait=False)

    def forget(self, customer: str) -> None:
        self._run(self.memory.forget(customer), wait=True, timeout=60)
        self._memory_event("forget", customer, "all memory of this customer erased")

    # ---- time ---------------------------------------------------------------------------
    def clock_date(self) -> date:
        value = self.store.meta("clock_date")
        return date.fromisoformat(value) if value else date.today()

    # ---- approvals ----------------------------------------------------------------------
    def register_action(self, name: str, fn: Callable[..., dict]) -> None:
        self.actions[name] = fn

    def run_action(self, name: str, args: dict[str, Any]) -> dict:
        if name not in self.actions:
            raise KeyError(f"no action registered as {name!r}; registered: {sorted(self.actions)}")
        return self.actions[name](self, **args)

    # ---- audit ledger -------------------------------------------------------------------
    def record_decision(self, *, actor: str, action: str, customer: str, outcome: str,
                        clauses: list[str], reasons: list[str], summary: str,
                        invoice: str | None = None, evidence: list[dict] = (),
                        calc: dict | None = None, stripe_ids: list[str] = (),
                        proposal: dict | None = None, status: str | None = None) -> dict:
        from clause.policy import NEEDS_APPROVAL, POLICY_VERSION

        decision_id = new_id("dec")
        doc = self.store.put("decision", decision_id, {
            "at": utcnow(), "clock_date": self.clock_date().isoformat(), "actor": actor, "action": action,
            "customer": customer, "outcome": outcome, "clauses": list(clauses), "reasons": list(reasons),
            "summary": summary, "invoice": invoice, "evidence": list(evidence), "calc": calc or {},
            "stripe_ids": list(stripe_ids), "proposal": proposal, "policy_version": POLICY_VERSION,
            "status": status or ("pending_approval" if outcome == NEEDS_APPROVAL else "executed"),
        })
        self.emit("decision", doc)
        self.remember_later(customer, "decision", decision_id, decision_memory_text(doc))
        return doc

    def update_decision(self, decision_id: str, **fields: Any) -> dict:
        doc = self.store.update("decision", decision_id, **fields)
        self.emit("decision", doc)
        return doc


def decision_memory_text(d: dict[str, Any]) -> str:
    parts = [f"On {d['clock_date']} the {d['actor']} decided: {d['summary']}",
             f"Outcome {d['outcome']} under {', '.join(d['clauses'])}."]
    if d["reasons"]:
        parts.append("Because: " + "; ".join(d["reasons"]) + ".")
    for ev in d.get("evidence") or []:
        if ev.get("quote"):
            parts.append(f"Evidence ({ev.get('type')} {ev.get('ref', '')}): \"{ev['quote']}\".")
    if d.get("stripe_ids"):
        parts.append("Stripe: " + ", ".join(d["stripe_ids"]) + ".")
    return " ".join(parts)


def _load_modules(ctx: Ctx) -> None:
    for name in FEATURE_MODULES:
        try:
            mod = importlib.import_module(name)
        except ImportError as exc:
            if exc.name == name:
                log.warning("%s not available yet", name)
                continue
            raise
        ctx.modules[name] = mod
        if hasattr(mod, "register"):
            mod.register(ctx)


def build_ctx(settings: Settings, *, fake: bool = False) -> Ctx:
    """Real services when keys are present; fakes for tests (fake=True) or missing keys."""
    from clause.llm import ClaudeLLM, FakeLLM
    from clause.stripe_gateway import FakeGateway, StripeGateway

    store = Store(":memory:" if fake else settings.db_path)

    if not fake and settings.stripe_secret_key:
        gateway: Any = StripeGateway(settings.stripe_secret_key)
    else:
        if not fake:
            log.warning("STRIPE_SECRET_KEY missing: using FakeGateway")
        gateway = FakeGateway()

    if not fake and settings.anthropic_api_key:
        llm: Any = ClaudeLLM(settings.anthropic_api_key, settings.model)
    else:
        if not fake:
            log.warning("ANTHROPIC_API_KEY missing: using FakeLLM")
        llm = FakeLLM()

    memory: Memory = LocalMemory()
    if not fake and settings.memory_backend == "cognee":
        try:
            from clause.memory import CogneeMemory, configure_cognee_env

            configure_cognee_env(settings.data_dir, settings.anthropic_api_key, f"anthropic/{settings.model}")
            memory = CogneeMemory()
        except Exception as exc:
            log.warning("Cognee unavailable (%s): using LocalMemory", exc)

    mailer: Any = StubMailer()
    try:
        mail = importlib.import_module("clause.mail")
        gmail_ready = bool(settings.gmail_address and settings.gmail_app_password)
        if not fake and settings.mail_mode == "gmail" and not gmail_ready:
            log.warning("MAIL_MODE=gmail but GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing: using the outbox")
        if not fake and settings.mail_mode == "gmail" and gmail_ready and hasattr(mail, "GmailMailer"):
            mailer = mail.GmailMailer(settings.gmail_address, settings.gmail_app_password)
        elif hasattr(mail, "OutboxMailer"):
            mailer = mail.OutboxMailer()
    except ImportError as exc:
        if exc.name != "clause.mail":
            raise

    ctx = Ctx(settings=settings, store=store, bus=Bus(), memory=memory, gateway=gateway, llm=llm, mailer=mailer)
    _load_modules(ctx)
    return ctx
