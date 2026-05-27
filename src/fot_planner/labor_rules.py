"""Трудоёмкость (чел.-мес.) по договорам: план и ограничения для MIP."""

from __future__ import annotations

from fot_planner.models import Contract, ContractLaborPlan, Employee, PlanningContext, labor_row_id

GOZ_CONTRACT_TYPE = "goszakaz"


def is_goz_contract(contract: Contract) -> bool:
    return contract.contract_type == GOZ_CONTRACT_TYPE


def _labor_group(position: str | None, equivalence_group: str | None) -> str | None:
    """Ключ группировки трудоёмкости: сначала группа, иначе исходная должность."""
    return equivalence_group or position


def labor_rows_for_contract(ctx: PlanningContext, contract_id: str) -> list[tuple[int, ContractLaborPlan]]:
    """Строки трудоёмкости по договору: (индекс в ctx.labor_plans, строка)."""
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
    """План трудоёмкости по договору: группируем по группе взаимозаменяемости (legacy-отчёты)."""
    grouped: dict[str | None, float] = {}
    for _idx, lp in labor_rows_for_contract(ctx, contract_id):
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


def employee_compatible_with_labor_row(employee: Employee, lp: ContractLaborPlan) -> bool:
    """Сотрудник может закрывать строку трудоёмкости по группе взаимозаменяемости."""
    if lp.equivalence_group:
        return employee.equivalence_group == lp.equivalence_group
    if lp.position:
        return employee.position == lp.position
    return True


def contract_has_labor_plan(ctx: PlanningContext, contract_id: str) -> bool:
    """На договоре задана хотя бы одна строка contract_labor за год плана."""
    return bool(labor_rows_for_contract(ctx, contract_id))


def employee_compatible_with_contract_labor(
    ctx: PlanningContext, employee: Employee, contract_id: str
) -> bool:
    """Есть ли на договоре строка трудоёмкости, совместимая с сотрудником."""
    for _idx, lp in labor_rows_for_contract(ctx, contract_id):
        if employee_compatible_with_labor_row(employee, lp):
            return True
    return False


def compatible_labor_row_indices(
    ctx: PlanningContext, employee: Employee, contract_id: str
) -> list[int]:
    """Индексы строк ctx.labor_plans на договоре, совместимых с сотрудником."""
    out: list[int] = []
    for idx, lp in enumerate(ctx.labor_plans):
        if lp.contract_id != contract_id or lp.year != ctx.year or lp.person_months <= 0:
            continue
        if employee_compatible_with_labor_row(employee, lp):
            out.append(idx)
    return out


def labor_pm_terms_for_row(labor_pm: dict, lp_idx: int, months: list[int]):
    """Сумма labor_pm по строке за указанные месяцы."""
    terms = [
        var
        for (e_id, c_id, m, row_idx), var in labor_pm.items()
        if row_idx == lp_idx and m in months
    ]
    if not terms:
        return None
    return sum(terms) if len(terms) > 1 else terms[0]


def labor_payment_terms_for_row(labor_pay: dict, lp_idx: int, months: list[int]):
    """Сумма labor_payment_amount по строке за указанные месяцы."""
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


def person_month_terms_for_labor(
    employees: dict,
    uses: dict,
    contract_id: str,
    months: list[int],
    position: str | None = None,
    equivalence_group: str | None = None,
):
    """
    Чел.-мес. окладов на договоре (legacy: лимит ставок по группе на договоре).

    Используется в ограничении max_positions, не для fact_labor_pm по строкам.
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


__all__ = [
    "GOZ_CONTRACT_TYPE",
    "compatible_labor_row_indices",
    "contract_has_labor_plan",
    "employee_compatible_with_contract_labor",
    "employee_compatible_with_labor_row",
    "goz_allowed_positions",
    "is_goz_contract",
    "labor_average_balance_gap",
    "labor_payment_terms_for_row",
    "labor_pm_terms_for_row",
    "labor_row_id",
    "labor_rows_for_contract",
    "person_month_terms",
    "person_month_terms_for_labor",
    "planned_labor_amount",
    "planned_labor_groups",
    "total_planned_person_months",
]
