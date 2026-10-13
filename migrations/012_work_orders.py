# -*- coding: utf-8 -*-
"""Приказы о рабочих группах (решение пользователя 28.09.2026): приказ и его
состав — строки штатного расписания с видом работ. Это новые таблицы, старые
таблицы и графы не меняются.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from __init__ import columns  # noqa: E402

VERSION = 12
TITLE = "приказы о рабочих группах и их состав"
# Новых граф в старых таблицах нет — только новые таблицы (см. шаг 010).
COLUMNS = {}

TABLES = {
    "work_orders": (
        "CREATE TABLE work_orders ("
        "id INTEGER PRIMARY KEY, kind VARCHAR(20) NOT NULL, number VARCHAR(60), "
        "date VARCHAR(20), base_number VARCHAR(60), base_date VARCHAR(20), "
        "contract_code VARCHAR(60), agreement VARCHAR(120), agreement_date VARCHAR(20), "
        "topic VARCHAR(500), account VARCHAR(60), kfo VARCHAR(20), date_from VARCHAR(20), "
        "date_to VARCHAR(20), leader VARCHAR(200), source VARCHAR(300), "
        "document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE)"),
    "work_order_members": (
        "CREATE TABLE work_order_members ("
        "id INTEGER PRIMARY KEY, "
        "order_id INTEGER NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE, "
        "n INTEGER, action VARCHAR(20) NOT NULL, fio VARCHAR(200), person_code VARCHAR(60), "
        "position VARCHAR(200), department VARCHAR(300), work TEXT, leader VARCHAR(10), "
        "employee_code VARCHAR(60))"),
}


def plan(conn):
    return [ddl for name, ddl in TABLES.items() if not columns(conn, name)]


def apply(conn):
    for name, ddl in TABLES.items():
        if not columns(conn, name):
            conn.execute(ddl)
