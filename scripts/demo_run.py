# -*- coding: utf-8 -*-
"""Сквозной прогон демо через API: документы → разбор → расчет → отчет.

То же, что экономист делает руками: создать план, загрузить четыре файла из
data/demo в реестр, дождаться разбора, запустить расчет и открыть отчет.
Скрипт печатает, что вышло на каждом шаге, и падает, если какой-то раздел
отчета остался пустым — это и есть проверка «ничего не забыли отрисовать».

    .venv/Scripts/python.exe scripts/demo_run.py            # новый план
    .venv/Scripts/python.exe scripts/demo_run.py --reset    # убрать тестовые документы

Сервер должен быть запущен на 127.0.0.1:8770.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DEMO = os.path.join(ROOT, "data", "demo")
BASE = "http://127.0.0.1:8770"
FILES = ["Штатное расписание 2026.xlsx", "Договоры 2026.xlsx",
         "РКМ ГОЗ Радар-26.xlsx", "Структура цены Грант-26.xlsx"]
#: Тестовые документы прежних прогонов — их строки мешают демо-реестру.
JUNK = ("uc12_", "ОБРАЗЕЦ", "demo_input", "succ_input", "проверка_", "head_in")


def call(path, data=None, method=None):
    body = None
    headers = {}
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=body, headers=headers,
                                 method=method or ("POST" if data is not None else "GET"))
    with urllib.request.urlopen(req, timeout=600) as r:
        raw = r.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def upload(case_id, paths, scope="реестр"):
    boundary = "----fot" + uuid.uuid4().hex
    parts = []
    for p in paths:
        name = os.path.basename(p)
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        with open(p, "rb") as f:
            data = f.read()
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"files\"; filename=\"%s\"\r\n"
                      "Content-Type: %s\r\n\r\n" % (boundary, name, ctype)).encode("utf-8")
                     + data + b"\r\n")
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"scope\"\r\n\r\n%s\r\n"
                  % (boundary, scope)).encode("utf-8"))
    body = b"".join(parts) + ("--%s--\r\n" % boundary).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/case/%d/upload" % case_id, data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_parsed(case_id, doc_ids, timeout=420):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = call("/api/case/%d" % case_id)
        docs = {d["id"]: d for d in st["documents"]}
        busy = [a for a in st["activities"] if a["state"] == "идет"]
        pending = [i for i in doc_ids if docs.get(i, {}).get("state") == "ожидает"]
        if not busy and not pending:
            return st
        time.sleep(3)
    raise SystemExit("разбор не завершился за %d с" % timeout)


def wait_run(case_id, run_id, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = call("/api/case/%d" % case_id)
        run = [r for r in st["runs"] if r["id"] == run_id][0]
        if run["status"] != "идет":
            return run, st
        time.sleep(3)
    raise SystemExit("расчет не завершился за %d с" % timeout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="удалить тестовые документы прежних прогонов")
    ap.add_argument("--title", default="План ФОТ на 2026 год")
    ap.add_argument("--docs", default=DEMO, help="папка с четырьмя файлами (по умолчанию data/demo)")
    ap.add_argument("--allow-fail", action="store_true",
                    help="не считать ошибкой план, который не сошелся (вариант «дефицит»)")
    args = ap.parse_args()

    if args.reset:
        cases = call("/api/cases")
        st = call("/api/case/%d" % cases[0]["id"]) if cases else {"documents": []}
        for d in st["documents"]:
            if any(j.lower() in d["name"].lower() for j in JUNK) or d["name"] in FILES:
                call("/api/document/%d" % d["id"], method="DELETE")
                print("удален тестовый документ:", d["name"])

    case = call("/api/cases", {"title": args.title, "year": 2026})
    cid = case["id"]
    print("план создан: №%d «%s»" % (cid, args.title))

    res = upload(cid, [os.path.join(args.docs, f) for f in FILES])
    ids = res.get("documents") or []
    print("загружено документов:", len(ids))
    st = wait_parsed(cid, ids)
    for d in st["documents"]:
        if d["id"] in ids:
            print("  %-34s %-22s %s" % (d["name"][:34], d["state"], d["summary"] or ""))
    counts = st["case"]["counts"]
    print("реестр: сотрудников %s, договоров %s" % (counts["employees"], counts["contracts"]))
    data = call("/api/case/%d/data" % cid)
    print("  поступлений %d, строк трудоемкости %d" % (len(data["inflows"]), len(data["labor"])))
    for l in data["labor"]:
        print("   ", l["contract"], l["position"], l["person_months"], "чел.-мес.", l["avg_cost"], "₽, людей", l.get("headcount"))

    run = call("/api/case/%d/solve" % cid, {})
    rid = run["run_id"]
    print("расчет запущен: прогон №%d" % rid)
    run, st = wait_run(cid, rid)
    print("расчет:", run["status"], run.get("seconds"), "с")
    for m in st["messages"][-3:]:
        print("  лента:", (m.get("text") or "")[:160].replace("\n", " "))
    if run["status"] != "OPTIMAL":
        for m in st["messages"][-6:]:
            print("  лента:", (m.get("text") or "")[:300].replace("\n", " "))
        if args.allow_fail:
            print("план не сошелся, как и ожидалось; план №%d, прогон №%d" % (cid, rid))
            return
        raise SystemExit("план не сошелся")
    s = run.get("summary") or {}
    for g in s.get("цели") or []:
        print("  цель:", g["приоритет"], "|", g["цель"], "|", g["значение"], g["единица"])

    rep = call("/api/case/%d/run/%d/report" % (cid, rid))
    checks = {
        "1 итоги": bool(rep["итоги"]["ФОТ"]),
        "2 договоры": len(rep["договоры"]),
        "3 освоение": len(rep["освоение"]["договоры"]),
        "4 касса": len(rep["касса"]),
        "5 виды": len(rep["виды"]),
        "6 регистр": len(rep["регистр"]),
        "7 ставки": len(rep["ставки"]),
        "8 БЭП": len(rep["бэп"]),
        "9 П4": len(rep["п4"]),
        "10 трудоемкость": len(rep["трудоемкость"]),
        "11 кто закрывает": len(rep["кто"]),
        "12 незакрыто": len(rep["незакрыто"]),
        "13 цели": len(s.get("цели") or []),
    }
    empty = [k for k, v in checks.items() if not v and k != "12 незакрыто"]
    for k, v in checks.items():
        print("  раздел %-16s %s" % (k, v))
    for c in rep["договоры"]:
        print("  договор %-11s %s · выплаты %s из %s мес · освоение %s · %s"
              % (c["код"], c["срок выплат"], c["месяцев с выплатами"], c["месяцев в окне"],
                 c["освоение"], c["статус"]))
    for l in rep["трудоемкость"]:
        print("  трудоемкость %-11s %-26s план %s факт %s людей %s/%s %s"
              % (l["договор"], l["строка"], l["план чел-мес"], l["факт чел-мес"],
                 l["людей макс"], l["людей предел"], l["статус"]))
    if empty:
        raise SystemExit("пустые разделы отчета: " + ", ".join(empty))
    print("все разделы заполнены; план №%d, прогон №%d" % (cid, rid))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
