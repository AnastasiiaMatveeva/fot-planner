"""Трудоёмкость (чел.-мес.) по договорам: план и ограничения для MIP."""

from __future__ import annotations

from fot_planner.models import Contract, PlanningContext

GOZ_CONTRACT_TYPE = "goszakaz"


def is_goz_contract(contract: Contract) -> bool:
    return contract.contract_type == GOZ_CONTRACT_TYPE


def _labor_group(position: str | None, equivalence_group: str | None) -> str | None:
    """Ключ группировки трудоёмкости: сначала группа, иначе исходная должность."""
    return equivalence_group or position


def planned_labor_groups(ctx: PlanningContext, contract_id: str) -> list[tuple[str | None, float]]:
    """План трудоёмкости по договору: группируем по группе взаимозаменяемости."""
    grouped: dict[str | None, float] = {}
    for lp in ctx.labor_plans:
        if lp.contract_id != contract_id or lp.year != ctx.year:
            continue
        group = _labor_group(lp.position, lp.equivalence_group)
        grouped[group] = grouped.get(group, 0.0) + lp.person_months
    return [(group, person_months) for group, person_months in grouped.items()]


def total_planned_person_months(ctx: PlanningContext, contract_id: str) -> float:
    return sum(pm for _group, pm in planned_labor_groups(ctx, contract_id))


def goz_allowed_positions(contract: Contract, ctx: PlanningContext) -> set[str] | None:
    """ГОЗ: должности из labor (если заданы), иначе из contract_positions."""
    from_labor = {
        lp.position
        for lp in ctx.labor_plans
        if lp.contract_id == contract.id and lp.year == ctx.year and lp.position
    }
    if from_labor:
        return from_labor
    if contract.position_rules:
        return {pr.position for pr in contract.position_rules}
    return None


def person_month_terms_for_labor(
    employees: dict,
    uses: dict,
    contract_id: str,
    months: list[int],
    position: str | None = None,
    equivalence_group: str | None = None,
):
    """
    Чел.-мес. окладов на договоре.

    Если задана equivalence_group, учитываем только сотрудников этой группы.
    Если группы нет, но задана position, используем старую проверку по точной должности.
    """
    terms = []
    for (e_id, cid, m, kind), uvar in uses.items():
        if cid != contract_id or kind != "salary" or m not in months:
            continue
        employee = employees[e_id]
        if equivalence_group and employee.equivalence_group != equivalence_group:
            continue
        if not equivalence_group and position and employee.position != position:
            continue
        terms.append(employee.rate * uvar)
    if not terms:
        return None
    return sum(terms) if len(terms) > 1 else terms[0]


def person_month_terms(employees: dict, uses: dict, contract_id: str, months: list[int]):
    return person_month_terms_for_labor(employees, uses, contract_id, months, position=None)
