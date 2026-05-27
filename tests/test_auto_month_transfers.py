"""Автопереносы: неподвижные только для месяцев ДО текущего."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel_io import (
    SHEET_CONTRACT_BUDGET,
    SHEET_MIN_BALANCE_MATRIX,
    create_template,
)
from fot_planner.planner import run_planning


def _zero_budget(contract_id: str, year: int) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "contract_id": contract_id,
                "year": year,
                "month": m,
                "inflow_amount": 0,
                "lock": "",
            }
            for m in range(1, 13)
        ]
    )


def _workbook(path: Path, jan_inflow: float, mar_inflow: float, min_bal: float) -> None:
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
                "incentive": 200000,
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
                "number": "1",
                "contract_type": "grant",
                "status": "active",
                "start_date": f"{year}-03-01",
                "end_date": f"{year}-12-31",
                "spend_deadline": "",
                "total_fot": 3_150_000,
                "priority": 1,
                "allow_salary": True,
                "allow_incentive": True,
                "probability": 1,
                "use_after_end": False,
                "months_after_end": 0,
                "allow_monthly_carryover": True,
                "require_salary_reserve": False,
            }
        ]
    )
    fot_matrix = pd.DataFrame({"contract_id": ["C001"]})
    min_matrix = pd.DataFrame({"contract_id": ["C001"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [jan_inflow if m == 1 else mar_inflow if m == 3 else 250_000]
        min_matrix[str(m)] = [min_bal if m == 3 else ""]

    positions = pd.DataFrame(
        [{"contract_id": "C001", "position": "инженер", "max_monthly_payment": 500_000}]
    )
    settings = pd.DataFrame(
        [
            {
                "year": year,
                "weight_uncovered_salary": 1_000_000,
                "max_salary_contracts_per_year": 2,
                "min_fot_months_for_salary_reserve": 12,
                "allow_backward_reallocation": True,
            }
        ]
    )
    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        positions.to_excel(w, sheet_name="contract_positions", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)
        min_matrix.to_excel(w, sheet_name=SHEET_MIN_BALANCE_MATRIX, index=False)
        settings.to_excel(w, sheet_name="settings", index=False)


def test_system_transfers_from_later_month_to_january(tmp_path: Path):
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    _workbook(inp, jan_inflow=50_000, mar_inflow=600_000, min_bal=200_000)

    result = run_planning(inp, out, time_limit_sec=90)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    to_jan = [t for t in result.month_transfers if t.to_month == 1 and t.amount > 1000]
    assert to_jan, "ожидался автоперенос на январь"

    mar = next(b for b in result.contract_balances if b.month == 3)
    assert mar.transfer_out <= mar.movable_balance + 1
    expected_cap = mar.opening_balance + mar.inflow + mar.transfer_in - mar.spent - 200_000
    assert mar.movable_balance == pytest.approx(max(0.0, expected_cap), abs=1)


def test_march_full_carry_to_april(tmp_path: Path):
    """В апрель уходит весь остаток марта, не «остаток − 200k»."""
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    _workbook(inp, jan_inflow=200_000, mar_inflow=500_000, min_bal=200_000)

    result = run_planning(inp, out, time_limit_sec=90)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    mar = next(b for b in result.contract_balances if b.month == 3)
    apr = next(b for b in result.contract_balances if b.month == 4)
    assert mar.carried_forward == pytest.approx(mar.closing_balance, abs=1)
    assert apr.opening_balance == pytest.approx(mar.closing_balance, abs=1)


def test_locked_amount_limits_backward_only_not_closing_floor(tmp_path: Path):
    """Неподвижные 200k — только лимит переноса назад; остаток на конец месяца может быть выше 200k."""
    path = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
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
                "number": "1",
                "contract_type": "grant",
                "status": "active",
                "start_date": f"{year}-03-01",
                "end_date": f"{year}-12-31",
                "spend_deadline": "",
                "total_fot": 150_000,
                "priority": 1,
                "allow_salary": True,
                "allow_incentive": True,
                "probability": 1,
                "use_after_end": False,
                "months_after_end": 0,
                "allow_monthly_carryover": False,
                "require_salary_reserve": False,
            }
        ]
    )
    fot_matrix = pd.DataFrame({"contract_id": ["C001"]})
    min_matrix = pd.DataFrame({"contract_id": ["C001"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [250_000 if m == 3 else 0]
        min_matrix[str(m)] = [200_000 if m == 3 else 0]

    positions = pd.DataFrame(
        [{"contract_id": "C001", "position": "инженер", "max_monthly_payment": 500_000}]
    )
    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        positions.to_excel(w, sheet_name="contract_positions", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)
        min_matrix.to_excel(w, sheet_name=SHEET_MIN_BALANCE_MATRIX, index=False)

    result = run_planning(path, out, time_limit_sec=90)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    mar = next(b for b in result.contract_balances if b.month == 3)
    assert mar.spent >= 90_000
    assert mar.closing_balance < mar.min_balance_required - 1
    assert mar.movable_balance == pytest.approx(
        max(0.0, mar.opening_balance + mar.inflow - mar.spent - mar.min_balance_required),
        abs=1,
    )
