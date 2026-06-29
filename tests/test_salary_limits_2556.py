from __future__ import annotations

import pandas as pd

from fot_planner.excel import create_template, load_context


def test_template_contains_position_categories_and_order_2556_limits(tmp_path):
    path = tmp_path / "input.xlsx"
    create_template(path)

    table_2556 = pd.read_excel(path, sheet_name="2556")
    table_p4 = pd.read_excel(path, sheet_name="p4")
    assert list(table_2556.columns) == [
        "должность",
        "категория персонала",
        "лимит на 1 ставку",
        "примечание",
    ]
    assert list(table_p4.columns) == [
        "должность",
        "категория персонала",
        "лимит на 1 ставку",
        "примечание",
    ]
    assert len(table_2556) == 33
    assert len(table_p4) == 33

    engineer = table_2556.loc[table_2556["должность"] == "Инженер"].iloc[0]
    assert engineer["категория персонала"] == "НТП"
    assert engineer["лимит на 1 ставку"] == 110_000

    engineer_p4 = table_p4.loc[table_p4["должность"] == "Инженер"].iloc[0]
    assert engineer_p4["лимит на 1 ставку"] == 162_648.30

    engineer_i = table_2556.loc[table_2556["должность"] == "Инженер 1 категории"].iloc[0]
    assert engineer_i["лимит на 1 ставку"] == 120_000

    researcher = table_2556.loc[table_2556["должность"] == "Научный сотрудник"].iloc[0]
    assert researcher["категория персонала"] == "НР"
    assert pd.isna(researcher["лимит на 1 ставку"])

    researcher_p4 = table_p4.loc[table_p4["должность"] == "Научный сотрудник"].iloc[0]
    assert researcher_p4["лимит на 1 ставку"] == 280_023.62

    director = table_p4.loc[table_p4["должность"] == "Директор центра"].iloc[0]
    assert pd.isna(director["лимит на 1 ставку"])


def test_changed_order_2556_limit_is_loaded_from_excel(tmp_path):
    path = tmp_path / "input.xlsx"
    create_template(path)

    table_2556 = pd.read_excel(path, sheet_name="2556")
    table_p4 = pd.read_excel(path, sheet_name="p4")
    table_2556.loc[
        table_2556["должность"] == "Инженер", "лимит на 1 ставку"
    ] = 111_111
    table_p4.loc[
        table_p4["должность"] == "Инженер", "лимит на 1 ставку"
    ] = 166_000
    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
        table_2556.to_excel(writer, sheet_name="2556", index=False)
        table_p4.to_excel(writer, sheet_name="p4", index=False)

    ctx = load_context(path)
    engineer = next(row for row in ctx.position_salary_limits if row.position == "Инженер")
    assert engineer.personnel_category == "НТП"
    assert engineer.order_2556_limit == 111_111
    assert engineer.p4_limit == 166_000
