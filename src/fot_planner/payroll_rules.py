from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

import pyomo.environ as pyo

from fot_planner.labor_rules import total_planned_person_months
from fot_planner.limit_codes import normalize_limit_code
from fot_planner.models import PaymentKind, PlanningContext
from fot_planner.position_reference import normalize_position
from fot_planner.salary_limits_2556 import effective_p4_limit, p4_applies_to_category

AVERAGE_LIMIT_MODE = "average"
PLANNING_CAP_LIMIT_MODE = "planning_cap"
RULE_BIG_M = 1e7

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
    limit_mode: str = PLANNING_CAP_LIMIT_MODE


PayrollRule = Callable[[PayrollRuleContext], None]


def _contract_payment_limit_groups(
    ctx: PlanningContext,
) -> dict[tuple[str, str], tuple[PaymentKind, ...]]:
    groups: dict[tuple[str, str], set[PaymentKind]] = defaultdict(set)
    for row in ctx.contract_payment_limits:
        limit_code = normalize_limit_code(row.limit_code)
        if not limit_code:
            continue
        groups[(row.contract_id, limit_code)].add(row.payment_kind)
    return {
        key: tuple(sorted(payment_kinds, key=int))
        for key, payment_kinds in groups.items()
    }


def _payment_limit_entries_by_code(ctx: PlanningContext) -> dict[str, tuple]:
    entries: dict[str, list] = defaultdict(list)
    for row in ctx.contract_payment_limits:
        limit_code = normalize_limit_code(row.limit_code)
        if not limit_code:
            continue
        entries[limit_code].append(row)
    return {key: tuple(value) for key, value in entries.items()}


def _contract_has_payment_limit(
    ctx: PlanningContext,
    contract_id: str,
    payment_kind: PaymentKind,
    limit_code: str,
) -> bool:
    normalized = normalize_limit_code(limit_code)
    return any(
        row.contract_id == contract_id
        and row.payment_kind == payment_kind
        and normalize_limit_code(row.limit_code) == normalized
        for row in ctx.contract_payment_limits
    )


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
            limits.setdefault(category, limit)
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


