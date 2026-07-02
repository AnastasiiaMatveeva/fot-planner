"""Валидация входных данных перед расчётом."""

from __future__ import annotations

from datetime import date

from fot_planner.contract_calendar import (
    payment_deadline_date,
    payment_kind_enabled,
    payment_month_count,
)
from fot_planner.models import ConflictRecord, PAYMENT_KINDS, PaymentKind, PlanningContext
from fot_planner.open_rate_rules import (
    MAIN_QUARTERS_MAX,
    PART_QUARTERS_MAX,
    keeps_staff_rate_without_opening_extra,
    max_total_quarters,
    quarters_to_rate,
    staff_rate_min_quarters,
)
from fot_planner.salary_limits_2556 import p4_applies_to_category

_DEADLINE_LABELS = {
    "salary": "оклада",
    "flex": "надбавок",
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
        staff_q = staff_rate_min_quarters(e.rate)
        employment_max_q = (
            PART_QUARTERS_MAX
            if e.employment_type == "part_time"
            else MAIN_QUARTERS_MAX
        )
        if keeps_staff_rate_without_opening_extra(e.employment_category, e.position):
            max_q = employment_max_q
        else:
            max_q = min(employment_max_q, max_total_quarters(e.employment_category))
        if staff_q > max_q:
            conflicts.append(
                ConflictRecord(
                    code="INVALID_EMPLOYMENT_RATE",
                    message=(
                        f"Сотрудник {e.id}: ставка {e.rate:g} превышает максимум "
                        f"{quarters_to_rate(max_q):g} для указанного типа и категории "
                        "занятости"
                    ),
                    employee_id=e.id,
                )
            )
        overlapping_contracts = sorted(
            set(e.allowed_contracts) & set(e.forbidden_contracts)
        )
        for cid in overlapping_contracts:
            conflicts.append(
                ConflictRecord(
                    code="CONTRACT_ALLOWED_AND_FORBIDDEN",
                    message=(
                        f"Сотрудник {e.id}: договор {cid} одновременно указан "
                        "в разрешённых и запрещённых договорах"
                    ),
                    employee_id=e.id,
                    contract_id=cid,
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
        if c.allow_salary:
            if c.salary_payment_deadline is None:
                conflicts.append(
                    ConflictRecord(
                        code="MISSING_PAYMENT_DEADLINE",
                        message=(
                            f"Договор {c.id}: укажите конечную дату выплат "
                            f"({_DEADLINE_LABELS['salary']}) — колонка "
                            f"«конечная дата выплат оклада»"
                        ),
                        contract_id=c.id,
                    )
                )
            elif payment_month_count(c, ctx.year, PaymentKind.SALARY) <= 0:
                conflicts.append(
                    ConflictRecord(
                        code="EMPTY_PAYMENT_WINDOW",
                        message=(
                            f"Договор {c.id}: нет месяцев в {ctx.year} для выплат "
                            f"({_DEADLINE_LABELS['salary']}) до "
                            f"{payment_deadline_date(c, 'salary').isoformat()}"
                        ),
                        contract_id=c.id,
                    )
                )
        if any(payment_kind_enabled(c, kind) for kind in PAYMENT_KINDS if kind.is_non_salary):
            if c.allowances_payment_deadline is None:
                conflicts.append(
                    ConflictRecord(
                        code="MISSING_PAYMENT_DEADLINE",
                        message=(
                            f"Договор {c.id}: укажите конечную дату выплат "
                            f"({_DEADLINE_LABELS['flex']}) — колонка "
                            f"«конечная дата выплат надбавок»"
                        ),
                        contract_id=c.id,
                    )
                )
            else:
                flex_kinds = [
                    kind for kind in PAYMENT_KINDS if kind.is_non_salary and payment_kind_enabled(c, kind)
                ]
                if flex_kinds and all(
                    payment_month_count(c, ctx.year, kind) <= 0 for kind in flex_kinds
                ):
                    conflicts.append(
                        ConflictRecord(
                            code="EMPTY_PAYMENT_WINDOW",
                            message=(
                                f"Договор {c.id}: нет месяцев в {ctx.year} для выплат "
                                f"({_DEADLINE_LABELS['flex']}) до "
                                f"{payment_deadline_date(c, flex_kinds[0]).isoformat()}"
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

    for row in ctx.secret_allowances:
        if row.employee_id not in emp_ids:
            conflicts.append(
                ConflictRecord(
                    code="UNKNOWN_EMPLOYEE",
                    message=f"Лист 120_надбавка: неизвестный сотрудник {row.employee_id}",
                    employee_id=row.employee_id,
                )
            )
        if row.secret_contract_id not in contract_ids:
            conflicts.append(
                ConflictRecord(
                    code="UNKNOWN_CONTRACT",
                    message=f"Лист 120_надбавка: неизвестный договор секретности {row.secret_contract_id}",
                    employee_id=row.employee_id,
                    contract_id=row.secret_contract_id,
                )
            )
        if row.rate <= 0:
            conflicts.append(
                ConflictRecord(
                    code="INVALID_SECRET_ALLOWANCE_RATE",
                    message=(
                        f"Лист 120_надбавка: ставка 120 для сотрудника {row.employee_id} "
                        f"должна быть > 0"
                    ),
                    employee_id=row.employee_id,
                    contract_id=row.secret_contract_id,
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

    for row in ctx.position_salary_limits:
        if (
            row.p4_limit is not None
            and row.p4_limit > 0
            and not p4_applies_to_category(row.personnel_category)
        ):
            conflicts.append(
                ConflictRecord(
                    code="P4_IGNORED_FOR_CATEGORY_WARNING",
                    message=(
                        f"Должность {row.position!r} ({row.personnel_category}): "
                        f"П4 не применяется — только для категорий НТП и НР"
                    ),
                )
            )

    for c in ctx.contracts:
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
