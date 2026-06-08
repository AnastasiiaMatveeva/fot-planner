"""Штраф за отклонение фактического освоения от равномерного плана по договору."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel import create_template
from fot_planner.fot_schedule import monthly_spend_targets
from fot_planner.planner import run_planning


def _uniform_spend_workbook(path: Path, *, uniform_weight: float) -> None:
    create_template(path)
    year = date.today().year

    employees = pd.DataFrame(
        [
            {
                "id": "E001",
                "full_name": "Тест",
                "position": "инженер",
                "department": "отдел",
                "rate": 1.0,
                "monthly_wage": 100_000,
                "start_date": f"{year}-01-01",
                "end_date": "",
                "allowed_contracts": "",
                "forbidden_contracts": "",
            }
        ]
    )
    contracts = pd.DataFrame(
        [
            {
                "id": "C001",
                "name": "Проект",
                "number": "1",
                "contract_type": "minprom",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "total_fot": 1_200_000,
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": True,
            }
        ]
    )
    positions = pd.DataFrame(
        [{"contract_id": "C001", "position": "инженер", "max_monthly_payment": 150_000}]
    )
    budget = pd.DataFrame(
        [
            {"contract_id": "C001", "year": year, "month": 1, "inflow_amount": 1_200_000},
            *[
                {"contract_id": "C001", "year": year, "month": m, "inflow_amount": 0}
                for m in range(2, 13)
            ],
        ]
    )
    settings = pd.DataFrame(
        [
            {
                "year": year,
                "weight_deficit_amount": 1_000_000,
                "weight_salary_switch": 0,
                "weight_uniform_spend_deviation": uniform_weight,
                "weight_labor_deviation": 0,
                "max_salary_contracts_per_year": 1,
            }
        ]
    )

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        employees.to_excel(writer, sheet_name="employees", index=False)
        contracts.to_excel(writer, sheet_name="contracts", index=False)
        positions.to_excel(writer, sheet_name="contract_positions", index=False)
        budget.to_excel(writer, sheet_name="contract_monthly_budget", index=False)
        settings.to_excel(writer, sheet_name="settings", index=False)


def test_monthly_spend_targets_divides_single_inflow_over_active_months():
    from fot_planner.excel import load_context

    path = Path(__file__).parent / "_tmp_uniform_target.xlsx"
    _uniform_spend_workbook(path, uniform_weight=50_000)
    ctx = load_context(path)
    c = ctx.contracts[0]
    targets = monthly_spend_targets(c, ctx.year)
    assert targets[1] == pytest.approx(100_000)
    assert all(targets[m] == pytest.approx(100_000) for m in range(1, 13))
    assert sum(targets.values()) == pytest.approx(1_200_000)


def test_high_uniform_penalty_spreads_spend(tmp_path: Path):
    path = tmp_path / "uniform.xlsx"
    _uniform_spend_workbook(path, uniform_weight=500_000)
    result = run_planning(path, tmp_path / "out.xlsx", time_limit_sec=60)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    by_month = {
        m: sum(a.amount for a in result.allocations if a.contract_id == "C001" and a.month == m)
        for m in range(1, 13)
    }
    active = [m for m, s in by_month.items() if s > 0]
    assert len(active) >= 10
    values = [by_month[m] for m in active]
    assert max(values) - min(values) <= 100_000 + 1_000


def test_uniform_penalty_prefers_single_contract_over_micro_split(tmp_path: Path):
    """При достаточном ФОТ на одном договоре не дробим надбавку ради микровыравнивания."""
    from tests.test_contract_fragment_penalty import _fragment_workbook

    path = tmp_path / "no_micro_split.xlsx"
    _fragment_workbook(path, admin_complexity_weight=200_000, uniform_weight=100_000)
    result = run_planning(path, tmp_path / "out.xlsx", time_limit_sec=60)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")
    april = [
        a
        for a in result.allocations
        if a.employee_id == "E001"
        and a.month == 4
        and a.payment_kind == "allowance"
        and a.amount > 0.01
    ]
    assert sum(a.amount for a in april) == pytest.approx(25_000, rel=0.01)
    assert len({a.contract_id for a in april}) == 1
    assert all(a.amount >= 24_000 for a in april)
