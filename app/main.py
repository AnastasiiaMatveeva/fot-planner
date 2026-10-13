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

import contextlib
import hashlib
import json
import re
import uuid
import socket
import logging
import fastapi
import os
import shutil
import subprocess
import sys
import threading
import time
import types
from urllib.parse import quote

from fastapi import Body, BackgroundTasks, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "docs", "ui"))

import actuality         # noqa: E402
import agents            # noqa: E402
import basis             # noqa: E402
import build_input       # noqa: E402
import chat              # noqa: E402
import docread           # noqa: E402
import facts             # noqa: E402
import intake            # noqa: E402
import llm               # noqa: E402
import order             # noqa: E402
import workgroup         # noqa: E402
import reference         # noqa: E402
import salary            # noqa: E402
from db import (
    Decision, record_decision,         # noqa: E402
    claim_operation, finish_operation,
    Activity, Case, Contract, ContractValue, Correction, Document, Employee, Inflow, LaborRow,
    Message, MgmtDepartment, Proposal, Question, Run, SalaryMonth, SalaryRow, SecretAllowance,
    Substitution,
    Verdict, WorkOrder, WorkOrderMember,
    DATA_DIR, RESULT_DIR, UPLOAD_DIR, init_db, now, session,
)

def _solver_exe():
    """Решатель из окружения проекта: .venv на Windows или Linux, иначе рядом
    с интерпретатором — в докере пакет ставится в системный Python."""
    for path in (os.path.join(ROOT, ".venv", "Scripts", "fot-planner.exe"),
                 os.path.join(ROOT, ".venv", "bin", "fot-planner"),
                 os.path.join(os.path.dirname(sys.executable),
                              "fot-planner.exe" if os.name == "nt" else "fot-planner")):
        if os.path.isfile(path):
            return path
    return path


EXE = _solver_exe()
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

@contextlib.asynccontextmanager
async def _lifespan(_app):
    # Начальная загрузка из папки (SOURCE-015) — только при старте сервера:
    # стенды и скрипты импортируют модуль ради путей и сборщика входа.
    threading.Thread(target=_load_seed, daemon=True).start()
    yield


app = FastAPI(title="Планирование ФОТ", lifespan=_lifespan)
init_db()


#: Исполнитель этого процесса: хост, pid и случайная метка. Пишется в прогон
#: при запуске; по нему видно, чей расчёт идёт, а не только что «идет».
EXECUTOR = "%s:%d:%s" % (socket.gethostname(), os.getpid(), uuid.uuid4().hex[:6])
#: Как часто идущий прогон подтверждает, что жив, и после какого молчания
#: его можно признать брошенным. Порог — четыре пропущенных удара: один
#: пропуск бывает от занятой машины, четыре подряд — от мёртвого процесса.
HEARTBEAT_SEC = 15
STALE_SEC = 4 * HEARTBEAT_SEC


def _run_is_stale(run):
    """Прогон без свежего heartbeat. Прогоны до этой графы (None) — тоже:
    подтвердить, что они живы, некому."""
    if run.heartbeat is None:
        return True
    return (now() - run.heartbeat).total_seconds() > STALE_SEC


def _close_orphans():
    """Строки работы и прогоны, брошенные прошлым запуском.

    Фоновая задача живёт в процессе сервера: перезапуск её убивает, а строка
    «агент работает» и прогон «идет» остаются навсегда — в шапке плана
    висит агент, которого нет. При старте закрываем такие следы честно.

    Честно — значит не трогая чужой живой расчёт: второй экземпляр сервера
    или стенд, поднятый рядом, раньше помечал прерванным прогон, который в
    эту секунду считал другой процесс (RUN-002). Брошенным считается только
    прогон, чей heartbeat замолчал дольше STALE_SEC. Строки работы агентов
    к прогону не привязаны, поэтому их закрываем лишь когда живых прогонов
    нет вовсе.
    """
    from db import Activity, Run, session as _session
    db = _session()
    try:
        running = db.query(Run).filter_by(status="идет").all()
        stale_runs = [r for r in running if _run_is_stale(r)]
        alive = len(running) - len(stale_runs)
        for r in stale_runs:
            r.status = "прерван"
        stale_acts = []
        if not alive:
            stale_acts = db.query(Activity).filter_by(state="идет").all()
            for a in stale_acts:
                a.state = "прервано"
                a.detail = "сервис был перезапущен, работа не завершена"
        if stale_acts or stale_runs:
            db.commit()
        return {"прервано": len(stale_runs), "живых": alive}
    finally:
        db.close()


def _artifacts():
    from db import ARTIFACT_DIR
    from fot_planner.harness_local.artifacts import ArtifactStore
    return ArtifactStore(ARTIFACT_DIR)


def _manifest_draft(run, src, settings, sources, command, limit, attempt_of=None):
    """Закрепить вход до первого действия решателя (VER-002).

    Вход, справочник и версия кода уходят в хранилище артефактов; снимок
    ссылается на их хеши. Путь к файлу в рабочей папке снимком не считается:
    файл перезапишет следующий прогон того же плана.
    """
    from fot_planner.harness_local import manifest as mf
    from fot_planner.harness_local.catalogue import default_catalogue
    store = _artifacts()
    catalogue = default_catalogue()
    required = [{**c, "descriptor_sha256": catalogue.descriptor_digest(c["id"], c["version"])}
                for c in catalogue.required_checks("fot.local")]
    draft = mf.build_manifest(
        run_id=run.id, case_id=run.case_id, executor=run.executor or EXECUTOR,
        input_sha256=store.put_file(src),
        reference_sha256=store.put_file(reference.TEMPLATE) if os.path.isfile(reference.TEMPLATE) else None,
        settings=settings or {}, sources=sources,
        code=mf.code_version(mf.default_src_root()),
        command=[os.path.basename(command[0])] + list(command[1:]), time_limit_sec=limit,
        attempt_of=attempt_of, required_checks=required)
    run.manifest_sha256 = mf.store_manifest(store, draft)
    return draft


def _manifest_final(run, draft, out, solver_status, audit_status, audit_lines, seconds, audit_checks=None,
                    independent_audit=None):
    """Итог после всех преобразований результата — хеш с конечного файла."""
    from fot_planner.harness_local import manifest as mf
    store = _artifacts()
    done = mf.finalize_manifest(
        draft, output_sha256=store.put_file(out) if out and os.path.isfile(out) else None,
        solver_status=solver_status, audit_status=audit_status, audit_lines=audit_lines,
        seconds=seconds, audit_checks=audit_checks, execution_status=run.status)
    if independent_audit is not None:
        done['independent_audit'] = independent_audit
    run.manifest_sha256 = mf.store_manifest(store, done)
    return done


def _manifest_close(db, run_id, draft, draft_digest, out, audit_status, audit_lines, seconds,
                    audit_checks=None, solver_status=None):
    """Закрыть снимок прогона, чем бы тот ни кончился.

    Удачный расчёт закрывает снимок сам, после дозаписи результата. Отказ,
    обрыв и «не успел» раньше оставляли только черновик — а снимок нужен
    каждому выполнению (VER-002): у неудачи тоже есть вход, версия кода и
    статус, и по ним её можно воспроизвести. Черновик узнаём по хешу: если
    ссылка в прогоне уже другая, итог записан раньше.
    """
    if draft is None:
        return None
    run = db.get(Run, run_id)
    if run is None or run.manifest_sha256 != draft_digest:
        return None
    # None — прежний вызывающий код; пустая строка — новый протокол,
    # в котором solver не сообщил статус. Не подменять его состоянием задачи.
    final_solver_status = run.status if solver_status is None else (solver_status or "UNKNOWN")
    done = _manifest_final(run, draft, out, final_solver_status,
                           audit_status, audit_lines, seconds, audit_checks=audit_checks)
    db.commit()
    return done


def _heartbeat(run_id, stop):
    """Пока идёт расчёт, раз в HEARTBEAT_SEC отмечать, что прогон жив.

    Работает в своём потоке со своей сессией: основной поток занят
    подпроцессом решателя и к базе не обращается.
    """
    while not stop.wait(HEARTBEAT_SEC):
        s = session()
        try:
            r = s.get(Run, run_id)
            if r is None or r.status != "идет":
                return
            r.heartbeat = now()
            s.commit()
        except Exception:  # noqa: BLE001 — пропущенный удар не роняет расчёт
            pass
        finally:
            s.close()


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
                 # Факт начислений (30.09.2026): по какой месяц расчёт его
                 # берёт и по каким месяцам года он есть.
                 "fact_through": _fact_through(db, case),
                 "fact_months": facts.months_available(db, case.year),
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
                  "settings": json.loads(r.settings) if r.settings else None,
                  # Актуальность (VER-003): прежний результат остаётся, но
                  # после смены зависимости показывается как требующий пересмотра.
                  "review": r.review or actuality.CURRENT,
                  "review_log": json.loads(r.review_log) if r.review_log else [],
                  "retry_of": r.retry_of,
                  "retryable": r.status in RETRYABLE and bool(r.manifest_sha256)} for r in runs],
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
                        "review": (last.review or actuality.CURRENT) if last else None,
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
            _warm_once(("pdf", doc.id), _warm_pdf, doc.id)
        elif case and doc:
            intake.handle_document(db, case, doc)
            _prerender_first_sheet(doc)
            # Печать в PDF — удобство карточки, а не разбор. Она идёт своим
            # потоком: у книги РКМ тридцать листов, Excel печатает их минуты,
            # и всё это время следующие документы стояли в очереди
            # неразобранными, а расчёт запускался без трудоёмкости.
            _warm_once(("pdf", doc.id), _warm_pdf, doc.id)
    finally:
        db.close()


#: Папка начальной загрузки (SOURCE-015): документы организации, которые
#: сервер разбирает сам при старте — без плана и без подтверждений.
SEED_DIR = os.path.abspath(os.environ.get("FOT_SEED_DIR") or os.path.join(DATA_DIR, "seed"))


def _seed_files(folder):
    """Файлы папки в порядке разбора: сначала перечни должностей — приказ
    правит величины только у заведённых должностей, — затем остальные по
    пути: зарплата и факт ищут людей в реестре, их папки нумеруются после
    штатного и договоров."""
    files = []
    for root, dirs, names in os.walk(folder):
        dirs.sort()
        for name in sorted(names):
            # Служебные файлы Office и скрытые — не документы.
            if not name.startswith(("~$", ".")):
                files.append(os.path.join(root, name))
    first = [f for f in files if intake.positions_layout(f) is not None]
    return first + [f for f in files if f not in first]


def _load_seed(folder=None):
    """Разобрать документы папки, которых ещё нет в базе. Возвращает id
    добавленных документов.

    Плана нет: лента и журнал работ уходят в лог сервера. Расхождения
    нормативных документов записываются в справочник сразу — за содержимое
    папки отвечает тот, кто её положил.
    """
    folder = folder or SEED_DIR
    log = logging.getLogger("fot.seed")
    if not os.path.isdir(folder):
        return []
    org = types.SimpleNamespace(id=None, year=time.localtime().tm_year, passport=None,
                                stage=None)
    added = []
    db = session()
    try:
        known = {sha for (sha,) in db.query(Document.sha256).all() if sha}
        for path in _seed_files(folder):
            with open(path, "rb") as f:
                data = f.read()
            digest = hashlib.sha256(data).hexdigest()
            if digest in known:
                continue
            known.add(digest)
            name = os.path.basename(path)
            stored = os.path.join(UPLOAD_DIR, "seed_%d_%s" % (int(time.time() * 1000), name))
            with open(stored, "wb") as out:
                out.write(data)
            doc = Document(case_id=None, name=name, path=stored, size=len(data),
                           sha256=digest, scope="реестр")
            # Изменённый файл с тем же именем — новая версия, как при загрузке.
            prev = _previous_version(db, name, None)
            if prev is not None:
                _retire_document(db, prev, new_version=(prev.version or 1) + 1)
                doc.version = (prev.version or 1) + 1
                doc.supersedes_id = prev.id
            db.add(doc)
            db.commit()
            added.append(doc.id)
            log.info("начальная загрузка: %s", os.path.relpath(path, folder))
            try:
                intake.handle_document(db, org, doc)
                db.refresh(doc)
                gave = json.loads(doc.gave) if doc.gave else {}
                if doc.kind == "нормативный документ" and gave.get("расхождения"):
                    res = _write_document_reference(db, doc, gave["расхождения"],
                                                    grounds="начальная загрузка")
                    log.info("«%s»: записано в справочник %d", name, len(res["applied"]))
                _prerender_first_sheet(doc)
            except Exception:  # noqa: BLE001 — один файл не останавливает папку
                log.exception("начальная загрузка: «%s» не разобран", name)
                db.rollback()
            db.refresh(doc)
            log.info("«%s»: %s — %s", name, doc.state, doc.summary or doc.kind or "")
    finally:
        db.close()
    return added


#: Грелки, которые уже идут: ключ → поток. Карточку документа опрашивают
#: раз в несколько секунд, и раньше каждый опрос заводил новый поток печати
#: через COM и новый разбор книги. 12.10.2026 прогон пятидесяти документов
#: опрашивал карточки — сотни таких потоков, и сервер падал с 0xC0000005.
_WARMING = set()
_WARMING_LOCK = threading.Lock()


def _warm_once(key, target, *args):
    """Запустить грелку, если такая же ещё не идёт."""
    with _WARMING_LOCK:
        if key in _WARMING:
            return
        _WARMING.add(key)

    def run():
        try:
            target(*args)
        finally:
            with _WARMING_LOCK:
                _WARMING.discard(key)
    threading.Thread(target=run, daemon=True).start()


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


def _set_muted(db, case, doc_id, muted, grounds="флажок в карточке документа"):
    ids = _muted(case)
    if muted:
        ids.add(doc_id)
    else:
        ids.discard(doc_id)
    case.muted_docs = json.dumps(sorted(ids))
    from fot_planner.harness_local import decisions as D
    record_decision(db, kind="состав плана", subject_kind="план", subject_id=case.id,
                    subject_digest=D.digest(sorted(ids)), scope="план %d" % case.id,
                    action={"документ": doc_id, "исключён": bool(muted)}, grounds=grounds)
    actuality.mark_review(db, actuality.runs_of_case(db, case.id),
                          "документ «%s» %s" % (actuality.document_name(db, doc_id),
                                                "исключён из плана" if muted else "возвращён в план"))
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


def _previous_version(db, name, contract=None):
    """Действующая версия документа с этим именем; None — документ новый.

    Без договора — любой документ с тем же именем, как и прежде. При загрузке
    в договор «РКМ.xlsx» соседнего договора — другой документ, а не прежняя
    версия: ищем среди документов этого договора — загруженных в него или
    давших ему строки.
    """
    q = (db.query(Document).filter_by(name=name)
         .filter(Document.state != "заменен").order_by(Document.id.desc()))
    if not contract:
        return q.first()
    for d in q.all():
        if d.contract_code == contract or (not d.contract_code
                                           and contract in basis.links_of(db, d)):
            return d
    return None


@app.post("/api/contracts")
async def create_contract(request: Request):
    """Новый договор реестра: шифр и наименование (UI-006).

    Договор заводится раньше документов: дальше в него загружается пакет, и
    данные документов ложатся на этот договор, а не на шифр из их шапок
    (SOURCE-004).
    """
    body = await request.json()
    code = str(body.get("code") or "").strip()[:60]
    name = str(body.get("name") or "").strip()[:300] or None
    if not code:
        raise HTTPException(400, "нужен шифр договора")
    db = session()
    try:
        if db.query(Contract).filter_by(code=code).first() is not None:
            raise HTTPException(409, "договор %s уже есть в реестре" % code)
        db.add(Contract(code=code, name=name, source="создан в реестре"))
        from fot_planner.harness_local import decisions as D
        record_decision(db, kind="данные", subject_kind="договор", subject_id=code,
                        subject_digest=D.digest(code, name), scope="реестр",
                        action={"создан договор": code, "наименование": name},
                        grounds="«Новый договор» в реестре")
        db.commit()
        return {"ok": True, "code": code}
    finally:
        db.close()


def _contract_docs(db, code):
    """Документы, из которых в реестр попало хоть что-то по этому договору."""
    ids = set()
    for model in (Contract, LaborRow, Inflow, ContractValue):
        for (doc_id,) in db.query(model.document_id).filter_by(contract_code=code).all() \
                if model is not Contract else \
                db.query(model.document_id).filter_by(code=code).all():
            if doc_id:
                ids.add(doc_id)
    for (doc_id,) in db.query(SecretAllowance.document_id) \
            .filter_by(secret_contract_code=code).all():
        if doc_id:
            ids.add(doc_id)
    return [d for d in (db.get(Document, i) for i in sorted(ids)) if d is not None]


def _contract_impact(db, code):
    """Что исчезнет вместе с договором — числами, до удаления."""
    docs = _contract_docs(db, code)
    plans = 0
    for case in db.query(Case).all():
        if code not in basis.read(case)["вне расчёта"]:
            plans += 1
    return {
        "документов": len(docs),
        "редакций": sum(1 for d in docs if (d.version or 1) > 1),
        "строк трудоёмкости": db.query(LaborRow).filter_by(contract_code=code).count(),
        "поступлений": db.query(Inflow).filter_by(contract_code=code).count(),
        "величин": db.query(ContractValue).filter_by(contract_code=code).count(),
        "надбавок 120": db.query(SecretAllowance)
                          .filter_by(secret_contract_code=code).count(),
        "планов в расчёте": plans,
        "файлы": [d.name for d in docs],
    }


def _forget_contract_rows(db, code):
    """Стереть всё, что помнит этот шифр, кроме самих документов."""
    return {
        "договоров": db.query(Contract).filter_by(code=code).delete(),
        "строк трудоёмкости": db.query(LaborRow).filter_by(contract_code=code).delete(),
        "поступлений": db.query(Inflow).filter_by(contract_code=code).delete(),
        "величин": db.query(ContractValue).filter_by(contract_code=code).delete(),
        "надбавок 120": db.query(SecretAllowance)
                          .filter_by(secret_contract_code=code).delete(),
    }


def _drop_from_plans(db, code):
    """Убрать шифр из выбора планов: ссылки на несуществующий договор — мусор."""
    touched = 0
    for case in db.query(Case).all():
        chosen = basis.read(case)
        off, overrides = chosen["вне расчёта"], chosen["договоры"]
        if code not in off and code not in overrides:
            continue
        off.discard(code)
        overrides.pop(code, None)
        chosen["условия реестра"].pop(code, None)
        basis._write(case, chosen)
        touched += 1
    return touched


@app.get("/api/contracts/{code}/impact")
def contract_impact(code: str):
    """Последствия удаления договора — до того, как экономист решит."""
    db = session()
    try:
        if db.query(Contract).filter_by(code=code).first() is None:
            raise HTTPException(404, "договор %s не найден" % code)
        return _contract_impact(db, code)
    finally:
        db.close()


