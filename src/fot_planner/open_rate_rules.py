"""Открытые ставки: основное место и совместительство (шаг 0.25)."""

from __future__ import annotations

from typing import Literal

from fot_planner.position_reference import normalize_position

EmploymentCategory = Literal["regular", "student", "graduate_student"]
EmploymentType = Literal["auto", "main", "part_time"]

OPEN_RATE_STEP = 0.25
MAIN_RATE_MIN = 0.25
MAIN_RATE_MAX = 1.0
PART_RATE_MAX = 0.5
TOTAL_RATE_MAX_REGULAR = 1.5

MAIN_QUARTERS_MAX = int(MAIN_RATE_MAX / OPEN_RATE_STEP)  # 4
PART_QUARTERS_MAX = int(PART_RATE_MAX / OPEN_RATE_STEP)  # 2
TOTAL_QUARTERS_MAX_REGULAR = int(TOTAL_RATE_MAX_REGULAR / OPEN_RATE_STEP)  # 6
LABORATORY_ASSISTANT_POSITION_MARKERS = ("лаборант",)


def normalize_employment_category(raw: str | None) -> EmploymentCategory:
    if not raw:
        return "regular"
    key = str(raw).strip().lower().replace(" ", "_")
    if key in ("regular", "основной", "основная", "обычный"):
        return "regular"
    if key in ("student", "студент"):
        return "student"
    if key in ("graduate_student", "aspirant", "аспирант"):
        return "graduate_student"
    return "regular"


def _position_has_marker(position: str | None, markers: tuple[str, ...]) -> bool:
    normalized = normalize_position(position)
    return any(marker in normalized for marker in markers)


def is_laboratory_assistant_position(position: str | None) -> bool:
    """Лаборантам нельзя открывать доп. ставки сверх штатной."""
    return _position_has_marker(position, LABORATORY_ASSISTANT_POSITION_MARKERS)


def keeps_staff_rate_without_opening_extra(
    category: EmploymentCategory,
    position: str | None,
) -> bool:
    """Для этих строк доверяем вводу и не открываем ставки сверх указанной."""
    return category == "student" or is_laboratory_assistant_position(position)


def normalize_employment_type(raw: str | None) -> EmploymentType:
    if not raw:
        return "auto"
    key = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
    if key in ("main", "основное", "основная", "основной"):
        return "main"
    if key in (
        "part_time",
        "parttime",
        "совместительство",
        "по_совместительству",
        "совместитель",
    ):
        return "part_time"
    return "auto"


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


def row_max_total_quarters(
    category: EmploymentCategory,
    employment_type: EmploymentType,
    staff_rate: float,
    position: str | None,
) -> int:
    """Максимальная суммарная открытая ставка по строке сотрудника."""
    if employment_type == "main":
        max_q = MAIN_QUARTERS_MAX
    elif employment_type == "part_time":
        max_q = PART_QUARTERS_MAX
    else:
        max_q = max_total_quarters(category)

    if keeps_staff_rate_without_opening_extra(category, position):
        return staff_rate_min_quarters(staff_rate)
    return max_q


def quarters_to_rate(quarters: int) -> float:
    return quarters * OPEN_RATE_STEP


def salary_cap_multiplier(open_rate: float, staff_rate: float) -> float:
    """Множитель потолка оклада при открытой ставке на основном договоре."""
    if staff_rate <= 0:
        return 1.0
    return open_rate / staff_rate
