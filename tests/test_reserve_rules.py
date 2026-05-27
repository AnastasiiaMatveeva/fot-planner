"""Правила резерва по числу месяцев с ФОТ."""

from datetime import date

from fot_planner.models import Contract, ContractMonthlyBudget
from fot_planner.reserve_rules import (
    fot_inflow_month_count,
    is_long_fot_project,
    requires_salary_reserve,
)


def _contract(months_with_inflow: list[int]) -> Contract:
    budgets = [
        ContractMonthlyBudget(
            contract_id="C001",
            year=2026,
            month=m,
            inflow_amount=100_000,
        )
        for m in months_with_inflow
    ]
    return Contract(
        id="C001",
        name="T",
        number="1",
        contract_type="goszakaz",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        spend_deadline=None,
        total_fot=1_000_000,
        monthly_budgets=budgets,
    )


def test_fot_month_count():
    assert fot_inflow_month_count(_contract([1, 2, 3, 4, 5, 6, 7])) == 7


def test_long_fot_project_default_threshold():
    assert is_long_fot_project(_contract(list(range(1, 8))), 6) is True
    assert is_long_fot_project(_contract(list(range(1, 7))), 6) is False


def test_requires_reserve_by_fot_months_not_type():
    long_gos = _contract(list(range(1, 13)))
    short_gos = _contract([1, 2, 3])
    assert requires_salary_reserve(long_gos, min_fot_months=6) is True
    assert requires_salary_reserve(short_gos, min_fot_months=6) is False


def test_contract_override_disables_auto_reserve():
    c = _contract(list(range(1, 13)))
    c.require_salary_reserve = False
    assert requires_salary_reserve(c, min_fot_months=6) is False
