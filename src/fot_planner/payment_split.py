"""Разделение месячной зарплаты: оклад с договора и добор гибкими видами выплат."""

from __future__ import annotations

from dataclasses import dataclass

from fot_planner.models import Contract, Employee


def employee_monthly_payment_due(employee: Employee) -> float:
    """Полная месячная сумма, которую нужно выплатить сотруднику."""
    return max(0.0, employee.monthly_wage)


@dataclass(frozen=True)
class SalaryPositionOption:
    """
    Вариант "посадки" сотрудника на позицию договора для выплат salary.

    Каждая строка contract_positions — отдельный вариант.
    """

    position_rule_index: int
    position: str | None
    equivalence_group: str | None
    organization_cap: float
    contract_cap: float
    final_cap: float


def salary_position_options(contract: Contract, employee: Employee) -> list[SalaryPositionOption]:
    """
    Возможные варианты по строкам contract_positions для выплат salary.

    Должность сотрудника не блокирует назначение.
    Организационный потолок берётся из справочника должностей для позиции строки
    (ContractPositionRule.reference_salary_for_rate).
    """
    opts: list[SalaryPositionOption] = []
    for idx, pr in enumerate(contract.position_rules):
        contract_cap = pr.max_monthly_payment
        organization_cap = pr.reference_salary_for_rate
        if contract_cap is None or contract_cap <= 0:
            continue
        if organization_cap is None or organization_cap <= 0:
            continue

        cap_org = organization_cap * employee.rate
        cap_contract = contract_cap * employee.rate
        final = min(cap_org, cap_contract)
        opts.append(
            SalaryPositionOption(
                position_rule_index=idx,
                position=pr.position,
                equivalence_group=pr.equivalence_group,
                organization_cap=cap_org,
                contract_cap=cap_contract,
                final_cap=final,
            )
        )
    return opts


def max_salary_amount_if_contract_used(contract: Contract, employee: Employee) -> float:
    """Верхняя граница salary на договоре (до выбора позиции в оптимизаторе)."""
    due = employee_monthly_payment_due(employee)
    if due <= 0:
        return 0.0
    opts = salary_position_options(contract, employee)
    if not opts:
        return 0.0
    return min(due, max(o.final_cap for o in opts))


def flex_remainder_after_salary(employee: Employee, salary_paid: float) -> float:
    """
    Остаток полной зарплаты после salary — только для отчётов и тестов.

    В оптимизаторе: salary + allowance + incentive == employee_monthly_payment_due.
    """
    return max(0.0, employee_monthly_payment_due(employee) - salary_paid)
