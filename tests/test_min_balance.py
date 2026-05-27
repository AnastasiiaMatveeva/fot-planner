"""Минимальный остаток на счёте (ручной пол)."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel_io import SHEET_MIN_BALANCE_MATRIX, create_template, load_context
from fot_planner.planner import run_planning


def _grant_workbook(path: Path, monthly_inflow: float, min_balance_jan: float = 0) -> None:
    create_template(path)
    year = date.today().year

    employees = pd.DataFrame(
        [
            {
                "id": "E001",
                "full_name": "Иванов",
                "position": "инженер",
                "department": "лаб",
                "rate": 1.0,
                "salary": 100000,
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
                "name": "Грант",
                "number": "1",
                "contract_type": "grant",
                "status": "active",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "spend_deadline": "",
                "total_fot": 1_200_000,
                "priority": 1,
                "allow_salary": True,
                "allow_incentive": True,
                "probability": 1,
                "use_after_end": False,
                "months_after_end": 0,
                "allow_monthly_carryover": True,
                "opening_balance": 0,
                "require_salary_reserve": False,
                "min_monthly_balance": "",
            }
        ]
    )
    fot_matrix = pd.DataFrame({"contract_id": ["C001"]})
    min_matrix = pd.DataFrame({"contract_id": ["C001"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [monthly_inflow]
        min_matrix[str(m)] = [min_balance_jan if m == 1 else ""]

    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)
        min_matrix.to_excel(w, sheet_name=SHEET_MIN_BALANCE_MATRIX, index=False)


def test_min_balance_matrix_loaded(tmp_path: Path):
    path = tmp_path / "input.xlsx"
    _grant_workbook(path, monthly_inflow=200_000, min_balance_jan=50_000)
    ctx = load_context(path)
    c = ctx.contracts[0]
    jan = next(mb for mb in c.monthly_budgets if mb.month == 1)
    assert jan.min_balance == 50_000


def test_min_balance_enforced(tmp_path: Path):
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    _grant_workbook(inp, monthly_inflow=200_000, min_balance_jan=80_000)

    result = run_planning(inp, out, time_limit_sec=60)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    jan = next(b for b in result.contract_balances if b.month == 1)
    assert jan.min_balance_required == pytest.approx(80_000, abs=1)
    assert jan.closing_balance >= 80_000 - 0.01


def test_contract_level_min_monthly_balance(tmp_path: Path):
    path = tmp_path / "input.xlsx"
    _grant_workbook(path, monthly_inflow=200_000)
    ctx = load_context(path)
    jan = next(mb for mb in ctx.contracts[0].monthly_budgets if mb.month == 1)
    assert jan.min_balance is None or jan.min_balance == 0
