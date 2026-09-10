# -*- coding: utf-8 -*-
"""Проверка готового плана по правилам организации.

Зачем отдельно от решателя. Решатель отвечает на вопрос «нашлось ли решение
модели», и отвечает честно: OPTIMAL значит, что при записанных условиях
лучше не бывает. Но если условие записано неверно или не записано вовсе,
OPTIMAL достаётся плану со ставкой 0,86 — нефизичной, потому что кадровик
такую не оформит. Статус решателя об этом молчать обязан: модель он решил.

Поэтому статуса два и они не смешиваются:

    solver_status = OPTIMAL     нашлось решение модели
    audit_status  = FAIL        план правилам не соответствует

Аудит читает только вход и результат, ничего не решает и ничего не чинит.
Он смотрит на выданные числа глазами человека, который эти числа получит.
Правило без номера сюда не попадает: у каждой проверки есть идентификатор
из `docs/ПРАВИЛА_РЕШАТЕЛЯ.md`, и от нарушения можно дойти до строки свода и
до случая стенда.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fot_planner.models import AuditViolation, PlanningResult

#: Шаг ставки. Кадровое оформление меньшей доли не знает: приказом открывают
#: четверть, половину, три четверти, целую.
RATE_STEP = 0.25
#: Ставки — доли, посчитанные в числах с плавающей точкой; 0,7500000001 это
#: та же три четверти, а 0,86 — не она.
RATE_EPS = 1e-6

MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль",
          "август", "сентябрь", "октябрь", "ноябрь", "декабрь")

#: Проверки в порядке номеров. Каждая — функция (вход, результат) → нарушения.
CHECKS: list[tuple[str, str, object]] = []


def check(rule_id: str, name: str):
    """Записать проверку в список. Порядок вывода — порядок объявления."""
    def wrap(fn):
        CHECKS.append((rule_id, name, fn))
        return fn
    return wrap


@dataclass
class AuditReport:
    """Итог проверки: годится план или нет и что именно не так."""

    status: str = "OK"  # OK | FAIL
    violations: list[AuditViolation] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)  # номера отработавших правил

    @property
    def errors(self) -> list[AuditViolation]:
        return [v for v in self.violations if v.severity == "error"]

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "checked": list(self.checked),
            "violations": [
                {"rule_id": v.rule_id, "severity": v.severity, "message": v.message,
                 "employee": v.employee_id, "contract": v.contract_id,
                 "month": v.month, "actual": v.actual, "expected": v.expected}
                for v in self.violations
            ],
        }


def _month_name(month) -> str:
    return MONTHS[month - 1] if isinstance(month, int) and 1 <= month <= 12 else str(month)


def _who(ctx, employee_id: str) -> str:
    """Человека называем так, как его знает экономист, а не табельным номером."""
    for e in getattr(ctx, "employees", None) or []:
        if e.id == employee_id:
            name = (getattr(e, "full_name", None) or "").strip()
            # Табельный номер вместо имени бывает во входе стенда: повторять
            # его дважды («E1 (E1)») ни к чему.
            return "%s (%s)" % (name, employee_id) if name and name != employee_id \
                else employee_id
    return employee_id


def _is_step(value: float) -> bool:
    return abs(value / RATE_STEP - round(value / RATE_STEP)) <= RATE_EPS


@check("RATE-001", "Шаг открытой ставки")
def _rate_step(ctx, result: PlanningResult) -> list[AuditViolation]:
    """Открытая ставка кратна 0,25 и не отрицательна.

    Проверяются именно открытые ставки плана, а не ставки штатного
    расписания: план вправе открыть человеку долю сверх основного места, но
    любая доля остаётся долей четверти.
    """
    out = []
    for row in result.open_rate_attributions:
        rate = float(row.open_rate)
        if rate < -RATE_EPS:
            out.append(AuditViolation(
                rule_id="RATE-001", severity="error",
                message="%s, %s, договор %s: открытая ставка %s — отрицательная"
                        % (_who(ctx, row.employee_id), _month_name(row.month),
                           row.contract_id, _fmt(rate)),
                employee_id=row.employee_id, contract_id=row.contract_id,
                month=row.month, actual=round(rate, 6), expected="кратно 0,25"))
        elif not _is_step(rate):
            out.append(AuditViolation(
                rule_id="RATE-001", severity="error",
                message="%s, %s, договор %s: открытая ставка %s не кратна 0,25"
                        % (_who(ctx, row.employee_id), _month_name(row.month),
                           row.contract_id, _fmt(rate)),
                employee_id=row.employee_id, contract_id=row.contract_id,
                month=row.month, actual=round(rate, 6), expected="кратно 0,25"))
    return out


def _fmt(value: float) -> str:
    """Число по-русски: запятая и без хвоста нулей."""
    text = ("%.4f" % value).rstrip("0").rstrip(".")
    return (text or "0").replace(".", ",")


def audit(ctx, result: PlanningResult) -> AuditReport:
    """Проверить готовый план. Вход нужен только чтобы называть людей словами."""
    report = AuditReport()
    for rule_id, _name, fn in CHECKS:
        report.checked.append(rule_id)
        report.violations.extend(fn(ctx, result))
    report.status = "FAIL" if report.errors else "OK"
    return report
