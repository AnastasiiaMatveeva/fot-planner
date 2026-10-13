"""Доступность договора по месяцам и сроки выплат по виду."""

from __future__ import annotations

from datetime import date

from fot_planner.models import PAYMENT_KINDS, Contract, PaymentKind


def payment_deadline_date(contract: Contract, kind: PaymentKind) -> date:
    """Последний день выплат вида с договора (явная дата или end_date)."""
    if kind is PaymentKind.SALARY:
        deadline = contract.salary_payment_deadline
    else:
        deadline = contract.allowances_payment_deadline
    if deadline is not None:
        return deadline
    return contract.end_date


def payment_kind_enabled(contract: Contract, kind: PaymentKind) -> bool:
    if kind is PaymentKind.SALARY:
        return contract.allow_salary
    if kind is PaymentKind.K120:
        return contract.allow_secret
    if kind is PaymentKind.K122:
        return contract.allow_allowance
    if kind is PaymentKind.K124:
        return contract.allow_incentive
    if kind is PaymentKind.K152:
        return contract.allow_extra_work
    return contract.allow_order_incentive


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
    return contract_payment_window_includes_month(contract, year, month, kind)


def contract_payment_window_includes_month(
    contract: Contract, year: int, month: int, kind: PaymentKind
) -> bool:
    """Месяц попадает в окно выплат вида: от начала договора до срока выплаты."""
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


def secret_allowance_active(row, contracts: dict, year: int, month: int) -> bool:
    """Строка листа 120 действует в месяце (правило 10).

    Окно строки задано экономистом — действует оно; не задано — окно 120
    договора секретности: от его начала до срока выплат надбавок."""
    secret_contract = contracts.get(row.secret_contract_id)
    start = getattr(row, "start_date", None)
    end = getattr(row, "end_date", None)
    if start is None and end is None:
        if secret_contract is None:
            return False
        return contract_payment_window_includes_month(
            secret_contract, year, month, PaymentKind.K120)
    period_start, period_end = _month_period(year, month)
    if start is None:
        start = secret_contract.start_date if secret_contract else date(year, 1, 1)
    if end is None:
        end = (payment_deadline_date(secret_contract, PaymentKind.K120)
               if secret_contract else date(year, 12, 31))
    return start <= period_end and period_start <= end


def secret_allowance_pay_contracts(rows, contracts: dict, employee_id: str,
                                   year: int, month: int) -> set[str]:
    """Закреплённые договоры выплаты 120 у сотрудника в месяце; пусто —
    договор выбирает оптимизатор (дополнение 18.09.2026 к правилу 10)."""
    return {row.pay_contract_id for row in rows
            if row.employee_id == employee_id and getattr(row, "pay_contract_id", None)
            and secret_allowance_active(row, contracts, year, month)}
