"""min_balance_for_month — порог из monthly_budgets."""

from datetime import date

from fot_planner.models import Contract, ContractMonthlyBudget
from fot_planner.fot_schedule import min_balance_for_month


def _contract(budgets: list[ContractMonthlyBudget]) -> Contract:
    return Contract(
        id="C001",
        name="T",
        number="1",
        contract_type="goszakaz",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        total_fot=1_000_000,
        monthly_budgets=budgets,
    )


def test_min_balance_for_month_returns_value():
    c = _contract(
        [
            ContractMonthlyBudget("C001", 2026, 1, 0.0, min_balance=50_000),
            ContractMonthlyBudget("C001", 2026, 2, 0.0, min_balance=None),
        ]
    )
    assert min_balance_for_month(c, 1) == 50_000
    assert min_balance_for_month(c, 2) == 0.0
    assert min_balance_for_month(c, 3) == 0.0
