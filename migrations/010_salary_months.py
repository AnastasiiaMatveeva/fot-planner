# -*- coding: utf-8 -*-
"""Зарплата по месяцам (решение пользователя 19.09.2026): у строки штатного
расписания зарплата меняется в течение года. Строки таблицы — сотрудники,
которых принёс документ зарплаты или добавил экономист; суммы — только
месяцы со своей суммой. Старые таблицы и графы не меняются.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from __init__ import columns  # noqa: E402

VERSION = 10
TITLE = "таблицы зарплаты по месяцам: строки и суммы месяцев"
# Новых граф в старых таблицах нет — только новые таблицы. Графы с внешним
# ключом сюда и нельзя: стенд миграций, собирая старую базу, удаляет графы
# шагов, а SQLite не удаляет графу, на которой держится внешний ключ.
COLUMNS = {}

TABLES = {
    "salary_rows": (
        "CREATE TABLE salary_rows ("
        "id INTEGER PRIMARY KEY, employee_code VARCHAR(60) NOT NULL, year INTEGER NOT NULL, "
        "source VARCHAR(300), "
        "document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE)"),
    "salary_months": (
        "CREATE TABLE salary_months ("
        "id INTEGER PRIMARY KEY, employee_code VARCHAR(60) NOT NULL, "
        "year INTEGER NOT NULL, month INTEGER NOT NULL, amount FLOAT NOT NULL, "
        "source VARCHAR(300), "
        "document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE)"),
}


def plan(conn):
    return [ddl for name, ddl in TABLES.items() if not columns(conn, name)]


def apply(conn):
    for name, ddl in TABLES.items():
        if not columns(conn, name):
            conn.execute(ddl)
