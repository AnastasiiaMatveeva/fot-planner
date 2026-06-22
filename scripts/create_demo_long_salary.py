"""
Единственное активное демо: оклады на длинных (max_contracts_per_year=1), надбавки на краткосрочных.

  python scripts/create_demo_long_salary.py
  fot-planner solve -i data/demo_long_salary.xlsx -o data/demo_long_salary_result.xlsx
  python scripts/analyze_long_salary_result.py
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from fot_planner.excel import create_template

YEAR = 2026

# ФОТ: годовой фонд оплаты команды = 5 400 000 (130+100+115+105) × 12.
# Длинные договоры — оклады и часть надбавок; краткие — сезон надбавок.
FOT_LONG_ENG = 2_570_000
FOT_LONG_ANALYTICS = 2_180_000
# Краткие: касса на сезон надбавок; суммарно с длинными укладывается в годовой ФОТ команды
FOT_SHORT = 325_000


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


def _contract(
    code: str,
    name: str,
    number: str,
    ctype: str,
    start: str,
    end: str,
    fot: int,
) -> dict:
    return {
        "код": code,
        "название": name,
        "номер": number,
        "тип договора": ctype,
        "дата начала": start,
        "дата окончания": end,
        "срок освоения": "",
        "фот": fot,
        "оклад разрешен": True,
        "надбавка разрешена": True,
        "стимулирующая разрешена": True,
        "месяцев после окончания": 0,
        "перенос остатков": True,
        "резерв оклада": False,
        "полное освоение за дней до срока": "",
    }


def create_demo_long_salary(path: str | Path = "data/demo_long_salary.xlsx") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    create_template(path)

    settings = pd.DataFrame(
        [
            {
                "год": YEAR,
                "штраф дефицита": 1_000_000,
                "макс договоров оклада в год": 1,
                "месяцев фот для резерва": 6,
                "допуск трудоемкости гоз": 0.05,
                "вес отклонения равномерного освоения": 50_000,
                "штраф административной сложности выплат": 200_000,
                "штраф дробления переменных выплат": 200_000,
            }
        ]
    )

    employees = pd.DataFrame(
        [
            {
                "код": "E001",
                "фио": "Иванов",
                "должность": "ведущий инженер",
                "подразделение": "РНД",
                "ставка": 1.0,
                "зарплата": 130_000,

                "дата начала": f"{YEAR}-01-01",
                "дата окончания": "",
                "разрешенные договоры": "",
                "запрещенные договоры": "",
            },
            {
                "код": "E002",
                "фио": "Петров",
                "должность": "инженер",
                "подразделение": "РНД",
                "ставка": 1.0,
                "зарплата": 100_000,

                "дата начала": f"{YEAR}-01-01",
                "дата окончания": "",
                "разрешенные договоры": "",
                "запрещенные договоры": "",
            },
            {
                "код": "E003",
                "фио": "Сидоров",
                "должность": "аналитик",
                "подразделение": "РНД",
                "ставка": 1.0,
                "зарплата": 115_000,

                "дата начала": f"{YEAR}-01-01",
                "дата окончания": "",
                "разрешенные договоры": "",
                "запрещенные договоры": "",
            },
            {
                "код": "E004",
                "фио": "Орлова",
                "должность": "конструктор",
                "подразделение": "РНД",
                "ставка": 1.0,
                "зарплата": 105_000,

                "дата начала": f"{YEAR}-01-01",
                "дата окончания": "",
                "разрешенные договоры": "",
                "запрещенные договоры": "",
            },
        ]
    )

    contracts = pd.DataFrame(
        [
            _contract(
                "C_LONG_ENG",
                "Длинный инженерный",
                "L-ENG",
                "grant",
                f"{YEAR}-01-01",
                f"{YEAR}-12-31",
                FOT_LONG_ENG,
            ),
            _contract(
                "C_LONG_ANALYTICS",
                "Длинный аналитический",
                "L-AN",
                "grant",
                f"{YEAR}-01-01",
                f"{YEAR}-12-31",
                FOT_LONG_ANALYTICS,
            ),
            _contract(
                "C_SHORT_A",
                "Краткосрочный A",
                "S-A",
                "grant",
                f"{YEAR}-04-01",
                f"{YEAR}-09-30",
                FOT_SHORT,
            ),
            _contract(
                "C_SHORT_B",
                "Краткосрочный B",
                "S-B",
                "grant",
                f"{YEAR}-07-01",
                f"{YEAR}-12-31",
                FOT_SHORT,
            ),
        ]
    )

    positions = pd.DataFrame(
        [
            {"договор": "C_LONG_ENG", "должность": "ведущий инженер", "макс выплата": 100_000},
            {"договор": "C_LONG_ENG", "должность": "инженер", "макс выплата": 80_000},
            {"договор": "C_LONG_ANALYTICS", "должность": "аналитик", "макс выплата": 90_000},
            {"договор": "C_LONG_ANALYTICS", "должность": "конструктор", "макс выплата": 80_000},
            {"договор": "C_SHORT_A", "должность": "ведущий инженер", "макс выплата": 100_000},
            {"договор": "C_SHORT_A", "должность": "инженер", "макс выплата": 80_000},
            {"договор": "C_SHORT_A", "должность": "аналитик", "макс выплата": 90_000},
            {"договор": "C_SHORT_A", "должность": "конструктор", "макс выплата": 80_000},
            {"договор": "C_SHORT_B", "должность": "ведущий инженер", "макс выплата": 100_000},
            {"договор": "C_SHORT_B", "должность": "инженер", "макс выплата": 80_000},
            {"договор": "C_SHORT_B", "должность": "аналитик", "макс выплата": 90_000},
            {"договор": "C_SHORT_B", "должность": "конструктор", "макс выплата": 80_000},
        ]
    )

    fot_matrix = pd.DataFrame(
        [
            _fot_row("C_LONG_ENG", {1: FOT_LONG_ENG}),
            _fot_row("C_LONG_ANALYTICS", {1: FOT_LONG_ANALYTICS}),
            _fot_row("C_SHORT_A", {4: FOT_SHORT}),
            _fot_row("C_SHORT_B", {7: FOT_SHORT}),
        ]
    )

    min_balance = pd.DataFrame([_zero_min_row(c) for c in ("C_LONG_ENG", "C_LONG_ANALYTICS", "C_SHORT_A", "C_SHORT_B")])

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
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)
        min_balance.to_excel(w, sheet_name="min_balance_matrix", index=False)
        manual_assignments.to_excel(w, sheet_name="manual_assignments", index=False)
        manual_prohibitions.to_excel(w, sheet_name="manual_prohibitions", index=False)

    return path


if __name__ == "__main__":
    out = create_demo_long_salary()
    print(f"Created {out.resolve()}")
