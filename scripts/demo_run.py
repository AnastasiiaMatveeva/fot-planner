# -*- coding: utf-8 -*-
"""Сквозной прогон демо через API: документы → разбор → расчет → отчет.

То же, что экономист делает руками: создать план, загрузить четыре файла из
data/demo в реестр, дождаться разбора, запустить расчет и открыть отчет.
Скрипт печатает, что вышло на каждом шаге, и падает, если план не сошёлся,
если какой-то раздел отчёта остался пустым или если в плане не видно того,
ради чего демо собрано: видов выплат (оклад, 120, 122, 124, приказ),
совместительства, замещения, БЭП, П4, надбавки 120 в реестре, соблюдения
всех проверяемых условий.

    .venv/Scripts/python.exe scripts/demo_run.py                 # новый план
    .venv/Scripts/python.exe scripts/demo_run.py --reset         # убрать демо-планы и демо-файлы, потом прогнать
    .venv/Scripts/python.exe scripts/demo_run.py --chat          # + закрепление словами и пересчёт
    .venv/Scripts/python.exe scripts/demo_run.py --docs data/demo_variants/дефицит --allow-fail --title "Вариант: дефицит"

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
JUNK = ("uc12_", "ОБРАЗЕЦ", "demo_input", "succ_input", "проверка_", "head_in", "prikaz_")
#: Планы, которые создаёт этот скрипт, — их «--reset» убирает.
DEMO_TITLES = ("План ФОТ на 2026 год", "Вариант:")

#: Что обязано быть в демо-плане. Каждая проверка — одно правило решателя.
#: Пять договоров: ГОЗ с трудоёмкостью (БЭП и П2556), коммерческий (124 и
#: П4), грант этапами, «Приоритет 2030» со 152 и внебюджет с приказом.
EXPECT_KINDS = ("оклад", "120 — гостайна", "122 — за качество", "124 — интенсивность",
                "152 — дополнительная работа", "стимулирующая приказом")
CHAT_FIX = "оставь Петрова на гранте с июня по декабрь"


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


def wait_parsed(case_id, doc_ids, timeout=600):
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


def wait_run(case_id, run_id, timeout=1800, patient=False):
    """patient — ждать и статус «прерван»: он ставится при перезапуске
    сервера или импорте main из скрипта, а решатель при этом может ещё
    работать и допишет настоящий статус."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = call("/api/case/%d" % case_id)
        run = [r for r in st["runs"] if r["id"] == run_id][0]
        if run["status"] != "идет" and not (patient and run["status"] == "прерван"):
            # Агент проверки дописывает ленту после статуса — подождать его.
            for _ in range(20):
                st = call("/api/case/%d" % case_id)
                if not [a for a in st["activities"] if a["state"] == "идет"]:
                    break
                time.sleep(3)
            return run, st
        time.sleep(5)
    raise SystemExit("расчет не завершился за %d с" % timeout)


