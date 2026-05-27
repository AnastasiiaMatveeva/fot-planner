"""Стабильность назначения оклада по кварталам и году."""

from __future__ import annotations

QUARTER_MONTHS: tuple[tuple[int, ...], ...] = (
    (1, 2, 3),
    (4, 5, 6),
    (7, 8, 9),
    (10, 11, 12),
)

# Месяцы, в которых фиксируется смена договора оклада (переход m-1 → m).
QUARTER_SALARY_CHANGE_MONTHS: tuple[tuple[int, ...], ...] = (
    (2, 3),
    (4, 5, 6),
    (7, 8, 9),
    (10, 11, 12),
)

QUARTER_START_MONTHS: frozenset[int] = frozenset(m[0] for m in QUARTER_MONTHS[1:])


def is_quarter_boundary_month(month: int) -> bool:
    """Первый месяц квартала (кроме января) — частный случай перехода в salary_change."""
    return month in QUARTER_START_MONTHS
