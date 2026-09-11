# -*- coding: utf-8 -*-
"""L1.1: у прогона появляются исполнитель, heartbeat и ключ операции
(RUN-001, RUN-002). Старые прогоны остаются с пустыми графами: подтвердить,
что они живы, некому, и при старте они считаются брошенными, как и раньше.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from __init__ import add_missing  # noqa: E402

VERSION = 2
TITLE = "прогон: исполнитель, heartbeat, ключ операции"

COLUMNS = {
    "runs": [("executor", "VARCHAR(120)"), ("heartbeat", "DATETIME"),
             ("operation_id", "VARCHAR(80)")],
}


def plan(conn):
    out = []
    for table, cols in COLUMNS.items():
        out += add_missing(conn, table, cols, plan_only=True)
    return out


def apply(conn):
    for table, cols in COLUMNS.items():
        add_missing(conn, table, cols)
