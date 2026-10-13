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
from dataclasses import asdict, dataclass, field
import json

from fot_planner.actuals import fact_amounts, frozen_facts, frozen_through, relaxed_labor_band
from fot_planner.contract_calendar import (
    contract_allows_month,
    contract_allows_payment_month,
    payment_kind_enabled,
)
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
#: Правило применимости: (вход, результат) → (применима ли, основание).
#: Неприменимость обязана иметь проверяемое основание (CAP-004): «во входе нет
#: договоров» — основание; «не реализовано» — не основание, а not_run.
APPLIES: dict[str, object] = {}


class MeasuredFindings(list):
    """Список нарушений остаётся совместимым; сравнения хранятся рядом.

    Измерение снимается в месте проверки, не восстанавливается по сообщению
    и не вычисляется второй, потенциально расходящейся реализацией правила.
    """
    def __init__(self):
        super().__init__()
        self.measurements = []
        self.gaps = []

    def measure(self, metric, actual, unit, *, passed, lower=None, upper=None,
                absolute_tolerance=0, relative_tolerance=None,
                lower_inclusive=True, upper_inclusive=True, **coordinates):
        self.measurements.append({
            'metric': metric, 'actual': actual, 'unit': unit,
            'lower': lower, 'upper': upper, 'absolute_tolerance': absolute_tolerance,
            'lower_inclusive': lower_inclusive, 'upper_inclusive': upper_inclusive,
            'relative_tolerance': relative_tolerance, 'passed': bool(passed),
            'coordinates': coordinates})
        return passed


def check(rule_id: str, name: str, applies=None):
    """Записать проверку в список. Порядок вывода — порядок объявления."""
    def wrap(fn):
        CHECKS.append((rule_id, name, fn))
        if applies is not None:
            APPLIES[rule_id] = applies
        return fn
    return wrap


def _needs(attr: str, what: str):
    """Проверка применима, когда во входе есть такие записи."""
    def rule(ctx, result):
        return bool(getattr(ctx, attr, None)), "во входе нет %s" % what
    return rule


def _needs_all(*rules):
    def rule(ctx, result):
        for r in rules:
            ok, why = r(ctx, result)
            if not ok:
                return False, why
        return True, ""
    return rule


def _needs_open_rates(ctx, result):
    return bool(result.open_rate_attributions), "в плане нет открытых ставок"


@dataclass
class AuditReport:
    """Итог проверки: годится план или нет и что именно не так."""

    status: str = "OK"  # OK | FAIL
    violations: list[AuditViolation] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)  # номера отработавших правил
    # Состояние каждой проверки: passed | failed | not_applicable (с основанием)
    # | not_run (обязательна, но не реализована). Пустой список — не «всё
    # хорошо», а «ничего не проверялось».
    checks: list[dict] = field(default_factory=list)

    @property
    def errors(self) -> list[AuditViolation]:
        return [v for v in self.violations if v.severity == "error"]

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "checked": list(self.checked),
            "checks": [dict(c) for c in self.checks],
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


# Пересчёт с факта (правило 26, владелец продукта 30.09.2026): месяцы 1…F —
# история с листа «факт_выплат». Правила одного месяца (зарплата целиком,
# виды и сроки, шаг ставки, основное место) к ним не применяются: план их не
# выбирал, и история, нарушившая правило, план негодным не делает. Правила,
# которые тянутся через год (ФОТ договора, годовая трудоёмкость), считают
# факт вместе с планом; нарушение, которое сделала одна история, — warning,
# а не error. Что месяцы факта в плане равны факту, проверяет PAYMENT-001.
def _plan_month(ctx, month) -> bool:
    """Месяц выбирал расчёт, а не история."""
    return not isinstance(month, int) or month > frozen_through(ctx)


@check("SALARY-001", "Полная месячная зарплата",
       applies=_needs_all(_needs("contracts", "договоров"), _needs("employees", "сотрудников")))
