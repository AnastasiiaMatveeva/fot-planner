from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fot_planner.labor_rules import total_planned_person_months
from fot_planner.models import PaymentKind, STAFF_LIMIT_KINDS, PlanningContext
from fot_planner.position_reference import normalize_position
from fot_planner.salary_limits_2556 import effective_p4_limit

AVERAGE_LIMIT_MODE = "average"
PLANNING_CAP_LIMIT_MODE = "planning_cap"
RULE_BIG_M = 1e7

P4_LIMIT_KINDS: tuple[PaymentKind, ...] = (
    PaymentKind.SALARY,
    PaymentKind.K122,
    PaymentKind.K124,
)

BEP_LIMIT_KINDS: tuple[PaymentKind, ...] = (
    PaymentKind.SALARY,
    PaymentKind.K120,
    PaymentKind.K122,
    PaymentKind.K124,
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
    limit_mode: str = PLANNING_CAP_LIMIT_MODE


PayrollRule = Callable[[PayrollRuleContext], None]


def parse_limit_sources(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set, frozenset)):
        raw_parts = value
    else:
        text = str(value).strip().lower().replace("ё", "е")
        for sep in (";", "|", "/"):
            text = text.replace(sep, ",")
        raw_parts = text.split(",")
    return {str(part).strip().lower() for part in raw_parts if str(part).strip()}


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


def _employee_limit_by_source(limit_row, employee, contract, source: str) -> float | None:
    source = source.lower()
    if source == "2556":
        return _limit_for_rate(getattr(limit_row, "order_2556_limit", None), employee.rate)
    if source == "p4":
        return _limit_for_rate(effective_p4_limit(limit_row), employee.rate)
    if source == "agreement":
        return _limit_for_rate(contract.agreement_staff_limit, employee.rate)
    return None


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


def _employee_month_staff_terms(
    rule_ctx: PayrollRuleContext,
    *,
    employee_id: str,
    month: int,
    kinds: tuple[PaymentKind, ...] = STAFF_LIMIT_KINDS,
):
    """Штатные выплаты сотрудника за месяц по всем договорам."""
    return [
        rule_ctx.alloc[k]
        for k in rule_ctx.alloc_keys
        if k[0] == employee_id and k[2] == month and k[3] in kinds
    ]


def _p4_contract_staff_uses(
    rule_ctx: PayrollRuleContext,
    *,
    contract_id: str,
    employee_id: str,
    month: int,
):
    if "p4" not in parse_limit_sources(rule_ctx.contracts[contract_id].staff_limit_sources):
        return []
    return [
        rule_ctx.uses[(employee_id, contract_id, month, kind)]
        for kind in P4_LIMIT_KINDS
        if (employee_id, contract_id, month, kind) in rule_ctx.alloc_key_set
    ]


