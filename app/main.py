# -*- coding: utf-8 -*-
"""Сервис планирования ФОТ: веб-приложение.

Устройство. Экономист работает в ленте дела: загружает документы, читает, что
с ними сделали агенты, отвечает на вопросы, запускает расчет. Всё состояние —
в базе, поэтому уйти и вернуться можно в любой момент, а не «пока открыта
вкладка». Долгие операции (разбор документа, расчет) идут фоном и отмечаются
строкой в таблице работ, которую страница опрашивает.

Запуск:
    .venv\\Scripts\\python.exe -m uvicorn main:app --app-dir app --port 8770
"""
from __future__ import annotations

import hashlib
import json
import logging
import fastapi
import os
import shutil
import subprocess
import sys
import threading
import time

from fastapi import Body, BackgroundTasks, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "docs", "ui"))

import agents            # noqa: E402
import build_input       # noqa: E402
import chat              # noqa: E402
import docread           # noqa: E402
import intake            # noqa: E402
import llm               # noqa: E402
import reference         # noqa: E402
from db import (         # noqa: E402
    Activity, Case, Contract, Correction, Document, Employee, Inflow, LaborRow,
    Message, Proposal, Question, Run, SecretAllowance, Substitution, Verdict,
    RESULT_DIR, UPLOAD_DIR, init_db, now, session,
)

EXE = os.path.join(ROOT, ".venv", "Scripts", "fot-planner.exe")
STATIC = os.path.join(HERE, "static")


# ── ключи модели из .env ────────────────────────────────────────
def _load_env():
    """Настройки модели из .env — в корне проекта или рядом с приложением."""
    for path in (os.path.join(ROOT, ".env"), os.path.join(ROOT, "env"),
                 os.path.join(HERE, ".env"), os.path.join(ROOT, "docs", "ui", ".env")):
        if not os.path.isfile(path):
            continue
        loaded = []
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v and not os.environ.get(k):
                    os.environ[k] = v
                    loaded.append(k)
        return os.path.relpath(path, ROOT), loaded
    return None, []


ENV_SOURCE, ENV_LOADED = _load_env()

app = FastAPI(title="Планирование ФОТ")
init_db()


def _close_orphans():
    """Строки работы и прогоны, брошенные прошлым запуском.

    Фоновая задача живёт в процессе сервера: перезапуск её убивает, а строка
    «агент работает» и прогон «идет» остаются навсегда — в шапке плана
    висит агент, которого нет. При старте закрываем такие следы честно.
    """
    from db import Activity, Run, session as _session
    db = _session()
    try:
        stale_acts = db.query(Activity).filter_by(state="идет").all()
        for a in stale_acts:
            a.state = "прервано"
            a.detail = "сервис был перезапущен, работа не завершена"
        stale_runs = db.query(Run).filter_by(status="идет").all()
        for r in stale_runs:
            r.status = "прерван"
        if stale_acts or stale_runs:
            db.commit()
    finally:
        db.close()


# Стенды и скрипты импортируют этот модуль ради сборщика входа и путей — и
# при импорте закрывали живой расчёт сервера как «брошенный» (09.09.2026:
# demo_offline.py пометил прогон приложения прерванным). Закрываем сироты
# только в процессе самого сервера.
if not os.environ.get("FOT_SKIP_ORPHANS"):
    _close_orphans()


# ── сериализация для страницы ───────────────────────────────────
def _dt(v):
    return v.strftime("%d.%m.%Y %H:%M") if v else None


def case_state(db, case):
    # Документы общие для организации: договор на три года обслуживает три
    # плана, и перезагружать его в каждый незачем.
    docs = (db.query(Document).filter(Document.state != "заменен")
            .order_by(Document.id).all())
    msgs = db.query(Message).filter_by(case_id=case.id).order_by(Message.id).all()
    acts = (db.query(Activity).filter_by(case_id=case.id)
            .order_by(Activity.id.desc()).limit(40).all())
    qs = db.query(Question).filter_by(case_id=case.id).order_by(Question.id).all()
    runs = db.query(Run).filter_by(case_id=case.id).order_by(Run.id.desc()).limit(5).all()
    provider_name, provider_model, why = llm.provider()
    return {
        "case": {"id": case.id, "title": case.title, "year": case.year,
                 "stage": case.stage, "updated": _dt(case.updated),
                 "has_data": bool(case.passport) or _registry_ready(db),
                 "counts": {
                     "employees": db.query(Employee).count(),
                     "contracts": db.query(Contract).count(),
                     "substitutions": db.query(Substitution).count()}},
        "documents": [{"id": d.id, "name": d.name, "kind": d.kind, "state": d.state,
                       "by": d.parsed_by, "summary": d.summary,
                       "uploaded": _dt(d.uploaded), "size": d.size,
                       # Реестр — общие документы; план — песочница этого плана.
                       "scope": ("план" if d.scope == "план" and d.case_id == case.id
                                 else "реестр"),
                       "muted": d.id in _muted(case)}
                      for d in docs
                      if d.scope != "план" or d.case_id == case.id],
        "messages": [{"id": m.id, "who": m.who, "agent": m.agent,
                      "to": m.to_agent, "text": m.text,
                      "payload": json.loads(m.payload) if m.payload else None,
                      "created": _dt(m.created)} for m in msgs],
        "activities": [{"id": a.id, "agent": a.agent, "title": a.title, "state": a.state,
                        "detail": a.detail, "seconds": a.seconds,
                        "artifact": json.loads(a.artifact) if a.artifact else None,
                        "started": _dt(a.started)} for a in reversed(acts)],
        "questions": [{"id": q.id, "agent": q.agent, "text": q.text,
                       "options": json.loads(q.options) if q.options else None,
                       "answer": q.answer} for q in qs],
        "runs": [{"id": r.id, "status": r.status, "seconds": r.seconds,
                  "created": _dt(r.created),
                  "summary": json.loads(r.summary) if r.summary else None,
                  "sources": json.loads(r.sources) if r.sources else None,
                  "settings": json.loads(r.settings) if r.settings else None} for r in runs],
        "agents": agents.agent_list(),
        "solver": agents.SOLVER,
        "model": {"provider": provider_name, "name": provider_model,
                  "label": llm.PROVIDER_LABEL.get(provider_name, provider_name),
                  "why": why},
    }


# ── дела ────────────────────────────────────────────────────────
@app.get("/api/cases")
def list_cases():
    db = session()
    try:
        rows = db.query(Case).order_by(Case.updated.desc()).all()
        out = []
        for c in rows:
            # Последний удачный прогон: по нему «Текущий план» открывается
            # сразу сводкой, минуя ленту.
            last = (db.query(Run).filter_by(case_id=c.id, status="OPTIMAL")
                    .order_by(Run.id.desc()).first())
            out.append({"id": c.id, "title": c.title, "year": c.year, "stage": c.stage,
                        "updated": _dt(c.updated),
                        # Список группируется по давности, для этого нужна дата,
                        # а не отформатированная строка.
                        "updated_at": c.updated.isoformat() if c.updated else None,
                        "documents": db.query(Document).filter_by(case_id=c.id).count(),
                        "run_id": last.id if last else None,
                        "computed_at": _dt(last.created) if last else None})
        return out
    finally:
        db.close()


@app.post("/api/cases")
async def create_case(request: Request):
    body = await request.json() if await request.body() else {}
    db = session()
    try:
        year = int(body.get("year") or 2026)
        case = Case(title=body.get("title") or "", year=year)
        db.add(case)
        db.commit()
        if not case.title:
            # Несколько планов на один год отличаем номером, а не всегда.
            same = db.query(Case).filter(Case.year == year, Case.id != case.id).count()
            case.title = ("План ФОТ на %d год" % year if not same
                          else "План ФОТ на %d год · вариант %d" % (year, same + 1))
            db.commit()
        agents.say(db, case.id,
                   "План открыт. Загрузите документы по договорам: план работ, "
                   "договоры с фондами и поступлениями, сотрудников с окладами "
                   "и ставками. Разберу и скажу, чего не хватает. "
                   "Приказы, положение об оплате труда и правила замещения "
                   "загружать не нужно — они уже в нормативной базе организации "
                   "и общие для всех планов. Их достаточно обновлять, когда выходит "
                   "новая редакция.", who="агент", agent="intake")
        return {"id": case.id}
    finally:
        db.close()


@app.patch("/api/case/{case_id}")
async def rename_case(case_id: int, request: Request):
    """Переименовать план.

    Название дает экономист: «План ФОТ на 2026 год» ничего не говорит, когда
    планов на год несколько, а «после сокращения» или «с новым договором» —
    говорит.
    """
    body = await request.json()
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "пустое название")
    db = session()
    try:
        case = db.get(Case, case_id)
        if case is None:
            raise HTTPException(404, "план не найден")
        case.title = title[:200]
        db.commit()
        return {"ok": True, "title": case.title}
    finally:
        db.close()


@app.delete("/api/case/{case_id}")
def delete_case(case_id: int):
    """Удалить план: ленту, работы агентов, вопросы и прогоны расчета.

    Документы, договоры, штатка и нормативы остаются: они принадлежат
    организации и нужны остальным планам. Отвязываем их до удаления, иначе
    каскад унес бы весь реестр вслед за одним планом.
    """
    db = session()
    try:
        case = db.get(Case, case_id)
        if case is None:
            raise HTTPException(404, "план не найден")

        for model in (Document, Employee, Contract, Substitution):
            db.query(model).filter_by(case_id=case_id).update({"case_id": None})
        db.commit()

        for run in db.query(Run).filter_by(case_id=case_id).all():
            for path in (run.input_path, run.result_path):
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass

        title = case.title
        db.delete(case)          # лента, работы, вопросы и прогоны уходят каскадом
        db.commit()
        return {"ok": True, "title": title}
    finally:
        db.close()


@app.get("/api/case/{case_id}")
def get_case(case_id: int):
    db = session()
    try:
        case = db.get(Case, case_id)
        if case is None:
            raise HTTPException(404, "план не найден")
        return case_state(db, case)
    finally:
        db.close()


# ── документы ───────────────────────────────────────────────────
def _process(case_id: int, doc_id: int):
    """Фоновая обработка: агент разбирает документ."""
    db = session()
    try:
        case = db.get(Case, case_id)
        doc = db.get(Document, doc_id)
        if case and doc and (doc.scope or "реестр") == "план":
            # Песочница: вид определяем, но в реестр ничего не пишем —
            # только предложения, которые экономист подтверждает сам.
            kind, owner, by, garbled = intake.classify(doc.path, doc.name)
            doc.kind, doc.parsed_by = kind, by
            # В песочнице документ читается, но в реестр не пишется: сначала
            # предложения, потом подтверждение экономиста. Ставить ему при
            # этом «данные не извлечены» нельзя — файл прочитан, и экономист
            # видел этот ярлык на обычной, полностью читаемой книге.
            doc.state = "текст нечитаемый" if garbled is not None else (
                "не прочитан" if by is None else "ожидает")
            db.commit()
            if by is not None and garbled is None:
                intake.propose_entities(db, case, doc)
                db.refresh(doc)
                if doc.state == "ожидает":
                    doc.state = "разобран"
                    doc.summary = "в песочнице плана: строк для реестра нет"
                    db.commit()
            _prerender_first_sheet(doc)
            threading.Thread(target=_warm_pdf, args=(doc.id,), daemon=True).start()
        elif case and doc:
            intake.handle_document(db, case, doc)
            _prerender_first_sheet(doc)
            # Печать в PDF — удобство карточки, а не разбор. Она идёт своим
            # потоком: у книги РКМ тридцать листов, Excel печатает их минуты,
            # и всё это время следующие документы стояли в очереди
            # неразобранными, а расчёт запускался без трудоёмкости.
            threading.Thread(target=_warm_pdf, args=(doc.id,), daemon=True).start()
    finally:
        db.close()


def _warm_pdf(doc_id):
    """Напечатать документ в PDF заранее, в фоне.

    Экономист открывает карточку и сразу видит вкладку «Документ»; печать
    книги занимает у Excel десяток секунд. Если начать её при разборе
    документа, к открытию карточки файл уже готов.
    """
    db = session()
    try:
        doc = db.get(Document, doc_id)
        if doc is not None:
            _as_pdf(doc)
    except Exception:  # noqa: BLE001 — грелка не должна ронять запрос
        pass
    finally:
        db.close()


def _prerender_first_sheet(doc):
    """Нарисовать первый лист книги заранее, пока идет разбор документа.

    Открыть книгу РКМ — пять секунд, и на них приходится почти все ожидание
    первого показа. Здесь эти секунды никому не заметны: экономист еще не
    открыл карточку.
    """
    if os.path.splitext(doc.path)[1].lower() not in WORKBOOK_TYPES:
        return
    try:
        names = _workbook(doc.path).sheetnames
        if names:
            _sheet_html(doc, names[0])
    except Exception:  # noqa: BLE001 — предпросмотр не должен ронять разбор
        pass


def _cyrillic(text):
    return sum(1 for c in text if "А" <= c <= "я" or c in "Ёё")


def _filename(raw):
    """Имя файла из multipart — в нормальную кодировку.

    Разбор multipart отдает имя как latin-1, а прислано оно может быть в UTF-8
    (браузеры) или в кодировке консоли Windows (командная строка). «должности»
    превращается то в «Ð´Ð¾Ð»Ð¶Ð½Ð¾ÑÑи», то в «äîëæíîñòè». Пробуем обе и берем
    ту, где получилась кириллица; если ни одна не помогла — оставляем как есть.
    """
    if not raw:
        return "документ"
    if _cyrillic(raw):
        return raw
    try:
        data = raw.encode("latin-1")
    except UnicodeEncodeError:
        return raw
    # Порядок важен: cp1251 декодирует что угодно и выдает правдоподобную
    # кириллическую кашу, а UTF-8 на чужих байтах просто не разбирается —
    # поэтому его успех и есть признак, что угадали верно.
    for enc in ("utf-8", "cp1251"):
        try:
            candidate = data.decode(enc)
        except UnicodeDecodeError:
            continue
        if _cyrillic(candidate):
            return candidate
    return raw


def _muted(case):
    """Документы, которые чат этого плана не смотрит: снятые флажки."""
    try:
        return set(json.loads(case.muted_docs) if case.muted_docs else [])
    except (TypeError, ValueError):
        return set()


def _set_muted(db, case, doc_id, muted):
    ids = _muted(case)
    if muted:
        ids.add(doc_id)
    else:
        ids.discard(doc_id)
    case.muted_docs = json.dumps(sorted(ids))
    db.commit()


@app.post("/api/case/{case_id}/doc/{doc_id}/mute")
async def mute_document(case_id: int, doc_id: int, request: Request):
    """Флажок документа: снят — чат плана его не видит, поставлен — видит."""
    body = await request.json()
    db = session()
    try:
        case = db.get(Case, case_id)
        if case is None or db.get(Document, doc_id) is None:
            raise HTTPException(404, "план или документ не найден")
        _set_muted(db, case, doc_id, bool(body.get("muted")))
        return {"ok": True, "muted": sorted(_muted(case))}
    finally:
        db.close()


@app.post("/api/case/{case_id}/upload")
async def upload(case_id: int, background: BackgroundTasks, files: list[UploadFile],
                 scope: str = fastapi.Form("реестр")):
    # «план» — песочница: документ виден чату этого плана, разбор дает только
    # предложения, строки в реестр не идут.
    scope = "план" if str(scope).strip().lower() == "план" else "реестр"
    db = session()
    try:
        case = db.get(Case, case_id)
        if case is None:
            raise HTTPException(404, "план не найден")
        added = []
        for f in files:
            data = await f.read()
            name = _filename(f.filename)
            digest = hashlib.sha256(data).hexdigest()
            # Тот же файл уже есть — это новая версия, а не сосед. Прежняя
            # остается для истории, но ее строки уходят из реестра: кормить
            # расчет двумя редакциями одного документа нельзя.
            prev = (db.query(Document).filter_by(name=name)
                    .filter(Document.state != "заменен")
                    .order_by(Document.id.desc()).first())
            if prev is not None and prev.sha256 == digest:
                # Байт в байт тот же файл: версии не будет, разбора тоже —
                # иначе случайный повтор загрузки стирал бы принятые строки
                # и правки экономиста ради того же самого содержимого.
                agents.say(db, case_id, "«%s» уже загружен, файл не изменился — "
                           "оставил прежний." % name, agent="intake")
                db.commit()
                continue
            safe = "%d_%d_%s" % (case_id, int(time.time() * 1000), os.path.basename(name))
            path = os.path.join(UPLOAD_DIR, safe)
            with open(path, "wb") as out:
                out.write(data)
            doc = Document(case_id=case_id, name=name, path=path, size=len(data),
                           sha256=digest, scope=scope)
            if prev is not None:
                _retire_document(db, prev)
                doc.version = (prev.version or 1) + 1
                doc.supersedes_id = prev.id
            db.add(doc)
            db.commit()
            added.append(doc.id)
            agents.say(db, case_id, "Загружен документ «%s»." % name, who="экономист",
                       document_id=doc.id)
            # Тот же файл под другим именем: содержимое совпадает байт в байт.
            # Молчать об этом нельзя — одни и те же данные попадут в реестр
            # дважды, а по именам это не видно.
            twin = (db.query(Document).filter(Document.sha256 == digest,
                                              Document.id != doc.id,
                                              Document.name != name,
                                              Document.state != "заменен")
                    .order_by(Document.id.desc()).first())
            if twin is not None:
                agents.say(db, case_id,
                           "Содержимое «%s» совпадает с «%s», загруженным раньше. "
                           "Если это тот же документ, лишнюю копию лучше удалить: "
                           "иначе одни и те же данные окажутся в реестре дважды."
                           % (name, twin.name), agent="intake", document_id=doc.id)
                db.commit()
        # Все файлы уже были в реестре (повторная загрузка тех же форм в новый
        # план): разбирать нечего, но план от этого не «в сборе данных» —
        # реестр организации полон, считать можно.
        if not added and _registry_ready(db):
            if case.stage == "сбор данных":
                case.stage = "готово к расчету"
                agents.say(db, case_id, "Документы уже в реестре организации, данных "
                           "достаточно для расчета. Могу считать.", agent="intake",
                           payload={"kind": "offer_solve"})
            db.commit()
            return {"ok": True, "documents": added}
        case.stage = "сбор данных"
        db.commit()
        for doc_id in added:
            background.add_task(_process, case_id, doc_id)
        return {"ok": True, "documents": added}
    finally:
        db.close()


