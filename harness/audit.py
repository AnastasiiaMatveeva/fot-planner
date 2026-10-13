# -*- coding: utf-8 -*-
"""Стенд проверки результата: годный план проходит, негодный — нет.

Проверяется сам аудитор, а не решатель: планы здесь собираются руками, из
записей результата. Так проверка идёт миллисекунды и ловит именно то, ради
чего аудитор существует, — что он не пропускает нефизичное число и называет
человека, месяц и правило.

Случай, найденный в жизни, попадает сюда навсегда: аудитор без случая через
правку-другую перестаёт ловить то, ради чего написан.

    .venv/Scripts/python.exe harness/audit.py
    .venv/Scripts/python.exe harness/audit.py --only а01
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from datetime import date  # noqa: E402

from fot_planner.models import (  # noqa: E402
    AllocationRecord, Contract, ContractBalanceRecord, ContractLaborPlan,
    DeficitRecord, Employee, LaborPaymentAttribution, LaborPmAttribution,
    OpenRateAttribution, PaymentKind, PlanningContext, PlanningResult,
    labor_row_id,
)
from fot_planner.result_audit import audit  # noqa: E402

YEAR = 2026
CASES = []


def case(num, title):
    def wrap(fn):
        CASES.append((num, title, fn))
        return fn
    return wrap


class Ctx:
    """Вход в объёме, который нужен аудиту: людей он зовёт по имени."""

    def __init__(self, employees):
        self.year = YEAR
        self.employees = employees


def person(emp_id="E001", fio="Иванов Иван Иванович", rate=1.0):
    return Employee(
        id=emp_id, full_name=fio, position="Инженер", rate=rate,
        monthly_wage=160000.0, department="Отдел 1",
    )


def plan(rates, employees=None):
    """План из одних открытых ставок: (человек, договор, месяц, ставка)."""
    result = PlanningResult(
        year=YEAR, allocations=[], deficits=[], conflicts=[],
        contract_balances=[], solver_status="OPTIMAL", objective_value=0.0,
        solve_time_sec=0.0,
    )
    result.open_rate_attributions = [
        OpenRateAttribution(employee_id=e, contract_id=c, year=YEAR, month=m,
                            open_rate=v, is_main=True, position="Инженер")
        for e, c, m, v in rates
    ]
    return Ctx(employees or [person()]), result


def expect_ok(report):
    if report.status != "OK":
        return ["план годный, а аудит сказал %s: %s" % (
            report.status, "; ".join(v.message for v in report.violations))]
    return []


def expect_fail(report, rule_id, must_mention=()):
    if report.status != "FAIL":
        return ["негодный план прошёл аудит"]
    hit = [v for v in report.violations if v.rule_id == rule_id]
    if not hit:
        return ["нарушение есть, но не по правилу %s: %s" % (
            rule_id, ", ".join(v.rule_id for v in report.violations))]
    bad = []
    text = " ".join(v.message for v in hit)
    for word in must_mention:
        if word not in text:
            bad.append("в сообщении нет «%s»: %s" % (word, text))
    return bad


@case("а01", "RATE-001: четверти проходят")
def _c01():
    ctx, res = plan([("E001", "C_GOZ-26", 6, 0.75),
                     ("E001", "C_COM-26", 6, 0.5),
                     ("E001", "C_GOZ-26", 7, 1.0),
                     ("E001", "C_GOZ-26", 8, 0.25),
                     ("E001", "C_GOZ-26", 9, 0.0)])
    return expect_ok(audit(ctx, res))


@case("а02", "RATE-001: 0,86 не проходит, названы человек, месяц и договор")
def _c02():
    ctx, res = plan([("E001", "C_GOZ-26", 6, 0.86)])
    return expect_fail(audit(ctx, res), "RATE-001",
                       ("Иванов Иван Иванович", "июнь", "C_GOZ-26", "0,86"))


@case("а03", "RATE-001: доля правильной величины, но не кратная шагу")
def _c03():
    # 0,3 меньше половины и на вид безобидна: приказом такую не открывают.
    ctx, res = plan([("E001", "C_GOZ-26", 3, 0.3)])
    return expect_fail(audit(ctx, res), "RATE-001", ("март", "0,3"))


@case("а04", "RATE-001: отрицательная ставка")
def _c04():
    ctx, res = plan([("E001", "C_GOZ-26", 5, -0.25)])
    return expect_fail(audit(ctx, res), "RATE-001", ("отрицательная",))


@case("а05", "RATE-001: погрешность счёта не считается нарушением")
def _c05():
    # Решатель возвращает доли числами с плавающей точкой: 0,7499999997 — это
    # три четверти, и придираться к ней значит красить годные планы красным.
    ctx, res = plan([("E001", "C_GOZ-26", 6, 0.75 - 1e-9),
                     ("E001", "C_GOZ-26", 7, 0.5 + 3e-10)])
    return expect_ok(audit(ctx, res))


@case("а06", "RATE-001: нарушение у второго человека не теряется")
def _c06():
    people = [person(), person("E002", "Сидорова Анна Петровна", 0.5)]
    ctx, res = plan([("E001", "C_GOZ-26", 6, 1.0),
                     ("E002", "C_GRANT-26", 6, 0.4)], employees=people)
    return expect_fail(audit(ctx, res), "RATE-001",
                       ("Сидорова Анна Петровна", "0,4"))


@case("а07", "Пустой план проходит: проверять нечего")
def _c07():
    ctx, res = plan([])
    return expect_ok(audit(ctx, res))


@case("а08", "Отчёт аудита переводится в записи для ленты и выгрузки")
def _c08():
    ctx, res = plan([("E001", "C_GOZ-26", 6, 0.86)])
    data = audit(ctx, res).as_dict()
    bad = []
    if data.get("status") != "FAIL":
        bad.append("status не FAIL")
    if "RATE-001" not in (data.get("checked") or []):
        bad.append("в checked нет RATE-001: %s" % data.get("checked"))
    row = (data.get("violations") or [{}])[0]
    for key, want in (("rule_id", "RATE-001"), ("employee", "E001"),
                      ("contract", "C_GOZ-26"), ("month", 6),
                      ("expected", "кратно 0,25")):
        if row.get(key) != want:
            bad.append("%s = %r, ожидалось %r" % (key, row.get(key), want))
    if abs(float(row.get("actual") or 0) - 0.86) > 1e-9:
        bad.append("actual = %r" % row.get("actual"))
    return bad


@case("а09", "Расчёт кладёт итог аудита в результат отдельно от статуса решателя")
def _c09():
    from fot_planner.models import PlanningResult as PR
    fresh = PR(year=YEAR, allocations=[], deficits=[], conflicts=[],
               contract_balances=[], solver_status="OPTIMAL",
               objective_value=0.0, solve_time_sec=0.0)
    bad = []
    if fresh.audit_status != "NOT_RUN":
        bad.append("непроверенный план не помечен NOT_RUN: %s" % fresh.audit_status)
    if fresh.audit_violations:
        bad.append("у непроверенного плана есть нарушения")
    return bad


# ── Остальные карточки: план с договорами, зарплатой, трудоёмкостью ───────


def contract(cid="C_A", fot=2_000_000.0, **flags):
    """Договор на год с явными сроками выплат: карточки читают окно выплат."""
    c = Contract(id=cid, name=cid, number="1/26", contract_type="НИР",
                 start_date=date(YEAR, 1, 1), end_date=date(YEAR, 12, 31),
                 total_fot=fot, allow_salary=True, allow_secret=False,
                 allow_allowance=True, allow_incentive=False,
                 allow_extra_work=False, allow_order_incentive=False,
                 salary_payment_deadline=date(YEAR, 12, 31),
                 allowances_payment_deadline=date(YEAR, 12, 31))
    for k, v in flags.items():
        setattr(c, k, v)
    return c


def full_plan(employees, contracts, pays, deficits=(), balances=(), labor=(),
              pm=(), money=(), rates=None):
    """Настоящий PlanningContext и результат из записей.

    pays — (строка, договор, месяц, вид, сумма); labor — строки РКМ;
    pm/money — их закрытие; rates — открытые ставки (строка, договор, месяц,
    ставка, основное). Если rates не задан, денежные fixtures получают
    синтетические ставки: одна основная по человеку, остальные по 0.5.
    Это только наполнение теста, не вывод ставок аудитором из зарплаты;
    проверки занятости задают rates явно, в том числе пустой список.
    """
    ctx = PlanningContext(year=YEAR, employees=list(employees),
                          contracts=list(contracts), labor_plans=list(labor))
    res = PlanningResult(year=YEAR, allocations=[
        AllocationRecord(employee_id=e, contract_id=c, year=YEAR, month=m,
                         payment_kind=k, amount=v) for e, c, m, k, v in pays],
        deficits=[DeficitRecord(employee_id=e, year=YEAR, month=m, due_amount=d,
                                paid_amount=d - v, amount=v) for e, m, d, v in deficits],
        conflicts=[], contract_balances=[
            ContractBalanceRecord(contract_id=c, year=YEAR, month=m, opening_balance=0.0,
                                  inflow=0.0, spent=0.0, closing_balance=b)
            for c, m, b in balances],
        solver_status="OPTIMAL", objective_value=0.0, solve_time_sec=0.0)
    res.labor_pm_attributions = [
        LaborPmAttribution(employee_id=e, contract_id=lp.contract_id, year=YEAR, month=m,
                           labor_row_id=labor_row_id(lp), position=lp.position,
                           equivalence_group=None, person_months=v)
        for lp, e, m, v in pm]
    res.labor_payment_attributions = [
        LaborPaymentAttribution(employee_id=e, contract_id=lp.contract_id, year=YEAR,
                                month=m, payment_kind=PaymentKind.SALARY,
                                labor_row_id=labor_row_id(lp), position=lp.position,
                                equivalence_group=None, amount=v)
        for lp, e, m, v in money]
    if rates is None:
        rates, seen_rows, seen_persons = [], set(), set()
        employees_by_id = {e.id: e for e in employees}
        for eid, cid, month, kind, amount in pays:
            if kind != PaymentKind.SALARY or amount <= .01 or (eid, month) in seen_rows:
                continue
            employee = employees_by_id[eid]
            person_key = (str(employee.person_id or employee.full_name).strip().lower(), month)
            main = person_key not in seen_persons
            rates.append((eid, cid, month, min(employee.rate, 1.0 if main else .5), main))
            seen_rows.add((eid, month))
            seen_persons.add(person_key)
    res.open_rate_attributions = [
        OpenRateAttribution(employee_id=e, contract_id=c, year=YEAR, month=m,
                            open_rate=v, is_main=main, position="Инженер")
        for e, c, m, v, main in rates]
    return ctx, res


def year_salary(emp_id, cid, wage):
    return [(emp_id, cid, m, PaymentKind.SALARY, wage) for m in range(1, 13)]


@case("а10", "SALARY-001: зарплата целиком проходит, недоплата без дефицита — нет")
def _c10():
    e, c = person(), contract()
    ctx, res = full_plan([e], [c], year_salary("E001", "C_A", 160000.0))
    bad = expect_ok(audit(ctx, res))
    pays = year_salary("E001", "C_A", 160000.0)
    pays[5] = ("E001", "C_A", 6, PaymentKind.SALARY, 150000.0)   # июнь: минус 10 000
    ctx, res = full_plan([e], [c], pays)
    return bad + expect_fail(audit(ctx, res), "SALARY-001",
                             ("Иванов Иван Иванович", "июнь", "150 000", "160 000"))


@case("а11", "SALARY-001: разрешённый дефицит закрывает недоплату")
def _c11():
    e, c = person(), contract()
    pays = year_salary("E001", "C_A", 160000.0)
    pays[5] = ("E001", "C_A", 6, PaymentKind.SALARY, 150000.0)
    ctx, res = full_plan([e], [c], pays, deficits=[("E001", 6, 160000.0, 10000.0)])
    ctx.allow_deficit = True
    return expect_ok(audit(ctx, res))


@case("а12", "PAYMENT-001: запрещённый договором вид и выплата вне окна")
def _c12():
    e, c = person(), contract()                       # 124 на C_A запрещена
    pays = year_salary("E001", "C_A", 160000.0)
    pays.append(("E001", "C_A", 3, PaymentKind.K124, 5000.0))
    ctx, res = full_plan([e], [c], pays)
    bad = expect_fail(audit(ctx, res), "PAYMENT-001", ("март", "C_A", "K124"))
    c2 = contract(salary_payment_deadline=date(YEAR, 6, 30))   # оклад только до июня
    ctx, res = full_plan([e], [c2], year_salary("E001", "C_A", 160000.0))
    return bad + expect_fail(audit(ctx, res), "PAYMENT-001", ("июль", "вне окна"))


@case("а13", "LABOR-001: строка РКМ в допуске проходит, недобор 20 % — нет")
def _c13():
    e, c = person(), contract()
    lp = ContractLaborPlan(contract_id="C_A", year=YEAR, person_months=12.0,
                           position="Инженер", avg_monthly_labor_cost=160000.0)
    pays = year_salary("E001", "C_A", 160000.0)
    good_pm = [(lp, "E001", m, 1.0) for m in range(1, 13)]
    good_money = [(lp, "E001", m, 160000.0) for m in range(1, 13)]
    ctx, res = full_plan([e], [c], pays, labor=[lp], pm=good_pm, money=good_money)
    bad = expect_ok(audit(ctx, res))
    short_pm = [(lp, "E001", m, 0.8) for m in range(1, 13)]           # 9,6 из 12
    ctx, res = full_plan([e], [c], pays, labor=[lp], pm=short_pm, money=good_money)
    return bad + expect_fail(audit(ctx, res), "LABOR-001", ("C_A/Инженер", "9,6", "12"))


@case("а14", "PERSON-001: две строки одного табельного номера, вместе 1,75 — нет")
def _c14():
    main = person("E001-1", "Иванов Иван Иванович", 1.0)
    part = person("E001-2", "Иванов Иван Иванович", 0.75)
    main.person_id = part.person_id = "T-001"
    c = contract(fot=5_000_000.0)          # денег хватает: проверяем только ставки
    rates = [("E001-1", "C_A", 6, 1.0, True), ("E001-2", "C_A", 6, 0.75, False)]
    rates = [(eid, cid, month, rate, main) for eid, cid, _, rate, main in rates
             for month in range(1, 13)]
    pays = year_salary("E001-1", "C_A", 160000.0) + year_salary("E001-2", "C_A", 160000.0)
    ctx, res = full_plan([main, part], [c], pays, rates=rates)
    bad = expect_fail(audit(ctx, res), "PERSON-001", ("t-001", "июнь", "1,5"))
    # Однофамильцы с разными номерами — разные люди, у каждого своя 1,0.
    other = person("E002", "Иванов Иван Иванович", 1.0)
    other.person_id = "T-002"
    rates = [("E001-1", "C_A", 6, 1.0, True), ("E002", "C_A", 6, 1.0, True)]
    rates = [(eid, cid, month, rate, main) for eid, cid, _, rate, main in rates
             for month in range(1, 13)]
    pays = year_salary("E001-1", "C_A", 160000.0) + year_salary("E002", "C_A", 160000.0)
    ctx, res = full_plan([main, other], [c], pays, rates=rates)
    return bad + expect_ok(audit(ctx, res))


@case("а15", "FOT-001: выплаты выше ФОТ договора и касса ниже нуля")
def _c15():
    e, c = person(), contract(fot=1_000_000.0)          # зарплата за год 1 920 000
    ctx, res = full_plan([e], [c], year_salary("E001", "C_A", 160000.0))
    bad = expect_fail(audit(ctx, res), "FOT-001", ("C_A", "1 920 000", "1 000 000"))
    c2 = contract()
    ctx, res = full_plan([e], [c2], year_salary("E001", "C_A", 160000.0),
                         balances=[("C_A", 2, -5000.0)])
    return bad + expect_fail(audit(ctx, res), "FOT-001", ("февраль", "ниже нуля"))


@case("а16", "LABOR-001: месяцы нельзя компенсировать годовым итогом; годовой строке месяцы не навязываются")
def _c16():
    e, c = person(), contract()
    lp = ContractLaborPlan(contract_id='C_A', year=YEAR, person_months=2,
                           position='Инженер', monthly={1: 1, 2: 1})
    pays = year_salary('E001', 'C_A', 160000.0)
    def plan(values):
        ctx, result = full_plan([e], [c], pays, labor=[lp],
            pm=[(lp, 'E001', month, value) for month, value in values.items()])
        ctx.salary_stability.goz_labor_tolerance = .05
        return ctx, result
    bad = expect_ok(audit(*plan({1: 1.05, 2: .95})))
    for values in ({1: 1.2, 2: .8}, {1: 1, 3: 1}, {1: 1.05001, 2: .94999}):
        report = audit(*plan(values))
        bad += expect_fail(report, 'LABOR-001', ('C_A/Инженер',))
        monthly = [m for check in report.checks for m in check.get('measurements', [])
                   if m['metric'] == 'labor_month_person_months']
        if len(monthly) != 12 or not any(not m['passed'] for m in monthly):
            bad.append('нет полного набора месячных измерений с нарушением')
    lp.monthly = None
    bad += expect_ok(audit(*plan({3: 2})))
    return bad


@case("а17", "PAYMENT-001: 122 требует оклад той же строки, договора и месяца")
def _c17():
    e, a, b = person(), contract(), contract('C_B')
    base = year_salary('E001', 'C_A', 160000.0)
    base[5] = ('E001', 'C_A', 6, PaymentKind.SALARY, 150000.0)
    good = base + [('E001', 'C_A', 6, PaymentKind.K122, 10000.0)]
    bad = expect_ok(audit(*full_plan([e], [a, b], good)))
    wrong_contract = base + [('E001', 'C_B', 6, PaymentKind.K122, 10000.0)]
    bad += expect_fail(audit(*full_plan([e], [a, b], wrong_contract)), 'PAYMENT-001',
                       ('122', 'C_B', 'июнь', 'оклад'))
    # При совпадающем физическом лице оклад другой строки также не подходит.
    other = person('E002', e.full_name)
    e.person_id = other.person_id = 'T-001'
    wrong_row = year_salary('E001', 'C_A', 160000.0) + year_salary('E002', 'C_B', 160000.0)
    wrong_row[-7] = ('E002', 'C_B', 6, PaymentKind.SALARY, 150000.0)
    wrong_row.append(('E002', 'C_A', 6, PaymentKind.K122, 10000.0))
    bad += expect_fail(audit(*full_plan([e, other], [a, b], wrong_row)), 'PAYMENT-001',
                       ('122', 'E002', 'оклад'))
    return bad


@case("а18", "PAYMENT-001: ручные назначения, запреты и списки договоров не теряются")
def _c18():
    from fot_planner.models import ManualAssignment, ManualProhibition
    e, c = person(), contract()
    ctx, result = full_plan([e], [c], year_salary('E001', 'C_A', 160000.0))
    ctx.manual_assignments = [ManualAssignment('E001', 'C_A', YEAR, 6, 6,
                                               PaymentKind.SALARY, 160000)]
    bad = expect_ok(audit(ctx, result))
    ctx.manual_assignments[0].fixed_amount = 150000
    bad += expect_fail(audit(ctx, result), 'PAYMENT-001', ('ручное', 'июнь'))
    ctx.manual_assignments = []
    ctx.manual_prohibitions = [ManualProhibition('E001', 'C_A', YEAR, 6, 6, PaymentKind.SALARY)]
    bad += expect_fail(audit(ctx, result), 'PAYMENT-001', ('запрет', 'июнь'))
    ctx.manual_prohibitions[0].year = YEAR - 1
    bad += expect_ok(audit(ctx, result))
    ctx.manual_prohibitions = []
    e.allowed_contracts = ['C_B']
    bad += expect_fail(audit(ctx, result), 'PAYMENT-001', ('разреш',))
    e.allowed_contracts = []
    e.forbidden_contracts = ['C_A']
    bad += expect_fail(audit(ctx, result), 'PAYMENT-001', ('запрет',))
    e.forbidden_contracts = []
    ctx.manual_assignments = [ManualAssignment('E001', 'C_A', YEAR, 6, 6, PaymentKind.K122)]
    bad += expect_fail(audit(ctx, result), 'PAYMENT-001', ('ручное', 'июнь'))
    ctx.manual_assignments[0].fixed_amount = 0
    report = audit(ctx, result)
    if not any(check.get('coverage_gaps') for check in report.checks):
        bad.append('непроверенная нулевая фиксация не отмечена пробелом')
    return bad


@case("а19", "SALARY-001: дефицит не закрывает зарплату без разрешения экономиста")
def _c19():
    e, c = person(), contract()
    pays = year_salary('E001', 'C_A', 160000.0)
    pays[5] = ('E001', 'C_A', 6, PaymentKind.SALARY, 150000.0)
    ctx, result = full_plan([e], [c], pays, deficits=[('E001', 6, 160000, 10000)])
    ctx.allow_deficit = False
    bad = expect_fail(audit(ctx, result), 'SALARY-001', ('дефицит', 'июнь', 'не разреш'))
    ctx.allow_deficit = True
    bad += expect_ok(audit(ctx, result))
    return bad


@case("а20", "PAYMENT-001: 152 исключает 120/122/124 в строке и месяце, но не у другой строки человека")
def _c20():
    e, other = person(), person('E002')
    e.person_id = other.person_id = 'T-001'
    c = contract(fot=5_000_000, allow_secret=True, allow_incentive=True, allow_extra_work=True)
    pays = year_salary('E001', 'C_A', 160000.0)
    pays[5] = ('E001', 'C_A', 6, PaymentKind.SALARY, 145000.0)
    pays.append(('E001', 'C_A', 6, PaymentKind.K152, 15000.0))
    bad = expect_ok(audit(*full_plan([e], [c], pays)))
    for kind in (PaymentKind.K120, PaymentKind.K122, PaymentKind.K124):
        mixed = pays[:-1] + [('E001', 'C_A', 6, PaymentKind.K152, 5000.0),
                            ('E001', 'C_A', 6, kind, 10000.0)]
        report = audit(*full_plan([e], [c], mixed))
        bad += expect_fail(report, 'PAYMENT-001', ('152', str(kind.value), 'июнь'))
        if not any(m['metric'] == 'k152_excluded_amount' and not m['passed']
                   and m['actual'] == 10000 and m['upper'] == .01
                   and m['coordinates'] == dict(employee_id='E001', month=6, payment_kind=kind.name)
                   for check in report.checks for m in (check.get('measurements') or [])):
            bad.append('нет измерения конфликта с 152')
    # Другой договор не отменяет запрет в одной строке.
    c2 = contract('C_B', allow_incentive=True)
    mixed = pays[:-1] + [('E001', 'C_A', 6, PaymentKind.K152, 5000),
                        ('E001', 'C_B', 6, PaymentKind.K124, 10000)]
    bad += expect_fail(audit(*full_plan([e], [c, c2], mixed)), 'PAYMENT-001', ('152', '124'))
    # Другая строка того же человека — независимая выплата.
    second = year_salary('E002', 'C_A', 160000.0)
    second[5] = ('E002', 'C_A', 6, PaymentKind.SALARY, 150000.0)
    second.append(('E002', 'C_A', 6, PaymentKind.K124, 10000.0))
    bad += expect_ok(audit(*full_plan([e, other], [c], pays + second)))
    # Та же строка, но следующий месяц — тоже допустимо.
    later = list(pays)
    later[6] = ('E001', 'C_A', 7, PaymentKind.SALARY, 150000.0)
    later.append(('E001', 'C_A', 7, PaymentKind.K124, 10000.0))
    bad += expect_ok(audit(*full_plan([e], [c], later)))
    # Порог применяется к сумме вида: дробление на мелкие записи его не обходит.
    tiny = pays + [('E001', 'C_A', 6, PaymentKind.K124, .01)]
    bad += expect_ok(audit(*full_plan([e], [c], tiny)))
    split = pays + [('E001', 'C_A', 6, PaymentKind.K124, .006),
                    ('E001', 'C_B', 6, PaymentKind.K124, .006)]
    bad += expect_fail(audit(*full_plan([e], [c, c2], split)), 'PAYMENT-001', ('152', '124'))
    return bad


@case("а21", "PAYMENT-001: приоритет — отдельные режимы, оклад другого договора исключает штатные")
def _c21():
    e, other = person(), person('E002')
    e.person_id = other.person_id = 'T-001'
    c = contract(fot=5_000_000, priority_payment_mode=True,
                 allow_extra_work=True, allow_secret=True, allow_incentive=True)
    ordinary = contract('C_B', fot=5_000_000)
    pays = year_salary('E001', 'C_A', 160000.0)
    bad = expect_ok(audit(*full_plan([e], [c], pays)))
    mixed = list(pays)
    mixed[5] = ('E001', 'C_A', 6, PaymentKind.SALARY, 145000.0)
    mixed.append(('E001', 'C_A', 6, PaymentKind.K152, 15000.0))
    report = audit(*full_plan([e], [c], mixed))
    bad += expect_fail(report, 'PAYMENT-001', ('приоритет', '152', 'июнь', 'C_A'))
    if not any(m['metric'] == 'priority_staff_with_152' and not m['passed']
               and m['actual'] == 145000 and m['upper'] == .01
               and m['coordinates'] == dict(employee_id='E001', contract_id='C_A',
                                            month=6, payment_kind='SALARY')
               for check in report.checks for m in (check.get('measurements') or [])):
        bad.append('нет измерения штатных выплат при 152')
    c.priority_payment_mode = False
    bad += expect_ok(audit(*full_plan([e], [c], mixed)))
    c.priority_payment_mode = True
    mixed[5] = ('E001', 'C_B', 6, PaymentKind.SALARY, 145000.0)
    bad += expect_ok(audit(*full_plan([e], [c, ordinary], mixed)))
    for kind in (PaymentKind.SALARY, PaymentKind.K120, PaymentKind.K122, PaymentKind.K124):
        staff = list(pays)
        staff[5] = ('E001', 'C_B', 6, PaymentKind.SALARY, 150000.0)
        staff.extend([('E001', 'C_A', 6, PaymentKind.SALARY, 5000.0),
                      ('E001', 'C_A', 6, kind, 5000.0)])
        report = audit(*full_plan([e], [c, ordinary], staff))
        bad += expect_fail(report, 'PAYMENT-001', ('приоритет', 'другом договоре', 'июнь'))
        if not any(m['metric'] == 'priority_staff_with_external_salary' and not m['passed']
                   and m['coordinates']['payment_kind'] == kind.name
                   for check in report.checks for m in (check.get('measurements') or [])):
            bad.append('нет измерения запрета вида ' + kind.name)
    # Другой месяц/строка с тем же табельным номером не запрещают штатный режим.
    shifted = list(pays)
    shifted[5] = ('E001', 'C_B', 6, PaymentKind.SALARY, 160000.0)
    bad += expect_ok(audit(*full_plan([e], [c, ordinary], shifted)))
    bad += expect_ok(audit(*full_plan([e, other], [c, ordinary],
                                     pays + year_salary('E002', 'C_B', 160000.0))))
    # Дробление на записи меньше порога не скрывает совместное использование.
    split = [('E001', 'C_A', 6, PaymentKind.K152, 160000.0),
             ('E001', 'C_A', 6, PaymentKind.SALARY, .006),
             ('E001', 'C_A', 6, PaymentKind.SALARY, .006)]
    report = audit(*full_plan([e], [c], pays[:5] + pays[6:] + split))
    bad += expect_fail(report, 'PAYMENT-001', ('приоритет', '152'))
    return bad


@case("а22", "PERSON-001: получателю оклада нужно ровно одно основное место по табельному номеру")
def _c22():
    e, other = person(), person('E002', rate=.5)
    e.person_id = other.person_id = 'T-001'
    for employee in (e, other):
        employee.start_date, employee.end_date = date(YEAR, 6, 1), date(YEAR, 6, 30)
    c = contract(fot=5_000_000)
    pays = [('E001', 'C_A', 6, PaymentKind.SALARY, 160000),
            ('E002', 'C_A', 6, PaymentKind.SALARY, 160000)]
    main = ('E001', 'C_A', 6, 1.0, True)
    part = ('E002', 'C_A', 6, .5, False)
    bad = expect_ok(audit(*full_plan([e, other], [c], pays, rates=[main, part])))
    variants = [([main, (*part[:4], True)], 2),
                ([(*main[:4], False), part], 0),
                ([], 0), ([('E001', 'C_A', 6, 0, True), part], 0),
                ([('E001', 'C_A', 7, 1, True), part], 0)]
    for rates, expected_count in variants:
        report = audit(*full_plan([e, other], [c], pays, rates=rates))
        bad += expect_fail(report, 'PERSON-001', ('t-001', 'июнь', 'основных мест'))
        if not any(m['metric'] == 'person_main_count' and not m['passed']
                   and m['actual'] == expected_count and m['lower'] == m['upper'] == 1
                   and m['coordinates'] == dict(person_id='t-001', month=6)
                   for check in report.checks for m in (check.get('measurements') or [])):
            bad.append('нет измерения числа основных мест')
    other.person_id = 'T-002'
    bad += expect_ok(audit(*full_plan([e, other], [c], pays,
                                     rates=[main, (*part[:4], True)])))
    return bad


@case("а23", "PERSON-001: по строке не больше 0,5 совместительства; у человека не больше двух")
def _c23():
    employees = [person('E' + str(i), rate=.25) for i in range(4)]
    for e in employees:
        e.monthly_wage = 0
        e.person_id = 'T-001'
        e.employment_type = 'main' if e.id == 'E0' else 'part_time'
    c, c2 = contract(), contract('C_B')
    rates = [('E0', 'C_A', 6, .5, True), ('E1', 'C_A', 6, .25, False),
             ('E1', 'C_B', 6, .25, False)]
    bad = expect_ok(audit(*full_plan(employees[:2], [c, c2], [], rates=rates)))
    excess = rates[:-1] + [('E1', 'C_B', 6, .5, False)]
    report = audit(*full_plan(employees[:2], [c, c2], [], rates=excess))
    bad += expect_fail(report, 'PERSON-001', ('E1', 'июнь', 'совместительство'))
    if not any(m['metric'] == 'row_part_time_rate' and m['actual'] == .75 and not m['passed']
               for check in report.checks for m in (check.get('measurements') or [])):
        bad.append('нет суммарного измерения совместительства строки')
    # Метка основного места не освобождает входную строку part_time от предела.
    bad += expect_fail(audit(*full_plan(employees[:2], [c], [],
        rates=[('E1', 'C_A', 6, .75, True)])), 'PERSON-001', ('совместительство',))
    # Неосновные ставки обычной строки тоже суммируются через договоры.
    employees[1].employment_type = 'auto'
    bad += expect_fail(audit(*full_plan(employees[:2], [c, c2], [], rates=excess)),
                       'PERSON-001', ('совместительство',))
    employees[1].employment_type = 'part_time'
    report = audit(*full_plan(employees, [c], [], rates=()))
    bad += expect_fail(report, 'PERSON-001', ('t-001', 'совместительств 3'))
    if not any(m['metric'] == 'person_part_time_rows' and m['actual'] == 3 and not m['passed']
               for check in report.checks for m in (check.get('measurements') or [])):
        bad.append('нет измерения числа совместительств')
    employees[3].person_id = 'T-002'
    bad += expect_ok(audit(*full_plan(employees, [c], [], rates=())))
    employees[3].person_id = 'T-001'
    employees[2].end_date = date(YEAR, 6, 30)
    employees[3].start_date = date(YEAR, 7, 1)
    bad += expect_ok(audit(*full_plan(employees, [c], [], rates=())))
    return bad


@case("а24", "PERSON-001: предел аспиранта по человеку, студент без добавления к входной ставке")
def _c24():
    e, other = person(), person('E002')
    e.monthly_wage = other.monthly_wage = 0
    e.person_id = other.person_id = 'T-001'
    other.employment_category = 'graduate_student'
    c = contract()
    rates = [('E001', 'C_A', 6, .5, True), ('E002', 'C_A', 6, .25, True)]
    bad = expect_ok(audit(*full_plan([e, other], [c], [], rates=rates)))
    report = audit(*full_plan([e, other], [c], [], rates=[('E001', 'C_A', 6, 1, True)]))
    bad += expect_fail(report, 'PERSON-001', ('t-001', 'июнь', '0,75'))
    if not any(m['metric'] == 'person_total_rate' and m['upper'] == .75 and not m['passed']
               for check in report.checks for m in (check.get('measurements') or [])):
        bad.append('нет измерения предела категории')
    other.person_id = 'T-002'
    bad += expect_ok(audit(*full_plan([e, other], [c], [], rates=rates)))
    other.employment_category = 'student'
    other.rate = 1.0
    bad += expect_ok(audit(*full_plan([other], [c], [],
                                     rates=[('E002', 'C_A', 6, 1.0, True)])))
    report = audit(*full_plan([other], [c], [],
                             rates=[('E002', 'C_A', 6, 1.0, True),
                                    ('E002', 'C_B', 6, .25, False)]))
    bad += expect_fail(report, 'PERSON-001', ('E002', 'студент', 'входной ставки'))
    if not any(m['metric'] == 'student_row_rate' and m['upper'] == 1 and not m['passed']
               for check in report.checks for m in (check.get('measurements') or [])):
        bad.append('нет измерения ограничения студента входной ставкой')
    return bad


@case("а25", "SALARY-001: зарплата с апреля своя — сверка с суммой месяца; прежняя после изменения и новая раньше срока не проходят")
def _c25():
    # Лист «зарплата_по_месяцам» (правило 1, решение пользователя 19.09.2026):
    # с апреля 180 000, январь–март без своей суммы — 160 000 штатного расписания.
    e, c = person(), contract(fot=3_000_000.0)
    e.monthly_wages = {m: 180000.0 for m in range(4, 13)}
    good = [('E001', 'C_A', m, PaymentKind.SALARY, 160000.0 if m < 4 else 180000.0)
            for m in range(1, 13)]
    bad = expect_ok(audit(*full_plan([e], [c], good)))
    # После изменения выплачена прежняя сумма — нарушение в каждом месяце с
    # апреля, и только в них.
    report = audit(*full_plan([e], [c], year_salary('E001', 'C_A', 160000.0)))
    bad += expect_fail(report, 'SALARY-001', ('Иванов Иван Иванович', 'апрель', '160 000', '180 000'))
    months = sorted(v.month for v in report.violations if v.rule_id == 'SALARY-001')
    if months != list(range(4, 13)):
        bad.append('нарушения SALARY-001 в месяцах %s, ждали апрель–декабрь' % months)
    if not any(m['metric'] == 'allocated_plus_deficit' and not m['passed']
               and m['actual'] == 160000 and m['lower'] == m['upper'] == 180000
               and m['coordinates'] == dict(employee_id='E001', month=4)
               for check in report.checks for m in (check.get('measurements') or [])):
        bad.append('нет измерения апреля с суммой месяца 180 000')
    # Новая сумма раньше срока — тоже нарушение: в марте положено 160 000.
    report = audit(*full_plan([e], [c], year_salary('E001', 'C_A', 180000.0)))
    bad += expect_fail(report, 'SALARY-001', ('март', '180 000', '160 000'))
    months = sorted(v.month for v in report.violations if v.rule_id == 'SALARY-001')
    if months != [1, 2, 3]:
        bad.append('новая сумма раньше срока: нарушения в месяцах %s, ждали январь–март' % months)
    # Своя нулевая сумма месяца — тоже сумма: выплата в этом месяце лишняя.
    e.monthly_wages[5] = 0.0
    report = audit(*full_plan([e], [c], good))
    bad += expect_fail(report, 'SALARY-001', ('май', '180 000'))
    return bad


@case("а26", "PAYMENT-001: закрепление без вида — годится выплата с договора любого вида; месяц без неё — нарушение")
def _c26():
    from fot_planner.models import ManualAssignment
    # MANUAL-ANY-001 (решение пользователя 19.09.2026): вид у закрепления не
    # задан — в каждом месяце срока нужна хоть одна выплата с договора, а
    # какая, решает расчёт. Здесь в марте 124, в апреле оклад, в мае приказ.
    e, a = person(), contract()
    b = contract('C_B', allow_incentive=True, allow_order_incentive=True)
    pays = year_salary('E001', 'C_A', 160000.0)
    for month, kind in ((3, PaymentKind.K124), (4, PaymentKind.SALARY),
                        (5, PaymentKind.ORDER_INCENTIVE)):
        pays[month - 1] = ('E001', 'C_A', month, PaymentKind.SALARY, 150000.0)
        pays.append(('E001', 'C_B', month, kind, 10000.0))
    ctx, result = full_plan([e], [a, b], pays)
    ctx.manual_assignments = [ManualAssignment('E001', 'C_B', YEAR, 3, 5, None)]
    bad = expect_ok(audit(ctx, result))
    # Май без выплаты с C_B: зарплата целиком с C_A, закрепление не выполнено
    # ровно в мае, координата вида — «любой».
    no_may = [p for p in pays if not (p[1] == 'C_B' and p[2] == 5)]
    no_may[4] = ('E001', 'C_A', 5, PaymentKind.SALARY, 160000.0)
    ctx, result = full_plan([e], [a, b], no_may)
    ctx.manual_assignments = [ManualAssignment('E001', 'C_B', YEAR, 3, 5, None)]
    report = audit(ctx, result)
    bad += expect_fail(report, 'PAYMENT-001', ('Иванов Иван Иванович', 'май', 'C_B',
                                               'ручное назначение без вида'))
    months = sorted(v.month for v in report.violations if v.rule_id == 'PAYMENT-001')
    if months != [5]:
        bad.append('нарушения PAYMENT-001 в месяцах %s, ждали только май' % months)
    if not any(m['metric'] == 'manual_assignment_present' and not m['passed'] and m['actual'] == 0
               and m['coordinates'] == dict(employee_id='E001', contract_id='C_B', month=5,
                                            payment_kind='любой')
               for check in report.checks for m in (check.get('measurements') or [])):
        bad.append('нет измерения мая с видом «любой»')
    # Сумма у закрепления без вида не сверяется с догадкой о виде: пробел, а
    # присутствие выплаты проверяется по-прежнему.
    ctx.manual_assignments[0].fixed_amount = 10000.0
    report = audit(ctx, result)
    bad += expect_fail(report, 'PAYMENT-001', ('май', 'без вида'))
    if not any(g.get('payment_kind') == 'любой' for check in report.checks
               for g in (check.get('coverage_gaps') or [])):
        bad.append('сумма у закрепления без вида не отмечена пробелом')
    return bad


# ── Пересчёт с факта (правило 26, владелец продукта 30.09.2026) ──────────
# «не трогая январь и февраль, пересчитал… просто нельзя менять их».


def with_facts(ctx, last, pays):
    """Вход с листом «факт_выплат»: месяцы 1…last — эти выплаты как факт."""
    from fot_planner.models import ActualPayment
    ctx.frozen_through = last
    ctx.actuals = [ActualPayment(e, c, m, k, v, rate=1.0 if k == PaymentKind.SALARY else None)
                   for e, c, m, k, v in pays if m <= last]
    return ctx


@case("а27", "PAYMENT-001: месяцы факта — ровно лист «факт_выплат»; изменённая, потерянная или добавленная выплата прошедшего месяца — нарушение")
def _c27():
    e, a, b = person(), contract(), contract('C_B')
    pays = [('E001', 'C_B' if m <= 2 else 'C_A', m, PaymentKind.SALARY, 160000.0)
            for m in range(1, 13)]

    def plan(result_pays):
        ctx, res = full_plan([e], [a, b], result_pays)
        return with_facts(ctx, 2, pays), res

    bad = expect_ok(audit(*plan(pays)))
    changed = [('E001', 'C_B', 1, PaymentKind.SALARY, 150000.0)] + pays[1:]
    report = audit(*plan(changed))
    bad += expect_fail(report, 'PAYMENT-001', ('январь', 'месяц факта', 'C_B', '160 000', '150 000'))
    if not any(m['metric'] == 'frozen_fact_amount' and not m['passed'] and m['actual'] == 150000
               and m['lower'] == m['upper'] == 160000
               and m['coordinates'] == dict(employee_id='E001', contract_id='C_B', month=1,
                                            payment_kind='SALARY')
               for check in report.checks for m in (check.get('measurements') or [])):
        bad.append('нет измерения изменённого факта')
    bad += expect_fail(audit(*plan(pays + [('E001', 'C_A', 2, PaymentKind.K122, 5000.0)])),
                       'PAYMENT-001', ('февраль', 'добавлена'))
    bad += expect_fail(audit(*plan(pays[1:])), 'PAYMENT-001', ('январь', 'потеряна'))
    return bad


@case("а28", "Месяцы факта не делают план негодным: ставка 0,3, недоплата, 122 без оклада — чисто; ФОТ, превышенный фактом, — предупреждение, перерасход после факта — нарушение")
def _c28():
    e, a = person(), contract()
    history = [('E001', 'C_A', 1, PaymentKind.SALARY, 48000.0),      # 0,3 ставки
               ('E001', 'C_A', 1, PaymentKind.K122, 500.0),          # и недоплата
               ('E001', 'C_A', 2, PaymentKind.K122, 160000.0)]       # 122 без оклада
    rates = [('E001', 'C_A', 1, .3, True)] + [('E001', 'C_A', m, 1.0, True) for m in range(3, 13)]
    ctx, res = full_plan([e], [a], history + year_salary('E001', 'C_A', 160000.0)[2:], rates=rates)
    bad = expect_ok(audit(with_facts(ctx, 2, history), res))
    # Те же числа без факта — план негоден: нарушения месяцев не спрятаны.
    ctx.frozen_through, ctx.actuals = 0, []
    report = audit(ctx, res)
    found = {v.rule_id for v in report.violations if v.severity == 'error'}
    if not {'RATE-001', 'SALARY-001', 'PAYMENT-001'} <= found:
        bad.append('без факта те же числа прошли: %s' % sorted(found))
    # ФОТ 300 000, факт — 320 000; человек работал по февраль.
    e.end_date = date(YEAR, 2, 28)
    c = contract(fot=300_000.0)
    history = [('E001', 'C_A', m, PaymentKind.SALARY, 160000.0) for m in (1, 2)]
    ctx, res = full_plan([e], [c], history)
    report = audit(with_facts(ctx, 2, history), res)
    bad += expect_ok(report)
    if not any(v.rule_id == 'FOT-001' and v.severity == 'warning' and 'факта' in v.message
               for v in report.violations):
        bad.append('перерасход ФОТ фактом не назван предупреждением')
    later = history + [('E001', 'C_A', 3, PaymentKind.SALARY, 1000.0)]
    ctx, res = full_plan([e], [c], later)
    bad += expect_fail(audit(with_facts(ctx, 2, later), res), 'FOT-001', ('C_A', '321 000'))
    return bad


@case("а29", "LABOR-001: год строки РКМ считает факт — перебор истории предупреждение; коридор месяца факта не проверяется, месяцы после него — как всегда")
def _c29():
    e, c = person(), contract()
    pays = year_salary('E001', 'C_A', 160000.0)
    lp = ContractLaborPlan(contract_id='C_A', year=YEAR, person_months=1.5,
                           position='Инженер', avg_monthly_labor_cost=100000.0)
    ctx, res = full_plan([e], [c], pays, labor=[lp],
                         pm=[(lp, 'E001', m, 1.0) for m in (1, 2)],
                         money=[(lp, 'E001', m, 100000.0) for m in (1, 2)])
    ctx.salary_stability.goz_labor_tolerance = .05
    report = audit(with_facts(ctx, 2, pays), res)
    bad = expect_ok(report)
    if not any(v.rule_id == 'LABOR-001' and v.severity == 'warning' for v in report.violations):
        bad.append('перебор строки РКМ фактом не назван предупреждением')
    ctx.frozen_through, ctx.actuals = 0, []
    bad += expect_fail(audit(ctx, res), 'LABOR-001', ('C_A/Инженер', '2 чел.-мес.'))
    # План по месяцам январь–март: история закрыла по 0,5, март — ровно.
    monthly = ContractLaborPlan(contract_id='C_A', year=YEAR, person_months=3,
                                position='Инженер', monthly={1: 1, 2: 1, 3: 1})
    rows = {1: .5, 2: .5, 3: 1.0}
    ctx, res = full_plan([e], [c], pays, labor=[monthly],
                         pm=[(monthly, 'E001', m, v) for m, v in rows.items()])
    ctx.salary_stability.goz_labor_tolerance = .05
    bad += expect_ok(audit(with_facts(ctx, 2, pays), res))
    rows[3] = .5                                          # недобор марта — уже план
    ctx, res = full_plan([e], [c], pays, labor=[monthly],
                         pm=[(monthly, 'E001', m, v) for m, v in rows.items()])
    ctx.salary_stability.goz_labor_tolerance = .05
    bad += expect_fail(audit(with_facts(ctx, 2, pays), res), 'LABOR-001', ('март',))
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="номера случаев через запятую")
    args = ap.parse_args()
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    rows, failed = [], 0
    for num, title, fn in CASES:
        if only and num not in only:
            continue
        t0 = time.time()
        try:
            bad = fn() or []
        except Exception as exc:  # noqa: BLE001 — стенд не должен падать целиком
            bad = ["ошибка стенда: %s: %s" % (type(exc).__name__, str(exc)[:200])]
        rows.append((num, title, bad))
        failed += 1 if bad else 0
        print("[%s] %-9s %4.1f с  %s" % (num, "ПРОВАЛ" if bad else "ОК",
                                         time.time() - t0, title))
        for b in bad[:6]:
            print("      ✗", b)
        sys.stdout.flush()
    print("\nитого: %d случаев, провалов %d" % (len(rows), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
