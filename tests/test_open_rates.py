"""Открытые ставки: основное место и совместительство."""

from __future__ import annotations

import pytest

from fot_planner.open_rate_rules import (
    MAIN_QUARTERS_MAX,
    OPEN_RATE_STEP,
    PART_QUARTERS_MAX,
    max_total_quarters,
    quarters_to_rate,
    staff_rate_min_quarters,
    normalize_employment_category,
)


def test_staff_rate_min_quarters():
    assert staff_rate_min_quarters(1.0) == 4
    assert staff_rate_min_quarters(0.5) == 2
    assert staff_rate_min_quarters(0.25) == 1


def test_max_total_by_category():
    assert max_total_quarters("regular") == 6
    assert max_total_quarters("student") == 2
    assert max_total_quarters("graduate_student") == 3


def test_quarters_to_rate():
    assert quarters_to_rate(4) == pytest.approx(1.0)
    assert quarters_to_rate(2) == pytest.approx(0.5)


def test_normalize_employment_category():
    assert normalize_employment_category("студент") == "student"
    assert normalize_employment_category("аспирант") == "graduate_student"
    assert normalize_employment_category(None) == "regular"


def test_main_and_part_quarter_limits():
    assert MAIN_QUARTERS_MAX * OPEN_RATE_STEP == 1.0
    assert PART_QUARTERS_MAX * OPEN_RATE_STEP == 0.5


def test_rate_below_staff_shortfall_in_quarters():
    """Дефицит до штатной: staff_q_min - sum(q), штрафуется в оптимизаторе."""
    assert staff_rate_min_quarters(1.0) == 4
    assert staff_rate_min_quarters(1.0) - 3 == 1  # не хватает 0.25 ставки


def test_open_rates_two_contracts_integration(tmp_path):
    """С зарплатой на основном договоре и надбавкой на втором — план выполним."""
    from datetime import date
    from pathlib import Path

    import pandas as pd

    from fot_planner.excel import create_template
    from fot_planner.planner import run_planning

    path = tmp_path / "open_rate.xlsx"
    create_template(path)
    year = date.today().year
    employees = pd.DataFrame(
        [
            {
                "id": "E001",
                "full_name": "Иванов",
                "position": "инженер",
                "department": "lab",
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
                "id": "C_MAIN",
                "name": "Основной",
                "number": "1",
                "contract_type": "grant",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "total_fot": 800_000,
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": True,
            },
            {
                "id": "C_PART",
                "name": "Совместительство",
                "number": "2",
                "contract_type": "off_budget",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "total_fot": 400_000,
                "allow_salary": False,
                "allow_allowance": True,
                "allow_incentive": True,
                "allow_main_employment": False,
                "allow_part_time": True,
            },
        ]
    )
    positions = pd.DataFrame(
        [
            {
                "contract_id": "C_MAIN",
                "position": "инженер",
                "max_monthly_payment": 80_000,
                "reference_salary_for_rate": 80_000,
            }
        ]
    )
    budget = pd.DataFrame(
        [
            {"contract_id": "C_MAIN", "year": year, "month": 1, "inflow_amount": 800_000},
            {"contract_id": "C_PART", "year": year, "month": 1, "inflow_amount": 400_000},
        ]
    )
    settings = pd.DataFrame([{"year": year, "enable_open_rates": True}])
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        employees.to_excel(writer, sheet_name="employees", index=False)
        contracts.to_excel(writer, sheet_name="contracts", index=False)
        positions.to_excel(writer, sheet_name="contract_positions", index=False)
        budget.to_excel(writer, sheet_name="contract_monthly_budget", index=False)
        settings.to_excel(writer, sheet_name="settings", index=False)

    result = run_planning(path, tmp_path / "out.xlsx", time_limit_sec=120)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE"), result.conflicts
    assert result.open_rate_attributions
    jan_main = [
        r
        for r in result.open_rate_attributions
        if r.month == 1 and r.contract_id == "C_MAIN" and r.is_main
    ]
    assert jan_main
    assert jan_main[0].open_rate >= 0.25
