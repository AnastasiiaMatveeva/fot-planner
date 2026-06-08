"""
Демо «блоки договоров»: 3 сотрудника, 4 договора, без manual_assignments.

  python scripts/create_demo_blocks.py
  fot-planner solve -i data/demo_blocks.xlsx -o data/demo_blocks_result.xlsx
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from fot_planner.excel import create_template

YEAR = 2026


def _fot_row(contract_id: str, inflow: dict[int, int]) -> dict:
    row = {"договор": contract_id}
    for m in range(1, 13):
        row[str(m)] = inflow.get(m, 0)
    return row


def _zero_min_row(contract_id: str) -> dict:
    row = {"договор": contract_id}
    for m in range(1, 13):
        row[str(m)] = 0
    return row


def create_demo_blocks(path: str | Path = "data/demo_blocks.xlsx") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    create_template(path)

    settings = pd.DataFrame(
        [
            {
                "год": YEAR,
                "штраф дефицита": 1_000_000,
                "фиксировать оклад в квартале": True,
                "макс договоров оклада в год": 3,
                "штраф смены оклада в квартале": True,
                "месяцев фот для резерва": 6,
                "допуск трудоемкости гоз": 0.05,
                "вес отклонения равномерного освоения": 1,
                "вес штрафа смены оклада": 500_000,
                "вес штрафа смены надбавки": 300_000,
                "вес штрафа смены стимулирующей": 300_000,
            }
        ]
    )

    employees = pd.DataFrame(
        [
            {
                "код": "E001",
                "фио": "Иванов Иван Иванович",
                "должность": "ведущий инженер",
                "подразделение": "РНД",
                "ставка": 1.0,
                "зарплата": 180_000_

                "дата начала": f"{YEAR}-01-01",
                "дата окончания": "",
                "разрешенные договоры": "C_BASE;C_GOS;C_MINPROM",
                "запрещенные договоры": "",
            },
            {
                "код": "E002",
                "фио": "Петров Петр Петрович",
                "должность": "инженер-помощник",
                "подразделение": "РНД",
                "ставка": 1.0,
                "зарплата": 120_000_

                "дата начала": f"{YEAR}-01-01",
                "дата окончания": "",
                "разрешенные договоры": "C_BASE;C_GOS;C_MINPROM",
                "запрещенные договоры": "",
            },
            {
                "код": "E003",
                "фио": "Сидоров Сергей Сергеевич",
                "должность": "аналитик",
                "подразделение": "РНД",
                "ставка": 1.0,
                "зарплата": 110_000_

                "дата начала": f"{YEAR}-01-01",
                "дата окончания": "",
                "разрешенные договоры": "C_GRANT",
                "запрещенные договоры": "",
            },
        ]
    )

    contracts = pd.DataFrame(
        [
            {
                "код": "C_BASE",
                "название": "Базовый договор до старта ГОЗ",
                "номер": "B-001",
                "тип договора": "base",
                "дата начала": f"{YEAR}-01-01",
                "дата окончания": f"{YEAR}-02-28",
                "срок освоения": "",
                "фот": 600_000,
                "оклад разрешен": True,
                "надбавка разрешена": True,
                "стимулирующая разрешена": True,
                "месяцев после окончания": 0,
                "перенос остатков": True,
                "резерв оклада": False,
                "полное освоение за дней до срока": "",
            },
            {
                "код": "C_GOS",
                "название": "ГОЗ: разработка модуля",
                "номер": "G-001",
                "тип договора": "goszakaz",
                "дата начала": f"{YEAR}-03-01",
                "дата окончания": f"{YEAR}-12-30",
                "срок освоения": "",
                "фот": 1_500_000,
                "оклад разрешен": True,
                "надбавка разрешена": True,
                "стимулирующая разрешена": True,
                "месяцев после окончания": 0,
                "перенос остатков": True,
                "резерв оклада": False,
                "полное освоение за дней до срока": 20,
            },
            {
                "код": "C_GRANT",
                "название": "Грант: аналитическое сопровождение",
                "номер": "R-001",
                "тип договора": "grant",
                "дата начала": f"{YEAR}-01-01",
                "дата окончания": f"{YEAR}-12-31",
                "срок освоения": "",
                "фот": 1_320_000,
                "оклад разрешен": True,
                "надбавка разрешена": True,
                "стимулирующая разрешена": True,
                "месяцев после окончания": 0,
                "перенос остатков": True,
                "резерв оклада": False,
                "полное освоение за дней до срока": "",
            },
            {
                "код": "C_MINPROM",
                "название": "Минпромторг: опытный образец",
                "номер": "M-001",
                "тип договора": "minprom",
                "дата начала": f"{YEAR}-08-01",
                "дата окончания": f"{YEAR}-12-31",
                "срок освоения": "",
                "фот": 1_500_000,
                "оклад разрешен": True,
                "надбавка разрешена": True,
                "стимулирующая разрешена": True,
                "месяцев после окончания": 0,
                "перенос остатков": True,
                "резерв оклада": False,
                "полное освоение за дней до срока": "",
            },
        ]
    )

    positions = pd.DataFrame(
        [
            {"договор": "C_BASE", "должность": "ведущий инженер", "макс выплата": 180_000},
            {"договор": "C_BASE", "должность": "инженер-помощник", "макс выплата": 120_000},
            {"договор": "C_GOS", "должность": "инженер", "макс выплата": 180_000},
            {"договор": "C_GRANT", "должность": "аналитик", "макс выплата": 110_000},
            {"договор": "C_MINPROM", "должность": "ведущий инженер", "макс выплата": 180_000},
            {"договор": "C_MINPROM", "должность": "инженер-помощник", "макс выплата": 120_000},
        ]
    )

    labor = pd.DataFrame(
        [
            {"договор": "C_BASE", "год": YEAR, "трудоемкость": 4, "должность": "инженер"},
            {"договор": "C_GOS", "год": YEAR, "трудоемкость": 10, "должность": "инженер"},
            {"договор": "C_GRANT", "год": YEAR, "трудоемкость": 12, "должность": "аналитик"},
            {"договор": "C_MINPROM", "год": YEAR, "трудоемкость": 10, "должность": "инженер"},
        ]
    )

    fot_matrix = pd.DataFrame(
        [
            _fot_row("C_BASE", {1: 600_000}),
            _fot_row("C_GOS", {3: 1_500_000}),
            _fot_row("C_GRANT", {1: 1_320_000}),
            _fot_row("C_MINPROM", {8: 1_500_000}),
        ]
    )

    min_balance = pd.DataFrame(
        [
            _zero_min_row("C_BASE"),
            _zero_min_row("C_GOS"),
            _zero_min_row("C_GRANT"),
            _zero_min_row("C_MINPROM"),
        ]
    )

    manual_assignments = pd.DataFrame(
        columns=["сотрудник", "договор", "год", "месяц с", "месяц по", "вид выплаты", "фикс сумма"]
    )
    manual_prohibitions = pd.DataFrame(
        columns=["сотрудник", "договор", "год", "месяц с", "месяц по", "вид выплаты"]
    )

    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        settings.to_excel(w, sheet_name="settings", index=False)
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        positions.to_excel(w, sheet_name="contract_positions", index=False)
        labor.to_excel(w, sheet_name="contract_labor", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)
        min_balance.to_excel(w, sheet_name="min_balance_matrix", index=False)
        manual_assignments.to_excel(w, sheet_name="manual_assignments", index=False)
        manual_prohibitions.to_excel(w, sheet_name="manual_prohibitions", index=False)

    return path


if __name__ == "__main__":
    out = create_demo_blocks()
    print(f"Created {out.resolve()}")
