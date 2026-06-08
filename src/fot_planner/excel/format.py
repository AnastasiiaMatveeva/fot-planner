"""Оформление пользовательского выходного Excel."""

from __future__ import annotations

from pathlib import Path

from fot_planner.excel.reports.user_report import (
    MONTH_COLS,
    SHEET_ADMIN,
    SHEET_BALANCES,
    SHEET_CONTRACT_PAYMENTS,
    SHEET_DEFICIT_MONTH,
    SHEET_DEFICITS,
    SHEET_EMPLOYEE_PAYMENTS,
    SHEET_ISSUES,
    SHEET_LABOR_CONTROL,
    SHEET_LABOR_PAYMENTS,
    SHEET_PLAN,
    SHEET_POSITION,
    SHEET_README,
    SHEET_SCHEME,
    SHEET_SPEND_PLAN,
    SHEET_SPLIT,
    SHEET_SUMMARY,
)

MATRIX_SHEETS = frozenset(
    {
        SHEET_EMPLOYEE_PAYMENTS,
        SHEET_CONTRACT_PAYMENTS,
        SHEET_BALANCES,
        SHEET_SPEND_PLAN,
        SHEET_DEFICIT_MONTH,
    }
)

MONEY_HINTS = (
    "сумма",
    "выплат",
    "остаток",
    "поступ",
    "дефицит",
    "план",
    "факт",
    "отклон",
    "потреб",
    "накоп",
    "миним",
    "доступ",
    "значение",
    "итого",
)

PM_HINTS = ("чел", "мес", "труд")


def _is_money_header(header: str) -> bool:
    h = header.lower()
    return any(t in h for t in MONEY_HINTS)


def _is_pm_header(header: str) -> bool:
    h = header.lower()
    return any(t in h for t in PM_HINTS) and "сумма" not in h


def format_user_workbook(path: Path) -> None:
    from openpyxl import load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = load_workbook(path)
    header_fill = PatternFill(fill_type="solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    ok_fill = PatternFill(fill_type="solid", fgColor="C6EFCE")
    warn_fill = PatternFill(fill_type="solid", fgColor="FFEB9C")
    err_fill = PatternFill(fill_type="solid", fgColor="FFC7CE")

    for ws in wb.worksheets:
        if ws.max_row < 1:
            continue
        ws.freeze_panes = "A2"
        if ws.max_row > 1 and ws.title not in (SHEET_README,):
            ws.auto_filter.ref = ws.dimensions
        ws.sheet_view.showGridLines = False

        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.row_dimensions[1].height = 28

        if ws.title in MATRIX_SHEETS:
            ws.freeze_panes = "D2" if ws.max_column >= 4 else "B2"

        status_col = None
        level_col = None
        for idx, cell in enumerate(ws[1], start=1):
            val = str(cell.value or "").lower()
            if val == "статус":
                status_col = idx
            if val == "уровень":
                level_col = idx

        for col_idx, column in enumerate(ws.columns, start=1):
            letter = get_column_letter(col_idx)
            header = str(column[0].value or "")
            width = max(len(str(c.value or "")) for c in column)
            ws.column_dimensions[letter].width = min(max(width + 2, 10), 32)

            if _is_money_header(header) or header in MONTH_COLS:
                for cell in column[1:]:
                    if isinstance(cell.value, (int, float)):
                        cell.number_format = "#,##0"
            elif _is_pm_header(header):
                for cell in column[1:]:
                    if isinstance(cell.value, (int, float)):
                        cell.number_format = "0.00"

        if status_col or level_col:
            col = status_col or level_col
            for row in range(2, ws.max_row + 1):
                cell = ws.cell(row=row, column=col)
                val = str(cell.value or "").lower()
                if val in ("выполнено", "ок", "да") or val.startswith("выполнено по группе"):
                    cell.fill = ok_fill
                elif val.startswith("отклонение") or val in ("предупреждение", "warning"):
                    cell.fill = warn_fill
                elif val in ("ошибка", "error", "дефицит", "нет") or "ошибка" in val:
                    cell.fill = err_fill

        if ws.title == SHEET_SUMMARY:
            ws.sheet_properties.tabColor = "70AD47"
        elif ws.title == SHEET_ISSUES:
            ws.sheet_properties.tabColor = "FFC000"
        elif ws.title in (SHEET_EMPLOYEE_PAYMENTS, SHEET_CONTRACT_PAYMENTS):
            ws.sheet_properties.tabColor = "5B9BD5"
        elif ws.title == SHEET_LABOR_CONTROL:
            ws.sheet_properties.tabColor = "70AD47"
            ws.freeze_panes = "B2"

    wb.save(path)