def _progress(db, case):
    """Состояние плана этапами: где он сейчас и что мешает дойти до результата.

    Лента работ отвечает на вопрос «что делали агенты», а экономисту нужен
    другой: «где мой план и что от меня требуется». Пять этапов — документы,
    разбор, данные для расчета, расчет, результат — и у каждого состояние:
    готово, требует внимания, стоит, не начато. Первый стоящий этап и есть
    ответ «что мешает».
    """
    import tempfile

    docs = db.query(Document).order_by(Document.id).all()
    stages = []

    # 1. Документы
    bad = [d for d in docs if d.state in ("не прочитан", "текст нечитаемый")]
    rows = [{"текст": d.name, "состояние": docStatus(d.state), "document_id": d.id}
            for d in docs]
    stages.append({
        "имя": "Документы",
        "состояние": "стоит" if not docs else ("внимание" if bad else "готово"),
        "итог": ("документов нет — загрузите их в реестр" if not docs else
                 "%d %s, из них не читаются %d" % (len(docs), _px(len(docs), "документ", "документа", "документов"), len(bad))
                 if bad else "%d %s" % (len(docs), _px(len(docs), "документ", "документа", "документов"))),
        "строки": rows, "действие": {"текст": "Открыть реестр", "куда": "реестр"},
    })

    # 2. Разбор
    pending = db.query(Proposal).filter_by(state="предложено").all()
    by_doc = {}
    for pr in pending:
        by_doc[pr.document_id] = by_doc.get(pr.document_id, 0) + 1
    questions = (db.query(Question).filter_by(case_id=case.id, answer=None)
                 .order_by(Question.id).all())
    rows = []
    for did, n in by_doc.items():
        d = db.get(Document, did)
        rows.append({"текст": "%s — %d %s ждут подтверждения" % (
            d.name if d else did, n, _px(n, "строка", "строки", "строк")),
            "document_id": did, "внимание": True})
    for q in questions:
        rows.append({"текст": q.text, "case_id": case.id, "внимание": True})
    needs = bool(by_doc or questions)
    stages.append({
        "имя": "Разбор",
        "состояние": "не начато" if not docs else ("стоит" if needs else "готово"),
        "итог": ("ждет вас: %d %s на подтверждение, %d %s агента"
                 % (len(pending), _px(len(pending), "строка", "строки", "строк"),
                    len(questions), _px(len(questions), "вопрос", "вопроса", "вопросов"))
                 if needs else "все документы разобраны"),
        "строки": rows,
        "действие": ({"текст": "Подтвердить", "куда": "документ",
                      "document_id": next(iter(by_doc))} if by_doc else
                     {"текст": "Ответить", "куда": "план", "case_id": case.id} if questions
                     else None),
    })

    # 3. Данные для расчета — тот же сборщик, что перед расчетом, только
    # в никуда: его предупреждения и есть список допущений.
    pf = _preflight(db, case)
    warn, counts, blocked = pf["допущения"], pf["строки"], pf["стоит"]
    rows = [{"текст": "%s: %d" % (k, v)} for k, v in counts]
    rows += [{"текст": w, "внимание": True} for w in warn]
    stages.append({
        "имя": "Данные для расчета",
        "состояние": "стоит" if blocked else ("внимание" if warn else "готово"),
        "итог": ("нет сотрудников или договоров — считать не на чем" if blocked else
                 "%d %s приняты сервисом за вас — проверьте"
                 % (len(warn), _px(len(warn), "допущение", "допущения", "допущений"))
                 if warn else "все листы заполнены из документов"),
        "строки": rows, "действие": {"текст": "Открыть реестр", "куда": "реестр"},
    })

    # 4. Расчет
    run = (db.query(Run).filter_by(case_id=case.id).order_by(Run.id.desc()).first())
    stages.append({
        "имя": "Расчет",
        "состояние": ("не начато" if run is None else
                      "готово" if run.status == "OPTIMAL" else "стоит"),
        "итог": ("еще не запускался" if run is None else
                 "план найден за %s с" % run.seconds if run.status == "OPTIMAL" else
                 "решения нет: %s" % (run.status or "ошибка")),
        "строки": ([] if run is None else
                   [{"текст": "прогон № %d, %s" % (run.id, run.status or "—")}]),
        "действие": {"текст": "К плану", "куда": "план", "case_id": case.id},
    })

    # 5. Результат
    problems = {"Ошибка": 0, "Предупреждение": 0}
    plan_rows = None
    if run is not None and run.status == "OPTIMAL":
        js = os.path.join(RESULT_DIR, "case%d_run%d.json" % (case.id, run.id))
        if os.path.exists(js):
            with open(js, encoding="utf-8") as f:
                res = json.load(f)
            plan_rows = len(res.get("plan") or [])
            for w in res.get("warnings") or []:
                if w and w[0] in problems:
                    problems[w[0]] += 1
    stages.append({
        "имя": "Результат",
        "состояние": ("не начато" if plan_rows is None else
                      "внимание" if problems["Ошибка"] else "готово"),
        "итог": ("пока нет" if plan_rows is None else
                 "%d %s плана, ошибок %d, предупреждений %d"
                 % (plan_rows, _px(plan_rows, "строка", "строки", "строк"),
                    problems["Ошибка"], problems["Предупреждение"])),
        "строки": [], "действие": ({"текст": "Смотреть результат", "куда": "план",
                                    "case_id": case.id, "вкладка": "plan"}
                                   if plan_rows is not None else None),
    })

    blocker = next((s for s in stages if s["состояние"] == "стоит"), None)
    return {"id": case.id, "план": case.title, "год": case.year, "этап": case.stage,
            "этапы": stages,
            "мешает": blocker["итог"] if blocker else None,
            "мешает_этап": blocker["имя"] if blocker else None}


def _preflight(db, case):
    """Что уйдет в расчет и какие допущения сервис примет за экономиста.

    Считается тем же сборщиком, что и перед запуском, только в никуда. Одно и
    то же показывается на этапе «данные для расчета» и в окне перед кнопкой
    «Запустить»: для необратимого и денежного действия показ намерения — не
    любезность, а обязательное условие.
    """
    import tempfile

    data = _registry_data(db, case)
    warn = []
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        tmp.close()
        build_input.build(reference.TEMPLATE, tmp.name, data, warn)
        os.remove(tmp.name)
    except Exception as e:  # noqa: BLE001 — сборка не должна ронять сводку
        warn.append("Сборка входного файла не удалась: %s" % str(e)[:200])
    ps = _apply_plan_settings(data, case)
    counts = [("сотрудников", len(data["employees"])), ("договоров", len(data["contracts"])),
              ("поступлений", len(data["inflows"])), ("строк трудоемкости", len(data["labor"])),
              ("надбавок 120", len(data["secret"])),
              ("правил замещения", len(data["substitutions"])),
              ("закреплений", len(ps["назначения"])), ("запретов", len(ps["запреты"]))]
    for name, value in sorted((ps.get("настройки") or {}).items()):
        counts.append((name, value))
    settings = ps.get("настройки") or {}
    constraints = [
        ("Период", "%s год" % case.year),
        ("Трудоёмкость", "в пределах допуска %s" % settings.get("допуск трудоёмкости", "5%")),
        ("Виды выплат", "только разрешённые договором"),
        ("Основная ставка", "обязательна для основного сотрудника"),
        ("Дефицит выплат", "разрешён" if str(settings.get("разрешить дефицит", "нет")).lower() in ("да", "true", "1") else "не разрешён"),
        ("Правила замещения", "%d загружено" % len(data["substitutions"])),
    ]
    return {"строки": counts, "ограничения": constraints, "допущения": warn,
            "стоит": not data["employees"] or not data["contracts"],
            "год": case.year}


@app.get("/api/case/{case_id}/preflight")
def preflight(case_id: int):
    db = session()
    try:
        case = db.get(Case, case_id)
        if case is None:
            raise HTTPException(404, "план не найден")
        return _preflight(db, case)
    finally:
        db.close()


def _px(n, one, few, many):
    d, h = n % 10, n % 100
    if 11 <= h <= 14:
        return many
    if d == 1:
        return one
    if 2 <= d <= 4:
        return few
    return many


#: Статусы документа так, как их называет таблица реестра.
_DOC_STATUS = {"разобран": "Обработан", "ждет подтверждения": "Требует подтверждения",
               "не распознан": "Данные не извлечены", "не прочитан": "Файл не прочитан",
               "текст нечитаемый": "Текст нечитаемый", "ожидает": "В обработке"}


def docStatus(state):
    return _DOC_STATUS.get(state, state or "—")


#: Название шага по глаголу из заголовка работы — коротко и существительным.
_STEPS = (("определяет вид", "определение вида"),
          ("сверяет", "сверка со справочником"),
          ("разбирает", "разбор"),
          ("перечитывает", "чтение без шаблона"),
          ("читает правила замещения", "чтение правил замещения"),
          ("вносит правку", "правка из чата"))


def _step_name(title):
    for verb, noun in _STEPS:
        if title.startswith(verb):
            return noun
    return title.split("«")[0].strip() or title


@app.get("/api/agents")
def agents_page(limit: int = 60):
    """Все, что делали агенты, по всем планам — одной страницей.

    Схему из семи коробок со стрелками рисовать незачем: это картинка замысла,
    а не состояния, и половина коробок в этой сборке не существует. Страница
    отвечает на вопросы, которые у экономиста есть на самом деле: что от меня
    ждут, что уже сделано и на чем основано, что не работает вовсе.
    """
    db = session()
    try:
        titles = {c.id: c.title for c in db.query(Case).all()}

        waiting = []
        for q in (db.query(Question).filter_by(answer=None)
                  .order_by(Question.id.desc()).all()):
            waiting.append({"вид": "вопрос", "план": titles.get(q.case_id),
                            "case_id": q.case_id,
                            "агент": agents.AGENTS.get(q.agent, {}).get("name", q.agent),
                            "текст": q.text,
                            "варианты": json.loads(q.options) if q.options else None})
        for d in (db.query(Document).filter_by(state="ждет подтверждения")
                  .order_by(Document.id.desc()).all()):
            left = (db.query(Proposal)
                    .filter_by(document_id=d.id, state="предложено").count())
            waiting.append({"вид": "подтверждение", "документ": d.name,
                            "document_id": d.id, "строк": left,
                            "текст": "Агент прочитал документ без шаблона и "
                                     "предлагает строки — их нет в расчете, "
                                     "пока вы их не приняли."})

        work = []
        for a in (db.query(Activity).order_by(Activity.id.desc()).limit(limit).all()):
            info = agents.AGENTS.get(a.agent) or {}
            work.append({
                "id": a.id, "агент": info.get("name", a.agent),
                "номер": info.get("n"), "что": a.title, "состояние": a.state,
                "подробность": a.detail, "секунд": a.seconds,
                "план": titles.get(a.case_id), "case_id": a.case_id,
                "когда": _dt(a.started) if getattr(a, "started", None) else None,
                "артефакт": [[k, v] for k, v in
                             (json.loads(a.artifact) if a.artifact else {}).items()
                             if v not in (None, "", [], {})],
            })

        # Верхний уровень — след по документу: одна строка на документ, что с
        # ним произошло от загрузки до итога, и в нее можно провалиться до
        # каждой работы и ее артефакта. Следить за всем общением агентов
        # подряд тяжело и незачем; экономисту нужно «что стало с моим
        # документом», а не лента.
        docs = {d.name: d for d in db.query(Document).all()}
        traces: dict = {}
        for w in reversed(work):                 # в хронологическом порядке
            art = dict(w["артефакт"])
            name = art.get("файл")
            if not name:
                continue
            t = traces.setdefault(name, {"документ": name, "шаги": [],
                                         "document_id": None, "статус": None,
                                         "план": w["план"]})
            t["шаги"].append({"id": w["id"], "что": _step_name(w["что"]),
                              "агент": w["агент"], "состояние": w["состояние"],
                              "секунд": w["секунд"],
                              "подробность": w["подробность"],
                              "артефакт": [[k, v] for k, v in art.items()
                                           if k != "файл"]})
        for name, t in traces.items():
            d = docs.get(name)
            if d is not None:
                t["document_id"] = d.id
                t["статус"] = d.state
                t["внесено"] = {
                    "сотрудников": db.query(Employee).filter_by(document_id=d.id).count(),
                    "договоров": db.query(Contract).filter_by(document_id=d.id).count(),
                    "правил замещения": db.query(Substitution)
                                          .filter_by(document_id=d.id).count(),
                    "строк трудоемкости": db.query(LaborRow)
                                            .filter_by(document_id=d.id).count(),
                    "поступлений": db.query(Inflow).filter_by(document_id=d.id).count(),
                }
                t["ждет"] = (db.query(Proposal)
                             .filter_by(document_id=d.id, state="предложено").count())
        trace_list = sorted(traces.values(),
                            key=lambda t: -max(s["id"] for s in t["шаги"]))

        counts = {}
        for a in db.query(Activity).all():
            counts[a.agent] = counts.get(a.agent, 0) + 1
        roster = []
        for one in agents.agent_list():
            roster.append({"номер": one["n"], "имя": one["name"],
                           "делает": one["does"], "работает": one["real"],
                           "работ": counts.get(one["key"], 0),
                           "версия": agents.version_of(one["key"])})
        plans = [_progress(db, c) for c in
                 db.query(Case).order_by(Case.updated.desc()).all()]
        return {"ждет": waiting, "работы": work, "следы": trace_list,
                "планы": plans, "агенты": roster, "решатель": agents.SOLVER}
    finally:
        db.close()


@app.get("/api/documents")
def all_documents():
    """Все загруженные документы организации и что из каждого извлечено."""
    db = session()
    try:
        out = []
        for d in (db.query(Document).filter(Document.state != "заменен")
                  .order_by(Document.id.desc()).all()):
            labor_rows = db.query(LaborRow).filter_by(document_id=d.id).all()
            source_labor = 0
            for row in labor_rows:
                try:
                    source_labor += len(json.loads(row.details)) if row.details else 1
                except (TypeError, ValueError):
                    source_labor += 1
            out.append({
                "id": d.id, "name": d.name, "kind": d.kind, "state": d.state,
                "version": d.version or 1,
                "by": d.parsed_by, "summary": d.summary, "size": d.size,
                "uploaded": _dt(d.uploaded), "case_id": d.case_id,
                "gave": json.loads(d.gave) if getattr(d, "gave", None) else None,
                "produced": {
                    "сотрудников": db.query(Employee).filter_by(document_id=d.id).count(),
                    "договоров": db.query(Contract).filter_by(document_id=d.id).count(),
                    "поступлений": db.query(Inflow).filter_by(document_id=d.id).count(),
                    "строк трудоемкости": len(labor_rows),
                    "исходных строк трудоемкости": (source_labor
                        if source_labor > len(labor_rows) else 0),
                    "надбавок 120": db.query(SecretAllowance)
                                      .filter_by(document_id=d.id).count(),
                    "правил замещения": db.query(Substitution)
                                          .filter_by(document_id=d.id).count(),
                }})
        return out
    finally:
        db.close()


