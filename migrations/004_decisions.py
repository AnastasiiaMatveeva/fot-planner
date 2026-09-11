# -*- coding: utf-8 -*-
"""L1.4: таблица локальных решений — кто, что, над какой версией предмета и
на каком основании решил (LocalDecision, VER-004, NFR-002).
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from __init__ import columns  # noqa: E402

VERSION = 4
TITLE = "таблица решений: кто, что, над какой версией предмета решил"
COLUMNS = {}  # новых граф в старых таблицах нет — только новая таблица

DDL = ("CREATE TABLE decisions ("
       "id INTEGER PRIMARY KEY, created DATETIME, actor VARCHAR(120), actor_source VARCHAR(60), "
       "kind VARCHAR(40), subject_kind VARCHAR(40), subject_id VARCHAR(80), subject_digest VARCHAR(64), "
       "scope VARCHAR(120), action TEXT, grounds TEXT, precondition VARCHAR(64), outcome VARCHAR(120))")


def plan(conn):
    return [] if columns(conn, "decisions") else [DDL]


def apply(conn):
    if not columns(conn, "decisions"):
        conn.execute(DDL)
