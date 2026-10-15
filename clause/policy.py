"""The written credit-control policy (POLICY.md) as code. Pure functions, no I/O, no LLM.

The LLM reads documents and drafts prose; it never decides whether or how much to charge.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

POLICY_VERSION = "1.0"

MAX_TERMS_DAYS = 60
DEFAULT_TERMS_DAYS = 30
CHASE_STAGES_DAYS = (1, 14, 30)
CHASE_MIN_GAP_DAYS = 7
PROMISE_GRACE_DAYS = 1
INTEREST_MIN_DAYS_OVERDUE = 7
AUTO_INTEREST_CAP_PENCE = 25_000
AUTO_CREDIT_CAP_PENCE = 5_000
AUTO_CREDIT_MAX_PCT = 10

CLAUSES = {
    "CC-1": "Billing",
    "CC-2": "Chasing",
    "CC-3": "Disputes",
    "CC-4": "Promises",
    "CC-5": "Interest",
    "CC-6": "Credit notes",
    "CC-7": "Guardrails",
    "CC-8": "Approvals",
}

AUTO = "auto"
NEEDS_APPROVAL = "needs_approval"
DENY = "deny"
HOLD = "hold"
NONE = "none"


@dataclass
class Verdict:
    outcome: str
    clauses: list[str]
    reasons: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.outcome == AUTO


def billing(contract_amount_pence: int, invoice_amount_pence: int, terms_days: int) -> Verdict:
    if invoice_amount_pence != contract_amount_pence:
        return Verdict(DENY, ["CC-1"], [
            f"Invoice amount £{invoice_amount_pence / 100:,.2f} differs from contract amount "
            f"£{contract_amount_pence / 100:,.2f}"])
    if contract_amount_pence <= 0:
        return Verdict(DENY, ["CC-1"], ["Contract amount must be positive"])
    if terms_days > MAX_TERMS_DAYS:
        return Verdict(NEEDS_APPROVAL, ["CC-1"], [
            f"Payment terms of {terms_days} days exceed {MAX_TERMS_DAYS} days"])
    return Verdict(AUTO, ["CC-1"], [
        f"Signed contract received; invoice equals contract amount; terms {terms_days} days"])


@dataclass
class InvoiceStanding:
    """Everything the collections policy needs to know about one open invoice on one day."""

    today: date
    due: date
    amount_remaining_pence: int
    disputed: bool = False
    promise_date: date | None = None
    chases_sent: list[tuple[int, date]] = field(default_factory=list)  # (stage days, sent on)
    interest_claimed: bool = False
    key_account: bool = False

    @property
    def days_overdue(self) -> int:
        return (self.today - self.due).days

    @property
    def promise_active(self) -> bool:
        return self.promise_date is not None and (self.today - self.promise_date).days <= PROMISE_GRACE_DAYS

    @property
    def promise_broken(self) -> bool:
        return self.promise_date is not None and not self.promise_active


def next_chase(s: InvoiceStanding) -> tuple[Verdict, int | None]:
    """Whether to chase today, and which stage (1, 14 or 30 days overdue)."""
    if s.amount_remaining_pence <= 0 or s.days_overdue < CHASE_STAGES_DAYS[0]:
        return Verdict(NONE, ["CC-2"], ["Not overdue"]), None
    if s.disputed:
        return Verdict(HOLD, ["CC-3"], ["Invoice disputed in writing; chasing paused until a human resolves it"]), None
    if s.promise_active:
        return Verdict(HOLD, ["CC-4"], [
            f"Customer promised payment by {s.promise_date.isoformat()}; paused until "
            f"{PROMISE_GRACE_DAYS} day grace passes"]), None
    sent_stages = {stage for stage, _ in s.chases_sent}
    due_stages = [st for st in CHASE_STAGES_DAYS if s.days_overdue >= st and st not in sent_stages]
    if not due_stages:
        return Verdict(NONE, ["CC-2"], ["No chase stage due"]), None
    if s.chases_sent:
        last = max(d for _, d in s.chases_sent)
        if (s.today - last).days < CHASE_MIN_GAP_DAYS:
            return Verdict(NONE, ["CC-2"], [f"Last chase {last.isoformat()} is under {CHASE_MIN_GAP_DAYS} days ago"]), None
    stage = max(due_stages)  # skip stages that were missed while paused
    reasons = [f"{s.days_overdue} days overdue; stage {stage}-day chase is due"]
    clauses = ["CC-2"]
    if s.promise_broken:
        reasons.append(f"Promise to pay by {s.promise_date.isoformat()} was broken")
        clauses.append("CC-4")
    return Verdict(AUTO, clauses, reasons), stage


def interest(s: InvoiceStanding, claim_total_pence: int) -> Verdict:
    """Whether a late-payment claim of `claim_total_pence` may be raised today."""
    if s.interest_claimed:
        return Verdict(NONE, ["CC-5"], ["Interest already claimed on this invoice"])
    if s.amount_remaining_pence <= 0:
        return Verdict(NONE, ["CC-5"], ["Invoice settled"])
    if s.days_overdue < INTEREST_MIN_DAYS_OVERDUE:
        return Verdict(NONE, ["CC-5"], [f"Only {s.days_overdue} days overdue (minimum {INTEREST_MIN_DAYS_OVERDUE})"])
    if s.disputed:
        return Verdict(HOLD, ["CC-3", "CC-5"], ["Disputed invoice: no interest until resolved"])
    if s.promise_active:
        return Verdict(HOLD, ["CC-4", "CC-5"], [f"Promise to pay by {s.promise_date.isoformat()} still running"])
    if claim_total_pence <= 0:
        return Verdict(NONE, ["CC-5"], ["Nothing to claim"])
    reasons = [f"{s.days_overdue} days overdue, not disputed, no running promise"]
    if s.promise_broken:
        reasons.append(f"Promise to pay by {s.promise_date.isoformat()} was broken")
    if s.key_account:
        return Verdict(NEEDS_APPROVAL, ["CC-5", "CC-8"], reasons + ["Key account: a human approves every claim"])
    if claim_total_pence > AUTO_INTEREST_CAP_PENCE:
        return Verdict(NEEDS_APPROVAL, ["CC-5", "CC-8"], reasons + [
            f"Claim £{claim_total_pence / 100:,.2f} is over the £{AUTO_INTEREST_CAP_PENCE / 100:,.2f} auto limit"])
    return Verdict(AUTO, ["CC-5"], reasons + [
        f"Claim £{claim_total_pence / 100:,.2f} is within the £{AUTO_INTEREST_CAP_PENCE / 100:,.2f} auto limit"])


def credit(invoice_total_pence: int, already_credited_pence: int, requested_pence: int,
           reason_is_product_issue: bool, injection_suspected: bool) -> Verdict:
    if injection_suspected:
        return Verdict(DENY, ["CC-7"], ["Request came with instructions aimed at the agent; denied and flagged"])
    if requested_pence <= 0:
        return Verdict(DENY, ["CC-6"], ["No credit amount stated"])
    if already_credited_pence + requested_pence > invoice_total_pence:
        return Verdict(DENY, ["CC-6"], ["Total credits would exceed the invoice total"])
    pct_cap = invoice_total_pence * AUTO_CREDIT_MAX_PCT // 100
    if (reason_is_product_issue and requested_pence <= AUTO_CREDIT_CAP_PENCE and requested_pence <= pct_cap):
        return Verdict(AUTO, ["CC-6"], [
            f"£{requested_pence / 100:,.2f} for a product/service issue is within £{AUTO_CREDIT_CAP_PENCE / 100:,.2f} "
            f"and {AUTO_CREDIT_MAX_PCT}% of the invoice (£{pct_cap / 100:,.2f})"])
    why = []
    if not reason_is_product_issue:
        why.append("Reason is not a product/service issue")
    if requested_pence > AUTO_CREDIT_CAP_PENCE:
        why.append(f"£{requested_pence / 100:,.2f} is over the £{AUTO_CREDIT_CAP_PENCE / 100:,.2f} auto limit")
    if requested_pence > pct_cap:
        why.append(f"£{requested_pence / 100:,.2f} is over {AUTO_CREDIT_MAX_PCT}% of the invoice")
    return Verdict(NEEDS_APPROVAL, ["CC-6", "CC-8"], why)
