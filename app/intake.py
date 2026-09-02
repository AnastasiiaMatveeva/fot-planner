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
import re
import sys

import docread

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
# Разбор РКМ и структур цены уже написан и проверен на образцах экономистов.
sys.path.insert(0, os.path.join(ROOT, "docs", "ui"))

import extract          # noqa: E402
import llm              # noqa: E402
from agents import say, working  # noqa: E402
from db import Contract, Document, Employee, Question, Substitution, now  # noqa: E402

# Слова, по которым книга опознается как нормативный документ, а не как
# расчетно-калькуляционные материалы по договору.
SUBST_HINTS = ("исходная должность", "может быть замещена", "замещение",
               "взаимозаменяем")
NORM_HINTS = ("приказ", "оклад", "предельн", "положение об оплате",
              "должностной оклад", "2556", "тарифн")
CONTRACT_HINTS = ("ркм", "калькуляц", "структура цены", "трудоемк",
                  "договор", "шифр", "смета", "форма 1", "форма 9")


def _peek(path, limit=4000):
    """Начало документа текстом — чтобы понять, что это.

    Раньше умели заглядывать только в книги Excel, но нормативные документы
    приходят PDF и старым .doc, а решать, чей это документ, надо и по ним.
    """
    try:
        text = docread.to_text(path, limit)
    except docread.Unreadable:
        return "", None
    return text.lower(), None


#: Вид документа -> кто его обрабатывает. «штатное расписание» и документы по
#: договору идут одному агенту: разбор у них общий.
OWNER = {
    "документ по договору": "intake",
    "штатное расписание": "intake",
    "нормативный документ": "norms",
    "правила замещения должностей": "substitutions",
}


def classify_by_words(path):
    """Запасной способ: счет ключевых слов. Работает, когда модель недоступна."""
    text, _ = _peek(path)
    if not text:
        return "не прочитан", None, 0.0
    flat = re.sub(r"\s+", " ", text)          # в шапках попадаются переносы строк
    if sum(1 for w in SUBST_HINTS if w in flat) >= 2:
        return "правила замещения должностей", "substitutions", 1.0
    norm = sum(1 for w in NORM_HINTS if w in flat)
    contract = sum(1 for w in CONTRACT_HINTS if w in flat)
    if norm == contract == 0:
        return "не опознан", None, 0.0
    if norm > contract:
        return "нормативный документ", "norms", norm / (norm + contract)
    return "документ по договору", "intake", contract / (norm + contract)


def classify(path, filename=""):
    """(вид документа, чей он, чем определили).

    Вид определяет модель: счет ключевых слов на настоящих документах путается
    — в приказе об оплате труда слово «договор» встречается не реже, чем в
    расчетно-калькуляционных материалах. Если модель недоступна или не смогла,
    остается счет слов.
    """
    res = llm.classify(path, filename)
    if res.get("ok"):
        kind = res["kind"]
        if kind == "иное":
            return kind, None, "модель " + res["model"]
        return kind, OWNER.get(kind), "модель " + res["model"]

    if res.get("unavailable"):
        kind, owner, _ = classify_by_words(path)
        return kind, owner, "разбор по заголовкам"

    # Файл не прочитан — про это надо сказать прямо, а не гадать по словам.
    return res.get("error") or "не прочитан", None, None


# ── правила замещения должностей ────────────────────────────────
def run_substitutions(db, case, doc):
    """Прочитать таблицу «кого кем можно заместить» и положить в дело.

    Эти правила направленные: главного инженера проекта можно заместить
    инженером, обратное неверно. Модель сервиса пока оперирует симметричными
    группами взаимозаменяемости, поэтому правила сохраняются и показываются,
    но в расчет не подставляются — об этом агент говорит прямо.
    """
    import openpyxl

    with working(db, case.id, "intake", "читает правила замещения «%s»" % doc.name) as w:
        wb = openpyxl.load_workbook(doc.path, data_only=True)
        ws = wb.worksheets[0]
        db.query(Substitution).filter_by(case_id=case.id).delete()
        pairs = 0
        for r in range(2, ws.max_row + 1):
            src = ws.cell(r, 1).value
            dst = ws.cell(r, 2).value
            if not src:
                continue
            db.add(Substitution(case_id=case.id, position=str(src).strip(),
                                replaced_by=str(dst).strip() if dst else "",
                                source=doc.name))
            pairs += 1
        doc.state = "разобран"
        doc.kind = "правила замещения должностей"
        doc.parsed_by = "разбор по заголовкам"
        doc.summary = "правил %d" % pairs
        w["detail"] = doc.summary
        db.commit()

    say(db, case.id,
        "Прочитал «%s»: %d %s замещения должностей. Правила направленные — "
        "кого кем можно заменить, не наоборот. Модель расчета пока работает "
        "симметричными группами взаимозаменяемости, поэтому эти правила "
        "сохранены и доступны для просмотра, но в расчет не подставляются."
        % (doc.name, pairs, _plural(pairs, "правило", "правила", "правил")),
        agent="intake")
    db.commit()


