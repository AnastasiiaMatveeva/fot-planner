"""Перенос остатка кассы только вперёд (carry-forward)."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel import SHEET_MIN_BALANCE_MATRIX, create_template
from fot_planner.planner import run_planning


def test_march_full_carry_to_april(tmp_path: Path):
    """Остаток марта целиком становится входящим остатком апреля."""
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    create_template(inp)
    year = date.today().year

    employees = pd.DataFrame(
        [
            {
                "id": "E001",
                "full_name": "Иванов",
                "position": "инженер",
                "department": "лаб",
                "rate": 1.0,
                "monthly_wage": 50_000,
                "start_date": f"{year}-03-01",
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
                "contract_type": "grant",
                "start_date": f"{year}-03-01",
                "end_date": f"{year}-12-31",
                "total_fot": 500_000,
                "allow_salary": True,
                "allow_incentive": False,
                "require_salary_reserve": False,
            }
        ]
    )
    fot_matrix = pd.DataFrame({"contract_id": ["C001"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [500_000 if m == 3 else 0]

    positions = pd.DataFrame(
        [{"contract_id": "C001", "position": "инженер", "max_monthly_payment": 200_000}]
    )

    with pd.ExcelWriter(inp, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        positions.to_excel(w, sheet_name="contract_positions", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)

    result = run_planning(inp, out, time_limit_sec=90)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    mar = next(b for b in result.contract_balances if b.month == 3)
    apr = next(b for b in result.contract_balances if b.month == 4)
    assert mar.carried_forward == pytest.approx(mar.closing_balance, abs=1)
    assert apr.opening_balance == pytest.approx(mar.closing_balance, abs=1)
    assert not result.month_transfers


def test_min_balance_requires_closing_floor(tmp_path: Path):
    """min_balance_matrix — минимальный остаток на конец месяца."""
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    create_template(inp)
    year = date.today().year

    employees = pd.DataFrame(
        [
            {
                "id": "E001",
                "full_name": "Иванов",
                "position": "инженер",
                "department": "лаб",
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
                "name": "Грант",
                "contract_type": "grant",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "total_fot": 1_200_000,
                "allow_salary": True,
                "allow_incentive": False,
                "require_salary_reserve": False,
            }
        ]
    )
    fot_matrix = pd.DataFrame({"contract_id": ["C001"]})
    min_matrix = pd.DataFrame({"contract_id": ["C001"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [200_000]
        min_matrix[str(m)] = [80_000 if m == 1 else ""]

    with pd.ExcelWriter(inp, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)
        min_matrix.to_excel(w, sheet_name=SHEET_MIN_BALANCE_MATRIX, index=False)

    result = run_planning(inp, out, time_limit_sec=60)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    jan = next(b for b in result.contract_balances if b.month == 1)
    assert jan.min_balance_required == pytest.approx(80_000, abs=1)
    assert jan.closing_balance >= 80_000 - 0.01
