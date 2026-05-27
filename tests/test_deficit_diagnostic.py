"""Диагностический режим дефицита: недоплата откладывается к концу года."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel_io import create_template
from fot_planner.planner import run_planning


def _workbook(
    path: Path,
    *,
    monthly_inflow: list[float],
    allow_deficit: bool,
    wage: float = 100_000,
) -> None:
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
                "monthly_wage": wage,
                "incentive": 0,
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
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
                "spend_deadline": "",
                "total_fot": sum(monthly_inflow),
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": False,
                "allow_monthly_carryover": True,
                "require_salary_reserve": False,
            }
        ]
    )
    positions = pd.DataFrame(
        [{"contract_id": "C001", "position": "инженер", "max_monthly_payment": 150_000}]
    )
    fot_matrix = pd.DataFrame({"contract_id": ["C001"]})
    for m, val in enumerate(monthly_inflow, start=1):
        fot_matrix[str(m)] = [val]

    settings = pd.DataFrame(
        [
            {
                "year": year,
                "allow_deficit": allow_deficit,
                "weight_uncovered_salary": 1_000_000_000,
            }
        ]
    )

    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        positions.to_excel(w, sheet_name="contract_positions", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)
        settings.to_excel(w, sheet_name="settings", index=False)


def test_no_deficit_when_funds_sufficient(tmp_path: Path):
    inp = tmp_path / "in.xlsx"
    out = tmp_path / "out.xlsx"
    _workbook(inp, monthly_inflow=[100_000] * 12, allow_deficit=True)

    result = run_planning(inp, out, time_limit_sec=90)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")
    assert result.deficits == []


def test_infeasible_without_deficit_mode(tmp_path: Path):
    inp = tmp_path / "in.xlsx"
    out = tmp_path / "out.xlsx"
    inflows = [100_000] * 10 + [0, 0]
    _workbook(inp, monthly_inflow=inflows, allow_deficit=False)

    result = run_planning(inp, out, time_limit_sec=90)
    assert result.solver_status == "INFEASIBLE"


def test_deficit_concentrated_in_late_months(tmp_path: Path):
    """ФОТ хватает на 10 месяцев; дефицит — в ноябре и декабре, не в январе."""
    inp = tmp_path / "in.xlsx"
    out = tmp_path / "out.xlsx"
    inflows = [100_000] * 10 + [0, 0]
    _workbook(inp, monthly_inflow=inflows, allow_deficit=True)

    result = run_planning(inp, out, time_limit_sec=120)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")
    assert result.deficits

    jan_def = sum(d.amount for d in result.deficits if d.month == 1)
    assert jan_def == pytest.approx(0, abs=1)

    late_def = sum(d.amount for d in result.deficits if d.month >= 11)
    assert late_def > 0

    nov_dec_paid = sum(a.amount for a in result.allocations if a.month >= 11)
    assert nov_dec_paid < 200_000 - 1000

    early_paid = sum(a.amount for a in result.allocations if a.month <= 10)
    assert early_paid >= 1_000_000 - 2000


def test_deficit_report_columns(tmp_path: Path):
    inp = tmp_path / "in.xlsx"
    out = tmp_path / "out.xlsx"
    inflows = [100_000] * 10 + [0, 0]
    _workbook(inp, monthly_inflow=inflows, allow_deficit=True)

    run_planning(inp, out, time_limit_sec=120)
    deficits = pd.read_excel(out, sheet_name="Дефициты")
    by_month = pd.read_excel(out, sheet_name="Дефицит по месяцам")

    assert "требовалось выплатить" in deficits.columns
    assert "Дефицит" in by_month["показатель"].values
    assert "Накопленный дефицит" in by_month["показатель"].values
