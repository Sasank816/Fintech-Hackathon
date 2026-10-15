"""UK late-payment law as the agent applies it.

Source: Late Payment of Commercial Debts (Interest) Act 1998, as summarised by GOV.UK
(https://www.gov.uk/late-commercial-payments-interest-debt-recovery), checked 15 Oct 2026.

- A payment with no agreed date is late 30 days after the customer receives the invoice
  (or after delivery, if later). Agreed B2B terms are usually at most 60 days.
- Statutory interest is 8% plus the Bank of England reference rate, simple, accrued daily.
- Statutory interest cannot be claimed if the contract sets a different interest rate.
- Fixed-sum compensation: under £1,000 -> £40; £1,000 to £9,999.99 -> £70; £10,000+ -> £100.
- The reference rate is the Bank Rate in force on 31 December (for Jan-Jun) or 30 June
  (for Jul-Dec).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

STATUTORY_UPLIFT_PCT = Decimal("8")
DEFAULT_TERMS_DAYS = 30
MAX_FAIR_TERMS_DAYS = 60

# Bank Rate on the reference day for each half-year. Bank Rate has been 3.75% since the
# cut on 18 Dec 2025 (held 30 Jul and 17 Sep 2026). Update this table when it changes.
REFERENCE_RATES: dict[tuple[int, int], Decimal] = {
    (2026, 1): Decimal("3.75"),  # Bank Rate on 31 Dec 2025
    (2026, 2): Decimal("3.75"),  # Bank Rate on 30 Jun 2026
}
REFERENCE_RATE_SOURCE = "Bank of England Bank Rate 3.75% (in force since 18 Dec 2025)"


class UnknownReferenceRate(LookupError):
    """Raised when the reference-rate table has no entry for a date."""


def half_year(on: date) -> tuple[int, int]:
    return (on.year, 1 if on.month <= 6 else 2)


def reference_rate(on: date) -> Decimal:
    try:
        return REFERENCE_RATES[half_year(on)]
    except KeyError as exc:
        raise UnknownReferenceRate(f"No Bank of England reference rate recorded for {half_year(on)}") from exc


def statutory_rate(on: date) -> Decimal:
    """Annual statutory rate in percent for a debt that became late on `on`."""
    return STATUTORY_UPLIFT_PCT + reference_rate(on)


def compensation_pence(debt_pence: int) -> int:
    """Fixed sum for recovery costs, set by the Act (s.5A)."""
    if debt_pence < 0:
        raise ValueError("debt cannot be negative")
    if debt_pence < 100_000:
        return 4_000
    if debt_pence < 1_000_000:
        return 7_000
    return 10_000


def simple_interest_pence(debt_pence: int, annual_rate_pct: Decimal, days_late: int) -> int:
    """debt x rate / 365 x days, rounded half-up to the penny (GOV.UK method)."""
    if days_late <= 0 or debt_pence <= 0:
        return 0
    pounds = Decimal(debt_pence) / 100
    interest = pounds * (annual_rate_pct / 100) / Decimal(365) * Decimal(days_late)
    return int((interest.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100).to_integral_value())


def due_date(invoice_received: date, terms_days: int | None, delivered: date | None = None) -> date:
    """Due date: agreed terms from receipt; with no agreed terms, 30 days from the later of
    receipt and delivery."""
    if terms_days:
        return invoice_received + timedelta(days=terms_days)
    start = max(invoice_received, delivered or invoice_received)
    return start + timedelta(days=DEFAULT_TERMS_DAYS)


@dataclass(frozen=True)
class LateClaim:
    """What the supplier may lawfully add to an overdue debt."""

    basis: str  # "statutory" | "contractual"
    debt_pence: int
    days_late: int
    annual_rate_pct: Decimal
    interest_pence: int
    compensation_pence: int
    rate_explanation: str

    @property
    def total_pence(self) -> int:
        return self.interest_pence + self.compensation_pence

    def formula(self) -> str:
        line = (
            f"£{self.debt_pence / 100:,.2f} × {self.annual_rate_pct}% ÷ 365 × {self.days_late} days"
            f" = £{self.interest_pence / 100:,.2f} interest"
        )
        if self.compensation_pence:
            line += f"; + £{self.compensation_pence / 100:,.0f} fixed compensation (debt band)"
        return line


def late_claim(
    debt_pence: int,
    became_late_on: date,
    today: date,
    contractual_rate_over_base_pct: Decimal | None,
) -> LateClaim:
    """Interest (and compensation, when statutory) owed on `today` for a debt late since
    `became_late_on`. A contractual rate displaces the statutory scheme entirely."""
    days_late = max(0, (today - became_late_on).days)
    base = reference_rate(became_late_on)
    if contractual_rate_over_base_pct is not None:
        rate = base + contractual_rate_over_base_pct
        return LateClaim(
            basis="contractual",
            debt_pence=debt_pence,
            days_late=days_late,
            annual_rate_pct=rate,
            interest_pence=simple_interest_pence(debt_pence, rate, days_late),
            compensation_pence=0,
            rate_explanation=(
                f"Contract rate: {contractual_rate_over_base_pct}% over base ({base}%) = {rate}%. "
                "A contractual rate replaces statutory interest, so no statutory compensation."
            ),
        )
    rate = STATUTORY_UPLIFT_PCT + base
    return LateClaim(
        basis="statutory",
        debt_pence=debt_pence,
        days_late=days_late,
        annual_rate_pct=rate,
        interest_pence=simple_interest_pence(debt_pence, rate, days_late),
        compensation_pence=compensation_pence(debt_pence),
        rate_explanation=(
            f"Statutory rate: 8% + reference rate {base}% ({REFERENCE_RATE_SOURCE}) = {rate}%. "
            "Late Payment of Commercial Debts (Interest) Act 1998."
        ),
    )
