# -*- coding: utf-8 -*-
"""Работа агентов над загруженными документами.

Здесь живет то, что экономист видит в ленте: агент получает файл, определяет,
что это, разбирает и отчитывается. Разбор не изобретается заново — берется
рабочий код: якорный разбор РКМ и структур цены из ``extract`` и разбор
нормативных документов моделью из ``llm``.

Маршрутизация простая и намеренно объяснимая: по содержимому книги решается,
чей это документ — агента ввода данных или агента нормативной базы. Если
уверенности нет, документ не пропадает молча: агент задает вопрос.
"""
from __future__ import annotations

import json
import os
import sys

import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
# Разбор РКМ и структур цены уже написан и проверен на образцах экономистов.
sys.path.insert(0, os.path.join(ROOT, "docs", "ui"))

import extract          # noqa: E402
import llm              # noqa: E402
from agents import say, working  # noqa: E402
from db import Document, Question, now  # noqa: E402

# Слова, по которым книга опознается как нормативный документ, а не как
# расчетно-калькуляционные материалы по договору.
NORM_HINTS = ("приказ", "оклад", "предельн", "положение об оплате",
              "должностной оклад", "2556", "тарифн")
CONTRACT_HINTS = ("ркм", "калькуляц", "структура цены", "трудоемк",
                  "договор", "шифр", "смета", "форма 1", "форма 9")


def _peek(path, limit=4000):
    """Первые ячейки книги текстом — чтобы понять, что это за документ."""
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception:  # noqa: BLE001
        return "", []
    names = list(wb.sheetnames)
    chunks = [" ".join(names)]
    size = 0
    for ws in wb.worksheets[:4]:
        for row in ws.iter_rows(min_row=1, max_row=40, max_col=20, values_only=True):
            line = " ".join(str(v) for v in row if v is not None)
            if not line:
                continue
            chunks.append(line)
            size += len(line)
            if size > limit:
                break
        if size > limit:
            break
    wb.close()
    return " ".join(chunks).lower(), names


def classify(path):
    """(вид документа, чей он, насколько уверены)."""
    text, _sheets = _peek(path)
    if not text:
        return "не прочитан", None, 0.0
    norm = sum(1 for w in NORM_HINTS if w in text)
    contract = sum(1 for w in CONTRACT_HINTS if w in text)
    if norm == contract == 0:
        return "не опознан", None, 0.0
    if norm > contract:
        return "нормативный документ", "norms", norm / (norm + contract)
    return "документ по договору", "intake", contract / (norm + contract)


# ── агент 1: данные договоров ───────────────────────────────────
def run_intake(db, case, doc):
    """Разобрать документ по договору и доложить в ленту."""
    with working(db, case.id, "intake", "разбирает «%s»" % doc.name) as w:
        out = extract.extract(doc.path)
        passport = out["passport"]
        case.passport = json.dumps(passport, ensure_ascii=False)
        doc.state = "разобран"
        doc.kind = "документ по договору"
        doc.parsed_by = "разбор по заголовкам"
        emp = len(passport.get("employees") or [])
        ctr = len(passport.get("contracts") or [])
        doc.summary = "сотрудников %d, договоров %d" % (emp, ctr)
        w["detail"] = doc.summary
        db.commit()

    ready = out.get("ready") or {}
    say(db, case.id,
        "Разобрал «%s». Нашел сотрудников — %d, договоров — %d." % (doc.name, emp, ctr),
        agent="intake",
        payload={"kind": "passport", "employees": emp, "contracts": ctr,
                 "log": out.get("log") or []})

    for q in (out.get("questions") or [])[:5]:
        text = q if isinstance(q, str) else (q.get("text") or str(q))
        opts = None if isinstance(q, str) else q.get("options")
        db.add(Question(case_id=case.id, agent="intake", text=text,
                        options=json.dumps(opts, ensure_ascii=False) if opts else None))
    db.commit()

    if ready.get("ok"):
        case.stage = "готово к расчету"
        say(db, case.id, "Данных достаточно для расчета. Могу считать.",
            agent="intake", payload={"kind": "offer_solve"})
    else:
        missing = ", ".join(ready.get("missing") or []) or "часть показателей"
        say(db, case.id, "Для расчета не хватает: %s. %s" % (missing, ready.get("hint") or ""),
            agent="intake")
    db.commit()


# ── агент 7: нормативная база ───────────────────────────────────
def run_norms(db, case, doc):
    """Сверить нормативный документ со справочником и предложить изменения."""
    import reference

    with working(db, case.id, "norms", "сверяет «%s» со справочником" % doc.name) as w:
        res = llm.parse(doc.path, doc.name)
        by = "модель " + res["model"] if res.get("ok") else "разбор по заголовкам"
        rows = res.get("rows") if res.get("ok") else None
        if rows is None:
            rows = reference.rows_by_anchors(doc.path)
            by = "разбор по заголовкам"
        changes, unknown, seen = reference.compare(rows)
        doc.state = "разобран"
        doc.kind = "нормативный документ"
        doc.parsed_by = by
        doc.summary = "расхождений %d" % len(changes)
        w["detail"] = doc.summary
        db.commit()

    if not changes:
        say(db, case.id,
            "Сверил «%s» со справочником — расхождений нет." % doc.name, agent="norms")
        db.commit()
        return

    say(db, case.id,
        "В «%s» нашел %d %s со справочником. Показываю, что изменится; "
        "запишу только после вашего подтверждения."
        % (doc.name, len(changes), _plural(len(changes), "расхождение", "расхождения", "расхождений")),
        agent="norms",
        payload={"kind": "reference_diff", "changes": changes, "unknown": unknown,
                 "basis": res.get("basis"), "effective_from": res.get("effective_from"),
                 "by": by})
    db.commit()


def _plural(n, one, few, many):
    d, h = n % 10, n % 100
    if 11 <= h <= 14:
        return many
    if d == 1:
        return one
    if 2 <= d <= 4:
        return few
    return many


def handle_document(db, case, doc):
    """Определить, чей документ, и передать нужному агенту."""
    kind, owner, confidence = classify(doc.path)
    if owner is None:
        doc.state = "не распознан"
        doc.kind = kind
        db.commit()
        db.add(Question(
            case_id=case.id, agent="intake",
            text="Не понял, что за документ «%s». Что это?" % doc.name,
            options=json.dumps(["документ по договору", "нормативный документ",
                                "не нужен, удалить"], ensure_ascii=False)))
        say(db, case.id,
            "Не смог определить вид документа «%s» — подскажите, что это." % doc.name,
            agent="intake")
        db.commit()
        return

    try:
        if owner == "norms":
            run_norms(db, case, doc)
        else:
            run_intake(db, case, doc)
    except Exception as exc:  # noqa: BLE001 — сообщение вместо падения фона
        doc.state = "не распознан"
        doc.summary = str(exc)[:300]
        db.commit()
        say(db, case.id, "Не смог разобрать «%s»: %s" % (doc.name, exc), agent=owner)
        db.commit()
