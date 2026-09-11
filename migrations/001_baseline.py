# -*- coding: utf-8 -*-
"""Исходная схема: графы, которые приложение добавляло при старте до
появления нумерованных миграций (состояние на 11.09.2026 до L1.1).

Шаг ничего не ломает на базе, где графы уже есть: он лишь убеждается, что
они на месте, и ставит базе версию 1. На старой базе без них — добавляет.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from __init__ import add_missing  # noqa: E402

VERSION = 1
TITLE = "исходная схема: графы, добавлявшиеся при старте приложения"

COLUMNS = {
    "messages": [("document_id", "INTEGER")],
    "cases": [("muted_docs", "TEXT"), ("plan_settings", "TEXT")],
    "documents": [("version", "INTEGER"), ("supersedes_id", "INTEGER"),
                  ("scope", "VARCHAR(20)"), ("sha256", "VARCHAR(64)"), ("gave", "TEXT")],
    "proposals": [("grade", "VARCHAR(20)"), ("reason", "TEXT")],
    "runs": [("sources", "TEXT")],
    "labor_rows": [("headcount", "FLOAT"), ("months", "TEXT"), ("details", "TEXT")],
    "verdicts": [("grade", "VARCHAR(20)")],
    "employees": [("person_code", "VARCHAR(60)"), ("department", "VARCHAR(200)"),
                  ("employment_type", "VARCHAR(40)"), ("employment_category", "VARCHAR(40)"),
                  ("allowed_contracts", "VARCHAR(300)"), ("forbidden_contracts", "VARCHAR(300)")],
    "contracts": [("account", "VARCHAR(60)"), ("department", "VARCHAR(200)"),
                  ("priority", "VARCHAR(60)"), ("allow_main", "VARCHAR(10)"),
                  ("allow_part_time", "VARCHAR(10)"), ("salary_deadline", "VARCHAR(20)"),
                  ("allowance_deadline", "VARCHAR(20)")],
}


def plan(conn):
    out = []
    for table, cols in COLUMNS.items():
        out += add_missing(conn, table, cols, plan_only=True)
    return out


def apply(conn):
    for table, cols in COLUMNS.items():
        add_missing(conn, table, cols)