def _full_salary(ctx, result: PlanningResult) -> list[AuditViolation]:
    """План не может молча потерять активную строку сотрудника."""
    if not getattr(ctx, "contracts", None):
        return []
    paid = defaultdict(float)
    out = MeasuredFindings()
    for a in result.allocations:
        paid[(a.employee_id, a.month)] += float(a.amount)
    for d in result.deficits:
        paid[(d.employee_id, d.month)] += float(d.amount)
        allowed = bool(getattr(ctx, 'allow_deficit', False))
        if not out.measure('deficit_amount', float(d.amount), 'RUB',
                           upper=None if allowed else 0, absolute_tolerance=.005,
                           passed=allowed or d.amount <= .005,
                           employee_id=d.employee_id, month=d.month):
            out.append(AuditViolation(
                rule_id='SALARY-001', severity='error', employee_id=d.employee_id,
                month=d.month, actual=float(d.amount), expected='дефицит не разрешён',
                message=(f'{_who(ctx, d.employee_id)}, {_month_name(d.month)}: '
                         f'дефицит {_fmt(d.amount)} ₽ не разрешён экономистом')))
    for e in ctx.employees:
        for month in range(1, 13):
            if not employee_active_in_month(e, ctx.year, month) or not _plan_month(ctx, month):
                continue
            # Зарплата у каждого месяца своя (правило 1, решение пользователя
            # 19.09.2026): сверка с одной суммой на год пропустила бы план,
            # где после повышения выплачена прежняя зарплата. Строка без
            # зарплаты не проверяется, как и раньше, а своя сумма месяца
            # сверяется и нулевая: ровно она и положена.
            due = employee_monthly_payment_due(e, month)
            if due <= 0 and month not in (getattr(e, "monthly_wages", None) or {}):
                continue
            actual = paid[(e.id, month)]
            if not out.measure('allocated_plus_deficit', actual, 'RUB',
                               lower=due, upper=due, absolute_tolerance=1.0,
                               employee_id=e.id, month=month,
                               passed=not (abs(actual - due) > 1.0)):
                out.append(AuditViolation(
                    rule_id="SALARY-001", severity="error",
                    message=(f"{_who(ctx, e.id)}, {_month_name(month)}: выплачено "
                             f"{_fmt(actual)} ₽ вместо {_fmt(due)} ₽"),
                    employee_id=e.id, month=month, actual=round(actual, 2),
                    expected="полная месячная зарплата"))
    return out


@check("PAYMENT-001", "Разрешённый вид выплаты и срок", applies=_needs("contracts", "договоров"))
def _payment_window(ctx, result: PlanningResult) -> list[AuditViolation]:
    contracts = {c.id: c for c in getattr(ctx, "contracts", [])}
    if not contracts:
        return []
    out = MeasuredFindings()
    salary = defaultdict(float)
    for allocation in result.allocations:
        if allocation.payment_kind == PaymentKind.SALARY:
            salary[(allocation.employee_id, allocation.contract_id, allocation.month)] += float(allocation.amount)
    for a in result.allocations:
        if a.amount <= 0.01 or not _plan_month(ctx, a.month):
            continue
        c = contracts.get(a.contract_id)
        if c is None:
            continue
        enabled = payment_kind_enabled(c, a.payment_kind)
        coords = dict(employee_id=a.employee_id, contract_id=a.contract_id,
                      month=a.month, payment_kind=a.payment_kind.name)
        if not out.measure('payment_kind_enabled', enabled, 'boolean',
                           lower=1, upper=1, passed=enabled, **coords):
            out.append(AuditViolation(
                rule_id="PAYMENT-001", severity="error",
                message=(f"{_who(ctx, a.employee_id)}, {_month_name(a.month)}, "
                         f"договор {a.contract_id}: вид {a.payment_kind.name} запрещён"),
                employee_id=a.employee_id, contract_id=a.contract_id, month=a.month,
                expected="вид разрешён договором"))
        elif not out.measure('payment_month_allowed',
                            allowed := contract_allows_payment_month(c, ctx.year, a.month, a.payment_kind),
                            'boolean', lower=1, upper=1, passed=allowed, **coords):
            out.append(AuditViolation(
                rule_id="PAYMENT-001", severity="error",
                message=(f"{_who(ctx, a.employee_id)}, {_month_name(a.month)}, "
                         f"договор {a.contract_id}: выплата вне окна"),
                employee_id=a.employee_id, contract_id=a.contract_id, month=a.month,
                expected="в пределах срока договора и выплат"))
        if a.payment_kind == PaymentKind.K122:
            salary_amount = salary[(a.employee_id, a.contract_id, a.month)]
            if not out.measure('salary_on_allowance_contract', salary_amount, 'RUB',
                               lower=.01, lower_inclusive=False, passed=salary_amount > .01, **coords):
                out.append(AuditViolation(
                    rule_id='PAYMENT-001', severity='error', employee_id=a.employee_id,
                    contract_id=a.contract_id, month=a.month, actual=salary_amount,
                    expected='оклад той же строки на том же договоре в том же месяце',
                    message=(f'{_who(ctx, a.employee_id)}, {_month_name(a.month)}, '
                             f'договор {a.contract_id}: 122 без оклада этой строки')))
    _manual_payment_checks(ctx, result, out)
    _k152_checks(ctx, result, out)
    _priority_checks(ctx, result, out)
    _fact_checks(ctx, result, out)
    return out


