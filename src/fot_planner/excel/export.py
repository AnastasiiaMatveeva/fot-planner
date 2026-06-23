"""Экспорт результата планирования в Excel."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from fot_planner.models import PlanningContext, PlanningResult

def export_result(path: str | Path, ctx: PlanningContext, result: PlanningResult) -> None:
    path = Path(path)
    from fot_planner.excel.reports.labor_control import write_labor_control_sheet
    from fot_planner.excel.reports.staff_detail import write_staff_detail_sheet
    from fot_planner.excel.format import format_user_workbook
    from fot_planner.excel.reports.user_report import (
        SHEET_ADMIN,
        SHEET_BALANCES,
        SHEET_CONTRACT_PAYMENTS,
        SHEET_DEFICIT_MONTH,
        SHEET_DEFICITS,
        SHEET_EMPLOYEE_PAYMENTS,
        SHEET_ISSUES,
        SHEET_LABOR_PAYMENTS,
        SHEET_PLAN,
        SHEET_POSITION,
        SHEET_README,
        SHEET_SCHEME,
        SHEET_SPEND_PLAN,
        SHEET_SPLIT,
        SHEET_SUMMARY,
        build_user_excel_report,
    )

    report = build_user_excel_report(ctx, result)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        report.readme.to_excel(writer, sheet_name=SHEET_README, index=False)
        report.summary.to_excel(writer, sheet_name=SHEET_SUMMARY, index=False)
        report.issues.to_excel(writer, sheet_name=SHEET_ISSUES, index=False)
        report.employee_payments.to_excel(writer, sheet_name=SHEET_EMPLOYEE_PAYMENTS, index=False)
        report.contract_payments.to_excel(writer, sheet_name=SHEET_CONTRACT_PAYMENTS, index=False)
        report.balances.to_excel(writer, sheet_name=SHEET_BALANCES, index=False)
        report.spend_plan.to_excel(writer, sheet_name=SHEET_SPEND_PLAN, index=False)
        report.plan.to_excel(writer, sheet_name=SHEET_PLAN, index=False)
        report.labor_payments.to_excel(writer, sheet_name=SHEET_LABOR_PAYMENTS, index=False)
        report.position_control.to_excel(writer, sheet_name=SHEET_POSITION, index=False)
        report.admin.to_excel(writer, sheet_name=SHEET_ADMIN, index=False)
        report.split_check.to_excel(writer, sheet_name=SHEET_SPLIT, index=False)
        report.scheme_changes.to_excel(writer, sheet_name=SHEET_SCHEME, index=False)
        report.deficits.to_excel(writer, sheet_name=SHEET_DEFICITS, index=False)
        report.deficit_by_month.to_excel(writer, sheet_name=SHEET_DEFICIT_MONTH, index=False)

    format_user_workbook(path)
    write_labor_control_sheet(path, ctx, result)
    write_staff_detail_sheet(path, ctx, result)


