"""Поступления ФОТ, минимальный остаток кассы и план освоения по интервалам."""

from __future__ import annotations

from fot_planner.models import Contract, ContractMonthlyBudget
from fot_planner.contract_calendar import contract_allows_month


def active_months_in_year(contract: Contract, year: int) -> list[int]:
    """Месяцы года, когда договор доступен для выплат (по срокам трёх видов)."""
    return [m for m in range(1, 13) if contract_allows_month(contract, year, m)]


def monthly_spend_targets(contract: Contract, year: int) -> dict[int, float]:
    """
    Равномерный план освоения по кассовым интервалам:
    каждое поступление распределяется от месяца поступления
    до месяца перед следующим поступлением.
    Последнее поступление — до конца активного периода договора.
    """
    active_months = active_months_in_year(contract, year)
    if not active_months:
        return {}

    targets: dict[int, float] = {m: 0.0 for m in active_months}

    inflows = sorted(
        (mb.month, mb.inflow_amount)
        for mb in contract.monthly_budgets
        if mb.year == year and mb.inflow_amount > 0
    )

    if not inflows:
        return targets

    for idx, (start_month, amount) in enumerate(inflows):
        if idx + 1 < len(inflows):
            next_inflow_month = inflows[idx + 1][0]
            end_month = next_inflow_month - 1
        else:
            end_month = max(active_months)

        spend_months = [
            m for m in active_months
            if start_month <= m <= end_month
        ]

        if not spend_months:
            continue

        portion = amount / len(spend_months)

        for m in spend_months:
            targets[m] += portion

    return targets


def default_fot_inflow_at_start(contracts: list[Contract], year: int) -> None:
    """
    Физическое поступление, если лист «фот_по_месяцам» не задан: весь total_fot
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


def month_inflow_amount(contract: Contract, year: int, month: int) -> float:
    """Поступление в месяце из monthly_budgets (0, если месяц не задан)."""
    for mb in contract.monthly_budgets:
        if mb.year == year and mb.month == month:
            return mb.inflow_amount
    return 0.0


def min_balance_for_month(contract: Contract, month: int) -> float:
    """Мин. остаток на конец месяца из «минимальные_остатки» (0, если не задан)."""
    for mb in contract.monthly_budgets:
        if mb.month == month and mb.min_balance is not None:
            return mb.min_balance
    return 0.0
