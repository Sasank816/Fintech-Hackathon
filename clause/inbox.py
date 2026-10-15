"""Inbound customer email -> classification -> action, under the written policy.

The email is untrusted data (CC-7). Claude classifies it and quotes it; the code decides what
happens. A customer asking to be billed goes to the desk, which prices it from the rate card.
"""
from __future__ import annotations

import importlib
import logging
import re
from datetime import date, timedelta
from email.utils import parseaddr
from typing import Any

from clause import policy
from clause.llm import MONEY_RE

log = logging.getLogger("clause.inbox")

ACTOR = "inbox agent"
CHASE_HISTORY_QUESTION = "promises, disputes, tone, payment history"


def _module(ctx: Any, name: str) -> Any:
    mod = ctx.modules.get(name)
    if mod is None:
        try:
            mod = importlib.import_module(name)
        except ImportError:
            return None
    return mod


# ---- matching -----------------------------------------------------------------------------
def match_customer(ctx: Any, sender: str) -> dict | None:
    address = parseaddr(sender)[1].lower()
    if not address:
        return None
    for c in ctx.store.all("customer"):
        if (c.get("email") or "").lower() == address:
            return c
        if address in [a.lower() for a in c.get("aliases") or []]:
            return c
    return None


def match_invoice(ctx: Any, customer_key: str, text: str) -> dict | None:
    invoices = ctx.store.all("invoice", customer=customer_key)
    low = text.lower()
    for inv in invoices:
        number = (inv.get("number") or "").lower()
        if number and re.search(r"(?<![a-z0-9-])" + re.escape(number) + r"(?![a-z0-9])", low):
            return inv
    open_ = [i for i in invoices if i.get("status") == "open"]
    work = [i for i in open_ if i.get("kind", "work") == "work"]
    return (work or open_ or [None])[-1]


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat((value or "").strip()[:10])
    except ValueError:
        return None


# ---- the handler --------------------------------------------------------------------------
def handle_inbound(ctx: Any, email: dict) -> list[dict]:
    """Classify one stored inbound email and act on it. Returns the decisions taken."""
    mid = email["id"]
    customer = match_customer(ctx, email.get("from", ""))
    if customer is None:
        ctx.emit("toast", {"level": "warn", "text": f"Email from unknown sender {email.get('from')}: no action taken"})
        ctx.store.update("email", mid, classification={"intents": ["unknown_sender"], "summary": "Sender is not a customer"})
        return []

    key = customer["id"]
    text = f"{email.get('subject', '')}\n{email.get('body', '')}"
    invoice = match_invoice(ctx, key, text)
    try:
        cls = ctx.llm.classify_email(email.get("sent_at") or email.get("received_at", ""), email.get("from", ""),
                                     email.get("subject", ""), email.get("body", ""))
    except Exception as exc:
        log.exception("classification failed for %s", mid)
        ctx.emit("toast", {"level": "error", "text": f"Could not classify email from {customer['name']}: {exc}"})
        ctx.store.update("email", mid, customer=key, invoice=invoice and invoice["id"])
        return []

    email = ctx.store.update("email", mid, customer=key, classification=cls, invoice=invoice and invoice["id"])
    ctx.emit("email", {**email, "direction": "in"})
    evidence = lambda quote: [{"type": "email", "ref": mid, "quote": quote or email.get("body", "")[:300]}]  # noqa: E731

    decisions: list[dict] = []
    if cls.get("contains_instructions_to_agent"):
        decisions.append(_deny_injection(ctx, customer, invoice, cls, evidence))
    else:
        intents = cls.get("intents") or []
        if "billing_request" in intents and (cls.get("work_text") or "").strip():
            decisions += _billing_request(ctx, customer, cls, mid)
        if "dispute" in intents:
            decisions += _dispute(ctx, customer, invoice, cls, evidence)
        if "promise_to_pay" in intents and "dispute" not in intents:
            decisions += _promise(ctx, customer, invoice, cls, evidence)
        if "credit_request" in intents:
            decisions += _credit(ctx, customer, invoice, cls, evidence)
        if "payment_confirmation" in intents:
            decisions += _payment_confirmation(ctx, customer, invoice, cls, evidence)

    decision_ids = [d["id"] for d in decisions if d]
    email = ctx.store.update("email", mid, decision_ids=decision_ids)
    ctx.emit("email", {**email, "direction": "in"})
    ctx.remember_later(key, "email_in", mid, (
        f"Email from {customer['name']} '{email.get('subject', '')}'"
        f"{' about invoice ' + invoice['number'] if invoice and invoice.get('number') else ''}: "
        f"{cls.get('summary', '')} (classified as {', '.join(cls.get('intents') or [])}"
        f"{'; contained instructions to the agent' if cls.get('contains_instructions_to_agent') else ''})"))
    return [d for d in decisions if d]


