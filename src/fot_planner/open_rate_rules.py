"""Открытые ставки: основное место и совместительство (шаг 0.25)."""

from __future__ import annotations

from typing import Literal

EmploymentCategory = Literal["regular", "student", "graduate_student"]

OPEN_RATE_STEP = 0.25
MAIN_RATE_MIN = 0.25
MAIN_RATE_MAX = 1.0
PART_RATE_MAX = 0.5
TOTAL_RATE_MAX_REGULAR = 1.5

MAIN_QUARTERS_MAX = int(MAIN_RATE_MAX / OPEN_RATE_STEP)  # 4
PART_QUARTERS_MAX = int(PART_RATE_MAX / OPEN_RATE_STEP)  # 2
TOTAL_QUARTERS_MAX_REGULAR = int(TOTAL_RATE_MAX_REGULAR / OPEN_RATE_STEP)  # 6


def normalize_employment_category(raw: str | None) -> EmploymentCategory:
    if not raw:
        return "regular"
    key = str(raw).strip().lower().replace(" ", "_")
    if key in ("student", "студент"):
        return "student"
    if key in ("graduate_student", "aspirant", "аспирант"):
        return "graduate_student"
    return "regular"


def staff_rate_min_quarters(staff_rate: float) -> int:
    """Минимум четвертей ставки: штатную не уменьшаем."""
    if staff_rate <= 0:
        return 0
    return max(1, int(round(staff_rate / OPEN_RATE_STEP)))


def max_total_quarters(category: EmploymentCategory) -> int:
    if category == "student":
        return 2  # 0.5
    if category == "graduate_student":
        return 3  # 0.75
    return TOTAL_QUARTERS_MAX_REGULAR


def quarters_to_rate(quarters: int) -> float:
    return quarters * OPEN_RATE_STEP


def salary_cap_multiplier(open_rate: float, staff_rate: float) -> float:
    """Множитель потолка оклада при открытой ставке на основном договоре."""
    if staff_rate <= 0:
        return 1.0
    return open_rate / staff_rate
