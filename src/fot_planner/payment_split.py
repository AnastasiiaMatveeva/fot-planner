"""Разделение зарплаты: обязательный оклад и добор надбавкой."""

from __future__ import annotations

from fot_planner.models import Contract, Employee


def employee_salary_due(employee: Employee) -> float:
    """Окладная часть потребности сотрудника (пока = monthly_wage целиком)."""
    return max(0.0, employee.monthly_wage)


def _compatible_position_rules(contract: Contract, employee: Employee):
    if not contract.position_rules:
        return []

    if employee.equivalence_group:
        group_rules = [
            pr
            for pr in contract.position_rules
            if pr.equivalence_group == employee.equivalence_group
        ]
        if group_rules:
            exact = [pr for pr in group_rules if pr.position == employee.position]
            if exact:
                return exact
            return group_rules

    return [pr for pr in contract.position_rules if pr.position == employee.position]


def _salary_cap_rules(contract: Contract, employee: Employee):
    """Правила для потолка оклада: сначала точное совпадение должности, иначе группа."""
    return _compatible_position_rules(contract, employee)


def salary_cap_on_contract(contract: Contract, employee: Employee) -> float | None:
    """
    Потолок оклада с договора: max_monthly_payment × ставка по совместимым должностям.

    None — потолок не задан (оклад может равняться всей окладной части).
    """
    if not contract.position_rules:
        return None

    caps = [
        pr.max_monthly_payment * employee.rate
        for pr in _salary_cap_rules(contract, employee)
        if pr.max_monthly_payment is not None
    ]
    return max(caps) if caps else None


def required_salary_on_contract(contract: Contract, employee: Employee) -> float:
    """
    Допустимая окладная сумма на договоре:
    min(employee_salary_due, salary_cap).

    Если потолок не задан — вся окладная часть.
    """
    due = employee_salary_due(employee)
    if due <= 0:
        return 0.0
    cap = salary_cap_on_contract(contract, employee)
    if cap is None:
        return due
    return min(due, cap)


def salary_part_on_contract(contract: Contract, employee: Employee) -> float:
    """Alias для required_salary_on_contract (обратная совместимость)."""
    return required_salary_on_contract(contract, employee)


def allowance_part(employee: Employee, salary_paid: float) -> float:
    """Добор до полной зарплаты после оклада: monthly_wage − выплаченный оклад."""
    return max(0.0, employee.monthly_wage - salary_paid)


def salary_compensation_amount(employee: Employee, salary_paid: float) -> float:
    """
    Часть оклада, вынужденно закрытая allowance/incentive из-за потолка:
    employee_salary_due − salary_paid (при полной выплате оклада = cap-gap).
    """
    return max(0.0, employee_salary_due(employee) - salary_paid)
