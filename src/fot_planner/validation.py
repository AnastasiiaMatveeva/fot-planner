"""Валидация входных данных перед расчётом."""

from __future__ import annotations

from datetime import date

from fot_planner.contract_calendar import contract_allows_month
from fot_planner.contract_types import KNOWN_CONTRACT_TYPES
from fot_planner.fot_schedule import cumulative_inflow_through_month
from fot_planner.labor_rules import (
    is_goz_contract,
    labor_rows_for_contract,
    planned_labor_amount,
    total_planned_person_months,
)
from fot_planner.contract_calendar import (
    latest_payment_month,
    payment_deadline_date,
    payment_kind_enabled,
    payment_month_count,
)
from fot_planner.models import PAYMENT_KINDS, ConflictRecord, PlanningContext

_KIND_LABELS = {
    "salary": "оклад",
    "allowance": "надбавка",
    "incentive": "стимулирующая",
}


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
        if e.monthly_wage < 0:
            conflicts.append(
                ConflictRecord(
                    code="INVALID_PAYMENT",
                    message=f"Сотрудник {e.id}: отрицательная зарплата",
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
        for kind in PAYMENT_KINDS:
            if not payment_kind_enabled(c, kind):
                continue
            terms = c.salary_terms if kind == "salary" else (
                c.allowance_terms if kind == "allowance" else c.incentive_terms
            )
            if terms.payment_deadline is None:
                conflicts.append(
                    ConflictRecord(
                        code="MISSING_PAYMENT_DEADLINE",
                        message=(
                            f"Договор {c.id}: укажите срок выплат "
                            f"({_KIND_LABELS[kind]}) — колонка "
                            f"«срок выплат {_KIND_LABELS[kind]}»"
                        ),
                        contract_id=c.id,
                    )
                )
            elif payment_month_count(c, ctx.year, kind) <= 0:
                conflicts.append(
                    ConflictRecord(
                        code="EMPTY_PAYMENT_WINDOW",
                        message=(
                            f"Договор {c.id}: нет месяцев в {ctx.year} для выплат "
                            f"({_KIND_LABELS[kind]}) до "
                            f"{payment_deadline_date(c, kind).isoformat()}"
                        ),
                        contract_id=c.id,
                    )
                )
    for c in ctx.contracts:
        for mb in c.monthly_budgets:
            if mb.inflow_amount < 0:
                conflicts.append(
                    ConflictRecord(
                        code="INVALID_BUDGET_INFLOW",
                        message=(
                            f"Договор {c.id}, месяц {mb.month}: "
                            f"отрицательное поступление ({mb.inflow_amount})"
                        ),
                        contract_id=c.id,
                        month=mb.month,
                    )
                )

    for e in ctx.employees:
        if e.monthly_wage > 0 and (
            e.reference_salary_for_rate is None or e.reference_salary_for_rate <= 0
        ):
            conflicts.append(
                ConflictRecord(
                    code="MISSING_ORG_SALARY_CAP_WARNING",
                    message=(
                        f"Сотрудник {e.id}: нет оклада по справочнику должностей "
                        f"(организационный потолок оклада) — salary может быть 0"
                    ),
                    employee_id=e.id,
                )
            )
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
        if c.allow_salary and not any(
            pr.max_monthly_payment is not None for pr in c.position_rules
        ):
            conflicts.append(
                ConflictRecord(
                    code="MISSING_SALARY_CAP_WARNING",
                    message=(
                        f"Договор {c.id}: разрешён оклад, но нет потолка в contract_positions "
                        f"(«макс выплата») — salary с договора будет 0"
                    ),
                    contract_id=c.id,
                )
            )
        for pr in c.position_rules:
            if pr.position and pr.equivalence_group is None:
                conflicts.append(
                    ConflictRecord(
                        code="UNKNOWN_CONTRACT_POSITION_WARNING",
                        message=(
                            f"Договор {c.id}: должность {pr.position!r} из contract_positions "
                            f"не найдена в справочнике должностей"
                        ),
                        contract_id=c.id,
                    )
                )
        for group in {pr.equivalence_group for pr in c.position_rules if pr.equivalence_group}:
            if group not in employee_groups:
                conflicts.append(
                    ConflictRecord(
                        code="NO_EMPLOYEE_FOR_POSITION_GROUP_WARNING",
                        message=(
                            f"Договор {c.id}: есть договорная позиция группы {group!r}, "
                            f"но нет сотрудников этой группы в справочнике"
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
                    code="UNKNOWN_LABOR_POSITION_WARNING",
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
                    code="NO_EMPLOYEE_FOR_LABOR_GROUP_WARNING",
                    message=(
                        f"Договор {lp.contract_id}: задана трудоёмкость по группе "
                        f"{lp.equivalence_group!r}, но нет сотрудников этой группы"
                    ),
                    contract_id=lp.contract_id,
                )
            )
        if lp.person_months > 0 and (lp.avg_monthly_labor_cost is None or lp.avg_monthly_labor_cost <= 0):
            conflicts.append(
                ConflictRecord(
                    code="MISSING_AVG_LABOR_COST_WARNING",
                    message=(
                        f"Договор {lp.contract_id}: для строки трудоёмкости "
                        f"({lp.position or lp.equivalence_group or 'без должности'}) "
                        f"нужна «средняя стоимость выполнения работ в месяц» > 0"
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
        if is_goz_contract(c) and ctx.salary_stability.goz_average_salary_limit > 0:
            planned_pm = total_planned_person_months(ctx, c.id)
            if planned_pm <= 0:
                conflicts.append(
                    ConflictRecord(
                        code="GOZ_BEP_WITHOUT_LABOR_WARNING",
                        message=(
                            f"Договор {c.id}: норматив средней зарплаты ГОЗ не применяется, "
                            "потому что не задана трудоёмкость в contract_labor"
                        ),
                        contract_id=c.id,
                    )
                )
            else:
                bep_fot_limit = (
                    ctx.salary_stability.goz_average_salary_limit * planned_pm
                )
                if c.total_fot > bep_fot_limit + 0.01:
                    conflicts.append(
                        ConflictRecord(
                            code="GOZ_FOT_ABOVE_BEP_LIMIT",
                            message=(
                                f"Договор {c.id}: ФОТ {c.total_fot:.2f} ₽ превышает "
                                f"лимит БЭП {bep_fot_limit:.2f} ₽ "
                                f"({planned_pm:.4g} чел.-мес. × "
                                f"{ctx.salary_stability.goz_average_salary_limit:.2f} ₽)"
                            ),
                            contract_id=c.id,
                        )
                    )

        if c.total_fot > 0:
            expected_labor_cost = sum(
                planned_labor_amount(lp)
                for _idx, lp in labor_rows_for_contract(ctx, c.id)
            )
            if expected_labor_cost > c.total_fot + 0.01:
                conflicts.append(
                    ConflictRecord(
                        code="FOT_BELOW_PLANNED_LABOR_COST_WARNING",
                        message=(
                            f"Договор {c.id}: по трудоёмкости ожидается "
                            f"{expected_labor_cost:.0f} ₽, лимит ФОТ {c.total_fot:.0f} ₽ — "
                            f"возможно, ФОТ недостаточен для заданной трудоёмкости"
                        ),
                        contract_id=c.id,
                    )
                )

        if c.requires_full_fot_spend:
            last_m = latest_payment_month(c, ctx.year)
            if last_m is not None:
                cum_inflow = cumulative_inflow_through_month(c, ctx.year, last_m)
                if cum_inflow + 0.01 < c.total_fot:
                    conflicts.append(
                        ConflictRecord(
                            code="FOT_INFLOW_SHORTFALL_WARNING",
                            message=(
                                f"Договор {c.id}: к последнему разрешённому месяцу "
                                f"выплат ({last_m}) поступает {cum_inflow:.0f} ₽ из "
                                f"{c.total_fot:.0f} ₽ ФОТ. Полное освоение ФОТ до срока "
                                f"выплат может быть невозможно"
                            ),
                            contract_id=c.id,
                            year=ctx.year,
                            month=last_m,
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
