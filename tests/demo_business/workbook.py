"""Сборка входного Excel для демонстрационного сценария."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from fot_planner.excel import create_template
from demo_business.scenario import DemoScenario


def _bool_ru(val: bool) -> str:
    return "да" if val else "нет"


def write_demo_workbook(scenario: DemoScenario, path: str | Path) -> Path:
    path = Path(path)
    create_template(path)

    settings = pd.DataFrame(
        [
            {
                "год": scenario.year,
                "разрешить дефицит": _bool_ru(scenario.settings.allow_deficit),
                "штраф административной сложности выплат": scenario.settings.admin_complexity_weight,
                "штраф смены договора оклада": scenario.settings.salary_switch_weight,
                "штраф отклонения от равномерного освоения": scenario.settings.uniform_weight,
            }
        ]
    )

    position_ref = pd.DataFrame(
        [
            {
                "должность": p.position,
                "группа взаимозаменяемости": p.group,
                "уровень": p.level,
                "оклад по справочнику за 1 ставку": p.reference_salary,
            }
            for p in scenario.positions
        ]
    )

    employees = pd.DataFrame(
        [
            {
                "код строки": f"{e.id}-1",
                "фио": e.full_name,
                "должность": e.position,
                "подразделение": e.department,
                "ставка": 1.0,
                "тип занятости": "основное",
                "зарплата": e.salary + e.allowance,
                "дата начала": f"{scenario.year}-01-01",
                "дата окончания": "",
                "разрешенные договоры": "",
                "запрещенные договоры": "",
            }
            for e in scenario.employees
        ]
    )

    contracts = pd.DataFrame(
        [
            {
                "код": c.id,
                "название": c.name,
                "номер": c.number,
                "тип договора": c.contract_type,
                "ГОЗ": c.is_goz,
                "статус": "active",
                "дата начала": c.start.isoformat(),
                "дата окончания": c.end.isoformat(),
                "срок освоения": "",
                "фот": c.total_fot,
                "оклад разрешен": True,
                "122 разрешена": True,
                "124 разрешена": True,
                "месяцев после окончания": 0,
                "перенос остатков": True,
                "полное освоение за дней до срока": "",
            }
            for c in scenario.contracts
        ]
    )

    fot_rows = []
    for c in scenario.contracts:
        row = {"договор": c.id}
        for m in range(1, 13):
            row[str(m)] = c.inflow(m)
        fot_rows.append(row)
    fot_matrix = pd.DataFrame(fot_rows)

    position_rows = []
    for c in scenario.contracts:
        for pos in c.positions:
            position_rows.append(
                {
                    "договор": c.id,
                    "должность": pos.position,
                    "макс ставки": pos.max_positions,
                    "макс выплата": pos.max_monthly_payment,
                }
            )
    contract_positions = pd.DataFrame(position_rows)

    labor_rows = []
    for c in scenario.contracts:
        for lr in c.labor:
            labor_rows.append(
                {
                    "договор": c.id,
                    "год": scenario.year,
                    "должность": lr.position,
                    "трудоемкость": lr.person_months,
                    "средняя стоимость выполнения работ в месяц": lr.avg_monthly_cost,
                }
            )
    contract_labor = pd.DataFrame(labor_rows)

    manual_assignments = pd.DataFrame(
        columns=[
            "сотрудник",
            "договор",
            "год",
            "месяц с",
            "месяц по",
            "вид выплаты",
            "фикс сумма",
        ]
    )
    manual_prohibitions = pd.DataFrame(
        columns=["сотрудник", "договор", "год", "месяц с", "месяц по", "вид выплаты"]
    )

    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        settings.to_excel(w, sheet_name="settings", index=False)
        position_ref.to_excel(w, sheet_name="справочник_должностей", index=False)
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)
        contract_positions.to_excel(w, sheet_name="contract_positions", index=False)
        contract_labor.to_excel(w, sheet_name="contract_labor", index=False)
        manual_assignments.to_excel(w, sheet_name="manual_assignments", index=False)
        manual_prohibitions.to_excel(w, sheet_name="manual_prohibitions", index=False)

    return path
