"""Отчёты по диагностическому дефициту выплат."""

from __future__ import annotations

from fot_planner.models import PlanningContext, PlanningResult
from fot_planner.payment_split import employee_monthly_payment_due
from fot_planner.validation import employee_active_in_month


def build_deficits_detail_dataframe(ctx: PlanningContext, result: PlanningResult):
    import pandas as pd

    from fot_planner.excel import PAYMENT_KIND_RU, RU_MONTHS

    rows: list[dict] = []
    for d in result.deficits:
        emp = next((e for e in ctx.employees if e.id == d.employee_id), None)
        name = emp.full_name if emp else d.employee_id
        rows.append(
            {
                "сотрудник": name,
                "табельный номер": d.employee_id,
                "месяц": RU_MONTHS[d.month],
                "вид выплаты": PAYMENT_KIND_RU.get(d.payment_kind, "итого")
                if d.payment_kind
                else "итого",
                "требовалось выплатить": round(d.due_amount, 2),
                "выплачено": round(d.paid_amount, 2),
                "дефицит": round(d.amount, 2),
                "причина / комментарий": "; ".join(d.reasons),
            }
        )
    return pd.DataFrame(rows)


def build_deficit_by_month_dataframe(ctx: PlanningContext, result: PlanningResult):
    import pandas as pd

    from fot_planner.excel.constants import RU_MONTHS

    employees_by_id = {e.id: e for e in ctx.employees}
    cum_deficit = 0.0
    rows: list[dict] = []

    for m in range(1, 13):
        total_need = 0.0
        for e in ctx.employees:
            if employee_active_in_month(e, ctx.year, m):
                total_need += employee_monthly_payment_due(e)

        paid = sum(a.amount for a in result.allocations if a.month == m)
        month_deficit = sum(d.amount for d in result.deficits if d.month == m)
        cum_deficit += month_deficit

        rows.append(
            {
                "месяц": RU_MONTHS[m],
                "общая потребность": round(total_need, 2),
                "выплачено": round(paid, 2),
                "дефицит": round(month_deficit, 2),
                "накопленный дефицит": round(cum_deficit, 2),
            }
        )

    return pd.DataFrame(rows)