def _fact_checks(ctx, result, out):
    """Месяцы факта в плане — ровно лист «факт_выплат» (правило 26).

    Владелец продукта 30.09.2026: «просто нельзя менять их». Сверяется
    каждая четвёрка «строка — договор — месяц — вид» месяцев 1…F в обе
    стороны: сумма факта не изменена, выплата факта не потеряна, новой
    выплаты в прошедшем месяце не появилось. Факт по договору, которого во
    входе нет, расчёт не держит — его здесь нет и в ожидании.
    """
    last = frozen_through(ctx)
    if not last:
        return
    expected = fact_amounts(frozen_facts(ctx))
    actual = defaultdict(float)
    for a in result.allocations:
        if isinstance(a.month, int) and 1 <= a.month <= last:
            actual[(a.employee_id, a.contract_id, a.month, a.payment_kind)] += float(a.amount)
    for key in sorted(set(expected) | set(actual), key=lambda k: (k[2], k[0], k[1], int(k[3]))):
        eid, cid, month, kind = key
        want, got = round(expected.get(key, 0.0), 2), round(actual.get(key, 0.0), 2)
        if out.measure('frozen_fact_amount', got, 'RUB', lower=want, upper=want,
                       absolute_tolerance=.01, passed=abs(got - want) <= .01,
                       employee_id=eid, contract_id=cid, month=month, payment_kind=kind.name):
            continue
        what = ('добавлена выплата, которой в факте нет' if key not in expected
                else 'выплата факта потеряна' if got <= .01
                else f'сумма факта {_fmt(want)} ₽ изменена на {_fmt(got)} ₽')
        out.append(AuditViolation(
            rule_id='PAYMENT-001', severity='error', employee_id=eid, contract_id=cid,
            month=month, actual=got, expected=f'факт {want}',
            message=(f'{_who(ctx, eid)}, {_month_name(month)} — месяц факта, договор {cid}, '
                     f'вид {kind.name}: {what}')))


def _priority_checks(ctx, result, out):
    priority_ids = {c.id for c in ctx.contracts if c.priority_payment_mode}
    if not priority_ids:
        return
    paid = defaultdict(lambda: defaultdict(float))
    salary = defaultdict(lambda: defaultdict(float))
    for a in result.allocations:
        if a.amount <= 0 or not _plan_month(ctx, a.month):
            continue
        paid[(a.employee_id, a.contract_id, a.month)][a.payment_kind] += float(a.amount)
        if a.payment_kind == PaymentKind.SALARY:
            salary[(a.employee_id, a.month)][a.contract_id] += float(a.amount)
    # Проверяем фактические суммы независимо от uses/ограничений оптимизатора.
    # Приказ не входит в штатные виды; его отдельная квалификация — пробел.
    staff_kinds = (PaymentKind.SALARY, PaymentKind.K120, PaymentKind.K122, PaymentKind.K124)
    for (eid, cid, month), amounts in paid.items():
        if cid not in priority_ids:
            continue
        coords = dict(employee_id=eid, contract_id=cid, month=month)
        external_salary = sum(value for other_cid, value in salary[(eid, month)].items()
                              if other_cid != cid)
        for metric, value in (('priority_152_amount', amounts[PaymentKind.K152]),
                              ('priority_external_salary', external_salary)):
            out.measure(metric, value, 'RUB', lower=0, passed=value >= 0, **coords)
        conditions = (
            (amounts[PaymentKind.K152] > .01, 'priority_staff_with_152', 'одновременно с 152'),
            (external_salary > .01, 'priority_staff_with_external_salary', 'при окладе на другом договоре'),
        )
        for active, metric, reason in conditions:
            if not active:
                continue
            for kind in staff_kinds:
                actual = amounts[kind]
                if not out.measure(metric, actual, 'RUB', upper=.01, passed=actual <= .01,
                                   payment_kind=kind.name, **coords):
                    out.append(AuditViolation(
                        rule_id='PAYMENT-001', severity='error', **coords, actual=actual,
                        expected='раздельные режимы штатных выплат и 152 приоритета',
                        message=(f'{_who(ctx, eid)}, {_month_name(month)}, {cid}: '
                                 f'приоритет — {kind.value} {reason} ({_fmt(actual)} ₽)')))


def _k152_checks(ctx, result, out):
    # Запрет относится к строке и месяцу, а не к договору или человеку.
    # Перечень надбавок задан по правилу 11 независимо от ограничений модели;
    # совместимость с отдельным видом «приказ» здесь не квалифицируется.
    paid = defaultdict(lambda: defaultdict(float))
    for a in result.allocations:
        if a.amount > 0 and _plan_month(ctx, a.month):
            paid[(a.employee_id, a.month)][a.payment_kind] += float(a.amount)
    for (eid, month), amounts in paid.items():
        if amounts[PaymentKind.K152] <= .01:
            continue
        for kind in (PaymentKind.K120, PaymentKind.K122, PaymentKind.K124):
            actual = amounts[kind]
            if not out.measure('k152_excluded_amount', actual, 'RUB', upper=.01,
                               passed=actual <= .01, employee_id=eid, month=month,
                               payment_kind=kind.name):
                out.append(AuditViolation(
                    rule_id='PAYMENT-001', severity='error', employee_id=eid,
                    month=month, actual=actual,
                    expected='при 152 нет надбавок 120/122/124 в той же строке и месяце',
                    message=(f'{_who(ctx, eid)}, {_month_name(month)}: '
                             f'152 одновременно с {kind.value} ({_fmt(actual)} ₽)')))


