# -*- coding: utf-8 -*-
"""Основание плана (SOURCE-003): план выбирает договоры и версии документов;
документ помнит договор, в который загружен, и снимок своих строк на момент
замены новой версией.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from __init__ import add_missing  # noqa: E402

VERSION = 7
TITLE = "план: основание расчёта; документ: договор и снимок строк версии"

COLUMNS = {
    "cases": [("plan_basis", "TEXT")],
    "documents": [("contract_code", "VARCHAR(60)"), ("rows_snapshot", "TEXT")],
}


def plan(conn):
    out = []
    for table, cols in COLUMNS.items():
        out += add_missing(conn, table, cols, plan_only=True)
    return out


def apply(conn):
    for table, cols in COLUMNS.items():
        add_missing(conn, table, cols)
