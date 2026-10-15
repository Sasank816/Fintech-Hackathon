"""Agent 2: collections. Chases, statutory/contractual interest, approvals for key accounts.

`collections_tick(ctx)` is idempotent per clock day. Amounts come from law.py, never the LLM.

ASSUMPTIONS (policy.py / llm.py / mail.py were not visible when writing this):
  * policy.next_chase(standing) and policy.interest(standing, total_pence) return an object with
    .outcome ("auto"|"needs_approval"|"deny"|"hold"|"none", str or Enum), .clauses, .reasons
    and (for chases) an optional .stage.
  * llm.draft_email(facts) -> dict {subject, body} or an object with those attributes.
  * llm.check_draft(draft, allowed_pence=..., must_include=...) -> bool, (bool, ...) or an object with .ok.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
from datetime import date
from decimal import Decimal

from clause import billing, law, mail, policy
from clause.billing import money
from clause.llm import check_draft

log = logging.getLogger("clause.collections")


def _oc(res) -> str:
    v = getattr(res, "outcome", res)
    return str(getattr(v, "value", v)).lower().split(".")[-1]


def _jsonable(v):
    if isinstance(v, (Decimal, date)):
        return str(v) if isinstance(v, Decimal) else v.isoformat()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def _calc(claim) -> dict:
    return _jsonable(dataclasses.asdict(claim)) if dataclasses.is_dataclass(claim) else _jsonable(dict(vars(claim)))


def _once(ctx, key: str) -> bool:
    """True the first time `key` is seen (and marks it)."""
    k = f"once_{hashlib.sha1(key.encode()).hexdigest()[:16]}"
    if ctx.store.meta(k):
        return False
    ctx.store.set_meta(k, "1")
    return True


def _standing(ctx, inv: dict, cust: dict, today: date):
    promise = inv.get("promise_date")
    chases = [(int(c["stage"]), date.fromisoformat(c["sent_on"])) for c in inv.get("chases", []) if c.get("sent_on")]
    return policy.InvoiceStanding(
        today=today, due=date.fromisoformat(inv["due_date"]), amount_remaining_pence=inv["remaining_pence"],
        disputed=bool(inv.get("disputed")), promise_date=date.fromisoformat(promise[:10]) if promise else None,
        chases_sent=chases, interest_claimed=bool(inv.get("interest_claimed")),
        key_account=bool(cust.get("key_account")))


def _claim(cust: dict, inv: dict, today: date):
    rate = cust.get("contract_rate_over_base")
    return law.late_claim(inv["remaining_pence"], date.fromisoformat(inv["due_date"]), today,
                          Decimal(str(rate)) if rate else None)


# --------------------------------------------------------------------------- chase emails


def _subject_body(d) -> tuple[str, str]:
    return (str(d["subject"]), str(d["body"])) if isinstance(d, dict) else (str(d.subject), str(d.body))


def _check_ok(r) -> bool:
    if isinstance(r, bool):
        return r
    if isinstance(r, tuple):
        return bool(r[0]) if r and isinstance(r[0], bool) else not r[0]
    if isinstance(r, (list, set)):
        return not r  # list of problems
    ok = getattr(r, "ok", None)
    return bool(ok) if ok is not None else bool(r)


def _template_email(facts: dict) -> tuple[str, str]:
    """Deterministic fallback: only code-computed numbers, always has the invoice number and pay link."""
    notice = f"\n{facts['interest_notice']}\n" if facts.get("interest_notice") else ""
    subject = f"Reminder: invoice {facts['invoice_number']} is {facts['days_overdue']} days overdue"
    body = (f"Hello {facts['customer_name']},\n\nInvoice {facts['invoice_number']} for {facts['amount_due']} was due on "
            f"{facts['due_date']} and is now {facts['days_overdue']} days overdue.\n{notice}\n"
            f"You can pay securely online here: {facts['pay_url']}\n\nIf you have already paid, thank you, and please "
            f"ignore this note.\n\nKind regards,\n{facts['business_name']}")
    return subject, body


def _chase_email(ctx, facts: dict, allowed: set[int], must: list[str]) -> tuple[str, str]:
    for attempt in range(2):
        try:
            d = ctx.llm.draft_email(facts)
            if _check_ok(check_draft(d, allowed_pence=allowed, must_include=must)):
                return _subject_body(d)
            log.info("draft failed check (attempt %d)", attempt + 1)
        except Exception as e:
            log.warning("draft_email error (attempt %d): %s", attempt + 1, e)
    return _template_email(facts)


# --------------------------------------------------------------------------- the tick


def _do_chase(ctx, inv, cust, today, standing, out: list) -> None:
    res, stage = policy.next_chase(standing)  # (Verdict, stage days or None)
    oc = _oc(res)
    if oc == "none":
        return
    clauses, reasons = list(getattr(res, "clauses", []) or []), list(getattr(res, "reasons", []) or [])
    if oc != "auto":  # hold / deny / needs_approval: record once per invoice per reason
        if _once(ctx, f"chase-{oc}-{inv['id']}-{'|'.join(reasons)}"):
            out.append(ctx.record_decision(
                actor="agent", action="chase", customer=inv["customer"], outcome=oc, clauses=clauses, reasons=reasons,
                summary=f"Chase on {inv['number']} not sent ({oc}): {'; '.join(reasons)}", invoice=inv["id"],
                evidence=[{"type": "stripe", "ref": inv["id"], "quote": f"open, due {inv['due_date']}"}],
                stripe_ids=[inv["id"]]))
        return

    due = date.fromisoformat(inv["due_date"])
    days_over = (today - due).days
    claim = _claim(cust, inv, today) if days_over > 0 else None
    interest_p = int(getattr(claim, "interest_pence", 0)) if claim else 0
    comp_p = int(getattr(claim, "compensation_pence", 0)) if claim else 0
    notice = None
    if claim and not inv.get("interest_claimed"):
        notice = (f"If this remains unpaid we will claim late-payment charges of {money(claim.total_pence)} "
                  f"({claim.formula()}).")
    facts = {
        "kind": "chase", "stage": str(stage),
        "stage_label": {1: "friendly reminder", 14: "firm reminder", 30: "final notice"}.get(stage, "reminder"),
        "customer_name": cust["name"], "business_name": getattr(ctx.settings, "business_name", "Hollis Electrical Ltd"),
        "invoice_number": inv["number"], "amount_due": money(inv["remaining_pence"]),
        "due_date": due.strftime("%d %b %Y"), "days_overdue": days_over, "pay_url": inv["hosted_invoice_url"],
        **({"interest_notice": notice} if notice else {}),
    }
    allowed = {inv["remaining_pence"]} | ({claim.total_pence, interest_p, comp_p} if claim else set())
    subject, body = _chase_email(ctx, facts, allowed, [inv["number"], inv["hosted_invoice_url"]])
    dec = ctx.record_decision(
        actor="agent", action="chase", customer=inv["customer"], outcome="auto", clauses=clauses, reasons=reasons,
        summary=f"Chased {cust['name']} on {inv['number']} ({money(inv['remaining_pence'])}, {days_over} days overdue)",
        invoice=inv["id"], evidence=[{"type": "stripe", "ref": inv["id"], "quote": f"open, due {inv['due_date']}"}],
        stripe_ids=[inv["id"]])
    sent = mail.send_customer_email(ctx, customer_key=inv["customer"], subject=subject, body=body, kind="chase",
                                    invoice_id=inv["id"], decision_id=dec["id"])
    fresh = ctx.store.get("invoice", inv["id"])
    fresh["chases"] = list(fresh.get("chases", [])) + [
        {"stage": facts["stage"], "sent_on": today.isoformat(), "outbox_id": (sent or {}).get("id")}]
    ctx.store.put("invoice", inv["id"], fresh)
    ctx.emit("invoice", fresh)
    out.append(dec)


def _do_interest(ctx, inv, cust, today, out: list) -> None:
    inv = ctx.store.get("invoice", inv["id"])  # chase may have updated it
    standing = _standing(ctx, inv, cust, today)
    if today <= date.fromisoformat(inv["due_date"]) or inv["remaining_pence"] <= 0:
        return
    claim = _claim(cust, inv, today)
    res = policy.interest(standing, claim.total_pence)
    oc = _oc(res)
    if oc == "none":
        return
    clauses = list(getattr(res, "clauses", []) or []) or ["CC-5"]
    reasons = list(getattr(res, "reasons", []) or [])
    contractual = bool(cust.get("contract_rate_over_base"))
    evidence = [{"type": "stripe", "ref": inv["id"], "quote": f"{inv['number']} open, due {inv['due_date']}"}]
    if contractual:
        evidence.append({"type": "contract", "ref": cust["id"], "quote": cust.get("contract_quote") or ""})
    kind_txt = "Contractual" if contractual else "Statutory"
    summary = f"{kind_txt} interest {money(claim.total_pence)} on {inv['number']} ({claim.formula()})"
    base = dict(actor="agent", customer=inv["customer"], clauses=clauses, reasons=reasons, invoice=inv["id"],
                evidence=evidence, calc=_calc(claim), stripe_ids=[inv["id"]])

    if oc == "auto":
        if not _once(ctx, f"interest-auto-{inv['id']}"):
            return
        dec = ctx.record_decision(action="billing.interest", outcome="auto", summary=summary, **base)
        billing.interest(ctx, invoice_id=inv["id"], today=today.isoformat(),
                         expected_total_pence=claim.total_pence, decision_id=dec["id"])
        out.append(ctx.store.get("decision", dec["id"]) or dec)
    elif oc == "needs_approval":
        if not _once(ctx, f"interest-pending-{inv['id']}"):
            return
        out.append(ctx.record_decision(
            action="billing.interest", outcome="needs_approval", summary=summary + ": approval required",
            proposal={"action": "billing.interest",
                      "args": {"invoice_id": inv["id"], "today": today.isoformat(),
                               "expected_total_pence": claim.total_pence}},  # CC-8: re-asserted on approval
            **base))
    else:  # hold / deny
        if _once(ctx, f"interest-{oc}-{inv['id']}-{'|'.join(reasons)}"):
            out.append(ctx.record_decision(action="billing.interest", outcome=oc,
                                           summary=f"Interest on {inv['number']} not claimed ({oc})", **base))


def collections_tick(ctx) -> list[dict]:
    billing.register(ctx)
    today = ctx.clock_date()
    day_key = f"tick_{today.isoformat()}"
    if ctx.store.meta(day_key):
        return []
    out: list[dict] = []
    for inv in [d for d in ctx.store.all("invoice") if d.get("kind") == "work" and d.get("status") == "open"]:
        cust = ctx.store.get("customer", inv["customer"])
        try:
            standing = _standing(ctx, inv, cust, today)
            _do_chase(ctx, inv, cust, today, standing, out)
            _do_interest(ctx, inv, cust, today, out)
        except Exception:
            log.exception("collections failed for %s", inv.get("id"))
            raise
    ctx.store.set_meta(day_key, "1")
    return out
