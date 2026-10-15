"""LocalMemory: the four lifecycle ops, and the ctx wrappers that log them for the UI."""
from __future__ import annotations

import asyncio

from clause.config import Settings
from clause.context import build_ctx
from clause.memory import LocalMemory


def run(coro):
    return asyncio.run(coro)


def test_remember_then_recall_finds_it():
    m = LocalMemory()
    run(m.remember("acme", "promise", "TEST-0001", "Promised to pay TEST-0001 by 2026-12-04: 'we will pay Friday'"))
    run(m.remember("acme", "email_in", "<x>", "Asked about the EICR certificate"))
    hits = run(m.recall("acme", "did Acme promise a date?"))
    assert hits and "Promised to pay TEST-0001" in hits[0].text
    assert run(m.recall("bright", "did Bright promise a date?")) == []


def test_forget_erases_the_customer():
    m = LocalMemory()
    run(m.remember("acme", "dispute", "TEST-0001", "Disputed TEST-0001: the lights failed"))
    run(m.remember("bright", "dispute", "TEST-0009", "Disputed TEST-0009"))
    run(m.forget("acme"))
    assert run(m.recall("acme", "any disputes?")) == []
    assert run(m.recall("bright", "any disputes?"))


def test_improve_adds_a_feedback_note():
    m = LocalMemory()
    run(m.improve("acme", "Approved: Acme always pays late in December; do not escalate"))
    hits = run(m.recall("acme", "December escalate"))
    assert hits and hits[0].text.startswith("[feedback human]")


def test_event_log_records_all_four_ops():
    m = LocalMemory()
    run(m.remember("acme", "note", "1", "hello"))
    run(m.recall("acme", "hello"))
    run(m.improve("acme", "good"))
    run(m.forget("acme"))
    assert {"remember", "recall", "improve", "forget"} <= {e.op for e in m.events}


def test_ctx_wrappers_log_memory_events_for_the_ui():
    ctx = build_ctx(Settings(), fake=True)
    ctx.remember_later("acme", "promise", "TEST-0001", "Promised to pay TEST-0001 by 2026-12-04")
    assert ctx.recall("acme", "promise date")
    ctx.improve_later("acme", "Correct: promise noted")
    ctx.forget("acme")
    assert ctx.recall("acme", "promise date") == []
    assert [e["op"] for e in ctx.memory_log] == ["remember", "recall", "improve", "forget", "recall"]
    assert all(e["backend"] == "local" for e in ctx.memory_log)


def test_chase_history_masks_amounts():
    from clause import inbox

    ctx = build_ctx(Settings(), fake=True)
    ctx.remember_later("acme", "promise", "TEST-0001", "Promised to pay £4,800.00 on TEST-0001 by 2026-12-04")
    history = inbox.chase_history(ctx, "acme")
    assert history and "£" not in history[0] and "[amount]" in history[0]
