"""Agent 2: billing. Stripe invoices, status sync (webhook + poll), Test Clock simulation.

Every penny goes through here. Amounts come from code (rate card / law.py), never the LLM.

ASSUMPTIONS (I could not see stripe_gateway.py / app.py when writing this):
  * The gateway-facing calls are isolated in the `_gw_*` adapter block below. If the real
    `StripeGateway` / `FakeGateway` method or kwarg names differ, that block is the ONLY
    place to change.
  * A1's app.py puts the Ctx on `app.state.ctx` (used by the webhook + sim routes).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from clause import policy

log = logging.getLogger("clause.billing")
router = APIRouter()
_lock = threading.RLock()

# --------------------------------------------------------------------------- helpers


def money(pence: int) -> str:
    return f"£{pence / 100:,.2f}"


def _g(obj, *names, default=None):
    """First non-None attribute / key from obj."""
    for n in names:
        v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
        if v is not None:
            return v
    return default


def _id(x) -> str:
    return x if isinstance(x, str) else _g(x, "id")


def _to_date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).date() if v.tzinfo else v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=timezone.utc).date()
    return date.fromisoformat(str(v)[:10])


def _norm(info) -> dict:
    """Normalise InvoiceInfo (dataclass / object / dict) to plain fields."""
    status = _g(info, "status", default="open")
    total = int(_g(info, "total_pence", "total", default=0))
    remaining = _g(info, "remaining_pence", "amount_remaining")
    if remaining is None:
        remaining = 0 if status == "paid" else total
    return {
        "id": _g(info, "id"),
        "number": _g(info, "number"),
        "status": status,
        "total": total,
        "remaining": int(remaining),
        "due": _to_date(_g(info, "due_date", "due")),
        "hosted": _g(info, "hosted_invoice_url", "hosted_url"),
        "pdf": _g(info, "invoice_pdf", "pdf"),
        "paid_at": _to_date(_g(info, "paid_at")),
    }


def _run_id(ctx) -> str:
    """Per-reset nonce so idempotency keys never replay objects from a previous demo run."""
    rid = ctx.store.meta("run_id")
    if not rid:
        rid = uuid.uuid4().hex[:8]
        ctx.store.set_meta("run_id", rid)
    return rid


def _policy_version() -> str:
    return str(getattr(policy, "POLICY_VERSION", None) or getattr(policy, "VERSION", None) or "CC-1..8")


def _rate_card_version() -> str:
    try:
        p = Path(__file__).resolve().parent.parent / "data" / "ratecard.json"
        return str(json.loads(p.read_text()).get("version", "unknown"))
    except Exception:
        return "unknown"


def _line_sig(lines: list[dict]) -> str:
    return hashlib.sha1(
        json.dumps([[l.get("code"), l.get("description"), l.get("quantity"), l.get("unit_pence")] for l in lines],
                   sort_keys=True, default=str).encode()
    ).hexdigest()[:12]


# --------------------------------------------------------------------------- clock state


def _clock_ts(ctx) -> int:
    """Unix time all Test Clocks currently sit at (kept in lock-step by /api/sim/advance)."""
    v = ctx.store.meta("clock_ts")
    if v is None:
        v = int(time.time())
        ctx.store.set_meta("clock_ts", v)
        ctx.store.set_meta("clock_date", datetime.fromtimestamp(v, tz=timezone.utc).date().isoformat())
    return int(v)


def _clock_for_new_customer(ctx) -> str:
    ids = list(ctx.store.meta("clock_ids") or [])
    counts = Counter(c.get("clock_id") for c in ctx.store.all("customer"))
    for cid in ids:
        if counts[cid] < 3:
            return cid
    cid = _gw_create_clock(ctx, _clock_ts(ctx), f"clause-{_run_id(ctx)}-{len(ids) + 1}")
    ids.append(cid)
    ctx.store.set_meta("clock_ids", ids)
    return cid


# --------------------------------------------------------------------------- GATEWAY ADAPTER
# Only these functions touch ctx.gateway. Adjust here if stripe_gateway.py differs.


def _gw_create_clock(ctx, frozen_ts: int, name: str) -> str:
    return _id(ctx.gateway.create_clock(frozen_time=frozen_ts, name=name))


def _gw_create_customer(ctx, *, name, email, clock_id, key) -> str:
    return _id(ctx.gateway.create_customer(
        name=name, email=email, clock_id=clock_id,
        metadata={"clause_key": key}, idempotency_key=f"cust-{_run_id(ctx)}-{key}"))


def _gw_create_invoice(ctx, *, customer_id, lines, days_until_due, metadata, description, idem) -> dict:
    return _norm(ctx.gateway.create_invoice(
        customer_id=customer_id, lines=lines, days_until_due=days_until_due,
        metadata=metadata, description=description, idempotency_key=idem))


def _gw_get_invoice(ctx, invoice_id: str) -> dict:
    return _norm(ctx.gateway.get_invoice(invoice_id))


def _gw_pay(ctx, invoice_id: str) -> None:
    ctx.gateway.pay_invoice(invoice_id)


def _gw_advance(ctx, clock_id: str, to_ts: int) -> None:
    ctx.gateway.advance_clock(clock_id, to_ts)  # polls until ready


def _gw_credit_note(ctx, *, invoice_id, amount_pence, memo, metadata, idem) -> dict:
    r = ctx.gateway.credit_note(invoice_id=invoice_id, amount_pence=amount_pence, memo=memo,
                                metadata=metadata, idempotency_key=idem)
    return {"id": _id(r), "amount_pence": amount_pence}


# --------------------------------------------------------------------------- actions / registration


def register(ctx) -> None:
    if getattr(ctx, "_billing_registered", False):
        return
    ctx.register_action("billing.raise_invoice", raise_invoice)
    ctx.register_action("billing.credit_note", credit_note)
    ctx.register_action("billing.interest", interest)
    try:
        ctx._billing_registered = True
    except Exception:
        pass


def ensure_customer(ctx, key: str, name: str, email: str, **profile) -> dict:
    """Idempotent by key. New customers go on a clock with < 3 customers."""
    register(ctx)
    with _lock:
        doc = ctx.store.get("customer", key)
        if doc:
            return doc
        clock_id = _clock_for_new_customer(ctx)
        cus = _gw_create_customer(ctx, name=name, email=email, clock_id=clock_id, key=key)
        doc = {
            "id": key, "name": name, "email": email,
            "aliases": profile.get("aliases", []),
            "stripe_customer_id": cus, "clock_id": clock_id,
            "terms_days": int(profile.get("terms_days", 30)),
            "key_account": bool(profile.get("key_account", False)),
            "contract_rate_over_base": profile.get("contract_rate_over_base"),
            "contract_quote": profile.get("contract_quote"),
        }
        ctx.store.put("customer", key, doc)
        return doc


def raise_invoice(ctx, *, customer_key: str, lines: list[dict], kind: str, source: dict,
                  decision_id: str, description: str, parent_invoice: str | None = None) -> dict:
    register(ctx)
    cust = ctx.store.get("customer", customer_key)
    if not cust:
        raise KeyError(f"unknown customer {customer_key!r}")
    norm_lines = []
    for l in lines:
        q = l.get("quantity", 1)
        unit = int(l["unit_pence"])
        norm_lines.append({
            "code": l.get("code", ""), "description": l["description"], "quantity": q,
            "unit_pence": unit, "amount_pence": int(l.get("amount_pence", round(q * unit))),
            "evidence_quote": l.get("evidence_quote", ""),
        })
    with _lock:
        sig = _line_sig(norm_lines)
        for d in ctx.store.all("invoice"):  # idempotent: same decision + same lines -> same invoice
            if (d.get("decision_id") == decision_id and d.get("kind") == kind and d.get("customer") == customer_key
                    and d.get("parent_invoice") == parent_invoice and _line_sig(d.get("lines", [])) == sig):
                return d
        meta = {
            "decision_id": str(decision_id), "kind": kind,
            "source_type": str(source.get("type", "")), "source_ref": str(source.get("ref", ""))[:200],
            "policy_version": _policy_version(), "rate_card": _rate_card_version(),
            "parent_invoice": parent_invoice or "",
        }
        stripe_lines = [{**l, "metadata": meta} for l in norm_lines]
        info = _gw_create_invoice(
            ctx, customer_id=cust["stripe_customer_id"], lines=stripe_lines,
            days_until_due=int(cust["terms_days"]), metadata=meta, description=description,
            idem=f"inv-{_run_id(ctx)}-{decision_id}-{kind}-{customer_key}-{parent_invoice or ''}-{sig}")
        expected = sum(l["amount_pence"] for l in norm_lines)
        if info["total"] != expected:
            log.error("Stripe total %s != computed %s for %s", info["total"], expected, info["id"])
        due = info["due"] or (ctx.clock_date() + timedelta(days=int(cust["terms_days"])))
        doc = {
            "id": info["id"], "customer": customer_key, "number": info["number"], "kind": kind,
            "parent_invoice": parent_invoice, "status": info["status"], "overdue": False,
            "total_pence": info["total"], "remaining_pence": info["remaining"],
            "due_date": due.isoformat(), "hosted_invoice_url": info["hosted"], "invoice_pdf": info["pdf"],
            "lines": norm_lines, "source": source, "decision_id": decision_id,
            "chases": [], "interest_claimed": False, "disputed": False, "dispute_quote": None,
            "promise_date": None, "promise_quote": None, "paid_at": None,
        }
        ctx.store.put("invoice", info["id"], doc)
    ctx.emit("invoice", doc)
    what = ", ".join(f"{l['quantity']}× {l['description']}" for l in norm_lines)
    ctx.remember_later(
        customer_key, "invoice", info["id"],
        f"Invoice {doc['number']} for {money(doc['total_pence'])} raised for {what} — "
        f"source {source.get('type')}: '{source.get('quote', '')}'")
    return doc


def sync_invoice(ctx, invoice_id: str) -> dict:
    """Stripe is the source of truth for status/amounts. Emits only when something changed."""
    with _lock:
        doc = ctx.store.get("invoice", invoice_id)
        if not doc:
            raise KeyError(invoice_id)
        before = json.dumps(doc, sort_keys=True, default=str)
        info = _gw_get_invoice(ctx, invoice_id)
        was_paid = doc["status"] == "paid"
        doc.update(status=info["status"], remaining_pence=info["remaining"], total_pence=info["total"])
        for k_doc, k_info in (("number", "number"), ("hosted_invoice_url", "hosted"), ("invoice_pdf", "pdf")):
            if info[k_info]:
                doc[k_doc] = info[k_info]
        if info["due"]:
            doc["due_date"] = info["due"].isoformat()
        today = ctx.clock_date()
        doc["overdue"] = doc["status"] == "open" and today > date.fromisoformat(doc["due_date"])
        if doc["status"] == "paid" and not doc.get("paid_at"):
            doc["paid_at"] = today.isoformat()
        ctx.store.put("invoice", invoice_id, doc)
        changed = json.dumps(doc, sort_keys=True, default=str) != before
        newly_paid = doc["status"] == "paid" and not was_paid
    if changed:
        ctx.emit("invoice", doc)
    if newly_paid:
        _on_paid(ctx, doc)
    return doc


def _on_paid(ctx, doc: dict) -> None:
    """Runs once per invoice, whether the webhook or the poll notices the payment first."""
    with _lock:
        flag = f"paidrec_{doc['id']}"
        if ctx.store.meta(flag):
            return
        ctx.store.set_meta(flag, "1")
    paid_on = date.fromisoformat(doc["paid_at"]) if doc.get("paid_at") else ctx.clock_date()
    days_after = (paid_on - date.fromisoformat(doc["due_date"])).days
    when = (f"{days_after} days after due" if days_after > 0
            else "on the due date" if days_after == 0 else f"{-days_after} days before due")
    chases = len(doc.get("chases", []))
    amt = money(doc["total_pence"])
    ctx.record_decision(
        actor="stripe", action="payment_received", customer=doc["customer"], outcome="none",
        clauses=["CC-2"], reasons=["Payment received; chasing stops"],
        summary=f"Payment received for {doc['number']} ({amt})", invoice=doc["id"],
        evidence=[{"type": "stripe", "ref": doc["id"], "quote": "status paid"}], stripe_ids=[doc["id"]])
    ctx.remember_later(doc["customer"], "payment", doc["id"],
                       f"Paid {doc['number']} {amt} on {paid_on.isoformat()}, {when}, after {chases} chases")
    cust = ctx.store.get("customer", doc["customer"]) or {}
    ctx.emit("toast", {"level": "success", "text": f"{cust.get('name', doc['customer'])} paid {doc['number']} ({amt})"})


def credit_note(ctx, *, invoice_id: str, amount_pence: int, memo: str, decision_id: str) -> dict:
    doc = ctx.store.get("invoice", invoice_id)
    if not doc:
        raise KeyError(invoice_id)
    cn = _gw_credit_note(
        ctx, invoice_id=invoice_id, amount_pence=int(amount_pence), memo=memo,
        metadata={"decision_id": str(decision_id), "kind": "credit_note"},
        idem=f"cn-{_run_id(ctx)}-{invoice_id}-{decision_id}-{amount_pence}")
    ctx.remember_later(doc["customer"], "credit_note", cn["id"],
                       f"Credit note of {money(int(amount_pence))} issued against {doc['number']}: {memo}")
    return {"credit_note": cn, "invoice": sync_invoice(ctx, invoice_id)}


def interest(ctx, *, invoice_id: str, today: str, expected_total_pence: int | None = None,
             decision_id: str | None = None) -> dict:
    """Raise (and email) the statutory/contractual interest invoice for a parent invoice.

    Registered as `billing.interest`. Used by the AUTO path and by approvals (CC-8): it recomputes the
    claim from the current Stripe state and FAILS LOUDLY if the total differs from what was proposed.
    """
    from clause import law, mail  # local imports: avoid import cycles at module load

    parent = ctx.store.get("invoice", invoice_id)
    if not parent:
        raise KeyError(invoice_id)
    for d in ctx.store.all("invoice"):  # already claimed -> return the existing child
        if d.get("kind") == "interest" and d.get("parent_invoice") == invoice_id:
            return d
    parent = sync_invoice(ctx, invoice_id)
    if parent["status"] != "open":
        raise RuntimeError(f"CC-8: {parent['number']} is {parent['status']}, interest no longer applicable")
    cust = ctx.store.get("customer", parent["customer"])
    day = date.fromisoformat(today) if isinstance(today, str) else today
    due = date.fromisoformat(parent["due_date"])
    rate = cust.get("contract_rate_over_base")
    from decimal import Decimal
    claim = law.late_claim(parent["remaining_pence"], due, day, Decimal(str(rate)) if rate else None)
    if expected_total_pence is not None and claim.total_pence != int(expected_total_pence):
        raise RuntimeError(
            f"CC-8 violation: recomputed interest {claim.total_pence}p != approved {expected_total_pence}p "
            f"on {parent['number']}")
    if not decision_id:
        decision_id = _find_interest_decision(ctx, invoice_id) or f"dec_manual_{invoice_id}"
    interest_p = int(getattr(claim, "interest_pence", claim.total_pence))
    comp_p = int(getattr(claim, "compensation_pence", 0))
    contractual = bool(rate)
    lines = []
    if contractual:
        lines.append({"code": "INT", "description": f"Contractual interest ({cust.get('contract_quote')}): {claim.formula()}",
                      "quantity": 1, "unit_pence": claim.total_pence, "amount_pence": claim.total_pence,
                      "evidence_quote": cust.get("contract_quote") or ""})
    else:
        lines.append({"code": "INT", "description": "Statutory interest, Late Payment of Commercial Debts (Interest) "
                                                     f"Act 1998: {claim.formula()}",
                      "quantity": 1, "unit_pence": interest_p, "amount_pence": interest_p,
                      "evidence_quote": claim.formula()})
        if comp_p:
            lines.append({"code": "COMP", "description": f"Fixed compensation ({money(comp_p)})", "quantity": 1,
                          "unit_pence": comp_p, "amount_pence": comp_p, "evidence_quote": "Late Payment Act 1998 s5A"})
    assert sum(l["amount_pence"] for l in lines) == claim.total_pence, "interest lines do not add up to the claim"
    inv = raise_invoice(
        ctx, customer_key=parent["customer"], lines=lines, kind="interest",
        source={"type": "agent", "ref": invoice_id, "quote": f"{parent['number']} unpaid {(day - due).days} days after due"},
        decision_id=decision_id, description=f"Late payment interest on {parent['number']}", parent_invoice=invoice_id)
    parent = ctx.store.get("invoice", invoice_id)
    parent["interest_claimed"] = True
    ctx.store.put("invoice", invoice_id, parent)
    ctx.emit("invoice", parent)
    dec = ctx.store.get("decision", decision_id) if decision_id else None
    if dec:
        dec["stripe_ids"] = list(dict.fromkeys(list(dec.get("stripe_ids") or []) + [inv["id"]]))
        ctx.store.put("decision", decision_id, dec)
        ctx.emit("decision", dec)
    subject = f"Late payment interest invoice {inv['number']} for {parent['number']}"
    basis = (f"in line with the contract term: \"{cust.get('contract_quote')}\"" if contractual else
             "under the Late Payment of Commercial Debts (Interest) Act 1998")
    body = (f"Hello {cust['name']},\n\nInvoice {parent['number']} was due on {due.strftime('%d %b %Y')} and remains "
            f"unpaid. As set out {basis}, we have raised invoice {inv['number']} "
            f"for {money(inv['total_pence'])} covering late-payment charges ({claim.formula()}).\n\n"
            f"Pay online: {inv['hosted_invoice_url']}\n\nKind regards,\n"
            f"{getattr(ctx.settings, 'business_name', 'Hollis Electrical Ltd')}")
    out = mail.send_customer_email(ctx, customer_key=parent["customer"], subject=subject, body=body, kind="interest",
                                   invoice_id=inv["id"], decision_id=decision_id, attach_invoice_pdf=True)
    inv["chases"] = list(inv.get("chases", []))
    ctx.store.put("invoice", inv["id"], inv)
    inv["_outbox_id"] = (out or {}).get("id") if isinstance(out, dict) else None
    return {k: v for k, v in inv.items() if not k.startswith("_")}


def _find_interest_decision(ctx, invoice_id: str) -> str | None:
    best = None
    for d in ctx.store.all("decision"):
        p = d.get("proposal") or {}
        if p.get("action") == "billing.interest" and (p.get("args") or {}).get("invoice_id") == invoice_id:
            best = d.get("id") or best
    return best


# --------------------------------------------------------------------------- webhook


_SYNC_EVENTS = {
    "invoice.finalized", "invoice.sent", "invoice.paid", "invoice.payment_succeeded", "invoice.payment_failed",
    "invoice.voided", "invoice.marked_uncollectible", "credit_note.created", "test_helpers.test_clock.ready",
}


def handle_event(ctx, event: dict) -> bool:
    """Process one Stripe event. Returns False if it was a duplicate / ignored."""
    eid, etype = event["id"], event["type"]
    with _lock:
        if ctx.store.meta(f"evt_{eid}"):
            return False
        ctx.store.set_meta(f"evt_{eid}", "1")
    if etype not in _SYNC_EVENTS:
        return False
    obj = (event.get("data") or {}).get("object") or {}
    try:
        if etype == "test_helpers.test_clock.ready":
            sync_open_invoices(ctx)
        else:
            inv_id = obj.get("invoice") if etype == "credit_note.created" else obj.get("id")
            if inv_id and ctx.store.get("invoice", inv_id):
                sync_invoice(ctx, inv_id)  # invoice.paid -> _on_paid() fires inside, exactly once
    except Exception:
        with _lock:  # let Stripe's retry (or the poll) try again
            ctx.store.set_meta(f"evt_{eid}", "")
        raise
    return True


def sync_open_invoices(ctx) -> int:
    n = 0
    for d in list(ctx.store.all("invoice")):
        if d.get("status") == "open":
            try:
                sync_invoice(ctx, d["id"])
                n += 1
            except Exception as e:  # one bad invoice must not stop the rest
                log.warning("sync %s failed: %s", d.get("id"), e)
    return n


def _ctx(request: Request):
    return request.app.state.ctx


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    ctx = _ctx(request)
    payload = await request.body()
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET") or getattr(ctx.settings, "stripe_webhook_secret", None)
    if secret:
        import stripe
        try:
            event = stripe.Webhook.construct_event(payload, request.headers.get("stripe-signature", ""), secret)
        except Exception as e:
            raise HTTPException(400, f"invalid signature: {e}")
    else:
        log.warning("STRIPE_WEBHOOK_SECRET unset: accepting UNSIGNED webhook (local demo only)")
        event = json.loads(payload)
    await run_in_threadpool(handle_event, ctx, event)
    return {"ok": True}


# --------------------------------------------------------------------------- poll fallback

_poller: threading.Thread | None = None
_stop = threading.Event()


def _poll_loop(ctx, every: float) -> None:
    while not _stop.wait(every):
        try:
            sync_open_invoices(ctx)
        except Exception as e:
            log.warning("poll error: %s", e)


def startup(ctx, *, poll: bool = True, every: float = 5.0) -> None:
    """A1 calls this at app startup: registers actions and starts the 5 s poll fallback."""
    global _poller
    register(ctx)
    if poll and (_poller is None or not _poller.is_alive()):
        _stop.clear()
        _poller = threading.Thread(target=_poll_loop, args=(ctx, every), daemon=True, name="stripe-poll")
        _poller.start()


def shutdown() -> None:
    _stop.set()


# --------------------------------------------------------------------------- simulation


def advance(ctx, days: int) -> dict:
    from clause import collections as coll

    if days < 0:
        raise ValueError("days must be >= 0")
    with _lock:
        new_ts = _clock_ts(ctx) + days * 86400
        for cid in ctx.store.meta("clock_ids") or []:
            _gw_advance(ctx, cid, new_ts)
        ctx.store.set_meta("clock_ts", new_ts)
        today = datetime.fromtimestamp(new_ts, tz=timezone.utc).date()
        ctx.store.set_meta("clock_date", today.isoformat())
    ctx.emit("clock", {"clock_date": today.isoformat()})
    sync_open_invoices(ctx)
    decisions = coll.collections_tick(ctx)
    return {"clock_date": today.isoformat(), "decisions": decisions}


def _forget(ctx, key: str) -> None:
    coro = ctx.memory.forget(key)
    try:
        if ctx.loop is not None and ctx.loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, ctx.loop).result(timeout=30)
        else:
            asyncio.run(coro)
    except Exception as e:
        log.warning("memory.forget(%s) failed: %s", key, e)


def reset(ctx) -> dict:
    from scripts.seed import seed

    keys = [c.get("id") or c.get("name") for c in ctx.store.all("customer")] or ["acme", "bright", "northwind"]
    ctx.store.reset()
    for k in keys:
        _forget(ctx, k)
    ctx.memory_log.clear()  # the UI's memory feed starts empty after a reset
    seed(ctx)  # new run_id, clock at real now, customers + invoices
    ctx.emit("clock", {"clock_date": ctx.clock_date().isoformat()})
    return {"ok": True}


@router.post("/api/sim/advance")
def sim_advance(body: dict, request: Request):
    try:
        return advance(_ctx(request), int(body.get("days", 0)))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/sim/pay/{invoice_id}")
def sim_pay(invoice_id: str, request: Request):
    ctx = _ctx(request)
    if not ctx.store.get("invoice", invoice_id):
        raise HTTPException(404, "unknown invoice")
    _gw_pay(ctx, invoice_id)
    return sync_invoice(ctx, invoice_id)


@router.post("/api/sim/reset")
def sim_reset(request: Request):
    return reset(_ctx(request))
