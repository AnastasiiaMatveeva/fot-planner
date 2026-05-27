"""Сроки освоения ФОТ и окно выплат по договору.

На листе contracts одна логика «месяцев после окончания»:
  0  — выплаты до даты окончания; освоение всего ФОТ — к этой же дате (если нет −N дней);
  2  — выплаты до конца месяца +2 после окончания; освоение ФОТ — к этому же сроку;
  -1 в Excel → освоение за 20 дней до окончания (госзаказ), без продления месяцев.

Отдельная колонка «срок освоения» — явная дата полного освоения (если нужна раньше расчётной).
«Полное освоение за N дней до срока» — освоить весь total_fot за N дней до end_date.

Порядок расчёта крайней даты освоения (spend_deadline_date):
  1) явный «срок освоения», если задан;
  2) иначе end_date − N дней (типично 20 для ГОЗ);
  3) иначе end_date + месяцев после окончания (типично +2 для внебюджета);
  4) иначе end_date.

К этой дате для каждого договора с total_fot > 0 оптимизатор требует израсходовать весь ФОТ.
"""

from __future__ import annotations

from datetime import date, timedelta

from fot_planner.models import Contract


def payment_extension_months(contract: Contract) -> int:
    """Месяцев после end_date (0 = только до даты окончания)."""
    return contract.months_after_end if contract.months_after_end > 0 else 0


def effective_contract_end(contract: Contract) -> date:
    """Последний день по правилу «+N месяцев после end_date» (без −N дней для ГОЗ)."""
    end = contract.end_date
    extra = payment_extension_months(contract)
    if extra:
        y, m = end.year, end.month + extra
        while m > 12:
            m -= 12
            y += 1
        end = date(y, m, 28)
    return end


def spend_deadline_date(contract: Contract) -> date:
    """Крайний срок, к которому весь total_fot должен быть израсходован по плану."""
    if contract.spend_deadline is not None:
        return contract.spend_deadline
    days = contract.spend_complete_days_before_end
    if days is not None:
        return contract.end_date - timedelta(days=days)
    return effective_contract_end(contract)


def required_full_spend_month(contract: Contract, year: int) -> int | None:
    """Месяц года, к концу которого весь total_fot договора должен быть израсходован."""
    deadline = spend_deadline_date(contract)
    if deadline.year < year:
        return 1
    if deadline.year > year:
        return 12
    return deadline.month


def must_fully_spend_fot(contract: Contract) -> bool:
    """Любой договор с лимитом ФОТ обязан полностью освоить его к spend_deadline_date."""
    return contract.total_fot > 0
