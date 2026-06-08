from datetime import date
from pathlib import Path

import pandas as pd

from fot_planner.excel import create_template, load_context
from fot_planner.planner import run_planning


def test_budget_lock_loaded(tmp_path: Path):
    path = tmp_path / "input.xlsx"
    create_template(path)

    fot_matrix = pd.DataFrame({"договор": ["C001"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [""]
    fot_matrix["12"] = "2000000*"

    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)

    ctx = load_context(path)
    c = next(x for x in ctx.contracts if x.id == "C001")
    dec_row = next(mb for mb in c.monthly_budgets if mb.month == 12)
    assert dec_row.lock is True
    assert dec_row.inflow_amount == 2_000_000


def test_budget_lock_in_result(tmp_path: Path):
    path = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    create_template(path)
    year = date.today().year

    fot_matrix = pd.DataFrame({"договор": ["C001"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [""]
    fot_matrix["12"] = "2000000*"
    settings = pd.DataFrame(
        [
            {
                "year": year,
                "max_salary_contracts_per_year": 2,
                "min_fot_months_for_salary_reserve": 6,
                "goz_labor_tolerance": 0.05,
            }
        ]
    )

    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)
        settings.to_excel(w, sheet_name="settings", index=False)

    run_planning(path, out, time_limit_sec=60)

    locked = pd.read_excel(out, sheet_name="budget_locked")
    assert len(locked) >= 1
    assert locked.iloc[0]["inflow_amount"] == 2_000_000
