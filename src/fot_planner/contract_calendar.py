"""Доступность договора по месяцам планового года."""

from __future__ import annotations

from datetime import date

from fot_planner.spend_rules import spend_deadline_date


def contract_allows_month(contract, year: int, month: int) -> bool:
    """
    Можно ли в этом месяце платить с договора (оклад/надбавка/стимулирующие).

    Освоение ФОТ = выплаты с договора; последний месяц — по spend_deadline_date
    (конец договора, +N месяцев или −N дней до конца — как на листе contracts).
    """
    period_start = date(year, month, 1)
    if month == 12:
        period_end = date(year, 12, 31)
    else:
        period_end = date(year, month + 1, 1)
        period_end = date.fromordinal(period_end.toordinal() - 1)

    if contract.start_date > period_end:
        return False

    if period_start > spend_deadline_date(contract):
        return False
    return True
