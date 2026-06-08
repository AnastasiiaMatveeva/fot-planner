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
    SHEET_POSITION_SYNONYMS,
    SHEET_SETTINGS,
)
from fot_planner.excel.load import _month_from_column
from fot_planner.excel.workbook_format import format_workbook
from fot_planner.fot_schedule import default_fot_inflow_at_start
from fot_planner.position_reference import default_position_reference, default_position_synonyms


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
    contract_types = pd.DataFrame(
        [
            {
                "code": "goszakaz",
                "name": "Государственный заказ",
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": True,
            },
            {
                "code": "grant",
                "name": "Грант",
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": True,
            },
            {
                "code": "minprom",
                "name": "Минпромторг",
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": True,
            },
            {
                "code": "off_budget",
                "name": "Внебюджет",
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": True,
            },
        ]
    )
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
                "тип договора": "goszakaz",
                "дата начала": f"{year}-01-01",
                "дата окончания": f"{year}-12-31",
                "фот": 1_200_000,
                "оклад разрешен": True,
                "надбавка разрешена": True,
                "стимулирующая разрешена": True,
                "срок выплат оклада": f"{year}-12-31",
                "срок выплат надбавки": f"{year}-12-31",
                "срок выплат стимулирующей": f"{year}-12-31",
                "перенос остатков": True,
            }
        ]
    )
    positions = pd.DataFrame(
        [
            {
                "договор": "C001",
                "должность": "инженер",
                "макс ставки": 2,
                "макс выплата": 120000,
            }
        ]
    )
    min_balance_matrix = pd.DataFrame({"договор": ["C001"]})
    for m in range(1, 13):
        min_balance_matrix[str(m)] = [""]

    settings = pd.DataFrame(
        [
            {
                "год": year,
                "разрешить дефицит": "нет",
                "макс договоров оклада в год": 2,
                "штраф смены договора оклада": 500_000,
                "штраф административной сложности выплат": 50_000,
                "штраф отклонения от равномерного освоения": 10_000,
                "допуск трудоёмкости": 0.05,
                "штраф отклонения трудоёмкости": 100_000,
            }
        ]
    )
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

    wb = load_workbook(path)
    yellow = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")
    for sheet_name in (SHEET_CONTRACTS, SHEET_EMPLOYEES):
        ws = wb[sheet_name]
        for cell in ws[1]:
            cell.fill = yellow
    wb.save(path)
    format_workbook(path)
