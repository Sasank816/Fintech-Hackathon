"""Every Stripe write the agent makes goes through here. Each write carries the decision ID in
its metadata and an idempotency key derived from it, so a retried decision can never bill twice.
Test mode only: a live key is refused at construction.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Protocol


class LiveKeyRefused(ValueError):
    pass


@dataclass
class InvoiceInfo:
    id: str
    number: str
    status: str
    amount_due: int
    amount_remaining: int
    total: int
    due_date: date | None
    hosted_invoice_url: str
    customer: str

    @property
    def dashboard_url(self) -> str:
        return f"https://dashboard.stripe.com/test/invoices/{self.id}"


def to_date(ts: int | None) -> date | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date() if ts else None


def to_ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc).timestamp())


class Gateway(Protocol):
    def create_clock(self, start: date, name: str) -> str: ...
    def clock_date(self, clock_id: str) -> date: ...
    def advance_clock(self, clock_id: str, to: date) -> date: ...
    def create_customer(self, name: str, email: str, clock_id: str, metadata: dict[str, str]) -> str: ...
    def create_invoice(self, customer_id: str, lines: list[tuple[str, int]], days_until_due: int,
                       metadata: dict[str, str], idem: str, description: str) -> InvoiceInfo: ...
    def credit_note(self, invoice_id: str, amount_pence: int, memo: str, metadata: dict[str, str], idem: str) -> str: ...
    def get_invoice(self, invoice_id: str) -> InvoiceInfo: ...
    def pay_invoice_with_test_card(self, invoice_id: str, customer_id: str) -> InvoiceInfo: ...


class StripeGateway:
    def __init__(self, api_key: str):
        if not api_key.startswith(("sk_test_", "rk_test_")):
            raise LiveKeyRefused("Clause runs in Stripe test mode only; use an sk_test_ or rk_test_ key")
        import stripe

        self.client = stripe.StripeClient(api_key, max_network_retries=2)

    @staticmethod
    def _info(inv: Any) -> InvoiceInfo:
        return InvoiceInfo(
            id=inv.id, number=inv.number or inv.id, status=inv.status, amount_due=inv.amount_due,
            amount_remaining=inv.amount_remaining, total=inv.total, due_date=to_date(inv.due_date),
            hosted_invoice_url=inv.hosted_invoice_url or "", customer=inv.customer)

    def create_clock(self, start: date, name: str) -> str:
        return self.client.v1.test_helpers.test_clocks.create({"frozen_time": to_ts(start), "name": name}).id

    def clock_date(self, clock_id: str) -> date:
        return to_date(self.client.v1.test_helpers.test_clocks.retrieve(clock_id).frozen_time)

    def advance_clock(self, clock_id: str, to: date) -> date:
        self.client.v1.test_helpers.test_clocks.advance(clock_id, {"frozen_time": to_ts(to)})
        for _ in range(120):  # advancing is asynchronous; wait for status=ready
            clock = self.client.v1.test_helpers.test_clocks.retrieve(clock_id)
            if clock.status == "ready":
                return to_date(clock.frozen_time)
            if clock.status == "internal_failure":
                raise RuntimeError(f"test clock {clock_id} failed to advance")
            time.sleep(1)
        raise TimeoutError(f"test clock {clock_id} still advancing after 120s")

    def create_customer(self, name: str, email: str, clock_id: str, metadata: dict[str, str]) -> str:
        return self.client.v1.customers.create(
            {"name": name, "email": email, "test_clock": clock_id, "metadata": metadata},
            {"idempotency_key": f"cus-{metadata.get('customer_key', email)}-{clock_id}"}).id

    def create_invoice(self, customer_id: str, lines: list[tuple[str, int]], days_until_due: int,
                       metadata: dict[str, str], idem: str, description: str) -> InvoiceInfo:
        inv = self.client.v1.invoices.create({
            "customer": customer_id, "currency": "gbp", "collection_method": "send_invoice",
            "days_until_due": days_until_due, "auto_advance": False, "description": description,
            "pending_invoice_items_behavior": "exclude", "metadata": metadata,
        }, {"idempotency_key": f"{idem}-invoice"})
        for i, (label, pence) in enumerate(lines):
            self.client.v1.invoice_items.create({
                "customer": customer_id, "invoice": inv.id, "currency": "gbp", "amount": pence,
                "description": label, "metadata": metadata,
            }, {"idempotency_key": f"{idem}-item-{i}"})
        inv = self.client.v1.invoices.finalize_invoice(inv.id, {"auto_advance": False}, {"idempotency_key": f"{idem}-finalize"})
        inv = self.client.v1.invoices.send_invoice(inv.id, None, {"idempotency_key": f"{idem}-send"})
        return self._info(inv)

    def credit_note(self, invoice_id: str, amount_pence: int, memo: str, metadata: dict[str, str], idem: str) -> str:
        return self.client.v1.credit_notes.create({
            "invoice": invoice_id, "memo": memo, "metadata": metadata, "reason": "product_unsatisfactory",
            "lines": [{"type": "custom_line_item", "description": memo[:200], "quantity": 1, "unit_amount": amount_pence}],
        }, {"idempotency_key": f"{idem}-credit"}).id

    def get_invoice(self, invoice_id: str) -> InvoiceInfo:
        return self._info(self.client.v1.invoices.retrieve(invoice_id))

    def pay_invoice_with_test_card(self, invoice_id: str, customer_id: str) -> InvoiceInfo:
        pm = self.client.v1.payment_methods.attach("pm_card_visa", {"customer": customer_id})
        return self._info(self.client.v1.invoices.pay(invoice_id, {"payment_method": pm.id}))


class FakeGateway:
    """In-memory Stripe stand-in for the test suite. Mirrors the states the agent relies on."""

    def __init__(self) -> None:
        self.clocks: dict[str, date] = {}
        self.customers: dict[str, dict[str, Any]] = {}
        self.invoices: dict[str, dict[str, Any]] = {}
        self.credit_notes: dict[str, dict[str, Any]] = {}
        self.idem: dict[str, str] = {}
        self.calls: list[str] = []

    def create_clock(self, start: date, name: str) -> str:
        cid = f"clock_{len(self.clocks) + 1}"
        self.clocks[cid] = start
        return cid

    def clock_date(self, clock_id: str) -> date:
        return self.clocks[clock_id]

    def advance_clock(self, clock_id: str, to: date) -> date:
        self.clocks[clock_id] = to
        return to

    def create_customer(self, name: str, email: str, clock_id: str, metadata: dict[str, str]) -> str:
        cid = f"cus_{len(self.customers) + 1}"
        self.customers[cid] = {"name": name, "email": email, "clock": clock_id, "metadata": metadata}
        return cid

    def create_invoice(self, customer_id: str, lines: list[tuple[str, int]], days_until_due: int,
                       metadata: dict[str, str], idem: str, description: str) -> InvoiceInfo:
        self.calls.append(f"invoice:{idem}")
        if idem in self.idem:
            return self.get_invoice(self.idem[idem])
        clock = self.customers[customer_id]["clock"]
        from datetime import timedelta

        iid = f"in_{len(self.invoices) + 1}"
        total = sum(p for _, p in lines)
        self.invoices[iid] = {
            "number": f"TEST-{len(self.invoices) + 1:04d}", "status": "open", "total": total, "credited": 0,
            "paid": 0, "due": self.clocks[clock] + timedelta(days=days_until_due), "customer": customer_id,
            "lines": lines, "metadata": metadata}
        self.idem[idem] = iid
        return self.get_invoice(iid)

    def credit_note(self, invoice_id: str, amount_pence: int, memo: str, metadata: dict[str, str], idem: str) -> str:
        if idem in self.idem:
            return self.idem[idem]
        inv = self.invoices[invoice_id]
        if inv["credited"] + amount_pence > inv["total"]:
            raise ValueError("credit exceeds invoice total")
        inv["credited"] += amount_pence
        cn = f"cn_{len(self.credit_notes) + 1}"
        self.credit_notes[cn] = {"invoice": invoice_id, "amount": amount_pence, "memo": memo, "metadata": metadata}
        self.idem[idem] = cn
        if inv["total"] - inv["credited"] - inv["paid"] <= 0:
            inv["status"] = "paid"
        return cn

    def get_invoice(self, invoice_id: str) -> InvoiceInfo:
        inv = self.invoices[invoice_id]
        remaining = max(0, inv["total"] - inv["credited"] - inv["paid"])
        return InvoiceInfo(invoice_id, inv["number"], inv["status"], remaining, remaining, inv["total"],
                           inv["due"], f"https://invoice.stripe.com/test/{invoice_id}", inv["customer"])

    def pay_invoice_with_test_card(self, invoice_id: str, customer_id: str) -> InvoiceInfo:
        inv = self.invoices[invoice_id]
        inv["paid"] = inv["total"] - inv["credited"]
        inv["status"] = "paid"
        return self.get_invoice(invoice_id)
