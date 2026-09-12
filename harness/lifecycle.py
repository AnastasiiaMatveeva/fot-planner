# -*- coding: utf-8 -*-
"""Стенд жизненного цикла прогонов: кто считает, жив ли он, один ли он.

Проверяет два обязательства ТЗ развития агентов на временной базе, без
сервера, решателя и модели:

* RUN-002 — перезапуск сервиса не помечает «прерван» чужой живой расчёт:
  брошенным считается только прогон с замолчавшим heartbeat;
* RUN-001 — запуск расчёта с ключом операции: повтор той же команды
  возвращает тот же прогон, тот же ключ с другими данными — конфликт,
  занятый план не получает второго расчёта; реплика и решение по строкам
  с ключом — тот же ответ на повтор, конфликт на другие данные;
* RUN-002 — повтор прерванного расчёта считает закреплённый вход прежней
  попытки, попытки связаны; старый исполнитель результат не записывает.

    .venv/Scripts/python.exe harness/lifecycle.py
    .venv/Scripts/python.exe harness/lifecycle.py --only л02
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
CASES = []


def case(num, title):
    def wrap(fn):
        CASES.append((num, title, fn))
        return fn
    return wrap


class App:
    """Сервис на временной базе: один план, никаких документов."""

    def __init__(self, tmp):
        self.tmp = tmp
        os.environ["FOT_DATA_DIR"] = tmp
        os.environ["FOT_SKIP_ORPHANS"] = "1"
        # Справочник должностей — копия во временной папке: принятие строки
        # заглядывает в него, а стенд не должен править файл репозитория.
        ref = os.path.join(tmp, "demo_input.xlsx")
        shutil.copyfile(os.path.join(ROOT, "docs", "ui", "demo_input.xlsx"), ref)
        os.environ["FOT_REFERENCE_TEMPLATE"] = ref
        sys.path.insert(0, os.path.join(ROOT, "app"))
        import db as db_mod
        import main as main_mod
        self.main, self.d = main_mod, db_mod
        # Подмены стенда: возвращаются в конце случая, модули общие.
        self._orig = (main_mod.subprocess.run, main_mod.build_input.build)
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

    def restore(self):
        self.main.subprocess.run, self.main.build_input.build = self._orig

    def pinned_failure(self, settings, data=b"PINNED-INPUT", key="first"):
        """Прерванный прогон со снимком: закреплённый вход лежит в хранилище."""
        m = self.main
        run, _ = m._start_run(self.db, self.case.id, settings, operation_id=key)
        src = os.path.join(self.tmp, "in-%d.xlsx" % run.id)
        with open(src, "wb") as f:
            f.write(data)
        draft = m._manifest_draft(run, src, settings, {"документы": [{"id": 1, "имя": "Штатка"}]},
                                  ["fot-planner.exe", "solve", "-i", src], "240")
        digest = run.manifest_sha256
        run.status = "прерван"
        self.db.commit()
        m._manifest_close(self.db, run.id, draft, digest, None, "", [], 1.0)
        return run

    def messages(self):
        self.db.expire_all()
        return self.db.query(self.d.Message).filter_by(case_id=self.case.id).order_by(self.d.Message.id).all()


class Req:
    """Запрос FastAPI в объёме, который читают маршруты: тело JSON."""

    def __init__(self, body):
        self._body = json.dumps(body, ensure_ascii=False).encode("utf-8")

    async def body(self):
        return self._body

    async def json(self):
        return json.loads(self._body.decode("utf-8"))


class Bg:
    """Фоновые задачи FastAPI: собираем, запускаем рукой — стенд синхронный."""

    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *a, **kw):
        self.tasks.append((fn, a, kw))

    def run_all(self):
        for fn, a, kw in self.tasks:
            fn(*a, **kw)
        self.tasks = []


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


@case("л08", "RUN-002: повтор прерванного расчёта считает закреплённый вход прежней попытки, попытки связаны")
def _c08(app):
    out = []
    m = app.main
    first = app.pinned_failure({"допуск": 0.05})
    old_digest = first.manifest_sha256
    seen = {}

    def fake_solver(command, **kw):
        # Что легло на вход решателю — байты файла из команды.
        with open(command[command.index("-i") + 1], "rb") as f:
            seen["input"] = f.read()
        raise RuntimeError("решатель на стенде не запускается")

    def no_registry(*a, **kw):
        raise AssertionError("повтор прочитал реестр вместо закреплённого входа")

    m.subprocess.run, m.build_input.build = fake_solver, no_registry
    bg = Bg()
    res = asyncio.run(m.retry_run(app.case.id, first.id, bg, Req({"operation_id": "l08-retry"})))
    expect(res.get("retry_of") == first.id and len(bg.tasks) == 1, "маршрут повтора: %s" % res, out)
    bg.run_all()
    new = app.db.get(app.d.Run, res["run_id"])
    app.db.expire_all()
    expect(new.retry_of == first.id, "попытка не связана с прежней: retry_of=%s" % new.retry_of, out)
    expect(seen.get("input") == b"PINNED-INPUT", "повтор считал не закреплённый вход: %r" % seen.get("input"), out)
    expect(json.loads(new.settings) == {"допуск": 0.05}, "настройки попытки: %s" % new.settings, out)
    expect(new.status == "ошибка", "статус попытки после падения решателя: %s" % new.status, out)
    from fot_planner.harness_local import manifest as mf
    store = m._artifacts()
    m_old, m_new = mf.load_manifest(store, old_digest), mf.load_manifest(store, new.manifest_sha256)
    expect(m_new["input"]["sha256"] == m_old["input"]["sha256"], "хеш входа попытки отличается от закреплённого", out)
    expect((m_new.get("attempt") or {}).get("retry_of") == first.id, "в снимке нет связи попыток: %s" % m_new.get("attempt"), out)
    expect(m_new["sources"] == m_old["sources"], "источники попытки не из снимка", out)
    # Тот же ключ операции — тот же прогон, второй попытки нет.
    again = asyncio.run(m.retry_run(app.case.id, first.id, Bg(), Req({"operation_id": "l08-retry"})))
    expect(again.get("run_id") == new.id, "повтор ключа дал другой прогон: %s" % again, out)
    # Удачный расчёт не повторяется; без снимка — отказ, прогонов не прибавилось.
    before = app.db.query(app.d.Run).count()
    for r in (app.run(status="OPTIMAL", executor=m.EXECUTOR), app.run(status="прерван", executor=m.EXECUTOR)):
        try:
            asyncio.run(m.retry_run(app.case.id, r.id, Bg(), Req({})))
            out.append("повтор прогона «%s» без снимка/удачного прошёл" % r.status)
        except m.HTTPException as exc:
            expect(exc.status_code == 409, "код %s вместо 409 для «%s»" % (exc.status_code, r.status), out)
    expect(app.db.query(app.d.Run).count() == before + 2, "отказ в повторе всё же завёл прогон", out)
    return out


@case("л09", "RUN-002: прогон, закрытый другим процессом во время счёта, не получает результат от старого исполнителя")
def _c09(app):
    out = []
    m = app.main
    first = app.pinned_failure({})

    def fake_solver(command, **kw):
        # «Другой процесс» закрыл прогон как брошенный, пока решатель считал.
        other = app.d.session()
        try:
            r = other.query(app.d.Run).filter_by(status="идет").one()
            r.status = "прерван"
            other.commit()
        finally:
            other.close()
        return types.SimpleNamespace(returncode=0, stdout="Статус: OPTIMAL\nАудит: OK\n", stderr="")

    m.subprocess.run = fake_solver
    bg = Bg()
    res = asyncio.run(m.retry_run(app.case.id, first.id, bg, Req({})))
    bg.run_all()
    new = app.fresh(app.db.get(app.d.Run, res["run_id"]))
    expect(new.status == "прерван", "старый исполнитель записал результат: статус %s" % new.status, out)
    expect(not new.result_path and not new.summary, "старый исполнитель записал итог: %s %s" % (new.result_path, new.summary), out)
    expect(any("отброшен" in (x.text or "") for x in app.messages()), "в ленте нет слова об отброшенном результате", out)
    expect(app.fresh(app.case).stage != "посчитано", "план стал «посчитано» по отброшенному результату", out)
    return out


@case("л10", "RUN-001: реплика и решение по строкам с ключом операции — повтор даёт тот же ответ, другие данные под тем же ключом — конфликт")
def _c10(app):
    out = []
    m = app.main
    m.chat.reply = lambda db, case, text: {"ok": True, "action": "ничего", "reply": "ок"}
    bg = Bg()
    r1 = m.post_message(app.case.id, bg, {"text": "привет", "operation_id": "l10-msg"})
    r2 = m.post_message(app.case.id, bg, {"text": "привет", "operation_id": "l10-msg"})
    expect(r1 == r2, "повтор реплики дал другой ответ: %s / %s" % (r1, r2), out)
    said = [x for x in app.messages() if x.who == "экономист" and x.text == "привет"]
    expect(len(said) == 1, "реплика в ленте %d раз(а) вместо одного" % len(said), out)
    try:
        m.post_message(app.case.id, bg, {"text": "другое", "operation_id": "l10-msg"})
        out.append("тот же ключ с другим текстом принят")
    except m.HTTPException as exc:
        expect(exc.status_code == 409, "код %s вместо 409" % exc.status_code, out)
    expect(len([x for x in app.messages() if x.who == "экономист"]) == 1, "конфликтная реплика попала в ленту", out)
    # Без ключа — как раньше: каждая отправка отдельная реплика.
    m.post_message(app.case.id, bg, {"text": "привет"})
    expect(len([x for x in app.messages() if x.who == "экономист"]) == 2, "реплика без ключа не записана", out)

    doc = app.d.Document(case_id=app.case.id, name="Штатное расписание 2026.xlsx", kind="штатное расписание",
                         path=os.path.join(app.tmp, "x.xlsx"), state="разобран", sha256="a" * 64)
    app.db.add(doc)
    app.db.commit()
    pr = app.d.Proposal(document_id=doc.id, entity="сотрудник", state="предложено",
                        payload=json.dumps({"code": "E1", "fio": "Тестов Т.Т.", "position": "Инженер",
                                            "rate": 1.0, "salary": 100000}, ensure_ascii=False))
    app.db.add(pr)
    app.db.commit()
    body = {"accept": [pr.id], "document_sha256": "a" * 64, "operation_id": "l10-dec"}
    d1 = asyncio.run(m.decide_proposals(doc.id, Req(body)))
    d2 = asyncio.run(m.decide_proposals(doc.id, Req(body)))
    expect(d1 == d2 and d1.get("added", {}).get("сотрудник") == 1, "повтор решения дал другой ответ: %s / %s" % (d1, d2), out)
    app.db.expire_all()
    expect(app.db.query(app.d.Employee).filter_by(code="E1").count() == 1, "строка принята не один раз", out)
    expect(app.db.query(app.d.Decision).count() == 1, "решений записано %d вместо одного" % app.db.query(app.d.Decision).count(), out)
    try:
        asyncio.run(m.decide_proposals(doc.id, Req({"reject": [pr.id], "operation_id": "l10-dec"})))
        out.append("тот же ключ с другим решением принят")
    except m.HTTPException as exc:
        expect(exc.status_code == 409, "код %s вместо 409" % exc.status_code, out)
    ops = app.db.query(app.d.Operation).order_by(app.d.Operation.id).all()
    expect({o.id for o in ops} == {"l10-msg", "l10-dec"} and all(o.state == "готово" for o in ops),
           "операции: %s" % [(o.id, o.state) for o in ops], out)
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
        app = None
        try:
            app = App(tmp)
            bad = fn(app) or []
            app.db.close()
        except Exception as exc:  # noqa: BLE001 — стенд не должен падать целиком
            bad = ["ошибка стенда: %s: %s" % (type(exc).__name__, str(exc)[:200])]
        finally:
            if app is not None:
                app.restore()
                try:
                    app.d.engine.dispose()
                except Exception:  # noqa: BLE001
                    pass
            shutil.rmtree(tmp, ignore_errors=True)
            os.environ.pop("FOT_DATA_DIR", None)
            os.environ.pop("FOT_REFERENCE_TEMPLATE", None)
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
