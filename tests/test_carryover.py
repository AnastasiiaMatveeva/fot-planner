"""Касса договора: годовой кошелёк и перенос с декабря на январь."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel import create_template
from fot_planner.planner import run_planning


def _build_workbook(path: Path, *, allow_carryover: bool, budget_rows: list[dict]) -> None:
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
                "monthly_wage": 100000,
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
                "name": "Договор",
                "number": "1",
                "contract_type": "grant",
                "status": "active",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "total_fot": 1_200_000,
                "priority": 1,
                "allow_salary": True,
                "allow_incentive": True,
                "probability": 1,
                "use_after_end": False,
            }
        ]
    )
    by_month = {r["month"]: r["inflow_amount"] for r in budget_rows}
    fot_matrix = pd.DataFrame({"contract_id": ["C001"]})
    fot_lock = pd.DataFrame({"contract_id": ["C001"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [by_month.get(m, 0)]
        fot_lock[str(m)] = [""]

    settings = pd.DataFrame(
        [
            {
                "year": year,
                "max_salary_contracts_per_year": 2,
                "goz_labor_tolerance": 0.05,
            }
        ]
    )

    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)
        fot_lock.to_excel(w, sheet_name="fot_lock_matrix", index=False)
        settings.to_excel(w, sheet_name="settings", index=False)


@pytest.mark.parametrize("allow_carryover,expect_feb_deficit", [(True, False), (False, True)])
def test_year_pool_covers_low_february(tmp_path: Path, allow_carryover: bool, expect_feb_deficit: bool):
    """При годовом кошельке февраль с малым траншем покрывается; без — дефицит."""
    year = date.today().year
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    rows = [
        {"contract_id": "C001", "year": year, "month": 1, "available_amount": 150000, "inflow_amount": 150000},
        {"contract_id": "C001", "year": year, "month": 2, "available_amount": 50000, "inflow_amount": 50000},
    ] + [
        {
            "contract_id": "C001",
            "year": year,
            "month": m,
            "available_amount": 100000,
            "inflow_amount": 100000,
        }
        for m in range(3, 13)
    ]
    _build_workbook(inp, allow_carryover=allow_carryover, budget_rows=rows)

    result = run_planning(inp, out, time_limit_sec=60)
    if not allow_carryover:
        # Без переноса: в феврале касса 50k < оклада 100k — жёсткая касса и полное освоение → нет допустимого плана
        assert result.solver_status == "INFEASIBLE"
        return

    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    feb_deficits = [d for d in result.deficits if d.month == 2]
    assert len(feb_deficits) == 0


def test_forward_carry_january_surplus_to_february(tmp_path: Path):
    """Остаток января переносится на февраль."""
    year = date.today().year
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    rows = [
        {"contract_id": "C001", "year": year, "month": 1, "available_amount": 150000, "inflow_amount": 150000},
        {"contract_id": "C001", "year": year, "month": 2, "available_amount": 50000, "inflow_amount": 50000},
    ] + [
        {"contract_id": "C001", "year": year, "month": m, "available_amount": 100000, "inflow_amount": 100000}
        for m in range(3, 13)
    ]
    _build_workbook(inp, allow_carryover=True, budget_rows=rows)

    result = run_planning(inp, out, time_limit_sec=60)
    bal_jan = next(b for b in result.contract_balances if b.month == 1)
    bal_feb = next(b for b in result.contract_balances if b.month == 2)
    assert bal_jan.carried_forward == pytest.approx(50000, abs=1)
    assert bal_feb.opening_balance == pytest.approx(50000, abs=1)