def _manual_payment_checks(ctx, result, out):
    employees = {e.id: e for e in getattr(ctx, 'employees', [])}
    paid = defaultdict(float)
    paid_any_kind = defaultdict(float)
    for a in result.allocations:
        key = (a.employee_id, a.contract_id, a.month, a.payment_kind)
        paid[key] += float(a.amount)
        paid_any_kind[key[:3]] += float(a.amount)
        # Разрешения и запреты — о выборе расчёта; прошедший месяц — факт.
        if a.amount <= .01 or not _plan_month(ctx, a.month):
            continue
        employee = employees.get(a.employee_id)
        allowed = getattr(employee, 'allowed_contracts', [])
        forbidden = getattr(employee, 'forbidden_contracts', [])
        reasons = []
        if allowed and a.contract_id not in allowed:
            reasons.append('договор вне разрешённого списка строки')
        if a.contract_id in forbidden:
            reasons.append('запрет договора для строки')
        for p in getattr(ctx, 'manual_prohibitions', []):
            if (p.year == ctx.year and p.employee_id == a.employee_id and p.contract_id == a.contract_id
                    and p.month_from <= a.month <= p.month_to
                    and (p.payment_kind is None or p.payment_kind == a.payment_kind)):
                reasons.append('ручной запрет выплаты')
        coords = dict(employee_id=a.employee_id, contract_id=a.contract_id,
                      month=a.month, payment_kind=a.payment_kind.name)
        if not out.measure('manual_payment_allowed', not reasons, 'boolean', lower=1, upper=1,
                           passed=not reasons, **coords):
            out.append(AuditViolation(rule_id='PAYMENT-001', severity='error',
                employee_id=a.employee_id, contract_id=a.contract_id, month=a.month,
                expected='соблюдены разрешения и запреты экономиста',
                message=f'{_who(ctx, a.employee_id)}, {_month_name(a.month)}, {a.contract_id}: ' + '; '.join(reasons)))
    assignments = defaultdict(list)
    for a in getattr(ctx, 'manual_assignments', []):
        if a.year != ctx.year:
            continue
        # Закрепление на прошедший месяц не выполнить: месяц равен факту, и
        # решатель урезает срок до месяцев после факта (правило 26).
        for month in range(max(1, a.month_from, frozen_through(ctx) + 1), min(12, a.month_to) + 1):
            assignments[(a.employee_id, a.contract_id, month, a.payment_kind)].append(a.fixed_amount)
    for (eid, cid, month, kind), amounts in assignments.items():
        if kind is None:
            _kindless_assignment_check(ctx, out, eid, cid, month, amounts,
                                       paid_any_kind[(eid, cid, month)])
            continue
        actual = paid[(eid, cid, month, kind)]
        coords = dict(employee_id=eid, contract_id=cid, month=month, payment_kind=kind.name)
        # Несколько числовых назначений суммируются, как в ручном вводе модели.
        # Смешанный набор с None задаёт только присутствие: его семантика
        # требует отдельного решения, а не незаметного ужесточения аудитором.
        target = sum(amounts) if all(x is not None for x in amounts) else None
        if target is not None and target <= 0:
            out.gaps.append({**coords, 'reason': 'нулевая/отрицательная фиксация не проверена: требуется решение владельца'})
            continue
        if target is None and any(x is not None for x in amounts):
            out.gaps.append({**coords, 'reason': 'смешанная фиксация: проверено присутствие, точная сумма требует решения владельца'})
        if target is None:
            ok = out.measure('manual_assignment_present', actual, 'RUB', lower=.01,
                             lower_inclusive=False, passed=actual > .01, **coords)
        else:
            ok = out.measure('manual_assignment_amount', actual, 'RUB', lower=target, upper=target,
                             absolute_tolerance=.01, passed=abs(actual - target) <= .01, **coords)
        if not ok:
            out.append(AuditViolation(rule_id='PAYMENT-001', severity='error', employee_id=eid,
                contract_id=cid, month=month, actual=actual,
                expected=str(target) if target is not None else 'выплата назначена',
                message=f'{_who(ctx, eid)}, {_month_name(month)}, {cid}: ручное назначение не выполнено'))


def _kindless_assignment_check(ctx, out, eid, cid, month, amounts, actual):
    """Закрепление без вида (MANUAL-ANY-001, решение пользователя 19.09.2026).

    Годится выплата с договора любого вида, поэтому сверяется сумма всех
    видов пары в месяце, а координата вида — «любой». Суммы у такого
    закрепления нет: загрузчик её не пропускает, а пришедшая мимо него не
    проверяется и отмечается пробелом, а не сверяется с догадкой о виде.
    """
    coords = dict(employee_id=eid, contract_id=cid, month=month, payment_kind='любой')
    if any(x is not None for x in amounts):
        out.gaps.append({**coords, 'reason': 'сумма у закрепления без вида не проверена: '
                                             'сумма бывает только у конкретного вида'})
    if not out.measure('manual_assignment_present', actual, 'RUB', lower=.01,
                       lower_inclusive=False, passed=actual > .01, **coords):
        out.append(AuditViolation(rule_id='PAYMENT-001', severity='error', employee_id=eid,
            contract_id=cid, month=month, actual=actual, expected='выплата любого вида',
            message=(f'{_who(ctx, eid)}, {_month_name(month)}, {cid}: ручное назначение '
                     f'без вида не выполнено — выплат с договора нет')))