@app.get("/api/document/{doc_id}")
def one_document(doc_id: int):
    """Карточка документа: учет и то, что из него вышло, построчно.

    В реестре у документа было только имя и счетчик «сотрудников 4». Проверить
    по нему нечего: какие это сотрудники и откуда взялся оклад — не видно, а
    сам файл не открыть. Для ГОЗ это и есть главный вопрос проверяющего, и
    экономист должен уметь ответить на него, не выходя из сервиса.
    """
    db = session()
    try:
        d = db.get(Document, doc_id)
        if d is None:
            raise HTTPException(404, "документ не найден")
        emp = db.query(Employee).filter_by(document_id=d.id).order_by(Employee.id).all()
        ctr = db.query(Contract).filter_by(document_id=d.id).order_by(Contract.id).all()
        sub = (db.query(Substitution).filter_by(document_id=d.id)
               .order_by(Substitution.id).all())
        if os.path.splitext(d.path)[1].lower() in WORKBOOK_TYPES and os.path.exists(d.path):
            threading.Thread(target=_warm_workbook, args=(d.path,),
                             daemon=True).start()
        # Печать в PDF нужна и книге, и Word: карточка открывается на
        # вкладке «Документ», а печать идёт секунды.
        threading.Thread(target=_warm_pdf, args=(d.id,), daemon=True).start()
        return {
            "id": d.id, "name": d.name, "kind": d.kind, "state": d.state,
            "by": d.parsed_by, "summary": d.summary, "size": d.size,
            "gave": json.loads(d.gave) if getattr(d, "gave", None) else None,
            "формат": os.path.splitext(d.path)[1].lower() or "без расширения",
            "версия": d.version or 1,
            "заменяет": (lambda p: _dt(p.uploaded) if p else None)(
                db.get(Document, d.supersedes_id) if d.supersedes_id else None),
            "uploaded": _dt(d.uploaded), "case_id": d.case_id,
            "exists": os.path.exists(d.path),
            "печать готова": _pdf_ready(d),
            "onlyoffice": bool((os.environ.get("FOT_ONLYOFFICE_URL") or "").strip())
                          and os.path.splitext(d.path)[1].lower() in
                          sum(_OO_KIND.values(), ()),
            "employees": [{"code": e.code, "fio": e.fio, "position": e.position,
                           "rate": e.rate, "salary": e.salary,
                           "from": e.date_from, "to": e.date_to} for e in emp],
            "contracts": [{"code": c.code, "name": c.name, "number": c.number,
                           "kind": c.kind, "goz": c.goz, "fund": c.fund,
                           "kinds": c.kinds, "from": c.date_from,
                           "to": c.date_to} for c in ctr],
            "substitutions": [{"position": s.position, "replaced_by": s.replaced_by}
                              for s in sub],
            "предпросмотр": _preview(d),
            "переписка": [{"кто": m.who, "текст": m.text, "когда": _dt(m.created)}
                          for m in db.query(Message).filter_by(document_id=d.id)
                          .order_by(Message.id).all()][-20:],
            "proposals": [{"id": pr.id, "entity": pr.entity,
                           "fields": json.loads(pr.payload),
                           "evidence": pr.evidence, "state": pr.state,
                           "grade": pr.grade, "reason": pr.reason}
                          for pr in db.query(Proposal)
                          .filter_by(document_id=d.id, state="предложено")
                          .order_by(Proposal.id).all()],
        }
    finally:
        db.close()


#: Что из артефактов агента в карточке не нужно: одно уже стоит в шапке
#: документа, другое — внутренняя механика разбора. Отсев списком, а не
#: выборкой: у нового вида работы поля появятся сами, а дубли известны.
WORK_SKIP = {
    "файл", "формат", "размер, байт",        # то же есть в шапке карточки
    "определен вид", "чем определен", "чем разобрано",
    "передан агенту",
    "прочитано частями", "примечание модели",  # механика чтения
    "в расчет пойдет", "как", "почему",
}


def _document_work(db, name):
    """Что вышло из документа — из артефактов агентов, одним перечнем.

    Связь по имени файла: артефакт пишется агентом и хранит имя, а не ссылку.
    Для показа этого хватает; на удаление данных завязан document_id, там связь
    надежная.

    Разбивку по шагам не показываем: экономисту нужно, что из документа вышло,
    а не через сколько рук оно прошло. Берем по одной, последней записи на
    каждый вид работы — документ могли перезагружать.
    """
    out, seen = [], set()
    rows = (db.query(Activity).filter(Activity.artifact.isnot(None))
            .order_by(Activity.id.desc()).limit(200).all())
    for a in rows:
        try:
            art = json.loads(a.artifact)
        except ValueError:
            continue
        if art.get("файл") != name:
            continue
        step = a.title.split("«")[0].strip()
        if step in seen:
            continue
        seen.add(step)
        out.append([[k, v] for k, v in art.items()
                    if k not in WORK_SKIP and v not in (None, "", [], {})])
    fields, keys = [], set()
    for block in reversed(out):
        for k, v in block:
            if k in keys:
                continue
            keys.add(k)
            fields.append([k, v])
    return fields


def _doc_gave(db, doc_id):
    """Дал ли документ хоть одну строку реестра — по ссылке на него."""
    return any(db.query(model).filter_by(document_id=doc_id).count()
               for model in (Employee, Contract, Inflow, LaborRow,
                             SecretAllowance, Substitution))


@app.post("/api/document/{doc_id}/proposals")
async def decide_proposals(doc_id: int, request: Request):
    """Принять или отклонить строки, предложенные по документу.

    Принятая строка только здесь становится строкой реестра — и получает
    document_id, то есть источник. До этого ее нет нигде, кроме таблицы
    предложений, и ни в какой расчет она попасть не может.
    """
    body = await request.json()
    take = {int(x) for x in (body.get("accept") or [])}
    drop = {int(x) for x in (body.get("reject") or [])}
    db = session()
    try:
        doc = db.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "документ не найден")
        rows = (db.query(Proposal)
                .filter(Proposal.document_id == doc_id,
                        Proposal.state == "предложено").all())
        added = {"сотрудник": 0, "договор": 0, "трудоемкость": 0,
                 "поступление": 0, "надбавка 120": 0, "правило замещения": 0,
                 "должность": 0}
        ref_log = []
        version = agents.version_of("intake")
        for pr in rows:
            if pr.id in drop:
                pr.state = "отклонено"
                # Приговор — случай для стенда: эта строка была неверной.
                db.add(Verdict(document_name=doc.name, entity=pr.entity,
                               fields=pr.payload, verdict="отклонено",
                               agent_version=version, grade=pr.grade))
                # И память агента: в следующий раз он увидит, что так неверно.
                db.add(Correction(document_name=doc.name, document_kind=doc.kind,
                                  entity=pr.entity, wrong=pr.payload,
                                  note="отклонено экономистом", agent_version=version))
                continue
            if pr.id not in take:
                continue
            f = json.loads(pr.payload)
            db.add(Verdict(document_name=doc.name, entity=pr.entity,
                           fields=pr.payload, verdict="принято",
                           agent_version=version, grade=pr.grade))
            if pr.entity == "сотрудник":
                db.add(Employee(
                    code=str(f.get("code") or ""), fio=f.get("fio"),
                    position=f.get("position"), rate=f.get("rate"),
                    salary=f.get("salary"), department=f.get("department"),
                    employment_type=f.get("employment"),
                    employment_category=f.get("category"),
                    allowed_contracts=f.get("allowed"),
                    forbidden_contracts=f.get("forbidden"),
                    date_from=f.get("from"), date_to=f.get("to"),
                    source=doc.name, document_id=doc.id))
            elif pr.entity == "договор":
                db.add(Contract(
                    code=str(f.get("code") or ""), name=f.get("name"),
                    number=f.get("num"), kind=f.get("type"),
                    account=f.get("account"), department=f.get("department"),
                    goz=f.get("goz"), fund=f.get("fot"), kinds=_allowed_kinds(f),
                    priority=f.get("priority"), allow_main=f.get("allow_main"),
                    allow_part_time=f.get("allow_part"),
                    salary_deadline=f.get("salary_deadline"),
                    allowance_deadline=f.get("allowance_deadline"),
                    date_from=f.get("from"), date_to=f.get("to"),
                    source=doc.name, document_id=doc.id))
            elif pr.entity == "трудоемкость":
                db.add(LaborRow(
                    contract_code=str(f.get("contract") or ""),
                    year=f.get("year"), position=f.get("position"),
                    salary_page=f.get("page"), salary_group=f.get("group"),
                    position_level=f.get("level"),
                    person_months=f.get("person_months"),
                    avg_cost=f.get("avg_cost"),
                    headcount=f.get("headcount"),
                    source=doc.name, document_id=doc.id))
            elif pr.entity == "поступление":
                db.add(Inflow(
                    contract_code=str(f.get("contract") or ""),
                    year=f.get("year"), month=int(f.get("month") or 0),
                    amount=f.get("amount"),
                    source=doc.name, document_id=doc.id))
            elif pr.entity == "надбавка 120":
                db.add(SecretAllowance(
                    employee_code=str(f.get("employee") or ""),
                    secret_contract_code=f.get("contract"), rate=f.get("rate"),
                    source=doc.name, document_id=doc.id))
            elif pr.entity == "должность":
                # Должность живет не в базе, а листом «лимиты_по_должностям»
                # входного файла: оттуда ее читает сам решатель.
                what, changed = reference.upsert_position(
                    f.get("position"), f.get("category"),
                    f.get("salary_for_rate"), f.get("p2556"),
                    f.get("p4"), f.get("bep"))
                ref_log.append("«%s» %s%s" % (
                    f.get("position"), what,
                    " (%s)" % ", ".join(c[0] for c in changed) if changed else ""))
            else:
                db.add(Substitution(position=str(f.get("position") or ""),
                                    replaced_by=str(f.get("replaced_by") or ""),
                                    source=doc.name, document_id=doc.id))
            pr.state = "принято"
            added[pr.entity] += 1

        left = sum(1 for pr in rows if pr.state == "предложено")
        if not left:
            # Отклонить предложения — не значит «документ не прочитан»:
            # книгу по шаблону разобрал знакомый обработчик, и её строки уже
            # в реестре. Раньше отказ от лишней строки модели переводил
            # разобранный документ в «не распознан».
            doc.state = ("разобран" if any(added.values()) or _doc_gave(db, doc.id)
                         else "не распознан")
        db.commit()

        if any(added.values()) and doc.case_id:
            text = ("Принято из «%s»: %s. Строки записаны в реестр, источник — "
                    "этот документ." % (doc.name, ", ".join(
                        "%s %d" % (k, v) for k, v in added.items() if v)))
            if ref_log:
                text += (" В справочнике должностей: %s." % "; ".join(ref_log))
            agents.say(db, doc.case_id, text, agent="intake")
            db.commit()
        return {"ok": True, "added": added, "left": left, "reference": ref_log}
    finally:
        db.close()


#: Сколько показывать в предпросмотре. Карточка — это заглянуть в документ, а
#: не прочитать его целиком: для того есть «Открыть файл».
#: Приказ с таблицей пределов — двенадцать тысяч знаков; на двух с половиной
#: тысячах он обрывался на середине таблицы, ради которой его и открывают.
PREVIEW_CHARS = 12000
WORKBOOK_TYPES = (".xlsx", ".xlsm", ".xltx", ".xltm")
PREVIEW_DIR = os.path.join(os.path.dirname(UPLOAD_DIR), "previews")
os.makedirs(PREVIEW_DIR, exist_ok=True)


#: Разобранные книги. Открыть книгу РКМ — 5,4 секунды, а нарисовать из нее
#: лист — три десятых: конвертер открывал книгу заново на каждый лист, и
#: переключение в карточке стоило пяти секунд на ровном месте. Держим
#: последние две книги: экономист смотрит один документ, изредка два.
_BOOKS: "dict[str, tuple]" = {}
_BOOKS_LOCK = threading.Lock()
_BOOK_LOCKS: "dict[str, threading.Lock]" = {}
_BOOKS_KEEP = 2


def _workbook(path):
    """Разобранная книга из памяти, если файл с тех пор не менялся.

    Замок на каждый файл свой: карточка греет книгу заранее, а рамка
    предпросмотра просит лист почти сразу за ней. Без замка обе разбирали бы
    одну книгу параллельно — пять секунд работы дважды.
    """
    import openpyxl

    stamp = os.path.getmtime(path)
    with _BOOKS_LOCK:
        got = _BOOKS.get(path)
        if got and got[0] == stamp:
            return got[1]
        lock = _BOOK_LOCKS.setdefault(path, threading.Lock())

    with lock:
        with _BOOKS_LOCK:
            got = _BOOKS.get(path)
            if got and got[0] == stamp:
                return got[1]
        wb = openpyxl.load_workbook(path, data_only=True)
        with _BOOKS_LOCK:
            _BOOKS[path] = (stamp, wb)
            while len(_BOOKS) > _BOOKS_KEEP:
                gone = next(iter(_BOOKS))
                _BOOKS.pop(gone)
                _BOOK_LOCKS.pop(gone, None)
        return wb


def _warm_workbook(path):
    """Разобрать книгу заранее, пока экономист читает свойства документа.

    Первый лист стоит пяти секунд — столько занимает разбор книги. Если начать
    его при открытии карточки, к моменту, когда рамка попросит лист, книга уже
    готова, и ждать нечего.
    """
    try:
        _workbook(path)
    except Exception:  # noqa: BLE001 — грелка не должна ронять запрос
        pass


def _sheet_html(doc, sheet):
    """Лист книги как HTML — через конвертер, а не своей разметкой.

    Настоящие формы держатся на объединенных ячейках: у РКМ шапка склеена по
    десятку колонок, и таблица, собранная по клеткам, разваливается. Конвертер
    сохраняет объединения, ширины колонок и начертание.

    Результат кладем рядом с файлом: у книги РКМ тридцать листов, и карточку
    открывают не по одному разу. В ключ кэша входит отпечаток файла: номера
    записей после удаления документов повторяются, и по одному номеру
    показывалась прошлая книга — новая штатка на десять человек, а в рамке
    предпросмотра прежняя на восемь, с чужими зарплатами и без подсветки.
    """
    import hashlib

    from xlsx2html.core import get_sheet, render_table, worksheet_to_data

    key = hashlib.md5(("%d|%s|%s" % (doc.id, doc.sha256 or os.path.getmtime(doc.path), sheet or ""))
                      .encode("utf-8")).hexdigest()
    cached = os.path.join(PREVIEW_DIR, key + ".html")
    if os.path.exists(cached):
        with open(cached, encoding="utf-8") as f:
            return f.read()

    wb = _workbook(doc.path)
    ws = get_sheet(wb, sheet if sheet else 0)
    data = worksheet_to_data(ws, locale="ru", default_cell_border="none")
    html = render_table(data, lambda a, b: True, lambda a, b: True)
    with open(cached, "w", encoding="utf-8") as f:
        f.write(html)
    return html


def _unclip_columns(ws):
    """Раздвинуть столбцы и строки, где текст обрезан.

    Excel печатает лист как есть: «Наименование должности» в узком столбце
    обрывается на «Наименов», если справа тоже что-то написано, а строка с
    фиксированной высотой режет текст по верху. Читать такое нельзя.

    Каждое обращение к ячейке через COM — отдельный вызов в Excel, и на
    книге в тысячу ячеек это секунды. Поэтому значения читаем разом
    (``UsedRange.Value``), свойства спрашиваем у столбцов, а не у ячеек, а
    высоту строк подбираем одним вызовом на весь лист. Файл не меняется:
    книга открыта только для чтения.
    """
    try:
        ur = ws.UsedRange
        if ur.Cells.Count > 20000:
            return                      # огромный лист: печатаем как есть
        rows, cols = ur.Rows.Count, ur.Columns.Count
        vals = ur.Value
        if rows == 1 and cols == 1:
            return
        if rows == 1:
            vals = (vals,)
        if cols == 1:
            vals = tuple((v,) for v in vals)
        # Свойства столбца целиком: None — в столбце они разные, тогда
        # столбец не трогаем, чтобы не разъехалась авторская вёрстка.
        widths, wrap, merged = [], [], []
        for c in range(cols):
            col = ur.Columns(c + 1)
            widths.append(float(col.ColumnWidth or 0))
            wrap.append(col.WrapText)
            merged.append(col.MergeCells)
        need = {}
        for r in range(rows):
            row = vals[r]
            for c in range(cols - 1):
                v, nxt = row[c], row[c + 1]
                if not (isinstance(v, str) and v.strip()) or nxt in (None, ""):
                    continue
                if wrap[c] is not False or merged[c] is not False:
                    continue
                if len(v) > widths[c]:
                    need[c] = max(need.get(c, 0), len(v))
        # Ширина — по обрезанным ячейкам, а не по всему столбцу: длинный
        # заголовок в A1 раздвинул бы первый столбец на полстраницы.
        for c, chars in need.items():
            ur.Columns(c + 1).ColumnWidth = min(60, max(widths[c], chars + 2))
        # Высота — одним вызовом на весь лист: строка с фиксированной
        # высотой 13 pt и шрифтом 11 pt печаталась с обрезанным текстом.
        ur.Rows.AutoFit()
    except Exception:  # noqa: BLE001 — не вышло раздвинуть: печатаем как есть
        pass


#: В PDF печатаем тем, что стоит на машине: сначала Office (он рисует ровно
#: так, как документ выглядит у экономиста), потом LibreOffice.
def _office_to_pdf(src, out):
    try:
        import pythoncom
        import win32com.client as win32
    except ImportError:
        return False
    ext = os.path.splitext(src)[1].lower()
    excel = ext in WORKBOOK_TYPES
    pythoncom.CoInitialize()
    app = None
    try:
        app = win32.Dispatch("Excel.Application" if excel else "Word.Application")
        app.Visible = False
        app.DisplayAlerts = 0
        if excel:
            book = app.Workbooks.Open(src, ReadOnly=True, UpdateLinks=0)
            try:
                for ws in book.Worksheets:
                    _unclip_columns(ws)
                book.ExportAsFixedFormat(0, out)      # xlTypePDF
            finally:
                book.Close(False)
        else:
            d = app.Documents.Open(src, ReadOnly=True, AddToRecentFiles=False,
                                   Visible=False, ConfirmConversions=False)
            try:
                d.SaveAs2(out, FileFormat=17)         # wdFormatPDF
            finally:
                d.Close(False)
        return os.path.exists(out)
    except Exception:  # noqa: BLE001 — офиса нет или файл не открылся
        return False
    finally:
        try:
            if app is not None:
                app.Quit()
        except Exception:  # noqa: BLE001
            pass
        pythoncom.CoUninitialize()