def _store_passport(db, case, passport, source):
    """Разложить разобранное по таблицам, чтобы это можно было открыть и читать.

    Паспорт остается в деле целиком — из него собирается вход решателя. Но для
    просмотра нужны обычные строки: сотрудники и договоры, отсортированные,
    с указанием, из какого документа взяты. Пересобираем их заново на каждый
    разбор: документ — источник истины, ручные правки идут через ленту.
    """
    db.query(Employee).filter_by(case_id=case.id).delete()
    db.query(Contract).filter_by(case_id=case.id).delete()
    for e in passport.get("employees") or []:
        db.add(Employee(case_id=case.id, code=str(e.get("code") or ""),
                        fio=e.get("fio"), position=e.get("pos"),
                        rate=e.get("rate"), salary=e.get("sal"),
                        date_from=_excel_date(e.get("from")),
                        date_to=_excel_date(e.get("to")), source=source))
    for c in passport.get("contracts") or []:
        kinds = c.get("kinds")
        db.add(Contract(case_id=case.id, code=str(c.get("code") or ""),
                        name=c.get("name"), number=c.get("num"), kind=c.get("type"),
                        goz=c.get("goz"), fund=c.get("fot"),
                        kinds=", ".join(kinds) if isinstance(kinds, list) else kinds,
                        source=source))
    db.commit()


# ── агент 1: данные договоров ───────────────────────────────────
def run_intake(db, case, doc):
    """Разобрать документ по договору и доложить в ленту."""
    with working(db, case.id, "intake", "разбирает «%s»" % doc.name) as w:
        out = extract.extract(doc.path)
        passport = out["passport"]
        case.passport = json.dumps(passport, ensure_ascii=False)
        doc.state = "разобран"
        emp = len(passport.get("employees") or [])
        ctr = len(passport.get("contracts") or [])
        doc.summary = "сотрудников %d, договоров %d" % (emp, ctr)
        w["detail"] = doc.summary
        _store_passport(db, case, passport, doc.name)
        db.commit()

    ready = out.get("ready") or {}
    say(db, case.id,
        "Разобрал «%s». Нашел сотрудников — %d, договоров — %d." % (doc.name, emp, ctr),
        agent="intake",
        payload={"kind": "passport", "employees": emp, "contracts": ctr,
                 "log": out.get("log") or []})

    for q in (out.get("questions") or [])[:5]:
        text, opts = _question_text(q)
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


def _question_text(q):
    """Вопрос разборщика — во фразу, которую можно прочитать.

    extract возвращает объект с полями «где нашли», «что было в документе» и
    «почему не сошлось». Показывать его как есть нельзя: экономист видит
    словарь вместо вопроса.
    """
    if isinstance(q, str):
        return q, None
    if not isinstance(q, dict):
        return str(q), None
    parts = []
    if q.get("field"):
        parts.append("%s" % q["field"])
    if q.get("raw"):
        parts.append("в документе: %s" % q["raw"])
    if q.get("why"):
        parts.append(str(q["why"]))
    text = ". ".join(parts) if parts else (q.get("text") or str(q))
    return text, q.get("options")


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
    with working(db, case.id, "intake", "определяет вид «%s»" % doc.name) as w:
        kind, owner, by = classify(doc.path, doc.name)
        w["detail"] = kind
        db.commit()

    if owner is not None:
        doc.kind = kind
    if owner is None:
        doc.state = "не распознан"
        doc.kind = kind if by else None
        doc.parsed_by = by
        doc.summary = None if by else kind      # без by в kind лежит причина
        db.commit()
        db.add(Question(
            case_id=case.id, agent="intake",
            text="Не понял, что за документ «%s». Что это?" % doc.name,
            options=json.dumps(["документ по договору", "нормативный документ",
                                "правила замещения должностей",
                                "не нужен, удалить"], ensure_ascii=False)))
        say(db, case.id,
            ("Не смог прочитать «%s»: %s" % (doc.name, kind)) if not by
            else ("Определил «%s» как «%s» — с этим видом пока не работаю. "
                  "Подскажите, что это." % (doc.name, kind)),
            agent="intake")
        db.commit()
        return
    doc.parsed_by = by

    try:
        if owner == "norms":
            run_norms(db, case, doc)
        elif owner == "substitutions":
            run_substitutions(db, case, doc)
        else:
            run_intake(db, case, doc)
    except Exception as exc:  # noqa: BLE001 — сообщение вместо падения фона
        doc.state = "не распознан"
        doc.summary = str(exc)[:300]
        db.commit()
        say(db, case.id, "Не смог разобрать «%s»: %s" % (doc.name, exc), agent=owner)
        db.commit()
