# -*- coding: utf-8 -*-
"""L1.5: актуальность результата (review/review_log, VER-003), связь попыток
повтора (retry_of, RUN-002) и таблица операций с ключом (RUN-001).
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from __init__ import add_missing, columns  # noqa: E402

VERSION = 5
TITLE = "прогон: актуальность и связь попыток; таблица операций с ключом"

COLUMNS = {
    "runs": [("review", "VARCHAR(30)"), ("review_log", "TEXT"), ("retry_of", "INTEGER")],
}

DDL = ("CREATE TABLE operations ("
       "id VARCHAR(80) PRIMARY KEY, kind VARCHAR(40), subject VARCHAR(120), "
       "payload_sha256 VARCHAR(64), state VARCHAR(20), result TEXT, "
       "created DATETIME, finished DATETIME)")


def plan(conn):
    out = []
    for table, cols in COLUMNS.items():
        out += add_missing(conn, table, cols, plan_only=True)
    if not columns(conn, "operations"):
        out.append(DDL)
    return out


def apply(conn):
    for table, cols in COLUMNS.items():
        add_missing(conn, table, cols)
    if not columns(conn, "operations"):
        conn.execute(DDL)
