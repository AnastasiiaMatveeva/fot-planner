# -*- coding: utf-8 -*-
"""Лицевой счёт сотрудника (решение пользователя 28.09.2026): в штатном
расписании он уже проставлен, и по нему видно, с какого договора человеку
платят сейчас. Графа справочная — в расчёт не уходит, поэтому ни таблиц, ни
связей не добавляется: одна графа в строке штатного расписания.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from __init__ import add_missing  # noqa: E402

VERSION = 11
TITLE = "лицевой счёт в строке штатного расписания"
COLUMNS = {
    "employees": [("account", "VARCHAR(60)")],
}


def plan(conn):
    out = []
    for table, cols in COLUMNS.items():
        out += add_missing(conn, table, cols, plan_only=True)
    return out


def apply(conn):
    for table, cols in COLUMNS.items():
        add_missing(conn, table, cols)
