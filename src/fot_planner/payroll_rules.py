from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

import pyomo.environ as pyo

from fot_planner.contract_calendar import contract_payment_window_includes_month
from fot_planner.labor_rules import (
    labor_rows_for_contract,
    planned_labor_amount,
    total_planned_person_months,
)
from fot_planner.limit_codes import normalize_limit_code
from fot_planner.models import PaymentKind, PlanningContext
from fot_planner.position_reference import normalize_position
from fot_planner.salary_limits_2556 import effective_p4_limit, p4_applies_to_category

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


PayrollRule = Callable[[PayrollRuleContext], None]


def _payment_limit_entries_by_code(ctx: PlanningContext) -> dict[str, tuple]:
    entries: dict[str, list] = defaultdict(list)
    for row in ctx.contract_payment_limits:
        limit_code = normalize_limit_code(row.limit_code)
        if not limit_code:
            continue
        entries[limit_code].append(row)
    return {key: tuple(value) for key, value in entries.items()}


def _position_limits_by_code_and_position(ctx: PlanningContext) -> dict[tuple[str, str], object]:
    return {
        (normalize_limit_code(code), normalize_position(row.position)): row
        for code, rows in ctx.position_limit_tables.items()
        for row in rows
        if row.position
    }


def _position_salary_limits_by_position(ctx: PlanningContext) -> dict[str, object]:
    return {
        normalize_position(row.position): row
        for row in ctx.position_salary_limits
        if row.position
    }


def _position_salary_limit_for_employee(limit_by_position: dict[str, object], employee):
    return limit_by_position.get(normalize_position(employee.position))


def _p4_limit_by_personnel_category(ctx: PlanningContext) -> dict[str, float]:
    limits: dict[str, float] = {}
    for row in ctx.position_salary_limits:
        limit = effective_p4_limit(row)
        if limit is None:
            continue
        category = str(row.personnel_category or "").strip().upper().replace("Ё", "Е")
        if category:
            limits[category] = min(limits.get(category, limit), limit)
    return limits


def _limit_for_rate(limit: float | None, rate: float) -> float | None:
    if limit is None or limit <= 0 or rate <= 0:
        return None
    return float(limit) * rate


def _employee_limit_by_code(limit_by_code_position, employee, limit_code: str) -> float | None:
    code = normalize_limit_code(limit_code)
    limit_row = limit_by_code_position.get((code, normalize_position(employee.position)))
    if limit_row is None:
        return None
    if code == "p4" and not p4_applies_to_category(limit_row.personnel_category):
        return None
    return _limit_for_rate(limit_row.limit, employee.rate)


def _payment_terms(rule_ctx: PayrollRuleContext, *, employee_id: str, month: int, kind: PaymentKind):
    return [
        rule_ctx.alloc[k]
        for k in rule_ctx.alloc_keys
        if k[0] == employee_id and k[2] == month and k[3] == kind
    ]


def _contract_staff_terms(
    rule_ctx: PayrollRuleContext,
    contract_id: str,
    kinds: tuple[PaymentKind, ...],
):
    return [
        rule_ctx.alloc[k]
        for k in rule_ctx.alloc_keys
        if k[1] == contract_id and k[3] in kinds
    ]


def _contract_labor_average_limit(ctx: PlanningContext, contract_id: str) -> float | None:
    """Средняя стоимость чел.-месяца по трудоёмкости договора, если она задана."""

    labor_rows = [lp for _idx, lp in labor_rows_for_contract(ctx, contract_id)]
    labor_rows_with_cost = [
        lp
        for lp in labor_rows
        if lp.person_months > 0
        and lp.avg_monthly_labor_cost is not None
        and lp.avg_monthly_labor_cost > 0
    ]
    if not labor_rows_with_cost:
        return None
    planned_pm = sum(lp.person_months for lp in labor_rows_with_cost)
    if planned_pm <= 0:
        return None
    planned_amount = sum(planned_labor_amount(lp) for lp in labor_rows_with_cost)
    if planned_amount <= 0:
        return None
    return planned_amount / planned_pm


def _goz_bep_effective_limit(ctx: PlanningContext, contract_id: str) -> float | None:
    """Для ГОЗ/БЭП берём самый строгий потолок: БЭП и/или трудоёмкость договора."""

    limits: list[float] = []
    bep_limit = ctx.salary_stability.goz_average_salary_limit
    if bep_limit > 0:
        limits.append(float(bep_limit))
    labor_limit = _contract_labor_average_limit(ctx, contract_id)
    if labor_limit is not None and labor_limit > 0:
        limits.append(float(labor_limit))
    if not limits:
        return None
    return min(limits)


def _employee_category(rule_ctx: PayrollRuleContext, employee) -> str:
    limit_row = _position_salary_limit_for_employee(
        _position_salary_limits_by_position(rule_ctx.ctx),
        employee,
    )
    return str(getattr(limit_row, "personnel_category", "") or "").strip().upper().replace("Ё", "Е")


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


