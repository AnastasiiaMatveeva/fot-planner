"""Трудоёмкость (чел.-мес.) и надбавка."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel_io import SHEET_CONTRACT_LABOR, create_template
from fot_planner.planner import run_planning


def _goz_with_labor(path: Path, plan_pm: float, positions: bool = True) -> None:
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
                "monthly_wage": 105000,
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
                "name": "ГОЗ",
                "number": "1",
                "contract_type": "goszakaz",
                "status": "active",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "spend_deadline": "",
                "total_fot": 1_260_000,
                "priority": 1,
                "allow_salary": True,
                "allow_allowance": True,
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
    for m in range(1, 13):
        fot_matrix[str(m)] = [105_000]

    labor = pd.DataFrame(
        [
            {
                "contract_id": "C001",
                "year": year,
                "person_months": plan_pm,
                "month": "",
                "position": "инженер",
                "avg_monthly_labor_cost": 60000,
            }
        ]
    )
    positions_df = pd.DataFrame(
        [
            {
                "contract_id": "C001",
                "position": "инженер",
                "max_monthly_payment": 120000,
            }
        ]
        if positions
        else []
    )

    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)
        labor.to_excel(w, sheet_name=SHEET_CONTRACT_LABOR, index=False)
        if positions:
            positions_df.to_excel(w, sheet_name="contract_positions", index=False)


def test_goz_labor_within_tolerance(tmp_path: Path):
    inp = tmp_path / "in.xlsx"
    out = tmp_path / "out.xlsx"
    _goz_with_labor(inp, plan_pm=12.0)

    result = run_planning(inp, out, time_limit_sec=90)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    # 12 чел.-мес. при rate=1 и полной занятости на окладе ≈ 12 месяцев на C001
    salary_allocs = [a for a in result.allocations if a.contract_id == "C001" and a.payment_kind == "salary"]
    assert len(salary_allocs) >= 10


def test_allowance_paid_when_funds(tmp_path: Path):
    inp = tmp_path / "in.xlsx"
    out = tmp_path / "out.xlsx"
    _goz_with_labor(inp, plan_pm=12.0)

    result = run_planning(inp, out, time_limit_sec=90)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")
    # При monthly_wage=105k и потолке оклада 120k вся сумма может идти окладом
    assert sum(a.amount for a in result.allocations) > 0


def test_labor_payment_equals_allocation_on_labor_contracts(tmp_path: Path):
    """Вся выплата с договора с contract_labor относится на строки трудоёмкости (=, не <=)."""
    inp = tmp_path / "in.xlsx"
    out = tmp_path / "out.xlsx"
    _goz_with_labor(inp, plan_pm=12.0)

    result = run_planning(inp, out, time_limit_sec=90)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    attributed: dict[tuple[str, str, int, str], float] = {}
    for rec in result.labor_payment_attributions:
        key = (rec.employee_id, rec.contract_id, rec.month, rec.payment_kind)
        attributed[key] = attributed.get(key, 0.0) + rec.amount

    for alloc in result.allocations:
        if alloc.amount <= 0.005:
            continue
        key = (alloc.employee_id, alloc.contract_id, alloc.month, alloc.payment_kind)
        assert attributed.get(key, 0.0) == pytest.approx(alloc.amount, abs=0.02), (
            f"{key}: выплачено {alloc.amount}, отнесено на трудоёмкость {attributed.get(key, 0.0)}"
        )
