from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

import pyomo.environ as pyo

from fot_planner.contract_calendar import contract_payment_window_includes_month
from fot_planner.models import PaymentKind, PlanningContext
from fot_planner.open_rate_rules import OPEN_RATE_STEP, TOTAL_RATE_MAX_REGULAR
from fot_planner.position_reference import normalize_position
from fot_planner.salary_limits_2556 import (
    normalize_personnel_category,
    p4_applies_to_category,
)

AVERAGE_LIMIT_MODE = "average"
RULE_BIG_M = 1e7
P4_STAFF_KINDS = (PaymentKind.SALARY, PaymentKind.K122, PaymentKind.K124)
SALARY_122_KINDS = (PaymentKind.SALARY, PaymentKind.K122)
K152_EXCLUDES_KINDS = (
    PaymentKind.K120,
    PaymentKind.K122,
    PaymentKind.K124,
    PaymentKind.ORDER_INCENTIVE,
)

@dataclass(frozen=True)
class PayrollRuleContext:
    ctx: PlanningContext
    model: object
    alloc: object
    uses: object
    alloc_keys: list[tuple[str, str, int, PaymentKind]]
    alloc_key_set: set[tuple[str, str, int, PaymentKind]]
    contracts: dict
    employees: dict
    months: list[int]
    contract_list: list[str]
    sum_terms: Callable[[list], object]
    contract_allows_month: Callable[[object, int, int], bool]
    employee_active_in_month: Callable[[object, int, int], bool]
    open_rate_q: object | None = None
    open_rate_key_set: set[tuple[str, str, int]] | None = None


PayrollRule = Callable[[PayrollRuleContext], None]


def _position_limits_by_code_and_position(ctx: PlanningContext) -> dict[tuple[str, str], object]:
    return {
        (str(code).strip().lower(), normalize_position(row.position)): row
        for code, rows in ctx.position_limit_tables.items()
        for row in rows
        if row.position
    }


def _limit_for_rate(limit: float | None, rate: float) -> float | None:
    if limit is None or limit <= 0 or rate <= 0:
        return None
    return float(limit) * rate


def _employee_limit_by_code(limit_by_code_position, employee, limit_code: str) -> float | None:
    code = str(limit_code).strip().lower()
    limit_row = limit_by_code_position.get((code, normalize_position(employee.position)))
    if limit_row is None:
        return None
    if code == "p4" and not p4_applies_to_category(limit_row.personnel_category):
        return None
    return _limit_for_rate(limit_row.limit, employee.rate)


def _employee_limit_row_by_code(limit_by_code_position, employee, limit_code: str):
    code = str(limit_code).strip().lower()
    limit_row = limit_by_code_position.get((code, normalize_position(employee.position)))
    if limit_row is None:
        return None
    if code == "p4" and not p4_applies_to_category(limit_row.personnel_category):
        return None
    return limit_row


def _contract_open_rate_expr(rule_ctx: PayrollRuleContext, employee, contract_id: str, month: int):
    key = (employee.id, contract_id, month)
    if rule_ctx.open_rate_q is not None and key in (rule_ctx.open_rate_key_set or set()):
        return OPEN_RATE_STEP * rule_ctx.open_rate_q[key]
    return float(employee.rate)


def _employee_month_open_rate_expr(rule_ctx: PayrollRuleContext, employee, month: int):
    open_rate_key_set = rule_ctx.open_rate_key_set or set()
    keys = [
        key
        for key in open_rate_key_set
        if key[0] == employee.id and key[2] == month
    ]
    if rule_ctx.open_rate_q is not None and keys:
        return rule_ctx.sum_terms([OPEN_RATE_STEP * rule_ctx.open_rate_q[key] for key in keys])
    return float(employee.rate)


def _payment_terms(rule_ctx: PayrollRuleContext, *, employee_id: str, month: int, kind: PaymentKind):
    return [
        rule_ctx.alloc[k]
        for k in rule_ctx.alloc_keys
        if k[0] == employee_id and k[2] == month and k[3] == kind
    ]


def _goz_bep_average_limit(ctx: PlanningContext) -> float | None:
    """БЭП: средний лимит штатной части ГОЗ на 1 ставку."""

    limit = ctx.salary_stability.goz_average_salary_limit
    if limit is None or limit <= 0:
        return None
    return float(limit)


