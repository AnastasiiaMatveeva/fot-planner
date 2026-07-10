"""Окладная часть зарплаты: потолок оклада по должности и совместимость с договором."""

from __future__ import annotations

from dataclasses import dataclass

from fot_planner.labor_rules import employee_compatible_with_position_rule
from fot_planner.models import Contract, Employee


def employee_monthly_payment_due(employee: Employee) -> float:
    """Полная месячная сумма, которую нужно выплатить сотруднику."""
    return max(0.0, employee.monthly_wage)


def employee_reference_salary_cap(employee: Employee) -> float:
    """Потолок оклада: справочник должностей × ставка (не зависит от договора)."""
    org_rate = employee.reference_salary_for_rate
    if org_rate is None or org_rate <= 0:
        return 0.0
    return org_rate * employee.rate


def employee_max_salary_amount(employee: Employee) -> float:
    """Оклад не больше зарплаты и справочного потолка."""
    cap = employee_reference_salary_cap(employee)
    if cap <= 0:
        return 0.0
    return min(employee_monthly_payment_due(employee), cap)


@dataclass(frozen=True)
class SalaryPositionOption:
    """Вариант посадки на договорную должность/группу."""

    position_rule_index: int
    position: str | None
    equivalence_group: str | None


def salary_position_options(contract: Contract, employee: Employee) -> list[SalaryPositionOption]:
    """
    Строки договора, на которые можно посадить сотрудника (должность / группа).

    Потолок оклада здесь не задаётся — он только в справочнике на сотруднике.
    """
    if employee_reference_salary_cap(employee) <= 0:
        return []

    if not contract.position_rules:
        return [
            SalaryPositionOption(
                position_rule_index=0,
                position=employee.position,
                equivalence_group=employee.equivalence_group,
            )
        ]

    return [
        SalaryPositionOption(
            position_rule_index=idx,
            position=pr.position,
            equivalence_group=pr.equivalence_group,
        )
        for idx, pr in enumerate(contract.position_rules)
        if employee_compatible_with_position_rule(employee, pr)
    ]


def max_salary_amount_if_contract_used(contract: Contract, employee: Employee) -> float:
    """
    Верхняя граница оклада с договора для оптимизатора.

    Договор влияет только на посадку (есть ли совместимая строка).
    Сумма потолка — из справочника сотрудника, не из договора.
    """
    if not salary_position_options(contract, employee):
        return 0.0
    return employee_max_salary_amount(employee)
