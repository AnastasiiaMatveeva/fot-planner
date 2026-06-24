from __future__ import annotations

import pandas as pd

from fot_planner.excel import SHEET_POSITION_SALARY_LIMITS, create_template, load_context


def test_template_contains_position_categories_and_order_2556_limits(tmp_path):
    path = tmp_path / "input.xlsx"
    create_template(path)

    table = pd.read_excel(path, sheet_name=SHEET_POSITION_SALARY_LIMITS)
    assert list(table.columns) == [
        "должность",
        "категория персонала",
        "П2556",
        "П4",
        "примечание к П2556",
    ]
    assert len(table) == 33

    engineer = table.loc[table["должность"] == "Инженер"].iloc[0]
    assert engineer["категория персонала"] == "НТП"
    assert engineer["П2556"] == 110_000
    assert engineer["П4"] == 162_648.30

    engineer_i = table.loc[table["должность"] == "Инженер 1 категории"].iloc[0]
    assert engineer_i["П2556"] == 120_000

    researcher = table.loc[table["должность"] == "Научный сотрудник"].iloc[0]
    assert researcher["категория персонала"] == "НР"
    assert pd.isna(researcher["П2556"])
    assert researcher["П4"] == 280_023.62

    director = table.loc[table["должность"] == "Директор центра"].iloc[0]
    assert pd.isna(director["П4"])


def test_changed_order_2556_limit_is_loaded_from_excel(tmp_path):
    path = tmp_path / "input.xlsx"
    create_template(path)

    table = pd.read_excel(path, sheet_name=SHEET_POSITION_SALARY_LIMITS)
    table.loc[table["должность"] == "Инженер", "П2556"] = 111_111
    table.loc[table["должность"] == "Инженер", "П4"] = 166_000
    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
        table.to_excel(writer, sheet_name=SHEET_POSITION_SALARY_LIMITS, index=False)

    ctx = load_context(path)
    engineer = next(row for row in ctx.position_salary_limits if row.position == "Инженер")
    assert engineer.personnel_category == "НТП"
    assert engineer.order_2556_limit == 111_111
    assert engineer.p4_limit == 166_000
