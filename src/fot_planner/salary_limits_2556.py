"""Лимиты зарплаты по должностям: дефолты для шаблона и структуры загрузки."""

from __future__ import annotations

from dataclasses import dataclass

from fot_planner.defaults.position_limits import (
    P4_LIMIT_BY_CATEGORY_2025,
    P4_PERSONNEL_CATEGORIES,
    POSITION_SALARY_LIMIT_ROWS,
)

__all__ = [
    "P4_LIMIT_BY_CATEGORY_2025",
    "P4_PERSONNEL_CATEGORIES",
    "PositionLimit",
    "PositionSalaryLimit",
    "default_position_limit_tables",
    "default_position_salary_limits",
    "effective_p4_limit",
    "normalize_personnel_category",
    "p4_applies_to_category",
    "position_limit_tables_from_salary_limits",
]


@dataclass(frozen=True)
class PositionLimit:
    """Строка листа ограничения: должность и лимит на 1 ставку."""

    limit_code: str
    position: str
    personnel_category: str | None = None
    limit: float | None = None
    note: str | None = None


@dataclass(frozen=True)
class PositionSalaryLimit:
    """Строка единого справочника «должность — категория — лимиты»."""

    position: str
    personnel_category: str
    order_2556_limit: float | None = None
    p4_limit: float | None = None
    bep_limit: float | None = None
    note: str | None = None


def normalize_personnel_category(value: object) -> str:
    return str(value or "").strip().upper().replace("Ё", "Е")


def p4_applies_to_category(personnel_category: object) -> bool:
    return normalize_personnel_category(personnel_category) in P4_PERSONNEL_CATEGORIES


def effective_p4_limit(row: PositionSalaryLimit | None) -> float | None:
    """П4 для расчёта: только НТП/НР и только при заданном значении в справочнике."""
    if row is None or not p4_applies_to_category(row.personnel_category):
        return None
    limit = row.p4_limit
    if limit is None or limit <= 0:
        return None
    return float(limit)


def default_position_salary_limits() -> list[PositionSalaryLimit]:
    return [
        PositionSalaryLimit(
            position=position,
            personnel_category=personnel_category,
            order_2556_limit=order_2556_limit,
            p4_limit=P4_LIMIT_BY_CATEGORY_2025.get(personnel_category),
            bep_limit=None,
            note=note,
        )
        for position, personnel_category, order_2556_limit, note in POSITION_SALARY_LIMIT_ROWS
    ]


def position_limit_tables_from_salary_limits(
    rows: list[PositionSalaryLimit],
) -> dict[str, list[PositionLimit]]:
    tables = {
        "2556": [
            PositionLimit(
                limit_code="2556",
                position=row.position,
                personnel_category=row.personnel_category,
                limit=row.order_2556_limit,
                note=row.note,
            )
            for row in rows
        ],
        "p4": [
            PositionLimit(
                limit_code="p4",
                position=row.position,
                personnel_category=row.personnel_category,
                limit=row.p4_limit,
                note=None,
            )
            for row in rows
        ],
        "bep": [
            PositionLimit(
                limit_code="bep",
                position=row.position,
                personnel_category=row.personnel_category,
                limit=row.bep_limit,
                note=None,
            )
            for row in rows
        ],
    }
    return tables


def default_position_limit_tables() -> dict[str, list[PositionLimit]]:
    return position_limit_tables_from_salary_limits(default_position_salary_limits())
