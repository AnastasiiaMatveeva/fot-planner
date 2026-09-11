# -*- coding: utf-8 -*-
"""Стенд локальных решений: кто решил, над какой версией, что записано.

Временная база, подставной ответ модели, копия справочника во временной
папке — ни живые данные, ни файл справочника в репозитории не трогаются.

    .venv/Scripts/python.exe harness/decisions.py
    .venv/Scripts/python.exe harness/decisions.py --only р03
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
CASES = []


def case(num, title):
    def wrap(fn):
        CASES.append((num, title, fn))
        return fn
    return wrap


def expect(cond, msg, out):
    if not cond:
        out.append(msg)


class Req:
    """Запрос FastAPI в объёме, который читают маршруты: тело JSON."""

    def __init__(self, body):
        self._body = json.dumps(body, ensure_ascii=False).encode("utf-8")

    async def body(self):
        return self._body

    async def json(self):
        return json.loads(self._body.decode("utf-8"))


class App:
    def __init__(self, tmp):
        os.environ["FOT_DATA_DIR"] = os.path.join(tmp, "data")
        os.environ["FOT_SKIP_ORPHANS"] = "1"
        os.environ["FOT_OPERATOR"] = "Матвеева А.И."
        # Справочник должностей — копия во временной папке: запись в него
        # это тоже решение, и стенд не должен править файл репозитория.
        ref = os.path.join(tmp, "demo_input.xlsx")
        shutil.copyfile(os.path.join(ROOT, "docs", "ui", "demo_input.xlsx"), ref)
        os.environ["FOT_REFERENCE_TEMPLATE"] = ref
        sys.path.insert(0, os.path.join(ROOT, "app"))
        for mod in ("main", "chat", "db", "agents", "intake", "reference", "rules"):
            sys.modules.pop(mod, None)
        import chat as chat_mod
        import db as db_mod
        import main as main_mod
        self.main, self.d, self.chat = main_mod, db_mod, chat_mod
        self.db = db_mod.session()
        self.case = db_mod.Case(title="Стенд решений", year=2026, stage="сбор данных")
        self.db.add(self.case)
        self.db.commit()
        self.replies = []
        # Ответ модели подставляется записью; без «ok» сервис считает, что
        # модель недоступна, и не делает ничего — как и должен (ч15).
        chat_mod.reply = lambda db, case, text: self.replies.pop(0) if self.replies else {"ok": True, "action": "ничего", "reply": "ок"}
        main_mod._solve = lambda case_id, run_id, settings=None: None

    def document(self, name="Штатное расписание 2026.xlsx", sha="a" * 64):
        d = self.d.Document(case_id=self.case.id, name=name, kind="штатное расписание",
                            path=os.path.join(os.environ["FOT_DATA_DIR"], name), state="разобран", sha256=sha)
        self.db.add(d)
        self.db.commit()
        return d

    def proposal(self, doc, code="E9", fio="Тестов Тест Тестович"):
        p = self.d.Proposal(document_id=doc.id, entity="сотрудник",
                            payload=json.dumps({"code": code, "fio": fio, "position": "Инженер",
                                                "rate": 1.0, "salary": 100000}, ensure_ascii=False),
                            state="предложено")
        self.db.add(p)
        self.db.commit()
        return p

    def decisions(self):
        self.db.expire_all()
        return self.db.query(self.d.Decision).order_by(self.d.Decision.id).all()

    def run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def say(self, text, reply):
        self.replies.append(reply)

        class Bg:
            def add_task(self, fn, *a, **kw):
                pass
        return self.main.post_message(self.case.id, Bg(), {"text": text})


@case("р01", "оператор — из FOT_OPERATOR или учётной записи ОС, никогда из тела запроса")
def _c01(app):
    out = []
    from fot_planner.harness_local import decisions as D
    name, src = D.operator()
    expect(name == "Матвеева А.И." and src == "FOT_OPERATOR", "оператор %s (%s)" % (name, src), out)
    os.environ.pop("FOT_OPERATOR")
    name, src = D.operator()
    expect(name and src == "учётная запись ОС", "без переменной: %s (%s)" % (name, src), out)
    os.environ["FOT_OPERATOR"] = "Матвеева А.И."
    doc = app.document()
    pr = app.proposal(doc)
    app.run(app.main.decide_proposals(doc.id, Req({"accept": [pr.id], "actor": "Шеф"})))
    dec = app.decisions()
    expect(dec and dec[-1].actor == "Матвеева А.И." and "Шеф" not in (dec[-1].actor or ""),
           "подпись из тела запроса попала в решение: %s" % (dec and dec[-1].actor), out)
    return out


@case("р02", "принятие строки записывается как решение над версией документа с хешем предмета")
def _c02(app):
    out = []
    doc = app.document(sha="b" * 64)
    p1, p2 = app.proposal(doc, "E1"), app.proposal(doc, "E2", "Другой Д.Д.")
    res = app.run(app.main.decide_proposals(doc.id, Req({"accept": [p1.id], "reject": [p2.id],
                                                          "document_sha256": "b" * 64})))
    dec = app.decisions()
    expect(len(dec) == 1 and dec[0].kind == "данные" and dec[0].subject_kind == "документ"
           and dec[0].subject_id == str(doc.id), "решение не записано: %s" % [(d.kind, d.subject_id) for d in dec], out)
    d = dec[0]
    act = json.loads(d.action)
    expect(act.get("принято") == [p1.id] and act.get("отклонено") == [p2.id], "действие: %s" % act, out)
    expect(d.precondition == "b" * 64 and d.outcome == "применено", "предусловие/исход: %s %s" % (d.precondition, d.outcome), out)
    expect(len(d.subject_digest or "") == 64 and d.scope == "организация", "хеш или область: %s %s" % (d.subject_digest, d.scope), out)
    expect(app.db.query(app.d.Employee).filter_by(code="E1").count() == 1, "строка не принята в реестр", out)
    return out


@case("р03", "документ изменился после открытия формы — решение отклонено, реестр не тронут")
def _c03(app):
    out = []
    doc = app.document(sha="c" * 64)
    pr = app.proposal(doc, "E5")
    try:
        app.run(app.main.decide_proposals(doc.id, Req({"accept": [pr.id], "document_sha256": "старая-версия"})))
        out.append("устаревшее решение применено без ошибки")
    except app.main.HTTPException as exc:
        expect(exc.status_code == 409, "код %s вместо 409" % exc.status_code, out)
    app.db.expire_all()
    expect(app.db.query(app.d.Employee).filter_by(code="E5").count() == 0, "строка попала в реестр по устаревшей форме", out)
    expect(app.db.get(app.d.Proposal, pr.id).state == "предложено", "предложение изменило состояние", out)
    dec = app.decisions()
    expect(dec and dec[-1].outcome.startswith("отклонено"), "отказ не записан как решение: %s" % [x.outcome for x in dec], out)
    return out


@case("р04", "запись величины в справочник — решение вида «норма» над документом-источником")
def _c04(app):
    out = []
    doc = app.document("Приказ 2556.pdf", sha="d" * 64)
    doc.kind = "приказ"
    doc.gave = json.dumps({"расхождения": [{"pos": "Инженер", "field": "П2556", "old": 110000, "new": 111000}]}, ensure_ascii=False)
    app.db.commit()
    res = app.run(app.main.document_reference(doc.id, Req({"edits": [{"pos": "Инженер", "field": "П2556", "value": 111000}],
                                                            "document_sha256": "d" * 64})))
    dec = app.decisions()
    expect(dec and dec[-1].kind == "норма" and dec[-1].subject_id == str(doc.id), "решение по норме не записано: %s" % [(x.kind, x.subject_id) for x in dec], out)
    expect(res.get("applied"), "правка справочника не применена: %s" % res, out)
    act = json.loads(dec[-1].action) if dec else {}
    expect(act.get("правок") == 1, "действие: %s" % act, out)
    return out


@case("р05", "исключение документа из плана — решение вида «состав плана»")
def _c05(app):
    out = []
    doc = app.document("Старая штатка.xlsx", sha="e" * 64)
    app.run(app.main.mute_document(app.case.id, doc.id, Req({"muted": True})))
    dec = app.decisions()
    expect(dec and dec[-1].kind == "состав плана" and dec[-1].subject_kind == "план"
           and json.loads(dec[-1].action) == {"документ": doc.id, "исключён": True},
           "решение о составе не записано: %s" % [(x.kind, x.action) for x in dec], out)
    expect(dec[-1].scope == "план %d" % app.case.id, "область: %s" % dec[-1].scope, out)
    return out


@case("р06", "условие из чата — решение «настройка плана» с репликой как основанием")
def _c06(app):
    out = []
    app.say("допуск трудоёмкости 5 %", {"ok": True, "action": "закрепить", "fixes": [],
                                        "settings": [{"name": "допуск трудоёмкости", "value": "0.05"}], "reply": "Записал."})
    dec = [x for x in app.decisions() if x.kind == "настройка плана"]
    expect(dec, "решение по настройке не записано", out)
    if dec:
        expect("допуск трудоёмкости 5 %" in (dec[-1].grounds or ""), "основание не реплика: %s" % dec[-1].grounds, out)
        expect(dec[-1].subject_kind == "план" and dec[-1].actor == "Матвеева А.И.", "предмет/оператор: %s %s" % (dec[-1].subject_kind, dec[-1].actor), out)
        after = json.loads(app.db.get(app.d.Case, app.case.id).plan_settings or "{}")
        expect(dec[-1].subject_digest == hashlib.sha256(json.dumps(after, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
               "хеш предмета не равен хешу записанных настроек", out)
    return out


@case("р07", "список решений плана отдаётся маршрутом, новые сверху")
def _c07(app):
    out = []
    doc = app.document("Договоры.xlsx", sha="f" * 64)
    app.run(app.main.mute_document(app.case.id, doc.id, Req({"muted": True})))
    app.run(app.main.mute_document(app.case.id, doc.id, Req({"muted": False})))
    rows = app.main.list_decisions(case_id=app.case.id)
    expect(len(rows) == 2 and rows[0]["id"] > rows[1]["id"], "список: %s" % [(r.get("id"), r.get("kind")) for r in rows], out)
    expect(all(r["actor"] == "Матвеева А.И." and r["kind"] == "состав плана" for r in rows), "поля списка: %s" % rows[:1], out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    rows, failed = [], 0
    for num, title, fn in CASES:
        if only and num not in only:
            continue
        tmp = tempfile.mkdtemp(prefix="decisions_case_")
        t0 = time.time()
        try:
            app = App(tmp)
            bad = fn(app) or []
            app.db.close()
            app.d.engine.dispose()
        except Exception as exc:  # noqa: BLE001
            bad = ["ошибка стенда: %s: %s" % (type(exc).__name__, str(exc)[:300])]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            for k in ("FOT_DATA_DIR", "FOT_REFERENCE_TEMPLATE", "FOT_OPERATOR"):
                os.environ.pop(k, None)
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
