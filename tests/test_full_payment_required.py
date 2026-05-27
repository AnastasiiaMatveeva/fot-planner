"""Полная выплата сотрудникам с договоров — жёсткое ограничение, без дефицита."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel_io import create_template
from fot_planner.planner import run_planning


def test_employee_paid_in_full_from_contracts(tmp_path: Path):
    path = tmp_path / "full_pay.xlsx"
    create_template(path)
    year = date.today().year
    employees = pd.DataFrame(
        [
            {
                "id": "E001",
                "full_name": "Иванов",
                "position": "инженер",
                "department": "отдел",
                "rate": 1.0,
                "salary": 100_000,
                "allowance": 30_000,
                "incentive": 0,
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
                "contract_type": "grant",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "spend_deadline": "",
                "total_fot": 1_560_000,
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": True,
                "months_after_end": 0,
            }
        ]
    )
    positions = pd.DataFrame(
        [{"contract_id": "C001", "position": "инженер", "max_monthly_payment": 150_000}]
    )
    budget = pd.DataFrame(
        [
            {"contract_id": "C001", "year": year, "month": 1, "inflow_amount": 1_560_000},
            *[
                {"contract_id": "C001", "year": year, "month": m, "inflow_amount": 0}
                for m in range(2, 13)
            ],
        ]
    )
    settings = pd.DataFrame([{"year": year, "max_salary_contracts_per_year": 0}])
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        employees.to_excel(writer, sheet_name="employees", index=False)
        contracts.to_excel(writer, sheet_name="contracts", index=False)
        positions.to_excel(writer, sheet_name="contract_positions", index=False)
        budget.to_excel(writer, sheet_name="contract_monthly_budget", index=False)
        settings.to_excel(writer, sheet_name="settings", index=False)

    result = run_planning(path, tmp_path / "out.xlsx", time_limit_sec=60)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")
    assert result.deficits == []
    jan = sum(a.amount for a in result.allocations if a.month == 1)
    assert jan == pytest.approx(130_000, rel=0.01)
