"""
Лёгкий демо-файл: 5 сотрудников, 3 договора — все основные ограничения модели.

Запуск из корня проекта:
  python scripts/create_demo_light.py
  fot-planner solve -i data/demo_light.xlsx -o data/demo_light_result.xlsx
"""

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
    SHEET_MANUAL_ASSIGNMENTS,
    SHEET_MANUAL_PROHIBITIONS,
    SHEET_MIN_BALANCE_MATRIX,
    SHEET_SETTINGS,
    _format_workbook,
)

from create_demo_input import (
    CONTRACT_SHEET_COLUMNS,
    _contracts_sheet_dataframe,
    _contract_row,
)


def create_demo_light(path: str | Path = "data/demo_light.xlsx", *, year: int | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    year = year or date.today().year

    employees = [
        {
            "код": "E001",
            "фио": "Иванов И.И.",
            "должность": "инженер",
            "подразделение": "отдел НИОКР",
            "ставка": 1.0,
            "зарплата": 50_000,

            "дата начала": f"{year}-01-01",
            "дата окончания": "",
            "разрешенные договоры": "",
            "запрещенные договоры": "",
        },
        {
            "код": "E002",
            "фио": "Петров П.П.",
            "должность": "инженер",
            "подразделение": "отдел НИОКР",
            "ставка": 1.0,
            "зарплата": 42_000,

            "дата начала": f"{year}-01-01",
            "дата окончания": "",
            "разрешенные договоры": "",
            "запрещенные договоры": "",
        },
        {
            "код": "E003",
            "фио": "Сидорова С.С.",
            "должность": "инженер-лаборант",
            "подразделение": "лаборатория",
            "ставка": 0.5,
            "зарплата": 44_000,

            "дата начала": f"{year}-01-01",
            "дата окончания": "",
            "разрешенные договоры": "",
            "запрещенные договоры": "C_VB",
        },
        {
            "код": "E004",
            "фио": "Козлов К.К.",
            "должность": "инженер",
            "подразделение": "отдел НИОКР",
            "ставка": 1.0,
            "зарплата": 44_000,

            "дата начала": f"{year}-01-01",
            "дата окончания": "",
            "разрешенные договоры": "",
            "запрещенные договоры": "",
        },
        {
            "код": "E005",
            "фио": "Морозова М.М.",
            "должность": "инженер",
            "подразделение": "отдел НИОКР",
            "ставка": 1.0,
            "зарплата": 42_000,

            "дата начала": f"{year}-01-01",
            "дата окончания": "",
            "разрешенные договоры": "",
            "запрещенные договоры": "",
        },
    ]

    gos_row = _contract_row(
        code="C_GOS",
        name="ГОЗ (старт в марте)",
        number=f"ГЗ-{year}/1",
        contract_type="goszakaz",
        year=year,
        end_date=f"{year}-12-30",
        total_fot=580_000,
        carryover=True,
    )
    gos_row["дата начала"] = f"{year}-03-01"

    contracts = [
        gos_row,
        _contract_row(
            code="C_GRANT",
            name="Грант (весь год)",
            number=f"ГР-{year}/1",
            contract_type="grant",
            year=year,
            end_date=f"{year}-12-31",
            total_fot=1_064_000,
            carryover=True,
        ),
        _contract_row(
            code="C_VB",
            name="Внебюджет (+2 мес.)",
            number=f"ВБ-{year}/1",
            contract_type="off_budget",
            year=year,
            end_date=f"{year}-06-30",
            total_fot=600_000,
            flex_payment_deadline=f"{year}-08-31",
            carryover=True,
        ),
    ]

    position_limits = [
        {"договор": "C_GOS", "должность": "инженер", "макс выплата": 50_000},
        {"договор": "C_GOS", "должность": "инженер-лаборант", "макс выплата": 44_000},
        {"договор": "C_GRANT", "должность": "инженер", "макс выплата": 50_000},
        {"договор": "C_VB", "должность": "инженер", "макс выплата": 50_000},
    ]

    labor_rows = [
        {"договор": "C_GOS", "год": year, "трудоемкость": 10, "должность": "инженер"},
        {"договор": "C_GOS", "год": year, "трудоемкость": 2, "должность": "инженер-лаборант"},
        {"договор": "C_GRANT", "год": year, "трудоемкость": 12, "должность": "инженер"},
        {"договор": "C_VB", "год": year, "трудоемкость": 6, "должность": "инженер"},
    ]

    # Касса: авто-разнесение ФОТ по активным месяцам (ГОЗ — с марта, ~100k/мес).
    # Опционально: в fot_matrix можно задать пик в декабре для демо переносов.
    fot_matrix = pd.DataFrame(columns=["договор"] + [str(m) for m in range(1, 13)])

    min_balance_matrix = pd.DataFrame(columns=["договор"] + [str(m) for m in range(1, 13)])

    manual_assignments = [
        {
            "employee_id": "E001",
            "contract_id": "C_GRANT",
            "year": year,
            "month_from": 1,
            "month_to": 3,
            "payment_kind": "salary",
            "fixed_amount": "",
        },
    ]

    readme = pd.DataFrame(
        [
            {"раздел": "Сотрудники", "содержание": "5 чел.: инженер и лаборант (E003 запрещён на C_VB)"},
            {
                "раздел": "C_GOS",
                "содержание": "ГОЗ: март–дек, освоение за 20 дн. до конца, перенос; касса в дек.; труд ±5%",
            },
            {
                "раздел": "C_GRANT",
                "содержание": "Грант: год, перенос, резерв оклада (длинный ФОТ), мягкая трудоёмкость",
            },
            {
                "раздел": "C_VB",
                "содержание": "Внебюджет: янв–июн, +2 мес. выплаты, полное освоение к авг.",
            },
            {
                "раздел": "Жёстко",
                "содержание": "Полный ФОТ к сроку; касса; оклад с 1 договора/мес; макс. 2 договора оклада/год",
            },
            {
                "раздел": "Мягко",
                "содержание": "Дефицит, смена договора, трудоёмкость (не ГОЗ), переносы",
            },
            {"раздел": "Файл", "содержание": str(path.name)},
        ]
    )

    settings = pd.DataFrame(
        [
            {
                "год": year,
                "штраф дефицита": 1_000_000,
                "вес штрафа смены оклада": 500_000,
                "фиксировать оклад в квартале": False,
                "макс договоров оклада в год": 3,
                "штраф смены оклада в квартале": True,
                "месяцев фот для резерва": 6,
                "допуск трудоемкости гоз": 0.05,
            }
        ]
    )

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        readme.to_excel(writer, sheet_name="readme", index=False)
        settings.to_excel(writer, sheet_name=SHEET_SETTINGS, index=False)
        pd.DataFrame(employees).to_excel(writer, sheet_name=SHEET_EMPLOYEES, index=False)
        _contracts_sheet_dataframe(contracts, position_limits, labor_rows, year).to_excel(
            writer, sheet_name=SHEET_CONTRACTS, index=False
        )
        pd.DataFrame(position_limits).to_excel(writer, sheet_name=SHEET_CONTRACT_POSITIONS, index=False)
        pd.DataFrame(labor_rows).to_excel(writer, sheet_name=SHEET_CONTRACT_LABOR, index=False)
        fot_matrix.to_excel(writer, sheet_name=SHEET_FOT_MATRIX, index=False)
        min_balance_matrix.to_excel(writer, sheet_name=SHEET_MIN_BALANCE_MATRIX, index=False)
        pd.DataFrame(manual_assignments).to_excel(writer, sheet_name=SHEET_MANUAL_ASSIGNMENTS, index=False)
        pd.DataFrame(columns=["сотрудник", "договор", "год", "месяц с", "месяц по", "вид выплаты"]).to_excel(
            writer, sheet_name=SHEET_MANUAL_PROHIBITIONS, index=False
        )

    _format_workbook(path)
    return path


if __name__ == "__main__":
    out = create_demo_light()
    print(f"created {out.resolve()}")
