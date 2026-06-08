"""Сроки выплат по виду и requires_full_fot_spend."""

from datetime import date

from fot_planner.contract_calendar import (
    contract_allows_payment_month,
    payment_deadline_date,
    payment_month_count,
)
from fot_planner.models import Contract, PaymentKindTerms


def _contract(**kwargs) -> Contract:
    defaults = dict(
        id="T1",
        name="",
        number="",
        contract_type="grant",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 6, 30),
        total_fot=1_000_000,
        salary_terms=PaymentKindTerms(payment_deadline=date(2026, 6, 30)),
        allowance_terms=PaymentKindTerms(payment_deadline=date(2026, 6, 30)),
        incentive_terms=PaymentKindTerms(payment_deadline=date(2026, 6, 30)),
    )
    defaults.update(kwargs)
    return Contract(**defaults)


def test_payment_deadline_explicit_per_kind():
    c = _contract(
        salary_terms=PaymentKindTerms(payment_deadline=date(2026, 11, 1)),
        allowance_terms=PaymentKindTerms(payment_deadline=date(2027, 2, 2)),
    )
    assert payment_deadline_date(c, "salary") == date(2026, 11, 1)
    assert payment_deadline_date(c, "allowance") == date(2027, 2, 2)


def test_salary_stops_after_deadline_month():
    c = _contract(
        salary_terms=PaymentKindTerms(payment_deadline=date(2026, 6, 30)),
        allowance_terms=PaymentKindTerms(payment_deadline=date(2026, 8, 31)),
    )
    year = 2026
    assert contract_allows_payment_month(c, year, 6, "salary")
    assert not contract_allows_payment_month(c, year, 7, "salary")
    assert contract_allows_payment_month(c, year, 7, "allowance")
    assert payment_month_count(c, year, "allowance") == 8


def test_requires_full_fot_spend_when_total_fot_positive():
    assert _contract(total_fot=100).requires_full_fot_spend is True
    assert _contract(total_fot=0).requires_full_fot_spend is False
