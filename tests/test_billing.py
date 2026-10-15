"""Billing + collections, fully offline (FakeGateway via build_ctx(fake=True))."""
import pytest

from clause import billing, collections as coll
from clause.context import build_ctx
from scripts.seed import seed

try:
    from clause.config import get_settings as _settings
except ImportError:  # pragma: no cover
    from clause.config import Settings as _settings


@pytest.fixture()
def ctx():
    c = build_ctx(_settings(), fake=True)
    billing.startup(c, poll=False)
    seed(c)
    return c


def _inv(ctx, customer, kind="work"):
    return [d for d in ctx.store.all("invoice") if d["customer"] == customer and d["kind"] == kind]


def _decisions(ctx, **kw):
    return [d for d in ctx.store.all("decision") if all(d.get(k) == v for k, v in kw.items())]


def test_seed_raises_open_invoices_on_one_clock(ctx):
    assert len(ctx.store.meta("clock_ids")) == 1
    acme, nw = _inv(ctx, "acme")[0], _inv(ctx, "northwind")[0]
    assert (acme["status"], acme["total_pence"]) == ("open", 480000)
    assert nw["total_pence"] == 1250000 and not _inv(ctx, "bright")
    assert acme["decision_id"] and acme["hosted_invoice_url"] and acme["invoice_pdf"]


def test_fourth_customer_goes_on_new_clock(ctx):
    billing.ensure_customer(ctx, "extra", "Extra Ltd", "x@example.com")
    assert len(ctx.store.meta("clock_ids")) == 2
    assert billing.ensure_customer(ctx, "extra", "Extra Ltd", "x@example.com") == ctx.store.get("customer", "extra")


def test_pay_marks_paid_and_records_decision_once(ctx):
    inv = _inv(ctx, "acme")[0]
    events = ctx.bus.subscribe()
    ctx.gateway.pay_invoice(inv["id"])
    billing.sync_invoice(ctx, inv["id"])
    billing.sync_invoice(ctx, inv["id"])  # poll after webhook: must not double-record
    assert ctx.store.get("invoice", inv["id"])["status"] == "paid"
    pays = _decisions(ctx, action="payment_received", customer="acme")
    assert len(pays) == 1 and pays[0]["clauses"] == ["CC-2"] and pays[0]["outcome"] == "none"
    assert any("Paid" in n and "after 0 chases" in n for n in ctx.memory.notes.get("acme", []))
    seen = []
    while not events.empty():
        seen.append(events.get_nowait()["event"])
    assert "toast" in seen


def test_webhook_replay_processed_once(ctx):
    inv = _inv(ctx, "acme")[0]
    ctx.gateway.pay_invoice(inv["id"])
    ev = {"id": "evt_1", "type": "invoice.paid", "data": {"object": {"id": inv["id"]}}}
    assert billing.handle_event(ctx, ev) is True
    assert billing.handle_event(ctx, ev) is False
    assert len(_decisions(ctx, action="payment_received")) == 1


def test_advance_45_chases_and_interest(ctx):
    res = billing.advance(ctx, 45)
    acme = _inv(ctx, "acme")[0]
    assert acme["overdue"] and len(acme["chases"]) == 1
    child = _inv(ctx, "acme", "interest")
    assert len(child) == 1 and child[0]["total_pence"] == 9318 and child[0]["parent_invoice"] == acme["id"]
    assert [l["amount_pence"] for l in child[0]["lines"]] == [2318, 7000]
    assert ctx.store.get("invoice", acme["id"])["interest_claimed"] is True
    nw = [d for d in res["decisions"] if d["customer"] == "northwind" and d["outcome"] == "needs_approval"]
    assert len(nw) == 1 and not _inv(ctx, "northwind", "interest")
    assert {o["kind"] for o in ctx.store.all("outbox")} >= {"chase", "interest"}


def test_approving_northwind_raises_3981(ctx):
    billing.advance(ctx, 45)
    pending = _decisions(ctx, customer="northwind", outcome="needs_approval")[0]
    inv = ctx.run_action(pending["proposal"]["action"], pending["proposal"]["args"])
    assert inv["total_pence"] == 3981 and len(inv["lines"]) == 1
    assert ctx.store.get("decision", pending["id"])["stripe_ids"][-1] == inv["id"]


def test_approval_fails_loudly_if_total_changed(ctx):
    billing.advance(ctx, 45)
    args = dict(_decisions(ctx, customer="northwind", outcome="needs_approval")[0]["proposal"]["args"])
    args["expected_total_pence"] += 1
    with pytest.raises(RuntimeError, match="CC-8"):
        ctx.run_action("billing.interest", args)


def test_second_tick_same_day_is_a_noop(ctx):
    billing.advance(ctx, 45)
    n = len(ctx.store.all("decision"))
    assert coll.collections_tick(ctx) == []
    assert len(ctx.store.all("decision")) == n


def test_disputed_invoice_is_held_without_interest(ctx):
    acme = _inv(ctx, "acme")[0]
    acme["disputed"], acme["dispute_quote"] = True, "work not done"
    ctx.store.put("invoice", acme["id"], acme)
    res = billing.advance(ctx, 45)
    assert not _inv(ctx, "acme", "interest")
    held = [d for d in res["decisions"] if d["customer"] == "acme"]
    assert held and all(d["outcome"] == "hold" for d in held)


def test_reset_gives_clean_demo(ctx):
    billing.advance(ctx, 45)
    billing.reset(ctx)
    assert len(ctx.store.all("invoice")) == 2 and not _inv(ctx, "acme", "interest")
    assert ctx.clock_date() == __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date()
