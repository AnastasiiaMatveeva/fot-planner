# -*- coding: utf-8 -*-
"""Стенд миграций: показать, скопировать, применить, повторить, откатить.

Всё — на временных базах, собранных здесь же: сегодняшней (все графы уже
есть) и «старой» (графы прогона L1.1 удалены). Живая база не открывается.

    .venv/Scripts/python.exe harness/migrations.py
    .venv/Scripts/python.exe harness/migrations.py --only м03
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PY = sys.executable
MIGRATE = os.path.join(ROOT, "scripts", "migrate.py")
sys.path.insert(0, ROOT)
import migrations as M  # noqa: E402

#: Ожидания стенда берутся из самого реестра шагов: новый шаг не должен
#: ломать стенд, он должен им проверяться. «Старая» база — без граф всех
#: шагов после исходной схемы.
STEPS = M.steps()
TOP = STEPS[-1].VERSION
LEGACY_DROP = [(t, n) for s in STEPS if s.VERSION > 1 for t, cols in s.COLUMNS.items() for n, _ in cols]
CASES = []


def case(num, title):
    def wrap(fn):
        CASES.append((num, title, fn))
        return fn
    return wrap


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def logical(path):
    """Отпечаток содержимого, а не файла: резервная копия через API SQLite
    равна базе по данным и схеме, но не по байтам страниц."""
    conn = sqlite3.connect(path)
    try:
        return hashlib.sha256(chr(10).join(conn.iterdump()).encode("utf-8")).hexdigest()
    finally:
        conn.close()


def fresh_db(tmp, legacy=False, rows=True):
    """База текущей схемы через модели приложения; legacy — без граф L1.1."""
    data = os.path.join(tmp, "data-%s" % ("legacy" if legacy else "now"))
    os.environ["FOT_DATA_DIR"] = data
    os.environ["FOT_SKIP_ORPHANS"] = "1"
    for mod in ("db", "main", "chat", "agents", "intake", "reference", "rules"):
        sys.modules.pop(mod, None)
    sys.path.insert(0, os.path.join(ROOT, "app"))
    import db as d
    d.init_db()
    if rows:
        s = d.session()
        c = d.Case(title="Стенд миграций", year=2026, stage="сбор данных")
        s.add(c)
        s.commit()
        s.add(d.Run(case_id=c.id, status="OPTIMAL", settings="{}"))
        s.add(d.Message(case_id=c.id, who="экономист", text="привет"))
        s.commit()
        s.close()
    d.engine.dispose()
    path = os.path.join(data, "fot.sqlite3")
    if legacy:
        conn = sqlite3.connect(path)
        for table, col in LEGACY_DROP:
            conn.execute("ALTER TABLE %s DROP COLUMN %s" % (table, col))
        conn.commit()
        conn.close()
    return path


def migrate(*args, db, report=None):
    cmd = [PY, MIGRATE, "--db", db] + list(args)
    if report:
        cmd += ["--report", report]
    p = subprocess.run(cmd, cwd=ROOT, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    rep = json.load(io.open(report, encoding="utf-8")) if report and os.path.exists(report) else {}
    return p.returncode, p.stdout, rep


def cols(db, table):
    conn = sqlite3.connect(db)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    finally:
        conn.close()


def version(db):
    conn = sqlite3.connect(db)
    try:
        if not cols(db, "schema_version"):
            return 0
        return conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
    finally:
        conn.close()


def expect(cond, msg, out):
    if not cond:
        out.append(msg)


@case("м01", "dry-run ничего не пишет и перечисляет шаги")
def _c01(tmp):
    out, db = [], fresh_db(tmp, legacy=True)
    before = sha(db)
    code, text, rep = migrate("--dry-run", db=db, report=os.path.join(tmp, "r1.json"))
    expect(code == 0, "код %d: %s" % (code, text[-200:]), out)
    expect(sha(db) == before, "dry-run изменил базу", out)
    expect(rep.get("mode") == "dry-run" and [s["version"] for s in rep.get("steps", [])] == [s.VERSION for s in STEPS],
           "шаги в отчёте: %s" % [s.get("version") for s in rep.get("steps", [])], out)
    acts = [a for s in rep.get("steps", []) for a in s["actions"]]
    expect(any("ADD COLUMN executor" in a for a in acts), "в плане нет добавления executor: %s" % acts, out)
    expect(version(db) == 0, "версия после dry-run %s" % version(db), out)
    return out


@case("м02", "применение без копии отклоняется, база не тронута")
def _c02(tmp):
    out, db = [], fresh_db(tmp, legacy=True)
    before = sha(db)
    code, text, rep = migrate("--apply", db=db, report=os.path.join(tmp, "r2.json"))
    expect(code == 2, "код %d вместо 2" % code, out)
    expect(sha(db) == before, "база изменена без копии", out)
    expect(any("без копии" in e for e in rep.get("errors", [])), "причина не названа: %s" % rep.get("errors"), out)
    return out


@case("м03", "старая база: копия, шаги в транзакциях, графы на месте, строки целы")
def _c03(tmp):
    out, db = [], fresh_db(tmp, legacy=True)
    before = logical(db)
    bdir = os.path.join(tmp, "backup")
    code, text, rep = migrate("--apply", "--backup", bdir, db=db, report=os.path.join(tmp, "r3.json"))
    expect(code == 0, "код %d: %s" % (code, text[-300:]), out)
    expect(rep.get("backup") and os.path.isfile(rep["backup"]), "копии нет", out)
    expect(rep.get("backup") and logical(rep["backup"]) == before, "копия не совпадает с базой до миграции по данным", out)
    expect(logical(db) != before, "миграция не изменила схему", out)
    expect({n for t, n in LEGACY_DROP if t == "runs"} <= cols(db, "runs"), "графы прогона не добавлены", out)
    expect(version(db) == TOP, "версия %s вместо %s" % (version(db), TOP), out)
    expect(rep.get("rows_before") == rep.get("rows_after"), "строки изменились: %s → %s"
           % (rep.get("rows_before"), rep.get("rows_after")), out)
    expect(rep.get("rows_after", {}).get("runs") == 1 and rep["rows_after"].get("messages") == 1,
           "данные потеряны: %s" % rep.get("rows_after"), out)
    return out


@case("м04", "повтор на мигрированной базе ничего не делает")
def _c04(tmp):
    out, db = [], fresh_db(tmp, legacy=True)
    migrate("--apply", "--backup", os.path.join(tmp, "b"), db=db)
    after = sha(db)
    code, text, rep = migrate("--apply", "--backup", os.path.join(tmp, "b"), db=db,
                              report=os.path.join(tmp, "r4.json"))
    expect(code == 0, "код %d" % code, out)
    expect(sha(db) == after, "повтор изменил базу", out)
    expect(not rep.get("steps"), "повтор нашёл шаги: %s" % rep.get("steps"), out)
    expect(rep.get("backup") is None, "повтор сделал копию, хотя применять нечего", out)
    return out


@case("м05", "сегодняшняя база: графы есть, ставится только версия; --check зелёный")
def _c05(tmp):
    out, db = [], fresh_db(tmp, legacy=False)
    code, text, rep = migrate("--apply", "--backup", os.path.join(tmp, "b"), db=db,
                              report=os.path.join(tmp, "r5.json"))
    expect(code == 0, "код %d: %s" % (code, text[-200:]), out)
    expect(all(not s["actions"] for s in rep.get("steps", [])), "на новой базе нашлись действия: %s"
           % [s["actions"] for s in rep.get("steps", [])], out)
    expect(version(db) == TOP, "версия %s вместо %s" % (version(db), TOP), out)
    code, text, rep = migrate("--check", db=db, report=os.path.join(tmp, "r5c.json"))
    expect(code == 0 and rep.get("pending") == [] and rep.get("missing") == [],
           "check: код %d, %s" % (code, text[-200:]), out)
    return out


@case("м06", "база более нового выпуска не трогается")
def _c06(tmp):
    out, db = [], fresh_db(tmp, legacy=False)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, title TEXT, applied_at TEXT)")
    conn.execute("INSERT INTO schema_version VALUES (99, 'из будущего', '2027-01-01')")
    conn.commit()
    conn.close()
    before = sha(db)
    code, text, rep = migrate("--apply", "--backup", os.path.join(tmp, "b"), db=db,
                              report=os.path.join(tmp, "r6.json"))
    expect(code == 3, "код %d вместо 3" % code, out)
    expect(sha(db) == before, "база изменена", out)
    return out


@case("м07", "упавший шаг откатывается целиком: версия и графы прежние")
def _c07(tmp):
    out, db = [], fresh_db(tmp, legacy=True)
    # Ломаем шаг 002 копией каталога миграций: после первой графы — ошибка.
    broken = os.path.join(tmp, "migrations")
    shutil.copytree(os.path.join(ROOT, "migrations"), broken)
    src = io.open(os.path.join(broken, "002_run_owner.py"), encoding="utf-8").read()
    src = src.replace("def apply(conn):\n    for table, cols in COLUMNS.items():\n        add_missing(conn, table, cols)",
                      "def apply(conn):\n    conn.execute('ALTER TABLE runs ADD COLUMN executor VARCHAR(120)')\n"
                      "    raise RuntimeError('обрыв посреди шага')")
    io.open(os.path.join(broken, "002_run_owner.py"), "w", encoding="utf-8").write(src)
    for junk in ("__pycache__",):
        shutil.rmtree(os.path.join(broken, junk), ignore_errors=True)
    script = io.open(MIGRATE, encoding="utf-8").read().replace(
        "import migrations as M", "import importlib.util as _u, os as _o\n"
        "_s = _u.spec_from_file_location('migrations', _o.path.join(%r, '__init__.py'))\n"
        "M = _u.module_from_spec(_s); _s.loader.exec_module(M)" % broken)
    alt = os.path.join(tmp, "migrate_broken.py")
    io.open(alt, "w", encoding="utf-8").write(script)
    p = subprocess.run([PY, alt, "--db", db, "--apply", "--backup", os.path.join(tmp, "b"),
                        "--report", os.path.join(tmp, "r7.json")], cwd=ROOT, text=True,
                       encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    rep = json.load(io.open(os.path.join(tmp, "r7.json"), encoding="utf-8"))
    expect(p.returncode == 4, "код %d: %s" % (p.returncode, p.stdout[-300:]), out)
    expect(version(db) == 1, "версия %s вместо 1 (шаг 1 применён, 2 откачен)" % version(db), out)
    expect("executor" not in cols(db, "runs"), "графа из упавшего шага осталась — транзакция не откатилась", out)
    expect(any("обрыв посреди шага" in e for e in rep.get("errors", [])), "ошибка не записана: %s" % rep.get("errors"), out)
    return out


@case("м08", "восстановление из копии возвращает данные и схему до миграции")
def _c08(tmp):
    out, db = [], fresh_db(tmp, legacy=True)
    before = logical(db)
    code, text, rep = migrate("--apply", "--backup", os.path.join(tmp, "b"), db=db,
                              report=os.path.join(tmp, "r8.json"))
    expect(code == 0 and logical(db) != before, "миграция не изменила базу", out)
    code, text, rep2 = migrate("--restore", rep["backup"], db=db, report=os.path.join(tmp, "r8r.json"))
    expect(code == 0 and logical(db) == before, "после восстановления данные и схема не совпали с исходными", out)
    expect(sha(db) == sha(rep["backup"]), "восстановленный файл не равен копии побайтно", out)
    expect("executor" not in cols(db, "runs") and version(db) == 0, "после восстановления графы или версия остались", out)
    return out


@case("м09", "шаги миграций покрывают всё, что приложение добавляет при старте")
def _c09(tmp):
    out = []
    sys.path.insert(0, ROOT)
    import migrations as M
    sys.modules.pop("db", None)
    os.environ["FOT_DATA_DIR"] = os.path.join(tmp, "data-cov")
    sys.path.insert(0, os.path.join(ROOT, "app"))
    import db as d
    covered = {}
    for s in M.steps():
        for t, cols_ in s.COLUMNS.items():
            covered.setdefault(t, set()).update(n for n, _ in cols_)
    for t, cols_ in d._ADDED_COLUMNS.items():
        missing = {n for n, _ in cols_} - covered.get(t, set())
        expect(not missing, "в шагах миграций нет %s.%s" % (t, sorted(missing)), out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    rows, failed = [], 0
    for num, title, fn in CASES:
        if only and num not in only:
            continue
        tmp = tempfile.mkdtemp(prefix="migr_case_")
        t0 = time.time()
        try:
            bad = fn(tmp) or []
        except Exception as exc:  # noqa: BLE001
            bad = ["ошибка стенда: %s: %s" % (type(exc).__name__, str(exc)[:200])]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            os.environ.pop("FOT_DATA_DIR", None)
        rows.append((num, title, bad))
        failed += 1 if bad else 0
        print("[%s] %-9s %4.1f с  %s" % (num, "ПРОВАЛ" if bad else "ОК", time.time() - t0, title))
        for b in bad[:6]:
            print("      ✗", b)
        sys.stdout.flush()
    print("\nитого: %d случаев, провалов %d" % (len(rows), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
