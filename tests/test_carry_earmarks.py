"""Плановый перенос: остаток в from_month на to_month (март → январь)."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel_io import SHEET_CARRY_EARMARKS, create_template, load_context
from fot_planner.planner import run_planning


def _grant_workbook(path: Path, monthly_inflow: float, earmark_amount: float = 0) -> None:
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
                "number": "1",
                "contract_type": "grant",
                "status": "active",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "spend_deadline": "",
                "total_fot": 3_000_000,
                "priority": 1,
                "allow_salary": True,
                "allow_incentive": True,
                "probability": 1,
                "use_after_end": False,
                "months_after_end": 0,
                "allow_monthly_carryover": True,
                "opening_balance": 0,
                "require_salary_reserve": False,
            }
        ]
    )
    fot_matrix = pd.DataFrame({"contract_id": ["C001"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [monthly_inflow]

    earmarks = pd.DataFrame(
        [
            {
                "contract_id": "C001",
                "from_month": 3,
                "to_month": 1,
                "amount": earmark_amount,
                "formula": "",
                "comment": "март на январь",
            }
        ]
    ) if earmark_amount > 0 else pd.DataFrame(
        columns=["contract_id", "from_month", "to_month", "amount", "formula", "comment"]
    )

    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)
        earmarks.to_excel(w, sheet_name=SHEET_CARRY_EARMARKS, index=False)


def test_carry_earmark_loaded(tmp_path: Path):
    path = tmp_path / "input.xlsx"
    _grant_workbook(path, monthly_inflow=200_000, earmark_amount=80_000)
    ctx = load_context(path)
    e = ctx.contracts[0].carry_earmarks[0]
    assert e.from_month == 3
    assert e.to_month == 1
    assert e.amount == 80_000


def test_march_holds_earmark_april_gets_rest(tmp_path: Path):
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    _grant_workbook(inp, monthly_inflow=300_000, earmark_amount=100_000)

    result = run_planning(inp, out, time_limit_sec=90)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    by_m = {b.month: b for b in result.contract_balances if b.contract_id == "C001"}
    mar = by_m[3]
    apr = by_m[4]
    jan = by_m[1]

    assert mar.earmark_held >= 100_000 - 1
    assert mar.closing_balance >= 100_000 - 1
    assert apr.opening_balance == pytest.approx(mar.carried_forward, abs=1)
    assert mar.carried_forward <= mar.closing_balance - 100_000 + 1
    assert jan.earmark_credit == pytest.approx(100_000, abs=1)