def rule_contract_payment_limits(rule_ctx: PayrollRuleContext) -> None:
    """Лимиты из листа «договоры_ограничения»: одна группа = код ограничения."""

    limit_by_code_position = _position_limits_by_code_and_position(rule_ctx.ctx)
    entries_by_code = _payment_limit_entries_by_code(rule_ctx.ctx)
    for limit_code, entries in entries_by_code.items():
        if limit_code in {"2556", "bep", "p4"}:
            continue
        for employee in rule_ctx.ctx.employees:
            limit = _employee_limit_by_code(limit_by_code_position, employee, limit_code)
            if limit is None:
                continue
            for month in rule_ctx.months:
                terms = [
                    rule_ctx.alloc[(employee.id, row.contract_id, month, row.payment_kind)]
                    for row in entries
                    if row.contract_id in rule_ctx.contracts
                    and (employee.id, row.contract_id, month, row.payment_kind)
                    in rule_ctx.alloc_key_set
                ]
                if not terms:
                    continue
                rule_ctx.model.cons.add(rule_ctx.sum_terms(terms) <= limit)


def rule_2556_salary_122_staff_total(rule_ctx: PayrollRuleContext) -> None:
    """П2556: месячная штатная часть сотрудника (оклад + 122) ограничена по должности."""

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
                and key[3] in SALARY_122_KINDS
            ]
            if terms:
                rule_ctx.model.cons.add(rule_ctx.sum_terms(terms) <= limit)


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


def _p4_124_uses_by_employee_month(
    rule_ctx: PayrollRuleContext,
) -> dict[tuple[str, int], list]:
    uses_by_employee_month: dict[tuple[str, int], list] = defaultdict(list)
    for key in rule_ctx.alloc_keys:
        employee_id, _contract_id, month, kind = key
        if kind is PaymentKind.K124:
            uses_by_employee_month[(employee_id, month)].append(rule_ctx.uses[key])
    return dict(uses_by_employee_month)


def _p4_staff_terms(rule_ctx: PayrollRuleContext, *, employee_id: str, month: int) -> list:
    return [
        rule_ctx.alloc[key]
        for key in rule_ctx.alloc_keys
        if key[0] == employee_id and key[2] == month and key[3] in P4_STAFF_KINDS
    ]


def rule_p4_average_by_category(rule_ctx: PayrollRuleContext) -> None:
    """П4: средняя штатная сумма (оклад + 122 + 124) только для месяцев с 124."""

    category_limits = _p4_limit_by_personnel_category(rule_ctx.ctx)
    if not category_limits:
        return

    p4_uses_by_employee_month = _p4_124_uses_by_employee_month(rule_ctx)
    if not p4_uses_by_employee_month:
        return

    used_keys = list(p4_uses_by_employee_month)
    rule_ctx.model.p4_employee_month_used = pyo.Var(used_keys, domain=pyo.Binary)
    rule_ctx.model.p4_employee_month_staff_amount = pyo.Var(
        used_keys,
        domain=pyo.NonNegativeReals,
    )

    for key, uses in p4_uses_by_employee_month.items():
        employee_id, month = key
        used_var = rule_ctx.model.p4_employee_month_used[key]
        amount_var = rule_ctx.model.p4_employee_month_staff_amount[key]
        for use_var in uses:
            rule_ctx.model.cons.add(use_var <= used_var)
        rule_ctx.model.cons.add(used_var <= rule_ctx.sum_terms(uses))

        staff_terms = _p4_staff_terms(rule_ctx, employee_id=employee_id, month=month)
        if not staff_terms:
            rule_ctx.model.cons.add(amount_var == 0)
            continue
        staff_total = rule_ctx.sum_terms(staff_terms)
        rule_ctx.model.cons.add(amount_var <= staff_total)
        rule_ctx.model.cons.add(amount_var <= RULE_BIG_M * used_var)
        rule_ctx.model.cons.add(
            amount_var >= staff_total - RULE_BIG_M * (1 - used_var)
        )

    for category, limit in category_limits.items():
        payment_terms = []
        denominator_terms = []
        for employee in rule_ctx.ctx.employees:
            if _employee_category(rule_ctx, employee) != category:
                continue
            for month in rule_ctx.months:
                key = (employee.id, month)
                if key not in p4_uses_by_employee_month:
                    continue
                payment_terms.append(rule_ctx.model.p4_employee_month_staff_amount[key])
                if employee.rate > 0:
                    denominator_terms.append(
                        employee.rate * rule_ctx.model.p4_employee_month_used[key]
                    )
        if not payment_terms:
            continue
        if denominator_terms:
            rule_ctx.model.cons.add(
                rule_ctx.sum_terms(payment_terms)
                <= limit * rule_ctx.sum_terms(denominator_terms)
            )
        else:
            rule_ctx.model.cons.add(rule_ctx.sum_terms(payment_terms) == 0)


def rule_goz_bep_staff_total(rule_ctx: PayrollRuleContext) -> None:
    """ГОЗ/БЭП: штатная часть ГОЗ-договора ограничена средней зарплатой на трудоёмкость."""

    for contract_id, contract in rule_ctx.contracts.items():
        if not contract.is_goz_defense_order:
            continue
        planned_pm = total_planned_person_months(rule_ctx.ctx, contract_id)
        effective_limit = _goz_bep_effective_limit(rule_ctx.ctx, contract_id)
        if planned_pm <= 0 or effective_limit is None:
            continue
        terms = _contract_staff_terms(rule_ctx, contract_id, SALARY_122_KINDS)
        if terms:
            rule_ctx.model.cons.add(
                rule_ctx.sum_terms(terms) <= effective_limit * planned_pm
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
    rule_p4_average_by_category,
    rule_goz_bep_staff_total,
    rule_contract_payment_limits,
    rule_priority_either_staff_or_152,
    rule_priority_no_staff_if_salary_elsewhere,
)


def apply_payroll_rules(rule_ctx: PayrollRuleContext) -> None:
    for rule in PAYROLL_RULES:
        rule(rule_ctx)
