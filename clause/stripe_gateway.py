"""Every Stripe call the agent makes goes through here (via the adapter block in clause/billing.py).
Each write carries an idempotency key, so a retried decision can never bill twice.
Test mode only: a live key is refused at construction.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol


class LiveKeyRefused(ValueError):
    pass


@dataclass
class InvoiceInfo:
    id: str
    number: str
    status: str
    total: int
    amount_remaining: int
    due_date: date | None
    hosted_invoice_url: str
    invoice_pdf: str
    customer: str
    paid_at: date | None = None

    @property
    def dashboard_url(self) -> str:
        return f"https://dashboard.stripe.com/test/invoices/{self.id}"


def to_date(ts: int | None) -> date | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date() if ts else None


class Gateway(Protocol):
    def create_clock(self, *, frozen_time: int, name: str) -> str: ...
    def advance_clock(self, clock_id: str, to_ts: int) -> None: ...
    def create_customer(self, *, name: str, email: str, clock_id: str, metadata: dict[str, str],
                        idempotency_key: str) -> str: ...
    def create_invoice(self, *, customer_id: str, lines: list[dict[str, Any]], days_until_due: int,
                       metadata: dict[str, str], description: str, idempotency_key: str) -> InvoiceInfo: ...
    def get_invoice(self, invoice_id: str) -> InvoiceInfo: ...
    def pay_invoice(self, invoice_id: str) -> InvoiceInfo: ...
    def credit_note(self, *, invoice_id: str, amount_pence: int, memo: str, metadata: dict[str, str],
                    idempotency_key: str) -> str: ...


def _meta(d: dict[str, Any]) -> dict[str, str]:
    """Stripe metadata: string values, at most 500 characters each."""
    return {str(k)[:40]: str(v)[:500] for k, v in (d or {}).items() if v is not None}


class StripeGateway:
    def __init__(self, api_key: str):
        if not api_key.startswith(("sk_test_", "rk_test_")):
            raise LiveKeyRefused("Clause runs in Stripe test mode only; use an sk_test_ or rk_test_ key")
        import stripe

        self.client = stripe.StripeClient(api_key, max_network_retries=2)

    @staticmethod
    def _info(inv: Any) -> InvoiceInfo:
        transitions = getattr(inv, "status_transitions", None)
        paid_ts = getattr(transitions, "paid_at", None) if transitions else None
        return InvoiceInfo(
            id=inv.id, number=inv.number or inv.id, status=inv.status, total=inv.total,
            amount_remaining=inv.amount_remaining, due_date=to_date(inv.due_date),
            hosted_invoice_url=inv.hosted_invoice_url or "", invoice_pdf=inv.invoice_pdf or "",
            customer=inv.customer if isinstance(inv.customer, str) else inv.customer.id, paid_at=to_date(paid_ts))

    def create_clock(self, *, frozen_time: int, name: str) -> str:
        return self.client.v1.test_helpers.test_clocks.create({"frozen_time": int(frozen_time), "name": name[:300]}).id

    def advance_clock(self, clock_id: str, to_ts: int) -> None:
        clocks = self.client.v1.test_helpers.test_clocks
        if clocks.retrieve(clock_id).frozen_time >= int(to_ts):
            return
        clocks.advance(clock_id, {"frozen_time": int(to_ts)})
        for _ in range(180):  # advancing is asynchronous; wait for status=ready
            clock = clocks.retrieve(clock_id)
            if clock.status == "ready":
                return
            if clock.status == "internal_failure":
                raise RuntimeError(f"test clock {clock_id} failed to advance")
            time.sleep(1)
        raise TimeoutError(f"test clock {clock_id} still advancing after 180 s")

    def create_customer(self, *, name: str, email: str, clock_id: str, metadata: dict[str, str],
                        idempotency_key: str) -> str:
        return self.client.v1.customers.create(
            {"name": name, "email": email, "test_clock": clock_id, "metadata": _meta(metadata)},
            {"idempotency_key": idempotency_key}).id

    def create_invoice(self, *, customer_id: str, lines: list[dict[str, Any]], days_until_due: int,
                       metadata: dict[str, str], description: str, idempotency_key: str) -> InvoiceInfo:
        inv = self.client.v1.invoices.create({
            "customer": customer_id, "currency": "gbp", "collection_method": "send_invoice",
            "days_until_due": int(days_until_due), "auto_advance": False, "description": description[:500],
            "pending_invoice_items_behavior": "exclude", "metadata": _meta(metadata),
        }, {"idempotency_key": f"{idempotency_key}-invoice"})
        for i, line in enumerate(lines):
            self.client.v1.invoice_items.create({
                "customer": customer_id, "invoice": inv.id, "currency": "gbp", "amount": int(line["amount_pence"]),
                "description": str(line["description"])[:500], "metadata": _meta(line.get("metadata") or metadata),
            }, {"idempotency_key": f"{idempotency_key}-item-{i}"})
        inv = self.client.v1.invoices.finalize_invoice(inv.id, {"auto_advance": False},
                                                       {"idempotency_key": f"{idempotency_key}-finalize"})
        inv = self.client.v1.invoices.send_invoice(inv.id, None, {"idempotency_key": f"{idempotency_key}-send"})
        return self._info(inv)

    def get_invoice(self, invoice_id: str) -> InvoiceInfo:
        return self._info(self.client.v1.invoices.retrieve(invoice_id))

    def pay_invoice(self, invoice_id: str) -> InvoiceInfo:
        inv = self.client.v1.invoices.retrieve(invoice_id)
        if inv.status == "paid":
            return self._info(inv)
        customer = inv.customer if isinstance(inv.customer, str) else inv.customer.id
        pm = self.client.v1.payment_methods.attach("pm_card_visa", {"customer": customer})
        return self._info(self.client.v1.invoices.pay(invoice_id, {"payment_method": pm.id}))

    def credit_note(self, *, invoice_id: str, amount_pence: int, memo: str, metadata: dict[str, str],
                    idempotency_key: str) -> str:
        return self.client.v1.credit_notes.create({
            "invoice": invoice_id, "memo": memo[:500], "metadata": _meta(metadata), "reason": "product_unsatisfactory",
            "lines": [{"type": "custom_line_item", "description": memo[:200], "quantity": 1,
                       "unit_amount": int(amount_pence)}],
        }, {"idempotency_key": idempotency_key}).id


class FakeGateway:
    """In-memory Stripe stand-in for the test suite. Mirrors the states the agent relies on."""

    def __init__(self) -> None:
        self.clocks: dict[str, int] = {}
        self.customers: dict[str, dict[str, Any]] = {}
        self.invoices: dict[str, dict[str, Any]] = {}
        self.credit_notes: dict[str, dict[str, Any]] = {}
        self.idem: dict[str, str] = {}
        self.calls: list[str] = []

    def create_clock(self, *, frozen_time: int, name: str) -> str:
        cid = f"clock_{len(self.clocks) + 1}"
        self.clocks[cid] = int(frozen_time)
        return cid

    def advance_clock(self, clock_id: str, to_ts: int) -> None:
        self.clocks[clock_id] = max(self.clocks[clock_id], int(to_ts))

    def create_customer(self, *, name: str, email: str, clock_id: str, metadata: dict[str, str],
                        idempotency_key: str) -> str:
        if idempotency_key in self.idem:
            return self.idem[idempotency_key]
        cid = f"cus_{len(self.customers) + 1}"
        self.customers[cid] = {"name": name, "email": email, "clock": clock_id, "metadata": metadata}
        self.idem[idempotency_key] = cid
        return cid

    def create_invoice(self, *, customer_id: str, lines: list[dict[str, Any]], days_until_due: int,
                       metadata: dict[str, str], description: str, idempotency_key: str) -> InvoiceInfo:
        self.calls.append(f"invoice:{idempotency_key}")
        if idempotency_key in self.idem:
            return self.get_invoice(self.idem[idempotency_key])
        clock = self.customers[customer_id]["clock"]
        iid = f"in_{len(self.invoices) + 1}"
        self.invoices[iid] = {
            "number": f"TEST-{len(self.invoices) + 1:04d}", "status": "open",
            "total": sum(int(ln["amount_pence"]) for ln in lines), "credited": 0, "paid": 0,
            "due": to_date(self.clocks[clock]) + timedelta(days=int(days_until_due)), "customer": customer_id,
            "lines": lines, "metadata": metadata, "paid_at": None}
        self.idem[idempotency_key] = iid
        return self.get_invoice(iid)

    def get_invoice(self, invoice_id: str) -> InvoiceInfo:
        inv = self.invoices[invoice_id]
        remaining = max(0, inv["total"] - inv["credited"] - inv["paid"])
        return InvoiceInfo(invoice_id, inv["number"], inv["status"], inv["total"], remaining, inv["due"],
                           f"https://invoice.stripe.com/test/{invoice_id}", f"fake://invoice/{invoice_id}.pdf",  # never fetched over the network
                           inv["customer"], inv["paid_at"])

    def pay_invoice(self, invoice_id: str) -> InvoiceInfo:
        inv = self.invoices[invoice_id]
        inv["paid"] = inv["total"] - inv["credited"]
        inv["status"] = "paid"
        inv["paid_at"] = to_date(self.clocks[self.customers[inv["customer"]]["clock"]])
        return self.get_invoice(invoice_id)

    def credit_note(self, *, invoice_id: str, amount_pence: int, memo: str, metadata: dict[str, str],
                    idempotency_key: str) -> str:
        if idempotency_key in self.idem:
            return self.idem[idempotency_key]
        inv = self.invoices[invoice_id]
        if inv["credited"] + amount_pence > inv["total"]:
            raise ValueError("credit exceeds invoice total")
        inv["credited"] += amount_pence
        cn = f"cn_{len(self.credit_notes) + 1}"
        self.credit_notes[cn] = {"invoice": invoice_id, "amount": amount_pence, "memo": memo, "metadata": metadata}
        self.idem[idempotency_key] = cn
        if inv["total"] - inv["credited"] - inv["paid"] <= 0:
            inv["status"] = "paid"
        return cn
