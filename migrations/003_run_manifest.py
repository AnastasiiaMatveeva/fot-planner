# -*- coding: utf-8 -*-
"""L1.3: у прогона появляется ссылка на снимок (RunManifest) в хранилище
артефактов — хеш канонического JSON со входом, справочником, настройками,
источниками, версией кода решателя и результатом (VER-001, VER-002).
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from __init__ import add_missing  # noqa: E402

VERSION = 3
TITLE = "прогон: ссылка на снимок прогона в хранилище артефактов"

COLUMNS = {
    "runs": [("manifest_sha256", "VARCHAR(64)")],
}


def plan(conn):
    out = []
    for table, cols in COLUMNS.items():
        out += add_missing(conn, table, cols, plan_only=True)
    return out


def apply(conn):
    for table, cols in COLUMNS.items():
        add_missing(conn, table, cols)
