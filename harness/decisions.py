# -*- coding: utf-8 -*-
"""Стенд локальных решений: кто решил, над какой версией, что записано —
и какие результаты после решения требуют пересмотра (VER-003).

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

    def other_case(self):
        c = self.d.Case(title="Другой план", year=2027, stage="посчитано")
        self.db.add(c)
        self.db.commit()
        return c

    def result(self, case, docs=(), reference=True, status="OPTIMAL"):
        """Прогон с снимком источников: какие документы и справочник легли в основание."""
        src = {"документы": [{"id": d.id, "имя": d.name, "версия": d.version or 1} for d in docs]}
        if reference:
            src["справочник"] = {"файл": "demo_input.xlsx", "должностей": 10}
        r = self.d.Run(case_id=case.id, status=status, settings="{}", seconds=1.0,
                       summary=json.dumps({"plan_rows": 3}), sources=json.dumps(src, ensure_ascii=False))
        self.db.add(r)
        self.db.commit()
        return r

    def review(self, run):
        self.db.expire_all()
        r = self.db.get(self.d.Run, run.id)
        return r.review or "current", json.loads(r.review_log) if r.review_log else []

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


@case("р08", "VER-003: замена версии документа отмечает планы, посчитанные на ней; независимый план и неудачный прогон не трогаются")
def _c08(app):
    out = []
    doc = app.document("Штатное расписание.xlsx", sha="1" * 64)
    other_doc = app.document("Договоры.xlsx", sha="2" * 64)
    case2 = app.other_case()
    run_a = app.result(app.case, [doc])
    run_b = app.result(case2, [other_doc])
    run_fail = app.result(app.case, [doc], status="нет решения")
    app.main._retire_document(app.db, doc, new_version=2)
    st, log = app.review(run_a)
    expect(st == "review_required" and len(log) == 1 and "заменён версией 2" in log[0]["что"],
           "план на старой версии не отмечен: %s %s" % (st, log), out)
    expect(app.review(run_b) == ("current", []), "независимый план отмечен: %s" % (app.review(run_b),), out)
    expect(app.review(run_fail)[0] != "review_required", "неудачный прогон отмечен к пересмотру", out)
    app.db.expire_all()
    expect(app.db.get(app.d.Run, run_a.id).status == "OPTIMAL" and app.db.get(app.d.Run, run_a.id).summary,
           "отметка стёрла результат", out)
    msgs = [m for m in app.db.query(app.d.Message).filter_by(case_id=app.case.id).all()
            if m.payload and "review_required" in m.payload]
    expect(len(msgs) == 1 and json.loads(msgs[0].payload)["runs"] == [run_a.id], "в ленте нет реплики о пересмотре: %d" % len(msgs), out)
    expect(not [m for m in app.db.query(app.d.Message).filter_by(case_id=case2.id).all()], "реплика о пересмотре ушла в чужой план", out)
    return out


@case("р09", "VER-003: норма отмечает планы со справочником в основании; флажок и условие из чата — только свой план")
def _c09(app):
    out = []
    case2 = app.other_case()
    run_a = app.result(app.case, [])
    run_b = app.result(case2, [])
    run_c = app.result(case2, [], reference=False)
    doc = app.document("Приказ 2556.pdf", sha="d" * 64)
    doc.kind = "приказ"
    doc.gave = json.dumps({"расхождения": [{"pos": "Инженер", "field": "П2556", "old": 110000, "new": 112000}]}, ensure_ascii=False)
    app.db.commit()
    app.run(app.main.document_reference(doc.id, Req({"edits": [{"pos": "Инженер", "field": "П2556", "value": 112000}],
                                                     "document_sha256": "d" * 64})))
    for r, want in ((run_a, 1), (run_b, 1), (run_c, 0)):
        st, log = app.review(r)
        expect(len(log) == want and (st == "review_required") == bool(want),
               "после нормы прогон %d: %s %s" % (r.id, st, log), out)
    expect("справочник изменён" in app.review(run_a)[1][0]["что"] and "Инженер: П2556" in app.review(run_a)[1][0]["что"],
           "причина: %s" % app.review(run_a)[1], out)
    app.run(app.main.mute_document(app.case.id, doc.id, Req({"muted": True})))
    expect(len(app.review(run_a)[1]) == 2 and "исключён из плана" in app.review(run_a)[1][1]["что"],
           "флажок не отметил свой план: %s" % app.review(run_a)[1], out)
    expect(len(app.review(run_b)[1]) == 1, "флажок отметил чужой план", out)
    app.say("допуск трудоёмкости 5 %", {"ok": True, "action": "закрепить", "fixes": [],
                                        "settings": [{"name": "допуск трудоёмкости", "value": "0.05"}], "reply": "Записал."})
    expect(len(app.review(run_a)[1]) == 3 and "условие плана изменено" in app.review(run_a)[1][2]["что"],
           "условие из чата не отметило свой план: %s" % app.review(run_a)[1], out)
    expect(len(app.review(run_b)[1]) == 1 and app.review(run_c) == ("current", []), "условие из чата задело чужой план", out)
    return out


@case("р10", "VER-003: принятые строки и удаление документа отмечают зависимые планы; отметка видна в данных плана и списке планов")
def _c10(app):
    out = []
    case2 = app.other_case()
    doc = app.document("Штатное расписание.xlsx", sha="3" * 64)
    run_a = app.result(app.case, [doc])
    run_b = app.result(case2, [])
    pr = app.proposal(doc, "E7")
    app.run(app.main.decide_proposals(doc.id, Req({"accept": [pr.id], "document_sha256": "3" * 64})))
    for r in (run_a, run_b):
        st, log = app.review(r)
        expect(st == "review_required" and len(log) == 1 and "приняты строки" in log[0]["что"],
               "после принятия строк в общий реестр прогон %d: %s %s" % (r.id, st, log), out)
    app.main._forget_document(app.db, doc)
    expect(len(app.review(run_a)[1]) == 2 and "удалён" in app.review(run_a)[1][1]["что"],
           "удаление не отметило план на документе: %s" % app.review(run_a)[1], out)
    expect(len(app.review(run_b)[1]) == 1, "удаление отметило план, не использовавший документ", out)
    app.db.expire_all()
    state = app.main.case_state(app.db, app.db.get(app.d.Case, app.case.id))
    row = [r for r in state["runs"] if r["id"] == run_a.id][0]
    expect(row["status"] == "OPTIMAL" and row["review"] == "review_required" and len(row["review_log"]) == 2,
           "данные плана: %s" % {k: row[k] for k in ("status", "review", "review_log")}, out)
    cases = {c["id"]: c for c in app.main.list_cases()}
    expect(cases[app.case.id]["review"] == "review_required" and cases[app.case.id]["run_id"] == run_a.id,
           "список планов: %s" % cases.get(app.case.id), out)
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
