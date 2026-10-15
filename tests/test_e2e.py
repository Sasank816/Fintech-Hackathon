"""The whole demo story through the HTTP API, offline (FakeGateway, FakeLLM, LocalMemory, outbox mail).

Skips until billing, collections, mail and inbox have landed; after that it is the integration gate.
"""
from __future__ import annotations

import pytest

pytest.importorskip("clause.billing")
pytest.importorskip("clause.collections")
pytest.importorskip("clause.mail")
pytest.importorskip("clause.inbox")

from fastapi.testclient import TestClient  # noqa: E402

from clause.app import create_app  # noqa: E402
from clause.config import Settings  # noqa: E402
from clause.context import build_ctx  # noqa: E402


def invoices(ctx, customer, kind="work"):
    return [i for i in ctx.store.all("invoice", customer=customer) if i.get("kind", "work") == kind]


def test_the_demo_story_end_to_end():
    ctx = build_ctx(Settings(), fake=True)
    with TestClient(create_app(ctx, verify_keys=False)) as c:
        c.post("/api/sim/reset").raise_for_status()
        state = c.get("/api/state").json()
        assert {"acme", "bright", "northwind"} <= {cu["id"] for cu in state["customers"]}
        assert [i["total_pence"] for i in invoices(ctx, "acme")] == [480_000]
        assert [i["total_pence"] for i in invoices(ctx, "northwind")] == [1_250_000]

        # 1. Owner's voice note → priced invoice, emailed. Nobody typed an amount.
        r = c.post("/api/chat", json={"message": "Just finished at Bright Bakery: four hours labour, two RCDs and the call-out.",
                                      "mode": "voice"})
        r.raise_for_status()
        assert [i["total_pence"] for i in invoices(ctx, "bright")] == [36_100]
        assert any(o["kind"] == "invoice" and o["customer"] == "bright" for o in ctx.store.all("outbox"))

        # 2. Customer's email asks to be billed → invoice with no human step.
        acme_email = ctx.store.get("customer", "acme")["email"]
        c.post("/api/mail/inject", json={"from": acme_email, "subject": "Extra work",
                                         "body": "Hi, please go ahead and invoice the extra 3 hours and the EICR. Thanks"}
               ).raise_for_status()
        emailed = [i for i in invoices(ctx, "acme") if (i.get("source") or {}).get("type") == "email"]
        assert [i["total_pence"] for i in emailed] == [36_000]

        # 3. 45 days pass → chases, an automatic statutory interest invoice, a key account waits for approval.
        c.post("/api/sim/advance", json={"days": 45}).raise_for_status()
        assert 93_18 in [i["total_pence"] for i in invoices(ctx, "acme", "interest")]
        assert any(o["kind"] == "chase" for o in ctx.store.all("outbox"))
        pending = [d for d in c.get("/api/state").json()["approvals"] if d["customer"] == "northwind"]
        assert len(pending) == 1
        c.post(f"/api/approvals/{pending[0]['id']}", json={"approve": True}).raise_for_status()
        assert [i["total_pence"] for i in invoices(ctx, "northwind", "interest")] == [39_81]

        # 4. A promise to pay pauses chasing (CC-4).
        bright_email = ctx.store.get("customer", "bright")["email"]
        c.post("/api/mail/inject", json={"from": bright_email, "subject": "Re: invoice",
                                         "body": "Sorry for the delay, we'll pay by 2026-12-04."}).raise_for_status()
        assert invoices(ctx, "bright")[0]["promise_date"] == "2026-12-04"
        assert any(d["outcome"] == "hold" and "CC-4" in d["clauses"] for d in ctx.store.all("decision"))

        # 5. Acme pays the main invoice.
        main = next(i for i in invoices(ctx, "acme") if i["total_pence"] == 480_000)
        c.post(f"/api/sim/pay/{main['id']}").raise_for_status()
        assert ctx.store.get("invoice", main["id"])["status"] == "paid"

        # 6. Every pound explained.
        why = c.post("/api/why", json={"question": "Why did you charge Acme £93.18?"}).json()
        assert "CC-5" in why["answer"]
        assert {e["op"] for e in ctx.memory_log} >= {"remember", "recall"}
