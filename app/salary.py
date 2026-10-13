# -*- coding: utf-8 -*-
"""Зарплата строк штатного расписания по месяцам (решение пользователя 19.09.2026).

Общее для вкладки реестра «Заработная плата по месяцам» и чата: где лежит
сумма месяца, как правка попадает в журнал решений и какие планы
помечаются к пересмотру. Зарплаты в штатном расписании нет — только здесь.
"""
from __future__ import annotations

import actuality
import build_input
from db import Case, SalaryMonth, SalaryRow, record_decision

MANUAL = "введено вручную"


def changed(db, code, year, action, grounds, reason):
    """Журнал решений и пересмотр планов этого года после правки зарплаты.

    Зарплата общая для организации и уходит в расчёт каждого плана своего
    года, поэтому к пересмотру помечаются посчитанные планы этого года.
    reason пуст — расчёт от правки не меняется (строка без сумм)."""
    from fot_planner.harness_local import decisions as D
    record_decision(db, kind="данные", subject_kind="зарплата по месяцам",
                    subject_id="%s|%d" % (code, year),
                    subject_digest=D.digest(code, action), scope="реестр",
                    action=action, grounds=grounds)
    if reason:
        years = {c.id: c.year for c in db.query(Case).all()}
        actuality.mark_review(db, [r for r in actuality.results(db)
                                   if years.get(r.case_id) == year], reason)


def main_row(db, code, year, source=MANUAL):
    """Первая строка кода в году — сюда ложатся суммы без названной строки
    (чат, прежние вызовы). Строки нет — заводится."""
    row = (db.query(SalaryRow).filter_by(employee_code=code, year=year)
           .order_by(SalaryRow.id).first())
    if row is None:
        row = SalaryRow(employee_code=code, year=year, source=source)
        db.add(row)
        db.flush()
    return row


def set_month(db, code, year, month, amount, source=MANUAL, row_id=None):
    """Сумма строки в месяце; None снимает её. row_id — строка таблицы
    зарплаты (у человека в двух управленческих подразделениях их две,
    01.10.2026); без неё — первая строка кода, нет строки — заводится.

    Правка суммы из документа делает её ручной: следующий файл зарплаты не
    перепишет то, что экономист исправил. Возвращает прежнюю сумму."""
    row = db.get(SalaryRow, row_id) if row_id else None
    if row is None or row.employee_code != code or row.year != year:
        row = main_row(db, code, year, source)
    cell = db.query(SalaryMonth).filter_by(salary_row_id=row.id, month=month).first()
    was = cell.amount if cell is not None else None
    if amount is None:
        if cell is not None:
            db.delete(cell)
    elif cell is None:
        db.add(SalaryMonth(employee_code=code, year=year, month=month, amount=amount,
                           source=source, salary_row_id=row.id))
    else:
        cell.amount, cell.source, cell.document_id = amount, source, None
    return was


def month_totals(db, year, codes=None):
    """{(код, месяц): сумма} — зарплата месяца: сумма строк человека."""
    q = db.query(SalaryMonth.employee_code, SalaryMonth.month, SalaryMonth.amount).filter(
        SalaryMonth.year == year)
    if codes is not None:
        q = q.filter(SalaryMonth.employee_code.in_(list(codes)))
    out = {}
    for code, month, amount in q.all():
        out[(code, month)] = round(out.get((code, month), 0.0) + (amount or 0.0), 2)
    return out


def fill_term(db, employee, year, amount, grounds):
    """Одна сумма во все месяцы срока строки в году — «у Петрова оклад 90 000»
    в чате (решение пользователя 19.09.2026: «заполни даты согласно датам
    его работы»). Возвращает месяцы срока; пусто — в этом году строка не
    работает, и ничего не записано."""
    months = build_input.active_months(employee.date_from, employee.date_to, year)
    was = {m: set_month(db, employee.code, year, m, amount) for m in months}
    if months:
        changed(db, employee.code, year,
                {"строка": employee.code, "год": year, "месяцы": months, "сумма": amount,
                 "было": was}, grounds,
                "изменена зарплата строки %s на %d год" % (employee.code, year))
    return months
