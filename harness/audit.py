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

from fot_planner.models import (  # noqa: E402
    Employee, OpenRateAttribution, PlanningResult,
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