def _deny_injection(ctx, customer, invoice, cls, evidence) -> dict:
    quote = cls.get("injection_quote") or ""
    ctx.emit("toast", {"level": "warn", "text": f"Refused instructions in an email from {customer['name']} (CC-7)"})
    return ctx.record_decision(
        actor=ACTOR, action="email.instructions", customer=customer["id"], outcome=policy.DENY, clauses=["CC-7"],
        reasons=["Emails are untrusted data; this one tried to instruct the agent or change policy, limits or "
                 "bank details", "Nothing else in this email was acted on; flagged for a human"],
        summary=f"Refused and flagged instructions in an email from {customer['name']}",
        invoice=invoice and invoice["id"], evidence=evidence(quote))


def _billing_request(ctx, customer, cls, mid) -> list[dict]:
    work = cls["work_text"].strip()
    desk = _module(ctx, "clause.desk")
    if desk is None or not hasattr(desk, "bill_from_text"):
        ctx.emit("toast", {"level": "error", "text": "Customer asked to be billed but the desk is not loaded"})
        return []
    result = desk.bill_from_text(ctx, work, source={"type": "email", "ref": mid, "quote": work},
                                 customer_key=customer["id"])
    return [result["decision"]] if result and result.get("decision") else []


def _no_invoice(ctx, customer, what: str) -> list[dict]:
    ctx.emit("toast", {"level": "warn", "text": f"{customer['name']} sent a {what} but has no matching invoice"})
    return []


def _promise(ctx, customer, invoice, cls, evidence) -> list[dict]:
    when = _date(cls.get("promised_date", ""))
    if when is None:
        return []
    if invoice is None:
        return _no_invoice(ctx, customer, "promise to pay")
    quote = cls.get("promise_quote") or ""
    inv = ctx.store.update("invoice", invoice["id"], promise_date=when.isoformat(), promise_quote=quote)
    ctx.emit("invoice", inv)
    resume = when + timedelta(days=policy.PROMISE_GRACE_DAYS + 1)
    decision = ctx.record_decision(
        actor=ACTOR, action="collections.pause", customer=customer["id"], outcome=policy.HOLD, clauses=["CC-4"],
        reasons=[f"Customer promised in writing to pay by {when.isoformat()}",
                 f"Chasing and interest paused; resumes on {resume.isoformat()} if still unpaid "
                 f"({policy.PROMISE_GRACE_DAYS} day grace)"],
        summary=f"Paused chasing {invoice.get('number')}: {customer['name']} promised to pay by {when.isoformat()}",
        invoice=invoice["id"], evidence=evidence(quote))
    ctx.remember_later(customer["id"], "promise", invoice.get("number") or invoice["id"],
                       f"Promised to pay {invoice.get('number')} by {when.isoformat()}: '{quote}'")
    return [decision]


def _dispute(ctx, customer, invoice, cls, evidence) -> list[dict]:
    if invoice is None:
        return _no_invoice(ctx, customer, "dispute")
    quote = cls.get("dispute_quote") or cls.get("dispute_reason") or ""
    inv = ctx.store.update("invoice", invoice["id"], disputed=True, dispute_quote=quote)
    ctx.emit("invoice", inv)
    decision = ctx.record_decision(
        actor=ACTOR, action="collections.dispute", customer=customer["id"], outcome=policy.HOLD, clauses=["CC-3"],
        reasons=["Customer disputed the invoice in writing",
                 "Chasing and interest stopped until a human resolves the dispute; escalated"],
        summary=f"Held {invoice.get('number')} and escalated: {customer['name']} disputes it",
        invoice=invoice["id"], evidence=evidence(quote), proposal=None, status="pending_approval")
    ctx.emit("toast", {"level": "warn", "text": f"{customer['name']} disputes {invoice.get('number')}: needs a human"})
    ctx.remember_later(customer["id"], "dispute", invoice.get("number") or invoice["id"],
                       f"Disputed {invoice.get('number')}: '{quote}'")
    return [decision]