def _contract_employee_terms(
    rule_ctx: PayrollRuleContext,
    *,
    employee_id: str,
    contract_id: str,
    month: int,
    kinds: tuple[PaymentKind, ...],
):
    return [
        rule_ctx.alloc[(employee_id, contract_id, month, kind)]
        for kind in kinds
        if (employee_id, contract_id, month, kind) in rule_ctx.alloc_key_set
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


def _contract_employee_month_terms(
    rule_ctx: PayrollRuleContext,
    *,
    contract_id: str,
    employee_id: str,
    month: int,
    kinds: tuple[PaymentKind, ...],
):
    return [
        rule_ctx.alloc[k]
        for k in rule_ctx.alloc_keys
        if k[0] == employee_id and k[1] == contract_id and k[2] == month and k[3] in kinds
    ]


def _employee_category(rule_ctx: PayrollRuleContext, employee) -> str:
    limit_row = _position_salary_limit_for_employee(
        _position_salary_limits_by_position(rule_ctx.ctx),
        employee,
    )
    return str(getattr(limit_row, "personnel_category", "") or "").strip().upper().replace("Ё", "Е")


def rule_120_5_percent(rule_ctx: PayrollRuleContext) -> None:
    """120: total secret allowance in a month is limited by salary base percent."""

    for employee in rule_ctx.ctx.employees:
        for month in rule_ctx.months:
            if not rule_ctx.employee_active_in_month(employee, rule_ctx.ctx.year, month):
                continue
            secret_terms = _payment_terms(
                rule_ctx, employee_id=employee.id, month=month, kind=PaymentKind.K120
            )
            if not secret_terms:
                continue

            contract_rates = [
                rule_ctx.contracts[k[1]].secret_rate
                for k in rule_ctx.alloc_keys
                if k[0] == employee.id and k[2] == month and k[3] == PaymentKind.K120
            ]
            secret_rate = max(contract_rates) if contract_rates else 0.05
            base_salary = (
                employee.reference_salary_for_rate * employee.rate
                if employee.reference_salary_for_rate and employee.reference_salary_for_rate > 0
                else None
            )
            if base_salary is not None:
                rule_ctx.model.cons.add(rule_ctx.sum_terms(secret_terms) <= secret_rate * base_salary)
                continue

            salary_terms = _payment_terms(
                rule_ctx, employee_id=employee.id, month=month, kind=PaymentKind.SALARY
            )
            if salary_terms:
                rule_ctx.model.cons.add(
                    rule_ctx.sum_terms(secret_terms) <= secret_rate * rule_ctx.sum_terms(salary_terms)
                )
            else:
                rule_ctx.model.cons.add(rule_ctx.sum_terms(secret_terms) == 0)


def rule_122_requires_salary_on_same_contract(rule_ctx: PayrollRuleContext) -> None:
    """122 только на том же договоре, где в этом месяце платится оклад."""

    for employee in rule_ctx.ctx.employees:
        for contract_id in rule_ctx.contract_list:
            contract = rule_ctx.contracts[contract_id]
            if not contract.allowance_requires_salary_contract:
                continue
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


def rule_contract_payment_limits(rule_ctx: PayrollRuleContext) -> None:
    """Лимиты из листа «договоры_ограничения»: одна группа = код ограничения."""

    limit_by_code_position = _position_limits_by_code_and_position(rule_ctx.ctx)
    entries_by_code = _payment_limit_entries_by_code(rule_ctx.ctx)
    for limit_code, entries in entries_by_code.items():
        if limit_code == "bep":
            continue
        if limit_code == "p4" and rule_ctx.limit_mode == AVERAGE_LIMIT_MODE:
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


def rule_124_only_on_p4_contracts(rule_ctx: PayrollRuleContext) -> None:
    """124 платится только там, где выплата 124 явно привязана к ограничению П4."""

    for employee in rule_ctx.ctx.employees:
        for contract_id in rule_ctx.contract_list:
            if _contract_has_payment_limit(
                rule_ctx.ctx,
                contract_id,
                PaymentKind.K124,
                "p4",
            ):
                continue
            for month in rule_ctx.months:
                key = (employee.id, contract_id, month, PaymentKind.K124)
                if key in rule_ctx.alloc_key_set:
                    rule_ctx.model.cons.add(rule_ctx.uses[key] == 0)


def rule_p4_average_by_category(rule_ctx: PayrollRuleContext) -> None:
    """П4 как средняя по НТП/НР для выплат, привязанных к ограничению П4."""

    category_limits = _p4_limit_by_personnel_category(rule_ctx.ctx)
    if not category_limits:
        return

    p4_entries = _payment_limit_entries_by_code(rule_ctx.ctx).get("p4", ())
    p4_entries = tuple(row for row in p4_entries if row.contract_id in rule_ctx.contracts)
    if not p4_entries:
        return

    used_keys: list[tuple[str, int]] = []
    p4_uses_by_employee_month: dict[tuple[str, int], list] = defaultdict(list)
    for employee in rule_ctx.ctx.employees:
        for month in rule_ctx.months:
            uses = [
                rule_ctx.uses[(employee.id, row.contract_id, month, row.payment_kind)]
                for row in p4_entries
                if (employee.id, row.contract_id, month, row.payment_kind)
                in rule_ctx.alloc_key_set
            ]
            if not uses:
                continue
            key = (employee.id, month)
            used_keys.append(key)
            p4_uses_by_employee_month[key] = uses

    if not used_keys:
        return

    rule_ctx.model.p4_employee_month_used = pyo.Var(used_keys, domain=pyo.Binary)
    for key, uses in p4_uses_by_employee_month.items():
        used_var = rule_ctx.model.p4_employee_month_used[key]
        for use_var in uses:
            rule_ctx.model.cons.add(use_var <= used_var)
        rule_ctx.model.cons.add(used_var <= rule_ctx.sum_terms(uses))

    for category, limit in category_limits.items():
        payment_terms = []
        denominator_terms = []
        for employee in rule_ctx.ctx.employees:
            if _employee_category(rule_ctx, employee) != category:
                continue
            for month in rule_ctx.months:
                terms = [
                    rule_ctx.alloc[(employee.id, row.contract_id, month, row.payment_kind)]
                    for row in p4_entries
                    if (employee.id, row.contract_id, month, row.payment_kind)
                    in rule_ctx.alloc_key_set
                ]
                if not terms:
                    continue
                payment_terms.extend(terms)
                used_key = (employee.id, month)
                if used_key in p4_uses_by_employee_month and employee.rate > 0:
                    denominator_terms.append(
                        employee.rate * rule_ctx.model.p4_employee_month_used[used_key]
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
    """BEP: выплаты, привязанные к БЭП, ограничены средней зарплатой на трудоемкость."""

    bep_limit = rule_ctx.ctx.salary_stability.goz_average_salary_limit
    if bep_limit <= 0:
        return
    for (contract_id, limit_code), payment_kinds in _contract_payment_limit_groups(
        rule_ctx.ctx
    ).items():
        if limit_code != "bep" or contract_id not in rule_ctx.contracts:
            continue
        planned_pm = total_planned_person_months(rule_ctx.ctx, contract_id)
        if planned_pm <= 0:
            continue
        terms = _contract_staff_terms(rule_ctx, contract_id, payment_kinds)
        if terms:
            rule_ctx.model.cons.add(rule_ctx.sum_terms(terms) <= bep_limit * planned_pm)


def rule_bep_individual_planning_cap(rule_ctx: PayrollRuleContext) -> None:
    """БЭП: рабочий индивидуальный ориентир на ставку; отключается при fallback на среднюю."""

    if rule_ctx.limit_mode == AVERAGE_LIMIT_MODE:
        return
    bep_limit = rule_ctx.ctx.salary_stability.goz_average_salary_limit
    if bep_limit <= 0:
        return
    bep_groups = [
        (contract_id, payment_kinds)
        for (contract_id, limit_code), payment_kinds in _contract_payment_limit_groups(
            rule_ctx.ctx
        ).items()
        if limit_code == "bep" and contract_id in rule_ctx.contracts
    ]
    if not bep_groups:
        return

    for employee in rule_ctx.ctx.employees:
        if employee.rate <= 0:
            continue
        cap = bep_limit * employee.rate
        for contract_id, payment_kinds in bep_groups:
            for month in rule_ctx.months:
                terms = _contract_employee_month_terms(
                    rule_ctx,
                    contract_id=contract_id,
                    employee_id=employee.id,
                    month=month,
                    kinds=payment_kinds,
                )
                if terms:
                    rule_ctx.model.cons.add(rule_ctx.sum_terms(terms) <= cap)


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
    rule_120_5_percent,
    rule_122_requires_salary_on_same_contract,
    rule_contract_payment_limits,
    rule_124_only_on_p4_contracts,
    rule_p4_average_by_category,
    rule_goz_bep_staff_total,
    rule_bep_individual_planning_cap,
    rule_priority_either_staff_or_152,
    rule_priority_no_staff_if_salary_elsewhere,
)


def apply_payroll_rules(rule_ctx: PayrollRuleContext) -> None:
    for rule in PAYROLL_RULES:
        rule(rule_ctx)