def _contract_staff_terms(
    rule_ctx: PayrollRuleContext,
    contract_id: str,
    kinds: tuple[PaymentKind, ...] = STAFF_LIMIT_KINDS,
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


def _p4_denominator_use_var(
    rule_ctx: PayrollRuleContext,
    employee_id: str,
    contract_id: str,
    month: int,
):
    for kind in (PaymentKind.SALARY, PaymentKind.K124, PaymentKind.K122):
        key = (employee_id, contract_id, month, kind)
        if key in rule_ctx.alloc_key_set:
            return rule_ctx.uses[key]
    return None


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


def rule_salary_allowance_limit(rule_ctx: PayrollRuleContext) -> None:
    """Лимит на сумму оклад + 122 на одном договоре (П2556 и др.; без 124)."""

    limit_by_position = _position_salary_limits_by_position(rule_ctx.ctx)
    for employee in rule_ctx.ctx.employees:
        limit_row = _position_salary_limit_for_employee(limit_by_position, employee)
        for contract_id in rule_ctx.contract_list:
            contract = rule_ctx.contracts[contract_id]
            sources = parse_limit_sources(contract.salary_allowance_limit_sources)
            if not sources:
                continue
            for month in rule_ctx.months:
                allowance_key = (employee.id, contract_id, month, PaymentKind.K122)
                if allowance_key not in rule_ctx.alloc_key_set:
                    continue
                salary_key = (employee.id, contract_id, month, PaymentKind.SALARY)
                if salary_key not in rule_ctx.alloc_key_set:
                    continue

                terms = _contract_employee_terms(
                    rule_ctx,
                    employee_id=employee.id,
                    contract_id=contract_id,
                    month=month,
                    kinds=(PaymentKind.SALARY, PaymentKind.K122),
                )
                if not terms:
                    continue
                for source in sources:
                    limit = _employee_limit_by_source(limit_row, employee, contract, source)
                    if limit is not None:
                        rule_ctx.model.cons.add(rule_ctx.sum_terms(terms) <= limit)


def rule_124_only_on_p4_contracts(rule_ctx: PayrollRuleContext) -> None:
    """124 платится только с договоров, для которых включён источник лимита p4."""

    for employee in rule_ctx.ctx.employees:
        for contract_id in rule_ctx.contract_list:
            contract = rule_ctx.contracts[contract_id]
            if "p4" in parse_limit_sources(contract.staff_limit_sources):
                continue
            for month in rule_ctx.months:
                key = (employee.id, contract_id, month, PaymentKind.K124)
                if key in rule_ctx.alloc_key_set:
                    rule_ctx.model.cons.add(rule_ctx.uses[key] == 0)


def rule_124_p4_staff_limit(rule_ctx: PayrollRuleContext) -> None:
    """П4: рабочий индивидуальный ориентир оклад+122+124 на месяц (НТП/НР)."""

    if rule_ctx.limit_mode == AVERAGE_LIMIT_MODE:
        return

    limit_by_position = _position_salary_limits_by_position(rule_ctx.ctx)
    for employee in rule_ctx.ctx.employees:
        limit_row = _position_salary_limit_for_employee(limit_by_position, employee)
        p4_limit = _employee_limit_by_source(limit_row, employee, None, "p4")
        if p4_limit is None:
            continue
        for month in rule_ctx.months:
            if not rule_ctx.employee_active_in_month(employee, rule_ctx.ctx.year, month):
                continue
            terms = _employee_month_staff_terms(
                rule_ctx, employee_id=employee.id, month=month
            )
            if not terms:
                continue
            p4_uses = []
            for contract_id in rule_ctx.contract_list:
                p4_uses.extend(
                    _p4_contract_staff_uses(
                        rule_ctx,
                        contract_id=contract_id,
                        employee_id=employee.id,
                        month=month,
                    )
                )
            for used_var in p4_uses:
                rule_ctx.model.cons.add(
                    rule_ctx.sum_terms(terms) <= p4_limit + RULE_BIG_M * (1 - used_var)
                )


def rule_p4_average_by_category(rule_ctx: PayrollRuleContext) -> None:
    """П4 как средняя по НТП/НР для договоров с источником p4, без 152 и приказа."""

    category_limits = _p4_limit_by_personnel_category(rule_ctx.ctx)
    if not category_limits:
        return

    p4_contract_ids = [
        contract_id
        for contract_id in rule_ctx.contract_list
        if "p4" in parse_limit_sources(rule_ctx.contracts[contract_id].staff_limit_sources)
    ]
    if not p4_contract_ids:
        return

    for category, limit in category_limits.items():
        payment_terms = []
        denominator_terms = []
        for employee in rule_ctx.ctx.employees:
            if _employee_category(rule_ctx, employee) != category:
                continue
            for contract_id in p4_contract_ids:
                for month in rule_ctx.months:
                    terms = _contract_employee_month_terms(
                        rule_ctx,
                        contract_id=contract_id,
                        employee_id=employee.id,
                        month=month,
                        kinds=P4_LIMIT_KINDS,
                    )
                    if not terms:
                        continue
                    payment_terms.extend(terms)
                    used_var = _p4_denominator_use_var(
                        rule_ctx, employee.id, contract_id, month
                    )
                    if used_var is not None and employee.rate > 0:
                        denominator_terms.append(employee.rate * used_var)
        if not payment_terms:
            continue
        if denominator_terms:
            rule_ctx.model.cons.add(
                rule_ctx.sum_terms(payment_terms)
                <= limit * rule_ctx.sum_terms(denominator_terms)
            )
        else:
            rule_ctx.model.cons.add(rule_ctx.sum_terms(payment_terms) == 0)


def rule_staff_limit_sources(rule_ctx: PayrollRuleContext) -> None:
    """Generic per-employee staff limits: 2556/agreement and future sources."""

    limit_by_position = _position_salary_limits_by_position(rule_ctx.ctx)
    for employee in rule_ctx.ctx.employees:
        limit_row = _position_salary_limit_for_employee(limit_by_position, employee)
        for contract_id in rule_ctx.contract_list:
            contract = rule_ctx.contracts[contract_id]
            sources = parse_limit_sources(contract.staff_limit_sources) - {"bep", "p4"}
            if not sources:
                continue
            for month in rule_ctx.months:
                terms = _contract_employee_terms(
                    rule_ctx,
                    employee_id=employee.id,
                    contract_id=contract_id,
                    month=month,
                    kinds=STAFF_LIMIT_KINDS,
                )
                if not terms:
                    continue
                for source in sources:
                    limit = _employee_limit_by_source(limit_row, employee, contract, source)
                    if limit is not None:
                        rule_ctx.model.cons.add(rule_ctx.sum_terms(terms) <= limit)


def rule_goz_bep_staff_total(rule_ctx: PayrollRuleContext) -> None:
    """BEP: total staff payments for the contract are limited by BEP average."""

    bep_limit = rule_ctx.ctx.salary_stability.goz_average_salary_limit
    if bep_limit <= 0:
        return
    for contract_id in rule_ctx.contract_list:
        contract = rule_ctx.contracts[contract_id]
        if "bep" not in parse_limit_sources(contract.staff_limit_sources):
            continue
        planned_pm = total_planned_person_months(rule_ctx.ctx, contract_id)
        if planned_pm <= 0:
            continue
        terms = _contract_staff_terms(rule_ctx, contract_id, BEP_LIMIT_KINDS)
        if terms:
            rule_ctx.model.cons.add(rule_ctx.sum_terms(terms) <= bep_limit * planned_pm)


def rule_bep_individual_planning_cap(rule_ctx: PayrollRuleContext) -> None:
    """БЭП: рабочий индивидуальный ориентир на ставку; отключается при fallback на среднюю."""

    if rule_ctx.limit_mode == AVERAGE_LIMIT_MODE:
        return
    bep_limit = rule_ctx.ctx.salary_stability.goz_average_salary_limit
    if bep_limit <= 0:
        return
    bep_contract_ids = [
        contract_id
        for contract_id in rule_ctx.contract_list
        if "bep" in parse_limit_sources(rule_ctx.contracts[contract_id].staff_limit_sources)
    ]
    if not bep_contract_ids:
        return

    for employee in rule_ctx.ctx.employees:
        if employee.rate <= 0:
            continue
        cap = bep_limit * employee.rate
        for contract_id in bep_contract_ids:
            for month in rule_ctx.months:
                terms = _contract_employee_month_terms(
                    rule_ctx,
                    contract_id=contract_id,
                    employee_id=employee.id,
                    month=month,
                    kinds=BEP_LIMIT_KINDS,
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
    rule_salary_allowance_limit,
    rule_124_only_on_p4_contracts,
    rule_124_p4_staff_limit,
    rule_p4_average_by_category,
    rule_staff_limit_sources,
    rule_goz_bep_staff_total,
    rule_bep_individual_planning_cap,
    rule_priority_either_staff_or_152,
    rule_priority_no_staff_if_salary_elsewhere,
)


def apply_payroll_rules(rule_ctx: PayrollRuleContext) -> None:
    for rule in PAYROLL_RULES:
        rule(rule_ctx)
