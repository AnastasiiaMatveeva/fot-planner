"""Полное освоение ФОТ к сроку."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel_io import create_template
from fot_planner.planner import run_planning
from fot_planner.spend_rules import required_full_spend_month


def _small_off_budget_workbook(path: Path) -> None:
    create_template(path)
    year = date.today().year
    end = date(year, 6, 30)
    spend_deadline = date(year, 8, 31)

    employees = pd.DataFrame(
        [
            {
                "id": "25586",
                "full_name": "Петров",
                "position": "инженер",
                "department": "лаб",
                "rate": 1.0,
                "salary": 90_000,
                "allowance": 8_000,
                "incentive": 12_000,
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
                "id": "V01",
                "name": "Внебюджет 1",
                "number": "1",
                "contract_type": "off_budget",
                "status": "active",
                "start_date": f"{year}-01-01",
                "end_date": end.isoformat(),
                "spend_deadline": spend_deadline.isoformat(),
                "total_fot": 600_000,
                "priority": 1,
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": True,
                "probability": 1,
                "use_after_end": True,
                "months_after_end": 2,
                "allow_monthly_carryover": True,
            }
        ]
    )
    fot_matrix = pd.DataFrame({"contract_id": ["V01"]})
    for m in range(1, 9):
        fot_matrix[str(m)] = [75_000 if m <= 6 else 100_000]
    for m in range(9, 13):
        fot_matrix[str(m)] = [0]

    positions = pd.DataFrame(
        [
            {
                "contract_id": "V01",
                "position": "инженер",
                "max_monthly_payment": 100_000,
            }
        ]
    )
    labor = pd.DataFrame(
        [
            {
                "contract_id": "V01",
                "year": year,
                "person_months": 4,
                "month": "",
                "position": "инженер",
            }
        ]
    )
    settings = pd.DataFrame(
        [
            {
                "year": year,
                "max_salary_contracts_per_year": 2,
            }
        ]
    )

    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)
        positions.to_excel(w, sheet_name="contract_positions", index=False)
        labor.to_excel(w, sheet_name="contract_labor", index=False)
        settings.to_excel(w, sheet_name="settings", index=False)


def test_off_budget_spent_by_extended_deadline(tmp_path: Path):
    inp = tmp_path / "in.xlsx"
    out = tmp_path / "out.xlsx"
    _small_off_budget_workbook(inp)

    result = run_planning(inp, out, time_limit_sec=90)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    spent = sum(a.amount for a in result.allocations if a.contract_id == "V01")
    assert spent >= 600_000 - 1000
