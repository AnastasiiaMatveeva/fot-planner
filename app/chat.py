# -*- coding: utf-8 -*-
"""Ответ агента на реплику экономиста.

Раньше любая реплика записывалась как ответ на первый неотвеченный вопрос и
получала «Принял: ...». Это неверно вдвойне: экономист чаще спрашивает, чем
отвечает, и даже настоящий ответ требует действия, а не квитанции.

Здесь реплика идет в модель вместе с состоянием плана — какие документы
загружены, что из них извлечено, чего не хватает, что спрашивал агент. Модель
отвечает по существу и, если нужно, называет действие: переразобрать документ
как другой вид, запустить расчет, ответить на свой же вопрос. Действия
выполняет сервис, а не модель: она только предлагает.
"""
from __future__ import annotations

import json

import llm
from agents import AGENTS

#: Что агент умеет сделать по итогам разговора. Список закрытый: модель
#: выбирает из него, а выполняет сервис.
ACTIONS = ("ничего", "переразобрать документ", "запустить расчет",
           "ответить на вопрос агента", "заполнить поле")

#: Что экономист может продиктовать в чате. Список закрытый и по одной
#: причине: модель называет поле словами, а писать в базу по слову от модели
#: нельзя. Здесь слово переводится в столбец строки реестра и в ключ паспорта,
#: по которому собирается вход решателя, — писать надо в оба места, иначе
#: правка либо не видна в таблице, либо не доходит до расчета.
FIELDS = {
    "договор": {
        "ГОЗ": ("goz", "goz"),
        "фонд": ("fund", "fot"),
        "наименование": ("name", "name"),
        "номер": ("number", "num"),
        "вид": ("kind", "type"),
        "действует с": ("date_from", "from"),
        "действует по": ("date_to", "to"),
        "разрешенные выплаты": ("kinds", "kinds"),
    },
    "сотрудник": {
        "должность": ("position", "pos"),
        "ставка": ("rate", "rate"),
        "оклад": ("salary", "sal"),
        "работает с": ("date_from", "from"),
        "работает по": ("date_to", "to"),
    },
}

#: Поля, где значение — число, а не текст.
NUMERIC = {"фонд", "ставка", "оклад"}

SYSTEM = """Ты — агент сервиса планирования фонда оплаты труда. Отвечаешь
экономисту в рабочей ленте плана.

Как отвечать:
- по существу и коротко, две-три фразы, деловым языком;
- опирайся только на состояние плана, которое дано ниже, ничего не выдумывай;
- если экономист говорит, что чего-то не понял, объясни своими словами, а не
  повторяй прежнюю формулировку;
- если он подсказывает, что за документ, предложи переразобрать этот документ
  как указанный вид — но только если документ вообще прочитан. Документ в
  состоянии «не прочитан» от указания вида читаемым не станет: тут надо
  попросить приложить его в текстовом виде или ввести величины вручную;
- если данных для расчета не хватает, скажи, каких именно и откуда их взять;
- не обещай того, чего сервис не делает.

Форма бывает не по шаблону, и тогда часть величин из нее не извлеклась. Если
экономист сам называет величину — «договор 1234 ГОЗ», «по 0421 фонд 3 млн»,
«у Петрова оклад 90 000», — выбери действие «заполнить поле» и перечисли
правки в edits. Что можно заполнить:
- у договора: ГОЗ, фонд, наименование, номер, вид, действует с, действует по,
  разрешенные выплаты. «ГОЗ» — это признак «да» или «нет»: гособоронзаказ это
  или обычная работа. «Вид» — совсем другое: ОКР, НИР, поставка. Фраза
  «договор 1234 — это ГОЗ» означает поле ГОЗ со значением «да», а не вид;
- у сотрудника: должность, ставка, оклад, работает с, работает по.
Ключ — шифр договора или табельный номер сотрудника, ровно так, как назвал
экономист. Ничего не додумывай: заполняй только то, что он сказал вслух. Если
он называет величину, которой в этом списке нет, скажи прямо, что такое поле
через чат не заполняется.

Чего сервис пока не умеет, и об этом надо говорить прямо: распознавать сканы
без текстового слоя; извлекать таблицы из старых файлов .doc; подставлять
в расчет направленные правила замещения должностей."""

