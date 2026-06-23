"""Касса покрывает будущие назначенные выплаты без отдельного резерва под трудоёмкость."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel import create_template
from fot_planner.planner import run_planning


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
                "id": "C_GOS",
                "name": "ГОЗ",
                "number": "1",
                "contract_type": "goszakaz",
                "start_date": f"{year}-03-01",
                "end_date": f"{year}-12-31",
                "total_fot": 1_500_000,
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": False,
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
                "avg_monthly_labor_cost": 150_000,
            }
        ]
    )
    settings = pd.DataFrame(
        [
            {
                "year": year,
                "weight_deficit_amount": 1_000_000,
                "weight_uniform_spend_deviation": 0,
                "max_salary_contracts_per_year": 1,
                "goz_labor_tolerance": 0.01,
                # Этот сценарий проверяет кассу; норматив БЭП вынесен за рамки теста.
                "goz_average_salary_limit": 150_000,
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


def test_cash_balance_non_negative_with_carry(tmp_path: Path):
    """Остаток кассы не уходит в минус; будущие выплаты учитываются через alloc."""
    inp = tmp_path / "in.xlsx"
    out = tmp_path / "out.xlsx"
    _gos_early_spend_workbook(inp)

    result = run_planning(inp, out, time_limit_sec=120)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    gos_balances = [b for b in result.contract_balances if b.contract_id == "C_GOS"]
    for b in gos_balances:
        assert b.closing_balance >= -0.01

    mar = next(b for b in gos_balances if b.month == 3)
    assert mar.inflow == pytest.approx(1_500_000, abs=1)

    spent_total = sum(a.amount for a in result.allocations if a.contract_id == "C_GOS")
    assert spent_total >= 1_500_000 - 2000


def test_heavy_q1_allowance_limited_by_cash(tmp_path: Path):
    """Без отдельного резерва ранний расход ограничен только кассой и полной выплатой."""
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
                "month_from": 3,
                "month_to": 3,
                "payment_kind": "allowance",
                "fixed_amount": 400_000,
            },
            {
                "employee_id": "E001",
                "contract_id": "C_GOS",
                "year": year,
                "month_from": 4,
                "month_to": 4,
                "payment_kind": "allowance",
                "fixed_amount": 400_000,
            },
            {
                "employee_id": "E001",
                "contract_id": "C_GOS",
                "year": year,
                "month_from": 5,
                "month_to": 5,
                "payment_kind": "allowance",
                "fixed_amount": 400_000,
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
        assert mar_may <= 1_500_000 + 500
        for b in result.contract_balances:
            if b.contract_id == "C_GOS":
                assert b.closing_balance >= -0.01
    else:
        assert result.solver_status == "INFEASIBLE"
