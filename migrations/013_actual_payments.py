# -*- coding: utf-8 -*-
"""Факт начислений (решение пользователя 30.09.2026: пересчёт по факту —
прошедшие месяцы план берёт как были начислены). Новая таблица, старые
таблицы и графы не меняются.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from __init__ import columns  # noqa: E402

VERSION = 13
TITLE = "факт начислений по месяцам"
# Новых граф в старых таблицах нет — только новая таблица (см. шаг 012).
COLUMNS = {}

TABLES = {
    "actual_payments": (
        "CREATE TABLE actual_payments ("
        "id INTEGER PRIMARY KEY, employee_code VARCHAR(200), person_code VARCHAR(60), "
        "fio VARCHAR(200), contract_code VARCHAR(60), contract_text VARCHAR(300), "
        "year INTEGER NOT NULL, month INTEGER NOT NULL, kind VARCHAR(20) NOT NULL, "
        "amount FLOAT NOT NULL, rate FLOAT, is_main BOOLEAN, matched BOOLEAN, "
        "source VARCHAR(300), "
        "document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE)"),
}


def plan(conn):
    return [ddl for name, ddl in TABLES.items() if not columns(conn, name)]


def apply(conn):
    for name, ddl in TABLES.items():
        if not columns(conn, name):
            conn.execute(ddl)