def _account_starts_with_23(contract) -> bool:
    account = str(getattr(contract, "account", "") or "")
    account = "".join(ch for ch in account if ch.isdigit())
    return account.startswith("23")


def _active_secret_allowance_rate(
    rule_ctx: PayrollRuleContext,
    *,
    employee_id: str,
    month: int,
) -> float | None:
    rates: list[float] = []
    for row in rule_ctx.ctx.secret_allowances:
        if row.employee_id != employee_id:
            continue
        secret_contract = rule_ctx.contracts.get(row.secret_contract_id)
        if secret_contract is None:
            continue
        if contract_payment_window_includes_month(
            secret_contract,
            rule_ctx.ctx.year,
            month,
            PaymentKind.K120,
        ):
            rates.append(row.rate)
    if not rates:
        return None
    return max(rates)


def rule_120_exact_percent(rule_ctx: PayrollRuleContext) -> None:
    """120: если сотрудник указан на листе 120_надбавка, начислить ровно процент от оклада."""

    for employee in rule_ctx.ctx.employees:
        for month in rule_ctx.months:
            if not rule_ctx.employee_active_in_month(employee, rule_ctx.ctx.year, month):
                continue
            secret_terms = _payment_terms(
                rule_ctx, employee_id=employee.id, month=month, kind=PaymentKind.K120
            )

            allowance_rate = _active_secret_allowance_rate(
                rule_ctx,
                employee_id=employee.id,
                month=month,
            )
            if allowance_rate is None:
                if secret_terms:
                    rule_ctx.model.cons.add(rule_ctx.sum_terms(secret_terms) == 0)
                continue

            salary_terms = _payment_terms(
                rule_ctx, employee_id=employee.id, month=month, kind=PaymentKind.SALARY
            )
            if salary_terms:
                rule_ctx.model.cons.add(
                    rule_ctx.sum_terms(secret_terms) == allowance_rate * rule_ctx.sum_terms(salary_terms)
                )
            elif secret_terms:
                rule_ctx.model.cons.add(rule_ctx.sum_terms(secret_terms) == 0)
            else:
                continue

            for contract_id in rule_ctx.contract_list:
                secret_key = (employee.id, contract_id, month, PaymentKind.K120)
                if secret_key not in rule_ctx.alloc_key_set:
                    continue
                contract = rule_ctx.contracts[contract_id]
                if _account_starts_with_23(contract):
                    continue
                salary_key = (employee.id, contract_id, month, PaymentKind.SALARY)
                if salary_key not in rule_ctx.alloc_key_set:
                    rule_ctx.model.cons.add(rule_ctx.uses[secret_key] == 0)
                    continue
                rule_ctx.model.cons.add(rule_ctx.uses[secret_key] <= rule_ctx.uses[salary_key])
                rule_ctx.model.cons.add(
                    rule_ctx.alloc[secret_key] <= allowance_rate * rule_ctx.alloc[salary_key]
                )


def rule_122_requires_salary_on_same_contract(rule_ctx: PayrollRuleContext) -> None:
    """122 только на том же договоре, где в этом месяце платится оклад."""

    for employee in rule_ctx.ctx.employees:
        for contract_id in rule_ctx.contract_list:
            for month in rule_ctx.months:
                if not rule_ctx.employee_active_in_month(employee, rule_ctx.ctx.year, month):
                    continue
                allowance_key = (employee.id, contract_id, month, PaymentKind.K122)
                if allowance_key not in rule_ctx.alloc_key_set:
                    continue
                salary_key = (employee.id, contract_id, month, PaymentKind.SALARY)
                if salary_key not in rule_ctx.alloc_key_set:
                    rule_ctx.model.cons.add(rule_ctx.uses[allowance_key] == 0)
                    continue
                rule_ctx.model.cons.add(rule_ctx.uses[allowance_key] <= rule_ctx.uses[salary_key])


