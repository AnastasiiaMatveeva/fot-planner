"""Excel: чтение входа, шаблон, экспорт результата и пользовательские отчёты."""

from fot_planner.excel.constants import (
    COLUMN_ALIASES,
    PAYMENT_KIND_RU,
    RU_MONTHS,
    SHEET_CONTRACT_BUDGET,
    SHEET_CONTRACT_LABOR,
    SHEET_CONTRACT_POSITIONS,
    SHEET_CONTRACTS,
    SHEET_EMPLOYEES,
    SHEET_FOT_LOCK_MATRIX,
    SHEET_FOT_MATRIX,
    SHEET_MANUAL_ASSIGNMENTS,
    SHEET_MANUAL_PROHIBITIONS,
    SHEET_META,
    SHEET_MIN_BALANCE_MATRIX,
    SHEET_POSITION_SALARY_LIMITS,
    SHEET_SETTINGS,
)
from fot_planner.excel.export import export_result
from fot_planner.excel.export_tables import (
    labor_by_row_dataframe,
    position_control_dataframe,
)
from fot_planner.excel.load import _load_contracts, load_context
from fot_planner.excel.parsing import _canonicalize_columns
from fot_planner.excel.template import create_template
from fot_planner.excel.workbook_format import format_workbook

# Aliases for scripts/tests that used excel_io private helpers
_format_workbook = format_workbook
_position_control_dataframe = position_control_dataframe
_labor_by_row_dataframe = labor_by_row_dataframe
_PAYMENT_KIND_RU = PAYMENT_KIND_RU

__all__ = [
    "COLUMN_ALIASES",
    "PAYMENT_KIND_RU",
    "RU_MONTHS",
    "SHEET_CONTRACT_BUDGET",
    "SHEET_CONTRACT_LABOR",
    "SHEET_CONTRACT_POSITIONS",
    "SHEET_CONTRACTS",
    "SHEET_EMPLOYEES",
    "SHEET_FOT_LOCK_MATRIX",
    "SHEET_FOT_MATRIX",
    "SHEET_MANUAL_ASSIGNMENTS",
    "SHEET_MANUAL_PROHIBITIONS",
    "SHEET_META",
    "SHEET_MIN_BALANCE_MATRIX",
    "SHEET_POSITION_SALARY_LIMITS",
    "SHEET_SETTINGS",
    "create_template",
    "export_result",
    "format_workbook",
    "load_context",
    "labor_by_row_dataframe",
    "position_control_dataframe",
    "_canonicalize_columns",
    "_format_workbook",
    "_labor_by_row_dataframe",
    "_load_contracts",
    "_position_control_dataframe",
    "_PAYMENT_KIND_RU",
]
