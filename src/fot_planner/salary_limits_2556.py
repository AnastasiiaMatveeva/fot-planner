"""Редактируемые лимиты зарплаты по должностям."""

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
    "PositionSalaryLimit",
    "default_position_salary_limits",
    "effective_p4_limit",
    "normalize_personnel_category",
    "p4_applies_to_category",
]


@dataclass(frozen=True)
class PositionSalaryLimit:
    """Строка единого справочника «должность — категория — лимиты»."""

    position: str
    personnel_category: str
    order_2556_limit: float | None = None
    p4_limit: float | None = None
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
            note=note,
        )
        for position, personnel_category, order_2556_limit, note in POSITION_SALARY_LIMIT_ROWS
    ]
