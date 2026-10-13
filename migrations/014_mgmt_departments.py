# -*- coding: utf-8 -*-
"""Управленческие подразделения (владелец продукта 01.10.2026: «просто хотят
видеть, как у управленческих подразделений распределяются деньги»; вариант
дизайнера «как в 1С» — со справочником).

Справочник подразделений — новая таблица. Строка таблицы зарплаты по
месяцам получает подразделение, и у человека строк может быть несколько —
по одной на подразделение, каждая со своими суммами («дублируешь его просто
как фамилию… и по управленке относишь не только к отделу №1, а ещё к
обязательствам»). Сумма месяца ссылается на свою строку; прежние суммы
привязываются к единственной строке своего кода и года.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from __init__ import add_missing, columns  # noqa: E402

VERSION = 14
TITLE = "управленческие подразделения и строки зарплаты по подразделениям"
COLUMNS = {
    "salary_rows": [("mgmt_department_id", "INTEGER")],
    "salary_months": [("salary_row_id", "INTEGER")],
}
TABLES = {
    "mgmt_departments": (
        "CREATE TABLE mgmt_departments ("
        "id INTEGER PRIMARY KEY, name VARCHAR(200) NOT NULL, position INTEGER, "
        "note VARCHAR(300))"),
}
BACKFILL = ("UPDATE salary_months SET salary_row_id = (SELECT MIN(r.id) FROM salary_rows r "
            "WHERE r.employee_code = salary_months.employee_code "
            "AND r.year = salary_months.year) WHERE salary_row_id IS NULL")


def _needs_backfill(conn):
    """Есть суммы без своей строки, которые можно к ней привязать.

    Раньше заполнение планировалось всегда, когда есть таблица сумм, и на
    новой базе миграция находила «работу» там, где делать нечего.
    """
    have = columns(conn, "salary_months")
    if not have or not columns(conn, "salary_rows"):
        return False
    if "salary_row_id" not in have:
        return conn.execute("SELECT 1 FROM salary_months LIMIT 1").fetchone() is not None
    return conn.execute(
        "SELECT 1 FROM salary_months m WHERE m.salary_row_id IS NULL AND EXISTS ("
        "SELECT 1 FROM salary_rows r WHERE r.employee_code = m.employee_code "
        "AND r.year = m.year) LIMIT 1").fetchone() is not None


def plan(conn):
    out = [ddl for name, ddl in TABLES.items() if not columns(conn, name)]
    for table, cols in COLUMNS.items():
        out += add_missing(conn, table, cols, plan_only=True)
    if _needs_backfill(conn):
        out.append(BACKFILL)
    return out


def apply(conn):
    for name, ddl in TABLES.items():
        if not columns(conn, name):
            conn.execute(ddl)
    for table, cols in COLUMNS.items():
        add_missing(conn, table, cols)
    if columns(conn, "salary_months"):
        conn.execute(BACKFILL)
