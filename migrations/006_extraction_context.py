# -*- coding: utf-8 -*-
"""L2.3: предложение помнит снимок контекста извлечения; приговор привязан
к точной редакции документа, предложению и этому снимку, а не к имени
файла (MEM-003, VER-002).
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from __init__ import add_missing  # noqa: E402

VERSION = 6
TITLE = "предложение: снимок контекста; приговор: редакция документа, предложение, снимок"

COLUMNS = {
    "proposals": [("context_sha256", "VARCHAR(64)")],
    "verdicts": [("document_id", "INTEGER"), ("document_sha256", "VARCHAR(64)"),
                 ("proposal_id", "INTEGER"), ("context_sha256", "VARCHAR(64)")],
}


def plan(conn):
    out = []
    for table, cols in COLUMNS.items():
        out += add_missing(conn, table, cols, plan_only=True)
    return out


def apply(conn):
    for table, cols in COLUMNS.items():
        add_missing(conn, table, cols)
