from datetime import date
from pathlib import Path

import pytest

from fot_planner.excel_io import create_template, load_context
from fot_planner.planner import run_planning


@pytest.fixture
def mini_workbook(tmp_path: Path) -> Path:
    path = tmp_path / "input.xlsx"
    create_template(path)

    import pandas as pd

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
            },
            {
                "id": "E002",
                "full_name": "Петров",
                "position": "инженер",
                "department": "лаб",
                "rate": 0.5,
                "salary": 50000,
                "incentive": 10000,
                "start_date": f"{year}-01-01",
                "end_date": "",
                "allowed_contracts": "",
                "forbidden_contracts": "",
            },
        ]
    )
    fot_matrix = pd.DataFrame({"contract_id": ["C001"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [1_920_000 if m == 1 else 0]

    contracts = pd.read_excel(path, sheet_name="contracts")
    fot_col = "фот" if "фот" in contracts.columns else "total_fot"
    contracts[fot_col] = 1_920_000

    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)

    return path


def test_solve_small(mini_workbook: Path, tmp_path: Path):
    out = tmp_path / "result.xlsx"
    result = run_planning(mini_workbook, out, time_limit_sec=60)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")
    assert out.exists()
    ctx = load_context(mini_workbook)
    assert len(ctx.employees) == 2
