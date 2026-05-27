"""Расчёт срока полного освоения ФОТ."""

from datetime import date

from fot_planner.models import Contract
from fot_planner.spend_rules import (
    must_fully_spend_fot,
    required_full_spend_month,
    spend_deadline_date,
)


def _contract(**kwargs) -> Contract:
    defaults = dict(
        id="T1",
        name="",
        number="",
        contract_type="grant",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 6, 30),
        spend_deadline=None,
        total_fot=1_000_000,
        months_after_end=0,
        allow_monthly_carryover=True,
    )
    defaults.update(kwargs)
    return Contract(**defaults)


def test_spend_deadline_explicit_overrides():
    c = _contract(spend_deadline=date(2026, 5, 15), months_after_end=2)
    assert spend_deadline_date(c) == date(2026, 5, 15)


def test_spend_deadline_goz_20_days_before_end():
    c = _contract(
        contract_type="goszakaz",
        end_date=date(2026, 6, 30),
        months_after_end=0,
        spend_complete_days_before_end=20,
    )
    assert spend_deadline_date(c) == date(2026, 6, 10)


def test_spend_deadline_off_budget_plus_two_months():
    c = _contract(
        contract_type="off_budget",
        end_date=date(2026, 6, 30),
        months_after_end=2,
        spend_complete_days_before_end=None,
    )
    assert spend_deadline_date(c) == date(2026, 8, 28)


def test_spend_deadline_grant_end_date():
    c = _contract(months_after_end=0, spend_complete_days_before_end=None)
    assert spend_deadline_date(c) == date(2026, 6, 30)


def test_must_fully_spend_when_total_fot_positive():
    assert must_fully_spend_fot(_contract(total_fot=100)) is True
    assert must_fully_spend_fot(_contract(total_fot=0)) is False


def test_required_full_spend_month_from_deadline():
    c = _contract(end_date=date(2026, 8, 31), months_after_end=0)
    assert required_full_spend_month(c, 2026) == 8
