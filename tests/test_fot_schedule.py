"""Физические поступления ФОТ по месяцам договора."""

from datetime import date

from fot_planner.fot_schedule import (
    active_months_in_year,
    default_fot_inflow_at_start,
)
from fot_planner.models import Contract


def test_default_inflow_at_contract_start():
    year = 2026
    contract = Contract(
        id="V01",
        name="ВБ",
        number="1",
        contract_type="off_budget",
        start_date=date(year, 3, 1),
        end_date=date(year, 6, 30),
        spend_deadline=None,
        total_fot=600_000,
        months_after_end=2,
        allow_monthly_carryover=True,
    )
    default_fot_inflow_at_start([contract], year)
    months = active_months_in_year(contract, year)
    assert months == [3, 4, 5, 6, 7, 8]
    assert len(contract.monthly_budgets) == 6
    assert sum(mb.inflow_amount for mb in contract.monthly_budgets) == 600_000
    assert contract.monthly_budgets[0].month == 3
    assert contract.monthly_budgets[0].inflow_amount == 600_000
    assert contract.monthly_budgets[1].inflow_amount == 0.0


def test_template_without_fot_matrix_inflow_at_start(tmp_path):
    from fot_planner.excel_io import create_template, load_context
    from fot_planner.validation import contract_allows_month

    path = tmp_path / "input.xlsx"
    create_template(path)
    ctx = load_context(path)
    c = next(x for x in ctx.contracts if x.id == "C001")
    active = [m for m in range(1, 13) if contract_allows_month(c, ctx.year, m)]
    assert active[0] == 1
    assert abs(sum(mb.inflow_amount for mb in c.monthly_budgets) - c.total_fot) < 0.01
    start_budget = next(mb for mb in c.monthly_budgets if mb.month == active[0])
    assert abs(start_budget.inflow_amount - c.total_fot) < 0.01
    assert all(mb.inflow_amount == 0 for mb in c.monthly_budgets if mb.month != active[0])
