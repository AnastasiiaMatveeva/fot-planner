# -*- coding: utf-8 -*-
"""Стенд жизненного цикла прогонов: кто считает, жив ли он, один ли он.

Проверяет два обязательства ТЗ развития агентов на временной базе, без
сервера, решателя и модели:

* RUN-002 — перезапуск сервиса не помечает «прерван» чужой живой расчёт:
  брошенным считается только прогон с замолчавшим heartbeat;
* RUN-001 — запуск расчёта с ключом операции: повтор той же команды
  возвращает тот же прогон, тот же ключ с другими данными — конфликт,
  занятый план не получает второго расчёта.

    .venv/Scripts/python.exe harness/lifecycle.py
    .venv/Scripts/python.exe harness/lifecycle.py --only л02
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CASES = []


def case(num, title):
    def wrap(fn):
        CASES.append((num, title, fn))
        return fn
    return wrap


class App:
    """Сервис на временной базе: один план, никаких документов."""

    def __init__(self, tmp):
        os.environ["FOT_DATA_DIR"] = tmp
        os.environ["FOT_SKIP_ORPHANS"] = "1"
        sys.path.insert(0, os.path.join(ROOT, "app"))
        import db as db_mod
        import main as main_mod
        self.main, self.d = main_mod, db_mod
        self.db = db_mod.session()
        self.case = db_mod.Case(title="Стенд прогонов", year=2026, stage="сбор данных")
        self.db.add(self.case)
        self.db.commit()

    def run(self, status="идет", heartbeat_ago=None, executor="other-host:1:abc"):
        """Прогон в базе. heartbeat_ago=None — старый прогон без графы."""
        hb = None if heartbeat_ago is None else self.d.now() - dt.timedelta(seconds=heartbeat_ago)
        r = self.d.Run(case_id=self.case.id, status=status, settings="{}",
                       executor=executor, heartbeat=hb)
        self.db.add(r)
        self.db.commit()
        return r

    def activity(self):
        a = self.d.Activity(case_id=self.case.id, agent="solver", title="ищет план", state="идет")
        self.db.add(a)
        self.db.commit()
        return a

    def fresh(self, obj):
        self.db.expire_all()
        return self.db.get(type(obj), obj.id)


def expect(cond, msg, out):
    if not cond:
        out.append(msg)


@case("л01", "RUN-002: живой прогон другого процесса перезапуск не трогает")
def _c01(app):
    out = []
    alive = app.run(heartbeat_ago=3)
    act = app.activity()
    res = app.main._close_orphans()
    expect(app.fresh(alive).status == "идет", "живой прогон помечен %s" % app.fresh(alive).status, out)
    expect(app.fresh(act).state == "идет", "строка работы закрыта при живом прогоне", out)
    expect(res == {"прервано": 0, "живых": 1}, "итог %s" % res, out)
    return out


@case("л02", "RUN-002: замолчавший и старый (без heartbeat) прогоны помечаются «прерван»")
def _c02(app):
    out = []
    silent = app.run(heartbeat_ago=app.main.STALE_SEC + 5)
    legacy = app.run(heartbeat_ago=None, executor=None)
    act = app.activity()
    res = app.main._close_orphans()
    expect(app.fresh(silent).status == "прерван", "замолчавший: %s" % app.fresh(silent).status, out)
    expect(app.fresh(legacy).status == "прерван", "старый без heartbeat: %s" % app.fresh(legacy).status, out)
    expect(app.fresh(act).state == "прервано", "строка работы не закрыта: %s" % app.fresh(act).state, out)
    expect(res == {"прервано": 2, "живых": 0}, "итог %s" % res, out)
    return out


@case("л03", "RUN-002: живой и брошенный рядом — закрывается только брошенный, строки работы остаются")
def _c03(app):
    out = []
    alive = app.run(heartbeat_ago=1)
    dead = app.run(heartbeat_ago=600)
    act = app.activity()
    app.main._close_orphans()
    expect(app.fresh(alive).status == "идет", "живой помечен", out)
    expect(app.fresh(dead).status == "прерван", "брошенный не помечен", out)
    expect(app.fresh(act).state == "идет", "строка работы закрыта, хотя есть живой прогон", out)
    return out


@case("л04", "RUN-001: повтор команды с тем же ключом возвращает тот же прогон")
def _c04(app):
    out = []
    r1, created1 = app.main._start_run(app.db, app.case.id, {"допуск": 0.05}, operation_id="op-1")
    r2, created2 = app.main._start_run(app.db, app.case.id, {"допуск": 0.05}, operation_id="op-1")
    expect(created1 and not created2, "создан: %s, %s" % (created1, created2), out)
    expect(r1.id == r2.id, "разные прогоны %s и %s" % (r1.id, r2.id), out)
    expect(app.db.query(app.d.Run).count() == 1, "прогонов в базе %d" % app.db.query(app.d.Run).count(), out)
    expect(r1.executor == app.main.EXECUTOR and r1.heartbeat is not None, "исполнитель или heartbeat не записаны", out)
    return out


@case("л05", "RUN-001: тот же ключ с другими данными — конфликт 409, второго прогона нет")
def _c05(app):
    out = []
    app.main._start_run(app.db, app.case.id, {"допуск": 0.05}, operation_id="op-2")
    try:
        app.main._start_run(app.db, app.case.id, {"допуск": 0.03}, operation_id="op-2")
        out.append("конфликт не обнаружен")
    except app.main.HTTPException as exc:
        expect(exc.status_code == 409, "код %s вместо 409" % exc.status_code, out)
    expect(app.db.query(app.d.Run).count() == 1, "прогонов в базе %d" % app.db.query(app.d.Run).count(), out)
    return out


@case("л06", "RUN-001: занятый план не получает второго расчёта, ключ ли есть, нет ли")
def _c06(app):
    out = []
    busy = app.run(heartbeat_ago=1, executor=app.main.EXECUTOR)
    r, created = app.main._start_run(app.db, app.case.id, {})
    expect(not created and r.id == busy.id, "второй прогон создан: %s %s" % (created, r.id), out)
    r2, created2 = app.main._start_run(app.db, app.case.id, {}, operation_id="op-3")
    expect(not created2 and r2.id == busy.id, "второй прогон с ключом создан: %s %s" % (created2, r2.id), out)
    expect(app.db.query(app.d.Run).count() == 1, "прогонов в базе %d" % app.db.query(app.d.Run).count(), out)
    return out


@case("л07", "RUN-002: heartbeat идущего прогона обновляется и замолкает по завершении")
def _c07(app):
    out = []
    run = app.run(heartbeat_ago=300, executor=app.main.EXECUTOR)
    app.main.HEARTBEAT_SEC = 0.05
    stop = threading.Event()
    t = threading.Thread(target=app.main._heartbeat, args=(run.id, stop), daemon=True)
    t.start()
    time.sleep(0.4)
    hb1 = app.fresh(run).heartbeat
    expect(hb1 is not None and (app.d.now() - hb1).total_seconds() < 5, "heartbeat не обновился: %s" % hb1, out)
    stop.set()
    t.join(1.0)
    expect(not t.is_alive(), "поток heartbeat не остановился", out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="номера случаев через запятую")
    args = ap.parse_args()
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    rows, failed = [], 0
    for num, title, fn in CASES:
        if only and num not in only:
            continue
        tmp = tempfile.mkdtemp(prefix="lifecycle_case_")
        t0 = time.time()
        for mod in ("main", "chat", "db", "agents", "intake", "reference", "rules"):
            sys.modules.pop(mod, None)
        try:
            app = App(tmp)
            bad = fn(app) or []
            app.db.close()
        except Exception as exc:  # noqa: BLE001 — стенд не должен падать целиком
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