def rule_152_excludes_other_supplements(rule_ctx: PayrollRuleContext) -> None:
    """Если у сотрудника в месяце выбрана 152, другие надбавки в этом месяце не назначаются."""

    for employee in rule_ctx.ctx.employees:
        for month in rule_ctx.months:
            extra_keys = [
                key
                for key in rule_ctx.alloc_keys
                if key[0] == employee.id and key[2] == month and key[3] is PaymentKind.K152
            ]
            if not extra_keys:
                continue
            other_keys = [
                key
                for key in rule_ctx.alloc_keys
                if key[0] == employee.id
                and key[2] == month
                and key[3] in K152_EXCLUDES_KINDS
            ]
            if not other_keys:
                continue
            for extra_key in extra_keys:
                for other_key in other_keys:
                    rule_ctx.model.cons.add(
                        rule_ctx.uses[extra_key] + rule_ctx.uses[other_key] <= 1
                    )


def rule_2556_salary_122_staff_total(rule_ctx: PayrollRuleContext) -> None:
    """П2556: штатная часть (оклад + 122) ограничена по должности и открытой ставке договора."""

    limit_by_code_position = _position_limits_by_code_and_position(rule_ctx.ctx)
    target_records: list[tuple[tuple[str, str, int], object, list]] = []
    for employee in rule_ctx.ctx.employees:
        limit_row = _employee_limit_row_by_code(limit_by_code_position, employee, "2556")
        if limit_row is None or limit_row.limit is None or limit_row.limit <= 0:
            continue
        base_limit = float(limit_row.limit)
        for month in rule_ctx.months:
            if not rule_ctx.employee_active_in_month(employee, rule_ctx.ctx.year, month):
                continue
            for contract_id in rule_ctx.contract_list:
                terms = [
                    rule_ctx.alloc[key]
                    for key in rule_ctx.alloc_keys
                    if key[0] == employee.id
                    and key[1] == contract_id
                    and key[2] == month
                    and key[3] in SALARY_122_KINDS
                ]
                if terms:
                    total = rule_ctx.sum_terms(terms)
                    limit = base_limit * _contract_open_rate_expr(
                        rule_ctx,
                        employee,
                        contract_id,
                        month,
                    )
                    rule_ctx.model.cons.add(total <= limit)
                    target_records.append(((employee.id, contract_id, month), limit, terms))

    if not target_records:
        return

    keys = [key for key, _limit, _terms in target_records]
    rule_ctx.model.staff_2556_limit_deviation = pyo.Var(
        keys,
        domain=pyo.NonNegativeReals,
    )
    for key, limit, terms in target_records:
        total = rule_ctx.sum_terms(terms)
        deviation = rule_ctx.model.staff_2556_limit_deviation[key]
        rule_ctx.model.cons.add(deviation >= total - limit)
        rule_ctx.model.cons.add(deviation >= limit - total)


def rule_order_incentive_2556_amount(rule_ctx: PayrollRuleContext) -> None:
    """П2556: стимулирующая приказом ограничена отдельно, только суммой приказа."""

    limit_by_code_position = _position_limits_by_code_and_position(rule_ctx.ctx)
    for employee in rule_ctx.ctx.employees:
        limit = _employee_limit_by_code(limit_by_code_position, employee, "2556")
        if limit is None:
            continue
        for month in rule_ctx.months:
            if not rule_ctx.employee_active_in_month(employee, rule_ctx.ctx.year, month):
                continue
            terms = [
                rule_ctx.alloc[key]
                for key in rule_ctx.alloc_keys
                if key[0] == employee.id
                and key[2] == month
                and key[3] is PaymentKind.ORDER_INCENTIVE
            ]
            if terms:
                rule_ctx.model.cons.add(rule_ctx.sum_terms(terms) <= limit)


def rule_order_incentive_once_per_month(rule_ctx: PayrollRuleContext) -> None:
    """Приказ — разовая выплата: не больше одного приказа сотруднику в месяц."""

    for employee in rule_ctx.ctx.employees:
        for month in rule_ctx.months:
            order_uses = [
                rule_ctx.uses[key]
                for key in rule_ctx.alloc_keys
                if key[0] == employee.id
                and key[2] == month
                and key[3] is PaymentKind.ORDER_INCENTIVE
            ]
            if order_uses:
                rule_ctx.model.cons.add(rule_ctx.sum_terms(order_uses) <= 1)


def _p4_staff_terms(rule_ctx: PayrollRuleContext, *, employee_id: str, month: int) -> list:
    return [
        rule_ctx.alloc[key]
        for key in rule_ctx.alloc_keys
        if key[0] == employee_id and key[2] == month and key[3] in P4_STAFF_KINDS
    ]