def _soffice_to_pdf(src, out):
    exe = shutil.which("soffice") or shutil.which("soffice.exe")
    if exe is None:
        for guess in (os.path.join("C:", os.sep, "Program Files", "LibreOffice",
                                   "program", "soffice.exe"),
                      os.path.join("C:", os.sep, "Program Files (x86)", "LibreOffice",
                                   "program", "soffice.exe")):
            if os.path.exists(guess):
                exe = guess
                break
    if exe is None:
        return False
    outdir = os.path.dirname(out)
    try:
        subprocess.run([exe, "--headless", "--convert-to", "pdf", "--outdir", outdir, src],
                       check=True, timeout=180,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        return False
    made = os.path.join(outdir, os.path.splitext(os.path.basename(src))[0] + ".pdf")
    if os.path.exists(made):
        if os.path.abspath(made) != os.path.abspath(out):
            os.replace(made, out)
        return True
    return False


#: Печать идёт секундами, поэтому один документ печатаем один раз.
_PDF_LOCK = threading.Lock()


def _pdf_ready(doc):
    """Есть ли уже напечатанный PDF (или сам документ им и является)."""
    import hashlib

    ext = os.path.splitext(doc.path)[1].lower()
    if ext == ".pdf":
        return True
    key = hashlib.md5(("pdf4|%d|%s|%s" % (doc.id, doc.sha256 or "", doc.path))
                      .encode("utf-8")).hexdigest()
    return os.path.exists(os.path.join(PREVIEW_DIR, key + ".pdf"))


def _as_pdf(doc):
    """PDF-вид документа: сам файл, готовый кэш или свежая печать."""
    import hashlib

    ext = os.path.splitext(doc.path)[1].lower()
    if ext == ".pdf":
        return doc.path
    if ext not in (".doc", ".docx") and ext not in WORKBOOK_TYPES:
        return None
    key = hashlib.md5(("pdf4|%d|%s|%s" % (doc.id, doc.sha256 or "", doc.path))
                      .encode("utf-8")).hexdigest()
    out = os.path.join(PREVIEW_DIR, key + ".pdf")
    if os.path.exists(out):
        return out
    src = os.path.abspath(doc.path)
    with _PDF_LOCK:
        if os.path.exists(out):
            return out
        if _office_to_pdf(src, out) or _soffice_to_pdf(src, out):
            return out
    return None


#: Разметка, которую оставляем от Word: смысл, а не оформление.
_KEEP_TAGS = {"p", "br", "b", "strong", "i", "em", "u", "sup", "sub",
              "h1", "h2", "h3", "h4", "h5", "h6",
              "ul", "ol", "li", "table", "thead", "tbody", "tr", "td", "th"}
_KEEP_ALIGN = ("center", "right", "justify")


def _word_html_file(doc):
    """Напечатать документ Word в HTML и вернуть путь к файлу.

    Рядом Word кладёт папку с картинками — она нужна, чтобы вид совпадал.
    """
    import hashlib

    ext = os.path.splitext(doc.path)[1].lower()
    if ext not in (".doc", ".docx"):
        return None
    key = hashlib.md5(("rich|%d|%s" % (doc.id, doc.path)).encode("utf-8")).hexdigest()
    out = os.path.join(PREVIEW_DIR, key + ".htm")
    if os.path.exists(out):
        return out
    src = os.path.abspath(doc.path)
    try:
        import pythoncom
        import win32com.client as win32
    except ImportError:
        return None
    pythoncom.CoInitialize()
    app = None
    try:
        app = win32.Dispatch("Word.Application")
        app.Visible = False
        app.DisplayAlerts = 0
        d = app.Documents.Open(src, ReadOnly=True, AddToRecentFiles=False,
                               Visible=False, ConfirmConversions=False)
        try:
            d.WebOptions.AllowPNG = True
            d.SaveAs2(out, FileFormat=10)      # wdFormatFilteredHTML
        finally:
            d.Close(False)
        return out if os.path.exists(out) else None
    except Exception:  # noqa: BLE001 — Word недоступен: остаётся наш разбор
        return None
    finally:
        try:
            if app is not None:
                app.Quit()
        except Exception:  # noqa: BLE001
            pass
        pythoncom.CoUninitialize()


def _word_html(doc):
    """Документ Word как размеченный текст: заголовки, списки, таблицы.

    Печатает Word (отфильтрованный HTML), поэтому вид совпадает с тем, что
    экономист видит в самом файле. Разметку чистим: остаются только теги из
    ``_KEEP_TAGS``, из атрибутов — выравнивание и объединение ячеек.
    """
    import hashlib
    import re

    ext = os.path.splitext(doc.path)[1].lower()
    if ext not in (".doc", ".docx"):
        return None
    key = hashlib.md5(("html|%d|%s" % (doc.id, doc.path)).encode("utf-8")).hexdigest()
    cached = os.path.join(PREVIEW_DIR, key + ".html")
    if os.path.exists(cached):
        with open(cached, encoding="utf-8") as f:
            return f.read()

    src = os.path.abspath(doc.path)
    raw_path = os.path.join(PREVIEW_DIR, key + ".htm")
    ok = False
    try:
        import pythoncom
        import win32com.client as win32

        pythoncom.CoInitialize()
        app = None
        try:
            app = win32.Dispatch("Word.Application")
            app.Visible = False
            app.DisplayAlerts = 0
            d = app.Documents.Open(src, ReadOnly=True, AddToRecentFiles=False,
                                   Visible=False, ConfirmConversions=False)
            try:
                d.SaveAs2(raw_path, FileFormat=10)      # wdFormatFilteredHTML
            finally:
                d.Close(False)
            ok = os.path.exists(raw_path)
        finally:
            try:
                if app is not None:
                    app.Quit()
            except Exception:  # noqa: BLE001
                pass
            pythoncom.CoUninitialize()
    except Exception:  # noqa: BLE001 — Word недоступен: остаётся наш разбор
        ok = False
    if not ok:
        return None

    with open(raw_path, "rb") as f:
        raw = f.read()
    m = re.search(rb"charset=([\w-]+)", raw[:4000])
    enc = (m.group(1).decode("ascii", "ignore") if m else "windows-1251")
    try:
        text = raw.decode(enc, "ignore")
    except LookupError:
        text = raw.decode("windows-1251", "ignore")

    body = re.search(r"<body[^>]*>(.*)</body>", text, re.S | re.I)
    html = body.group(1) if body else text
    html = re.sub(r"(?is)<(script|style|xml)[^>]*>.*?</\1>", "", html)
    html = re.sub(r"(?is)<!--.*?-->", "", html)
    html = re.sub(r"(?is)<o:p[^>]*>.*?</o:p>", "", html)

    def keep(mm):
        closing, tag, attrs = mm.group(1), mm.group(2).lower(), mm.group(3) or ""
        if tag not in _KEEP_TAGS:
            return ""
        if closing:
            return "</%s>" % tag
        out = []
        al = re.search(r"""(?i)text-align\s*:\s*(\w+)""", attrs)
        if al and al.group(1).lower() in _KEEP_ALIGN:
            out.append('class="a%s"' % al.group(1).lower())
        for name in ("colspan", "rowspan"):
            sp = re.search(r"""(?i)\b%s\s*=\s*["']?(\d+)""" % name, attrs)
            if sp:
                out.append('%s="%s"' % (name, sp.group(1)))
        return "<%s%s>" % (tag, (" " + " ".join(out)) if out else "")

    html = re.sub(r"(?s)<(/?)([A-Za-z][\w:-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>", keep, html)
    html = re.sub(r"(?is)<p>\s*(&nbsp;|\s)*</p>", "", html)
    html = re.sub(r"&nbsp;", " ", html)
    html = re.sub(r"[ \t]{2,}", " ", html)
    if len(re.sub(r"<[^>]+>", "", html).strip()) < 40:
        return None
    with open(cached, "w", encoding="utf-8") as f:
        f.write(html)
    return html


def _preview(doc):
    """Заглянуть в документ, не выходя из карточки.

    Показываем не пересказ агента, а сам документ: книгу — таблицей, PDF —
    как есть, остальное — тем текстом, который увидел разборщик. Последнее
    важнее всего там, где текст оказался кашей: причина отказа видна глазами,
    а не со слов.
    """
    ext = os.path.splitext(doc.path)[1].lower()
    if not os.path.exists(doc.path):
        return {"вид": "нет", "почему": "файла нет на диске"}
    # «Как есть» — вид документа страницами. PDF уже такой, Word и книгу
    # печатаем в PDF по требованию: печать идёт секунды, в карточке её нет.
    printable = ext in (".pdf", ".doc", ".docx") or ext in WORKBOOK_TYPES
    if ext == ".pdf":
        # Рядом с самим PDF — тот текст, который увидел разборщик: браузерную
        # отрисовку документа не разметить, а текст размечается как обычная
        # страница, и на нём работают маска и выделение.
        out = {"вид": "документ"}
        try:
            text = docread.to_text(doc.path, PREVIEW_CHARS)
        except Exception:  # noqa: BLE001 — скан без текста: остаётся вид документа
            text = ""
        if text.strip():
            out["текст"] = text[:PREVIEW_CHARS]
            out["обрезано"] = len(text) >= PREVIEW_CHARS
        return out
    if ext in WORKBOOK_TYPES:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(doc.path, data_only=True, read_only=True)
            names = wb.sheetnames
            wb.close()
        except Exception as e:  # noqa: BLE001 — битая книга не должна ронять карточку
            return {"вид": "нет", "почему": str(e)[:200]}
        out = {"вид": "книга", "листы": names, "как есть": printable}
        try:
            text = docread.to_text(doc.path, PREVIEW_CHARS)
        except Exception:  # noqa: BLE001 — книга без текста: остаются листы
            text = ""
        if text.strip():
            out["текст"] = text[:PREVIEW_CHARS]
            out["обрезано"] = len(text) >= PREVIEW_CHARS
        return out
    try:
        text = docread.to_text(doc.path, PREVIEW_CHARS)
    except docread.Unreadable as e:
        return {"вид": "нет", "почему": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"вид": "нет", "почему": str(e)[:200]}
    out = {"вид": "текст", "текст": text[:PREVIEW_CHARS],
           "обрезано": len(text) >= PREVIEW_CHARS, "как есть": printable}
    out["разметка"] = bool(_word_html_file(doc))
    return out


#: Что браузер показывает сам, не скачивая.
INLINE_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


@app.post("/api/document/{doc_id}/message")
def document_message(doc_id: int, background: BackgroundTasks, body: dict = Body(...)):
    """Разговор о документе: «тут ошибка» — прямо в карточке.

    Раньше неправильно разобранный документ было не исправить: реестр
    показывал, но не слушал. Теперь реплика уходит агенту вместе с состоянием
    документа; правка ложится в реестр, в память агента и в приговор для
    стенда; подсказанный вид — в переразбор.
    """
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "пустое сообщение")
    fragment = body.get("fragment") or None
    db = session()
    try:
        doc = db.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "документ не найден")
        # Документ принадлежит организации, а сообщение по схеме — плану:
        # у сообщения case_id обязателен. Разговор о документе привязываем к
        # плану, из которого документ пришел, а если тот удален — к любому
        # живому: разговор здесь о документе, план только формальность.
        case = db.get(Case, doc.case_id) if doc.case_id else None
        if case is None:
            case = db.query(Case).order_by(Case.updated.desc()).first()
        if case is None:
            raise HTTPException(400, "сначала создайте план: разговор хранится при плане")
        cid = case.id
        # В переписке — слова экономиста и выделенный им кусок; лист,
        # строка и графа нужны агенту, а не читателю переписки, и уходят
        # в подсказку модели.
        shown = text
        quote = str((fragment or {}).get("цитата") or "")[:200]
        if quote:
            shown = "%s\n\u25b8 «%s»" % (text, quote)
        db.add(Message(case_id=cid, document_id=doc.id, who="экономист", text=shown))
        db.commit()

        res = chat.reply_doc(db, doc, text, fragment)
        if not res.get("ok"):
            db.add(Message(case_id=cid, document_id=doc.id, who="агент",
                           agent="intake", text="Не могу ответить: %s" % res.get("error")))
            db.commit()
            return {"ok": True}
        db.add(Message(case_id=cid, document_id=doc.id, who="агент",
                       agent="intake", text=res["reply"]))
        db.commit()

        version = agents.version_of("intake")
        action = res.get("action")
        if action == "заполнить поле" and res.get("edits"):
            lines = chat.apply_edits(db, None, res["edits"])
            for entity, key, field, was, val in getattr(chat.apply_edits, "last_changes", []):
                db.add(Correction(
                    document_name=doc.name, document_kind=doc.kind, entity=entity,
                    wrong=json.dumps({key: was, "поле": field}, ensure_ascii=False),
                    right=json.dumps({key: val, "поле": field}, ensure_ascii=False),
                    note=text[:300], agent_version=version))
            db.add(Message(case_id=cid, document_id=doc.id, who="агент",
                           agent="intake", text="\n".join(lines) or "Не разобрал, что записать."))
            db.commit()
        elif action == "переразобрать документ" and res.get("as_kind"):
            kind = res["as_kind"]
            owner = intake.OWNER.get(kind)
            # Замечание к строке — не повод менять вид документа: вопрос
            # «почему не извлек 250 000» модель принимала за «это штатное
            # расписание», и приказ уходил в разборщик книг, где .doc не
            # читается вовсе. Тот же запрет, что и в общем разговоре.
            refused = _cannot_reprocess(doc, owner)
            if refused:
                db.add(Message(case_id=cid, document_id=doc.id, who="агент",
                               agent="intake", text=refused))
                db.commit()
            elif case is not None:
                db.add(Correction(document_name=doc.name, document_kind=doc.kind,
                                  entity="вид документа", wrong=doc.kind, right=kind,
                                  note=text[:300], agent_version=version))
                db.commit()
                background.add_task(_reprocess, case.id, doc.id, owner, kind)
        else:
            # Слова экономиста без готовой правки — тоже память: «это неверно»
            # уже отсекает вариант при следующем разборе.
            low = text.lower()
            if any(w in low for w in ("неверн", "ошибк", "не так", "неправильн")):
                db.add(Correction(document_name=doc.name, document_kind=doc.kind,
                                  note=text[:300], agent_version=version))
                db.commit()
        return {"ok": True, "action": action}
    finally:
        db.close()


#: Что из строки реестра уходит в решатель: поле строки → графа входного
#: файла. Порядок важен: по нему подписывается подсветка.
SOLVER_FIELDS = {
    "employees": [("code", "код строки"), ("fio", "ФИО"), ("position", "должность"),
                  ("department", "подразделение"), ("rate", "ставка"),
                  ("employment_type", "тип занятости"),
                  ("employment_category", "категория занятости"),
                  ("salary", "зарплата"), ("date_from", "дата начала"),
                  ("date_to", "дата окончания"),
                  ("allowed_contracts", "разрешенные договоры"),
                  ("forbidden_contracts", "запрещенные договоры")],
    "contracts": [("code", "код"), ("name", "название"), ("number", "номер"),
                  ("kind", "тип договора"), ("account", "счет"), ("goz", "ГОЗ"),
                  ("department", "подразделение"), ("priority", "Приоритет"),
                  ("date_from", "дата начала"), ("date_to", "дата окончания"),
                  ("fund", "фот"), ("kinds", "разрешенные выплаты"),
                  ("allow_main", "основное место разрешено"),
                  ("allow_part_time", "совместительство разрешено"),
                  ("salary_deadline", "конечная дата выплат оклада"),
                  ("allowance_deadline", "конечная дата выплат надбавок")],
    "labor": [("contract_code", "договор"), ("position", "должность"),
              ("person_months", "трудоемкость"), ("avg_cost", "средняя стоимость"),
              ("headcount", "количество человек")],
    "inflows": [("contract_code", "договор"), ("month", "месяц"), ("amount", "сумма")],
    "secret": [("employee_code", "сотрудник"),
               ("secret_contract_code", "договор секретности"), ("rate", "ставка 120")],
    "substitutions": [("position", "должность"), ("replaced_by", "может быть замещена")],
}


