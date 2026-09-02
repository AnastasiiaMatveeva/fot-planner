# -*- coding: utf-8 -*-
"""Справочник должностей: чтение, сверка, запись.

Хранится не в базе, а листом «лимиты_по_должностям» входного файла — того
самого, который читает решатель. Так правка попадает в расчет без
промежуточных копий и без риска, что интерфейс и модель разойдутся.

Должности сопоставляются справочником синонимов самого сервиса: в документах
пишут «Вед. инженер», «Инженер I категории», «МНС», и второй справочник для
этого заводить незачем.
"""
from __future__ import annotations

import os
import sys

import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

TEMPLATE = os.path.join(ROOT, "docs", "ui", "demo_input.xlsx")
SHEET = "лимиты_по_должностям"
COLS = {"оклад": 6, "П2556": 7, "П4": 8}
FIELD_KEY = {"оклад": "sal", "П2556": "p2556", "П4": "p4"}


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(" ", "").replace(" ", "").replace(",", ".")
    s = "".join(c for c in s if c.isdigit() or c in ".-")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def read_rows():
    wb = openpyxl.load_workbook(TEMPLATE)
    ws = wb[SHEET]
    out = []
    for r in range(2, ws.max_row + 1):
        pos = ws.cell(r, 1).value
        if not pos:
            continue
        out.append({"pos": str(pos), "cat": ws.cell(r, 2).value or "",
                    "sal": _num(ws.cell(r, 6).value),
                    "p2556": _num(ws.cell(r, 7).value),
                    "p4": _num(ws.cell(r, 8).value),
                    "note": ws.cell(r, 10).value or ""})
    return out


_INDEX = {"for": None, "map": {}}


def _index(rows):
    names = tuple(r["pos"] for r in rows)
    if _INDEX["for"] == names:
        return _INDEX["map"]
    from fot_planner.position_reference import (
        default_position_synonyms, normalize_position,
    )
    idx = {normalize_position(r["pos"]): r for r in rows}
    for raw, canonical in default_position_synonyms().items():
        target = idx.get(normalize_position(canonical))
        if target is not None:
            idx.setdefault(normalize_position(raw), target)
    _INDEX["for"], _INDEX["map"] = names, idx
    return idx


def compare(rows):
    """Величины из документа против действующего справочника."""
    from fot_planner.position_reference import normalize_position
    current = read_rows()
    idx = _index(current)
    changes, unknown, seen = [], [], set()
    for row in rows or []:
        pos = str(row.get("pos") or "").strip()
        field = row.get("field")
        value = row.get("value")
        key = FIELD_KEY.get(field)
        if not pos or not key or value is None:
            continue
        hit = idx.get(normalize_position(pos))
        if hit is None:
            if pos not in unknown:
                unknown.append(pos)
            continue
        seen.add(field)
        old = hit[key]
        if old != float(value):
            changes.append({"pos": hit["pos"], "field": field,
                            "old": old, "new": float(value)})
    return changes, unknown[:20], sorted(seen)


def apply(edits):
    """Записать величины в лист. edits: [{pos, field, value}]."""
    if not edits:
        return []
    from fot_planner.position_reference import normalize_position
    wb = openpyxl.load_workbook(TEMPLATE)
    ws = wb[SHEET]
    at = {}
    for r in range(2, ws.max_row + 1):
        v = ws.cell(r, 1).value
        if v:
            at[normalize_position(v)] = r
    applied = []
    for e in edits:
        r = at.get(normalize_position(e.get("pos", "")))
        col = COLS.get(e.get("field"))
        if not r or not col:
            continue
        new = _num(e.get("value") if "value" in e else e.get("new"))
        old = _num(ws.cell(r, col).value)
        if old == new:
            continue
        ws.cell(r, col).value = new
        applied.append({"pos": ws.cell(r, 1).value, "field": e["field"],
                        "old": old, "new": new})
    if applied:
        wb.save(TEMPLATE)
    return applied


def rows_by_anchors(path):
    """Запасной разбор документа: таблица с ожидаемыми заголовками."""
    wb = openpyxl.load_workbook(path, data_only=True)
    want = {"оклад": "оклад", "п2556": "П2556", "п4": "П4"}
    for ws in wb.worksheets:
        for r in range(1, min(ws.max_row, 12) + 1):
            hdr = {}
            for c in range(1, ws.max_column + 1):
                t = str(ws.cell(r, c).value or "").strip().lower()
                if t.startswith("должност"):
                    hdr["pos"] = c
                for k, field in want.items():
                    if t.replace(" ", "").startswith(k):
                        hdr[field] = c
            if "pos" in hdr and len(hdr) > 1:
                rows = []
                for rr in range(r + 1, ws.max_row + 1):
                    pos = ws.cell(rr, hdr["pos"]).value
                    if not pos:
                        continue
                    for field, col in hdr.items():
                        if field == "pos":
                            continue
                        v = _num(ws.cell(rr, col).value)
                        if v is not None:
                            rows.append({"pos": str(pos), "field": field, "value": v})
                return rows
    return []
