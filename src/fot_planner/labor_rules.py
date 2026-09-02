"""Трудоёмкость (чел.-мес.) по договорам: план, посадка сотрудника, ограничения для MIP."""

from __future__ import annotations

from fot_planner.models import (
    Contract,
    ContractLaborPlan,
    ContractPositionRule,
    Employee,
    PlanningContext,
)
from fot_planner.position_reference import normalize_position


def _same_position_text(left: str | None, right: str | None) -> bool:
    left_key = normalize_position(left)
    right_key = normalize_position(right)
    return bool(left_key and right_key and left_key == right_key)


def employee_matches_position_spec(
    employee: Employee,
    *,
    position: str | None,
    equivalence_group: str | None,
) -> bool:
    """
    Можно ли посадить сотрудника на должность/группу договора или строки трудоёмкости.

    1. Должность/группа не заданы — посадка без ограничения.
    2. Должность совпала — можно.
    3. Замена через окладную группу: она симметрична, «инженер» и
       «программист» взаимозаменяемы в обе стороны.
    4. Замена по правилам замещения: они направленные. Главного инженера
       проекта можно заместить инженером, обратное неверно, и симметричная
       группа этого не выражает — она либо пускает обоих, либо никого.
       Правило спрашивается со стороны сотрудника: закрываемая должность
       должна быть среди тех, которые он может заместить.
    """
    if not position and not equivalence_group:
        return True
    if _same_position_text(employee.position, position):
        return True
    if _same_position_text(employee.equivalence_group, equivalence_group):
        return True
    if position and normalize_position(position) in employee.can_substitute:
        return True
    return False


def employee_compatible_with_labor_row(employee: Employee, lp: ContractLaborPlan) -> bool:
    """Сотрудник может закрывать строку трудоёмкости."""
    return employee_matches_position_spec(
        employee,
        position=lp.position,
        equivalence_group=lp.equivalence_group,
    )


def employee_compatible_with_position_rule(
    employee: Employee, rule: ContractPositionRule
) -> bool:
    """Сотрудник подходит под договорную должность/группу."""
    return employee_matches_position_spec(
        employee,
        position=rule.position,
        equivalence_group=rule.equivalence_group,
    )


def employee_can_place_on_contract(
    employee: Employee, contract: Contract, ctx: PlanningContext
) -> bool:
    """
    Можно ли сотрудника посадить на договор (трудоёмкость → ставка → оклад).

    С трудоёмкостью — хотя бы одна подходящая строка labor.
    Без трудоёмкости — хотя бы одна договорная должность/группа (или ограничений нет).
    """
    labor_rows = labor_rows_for_contract(ctx, contract.id)
    if labor_rows:
        return any(
            employee_compatible_with_labor_row(employee, lp) for _idx, lp in labor_rows
        )
    if contract.position_rules:
        return any(
            employee_compatible_with_position_rule(employee, pr)
            for pr in contract.position_rules
        )
    return True


def _labor_group(position: str | None, equivalence_group: str | None) -> str | None:
    """Ключ группировки трудоёмкости: сначала группа, иначе исходная должность."""
    return equivalence_group or position

def labor_rows_for_contract(ctx: PlanningContext, contract_id: str) -> list[tuple[int, ContractLaborPlan]]:
    """Какие строки трудоёмкости у договора и под какими номерами они лежат в общем списке"""
    rows: list[tuple[int, ContractLaborPlan]] = []
    for idx, lp in enumerate(ctx.labor_plans):
        if lp.contract_id == contract_id and lp.year == ctx.year and lp.person_months > 0:
            rows.append((idx, lp))
    return rows


def planned_labor_amount(lp: ContractLaborPlan) -> float:
    """Плановая сумма по строке: чел.-мес. × средняя зарплата."""
    avg = lp.avg_monthly_labor_cost or 0.0
    return lp.person_months * avg


def planned_labor_groups(ctx: PlanningContext, contract_id: str) -> list[tuple[str | None, float]]:
    """План трудоёмкости по договору: группируем по группе взаимозаменяемости."""
    grouped: dict[str | None, float] = {}
    for _idx, lp in labor_rows_for_contract(ctx, contract_id):
        group = _labor_group(lp.position, lp.equivalence_group)
        grouped[group] = grouped.get(group, 0.0) + lp.person_months
    return [(group, person_months) for group, person_months in grouped.items()]


def total_planned_person_months(ctx: PlanningContext, contract_id: str) -> float:
    return sum(pm for _group, pm in planned_labor_groups(ctx, contract_id))


def contract_has_labor_plan(ctx: PlanningContext, contract_id: str) -> bool:
    """На договоре задана трудоёмкость."""
    return bool(labor_rows_for_contract(ctx, contract_id))


def labor_pm_terms_for_row(labor_pm: dict, lp_idx: int, months: list[int]):
    """Сумма человеко/месяцев за указанные месяцы."""
    terms = [
        var
        for (e_id, c_id, m, row_idx), var in labor_pm.items()
        if row_idx == lp_idx and m in months
    ]
    if not terms:
        return None
    return sum(terms) if len(terms) > 1 else terms[0]


def labor_payment_terms_for_row(labor_pay: dict, lp_idx: int, months: list[int]):
    """Сумма выплат по строке трудоёмкости (должность/группа) за указанные месяцы."""
    terms = [
        var
        for (e_id, c_id, m, kind, row_idx), var in labor_pay.items()
        if row_idx == lp_idx and m in months
    ]
    if not terms:
        return None
    return sum(terms) if len(terms) > 1 else terms[0]


def labor_average_balance_gap(amount_expr, pm_expr, plan_pm: float, plan_amount: float):
    """
    Линейная мера расхождения средней по строке.

    Ноль, когда fact_amount / fact_pm = plan_amount / plan_pm (средняя сходится).
    """
    return amount_expr * plan_pm - pm_expr * plan_amount
