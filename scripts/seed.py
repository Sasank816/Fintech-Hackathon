"""Seed the demo: 3 customers on one Test Clock starting now, plus the two seeded invoices.

Run standalone:  python -m scripts.seed      (also used by POST /api/sim/reset)
Bright Bakery deliberately starts with no invoices (the voice note raises one live).
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone

DEMO_CUSTOMERS = [
    dict(key="acme", name="Acme Paper Ltd", env="DEMO_EMAIL_ACME", aliases=["Acme", "Acme Paper"], terms_days=30),
    dict(key="bright", name="Bright Bakery Co", env="DEMO_EMAIL_BRIGHT", aliases=["Bright", "Bright Bakery"],
         terms_days=30),
    dict(key="northwind", name="Northwind Logistics Ltd", env="DEMO_EMAIL_NORTHWIND",
         aliases=["Northwind", "Northwind Logistics"], terms_days=30, key_account=True,
         contract_rate_over_base="4.0",
         contract_quote="Interest on late payment accrues at 4% per annum above the Bank of England base rate."),
]

SEED_INVOICES = [
    dict(customer="acme", code="CONTRACT", description="Q3 maintenance contract", pence=480000),
    dict(customer="northwind", code="CONTRACT", description="Warehouse rewire, phase 1", pence=1250000),
]


def seed(ctx, *, force: bool = True) -> dict:
    """(Re)create the demo state. `force=False` is a no-op when already seeded."""
    from clause import billing

    if not force and ctx.store.meta("seeded"):
        return {"seeded": False}
    now = int(time.time())
    ctx.store.set_meta("run_id", uuid.uuid4().hex[:8])  # fresh Stripe idempotency keys every run
    ctx.store.set_meta("clock_ids", [])
    ctx.store.set_meta("clock_ts", now)
    ctx.store.set_meta("clock_date", datetime.fromtimestamp(now, tz=timezone.utc).date().isoformat())

    for c in DEMO_CUSTOMERS:
        profile = {k: v for k, v in c.items() if k not in ("key", "name", "env")}
        billing.ensure_customer(ctx, c["key"], c["name"], os.environ.get(c["env"]) or f"{c['key']}@example.com",
                                **profile)

    for s in SEED_INVOICES:
        cust = ctx.store.get("customer", s["customer"])
        quote = f"{s['description']} (agreed contract, {cust['name']})"
        dec = ctx.record_decision(
            actor="system", action="seed_invoice", customer=s["customer"], outcome="auto", clauses=["CC-1"],
            reasons=["Contract invoice seeded from the signed agreement"],
            summary=f"Seeded contract invoice '{s['description']}' for {cust['name']}",
            evidence=[{"type": "seed", "ref": "contract", "quote": quote}])
        billing.raise_invoice(
            ctx, customer_key=s["customer"], kind="work", decision_id=dec["id"],
            source={"type": "seed", "ref": "contract", "quote": quote}, description=s["description"],
            lines=[{"code": s["code"], "description": s["description"], "quantity": 1, "unit_pence": s["pence"],
                    "amount_pence": s["pence"], "evidence_quote": quote}])
    ctx.store.set_meta("seeded", "1")
    return {"seeded": True}


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(override=True)
    from clause import billing
    from clause.context import build_ctx

    try:
        from clause.config import get_settings as _settings
    except ImportError:  # pragma: no cover
        from clause.config import Settings as _settings
    _ctx = build_ctx(_settings())
    billing.startup(_ctx, poll=False)
    print(seed(_ctx, force=False))
    for inv in _ctx.store.all("invoice"):
        print(inv["number"], inv["customer"], inv["total_pence"], inv["hosted_invoice_url"])
