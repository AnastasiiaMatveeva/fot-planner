"""Generate a synthetic Russian Excel input workbook for demo/testing."""

from __future__ import annotations

from datetime import date, timedelta
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
    _canonicalize_columns,
    _format_workbook,
    _load_contracts,
)

SHEET_CONTRACT_TYPES = "contract_types"
from fot_planner.fot_schedule import spread_fot_by_active_months

CONTRACT_SHEET_COLUMNS = [
    "код",
    "название",
    "номер",
    "тип договора",
    "дата начала",
    "дата окончания",
    "срок освоения",
    "фот за год",
    "мин. фот (труд)",
    "мин. поступление/мес",
    "перенос остатков",
    "месяцев после окончания",
    "полное освоение за дней до срока",
    "оклад разрешен",
    "надбавка разрешена",
    "стимулирующая разрешена",
    "резерв оклада",
]


def _employees(year: int) -> list[dict]:
    rows = []
    for i in range(1, 101):
        rate = 0.5 if i % 5 == 0 else 1.0
        is_lab = i % 4 == 0
        position = "инженер-лаборант" if is_lab else "инженер"
        base_salary = 70_000 if is_lab else 90_000
        rows.append(
            {
                "код": f"E{i:03d}",
                "фио": f"Сотрудник {i:03d}",
                "должность": position,
                "подразделение": "лаборатория" if is_lab else "инженерный отдел",
                "ставка": rate,
                "оклад": base_salary * rate,
                "надбавка": 8_000 * rate,
                "стимулирующая": 12_000 * rate,
                "дата начала": f"{year}-01-01",
                "дата окончания": "",
                "разрешенные договоры": "",
                "запрещенные договоры": "",
            }
        )
    return rows


def _contract_types(*, tight: bool = False) -> pd.DataFrame:
    grant_spend_days = 20 if tight else ""
    return pd.DataFrame(
        [
            {
                "код типа": "goszakaz",
                "название": "Государственный заказ",
                "перенос остатков": False,
                "использовать после окончания": False,
                "месяцев после окончания": 0,
                "полное освоение за дней до срока": 20,
                "оклад разрешен": True,
                "надбавка разрешена": True,
                "стимулирующая разрешена": True,
            },
            {
                "код типа": "grant",
                "название": "Грант",
                "перенос остатков": True,
                "использовать после окончания": False,
                "месяцев после окончания": 0,
                "полное освоение за дней до срока": grant_spend_days,
                "оклад разрешен": True,
                "надбавка разрешена": True,
                "стимулирующая разрешена": True,
            },
            {
                "код типа": "minprom",
                "название": "Минпромторг",
                "перенос остатков": True,
                "использовать после окончания": False,
                "месяцев после окончания": 0,
                "полное освоение за дней до срока": "",
                "оклад разрешен": True,
                "надбавка разрешена": True,
                "стимулирующая разрешена": True,
            },
            {
                "код типа": "off_budget",
                "название": "Внебюджет",
                "перенос остатков": True,
                "использовать после окончания": True,
                "месяцев после окончания": 2,
                "полное освоение за дней до срока": "",
                "оклад разрешен": True,
                "надбавка разрешена": True,
                "стимулирующая разрешена": True,
            },
        ]
    )


def _contracts(year: int, grant_amounts: list[int], off_budget_amounts: list[int]) -> list[dict]:
    rows = []
    for idx, amount in enumerate(grant_amounts, 1):
        rows.append(
            {
                "код": f"G{idx:02d}",
                "название": f"Грант {idx}",
                "номер": f"ГР-{idx:02d}/{year}",
                "тип договора": "grant",
                "статус": "active",
                "дата начала": f"{year}-01-01",
                "дата окончания": f"{year}-12-31",
                "срок освоения": "",
                "фот": amount * 12,
                "приоритет": 1,
                "оклад разрешен": True,
                "надбавка разрешена": True,
                "стимулирующая разрешена": True,
                "вероятность": 1,
                "использовать после окончания": False,
                "месяцев после окончания": 0,
                "перенос остатков": True,
            }
        )
    for idx, amount in enumerate(off_budget_amounts, 1):
        rows.append(
            {
                "код": f"V{idx:02d}",
                "название": f"Внебюджет {idx}",
                "номер": f"ВБ-{idx:02d}/{year}",
                "тип договора": "off_budget",
                "статус": "active",
                "дата начала": f"{year}-01-01",
                "дата окончания": f"{year}-12-31",
                "срок освоения": "",
                "фот": amount * 12,
                "приоритет": 2,
                "оклад разрешен": True,
                "надбавка разрешена": True,
                "стимулирующая разрешена": True,
                "вероятность": 1,
                "использовать после окончания": True,
                "месяцев после окончания": 2,
                "перенос остатков": True,
            }
        )
    return rows


def _position_limits(contract_ids: list[str], *, stress: bool = False, tight: bool = False) -> list[dict]:
    """Потолок выплаты по должности; объём работ — в contract_labor (чел.-мес.)."""
    rows = []
    for contract_id in contract_ids:
        is_grant = contract_id.startswith("G")
        if tight:
            engineer_payment = 95_000
            lab_payment = 75_000
        elif stress:
            engineer_payment = 75_000 if is_grant else 60_000
            lab_payment = 55_000 if is_grant else 45_000
        else:
            engineer_payment = 120_000 if is_grant else 85_000
            lab_payment = 90_000 if is_grant else 65_000
        rows.append(
            {
                "договор": contract_id,
                "должность": "инженер",
                "макс выплата": engineer_payment,
            }
        )
        rows.append(
            {
                "договор": contract_id,
                "должность": "инженер-лаборант",
                "макс выплата": lab_payment,
            }
        )
    return rows