SHAPE = """Ответь одним объектом JSON:
{"reply": "что сказать экономисту",
 "action": "ничего | переразобрать документ | запустить расчет | ответить на вопрос агента",
 "document": "имя документа, если действие относится к нему, иначе null",
 "as_kind": "вид документа для переразбора, иначе null",
 "answer": "если это ответ на вопрос агента — его суть, иначе null",
 "edits": [{"entity": "договор | сотрудник",
            "key": "шифр договора или табельный номер",
            "field": "название поля из списка выше",
            "value": "значение словами или числом"}]}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "action": {"type": "string", "enum": list(ACTIONS)},
        "document": {"type": ["string", "null"]},
        "as_kind": {"type": ["string", "null"], "enum": list(llm.DOC_KINDS) + [None]},
        "answer": {"type": ["string", "null"]},
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "enum": list(FIELDS)},
                    "key": {"type": "string"},
                    "field": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["entity", "key", "field", "value"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["reply", "action"],
    "additionalProperties": False,
}


def context(db, case):
    """Состояние плана словами — то, на что модель может опереться."""
    from db import Contract, Document, Employee, Question, Run, Substitution

    lines = ["План: %s, год %s, этап: %s." % (case.title, case.year, case.stage)]

    docs = db.query(Document).filter_by(case_id=case.id).order_by(Document.id).all()
    if docs:
        lines.append("Загруженные документы:")
        for d in docs:
            lines.append("- «%s»: %s%s%s" % (
                d.name, d.state,
                ", вид: %s" % d.kind if d.kind else "",
                ", %s" % d.summary if d.summary else ""))
    else:
        lines.append("Документов пока нет.")

    emp = db.query(Employee).filter_by(case_id=case.id).count()
    ctr = db.query(Contract).filter_by(case_id=case.id).count()
    sub = db.query(Substitution).filter_by(case_id=case.id).count()
    lines.append("Собрано: сотрудников %d, договоров %d, правил замещения %d."
                 % (emp, ctr, sub))

    open_q = (db.query(Question).filter_by(case_id=case.id, answer=None)
              .order_by(Question.id).all())
    if open_q:
        lines.append("Агент ждет ответа на вопросы:")
        for q in open_q:
            opts = json.loads(q.options) if q.options else None
            lines.append("- %s%s" % (q.text, " Варианты: %s." % ", ".join(opts) if opts else ""))

    runs = db.query(Run).filter_by(case_id=case.id).order_by(Run.id.desc()).limit(3).all()
    if runs:
        lines.append("Расчеты: " + "; ".join(
            "№%d %s%s" % (r.id, r.status, " за %s с" % r.seconds if r.seconds else "")
            for r in runs))
    else:
        lines.append("Расчет не запускался.")

    lines.append("Агенты сервиса: " + ", ".join(
        "%d %s%s" % (a["n"], a["name"], "" if a["real"] else " (не реализован)")
        for a in sorted(AGENTS.values(), key=lambda x: x["n"])))
    return "\n".join(lines)


def history(db, case, limit=12):
    """Последние реплики — чтобы агент не отвечал в отрыве от разговора."""
    from db import Message
    msgs = (db.query(Message).filter_by(case_id=case.id)
            .order_by(Message.id.desc()).limit(limit).all())
    out = []
    for m in reversed(msgs):
        who = "Экономист" if m.who == "экономист" else "Агент"
        out.append("%s: %s" % (who, m.text))
    return "\n".join(out)


DOC_NOTE = """Сейчас разговор идет не о плане, а об одном документе из реестра:
экономист смотрит, что из него извлечено, и поправляет. Если он говорит, что
величина или строка неверна, — выбери «заполнить поле» с верными значениями,
если он их назвал; если не назвал — спроси, как верно, коротко. Если он
говорит, что документ не того вида, — «переразобрать документ». Расчет из
этого разговора не запускается."""


def doc_context(db, doc):
    """Состояние документа словами: что извлечено, что ждет подтверждения."""
    from db import Contract, Employee, Inflow, LaborRow, Proposal, Substitution

    lines = ["Документ «%s», вид: %s, состояние: %s." % (doc.name, doc.kind or "не определен",
                                                         doc.state)]
    emp = db.query(Employee).filter_by(document_id=doc.id).all()
    ctr = db.query(Contract).filter_by(document_id=doc.id).all()
    if emp:
        lines.append("Сотрудники из документа: " + "; ".join(
            "%s %s, %s, ставка %s, зарплата %s" % (e.code, e.fio, e.position, e.rate, e.salary)
            for e in emp[:20]))
    if ctr:
        lines.append("Договоры из документа: " + "; ".join(
            "%s %s, фонд %s, ГОЗ %s, виды выплат %s" % (c.code, c.name, c.fund, c.goz, c.kinds)
            for c in ctr[:20]))
    n_lab = db.query(LaborRow).filter_by(document_id=doc.id).count()
    n_inf = db.query(Inflow).filter_by(document_id=doc.id).count()
    n_sub = db.query(Substitution).filter_by(document_id=doc.id).count()
    lines.append("Еще из документа: строк трудоемкости %d, поступлений %d, правил "
                 "замещения %d." % (n_lab, n_inf, n_sub))
    pend = db.query(Proposal).filter_by(document_id=doc.id, state="предложено").count()
    if pend:
        lines.append("Ждут подтверждения экономиста: %d строк." % pend)
    return "\n".join(lines)


def doc_history(db, doc, limit=10):
    from db import Message
    msgs = (db.query(Message).filter_by(document_id=doc.id)
            .order_by(Message.id.desc()).limit(limit).all())
    return "\n".join("%s: %s" % ("Экономист" if m.who == "экономист" else "Агент", m.text)
                      for m in reversed(msgs))


def reply_doc(db, doc, text):
    """Ответ агента в разговоре о документе. ok=False — модель недоступна."""
    name, model, why = llm.provider()
    if name is None:
        return {"ok": False, "error": why}
    user = ("Состояние документа:\n%s\n\nПоследние реплики:\n%s\n\n"
            "Новая реплика экономиста: %s" % (doc_context(db, doc), doc_history(db, doc), text))
    try:
        if name == "anthropic":
            raw = _ask_anthropic(user, model, SYSTEM + "\n\n" + DOC_NOTE)
        else:
            raw = llm._call_openai_compatible(
                user, doc.name, model, llm._endpoint(name), api_key=llm._key(name),
                schema=llm._use_schema(name), no_thinking=llm._no_thinking(name),
                insecure=llm._truthy(__import__("os").environ.get("FOT_LLM_INSECURE_TLS")),
                system=SYSTEM + "\n\n" + DOC_NOTE, shape=SHAPE, json_schema=SCHEMA,
                schema_name="chat_reply")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    if not raw or not raw.get("reply"):
        return {"ok": False, "error": "модель вернула пустой ответ"}
    action = raw.get("action")
    edits = raw.get("edits")
    return {"ok": True, "reply": raw["reply"].strip(),
            "action": action if action in ACTIONS else "ничего",
            "as_kind": raw.get("as_kind") or None,
            "edits": edits if isinstance(edits, list) else []}


def reply(db, case, text):
    """Ответ на реплику. Возвращает dict; ok=False — модель недоступна."""
    name, model, why = llm.provider()
    if name is None:
        return {"ok": False, "error": why}

    user = ("Состояние дела:\n%s\n\nПоследние реплики:\n%s\n\n"
            "Новая реплика экономиста: %s" % (context(db, case), history(db, case), text))
    try:
        if name == "anthropic":
            raw = _ask_anthropic(user, model)
        else:
            raw = llm._call_openai_compatible(
                user, case.title, model, llm._endpoint(name), api_key=llm._key(name),
                schema=llm._use_schema(name), no_thinking=llm._no_thinking(name),
                insecure=llm._truthy(__import__("os").environ.get("FOT_LLM_INSECURE_TLS")),
                system=SYSTEM, shape=SHAPE, json_schema=SCHEMA, schema_name="chat_reply")
    except Exception as e:  # noqa: BLE001 — сеть, лимиты, битый JSON
        return {"ok": False, "error": str(e)}
    if not raw or not raw.get("reply"):
        return {"ok": False, "error": "модель вернула пустой ответ"}

    action = raw.get("action")
    edits = raw.get("edits")
    return {"ok": True, "reply": raw["reply"].strip(),
            "action": action if action in ACTIONS else "ничего",
            "document": raw.get("document") or None,
            "as_kind": raw.get("as_kind") or None,
            "answer": raw.get("answer") or None,
            "edits": edits if isinstance(edits, list) else []}


def _ask_anthropic(user, model, system=None):
    from typing import Literal, Optional

    import anthropic
    from pydantic import BaseModel

    class Reply(BaseModel):
        reply: str
        action: Literal["ничего", "переразобрать документ", "запустить расчет",
                        "ответить на вопрос агента"]
        document: Optional[str] = None
        as_kind: Optional[str] = None
        answer: Optional[str] = None

    msg = anthropic.Anthropic().messages.parse(
        model=model, max_tokens=1500, system=system or SYSTEM,
        messages=[{"role": "user", "content": user}], output_format=Reply)
    return msg.parsed_output.model_dump() if msg.parsed_output else None


# ── правки, продиктованные в чате ────────────────────────────────────────
#
# Форма не по шаблону — обычное дело: величина в ней есть, но лежит не там,
# где ее ищет разборщик, и в реестр не попадает. Раньше выхода не было
# вообще: чат отвечал словами и ничего не менял, а поправить фонд или
# признак ГОЗ можно было только пересобрав документ.
#
# Правка идет сразу в два места. Строка реестра — то, что экономист видит в
# таблице. Паспорт дела — то, из чего собирается входной файл решателя. Одно
# без другого дает молчаливое расхождение: в таблице значение стоит, а
# считается по-старому.
#
# Откуда взялось значение, видно и потом: в графу «источник» пишется, что это
# правка экономиста и когда сделана, а сама реплика остается в ленте. Для ГОЗ
# это обязательно — проверяющий спросит, откуда цифра.

def _num(v):
    """Число из того, как его пишут: 3 000 000, 3000000.50, 1,5."""
    s = str(v or "").replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    s = "".join(ch for ch in s if ch.isdigit() or ch in ".-")
    try:
        return float(s)
    except ValueError:
        return None


def _goz(v):
    """«ГОЗ», «государственный», «да» — все это да; остальное нет."""
    s = str(v or "").strip().lower()
    if s in ("да", "гоз", "yes", "true", "1") or "гособорон" in s or "государствен" in s:
        return "да"
    if s in ("нет", "no", "false", "0") or "граждан" in s or "коммерч" in s:
        return "нет"
    return None


def _redirect(field, raw):
    """Поправить поле, если модель перепутала признак ГОЗ с видом договора.

    Эти два поля путаются постоянно: «договор 1234 — ГОЗ» модель кладет в
    «вид», хотя ГОЗ — признак да/нет, а вид это ОКР или поставка. Подсказка
    в промпте помогает не всегда, поэтому здесь еще и проверка: слово ГОЗ в
    значении «вида» — это признак.
    """
    if field == "вид" and _goz(raw) == "да":
        return "ГОЗ"
    return field


def _value(field, raw):
    """Значение к виду, в котором оно лежит в базе. None — не разобрали."""
    if field == "ГОЗ":
        return _goz(raw)
    if field in NUMERIC:
        return _num(raw)
    s = str(raw or "").strip()
    return s or None


def _find_contract(db, case_id, key):
    """Договор по шифру или номеру. Реестр общий, план берет из него свое."""
    from sqlalchemy import or_
    from db import Contract

    rows = (db.query(Contract)
            .filter(or_(Contract.case_id == case_id, Contract.case_id.is_(None)))
            .order_by(Contract.id).all())
    k = str(key or "").strip().lower()
    for c in rows:
        if (c.code or "").lower() == k or (c.number or "").lower() == k:
            return c
    for c in rows:
        if k and (k in (c.code or "").lower() or k in (c.number or "").lower()):
            return c
    return None


def _find_employee(db, case_id, key):
    from sqlalchemy import or_
    from db import Employee

    rows = (db.query(Employee)
            .filter(or_(Employee.case_id == case_id, Employee.case_id.is_(None)))
            .order_by(Employee.id).all())
    k = str(key or "").strip().lower()
    for e in rows:
        if (e.code or "").lower() == k:
            return e
    for e in rows:
        if k and k in ((e.fio or "") + " " + (e.code or "")).lower():
            return e
    return None


def _shown(v):
    if isinstance(v, float):
        return ("%.2f" % v).rstrip("0").rstrip(".").replace(".", ",")
    return str(v)


def apply_edits(db, case, edits):
    """Записать продиктованное. Возвращает строки отчета для ленты."""
    import datetime as dt
    import json as _json

    from db import now

    passport = _json.loads(case.passport) if case and case.passport else None
    case_id = case.id if case else None
    mark = "правка экономиста %s" % dt.datetime.now().strftime("%d.%m.%Y")
    out, touched = [], False
    changes = []          # (сущность, ключ, поле, было, стало) — для памяти

    for ed in edits or []:
        entity = (ed.get("entity") or "").strip()
        field = (ed.get("field") or "").strip()
        key = (ed.get("key") or "").strip()
        table = FIELDS.get(entity)
        if table is None:
            out.append("Не знаю, к чему отнести «%s»." % entity)
            continue
        field = _redirect(field, ed.get("value")) if entity == "договор" else field
        if field not in table:
            out.append("Поле «%s» через чат не заполняется. У %s можно: %s."
                       % (field, entity, ", ".join(table)))
            continue
        val = _value(field, ed.get("value"))
        if val is None:
            out.append("Не разобрал значение «%s» для поля «%s»."
                       % (ed.get("value"), field))
            continue

        column, pkey = table[field]
        if entity == "договор":
            row = _find_contract(db, case_id, key)
            if row is None:
                # Договора нет ни в одном документе: заводим строку реестра —
                # именно этого и не хватало, когда форма пришла не по шаблону.
                from db import Contract
                row = Contract(code=key, case_id=None)
                db.add(row)
                out.append("Договора %s в реестре не было — завела строку. "
                           "В расчет он войдет, только когда появится в шаблоне "
                           "расчета: вход решателя собирается по нему." % key)
            bag = (passport or {}).get("contracts") or []
            ident = ("code", row.code)
        else:
            row = _find_employee(db, case_id, key)
            if row is None:
                out.append("Сотрудника «%s» в штатном расписании нет. "
                           "Заводить людей через чат не берусь: строка штатки "
                           "заводится приказом." % key)
                continue
            bag = (passport or {}).get("employees") or []
            ident = ("code", row.code)

        was = getattr(row, column)
        setattr(row, column, val)
        row.source = mark
        touched = True
        changes.append((entity, key, field, was, val))

        # То же значение в паспорт: вход решателя собирается из него, и без
        # этого правка осталась бы только в таблице.
        for item in bag:
            if str(item.get(ident[0]) or "").lower() == str(ident[1] or "").lower():
                item[pkey] = val
                break

        out.append("%s %s: %s — %s%s."
                   % (entity.capitalize(), row.code, field, _shown(val),
                      "" if was in (None, "") else " (было %s)" % _shown(was)))

    if touched:
        if case is not None and passport is not None:
            case.passport = _json.dumps(passport, ensure_ascii=False)
        if case is not None:
            case.updated = now()
        db.commit()
    apply_edits.last_changes = changes
    return out
