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
import os
import subprocess
import sys
import threading
import time

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile
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
                  "summary": json.loads(r.summary) if r.summary else None,
                  "sources": json.loads(r.sources) if r.sources else None} for r in runs],
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
            _prerender_first_sheet(doc)
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
                           sha256=digest)
            if prev is not None:
                _retire_document(db, prev)
                doc.version = (prev.version or 1) + 1
                doc.supersedes_id = prev.id
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
    counts = [("сотрудников", len(data["employees"])), ("договоров", len(data["contracts"])),
              ("поступлений", len(data["inflows"])), ("строк трудоемкости", len(data["labor"])),
              ("надбавок 120", len(data["secret"])),
              ("правил замещения", len(data["substitutions"]))]
    return {"строки": counts, "допущения": warn,
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
            out.append({
                "id": d.id, "name": d.name, "kind": d.kind, "state": d.state,
                "version": d.version or 1,
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
        if os.path.splitext(d.path)[1].lower() in WORKBOOK_TYPES and os.path.exists(d.path):
            threading.Thread(target=_warm_workbook, args=(d.path,),
                             daemon=True).start()
        return {
            "id": d.id, "name": d.name, "kind": d.kind, "state": d.state,
            "by": d.parsed_by, "summary": d.summary, "size": d.size,
            "формат": os.path.splitext(d.path)[1].lower() or "без расширения",
            "версия": d.version or 1,
            "отпечаток": d.sha256,
            "заменяет": (lambda p: _dt(p.uploaded) if p else None)(
                db.get(Document, d.supersedes_id) if d.supersedes_id else None),
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
                    account=f.get("account"), goz=f.get("goz"),
                    fund=f.get("fot"), kinds=_allowed_kinds(f),
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
            doc.state = "разобран" if any(added.values()) else "не распознан"
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
PREVIEW_CHARS = 2500
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
    открывают не по одному разу. Файл документа не меняется — при повторной
    загрузке заводится новая запись со своим номером, — поэтому кэш можно не
    сбрасывать.
    """
    import hashlib

    from xlsx2html.core import get_sheet, render_table, worksheet_to_data

    key = hashlib.md5(("%d|%s" % (doc.id, sheet or "")).encode("utf-8")).hexdigest()
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
    if ext == ".pdf":
        return {"вид": "документ"}
    if ext in WORKBOOK_TYPES:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(doc.path, data_only=True, read_only=True)
            names = wb.sheetnames
            wb.close()
        except Exception as e:  # noqa: BLE001 — битая книга не должна ронять карточку
            return {"вид": "нет", "почему": str(e)[:200]}
        return {"вид": "книга", "листы": names}
    try:
        text = docread.to_text(doc.path, PREVIEW_CHARS)
    except docread.Unreadable as e:
        return {"вид": "нет", "почему": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"вид": "нет", "почему": str(e)[:200]}
    return {"вид": "текст", "текст": text[:PREVIEW_CHARS],
            "обрезано": len(text) >= PREVIEW_CHARS}


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
async def document_message(doc_id: int, background: BackgroundTasks, request: Request):
    """Разговор о документе: «тут ошибка» — прямо в карточке.

    Раньше неправильно разобранный документ было не исправить: реестр
    показывал, но не слушал. Теперь реплика уходит агенту вместе с состоянием
    документа; правка ложится в реестр, в память агента и в приговор для
    стенда; подсказанный вид — в переразбор.
    """
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "пустое сообщение")
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
        db.add(Message(case_id=cid, document_id=doc.id, who="экономист", text=text))
        db.commit()

        res = chat.reply_doc(db, doc, text)
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
            if case is not None:
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
                       "source": r.source}
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
            if m:
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
    found["текст"] = ("Решения нет. " + " ".join(found["почему"]) +
                      (" Проверки решателя — на вкладке «Ограничения»." if found["has_result"] else ""))
    return found


#: Ослабления для опытов: подпись, графа листа «настройки», значение.
_RELAXATIONS = (
    ("допустить дефицит выплат", "разрешить дефицит", "да"),
    ("не требовать точного попадания в трудоемкость", "допуск трудоёмкости", 1),
    ("не требовать равномерного освоения", "штраф отклонения от равномерного освоения", 0),
)


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
            p = subprocess.run([EXE, "solve", "-i", tmp, "-o", res],
                               capture_output=True, text=True, timeout=180, cwd=ROOT)
            if p.returncode != 0:
                out.append((name, "не сходится", ""))
                continue
            cost = _relaxation_cost(res)
            out.append((name, "сходится", cost))
            break
        except Exception as exc:  # noqa: BLE001 — опыт не должен ронять разбор
            out.append((name, "опыт не удался: %s" % str(exc)[:120], ""))
    return out


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
    У каждого — версия и отпечаток содержимого: по ним через год можно
    проверить, что план построен на этом файле, а не на исправленном.
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
                           "должностей": len(reference.read_rows())}}


def _write_sources_sheet(path, sources):
    """Лист «источники» в файле результата: план уходит из сервиса в письмо
    и в архив, и должен нести основание с собой."""
    try:
        from openpyxl import load_workbook

        wb = load_workbook(path)
        if "источники" in wb.sheetnames:
            del wb["источники"]
        ws = wb.create_sheet("источники")
        ws.append(["документ", "версия", "загружен", "отпечаток SHA-256", "строк в реестре"])
        for d in sources["документы"]:
            ws.append([d["имя"], d["версия"], d["загружен"], d["отпечаток"],
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
            src = os.path.join(RESULT_DIR, "case%d_input.xlsx" % case_id)
            warn = []
            build_input.build(reference.TEMPLATE, src, _registry_data(db, case), warn)
            run.input_path = src
            sources = _run_sources(db)
            run.sources = json.dumps(sources, ensure_ascii=False)
            w["detail"] = "; ".join(warn) or "вход собран"
            w["artifact"] = {"документов в основании": len(sources["документы"]),
                             "версии агентов": sources["агенты"]}
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
