"""Резерв по окладам и неподвижный остаток по месяцам (min_balance_matrix)."""

from __future__ import annotations

from fot_planner.models import Contract

_INFLOW_EPS = 0.01


def fot_inflow_month_count(contract: Contract) -> int:
    """Число месяцев с ненулевым поступлением по fot_matrix / бюджету договора."""
    months: set[int] = set()
    for mb in contract.monthly_budgets:
        if mb.inflow_amount > _INFLOW_EPS:
            months.add(mb.month)
    return len(months)


def is_long_fot_project(contract: Contract, min_fot_months: int) -> bool:
    """Проект с «длинным» ФОТ: больше min_fot_months месяцев с поступлениями."""
    return fot_inflow_month_count(contract) > min_fot_months


def requires_salary_reserve(
    contract: Contract,
    *,
    min_fot_months: int = 6,
) -> bool:
    """
    Нужен резерв под оклады на конец месяца (closing ≥ Σ оклад×ставка назначенных).

    По умолчанию — если в fot_matrix больше min_fot_months месяцев с поступлением,
    независимо от типа договора. Явное require_salary_reserve на договоре перекрывает.
    """
    if contract.require_salary_reserve is not None:
        return contract.require_salary_reserve
    return is_long_fot_project(contract, min_fot_months)


def min_balance_for_month(contract: Contract, month: int) -> float:
    """
    Неподвижные для переноса НАЗАД (лист min_balance_matrix): из месяца m нельзя
    отправить на более ранние месяцы больше, чем (пул − эта сумма).
    На конец месяца и вперёд (m+1) эта сумма не обязана оставаться на счёте.
    """
    for mb in contract.monthly_budgets:
        if mb.month == month and mb.min_balance is not None:
            return mb.min_balance
    return 0.0


def monthly_salary_reserve_expr(employees: dict, uses: dict, contract_id: str, month: int):
    terms = []
    for (e_id, cid, mo, kind), uvar in uses.items():
        if cid != contract_id or mo != month or kind != "salary":
            continue
        emp = employees[e_id]
        terms.append(emp.salary * emp.rate * uvar)
    if not terms:
        return None
    return sum(terms) if len(terms) > 1 else terms[0]


def monthly_salary_reserve_value(employees: dict, uses: dict, contract_id: str, month: int) -> float:
    total = 0.0
    for (e_id, cid, mo, kind), uvar in uses.items():
        if cid != contract_id or mo != month or kind != "salary":
            continue
        if uvar.solution_value() > 0.5:
            emp = employees[e_id]
            total += emp.salary * emp.rate
    return total
