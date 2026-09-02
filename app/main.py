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

import json
import os
import subprocess
import sys
import time

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "docs", "ui"))

import agents            # noqa: E402
import chat              # noqa: E402
import intake            # noqa: E402
import llm               # noqa: E402
import reference         # noqa: E402
from db import (         # noqa: E402
    Activity, Case, Contract, Document, Employee, Message, Proposal, Question, Run,
    Substitution,
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


# ── сериализация для страницы ───────────────────────────────────
def _dt(v):
    return v.strftime("%d.%m.%Y %H:%M") if v else None


def case_state(db, case):
    # Документы общие для организации: договор на три года обслуживает три
    # плана, и перезагружать его в каждый незачем.
    docs = db.query(Document).order_by(Document.id).all()
    msgs = db.query(Message).filter_by(case_id=case.id).order_by(Message.id).all()
    acts = (db.query(Activity).filter_by(case_id=case.id)
            .order_by(Activity.id.desc()).limit(40).all())
    qs = db.query(Question).filter_by(case_id=case.id).order_by(Question.id).all()
    runs = db.query(Run).filter_by(case_id=case.id).order_by(Run.id.desc()).limit(5).all()
    provider_name, provider_model, why = llm.provider()
    return {
        "case": {"id": case.id, "title": case.title, "year": case.year,
                 "stage": case.stage, "updated": _dt(case.updated),
                 "has_data": bool(case.passport),
                 "counts": {
                     "employees": db.query(Employee).count(),
                     "contracts": db.query(Contract).count(),
                     "substitutions": db.query(Substitution).count()}},
        "documents": [{"id": d.id, "name": d.name, "kind": d.kind, "state": d.state,
                       "by": d.parsed_by, "summary": d.summary,
                       "uploaded": _dt(d.uploaded), "size": d.size} for d in docs],
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
                  "summary": json.loads(r.summary) if r.summary else None} for r in runs],
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
        return [{"id": c.id, "title": c.title, "year": c.year, "stage": c.stage,
                 "updated": _dt(c.updated),
                 # Список группируется по давности, для этого нужна дата,
                 # а не отформатированная строка.
                 "updated_at": c.updated.isoformat() if c.updated else None,
                 "documents": db.query(Document).filter_by(case_id=c.id).count()}
                for c in rows]
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
        if case and doc:
            intake.handle_document(db, case, doc)
    finally:
        db.close()


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


@app.post("/api/case/{case_id}/upload")
async def upload(case_id: int, background: BackgroundTasks, files: list[UploadFile]):
    db = session()
    try:
        case = db.get(Case, case_id)
        if case is None:
            raise HTTPException(404, "план не найден")
        added = []
        for f in files:
            data = await f.read()
            name = _filename(f.filename)
            safe = "%d_%d_%s" % (case_id, int(time.time() * 1000), os.path.basename(name))
            path = os.path.join(UPLOAD_DIR, safe)
            with open(path, "wb") as out:
                out.write(data)
            # Повторная загрузка того же файла — это исправленная редакция,
            # а не второй документ. Прежнюю запись и все извлеченное из нее
            # убираем, иначе реестр зарастает дублями.
            for old_doc in db.query(Document).filter_by(name=name).all():
                _forget_document(db, old_doc)
            doc = Document(case_id=case_id, name=name, path=path, size=len(data))
            db.add(doc)
            db.commit()
            added.append(doc.id)
            agents.say(db, case_id, "Загружен документ «%s»." % name, who="экономист")
        case.stage = "сбор данных"
        db.commit()
        for doc_id in added:
            background.add_task(_process, case_id, doc_id)
        return {"ok": True, "documents": added}
    finally:
        db.close()