def _labor_rows(
    year: int,
    contract_ids: list[str],
    *,
    tight: bool = False,
    totals_by_contract: dict[str, int] | None = None,
) -> list[dict]:
    rows = []
    for contract_id in contract_ids:
        is_grant = contract_id.startswith("G")
        if tight and totals_by_contract:
            fot = totals_by_contract.get(contract_id, 0)
            engineer_pm = max(0.25, round(fot / 600_000, 2))
            lab_pm = max(0.1, round(fot / 1_000_000, 2))
        elif tight:
            engineer_pm = 6 if is_grant else 2
            lab_pm = 2 if is_grant else 1
        else:
            engineer_pm = 35 if is_grant else 8
            lab_pm = 12 if is_grant else 3
        rows.append(
            {
                "договор": contract_id,
                "год": year,
                "трудоемкость": engineer_pm,
                "должность": "инженер",
            }
        )
        rows.append(
            {
                "договор": contract_id,
                "год": year,
                "трудоемкость": lab_pm,
                "должность": "инженер-лаборант",
            }
        )
    return rows


def create_demo_input(path: str | Path = "data/demo_input.xlsx") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    year = date.today().year

    grant_amounts = [47_000_000, 44_000_000, 1_000_000]
    off_budget_amounts = [
        1_500_000,
        2_000_000,
        917_000,
        16_000,
        1_500_000,
        3_000_000,
        2_100_000,
        2_300_000,
        3_100_000,
        1_800_000,
        5_300_000,
        899_000,
        2_000_000,
        2_000_000,
    ]

    contract_ids = [f"G{i:02d}" for i in range(1, 4)] + [f"V{i:02d}" for i in range(1, 15)]
    monthly_amounts = {
        **{f"G{i:02d}": amount for i, amount in enumerate(grant_amounts, 1)},
        **{f"V{i:02d}": amount for i, amount in enumerate(off_budget_amounts, 1)},
    }

    fot_matrix = pd.DataFrame({"договор": contract_ids})
    min_balance_matrix = pd.DataFrame({"договор": contract_ids})
    for month in range(1, 13):
        fot_matrix[str(month)] = [monthly_amounts[contract_id] for contract_id in contract_ids]
        min_balance_matrix[str(month)] = ["" for _ in contract_ids]

    settings = pd.DataFrame(
        [
            {
                "год": year,
                "штраф дефицита": 1_000_000,
                "фиксировать оклад в квартале": True,
                "макс договоров оклада в год": 2,
                "штраф смены оклада в квартале": True,
                "месяцев фот для резерва": 6,
                "допуск трудоемкости гоз": 0.05,
            }
        ]
    )
    readme = pd.DataFrame(
        [
            {"лист": "employees", "описание": "100 сотрудников: инженер / инженер-лаборант, ставки 1.0 или 0.5"},
            {"лист": "contracts", "описание": "3 гранта и 14 внебюджетов"},
            {"лист": "contract_positions", "описание": "Лимиты ставок и максимальной выплаты по должности"},
            {"лист": "contract_labor", "описание": "План трудоёмкости, чел.-мес."},
            {"лист": "fot_matrix", "описание": "ФОТ по месяцам: суммы повторены на каждый месяц"},
            {"лист": "min_balance_matrix", "описание": "Неподвижные для переноса назад, сейчас пусто"},
        ]
    )

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        readme.to_excel(writer, sheet_name="readme", index=False)
        settings.to_excel(writer, sheet_name=SHEET_SETTINGS, index=False)
        pd.DataFrame(_employees(year)).to_excel(writer, sheet_name=SHEET_EMPLOYEES, index=False)
        _contract_types(tight=tight).to_excel(writer, sheet_name=SHEET_CONTRACT_TYPES, index=False)
        pd.DataFrame(_contracts(year, grant_amounts, off_budget_amounts)).to_excel(
            writer, sheet_name=SHEET_CONTRACTS, index=False
        )
        pd.DataFrame(_position_limits(contract_ids)).to_excel(
            writer, sheet_name=SHEET_CONTRACT_POSITIONS, index=False
        )
        pd.DataFrame(
            _labor_rows(
                year,
                contract_ids,
                tight=tight,
                totals_by_contract=totals_by_contract if tight else None,
            )
        ).to_excel(writer, sheet_name=SHEET_CONTRACT_LABOR, index=False)
        fot_matrix.to_excel(writer, sheet_name=SHEET_FOT_MATRIX, index=False)
        min_balance_matrix.to_excel(writer, sheet_name=SHEET_MIN_BALANCE_MATRIX, index=False)
        pd.DataFrame(columns=["сотрудник", "договор", "год", "месяц с", "месяц по", "вид выплаты", "фикс сумма"]).to_excel(
            writer, sheet_name=SHEET_MANUAL_ASSIGNMENTS, index=False
        )
        pd.DataFrame(columns=["сотрудник", "договор", "год", "месяц с", "месяц по", "вид выплаты"]).to_excel(
            writer, sheet_name=SHEET_MANUAL_PROHIBITIONS, index=False
        )

    _format_workbook(path, result=False)
    return path


if __name__ == "__main__":
    output = create_demo_input()
    print(f"created {output.resolve()}")
    stress_output = create_demo_input("data/demo_input_stress.xlsx", stress=True)
    print(f"created {stress_output.resolve()}")
    stress_output = create_demo_input("data/demo_input_stress.xlsx", stress=True)
    print(f"created {stress_output.resolve()}")
