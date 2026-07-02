"""Редактируемые значения по умолчанию для fot-planner.

Все константы и справочники, которые задают бизнес-правила и заполняют шаблон Excel,
собраны в подмодулях этого пакета. Логика расчёта остаётся в остальных модулях.
"""

from fot_planner.defaults.position_limits import (
    P4_LIMIT_BY_CATEGORY_2025,
    P4_PERSONNEL_CATEGORIES,
    POSITION_SALARY_LIMIT_ROWS,
)
from fot_planner.defaults.position_reference import (
    PERSONNEL_CATEGORIES_BY_POSITION,
    POSITION_REFERENCE_ROWS,
    POSITION_SYNONYMS,
)
from fot_planner.defaults.settings import (
    DEFAULT_GOZ_AVERAGE_SALARY_LIMIT,
    DEFAULT_GOZ_LABOR_TOLERANCE,
    DEFAULT_SETTINGS_ROW,
)

__all__ = [
    "DEFAULT_GOZ_AVERAGE_SALARY_LIMIT",
    "DEFAULT_GOZ_LABOR_TOLERANCE",
    "DEFAULT_SETTINGS_ROW",
    "P4_LIMIT_BY_CATEGORY_2025",
    "P4_PERSONNEL_CATEGORIES",
    "PERSONNEL_CATEGORIES_BY_POSITION",
    "POSITION_REFERENCE_ROWS",
    "POSITION_SALARY_LIMIT_ROWS",
    "POSITION_SYNONYMS",
]
