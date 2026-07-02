"""Экспорт результата планирования в Excel."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from fot_planner.models import PlanningContext, PlanningResult

def export_result(path: str | Path, ctx: PlanningContext, result: PlanningResult) -> None:
    path = Path(path)
    from fot_planner.excel.reports.labor_control import write_labor_control_sheet
    from fot_planner.excel.reports.staff_detail import write_staff_detail_sheet
    from fot_planner.excel.format import finalize_user_workbook, format_user_workbook
    from fot_planner.excel.reports.user_report import (
        SHEET_CONTRACT_FOT,
        SHEET_DEFICIT_MONTH,
        SHEET_DEFICITS,
        SHEET_ISSUES,
        SHEET_PLAN,
        SHEET_SUMMARY,
        build_user_excel_report,
    )

    report = build_user_excel_report(ctx, result)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        report.summary.to_excel(writer, sheet_name=SHEET_SUMMARY, index=False)
        report.issues.to_excel(writer, sheet_name=SHEET_ISSUES, index=False)
        report.plan.to_excel(writer, sheet_name=SHEET_PLAN, index=False)
        report.contract_fot.to_excel(writer, sheet_name=SHEET_CONTRACT_FOT, index=False)
        if any(d.amount > 0.005 for d in result.deficits):
            report.deficits.to_excel(writer, sheet_name=SHEET_DEFICITS, index=False)
            report.deficit_by_month.to_excel(writer, sheet_name=SHEET_DEFICIT_MONTH, index=False)

    format_user_workbook(path)
    write_labor_control_sheet(path, ctx, result)
    write_staff_detail_sheet(path, ctx, result)
    finalize_user_workbook(path)