@check("LABOR-001", "Трудоёмкость в пределах допуска", applies=_needs("labor_plans", "строк трудоёмкости"))
def _labor_tolerance(ctx, result: PlanningResult) -> list[AuditViolation]:
    plans = getattr(ctx, "labor_plans", None) or []
    if not plans:
        return []
    pm = defaultdict(float)
    amount = defaultdict(float)
    amount_month = defaultdict(float)
    for a in result.labor_pm_attributions:
        pm[(a.labor_row_id, a.month)] += float(a.person_months)
    for a in result.labor_payment_attributions:
        amount[a.labor_row_id] += float(a.amount)
        amount_month[(a.labor_row_id, getattr(a, "month", None))] += float(a.amount)
    tol = min(max(float(ctx.salary_stability.goz_labor_tolerance), 0.0), MAX_LABOR_TOLERANCE)
    last = frozen_through(ctx)
    contracts = {c.id: c for c in getattr(ctx, "contracts", None) or []}
    out = MeasuredFindings()
    for lp in plans:
        row = labor_row_id(lp)
        target_amount = planned_labor_amount(lp)
        actual_pm = sum(pm[(row, m)] for m in range(1, 13))
        pm_tolerance = max(0.01, abs(lp.person_months) * tol + 1e-6)
        amount_tolerance = max(1.0, target_amount * tol)
        pm_ok = not (abs(actual_pm - lp.person_months) > pm_tolerance)
        pm_lower = pm_upper = lp.person_months
        amount_ok = not (abs(amount[row] - target_amount) > amount_tolerance)
        amount_lower = amount_upper = target_amount
        if last:
            pm_band = (lp.person_months - pm_tolerance, lp.person_months + pm_tolerance)
            amount_band = (target_amount - amount_tolerance, target_amount + amount_tolerance)
            # Год строки с фактом: сделанное в месяцах факта входит в сумму, а
            # коридор ослабляется ровно на то, что уже сделала история, — как
            # в модели (relaxed_labor_band, правило 26).
            if lp.monthly:
                ahead = sum(float(v) for m, v in lp.monthly.items() if m > last)
                has_future, monthly_ahead = ahead > 0, ahead
            else:
                contract = contracts.get(lp.contract_id)
                has_future = contract is None or any(
                    contract_allows_month(contract, ctx.year, m) for m in range(last + 1, 13))
                monthly_ahead = None
            done_pm = sum(pm[(row, m)] for m in range(1, last + 1))
            done_amount = sum(amount_month[(row, m)] for m in range(1, last + 1))
            avg = float(lp.avg_monthly_labor_cost or 0.0)
            pm_band = relaxed_labor_band(*pm_band, done_pm, tol=tol, has_future=has_future,
                                         monthly_future_plan=monthly_ahead)
            amount_band = relaxed_labor_band(
                *amount_band, done_amount, tol=tol, has_future=has_future,
                monthly_future_plan=None if monthly_ahead is None else avg * monthly_ahead)
            pm_ok = pm_band[0] - 1e-9 <= actual_pm <= pm_band[1] + 1e-9
            pm_lower, pm_upper = pm_band[0] + pm_tolerance, pm_band[1] - pm_tolerance
            amount_ok = amount_band[0] - 1e-6 <= amount[row] <= amount_band[1] + 1e-6
            amount_lower = amount_band[0] + amount_tolerance
            amount_upper = amount_band[1] - amount_tolerance
        if not out.measure('labor_person_months', actual_pm, 'person_month',
                           lower=pm_lower, upper=pm_upper,
                           absolute_tolerance=pm_tolerance, relative_tolerance=tol,
                           contract_id=lp.contract_id, labor_row_id=row,
                           passed=pm_ok):
            out.append(AuditViolation(
                rule_id="LABOR-001", severity="error",
                message=(f"{lp.contract_id}/{lp.position or lp.equivalence_group}: "
                         f"{_fmt(actual_pm)} чел.-мес. вместо {_fmt(lp.person_months)} "
                         f"(допуск {tol * 100:g} %)"),
                contract_id=lp.contract_id, actual=round(actual_pm, 4),
                expected=f"{lp.person_months} ± {tol * 100:g} %"))
        elif abs(actual_pm - lp.person_months) > pm_tolerance:
            out.append(AuditViolation(
                rule_id="LABOR-001", severity="warning",
                message=(f"{lp.contract_id}/{lp.position or lp.equivalence_group}: "
                         f"{_fmt(actual_pm)} чел.-мес. вместо {_fmt(lp.person_months)} — "
                         f"так сложилось по факту прошедших месяцев"),
                contract_id=lp.contract_id, actual=round(actual_pm, 4),
                expected=f"{lp.person_months} ± {tol * 100:g} %"))
        # Годовой итог не доказывает выполнение этапов: недобор января нельзя
        # компенсировать февралём. Отсутствующие месяцы непустого плана — ноль.
        # Месяц факта — история: его коридор не проверяется.
        if lp.monthly:
            for month in range(last + 1, 13):
                target_pm = float(lp.monthly.get(month, 0.0))
                actual_month = pm[(row, month)]
                lower, upper = (1 - tol) * target_pm, (1 + tol) * target_pm
                if not out.measure('labor_month_person_months', actual_month, 'person_month',
                        lower=lower, upper=upper, absolute_tolerance=1e-6, relative_tolerance=tol,
                        contract_id=lp.contract_id, labor_row_id=row, month=month,
                        passed=lower - 1e-6 <= actual_month <= upper + 1e-6):
                    out.append(AuditViolation(
                        rule_id='LABOR-001', severity='error', contract_id=lp.contract_id,
                        month=month, actual=actual_month,
                        expected=f'{target_pm} ± {tol * 100:g} %',
                        message=(f'{lp.contract_id}/{lp.position or lp.equivalence_group}, '
                                 f'{_month_name(month)}: {_fmt(actual_month)} чел.-мес. '
                                 f'вместо {_fmt(target_pm)} (допуск {tol * 100:g} %)')))
        if target_amount > 0 and not out.measure('labor_amount', amount[row], 'RUB',
                lower=amount_lower, upper=amount_upper, absolute_tolerance=amount_tolerance,
                relative_tolerance=tol, contract_id=lp.contract_id, labor_row_id=row,
                passed=amount_ok):
            out.append(AuditViolation(
                rule_id="LABOR-001", severity="error",
                message=(f"{lp.contract_id}/{lp.position or lp.equivalence_group}: "
                         f"{_fmt(amount[row])} ₽ по трудоёмкости вместо "
                         f"{_fmt(target_amount)} ₽ (допуск {tol * 100:g} %)"),
                contract_id=lp.contract_id, actual=round(amount[row], 2),
                expected=f"{target_amount} ± {tol * 100:g} %"))
        elif target_amount > 0 and abs(amount[row] - target_amount) > amount_tolerance:
            out.append(AuditViolation(
                rule_id="LABOR-001", severity="warning",
                message=(f"{lp.contract_id}/{lp.position or lp.equivalence_group}: "
                         f"{_fmt(amount[row])} ₽ по трудоёмкости вместо "
                         f"{_fmt(target_amount)} ₽ — так сложилось по факту прошедших месяцев"),
                contract_id=lp.contract_id, actual=round(amount[row], 2),
                expected=f"{target_amount} ± {tol * 100:g} %"))
    return out


