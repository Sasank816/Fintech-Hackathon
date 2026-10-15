"""POLICY.md is what a human reads; clause/policy.py is what the agent runs. They must agree."""
from pathlib import Path

from clause import policy

DOC = (Path(__file__).resolve().parent.parent / "POLICY.md").read_text(encoding="utf-8")


def money(pence: int) -> str:
    return f"£{pence / 100:,.2f}"


def test_every_limit_in_code_is_stated_in_the_policy():
    expected = [
        money(policy.AUTO_BILL_CAP_PENCE), f"**{policy.MAX_LINE_QUANTITY}**", f"**{policy.DUPLICATE_WINDOW_DAYS} days**",
        f"**{policy.MAX_TERMS_DAYS} days**", f"**{policy.DEFAULT_TERMS_DAYS} days**",
        ", ".join(str(d) for d in policy.CHASE_STAGES_DAYS[:-1]) + f" and {policy.CHASE_STAGES_DAYS[-1]} days",
        f"**{policy.CHASE_MIN_GAP_DAYS} days**", f"**{policy.PROMISE_GRACE_DAYS} day**",
        f"**{policy.INTEREST_MIN_DAYS_OVERDUE} days**", money(policy.AUTO_INTEREST_CAP_PENCE),
        money(policy.AUTO_CREDIT_CAP_PENCE), f"**{policy.AUTO_CREDIT_MAX_PCT}%**",
    ]
    missing = [e for e in expected if e not in DOC]
    assert not missing, f"POLICY.md does not state: {missing}"


def test_every_clause_is_documented():
    for cid, title in policy.CLAUSES.items():
        assert f"**{cid} {title}**" in DOC
