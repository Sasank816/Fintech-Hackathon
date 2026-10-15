"""UK late-payment interest maths (law.py). Golden numbers from the shared contract."""
from datetime import date, timedelta
from decimal import Decimal

from clause import law

DUE = date(2026, 10, 1)
TODAY = DUE + timedelta(days=15)  # 45 days after issue on net 30


def test_acme_statutory_9318():
    c = law.late_claim(480000, DUE, TODAY, None)
    assert c.interest_pence == 2318 and c.compensation_pence == 7000 and c.total_pence == 9318


def test_northwind_contractual_3981_no_compensation():
    c = law.late_claim(1250000, DUE, TODAY, Decimal("4.0"))
    assert c.total_pence == 3981 and c.compensation_pence == 0


def test_bright_4174():
    assert law.late_claim(36100, DUE, TODAY, None).total_pence == 4174


def test_compensation_bands():
    comp = lambda p: law.late_claim(p, DUE, TODAY, None).compensation_pence
    assert comp(99999) == 4000          # < £1,000
    assert comp(100000) == 7000         # £1,000 - £9,999.99
    assert comp(999999) == 7000
    assert comp(1000000) == 10000       # >= £10,000


def test_contractual_rate_means_no_compensation():
    assert law.late_claim(50000, DUE, TODAY, Decimal("4.0")).compensation_pence == 0
