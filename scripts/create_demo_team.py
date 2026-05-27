"""
Демо «команда на 4 договорах»: поступления в месяц старта, carry вперёд, стабильность оклада.

Запуск из корня проекта:
  python scripts/create_demo_team.py
  fot-planner solve -i data/demo_team.xlsx -o data/demo_team_result.xlsx
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from fot_planner.excel_io import (
    SHEET_CONTRACT_LABOR,
    SHEET_CONTRACT_POSITIONS,
    SHEET_CONTRACTS,
    SHEET_EMPLOYEES,
    SHEET_FOT_MATRIX,
    SHEET_MANUAL_ASSIGNMENTS,
    SHEET_MANUAL_PROHIBITIONS,
    SHEET_MIN_BALANCE_MATRIX,
    SHEET_SETTINGS,
)

from fot_planner.excel_io import _format_workbook

from create_demo_input import (
    _contracts_sheet_dataframe,
    _contract_row,
)


def _fot_inflow_row(contract_id: str, inflow_by_month: dict[int, int]) -> dict:
    """Явные 0 в остальных месяцах — иначе после spread_fot остаются лишние поступления."""
    row: dict = {"договор": contract_id}
    for m in range(1, 13):
        row[str(m)] = inflow_by_month.get(m, 0)
    return row


def create_demo_team(path: str | Path = "data/demo_team.xlsx", *, year: int | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    year = year or date.today().year

    employees = [
        {
            "код": "E001",
            "фио": "Иванов И.И.",
            "должность": "ведущий инженер",
            "подразделение": "НИОКР",
            "ставка": 1.0,
            "оклад": 150_000,
            "надбавка": 30_000,
            "стимулирующая": 0,
            "дата начала": f"{year}-01-01",
            "дата окончания": "",
            "разрешенные договоры": "",
            "запрещенные договоры": "",
        },
        {
            "код": "E002",
            "фио": "Петров П.П.",
            "должность": "инженер-помощник",
            "подразделение": "НИОКР",
            "ставка": 1.0,
            "оклад": 90_000,
            "надбавка": 30_000,
            "стимулирующая": 0,
            "дата начала": f"{year}-01-01",
            "дата окончания": "",
            "разрешенные договоры": "",
            "запрещенные договоры": "",
        },
        {
            "код": "E003",
            "фио": "Сидоров С.С.",
            "должность": "аналитик",
            "подразделение": "аналитика",
            "ставка": 1.0,
            "оклад": 80_000,
            "надбавка": 30_000,
            "стимулирующая": 0,
            "дата начала": f"{year}-01-01",
            "дата окончания": "",
            "разрешенные договоры": "",
            "запрещенные договоры": "",
        },
        {
            "код": "E004",
            "фио": "Орлова О.О.",
            "должность": "конструктор",
            "подразделение": "КБ",
            "ставка": 1.0,
            "оклад": 70_000,
            "надбавка": 30_000,
            "стимулирующая": 0,
            "дата начала": f"{year}-01-01",
            "дата окончания": "",
            "разрешенные договоры": "",
            "запрещенные договоры": "",
        },
        {
            "код": "E005",
            "фио": "Кузнецов К.К.",
            "должность": "младший инженер",
            "подразделение": "НИОКР",
            "ставка": 0.5,
            # Итого 50k/мес; потолок по должности ×0.5 — оклад и надбавка отдельными строками
            "оклад": 30_000,
            "надбавка": 20_000,
            "стимулирующая": 0,
            "дата начала": f"{year}-04-01",
            "дата окончания": f"{year}-07-31",
            "разрешенные договоры": "",
            "запрещенные договоры": "",
        },
    ]

    contracts = [
        {
            **_contract_row(
                code="C_BASE",
                name="Базовый (янв–фев)",
                number=f"БЗ-{year}",
                contract_type="minprom",
                year=year,
                end_date=f"{year}-02-28",
                total_fot=600_000,
                carryover=True,
            ),
            "дата начала": f"{year}-01-01",
        },
        {
            **_contract_row(
                code="C_GOS",
                name="ГОЗ (март–дек)",
                number=f"ГЗ-{year}",
                contract_type="goszakaz",
                year=year,
                end_date=f"{year}-12-30",
                total_fot=1_500_000,
                months_after_end=-1,
                carryover=True,
            ),
            "дата начала": f"{year}-03-01",
        },
        {
            **_contract_row(
                code="C_GRANT",
                name="Грант (год)",
                number=f"ГР-{year}",
                contract_type="grant",
                year=year,
                end_date=f"{year}-12-31",
                total_fot=1_320_000,
                carryover=True,
            ),
            "дата начала": f"{year}-01-01",
        },
        {
            **_contract_row(
                code="C_MINPROM",
                name="Минпромторг (апр–дек)",
                number=f"МП-{year}",
                contract_type="minprom",
                year=year,
                end_date=f"{year}-12-31",
                total_fot=2_600_000,
                carryover=True,
            ),
            "дата начала": f"{year}-04-01",
        },
    ]

    # Потолок = оклад по должности (× ставка): оклад — одна строка, надбавка — вторая (не 200k «на всё»).
    caps_by_position = {
        "ведущий инженер": 150_000,
        "инженер-помощник": 90_000,
        "аналитик": 80_000,
        "конструктор": 70_000,
        "младший инженер": 50_000,
    }
    position_limits = [
        {"договор": cid, "должность": pos, "макс выплата": cap}
        for cid in ("C_BASE", "C_GOS", "C_GRANT", "C_MINPROM")
        for pos, cap in caps_by_position.items()
    ]

    labor_rows = [
        {"договор": "C_BASE", "год": year, "трудоемкость": 4, "должность": "инженер"},
        {"договор": "C_GOS", "год": year, "трудоемкость": 10, "должность": "инженер"},
        {"договор": "C_GRANT", "год": year, "трудоемкость": 12, "должность": "инженер"},
        {"договор": "C_MINPROM", "год": year, "трудоемкость": 21, "должность": "инженер"},
    ]

    fot_matrix = pd.DataFrame(
        [
            _fot_inflow_row("C_BASE", {1: 600_000}),
            _fot_inflow_row("C_GOS", {3: 1_500_000}),
            _fot_inflow_row("C_GRANT", {1: 1_320_000}),
            _fot_inflow_row("C_MINPROM", {4: 2_600_000}),
        ]
    )

    def _salary_bind(employee_id: str, contract_id: str, month_from: int, month_to: int) -> dict:
        return {
            "employee_id": employee_id,
            "contract_id": contract_id,
            "year": year,
            "month_from": month_from,
            "month_to": month_to,
            "payment_kind": "salary",
            "fixed_amount": "",
        }

    def _allowance_bind(
        employee_id: str, contract_id: str, month_from: int, month_to: int, amount: int
    ) -> dict:
        return {
            "employee_id": employee_id,
            "contract_id": contract_id,
            "year": year,
            "month_from": month_from,
            "month_to": month_to,
            "payment_kind": "allowance",
            "fixed_amount": amount,
        }

    def _pay_binds(
        employee_id: str,
        contract_id: str,
        month_from: int,
        month_to: int,
        allowance: int,
        *,
        fix_allowance: bool = True,
    ) -> list[dict]:
        rows = [_salary_bind(employee_id, contract_id, month_from, month_to)]
        if allowance > 0 and fix_allowance:
            rows.append(_allowance_bind(employee_id, contract_id, month_from, month_to, allowance))
        return rows

    # Оклад — договор; надбавка — фикс. на том же договоре (кроме MINPROM у Иванова/Петрова: иначе >2.6M ФОТ).
    manual_assignments: list[dict] = []
    for emp, contract, m_from, m_to, allowance, fix_allow in (
        ("E001", "C_BASE", 1, 2, 30_000, True),
        ("E001", "C_GOS", 3, 7, 30_000, True),
        ("E001", "C_MINPROM", 8, 12, 30_000, False),
        ("E002", "C_BASE", 1, 2, 30_000, True),
        ("E002", "C_GOS", 3, 7, 30_000, True),
        ("E002", "C_MINPROM", 8, 12, 30_000, False),
        ("E003", "C_GRANT", 1, 12, 30_000, False),
        ("E004", "C_MINPROM", 4, 12, 30_000, True),
        ("E005", "C_MINPROM", 4, 7, 20_000, True),
    ):
        manual_assignments.extend(_pay_binds(emp, contract, m_from, m_to, allowance, fix_allowance=fix_allow))

    readme = pd.DataFrame(
        [
            {"раздел": "Идея", "содержание": "Деньги в месяц старта договора; дальше расход через carry, не xfer назад"},
            {"раздел": "C_BASE", "содержание": "600k в янв.; Иванов+Петров янв–фев (4 чел.-мес.)"},
            {"раздел": "C_GOS", "содержание": "1.5M в марте; ГОЗ 10 чел.-мес. ±5%; ускоренное освоение мар–июль"},
            {"раздел": "C_GRANT", "содержание": "1.32M в янв.; Сидоров весь год — 0 смен оклада"},
            {
                "раздел": "C_MINPROM",
                "содержание": "2.6M в апр.; Орлова апр–дек; Кузнецов апр–июль (0.5 ставки); Иванов/Петров с авг.",
            },
            {"раздел": "E005", "содержание": "Кузнецов активен только апр–июль (2 чел.-мес. на Минпромторг)"},
            {
                "раздел": "Выплаты",
                "содержание": "Оклад+надбавка; потолок contract_positions < оклада → две строки на договоре",
            },
            {"раздел": "Оклад", "содержание": "1 договор/мес; ≤1 смена/квартал; штраф salary_change"},
            {
                "раздел": "manual_assignments",
                "содержание": "Оклад — договор; надбавка — фикс. сумма на том же договоре (для демо в плане)",
            },
            {"раздел": "Проверка", "содержание": "остатки_и_переносы — carry; переносы — пусто/мало"},
            {"раздел": "Файл", "содержание": str(path.name)},
        ]
    )

    settings = pd.DataFrame(
        [
            {
                "год": year,
                "штраф дефицита": 1_000_000,
                "вес штрафа смены оклада": 500_000,
                "вес отклонения равномерного освоения": 50_000,
                "вес штрафа смены надбавки": 300_000,
                "вес штрафа смены стимулирующей": 300_000,
                "фиксировать оклад в квартале": True,
                "макс договоров оклада в год": 3,
                "штраф смены оклада в квартале": True,
                "месяцев фот для резерва": 6,
                "допуск трудоемкости гоз": 0.05,
            }
        ]
    )

    contracts_df = _contracts_sheet_dataframe(contracts, position_limits, labor_rows, year)
    for i, row in enumerate(contracts):
        contracts_df.loc[contracts_df["код"] == row["код"], "мин. поступление/мес"] = 0

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        readme.to_excel(writer, sheet_name="readme", index=False)
        settings.to_excel(writer, sheet_name=SHEET_SETTINGS, index=False)
        pd.DataFrame(employees).to_excel(writer, sheet_name=SHEET_EMPLOYEES, index=False)
        contracts_df.to_excel(writer, sheet_name=SHEET_CONTRACTS, index=False)
        pd.DataFrame(position_limits).to_excel(writer, sheet_name=SHEET_CONTRACT_POSITIONS, index=False)
        pd.DataFrame(labor_rows).to_excel(writer, sheet_name=SHEET_CONTRACT_LABOR, index=False)
        fot_matrix.to_excel(writer, sheet_name=SHEET_FOT_MATRIX, index=False)
        pd.DataFrame(columns=["договор"] + [str(m) for m in range(1, 13)]).to_excel(
            writer, sheet_name=SHEET_MIN_BALANCE_MATRIX, index=False
        )
        pd.DataFrame(manual_assignments).to_excel(writer, sheet_name=SHEET_MANUAL_ASSIGNMENTS, index=False)
        pd.DataFrame(columns=["сотрудник", "договор", "год", "месяц с", "месяц по", "вид выплаты"]).to_excel(
            writer, sheet_name=SHEET_MANUAL_PROHIBITIONS, index=False
        )

    _format_workbook(path)
    return path


if __name__ == "__main__":
    out = create_demo_team()
    print(f"created {out.resolve()}")
