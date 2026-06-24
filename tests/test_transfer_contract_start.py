"""Физическая касса: поступления только вперёд, без переноса из будущего."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel import create_template, load_context
from fot_planner.planner import run_planning
from fot_planner.validation import contract_allows_month


def _goszakaz_march_start_workbook(path: Path) -> None:
    create_template(path)
    year = date.today().year

    employees = pd.DataFrame(
        [
            {
                "id": "E001",
                "full_name": "Иванов И.И.",
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
                "id": "C_GOS",
                "name": "ГОЗ контрольный",
                "number": "1",
                "contract_type": "goszakaz",
                "start_date": f"{year}-03-01",
                "end_date": f"{year}-12-30",
                "total_fot": 1_000_000,
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": True,
            }
        ]
    )
    fot_matrix = pd.DataFrame({"contract_id": ["C_GOS"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [1_000_000 if m == 12 else 0]

    positions = pd.DataFrame(
        [{"contract_id": "C_GOS", "position": "инженер", "max_monthly_payment": 100_000}]
    )
    labor = pd.DataFrame(
        [
            {
                "contract_id": "C_GOS",
                "year": year,
                "person_months": 10,
                "position": "инженер",
                "avg_monthly_labor_cost": 100_000,
            }
        ]
    )
    settings = pd.DataFrame(
        [
            {
                "year": year,
                "weight_deficit_amount": 1_000_000,
                "max_salary_contracts_per_year": 2,
                "goz_labor_tolerance": 0.05,
            }
        ]
    )

    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        positions.to_excel(w, sheet_name="contract_positions", index=False)
        labor.to_excel(w, sheet_name="contract_labor", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)
        settings.to_excel(w, sheet_name="settings", index=False)


def test_contract_active_months_start_in_march(tmp_path: Path):
    inp = tmp_path / "input.xlsx"
    _goszakaz_march_start_workbook(inp)
    year = date.today().year

    ctx = load_context(inp)
    c = next(x for x in ctx.contracts if x.id == "C_GOS")
    assert not contract_allows_month(c, year, 1)
    assert not contract_allows_month(c, year, 2)
    assert contract_allows_month(c, year, 3)


def test_december_inflow_cannot_pay_before_december(tmp_path: Path):
    """Деньги в декабре — выплаты март–ноябрь без других источников невозможны."""
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "out.xlsx"
    _goszakaz_march_start_workbook(inp)

    result = run_planning(inp, out, time_limit_sec=120)
    assert result.solver_status == "INFEASIBLE"
    assert not result.allocations


def test_march_inflow_carries_forward(tmp_path: Path):
    """Поступление в марте переносится вперёд как остаток кассы."""
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "out.xlsx"
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
                "monthly_wage": 150_000,
                "start_date": f"{year}-03-01",
                "end_date": f"{year}-12-31",
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
                "total_fot": 1_500_000,
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": False,
            }
        ]
    )
    fot_matrix = pd.DataFrame({"contract_id": ["C001"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [1_500_000 if m == 3 else 0]
    positions = pd.DataFrame(
        [{"contract_id": "C001", "position": "инженер", "max_monthly_payment": 350_000}]
    )

    with pd.ExcelWriter(inp, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        positions.to_excel(w, sheet_name="contract_positions", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)

    result = run_planning(inp, out, time_limit_sec=120)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    mar = next(b for b in result.contract_balances if b.contract_id == "C001" and b.month == 3)
    apr = next(b for b in result.contract_balances if b.contract_id == "C001" and b.month == 4)

    assert mar.inflow == pytest.approx(1_500_000, abs=1)
    assert mar.spent == pytest.approx(150_000, abs=500)
    assert mar.closing_balance == pytest.approx(1_350_000, abs=500)
    assert apr.opening_balance == pytest.approx(1_350_000, abs=500)
    assert apr.spent == pytest.approx(150_000, abs=500)
    assert apr.closing_balance == pytest.approx(1_200_000, abs=500)
