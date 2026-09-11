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

from collections import defaultdict
from dataclasses import dataclass, field

from fot_planner.contract_calendar import contract_allows_payment_month, payment_kind_enabled
from fot_planner.labor_rules import planned_labor_amount
from fot_planner.models import (
    MAX_LABOR_TOLERANCE,
    AuditViolation,
    PaymentKind,
    PlanningResult,
    labor_row_id,
)
from fot_planner.payment_split import employee_monthly_payment_due
from fot_planner.validation import employee_active_in_month

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


def _person_key(employee) -> str:
    number = str(getattr(employee, "person_id", "") or "").strip().lower()
    if number:
        return number
    return " ".join(str(getattr(employee, "full_name", "") or "").lower().split()) or employee.id


@check("SALARY-001", "Полная месячная зарплата")
def _full_salary(ctx, result: PlanningResult) -> list[AuditViolation]:
    """План не может молча потерять активную строку сотрудника."""
    if not getattr(ctx, "contracts", None):
        return []
    paid = defaultdict(float)
    for a in result.allocations:
        paid[(a.employee_id, a.month)] += float(a.amount)
    for d in result.deficits:
        paid[(d.employee_id, d.month)] += float(d.amount)
    out = []
    for e in ctx.employees:
        due = employee_monthly_payment_due(e)
        if due <= 0:
            continue
        for month in range(1, 13):
            if not employee_active_in_month(e, ctx.year, month):
                continue
            actual = paid[(e.id, month)]
            if abs(actual - due) > 1.0:
                out.append(AuditViolation(
                    rule_id="SALARY-001", severity="error",
                    message=(f"{_who(ctx, e.id)}, {_month_name(month)}: выплачено "
                             f"{_fmt(actual)} ₽ вместо {_fmt(due)} ₽"),
                    employee_id=e.id, month=month, actual=round(actual, 2),
                    expected="полная месячная зарплата"))
    return out


@check("PAYMENT-001", "Разрешённый вид выплаты и срок")
def _payment_window(ctx, result: PlanningResult) -> list[AuditViolation]:
    contracts = {c.id: c for c in getattr(ctx, "contracts", [])}
    if not contracts:
        return []
    out = []
    for a in result.allocations:
        if a.amount <= 0.01:
            continue
        c = contracts.get(a.contract_id)
        if c is None:
            continue
        if not payment_kind_enabled(c, a.payment_kind):
            out.append(AuditViolation(
                rule_id="PAYMENT-001", severity="error",
                message=(f"{_who(ctx, a.employee_id)}, {_month_name(a.month)}, "
                         f"договор {a.contract_id}: вид {a.payment_kind.name} запрещён"),
                employee_id=a.employee_id, contract_id=a.contract_id, month=a.month,
                expected="вид разрешён договором"))
        elif not contract_allows_payment_month(c, ctx.year, a.month, a.payment_kind):
            out.append(AuditViolation(
                rule_id="PAYMENT-001", severity="error",
                message=(f"{_who(ctx, a.employee_id)}, {_month_name(a.month)}, "
                         f"договор {a.contract_id}: выплата вне окна"),
                employee_id=a.employee_id, contract_id=a.contract_id, month=a.month,
                expected="в пределах срока договора и выплат"))
    return out


