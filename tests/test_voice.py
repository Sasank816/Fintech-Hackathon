"""Voice notes: the summary is only a prompt for the desk, and it may never change a number."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from clause import voice
from clause.app import create_app
from clause.config import Settings
from clause.context import build_ctx
from clause.llm import FakeLLM

RAMBLE = ("Um so I've just, uh, finished up at Bright Bakery, it was a long one, er, four hours of labour "
          "in the end, you know, and I swapped out two RCDs, and obviously the call-out as well")


class SummaryLLM(FakeLLM):
    """FakeLLM that also answers the summary call with a canned result."""

    def __init__(self, result: dict):
        super().__init__(name="summary-stub")
        self.result = result
        self.calls = 0

    def _json(self, system, user, schema):
        if "kind" not in schema["properties"]:  # the desk's work extraction: behave like FakeLLM
            raise AssertionError("desk extraction should not use the stub")
        self.calls += 1
        return self.result


@pytest.fixture
def ctx():
    c = build_ctx(Settings(), fake=True)
    raised: list[dict] = []

    def fake_raise(ctx, **kw):
        inv = {"id": f"in_v{len(raised) + 1}", "number": f"V-{len(raised) + 1}", "customer": kw["customer_key"],
               "status": "open", "total_pence": sum(ln["amount_pence"] for ln in kw["lines"]), "due_date": "2026-11-14"}
        raised.append(kw)
        return ctx.store.put("invoice", inv["id"], inv)

    c.register_action("billing.raise_invoice", fake_raise)
    c.register_action("mail.send_invoice_email", lambda ctx, **kw: None)
    c.store.put("customer", "bright", {"name": "Bright Bakery Co", "email": "b@example.com", "aliases": ["Bright Bakery"],
                                        "stripe_customer_id": "cus_b", "terms_days": 30, "key_account": False})
    c.raised = raised
    return c


def use_summary(ctx, monkeypatch, result: dict) -> SummaryLLM:
    llm = SummaryLLM(result)
    ctx.llm = llm
    # the desk reads work with the fake extractor whatever the LLM is, so these tests pin voice alone
    monkeypatch.setattr(voice.desk, "extract_work", voice.desk.fake_extract_work)
    return llm


def test_clean_drops_filler_but_keeps_the_facts():
    assert voice.clean("Um, four hours, uh, at Acme you know") == "four hours, at Acme"


def test_numbers_match_across_words_and_digits():
    assert voice.numbers_in("four hours and 2 RCDs, £1,250.00") == {"4", "2", "1250"}
    assert voice.invented_numbers("Finished at Bright Bakery: 4 hours labour, 2 RCDs and 1 call-out", RAMBLE) == set()
    assert voice.invented_numbers("Finished at Bright Bakery: 6 hours labour", RAMBLE) == {"6"}


def test_short_note_is_used_as_spoken(ctx):
    s = voice.summarise(ctx, "approve Acme")
    assert s["prompt"] == "approve Acme" and not s["summarised"]


def test_without_a_model_the_cleaned_transcript_is_the_prompt(ctx):
    s = voice.summarise(ctx, RAMBLE)
    assert not s["summarised"] and "Um" not in s["prompt"] and "four hours of labour" in s["prompt"]


def test_long_note_is_summarised_and_billed(ctx, monkeypatch):
    use_summary(ctx, monkeypatch, {"kind": "bill",
                                   "prompt": "Finished at Bright Bakery: four hours labour, two RCDs and the call-out"})
    out = voice.handle_transcript(ctx, RAMBLE)
    assert out["summarised"] and out["prompt"].startswith("Finished at Bright Bakery")
    assert out["invoice_ids"] and len(ctx.raised) == 1
    note = ctx.store.all("voice_note")[-1]
    assert note["transcript"] == RAMBLE and note["prompt"] == out["prompt"]


def test_a_summary_that_changes_a_number_is_discarded(ctx, monkeypatch):
    use_summary(ctx, monkeypatch, {"kind": "bill", "prompt": "Finished at Bright Bakery: six hours labour"})
    s = voice.summarise(ctx, RAMBLE)
    assert not s["summarised"] and "four hours" in s["prompt"] and "number" in s["note"]


def test_questions_are_shaped_to_route_as_questions(ctx, monkeypatch):
    use_summary(ctx, monkeypatch, {"kind": "question", "prompt": "Tell me who still owes money"})
    s = voice.summarise(ctx, "right so, um, I was wondering, can you tell me who still owes me money at the moment please")
    assert s["prompt"].endswith("?")


def test_unclear_note_does_not_reach_the_desk(ctx, monkeypatch):
    use_summary(ctx, monkeypatch, {"kind": "unclear", "prompt": ""})
    out = voice.handle_transcript(ctx, "sorry hang on a second, that was the radio talking, not me, just ignore all of that one")
    assert out["decision_ids"] == [] and not ctx.raised and "didn't catch" in out["reply"]


def test_routes(ctx):
    with TestClient(create_app(ctx, verify_keys=False)) as c:
        assert "server_transcription" in c.get("/api/voice/status").json()
        assert c.post("/api/voice/prompt", json={"transcript": ""}).status_code == 400
        out = c.post("/api/voice/prompt", json={"transcript": "Finished at Bright Bakery, four hours labour"}).json()
        assert out["invoice_ids"] and "£" in out["reply"]
