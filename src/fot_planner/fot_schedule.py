"""Физические поступления ФОТ и равномерный план освоения (отдельно от кассы)."""

from __future__ import annotations

from fot_planner.models import Contract, ContractMonthlyBudget
from fot_planner.contract_calendar import contract_allows_month


def active_months_in_year(contract: Contract, year: int) -> list[int]:
    """Месяцы года, когда договор доступен для выплат (начало … конец + months_after_end)."""
    return [m for m in range(1, 13) if contract_allows_month(contract, year, m)]


def uniform_monthly_spend_target(contract: Contract, year: int) -> float | None:
    """Идеальное равномерное освоение ФОТ в активном месяце (total_fot / число месяцев)."""
    months = active_months_in_year(contract, year)
    if not months or contract.total_fot <= 0:
        return None
    return contract.total_fot / len(months)


def default_fot_inflow_at_start(contracts: list[Contract], year: int) -> None:
    """
    Физическое поступление, если fot_matrix не задан: весь total_fot
    в первый активный месяц договора, остальные месяцы — 0.
    """
    for contract in contracts:
        months = active_months_in_year(contract, year)
        if not months or contract.total_fot <= 0:
            contract.monthly_budgets = []
            continue
        start_m = months[0]
        contract.monthly_budgets = [
            ContractMonthlyBudget(
                contract_id=contract.id,
                year=year,
                month=month,
                inflow_amount=contract.total_fot if month == start_m else 0.0,
            )
            for month in months
        ]


def spread_fot_by_active_months(contracts: list[Contract], year: int) -> None:
    """Устарело: равномерное разнесение смешивало план и кассу. Используйте default_fot_inflow_at_start."""
    default_fot_inflow_at_start(contracts, year)


def month_inflow_amount(contract: Contract, year: int, month: int) -> float:
    """Поступление в месяце из monthly_budgets (0, если месяц не задан)."""
    for mb in contract.monthly_budgets:
        if mb.year == year and mb.month == month:
            return mb.inflow_amount
    return 0.0 if contract.monthly_budgets else 0.0


def cumulative_inflow_through_month(contract: Contract, year: int, through_month: int) -> float:
    """Сумма физических поступлений с января по through_month включительно."""
    return sum(
        month_inflow_amount(contract, year, m)
        for m in range(1, through_month + 1)
    )
