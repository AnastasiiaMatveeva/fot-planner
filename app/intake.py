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
from agents import handoff, say, working  # noqa: E402
from db import (  # noqa: E402
    Contract, Document, Employee, Proposal, Question, Substitution, now,
)

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


#: Обработчик -> агент, который его выполняет. Правила замещения читает тот же
#: агент ввода данных, поэтому передачи между агентами там не возникает.
AGENT_OF = {"intake": "intake", "substitutions": "intake", "norms": "norms"}
AGENT_TITLE = {"intake": "Извлечение данных", "norms": "Нормативная база"}

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
    и уходят в расчет отдельным листом входного файла.
    """
    import openpyxl

    with working(db, case.id, "intake", "читает правила замещения «%s»" % doc.name) as w:
        wb = openpyxl.load_workbook(doc.path, data_only=True)
        ws = wb.worksheets[0]
        # Правила общие для организации: новая редакция заменяет прежнюю
        # целиком, а не добавляется к делу.
        db.query(Substitution).delete()
        pairs = 0
        for r in range(2, ws.max_row + 1):
            src = ws.cell(r, 1).value
            dst = ws.cell(r, 2).value
            if not src:
                continue
            db.add(Substitution(position=str(src).strip(),
                                replaced_by=str(dst).strip() if dst else "",
                                source=doc.name, document_id=doc.id))
            pairs += 1
        doc.state = "разобран"
        doc.kind = "правила замещения должностей"
        doc.parsed_by = "разбор по заголовкам"
        doc.summary = "правил %d" % pairs
        w["detail"] = doc.summary
        w["artifact"] = {"файл": doc.name, "правил замещения": pairs,
                         "куда записано": "нормативная база организации",
                         "применяется в расчете": True,
                         "как": "лист «правила_замещения» входного файла; "
                                "правила направленные — кого кем можно "
                                "заместить, не наоборот"}
        unknown = _unknown_positions(db, doc.id)
        if unknown:
            w["artifact"]["не сопоставлено со справочником"] = unknown
        db.commit()

    if unknown:
        # Правило с незнакомым названием не срабатывает и никак себя не
        # проявляет: расчет проходит, просто замещение не применяется. Об этом
        # надо сказать сразу, пока экономист держит документ в руках.
        say(db, case.id,
            "В правилах есть должности, которых нет в справочнике: %s. Такие "
            "правила в расчете не сработают — название должно совпадать со "
            "справочником. Проверьте написание в документе."
            % ", ".join("«%s»" % u for u in unknown[:8]), agent="intake")
        db.commit()

    say(db, case.id,
        "Прочитал «%s»: %d %s замещения должностей. Записал в нормативную базу "
        "организации — правила общие для всех дел, перезагружать их в каждое "
        "не нужно. Правила направленные: слева должность сотрудника, справа "
        "должности, которые ему можно дать дополнительно. В расчет уходят: "
        "работу по такой должности сотрудник выполнить может, обратное — нет."
        % (doc.name, pairs, _plural(pairs, "правило", "правила", "правил")),
        agent="intake")
    db.commit()


def _excel_date(v):
    """Даты в книгах приходят порядковым номером — приводим к читаемому виду."""
    if v is None or v == "":
        return None
    s = str(v).strip()
    try:
        n = float(s)
    except ValueError:
        return s                      # уже строка вида 01.01.2026
    if not (1 <= n <= 80000):
        return s
    import datetime as _dt
    # Excel считает от 30.12.1899: 1900 год ошибочно високосный, эпоха сдвинута.
    return (_dt.date(1899, 12, 30) + _dt.timedelta(days=int(n))).strftime("%d.%m.%Y")


def _store_passport(db, case, passport, doc):
    """Разложить разобранное по реестрам организации.

    Ни штатка, ни договоры не принадлежат плану: договор заключается на
    несколько лет и обслуживает столько же планов, штатка меняется приказами,
    а не с каждым расчетом. Поэтому строки общие, а план на год берет из
    реестра то, что в этом году действует.

    Строки заменяются в пределах одного документа: повторная загрузка того же
    файла обновляет только то, что из него пришло, и не трогает остальное.
    """
    db.query(Employee).filter_by(document_id=doc.id).delete()
    db.query(Contract).filter_by(document_id=doc.id).delete()
    for e in passport.get("employees") or []:
        db.add(Employee(code=str(e.get("code") or ""),
                        fio=e.get("fio"), position=e.get("pos"),
                        rate=e.get("rate"), salary=e.get("sal"),
                        date_from=_excel_date(e.get("from")),
                        date_to=_excel_date(e.get("to")),
                        source=doc.name, document_id=doc.id))
    for c in passport.get("contracts") or []:
        kinds = c.get("kinds")
        db.add(Contract(code=str(c.get("code") or ""),
                        name=c.get("name"), number=c.get("num"), kind=c.get("type"),
                        goz=c.get("goz"), fund=c.get("fot"),
                        kinds=", ".join(kinds) if isinstance(kinds, list) else kinds,
                        date_from=_excel_date(c.get("from")),
                        date_to=_excel_date(c.get("to")),
                        source=doc.name, document_id=doc.id))
    db.commit()


#: Какие форматы читает каждый обработчик. Разбор документов по договору и
#: правил замещения читает ячейки книги: PDF и .doc туда отдавать бессмысленно.
FORMATS = {
    "intake": (".xlsx", ".xlsm", ".xls"),
    "substitutions": (".xlsx", ".xlsm", ".xls"),
    "norms": (".xlsx", ".xlsm", ".xls", ".pdf", ".doc", ".docx"),
}


def _wrong_format(doc, owner):
    """Человеческая причина, если формат обработчику не подходит."""
    ext = os.path.splitext(doc.path)[1].lower()
    allowed = FORMATS.get(owner, ())
    if not allowed or ext in allowed:
        return None
    return ("файл %s, а такие документы читаются только из %s"
            % (ext or "без расширения", ", ".join(allowed)))


# ── документ незнакомой формы ───────────────────────────────────
def _known(db, entity, f):
    """Уже есть такая строка в реестре? Тогда предлагать ее незачем."""
    if entity == "сотрудник":
        code = str(f.get("code") or "").strip()
        fio = str(f.get("fio") or "").strip()
        rows = db.query(Employee).all()
        return any((code and (e.code or "").strip().lower() == code.lower())
                   or (fio and (e.fio or "").strip().lower() == fio.lower())
                   for e in rows)
    if entity == "договор":
        code = str(f.get("code") or "").strip()
        return bool(code) and any((c.code or "").strip().lower() == code.lower()
                                  for c in db.query(Contract).all())
    if entity == "должность":
        # Должность считается известной, только если она уже в справочнике и
        # документ не приносит по ней других величин: перечень должностей
        # обычно совпадает со справочником целиком, а вот новая редакция
        # приказа меняет оклад — про это молчать нельзя.
        import reference
        from fot_planner.position_reference import normalize_position
        name = normalize_position(str(f.get("pos") or "").strip())
        if not name:
            return True
        for row in reference.read_rows():
            if normalize_position(row["pos"]) != name:
                continue
            for key in ("sal", "p2556", "p4"):
                v = f.get(key)
                if v is not None and row.get(key) != v:
                    return False        # величина расходится — предложить
            cat = str(f.get("cat") or "").strip()
            if cat and str(row.get("cat") or "").strip() != cat:
                return False
            return True
        return False                     # должности в справочнике нет
    pos = str(f.get("position") or "").strip().lower()
    return bool(pos) and any((s.position or "").strip().lower() == pos
                             for s in db.query(Substitution).all())


def propose_entities(db, case, doc):
    """Прочитать документ без шаблона и предложить найденное экономисту.

    Записать прочитанное прямо в реестр нельзя: у формы не по шаблону величина
    лежит не там, где ее ждут, и модель может взять оклад из соседней графы.
    Расчет от этого не упадет — он выдаст правдоподобный неверный план, а это
    ГОЗ. Поэтому строки кладутся в отдельную таблицу и ждут, пока экономист
    их посмотрит: до подтверждения их нет ни в реестре, ни в паспорте дела,
    ни во входном файле решателя.

    Возвращает True, если что-то предложено.
    """
    with working(db, case.id, "intake", "перечитывает «%s» без шаблона" % doc.name) as w:
        res = llm.freeform(doc.path, doc.name)
        db.query(Proposal).filter_by(document_id=doc.id, state="предложено").delete()

        if not res.get("ok"):
            w["detail"] = res.get("error") or "не разобрал"
            db.commit()
            return False

        buckets = (("employees", "сотрудник"), ("contracts", "договор"),
                   ("substitutions", "правило замещения"),
                   ("positions", "должность"))
        counts, skipped = {}, 0
        for key, entity in buckets:
            counts[entity] = 0
            for item in res.get(key) or []:
                where = item.pop("место", None)
                if _known(db, entity, item):
                    # Знакомый обработчик уже записал эту строку — предлагать
                    # ее второй раз значит просить подтвердить проверенное.
                    skipped += 1
                    continue
                db.add(Proposal(document_id=doc.id, entity=entity,
                                payload=json.dumps(item, ensure_ascii=False),
                                evidence=str(where)[:400] if where else None))
                counts[entity] += 1
        total = sum(counts.values())
        w["detail"] = ("предложено %d, уже в реестре %d" % (total, skipped)
                       if total or skipped else "сверх разобранного ничего")
        w["artifact"] = {"файл": doc.name, "прочитано частями": res.get("parts"),
                         "предложено строк": total,
                         "пропущено как уже известные": skipped,
                         "в расчет пойдет": "только после подтверждения"}
        if total:
            doc.state = "ждет подтверждения"
            doc.summary = ", ".join("%s %d" % (k, v) for k, v in counts.items() if v)
        db.commit()

    if not total:
        return False

    say(db, case.id,
        "В «%s» нашлось сверх разобранного: %s. В реестр и в расчет это пока "
        "не пошло — откройте документ в реестре, проверьте строки и "
        "подтвердите те, что верны. У каждой написано, откуда она взята."
        % (doc.name, doc.summary), agent="intake")
    db.commit()
    return True


def _ask_kind(db, case, doc):
    """Спросить вид документа — когда прочитать содержимое не удалось."""
    db.add(Question(
        case_id=case.id, agent="intake",
        text="Что за документ «%s»? Прочитал его, но по содержанию это не "
             "похоже ни на один вид, с которым я работаю." % doc.name,
        options=json.dumps(["документ по договору", "нормативный документ",
                            "правила замещения должностей",
                            "не нужен, удалить"], ensure_ascii=False)))
    say(db, case.id,
        "Прочитал «%s», но не понял, что это за документ, и строк из него не "
        "достал. Подскажите вид — разберу заново." % doc.name, agent="intake")
    db.commit()


def _unknown_positions(db, document_id):
    """Замещающие должности, которых нет в справочнике.

    Такое правило не срабатывает и молчит: расчет проходит, замещение просто
    не применяется. Причина обычно в написании — «Инженер 1 кат.» вместо
    «Инженер 1 категории» — или в опечатке источника.

    Варианты «в другом подразделении» не считаем ошибкой: это та же должность,
    и обычная строка с этим названием правило уже покрывает.
    """
    import reference
    from fot_planner.position_reference import normalize_position

    known = {normalize_position(r["pos"]) for r in reference.read_rows()}
    bad = []
    for s in db.query(Substitution).filter_by(document_id=document_id).all():
        for name in str(s.replaced_by or "").replace(";", ",").split(","):
            name = name.strip()
            if not name or "в другом подразделении" in name.lower():
                continue
            if normalize_position(name) not in known and name not in bad:
                bad.append(name)
    return bad


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
        w["artifact"] = {"файл": doc.name, "сотрудников": emp, "договоров": ctr,
                         "прочитано в документе": out.get("log") or [],
                         "спорных значений": len(out.get("questions") or []),
                         "готово к расчету": bool((out.get("ready") or {}).get("ok"))}
        _store_passport(db, case, passport, doc)
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
        if res.get("ok"):
            rows, by = res["rows"], "модель " + res["model"]
        elif docread.kind_of(doc.path) in (".xlsx", ".xlsm", ".xls"):
            # Запасной разбор читает ячейки книги — PDF и .doc ему не по зубам.
            rows, by = reference.rows_by_anchors(doc.path), "разбор по заголовкам"
        else:
            raise ValueError(res.get("error") or "документ не разобран")
        changes, unknown, seen = reference.compare(rows)
        doc.state = "разобран"
        doc.parsed_by = by
        doc.summary = "расхождений %d" % len(changes)
        w["detail"] = doc.summary
        w["artifact"] = {"файл": doc.name, "чем разобрано": by,
                         "извлечено величин": len(rows or []),
                         "величины": sorted(seen) or [],
                         "основание": res.get("basis"),
                         "действует с": res.get("effective_from"),
                         "расхождений со справочником": len(changes),
                         "должностей вне справочника": len(unknown),
                         "примечание модели": res.get("notes")}
        db.commit()

    if not changes:
        say(db, case.id,
            "Сверил «%s» со справочником — расхождений нет." % doc.name, agent="norms")
        db.commit()
        return

    say(db, case.id,
        "В «%s» нашел %d %s со справочником организации. Он общий для всех дел, "
        "поэтому изменения затронут и другие планы. Показываю, что изменится; "
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
        w["artifact"] = {"файл": doc.name, "формат": docread.kind_of(doc.path),
                         "размер, байт": doc.size, "определен вид": kind,
                         "чем определен": by or "не удалось прочитать",
                         "передан агенту": AGENT_TITLE.get(AGENT_OF.get(owner), "—")}
        db.commit()

    # Два разных отказа, и путать их нельзя. Файл, который не читается,
    # не станет читаемым от того, что экономист назовет его вид: спрашивать
    # тут нечего, надо сказать, чем помочь. Вопрос уместен только когда
    # документ прочитан, а вид непонятен — тогда ответ меняет обработчик.
    if by is None:
        doc.state = "не прочитан"
        doc.summary = kind                      # без by в kind лежит причина
        db.commit()
        say(db, case.id, "Не смог прочитать «%s». %s" % (doc.name, kind),
            agent="intake")
        db.commit()
        return

    if owner is None:
        # Форма незнакомая — это не повод не читать документ. Раньше здесь
        # разбор кончался вопросом «что это?», и документ не давал ничего.
        doc.kind = kind
        doc.parsed_by = by
        doc.state = "не распознан"
        db.commit()
        if not propose_entities(db, case, doc):
            _ask_kind(db, case, doc)
        return

    doc.kind = kind
    doc.parsed_by = by
    db.commit()

    # Формат проверяем до разбора. Иначе openpyxl роняет PDF и его английское
    # «does not support .pdf file format» уходит прямо в реестр — финансисту
    # это не сообщение, а шум.
    bad = _wrong_format(doc, owner)
    if bad:
        doc.state = "не распознан"
        doc.summary = bad
        db.commit()
        say(db, case.id,
            "«%s»: %s. Прочитаю его без шаблона." % (doc.name, bad), agent="intake")
        db.commit()
        if not propose_entities(db, case, doc):
            doc.summary = "%s; сверх этого данных не нашлось" % bad
            db.commit()
        return

    target = AGENT_OF.get(owner, owner)
    if target != "intake":
        handoff(db, case.id, "intake", target,
                "«%s» — это %s, передаю агенту «%s»."
                % (doc.name, kind, AGENT_TITLE.get(target, target)),
                payload={"kind": "handoff", "document": doc.name, "as": kind})

    try:
        if owner == "norms":
            run_norms(db, case, doc)
        elif owner == "substitutions":
            run_substitutions(db, case, doc)
        else:
            run_intake(db, case, doc)
        # Вид документа не обещает, что в нем нет ничего сверх этого вида.
        # Служебная записка проходит как нормативный документ, а внутри —
        # трое сотрудников и договор, и знакомый обработчик их не заметит.
        # Поэтому после него документ читается еще раз, уже без шаблона.
        propose_entities(db, case, doc)
    except Exception as exc:  # noqa: BLE001 — сообщение вместо падения фона
        # Текст исключения — для ленты и карточки, но не для графы реестра:
        # там нужно состояние, а не английская диагностика библиотеки.
        doc.state = "не распознан"
        doc.summary = "разбор не удался"
        db.commit()
        say(db, case.id, "Не смог разобрать «%s»: %s" % (doc.name, exc), agent=owner)
        db.commit()
