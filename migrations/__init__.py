# -*- coding: utf-8 -*-
"""Нумерованные шаги изменения схемы базы.

Зачем отдельно от `app/db.py::init_db`. Тот при каждом старте молча
добавляет недостающие графы в живую базу — путь, которым схема менялась
всё это время. Он остаётся как совместимость (MIG-003), но ответственность
за схему переходит сюда: у базы появляется номер версии, каждый шаг можно
показать до применения (dry-run), применить в транзакции с резервной копией
и повторить без последствий (MIG-002).

Шаг — модуль `NNN_имя.py` с полями:

    VERSION = NNN
    TITLE   = "что делает"
    def plan(conn) -> list[str]     # что будет сделано; пусто — нечего
    def apply(conn) -> None         # сами изменения, только через conn

Шаги пишутся на sqlite3 без моделей SQLAlchemy: миграция должна работать
и на базе, которую текущие модели уже не описывают.
"""
from __future__ import annotations

import importlib.util
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))


def steps():
    """Шаги по возрастанию номера; номер в имени файла обязан совпадать с VERSION."""
    out = []
    for name in sorted(os.listdir(HERE)):
        m = re.match(r"^(\d{3})_[a-z0-9_]+\.py$", name)
        if not m:
            continue
        spec = importlib.util.spec_from_file_location("migrations." + name[:-3],
                                                      os.path.join(HERE, name))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if mod.VERSION != int(m.group(1)):
            raise RuntimeError("%s: VERSION=%s не совпадает с номером в имени" % (name, mod.VERSION))
        out.append(mod)
    versions = [s.VERSION for s in out]
    if versions != sorted(set(versions)):
        raise RuntimeError("номера шагов повторяются: %s" % versions)
    return out


def columns(conn, table):
    """Имена граф таблицы; пусто — таблицы нет."""
    return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def add_missing(conn, table, cols, plan_only=False):
    """ALTER TABLE ADD COLUMN для отсутствующих граф. Возвращает список действий."""
    have = columns(conn, table)
    if not have:
        return ["таблица %s отсутствует: create_all приложения создаст её целиком" % table]
    done = []
    for name, kind in cols:
        if name in have:
            continue
        done.append("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, kind))
        if not plan_only:
            conn.execute(done[-1])
    return done
