"""Шаблон входного Excel."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import PatternFill

from fot_planner.excel.constants import (
    SHEET_CONTRACT_BUDGET,
    SHEET_CONTRACT_LABOR,
    SHEET_CONTRACT_POSITIONS,
    SHEET_CONTRACTS,
    SHEET_EMPLOYEES,
    SHEET_FOT_LOCK_MATRIX,
    SHEET_FOT_MATRIX,
    SHEET_MANUAL_ASSIGNMENTS,
    SHEET_MANUAL_PROHIBITIONS,
    SHEET_MIN_BALANCE_MATRIX,
    SHEET_POSITION_REFERENCE,
    SHEET_POSITION_SALARY_LIMITS,
    SHEET_POSITION_SYNONYMS,
    SHEET_SETTINGS,
)
from fot_planner.excel.load import _month_from_column
from fot_planner.excel.workbook_format import format_workbook
from fot_planner.defaults.contract_types import CONTRACT_TYPE_PRESETS
from fot_planner.defaults.settings import DEFAULT_SETTINGS_ROW
from fot_planner.position_reference import default_position_reference, default_position_synonyms
from fot_planner.salary_limits_2556 import default_position_salary_limits, effective_p4_limit


def create_template(path: str | Path) -> None:
    path = Path(path)
    year = date.today().year

    employees = pd.DataFrame(
        [
            {
                "код": "E001",
                "фио": "Иванов Иван Иванович",
                "должность": "инженер",
                "подразделение": "лаборатория",
                "ставка": 1.0,
                "зарплата": 100000,
                "дата начала": f"{year}-01-01",
                "дата окончания": "",
                "разрешенные договоры": "",
                "запрещенные договоры": "",
            }
        ]
    )
    contract_types = pd.DataFrame(CONTRACT_TYPE_PRESETS)
    contract_labor = pd.DataFrame(
        columns=[
            "договор",
            "год",
            "трудоемкость",
            "должность",
            "средняя стоимость выполнения работ в месяц",
        ]
    )
    contracts = pd.DataFrame(
        [
            {
                "код": "C001",
                "название": "НИОКР Альфа",
                "номер": "123/2025",
                "тип договора": "goz",
                "дата начала": f"{year}-01-01",
                "дата окончания": f"{year}-12-31",
                "фот": 1_200_000,
                "оклад разрешен": True,
                "120 разрешена": False,
                "надбавка разрешена": True,
                "124 разрешена": False,
                "152 разрешена": False,
                "стимулирующая приказом разрешена": False,
                "конечная дата выплат оклада": f"{year}-12-31",
                "конечная дата выплат надбавок": f"{year}-12-31",
                "перенос остатков": True,
                "приоритет окладного якоря": 0,
            }
        ]
    )
    positions = pd.DataFrame(
        [
            {
                "договор": "C001",
                "должность": "инженер",
                "макс ставки": 2,
            }
        ]
    )
    min_balance_matrix = pd.DataFrame({"договор": ["C001"]})
    for m in range(1, 13):
        min_balance_matrix[str(m)] = [""]

    settings = pd.DataFrame([{"год": year, **DEFAULT_SETTINGS_ROW}])
    fot_matrix = pd.DataFrame({"договор": ["C001"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [1_200_000 if m == 1 else ""]

    manual_assignments = pd.DataFrame(
        columns=[
            "сотрудник",
            "договор",
            "год",
            "месяц_с",
            "месяц_по",
            "вид выплаты",
            "сумма",
        ]
    )
    manual_prohibitions = pd.DataFrame(
        columns=["сотрудник", "договор", "год", "месяц_с", "месяц_по", "вид выплаты"]
    )
    fot_lock = pd.DataFrame({"договор": ["C001"]})
    for m in range(1, 13):
        fot_lock[str(m)] = [""]

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        employees.to_excel(writer, sheet_name=SHEET_EMPLOYEES, index=False)
        contract_types.to_excel(writer, sheet_name="contract_types", index=False)
        contracts.to_excel(writer, sheet_name=SHEET_CONTRACTS, index=False)
        positions.to_excel(writer, sheet_name=SHEET_CONTRACT_POSITIONS, index=False)
        contract_labor.to_excel(writer, sheet_name=SHEET_CONTRACT_LABOR, index=False)
        fot_matrix.to_excel(writer, sheet_name=SHEET_FOT_MATRIX, index=False)
        min_balance_matrix.to_excel(writer, sheet_name=SHEET_MIN_BALANCE_MATRIX, index=False)
        fot_lock.to_excel(writer, sheet_name=SHEET_FOT_LOCK_MATRIX, index=False)
        manual_assignments.to_excel(writer, sheet_name=SHEET_MANUAL_ASSIGNMENTS, index=False)
        manual_prohibitions.to_excel(writer, sheet_name=SHEET_MANUAL_PROHIBITIONS, index=False)
        settings.to_excel(writer, sheet_name=SHEET_SETTINGS, index=False)
        pd.DataFrame(
            [
                {
                    "должность": row.position,
                    "группа взаимозаменяемости": row.equivalence_group,
                    "уровень": row.level,
                    "оклад по справочнику за 1 ставку": row.reference_salary_for_rate,
                }
                for row in default_position_reference()
            ]
        ).to_excel(writer, sheet_name=SHEET_POSITION_REFERENCE, index=False)
        pd.DataFrame(
            [
                {"как написано": raw, "должность из справочника": canonical}
                for raw, canonical in default_position_synonyms().items()
            ]
        ).to_excel(writer, sheet_name=SHEET_POSITION_SYNONYMS, index=False)
        pd.DataFrame(
            [
                {
                    "должность": row.position,
                    "категория персонала": row.personnel_category,
                    "П2556": row.order_2556_limit if row.order_2556_limit is not None else "",
                    "П4": effective_p4_limit(row) or "",
                    "примечание к П2556": row.note or "",
                }
                for row in default_position_salary_limits()
            ]
        ).to_excel(writer, sheet_name=SHEET_POSITION_SALARY_LIMITS, index=False)

    wb = load_workbook(path)
    yellow = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")
    for sheet_name in (SHEET_CONTRACTS, SHEET_EMPLOYEES):
        ws = wb[sheet_name]
        for cell in ws[1]:
            cell.fill = yellow
    wb.save(path)
    format_workbook(path)