def rule_p4_monthly_staff_limit(rule_ctx: PayrollRuleContext) -> None:
    """П4: в месяце с 124 штатная часть сотрудника (оклад + 122 + 124) не выше лимита П4."""

    limit_by_code_position = _position_limits_by_code_and_position(rule_ctx.ctx)
    employees_by_id = {employee.id: employee for employee in rule_ctx.ctx.employees}
    p4_employee_month_124_uses: dict[tuple[str, int], list] = defaultdict(list)

    for key in rule_ctx.alloc_keys:
        employee_id, _contract_id, month, kind = key
        if kind is not PaymentKind.K124:
            continue
        p4_employee_month_124_uses[(employee_id, month)].append(rule_ctx.uses[key])

    if not p4_employee_month_124_uses:
        return

    p4_employee_month_keys: list[tuple[str, int]] = []
    p4_employee_month_limit: dict[tuple[str, int], float] = {}
    p4_employee_month_total_rate: dict[tuple[str, int], object] = {}
    p4_employee_month_max_rate: dict[tuple[str, int], float] = {}
    p4_group_employee_months: dict[tuple[str, int], list[tuple[str, int]]] = defaultdict(list)
    p4_group_limit: dict[tuple[str, int], float] = {}

    for month_key in p4_employee_month_124_uses:
        employee_id, month = month_key
        employee = employees_by_id.get(employee_id)
        if employee is None:
            continue
        if not rule_ctx.employee_active_in_month(employee, rule_ctx.ctx.year, month):
            continue
        limit_row = limit_by_code_position.get(("p4", normalize_position(employee.position)))
        if limit_row is None or not p4_applies_to_category(limit_row.personnel_category):
            continue
        limit = limit_row.limit
        if limit is None or limit <= 0 or employee.rate <= 0:
            continue

        category = normalize_personnel_category(limit_row.personnel_category)
        group_key = (category, month)
        p4_employee_month_keys.append(month_key)
        p4_employee_month_limit[month_key] = float(limit)
        p4_employee_month_total_rate[month_key] = _employee_month_open_rate_expr(
            rule_ctx,
            employee,
            month,
        )
        p4_employee_month_max_rate[month_key] = max(
            float(employee.rate),
            TOTAL_RATE_MAX_REGULAR,
        )
        p4_group_employee_months[group_key].append(month_key)
        current_group_limit = p4_group_limit.get(group_key)
        p4_group_limit[group_key] = (
            float(limit)
            if current_group_limit is None
            else min(current_group_limit, float(limit))
        )

    if not p4_employee_month_keys:
        return

    p4_group_keys = list(p4_group_employee_months)
    rule_ctx.model.p4_month_active = pyo.Var(p4_employee_month_keys, domain=pyo.Binary)
    rule_ctx.model.p4_staff_included = pyo.Var(
        p4_employee_month_keys, domain=pyo.NonNegativeReals
    )
    rule_ctx.model.p4_active_rate = pyo.Var(
        p4_employee_month_keys, domain=pyo.NonNegativeReals
    )
    rule_ctx.model.p4_group_under_limit = pyo.Var(
        p4_group_keys, domain=pyo.NonNegativeReals
    )
    rule_ctx.model.p4_employee_limit_deviation = pyo.Var(
        p4_employee_month_keys, domain=pyo.NonNegativeReals
    )

    for month_key in p4_employee_month_keys:
        employee_id, month = month_key
        active = rule_ctx.model.p4_month_active[month_key]
        uses_124 = p4_employee_month_124_uses[month_key]
        for use_var in uses_124:
            rule_ctx.model.cons.add(use_var <= active)
        rule_ctx.model.cons.add(active <= rule_ctx.sum_terms(uses_124))

        active_rate = rule_ctx.model.p4_active_rate[month_key]
        total_rate = p4_employee_month_total_rate[month_key]
        max_rate = p4_employee_month_max_rate[month_key]
        rule_ctx.model.cons.add(active_rate <= total_rate)
        rule_ctx.model.cons.add(active_rate <= max_rate * active)
        rule_ctx.model.cons.add(active_rate >= total_rate - max_rate * (1 - active))

        staff_terms = _p4_staff_terms(rule_ctx, employee_id=employee_id, month=month)
        if not staff_terms:
            continue
        staff_total = rule_ctx.sum_terms(staff_terms)
        included = rule_ctx.model.p4_staff_included[month_key]
        rule_ctx.model.cons.add(included <= staff_total)
        rule_ctx.model.cons.add(included <= RULE_BIG_M * active)
        rule_ctx.model.cons.add(included >= staff_total - RULE_BIG_M * (1 - active))

        employee_limit = p4_employee_month_limit[month_key] * active_rate
        deviation = rule_ctx.model.p4_employee_limit_deviation[month_key]
        # Предел П4 не потолок, а норма: если в месяце начислена 124, штатная
        # часть режется ровно по пределу на ставку. Превышение запрещено, а
        # отклонение вниз убирает своя стадия целевой функции. Раньше
        # превышение только штрафовалось, и надбавку получал в том числе тот,
        # у кого оклад с 122 уже выше предела.
        rule_ctx.model.cons.add(included == employee_limit)
        rule_ctx.model.cons.add(deviation >= included - employee_limit)
        rule_ctx.model.cons.add(deviation >= employee_limit - included)

    for group_key in p4_group_keys:
        group_limit = p4_group_limit[group_key]
        employee_months = p4_group_employee_months[group_key]
        included_total = rule_ctx.sum_terms(
            [rule_ctx.model.p4_staff_included[key] for key in employee_months]
        )
        active_rate = rule_ctx.sum_terms(
            [rule_ctx.model.p4_active_rate[key] for key in employee_months]
        )
        rule_ctx.model.cons.add(included_total <= group_limit * active_rate)
        rule_ctx.model.cons.add(
            rule_ctx.model.p4_group_under_limit[group_key]
            >= group_limit * active_rate - included_total
        )


