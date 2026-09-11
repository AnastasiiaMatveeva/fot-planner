# -*- coding: utf-8 -*-
"""Стенд чата: что сервис делает с репликой экономиста.

Правила записаны словами в `docs/ЧАТ_ПРАВИЛА.md`, здесь они проверяются.
Проверяется не формулировка модели, а поведение сервиса: ответ модели
подставляется записью, поэтому стенд не ходит в сеть и идёт секунды.

Каждый случай получает свежую базу во временной папке (`FOT_DATA_DIR`),
маленький реестр (два человека, два договора) и план. Дальше в сервис
подаётся реплика, а рядом — заготовленный ответ модели.

    .venv/Scripts/python.exe harness/chat.py
    .venv/Scripts/python.exe harness/chat.py --only ч05
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CASES = []


def case(num, title):
    def wrap(fn):
        CASES.append((num, title, fn))
        return fn
    return wrap


class App:
    """Сервис на временной базе: реестр, план, реплики."""

    def __init__(self, tmp):
        os.environ["FOT_DATA_DIR"] = tmp
        os.environ["FOT_SKIP_ORPHANS"] = "1"
        sys.path.insert(0, os.path.join(ROOT, "app"))
        import chat as chat_mod
        import db as db_mod
        import main as main_mod
        self.main, self.db_mod, self.chat = main_mod, db_mod, chat_mod
        self.db = db_mod.session()
        self.case = db_mod.Case(title="Стенд чата", year=2026, stage="сбор данных")
        self.db.add(self.case)
        self.db.commit()
        self._registry()
        self.replies = []          # что подставлять вместо ответа модели
        chat_mod.reply = self._fake_reply
        # Фон в стенде не нужен: расчёт запускать нечем и незачем.
        self.solved = []
        main_mod._solve = lambda case_id, run_id, settings=None: self.solved.append(run_id)

    def _registry(self):
        d = self.db_mod
        self.db.add(d.Employee(code="E1", fio="Петров Павел Петрович",
                               position="Инженер", rate=1.0, salary=100000,
                               source="стенд"))
        self.db.add(d.Employee(code="E2", fio="Иванов Игорь Иванович",
                               position="Ведущий инженер", rate=1.0, salary=145000,
                               source="стенд"))
        self.db.add(d.Contract(code="C_GRANT-26", name="Грант", fund=1000000,
                               date_from="01.01.2026", date_to="31.12.2026",
                               kinds="оклад, 122", source="стенд"))
        self.db.add(d.Contract(code="C_BASE-26", name="Внебюджет", fund=5000000,
                               date_from="01.01.2026", date_to="31.12.2026",
                               kinds="оклад, 122, приказ", source="стенд"))
        self.db.commit()

    # ── подстановка ответа модели ───────────────────────────────────────
    def _fake_reply(self, db, case, text):
        return self.replies.pop(0) if self.replies else {
            "ok": True, "reply": "Принял.", "action": "ничего",
            "edits": [], "claims": [], "fixes": [], "settings": []}

    def say(self, text, reply=None):
        """Реплика экономиста. reply — что «ответит» модель."""
        if reply is not None:
            self.replies.append(reply)
        body = {"text": text}

        class Bg:
            def __init__(self): self.tasks = []
            def add_task(self, fn, *a, **kw): self.tasks.append((fn, a, kw))

        bg = Bg()
        out = self.main.post_message(self.case.id, bg, body)
        for fn, a, kw in bg.tasks:
            fn(*a, **kw)
        self.db.expire_all()
        return out

    def messages(self):
        m = self.db_mod
        rows = (self.db.query(m.Message).filter_by(case_id=self.case.id)
                .order_by(m.Message.id).all())
        return [(r.who, r.text or "") for r in rows]

    def last(self):
        return self.messages()[-1][1] if self.messages() else ""

    def plan_settings(self):
        self.db.refresh(self.case)
        return self.chat.plan_settings(self.case)

    def runs(self):
        m = self.db_mod
        return self.db.query(m.Run).filter_by(case_id=self.case.id).all()

    def add_run(self, status="OPTIMAL"):
        run = self.db_mod.Run(case_id=self.case.id, status=status, settings="{}")
        self.db.add(run)
        self.db.commit()
        return run


def expect(ok, why, out):
    if not ok:
        out.append(why)
    return ok


def model(reply="Принял.", action="ничего", **kw):
    d = {"ok": True, "reply": reply, "action": action, "document": None,
         "as_kind": None, "answer": None, "edits": [], "claims": [],
         "fixes": [], "settings": []}
    d.update(kw)
    return d


# ── случаи ──────────────────────────────────────────────────────────────
@case("ч01", "Реплика попадает в ленту раньше ответа модели")
def c01(app):
    out = []
    app.say("что с планом?", model(reply="План пока не считался."))
    msgs = app.messages()
    mine = [i for i, (who, t) in enumerate(msgs) if who == "экономист" and "что с планом" in t]
    theirs = [i for i, (who, t) in enumerate(msgs) if who == "агент"]
    expect(mine, "реплика экономиста не записана", out)
    expect(mine and theirs and mine[0] < theirs[-1], "ответ записан раньше реплики", out)
    return out


@case("ч02", "Ответ модели попадает в ленту как есть")
def c02(app):
    out = []
    app.say("сколько выплачено?", model(reply="В состоянии плана этого нет."))
    expect("В состоянии плана этого нет." == app.last(), "ответ не показан: %s" % app.last(), out)
    return out


@case("ч03", "Состояние документа подаётся модели вместе с планом")
def c03(app):
    out = []
    d = app.db_mod
    doc = d.Document(name="Структура цены.xlsx", path="нет", state="не распознан",
                     summary="в файле только пустая форма", kind="документ по договору")
    app.db.add(doc)
    app.db.commit()
    ctx = app.chat.context(app.db, app.case)
    expect("Структура цены.xlsx" in ctx, "документа нет в состоянии плана", out)
    expect("в файле только пустая форма" in ctx, "причина неудачи не подана модели", out)
    return out


@case("ч04", "«Объясни заново» не запускает расчёт")
def c04(app):
    out = []
    app.add_run("OPTIMAL")
    r = app.say("объясни заново, почему так вышло",
                model(reply="Объясняю иначе.", action="ничего"))
    expect(r.get("action") != "расчет", "расчёт запущен на просьбу объяснить", out)
    expect(not app.solved, "решатель вызван", out)
    return out


@case("ч05", "Закрепление словами пишется в переменные плана")
def c05(app):
    out = []
    app.say("оставь Петрова на гранте с июня по декабрь",
            model(reply="Закрепляю.", fixes=[{"employee": "Петров", "contract": "C_GRANT-26",
                                              "month_from": 6, "month_to": 12,
                                              "kind": "оклад", "mode": "назначить"}]))
    ps = app.plan_settings()
    got = ps["назначения"]
    expect(len(got) == 1, "закреплений %d вместо одного" % len(got), out)
    if got:
        a = got[0]
        expect(a["сотрудник"] == "E1", "закреплён не тот сотрудник: %s" % a["сотрудник"], out)
        expect(a["договор"] == "C_GRANT-26", "закреплён не тот договор: %s" % a["договор"], out)
        expect((a["с"], a["по"]) == (6, 12), "месяцы %s–%s вместо 6–12" % (a["с"], a["по"]), out)
    expect(any("Петров" in t for _w, t in app.messages()[-3:]),
           "в ленте нет строки о том, что записано", out)
    return out


@case("ч06", "Без месяцев берётся срок договора, и об этом сказано вслух")
def c06(app):
    out = []
    app.say("закрепи Петрова на гранте",
            model(reply="Закрепляю.", fixes=[{"employee": "Петров",
                                              "contract": "C_GRANT-26",
                                              "month_from": None, "month_to": None,
                                              "kind": "оклад", "mode": "назначить"}]))
    got = app.plan_settings()["назначения"]
    expect(len(got) == 1, "закреплений %d вместо одного" % len(got), out)
    if got:
        expect((got[0]["с"], got[0]["по"]) == (1, 12),
               "месяцы %s–%s вместо срока договора" % (got[0]["с"], got[0]["по"]), out)
    expect(any("срок договора" in t for _w, t in app.messages()[-3:]),
           "сервис не сказал, что взял срок договора", out)
    return out


@case("ч06б", "Без договора ничего не пишется, сервис просит уточнить")
def c06b(app):
    out = []
    app.say("закрепи Петрова",
            model(reply="На каком договоре?", fixes=[{"employee": "Петров", "contract": None,
                                                      "month_from": None, "month_to": None,
                                                      "kind": "оклад", "mode": "назначить"}]))
    expect(not app.plan_settings()["назначения"], "записано закрепление без договора", out)
    expect(any("не назван договор" in t for _w, t in app.messages()[-3:]),
           "сервис не попросил назвать договор: %s" % app.last(), out)
    return out


@case("ч07", "Месяцы во фразе разбираются кодом")
def c07(app):
    out = []
    for text, want in (("с июня по декабрь", (6, 12)), ("в марте", (3, 3)),
                       ("июнь–сентябрь", (6, 9)), ("без месяцев", None)):
        got = app.chat.parse_months(text)
        expect(got == want, "«%s» → %s вместо %s" % (text, got, want), out)
    return out


@case("ч08", "Настройки расчёта разбираются кодом")
def c08(app):
    out = []
    got = {d["name"]: str(d["value"]) for d in app.chat.parse_settings(
        "допуск трудоёмкости 5 %, разреши дефицит, не больше трёх договоров оклада")}
    expect(got.get("допуск трудоёмкости") == "5", "допуск: %s" % got.get("допуск трудоёмкости"), out)
    expect(got.get("разрешить дефицит") == "да", "дефицит: %s" % got.get("разрешить дефицит"), out)
    expect(got.get("макс договоров оклада в год") == "3",
           "договоров: %s" % got.get("макс договоров оклада в год"), out)
    app.say("допуск трудоёмкости 5 %", model(reply="Записал допуск."))
    expect(str(app.plan_settings()["настройки"].get("допуск трудоёмкости")).startswith("0.05"),
           "допуск не записан: %s" % app.plan_settings()["настройки"], out)
    return out


@case("ч09", "Правка поля пишется в реестр и отражается в ленте")
def c09(app):
    out = []
    app.say("у Петрова оклад 90 000",
            model(reply="Записал.", action="заполнить поле",
                  edits=[{"entity": "сотрудник", "key": "E1", "field": "оклад",
                          "value": "90000"}]))
    emp = app.db.query(app.db_mod.Employee).filter_by(code="E1").first()
    app.db.refresh(emp)
    expect(float(emp.salary) == 90000, "оклад в реестре %s" % emp.salary, out)
    expect(any("90" in t for _w, t in app.messages()[-2:]), "в ленте нет строки о правке", out)
    return out


@case("ч10", "Поля вне закрытого списка не пишутся")
def c10(app):
    out = []
    before = app.db.query(app.db_mod.Employee).filter_by(code="E1").first().position
    app.say("у Петрова любимый цвет синий",
            model(reply="Такое поле не заполняется.", action="заполнить поле",
                  edits=[{"entity": "сотрудник", "key": "E1", "field": "любимый цвет",
                          "value": "синий"}]))
    emp = app.db.query(app.db_mod.Employee).filter_by(code="E1").first()
    app.db.refresh(emp)
    expect(emp.position == before, "должность изменилась: %s" % emp.position, out)
    return out


@case("ч11", "Претензия к плану меняет вес цели и пересчитывает")
def c11(app):
    out = []
    app.add_run("OPTIMAL")
    r = app.say("слишком много переводов",
                model(reply="Уменьшу переводы вдвое.", action="претензия к плану",
                      claims=[{"goal": "переводы", "direction": "меньше", "factor": 2}]))
    expect(r.get("action") == "расчет", "пересчёт не запущен: %s" % r.get("action"), out)
    runs = app.runs()
    expect(len(runs) == 2, "прогонов %d вместо двух" % len(runs), out)
    if len(runs) == 2:
        s = json.loads(runs[-1].settings or "{}")
        expect(s, "веса не записаны в настройки прогона", out)
    return out


@case("ч12", "Без удачного расчёта условие записывается, но не считает")
def c12(app):
    out = []
    r = app.say("оставь Петрова на гранте с июня по декабрь",
                model(reply="Закрепляю.", fixes=[{"employee": "Петров", "contract": "C_GRANT-26",
                                                  "month_from": 6, "month_to": 12,
                                                  "kind": "оклад", "mode": "назначить"}]))
    expect(app.plan_settings()["назначения"], "закрепление не записано", out)
    expect(r.get("action") == "закреплено", "сервис посчитал без удачного прогона: %s" % r.get("action"), out)
    expect(not app.solved, "решатель вызван", out)
    return out


@case("ч13", "Второй расчёт того же плана не запускается")
def c13(app):
    out = []
    app.add_run("идет")
    r = app.say("посчитай ещё раз", model(reply="Считаю.", action="запустить расчет"))
    expect(r.get("action") == "ничего", "запущен второй расчёт: %s" % r.get("action"), out)
    expect(len(app.runs()) == 1, "прогонов стало %d" % len(app.runs()), out)
    expect("уже идет" in app.last(), "в ленте нет объяснения: %s" % app.last(), out)
    return out


@case("ч14", "Документ исключается и возвращается словами")
def c14(app):
    out = []
    doc = app.db_mod.Document(name="Штатка 2025.xlsx", path="нет", state="разобран",
                              case_id=app.case.id)
    app.db.add(doc)
    app.db.commit()
    app.say("не бери прошлогоднюю штатку",
            model(reply="Исключил.", action="исключить документ", document="Штатка 2025.xlsx"))
    app.db.refresh(app.case)
    muted = json.loads(app.case.muted_docs or "[]")
    expect(doc.id in muted, "документ не исключён: %s" % muted, out)
    app.say("верни штатку",
            model(reply="Вернул.", action="вернуть документ", document="Штатка 2025.xlsx"))
    app.db.refresh(app.case)
    muted = json.loads(app.case.muted_docs or "[]")
    expect(doc.id not in muted, "документ не возвращён: %s" % muted, out)
    return out


@case("ч15", "Модель недоступна — сервис отвечает, а не молчит")
def c15(app):
    out = []
    app.say("почему так вышло?", {"ok": False, "error": "ключ не задан"})
    expect("ключ не задан" in app.last() or "Не могу ответить" in app.last(),
           "нет честного ответа: %s" % app.last(), out)
    return out


@case("ч16", "Несуществующий человек в ответе модели ничего не ломает")
def c16(app):
    out = []
    app.say("закрепи Сидорову на гранте с июня по декабрь",
            model(reply="Закрепляю.", fixes=[{"employee": "Сидорова", "contract": "C_GRANT-26",
                                              "month_from": 6, "month_to": 12,
                                              "kind": "оклад", "mode": "назначить"}]))
    expect(not app.plan_settings()["назначения"], "записано закрепление на чужого", out)
    expect("не нашел" in app.last().lower(), "сервис не сказал, что не нашёл: %s" % app.last(), out)
    return out


@case("ч17", "Чужое действие модели ничего не делает")
def c17(app):
    out = []
    # Список действий закрыт: `chat.reply` заменяет чужое имя на «ничего»,
    # а сервис и без того выполняет только известные ветки.
    expect("удалить всё" not in app.chat.ACTIONS, "«удалить всё» в списке действий", out)
    app.say("сделай красиво", model(reply="Готово.", action="удалить всё"))
    expect(len(app.runs()) == 0, "появился прогон", out)
    expect(not app.plan_settings()["назначения"], "появилось закрепление", out)
    expect(not app.solved, "решатель вызван", out)
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
        tmp = tempfile.mkdtemp(prefix="chat_case_")
        t0 = time.time()
        # Каждому случаю — своя база и свежие модули сервиса.
        for mod in ("main", "chat", "db", "agents", "intake", "reference", "rules"):
            sys.modules.pop(mod, None)
        try:
            app = App(tmp)
            bad = fn(app) or []
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
