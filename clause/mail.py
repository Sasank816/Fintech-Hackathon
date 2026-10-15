"""Email in and out. Every email the business sends goes through `send_customer_email` (Gmail SMTP,
or recorded only in outbox mode); every customer email that arrives, from the Gmail inbox poller or
from `POST /api/mail/inject`, goes through `ingest` and on to `inbox.handle_inbound`.

Gmail uses an app password (2-Step Verification on), not OAuth.
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
import smtplib
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parseaddr, parsedate_to_datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from clause.store import new_id, utcnow

log = logging.getLogger("clause.mail")

POLL_SECONDS = 10

router = APIRouter()
_ctx: Any = None  # set by register(); the routes use it


# ---- parsing ------------------------------------------------------------------------------
_WROTE_RE = re.compile(r"^\s*On\s.+wrote:\s*$", re.I)


def strip_quoted(text: str) -> str:
    """Drop quoted reply text: '>' lines and everything after "On ... wrote:"."""
    lines = text.replace("\r\n", "\n").split("\n")
    out = []
    for i, line in enumerate(lines):
        if _WROTE_RE.match(line):
            break
        # Gmail wraps a long "On ... wrote:" over two lines
        if line.strip().startswith("On ") and i + 1 < len(lines) and lines[i + 1].strip().endswith("wrote:"):
            break
        if line.lstrip().startswith(">"):
            continue
        out.append(line)
    return "\n".join(out).strip()


def parse_message(raw: bytes) -> dict[str, Any]:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        body = part.get_content()
    else:  # HTML-only email: crude tag strip is enough for classification
        html = msg.get_body(preferencelist=("html",))
        body = re.sub(r"<[^>]+>", " ", html.get_content()) if html is not None else ""
    try:
        sent = parsedate_to_datetime(msg["Date"]).isoformat() if msg["Date"] else utcnow()
    except (TypeError, ValueError):
        sent = utcnow()
    return {
        "message_id": (msg["Message-ID"] or f"<{new_id('in')}@clause.local>").strip(),
        "from": str(msg["From"] or ""),
        "to": str(msg["To"] or ""),
        "subject": str(msg["Subject"] or ""),
        "body": strip_quoted(body),
        "sent_at": sent,
    }


# ---- mailers ------------------------------------------------------------------------------
class OutboxMailer:
    """Records only. The UI still shows every email; nothing leaves the machine."""

    mode = "outbox"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, to: str, subject: str, body: str, attachments: list[tuple[str, bytes, str]] = ()) -> str:
        message_id = make_msgid(domain="clause.local")
        self.sent.append({"to": to, "subject": subject, "body": body, "message_id": message_id,
                          "attachments": [name for name, _, _ in attachments]})
        return message_id

    def fetch_unseen(self) -> list[dict[str, Any]]:
        return []


class GmailMailer:
    mode = "gmail"

    def __init__(self, address: str, app_password: str, from_name: str = ""):
        if not address or not app_password:
            raise ValueError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD are required for MAIL_MODE=gmail")
        self.address = address
        self.app_password = app_password
        self.from_name = from_name
        self._imap_lock = threading.Lock()

    def send(self, to: str, subject: str, body: str, attachments: list[tuple[str, bytes, str]] = ()) -> str:
        msg = EmailMessage()
        msg["From"] = f"{self.from_name} <{self.address}>" if self.from_name else self.address
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = format_datetime(datetime.now().astimezone())
        message_id = make_msgid(domain=self.address.split("@")[-1])
        msg["Message-ID"] = message_id
        msg.set_content(body)
        msg.add_alternative(email_html(subject, body, self.from_name or self.address), subtype="html")
        for filename, data, mimetype in attachments:
            maintype, _, subtype = mimetype.partition("/")
            msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=filename)
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(self.address, self.app_password)
            smtp.send_message(msg)
        return message_id

    def fetch_unseen(self) -> list[dict[str, Any]]:
        """Unread messages in INBOX, parsed; fetching marks them seen."""
        out = []
        with self._imap_lock, imaplib.IMAP4_SSL("imap.gmail.com", 993) as imap:
            imap.login(self.address, self.app_password)
            imap.select("INBOX")
            status, data = imap.search(None, "UNSEEN")
            if status != "OK":
                return []
            for num in data[0].split():
                status, parts = imap.fetch(num, "(RFC822)")  # RFC822 (not BODY.PEEK) sets \\Seen
                if status != "OK":
                    continue
                for part in parts:
                    if isinstance(part, tuple):
                        try:
                            out.append(parse_message(part[1]))
                        except Exception:
                            log.exception("could not parse message %s", num)
            imap.logout()
        return out


# ---- sending ------------------------------------------------------------------------------
def _download_pdf(url: str) -> bytes | None:
    try:
        import httpx

        resp = httpx.get(url, timeout=8, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        log.warning("invoice PDF download failed, sending without attachment: %s", exc)
        return None


def send_customer_email(ctx: Any, *, customer_key: str, subject: str, body: str, kind: str,
                        invoice_id: str | None = None, decision_id: str | None = None,
                        attach_invoice_pdf: bool = False) -> dict:
    customer = ctx.store.get("customer", customer_key)
    if customer is None or not customer.get("email"):
        raise KeyError(f"customer {customer_key!r} has no email address")
    invoice = ctx.store.get("invoice", invoice_id) if invoice_id else None

    url = (invoice or {}).get("hosted_invoice_url")
    if url and url not in body:
        body = f"{body.rstrip()}\n\nPay securely online: {url}\n"

    attachments: list[tuple[str, bytes, str]] = []
    if attach_invoice_pdf and invoice and invoice.get("invoice_pdf"):
        pdf = _download_pdf(invoice["invoice_pdf"])
        if pdf:
            attachments.append((f"Invoice-{invoice.get('number') or invoice_id}.pdf", pdf, "application/pdf"))

    mode = getattr(ctx.mailer, "mode", "outbox")
    error = None
    try:
        message_id = ctx.mailer.send(customer["email"], subject, body, attachments)
    except Exception as exc:  # a mail failure must not lose the record of what we meant to send
        log.exception("sending to %s failed", customer["email"])
        error, mode, message_id = str(exc), "outbox", None
        ctx.emit("toast", {"level": "error", "text": f"Email to {customer['name']} failed: {exc}"})

    out_id = new_id("out")
    doc = ctx.store.put("outbox", out_id, {
        "customer": customer_key, "to": customer["email"], "subject": subject, "body": body,
        "sent_at": utcnow(), "mode": mode, "message_id": message_id, "invoice": invoice_id,
        "decision_id": decision_id, "kind": kind, "attachments": [a[0] for a in attachments], "error": error,
    })
    ctx.emit("email", {**doc, "direction": "out"})
    ctx.remember_later(customer_key, "email_out", out_id, f"Sent {kind} email '{subject}': {body}")
    return doc


def invoice_email_text(ctx: Any, invoice: dict, customer: dict) -> tuple[str, str]:
    business = ctx.settings.business_name
    total = f"£{invoice.get('total_pence', 0) / 100:,.2f}"
    lines = [f"Dear {customer['name']},", ""]
    if invoice.get("kind") == "interest":
        lines.append(f"Please find attached invoice {invoice.get('number')} for {total}, a claim for late-payment "
                     f"interest and compensation on invoice {invoice.get('parent_number') or invoice.get('parent_invoice')}.")
        subject = f"Late payment interest: invoice {invoice.get('number')} from {business}"
    else:
        lines.append(f"Please find attached invoice {invoice.get('number')} for {total}, due on {invoice.get('due_date')}.")
        subject = f"Invoice {invoice.get('number')} from {business}"
    for line in invoice.get("lines") or []:
        lines.append(f"  - {line.get('description')} x{line.get('quantity')}: £{line.get('amount_pence', 0) / 100:,.2f}")
    if invoice.get("hosted_invoice_url"):
        lines += ["", f"Pay securely online: {invoice['hosted_invoice_url']}"]
    lines += ["", "Kind regards,", f"Accounts team, {business}"]
    return subject, "\n".join(lines)


def send_invoice_email(ctx: Any, *, invoice_id: str, decision_id: str | None = None,
                       subject: str | None = None, body: str | None = None) -> dict:
    """Registered as `mail.send_invoice_email`: email an invoice (or interest invoice) with its pay link."""
    invoice = ctx.store.get("invoice", invoice_id)
    if invoice is None:
        raise KeyError(f"invoice {invoice_id} not found")
    customer = ctx.store.get("customer", invoice["customer"])
    default_subject, default_body = invoice_email_text(ctx, invoice, customer)
    return send_customer_email(
        ctx, customer_key=invoice["customer"], subject=subject or default_subject, body=body or default_body,
        kind="interest" if invoice.get("kind") == "interest" else "invoice", invoice_id=invoice_id,
        decision_id=decision_id or invoice.get("decision_id"), attach_invoice_pdf=True)


# ---- receiving ----------------------------------------------------------------------------
def ingest(ctx: Any, msg: dict[str, Any]) -> dict[str, Any]:
    """Store one inbound email, show it, and act on it. Same path for Gmail and /api/mail/inject."""
    from clause import inbox

    message_id = msg.get("message_id") or f"<{new_id('in')}@clause.local>"
    existing = ctx.store.get("email", message_id)
    if existing is not None:
        return {"email": existing, "decisions": []}
    doc = ctx.store.put("email", message_id, {
        "customer": None, "from": msg.get("from", ""), "to": msg.get("to", ""), "subject": msg.get("subject", ""),
        "body": msg.get("body", ""), "received_at": utcnow(), "sent_at": msg.get("sent_at") or utcnow(),
        "classification": None, "invoice": None, "decision_ids": [],
    })
    ctx.emit("email", {**doc, "direction": "in"})
    try:
        decisions = inbox.handle_inbound(ctx, doc)
    except Exception as exc:
        log.exception("handling %s failed", message_id)
        ctx.emit("toast", {"level": "error", "text": f"Could not process email '{doc['subject']}': {exc}"})
        decisions = []
    return {"email": ctx.store.get("email", message_id), "decisions": decisions}


def poll_once(ctx: Any) -> list[dict[str, Any]]:
    own = (ctx.settings.gmail_address or "").lower()
    results = []
    for msg in ctx.mailer.fetch_unseen():
        if own and parseaddr(msg.get("from", ""))[1].lower() == own:
            continue  # our own sent mail bouncing back into the inbox
        results.append(ingest(ctx, msg))
    return results


def _poll_forever(ctx: Any) -> None:
    while True:
        try:
            poll_once(ctx)
        except Exception as exc:
            log.warning("inbox poll failed: %s", exc)
        time.sleep(POLL_SECONDS)


# ---- wiring -------------------------------------------------------------------------------
def register(ctx: Any) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_action("mail.send_invoice_email", send_invoice_email)
    if isinstance(ctx.mailer, GmailMailer) and not ctx.mailer.from_name:
        ctx.mailer.from_name = ctx.settings.business_name


def startup(ctx: Any) -> None:
    if getattr(ctx.mailer, "mode", "outbox") == "gmail":
        threading.Thread(target=_poll_forever, args=(ctx,), name="gmail-poller", daemon=True).start()
        log.info("Gmail inbox poller started (every %ss)", POLL_SECONDS)


def _require_ctx() -> Any:
    if _ctx is None:
        raise HTTPException(503, "mail not registered")
    return _ctx


@router.post("/api/mail/poll")
def api_poll() -> dict[str, Any]:
    ctx = _require_ctx()
    try:
        results = poll_once(ctx)
    except Exception as exc:
        raise HTTPException(502, f"inbox poll failed: {exc}") from exc
    return {"emails": [r["email"] for r in results]}


@router.post("/api/mail/inject")
def api_inject(body: dict = Body(...)) -> dict[str, Any]:
    ctx = _require_ctx()
    if not body.get("from") or not (body.get("body") or "").strip():
        raise HTTPException(400, "from and body are required")
    return ingest(ctx, {
        "message_id": f"<{new_id('inject')}@clause.local>", "from": body["from"],
        "to": ctx.settings.gmail_address, "subject": body.get("subject", ""),
        "body": strip_quoted(body["body"]), "sent_at": utcnow(),
    })


def _hit(h: Any) -> dict[str, str]:
    return {"text": h.text, "source": h.source}


@router.get("/api/memory/{customer}")
def api_recall(customer: str, q: str = "") -> dict[str, Any]:
    ctx = _require_ctx()
    hits = ctx.recall(customer, q or f"What do we know about {customer}: promises, disputes, payments, decisions?")
    return {"customer": customer, "backend": ctx.memory.backend, "hits": [_hit(h) for h in hits]}


@router.post("/api/memory/{customer}/forget")
def api_forget(customer: str) -> dict[str, Any]:
    ctx = _require_ctx()
    ctx.forget(customer)
    return {"ok": True}


# ---- branded HTML (voiz style; the plain text above stays as the fallback) ---------------------
import html as _html
import re as _re

_URL = _re.compile(r"https?://[^\s<>\"]+")
_MONEY = _re.compile(r"(£[0-9][0-9,]*\.[0-9]{2})")


def _inline(text: str) -> str:
    out = _html.escape(text)
    out = _MONEY.sub(r"<strong>\1</strong>", out)
    return _URL.sub(lambda m: f'<a href="{m.group(0)}" style="color:#00794A">{m.group(0)}</a>', out)


def email_html(subject: str, body: str, sender: str) -> str:
    pay_url = next((u for u in _URL.findall(body) if "stripe.com" in u), "")
    button = (f"<p style='margin:18px 0 6px'><a href='{pay_url}' style='display:inline-block;background:#39FF88;color:#0B0B0F;"
              f"text-decoration:none;font-weight:600;font-size:15px;padding:12px 22px;border-radius:999px'>Pay securely online</a></p>"
              f"<p style='margin:0 0 8px;font-size:12px;color:#6B6B78'>Secure payment by Stripe · test mode</p>") if pay_url else ""
    blocks, rows = [], []
    for raw in body.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("- "):
            item = line.strip()[2:]
            desc, _, amt = item.rpartition(": ")
            rows.append((desc or item, amt if desc else ""))
            continue
        if rows:
            blocks.append("<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='margin:4px 0 12px'>"
                          + "".join(f"<tr><td style='padding:10px 12px;background:#F5F5F7;border-radius:10px;font-size:14px;color:#4A4A55'>{_html.escape(d)}</td>"
                                    f"<td style='padding:10px 12px;background:#F5F5F7;border-radius:10px;font-size:14px;font-weight:600;text-align:right;white-space:nowrap'>{_html.escape(a)}</td></tr>"
                                    f"<tr><td colspan='2' style='height:4px'></td></tr>" for d, a in rows)
                          + "</table>")
            rows = []
        if not line.strip():
            blocks.append("")
        elif pay_url and pay_url in line:
            blocks.append(button)
        else:
            blocks.append(f"<p style='margin:0 0 10px;font-size:15px;line-height:1.55;color:#0B0B0F'>{_inline(line)}</p>")
    return f"""<!doctype html><html><body style="margin:0;padding:24px 12px;background:#F5F5F7;font-family:Inter,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;background:#FFFFFF;border-radius:20px;overflow:hidden">
<tr><td style="height:8px;background:#39FF88;background-image:linear-gradient(90deg,#B6FF3B,#39FF88,#00E0C6)"></td></tr>
<tr><td style="padding:22px 28px 6px"><span style="font-size:26px;font-weight:800;letter-spacing:-1px;color:#0B0B0F">voiz</span>
<span style="font-size:13px;color:#6B6B78;margin-left:8px">for {_html.escape(sender)}</span></td></tr>
<tr><td style="padding:10px 28px 4px"><div style="font-size:18px;font-weight:600;color:#0B0B0F;margin:0 0 14px">{_html.escape(subject)}</div>
{"".join(blocks)}</td></tr>
<tr><td style="padding:16px 28px 22px;border-top:1px solid #EBEBF0;font-size:12px;color:#6B6B78">Sent by voiz, the accounts assistant, on behalf of {_html.escape(sender)}. Every charge follows the written credit policy; reply to this email with any question.</td></tr>
</table></td></tr></table></body></html>"""