def _norm_cell(v):
    """Значение ячейки к виду, в котором его можно сравнить со строкой реестра."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, (int, float)):
        f = float(v)
        return ("%d" % f) if f == int(f) else ("%s" % round(f, 4))
    s = str(v).strip()
    if not s:
        return None
    low = s.replace(",", ".").replace(" ", "").replace("\u00a0", "")
    try:
        f = float(low)
        return ("%d" % f) if f == int(f) else ("%s" % round(f, 4))
    except ValueError:
        pass
    return " ".join(s.split()).lower()


@app.get("/api/document/{doc_id}/marks")
def document_marks(doc_id: int):
    """Значения, которые из этого документа ушли в расчёт.

    Отдаём не координаты (в книге они у каждого листа свои, а разбор идёт по
    заголовкам), а сами значения с подписью графы входного файла: рамка
    предпросмотра сама найдёт их в разметке листа и подсветит.
    """
    db = session()
    try:
        d = db.get(Document, doc_id)
        if d is None:
            raise HTTPException(404, "документ не найден")
        rows = {
            "employees": db.query(Employee).filter_by(document_id=d.id).all(),
            "contracts": db.query(Contract).filter_by(document_id=d.id).all(),
            "labor": db.query(LaborRow).filter_by(document_id=d.id).all(),
            "inflows": db.query(Inflow).filter_by(document_id=d.id).all(),
            "secret": db.query(SecretAllowance).filter_by(document_id=d.id).all(),
            "substitutions": db.query(Substitution).filter_by(document_id=d.id).all(),
        }
        marks, counts = {}, {}
        marked_cells = []
        for entity, items in rows.items():
            if not items:
                continue
            counts[entity] = len(items)
            for it in items:
                details = []
                if entity == "labor" and getattr(it, "details", None):
                    try:
                        details = json.loads(it.details)
                    except (TypeError, ValueError):
                        details = []
                precise = any(d.get("cells") for d in details if isinstance(d, dict))
                fields_for_values = SOLVER_FIELDS[entity]
                if precise:
                    # Число 1 из headcount совпадает с номером первой строки.
                    # Для форм с адресами ячеек оставляем глобально только шифр
                    # договора; остальные значения красим строго по координате.
                    fields_for_values = [("contract_code", "договор")]
                for attr, title in fields_for_values:
                    key = _norm_cell(getattr(it, attr, None))
                    if key is None or len(str(key)) < 1:
                        continue
                    marks.setdefault(str(key), title)
                # У трудоёмкости агрегат хранит вход оптимизатора, а details —
                # все строки формы до объединения. Маска «Извлечённые данные»
                # должна показывать и этап, вид работ, даты и контрольную сумму.
                if entity == "labor" and details:
                    for detail in details:
                        hours = detail.get("labor_unit") == "чел.-ч"
                        fields = (("stage", "этап"), ("work_type", "вид работ"),
                                  ("position", "должность"),
                                  ("headcount", "количество человек"),
                                  ("labor_per_person", "часов на человека" if hours
                                   else "месяцев на человека"),
                                  ("person_months", "всего человеко-часов" if hours
                                   else "всего человеко-месяцев"),
                                  ("avg_cost", "стоимость человеко-часа" if hours
                                   else "стоимость человеко-месяца"),
                                  ("total_cost", "сумма ФОТ"), ("from", "начало"),
                                  ("to", "окончание"))
                        for attr, title in fields:
                            cell_id = (detail.get("cells") or {}).get(attr)
                            if cell_id:
                                marked_cells.append({"id": cell_id, "title": title})
                            elif not precise:
                                key = _norm_cell(detail.get(attr))
                                if key is not None:
                                    marks.setdefault(str(key), title)
        # У нормативного документа строк реестра нет: он даёт величины
        # справочнику должностей. Их записал разборщик — берём оттуда,
        # иначе маска молчит там, где документ отработал.
        if not marks and getattr(d, "gave", None):
            try:
                gave = json.loads(d.gave)
            except ValueError:
                gave = {}
            given = gave.get("значения") or {}
            if given:
                marks = {str(k): v for k, v in given.items()}
                counts["справочник должностей"] = len(marks)
                lines = gave.get("строки") or []
                if lines:
                    return {"ok": True, "значения": marks, "строк": counts,
                            "строки": lines}
        return {"ok": True, "значения": marks, "строк": counts,
                "ячейки": marked_cells}
    finally:
        db.close()


@app.get("/api/document/{doc_id}/rich")
def document_rich(doc_id: int):
    """Документ Word его собственной вёрсткой — для вкладки «текстом».

    Отдаём разметку, напечатанную Word: свои шрифты, отступы и таблицы. Из
    неё убираем только скрипты — остальное и есть вид документа.
    """
    import re

    db = session()
    try:
        d = db.get(Document, doc_id)
        if d is None:
            raise HTTPException(404, "документ не найден")
        path = _word_html_file(d)
        if not path:
            raise HTTPException(415, "нечем показать разметку документа")
        with open(path, "rb") as f:
            raw = f.read()
        m = re.search(rb"charset=([\w-]+)", raw[:4000])
        enc = (m.group(1).decode("ascii", "ignore") if m else "windows-1251")
        try:
            html = raw.decode(enc, "ignore")
        except LookupError:
            html = raw.decode("windows-1251", "ignore")
        html = re.sub(r"(?is)<script[^>]*>.*?</script>", "", html)
        # Картинки лежат в папке рядом; отдаём их своей ручкой.
        folder = os.path.splitext(os.path.basename(path))[0] + ".files"
        html = html.replace(folder + "/", "/api/document/%d/richfile/" % doc_id)
        return HTMLResponse(html)
    finally:
        db.close()


@app.get("/api/document/{doc_id}/richfile/{name}")
def document_richfile(doc_id: int, name: str):
    """Картинка из напечатанной разметки: только из своей папки."""
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "недопустимое имя")
    db = session()
    try:
        d = db.get(Document, doc_id)
        if d is None:
            raise HTTPException(404, "документ не найден")
        path = _word_html_file(d)
        if not path:
            raise HTTPException(404, "разметки нет")
        folder = os.path.join(PREVIEW_DIR,
                              os.path.splitext(os.path.basename(path))[0] + ".files")
        full = os.path.join(folder, name)
        if not os.path.isfile(full):
            raise HTTPException(404, "файл не найден")
        return FileResponse(full)
    finally:
        db.close()


@app.get("/api/document/{doc_id}/asis")
def document_asis(doc_id: int):
    """Документ как он выглядит: PDF или напечатанный в PDF Word/книга."""
    db = session()
    try:
        d = db.get(Document, doc_id)
        if d is None:
            raise HTTPException(404, "документ не найден")
        if not os.path.exists(d.path):
            raise HTTPException(404, "файл не найден на диске")
        path = _as_pdf(d)
        if not path:
            raise HTTPException(415, "нечем показать документ страницами")
        return FileResponse(path, media_type="application/pdf",
                            headers={"Content-Disposition": "inline"})
    finally:
        db.close()


@app.get("/api/document/{doc_id}/preview")
def document_preview(doc_id: int, sheet: str | None = None):
    """Лист книги, отрисованный конвертером, — для рамки предпросмотра."""
    db = session()
    try:
        d = db.get(Document, doc_id)
        if d is None:
            raise HTTPException(404, "документ не найден")
        if os.path.splitext(d.path)[1].lower() not in WORKBOOK_TYPES:
            raise HTTPException(400, "предпросмотр листами только для книг")
        try:
            html = _sheet_html(d, sheet)
        except Exception as e:  # noqa: BLE001 — лист может быть пустым или битым
            html = ("<p style='font:13px system-ui;color:#64748B'>Лист не "
                    "показывается: %s</p>" % str(e)[:200])
        return HTMLResponse(
            "<meta charset='utf-8'>"
            "<style>body{margin:12px;font:12px/1.4 system-ui,sans-serif}"
            "table{border-collapse:collapse}"
            "td,th{border:1px solid #E3E8EF;padding:3px 6px;"
            "vertical-align:top;white-space:pre-wrap}</style>" + html)
    finally:
        db.close()


@app.get("/api/document/{doc_id}/file")
def document_file(doc_id: int):
    """Отдать исходный файл. Проверить извлеченное можно только по оригиналу."""
    db = session()
    try:
        d = db.get(Document, doc_id)
        if d is None:
            raise HTTPException(404, "документ не найден")
        if not os.path.exists(d.path):
            raise HTTPException(404, "файл не найден на диске")
        ext = os.path.splitext(d.path)[1].lower()
        if ext in INLINE_TYPES:
            return FileResponse(
                d.path, media_type=INLINE_TYPES[ext],
                headers={"Content-Disposition": "inline"})
        return FileResponse(d.path, filename=d.name)
    finally:
        db.close()


#: Что ONLYOFFICE считает текстом, таблицей и презентацией.
_OO_KIND = {"word": (".doc", ".docx", ".rtf", ".odt", ".txt", ".pdf"),
            "cell": (".xls", ".xlsx", ".xlsm", ".xltx", ".xltm", ".csv", ".ods"),
            "slide": (".ppt", ".pptx", ".odp")}


@app.get("/api/document/{doc_id}/oo")
def document_onlyoffice(doc_id: int, request: Request):
    """Настройка просмотрщика ONLYOFFICE для этого документа.

    Файл забирает не браузер, а сам сервер ONLYOFFICE — по ссылке из
    ``document.url``. Поэтому адрес нашего сервиса для него задаётся
    отдельно (``FOT_SELF_URL``): изнутри докера «localhost» — это сам
    контейнер, а не мы. Режим только для чтения: правку документов сервис
    не ведёт, экономист смотрит и оставляет замечания.
    """
    api_url = (os.environ.get("FOT_ONLYOFFICE_URL") or "").strip()
    if not api_url:
        raise HTTPException(404, "ONLYOFFICE не настроен")
    db = session()
    try:
        d = db.get(Document, doc_id)
        if d is None:
            raise HTTPException(404, "документ не найден")
        ext = os.path.splitext(d.path)[1].lower()
        kind = next((k for k, exts in _OO_KIND.items() if ext in exts), None)
        if kind is None:
            raise HTTPException(415, "этот формат ONLYOFFICE не показывает")
        base = (os.environ.get("FOT_SELF_URL") or "").strip().rstrip("/")
        if not base:
            base = str(request.base_url).rstrip("/")
        # Ключ версии: пока файл тот же, сервер отдаёт документ из кэша.
        key = (d.sha256 or "")[:20] or ("doc%d-%s" % (d.id, d.uploaded))
        return {"apiUrl": api_url,
                "documentType": kind,
                "type": "desktop", "width": "100%", "height": "100%",
                "document": {"fileType": ext.lstrip("."), "key": key,
                             "title": d.name,
                             "url": "%s/api/document/%d/file" % (base, d.id),
                             "permissions": {"edit": False, "download": True,
                                             "print": True, "comment": False,
                                             "review": False}},
                # Без имени пользователя просмотрщик спрашивает его сам —
                # окно поверх документа при каждом открытии.
                "editorConfig": {"mode": "view", "lang": "ru",
                                 "user": {"id": "fot", "name": "Экономист"},
                                 "customization": {"compactToolbar": True,
                                                   "autosave": False, "chat": False,
                                                   "comments": False,
                                                   "feedback": False,
                                                   "forcesave": False,
                                                   "help": False,
                                                   "zoom": 100}}}
    finally:
        db.close()


#: Из каких полей предложения собирается графа «разрешенные выплаты».
_KIND_FIELDS = (("allow_salary", "оклад"), ("allow_120", "120"),
                ("allow_122", "122"), ("allow_124", "124"),
                ("allow_152", "152"), ("allow_order", "приказ"))


def _allowed_kinds(f):
    """Разрешенные виды выплат одной строкой — как их показывает реестр."""
    yes = [name for key, name in _KIND_FIELDS
           if str(f.get(key) or "").strip().lower() in ("да", "true", "1", "+")]
    return ", ".join(yes) or None


def _registry_data(db, case):
    """Все, что агент собрал для расчета, — из реестра организации.

    Реестр общий для всех планов: договор живет несколько лет, штатка меняется
    приказами. План на год берет из реестра то, что в этом году действует, а
    год берется из самого плана.
    """
    return {
        "year": case.year,
        "employees": db.query(Employee).order_by(Employee.id).all(),
        "contracts": db.query(Contract).order_by(Contract.id).all(),
        "inflows": db.query(Inflow).order_by(Inflow.id).all(),
        "labor": db.query(LaborRow).order_by(LaborRow.id).all(),
        "secret": db.query(SecretAllowance).order_by(SecretAllowance.id).all(),
        "substitutions": [(s.position, s.replaced_by) for s in
                          db.query(Substitution).order_by(Substitution.id).all()],
        "settings": _case_settings(case),
    }


def _apply_plan_settings(data, case):
    """Переменные, заданные экономистом в чате, поверх данных реестра:
    закрепления и запреты — листами ручных назначений, настройки — в
    «настройки». Они живут с планом и уходят в каждый его расчёт."""
    ps = chat.plan_settings(case)
    if ps.get("настройки"):
        data["settings"] = {**(data.get("settings") or {}), **ps["настройки"]}
    data["manual_assignments"] = ps.get("назначения") or []
    data["manual_prohibitions"] = ps.get("запреты") or []
    return ps


def _case_settings(case):
    """Лист «настройки» из последнего загруженного шаблона этого плана."""
    try:
        return (json.loads(case.passport) if case.passport else {}).get("settings") or {}
    except (TypeError, ValueError):
        return {}


def _retire_document(db, doc):
    """Прежняя версия документа: строки из реестра убрать, файл и запись оставить."""
    for model in (Employee, Contract, LaborRow, Inflow, SecretAllowance, Substitution):
        db.query(model).filter_by(document_id=doc.id).delete()
    db.query(Proposal).filter_by(document_id=doc.id, state="предложено").delete()
    doc.state = "заменен"
    db.commit()


def _forget_document(db, doc):
    """Удалить документ вместе со всем, что из него извлечено.

    Данные помнят документ-источник, поэтому сирот не остается: ушел документ —
    ушли его сотрудники, договоры и правила замещения.
    """
    gone = {
        "сотрудников": db.query(Employee).filter_by(document_id=doc.id).delete(),
        "договоров": db.query(Contract).filter_by(document_id=doc.id).delete(),
        "строк трудоемкости": db.query(LaborRow).filter_by(document_id=doc.id).delete(),
        "поступлений": db.query(Inflow).filter_by(document_id=doc.id).delete(),
        "надбавок 120": db.query(SecretAllowance)
                          .filter_by(document_id=doc.id).delete(),
        "правил замещения": db.query(Substitution).filter_by(document_id=doc.id).delete(),
    }
    # Непринятые предложения уходят вместе с документом: подтверждать строки
    # файла, которого больше нет, не по чему. ON DELETE CASCADE в схеме есть,
    # но SQLite не применяет внешние ключи без PRAGMA foreign_keys.
    db.query(Proposal).filter_by(document_id=doc.id).delete()
    # Величины справочника (оклад, П2556, П4, БЭП) живут в самом справочнике,
    # а не в этой записи: по ним уже посчитаны планы, поэтому вместе с
    # документом они не стираются. Но основание пропадает, и реестр должен
    # это показать, а не ссылаться на удалённый файл.
    try:
        lost = reference.forget_source(doc.id)
    except Exception:  # noqa: BLE001 — справочник может быть занят
        lost = []
    if lost:
        gone["величин без основания"] = len(lost)
        gone["_величины"] = ", ".join(lost)
    path = doc.path
    db.delete(doc)
    db.commit()
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
    return gone


@app.delete("/api/document/{doc_id}")
def delete_document(doc_id: int):
    """Удалить документ вместе со всем, что из него извлечено.

    Данные помнят документ-источник, поэтому удаление не оставляет сирот:
    ушел документ — ушли его сотрудники, договоры и правила замещения.
    Сам файл тоже удаляется: держать его без учетной записи незачем.
    """
    db = session()
    try:
        doc = db.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "документ не найден")
        name, case_id = doc.name, doc.case_id
        gone = _forget_document(db, doc)
        if case_id:
            names = gone.pop("_величины", "")
            lost = ", ".join("%s %d" % (k, v) for k, v in gone.items()
                             if v and not str(k).startswith("_"))
            if names:
                lost += " (%s — теперь без основания, величины остались)" % names
            agents.say(db, case_id,
                       "Удален документ «%s»%s." % (name, ", с ним " + lost if lost else ""),
                       who="экономист")
        return {"ok": True, "removed": gone}
    finally:
        db.close()


# ── лента и вопросы ─────────────────────────────────────────────
def _running_run(db, case_id):
    """Идущий прогон этого плана, если он есть.

    Второй расчет того же плана не нужен никому: HiGHS занимает машину, и
    оба прогона идут вдвое дольше. Случай не выдуманный — агент в ленте
    предложил «пересчитать» во время расчета, и на восьми людях считались
    сразу два плана.
    """
    return (db.query(Run).filter_by(case_id=case_id, status="идет")
            .order_by(Run.id.desc()).first())


@app.post("/api/case/{case_id}/message")
def post_message(case_id: int, background: BackgroundTasks, body: dict = Body(...)):
    """Реплика экономиста: пишем ее в ленту и отвечаем моделью.

    Обработчик намеренно обычный (не async): модель отвечает секунды, а
    запрос к ней блокирующий. В корутине он останавливал весь событийный
    цикл — опрос ленты не отвечал, и реплика экономиста вместе с отметкой
    «агент работает» появлялась на экране только вместе с ответом. Обычный
    обработчик FastAPI выполняет в отдельном потоке, и лента живет.
    """
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "пустое сообщение")
    db = session()
    try:
        case = db.get(Case, case_id)
        if case is None:
            raise HTTPException(404, "план не найден")
        agents.say(db, case_id, text, who="экономист")

        # Реплика идет в модель вместе с состоянием дела: экономист чаще
        # спрашивает, чем отвечает, и квитанция «Принял» — не ответ.
        with agents.working(db, case_id, "intake", "разбирает реплику") as w:
            res = chat.reply(db, case, text)
            w["detail"] = (res.get("reply") or res.get("error") or "")[:200]
            db.commit()

        if not res.get("ok"):
            # Без модели остается прежнее поведение: правка данных разбором.
            if case.passport:
                passport = json.loads(case.passport)
                answer, changed, run = intake.extract.apply_chat(passport, text)
                if changed:
                    case.passport = json.dumps(passport, ensure_ascii=False)
                agents.say(db, case_id, answer, agent="intake")
                db.commit()
                return {"ok": True, "solve": bool(run)}
            agents.say(db, case_id,
                       "Не могу ответить: %s. Загрузите документы — их разбор "
                       "работает и без модели." % res.get("error"), agent="intake")
            db.commit()
            return {"ok": True}

        agents.say(db, case_id, res["reply"], agent="intake")
        db.commit()

        action = res.get("action")
        # Закрепления, запреты и настройки — переменные решателя, заданные
        # рукой экономиста. Пишем в план и пересчитываем, если план уже
        # считался; без месяцев — спрашиваем и ничего не пишем.
        if res.get("fixes") or res.get("settings") or chat.parse_settings(text):
            lines, questions = chat.apply_fixes(db, case, res.get("fixes"), text)
            lines += chat.apply_settings(db, case, res.get("settings"), text)
            db.commit()
            if lines:
                agents.say(db, case_id, "\n".join(lines), agent="tuning")
            if questions:
                agents.say(db, case_id, "\n".join(questions), agent="tuning",
                           payload={"kind": "ask_months"})
                db.commit()
                return {"ok": True, "action": "вопрос"}
            if not lines:
                db.commit()
                return {"ok": True, "action": "ничего"}
            last = (db.query(Run).filter_by(case_id=case_id, status="OPTIMAL")
                    .order_by(Run.id.desc()).first())
            if last is None:
                db.commit()
                return {"ok": True, "action": "закреплено"}
            busy = _running_run(db, case_id)
            if busy is not None:
                agents.say(db, case_id,
                           "Условие записано. Пересчитаю после расчета № %d — "
                           "он сейчас идет." % busy.id, agent="tuning")
                db.commit()
                return {"ok": True, "action": "закреплено"}
            base = json.loads(last.settings) if last and last.settings else {}
            run = Run(case_id=case_id, settings=json.dumps(base, ensure_ascii=False))
            db.add(run)
            db.commit()
            agents.say(db, case_id, "Пересчитываю план с этими условиями.", agent="tuning")
            db.commit()
            background.add_task(_solve, case_id, run.id, base)
            return {"ok": True, "action": "расчет", "run_id": run.id}

        if action == "ответить на вопрос агента":
            pending = (db.query(Question).filter_by(case_id=case_id, answer=None)
                       .order_by(Question.id).first())
            if pending is not None:
                pending.answer = res.get("answer") or text
                pending.answered = now()
                db.commit()

        if action == "заполнить поле":
            # Величину, продиктованную в чате, пишет сервис по закрытому
            # списку полей: модель называет поле словами, а имя столбца от
            # модели в базу не идет.
            with agents.working(db, case_id, "intake", "вносит правку") as w:
                lines = chat.apply_edits(db, case, res.get("edits"))
                w["detail"] = "; ".join(lines)[:200] or "нечего вносить"
                db.commit()
            agents.say(db, case_id,
                       "\n".join(lines) or "Не разобрал, что именно записать.",
                       agent="intake")
            db.commit()
            return {"ok": True, "action": "правка", "edits": len(lines)}

        # Действие модель предлагает, а выполняет сервис — и только если оно
        # выполнимо. Иначе «не поняла» запускает расчет на пустом деле, а PDF
        # уходит в разборщик книг Excel.
        if action == "переразобрать документ" and res.get("document"):
            doc = _find_document(db, case_id, res["document"])
            if doc is None:
                agents.say(db, case_id,
                           "Не нашел документ «%s» в этом деле." % res["document"],
                           agent="intake")
                db.commit()
                return {"ok": True, "action": "ничего"}
            kind = res.get("as_kind")
            owner = intake.OWNER.get(kind) if kind else None
            refused = _cannot_reprocess(doc, owner)
            if refused:
                agents.say(db, case_id, refused, agent="intake")
                db.commit()
                return {"ok": True, "action": "ничего"}
            background.add_task(_reprocess, case_id, doc.id, owner, kind)
            return {"ok": True, "action": "переразбор", "document": doc.name}

        if action in ("исключить документ", "вернуть документ") and res.get("document"):
            # «Не смотри на прошлогоднюю штатку» — агент снимает флажок сам;
            # «верни» — ставит обратно. Тот же флажок, что в панели документов.
            doc = _find_document(db, case_id, res["document"])
            if doc is None:
                agents.say(db, case_id, "Не нашел документ «%s»." % res["document"],
                           agent="intake")
                db.commit()
                return {"ok": True, "action": "ничего"}
            _set_muted(db, case, doc.id, action == "исключить документ")
            return {"ok": True, "action": "флажок", "document": doc.name}

        if action == "претензия к плану":
            # Претензия сдвигает веса целей относительно последнего удачного
            # прогона и запускает пересчет. Что именно сдвинуто — в ленте и в
            # настройках прогона, чтобы план помнил свои претензии.
            last = (db.query(Run).filter_by(case_id=case_id, status="OPTIMAL")
                    .order_by(Run.id.desc()).first())
            base = json.loads(last.settings) if last and last.settings else {}
            new_settings, lines = chat.claim_settings(base, res.get("claims"), text)
            if not res.get("claims"):
                agents.say(db, case_id, "Претензию понял, но в цель решателя "
                           "перевести не смог — назовите, что именно в плане не так: "
                           "переводы, связки с договорами, освоение по месяцам или "
                           "суммы по месяцам.", agent="solver")
                db.commit()
                return {"ok": True, "action": "ничего"}
            agents.say(db, case_id, "\n".join(lines), agent="solver")
            run = Run(case_id=case_id, settings=json.dumps(new_settings, ensure_ascii=False))
            db.add(run)
            db.commit()
            background.add_task(_solve, case_id, run.id, new_settings)
            return {"ok": True, "action": "расчет", "run_id": run.id}

        if action == "запустить расчет":
            busy = _running_run(db, case_id)
            if busy is not None:
                agents.say(db, case_id,
                           "Расчет № %d уже идет — дождитесь его, второй "
                           "расчет того же плана только замедлит первый."
                           % busy.id, agent="intake")
                db.commit()
                return {"ok": True, "action": "ничего"}
            if not case.passport:
                agents.say(db, case_id,
                           "Считать пока не на чем: в деле нет ни сотрудников, "
                           "ни договоров. Загрузите документ со штатным расписанием "
                           "и фондами договоров.", agent="intake")
                db.commit()
                return {"ok": True, "action": "ничего"}
            run = Run(case_id=case_id, settings="{}")
            db.add(run)
            db.commit()
            background.add_task(_solve, case_id, run.id, None)
            return {"ok": True, "action": "расчет", "run_id": run.id}

        return {"ok": True, "action": action}
    finally:
        db.close()


#: Какие форматы умеет разбирать каждый обработчик. Разбор документов по
#: договору и правил замещения читает ячейки книги, поэтому PDF и .doc туда
#: отдавать бессмысленно — лучше сказать честно, чем уронить разбор.
FORMATS = {
    "intake": (".xlsx", ".xlsm", ".xls"),
    "substitutions": (".xlsx", ".xlsm", ".xls"),
    "norms": (".xlsx", ".xlsm", ".xls", ".pdf", ".doc", ".docx"),
}


def _cannot_reprocess(doc, owner):
    """Причина, по которой переразбор невозможен, либо None."""
    if doc.state in ("не прочитан", "текст нечитаемый"):
        return ("«%s» не читается: %s Указание вида это не изменит."
                % (doc.name, doc.summary or "файл не удалось привести к тексту."))
    if owner is None:
        return None
    ext = os.path.splitext(doc.path)[1].lower()
    allowed = FORMATS.get(owner, ())
    if ext in allowed:
        return None
    return ("«%s» — файл %s, а такой вид документа читается только из %s. "
            "Приложите его в этом формате или введите величины вручную."
            % (doc.name, ext or "без расширения", ", ".join(allowed)))


def _find_document(db, case_id, name):
    """Документ по имени из ответа модели — она могла назвать его неточно."""
    docs = db.query(Document).filter_by(case_id=case_id).all()
    name = (name or "").strip().lower()
    for d in docs:
        if d.name.lower() == name:
            return d
    for d in docs:
        if name and (name in d.name.lower() or d.name.lower() in name):
            return d
    return None


def _reprocess(case_id: int, doc_id: int, owner: str | None, kind: str | None):
    """Переразобрать документ, зная от экономиста, что это за вид."""
    db = session()
    try:
        case = db.get(Case, case_id)
        doc = db.get(Document, doc_id)
        if not case or not doc:
            return
        if owner is None:
            intake.handle_document(db, case, doc)
            return
        doc.kind = kind
        doc.parsed_by = "вид указан экономистом"
        db.commit()
        try:
            if owner == "norms":
                intake.run_norms(db, case, doc)
            elif owner == "substitutions":
                intake.run_substitutions(db, case, doc)
            else:
                intake.run_intake(db, case, doc)
        except Exception as exc:  # noqa: BLE001
            doc.state = "не распознан"
            doc.summary = str(exc)[:300]
            db.commit()
            agents.say(db, case_id,
                       "Не смог разобрать «%s» как «%s». %s" % (doc.name, kind, exc),
                       agent=owner)
            db.commit()
    finally:
        db.close()


@app.post("/api/case/{case_id}/answer/{qid}")
async def answer_question(case_id: int, qid: int, request: Request):
    body = await request.json()
    answer = (body.get("answer") or "").strip()
    db = session()
    try:
        q = db.get(Question, qid)
        if q is None or q.case_id != case_id:
            raise HTTPException(404, "вопрос не найден")
        q.answer = answer
        q.answered = now()
        db.commit()
        agents.say(db, case_id, answer, who="экономист")
        agents.say(db, case_id, "Принял: %s" % answer, agent=q.agent)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ── просмотр данных ─────────────────────────────────────────────
@app.get("/api/case/{case_id}/data")
def case_data(case_id: int):
    """Входные данные дела: то, что экономист открывает и читает."""
    db = session()
    try:
        emps = db.query(Employee).order_by(Employee.code).all()
        ctrs = db.query(Contract).order_by(Contract.code).all()
        return {
            # Поля отдаются все, какие есть в контракте входного файла: то, что
            # уходит в расчет, экономист должен видеть и проверить.
            "employees": [{"code": e.code, "fio": e.fio, "position": e.position,
                           "department": e.department, "rate": e.rate,
                           "employment": e.employment_type,
                           "category": e.employment_category,
                           "salary": e.salary,
                           "allowed": e.allowed_contracts,
                           "forbidden": e.forbidden_contracts,
                           "from": e.date_from, "to": e.date_to,
                           "source": e.source} for e in emps],
            "contracts": [{"code": c.code, "name": c.name, "number": c.number,
                           "kind": c.kind, "account": c.account, "goz": c.goz,
                           "department": c.department,
                           "fund": c.fund, "kinds": c.kinds,
                           "priority": c.priority, "allow_main": c.allow_main,
                           "allow_part": c.allow_part_time,
                           "salary_deadline": c.salary_deadline,
                           "allowance_deadline": c.allowance_deadline,
                           "source": c.source,
                           "from": c.date_from, "to": c.date_to} for c in ctrs],
            "labor": [{"contract": r.contract_code, "year": r.year,
                       "position": r.position, "page": r.salary_page,
                       "group": r.salary_group, "level": r.position_level,
                        "person_months": r.person_months, "avg_cost": r.avg_cost,
                        "headcount": r.headcount, "source": r.source,
                        "months": json.loads(r.months) if r.months else None,
                        "details": json.loads(r.details) if r.details else None}
                      for r in db.query(LaborRow).order_by(LaborRow.id).all()],
            "inflows": [{"contract": r.contract_code, "year": r.year,
                         "month": r.month, "amount": r.amount,
                         "source": r.source}
                        for r in db.query(Inflow)
                        .order_by(Inflow.contract_code, Inflow.month).all()],
            "secret": [{"employee": r.employee_code,
                        "contract": r.secret_contract_code, "rate": r.rate,
                        "source": r.source}
                       for r in db.query(SecretAllowance)
                       .order_by(SecretAllowance.id).all()],
            # Нормативная база общая: правила и справочник не привязаны к делу.
            "substitutions": [{"position": s.position, "replaced_by": s.replaced_by,
                               "source": s.source} for s in
                              db.query(Substitution).order_by(Substitution.id).all()],
            "reference": reference.read_rows(),
            # Откуда каждая величина справочника: документ, дата, основание.
            "reference_sources": reference.read_sources(),
        }
    finally:
        db.close()


#: Разбор правил по прогону: чтение двух книг занимает секунды, а ответ
#: не меняется, пока прогон не пересчитан.
_RULES_CACHE: dict = {}


@app.get("/api/case/{case_id}/run/{run_id}/rules")
def run_rules(case_id: int, run_id: int):
    """Ограничения работы экономиста: соблюдены ли они в этом плане.

    Считаются независимо от решателя — по входному файлу и по плану выплат.
    Решатель говорит «OPTIMAL» и перечисляет свои показатели; экономисту нужно
    другое: выполняются ли правила, по которым он живет, и если нет — где.
    """
    src, out = _run_files(db_run(case_id, run_id))
    key = (run_id, os.path.getmtime(out))
    if key not in _RULES_CACHE:
        import rules as rules_mod
        _RULES_CACHE[key] = rules_mod.check(src, out)
    return {"правила": _RULES_CACHE[key]}


@app.get("/api/case/{case_id}/run/{run_id}/report")
def run_report(case_id: int, run_id: int):
    """План ФОТ на год в формах экономистов: двенадцать разделов числами."""
    src, out = _run_files(db_run(case_id, run_id))
    key = ("отчет", run_id, os.path.getmtime(out))
    if key not in _RULES_CACHE:
        import report as report_mod
        _RULES_CACHE[key] = report_mod.report(src, out)
    return _RULES_CACHE[key]


@app.get("/api/case/{case_id}/run/{run_id}/summary")
def run_summary(case_id: int, run_id: int):
    """План года цифрами: бюджет, освоение по месяцам, структура выплат, люди."""
    src, out = _run_files(db_run(case_id, run_id))
    key = ("сводка", run_id, os.path.getmtime(out))
    if key not in _RULES_CACHE:
        import rules as rules_mod
        _RULES_CACHE[key] = rules_mod.summary(src, out)
    return _RULES_CACHE[key]


def db_run(case_id: int, run_id: int):
    db = session()
    try:
        run = db.get(Run, run_id)
        if run is None or run.case_id != case_id:
            raise HTTPException(404, "прогон не найден")
        return run.input_path, run.result_path
    finally:
        db.close()


def _run_files(pair):
    src, out = pair
    if not src or not out or not os.path.exists(src) or not os.path.exists(out):
        raise HTTPException(404, "файлы прогона не сохранены")
    return src, out


@app.get("/api/case/{case_id}/run/{run_id}/payroll")
def run_payroll(case_id: int, run_id: int):
    """Состав зарплаты по месяцам: договоры, ставки, виды выплат, смены схемы."""
    src, out = _run_files(db_run(case_id, run_id))
    key = ("состав", run_id, os.path.getmtime(out))
    if key not in _RULES_CACHE:
        import rules as rules_mod
        _RULES_CACHE[key] = rules_mod.payroll(src, out)
    return _RULES_CACHE[key]


@app.get("/api/case/{case_id}/run/{run_id}/result")
def run_result(case_id: int, run_id: int):
    """Результат прогона: план, освоение, сводка, ограничения.

    Читается из файла, который сохранил решатель, а не пересчитывается: для
    ГОЗ важно показывать ровно то, на чем построен план.
    """
    db = session()
    try:
        run = db.get(Run, run_id)
        if run is None or run.case_id != case_id:
            raise HTTPException(404, "прогон не найден")
    finally:
        db.close()
    path = os.path.join(RESULT_DIR, "case%d_run%d.json" % (case_id, run_id))
    if not os.path.exists(path):
        raise HTTPException(404, "разбор результата не сохранен")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── справочник ──────────────────────────────────────────────────
@app.get("/api/reference")
def get_reference():
    return {"ok": True, "rows": reference.read_rows()}


@app.post("/api/reference")
async def post_reference(request: Request):
    body = await request.json()
    applied = reference.apply(body.get("edits") or [])
    case_id = body.get("case_id")
    if case_id:
        db = session()
        try:
            case_id = int(case_id)
            agents.say(db, case_id,
                       "Записал в справочник %d %s."
                       % (len(applied), intake._plural(len(applied), "значение",
                                                       "значения", "значений")),
                       agent="norms",
                       payload={"kind": "reference_applied", "applied": applied})
            if applied:
                # Оклады и предельные размеры — исходные данные расчета:
                # после правки прежний план посчитан по старым величинам.
                agents.handoff(db, case_id, "norms", "solver",
                               "Справочник изменен — прежний план посчитан по "
                               "старым величинам, нужен пересчет.")
        finally:
            db.close()
    return {"ok": True, "applied": applied}


@app.post("/api/document/{doc_id}/reference")
async def document_reference(doc_id: int, request: Request):
    """Записать в справочник расхождения, найденные этим документом.

    Та же запись, что из чата дела, но из карточки документа: после неё в
    карточке остаются только незаписанные строки, а дело получает пересчёт.
    """
    body = await request.json()
    edits = body.get("edits") or []
    db = session()
    try:
        d = db.get(Document, doc_id)
        if d is None:
            raise HTTPException(404, "документ не найден")
        applied = reference.apply(edits)
        gave = {}
        try:
            gave = json.loads(d.gave) if d.gave else {}
        except ValueError:
            gave = {}
        # Величина записана из этого документа — он и есть её источник.
        for field in sorted({a["field"] for a in applied}):
            reference.set_source(field, d.name, d.id, _dt(d.uploaded),
                                 gave.get("основание"), gave.get("действует с"))
        done = {(a["pos"], a["field"]) for a in applied}
        # Строка без изменения — тоже записана: справочник уже такой.
        asked = {(e.get("pos"), e.get("field")) for e in edits}
        rest = [c for c in gave.get("расхождения") or []
                if (c.get("pos"), c.get("field")) not in done
                and (c.get("pos"), c.get("field")) not in asked]
        gave["расхождения"] = rest
        gave["расхождений"] = len(rest)
        d.gave = json.dumps(gave, ensure_ascii=False)
        d.summary = "расхождений %d" % len(rest)
        if d.case_id:
            agents.say(db, d.case_id,
                       "Записал в справочник %d %s из «%s»."
                       % (len(applied), intake._plural(len(applied), "значение",
                                                       "значения", "значений"), d.name),
                       agent="norms", document_id=d.id,
                       payload={"kind": "reference_applied", "applied": applied})
            if applied:
                agents.handoff(db, d.case_id, "norms", "solver",
                               "Справочник изменен — прежний план посчитан по "
                               "старым величинам, нужен пересчет.")
        db.commit()
        return {"ok": True, "applied": applied, "осталось": rest}
    finally:
        db.close()


@app.post("/api/document/{doc_id}/source")
async def document_source(doc_id: int, request: Request):
    """Назвать документ источником величин справочника: {"fields": ["П4"]}.

    Скан без текстового слоя или письмо с нечитаемым слоем агент прочитать
    не может, а предел из него в справочнике стоит — внесён рукой. Связь
    «величина ← документ» экономист ставит сам; она уходит в основание
    каждого расчёта.
    """
    body = await request.json()
    fields = [str(f).strip() for f in (body.get("fields") or []) if str(f).strip()]
    db = session()
    try:
        d = db.get(Document, doc_id)
        if d is None:
            raise HTTPException(404, "документ не найден")
        gave = {}
        try:
            gave = json.loads(d.gave) if d.gave else {}
        except ValueError:
            gave = {}
        done = [f for f in fields
                if reference.set_source(f, d.name, d.id, _dt(d.uploaded),
                                        gave.get("основание"), gave.get("действует с"))]
        if done:
            gave["источник для"] = sorted(set((gave.get("источник для") or []) + done))
            d.gave = json.dumps(gave, ensure_ascii=False)
            case_id = d.case_id or (db.query(Case).order_by(Case.updated.desc()).first() or Case(id=None)).id
            if case_id:
                agents.say(db, case_id, "«%s» назван источником величин справочника: %s."
                           % (d.name, ", ".join(done)), who="экономист", document_id=d.id)
            db.commit()
        return {"ok": True, "fields": done, "sources": reference.read_sources()}
    finally:
        db.close()


# ── расчет ──────────────────────────────────────────────────────
def _explain_failure(db, case, run, src, out, err_text=""):
    """Почему решения нет — словами экономиста, а не кодом статуса.

    Решатель и при отказе пишет файл с листами «Итог расчета» и «Проблемы и
    предупреждения»: какое ограничение не сошлось и на каком объекте. Этого
    мало: «отклонение по строке трудоемкости C_NEW / Специалист» не говорит,
    что делать. Агент сверяет каждую ошибку с реестром — есть ли вообще
    сотрудник с такой должностью, может ли ее кто-то занять по правилам
    замещения, действует ли договор в эти месяцы — и называет причину и
    выход. Проверки решателя при этом сохраняются как результат прогона:
    вкладка «Ограничения» показывает их и для неудачного расчета.
    """
    import re
    import result2json
    from fot_planner.position_reference import normalize_position

    found = {"ошибки": [], "почему": [], "has_result": False, "текст": ""}
    warnings = []
    if os.path.exists(out):
        try:
            js = os.path.join(RESULT_DIR, "case%d_run%d.json" % (case.id, run.id))
            result2json.convert(src, out, js)
            with open(js, encoding="utf-8") as f:
                data = json.load(f)
            warnings = [w for w in data.get("warnings") or [] if w and w[0] == "Ошибка"]
            found["has_result"] = True
        except Exception as exc:  # noqa: BLE001 — разбор важнее файла проверок
            found["почему"].append("Проверки решателя прочитать не удалось: %s" % str(exc)[:160])
    found["ошибки"] = warnings
    # Если решатель не нашёл ни одного плана, все строки трудоёмкости
    # «не закрыты» на 100 % — это следствие, а не причина. Пересказывать их
    # по одной незачем: экономист прочтёт пять абзацев про должности, а
    # дело в деньгах или в жёстком ограничении.
    labor_errs = [w for w in warnings if w[1] == "Трудоёмкость"]
    no_plan = bool(labor_errs) and all(
        (w[5] is not None and abs(float(w[5] or 0)) >= 0.999 * _labor_plan_sum(db, w[2]))
        for w in labor_errs) and _labor_plan_sum(db, labor_errs[0][2]) > 0
    if no_plan:
        warnings = [w for w in warnings if w[1] != "Трудоёмкость"]
        found["почему"].append(
            "Ни одного допустимого плана не нашлось: ни одна строка трудоемкости не закрыта "
            "даже частично, значит, дело не в подборе людей, а в жестком условии — деньгах "
            "по месяцам, сроках договоров или числе людей по строкам РКМ.")

    employees = db.query(Employee).all()
    subs = db.query(Substitution).all()
    contracts = {(c.code or "").strip().lower(): c for c in db.query(Contract).all()}

    def holders(position):
        """Кто может занять должность: напрямую или по правилу замещения."""
        want = normalize_position(position)
        direct = [e for e in employees if normalize_position(e.position or "") == want]
        via = []
        for s in subs:
            allowed = [normalize_position(x.strip()) for x in (s.replaced_by or "").split(",")]
            if want in allowed:
                via += [e for e in employees
                        if normalize_position(e.position or "") == normalize_position(s.position or "")
                        and e not in direct and e not in via]
        return direct, via

    for w in warnings:
        section, obj, descr = w[1], w[2], w[4]
        if section == "Трудоёмкость" and obj and "/" in str(obj):
            code, position = [x.strip() for x in str(obj).split("/", 1)]
            row = next((r for r in db.query(LaborRow).all()
                        if (r.contract_code or "").strip().lower() == code.lower()
                        and normalize_position(r.position or "") == normalize_position(position)),
                       None)
            need = ("%s чел.-мес." % _n(row.person_months)) if row and row.person_months else "трудоемкость"
            direct, via = holders(position)
            c = contracts.get(code.lower())
            span = (" (действует %s — %s)" % (c.date_from, c.date_to)) if c and c.date_from else ""
            if not direct and not via:
                found["почему"].append(
                    "Договор %s%s требует %s по должности «%s», а в реестре нет "
                    "сотрудника с такой должностью, и по правилам замещения ее никто "
                    "занять не может. Выход: добавить сотрудника, исправить должность "
                    "в строке трудоемкости или добавить правило замещения."
                    % (code, span, need, position))
            else:
                who = ", ".join((e.fio or e.code) for e in (direct + via)[:4])
                text = ("Договор %s%s требует %s по должности «%s». Занять ее могут: %s%s."
                        % (code, span, need, position, who,
                           " (по правилам замещения)" if via and not direct else ""))
                # Чаще всего не сходится цена: в РКМ чел.-мес. стоит одно, а
                # оклад тех, кто будет работать, — другое. Тогда либо не
                # хватает ФОТ договора на все чел.-мес., либо чел.-мес.
                # выходит меньше плана.
                sal = [float(e.salary) / float(e.rate or 1) for e in direct + via
                       if e.salary and float(e.salary) > 0]
                cost = float(row.avg_cost) if row and row.avg_cost else None
                if sal and cost and row.person_months:
                    typical = sorted(sal)[len(sal) // 2]
                    if abs(typical - cost) / cost > 0.05:
                        text += (" Но средняя стоимость чел.-мес. в строке трудоемкости — "
                                 "%s ₽, а оклад этих сотрудников на ставку — %s ₽: %s чел.-мес. "
                                 "стоят %s ₽ при ФОТ договора %s ₽. Выход: уточнить среднюю "
                                 "стоимость или чел.-мес. в РКМ либо ФОТ договора."
                                 % (_money(cost), _money(typical), _n(row.person_months),
                                    _money(typical * float(row.person_months)),
                                    _money(c.fund) if c and c.fund else "—"))
                    else:
                        text += (" Распределить трудоемкость все равно не удалось: проверьте "
                                 "сроки договора и сотрудников, их ставки и допуск "
                                 "трудоемкости в настройках.")
                else:
                    text += (" Распределить трудоемкость не удалось: проверьте сроки "
                             "договора и сотрудников, их ставки и допуск трудоемкости "
                             "в настройках.")
                found["почему"].append(text)
        elif section == "Конфликт":
            m = re.search(r"\(([^)]+)\)", str(descr or ""))
            if m and not no_plan:
                found["почему"].append("Решатель назвал ограничение, которое не сошлось: %s."
                                       % m.group(1))
        else:
            where = " · ".join(str(x) for x in (obj, w[3]) if x not in (None, "", "—"))
            found["почему"].append("%s%s%s" % (descr, (" — " + where) if where else "",
                                               (". " + str(w[6])) if len(w) > 6 and w[6] else ""))

    if not found["почему"] and not found["has_result"] and err_text:
        # Не «нет решения», а остановка на входных данных: последняя строка
        # ошибки говорит, какая графа не прочиталась.
        last = [ln for ln in err_text.splitlines() if ln.strip()][-1][:300]
        found["почему"].append("Решатель остановился с ошибкой, до поиска плана не дошло: %s. "
                               "Это ошибка сборки входного файла — сообщите разработчику." % last)
    if not found["почему"]:
        found["почему"].append("Решатель не назвал ограничение. Проверьте предупреждения "
                               "сборки входа в ленте и поступления по договорам.")
    # Во что обойдется выход: те же данные, но с одним ослабленным условием.
    # Первое, при котором план сходится, и есть цена вопроса.
    found["опыты"] = _try_relaxations(src, case, run)
    for name, status, cost in found["опыты"]:
        if status == "сходится":
            found["почему"].append("План сходится, если %s%s." % (name, (": " + cost) if cost else ""))
            break
    found["текст"] = "Решения нет. " + " ".join(found["почему"])
    return found


#: Ослабления для опытов: подпись, графа листа «настройки», значение.
_RELAXATIONS = (
    ("допустить дефицит выплат", "разрешить дефицит", "да"),
    ("не требовать точного попадания в трудоемкость", "допуск трудоёмкости", 1),
    ("не требовать равномерного освоения", "штраф отклонения от равномерного освоения", 0),
)


def _registry_ready(db):
    """Реестр организации полон для расчета: люди, договоры, поступления."""
    return bool(db.query(Employee).count() and db.query(Contract).count()
                and db.query(Inflow).count())


def _labor_plan_sum(db, obj):
    """Плановая сумма строки РКМ «договор / должность» — для отсечки «факт 0»."""
    from fot_planner.position_reference import normalize_position
    try:
        code, position = [x.strip() for x in str(obj).split("/", 1)]
    except ValueError:
        return 0.0
    for r in db.query(LaborRow).all():
        if ((r.contract_code or "").strip().lower() == code.lower()
                and normalize_position(r.position or "") == normalize_position(position)):
            return float(r.person_months or 0) * float(r.avg_cost or 0)
    return 0.0


def _try_relaxations(src, case, run):
    """Прогнать те же данные с одним ослабленным условием — по очереди.

    Это дешевый аналог поиска минимального конфликта: три прогона по несколько
    секунд вместо разбора модели. Ответ экономисту — не «INFEASIBLE», а «план
    сходится, если допустить дефицит: общий дефицит 630 000 ₽, первый месяц —
    июнь». Опыт, при котором план сошелся, останавливает перебор.
    """
    from openpyxl import load_workbook

    out = []
    for name, column, value in _RELAXATIONS:
        try:
            tmp = os.path.join(RESULT_DIR, "case%d_run%d_relax%d.xlsx" % (case.id, run.id, len(out) + 1))
            res = tmp.replace(".xlsx", "_out.xlsx")
            wb = load_workbook(src)
            ws = wb["настройки"]
            hdr = [c.value for c in ws[1]]
            if column not in hdr:
                out.append((name, "графы нет в шаблоне", ""))
                continue
            ws.cell(row=2, column=hdr.index(column) + 1).value = value
            wb.save(tmp)
            limit = 480 if column == "разрешить дефицит" else 180
            p = subprocess.run([EXE, "solve", "-i", tmp, "-o", res],
                               capture_output=True, text=True, timeout=limit, cwd=ROOT)
            if p.returncode != 0:
                out.append((name, "не сходится", ""))
                continue
            cost = _relaxation_cost(res)
            out.append((name, "сходится", cost))
            break
        except subprocess.TimeoutExpired:
            # Модель с дефицитом тяжелее основной: три минуты — предел опыта.
            out.append((name, "не уложился в %d минут" % (limit // 60), ""))
        except Exception as exc:  # noqa: BLE001 — опыт не должен ронять разбор
            out.append((name, "опыт не удался: %s" % str(exc)[:120], ""))
    return out


def _solver_warnings(path):
    """Строки листа «Проблемы и предупреждения» готового результата."""
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True)
    if "Проблемы и предупреждения" not in wb.sheetnames:
        return []
    rows = list(wb["Проблемы и предупреждения"].iter_rows(values_only=True))
    out = []
    for r in rows[1:]:
        if not r or not r[0]:
            continue
        out.append({"уровень": r[0], "раздел": r[1], "объект": r[2], "месяц": r[3],
                    "что": r[4], "сумма": r[5], "куда": r[6] if len(r) > 6 else None,
                    "совет": r[7] if len(r) > 7 else None})
    return out


def _review_result(db, case_id, run_id, out):
    """Агент проверки: пересказать предупреждения решателя экономисту.

    Решатель пишет их в лист результата, который в интерфейсе не виден.
    Список — детерминированный, слова — модели: она называет числа и
    говорит, на что смотреть, но ничего не добавляет от себя.
    """
    try:
        rows = _solver_warnings(out)
    except Exception as exc:  # noqa: BLE001 — проверка не должна ронять расчет
        agents.say(db, case_id, "Предупреждения решателя прочитать не удалось: %s" % str(exc)[:120],
                   agent="checker")
        db.commit()
        return
    if not rows:
        agents.say(db, case_id, "Проверки решателя замечаний не нашли.", agent="checker",
                   payload={"kind": "review", "run_id": run_id, "строк": 0})
        db.commit()
        return
    lines = []
    for r in rows:
        where = " · ".join(str(x) for x in (r["объект"], r["месяц"]) if x not in (None, "", "—"))
        amount = (" — %s ₽" % _money(r["сумма"])) if isinstance(r["сумма"], (int, float)) else ""
        lines.append("%s, %s: %s%s%s" % (r["уровень"], r["раздел"], r["что"],
                                        (" (%s)" % where) if where else "", amount))
    text = "Предупреждения решателя (%d):\n" % len(rows) + "\n".join("• " + x for x in lines[:12])
    if len(lines) > 12:
        text += "\n• … и еще %d" % (len(lines) - 12)
    # Слова — от модели, если она доступна; список остаётся как есть.
    name, model, _why = llm.provider()
    if name and name != "anthropic":
        try:
            raw = llm._call_openai_compatible(
                "Предупреждения решателя по готовому плану ФОТ:\n" + "\n".join(lines),
                "проверка", model, llm._endpoint(name), api_key=llm._key(name),
                schema=llm._use_schema(name), no_thinking=llm._no_thinking(name),
                insecure=llm._truthy(os.environ.get("FOT_LLM_INSECURE_TLS")),
                system="Ты — агент проверки результата в сервисе планирования ФОТ. "
                       "Экономисту нужно две-четыре фразы: что решатель отметил, с числами, "
                       "и на что смотреть в первую очередь. Ничего не добавляй от себя, "
                       "не советуй того, чего нет в списке.",
                shape='Ответь одним объектом JSON: {"reply": "текст для экономиста"}',
                json_schema={"type": "object", "properties": {"reply": {"type": "string"}},
                             "required": ["reply"], "additionalProperties": False},
                schema_name="review_reply", max_tokens=600)
            if raw and raw.get("reply"):
                text = raw["reply"].strip() + "\n\n" + text
        except Exception:  # noqa: BLE001 — без модели остаётся список
            pass
    agents.say(db, case_id, text, agent="checker",
               payload={"kind": "review", "run_id": run_id, "строк": len(rows)})
    db.commit()


def _read_goals(path):
    """Лист «цели» результата: название, значение, единица, приоритет, вес."""
    from openpyxl import load_workbook

    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception:  # noqa: BLE001 — файла нет или он битый
        return []
    if "цели" not in wb.sheetnames:
        return []
    rows = list(wb["цели"].iter_rows(values_only=True))
    if not rows:
        return []
    head = [str(h) for h in rows[0]]
    return [dict(zip(head, r)) for r in rows[1:] if r and r[0]]


def _goals_diff_text(db, case_id, run_id, goals):
    """Что изменилось по взвешенным целям против предыдущего удачного прогона."""
    prev = (db.query(Run).filter(Run.case_id == case_id, Run.id < run_id,
                                 Run.status == "OPTIMAL")
            .order_by(Run.id.desc()).first())
    if prev is None or not prev.summary:
        return ""
    before = {g["цель"]: g for g in (json.loads(prev.summary).get("цели") or [])}
    parts, moved = [], False
    for g in goals or []:
        if g.get("приоритет") != "вес" or g["цель"] not in before:
            continue
        was, now_ = before[g["цель"]].get("значение"), g.get("значение")
        try:
            same = abs(float(was) - float(now_)) < 0.5
        except (TypeError, ValueError):
            same = was == now_
        moved = moved or not same
        unit = g.get("единица") or ""
        fmt = _money if unit == "₽" else _n
        parts.append("%s: %s → %s %s" % (g["цель"].lower(), fmt(was), fmt(now_), unit))
    if not parts:
        return ""
    head = " Против прошлого расчета " + ("изменилось: " if moved else
                                         "ничего не сдвинулось — претензия план не изменила, "
                                         "упёрлось в правила или данные. ")
    return head + "; ".join(parts) + "."


def _relaxation_cost(path):
    """Цена ослабления по листу «Итог расчета»: дефицит, первый месяц, отклонения."""
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True)
    if "Итог расчета" not in wb.sheetnames:
        return ""
    rows = {str(r[0]): r[1:] for r in wb["Итог расчета"].iter_rows(values_only=True) if r and r[0]}
    parts = []
    d = rows.get("Общий дефицит")
    if d and d[0]:
        parts.append("общий дефицит %s ₽" % _money(d[0]))
        m = rows.get("Первый месяц дефицита")
        if m and m[0] not in (None, "—"):
            parts.append("первый месяц дефицита — %s" % m[0])
    lab = rows.get("Строк трудоёмкости с отклонением")
    if lab and lab[0]:
        parts.append("строк трудоемкости с отклонением: %s" % lab[0])
    return ", ".join(parts)


def _money(v):
    try:
        return "{:,.0f}".format(float(v)).replace(",", "\u202f")
    except (TypeError, ValueError):
        return str(v)


def _n(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    return ("%.2f" % v).rstrip("0").rstrip(".").replace(".", ",")


def _run_sources(db):
    """Что легло в основание расчета: документы и версии агентов.

    Документ попадает сюда, если хоть одна строка реестра ссылается на него.
    У каждого — имя, версия и что он дал реестру. Отпечаток содержимого тоже
    сохраняется в записи прогона, но нигде не показывается: экономисту он
    ничего не говорит, а служебную сверку сервис делает сам при загрузке.
    """
    rows = {}
    for model, what in ((Employee, "сотрудников"), (Contract, "договоров"),
                        (LaborRow, "строк трудоемкости"), (Inflow, "поступлений"),
                        (SecretAllowance, "надбавок 120"),
                        (Substitution, "правил замещения")):
        for r in db.query(model).all():
            if r.document_id:
                rows.setdefault(r.document_id, {})
                rows[r.document_id][what] = rows[r.document_id].get(what, 0) + 1
    docs = []
    for doc_id, counts in sorted(rows.items()):
        d = db.get(Document, doc_id)
        if d is None:
            continue
        docs.append({"id": d.id, "имя": d.name, "версия": d.version or 1,
                     "отпечаток": d.sha256, "загружен": _dt(d.uploaded),
                     "строк": counts})
    return {"документы": docs,
            "агенты": {a: agents.version_of(a) for a in ("intake", "norms", "solver")},
            "справочник": {"файл": os.path.basename(reference.TEMPLATE),
                           "должностей": len(reference.read_rows()),
                           "источники": reference.read_sources()}}


def _write_sources_sheet(path, sources):
    """Лист «источники» в файле результата: план уходит из сервиса в письмо
    и в архив, и должен нести основание с собой."""
    try:
        from openpyxl import load_workbook

        wb = load_workbook(path)
        if "источники" in wb.sheetnames:
            del wb["источники"]
        ws = wb.create_sheet("источники")
        ws.append(["документ", "версия", "загружен", "строк в реестре"])
        for d in sources["документы"]:
            ws.append([d["имя"], d["версия"], d["загружен"],
                       ", ".join("%s %d" % (k, v) for k, v in d["строк"].items())])
        ws.append([])
        for a, v in sources["агенты"].items():
            ws.append(["агент «%s»" % a, v])
        ws.append(["справочник должностей", sources["справочник"]["должностей"]])
        wb.save(path)
    except Exception as exc:  # noqa: BLE001 — результат важнее листа с основанием
        logging.getLogger("fot.app").warning("лист источников не записан: %s", exc)


def _solve(case_id: int, run_id: int, settings: dict | None):
    db = session()
    try:
        case = db.get(Case, case_id)
        run = db.get(Run, run_id)
        with agents.working(db, case_id, "intake", "собирает входной файл для расчета") as w:
            # Вход — часть версии расчёта. Общее имя дела перезаписывалось
            # при следующем запуске, и старый результат начинал сравниваться
            # с новым составом сотрудников и ограничений.
            src = os.path.join(RESULT_DIR, "case%d_run%d_input.xlsx" %
                               (case_id, run_id))
            warn = []
            data = _registry_data(db, case)
            if settings:
                # Настройки прогона поверх настроек из шаблона: сюда попадают
                # веса целей, сдвинутые претензиями экономиста.
                data["settings"] = {**(data.get("settings") or {}),
                                    **{k: v for k, v in settings.items()
                                       if k != "претензии"}}
            _apply_plan_settings(data, case)
            build_input.build(reference.TEMPLATE, src, data, warn)
            run.input_path = src
            sources = _run_sources(db)
            run.sources = json.dumps(sources, ensure_ascii=False)
            # В строке работы — коротко; сами допущения уходят в ленту
            # сообщениями и лежат в артефакте, а не растягивают колонку.
            w["detail"] = "вход собран" + (", допущений %d" % len(warn) if warn else "")
            w["artifact"] = {"документов в основании": len(sources["документы"]),
                             "версии агентов": sources["агенты"],
                             "допущения": warn}
            db.commit()
        for line in warn:
            agents.say(db, case_id, line, agent="intake")
        if warn:
            db.commit()

        out = os.path.join(RESULT_DIR, "case%d_run%d.xlsx" % (case_id, run_id))
        with agents.working(db, case_id, "solver", "ищет план") as w:
            t0 = time.time()
            # Лимит — на стадию целей, а их девять. По умолчанию 120 с:
            # на демо-наборе стадия «отклонение от П2556» в него не
            # укладывалась, и оклад с надбавкой не доходили до предела.
            limit = os.environ.get("FOT_SOLVE_TIME_LIMIT", "240")
            p = subprocess.run([EXE, "solve", "-i", src, "-o", out,
                                "--time-limit", str(limit)],
                               capture_output=True, text=True, timeout=3600, cwd=ROOT)
            sec = round(time.time() - t0, 1)
            w["detail"] = "%s c" % sec
            db.commit()

        run.seconds = sec
        solver_status = ""
        audit_status, audit_lines = "", []
        for line in (p.stdout or "").splitlines():
            if line.startswith("Статус:"):
                solver_status = line.split(":", 1)[1].strip()
            elif line.startswith("Аудит:"):
                audit_status = line.split(":", 1)[1].strip()
            elif audit_status and line.startswith("  ["):
                audit_lines.append(line.strip())
        if p.returncode != 0 and not solver_status and not (p.stdout or "").strip():
            # Решатель не сказал ничего: его сняли (перезапуск сервера, снятие
            # процесса). Это не «решения нет» — разбор причин неразрешимости
            # здесь только займёт машину экспериментами на полчаса.
            run.status = "прерван"
            case.stage = "прерван"
            run.summary = json.dumps({"error": (p.stderr or "")[-800:]}, ensure_ascii=False)
            db.commit()
            agents.say(db, case_id, "Расчет прерван — решатель был остановлен. "
                       "Запустите расчет заново.", agent="solver")
            db.commit()
            return
        if p.returncode != 0 and solver_status in ("NOT_SOLVED", "TIME_LIMIT", "UNKNOWN", "ошибка"):
            # Решатель не ответил в отведённое время — это не «решения нет»,
            # а «не успел»: разбор причин неразрешимости тут ни к чему.
            run.status = "не успел"
            case.stage = "не успел"
            run.summary = json.dumps({"error": p.stdout[-800:]}, ensure_ascii=False)
            db.commit()
            agents.say(db, case_id, "Расчет не уложился в отведенное время (сервер был занят "
                       "другими расчетами). Запустите еще раз, когда предыдущие расчеты "
                       "завершатся.", agent="solver")
            db.commit()
            return
        if audit_status == "FAIL":
            # Решатель свою модель решил, но план не проходит правила
            # организации. Готовым его показывать нельзя: экономист увидит
            # числа, которых кадровик не оформит.
            run.status = "не прошёл проверку"
            case.stage = "не прошёл проверку"
            run.summary = json.dumps({"аудит": audit_lines}, ensure_ascii=False)
            db.commit()
            agents.say(db, case_id, "\n".join(
                ["План посчитан, но не прошёл проверку результата:"]
                + audit_lines[:10]
                + ["Это ошибка расчёта, а не данных: такие числа в приказ не "
                   "попадут. План не считается готовым."]), agent="solver")
            db.commit()
            return
        if p.returncode != 0:
            run.status = "нет решения"
            case.stage = "нет решения"
            agents.handoff(db, case_id, "solver", "infeasible",
                           "Решения нет, передаю разбор причин агенту «Анализ "
                           "невыполнимости».")
            db.commit()
            with agents.working(db, case_id, "infeasible",
                                "разбирает, почему решения нет") as w:
                found = _explain_failure(db, case, run, src, out,
                                         (p.stderr or p.stdout or "").strip())
                w["detail"] = ("причин: %d" % len(found["почему"])) if found["почему"] \
                              else "решатель причину не назвал"
                w["artifact"] = {"ошибок решателя": len(found["ошибки"]),
                                 "причины": found["почему"],
                                 "проверки сохранены": found["has_result"]}
            run.summary = json.dumps({"error": (p.stderr or p.stdout or "")[-800:],
                                      "анализ": found["почему"],
                                      "опыты": found.get("опыты") or [],
                                      "has_result": found["has_result"]},
                                     ensure_ascii=False)
            db.commit()
            agents.say(db, case_id, found["текст"], agent="infeasible",
                       payload={"kind": "infeasible", "run_id": run_id,
                                "почему": found["почему"]})
            db.commit()
            return

        run.status = "OPTIMAL"
        run.result_path = out
        _write_sources_sheet(out, sources)
        summary = {}
        try:
            import result2json
            js = os.path.join(RESULT_DIR, "case%d_run%d.json" % (case_id, run_id))
            result2json.convert(src, out, js)
            with open(js, encoding="utf-8") as f:
                data = json.load(f)
            summary = {"plan_rows": len(data.get("plan") or []),
                       "employees": len(data.get("employees") or []),
                       "contracts": len(data.get("contracts") or [])}
        except Exception as exc:  # noqa: BLE001
            summary = {"note": "результат посчитан, разбор для карточки не удался: %s" % exc}
        # Цели решателя: что вышло по каждой и какой вес держал. Экономист
        # сравнивает два прогона по этим числам, а не по всему плану.
        summary["цели"] = _read_goals(out)
        if settings and settings.get("претензии"):
            summary["претензии"] = settings["претензии"]
        run.summary = json.dumps(summary, ensure_ascii=False)
        case.stage = "посчитано"
        db.commit()
        text = "План посчитан за %s с. Строк плана — %s." % (sec, summary.get("plan_rows", "?"))
        # После претензии экономисту нужен не план, а ответ: сдвинулось ли то,
        # на что он жаловался. Сравниваем цели с предыдущим удачным прогоном.
        if settings and settings.get("претензии"):
            text += _goals_diff_text(db, case_id, run_id, summary["цели"])
        agents.say(db, case_id, text, agent="solver",
                   payload={"kind": "run", "run_id": run_id, **summary})
        db.commit()
        _review_result(db, case_id, run_id, out)
    except Exception as exc:  # noqa: BLE001
        run = db.get(Run, run_id)
        if run:
            run.status = "ошибка"
            run.summary = json.dumps({"error": str(exc)[:500]}, ensure_ascii=False)
            db.commit()
        agents.say(db, case_id, "Расчет не выполнен: %s" % exc, agent="solver")
        db.commit()
    finally:
        db.close()


@app.post("/api/case/{case_id}/solve")
async def solve(case_id: int, background: BackgroundTasks, request: Request):
    body = await request.json() if await request.body() else {}
    db = session()
    try:
        case = db.get(Case, case_id)
        if case is None:
            raise HTTPException(404, "план не найден")
        settings = body.get("settings")
        if not settings:
            # Кнопка наследует веса последнего удачного расчёта: претензии
            # экономиста не должны сбрасываться от того, что он нажал кнопку,
            # а не написал в чат.
            last = (db.query(Run).filter_by(case_id=case_id, status="OPTIMAL")
                    .order_by(Run.id.desc()).first())
            settings = json.loads(last.settings) if last and last.settings else {}
        busy = _running_run(db, case_id)
        if busy is not None:
            return {"ok": True, "run_id": busy.id, "идет": True}
        run = Run(case_id=case_id, settings=json.dumps(settings, ensure_ascii=False))
        db.add(run)
        db.commit()
        agents.say(db, case_id, "Запускаю расчет.", who="экономист")
        background.add_task(_solve, case_id, run.id, settings)
        return {"ok": True, "run_id": run.id}
    finally:
        db.close()


@app.delete("/api/case/{case_id}/run/{run_id}")
def delete_run(case_id: int, run_id: int):
    """Удалить версию расчёта: запись прогона и его файлы.

    Версии копятся: каждый пересчёт после правки данных или фразы в чате —
    ещё одна. Экономисту нужно уметь убрать неудачную, иначе список версий
    превращается в свалку, а на показе непонятно, какую открывать. Данные
    реестра при этом не трогаются: прогон — это только результат счёта.
    """
    db = session()
    try:
        run = db.get(Run, run_id)
        if run is None or run.case_id != case_id:
            raise HTTPException(404, "прогон не найден")
        case = db.get(Case, case_id)
        for path in (run.input_path, run.result_path,
                     os.path.join(RESULT_DIR, "case%d_run%d.json" % (case_id, run_id)),
                     os.path.join(RESULT_DIR, "case%d_run%d_report.xlsx" % (case_id, run_id))):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        db.delete(run)
        db.commit()
        # Последняя удачная версия ушла — план снова «готов к расчету»,
        # иначе он числится посчитанным, а показывать нечего.
        left = (db.query(Run).filter_by(case_id=case_id, status="OPTIMAL")
                .order_by(Run.id.desc()).first())
        if case is not None and left is None and case.stage == "посчитано":
            case.stage = "готово к расчету"
        agents.say(db, case_id, "Удалена версия расчета № %d." % run_id, who="экономист")
        db.commit()
        return {"ok": True, "run_id": run_id,
                "осталось": db.query(Run).filter_by(case_id=case_id).count()}
    finally:
        db.close()


@app.get("/api/case/{case_id}/result/{run_id}")
def download_result(case_id: int, run_id: int):
    """Скачать план: разделы отчёта листами (как на экране) плюс листы решателя.

    Раньше отдавался файл решателя, и в нём не было половины таблиц вкладки
    «План ФОТ». Книга собирается при первом скачивании и хранится рядом с
    результатом; пересобирается, если результат изменился.
    """
    db = session()
    try:
        run = db.get(Run, run_id)
        if run is None or run.case_id != case_id or not run.result_path:
            raise HTTPException(404, "результат не найден")
        src, out = run.input_path, run.result_path
        summary = json.loads(run.summary) if run.summary else {}
        versions = [{"номер": r.id, "статус": r.status, "секунд": r.seconds, "дата": _dt(r.created)}
                    for r in db.query(Run).filter_by(case_id=case_id).order_by(Run.id).all()]
    finally:
        db.close()
    if not (src and os.path.exists(src) and os.path.exists(out)):
        return FileResponse(out, filename="план_ФОТ_%d.xlsx" % run_id)
    book = os.path.join(RESULT_DIR, "case%d_run%d_report.xlsx" % (case_id, run_id))
    if not os.path.exists(book) or os.path.getmtime(book) < os.path.getmtime(out):
        import export_report
        try:
            export_report.build(src, out, book, goals=summary.get("цели"), versions=versions)
        except Exception as exc:  # noqa: BLE001 — без отчёта остаётся файл решателя
            agents_log = "выгрузка отчёта не собралась: %s" % exc
            print(agents_log)
            return FileResponse(out, filename="план_ФОТ_%d.xlsx" % run_id)
    return FileResponse(book, filename="план_ФОТ_%d.xlsx" % run_id)


@app.get("/api/health")
def health():
    name, model, why = llm.provider()
    return {"ok": True, "solver": os.path.exists(EXE),
            "model": {"provider": name, "name": model, "why": why},
            "env": {"source": ENV_SOURCE, "keys": ENV_LOADED}}


class NoCacheStatic(StaticFiles):
    """Статика без кеша.

    Приложение правится по ходу показа, а браузер держит прежнюю копию
    скрипта и стилей: правка есть на диске, а на экране старое поведение.
    Шрифты кешируем — они не меняются и весят заметно больше.
    """

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if path.startswith("fonts/"):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
        return resp


app.mount("/", NoCacheStatic(directory=STATIC, html=True), name="static")