def wait_new_run(case_id, after_run_id, timeout=120):
    """Дождаться, пока чат заведёт новый прогон (после фразы-закрепления)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = call("/api/case/%d" % case_id)
        new = [r for r in st["runs"] if r["id"] > after_run_id]
        if new:
            return max(r["id"] for r in new)
        time.sleep(3)
    return None


def reset():
    """Убрать планы и документы прежних демо-прогонов. Нормативные документы
    и правила замещения не трогаем — это реестр организации."""
    for c in call("/api/cases"):
        if any(c["title"].startswith(t) for t in DEMO_TITLES):
            call("/api/case/%d" % c["id"], method="DELETE")
            print("удален план:", c["title"])
    for d in call("/api/documents"):
        if any(j.lower() in d["name"].lower() for j in JUNK) or d["name"] in FILES:
            call("/api/document/%d" % d["id"], method="DELETE")
            print("удален тестовый документ:", d["name"])


def check_plan(cid, rid, run, st, expect_kinds, want_chat_fix=False):
    """Проверки готового плана: разделы, виды, правила. Возвращает список провалов."""
    fails = []
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
    for k, v in checks.items():
        print("  раздел %-16s %s" % (k, v))
    empty = [k for k, v in checks.items() if not v and k != "12 незакрыто"]
    if empty:
        fails.append("пустые разделы отчета: " + ", ".join(empty))
    for c in rep["договоры"]:
        print("  договор %-11s %-12s выплаты %s из %s мес · освоение %s · %s · %s"
              % (c["код"], c["признак"] or "", c["месяцев с выплатами"], c["месяцев в окне"],
                 c["освоение"], c["статус"], c["выплаты"]))
    for l in rep["трудоемкость"]:
        print("  трудоемкость %-11s %-26s план %s факт %s людей %s/%s %s"
              % (l["договор"], l["строка"], l["план чел-мес"], l["факт чел-мес"],
                 l["людей макс"], l["людей предел"], l["статус"]))
    bad_labor = [l for l in rep["трудоемкость"] if l["статус"] != "сходится"]
    if bad_labor:
        fails.append("трудоемкость не сходится: " + ", ".join(
            "%s/%s (%s)" % (l["договор"], l["строка"], l["статус"]) for l in bad_labor))

    # Виды выплат: каждый — своё правило решателя.
    got_kinds = {}
    for k in rep["виды"]:
        if k["сумма"]:
            got_kinds[k["вид"]] = got_kinds.get(k["вид"], 0.0) + k["сумма"]
    for name in expect_kinds:
        print("  вид %-30s %s" % (name, ("%.0f ₽" % got_kinds[name]) if name in got_kinds else "НЕТ"))
    missing = [k for k in expect_kinds if k not in got_kinds]
    if missing:
        fails.append("в плане нет видов выплат: " + ", ".join(missing))

    # Совместительство: у кого-то ставка сверх штатной.
    part = [r["фио"] for r in rep["ставки"]
            if any(c["занятость"] in ("совместительство", "смешанная") for c in r["договоры"])]
    print("  совместительство:", ", ".join(part) or "НЕТ")
    if not part:
        fails.append("решатель не открыл ни одного совместительства")

    # Замещение: чужую строку РКМ закрыл человек с другой должностью.
    subst = []
    for w in rep["кто"]:
        for p in w["люди"]:
            pos = (p.get("должность") or "").strip().lower()
            if pos and w["строка"] and pos != w["строка"].strip().lower():
                subst.append("%s (%s) → %s/%s" % (p.get("фио"), p.get("должность"),
                                                   w["договор"], w["строка"]))
    print("  замещение:", "; ".join(subst) or "НЕТ")
    if not subst:
        fails.append("замещение не сработало: чужие строки РКМ никто не закрыл")

    # «Приоритет 2030»: на договоре либо штатные выплаты, либо 152. Обе
    # половины правила должны быть видны в плане, иначе договор в демо
    # ничего не доказывает.
    prio = [c["код"] for c in rep["договоры"] if "приоритет" in (c["признак"] or "").lower()]
    if prio:
        print("  приоритет:", ", ".join(prio))
        for code in prio:
            k152 = k_staff = 0.0
            for k in rep["виды"]:
                if k["договор"] != code or not k["сумма"]:
                    continue
                if k["вид"].startswith("152"):
                    k152 += k["сумма"]
                else:
                    k_staff += k["сумма"]
            print("  приоритет %s: 152 — %.0f ₽, штатные выплаты — %.0f ₽" % (code, k152, k_staff))
            if not k152:
                fails.append("на договоре приоритета %s нет надбавки 152" % code)
            if not k_staff:
                fails.append("на договоре приоритета %s нет штатных выплат" % code)
        # 152 исключает другие надбавки в том же месяце — проверяем по людям.
        pay152 = call("/api/case/%d/run/%d/payroll" % (cid, rid))
        for person in pay152["люди"]:
            for mon in person["месяцы"]:
                if not mon:
                    continue
                kinds = set()
                for c in mon["договоры"]:
                    for k in c["виды"]:
                        kinds.add(k["вид"])
                if "152" in kinds and (kinds - {"152", "оклад"}):
                    fails.append("152 вместе с другими надбавками: %s, месяц %d"
                                 % (person["фио"], mon["м"]))
    goz = [c["код"] for c in rep["договоры"] if c["ГОЗ"]]
    print("  ГОЗ:", ", ".join(goz) or "НЕТ", "· строк БЭП %d · строк П4 %d" % (len(rep["бэп"]), len(rep["п4"])))
    over_bep = [b for b in rep["бэп"] if (b["запас"] or 0) < -1]
    if over_bep:
        fails.append("БЭП превышен: %s" % ["%s м%s" % (b["договор"], b["месяц"]) for b in over_bep])
    p4_bad = [p for p in rep["п4"] if abs(p["отклонение"] or 0) > 0.001]
    if p4_bad:
        fails.append("П4 не ровно: %s" % ["%s м%s %s" % (p["фио"], p["месяц"], p["отклонение"]) for p in p4_bad])
    if rep["п4"]:
        print("  П4: %s, месяцев %d, отклонение %s" % (rep["п4"][0]["фио"], len(rep["п4"]),
                                                      rep["п4"][0]["отклонение"]))

    # Условия работы экономиста — все соблюдены.
    rules = call("/api/case/%d/run/%d/rules" % (cid, rid))["правила"]
    broken = [r for r in rules if r["состояние"] == "нарушено"]
    print("  условий проверено %d, нарушено %d" % (len(rules), len(broken)))
    for r in broken:
        print("    НАРУШЕНО:", r["правило"], "—", r["факт"])
    if broken:
        fails.append("нарушены условия: " + "; ".join(r["правило"] for r in broken))

    if want_chat_fix:
        pay = call("/api/case/%d/run/%d/payroll" % (cid, rid))
        petrov = next((p for p in pay["люди"] if "Петров" in p["фио"]), None)
        ok = petrov is not None and all(
            any(c["код"] == "C_GRANT-26" and c["ставка"] for c in (petrov["месяцы"][m - 1] or {}).get("договоры", []))
            for m in range(6, 13))
        print("  закрепление Петрова на гранте июнь–декабрь:", "выполнено" if ok else "НЕТ")
        if not ok:
            fails.append("закрепление из чата не отразилось в плане")
    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="удалить демо-планы и демо-документы прежних прогонов")
    ap.add_argument("--title", default="План ФОТ на 2026 год")
    ap.add_argument("--docs", default=DEMO, help="папка с четырьмя файлами (по умолчанию data/demo)")
    ap.add_argument("--allow-fail", action="store_true",
                    help="не считать ошибкой план, который не сошелся (вариант «дефицит»)")
    ap.add_argument("--chat", action="store_true",
                    help="после расчёта закрепить Петрова на гранте фразой в чате и пересчитать")
    ap.add_argument("--kinds", default=",".join(EXPECT_KINDS),
                    help="какие виды выплат обязаны быть в плане (через запятую)")
    ap.add_argument("--existing", default=None, metavar="ПЛАН:ПРОГОН",
                    help="не загружать и не считать заново — дождаться и проверить этот прогон")
    args = ap.parse_args()
    expect_kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]

    if args.existing:
        cid, rid = (int(x) for x in args.existing.split(":"))
        run, st = wait_run(cid, rid, patient=True)
        print("расчет:", run["status"], run.get("seconds"), "с")
        if run["status"] != "OPTIMAL":
            raise SystemExit("план не сошелся")
        fails = check_plan(cid, rid, run, st, expect_kinds)
        data = call("/api/case/%d/data" % cid)
        if not data["secret"]:
            fails.append("надбавка 120 не попала в реестр")
        return finish(cid, rid, fails, args, expect_kinds)

    if args.reset:
        reset()

    case = call("/api/cases", {"title": args.title, "year": 2026})
    cid = case["id"]
    print("план создан: №%d «%s»" % (cid, args.title))

    res = upload(cid, [os.path.join(args.docs, f) for f in FILES])
    ids = res.get("documents") or []
    print("загружено документов:", len(ids))
    t0 = time.time()
    st = wait_parsed(cid, ids)
    print("разбор занял %.0f с" % (time.time() - t0))
    for d in st["documents"]:
        if d["id"] in ids:
            print("  %-34s %-22s %s" % (d["name"][:34], d["state"], d["summary"] or ""))
    bad_docs = [d for d in st["documents"] if d["id"] in ids and d["state"] != "разобран"]
    counts = st["case"]["counts"]
    print("реестр: сотрудников %s, договоров %s" % (counts["employees"], counts["contracts"]))
    data = call("/api/case/%d/data" % cid)
    print("  поступлений %d, строк трудоемкости %d, надбавок 120 %d, правил замещения %d"
          % (len(data["inflows"]), len(data["labor"]), len(data["secret"]), len(data["substitutions"])))
    for l in data["labor"]:
        print("   ", l["contract"], l["position"], l["person_months"], "чел.-мес.", l["avg_cost"], "₽, людей", l.get("headcount"))
    questions = [q for q in st["questions"] if not q.get("answer")]
    for q in questions:
        print("  ВОПРОС АГЕНТА:", q["text"][:200])
    fails = []
    if bad_docs:
        fails.append("документы не разобраны: " + ", ".join("%s (%s)" % (d["name"], d["state"]) for d in bad_docs))
    if not data["secret"]:
        fails.append("надбавка 120 не попала в реестр")

    run = call("/api/case/%d/solve" % cid, {})
    rid = run["run_id"]
    print("расчет запущен: прогон №%d" % rid)
    run, st = wait_run(cid, rid)
    print("расчет:", run["status"], run.get("seconds"), "с")
    for m in st["messages"][-3:]:
        print("  лента:", (m.get("text") or "")[:200].replace("\n", " "))
    if run["status"] != "OPTIMAL":
        for m in st["messages"][-6:]:
            print("  лента:", (m.get("text") or "")[:400].replace("\n", " "))
        if args.allow_fail:
            infeasible = [m for m in st["messages"] if (m.get("payload") or {}).get("kind") == "infeasible"]
            if not infeasible:
                raise SystemExit("план не сошелся, но агент невыполнимости причину не назвал")
            print("план не сошелся, как и ожидалось; причина названа; план №%d, прогон №%d" % (cid, rid))
            return
        raise SystemExit("план не сошелся")

    fails += check_plan(cid, rid, run, st, expect_kinds)
    return finish(cid, rid, fails, args, expect_kinds)


def finish(cid, rid, fails, args, expect_kinds):
    """Закрепление из чата (по флагу) и итог."""
    if args.chat and not fails:
        print("чат:", CHAT_FIX)
        call("/api/case/%d/message" % cid, {"text": CHAT_FIX})
        new_rid = wait_new_run(cid, rid)
        if new_rid is None:
            fails.append("после фразы в чате пересчёт не запустился")
        else:
            run2, st2 = wait_run(cid, new_rid, patient=True)
            print("пересчет №%d: %s %s с" % (new_rid, run2["status"], run2.get("seconds")))
            for m in st2["messages"][-4:]:
                print("  лента:", (m.get("text") or "")[:200].replace("\n", " "))
            if run2["status"] != "OPTIMAL":
                fails.append("пересчёт после закрепления не сошёлся")
            else:
                fails += check_plan(cid, new_rid, run2, st2, expect_kinds, want_chat_fix=True)

    if fails:
        print("\nПРОВАЛ:")
        for f in fails:
            print("  ✗", f)
        raise SystemExit(1)
    print("\nвсе проверки пройдены; план №%d, прогон №%d" % (cid, rid))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
