"""Окладная часть зарплаты: потолок оклада по должности и совместимость с договором."""

from __future__ import annotations

from dataclasses import dataclass

from fot_planner.labor_rules import employee_compatible_with_position_rule
from fot_planner.models import Contract, Employee


def employee_monthly_payment_due(employee: Employee, month: int | None = None) -> float:
    """Полная сумма, которую нужно выплатить строке сотрудника в месяце.

    Зарплата может меняться по месяцам (правило 1, решение пользователя
    19.09.2026): у месяца со своей суммой в ``monthly_wages`` действует она,
    у остальных — зарплата штатного расписания. Без месяца — зарплата
    штатного расписания.
    """
    wage = employee.monthly_wage
    if month is not None:
        wage = (getattr(employee, "monthly_wages", None) or {}).get(month, wage)
    return max(0.0, wage)


def employee_reference_salary_cap(employee: Employee, month: int | None = None) -> float:
    """Потолок оклада: справочник должностей × ставка (не зависит от договора).

    С месяцем — оклад, действующий в этом месяце (индексация с даты, лист
    «лимиты_по_месяцам», 01.10.2026); без месяца — строка справочника."""
    org_rate = employee.reference_salary_for_rate
    if month is not None:
        org_rate = (getattr(employee, "reference_salary_by_month", None) or {}).get(month, org_rate)
    if org_rate is None or org_rate <= 0:
        return 0.0
    return org_rate * employee.rate


def _has_salary_cap(employee: Employee) -> bool:
    """Оклад задан хотя бы в одном месяце года."""
    if employee_reference_salary_cap(employee) > 0:
        return True
    return any(v and v > 0 for v in (getattr(employee, "reference_salary_by_month", None)
                                     or {}).values())


def employee_max_salary_amount(employee: Employee, month: int | None = None) -> float:
    """Оклад не больше зарплаты месяца и справочного потолка этого месяца."""
    cap = employee_reference_salary_cap(employee, month)
    if cap <= 0:
        return 0.0
    return min(employee_monthly_payment_due(employee, month), cap)


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
    if not _has_salary_cap(employee):
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


def max_salary_amount_if_contract_used(
    contract: Contract, employee: Employee, month: int | None = None
) -> float:
    """
    Верхняя граница оклада с договора для оптимизатора.

    Договор влияет только на посадку (есть ли совместимая строка).
    Сумма потолка — из справочника сотрудника, не из договора; зарплата,
    которой оклад не может превысить, — зарплата этого месяца.
    """
    if not salary_position_options(contract, employee):
        return 0.0
    return employee_max_salary_amount(employee, month)
