from __future__ import annotations

import pandas as pd

from fot_planner.excel import create_template, load_context
from fot_planner.excel.constants import SHEET_POSITION_LIMITS


def test_template_contains_position_limits_sheet(tmp_path):
    path = tmp_path / "input.xlsx"
    create_template(path)

    limits = pd.read_excel(path, sheet_name=SHEET_POSITION_LIMITS)
    assert list(limits.columns) == [
        "должность",
        "категория персонала",
        "группа взаимозаменяемости",
        "уровень",
        "оклад",
        "П2556",
        "П4",
        "БЭП",
        "примечание",
    ]

    engineer = limits.loc[limits["должность"] == "Инженер"].iloc[0]
    assert engineer["категория персонала"] == "НТП"
    assert engineer["оклад"] == 40_400
    assert engineer["П2556"] == 110_000
    assert engineer["П4"] == 149_648.90
    assert engineer["БЭП"] == 112_261

    researcher = limits.loc[limits["должность"] == "Научный сотрудник"].iloc[0]
    assert researcher["категория персонала"] == "НР"
    assert researcher["оклад"] == 55_700
    assert pd.isna(researcher["П2556"])
    assert researcher["П4"] == 257_643.19

    director = limits.loc[limits["должность"] == "Директор центра"].iloc[0]
    assert pd.isna(director["П4"])


def test_changed_position_limits_are_loaded_from_excel(tmp_path):
    path = tmp_path / "input.xlsx"
    create_template(path)

    limits = pd.read_excel(path, sheet_name=SHEET_POSITION_LIMITS)
    engineer_mask = limits["должность"] == "Инженер"
    limits.loc[engineer_mask, "оклад"] = 41_111
    limits.loc[engineer_mask, "П2556"] = 111_111
    limits.loc[engineer_mask, "П4"] = 166_000
    limits.loc[engineer_mask, "БЭП"] = 100_000
    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
        limits.to_excel(writer, sheet_name=SHEET_POSITION_LIMITS, index=False)

    ctx = load_context(path)
    position = next(row for row in ctx.position_reference if row.position == "Инженер")
    engineer = next(row for row in ctx.position_salary_limits if row.position == "Инженер")
    assert position.reference_salary_for_rate == 41_111
    assert engineer.personnel_category == "НТП"
    assert engineer.order_2556_limit == 111_111
    assert engineer.p4_limit == 166_000
    assert engineer.bep_limit == 100_000
    assert ctx.salary_stability.goz_average_salary_limit == 100_000
