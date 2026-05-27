"""Распределение годового ФОТ по месяцам (касса) по срокам договора."""

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


def spread_fot_by_active_months(contracts: list[Contract], year: int) -> None:
    """
    Равномерно разнести total_fot по активным месяцам договора.
    Сроки и «месяцев после окончания» учитываются через contract_allows_month.
    """
    for contract in contracts:
        months = active_months_in_year(contract, year)
        if not months or contract.total_fot <= 0:
            contract.monthly_budgets = []
            continue
        per_month = contract.total_fot / len(months)
        contract.monthly_budgets = [
            ContractMonthlyBudget(
                contract_id=contract.id,
                year=year,
                month=month,
                inflow_amount=per_month,
            )
            for month in months
        ]
