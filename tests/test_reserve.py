"""Резерв: минимальный остаток = сумма окладов назначенных сотрудников × ставка."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel_io import create_template
from fot_planner.planner import run_planning


def _grant_workbook(
    path: Path,
    monthly_inflow: float,
    *,
    contract_type: str = "grant",
    months_with_inflow: list[int] | None = None,
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
                "total_fot": 1_200_000,
                "priority": 1,
                "allow_salary": True,
                "allow_incentive": True,
                "probability": 1,
                "use_after_end": False,
                "months_after_end": 0,
                "allow_monthly_carryover": True,

                "require_salary_reserve": True,
            }
        ]
    )
    fot_matrix = pd.DataFrame({"contract_id": ["C001"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [monthly_inflow]

    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)


def test_reserve_enforced_when_enough_funds(tmp_path: Path):
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    _grant_workbook(inp, monthly_inflow=250_000)

    result = run_planning(inp, out, time_limit_sec=60)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    jan = next(b for b in result.contract_balances if b.month == 1)
    assert jan.salary_reserve_required == pytest.approx(100_000, abs=1)
    assert jan.closing_balance >= jan.salary_reserve_required - 0.01


def test_reserve_on_goszakaz_with_long_fot_matrix(tmp_path: Path):
    """Резерв по числу месяцев ФОТ, не по типу grant."""
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    _grant_workbook(inp, monthly_inflow=200_000, contract_type="goszakaz")
    contracts = pd.read_excel(inp, sheet_name="contracts")
    contracts.loc[0, "total_fot"] = 1_200_000
    with pd.ExcelWriter(inp, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        contracts.to_excel(w, sheet_name="contracts", index=False)

    result = run_planning(inp, out, time_limit_sec=60)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")
    jan = next(b for b in result.contract_balances if b.month == 1)
    assert jan.salary_reserve_required == pytest.approx(100_000, abs=1)


def test_no_reserve_when_few_fot_months(tmp_path: Path):
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    _grant_workbook(
        inp,
        monthly_inflow=500_000,
        contract_type="grant",
        months_with_inflow=[1, 2, 3, 4, 5, 6],
    )

    result = run_planning(inp, out, time_limit_sec=60)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")
    jan = next(b for b in result.contract_balances if b.month == 1)
    assert jan.salary_reserve_required == 0.0


def test_reserve_enforced_when_explicitly_required(tmp_path: Path):
    """Явный резерв оклада на договоре при полном освоении ФОТ к сроку."""
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    _grant_workbook(inp, monthly_inflow=200_000)
    contracts = pd.read_excel(inp, sheet_name="contracts")
    reserve_col = "резерв оклада" if "резерв оклада" in contracts.columns else "require_salary_reserve"
    contracts[reserve_col] = contracts[reserve_col].astype(object)
    contracts.loc[0, reserve_col] = "да"
    with pd.ExcelWriter(inp, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        contracts.to_excel(w, sheet_name="contracts", index=False)

    result = run_planning(inp, out, time_limit_sec=60)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")
    jan = next(b for b in result.contract_balances if b.month == 1)
    assert jan.salary_reserve_required == pytest.approx(100_000, abs=1)