@check("LABOR-001", "Трудоёмкость в пределах допуска")
def _labor_tolerance(ctx, result: PlanningResult) -> list[AuditViolation]:
    plans = getattr(ctx, "labor_plans", None) or []
    if not plans:
        return []
    pm = defaultdict(float)
    amount = defaultdict(float)
    for a in result.labor_pm_attributions:
        pm[(a.labor_row_id, a.month)] += float(a.person_months)
    for a in result.labor_payment_attributions:
        amount[a.labor_row_id] += float(a.amount)
    tol = min(max(float(ctx.salary_stability.goz_labor_tolerance), 0.0), MAX_LABOR_TOLERANCE)
    out = []
    for lp in plans:
        row = labor_row_id(lp)
        target_amount = planned_labor_amount(lp)
        actual_pm = sum(pm[(row, m)] for m in range(1, 13))
        if abs(actual_pm - lp.person_months) > max(0.01, abs(lp.person_months) * tol + 1e-6):
            out.append(AuditViolation(
                rule_id="LABOR-001", severity="error",
                message=(f"{lp.contract_id}/{lp.position or lp.equivalence_group}: "
                         f"{_fmt(actual_pm)} чел.-мес. вместо {_fmt(lp.person_months)} "
                         f"(допуск {tol * 100:g} %)"),
                contract_id=lp.contract_id, actual=round(actual_pm, 4),
                expected=f"{lp.person_months} ± {tol * 100:g} %"))
        if target_amount > 0 and abs(amount[row] - target_amount) > max(1.0, target_amount * tol):
            out.append(AuditViolation(
                rule_id="LABOR-001", severity="error",
                message=(f"{lp.contract_id}/{lp.position or lp.equivalence_group}: "
                         f"{_fmt(amount[row])} ₽ по трудоёмкости вместо "
                         f"{_fmt(target_amount)} ₽ (допуск {tol * 100:g} %)"),
                contract_id=lp.contract_id, actual=round(amount[row], 2),
                expected=f"{target_amount} ± {tol * 100:g} %"))
    return out


@check("PERSON-001", "Ограничения человека")
def _person_limits(ctx, result: PlanningResult) -> list[AuditViolation]:
    employees = {e.id: e for e in getattr(ctx, "employees", [])}
    if not employees:
        return []
    by_person = defaultdict(list)
    for r in result.open_rate_attributions:
        employee = employees.get(r.employee_id)
        key = _person_key(employee) if employee is not None else str(r.employee_id)
        by_person[key].append(r)
    out = []
    for key, rows in by_person.items():
        for month in range(1, 13):
            active = [r for r in rows if r.month == month and r.open_rate > RATE_EPS]
            total = sum(r.open_rate for r in active)
            if total > 1.5 + RATE_EPS:
                out.append(AuditViolation(
                    rule_id="PERSON-001", severity="error",
                    message=f"табельный номер {key}, {_month_name(month)}: ставка {total:g} > 1,5",
                    month=month, actual=round(total, 6), expected="не больше 1,5"))
    return out


@check("FOT-001", "ФОТ договора и касса")
def _contract_money(ctx, result: PlanningResult) -> list[AuditViolation]:
    contracts = {c.id: c for c in getattr(ctx, "contracts", [])}
    if not contracts:
        return []
    spent = defaultdict(float)
    for a in result.allocations:
        spent[a.contract_id] += float(a.amount)
    out = []
    for cid, c in contracts.items():
        if spent[cid] > float(c.total_fot) + 1.0:
            out.append(AuditViolation(
                rule_id="FOT-001", severity="error",
                message=f"договор {cid}: выплаты {_fmt(spent[cid])} ₽ выше ФОТ {_fmt(c.total_fot)} ₽",
                contract_id=cid, actual=round(spent[cid], 2), expected=str(c.total_fot)))
    for b in result.contract_balances:
        if b.closing_balance < -1.0:
            out.append(AuditViolation(
                rule_id="FOT-001", severity="error",
                message=f"договор {b.contract_id}, {_month_name(b.month)}: касса { _fmt(b.closing_balance) } ₽ ниже нуля",
                contract_id=b.contract_id, month=b.month, actual=round(b.closing_balance, 2),
                expected="не ниже нуля"))
    return out


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
    """Число по-русски: тысячи через пробел, запятая, без хвоста нулей.

    Так экономист видит суммы везде в сервисе («160 000 ₽»); ставка 0,86 при
    этом остаётся ставкой, а не «0,8600».
    """
    text = ("%.4f" % value).rstrip("0").rstrip(".") or "0"
    sign, text = ("-", text[1:]) if text.startswith("-") else ("", text)
    whole, _, frac = text.partition(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    return sign + " ".join(groups) + ("," + frac if frac else "")


def audit(ctx, result: PlanningResult) -> AuditReport:
    """Проверить готовый план. Вход нужен только чтобы называть людей словами."""
    report = AuditReport()
    for rule_id, _name, fn in CHECKS:
        report.checked.append(rule_id)
        report.violations.extend(fn(ctx, result))
    report.status = "FAIL" if report.errors else "OK"
    return report
