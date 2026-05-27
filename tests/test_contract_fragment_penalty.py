"""Штраф за лишние договоры и дробление надбавок/стимулирующих."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel_io import create_template
from fot_planner.planner import run_planning


def _fragment_workbook(
    path: Path,
    *,
    uniform_weight: float = 10_000,
    flex_fragment_weight: float = 200_000,
    admin_complexity_weight: float = 0,
    short_total_fot: float = 25_000,
    short_inflow_april: float = 25_000,
    long_total_fot: float = 275_000,
) -> None:
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
                "salary": 0,
                "allowance": 25_000,
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
                "id": "C_SHORT",
                "name": "Краткосрочный",
                "number": "1",
                "contract_type": "minprom",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-04-30",
                "spend_deadline": "",
                "total_fot": short_total_fot,
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": True,
                "months_after_end": 0,
            },
            {
                "id": "C_LONG",
                "name": "Длинный",
                "number": "2",
                "contract_type": "minprom",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "spend_deadline": "",
                "total_fot": long_total_fot,
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": True,
                "months_after_end": 0,
            },
        ]
    )
    positions = pd.DataFrame(
        [
            {"contract_id": cid, "position": "инженер", "max_monthly_payment": 150_000}
            for cid in ("C_SHORT", "C_LONG")
        ]
    )
    budget_rows = [
        {
            "contract_id": "C_SHORT",
            "year": year,
            "month": 4,
            "inflow_amount": short_inflow_april,
        },
        {"contract_id": "C_LONG", "year": year, "month": 1, "inflow_amount": long_total_fot},
    ]
    for cid in ("C_SHORT", "C_LONG"):
        for m in range(1, 13):
            if any(r["contract_id"] == cid and r["month"] == m for r in budget_rows):
                continue
            budget_rows.append(
                {"contract_id": cid, "year": year, "month": m, "inflow_amount": 0}
            )
    budget = pd.DataFrame(budget_rows)
    settings = pd.DataFrame(
        [
            {
                "year": year,
                "weight_uncovered_salary": 1_000_000,
                "weight_uniform_spend_deviation": uniform_weight,
                "weight_labor_deviation": 0,
                "weight_flex_fragment": flex_fragment_weight,
                "weight_admin_complexity": admin_complexity_weight,
                "max_salary_contracts_per_year": 0,
            }
        ]
    )

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        employees.to_excel(writer, sheet_name="employees", index=False)
        contracts.to_excel(writer, sheet_name="contracts", index=False)
        positions.to_excel(writer, sheet_name="contract_positions", index=False)
        budget.to_excel(writer, sheet_name="contract_monthly_budget", index=False)
        settings.to_excel(writer, sheet_name="settings", index=False)


def test_allowance_not_split_across_contracts_when_one_is_enough(tmp_path: Path):
    path = tmp_path / "fragment.xlsx"
    _fragment_workbook(path)
    result = run_planning(path, tmp_path / "out.xlsx", time_limit_sec=60)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    april = [
        a
        for a in result.allocations
        if a.employee_id == "E001"
        and a.month == 4
        and a.payment_kind == "allowance"
        and a.amount > 0.01
    ]
    assert april
    assert sum(a.amount for a in april) == pytest.approx(25_000, rel=0.01)
    assert len({a.contract_id for a in april}) == 1
    assert all(a.amount >= 24_000 for a in april)


def test_allowance_no_ruble_tail_split(tmp_path: Path):
    """Нельзя 24 999 + 1: второй фрагмент меньше 10% надбавки."""
    path = tmp_path / "tail.xlsx"
    _fragment_workbook(path, flex_fragment_weight=200_000, admin_complexity_weight=0)
    result = run_planning(path, tmp_path / "out.xlsx", time_limit_sec=60)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")
    april = [
        a
        for a in result.allocations
        if a.employee_id == "E001"
        and a.month == 4
        and a.payment_kind == "allowance"
        and a.amount > 0.01
    ]
    assert sum(a.amount for a in april) == pytest.approx(25_000, rel=0.01)
    assert all(a.amount >= 1_000 for a in april)


def test_allowance_can_split_when_single_contract_insufficient(tmp_path: Path):
    path = tmp_path / "split_needed.xlsx"
    _fragment_workbook(
        path,
        short_total_fot=10_000,
        short_inflow_april=10_000,
        long_total_fot=290_000,
    )
    result = run_planning(path, tmp_path / "out.xlsx", time_limit_sec=60)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    april = [
        a
        for a in result.allocations
        if a.employee_id == "E001"
        and a.month == 4
        and a.payment_kind == "allowance"
        and a.amount > 0.01
    ]
    assert sum(a.amount for a in april) == pytest.approx(25_000, rel=0.01)
    assert len({a.contract_id for a in april}) >= 2

