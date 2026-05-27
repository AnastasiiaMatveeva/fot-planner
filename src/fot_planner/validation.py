"""Валидация входных данных перед расчётом."""

from __future__ import annotations

from datetime import date

from fot_planner.contract_calendar import contract_allows_month
from fot_planner.contract_types import KNOWN_CONTRACT_TYPES
from fot_planner.fot_schedule import active_months_in_year
from fot_planner.labor_rules import is_goz_contract, planned_labor_groups
from fot_planner.models import ConflictRecord, PlanningContext
from fot_planner.reserve_rules import fot_inflow_month_count
from fot_planner.spend_rules import payment_extension_months



def validate_context(ctx: PlanningContext) -> list[ConflictRecord]:
    conflicts: list[ConflictRecord] = []
    emp_ids = {e.id for e in ctx.employees}
    contract_ids = {c.id for c in ctx.contracts}

    for e in ctx.employees:
        if e.rate <= 0:
            conflicts.append(
                ConflictRecord(
                    code="INVALID_RATE",
                    message=f"Сотрудник {e.id}: ставка должна быть > 0",
                    employee_id=e.id,
                )
            )
        if e.salary < 0 or e.allowance < 0 or e.incentive < 0:
            conflicts.append(
                ConflictRecord(
                    code="INVALID_PAYMENT",
                    message=f"Сотрудник {e.id}: отрицательные выплаты",
                    employee_id=e.id,
                )
            )
        for cid in e.allowed_contracts:
            if cid not in contract_ids:
                conflicts.append(
                    ConflictRecord(
                        code="UNKNOWN_CONTRACT",
                        message=f"Сотрудник {e.id}: неизвестный договор {cid}",
                        employee_id=e.id,
                        contract_id=cid,
                    )
                )
        for cid in e.forbidden_contracts:
            if cid not in contract_ids:
                conflicts.append(
                    ConflictRecord(
                        code="UNKNOWN_CONTRACT",
                        message=f"Сотрудник {e.id}: неизвестный запрещённый договор {cid}",
                        employee_id=e.id,
                        contract_id=cid,
                    )
                )

    for c in ctx.contracts:
        if c.end_date < c.start_date:
            conflicts.append(
                ConflictRecord(
                    code="INVALID_DATES",
                    message=f"Договор {c.id}: дата окончания раньше начала",
                    contract_id=c.id,
                )
            )
        if c.total_fot < 0:
            conflicts.append(
                ConflictRecord(
                    code="INVALID_FOT",
                    message=f"Договор {c.id}: отрицательный ФОТ",
                    contract_id=c.id,
                )
            )
        if c.contract_type not in KNOWN_CONTRACT_TYPES:
            conflicts.append(
                ConflictRecord(
                    code="UNKNOWN_CONTRACT_TYPE",
                    message=(
                        f"Договор {c.id}: неизвестный тип {c.contract_type!r} "
                        f"(ожидается один из: {', '.join(sorted(KNOWN_CONTRACT_TYPES))})"
                    ),
                    contract_id=c.id,
                )
            )
    for c in ctx.contracts:
        for mb in c.monthly_budgets:
            if mb.lock and mb.inflow_amount <= 0:
                conflicts.append(
                    ConflictRecord(
                        code="LOCKED_BUDGET_EMPTY",
                        message=(
                            f"Договор {c.id}, месяц {mb.month}: lock=yes, "
                            f"но inflow_amount не задан"
                        ),
                        contract_id=c.id,
                        month=mb.month,
                    )
                )

    for e in ctx.employees:
        if e.position and e.equivalence_group is None:
            conflicts.append(
                ConflictRecord(
                    code="UNKNOWN_POSITION",
                    message=f"Сотрудник {e.id}: должность {e.position!r} не найдена в справочнике должностей",
                    employee_id=e.id,
                )
            )

    employee_groups = {e.equivalence_group for e in ctx.employees if e.equivalence_group}
    for c in ctx.contracts:
        for pr in c.position_rules:
            if pr.position and pr.equivalence_group is None:
                conflicts.append(
                    ConflictRecord(
                        code="UNKNOWN_CONTRACT_POSITION",
                        message=(
                            f"Договор {c.id}: должность {pr.position!r} из contract_positions "
                            f"не найдена в справочнике должностей"
                        ),
                        contract_id=c.id,
                    )
                )
        required_groups = {pr.equivalence_group for pr in c.position_rules if pr.equivalence_group}
        for group in required_groups:
            if group not in employee_groups:
                conflicts.append(
                    ConflictRecord(
                        code="NO_EMPLOYEE_FOR_POSITION_GROUP",
                        message=(
                            f"Договор {c.id}: есть договорная позиция группы {group!r}, "
                            f"но нет сотрудников этой группы"
                        ),
                        contract_id=c.id,
                    )
                )

    for ma in ctx.manual_assignments:
        if ma.employee_id not in emp_ids:
            conflicts.append(
                ConflictRecord(
                    code="UNKNOWN_EMPLOYEE",
                    message=f"Фиксация: неизвестный сотрудник {ma.employee_id}",
                    employee_id=ma.employee_id,
                )
            )
        if ma.contract_id not in contract_ids:
            conflicts.append(
                ConflictRecord(
                    code="UNKNOWN_CONTRACT",
                    message=f"Фиксация: неизвестный договор {ma.contract_id}",
                    contract_id=ma.contract_id,
                )
            )
        if not 1 <= ma.month_from <= 12 or not 1 <= ma.month_to <= 12:
            conflicts.append(
                ConflictRecord(
                    code="INVALID_MONTH",
                    message=f"Фиксация {ma.employee_id}/{ma.contract_id}: месяц вне 1–12",
                    employee_id=ma.employee_id,
                    contract_id=ma.contract_id,
                )
            )

    labor_by_contract: dict[str, float] = {}
    for lp in ctx.labor_plans:
        if lp.year != ctx.year:
            continue
        if lp.position and lp.equivalence_group is None:
            conflicts.append(
                ConflictRecord(
                    code="UNKNOWN_LABOR_POSITION",
                    message=(
                        f"Договор {lp.contract_id}: должность {lp.position!r} из contract_labor "
                        f"не найдена в справочнике должностей"
                    ),
                    contract_id=lp.contract_id,
                )
            )
        if lp.equivalence_group and lp.equivalence_group not in employee_groups:
            conflicts.append(
                ConflictRecord(
                    code="NO_EMPLOYEE_FOR_LABOR_GROUP",
                    message=(
                        f"Договор {lp.contract_id}: задана трудоёмкость по группе "
                        f"{lp.equivalence_group!r}, но нет сотрудников этой группы"
                    ),
                    contract_id=lp.contract_id,
                )
            )
        labor_by_contract[lp.contract_id] = labor_by_contract.get(lp.contract_id, 0.0) + lp.person_months

    for cid, plan_pm in labor_by_contract.items():
        contract = next((c for c in ctx.contracts if c.id == cid), None)
        if contract is None:
            continue
        if plan_pm <= 0:
            conflicts.append(
                ConflictRecord(
                    code="INVALID_LABOR",
                    message=f"Договор {cid}: трудоёмкость должна быть > 0",
                    contract_id=cid,
                )
            )
            continue
        if contract.total_fot <= 0:
            conflicts.append(
                ConflictRecord(
                    code="LABOR_WITHOUT_FOT",
                    message=f"Договор {cid}: задана трудоёмкость, но ФОТ ≤ 0",
                    contract_id=cid,
                )
            )

    for c in ctx.contracts:
        plan_pm = sum(pm for _pos, pm in planned_labor_groups(ctx, c.id))
        if plan_pm > 0 and c.total_fot > 0:
            cost_per_pm = c.total_fot / plan_pm
            min_pay = min(
                (e.salary + e.allowance + e.incentive) * e.rate
                for e in ctx.employees
                if e.rate > 0
            )
            if cost_per_pm < min_pay * 0.25:
                conflicts.append(
                    ConflictRecord(
                        code="LOW_COST_PER_PM_WARNING",
                        message=(
                            f"Договор {c.id}: стоимость 1 чел.-мес. ({cost_per_pm:.0f}) "
                            f"мала относительно выплат сотрудников"
                        ),
                        contract_id=c.id,
                    )
                )
        if (
            is_goz_contract(c)
            and plan_pm > 0
            and c.total_fot > 0
            and not ctx.allow_backward_reallocation
        ):
            active = active_months_in_year(c, ctx.year)
            max_pm = sum(
                e.rate
                for e in ctx.employees
                if e.rate > 0 and contract_allows_month(c, ctx.year, active[-1] if active else 12)
            )
            if plan_pm > len(active) * max_pm + 0.01:
                conflicts.append(
                    ConflictRecord(
                        code="LABOR_CAPACITY_WARNING",
                        message=(
                            f"Договор {c.id} (ГОЗ): план {plan_pm:.1f} чел.-мес. "
                            f"может не набраться доступными сотрудниками"
                        ),
                        contract_id=c.id,
                    )
                )

        if not c.allow_monthly_carryover and c.total_fot > 0:
            inflow_months = fot_inflow_month_count(c)
            if inflow_months == 1 and len(active_months_in_year(c, ctx.year)) > 1:
                conflicts.append(
                    ConflictRecord(
                        code="CARRYOVER_DISABLED_WARNING",
                        message=(
                            f"Договор {c.id}: перенос остатков выключен, "
                            f"но поступление в одном месяце при {len(active_months_in_year(c, ctx.year))} "
                            f"активных — возможна нерешаемость"
                        ),
                        contract_id=c.id,
                    )
                )

    for mp in ctx.manual_prohibitions:
        if mp.employee_id not in emp_ids:
            conflicts.append(
                ConflictRecord(
                    code="UNKNOWN_EMPLOYEE",
                    message=f"Запрет: неизвестный сотрудник {mp.employee_id}",
                    employee_id=mp.employee_id,
                )
            )
        if mp.contract_id not in contract_ids:
            conflicts.append(
                ConflictRecord(
                    code="UNKNOWN_CONTRACT",
                    message=f"Запрет: неизвестный договор {mp.contract_id}",
                    contract_id=mp.contract_id,
                )
            )

    return conflicts


def employee_active_in_month(employee, year: int, month: int) -> bool:
    period_start = date(year, month, 1)
    if month == 12:
        period_end = date(year, 12, 31)
    else:
        period_end = date(year, month + 1, 1)
        period_end = date.fromordinal(period_end.toordinal() - 1)

    if employee.start_date and employee.start_date > period_end:
        return False
    if employee.end_date and employee.end_date < period_start:
        return False
    return True
