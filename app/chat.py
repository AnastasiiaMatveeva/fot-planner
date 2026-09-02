# -*- coding: utf-8 -*-
"""Ответ агента на реплику экономиста.

Раньше любая реплика записывалась как ответ на первый неотвеченный вопрос и
получала «Принял: ...». Это неверно вдвойне: экономист чаще спрашивает, чем
отвечает, и даже настоящий ответ требует действия, а не квитанции.

Здесь реплика идет в модель вместе с состоянием дела — какие документы
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
           "ответить на вопрос агента")

SYSTEM = """Ты — агент сервиса планирования фонда оплаты труда. Отвечаешь
экономисту в рабочей ленте дела.

Как отвечать:
- по существу и коротко, две-три фразы, деловым языком;
- опирайся только на состояние дела, которое дано ниже, ничего не выдумывай;
- если экономист говорит, что чего-то не понял, объясни своими словами, а не
  повторяй прежнюю формулировку;
- если он подсказывает, что за документ, предложи переразобрать этот документ
  как указанный вид — но только если документ вообще прочитан. Документ в
  состоянии «не прочитан» от указания вида читаемым не станет: тут надо
  попросить приложить его в текстовом виде или ввести величины вручную;
- если данных для расчета не хватает, скажи, каких именно и откуда их взять;
- не обещай того, чего сервис не делает.

Чего сервис пока не умеет, и об этом надо говорить прямо: распознавать сканы
без текстового слоя; извлекать таблицы из старых файлов .doc; подставлять
в расчет направленные правила замещения должностей."""

SHAPE = """Ответь одним объектом JSON:
{"reply": "что сказать экономисту",
 "action": "ничего | переразобрать документ | запустить расчет | ответить на вопрос агента",
 "document": "имя документа, если действие относится к нему, иначе null",
 "as_kind": "вид документа для переразбора, иначе null",
 "answer": "если это ответ на вопрос агента — его суть, иначе null"}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "action": {"type": "string", "enum": list(ACTIONS)},
        "document": {"type": ["string", "null"]},
        "as_kind": {"type": ["string", "null"], "enum": list(llm.DOC_KINDS) + [None]},
        "answer": {"type": ["string", "null"]},
    },
    "required": ["reply", "action"],
    "additionalProperties": False,
}


def context(db, case):
    """Состояние дела словами — то, на что модель может опереться."""
    from db import Contract, Document, Employee, Question, Run, Substitution

    lines = ["Дело: %s, год %s, этап: %s." % (case.title, case.year, case.stage)]

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
    return {"ok": True, "reply": raw["reply"].strip(),
            "action": action if action in ACTIONS else "ничего",
            "document": raw.get("document") or None,
            "as_kind": raw.get("as_kind") or None,
            "answer": raw.get("answer") or None}


def _ask_anthropic(user, model):
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
        model=model, max_tokens=1500, system=SYSTEM,
        messages=[{"role": "user", "content": user}], output_format=Reply)
    return msg.parsed_output.model_dump() if msg.parsed_output else None
