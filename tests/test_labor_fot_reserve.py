"""Резерв ФОТ под оставшуюся трудоёмкость (оклад = человеко-месяц)."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel_io import create_template
from fot_planner.planner import run_planning
from fot_planner.spend_plan import build_spend_plan_fact_dataframe


def _gos_early_spend_workbook(path: Path) -> None:
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
                "salary": 150_000,
                "allowance": 50_000,
                "incentive": 0,
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
                "id": "C_GOS",
                "name": "ГОЗ",
                "number": "1",
                "contract_type": "goszakaz",
                "start_date": f"{year}-03-01",
                "end_date": f"{year}-12-31",
                "spend_deadline": "",
                "total_fot": 1_500_000,
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": False,
                "months_after_end": 0,
                "allow_monthly_carryover": True,
                "require_salary_reserve": False,
            }
        ]
    )
    fot_matrix = pd.DataFrame({"contract_id": ["C_GOS"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [1_500_000 if m == 3 else 0]

    positions = pd.DataFrame(
        [{"contract_id": "C_GOS", "position": "инженер", "max_monthly_payment": 250_000}]
    )
    labor = pd.DataFrame(
        [
            {
                "contract_id": "C_GOS",
                "year": year,
                "person_months": 10,
                "position": "инженер",
            }
        ]
    )
    settings = pd.DataFrame(
        [
            {
                "year": year,
                "weight_uncovered_salary": 1_000_000,
                "weight_uniform_spend_deviation": 0,
                "max_salary_contracts_per_year": 1,
                "goz_labor_tolerance": 0.01,
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


def test_july_cumulative_spend_capped_by_labor_reserve(tmp_path: Path):
    """При 7 чел.-мес. к июлю нельзя освоить весь ФОТ 1.5M (резерв 450k)."""
    inp = tmp_path / "in.xlsx"
    out = tmp_path / "out.xlsx"
    _gos_early_spend_workbook(inp)

    result = run_planning(inp, out, time_limit_sec=120)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    from fot_planner.excel_io import load_context

    ctx = load_context(inp)
    report = build_spend_plan_fact_dataframe(ctx, result)
    gos = report[report["договор"] == "C_GOS"]
    active = gos[gos["активный месяц"] == "да"]

    assert (active["порог выполнен"] == "да").all()

    july = active[active["месяц"] == 7].iloc[0]
    assert july["накопленный факт"] <= 1_050_000 + 500

    spent_total = sum(a.amount for a in result.allocations if a.contract_id == "C_GOS")
    assert spent_total >= 1_500_000 - 2000


def test_allowance_heavy_q1_infeasible_without_labor(tmp_path: Path):
    """Ранний расход без оклада не закрывает трудоёмкость — модель режет или INFEASIBLE."""
    inp = tmp_path / "in.xlsx"
    out = tmp_path / "out.xlsx"
    _gos_early_spend_workbook(inp)

    year = date.today().year
    manual = pd.DataFrame(
        [
            {
                "employee_id": "E001",
                "contract_id": "C_GOS",
                "year": year,
                "month": 3,
                "payment_kind": "allowance",
                "amount": 400_000,
            },
            {
                "employee_id": "E001",
                "contract_id": "C_GOS",
                "year": year,
                "month": 4,
                "payment_kind": "allowance",
                "amount": 400_000,
            },
            {
                "employee_id": "E001",
                "contract_id": "C_GOS",
                "year": year,
                "month": 5,
                "payment_kind": "allowance",
                "amount": 400_000,
            },
        ]
    )
    with pd.ExcelWriter(inp, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        manual.to_excel(w, sheet_name="manual_assignments", index=False)

    result = run_planning(inp, out, time_limit_sec=120)
    if result.solver_status in ("OPTIMAL", "FEASIBLE"):
        mar_may = sum(
            a.amount
            for a in result.allocations
            if a.contract_id == "C_GOS" and a.month in (3, 4, 5)
        )
        assert mar_may <= 1_200_000 + 500
    else:
        assert result.solver_status == "INFEASIBLE"