def rule_goz_bep_staff_total(rule_ctx: PayrollRuleContext) -> None:
    """ГОЗ/БЭП: штатная часть по ГОЗ-договору не выше суммы БЭП должностей × ставок.

    БЭП свой у каждой должности (справочник), поэтому предел договора в
    месяце — сумма по людям «БЭП должности × открытая ставка», а не одно число
    на всех. Раньше бралось наименьшее из справочника для всех разом.
    Должности без БЭП получают общее значение из настроек.
    """

    bep_limit = _goz_bep_average_limit(rule_ctx.ctx)
    if bep_limit is None:
        return
    limit_by_code_position = _position_limits_by_code_and_position(rule_ctx.ctx)

    def bep_of(employee) -> float:
        row = limit_by_code_position.get(("bep", normalize_position(employee.position)))
        if row is None or row.limit is None or row.limit <= 0:
            return bep_limit
        return float(row.limit)
    goz_contract_ids = {
        contract_id
        for contract_id, contract in rule_ctx.contracts.items()
        if contract.is_goz_defense_order
    }
    if not goz_contract_ids:
        return

    employee_by_id = {employee.id: employee for employee in rule_ctx.ctx.employees}
    active_records: list[tuple[tuple[str, str, int], list, list]] = []
    month_contract_groups: dict[tuple[str, int], list[tuple[str, str, int]]] = defaultdict(list)

    for employee in rule_ctx.ctx.employees:
        for contract_id in goz_contract_ids:
            for month in rule_ctx.months:
                if not rule_ctx.employee_active_in_month(employee, rule_ctx.ctx.year, month):
                    continue
                alloc_keys = [
                    key
                    for key in rule_ctx.alloc_keys
                    if key[0] == employee.id
                    and key[1] == contract_id
                    and key[2] == month
                    and key[3] in SALARY_122_KINDS
                ]
                if not alloc_keys:
                    continue
                active_key = (employee.id, contract_id, month)
                active_records.append(
                    (
                        active_key,
                        [rule_ctx.alloc[key] for key in alloc_keys],
                        [rule_ctx.uses[key] for key in alloc_keys],
                    )
                )
                month_contract_groups[(contract_id, month)].append(active_key)

    if not active_records:
        return

    active_keys = [key for key, _alloc_terms, _use_terms in active_records]
    group_keys = list(month_contract_groups)
    rule_ctx.model.goz_bep_employee_active = pyo.Var(
        active_keys,
        domain=pyo.Binary,
    )
    rule_ctx.model.goz_bep_average_under_limit = pyo.Var(
        group_keys,
        domain=pyo.NonNegativeReals,
    )

    for active_key, _alloc_terms, use_terms in active_records:
        active = rule_ctx.model.goz_bep_employee_active[active_key]
        for use_var in use_terms:
            rule_ctx.model.cons.add(use_var <= active)
        rule_ctx.model.cons.add(active <= rule_ctx.sum_terms(use_terms))

    for group_key, active_keys_for_group in month_contract_groups.items():
        contract_id, month = group_key
        included_total = rule_ctx.sum_terms(
            [
                rule_ctx.alloc[key]
                for key in rule_ctx.alloc_keys
                if key[1] == contract_id
                and key[2] == month
                and key[3] in SALARY_122_KINDS
            ]
        )
        target_terms = []
        open_rate_key_set = rule_ctx.open_rate_key_set or set()
        for employee_id, contract_id, month in active_keys_for_group:
            open_key = (employee_id, contract_id, month)
            employee = employee_by_id[employee_id]
            if open_key in open_rate_key_set:
                rate_expr = _contract_open_rate_expr(rule_ctx, employee, contract_id, month)
            else:
                rate_expr = employee.rate * rule_ctx.model.goz_bep_employee_active[
                    (employee_id, contract_id, month)
                ]
            target_terms.append(bep_of(employee) * rate_expr)
        target = rule_ctx.sum_terms(target_terms)
        rule_ctx.model.cons.add(included_total <= target)
        rule_ctx.model.cons.add(
            rule_ctx.model.goz_bep_average_under_limit[group_key]
            >= target - included_total
        )