@check("PERSON-001", "Ограничения человека", applies=_needs("employees", "сотрудников"))
def _person_limits(ctx, result: PlanningResult) -> list[AuditViolation]:
    employees = {e.id: e for e in getattr(ctx, "employees", [])}
    if not employees:
        return []
    by_person = defaultdict(list)
    people = defaultdict(list)
    for employee in employees.values():
        people[_person_key(employee)].append(employee)
    for r in result.open_rate_attributions:
        # Ставки месяцев факта — история: 0,3 или две основных там бывают.
        if not _plan_month(ctx, r.month):
            continue
        employee = employees.get(r.employee_id)
        key = _person_key(employee) if employee is not None else str(r.employee_id)
        by_person[key].append(r)
    out = MeasuredFindings()
    for key, rows in by_person.items():
        categories = {getattr(e, 'employment_category', 'regular') for e in people[key]}
        limit = .75 if 'graduate_student' in categories and 'student' not in categories else 1.5
        if 'student' in categories and len(categories) > 1:
            out.gaps.append({'person_id': key,
                             'reason': 'сочетание student с другими категориями не квалифицировано'})
        for month in range(frozen_through(ctx) + 1, 13):
            active = [r for r in rows if r.month == month and r.open_rate > RATE_EPS]
            total = sum(r.open_rate for r in active)
            if not out.measure('person_total_rate', total, 'FTE', upper=limit,
                               absolute_tolerance=RATE_EPS, person_id=key, month=month,
                               passed=not (total > limit + RATE_EPS)):
                out.append(AuditViolation(
                    rule_id="PERSON-001", severity="error",
                    message=f"табельный номер {key}, {_month_name(month)}: ставка {total:g} > {str(limit).replace('.', ',')}",
                    month=month, actual=round(total, 6), expected=f"не больше {limit:g}"))
            by_row = defaultdict(list)
            for r in active:
                by_row[r.employee_id].append(r)
            for eid, records in by_row.items():
                employee = employees.get(eid)
                part = sum(r.open_rate for r in records if not getattr(r, 'is_main', False)
                           or getattr(employee, 'employment_type', 'auto') == 'part_time')
                if not out.measure('row_part_time_rate', part, 'FTE', upper=.5,
                                   absolute_tolerance=RATE_EPS, passed=part <= .5 + RATE_EPS,
                                   employee_id=eid, month=month):
                    out.append(AuditViolation(rule_id='PERSON-001', severity='error',
                        employee_id=eid, month=month, actual=part, expected='совместительство строки <= 0.5',
                        message=f'{_who(ctx, eid)}, {_month_name(month)}: совместительство {part:g} > 0.5'))
                if getattr(employee, 'employment_category', None) == 'student':
                    row_total = sum(r.open_rate for r in records)
                    cap = float(employee.rate)
                    if not out.measure('student_row_rate', row_total, 'FTE', upper=cap,
                                       absolute_tolerance=RATE_EPS, passed=row_total <= cap + RATE_EPS,
                                       employee_id=eid, month=month):
                        out.append(AuditViolation(rule_id='PERSON-001', severity='error',
                            employee_id=eid, month=month, actual=row_total, expected=f'не выше входной ставки {cap:g}',
                            message=f'{_who(ctx, eid)}, {_month_name(month)}: студент — {row_total:g} выше входной ставки {cap:g}'))
    for key, employees_for_person in people.items():
        for month in range(frozen_through(ctx) + 1, 13):
            count = sum(1 for e in employees_for_person
                        if getattr(e, 'employment_type', 'auto') == 'part_time'
                        and employee_active_in_month(e, ctx.year, month))
            if not out.measure('person_part_time_rows', count, 'count', upper=2,
                               passed=count <= 2, person_id=key, month=month):
                out.append(AuditViolation(rule_id='PERSON-001', severity='error', month=month,
                    actual=count, expected='не больше двух одновременных совместительств',
                    message=f'табельный номер {key}, {_month_name(month)}: совместительств {count} > 2'))
    salary = defaultdict(float)
    for a in result.allocations:
        employee = employees.get(a.employee_id)
        if (employee is not None and a.payment_kind == PaymentKind.SALARY and a.amount > 0
                and _plan_month(ctx, a.month)):
            salary[(_person_key(employee), a.month)] += float(a.amount)
    for (key, month), amount in salary.items():
        if amount <= .01:
            continue
        # Проверяется и отсутствие всех записей ставок: нельзя признать
        # основное место существующим только по факту выплаты оклада.
        count = sum(1 for r in by_person[key] if r.month == month
                    and r.open_rate > RATE_EPS and r.is_main)
        coords = dict(person_id=key, month=month)
        out.measure('person_salary_amount', amount, 'RUB', lower=.01,
                    lower_inclusive=False, passed=amount > .01, **coords)
        if not out.measure('person_main_count', count, 'count', lower=1, upper=1,
                           passed=count == 1, **coords):
            out.append(AuditViolation(
                rule_id='PERSON-001', severity='error', month=month, actual=count,
                expected='ровно одно основное место у получателя оклада',
                message=f'табельный номер {key}, {_month_name(month)}: основных мест {count}, требуется 1'))
    return out


