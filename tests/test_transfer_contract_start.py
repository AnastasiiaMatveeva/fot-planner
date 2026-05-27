"""Переносы назад только в месяцы, когда договор уже действует."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel_io import create_template
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
                "salary": 100_000,
                "allowance": 0,
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
                "id": "C_GOS",
                "name": "ГОЗ контрольный",
                "number": "1",
                "contract_type": "goszakaz",
                "start_date": f"{year}-03-01",
                "end_date": f"{year}-12-30",
                "spend_deadline": "",
                "total_fot": 1_000_000,
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": True,
                "months_after_end": 0,
                "allow_monthly_carryover": True,
                "require_salary_reserve": False,
            }
        ]
    )
    fot_matrix = pd.DataFrame({"договор": ["C_GOS"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [1_000_000 if m == 12 else 0]

    positions = pd.DataFrame(
        [{"договор": "C_GOS", "должность": "инженер", "макс выплата": 100_000}]
    )
    labor = pd.DataFrame(
        [
            {
                "договор": "C_GOS",
                "год": year,
                "трудоемкость": 10,
                "должность": "инженер",
            }
        ]
    )
    settings = pd.DataFrame(
        [
            {
                "year": year,
                "weight_uncovered_salary": 1_000_000,
                "max_salary_contracts_per_year": 2,
                "goz_labor_tolerance": 0.05,
                "allow_backward_reallocation": True,
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


def test_contract_active_months_start_in_march():
    path = Path("_xfer_start_check.xlsx")
    _goszakaz_march_start_workbook(path)
    year = date.today().year

    from fot_planner.excel_io import load_context

    ctx = load_context(path)
    c = next(x for x in ctx.contracts if x.id == "C_GOS")
    assert not contract_allows_month(c, year, 1)
    assert not contract_allows_month(c, year, 2)
    assert contract_allows_month(c, year, 3)


def test_december_transfer_not_before_contract_start(tmp_path: Path):
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "out.xlsx"
    year = date.today().year
    _goszakaz_march_start_workbook(inp)

    result = run_planning(inp, out, time_limit_sec=120)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    for month in (1, 2):
        assert not any(a.contract_id == "C_GOS" and a.month == month for a in result.allocations)

    salary_by_month = {
        m: sum(a.amount for a in result.allocations if a.contract_id == "C_GOS" and a.month == m)
        for m in range(3, 13)
    }
    for m in range(3, 13):
        assert salary_by_month[m] == pytest.approx(100_000, abs=500)

    bad = [t for t in result.month_transfers if t.contract_id == "C_GOS" and t.to_month in (1, 2)]
    assert not bad, f"перенос до старта договора: {bad}"

    back_from_dec = [
        t for t in result.month_transfers if t.contract_id == "C_GOS" and t.from_month == 12 and t.amount > 1000
    ]
    assert back_from_dec, "ожидался перенос из декабря на месяцы после старта"
    assert all(3 <= t.to_month <= 11 for t in back_from_dec)

    spent = sum(a.amount for a in result.allocations if a.contract_id == "C_GOS")
    assert spent >= 1_000_000 - 2000