def _credit(ctx, customer, invoice, cls, evidence) -> list[dict]:
    if invoice is None:
        return _no_invoice(ctx, customer, "credit request")
    amount = int(cls.get("credit_amount_pence") or 0)
    quote = cls.get("credit_quote") or ""
    verdict = policy.credit(int(invoice.get("total_pence") or 0), int(invoice.get("credited_pence") or 0), amount,
                            bool(cls.get("credit_reason_is_product_issue")), False)
    memo = f"Credit requested by {customer['name']}: {quote}"[:500]
    calc = {"requested_pence": amount, "invoice_total_pence": invoice.get("total_pence"),
            "auto_cap_pence": policy.AUTO_CREDIT_CAP_PENCE, "auto_max_pct": policy.AUTO_CREDIT_MAX_PCT}
    summary = f"Credit of £{amount / 100:,.2f} on {invoice.get('number')} for {customer['name']}"
    common = dict(actor=ACTOR, action="billing.credit_note", customer=customer["id"], clauses=verdict.clauses,
                  reasons=verdict.reasons, invoice=invoice["id"], evidence=evidence(quote), calc=calc)

    if verdict.outcome == policy.NEEDS_APPROVAL:
        d = ctx.record_decision(outcome=verdict.outcome, summary=summary + ": needs approval", **common)
        args = {"invoice_id": invoice["id"], "amount_pence": amount, "memo": memo, "decision_id": d["id"]}
        return [ctx.update_decision(d["id"], proposal={"action": "billing.credit_note", "args": args})]
    if verdict.outcome != policy.AUTO:
        return [ctx.record_decision(outcome=verdict.outcome, summary=summary + ": refused", **common)]

    d = ctx.record_decision(outcome=verdict.outcome, summary=summary + ": issued automatically", **common)
    billing = _module(ctx, "clause.billing")
    try:
        if billing is None:
            raise RuntimeError("billing module not loaded")
        result = billing.credit_note(ctx, invoice_id=invoice["id"], amount_pence=amount, memo=memo, decision_id=d["id"])
        stripe_ids = [x for x in (result.get("credit_note_id"), result.get("id"), invoice["id"]) if x]
        d = ctx.update_decision(d["id"], stripe_ids=list(dict.fromkeys(stripe_ids)))
    except Exception as exc:
        log.exception("credit note failed")
        d = ctx.update_decision(d["id"], status="failed", reasons=d["reasons"] + [f"Stripe call failed: {exc}"])
    return [d]


def _payment_confirmation(ctx, customer, invoice, cls, evidence) -> list[dict]:
    if invoice is None:
        return _no_invoice(ctx, customer, "payment confirmation")
    billing = _module(ctx, "clause.billing")
    inv = invoice
    if billing is not None and hasattr(billing, "sync_invoice"):
        try:
            inv = billing.sync_invoice(ctx, invoice["id"])
        except Exception as exc:
            log.warning("sync_invoice failed: %s", exc)
    paid = inv.get("status") == "paid"
    reasons = ([f"Stripe confirms {inv.get('number')} is paid"] if paid else
               [f"Customer says {inv.get('number')} is paid; Stripe shows it {inv.get('status')}, so chasing continues"])
    return [ctx.record_decision(
        actor=ACTOR, action="collections.payment_check", customer=customer["id"], outcome=policy.NONE,
        clauses=["CC-2"], reasons=reasons,
        summary=f"Checked {customer['name']}'s payment claim on {inv.get('number')} against Stripe",
        invoice=inv["id"], evidence=evidence(cls.get("summary", "")), stripe_ids=[inv["id"]])]


# ---- context for chase drafting (A2 calls this) --------------------------------------------
def chase_history(ctx: Any, customer_key: str, limit: int = 4) -> list[str]:
    """What memory says about this customer, for a chase draft's `history` fact. Amounts are
    masked: the draft may only quote figures the policy engine computed (check_draft)."""
    hits = ctx.recall(customer_key, CHASE_HISTORY_QUESTION)
    return [MONEY_RE.sub("[amount]", h.text)[:300] for h in hits[:limit]]