@app.delete("/api/contracts/{code}")
async def delete_contract(code: str, request: Request):
    """Удалить договор. Два намерения, а не одно «удалить».

    `с документами` — договор завели по ошибке: файлы и всё извлечённое уходят.
    `без документов` — договор лишний, файлы нужны: документы остаются и
    попадают в «Документы без договора», их можно перечитать под другим шифром
    (SOURCE-005). Планы, посчитанные на этих данных, помечаются к пересмотру:
    основание изменилось, а прежний ответ остаётся историей (VER-003).
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — тело может не прийти вовсе
        body = {}
    mode = str((body or {}).get("документы") or "с документами")
    if mode not in ("с документами", "без документов"):
        raise HTTPException(400, "документы: «с документами» или «без документов»")
    db = session()
    try:
        row = db.query(Contract).filter_by(code=code).first()
        if row is None:
            raise HTTPException(404, "договор %s не найден" % code)
        name = row.name or code
        docs = _contract_docs(db, code)
        before = _contract_impact(db, code)
        for doc in docs:
            actuality.mark_review(db, actuality.runs_using_document(db, doc.id),
                                  "договор «%s» удалён" % code)
        if mode == "с документами":
            for doc in docs:
                _forget_document(db, doc)
        gone = _forget_contract_rows(db, code)
        gone["планов поправлено"] = _drop_from_plans(db, code)
        if mode == "с документами":
            gone["документов"] = len(docs)
        from fot_planner.harness_local import decisions as D
        record_decision(db, kind="данные", subject_kind="договор", subject_id=code,
                        subject_digest=D.digest(code, mode), scope="реестр",
                        action={"удалён договор": code, "документы": mode,
                                "было": before},
                        grounds="«Удалить договор» на экране договора")
        db.commit()
        return {"ok": True, "code": code, "name": name, "removed": gone}
    finally:
        db.close()


@app.post("/api/contracts/{code}")
async def rename_contract(code: str, request: Request):
    """Наименование договора (UI-009). Шифр не меняется: он опознаёт договор.

    Наименование подтягивается из шапки документа (SOURCE-005), но формулировка
    темы в форме и рабочее название договора совпадают не всегда — поэтому его
    правят на месте, как название плана.
    """
    body = await request.json()
    db = session()
    try:
        found = db.query(Contract).filter_by(code=code).first()
        if found is None:
            raise HTTPException(404, "договор %s не найден" % code)
        from fot_planner.harness_local import decisions as D
        # Графы, которых нет в документах, экономист вводит в ячейке списка
        # (SOURCE-007): {"fields": {"account": "…", "goz": "да"}}.
        if isinstance(body.get("fields"), dict):
            changed = basis.set_manual(db, found, body["fields"])
            if changed:
                record_decision(db, kind="данные", subject_kind="договор", subject_id=code,
                                subject_digest=D.digest(code, sorted(changed)), scope="реестр",
                                action={"договор": code, "графы": changed},
                                grounds="введено экономистом в списке договоров")
                # Значение договора общее: пересмотра ждут расчёты всех планов,
                # где договор в расчёте.
                for case in db.query(Case).all():
                    if code not in basis.read(case)["вне расчёта"]:
                        actuality.mark_review(db, actuality.runs_of_case(db, case.id),
                                              "у договора %s изменены графы: %s"
                                              % (code, ", ".join(sorted(changed))))
                db.commit()
                # Экономист вписал договору л/с или номер — приказы, которые
                # ждали такого договора, прикрепляются к нему сами.
                intake.rebind_workorders(db)
            return {"ok": True, "code": code, "changed": changed}
        name = str(body.get("name") or "").strip()[:300] or None
        if found.name == name:
            return {"ok": True, "code": code, "name": name}
        was, found.name = found.name, name
        record_decision(db, kind="данные", subject_kind="договор", subject_id=code,
                        subject_digest=D.digest(code, name), scope="реестр",
                        action={"договор": code, "наименование": name, "было": was},
                        grounds="переименование на экране договора")
        db.commit()
        return {"ok": True, "code": code, "name": name}
    finally:
        db.close()


#: Источник правила замещения, введённого на вкладке реестра (SOURCE-010).
SUBST_MANUAL = "введено вручную"


def _subst_set(value):
    """Набор должностей правила строкой графы «может быть замещена».

    Приходит списком из окна правки или строкой через запятую; порядок и
    повторы не значат ничего — решатель читает набор, поэтому храним его
    отсортированным и без повторов."""
    items = value if isinstance(value, list) else str(value or "").split(",")
    return ", ".join(sorted({str(x).strip() for x in items if str(x).strip()}))


def _subst_changed(db, sid, position, action, grounds):
    """Журнал решений и пересмотр планов после правки правила.

    Правила общие для организации и уходят листом «правила_замещения» в расчёт
    каждого плана, поэтому к пересмотру помечаются посчитанные планы всех
    дел, а не одного."""
    from fot_planner.harness_local import decisions as D
    record_decision(db, kind="данные", subject_kind="правило замещения",
                    subject_id=str(sid), subject_digest=D.digest(position, action),
                    scope="реестр", action=action, grounds=grounds)
    actuality.mark_review(db, actuality.results(db),
                          "изменены правила замещения: %s" % position)


def contract_choice(checked, codes):
    """Отмеченные договоры → пара (разрешены, запрещены) строки штатного.

    Хранится одно из двух — то, что короче: отмечено не меньше половины —
    запрещены неотмеченные, иначе разрешены отмеченные; отмечены все — без
    ограничений. Так договор, добавленный в реестр позже, попадает к
    сотруднику, у которого запрещены единицы, и не попадает к тому, у кого
    разрешены единицы (решение пользователя 30.09.2026)."""
    codes = list(dict.fromkeys(codes))
    on = [c for c in codes if c in set(checked)]
    off = [c for c in codes if c not in set(on)]
    if not off:
        return None, None
    if not on:
        raise HTTPException(400, "отметьте хотя бы один договор")
    if len(on) * 2 >= len(codes):
        return None, "; ".join(off)
    return "; ".join(on), None


@app.post("/api/employees/{code}/contracts")
async def employee_contracts(code: str, request: Request):
    """Договоры, на которых сотрудник может получать выплаты: {"checked":
    [шифры]}. Правка ручная — следующий разбор того же штатного расписания
    вернёт значения документа."""
    body = await request.json()
    db = session()
    try:
        row = db.query(Employee).filter_by(code=code).first()
        if row is None:
            raise HTTPException(404, "сотрудник не найден")
        codes = [c.code for c in db.query(Contract).order_by(Contract.code).all()]
        allowed, forbidden = contract_choice(body.get("checked") or [], codes)
        was = {"разрешены": row.allowed_contracts, "запрещены": row.forbidden_contracts}
        row.allowed_contracts, row.forbidden_contracts = allowed, forbidden
        from fot_planner.harness_local import decisions as D
        action = {"договоры сотрудника": code, "разрешены": allowed, "запрещены": forbidden,
                  "было": was}
        record_decision(db, kind="данные", subject_kind="сотрудник", subject_id=code,
                        subject_digest=D.digest(code, action), scope="реестр", action=action,
                        grounds="правка на вкладке «Штатное расписание»")
        actuality.mark_review(db, actuality.results(db), "изменены договоры сотрудника %s" % code)
        db.commit()
        return {"ok": True, "allowed": allowed, "forbidden": forbidden}
    finally:
        db.close()


@app.post("/api/substitutions")
async def add_substitution(request: Request):
    """Добавить правило замещения вручную (SOURCE-010).

    {"position": должность, "replaced_by": [должности]}. Правило — строка без
    документа: его не заменит и не сотрёт следующий файл правил."""
    body = await request.json()
    position = str(body.get("position") or "").strip()
    if not position:
        raise HTTPException(400, "нужна должность")
    db = session()
    try:
        if db.query(Substitution).filter_by(position=position).first() is not None:
            raise HTTPException(409, "правило для «%s» уже есть — измените его" % position)
        row = Substitution(position=position, replaced_by=_subst_set(body.get("replaced_by")),
                           source=SUBST_MANUAL, document_id=None)
        db.add(row)
        db.flush()
        _subst_changed(db, row.id, position,
                       {"добавлено правило": position, "набор": row.replaced_by},
                       "«Добавить правило» на вкладке «Правила замещения»")
        db.commit()
        return {"ok": True, "id": row.id}
    finally:
        db.close()


@app.patch("/api/substitutions/{sid}")
async def edit_substitution(sid: int, request: Request):
    """Изменить правило: должность или набор. Правило из документа после
    правки становится ручным — иначе следующий разбор того же файла молча
    вернул бы прежний набор (SOURCE-010)."""
    body = await request.json()
    db = session()
    try:
        row = db.get(Substitution, sid)
        if row is None:
            raise HTTPException(404, "правило не найдено")
        was = {"должность": row.position, "набор": row.replaced_by, "источник": row.source}
        if "position" in body:
            position = str(body.get("position") or "").strip()
            if not position:
                raise HTTPException(400, "нужна должность")
            other = db.query(Substitution).filter(Substitution.position == position,
                                                  Substitution.id != sid).first()
            if other is not None:
                raise HTTPException(409, "правило для «%s» уже есть" % position)
            row.position = position
        if "replaced_by" in body:
            row.replaced_by = _subst_set(body.get("replaced_by"))
        row.source, row.document_id = SUBST_MANUAL, None
        _subst_changed(db, sid, row.position,
                       {"изменено правило": row.position, "набор": row.replaced_by, "было": was},
                       "правка на вкладке «Правила замещения»")
        db.commit()
        return {"ok": True, "id": sid}
    finally:
        db.close()


@app.delete("/api/substitutions/{sid}")
def delete_substitution(sid: int):
    """Удалить правило. Правило из документа вернётся, если файл разобрать
    снова: это новый источник, а не отмена удаления (SOURCE-010)."""
    db = session()
    try:
        row = db.get(Substitution, sid)
        if row is None:
            raise HTTPException(404, "правило не найдено")
        was = {"удалено правило": row.position, "набор": row.replaced_by, "источник": row.source}
        position = row.position
        db.delete(row)
        _subst_changed(db, sid, position, was, "«Удалить правило» на вкладке «Правила замещения»")
        db.commit()
        return {"ok": True}
    finally:
        db.close()


#: Источник строки трудоёмкости, введённой на вкладке договора.
LABOR_MANUAL = "введено вручную"


def _labor_fields(body, row):
    """Поля ручной строки трудоёмкости — только переданные, с проверкой."""
    if "year" in body:
        raw = str(body.get("year") or "").strip()
        if raw and not raw.isdigit():
            raise HTTPException(400, "год — число, например 2026")
        row.year = int(raw) if raw else None
    if "position" in body:
        row.position = str(body.get("position") or "").strip() or None
    for key, attr in (("person_months", "person_months"), ("avg_cost", "avg_cost"),
                      ("headcount", "headcount")):
        if key in body:
            raw = str(body.get(key) or "").replace(" ", "").replace("\u00a0", "")
            raw = raw.replace(",", ".").replace("₽", "")
            if not raw:
                setattr(row, attr, None)
                continue
            try:
                value = float(raw)
            except ValueError:
                raise HTTPException(400, "нужно число")
            if value < 0:
                raise HTTPException(400, "число не может быть отрицательным")
            setattr(row, attr, value)


def _labor_changed(db, rid, code, action, grounds):
    """Журнал решений и пересмотр планов: трудоёмкость уходит в расчёт."""
    from fot_planner.harness_local import decisions as D
    record_decision(db, kind="данные", subject_kind="трудоёмкость",
                    subject_id=str(rid), subject_digest=D.digest(code, action),
                    scope="реестр", action=action, grounds=grounds)
    actuality.mark_review(db, actuality.results(db),
                          "изменена трудоёмкость договора %s" % code)


def _labor_row(r):
    return {"id": r.id, "contract": r.contract_code, "year": r.year,
            "position": r.position, "person_months": r.person_months,
            "avg_cost": r.avg_cost, "headcount": r.headcount, "source": r.source}


@app.post("/api/labor")
async def add_labor(request: Request):
    """Строка трудоёмкости вручную: договор, год, должность, чел.-мес.,
    стоимость единицы, специалистов. Строка без документа: следующий разбор
    файла её не сотрёт."""
    body = await request.json()
    db = session()
    try:
        code = str(body.get("contract") or "").strip()
        if db.query(Contract).filter_by(code=code).first() is None:
            raise HTTPException(400, "нет договора «%s» в реестре" % code)
        row = LaborRow(contract_code=code, source=LABOR_MANUAL, document_id=None)
        _labor_fields(body, row)
        db.add(row)
        db.flush()
        _labor_changed(db, row.id, code, {"добавлена строка": _labor_row(row)},
                       "«Добавить строку» на вкладке «Трудоёмкость» договора")
        db.commit()
        return {"ok": True, "id": row.id}
    finally:
        db.close()


@app.patch("/api/labor/{rid}")
async def edit_labor(rid: int, request: Request):
    """Правка ручной строки в ячейке. Строки из документа правят документом:
    у них за агрегатом стоит расшифровка по этапам, и правка агрегата её бы
    разорвала."""
    body = await request.json()
    db = session()
    try:
        row = db.get(LaborRow, rid)
        if row is None:
            raise HTTPException(404, "строка не найдена")
        if row.document_id is not None:
            raise HTTPException(409, "строка из документа — поправьте документ")
        was = _labor_row(row)
        _labor_fields(body, row)
        _labor_changed(db, rid, row.contract_code,
                       {"изменена строка": _labor_row(row), "было": was},
                       "правка на вкладке «Трудоёмкость» договора")
        db.commit()
        return {"ok": True, "id": rid}
    finally:
        db.close()


@app.delete("/api/labor/{rid}")
def delete_labor(rid: int):
    db = session()
    try:
        row = db.get(LaborRow, rid)
        if row is None:
            raise HTTPException(404, "строка не найдена")
        if row.document_id is not None:
            raise HTTPException(409, "строка из документа — удалите или замените документ")
        was = _labor_row(row)
        db.delete(row)
        _labor_changed(db, rid, was["contract"], {"удалена строка": was},
                       "«Удалить строку» на вкладке «Трудоёмкость» договора")
        db.commit()
        return {"ok": True}
    finally:
        db.close()


#: Источник строки надбавки 120, введённой на вкладке реестра (правило 10).
SECRET_MANUAL = "введено вручную"
_DMY = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")


def _secret_fields(db, body, row):
    """Поля строки 120 из тела запроса — только переданные, с проверкой.

    Сотрудник — код строки штатного расписания, договоры — шифры реестра,
    ставка — доля (5 % приходит как 5 или 0,05), даты — ДД.ММ.ГГГГ или пусто."""
    def text_of(key):
        return str(body.get(key) or "").strip()
    if "employee" in body:
        code = text_of("employee")
        if not code or db.query(Employee).filter_by(code=code).first() is None:
            raise HTTPException(400, "нет сотрудника «%s» в штатном расписании" % code)
        row.employee_code = code
    for key, attr, need in (("contract", "secret_contract_code", True),
                            ("pay_contract", "pay_contract_code", False)):
        if key in body:
            code = text_of(key)
            if not code and not need:
                setattr(row, attr, None)
                continue
            if db.query(Contract).filter_by(code=code).first() is None:
                raise HTTPException(400, "нет договора «%s» в реестре" % code)
            setattr(row, attr, code)
    if "rate" in body:
        raw = text_of("rate").replace(",", ".").replace("%", "")
        try:
            rate = float(raw)
        except ValueError:
            raise HTTPException(400, "ставка 120 — число, например 10 или 0,1")
        rate = rate / 100 if rate > 1 else rate
        if not 0 < rate <= 1:
            raise HTTPException(400, "ставка 120 — от 0 до 100 %")
        row.rate = rate
    for key in ("date_from", "date_to"):
        if key in body:
            value = text_of(key)
            if value and not _DMY.match(value):
                raise HTTPException(400, "дата — ДД.ММ.ГГГГ")
            setattr(row, key, value or None)


def _secret_changed(db, sid, employee, action, grounds):
    """Журнал решений и пересмотр посчитанных планов: лист 120 уходит в расчёт
    каждого плана, как правила замещения."""
    from fot_planner.harness_local import decisions as D
    record_decision(db, kind="данные", subject_kind="надбавка 120",
                    subject_id=str(sid), subject_digest=D.digest(employee, action),
                    scope="реестр", action=action, grounds=grounds)
    actuality.mark_review(db, actuality.results(db),
                          "изменена надбавка 120: %s" % employee)


def _secret_row(r):
    return {"id": r.id, "employee": r.employee_code, "contract": r.secret_contract_code,
            "rate": r.rate, "from": r.date_from, "to": r.date_to,
            "pay_contract": r.pay_contract_code, "source": r.source}


@app.post("/api/secret_allowances")
async def add_secret_allowance(request: Request):
    """Строка надбавки 120 вручную: сотрудник, договор секретности, ставка,
    окно и закреплённый договор выплаты. У сотрудника строк может быть
    несколько — по одной на договор секретности (правило 10, 18.09.2026)."""
    body = await request.json()
    db = session()
    try:
        row = SecretAllowance(source=SECRET_MANUAL, document_id=None, rate=0.05)
        _secret_fields(db, body, row)
        if not row.employee_code or not row.secret_contract_code:
            raise HTTPException(400, "нужны сотрудник и договор секретности")
        db.add(row)
        db.flush()
        _secret_changed(db, row.id, row.employee_code, {"добавлена строка": _secret_row(row)},
                        "«Добавить» на вкладке «Надбавка 120»")
        db.commit()
        return {"ok": True, "id": row.id}
    finally:
        db.close()


@app.patch("/api/secret_allowances/{sid}")
async def edit_secret_allowance(sid: int, request: Request):
    """Правка строки 120 в ячейке. Строка из документа после правки становится
    ручной: следующий разбор файла её не перепишет."""
    body = await request.json()
    db = session()
    try:
        row = db.get(SecretAllowance, sid)
        if row is None:
            raise HTTPException(404, "строка не найдена")
        was = _secret_row(row)
        _secret_fields(db, body, row)
        row.source, row.document_id = SECRET_MANUAL, None
        _secret_changed(db, sid, row.employee_code,
                        {"изменена строка": _secret_row(row), "было": was},
                        "правка на вкладке «Надбавка 120»")
        db.commit()
        return {"ok": True, "id": sid}
    finally:
        db.close()


@app.delete("/api/secret_allowances/{sid}")
def delete_secret_allowance(sid: int):
    db = session()
    try:
        row = db.get(SecretAllowance, sid)
        if row is None:
            raise HTTPException(404, "строка не найдена")
        was = _secret_row(row)
        db.delete(row)
        _secret_changed(db, sid, was["employee"], {"удалена строка": was},
                        "«Удалить» на вкладке «Надбавка 120»")
        db.commit()
        return {"ok": True}
    finally:
        db.close()


def _salary_amount(raw):
    """Сумма из ячейки: «150 000,50», «150000.5» или число; пусто — None.

    Ноль запрещён (решение пользователя 19.09.2026, «пока что»): месяц с
    нулём среди оплачиваемых делает план нерешаемым — оклад держится блоком
    не короче трёх месяцев."""
    if raw is None or str(raw).strip() == "":
        return None
    text = str(raw).replace(" ", "").replace(" ", "").replace(" ", "")
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        raise HTTPException(400, "нужна сумма числом")
    if value <= 0:
        raise HTTPException(400, "сумма должна быть больше нуля")
    return round(value, 2)


@app.put("/api/salary_months")
async def set_salary_month(request: Request):
    """Своя зарплата строки штатного расписания в месяце (решение пользователя
    19.09.2026: зарплата меняется в течение года).

    {"code": код строки, "year": год, "month": 1–12, "amount": сумма или пусто}.
    Пусто — месяц снова берёт зарплату из штатного расписания. Зарплата общая
    для организации и уходит в расчёт каждого плана этого года, поэтому
    посчитанные планы помечаются к пересмотру, а правка пишется в журнал."""
    body = await request.json()
    code = str(body.get("code") or "").strip()
    try:
        year, month = int(body.get("year")), int(body.get("month"))
    except (TypeError, ValueError):
        raise HTTPException(400, "нужны год и месяц")
    if not 1 <= month <= 12:
        raise HTTPException(400, "месяц — от 1 до 12")
    amount = _salary_amount(body.get("amount"))
    db = session()
    try:
        if db.query(Employee).filter_by(code=code).first() is None:
            raise HTTPException(404, "строка штатного расписания %s не найдена" % code)
        was = salary.set_month(db, code, year, month, amount,
                               row_id=int(body["row"]) if body.get("row") else None)
        salary.changed(db, code, year, {"строка": code, "год": year, "месяц": month,
                                        "сумма": amount, "было": was},
                       "правка на вкладке «Заработная плата по месяцам»",
                       "изменена зарплата строки %s за %02d.%d" % (code, month, year))
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.post("/api/salary_rows")
async def add_salary_row(request: Request):
    """Добавить в таблицу зарплаты сотрудника, выбранного по ФИО из штатного
    расписания. {"code": код строки, "year": год}. Сумм у новой строки нет:
    месяцы берут зарплату штатного расписания, пока их не исправят, поэтому
    расчёт от добавления не меняется."""
    body = await request.json()
    code = str(body.get("code") or "").strip()
    try:
        year = int(body.get("year"))
    except (TypeError, ValueError):
        raise HTTPException(400, "нужен год")
    dept = int(body["dept"]) if body.get("dept") else None
    db = session()
    try:
        if db.query(Employee).filter_by(code=code).first() is None:
            raise HTTPException(404, "строка штатного расписания %s не найдена" % code)
        if dept is not None and db.get(MgmtDepartment, dept) is None:
            raise HTTPException(404, "управленческое подразделение не найдено")
        # Строка человека — одна на подразделение (01.10.2026): вторая строка
        # того же человека — в другом подразделении.
        if db.query(SalaryRow).filter_by(employee_code=code, year=year,
                                         mgmt_department_id=dept).first() is not None:
            raise HTTPException(409, "строка %s в этом подразделении уже есть в таблице зарплаты"
                                % code)
        row = SalaryRow(employee_code=code, year=year, source=salary.MANUAL,
                        mgmt_department_id=dept)
        db.add(row)
        db.flush()
        salary.changed(db, code, year, {"добавлена строка": code, "год": year,
                                        "подразделение": dept},
                       "«Добавить сотрудника» на вкладке «Заработная плата по месяцам»",
                       None)
        db.commit()
        return {"ok": True, "id": row.id}
    finally:
        db.close()


@app.delete("/api/salary_rows/{rid}")
def delete_salary_row(rid: int):
    """Убрать строку из таблицы зарплаты вместе с её суммами: месяцы строки
    остаются без зарплаты, пока их не зададут снова."""
    db = session()
    try:
        row = db.get(SalaryRow, rid)
        if row is None:
            raise HTTPException(404, "строка не найдена")
        code, year = row.employee_code, row.year
        # Удаляется одна строка — строка этого подразделения (01.10.2026), а
        # не все строки человека за год.
        sums = db.query(SalaryMonth).filter_by(salary_row_id=row.id).all()
        for s in sums:
            db.delete(s)
        db.delete(row)
        salary.changed(db, code, year,
                       {"удалена строка": code, "год": year,
                        "суммы": {s.month: s.amount for s in sums}},
                       "«Удалить» на вкладке «Заработная плата по месяцам»",
                       "удалена строка зарплаты %s за %d" % (code, year) if sums else None)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


def _mgmt_rows(db, year):
    """Строки зарплаты года с управленческим подразделением — во вход расчёта
    (лист «управленческие_подразделения»): по ним раздел 16 отчёта делит
    выплаты человека между подразделениями (01.10.2026). [(код строки,
    подразделение или None, порядок, месяц, сумма)]."""
    depts = {d.id: d for d in db.query(MgmtDepartment).all()}
    rows = {r.id: r for r in db.query(SalaryRow).filter_by(year=year).all()}
    out = []
    for m in db.query(SalaryMonth).filter_by(year=year).order_by(SalaryMonth.id).all():
        r = rows.get(m.salary_row_id)
        d = depts.get(r.mgmt_department_id) if r is not None else None
        out.append((m.employee_code, d.name if d else None, d.position if d else None,
                    m.month, m.amount))
    return out


def _mgmt_list(db):
    """Справочник управленческих подразделений: по порядку, затем по имени;
    у каждого — число строк зарплаты."""
    used = {}
    for r in db.query(SalaryRow).filter(SalaryRow.mgmt_department_id.isnot(None)).all():
        used[r.mgmt_department_id] = used.get(r.mgmt_department_id, 0) + 1
    items = db.query(MgmtDepartment).all()
    items.sort(key=lambda d: (d.position is None, d.position or 0, d.name.lower()))
    return [{"id": d.id, "name": d.name, "position": d.position, "note": d.note,
             "rows": used.get(d.id, 0)} for d in items]


def _mgmt_name(db, raw, skip=None):
    name = " ".join(str(raw or "").split())[:200]
    if not name:
        raise HTTPException(400, "нужно наименование подразделения")
    for d in db.query(MgmtDepartment).all():
        if d.id != skip and d.name.lower() == name.lower():
            raise HTTPException(409, "подразделение «%s» уже есть" % d.name)
    return name


def _mgmt_logged(db, action, grounds):
    from fot_planner.harness_local import decisions as D
    record_decision(db, kind="данные", subject_kind="управленческое подразделение",
                    subject_id=str(action.get("id") or action.get("наименование")),
                    subject_digest=D.digest(action), scope="реестр", action=action,
                    grounds=grounds)


@app.post("/api/mgmt_departments")
async def add_mgmt_department(request: Request):
    """Завести управленческое подразделение: {"name", "position"?}. Расчёт от
    этого не меняется — подразделения только в отчёте (раздел 16)."""
    body = await request.json()
    db = session()
    try:
        d = MgmtDepartment(name=_mgmt_name(db, body.get("name")),
                           position=int(body["position"]) if body.get("position") else None)
        db.add(d)
        db.flush()
        _mgmt_logged(db, {"заведено подразделение": d.name, "id": d.id},
                     "вкладка «Управленческие подразделения»")
        db.commit()
        return {"ok": True, "id": d.id, "name": d.name}
    finally:
        db.close()


@app.patch("/api/mgmt_departments/{did}")
async def edit_mgmt_department(did: int, request: Request):
    """Переименовать подразделение или задать порядок: строки зарплаты
    ссылаются на него, поэтому новое имя у всех сразу."""
    body = await request.json()
    db = session()
    try:
        d = db.get(MgmtDepartment, did)
        if d is None:
            raise HTTPException(404, "подразделение не найдено")
        was = {"наименование": d.name, "порядок": d.position}
        if "name" in body:
            d.name = _mgmt_name(db, body.get("name"), skip=did)
        if "position" in body:
            raw = body.get("position")
            d.position = int(raw) if raw not in (None, "") else None
        _mgmt_logged(db, {"изменено подразделение": d.name, "id": did, "было": was},
                     "вкладка «Управленческие подразделения»")
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.delete("/api/mgmt_departments/{did}")
def delete_mgmt_department(did: int):
    """Удалить подразделение, на которое не ссылается ни одна строка
    зарплаты: деньги строки не должны молча уйти в «Без подразделения»."""
    db = session()
    try:
        d = db.get(MgmtDepartment, did)
        if d is None:
            raise HTTPException(404, "подразделение не найдено")
        n = db.query(SalaryRow).filter_by(mgmt_department_id=did).count()
        if n:
            raise HTTPException(409, "у подразделения «%s» строк зарплаты: %d — сначала "
                                "перенесите их" % (d.name, n))
        _mgmt_logged(db, {"удалено подразделение": d.name, "id": did},
                     "вкладка «Управленческие подразделения»")
        db.delete(d)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.patch("/api/salary_rows/{rid}")
async def set_salary_row_department(rid: int, request: Request):
    """Управленческое подразделение строки зарплаты: {"dept": id или пусто,
    "from": месяц 1–12}. С января — исправление всей строки; с другого месяца
    — перевод, как кадровый перевод в 1С: суммы с этого месяца уходят в
    строку человека с новым подразделением (её нет — заводится; есть и в
    тех же месяцах суммы — отказ с перечнем месяцев). Расчёт не меняется:
    подразделение — только для отчёта; правка — в журнал."""
    body = await request.json()
    dept = int(body["dept"]) if body.get("dept") else None
    try:
        start = int(body.get("from") or 1)
    except (TypeError, ValueError):
        raise HTTPException(400, "месяц — от 1 до 12")
    if not 1 <= start <= 12:
        raise HTTPException(400, "месяц — от 1 до 12")
    db = session()
    try:
        row = db.get(SalaryRow, rid)
        if row is None:
            raise HTTPException(404, "строка не найдена")
        if dept is not None and db.get(MgmtDepartment, dept) is None:
            raise HTTPException(404, "управленческое подразделение не найдено")
        if dept == row.mgmt_department_id:
            return {"ok": True, "id": row.id}
        moving = [m for m in db.query(SalaryMonth).filter_by(salary_row_id=row.id).all()
                  if m.month >= start]
        other = (db.query(SalaryRow).filter_by(employee_code=row.employee_code, year=row.year,
                                               mgmt_department_id=dept)
                 .filter(SalaryRow.id != row.id).first())
        whole = start == 1 or not moving or len(moving) == db.query(SalaryMonth).filter_by(
            salary_row_id=row.id).count()
        if other is None and whole:
            row.mgmt_department_id = dept
            target = row
        else:
            if other is None:
                other = SalaryRow(employee_code=row.employee_code, year=row.year,
                                  source=salary.MANUAL, mgmt_department_id=dept)
                db.add(other)
                db.flush()
            busy = sorted({m.month for m in db.query(SalaryMonth).filter_by(
                salary_row_id=other.id).all()} & {m.month for m in moving})
            if busy:
                raise HTTPException(409, "у строки в этом подразделении уже есть суммы за "
                                    "месяцы: %s" % ", ".join(str(m) for m in busy))
            for m in moving:
                m.salary_row_id = other.id
            target = other
            if whole and not db.query(SalaryMonth).filter_by(salary_row_id=row.id).count():
                db.delete(row)
        salary.changed(db, row.employee_code, row.year,
                       {"подразделение строки": row.employee_code, "с месяца": start,
                        "подразделение": dept}, "вкладка «Заработная плата по месяцам»", None)
        db.commit()
        return {"ok": True, "id": target.id}
    finally:
        db.close()


@app.post("/api/document/{doc_id}/contract")
async def assign_document_contract(doc_id: int, request: Request):
    """Документ без шифра в шапке — в договор, названный экономистом (SOURCE-005).

    Единственное место, где шифр вводят руками: документ, в котором его нет.
    Договора с таким шифром может не быть — он заводится; строки документа
    перечитываются уже под этим договором.
    """
    body = await request.json()
    code = str(body.get("code") or "").strip()[:60]
    if not code:
        raise HTTPException(400, "нужен шифр договора")
    db = session()
    try:
        doc = db.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "документ не найден")
        if db.query(Contract).filter_by(code=code).first() is None:
            # Без document_id: договор назван экономистом, а не прочитан из
            # документа. Строки документа при повторном разборе стираются —
            # с привязкой стёрся бы и он.
            db.add(Contract(code=code, source="шифр указан экономистом для «%s»" % doc.name))
        doc.contract_code = code
        from fot_planner.harness_local import decisions as D
        record_decision(db, kind="данные", subject_kind="договор", subject_id=code,
                        subject_digest=D.digest(code, doc.id), scope="реестр",
                        action={"документ": doc.name, "в договор": code},
                        grounds="шифр указан экономистом: в шапке документа его нет")
        db.commit()
        case = db.get(Case, doc.case_id) if doc.case_id else db.query(Case).first()
        try:
            # Приказ о рабочей группе читается своим разбором: общий искал бы в
            # нём договоры и сотрудников заново и не нашёл бы состава группы.
            if doc.kind == workgroup.KIND:
                intake.run_workgroup(db, case, doc)
            else:
                intake.run_intake(db, case, doc)
        except Exception as exc:  # noqa: BLE001
            doc.state = "не распознан"
            doc.summary = str(exc)[:300]
            db.commit()
        return {"ok": True, "code": code}
    finally:
        db.close()


@app.get("/api/case/{case_id}/basis")
def plan_basis(case_id: int):
    """Каталог документов реестра по договорам и выбор плана (SOURCE-003).

    План 0 — реестр открыт без плана: последние версии, все договоры в
    расчёте, выбирать не для чего.
    """
    db = session()
    try:
        case = db.get(Case, case_id) if case_id else None
        if case_id and case is None:
            raise HTTPException(404, "план не найден")
        return _basis_out(db, case)
    finally:
        db.close()


def _basis_out(db, case):
    """Основание плана для экрана: каталог документов и закрепления вместе.

    Один вид у запроса и у сохранения: сохранение правки условий отдавало
    каталог без закреплений, и таблица закреплений в панели пустела до
    следующей загрузки страницы (пользователь 21.09.2026: «я меняю настройки
    тут и меняются настройки в закрепить сотрудника»)."""
    # Правки, с которыми реестр сравнялся, снимаются до показа (30.09.2026):
    # экран не должен показывать правку, которой больше нечего править.
    gone = basis.dissolve_caught_up(db, case)
    out = basis.catalog(db, case)
    out["dissolved"] = gone
    if case is not None:
        # Закрепления сотрудников — условия договоров этого плана: их
        # показывают и правят в строке условий на вкладке «Договоры».
        out["fixes"] = chat.plan_settings(case)["назначения"]
        # Выплата членам рабочих групп в этом плане: «договор|сотрудник» →
        # {виды, только, сумма}; нет ключа — по умолчанию «вся, по возможности».
        out["workgroup_pay"] = chat.plan_settings(case).get("рабочие группы") or {}
        # Месяцы факта начислений года плана и тот, по который расчёт его
        # берёт, — из самого факта (30.09.2026).
        out["fact_months"] = facts.months_available(db, case.year)
        out["fact_through"] = _fact_through(db, case)
    return out


def _workgroup_rows(db):
    """Состав рабочих групп для реестра: член группы вместе со своим приказом."""
    orders = {o.id: o for o in db.query(WorkOrder).all()}
    out = []
    for m in db.query(WorkOrderMember).order_by(WorkOrderMember.order_id,
                                                 WorkOrderMember.n).all():
        o = orders.get(m.order_id)
        if o is None:
            continue
        out.append({"id": m.id, "order_id": o.id, "document_id": o.document_id,
                    "kind": o.kind, "number": o.number, "date": o.date,
                    "base": ("№ %s от %s" % (o.base_number, o.base_date))
                            if o.base_number else None,
                    "contract": o.contract_code, "agreement": o.agreement,
                    "account": o.account, "topic": o.topic,
                    "from": o.date_from, "to": o.date_to,
                    "action": m.action, "fio": m.fio, "person_code": m.person_code,
                    "position": m.position, "department": m.department, "work": m.work,
                    "leader": m.leader == "да", "employee": m.employee_code})
    return out


def _workgroup_current(db):
    """Действующий состав рабочих групп — для расчёта плана (30.09.2026).

    Группа складывается из приказов договора по датам: по каждой строке
    штатного расписания решает последний приказ, где она названа, —
    «включить» или «исключить». Строка, не сопоставленная со штатным
    расписанием, в расчёт не идёт: платить некому. Срок — срок группы из
    приказа, которым строка включена."""
    def day(text):
        p = str(text or "").strip().split(".")
        try:
            return (int(p[2]), int(p[1]), int(p[0])) if len(p) == 3 else (0, 0, 0)
        except ValueError:
            return (0, 0, 0)
    orders = sorted(db.query(WorkOrder).filter(WorkOrder.contract_code.isnot(None)).all(),
                    key=lambda o: (day(o.date), o.id))
    last = {}
    for o in orders:
        for m in (db.query(WorkOrderMember).filter_by(order_id=o.id)
                  .order_by(WorkOrderMember.n, WorkOrderMember.id).all()):
            if m.employee_code:
                last[(o.contract_code, m.employee_code)] = (o, m)
    out = []
    for (code, emp), (o, m) in last.items():
        if (m.action or "").strip().lower() == "исключить":
            continue
        out.append({"договор": code, "сотрудник": emp, "фио": m.fio,
                    "с": o.date_from, "по": o.date_to,
                    "приказ": "№ %s от %s" % (o.number or "?", o.date or "?")})
    return sorted(out, key=lambda r: (r["договор"], r["сотрудник"]))


def _workorders(db):
    """Приказы о рабочих группах для реестра: строка — приказ, не человек.

    Людей здесь нет — они во вкладке «Рабочая группа» договора; здесь то, что
    нужно для разбора пачки: к какому договору лёг приказ, сколько в нём
    людей и сколько из них не нашлось в штатном расписании."""
    docs = {d.id: d for d in db.query(Document).all()}
    counts = {}
    for m in db.query(WorkOrderMember).all():
        n, lost = counts.get(m.order_id, (0, 0))
        counts[m.order_id] = (n + 1, lost + (m.employee_code is None))
    out = []
    for o in db.query(WorkOrder).order_by(WorkOrder.id).all():
        n, lost = counts.get(o.id, (0, 0))
        d = docs.get(o.document_id)
        out.append({"id": o.id, "document_id": o.document_id,
                    "file": d.name if d else o.source, "kind": o.kind,
                    "number": o.number, "date": o.date,
                    "base": ("№ %s от %s" % (o.base_number, o.base_date))
                            if o.base_number else None,
                    "contract": o.contract_code, "agreement": o.agreement,
                    "account": o.account, "topic": o.topic, "leader": o.leader,
                    "from": o.date_from, "to": o.date_to,
                    "people": n, "lost": lost})
    return out


@app.post("/api/workorder/{order_id}/contract")
async def workorder_contract(order_id: int, request: Request):
    """Договор приказа о рабочей группе — выбором экономиста, когда разбор его
    не нашёл или нашёл не тот: {"code": шифр} или {"code": null}."""
    body = await request.json()
    db = session()
    try:
        o = db.get(WorkOrder, order_id)
        if o is None:
            raise HTTPException(404, "приказ не найден")
        code = str(body.get("code") or "").strip() or None
        if code and db.query(Contract).filter_by(code=code).first() is None:
            raise HTTPException(404, "договор %s не найден" % code)
        o.contract_code = code
        doc = db.get(Document, o.document_id) if o.document_id else None
        if doc is not None:
            doc.contract_code = code
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.post("/api/workorder/member/{member_id}")
async def workorder_member(member_id: int, request: Request):
    """Строка штатного расписания члена группы — выбором экономиста:
    {"employee": код строки} или {"employee": null}. Строка, а не человек:
    в группу входит связка «человек + должность» (28.09.2026)."""
    body = await request.json()
    db = session()
    try:
        m = db.get(WorkOrderMember, member_id)
        if m is None:
            raise HTTPException(404, "строка группы не найдена")
        code = str(body.get("employee") or "").strip() or None
        if code and db.query(Employee).filter_by(code=code).first() is None:
            raise HTTPException(404, "строка %s не найдена в штатном расписании" % code)
        m.employee_code = code
        db.commit()
        return {"ok": True}
    finally:
        db.close()


def _fix_key(x, code, contract, kind):
    return x["сотрудник"] == code and x["договор"] == contract and x["вид"] == kind


@app.post("/api/case/{case_id}/fixes/workgroup")
async def set_workgroup_pay(case_id: int, request: Request):
    """Выплата члену рабочей группы договора в этом плане (пользователь
    30.09.2026: «какую выплату ему дать, решает расчёт — это можно задать,
    только удалить нельзя, если не задано — то по возможности ВСЮ»).

    {"employee", "contract", "kinds": [виды], "only": да/нет, "amount"}.
    Пустые виды без «only» — по умолчанию: вся зарплата с договора по
    возможности. «only» — вся зарплата: прочие договоры плана ему запрещены.
    Названные виды — хотя бы одна выплата каждого; сумма — у одного вида.
    Сам член группы из закреплений не снимается: состав меняется приказом."""
    body = await request.json()
    db = session()
    try:
        case = db.get(Case, case_id)
        if case is None:
            raise HTTPException(404, "план не найден")
        code = str(body.get("employee") or "").strip()
        contract = str(body.get("contract") or "").strip()
        members = {(g["договор"], g["сотрудник"]) for g in _workgroup_current(db)}
        if (contract, code) not in members:
            raise HTTPException(404, "сотрудник %s не входит в рабочую группу договора %s"
                                % (code, contract))
        kinds = []
        for k in body.get("kinds") or []:
            k = str(k or "").strip().lower()
            if not k or k == "любой" or k in kinds:
                continue
            if k not in chat.KINDS:
                raise HTTPException(400, "неизвестный вид выплаты «%s»" % k)
            kinds.append(k)
        only = bool(body.get("only"))
        if only and kinds:
            raise HTTPException(400, "«вся зарплата» задаётся без названных видов выплат")
        amount = None
        if body.get("amount") not in (None, ""):
            if len(kinds) != 1:
                raise HTTPException(400, "сумма задаётся, когда назван один вид выплаты")
            amount = _salary_amount(body.get("amount"))
        if only:
            # Вся зарплата отсюда и работа по другому договору — спор двух
            # условий, а не выбор расчёта (как у закреплений, 21.09.2026).
            others = sorted({c for c, e in members if e == code and c != contract}
                            | {x["договор"] for x in chat.plan_settings(case)["назначения"]
                               if x["сотрудник"] == code and x["договор"] != contract})
            if others:
                raise HTTPException(409, "сотрудник закреплён и на договоре %s: «вся "
                                    "зарплата» с %s невыполнима" % (", ".join(others), contract))
        d = chat.plan_settings(case)
        pay = dict(d.get("рабочие группы") or {})
        key = "%s|%s" % (contract, code)
        if kinds or only:
            pay[key] = {"виды": kinds, "только": only, "сумма": amount}
        else:
            pay.pop(key, None)
        d["рабочие группы"] = pay
        chat._save_plan_settings(db, case, d, "выплата члену рабочей группы договора %s: %s — %s"
                                 % (contract, code, "вся зарплата" if only
                                    else ", ".join(kinds) if kinds else "вся, по возможности"))
        db.commit()
        return {"ok": True, "choice": pay.get(key)}
    finally:
        db.close()


@app.post("/api/case/{case_id}/fixes")
async def add_fix(case_id: int, request: Request):
    """Закрепить сотрудника за договором плана на срок (решение пользователя
    19.09.2026) — тот же список, что заполняет чат (plan_settings).

    {"employee": код строки, "contract": шифр, "from": 1–12, "to": 1–12,
    "kind": вид выплаты или пусто, "amount": сумма в месяц — только у вида,
    "replaces": вид правимой строки, если его сменили}.
    Вид не задан — «любой»: в каждом месяце срока сотрудник получает с этого
    договора хотя бы одну выплату, какую — решает расчёт."""
    body = await request.json()
    db = session()
    try:
        case = db.get(Case, case_id)
        if case is None:
            raise HTTPException(404, "план не найден")
        code = str(body.get("employee") or "").strip()
        emp = db.query(Employee).filter_by(code=code).first()
        if emp is None:
            raise HTTPException(404, "сотрудник %s не найден в штатном расписании" % code)
        contract = str(body.get("contract") or "").strip()
        if db.query(Contract).filter_by(code=contract).first() is None:
            raise HTTPException(404, "договор %s не найден" % contract)
        try:
            a, b = int(body.get("from")), int(body.get("to"))
        except (TypeError, ValueError):
            raise HTTPException(400, "нужен срок: месяц «с» и месяц «по»")
        if not 1 <= a <= b <= 12:
            raise HTTPException(400, "срок — месяцы от 1 до 12, «с» не позже «по»")
        # Строка таблицы — пара «сотрудник — договор»: названные виды
        # приходят списком и заменяют все прежние записи пары (решение
        # пользователя 21.09.2026: «множественный выбор»). Одиночный «kind»
        # остаётся для чата и правки одной ячейки.
        pair = isinstance(body.get("kinds"), list)
        if pair:
            kinds = []
            for k in body["kinds"]:
                k = str(k or "").strip().lower()
                if not k or k == "любой" or k in kinds:
                    continue
                if k not in chat.KINDS:
                    raise HTTPException(400, "неизвестный вид выплаты «%s»" % k)
                kinds.append(k)
            kinds = kinds or ["любой"]
        else:
            kind = str(body.get("kind") or "").strip().lower() or "любой"
            if kind != "любой" and kind not in chat.KINDS:
                raise HTTPException(400, "неизвестный вид выплаты «%s»" % kind)
            kinds = [kind]
        # «Вся зарплата с этого договора» — то же закрепление любым видом,
        # плюс запрет платить этому человеку с прочих договоров плана
        # (решение пользователя 21.09.2026). Запреты не хранятся: они
        # выводятся из признака перед расчётом (_apply_plan_settings).
        only = bool(body.get("only"))
        if only and kinds != ["любой"]:
            raise HTTPException(400, "«вся зарплата» задаётся без названных видов выплат")
        amount = None
        if body.get("amount") not in (None, ""):
            if kinds == ["любой"]:
                raise HTTPException(400, "сумма задаётся только вместе с видом выплаты")
            if len(kinds) > 1:
                raise HTTPException(400, "сумма задаётся, когда назван один вид выплаты")
            amount = _salary_amount(body.get("amount"))
        d = chat.plan_settings(case)
        if only:
            # Человек не может получать всю зарплату отсюда и быть закреплён
            # на другом договоре: это спор двух условий, а не выбор расчёта.
            others = sorted({x["договор"] for x in d["назначения"]
                             if x["сотрудник"] == emp.code and x["договор"] != contract})
            if others:
                raise HTTPException(409, "у сотрудника есть закрепление на договоре %s: "
                                    "снимите его или выберите другой вид выплаты"
                                    % ", ".join(others))
        recs = [{"сотрудник": emp.code, "фио": emp.fio, "договор": contract, "с": a, "по": b,
                 "вид": k, "сумма": amount if len(kinds) == 1 else None}
                for k in kinds]
        if only:
            for r in recs:
                r["только"] = True
        # Правка строки в таблице может сменить вид: прежняя запись той же
        # пары снимается этим же сохранением, а не отдельным запросом.
        was = str(body.get("replaces") or "").strip().lower()
        drop = set(kinds) | ({was} - {""})
        same_pair = lambda x: x["сотрудник"] == emp.code and x["договор"] == contract
        # Закрепление и запрет той же пары и вида взаимоисключают друг друга —
        # как в чате (chat.apply_fixes). «Любой» снимает запреты пары целиком.
        d["запреты"] = [x for x in d["запреты"]
                        if not (same_pair(x) and (kinds == ["любой"] or x["вид"] in drop))]
        d["назначения"] = [x for x in d["назначения"]
                           if not (same_pair(x) and (pair or x["вид"] in drop))] + recs
        chat._save_plan_settings(db, case, d,
                                 "закрепление в условиях договора %s: %s, месяцы %d–%d, %s"
                                 % (contract, emp.fio or emp.code, a, b,
                                    "вся зарплата" if only else ", ".join(kinds)))
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.delete("/api/case/{case_id}/fixes")
async def delete_fix(case_id: int, request: Request):
    """Снять закрепление: {"employee", "contract", "kind"}; {"all": true} —
    все виды пары, потому что строка таблицы — пара «сотрудник — договор»."""
    body = await request.json()
    db = session()
    try:
        case = db.get(Case, case_id)
        if case is None:
            raise HTTPException(404, "план не найден")
        code = str(body.get("employee") or "").strip()
        contract = str(body.get("contract") or "").strip()
        kind = str(body.get("kind") or "").strip().lower() or "любой"
        whole = bool(body.get("all"))
        d = chat.plan_settings(case)
        kept = [x for x in d["назначения"]
                if not (x["сотрудник"] == code and x["договор"] == contract
                        and (whole or x["вид"] == kind))]
        if len(kept) == len(d["назначения"]):
            raise HTTPException(404, "закрепление не найдено")
        d["назначения"] = kept
        chat._save_plan_settings(db, case, d, "снято закрепление в условиях договора %s: %s, %s"
                                 % (contract, code, "все виды" if whole else kind))
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ── проекты приказов из расчёта плана (30.09.2026) ─────────────────────
#: Виды выплат, переход которых между договорами — смена источника.
_ORDER_MOVABLE = ("оклад", "120", "122", "124", "152")
_ORDER_KIND_RANK = {k: i for i, k in enumerate(("оклад", "120", "122", "124", "152", "приказ"))}
_ORDER_RANK = {"стимулирующая": 0, "надбавка": 1, "источник": 2, "состав группы": 3}


def _order_run(db, case, run_id=None):
    """Расчёт, по которому собираются приказы: выбранный в шапке плана или
    последний удачный; None — удачного расчёта нет. Расчёт без файлов —
    ошибка, а не молчаливая подмена другим расчётом или закреплениями."""
    if run_id:
        run = db.get(Run, run_id)
        if run is None or run.case_id != case.id or run.status != "OPTIMAL":
            raise HTTPException(404, "расчёт не найден")
    else:
        run = (db.query(Run).filter_by(case_id=case.id, status="OPTIMAL")
               .order_by(Run.id.desc()).first())
        if run is None:
            return None
    if not (run.result_path and os.path.exists(run.result_path)):
        raise HTTPException(409, "Проекты приказов недоступны: файлы прогона не сохранены")
    return run


def _order_changes(plan, frozen=0):
    """Что меняется у сотрудников по месяцам — строки приказов: одно
    изменение — одна строка одного приказа (спецификация дизайнера
    30.09.2026).

    Стимулирующая — выплата «приказ» договора в месяце. Вид выплаты (оклад,
    120–152) у одного договора убыл, у другого прибыл — это смена источника:
    приказ у договора с наибольшим приростом (решение 30.09.2026: приказ
    издают в связи с новым источником), строки — все договоры, где вид
    изменился, и в «надбавки» этого месяца вид не идёт. Иначе надбавка
    договора появилась, изменилась или прекратилась — «надбавка». Оклад без
    перехода (смена ставки) приказом этих видов не оформляется. Январь
    сравнивать не с чем: надбавки в нём — «установить» (решение 30.09.2026:
    план года начинается своими приказами). Месяцы факта (по frozen) уже
    прошли — строк нет, но с последним из них сравнивается первый месяц
    плана."""
    amt = {}
    for p in plan:
        key = (p["emp"], p["month"], p["contract"], p["kind"])
        amt[key] = amt.get(key, 0.0) + float(p["amount"] or 0)
    emps = sorted({p["emp"] for p in plan})
    contracts = sorted({p["contract"] for p in plan})

    def line(order_kind, owner, m, emp, kind, where, was, now_, act=None):
        return {"приказ": order_kind, "договор": owner, "месяц": m, "сотрудник": emp,
                "вид": kind, "договор строки": where, "действие": act,
                "было": round(was, 2) if was > 0.5 else None,
                "стало": round(now_, 2) if now_ > 0.5 else None}

    out = []
    for emp in emps:
        for m in range(int(frozen or 0) + 1, 13):
            for c in contracts:
                v = amt.get((emp, m, c, "приказ"), 0.0)
                if v > 0.5:
                    out.append(line("стимулирующая", c, m, emp, "приказ", c, 0.0, v))
            for k in _ORDER_MOVABLE:
                was = {c: amt.get((emp, m - 1, c, k), 0.0) if m > 1 else 0.0 for c in contracts}
                now_ = {c: amt.get((emp, m, c, k), 0.0) for c in contracts}
                moved = [c for c in contracts if abs(now_[c] - was[c]) > 0.5]
                up = [c for c in moved if now_[c] > was[c]]
                down = [c for c in moved if now_[c] < was[c]]
                if up and down:
                    owner = sorted(up, key=lambda c: (-(now_[c] - was[c]), c))[0]
                    for c in moved:
                        out.append(line("источник", owner, m, emp, k, c, was[c], now_[c]))
                elif k != "оклад":
                    for c in moved:
                        act = ("установить" if was[c] <= 0.5 else "прекратить" if now_[c] <= 0.5
                               else "изменить")
                        out.append(line("надбавка", c, m, emp, k, c, was[c], now_[c], act))
    return out


def _ru_date(text):
    """«28.07.2022» или «28.07.22» → (год, месяц, день); не дата — None."""
    p = str(text or "").strip().split(".")
    try:
        y, m, d = int(p[2]), int(p[1]), int(p[0])
    except (IndexError, ValueError):
        return None
    return (y + 2000 if y < 100 else y, m, d)


def _group_base(db, code):
    """Приказ, в который вносятся изменения: последний приказ о создании
    группы договора (нет такого — последний любой); None — группы нет."""
    orders = sorted(db.query(WorkOrder).filter_by(contract_code=code).all(),
                    key=lambda o: (_ru_date(o.date) or (0, 0, 0), o.id))
    made = [o for o in orders if o.kind == "создание"]
    return (made or orders or [None])[-1]


def _group_changes(db, plan, frozen, year):
    """Изменения состава рабочих групп по расчёту — строки приказа о внесении
    изменений (UI-023, решения пользователя 12.10.2026). «Включить» — того,
    кому расчёт платит с договора выплату любого вида и кого нет в
    действующем составе группы, с 1-го числа месяца первой выплаты.
    «Исключить» — члена группы, которому с договора ничего не платят, с
    1-го числа месяца, с которого выплаты прекратились (в пределах срока
    группы). Только договоры, у которых группа уже есть: приказ о создании
    сервис не формирует. Месяцы факта приказов не дают."""
    first = int(frozen or 0) + 1
    paid = {}
    for p in plan:
        if float(p["amount"] or 0) > 0.5:
            paid.setdefault((p["contract"], p["emp"]), set()).add(int(p["month"]))
    with_group = {o.contract_code for o in db.query(WorkOrder)
                  .filter(WorkOrder.contract_code.isnot(None)).all()}
    members = {}
    for r in _workgroup_current(db):
        members.setdefault(r["договор"], {})[r["сотрудник"]] = r

    def months(r):
        """Месяцы года, в которые строка состоит в группе по сроку приказа."""
        start, end = _ru_date(r.get("с")), _ru_date(r.get("по"))
        lo = 1 if not start or start[0] < year else 13 if start[0] > year else start[1]
        hi = 12 if not end or end[0] > year else 0 if end[0] < year else end[1]
        return range(lo, hi + 1)

    def line(code, m, emp, act):
        return {"приказ": "состав группы", "договор": code, "месяц": m, "сотрудник": emp,
                "вид": "", "договор строки": code, "действие": act, "было": None,
                "стало": None}

    out = []
    for code in sorted({c for c, _ in paid} & with_group):
        group = members.get(code, {})
        for (c, emp), got in sorted(paid.items()):
            if c != code or emp in group:
                continue
            later = [m for m in got if m >= first]
            if later:
                out.append(line(code, min(later), emp, "включить"))
        for emp, r in sorted(group.items()):
            term = months(r)
            if not term:
                continue
            inside = [m for m in paid.get((code, emp), ()) if m in term]
            start = max(max(inside) + 1 if inside else term[0], first)
            if start <= term[-1]:
                out.append(line(code, start, emp, "исключить"))
    return out


def _order_lines(db, case, plan, frozen):
    """Все строки проектов приказов расчёта: выплаты и состав рабочих групп."""
    return (_order_changes(plan, frozen)
            + _group_changes(db, plan, frozen, case.year or time.localtime().tm_year))


def _order_source(db, case, run_id=None):
    """(план выплат, ставки, расчёт, месяцы факта) расчёта для приказов;
    None — удачного расчёта нет."""
    import rules as rules_mod
    run = _order_run(db, case, run_id)
    if run is None:
        return None
    res = rules_mod._read_result(run.result_path)
    return res["plan"], res.get("rates") or [], run, int(res.get("frozen") or 0)


def _order_sorted(lines, staff):
    """Строки приказа: сотрудники по ФИО, внутри — вид выплаты, договор."""
    fio = lambda code: str((staff.get(code).fio if staff.get(code) else None) or code)  # noqa: E731
    return sorted(lines, key=lambda r: (fio(r["сотрудник"]), _ORDER_KIND_RANK.get(r["вид"], 9),
                                        r["договор строки"]))


def _orders(db, lines):
    """Приказы: вид × договор × месяц, строки — изменения по сотрудникам."""
    staff = {e.code: e for e in db.query(Employee).all()}
    names = {c.code: c.name for c in db.query(Contract).all()}
    groups = {}
    for ln in lines:
        groups.setdefault((ln["приказ"], ln["договор"], ln["месяц"]), []).append(ln)
    out = []
    for (kind, contract, month), rows in groups.items():
        rows = _order_sorted(rows, staff)
        before = [r["было"] for r in rows if r["было"] is not None]
        after = [r["стало"] for r in rows if r["стало"] is not None]
        out.append({
            "key": "%s|%s|%d" % (kind, contract, month), "kind": kind,
            "title": order.ORDER_TYPES[kind]["name"], "contract": contract,
            "contract_name": names.get(contract), "month": month,
            "kinds": sorted({r["вид"] for r in rows if r["вид"]},
                            key=lambda k: _ORDER_KIND_RANK.get(k, 9)),
            "people": len({r["сотрудник"] for r in rows}),
            "before": round(sum(before), 2) if before else None,
            "after": round(sum(after), 2) if after else None,
            "lines": [{"employee": r["сотрудник"],
                       "fio": (staff[r["сотрудник"]].fio if r["сотрудник"] in staff else None)
                       or r["сотрудник"],
                       "person_code": (staff[r["сотрудник"]].person_code
                                       if r["сотрудник"] in staff else None) or r["сотрудник"],
                       "position": (staff[r["сотрудник"]].position
                                    if r["сотрудник"] in staff else None),
                       "kind": r["вид"], "contract": r["договор строки"], "action": r["действие"],
                       "before": r["было"], "after": r["стало"]} for r in rows]})
    out.sort(key=lambda o: (o["month"], o["contract"], _ORDER_RANK.get(o["kind"], 9)))
    return out


@app.get("/api/case/{case_id}/orders")
def plan_orders(case_id: int, run: int | None = None):
    """Проекты приказов плана — по расчёту, выбранному в шапке, иначе по
    последнему удачному (решение пользователя 28.09.2026: «отдельная вкладка
    приказы»; 30.09.2026: «по конкретному договору выбрать тип — приказ, в
    выбранный месяц для выбранного сотрудника, чтоб в шаблон подставилось и
    выгрузилось»; о каскаде выборов: «форма выбора приказа на сотрудника не
    продуманная»). Строка — приказ вида × договора × месяца со списком
    сотрудников (Т-11а), строки сотрудников — для приказа на одного (Т-11).
    Закрепление с выплатой «приказ» расчёт исполняет — оно приходит сюда
    стимулирующей выплатой с той суммой, что расчёт платит."""
    db = session()
    try:
        case = db.get(Case, case_id)
        if case is None:
            raise HTTPException(404, "план не найден")
        src = _order_source(db, case, run)
        if src is None:
            return {"run": None, "frozen": 0, "orders": []}
        plan, _rates, used, frozen = src
        return {"run": used.id, "frozen": frozen,
                "orders": _orders(db, _order_lines(db, case, plan, frozen))}
    finally:
        db.close()


@app.get("/api/case/{case_id}/orders/file")
def plan_order_file(case_id: int, kind: str, contract: str, month: int,
                    run: int | None = None, employee: str | None = None):
    """Приказ файлом Word: весь (все сотрудники, Т-11а) или на одного
    сотрудника (employee, Т-11). Бланк — шаблон организации
    data/templates/<вид>.docx, если он есть, иначе бланк сервиса."""
    from db import DATA_DIR
    if kind not in order.ORDER_TYPES:
        raise HTTPException(400, "неизвестный вид приказа «%s»" % kind)
    if not 1 <= month <= 12:
        raise HTTPException(400, "месяц — от 1 до 12")
    db = session()
    try:
        case = db.get(Case, case_id)
        if case is None:
            raise HTTPException(404, "план не найден")
        src = _order_source(db, case, run)
        if src is None:
            raise HTTPException(404, "расчёта плана нет: приказы собираются по нему")
        plan, rates, _used, frozen = src
        lines = [ln for ln in _order_lines(db, case, plan, frozen)
                 if ln["приказ"] == kind and ln["договор"] == contract and ln["месяц"] == month
                 and (not employee or ln["сотрудник"] == employee)]
        if not lines:
            raise HTTPException(404, "такого приказа в расчёте нет")
        staff = {e.code: e for e in db.query(Employee).all()}
        ctrs = {c.code: c for c in db.query(Contract).all()}
        rate_of = {(r["emp"], r["contract"], r["month"]): r["rate"] for r in rates}
        c = ctrs.get(contract)
        rows = []
        for ln in _order_sorted(lines, staff):
            e = staff.get(ln["сотрудник"])
            rows.append({"фио": (e.fio if e else None) or ln["сотрудник"],
                         "табельный": (e.person_code if e else None) or ln["сотрудник"],
                         "должность": e.position if e else None,
                         "подразделение": e.department if e else None,
                         "ставка": rate_of.get((ln["сотрудник"], ln["договор строки"], month)),
                         "вид": ln["вид"], "действие": ln["действие"],
                         "было": ln["было"], "стало": ln["стало"],
                         "договор": ln["договор строки"],
                         "счёт": ctrs[ln["договор строки"]].account
                         if ln["договор строки"] in ctrs else None})
        data = {"тип": kind, "организация": None, "договор": contract,
                "наименование": c.name if c else None, "номер договора": c.number if c else None,
                "счёт": c.account if c else None, "месяц": month, "год": case.year,
                "строки": rows}
        if kind == "состав группы":
            # Поля — те, что разбор приказа-входа читает обратно (app/workgroup.py):
            # подписанный приказ, загруженный в договор, меняет группу без правки.
            base = _group_base(db, contract)
            year = case.year or time.localtime().tm_year
            data["группа"] = {
                "основание номер": base.number if base else None,
                "основание дата": base.date if base else None,
                "соглашение": base.agreement if base else None,
                "соглашение дата": base.agreement_date if base else None,
                "тема": base.topic if base else None,
                "с": "01.%02d.%d" % (month, year),
                "по": (base.date_to if base and _ru_date(base.date_to) else "31.12.%d" % year)}
            if base and base.account and not data["счёт"]:
                data["счёт"] = base.account
        name = order.file_name(data, one=bool(employee))
        return Response(
            order.build_order(data, os.path.join(DATA_DIR, "templates")),
            media_type=("application/vnd.openxmlformats-officedocument"
                        ".wordprocessingml.document"),
            headers={"Content-Disposition": "attachment; filename*=UTF-8''%s" % quote(name)})
    finally:
        db.close()


@app.get("/api/order/templates")
def order_templates():
    """Заготовки бланков приказов одним архивом — для того, кто делает бланк
    организации: поля {Имя} и их перечень. Бланк кладут в
    data/templates/<вид>.docx под тем же именем. На экране этой кнопки нет:
    экономисту она не нужна (пользователь 30.09.2026 о ней: «зачем эта
    кнопка?»)."""
    name = "Заготовки бланков приказов.zip"
    return Response(order.templates_zip(), media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''%s"
                             % quote(name)})


@app.post("/api/case/{case_id}/basis")
async def change_plan_basis(case_id: int, request: Request):
    """Флажок договоров или версия документа — для этого плана.

    {"contracts": [шифры], "included": да/нет} или {"document": первая версия,
    "version": выбранная}. Ответ — каталог после изменения.
    """
    body = await request.json()
    db = session()
    try:
        case = db.get(Case, case_id)
        if case is None:
            raise HTTPException(404, "план не найден")
        if isinstance(body.get("contracts"), list):
            basis.set_included(db, case, body["contracts"], bool(body.get("included")),
                               grounds="флажок договора в реестре")
        elif isinstance(body.get("employees"), list):
            # Строки штатного расписания в расчёте этого плана (30.09.2026).
            basis.set_staff_included(db, case, body["employees"], bool(body.get("included")),
                                     grounds="флажок на вкладке «Штатное расписание» плана")
        elif body.get("contract") and isinstance(body.get("fields"), dict):
            # Условия договора для этого плана (SOURCE-006): пустое значение
            # снимает переопределение и возвращает реестровое.
            basis.set_overrides(db, case, body["contract"], body["fields"],
                                grounds="условия договора на экране договора")
        elif body.get("employee") and isinstance(body.get("fields"), dict):
            # Строка штатного расписания в этом плане (30.09.2026): ставка,
            # срок, зарплата по месяцам; пустое значение снимает правку.
            try:
                basis.set_employee_overrides(db, case, body["employee"], body["fields"],
                                             grounds="строка на вкладке «Штатное расписание» "
                                                    "или «Заработная плата по месяцам» плана")
            except ValueError as e:
                raise HTTPException(400, str(e))
        elif body.get("document") is not None and body.get("version") is not None:
            try:
                basis.set_version(db, case, int(body["document"]), int(body["version"]),
                                  grounds="версия документа в реестре")
            except LookupError as e:
                raise HTTPException(404, str(e))
            except ValueError as e:
                raise HTTPException(409, str(e))
        else:
            raise HTTPException(400, "нужны contracts или employees с included "
                                     "либо document и version")
        return _basis_out(db, case)
    finally:
        db.close()


@app.post("/api/case/{case_id}/upload")
async def upload(case_id: int, background: BackgroundTasks, files: list[UploadFile],
                 scope: str = fastapi.Form("реестр"), contract: str = fastapi.Form(""),
                 version_of: str = fastapi.Form("")):
    # «план» — песочница: документ виден чату этого плана, разбор дает только
    # предложения, строки в реестр не идут.
    scope = "план" if str(scope).strip().lower() == "план" else "реестр"
    # Загрузка в договор: файл встаёт в каталог этого договора (SOURCE-003).
    contract = str(contract or "").strip()[:60] or None
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
            prev = _previous_version(db, name, contract)
            # Новая версия в выбранный документ (как в Confluence): файл встаёт
            # в его цепочку, как бы он ни назывался (решение пользователя
            # 18.09.2026). Цепочку продолжает её действующая версия.
            if str(version_of or "").strip().isdigit():
                chosen = db.get(Document, int(version_of))
                while chosen is not None and chosen.state == "заменен":
                    nxt = db.query(Document).filter_by(supersedes_id=chosen.id).first()
                    if nxt is None:
                        break
                    chosen = nxt
                if chosen is None:
                    raise HTTPException(404, "документ не найден")
                prev = chosen
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
                           sha256=digest, scope=scope, contract_code=contract)
            if prev is not None:
                _retire_document(db, prev, new_version=(prev.version or 1) + 1)
                doc.version = (prev.version or 1) + 1
                doc.supersedes_id = prev.id
                doc.contract_code = contract or prev.contract_code
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

    warn = []
    data = _registry_data(db, case, warn)
    # Без зарплаты месяца считать нечего: сборка входа откажет с тем же
    # перечнем, поэтому здесь он — причина, а не допущение.
    gaps = build_input.salary_gaps(data["employees"], data.get("salary_months"), case.year)
    if not gaps:
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
            tmp.close()
            build_input.build(reference.TEMPLATE, tmp.name, data, warn)
            os.remove(tmp.name)
        except Exception as e:  # noqa: BLE001 — сборка не должна ронять сводку
            warn.append("Сборка входного файла не удалась: %s" % str(e)[:200])
    ps = _apply_plan_settings(data, case, warn)
    through = int(data.get("fact_through") or 0)
    if through:
        # Кто работает в месяцах факта, но в факте не встретился: его месяцы
        # расчёт оставит пустыми — это признак неполной выгрузки, а не план.
        seen = {(f.employee_code, f.month) for f in data["facts"]}
        lost = [e for e in data["employees"]
                if any((e.code, m) not in seen
                       for m in build_input.active_months(e.date_from, e.date_to, case.year)
                       if m <= through)]
        if not data["facts"]:
            warn.append("Факт начислений по %s не загружен: месяцы по %s включительно "
                        "останутся без выплат." % (facts.MONTH_WORDS[through - 1],
                                                   facts.MONTH_WORDS[through - 1]))
        elif lost:
            warn.append("В факте начислений по %s нет строк у сотрудников: %s — их месяцы "
                        "факта останутся без выплат."
                        % (facts.MONTH_WORDS[through - 1],
                           ", ".join((e.fio or e.code) for e in lost[:8])
                           + (" и ещё %d" % (len(lost) - 8) if len(lost) > 8 else "")))
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
        ("Факт начислений", "по %s включительно, строк %d" % (facts.MONTH_WORDS[through - 1],
                                                                len(data["facts"]))
         if through else "не учитывается"),
        ("Трудоёмкость", "в пределах допуска %s" % settings.get("допуск трудоёмкости", "5%")),
        ("Виды выплат", "только разрешённые договором"),
        ("Основная ставка", "обязательна для основного сотрудника"),
        ("Дефицит выплат", "разрешён" if str(settings.get("разрешить дефицит", "нет")).lower() in ("да", "true", "1") else "не разрешён"),
        ("Правила замещения", "%d загружено" % len(data["substitutions"])),
    ]
    why = []
    if not data["employees"] or not data["contracts"]:
        why.append("Нет сотрудников или договоров. Загрузите документы в реестр.")
    if gaps:
        why.append(build_input.gaps_text(gaps))
    return {"строки": counts, "ограничения": constraints, "допущения": warn,
            "стоит": bool(why), "почему": why, "год": case.year}


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
            # Хеш версии документа: форма возвращает его с решением, и сервер
            # не применит решение к документу, который с тех пор заменили.
            "sha256": d.sha256,
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
                    "величин пакета": db.query(ContractValue)
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
            _warm_once(("книга", d.path), _warm_workbook, d.path)
        # Печать в PDF нужна и книге, и Word: карточка открывается на
        # вкладке «Документ», а печать идёт секунды.
        if not _pdf_ready(d):
            _warm_once(("pdf", d.id), _warm_pdf, d.id)
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
            "employees": [{"code": e.code, "person_code": e.person_code,
                            "fio": e.fio, "position": e.position,
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
    expected = str(body.get("document_sha256") or "") or None
    op = str(body.get("operation_id") or "") or None
    db = session()
    try:
        return _operation(db, op, "решение по строкам", "документ %d" % doc_id,
                          {"accept": sorted(take), "reject": sorted(drop), "document_sha256": expected},
                          lambda: _decide_do(db, doc_id, take, drop, expected))
    finally:
        db.close()


def _verdict_row(doc, pr, verdict, version):
    """Приговор привязан к точной редакции документа, предложению и снимку
    контекста извлечения, а не к имени файла (MEM-003): другая редакция под
    тем же именем его не наследует, стенд проверяет те же байты и те же
    подсказки."""
    return Verdict(document_name=doc.name, entity=pr.entity, fields=pr.payload,
                   verdict=verdict, agent_version=version, grade=pr.grade,
                   document_id=doc.id, document_sha256=doc.sha256,
                   proposal_id=pr.id, context_sha256=pr.context_sha256)


def _decide_do(db, doc_id, take, drop, expected):
    if True:
        doc = db.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "документ не найден")
        from fot_planner.harness_local import decisions as D
        scope = "план %d" % doc.case_id if doc.scope == "план" and doc.case_id else "организация"
        if D.stale(expected, doc.sha256):
            # Форма показывала другую версию документа. Молча применить её
            # решение к новой версии нельзя (VER-004, UI-002): отказ, и он
            # тоже записывается — это решение, которого не было.
            record_decision(db, kind="данные", subject_kind="документ", subject_id=doc.id,
                            subject_digest=D.digest(doc.sha256, sorted(take), sorted(drop)),
                            scope=scope, action={"принято": sorted(take), "отклонено": sorted(drop)},
                            grounds="карточка документа", precondition=expected,
                            outcome="отклонено: документ изменился после открытия формы")
            db.commit()
            raise HTTPException(409, "документ изменился после открытия формы — откройте карточку заново")
        rows = (db.query(Proposal)
                .filter(Proposal.document_id == doc_id,
                        Proposal.state == "предложено").all())
        added = {"сотрудник": 0, "договор": 0, "трудоемкость": 0,
                 "поступление": 0, "надбавка 120": 0, "правило замещения": 0,
                 "должность": 0}
        ref_log = []
        correction_bindings = []
        version = agents.version_of("intake")
        for pr in rows:
            if pr.id in drop:
                pr.state = "отклонено"
                # Приговор — случай для стенда: эта строка была неверной.
                db.add(_verdict_row(doc, pr, "отклонено", version))
                # И память агента: в следующий раз он увидит, что так неверно.
                intake.remember_correction(db, doc, entity=pr.entity, wrong=pr.payload,
                                           note="отклонено экономистом", agent_version=version,
                                           decision_bindings=correction_bindings)
                continue
            if pr.id not in take:
                continue
            f = json.loads(pr.payload)
            db.add(_verdict_row(doc, pr, "принято", version))
            if pr.entity == "сотрудник":
                db.add(Employee(
                    code=str(f.get("code") or ""),
                    person_code=str(f.get("person_code") or f.get("code") or ""),
                    fio=f.get("fio"),
                    position=f.get("position"), rate=f.get("rate"),
                    salary=f.get("salary"), department=f.get("department"),
                    employment_type=f.get("employment"),
                    employment_category=f.get("category"),
                    allowed_contracts=f.get("allowed"),
                    forbidden_contracts=f.get("forbidden"),
                    account=f.get("account"),
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
        decided = [pr for pr in rows if pr.id in take or pr.id in drop]
        if decided:
            record_decision(db, kind="данные", subject_kind="документ", subject_id=doc.id,
                            subject_digest=D.digest(doc.sha256, [(pr.id, pr.entity, pr.payload) for pr in decided]),
                            scope=scope,
                            action={"принято": sorted(pr.id for pr in decided if pr.id in take),
                                    "отклонено": sorted(pr.id for pr in decided if pr.id in drop),
                                    "correction_bindings": correction_bindings},
                            grounds="карточка документа", precondition=expected)
        if any(added.values()):
            # Реестр изменился: прежние планы посчитаны без этих строк.
            actuality.mark_review(db, actuality.runs_for_registry_change(db, doc),
                                  "в реестр приняты строки из «%s» (%s)"
                                  % (doc.name, ", ".join("%s %d" % (k, v) for k, v in added.items() if v)))
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


def _office_child(src, out, html=False):
    """Office (COM) — только в дочернем процессе (app/office_pdf.py).

    COM в процессе сервера ронял его целиком: печать в PDF (0x80010001,
    затем 0xC0000005) и предпросмотр Word в HTML (0x800706ba), 12.10.2026.
    Упавший дочерний процесс значит только, что вида нет.
    """
    try:
        import win32com  # noqa: F401 — без pywin32 Office не позвать
    except ImportError:
        return False
    try:
        subprocess.run([sys.executable, os.path.join(HERE, "office_pdf.py")]
                       + (["--html"] if html else []) + [src, out],
                       timeout=300, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 — завис или не запустился
        return False
    return os.path.exists(out)


def _office_to_pdf(src, out):
    """Печать через Office — дочерним процессом; дальше пробуем LibreOffice."""
    return _office_child(src, out)


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
    return out if _office_child(src, out, html=True) else None


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
    ok = _office_child(src, raw_path, html=True)
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
            # Сначала строки, ждущие подтверждения: результат разбора живёт
            # в них, и правка из разговора о документе должна менять их, а не
            # заводить рядом строку реестра (12.10.2026).
            lines, rest = chat.apply_proposal_edits(db, doc, res["edits"])
            for pr, field, was, val in getattr(chat.apply_proposal_edits, "last_changes", []):
                # В память — строка словами, а не номер предложения: при
                # следующем разборе номера будут другие.
                f = json.loads(pr.payload)
                label = (f.get("code") or f.get("fio")
                         or " ".join(str(f.get(k)) for k in ("contract", "position", "month")
                                     if f.get(k)) or "п%d" % pr.id)
                intake.remember_correction(db, doc, entity=pr.entity, confirmed=True,
                    wrong=json.dumps({label: was, "поле": field}, ensure_ascii=False),
                    right=json.dumps({label: val, "поле": field}, ensure_ascii=False),
                    note=text[:300], agent_version=version)
            chat.apply_edits.last_changes = []
            if rest:
                lines += chat.apply_edits(db, None, rest, create=False)
            lines = list(dict.fromkeys(lines))      # одна причина — одна строка
            for entity, key, field, was, val in getattr(chat.apply_edits, "last_changes", []):
                finder = chat._find_contract if entity == 'договор' else chat._find_employee
                corrected = finder(db, None, key)
                same_source = corrected is not None and corrected.document_id == doc.id
                intake.remember_correction(db, doc, entity=entity,
                    confirmed=same_source,
                    wrong=json.dumps({key: was, "поле": field}, ensure_ascii=False),
                    right=json.dumps({key: val, "поле": field}, ensure_ascii=False),
                    note=text[:300], agent_version=version)
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
                intake.remember_correction(db, doc, entity="вид документа",
                                           wrong=doc.kind, right=kind,
                                           note=text[:300], agent_version=version)
                db.commit()
                background.add_task(_reprocess, case.id, doc.id, owner, kind)
        else:
            # Слова экономиста без готовой правки — тоже память: «это неверно»
            # уже отсекает вариант при следующем разборе.
            low = text.lower()
            if any(w in low for w in ("неверн", "ошибк", "не так", "неправильн")):
                intake.remember_correction(db, doc, confirmed=False,
                                           note=text[:300], agent_version=version)
                db.commit()
        return {"ok": True, "action": action}
    finally:
        db.close()


#: Что из строки реестра уходит в решатель: поле строки → графа входного
#: файла. Порядок важен: по нему подписывается подсветка.
SOLVER_FIELDS = {
    "employees": [("code", "код строки"), ("person_code", "табельный номер"),
                  ("fio", "ФИО"), ("position", "должность"),
                  ("department", "подразделение"), ("rate", "ставка"),
                  ("employment_type", "тип занятости"),
                  ("employment_category", "категория занятости"),
                  ("salary", "зарплата"), ("date_from", "дата начала"),
                  ("date_to", "дата окончания"),
                  ("allowed_contracts", "разрешенные договоры"),
                  ("forbidden_contracts", "запрещенные договоры"),
                  ("account", "лицевой счет")],
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


@app.get("/api/document/{doc_id}/corrections")
def document_corrections(doc_id: int):
    """Исправления по документу и их состояние: что пойдёт в подсказку, что
    спорит, что заменено решением оператора (MEM-002)."""
    db = session()
    try:
        doc = db.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "документ не найден")
        ctx = intake.correction_context(db, doc)
        groups = intake.correction_conflicts(db, doc)
        in_conflict = {i for g in groups for i in g}
        named = {"confirmed": "подтверждена, вне выборки", "proposed": "предложена",
                 "legacy_unknown": "область неизвестна"}
        rows = []
        for rec in intake._correction_records(db, doc):
            binding = rec.get("binding") or {}
            status = ("заменена" if rec["id"] in ctx["superseded"] else
                      "конфликт" if rec["id"] in in_conflict else
                      "применяется" if rec["id"] in ctx["correction_ids"] else
                      named.get(binding.get("status"), "не применяется"))
            c = rec["content"]
            rows.append({"id": rec["id"], "entity": c.get("entity"), "wrong": c.get("wrong"),
                         "right": c.get("right"), "note": c.get("note"),
                         "actor": rec.get("actor"), "status": status})
        return {"corrections": rows, "conflicts": groups, "superseded": ctx["superseded"],
                "hints": ctx["hints"], "document_sha256": doc.sha256}
    finally:
        db.close()


@app.post("/api/document/{doc_id}/corrections/resolve")
async def resolve_corrections(doc_id: int, request: Request):
    """Оператор выбирает, какая из противоречащих правок действует.

    Решение записывается с оператором и хешем предмета; остальные правки
    группы считаются заменёнными, но остаются в истории. Форма, показывавшая
    другую редакцию документа, отклоняется, и отказ тоже записан (VER-004).
    """
    body = await request.json()
    expected = str(body.get("document_sha256") or "") or None
    try:
        keep = int(body.get("keep"))
    except (TypeError, ValueError):
        raise HTTPException(400, "нужен номер правки keep")
    db = session()
    try:
        doc = db.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "документ не найден")
        from fot_planner.harness_local import decisions as D
        if D.stale(expected, doc.sha256):
            record_decision(db, kind="данные", subject_kind="конфликт правок", subject_id=keep,
                            subject_digest=D.digest(doc.sha256, keep), scope="документ %d" % doc.id,
                            action={"оставлено": keep},
                            grounds="разрешение конфликта исправлений оператором",
                            precondition=expected,
                            outcome="отклонено: документ изменился после открытия формы")
            db.commit()
            raise HTTPException(409, "документ изменился после открытия формы — откройте карточку заново")
        try:
            action = intake.resolve_correction_conflict(db, doc, keep, precondition=expected)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        db.commit()
        if doc.case_id:
            agents.say(db, doc.case_id,
                       "Конфликт исправлений по «%s» разрешён: действует правка № %d, "
                       "заменены № %s." % (doc.name, keep, ", ".join(map(str, action["заменены"]))),
                       agent="intake", document_id=doc.id)
        return {"ok": True, **action}
    finally:
        db.close()


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


def _registry_data(db, case, warn=None):
    """Все, что агент собрал для расчета, — из реестра организации.

    Реестр общий для всех планов: договор живет несколько лет, штатка меняется
    приказами. План на год берет из реестра то, что в этом году действует, а
    год берется из самого плана.

    Договоры и версии документов план выбирает сам (SOURCE-003): выбор
    применяется к копиям строк, реестр соседних планов не меняется. Что
    выбор убрал или подменил, дописывается в warn.
    """
    # Правки, с которыми реестр сравнялся, снимаются и перед расчётом: он
    # идёт за реестром, а в допущениях сказано, что снято (30.09.2026).
    gone = basis.dissolve_caught_up(db, case)
    if gone and warn is not None:
        warn.append("Правки плана сняты — реестр сравнялся с ними: %s." % "; ".join(gone))
    rows = basis.apply(db, case, {
        "employees": db.query(Employee).order_by(Employee.id).all(),
        "contracts": db.query(Contract).order_by(Contract.id).all(),
        "inflows": db.query(Inflow).order_by(Inflow.id).all(),
        "labor": db.query(LaborRow).order_by(LaborRow.id).all(),
        "secret": db.query(SecretAllowance).order_by(SecretAllowance.id).all(),
        "substitutions": db.query(Substitution).order_by(Substitution.id).all(),
        "values": db.query(ContractValue).order_by(ContractValue.id).all(),
    }, warn)
    data = {
        "year": case.year,
        "employees": rows["employees"],
        "contracts": rows["contracts"],
        "inflows": rows["inflows"],
        "labor": rows["labor"],
        "secret": rows["secret"],
        "values": rows["values"],
        "substitutions": [(s.position, s.replaced_by) for s in rows["substitutions"]],
        # Действующий состав рабочих групп: члены группы уходят в расчёт
        # закреплениями без вида (_apply_plan_settings).
        "workgroups": _workgroup_current(db),
        "settings": _case_settings(case),
        # Документы и число их строк во входе — основание расчёта.
        "row_sources": basis.row_sources(rows),
        # Свои суммы месяцев года плана; прочие месяцы решатель берёт из
        # зарплаты штатного расписания (правило 1, 19.09.2026).
        # Зарплата — только из таблицы по месяцам года плана (правило 1,
        # 19.09.2026): в штатном расписании её нет.
        # Правки зарплаты этого плана (30.09.2026) — поверх таблицы месяцев.
        # Строки одного человека в разных управленческих подразделениях
        # (01.10.2026) складываются: решатель платит человеку одну зарплату.
        "salary_months": basis.apply_salary(
            case, [(c, m, a) for (c, m), a in sorted(salary.month_totals(db, case.year).items())],
            warn),
        # Подразделения строк — в вход, для раздела 16 отчёта (снимок расчёта).
        "mgmt_rows": _mgmt_rows(db, case.year),
        # Факт начислений (30.09.2026): месяцы, за которые он загружен, расчёт
        # берёт как были и не пересчитывает (лист «факт_выплат»). Месяц
        # выбирать не нужно — он из самого документа (пользователь: «зачем
        # выбирать месяц, я не поняла? требуется подгрузить документ»).
        "fact_through": _fact_through(db, case),
        "facts": facts.rows_for(db, case.year, _fact_through(db, case),
                                skip_contracts=basis.read(case)["вне расчёта"]),
    }
    # Документы факта — тоже основание расчёта: замена или удаление файла
    # помечает расчёт к пересмотру, как у прочих документов.
    for f in data["facts"]:
        if f.document_id:
            one = data["row_sources"].setdefault(f.document_id, {})
            one["строк факта"] = one.get("строк факта", 0) + 1
    return data


def _fact_through(db, case):
    return facts.through(db, case.year) if case is not None else 0


@app.get("/api/case/{case_id}/facts/form")
def facts_form(case_id: int):
    """Форма для заполнения факта начислений — книга в форме 6.3 «ШР на
    дату, детализация назначений», раскладкой шаблона кадров пользователя
    (30.09.2026: «и как загружать её?»; «вот этот шаблон, я же скидывала»).
    Строки — уже загруженный факт года плана, «Дата построения» — последний
    день его месяцев; факта нет — одна шапка. Заполненную форму, как и
    выгрузку 1С, загружают на вкладке «Факт начислений»."""
    db = session()
    try:
        case = db.get(Case, case_id)
        if case is None:
            raise HTTPException(404, "план не найден")
        year = case.year or facts.case_year(db, case_id)
        name = "ШР детализация, факт начислений %s.xlsx" % year
        return Response(
            facts.form_book(year, facts.detail_rows(db, year), facts.through(db, year)),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename*=UTF-8''%s" % quote(name)})
    finally:
        db.close()


def _contract_months(c, year):
    """Месяцы года плана, в которых договор действует: (первый, последний).

    Даты — с правками плана (basis.apply уже подставил их в копию строки).
    Срок не задан — весь год; договор в этом году не действует — None."""
    def at(text):
        p = str(text or "").strip().split(".")
        try:
            return (int(p[2]), int(p[1])) if len(p) == 3 else None
        except ValueError:
            return None
    get = (lambda k: c.get(k)) if isinstance(c, dict) else (lambda k: getattr(c, k, None))
    if not year:
        return (1, 12)
    a, b = at(get("date_from")), at(get("date_to"))
    lo = 1 if a is None or a[0] < year else (a[1] if a[0] == year else 13)
    hi = 12 if b is None or b[0] > year else (b[1] if b[0] == year else 0)
    return (lo, hi) if lo <= hi else None


def _month_spans(months):
    """[1, 2, 3, 5] → [(1, 3), (5, 5)]."""
    out = []
    for m in sorted(set(months)):
        if out and m == out[-1][1] + 1:
            out[-1] = (out[-1][0], m)
        else:
            out.append((m, m))
    return out


def _apply_plan_settings(data, case, warn=None, ps=None):
    """Переменные, заданные экономистом в чате, поверх данных реестра:
    закрепления и запреты — листами ручных назначений, настройки — в
    «настройки». Они живут с планом и уходят в каждый его расчёт. ps —
    изменённые переменные для опыта разбора невыполнимости; без него —
    сохранённые в плане."""
    ps = ps if ps is not None else chat.plan_settings(case)
    if ps.get("настройки"):
        data["settings"] = {**(data.get("settings") or {}), **ps["настройки"]}
    # Веса целей после претензий — тоже условие плана (chat.apply_claims):
    # графы листа «настройки» с именами штрафов.
    if ps.get("веса"):
        data["settings"] = {**(data.get("settings") or {}), **ps["веса"]}
    fixes = ps.get("назначения") or []
    bans = list(ps.get("запреты") or [])
    year = data.get("year") or getattr(case, "year", None)
    # Члены рабочих групп договоров плана — закрепления без вида на срок группы
    # (пользователь 30.09.2026: «а что с сотрудниками, которые в группе? их же
    # тоже нужно отобразить, просто не дать удалить»): член группы получает с
    # договора выплату любого вида в каждом месяце срока, какого — решает
    # расчёт (остаточный принцип). Живут в приказах, а не в плане: снять их
    # можно только приказом, поэтому в хранимые закрепления они не пишутся.
    in_plan = {getattr(c, "code", None) or (c.get("code") if isinstance(c, dict) else None)
               for c in (data.get("contracts") or [])}
    # Выплата члену группы в этом плане (пользователь 30.09.2026: «какую
    # выплату ему дать, решает расчёт — это можно задать, только удалить
    # нельзя, если не задано — то по возможности ВСЮ»): не задано — вся
    # зарплата с договора по возможности (стадия решателя после БЭП);
    # «вся зарплата» — прочие договоры плана запрещены; названные виды —
    # хотя бы одна выплата каждого.
    group_pay = ps.get("рабочие группы") or {}
    # Строка штатного расписания вне расчёта плана (30.09.2026) уходит из
    # входа вместе со своими закреплениями: закрепление за тем, кого в
    # расчёте нет, решатель выполнить не может и кончает «решения нет».
    # Хранятся они по-прежнему — вернул строку в расчёт, вернулись и они.
    gone = basis.read(case)["сотрудники вне расчёта"]
    for g in data.get("workgroups") or []:
        if g.get("договор") not in in_plan or g.get("сотрудник") in gone:
            continue
        span = _contract_months({"date_from": g.get("с"), "date_to": g.get("по")}, year)
        if span is None:
            continue
        base = {"сотрудник": g["сотрудник"], "фио": g.get("фио"), "договор": g["договор"],
                "с": span[0], "по": span[1], "приказ": g.get("приказ")}
        choice = group_pay.get("%s|%s" % (g["договор"], g["сотрудник"])) or {}
        kinds = [k for k in (choice.get("виды") or []) if k in chat.KINDS]
        if kinds:
            pins = [{**base, "вид": k, "сумма": choice.get("сумма") if len(kinds) == 1 else None}
                    for k in kinds]
        elif choice.get("только"):
            pins = [{**base, "вид": "любой", "сумма": None, "только": True}]
        else:
            pins = [{**base, "вид": "любой", "сумма": None, "по возможности вся": True}]
        for pin in pins:
            if not any((f.get("сотрудник"), f.get("договор"), f.get("с"), f.get("по"),
                        f.get("вид"))
                       == (pin["сотрудник"], pin["договор"], pin["с"], pin["по"], pin["вид"])
                       for f in fixes):
                fixes = list(fixes) + [pin]
    # Месяцы без выплат договора в этом плане (владелец продукта 01.10.2026:
    # снятая галочка выключает договор на весь срок, а нужен «конкретный
    # месяц»): в эти месяцы с договора не платят никому — запрет каждому
    # сотруднику плана; закрепления на договоре теряют эти месяцы, иначе
    # закрепление и запрет спорили бы и расчёт кончался «решения нет».
    closed = {c: m for c, m in basis.closed_months(case).items() if c in in_plan}
    if closed:
        cut = []
        for f in fixes:
            shut = set(closed.get(f.get("договор")) or ())
            left = [m for m in range(int(f["с"]), int(f["по"]) + 1) if m not in shut]
            cut += [{**f, "с": a, "по": b} for a, b in _month_spans(left)]
        fixes = cut
        staff = [getattr(e, "code", None) or (e.get("code") if isinstance(e, dict) else None)
                 for e in (data.get("employees") or [])]
        for code, months in closed.items():
            for a, b in _month_spans(months):
                for emp in staff:
                    if emp:
                        bans.append({"сотрудник": emp, "фио": None, "договор": code,
                                     "с": a, "по": b, "вид": "любой", "сумма": None})
        if warn is not None:
            warn.append("Месяцы без выплат в этом плане: %s." % "; ".join(
                "%s — %s" % (code, facts.months_text(months))
                for code, months in sorted(closed.items())))
    # Закрепление действует только в месяцах договора в году плана, с
    # правками плана (решение пользователя 29.09.2026: «чтоб можно было
    # выбрать месяцы в период договора»): срок урезается до периода договора,
    # а закрепление целиком вне его в расчёт не идёт. Иначе срок, выбранный
    # до правки окончания договора, давал решателю невыполнимое требование, и
    # расчёт кончался «решения нет». Хранится выбранное экономистом —
    # урезается только вход расчёта; экран показывает то же урезанное.
    period = {}
    for c in data.get("contracts") or []:
        code = getattr(c, "code", None) or (c.get("code") if isinstance(c, dict) else None)
        if code:
            period[code] = _contract_months(c, year)
    kept, outside = [], []
    for f in fixes:
        if f.get("договор") not in period:
            # Договор снят с расчёта плана или ушёл из реестра: закрепление
            # на нём выполнить нечем, и расчёт кончался «решения нет»
            # (MANUAL_ASSIGNMENT_NO_PAYMENT, найдено сквозной проверкой
            # 30.09.2026). Хранится выбранное экономистом — вернут договор в
            # расчёт, вернётся и закрепление.
            outside.append(f)
            continue
        span = period[f["договор"]]
        if span is None:
            continue
        a, b = max(int(f["с"]), span[0]), min(int(f["по"]), span[1])
        if a <= b:
            kept.append({**f, "с": a, "по": b})
    fixes = kept
    bans = [b for b in bans if b.get("договор") in period]
    if outside and warn is not None:
        warn.append("Закрепления на договорах вне расчёта плана не учтены: %s."
                    % "; ".join(sorted({"%s — %s" % (f.get("фио") or f.get("сотрудник"),
                                                     f.get("договор")) for f in outside})))
    # Месяцы факта расчёт не пересчитывает (30.09.2026): закрепление в них
    # выполнять нечем — оно урезается до месяцев после «факт по», целиком в
    # факте — в расчёт не идёт. Хранится выбранное экономистом.
    through = int(data.get("fact_through") or 0)
    if through:
        fixes = [{**f, "с": max(int(f["с"]), through + 1)} for f in fixes
                 if int(f["по"]) > through]
        bans = [{**b, "с": max(int(b["с"]), through + 1)} for b in bans
                if int(b["по"]) > through]
    if gone:
        lost = sorted({str(f.get("фио") or f.get("сотрудник")) for f in fixes
                       if f.get("сотрудник") in gone})
        fixes = [f for f in fixes if f.get("сотрудник") not in gone]
        bans = [b for b in bans if b.get("сотрудник") not in gone]
        if lost and warn is not None:
            warn.append("Закрепления сотрудников вне расчёта плана не учтены: %s."
                        % ", ".join(lost))
    # «Вся зарплата с этого договора» (признак «только») — это закрепление
    # плюс запрет платить тому же человеку с прочих договоров плана в те же
    # месяцы. Запреты выводятся здесь, а не хранятся: договор, добавленный
    # в план позже, попадает под тот же запрет, а снятие признака ничего не
    # оставляет за собой.
    codes = [getattr(c, "code", None) or (c.get("code") if isinstance(c, dict) else None)
             for c in (data.get("contracts") or [])]
    for f in fixes:
        if not f.get("только"):
            continue
        for code in codes:
            if not code or code == f["договор"]:
                continue
            bans.append({"сотрудник": f["сотрудник"], "фио": f.get("фио"), "договор": code,
                         "с": f["с"], "по": f["по"], "вид": "любой", "сумма": None})
    data["manual_assignments"] = fixes
    data["manual_prohibitions"] = bans
    return ps


def _case_settings(case):
    """Лист «настройки» из последнего загруженного шаблона этого плана."""
    try:
        return (json.loads(case.passport) if case.passport else {}).get("settings") or {}
    except (TypeError, ValueError):
        return {}


def _retire_document(db, doc, new_version=None):
    """Прежняя версия документа: строки из реестра убрать, файл и запись оставить.

    Планы, посчитанные на этой версии, остаются, но требуют пересмотра
    (VER-003); планы, в основание которых документ не входил, не трогаются.
    """
    actuality.mark_review(db, actuality.runs_using_document(db, doc.id),
                          "документ «%s» заменён версией %s"
                          % (doc.name, new_version or ((doc.version or 1) + 1)))
    # Снимок строк версии — до удаления: план может выбрать эту версию для
    # расчёта (SOURCE-003), а разбор файла заново дал бы другой ответ модели.
    doc.rows_snapshot = basis.snapshot(db, doc)
    for model in (Employee, Contract, LaborRow, Inflow, SecretAllowance, Substitution,
                  ContractValue):
        db.query(model).filter_by(document_id=doc.id).delete()
    db.query(Proposal).filter_by(document_id=doc.id, state="предложено").delete()
    doc.state = "заменен"
    db.commit()


def _forget_document(db, doc):
    """Удалить документ вместе со всем, что из него извлечено.

    Данные помнят документ-источник, поэтому сирот не остается: ушел документ —
    ушли его сотрудники, договоры и правила замещения.
    """
    actuality.mark_review(db, actuality.runs_using_document(db, doc.id),
                          "документ «%s» удалён" % doc.name)
    gone = {
        "сотрудников": db.query(Employee).filter_by(document_id=doc.id).delete(),
        "договоров": db.query(Contract).filter_by(document_id=doc.id).delete(),
        "строк трудоемкости": db.query(LaborRow).filter_by(document_id=doc.id).delete(),
        "поступлений": db.query(Inflow).filter_by(document_id=doc.id).delete(),
        "надбавок 120": db.query(SecretAllowance)
                          .filter_by(document_id=doc.id).delete(),
        "правил замещения": db.query(Substitution).filter_by(document_id=doc.id).delete(),
        "величин пакета": db.query(ContractValue).filter_by(document_id=doc.id).delete(),
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
    # Удалена действующая версия, а у предыдущей есть снимок строк: они
    # возвращаются в реестр. Иначе вместе с ошибочной загрузкой договор
    # терял бы и данные прежней редакции.
    prev = (db.get(Document, doc.supersedes_id)
            if doc.supersedes_id and doc.state != "заменен" else None)
    # Цепочка версий не рвётся: следующая версия ссылается на предыдущую.
    db.query(Document).filter_by(supersedes_id=doc.id).update(
        {"supersedes_id": doc.supersedes_id})
    db.delete(doc)
    if prev is not None and prev.state == "заменен" and basis.restore(db, prev) is not None:
        gone["_версия"] = prev.version or 1
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
        back = gone.pop("_версия", None)
        if case_id:
            names = gone.pop("_величины", "")
            lost = ", ".join("%s %d" % (k, v) for k, v in gone.items()
                             if v and not str(k).startswith("_"))
            if names:
                lost += " (%s — теперь без основания, величины остались)" % names
            agents.say(db, case_id,
                       "Удален документ «%s»%s%s." % (
                           name, ", с ним " + lost if lost else "",
                           "; в реестре снова версия %s" % back if back else ""),
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


def _start_run(db, case_id, settings, operation_id=None, retry_of=None):
    """Единственный вход для нового прогона. Возвращает (прогон, создан ли).

    Прогон заводился в четырёх местах: кнопка, претензия в чате, закрепление
    и «посчитай». Каждое по-своему проверяло занятость, и ни одно не
    записывало, кто считает. Теперь запуск один: занятый план возвращает
    идущий прогон; ключ операции от клиента (RUN-001) делает повтор той же
    команды безопасным — вернётся тот же прогон, — а тот же ключ с другими
    данными отклоняется как конфликт, потому что молча заменить данные
    операции хуже, чем отказать.
    """
    from sqlalchemy import text
    payload = json.dumps(settings or {}, ensure_ascii=False, sort_keys=True)
    try:
        # SQLite сериализует писателей между процессами. Получаем право записи
        # ДО чтения operation_id/занятости; отдельный Python-lock не защитил бы
        # второй сервер. UPDATE не меняет предметных данных и не требует миграции.
        # Граница транзакции здесь уже была (commit при создании run); теперь
        # её закрывают и ветви повторного обращения, чтобы не держать блокировку.
        reserved = db.execute(text("UPDATE cases SET id = id WHERE id = :case_id"),
                              {"case_id": case_id})
        if reserved.rowcount != 1:
            raise HTTPException(404, "план не найден")
        if operation_id:
            same = db.query(Run).populate_existing().filter_by(operation_id=operation_id).first()
            if same is not None:
                same_payload = json.dumps(json.loads(same.settings or "{}"),
                                          ensure_ascii=False, sort_keys=True)
                if same.case_id != case_id or same_payload != payload or same.retry_of != retry_of:
                    raise HTTPException(409, "операция %s уже выполнена с другими данными"
                                        % operation_id)
                db.commit()
                return same, False
        busy = _running_run(db, case_id)
        if busy is not None:
            db.commit()
            return busy, False
        run = Run(case_id=case_id, settings=payload, executor=EXECUTOR,
                  heartbeat=now(), operation_id=operation_id or None,
                  retry_of=retry_of)
        db.add(run)
        db.commit()
        return run, True
    except Exception:
        db.rollback()
        raise


#: Сколько секунд операция с ключом считается идущей без итога. Дольше —
#: процесс упал до записи, и тот же ключ можно занять заново.
OPERATION_STALE_SEC = 600


def _operation(db, op_id, kind, subject, payload, fn):
    """Команда с ключом операции (RUN-001).

    Повтор той же команды возвращает сохранённый ответ, а не выполняет её
    второй раз; тот же ключ с другими данными — конфликт; команда, упавшая
    на середине, под тем же ключом вслепую не повторяется — нужен новый
    ключ, то есть явное решение. Без ключа команда выполняется как раньше.
    """
    if not op_id:
        return fn()
    state, saved = claim_operation(db, op_id, kind, subject, payload,
                                   stale_sec=OPERATION_STALE_SEC)
    if state == "готово":
        if isinstance(saved, dict) and saved.get("_http"):
            raise HTTPException(int(saved["_http"]), saved.get("error") or "")
        return saved
    if state == "сбой":
        raise HTTPException(409, "операция %s завершилась ошибкой (%s); повторите "
                            "команду с новым ключом" % (op_id, (saved or {}).get("error", "")[:160]))
    if state == "конфликт":
        raise HTTPException(409, "операция %s уже выполнена с другими данными" % op_id)
    if state == "идёт":
        raise HTTPException(409, "операция %s ещё выполняется" % op_id)
    try:
        result = fn()
    except HTTPException as exc:
        # Отказ — тоже итог: повтор получит тот же отказ, а не второе применение.
        finish_operation(db, op_id, {"_http": exc.status_code, "error": str(exc.detail)})
        raise
    except Exception as exc:  # noqa: BLE001 — итога нет, и это записывается
        finish_operation(db, op_id, {"error": "%s: %s" % (type(exc).__name__, str(exc)[:300])},
                         state="сбой")
        raise
    finish_operation(db, op_id, result)
    return result


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
    # Ключ операции от клиента (RUN-001): оборвавшийся запрос повторяется
    # тем же ключом и получает тот же ответ, а не вторую реплику в ленте.
    op = str(body.get("operation_id") or "") or None
    db = session()
    try:
        return _operation(db, op, "реплика", "план %d" % case_id, {"text": text},
                          lambda: _message_do(db, case_id, background, text))
    finally:
        db.close()


def _offline_reply(db, case, error):
    """Ответ на реплику, когда модель недоступна: последний расчёт и причина."""
    last = db.query(Run).filter_by(case_id=case.id).order_by(Run.id.desc()).first()
    state = ""
    if last is not None and last.status == "нет решения":
        try:
            why = (json.loads(last.summary) if last.summary else {}).get("анализ") or []
        except ValueError:
            why = []
        state = "Расчёт № %d — решения нет. %s" % (last.id, " ".join(why))
    elif last is not None and last.status == "OPTIMAL":
        state = "Расчёт № %d — план есть%s." % (
            last.id, ", требует пересмотра" if last.review == actuality.REVIEW else "")
    elif last is not None:
        state = "Расчёт № %d — %s." % (last.id, last.status)
    return ((state.strip() + "\n\n" if state else "") +
            "Модель недоступна (%s): на вопросы своими словами не отвечу. Без неё работают "
            "команды правок и разбор документов." % (error or "причина не названа"))


def _message_do(db, case_id, background, text):
    """Тело реплики: лента, ответ модели, действия сервиса по её ответу."""
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
            if changed or run or not answer.startswith("Не понял правку"):
                agents.say(db, case_id, answer, agent="intake")
                db.commit()
                return {"ok": True, "solve": bool(run)}
        # Вопрос, а не команда: разбор команд на него отвечал «Не понял
        # правку» (02.10.2026, на «решения нет?»). Без модели отвечаем тем,
        # что знаем без неё, — состоянием последнего расчёта — и честно
        # называем, почему не ответить своими словами.
        agents.say(db, case_id, _offline_reply(db, case, res.get("error")), agent="intake")
        db.commit()
        return {"ok": True}

    agents.say(db, case_id, res["reply"], agent="intake")
    db.commit()

    action = res.get("action")
    # Закрепления, запреты, настройки и претензии — переменные решателя,
    # заданные рукой экономиста. Пишем в план и пересчитываем, если план уже
    # считался; без месяцев — спрашиваем и ничего не пишем. Претензия
    # берётся из ответа модели независимо от выбранного ею действия (слабая
    # модель называет цели, а действие оставляет «ничего»), а если модель
    # целей не назвала — из самой реплики кодом (chat.parse_claims).
    claims = res.get("claims") or chat.parse_claims(text)
    if res.get("fixes") or res.get("settings") or chat.parse_settings(text) or claims:
        was = case.plan_settings
        last = (db.query(Run).filter_by(case_id=case_id, status="OPTIMAL")
                .order_by(Run.id.desc()).first())
        base = json.loads(last.settings) if last and last.settings else {}
        lines, questions = chat.apply_fixes(db, case, res.get("fixes"), text)
        lines += chat.apply_settings(db, case, res.get("settings"), text)
        if claims:
            lines += chat.apply_claims(db, case, claims, text, base=base)[0]
        db.commit()
        if lines:
            agents.say(db, case_id, "\n".join(lines), agent="tuning")
        if questions:
            agents.say(db, case_id, "\n".join(questions), agent="tuning",
                       payload={"kind": "ask_months"})
            db.commit()
            return {"ok": True, "action": "вопрос"}
        # Пересчёт стоит десять минут — он идёт, только если условие плана
        # действительно изменилось. «Не нашёл сотрудника» и претензия, не
        # сдвинувшая ни одного веса, расчёта не стоят.
        db.refresh(case)
        if not lines or case.plan_settings == was:
            db.commit()
            return {"ok": True, "action": "ничего"}
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
        base = {**base, **(chat.plan_settings(case).get("веса") or {})}
        run, created = _start_run(db, case_id, base)
        if not created:
            agents.say(db, case_id, "Условие записано. Расчёт № %d уже идёт." % run.id,
                       agent="tuning")
            db.commit()
            return {"ok": True, "action": "закреплено", "run_id": run.id}
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
        _set_muted(db, case, doc.id, action == "исключить документ",
                   grounds="реплика: %s" % (text or "")[:200])
        return {"ok": True, "action": "флажок", "document": doc.name}

    if action == "претензия к плану":
        # Претензия с названной целью обработана выше вместе с условиями
        # плана; сюда доходит только та, в которой цели не нашли ни модель,
        # ни разбор реплики.
        agents.say(db, case_id, "Претензию понял, но в цель решателя "
                   "перевести не смог — назовите, что именно в плане не так: "
                   "переводы, связки с договорами, освоение по месяцам или "
                   "суммы по месяцам.", agent="tuning")
        db.commit()
        return {"ok": True, "action": "ничего"}

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
        run, created = _start_run(db, case_id, {})
        if not created:
            agents.say(db, case_id, "Расчет № %d уже идет." % run.id, agent="tuning")
            db.commit()
            return {"ok": True, "action": "ничего", "run_id": run.id}
        background.add_task(_solve, case_id, run.id, None)
        return {"ok": True, "action": "расчет", "run_id": run.id}

    return {"ok": True, "action": action}


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
        manual = basis.manual_columns(db)
        return {
            # Поля отдаются все, какие есть в контракте входного файла: то, что
            # уходит в расчет, экономист должен видеть и проверить.
            "employees": [{"code": e.code, "person_code": e.person_code,
                            "fio": e.fio, "position": e.position,
                           "department": e.department, "rate": e.rate,
                           "employment": e.employment_type,
                           "category": e.employment_category,
                           "salary": e.salary,
                           "allowed": e.allowed_contracts,
                           "forbidden": e.forbidden_contracts,
                           "account": e.account,
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
                           # Графы, введённые руками поверх документа (SOURCE-007).
                           "manual": sorted(manual.get(c.code, [])),
                           "from": c.date_from, "to": c.date_to} for c in ctrs],
            # Итог ФОТ по реестру считает сервер, а не страница (UI-017, UI-002).
            "contracts_fund": round(sum(float(c.fund) for c in ctrs
                                        if c.fund not in (None, "")), 2),
            "labor": [{"id": r.id, "contract": r.contract_code, "year": r.year,
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
            # id — чтобы строку можно было править в ячейке и удалить.
            "secret": [_secret_row(r)
                       for r in db.query(SecretAllowance)
                       .order_by(SecretAllowance.id).all()],
            # Нормативная база общая: правила и справочник не привязаны к делу.
            # id — чтобы строку можно было изменить и удалить (SOURCE-010).
            "substitutions": [{"id": s.id, "position": s.position, "replaced_by": s.replaced_by,
                               "source": s.source} for s in
                              db.query(Substitution).order_by(Substitution.id).all()],
            # Рабочие группы договоров (28.09.2026): строка — член группы,
            # приказ при нём. Договор и строка штатного расписания могут быть
            # пусты — их выбирает экономист.
            "workgroups": _workgroup_rows(db),
            # Приказы о рабочих группах — строкой на приказ (вкладка реестра
            # «Приказы», 29.09.2026): что загружено и что осталось разобрать.
            "workorders": _workorders(db),
            "reference": reference.read_rows(),
            # Откуда каждая величина справочника: документ, дата, основание.
            "reference_sources": reference.read_sources(),
            # Величины с датами (SOURCE-012): экран показывает справочник на
            # дату и историю должности.
            "reference_history": reference.read_history(),
            # Управленческие подразделения (01.10.2026).
            "mgmt_departments": _mgmt_list(db),
            "salary": _salary_table(db, emps, _salary_year(db, case_id)),
            # Факт начислений года плана (30.09.2026): вкладка реестра и
            # вкладка плана — строками и формой 6.3 «ШР на дату, детализация
            # назначений», как раздел 6.3 отчёта и шаблон кадров.
            "facts": facts.registry_rows(db, _salary_year(db, case_id)),
            "facts_detail": facts.detail_rows(db, _salary_year(db, case_id)),
        }
    finally:
        db.close()


def _salary_year(db, case_id):
    """Год вкладки «Заработная плата по месяцам» — год открытого плана."""
    case = db.get(Case, case_id)
    return case.year if case is not None and case.year else now().year


def _salary_table(db, emps, year):
    """Таблица «Заработная плата по месяцам» года: строки, которые принёс
    документ или добавил экономист. В месяце — сумма из таблицы; зарплаты в
    штатном расписании нет (решение пользователя 19.09.2026), поэтому месяц
    без суммы пуст. active — месяцы срока строки: вне их сумма не нужна.
    Итог по месяцам считает сервер, а не страница (UI-017, UI-002).
    candidates — кого ещё можно добавить: строки штатного расписания без
    строки зарплаты."""
    by_code = {e.code: e for e in emps}
    # Строка — строка таблицы зарплаты: у человека в двух управленческих
    # подразделениях их две, у каждой свои суммы (01.10.2026).
    own = {(r.salary_row_id, r.month): r.amount
           for r in db.query(SalaryMonth).filter_by(year=year).all()}
    depts = {d.id: d.name for d in db.query(MgmtDepartment).all()}
    listed = (db.query(SalaryRow).filter_by(year=year)
              .order_by(SalaryRow.employee_code, SalaryRow.id).all())
    # Итог месяца без единой суммы пуст, а не 0: «0» читался бы как зарплата.
    rows, totals, seen = [], [None] * 12, set()
    for r in listed:
        e = by_code.get(r.employee_code)
        if e is None:
            continue
        seen.add(r.employee_code)
        months = [own.get((r.id, m)) for m in range(1, 13)]
        for m, value in enumerate(months):
            if value is not None:
                totals[m] = (totals[m] or 0.0) + value
        rows.append({"id": r.id, "code": e.code, "fio": e.fio, "months": months,
                     "active": build_input.active_months(e.date_from, e.date_to, year),
                     "source": r.source, "dept_id": r.mgmt_department_id,
                     "dept": depts.get(r.mgmt_department_id)})
    candidates = [{"code": e.code, "fio": e.fio} for e in emps if e.code not in seen]
    return {"year": year, "rows": rows,
            "totals": [None if t is None else round(t, 2) for t in totals],
            "candidates": candidates}


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
    """План ФОТ на год в формах экономистов: пятнадцать разделов числами."""
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


@app.get("/api/decisions")
def list_decisions(case_id: int | None = None, document_id: int | None = None, limit: int = 100):
    """Решения по плану или документу, новые сверху."""
    db = session()
    try:
        q = db.query(Decision)
        if document_id is not None:
            q = q.filter(Decision.subject_kind == "документ", Decision.subject_id == str(document_id))
        elif case_id is not None:
            doc_ids = [str(d.id) for d in db.query(Document).filter_by(case_id=case_id).all()]
            q = q.filter(((Decision.subject_kind == "план") & (Decision.subject_id == str(case_id)))
                         | ((Decision.subject_kind == "документ") & Decision.subject_id.in_(doc_ids or ["-"])))
        rows = q.order_by(Decision.id.desc()).limit(max(1, min(limit, 500))).all()
        return [{"id": r.id, "created": _dt(r.created), "actor": r.actor, "actor_source": r.actor_source,
                 "kind": r.kind, "subject_kind": r.subject_kind, "subject_id": r.subject_id,
                 "subject_digest": r.subject_digest, "scope": r.scope,
                 "action": json.loads(r.action) if r.action else None, "grounds": r.grounds,
                 "precondition": r.precondition, "outcome": r.outcome} for r in rows]
    finally:
        db.close()


@app.get("/api/case/{case_id}/run/{run_id}/manifest")
def run_manifest(case_id: int, run_id: int):
    """Снимок прогона и его сверка с хранилищем: на чём считали и цело ли оно."""
    from fot_planner.harness_local import manifest as mf
    db = session()
    try:
        run = db.get(Run, run_id)
        if run is None or run.case_id != case_id:
            raise HTTPException(404, "прогон не найден")
        if not run.manifest_sha256:
            raise HTTPException(404, "у прогона нет снимка: посчитан до появления снимков")
        store = _artifacts()
        manifest = mf.load_manifest(store, run.manifest_sha256)
        return {"sha256": run.manifest_sha256, "manifest": manifest,
                "problems": mf.verify_manifest(store, manifest)}
    finally:
        db.close()


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


def _reference_change(applied):
    """Одной строкой: какие величины справочника переписаны."""
    items = sorted({"%s: %s" % (a.get("pos"), a.get("field")) for a in applied})
    more = " и ещё %d" % (len(items) - 4) if len(items) > 4 else ""
    return "справочник изменён (%s%s)" % ("; ".join(items[:4]), more)


@app.post("/api/reference")
async def post_reference(request: Request):
    body = await request.json()
    # Дата, с которой величины действуют (01.10.2026): из карточки
    # расхождений документа; без неё — величина на все даты, как раньше.
    applied = reference.apply(body.get("edits") or [],
                              effective=body.get("effective_from") or None)
    case_id = body.get("case_id")
    if applied:
        db = session()
        try:
            actuality.mark_review(db, actuality.runs_pinned_reference(db),
                                  _reference_change(applied))
            db.commit()
        finally:
            db.close()
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
    expected = str(body.get("document_sha256") or "") or None
    db = session()
    try:
        d = db.get(Document, doc_id)
        if d is None:
            raise HTTPException(404, "документ не найден")
        return _write_document_reference(db, d, edits, expected)
    finally:
        db.close()


def _write_document_reference(db, d, edits, expected=None, grounds="карточка документа"):
    """Записать в справочник правки из документа ``d``: величины, источник,
    журнал решений. Общая для карточки документа и начальной загрузки из
    папки (SOURCE-015), где подтверждать некому."""
    from fot_planner.harness_local import decisions as D
    if D.stale(expected, d.sha256):
        record_decision(db, kind="норма", subject_kind="документ", subject_id=d.id,
                        subject_digest=D.digest(d.sha256, edits), scope="организация",
                        action={"правок": len(edits)}, grounds=grounds,
                        precondition=expected, outcome="отклонено: документ изменился после открытия формы")
        db.commit()
        raise HTTPException(409, "документ изменился после открытия формы — откройте карточку заново")
    try:
        told = json.loads(d.gave) if d.gave else {}
    except ValueError:
        told = {}
    # Величины документа действуют с его даты (01.10.2026): прежние
    # остаются в справочнике до неё — план прошлых месяцев не меняется.
    applied = reference.apply(edits, effective=told.get("действует с") or None, doc=d)
    if applied:
        actuality.mark_review(db, actuality.runs_pinned_reference(db),
                              _reference_change(applied))
    if edits:
        # Норма меняется для всей организации: справочник один.
        record_decision(db, kind="норма", subject_kind="документ", subject_id=d.id,
                        subject_digest=D.digest(d.sha256, edits), scope="организация",
                        action={"правок": len(applied), "поля": sorted({a["field"] for a in applied})},
                        grounds=grounds, precondition=expected)
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
def _conflict_reason(descr, no_plan):
    """Строка «Конфликт» решателя — словами для ленты, или None.

    Обычно названо ограничение в скобках, и при «ни одна строка трудоёмкости
    не закрыта» оно — следствие, его не пересказываем. Закрепление экономиста,
    которое не выполнить (MANUAL-ANY-001: договор в эти месяцы не может ему
    платить), — причина, а не следствие: её называем всегда, целиком."""
    import re
    text = str(descr or "")
    m = re.search(r"\(([^)]+)\)", text)
    if not m:
        return None
    if "акреплен" in text or "акреплён" in text:
        return "%s: %s." % (text.split("(")[0].strip().rstrip("."), m.group(1))
    if no_plan:
        return None
    return "Решатель назвал ограничение, которое не сошлось: %s." % m.group(1)


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
            "по месяцам, сроках договоров, числе людей по строкам РКМ или закреплениях "
            "плана.")

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
            reason = _conflict_reason(descr, no_plan)
            if reason:
                found["почему"].append(reason)
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
                               "сборки входа в ленте и закрепления плана.")
    # Сначала — закрепления экономиста: это его решения, и снять их дешевле,
    # чем допускать дефицит. Общая фраза «дело в деньгах или сроках» при
    # найденном закреплении вводит в заблуждение (02.10.2026: «вся зарплата»
    # Зайцева с C_BASE-26 не давала закрыть строки РКМ C_GOZ-26, а разбор
    # говорил о деньгах) — тогда она уходит, остаётся названное закрепление.
    pin = _try_without_pins(db, case, run)
    if pin:
        found["почему"] = [x for x in found["почему"]
                           if not x.startswith("Ни одного допустимого плана")]
        found["почему"].insert(0, "Мешает закрепление плана: расчёт сходится, если %s." % pin)
        found["опыты"] = [(pin, "сходится", "")]
        found["текст"] = "Решения нет. " + " ".join(found["почему"])
        return found
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
    """Реестр организации полон для расчета: люди и договоры. Поступления в
    расчёт не подаются (ПРАВИЛА_РЕШАТЕЛЯ, правило 21) и не требуются."""
    return bool(db.query(Employee).count() and db.query(Contract).count())


#: Сколько опытов над закреплениями плана — не больше: каждый — прогон решателя.
PIN_TRIALS = 6


def _pin_trials(db, ps):
    """Опыты над закреплениями экономиста: [(подпись, изменённые переменные)].

    У закрепления «вся зарплата» сначала пробуем снять только этот признак —
    это мягче и чаще всего и есть выход, — потом закрепление целиком. Члена
    рабочей группы снять нельзя (состав меняет приказ), пробуем только его
    «вся зарплата»."""
    names = {e.code: e.fio for e in db.query(Employee).all()}
    pins = ps.get("назначения") or []

    def key(f):
        return f.get("сотрудник"), f.get("договор"), f.get("с"), f.get("по")

    out, seen = [], []
    for f0 in pins:
        k = key(f0)
        if k in seen:
            continue
        seen.append(k)
        rest = [f for f in pins if key(f) != k]
        group = [f for f in pins if key(f) == k]
        who = "%s на %s, %s" % (f0.get("фио") or names.get(f0.get("сотрудник")) or f0.get("сотрудник"),
                                f0.get("договор"),
                                facts.months_text(range(int(f0["с"]), int(f0["по"]) + 1)))
        if any(f.get("только") for f in group):
            out.append(("у закрепления %s снять «вся зарплата»" % who,
                        {**ps, "назначения": rest + [{**f, "только": False} for f in group]}))
        out.append(("снять закрепление %s" % who, {**ps, "назначения": rest}))
    groups = ps.get("рабочие группы") or {}
    for gk, choice in groups.items():
        if choice.get("только") and "|" in gk:
            code, emp = gk.split("|", 1)
            out.append(("у члена рабочей группы %s на %s снять «вся зарплата»"
                        % (names.get(emp) or emp, code),
                        {**ps, "рабочие группы": {**groups, gk: {**choice, "только": False}}}))
    return out


def _try_without_pins(db, case, run):
    """Мешает ли закрепление экономиста: подпись выхода или None.

    Сначала один прогон без всех закреплений и признаков «вся зарплата»:
    не сходится и так — дело не в них, остальные опыты не нужны. Сходится —
    по одному, пока не найдётся то, без которого план есть. Повтор прогона
    из снимка не разбирается: его вход не из нынешнего реестра."""
    if getattr(run, "retry_of", None):
        return None
    ps = chat.plan_settings(case)
    trials = _pin_trials(db, ps)
    if not trials:
        return None
    bare = {**ps, "назначения": [],
            "рабочие группы": {k: {**v, "только": False}
                               for k, v in (ps.get("рабочие группы") or {}).items()}}
    if not _solves_with(db, case, run, bare, "pins0"):
        return None
    for i, (name, variant) in enumerate(trials[:PIN_TRIALS], 1):
        if _solves_with(db, case, run, variant, "pins%d" % i):
            return name
    return "снять закрепления плана"


def _solves_with(db, case, run, ps, tag):
    """Сходится ли расчёт на нынешнем реестре с переменными плана ps."""
    try:
        warn = []
        data = _registry_data(db, case, warn)
        settings = json.loads(run.settings) if run.settings else {}
        if settings:
            data["settings"] = {**(data.get("settings") or {}),
                                **{k: v for k, v in settings.items() if k != "претензии"}}
        _apply_plan_settings(data, case, warn, ps=ps)
        tmp = os.path.join(RESULT_DIR, "case%d_run%d_%s.xlsx" % (case.id, run.id, tag))
        build_input.build(reference.TEMPLATE, tmp, data, warn)
        res = tmp.replace(".xlsx", "_out.xlsx")
        p = subprocess.run([EXE, "solve", "-i", tmp, "-o", res],
                           capture_output=True, text=True, timeout=180, cwd=ROOT)
        return not _no_solution(p.returncode, _read_solver_output(p.stdout or "")["solver_status"])
    except Exception:  # noqa: BLE001 — опыт не должен ронять разбор
        return False


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
            if _no_solution(p.returncode, _read_solver_output(p.stdout or "")["solver_status"]):
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
    """Агент проверки: сверить готовый план по всему пакету правил.

    Числовые проверки остаются детерминированными. Модель получает их полный
    структурированный результат и может найти противоречия между правилами,
    но не может изменить план или придумать отсутствующие в пакете числа.
    """
    try:
        rows = _solver_warnings(out)
    except Exception as exc:  # noqa: BLE001 — проверка не должна ронять расчет
        agents.say(db, case_id, "Предупреждения решателя прочитать не удалось: %s" % str(exc)[:120],
                   agent="checker")
        db.commit()
        return
    lines = []
    for r in rows:
        where = " · ".join(str(x) for x in (r["объект"], r["месяц"]) if x not in (None, "", "—"))
        amount = (" — %s ₽" % _money(r["сумма"])) if isinstance(r["сумма"], (int, float)) else ""
        lines.append("%s, %s: %s%s%s" % (r["уровень"], r["раздел"], r["что"],
                                        (" (%s)" % where) if where else "", amount))
    text = ("Предупреждения решателя (%d):\n" % len(rows) +
            ("\n".join("• " + x for x in lines[:12]) if lines else "нет") )
    if len(lines) > 12:
        text += "\n• … и еще %d" % (len(lines) - 12)
    # Модель видит не только предупреждения решателя, но и независимую
    # сверку входного файла с готовым планом. В строки передаём ограниченный
    # объём: все состояния правил, факт и несколько самых узких мест.
    evidence = []
    try:
        run = db.get(Run, run_id)
        src = run.input_path if run else None
        if src and os.path.exists(src):
            import rules as rules_mod
            for rule in rules_mod.check(src, out):
                evidence.append({
                    "правило": rule.get("правило"),
                    "тип": rule.get("тип"),
                    "состояние": rule.get("состояние"),
                    "проверено": rule.get("проверено"),
                    "нарушений": rule.get("нарушений"),
                    "факт": rule.get("факт"),
                    "предел": rule.get("предел_общий"),
                    "строки": (rule.get("строки") or [])[:4],
                })
    except Exception as exc:  # noqa: BLE001 — числовая проверка не роняет расчет
        evidence.append({"ошибка_пакета_проверок": str(exc)[:200]})
    packet = json.dumps({"предупреждения": lines, "правила": evidence},
                        ensure_ascii=False, default=str)
    name, model, _why = llm.provider()
    if name and name != "anthropic":
        try:
            raw = llm._call_openai_compatible(
                "Пакет проверки готового плана ФОТ (JSON):\n" + packet,
                "проверка", model, llm._endpoint(name), api_key=llm._key(name),
                schema=llm._use_schema(name), no_thinking=llm._no_thinking(name),
                insecure=llm._truthy(os.environ.get("FOT_LLM_INSECURE_TLS")),
                 system="Ты — независимый агент проверки результата в сервисе планирования ФОТ. "
                        "Сверь все правила из JSON между собой, ищи пропуски, противоречия "
                        "и подозрительные числа вроде ставки 0,86. Используй только числа и "
                        "факты из JSON, ничего не выдумывай. Экономисту дай 4–6 коротких фраз: "
                        "сначала нарушения, затем самые важные места для проверки. Если всё "
                        "согласовано, так и скажи.",
                shape='Ответь одним объектом JSON: {"reply": "текст для экономиста"}',
                json_schema={"type": "object", "properties": {"reply": {"type": "string"}},
                             "required": ["reply"], "additionalProperties": False},
                schema_name="review_reply", max_tokens=600)
            if raw and raw.get("reply"):
                text = raw["reply"].strip() + "\n\n" + text
        except Exception:  # noqa: BLE001 — без модели остаётся список
            pass
    agents.say(db, case_id, text, agent="checker",
               payload={"kind": "review", "run_id": run_id,
                        "строк": len(rows), "правил": len(evidence)})
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


def _run_sources(db, data=None):
    """Что легло в основание расчета: документы и версии агентов.

    Документ попадает сюда, если хоть одна строка реестра ссылается на него.
    У каждого — имя, версия и что он дал реестру. Отпечаток содержимого тоже
    сохраняется в записи прогона, но нигде не показывается: экономисту он
    ничего не говорит, а служебную сверку сервис делает сам при загрузке.

    С данными сборки основание считается по строкам, ушедшим во вход: план
    мог снять договор или выбрать прежнюю версию документа (SOURCE-003).
    """
    if data is not None and data.get("row_sources") is not None:
        rows = data["row_sources"]
    else:
        rows = {}
        for model, what in ((Employee, "сотрудников"), (Contract, "договоров"),
                            (LaborRow, "строк трудоемкости"), (Inflow, "поступлений"),
                            (SecretAllowance, "надбавок 120"),
                            (Substitution, "правил замещения"),
                            (ContractValue, "величин пакета")):
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


def _pinned_input(db, run_id):
    """Закреплённый вход прежней попытки: байты из хранилища по хешу снимка.

    Повтор чистого вычисления (RUN-002) считает ровно то, что считала
    неудавшаяся попытка, — не то, что лежит в реестре сейчас. Возвращает
    (байты, снимок источников); если снимка или артефакта нет либо байты
    не сходятся с хешем — None: слепой повтор запрещён.
    """
    from fot_planner.harness_local import manifest as mf
    old = db.get(Run, run_id)
    if old is None or not old.manifest_sha256:
        return None
    store = _artifacts()
    try:
        manifest = mf.load_manifest(store, old.manifest_sha256)
    except Exception:  # noqa: BLE001 — снимка нет или он повреждён
        return None
    digest = (manifest.get("input") or {}).get("sha256")
    if not digest or not store.has(digest) or not store.verify(digest):
        return None
    sources = manifest.get("sources") or (json.loads(old.sources) if old.sources else {})
    return store.read(digest), sources


def _audit_requirements():
    """Обязательные проверки результата — из профиля каталога (CAP-004).

    Их номера уходят решателю флагом --require: обязательная, но не
    реализованная в аудите проверка даёт not_run и FAIL, а не «неприменимо».
    Каталог закреплён на процесс; без него сервис не стартует.
    """
    from fot_planner.harness_local.catalogue import default_catalogue
    return [c["requirement"] for c in default_catalogue().required_checks("fot.local")]


def _solver_command(src, out, limit):
    command = [EXE, "solve", "-i", src, "-o", out, "--time-limit", str(limit)]
    command += ['--audit-bundle', out + '.audit.json']
    required = _audit_requirements()
    if required:
        command += ["--require", ",".join(required)]
    return command


def _no_solution(returncode, solver_status):
    """Решения нет — только если решатель плана не нашёл. CLI отдаёт код 1 и
    при найденном плане, если есть предупреждения (коды *_WARNING — в том
    числе о факте начислений, 30.09.2026): такой план посчитан, а
    предупреждения лежат на листе «Проблемы и предупреждения» результата.
    Раньше любой код, кроме 0, читался как «решения нет»."""
    return returncode != 0 and solver_status not in ("OPTIMAL", "FEASIBLE")


def _read_solver_output(stdout):
    """Разобрать вывод решателя: статус модели, итог аудита, нарушения и
    состояние каждой проверки — passed / failed / not_applicable с основанием /
    not_run для обязательной, но не реализованной (CAP-004)."""
    from fot_planner.result_audit import parse_check_line
    out = {"solver_status": "", "audit_status": "", "audit_lines": [], "checks": []}
    for line in stdout.splitlines():
        if line.startswith("Статус:"):
            out["solver_status"] = line.split(":", 1)[1].strip()
        elif line.startswith("Аудит:"):
            out["audit_status"] = line.split(":", 1)[1].strip()
        elif out["audit_status"] and line.startswith("  ["):
            out["audit_lines"].append(line.strip())
        else:
            check = parse_check_line(line)
            if check:
                out["checks"].append(check)
    return out


def _solve(case_id: int, run_id: int, settings: dict | None, pinned_from: int | None = None):
    db = session()
    stop = threading.Event()
    threading.Thread(target=_heartbeat, args=(run_id, stop), daemon=True).start()
    draft, draft_digest, out, audit_status, audit_lines, sec = None, None, None, "", [], None
    audit_checks, solver_status = [], ""
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
            pinned = _pinned_input(db, pinned_from) if pinned_from else None
            if pinned_from and pinned is None:
                raise RuntimeError("вход прогона № %d не восстановить из снимка — "
                                   "слепой повтор запрещён" % pinned_from)
            if pinned is not None:
                # Повтор: вход берётся из снимка прежней попытки, реестр не
                # читается — иначе это был бы новый расчёт, а не повтор.
                with open(src, "wb") as f:
                    f.write(pinned[0])
                sources = pinned[1]
                w["detail"] = "вход взят из снимка прогона № %d" % pinned_from
            else:
                data = _registry_data(db, case, warn)
                if settings:
                    # Настройки прогона поверх настроек из шаблона: сюда попадают
                    # веса целей, сдвинутые претензиями экономиста.
                    data["settings"] = {**(data.get("settings") or {}),
                                        **{k: v for k, v in settings.items()
                                           if k != "претензии"}}
                _apply_plan_settings(data, case, warn)
                build_input.build(reference.TEMPLATE, src, data, warn)
                sources = _run_sources(db, data)
                # В строке работы — коротко; сами допущения уходят в ленту
                # сообщениями и лежат в артефакте, а не растягивают колонку.
                w["detail"] = "вход собран" + (", допущений %d" % len(warn) if warn else "")
            run.input_path = src
            run.sources = json.dumps(sources, ensure_ascii=False)
            # Снимок прежней попытки мог быть записан без части разделов —
            # артефакт шага показывает, что есть, а не падает.
            w["artifact"] = {"документов в основании": len(sources.get("документы") or []),
                             "версии агентов": sources.get("агенты"),
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
            command = _solver_command(src, out, limit)
            draft = _manifest_draft(run, src, settings, sources, command, limit,
                                    attempt_of=pinned_from)
            draft_digest = run.manifest_sha256
            db.commit()
            p = subprocess.run(command, capture_output=True, text=True, timeout=3600, cwd=ROOT)
            sec = round(time.time() - t0, 1)
            w["detail"] = "%s c" % sec
            db.commit()

        # Пока решатель считал, прогон мог закрыть другой процесс — как
        # брошенный (heartbeat замолчал) или рукой. Старый исполнитель
        # результат не записывает (RUN-002): в базе уже другое состояние,
        # и молча заменить его своим — значит, два исполнителя пишут один
        # прогон.
        db.expire_all()
        run = db.get(Run, run_id)
        if run is None or run.status != "идет" or (run.executor or EXECUTOR) != EXECUTOR:
            agents.say(db, case_id, "Результат расчёта № %d отброшен: прогон уже закрыт "
                       "как «%s», считал прежний исполнитель. Запустите расчёт заново."
                       % (run_id, run.status if run else "удалён"), agent="solver")
            db.commit()
            return

        run.seconds = sec
        parsed = _read_solver_output(p.stdout or "")
        solver_status, audit_status = parsed["solver_status"], parsed["audit_status"]
        audit_lines, audit_checks = parsed["audit_lines"], parsed["checks"]
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
            run.summary = json.dumps({"аудит": audit_lines, "проверки": audit_checks}, ensure_ascii=False)
            db.commit()
            agents.say(db, case_id, "\n".join(
                ["План посчитан, но не прошёл проверку результата:"]
                + audit_lines[:10]
                + ["Это ошибка расчёта, а не данных: такие числа в приказ не "
                   "попадут. План не считается готовым."]), agent="solver")
            db.commit()
            return
        if _no_solution(p.returncode, solver_status):
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
        from audit_runner import run_audit
        independent = run_audit(_artifacts(), draft, out + '.audit.json', out)
        if independent.get('verdict') != 'passed':
            run.status = case.stage = 'не прошёл проверку'
            run.summary = json.dumps({'независимый аудит': independent}, ensure_ascii=False)
            _manifest_final(run, draft, out, solver_status, audit_status, audit_lines, sec,
                             audit_checks=audit_checks, independent_audit=independent)
            db.commit()
            agents.say(db, case_id, 'Независимая проверка результата не пройдена. План не готов.', agent='solver')
            db.commit()
            return
        # Финальный снимок ссылается на те же неизменяемые байты, что проверил worker.
        audited_out = _artifacts().path(independent['output_sha256'])
        _manifest_final(run, draft, audited_out, solver_status, audit_status, audit_lines, sec,
                         audit_checks=audit_checks, independent_audit=independent)
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
        # Состояние каждой проверки результата: что применялось, что нет и
        # почему; обязательная без реализации сюда не дойдёт — план упал бы
        # раньше как не прошедший проверку (CAP-004).
        summary["проверки"] = audit_checks
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
        stop.set()
        try:
            _manifest_close(db, run_id, draft, draft_digest, out, audit_status, audit_lines, sec,
                             audit_checks=audit_checks, solver_status=solver_status)
        except Exception:  # noqa: BLE001 — снимок не должен добить и без того упавший прогон
            pass
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
        run, created = _start_run(db, case_id, settings,
                                  operation_id=str(body.get("operation_id") or "") or None)
        if not created:
            return {"ok": True, "run_id": run.id, "идет": run.status == "идет"}
        agents.say(db, case_id, "Запускаю расчет.", who="экономист")
        background.add_task(_solve, case_id, run.id, settings)
        return {"ok": True, "run_id": run.id}
    finally:
        db.close()


#: Что можно повторить как чистое вычисление: обрыв, «не успел» и падение.
#: «Нет решения» и «не прошёл проверку» — ответы решателя на тот же вход,
#: повтор даст то же самое; их место — разбор причин, а не повтор.
RETRYABLE = ("прерван", "не успел", "ошибка")


@app.post("/api/case/{case_id}/run/{run_id}/retry")
async def retry_run(case_id: int, run_id: int, background: BackgroundTasks, request: Request):
    """Повторить прерванный расчёт по закреплённому входу (RUN-002).

    Новая попытка связана с прежней (retry_of) и считает те же байты входа
    из хранилища артефактов, а не текущий реестр: если данные с тех пор
    изменились, это уже новый расчёт, и его запускают кнопкой. Без снимка
    или с повреждённым артефактом повтор отклоняется — слепой повтор
    запрещён.
    """
    body = await request.json() if await request.body() else {}
    db = session()
    try:
        run = db.get(Run, run_id)
        if run is None or run.case_id != case_id:
            raise HTTPException(404, "прогон не найден")
        if run.status not in RETRYABLE:
            raise HTTPException(409, "повторить можно только прерванный, не успевший или "
                                "упавший расчёт; у № %d статус «%s»" % (run_id, run.status))
        if _pinned_input(db, run_id) is None:
            raise HTTPException(409, "вход расчёта № %d не закреплён или артефакт повреждён — "
                                "слепой повтор запрещён, запустите расчёт заново" % run_id)
        settings = json.loads(run.settings) if run.settings else {}
        new, created = _start_run(db, case_id, settings,
                                  operation_id=str(body.get("operation_id") or "") or None,
                                  retry_of=run_id)
        if not created:
            return {"ok": True, "run_id": new.id, "идет": new.status == "идет"}
        agents.say(db, case_id, "Повторяю расчёт № %d по закреплённому входу — попытка № %d."
                   % (run_id, new.id), who="экономист")
        background.add_task(_solve, case_id, new.id, settings, run_id)
        return {"ok": True, "run_id": new.id, "retry_of": run_id}
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
    import export_report
    import report as report_mod
    import rules as rules_mod
    # Книга собирается заново и тогда, когда поменялся сам отчёт: иначе
    # после правки названий и граф (30.09.2026) выгрузка старого расчёта
    # отдавала прежние листы.
    fresh = max([os.path.getmtime(out)] + [os.path.getmtime(m.__file__)
                                           for m in (export_report, report_mod, rules_mod)])
    if not os.path.exists(book) or os.path.getmtime(book) < fresh:
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


@app.get("/plan/{rest:path}", include_in_schema=False)
@app.get("/registry", include_in_schema=False)
@app.get("/registry/{rest:path}", include_in_schema=False)
def screen_address(rest: str = ""):
    """Адрес экрана — путь, без «#» (пользователь 30.09.2026: «зачем решётка
    в урлах»): на /plan/… и /registry/… — та же страница, экран по адресу
    выбирает она сама (app.js, parseRoute). Без этого перезагрузка по такому
    адресу давала бы 404."""
    return FileResponse(os.path.join(STATIC, "index.html"),
                        headers={"Cache-Control": "no-store, must-revalidate"})


app.mount("/", NoCacheStatic(directory=STATIC, html=True), name="static")
