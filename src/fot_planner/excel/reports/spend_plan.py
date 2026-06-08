"""Отчёт «освоение план–факт»: касса и отклонение от равномерного плана освоения ФОТ."""

from __future__ import annotations

from fot_planner.fot_schedule import active_months_in_year, monthly_spend_targets
from fot_planner.labor_rules import planned_labor_groups
from fot_planner.models import PlanningContext, PlanningResult


def cumulative_labor_pm_from_result(
    result: PlanningResult,
    contract_id: str,
    through_month: int,
) -> float:
    """Фактическая накопленная трудоёмкость до конца месяца (из labor_pm_attributions)."""
    return sum(
        rec.person_months
        for rec in result.labor_pm_attributions
        if rec.contract_id == contract_id and rec.month <= through_month
    )


def planned_labor_pm_total(ctx: PlanningContext, contract_id: str) -> float:
    return sum(pm for _pos, pm in planned_labor_groups(ctx, contract_id))


def build_spend_plan_fact_dataframe(ctx: PlanningContext, result: PlanningResult):
    """Длинный формат для листа «освоение_план_факт»."""
    import pandas as pd

    balances_by_key = {(b.contract_id, b.month): b for b in result.contract_balances}
    rows: list[dict] = []

    for contract in ctx.contracts:
        active = active_months_in_year(contract, ctx.year)
        if not active or contract.total_fot <= 0:
            continue

        targets = monthly_spend_targets(contract, ctx.year)
        plan_pm = planned_labor_pm_total(ctx, contract.id)

        cum_plan = 0.0
        cum_actual = 0.0

        for m in range(1, 13):
            is_active = m in active
            balance = balances_by_key.get((contract.id, m))
            actual = balance.spent if balance and is_active else 0.0
            inflow = balance.inflow if balance else 0.0
            opening = balance.opening_balance if balance else 0.0
            closing = balance.closing_balance if balance else 0.0

            ideal_m = targets.get(m, 0.0) if is_active else 0.0
            deviation = actual - ideal_m
            cum_plan += ideal_m
            cum_actual += actual
            cum_dev = cum_actual - cum_plan

            cum_labor = (
                cumulative_labor_pm_from_result(result, contract.id, m) if is_active else 0.0
            )
            remaining_fot = contract.total_fot - cum_actual

            rows.append(
                {
                    "договор": contract.id,
                    "месяц": m,
                    "активный месяц": "да" if is_active else "нет",
                    "поступление": round(inflow, 2) if is_active else "",
                    "остаток на начало": round(opening, 2) if is_active else "",
                    "факт выплат": round(actual, 2) if is_active else "",
                    "остаток на конец": round(closing, 2) if is_active else "",
                    "равномерный план": round(ideal_m, 2),
                    "отклонение от равномерного плана": round(deviation, 2),
                    "накопленный план": round(cum_plan, 2),
                    "накопленный факт": round(cum_actual, 2),
                    "накопленное отклонение": round(cum_dev, 2),
                    "план трудоёмкости": round(plan_pm, 4) if m == active[0] else "",
                    "накопленная трудоёмкость": round(cum_labor, 4) if is_active else "",
                    "остаток ФОТ после месяца": round(remaining_fot, 2) if is_active else "",
                }
            )

    return pd.DataFrame(rows)
