# -*- coding: utf-8 -*-
"""Актуальность результатов: current → review_required (VER-003).

Посчитанный план опирается на документы-источники, справочник должностей,
строки реестра и условия плана. Когда что-то из этого меняется — документ
заменён новой версией или удалён, величина в справочнике переписана, в
реестр приняты новые строки, документ исключён из плана, условие плана
изменено из чата — прежний результат не стирается и не переписывается: он
остаётся историей, но получает отметку «требует пересмотра» и запись, что
именно изменилось. Результаты, которые от изменения не зависят (план другого
года, не использовавший этот документ), отметки не получают.

Отметка ставится только удачным прогонам: у отказа и обрыва пересматривать
нечего.
"""
from __future__ import annotations

import json

import agents
from db import Document, Run, now

REVIEW = "review_required"
CURRENT = "current"


def _sources_docs(run):
    try:
        src = json.loads(run.sources) if run.sources else {}
    except ValueError:
        return set()
    return {d.get("id") for d in (src.get("документы") or []) if d.get("id") is not None}


def results(db):
    """Удачные прогоны всех планов — только у них есть что пересматривать."""
    return db.query(Run).filter_by(status="OPTIMAL").order_by(Run.id).all()


def runs_using_document(db, doc_id):
    """Прогоны, в основание которых вошёл этот документ (по снимку источников)."""
    return [r for r in results(db) if doc_id in _sources_docs(r)]


def runs_of_case(db, case_id):
    return [r for r in results(db) if r.case_id == case_id]


def runs_pinned_reference(db):
    """Справочник должностей один на организацию: от него зависит каждый
    прогон, у которого он записан в основании."""
    out = []
    for r in results(db):
        try:
            src = json.loads(r.sources) if r.sources else {}
        except ValueError:
            src = {}
        if src.get("справочник"):
            out.append(r)
    return out


def runs_for_registry_change(db, doc):
    """Строки реестра приняты или убраны: документ плана трогает только
    его план, документ организации — все планы."""
    if doc is not None and doc.scope == "план" and doc.case_id:
        return runs_of_case(db, doc.case_id)
    return results(db)


def mark_review(db, runs, reason):
    """Отметить прогоны как требующие пересмотра и записать причину.

    Не коммитит: вызывающий код закрывает свою транзакцию сам, вместе с
    изменением, которое и стало причиной. В ленту каждого затронутого плана
    уходит одна реплика со списком прогонов — экономист должен увидеть, что
    прежний план больше не опирается на текущие данные.
    """
    touched = {}
    stamp = now().isoformat(timespec="seconds")
    for r in runs:
        if r.status != "OPTIMAL":
            continue
        try:
            log = json.loads(r.review_log) if r.review_log else []
        except ValueError:
            log = []
        log.append({"когда": stamp, "что": reason})
        r.review = REVIEW
        r.review_log = json.dumps(log, ensure_ascii=False)
        touched.setdefault(r.case_id, []).append(r.id)
    for case_id, ids in touched.items():
        agents.say(db, case_id,
                   "%s № %s требует пересмотра: %s."
                   % ("Расчёт" if len(ids) == 1 else "Расчёты",
                      ", ".join(str(i) for i in ids), reason),
                   agent="checker",
                   payload={"kind": "review_required", "runs": ids, "reason": reason})
    return [i for ids in touched.values() for i in ids]


def document_name(db, doc_id):
    d = db.get(Document, doc_id)
    return d.name if d is not None else "№ %s" % doc_id
