# -*- coding: utf-8 -*-
"""Миграция базы: показать, сделать копию, применить, проверить, вернуть.

    scripts/migrate.py --dry-run                       # что будет сделано, без записи
    scripts/migrate.py --apply --backup DIR            # копия, затем шаги в транзакциях
    scripts/migrate.py --check                         # версия и графы на месте?
    scripts/migrate.py --restore DIR/fot.sqlite3.<метка>.bak
    scripts/migrate.py --db ПУТЬ ...                   # другая база (копия, стенд)
    scripts/migrate.py ... --report ПУТЬ.json          # отчёт о прогоне

Правила, ради которых скрипт существует (MIG-002):

* применение без резервной копии не выполняется — `--no-backup` надо
  написать руками, и это остаётся в отчёте;
* каждый шаг идёт в своей транзакции: упавший шаг откатывается целиком,
  версия базы не меняется;
* повтор на той же базе ничего не делает — шаги идемпотентны и отмечены
  в таблице schema_version;
* база с версией выше известной скрипту не трогается: это база более нового
  выпуска, и «поправить» её старым кодом нельзя;
* отчёт содержит число строк каждой таблицы до и после и проверку связей
  (прогон без плана, сообщение без плана) — потерянные данные должны быть
  видны сразу.

Живая база приложения — по умолчанию; на неё скрипт запускает оператор,
остановив сервер. Стенд гоняет всё это на временных копиях.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
import migrations as M  # noqa: E402

DEFAULT_DB = os.path.abspath(os.environ.get("FOT_DATA_DIR") or os.path.join(ROOT, "app", "data"))
DEFAULT_DB = os.path.join(DEFAULT_DB, "fot.sqlite3")
TABLES = ("cases", "employees", "contracts", "substitutions", "documents", "activities",
          "questions", "messages", "runs", "labor_rows", "inflows", "secret_allowances",
          "corrections", "verdicts", "proposals")
#: Связи, обрыв которых означает потерянные данные, а не пустую таблицу.
LINKS = (("runs", "case_id", "cases"), ("messages", "case_id", "cases"),
         ("activities", "case_id", "cases"), ("documents", "case_id", "cases"))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_version_table(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version ("
                 "version INTEGER PRIMARY KEY, title TEXT, applied_at TEXT)")


def current_version(conn):
    if not M.columns(conn, "schema_version"):
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0] or 0)


def counts(conn):
    out = {}
    for t in TABLES:
        if M.columns(conn, t):
            out[t] = conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
    return out


def broken_links(conn):
    out = {}
    for child, col, parent in LINKS:
        # Связь проверяется только там, где обе таблицы и графа есть: на
        # старой базе графы может ещё не быть, и это не обрыв связи.
        if col not in M.columns(conn, child) or not M.columns(conn, parent):
            continue
        n = conn.execute(
            "SELECT COUNT(*) FROM %s c WHERE c.%s IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM %s p WHERE p.id = c.%s)" % (child, col, parent, col)).fetchone()[0]
        if n:
            out["%s.%s → %s" % (child, col, parent)] = n
    return out


def backup(db_path, backup_dir):
    """Согласованная копия через API резервного копирования SQLite: обычный
    copy живой базы может захватить полузаписанную страницу."""
    os.makedirs(backup_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = os.path.join(backup_dir, "fot.sqlite3.%s.bak" % stamp)
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def run(args):
    report = {"db": args.db, "mode": None, "started_at": dt.datetime.now().isoformat(),
              "steps": [], "backup": None, "errors": []}
    if args.restore:
        report["mode"] = "restore"
        if not os.path.isfile(args.restore):
            report["errors"].append("копии нет: %s" % args.restore)
            return report, 2
        shutil.copyfile(args.restore, args.db)
        report["restored_sha256"] = sha256(args.db)
        print("восстановлено из %s" % args.restore)
        return report, 0

    if not os.path.isfile(args.db):
        report["errors"].append("базы нет: %s" % args.db)
        return report, 2
    report["sha256_before"] = sha256(args.db)
    conn = sqlite3.connect(args.db)
    conn.isolation_level = None  # транзакциями управляем сами
    try:
        known = M.steps()
        have = current_version(conn)
        top = known[-1].VERSION if known else 0
        report.update({"version_before": have, "version_known": top,
                       "rows_before": counts(conn), "links_before": broken_links(conn)})
        if have > top:
            report["errors"].append("версия базы %d выше известной %d: база более нового выпуска"
                                    % (have, top))
            return report, 3
        pending = [s for s in known if s.VERSION > have]

        if args.check:
            report["mode"] = "check"
            missing = []
            for s in known:
                missing += ["v%d: %s" % (s.VERSION, line) for line in s.plan(conn)]
            report["pending"] = [s.VERSION for s in pending]
            report["missing"] = missing
            ok = not pending and not missing and not report["links_before"]
            print("версия %d из %d; не применено шагов: %d; недостающих действий: %d; "
                  "обрывов связей: %d" % (have, top, len(pending), len(missing),
                                          len(report["links_before"])))
            return report, 0 if ok else 1

        for s in pending:
            actions = s.plan(conn)
            report["steps"].append({"version": s.VERSION, "title": s.TITLE, "actions": actions,
                                    "applied": False})
        if args.dry_run or not args.apply:
            report["mode"] = "dry-run"
            print("версия базы %d, известно шагов до %d; к применению: %d" % (have, top, len(pending)))
            for st in report["steps"]:
                print("  v%d %s" % (st["version"], st["title"]))
                for a in st["actions"] or ["  (графы уже на месте — только отметка версии)"]:
                    print("     ", a)
            return report, 0

        report["mode"] = "apply"
        if not pending:
            print("применять нечего: версия %d" % have)
            report["rows_after"] = report["rows_before"]
            return report, 0
        if args.backup:
            report["backup"] = backup(args.db, args.backup)
            report["backup_sha256"] = sha256(report["backup"])
            print("копия: %s" % report["backup"])
        elif not args.no_backup:
            report["errors"].append("применение без копии: укажите --backup DIR или --no-backup")
            return report, 2
        ensure_version_table(conn)
        for s, st in zip(pending, report["steps"]):
            conn.execute("BEGIN")
            try:
                s.apply(conn)
                conn.execute("INSERT INTO schema_version(version, title, applied_at) VALUES (?, ?, ?)",
                             (s.VERSION, s.TITLE, dt.datetime.now().isoformat()))
                conn.execute("COMMIT")
                st["applied"] = True
                print("применён v%d: %s (%d действий)" % (s.VERSION, s.TITLE, len(st["actions"])))
            except Exception as exc:  # noqa: BLE001 — откат и честный отчёт важнее
                conn.execute("ROLLBACK")
                report["errors"].append("v%d: %s: %s" % (s.VERSION, type(exc).__name__, exc))
                report["version_after"] = current_version(conn)
                report["rows_after"] = counts(conn)
                return report, 4
        report["version_after"] = current_version(conn)
        report["rows_after"] = counts(conn)
        report["links_after"] = broken_links(conn)
        lost = {t: (report["rows_before"].get(t), report["rows_after"].get(t))
                for t in report["rows_before"] if report["rows_before"][t] != report["rows_after"].get(t)}
        if lost:
            report["errors"].append("число строк изменилось: %s" % lost)
            return report, 5
        return report, 0
    finally:
        conn.close()
        if os.path.isfile(args.db):
            report["sha256_after"] = sha256(args.db)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--backup", metavar="DIR")
    ap.add_argument("--no-backup", action="store_true", help="применить без копии (осознанно)")
    ap.add_argument("--restore", metavar="FILE")
    ap.add_argument("--report", metavar="PATH")
    args = ap.parse_args()
    report, code = run(args)
    report["exit_code"] = code
    for e in report["errors"]:
        print("ошибка:", e, file=sys.stderr)
    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)) or ".", exist_ok=True)
        io.open(args.report, "w", encoding="utf-8").write(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    return code


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
