"""One-off: split excel_io.py into fot_planner/excel/ package."""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "fot_planner"
EXCEL = ROOT / "excel"
REPORTS = EXCEL / "reports"


def slice_file(path: Path, start: int, end: int) -> str:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(lines[start - 1 : end])


def main() -> None:
    EXCEL.mkdir(exist_ok=True)
    REPORTS.mkdir(exist_ok=True)

    src_io = ROOT / "excel_io.py"
    lines = src_io.read_text(encoding="utf-8").splitlines(keepends=True)

    def sl(start: int, end: int) -> str:
        return "".join(lines[start - 1 : end])

    (EXCEL / "constants.py").write_text(
        '"""Имена листов, алиасы колонок, подписи месяцев."""\n\n'
        "from __future__ import annotations\n\n"
        + sl(48, 187),
        encoding="utf-8",
    )

    (EXCEL / "parsing.py").write_text(
        '"""Парсинг ячеек и канонизация колонок Excel."""\n\n'
        "from __future__ import annotations\n\n"
        "from datetime import date, datetime\n\n"
        "import pandas as pd\n\n"
        "from fot_planner.excel.constants import COLUMN_ALIASES\n\n"
        + sl(190, 284),
        encoding="utf-8",
    )

    load_imports = '''"""Загрузка PlanningContext из Excel."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

from fot_planner.contract_types import CONTRACT_TYPE_DEFAULTS, KNOWN_CONTRACT_TYPES
from fot_planner.excel.constants import (
    SHEET_CONTRACT_BUDGET,
    SHEET_CONTRACT_LABOR,
    SHEET_CONTRACT_POSITIONS,
    SHEET_CONTRACTS,
    SHEET_EMPLOYEES,
    SHEET_FOT_MATRIX,
    SHEET_MANUAL_ASSIGNMENTS,
    SHEET_MANUAL_PROHIBITIONS,
    SHEET_META,
    SHEET_MIN_BALANCE_MATRIX,
    SHEET_PLAN,
    SHEET_POSITION_REFERENCE,
    SHEET_POSITION_SYNONYMS,
    SHEET_SETTINGS,
)
from fot_planner.excel.parsing import (
    _bool,
    _canonicalize_columns,
    _canonicalize_manual_columns,
    _load_spend_complete_days,
    _month_from_column,
    _optional_bool,
    _optional_float,
    _optional_int,
    _parse_date,
    _resolve_monthly_wage,
    _split_list,
)
from fot_planner.fot_schedule import default_fot_inflow_at_start
from fot_planner.labor_rules import employee_compatible_with_labor_row
from fot_planner.models import (
    AllocationRecord,
    Contract,
    ContractLaborPlan,
    ContractMonthlyBudget,
    ContractPositionRule,
    Employee,
    ManualAssignment,
    ManualProhibition,
    OptimizationWeights,
    PaymentKind,
    PlanningContext,
    PositionReference,
    SalaryStabilityRules,
    labor_row_id,
)
from fot_planner.position_reference import (
    PositionReferenceRow,
    build_position_index,
    default_position_reference,
    default_position_synonyms,
    normalize_position,
    resolve_position,
)

'''
    (EXCEL / "load.py").write_text(load_imports + sl(285, 1025), encoding="utf-8")

    export_tables_imports = '''"""Таблицы для технического/внутреннего экспорта Excel."""

from __future__ import annotations

from collections import defaultdict

import pandas as pd

from fot_planner.excel.constants import PAYMENT_KIND_RU, RU_MONTHS
from fot_planner.labor_rules import labor_rows_for_contract, planned_labor_groups
from fot_planner.models import PlanningContext, PlanningResult, labor_row_id

'''
    (EXCEL / "export_tables.py").write_text(
        export_tables_imports + sl(1027, 1403).replace("_PAYMENT_KIND_RU", "PAYMENT_KIND_RU"),
        encoding="utf-8",
    )

    workbook_format_imports = '''"""Оформление входного и результирующего Excel (openpyxl)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from fot_planner.excel.constants import SHEET_FOT_MATRIX
from fot_planner.excel.parsing import _month_from_column

'''
    (EXCEL / "workbook_format.py").write_text(
        workbook_format_imports + sl(1405, 1515),
        encoding="utf-8",
    )

    export_body = sl(1516, 1561)
    export_body = export_body.replace(
        "from fot_planner.labor_control_sheet import write_labor_control_sheet",
        "from fot_planner.excel.reports.labor_control import write_labor_control_sheet",
    )
    export_body = export_body.replace(
        "from fot_planner.user_excel_format import format_user_workbook",
        "from fot_planner.excel.format import format_user_workbook",
    )
    export_body = export_body.replace(
        "from fot_planner.user_excel_report import",
        "from fot_planner.excel.reports.user_report import",
    )
    (EXCEL / "export.py").write_text(
        '"""Экспорт результата планирования в Excel."""\n\n'
        "from __future__ import annotations\n\n"
        "from pathlib import Path\n\n"
        "import pandas as pd\n\n"
        "from fot_planner.models import PlanningContext, PlanningResult\n\n"
        + export_body,
        encoding="utf-8",
    )

    template_imports = '''"""Шаблон входного Excel."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import PatternFill

from fot_planner.excel.constants import (
    SHEET_CONTRACT_LABOR,
    SHEET_CONTRACT_POSITIONS,
    SHEET_CONTRACTS,
    SHEET_EMPLOYEES,
    SHEET_FOT_MATRIX,
    SHEET_MANUAL_ASSIGNMENTS,
    SHEET_MANUAL_PROHIBITIONS,
    SHEET_MIN_BALANCE_MATRIX,
    SHEET_POSITION_REFERENCE,
    SHEET_POSITION_SYNONYMS,
    SHEET_SETTINGS,
)
from fot_planner.excel.workbook_format import format_workbook
from fot_planner.fot_schedule import default_fot_inflow_at_start
from fot_planner.position_reference import default_position_reference, default_position_synonyms

'''
    template_body = sl(1562, len(lines))
    template_body = template_body.replace("_format_workbook(", "format_workbook(")
    template_body = template_body.replace("_mark_template_locked_fot_cells", "mark_template_locked_fot_cells")
    (EXCEL / "template.py").write_text(
        template_imports
        + template_body.replace(
            "def mark_template_locked_fot_cells",
            "def mark_template_locked_fot_cells",
        ).replace(
            "def create_template",
            "def create_template",
        ),
        encoding="utf-8",
    )
    # rename private helpers in template
    tpl = (EXCEL / "template.py").read_text(encoding="utf-8")
    tpl = tpl.replace("def _mark_template_locked_fot_cells", "def mark_template_locked_fot_cells")
    if "mark_template_locked_fot_cells(path" in tpl and "_mark_template_locked_fot_cells(path" not in tpl:
        tpl = tpl.replace("_mark_template_locked_fot_cells(path", "mark_template_locked_fot_cells(path")
    (EXCEL / "template.py").write_text(tpl, encoding="utf-8")

    # Move report modules
    moves = [
        ("user_excel_format.py", EXCEL / "format.py"),
        ("user_excel_report.py", REPORTS / "user_report.py"),
        ("labor_control_sheet.py", REPORTS / "labor_control.py"),
        ("deficit_report.py", REPORTS / "deficit.py"),
        ("admin_complexity_report.py", REPORTS / "admin_complexity.py"),
    ]
    for src_name, dst in moves:
        if not (ROOT / src_name).exists():
            continue
        text = (ROOT / src_name).read_text(encoding="utf-8")
        text = text.replace("fot_planner.user_excel_report", "fot_planner.excel.reports.user_report")
        text = text.replace("fot_planner.user_excel_format", "fot_planner.excel.format")
        text = text.replace("fot_planner.labor_control_sheet", "fot_planner.excel.reports.labor_control")
        text = text.replace("fot_planner.excel", "fot_planner.excel")
        text = text.replace(
            "from fot_planner.excel import _labor_by_row_dataframe",
            "from fot_planner.excel.export_tables import labor_by_row_dataframe",
        )
        text = text.replace(
            "from fot_planner.excel import _position_control_dataframe",
            "from fot_planner.excel.export_tables import position_control_dataframe",
        )
        text = text.replace("_labor_by_row_dataframe", "labor_by_row_dataframe")
        text = text.replace("_position_control_dataframe", "position_control_dataframe")
        text = text.replace("fot_planner.excel import RU_MONTHS, _PAYMENT_KIND_RU", "fot_planner.excel.constants import PAYMENT_KIND_RU, RU_MONTHS")
        text = text.replace("_PAYMENT_KIND_RU", "PAYMENT_KIND_RU")
        text = text.replace("fot_planner.excel import RU_MONTHS", "fot_planner.excel.constants import RU_MONTHS")
        dst.write_text(text, encoding="utf-8")

    # Rename PAYMENT_KIND in constants
    const = (EXCEL / "constants.py").read_text(encoding="utf-8")
    const = const.replace("_PAYMENT_KIND_RU", "PAYMENT_KIND_RU")
    (EXCEL / "constants.py").write_text(const, encoding="utf-8")

    # export_tables: rename public functions
    et = (EXCEL / "export_tables.py").read_text(encoding="utf-8")
    for old, new in [
        ("def _position_control_dataframe", "def position_control_dataframe"),
        ("def _labor_by_group_dataframe", "def labor_by_group_dataframe"),
        ("def _labor_by_row_dataframe", "def labor_by_row_dataframe"),
        ("def _labor_payment_report_dataframe", "def labor_payment_report_dataframe"),
        ("def _project_monthly_grid", "def project_monthly_grid"),
        ("def _result_readable_tables", "def result_readable_tables"),
        ("def _export_balances_sheet", "def export_balances_sheet"),
        ("def _balance_export_columns", "def balance_export_columns"),
    ]:
        et = et.replace(old, new)
    (EXCEL / "export_tables.py").write_text(et, encoding="utf-8")

    wb = (EXCEL / "workbook_format.py").read_text(encoding="utf-8")
    wb = wb.replace("def _format_workbook", "def format_workbook")
    wb = wb.replace("def _format_result_workbook", "def format_result_workbook")
    wb = wb.replace("def _format_result_workbook_sheets", "def format_result_workbook_sheets")
    wb = wb.replace("_format_result_workbook_sheets(wb)", "format_result_workbook_sheets(wb)")
    (EXCEL / "workbook_format.py").write_text(wb, encoding="utf-8")

    # format.py: rename format_user_workbook if needed
    fmt = (EXCEL / "format.py").read_text(encoding="utf-8")
    fmt = fmt.replace("SHEET_LABOR_CONTROL", "SHEET_LABOR_CONTROL")
    (EXCEL / "format.py").write_text(fmt, encoding="utf-8")

    # __init__.py
    init = '''"""Excel: чтение входа, шаблон, экспорт результата и пользовательские отчёты."""

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
    SHEET_SETTINGS,
)
from fot_planner.excel.export import export_result
from fot_planner.excel.export_tables import (
    labor_by_row_dataframe,
    position_control_dataframe,
)
from fot_planner.excel.load import load_context
from fot_planner.excel.template import create_template
from fot_planner.excel.workbook_format import format_workbook

# Backward-compatible private names used by scripts/tests
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
    "SHEET_SETTINGS",
    "create_template",
    "export_result",
    "format_workbook",
    "load_context",
    "labor_by_row_dataframe",
    "position_control_dataframe",
    "_format_workbook",
    "_labor_by_row_dataframe",
    "_position_control_dataframe",
    "_PAYMENT_KIND_RU",
]
'''
    (EXCEL / "__init__.py").write_text(init, encoding="utf-8")
    (REPORTS / "__init__.py").write_text(
        '"""Пользовательские листы Excel."""\n',
        encoding="utf-8",
    )

    # Facade excel_io.py
    facade = '''"""Обратная совместимость: используйте fot_planner.excel."""

from fot_planner.excel import *  # noqa: F403
from fot_planner.excel import (
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
    SHEET_SETTINGS,
    _PAYMENT_KIND_RU,
    _format_workbook,
    _labor_by_row_dataframe,
    _position_control_dataframe,
    create_template,
    export_result,
    load_context,
)

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
    "SHEET_SETTINGS",
    "_PAYMENT_KIND_RU",
    "_format_workbook",
    "_labor_by_row_dataframe",
    "_position_control_dataframe",
    "create_template",
    "export_result",
    "load_context",
]
'''
    (ROOT / "excel_io.py").write_text(facade, encoding="utf-8")

    # Thin re-exports for old module paths
    for name, target in [
        ("user_excel_format.py", "from fot_planner.excel.format import *  # noqa: F403\n"),
        ("user_excel_report.py", "from fot_planner.excel.reports.user_report import *  # noqa: F403\n"),
        ("labor_control_sheet.py", "from fot_planner.excel.reports.labor_control import *  # noqa: F403\n"),
        ("deficit_report.py", "from fot_planner.excel.reports.deficit import *  # noqa: F403\n"),
        ("admin_complexity_report.py", "from fot_planner.excel.reports.admin_complexity import *  # noqa: F403\n"),
    ]:
        (ROOT / name).write_text(target, encoding="utf-8")

    print("Done:", EXCEL)


if __name__ == "__main__":
    main()
