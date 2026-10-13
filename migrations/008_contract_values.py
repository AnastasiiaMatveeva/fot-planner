# -*- coding: utf-8 -*-
"""Пакет договора (SOURCE-004): величины из документов договора — ФОТ
периодов, нормативы года, срок — с документом-источником и местом в нём.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from __init__ import columns  # noqa: E402

VERSION = 8
TITLE = "таблица величин пакета договора: ФОТ периодов, нормативы, срок"
COLUMNS = {}  # новых граф в старых таблицах нет — только новая таблица

DDL = ("CREATE TABLE contract_values ("
       "id INTEGER PRIMARY KEY, contract_code VARCHAR(60) NOT NULL, field VARCHAR(80) NOT NULL, "
       "year INTEGER, stage VARCHAR(80), value FLOAT, text VARCHAR(120), unit VARCHAR(20), "
       "label VARCHAR(300), place VARCHAR(200), source VARCHAR(300), "
       "document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE)")


def plan(conn):
    return [] if columns(conn, "contract_values") else [DDL]


def apply(conn):
    if not columns(conn, "contract_values"):
        conn.execute(DDL)