@check("FOT-001", "ФОТ договора и касса", applies=_needs("contracts", "договоров"))
def _contract_money(ctx, result: PlanningResult) -> list[AuditViolation]:
    contracts = {c.id: c for c in getattr(ctx, "contracts", [])}
    if not contracts:
        return []
    spent = defaultdict(float)
    spent_before = defaultdict(float)
    last = frozen_through(ctx)
    for a in result.allocations:
        spent[a.contract_id] += float(a.amount)
        if not _plan_month(ctx, a.month):
            spent_before[a.contract_id] += float(a.amount)
    out = MeasuredFindings()
    for cid, c in contracts.items():
        fot = float(c.total_fot)
        # Предел ФОТ — на год вместе с фактом. Если факт один уже его
        # превысил, план годен, пока перерасход не вырос: после факта с
        # договора не платили (правило 26).
        limit = max(fot, spent_before[cid]) if last else fot
        if not out.measure('contract_total_spent', spent[cid], 'RUB', upper=limit,
                           absolute_tolerance=1.0, contract_id=cid,
                           passed=not (spent[cid] > limit + 1.0)):
            out.append(AuditViolation(
                rule_id="FOT-001", severity="error",
                message=f"договор {cid}: выплаты {_fmt(spent[cid])} ₽ выше ФОТ {_fmt(c.total_fot)} ₽",
                contract_id=cid, actual=round(spent[cid], 2), expected=str(c.total_fot)))
        elif spent[cid] > fot + 1.0:
            out.append(AuditViolation(
                rule_id="FOT-001", severity="warning",
                message=(f"договор {cid}: выплаты {_fmt(spent[cid])} ₽ выше ФОТ {_fmt(fot)} ₽ — "
                         f"перерасход сделан в месяцах факта, после факта с договора не платили"),
                contract_id=cid, actual=round(spent[cid], 2), expected=str(c.total_fot)))
    for b in result.contract_balances:
        # Касса месяца факта — история: она могла уйти в минус.
        if not _plan_month(ctx, b.month):
            continue
        if not out.measure('closing_balance', b.closing_balance, 'RUB', lower=0,
                           absolute_tolerance=1.0, contract_id=b.contract_id, month=b.month,
                           passed=not (b.closing_balance < -1.0)):
            out.append(AuditViolation(
                rule_id="FOT-001", severity="error",
                message=f"договор {b.contract_id}, {_month_name(b.month)}: касса { _fmt(b.closing_balance) } ₽ ниже нуля",
                contract_id=b.contract_id, month=b.month, actual=round(b.closing_balance, 2),
                expected="не ниже нуля"))
    return out


