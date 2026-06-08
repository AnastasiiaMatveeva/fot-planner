"""Штраф за смену договора надбавки и стимулирующей (как для оклада)."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel import create_template
from fot_planner.planner import run_planning


def _flex_switch_workbook(path: Path, *, switch_weight: float) -> None:
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
                "monthly_wage": 35_000,
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
                "name": "A",
                "number": "1",
                "contract_type": "minprom",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "total_fot": 420_000,
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": True,
            },
            {
                "id": "C002",
                "name": "B (альтернатива надбавки)",
                "number": "2",
                "contract_type": "minprom",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "total_fot": 1,
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": True,
            },
        ]
    )
    positions = pd.DataFrame(
        [
            {"contract_id": cid, "position": "инженер", "max_monthly_payment": 50_000}
            for cid in ("C001", "C002")
        ]
    )
    budget = pd.DataFrame(
        [
            {"contract_id": "C001", "year": year, "month": 1, "inflow_amount": 420_000},
            {"contract_id": "C002", "year": year, "month": 1, "inflow_amount": 1},
            *[
                {"contract_id": cid, "year": year, "month": m, "inflow_amount": 0}
                for cid in ("C001", "C002")
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
                "weight_uniform_spend_deviation": 0,
                "weight_admin_complexity": switch_weight,
                "weight_labor_deviation": 0,
                "max_salary_contracts_per_year": 0,
            }
        ]
    )

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        employees.to_excel(writer, sheet_name="employees", index=False)
        contracts.to_excel(writer, sheet_name="contracts", index=False)
        positions.to_excel(writer, sheet_name="contract_positions", index=False)
        budget.to_excel(writer, sheet_name="contract_monthly_budget", index=False)
        settings.to_excel(writer, sheet_name="settings", index=False)


def _allowance_contract_by_month(result, employee_id: str) -> dict[int, str]:
    by_month: dict[int, str] = {}
    for a in result.allocations:
        if (
            a.employee_id == employee_id
            and a.payment_kind == "allowance"
            and a.amount > 0.01
        ):
            by_month[a.month] = a.contract_id
    return by_month


def test_high_flex_switch_penalty_keeps_allowance_on_one_contract(tmp_path: Path):
    path = tmp_path / "flex.xlsx"
    _flex_switch_workbook(path, switch_weight=500_000)
    result = run_planning(path, tmp_path / "out.xlsx", time_limit_sec=60)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    by_month = _allowance_contract_by_month(result, "E001")
    assert len(by_month) >= 10
    assert len(set(by_month.values())) == 1
