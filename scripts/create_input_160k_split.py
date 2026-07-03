"""Входной пример: 160k, грант = оклад+122, второй договор = 124+приказ."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from fot_planner.excel import (
    SHEET_CONTRACT_LABOR,
    SHEET_CONTRACT_POSITIONS,
    SHEET_CONTRACTS,
    SHEET_EMPLOYEES,
    SHEET_FOT_MATRIX,
    SHEET_SETTINGS,
)
from fot_planner.excel.constants import SHEET_POSITION_LIMITS
from fot_planner.excel.template import create_template


def create_input_160k_split(
    path: str | Path = "data/input.xlsx",
    *,
    year: int | None = None,
    wage: float = 160_000,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    year = year or date.today().year

    grant_monthly = 100_000.0  # оклад 40 400 + 122
    flex_monthly = wage - grant_monthly  # 124 + приказ

    create_template(path)
    position_limits = pd.read_excel(path, sheet_name=SHEET_POSITION_LIMITS)
    settings = pd.read_excel(path, sheet_name="настройки")
    settings.loc[0, "год"] = year

    employees = pd.DataFrame(
        [
            {
                "код строки": "E001-1",
                "фио": "Иванов Иван Иванович",
                "должность": "инженер",
                "подразделение": "отдел НИОКР",
                "ставка": 1.0,
                "тип занятости": "основное",
                "категория занятости": "основной",
                "зарплата": wage,
                "дата начала": f"{year}-01-01",
                "дата окончания": "",
                "разрешенные договоры": "",
                "запрещенные договоры": "",
            }
        ]
    )
    contracts = pd.DataFrame(
        [
            {
                "код": "C_GRANT",
                "название": "Грант (оклад + 122)",
                "номер": f"ГР-{year}/1",
                "тип договора": "grant",
                "счет": "",
                "ГОЗ": False,
                "дата начала": f"{year}-01-01",
                "дата окончания": f"{year}-12-31",
                "фот": grant_monthly * 12,
                "оклад разрешен": True,
                "120 разрешена": False,
                "122 разрешена": True,
                "124 разрешена": False,
                "152 разрешена": False,
                "стимулирующая приказом разрешена": False,
                "конечная дата выплат оклада": f"{year}-12-31",
                "конечная дата выплат надбавок": f"{year}-12-31",
                "перенос остатков": True,
            },
            {
                "код": "C_FLEX",
                "название": "Внебюджет (124 + приказ)",
                "номер": f"ВБ-{year}/1",
                "тип договора": "off_budget",
                "счет": "",
                "ГОЗ": False,
                "дата начала": f"{year}-01-01",
                "дата окончания": f"{year}-12-31",
                "фот": flex_monthly * 12,
                "оклад разрешен": False,
                "120 разрешена": False,
                "122 разрешена": False,
                "124 разрешена": True,
                "152 разрешена": False,
                "стимулирующая приказом разрешена": True,
                "конечная дата выплат оклада": f"{year}-12-31",
                "конечная дата выплат надбавок": f"{year}-12-31",
                "перенос остатков": True,
            },
        ]
    )
    positions = pd.DataFrame(
        [
            {"договор": "C_GRANT", "должность": "инженер", "макс ставки": 1},
            {"договор": "C_FLEX", "должность": "инженер", "макс ставки": 1},
        ]
    )
    labor = pd.DataFrame(
        columns=["договор", "год", "трудоемкость", "должность", "средняя зарплата"]
    )
    fot_matrix = pd.DataFrame({"договор": ["C_GRANT", "C_FLEX"]})
    for month in range(1, 13):
        fot_matrix[str(month)] = [grant_monthly, flex_monthly]

    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
        employees.to_excel(writer, sheet_name=SHEET_EMPLOYEES, index=False)
        contracts.to_excel(writer, sheet_name=SHEET_CONTRACTS, index=False)
        positions.to_excel(writer, sheet_name=SHEET_CONTRACT_POSITIONS, index=False)
        labor.to_excel(writer, sheet_name=SHEET_CONTRACT_LABOR, index=False)
        fot_matrix.to_excel(writer, sheet_name=SHEET_FOT_MATRIX, index=False)
        settings.to_excel(writer, sheet_name=SHEET_SETTINGS, index=False)
        position_limits.to_excel(writer, sheet_name=SHEET_POSITION_LIMITS, index=False)

    return path


def main() -> None:
    path = create_input_160k_split()
    print(f"Создан: {path.resolve()}")


if __name__ == "__main__":
    main()