@check("RATE-001", "Шаг открытой ставки", applies=_needs_open_rates)
def _rate_step(ctx, result: PlanningResult) -> list[AuditViolation]:
    """Открытая ставка кратна 0,25 и не отрицательна.

    Проверяются именно открытые ставки плана, а не ставки штатного
    расписания: план вправе открыть человеку долю сверх основного места, но
    любая доля остаётся долей четверти.
    """
    out = MeasuredFindings()
    for row in result.open_rate_attributions:
        # Ставка месяца факта — как её оформили; 0,3 в истории бывает.
        if not _plan_month(ctx, row.month):
            continue
        rate = float(row.open_rate)
        coords = dict(employee_id=row.employee_id, contract_id=row.contract_id, month=row.month)
        if not out.measure('open_rate_nonnegative', rate, 'FTE', lower=0,
                           absolute_tolerance=RATE_EPS, passed=not (rate < -RATE_EPS), **coords):
            out.append(AuditViolation(
                rule_id="RATE-001", severity="error",
                message="%s, %s, договор %s: открытая ставка %s — отрицательная"
                        % (_who(ctx, row.employee_id), _month_name(row.month),
                           row.contract_id, _fmt(rate)),
                employee_id=row.employee_id, contract_id=row.contract_id,
                month=row.month, actual=round(rate, 6), expected="кратно 0,25"))
        elif not out.measure('quarter_step_residual', abs(rate / RATE_STEP - round(rate / RATE_STEP)),
                             'quarter_step', lower=0, upper=0, absolute_tolerance=RATE_EPS,
                             passed=_is_step(rate), **coords):
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


def audit(ctx, result: PlanningResult, required=None) -> AuditReport:
    """Проверить готовый план. Вход нужен только чтобы называть людей словами.

    ``required`` — номера проверок, обязательных для этого результата (их
    задаёт каталог, CAP-004). Обязательная проверка без реализации получает
    not_run и нарушение: план не считается проверенным. Неприменимая
    проверка получает основание — «во входе нет договоров», — а не молчание.
    """
    report = AuditReport()
    for rule_id, name, fn in CHECKS:
        applies = APPLIES.get(rule_id)
        ok, why = applies(ctx, result) if applies is not None else (True, "")
        if not ok:
            report.checks.append({"rule_id": rule_id, "name": name,
                                  "status": "not_applicable", "reason": why})
            continue
        found = fn(ctx, result)
        report.checked.append(rule_id)
        report.violations.extend(found)
        measurements = getattr(found, 'measurements', None)
        coverage_gaps = getattr(found, 'gaps', [])
        report.checks.append({"rule_id": rule_id, "name": name, "reason": "",
                              "findings": [asdict(v) for v in found],
                              "coverage_gaps": coverage_gaps,
                              "measurement_schema": 1 if measurements is not None else None,
                              "measurements": measurements,
                              "measurement_gap": ('есть непроверенные условия: см. coverage_gaps' if coverage_gaps else None if measurements else
                                  "нет объектов сравнения" if measurements == [] else
                                  "исполнитель не передал измерения"),
                              "status": "failed" if any(v.severity == "error" for v in found)
                              else "passed"})
    known = {rule_id for rule_id, _name, _fn in CHECKS}
    for rule_id in required or ():
        if rule_id in known:
            continue
        report.checks.append({"rule_id": rule_id, "name": "", "status": "not_run",
                              "reason": "обязательная проверка не реализована в аудите"})
        report.violations.append(AuditViolation(
            rule_id=rule_id, severity="error",
            message="обязательная проверка %s не выполнена: реализации в аудите нет — "
                    "план не считается проверенным" % rule_id,
            expected="проверка выполнена"))
    report.status = "FAIL" if report.errors else "OK"
    return report


CHECK_LINE = "Проверка:"


def format_checks(checks) -> list[str]:
    """Машинный протокол v1: числа и координаты не восстанавливаются из текста."""
    return [CHECK_LINE + " " + json.dumps({"schema": 1, "check": c},
             ensure_ascii=False, separators=(",", ":"), allow_nan=False) for c in checks]


def parse_check_line(line: str):
    """Обратно из строки вывода; None — это не строка проверки."""
    text = line.strip()
    if not text.startswith(CHECK_LINE):
        return None
    payload = text[len(CHECK_LINE):].strip()
    if payload.startswith("{"):
        def invalid_constant(value):
            raise ValueError("недопустимое число в проверке: " + value)
        obj = json.loads(payload, parse_constant=invalid_constant)
        row = obj.get("check")
        if (type(obj.get("schema")) is not int or obj["schema"] != 1
                or not isinstance(row, dict) or not isinstance(row.get("rule_id"), str)
                or not row["rule_id"] or not isinstance(row.get("status"), str)):
            raise ValueError("неизвестная схема или неверная запись проверки")
        return row
    # Совместимость со снимками и выводом прежнего CLI.
    head, _, reason = payload.partition(" — ")
    parts = head.split()
    if len(parts) < 2:
        return None
    return {"rule_id": parts[0], "status": parts[1], "reason": reason.strip()}
