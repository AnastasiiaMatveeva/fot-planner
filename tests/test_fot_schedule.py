"""Авто-разнесение годового ФОТ по месяцам договора."""

from datetime import date

from fot_planner.excel_io import load_context
from fot_planner.fot_schedule import active_months_in_year, spread_fot_by_active_months
from fot_planner.models import Contract
from fot_planner.validation import contract_allows_month


def test_spread_fot_evenly_over_active_months():
    year = 2026
    contract = Contract(
        id="V01",
        name="ВБ",
        number="1",
        contract_type="off_budget",
        start_date=date(year, 1, 1),
        end_date=date(year, 6, 30),
        spend_deadline=None,
        total_fot=600_000,
        months_after_end=2,
        allow_monthly_carryover=True,
    )
    spread_fot_by_active_months([contract], year)
    months = active_months_in_year(contract, year)
    assert months == [1, 2, 3, 4, 5, 6, 7, 8]
    assert len(contract.monthly_budgets) == 8
    assert sum(mb.inflow_amount for mb in contract.monthly_budgets) == 600_000
    assert contract.monthly_budgets[0].inflow_amount == 75_000


def test_template_without_fot_matrix_uses_auto_spread(tmp_path):
    from fot_planner.excel_io import create_template

    path = tmp_path / "input.xlsx"
    create_template(path)
    ctx = load_context(path)
    c = next(x for x in ctx.contracts if x.id == "C001")
    active = [m for m in range(1, 13) if contract_allows_month(c, ctx.year, m)]
    assert len(active) == 12
    assert abs(sum(mb.inflow_amount for mb in c.monthly_budgets) - c.total_fot) < 0.01