@app.get("/api/documents")
def all_documents():
    """Все загруженные документы организации и что из каждого извлечено."""
    db = session()
    try:
        out = []
        for d in db.query(Document).order_by(Document.id.desc()).all():
            out.append({
                "id": d.id, "name": d.name, "kind": d.kind, "state": d.state,
                "by": d.parsed_by, "summary": d.summary, "size": d.size,
                "uploaded": _dt(d.uploaded), "case_id": d.case_id,
                "produced": {
                    "сотрудников": db.query(Employee).filter_by(document_id=d.id).count(),
                    "договоров": db.query(Contract).filter_by(document_id=d.id).count(),
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
        return {
            "id": d.id, "name": d.name, "kind": d.kind, "state": d.state,
            "by": d.parsed_by, "summary": d.summary, "size": d.size,
            "uploaded": _dt(d.uploaded), "case_id": d.case_id,
            "exists": os.path.exists(d.path),
            "employees": [{"code": e.code, "fio": e.fio, "position": e.position,
                           "rate": e.rate, "salary": e.salary,
                           "from": e.date_from, "to": e.date_to} for e in emp],
            "contracts": [{"code": c.code, "name": c.name, "number": c.number,
                           "kind": c.kind, "goz": c.goz, "fund": c.fund,
                           "kinds": c.kinds, "from": c.date_from,
                           "to": c.date_to} for c in ctr],
            "substitutions": [{"position": s.position, "replaced_by": s.replaced_by}
                              for s in sub],
            "proposals": [{"id": pr.id, "entity": pr.entity,
                           "fields": json.loads(pr.payload),
                           "evidence": pr.evidence, "state": pr.state}
                          for pr in db.query(Proposal)
                          .filter_by(document_id=d.id, state="предложено")
                          .order_by(Proposal.id).all()],
        }
    finally:
        db.close()


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
        added = {"сотрудник": 0, "договор": 0, "правило замещения": 0}
        for pr in rows:
            if pr.id in drop:
                pr.state = "отклонено"
                continue
            if pr.id not in take:
                continue
            f = json.loads(pr.payload)
            if pr.entity == "сотрудник":
                db.add(Employee(code=str(f.get("code") or ""), fio=f.get("fio"),
                                position=f.get("pos"), rate=f.get("rate"),
                                salary=f.get("sal"), date_from=f.get("from"),
                                date_to=f.get("to"), source=doc.name,
                                document_id=doc.id))
            elif pr.entity == "договор":
                db.add(Contract(code=str(f.get("code") or ""), name=f.get("name"),
                                number=f.get("num"), kind=f.get("type"),
                                goz=f.get("goz"), fund=f.get("fot"),
                                date_from=f.get("from"), date_to=f.get("to"),
                                source=doc.name, document_id=doc.id))
            else:
                db.add(Substitution(position=str(f.get("position") or ""),
                                    replaced_by=str(f.get("replaced_by") or ""),
                                    source=doc.name, document_id=doc.id))
            pr.state = "принято"
            added[pr.entity] += 1

        left = sum(1 for pr in rows if pr.state == "предложено")
        if not left:
            doc.state = "разобран" if any(added.values()) else "не распознан"
        db.commit()

        if any(added.values()) and doc.case_id:
            agents.say(db, doc.case_id,
                       "Принято из «%s»: %s. Строки записаны в реестр, источник "
                       "— этот документ." % (doc.name, ", ".join(
                           "%s %d" % (k, v) for k, v in added.items() if v)),
                       agent="intake")
            db.commit()
        return {"ok": True, "added": added, "left": left}
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
        return FileResponse(d.path, filename=d.name)
    finally:
        db.close()


def _forget_document(db, doc):
    """Удалить документ вместе со всем, что из него извлечено.

    Данные помнят документ-источник, поэтому сирот не остается: ушел документ —
    ушли его сотрудники, договоры и правила замещения.
    """
    gone = {
        "сотрудников": db.query(Employee).filter_by(document_id=doc.id).delete(),
        "договоров": db.query(Contract).filter_by(document_id=doc.id).delete(),
        "правил замещения": db.query(Substitution).filter_by(document_id=doc.id).delete(),
    }
    # Непринятые предложения уходят вместе с документом: подтверждать строки
    # файла, которого больше нет, не по чему. ON DELETE CASCADE в схеме есть,
    # но SQLite не применяет внешние ключи без PRAGMA foreign_keys.
    db.query(Proposal).filter_by(document_id=doc.id).delete()
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
            lost = ", ".join("%s %d" % (k, v) for k, v in gone.items() if v)
            agents.say(db, case_id,
                       "Удален документ «%s»%s." % (name, ", с ним " + lost if lost else ""),
                       who="экономист")
        return {"ok": True, "removed": gone}
    finally:
        db.close()


# ── лента и вопросы ─────────────────────────────────────────────
@app.post("/api/case/{case_id}/message")
async def post_message(case_id: int, background: BackgroundTasks, request: Request):
    body = await request.json()
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

        if action == "запустить расчет":
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
    if doc.state == "не прочитан":
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
            "employees": [{"code": e.code, "fio": e.fio, "position": e.position,
                           "rate": e.rate, "salary": e.salary,
                           "from": e.date_from, "to": e.date_to,
                           "source": e.source} for e in emps],
            "contracts": [{"code": c.code, "name": c.name, "number": c.number,
                           "kind": c.kind, "goz": c.goz, "fund": c.fund,
                           "kinds": c.kinds, "source": c.source,
                           "from": c.date_from, "to": c.date_to} for c in ctrs],
            # Нормативная база общая: правила и справочник не привязаны к делу.
            "substitutions": [{"position": s.position, "replaced_by": s.replaced_by,
                               "source": s.source} for s in
                              db.query(Substitution).order_by(Substitution.id).all()],
            "reference": reference.read_rows(),
        }
    finally:
        db.close()


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


# ── расчет ──────────────────────────────────────────────────────
def _solve(case_id: int, run_id: int, settings: dict | None):
    db = session()
    try:
        case = db.get(Case, case_id)
        run = db.get(Run, run_id)
        with agents.working(db, case_id, "intake", "собирает входной файл для расчета") as w:
            src = os.path.join(RESULT_DIR, "case%d_input.xlsx" % case_id)
            warn = []
            if case.passport:
                intake.extract.passport_to_input(json.loads(case.passport),
                                                 reference.TEMPLATE, src, warn)
            else:
                import shutil
                shutil.copy(reference.TEMPLATE, src)
            run.input_path = src
            w["detail"] = "; ".join(warn) or "вход собран"
            db.commit()
        for line in warn:
            agents.say(db, case_id, line, agent="intake")
        if warn:
            db.commit()

        out = os.path.join(RESULT_DIR, "case%d_run%d.xlsx" % (case_id, run_id))
        with agents.working(db, case_id, "solver", "ищет план") as w:
            t0 = time.time()
            p = subprocess.run([EXE, "solve", "-i", src, "-o", out],
                               capture_output=True, text=True, timeout=1800, cwd=ROOT)
            sec = round(time.time() - t0, 1)
            w["detail"] = "%s c" % sec
            db.commit()

        run.seconds = sec
        if p.returncode != 0:
            run.status = "нет решения"
            run.summary = json.dumps({"error": (p.stderr or p.stdout or "")[-800:]},
                                     ensure_ascii=False)
            db.commit()
            agents.handoff(db, case_id, "solver", "infeasible",
                           "Решения нет, передаю разбор причин агенту «Анализ "
                           "невыполнимости».")
            agents.say(db, case_id,
                       "Агент анализа невыполнимости в этой сборке не реализован. "
                       "Текст отказа решателя: %s"
                       % ((p.stderr or p.stdout or "").strip()[-300:] or "—"),
                       agent="infeasible")
            db.commit()
            return

        run.status = "OPTIMAL"
        run.result_path = out
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
        run.summary = json.dumps(summary, ensure_ascii=False)
        case.stage = "посчитано"
        db.commit()
        agents.say(db, case_id,
                   "План посчитан за %s с. Строк плана — %s." % (sec, summary.get("plan_rows", "?")),
                   agent="solver", payload={"kind": "run", "run_id": run_id, **summary})
        db.commit()
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
        run = Run(case_id=case_id,
                  settings=json.dumps(body.get("settings") or {}, ensure_ascii=False))
        db.add(run)
        db.commit()
        agents.say(db, case_id, "Запускаю расчет.", who="экономист")
        background.add_task(_solve, case_id, run.id, body.get("settings"))
        return {"ok": True, "run_id": run.id}
    finally:
        db.close()


@app.get("/api/case/{case_id}/result/{run_id}")
def download_result(case_id: int, run_id: int):
    db = session()
    try:
        run = db.get(Run, run_id)
        if run is None or run.case_id != case_id or not run.result_path:
            raise HTTPException(404, "результат не найден")
        return FileResponse(run.result_path, filename="план_ФОТ_%d.xlsx" % run_id)
    finally:
        db.close()


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
