# -*- coding: utf-8 -*-
"""Надбавка 120 (правило 10, дополнение 18.09.2026): у строки своё окно
выплаты и закреплённый договор выплаты. Старые строки остаются с пустыми
графами — окно договора секретности и выбор оптимизатора, как раньше.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from __init__ import add_missing  # noqa: E402

VERSION = 9
TITLE = "надбавка 120: окно строки и договор выплаты"

COLUMNS = {
    "secret_allowances": [("date_from", "VARCHAR(20)"), ("date_to", "VARCHAR(20)"),
                          ("pay_contract_code", "VARCHAR(60)")],
}


def plan(conn):
    out = []
    for table, cols in COLUMNS.items():
        out += add_missing(conn, table, cols, plan_only=True)
    return out


def apply(conn):
    for table, cols in COLUMNS.items():
        add_missing(conn, table, cols)
