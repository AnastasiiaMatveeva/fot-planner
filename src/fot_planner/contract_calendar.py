"""Доступность договора по месяцам и сроки выплат по виду."""

from __future__ import annotations

from datetime import date

from fot_planner.models import PAYMENT_KINDS, Contract, PaymentKind


def _terms_for_kind(contract: Contract, kind: PaymentKind):
    if kind == "salary":
        return contract.salary_terms
    if kind == "allowance":
        return contract.allowance_terms
    return contract.incentive_terms


def payment_deadline_date(contract: Contract, kind: PaymentKind) -> date:
    """Последний день выплат вида с договора (явная дата или end_date)."""
    terms = _terms_for_kind(contract, kind)
    if terms.payment_deadline is not None:
        return terms.payment_deadline
    return contract.end_date


def payment_kind_enabled(contract: Contract, kind: PaymentKind) -> bool:
    if kind == "salary":
        return contract.allow_salary
    if kind == "allowance":
        return contract.allow_allowance
    return contract.allow_incentive


def _month_period(year: int, month: int) -> tuple[date, date]:
    period_start = date(year, month, 1)
    if month == 12:
        period_end = date(year, 12, 31)
    else:
        period_end = date(year, month + 1, 1)
        period_end = date.fromordinal(period_end.toordinal() - 1)
    return period_start, period_end


def contract_allows_payment_month(
    contract: Contract, year: int, month: int, kind: PaymentKind
) -> bool:
    """
    Можно ли в этом месяце платить с договора указанным видом выплаты.

    [первый день месяца; последний день месяца] ∩ [start_date; срок выплат вида].
    """
    if not payment_kind_enabled(contract, kind):
        return False
    period_start, period_end = _month_period(year, month)
    if contract.start_date > period_end:
        return False
    if period_start > payment_deadline_date(contract, kind):
        return False
    return True


def payment_months_in_year(contract: Contract, year: int, kind: PaymentKind) -> list[int]:
    return [
        m
        for m in range(1, 13)
        if contract_allows_payment_month(contract, year, m, kind)
    ]


def payment_month_count(contract: Contract, year: int, kind: PaymentKind) -> int:
    return len(payment_months_in_year(contract, year, kind))


def contract_allows_month(contract: Contract, year: int, month: int) -> bool:
    """Хотя бы один разрешённый вид выплат доступен в месяце."""
    return any(
        contract_allows_payment_month(contract, year, month, kind)
        for kind in PAYMENT_KINDS
    )


def latest_payment_month(contract: Contract, year: int) -> int | None:
    """Последний месяц года с хотя бы одной разрешённой выплатой."""
    last: int | None = None
    for kind in PAYMENT_KINDS:
        if not payment_kind_enabled(contract, kind):
            continue
        months = payment_months_in_year(contract, year, kind)
        if months:
            last = max(last or 0, months[-1])
    return last
