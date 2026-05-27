"""Равномерный план освоения ФОТ и отчёт план–факт (не физический перенос денег)."""

from __future__ import annotations

from fot_planner.fot_schedule import active_months_in_year, uniform_monthly_spend_target
from fot_planner.labor_rules import person_month_terms_for_labor, planned_labor_groups
from fot_planner.models import PlanningContext, PlanningResult


def _sum_terms(terms):
    terms = list(terms)
    if not terms:
        return 0
    return sum(terms) if len(terms) > 1 else terms[0]


def spent_or_zero(alloc, contract_id: str, month: int):
    """Сумма выплат с договора в месяце (все виды) или 0 для Pyomo."""
    terms = [alloc[k] for k in alloc if k[1] == contract_id and k[2] == month]
    if not terms:
        return 0
    return _sum_terms(terms)


def labor_pm_or_zero(
    employees: dict,
    uses: dict,
    contract_id: str,
    months: list[int],
    equivalence_group: str | None = None,
):
    """Накопленная трудоёмкость (оклад/salary) за указанные месяцы."""
    expr = person_month_terms_for_labor(
        employees,
        uses,
        contract_id,
        months,
        position=None,
        equivalence_group=equivalence_group,
    )
    return expr if expr is not None else 0


def planned_labor_pm_total(ctx: PlanningContext, contract_id: str) -> float:
    return sum(pm for _pos, pm in planned_labor_groups(ctx, contract_id))


def cumulative_salary_pm_from_result(
    result: PlanningResult,
    employees: dict,
    contract_id: str,
    through_month: int,
    equivalence_group: str | None = None,
) -> float:
    """Фактическая накопленная трудоёмкость по окладу до конца месяца (для отчёта)."""
    total = 0.0
    for month in range(1, through_month + 1):
        for alloc in result.allocations:
            if (
                alloc.contract_id != contract_id
                or alloc.month != month
                or alloc.payment_kind != "salary"
                or alloc.amount < 0.01
            ):
                continue
            employee = employees[alloc.employee_id]
            if equivalence_group and employee.equivalence_group != equivalence_group:
                continue
            total += employee.rate
    return total


def build_spend_plan_fact_dataframe(ctx: PlanningContext, result: PlanningResult):
    """Лист «освоение_план_факт»: равномерный план vs факт и резерв под трудоёмкость."""
    import pandas as pd

    employees = {e.id: e for e in ctx.employees}
    rows: list[dict] = []

    for contract in ctx.contracts:
        active = active_months_in_year(contract, ctx.year)
        if not active or contract.total_fot <= 0:
            continue

        ideal = uniform_monthly_spend_target(contract, ctx.year) or 0.0
        plan_pm = planned_labor_pm_total(ctx, contract.id)
        cost_per_pm = contract.total_fot / plan_pm if plan_pm > 0 else 0.0

        cum_plan = 0.0
        cum_actual = 0.0

        for m in range(1, 13):
            is_active = m in active
            actual = (
                sum(
                    a.amount
                    for a in result.allocations
                    if a.contract_id == contract.id and a.month == m
                )
                if is_active
                else 0.0
            )
            ideal_m = ideal if is_active else 0.0
            deviation = actual - ideal_m
            cum_plan += ideal_m
            cum_actual += actual
            cum_dev = cum_actual - cum_plan

            cum_labor = cumulative_salary_pm_from_result(result, employees, contract.id, m) if is_active else 0.0
            remaining_labor = max(0.0, plan_pm - cum_labor) if plan_pm > 0 else 0.0
            required_reserve = remaining_labor * cost_per_pm
            remaining_fot = contract.total_fot - cum_actual
            threshold_ok = remaining_fot + 1.0 >= required_reserve if plan_pm > 0 else True

            rows.append(
                {
                    "договор": contract.id,
                    "месяц": m,
                    "активный месяц": "да" if is_active else "нет",
                    "равномерный план": round(ideal_m, 2),
                    "факт выплат": round(actual, 2),
                    "отклонение": round(deviation, 2),
                    "накопленный план": round(cum_plan, 2),
                    "накопленный факт": round(cum_actual, 2),
                    "накопленное отклонение": round(cum_dev, 2),
                    "план трудоёмкости": round(plan_pm, 4) if m == active[0] else "",
                    "накопленная трудоёмкость": round(cum_labor, 4) if is_active else "",
                    "оставшаяся трудоёмкость": round(remaining_labor, 4) if is_active else "",
                    "требуемый резерв под трудоёмкость": round(required_reserve, 2) if is_active else "",
                    "остаток ФОТ после месяца": round(remaining_fot, 2) if is_active else "",
                    "порог выполнен": "да" if threshold_ok else "нет",
                }
            )

    return pd.DataFrame(rows)