# Приоритет: штатные выплаты (не 152) на договоре приоритета.
_PRIORITY_STAFF_KINDS: tuple[PaymentKind, ...] = (
    PaymentKind.SALARY,
    PaymentKind.K120,
    PaymentKind.K122,
    PaymentKind.K124,
)


def rule_priority_either_staff_or_152(rule_ctx: PayrollRuleContext) -> None:
    """Приоритет: на договоре в месяце — либо штатные (1/122/…), либо 152."""

    for employee in rule_ctx.ctx.employees:
        for contract_id in rule_ctx.contract_list:
            contract = rule_ctx.contracts[contract_id]
            if not contract.priority_payment_mode:
                continue
            for month in rule_ctx.months:
                extra_key = (employee.id, contract_id, month, PaymentKind.K152)
                if extra_key not in rule_ctx.alloc_key_set:
                    continue
                for kind in _PRIORITY_STAFF_KINDS:
                    staff_key = (employee.id, contract_id, month, kind)
                    if staff_key in rule_ctx.alloc_key_set:
                        rule_ctx.model.cons.add(
                            rule_ctx.uses[extra_key] + rule_ctx.uses[staff_key] <= 1
                        )


def rule_priority_no_staff_if_salary_elsewhere(rule_ctx: PayrollRuleContext) -> None:
    """Оклад на другом договоре → на приоритете только 152 (без 1/122 на нём же)."""

    for employee in rule_ctx.ctx.employees:
        for month in rule_ctx.months:
            for priority_id in rule_ctx.contract_list:
                contract = rule_ctx.contracts[priority_id]
                if not contract.priority_payment_mode:
                    continue
                for other_id in rule_ctx.contract_list:
                    if other_id == priority_id:
                        continue
                    other_salary = (employee.id, other_id, month, PaymentKind.SALARY)
                    if other_salary not in rule_ctx.alloc_key_set:
                        continue
                    for kind in _PRIORITY_STAFF_KINDS:
                        staff_key = (employee.id, priority_id, month, kind)
                        if staff_key in rule_ctx.alloc_key_set:
                            rule_ctx.model.cons.add(
                                rule_ctx.uses[staff_key] <= 1 - rule_ctx.uses[other_salary]
                            )


PAYROLL_RULES: tuple[PayrollRule, ...] = (
    rule_120_exact_percent,
    rule_122_requires_salary_on_same_contract,
    rule_152_excludes_other_supplements,
    rule_2556_salary_122_staff_total,
    rule_order_incentive_2556_amount,
    rule_order_incentive_once_per_month,
    rule_p4_monthly_staff_limit,
    rule_goz_bep_staff_total,
    rule_priority_either_staff_or_152,
    rule_priority_no_staff_if_salary_elsewhere,
)


def apply_payroll_rules(rule_ctx: PayrollRuleContext) -> None:
    for rule in PAYROLL_RULES:
        rule(rule_ctx)
