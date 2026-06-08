from datetime import date
from pathlib import Path

import pandas as pd

from fot_planner.excel import SHEET_FOT_MATRIX, create_template, load_context


def test_matrix_budget_and_lock(tmp_path: Path):
    path = tmp_path / "input.xlsx"
    create_template(path)
    year = date.today().year

    amounts = pd.DataFrame(
        [
            {
                "contract_id": "C001",
                **{str(m): 100000 for m in range(1, 12)},
                "12": "2000000*",
            },
        ]
    )

    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        amounts.to_excel(w, sheet_name=SHEET_FOT_MATRIX, index=False)

    ctx = load_context(path)
    c = next(x for x in ctx.contracts if x.id == "C001")
    dec = next(mb for mb in c.monthly_budgets if mb.month == 12)
    jan = next(mb for mb in c.monthly_budgets if mb.month == 1)
    assert dec.inflow_amount == 2_000_000
    assert dec.lock is True
    assert jan.lock is False
