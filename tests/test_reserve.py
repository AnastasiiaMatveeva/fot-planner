"""Остатки договора считаются без отдельного резерва оклада."""

from datetime import date
from pathlib import Path

import pandas as pd

from fot_planner.excel import create_template
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
                "monthly_wage": 100_000,
                "start_date": f"{year}-01-01",
                "end_date": "",
            }
        ]
    )
    contracts = pd.DataFrame(
        [
            {
                "id": "C001",
                "name": "Грант",
                "contract_type": contract_type,
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "total_fot": 1_200_000,
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": False,
            }
        ]
    )
    fot_matrix = pd.DataFrame({"contract_id": ["C001"]})
    active_inflow = set(months_with_inflow) if months_with_inflow else set(range(1, 13))
    for m in range(1, 13):
        fot_matrix[str(m)] = [monthly_inflow if m in active_inflow else 0]

    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)


def test_plan_feasible_without_salary_reserve_field(tmp_path: Path):
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    _grant_workbook(inp, monthly_inflow=150_000)

    result = run_planning(inp, out, time_limit_sec=60)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    jan = next(b for b in result.contract_balances if b.month == 1)
    assert jan.closing_balance >= 0


def test_plan_feasible_without_salary_reserve_constraint(tmp_path: Path):
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
    assert jan.closing_balance >= 0
