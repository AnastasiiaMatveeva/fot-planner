# -*- coding: utf-8 -*-
"""Прогон демо-комплекта data/demo_pack через API — так же, как его
загружает экономист руками: папка за папкой в реестр, затем план и расчёт.

    .venv/Scripts/python.exe scripts/demo_pack.py clean     # убрать из реестра документы комплекта и демо-планы
    .venv/Scripts/python.exe scripts/demo_pack.py load      # загрузить комплект, посчитать, проверить
    .venv/Scripts/python.exe scripts/demo_pack.py load --dump-january январь.json --no-fact

«clean» удаляет только документы с именами файлов комплекта и прежнего
демо-набора (data/demo) и планы демо-прогонов; справочник должностей и
правила замещения — реестр организации — не трогает. Адрес сервера —
FOT_BASE_URL (по умолчанию 127.0.0.1:8770): проверять — на копии базы.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import demo_run as run  # noqa: E402

PACK = os.path.join(ROOT, "data", "demo_pack")
TITLE = "План ФОТ на 2026 год"
#: Виды выплат, которые обязаны быть в плане комплекта — как у демо-набора.
EXPECT_KINDS = run.EXPECT_KINDS


def steps(pack):
    """Загрузки по порядку: (название шага, [файлы]). Факт и зарплата находят
    сотрудников и договоры в реестре — они после штатного и договоров."""
    out = []
    for top in sorted(os.listdir(pack)):
        path = os.path.join(pack, top)
        if not os.path.isdir(path):
            continue
        subs = sorted(d for d in os.listdir(path) if os.path.isdir(os.path.join(path, d)))
        if subs:
            for sub in subs:
                folder = os.path.join(path, sub)
                files = sorted(os.listdir(folder), key=lambda n: (not n.startswith("Договор"), n))
                out.append(("%s / %s" % (top, sub), [os.path.join(folder, f) for f in files]))
        else:
            out.append((top, [os.path.join(path, f) for f in sorted(os.listdir(path))]))
    return out


def pack_names(pack):
    return {os.path.basename(f) for _s, files in steps(pack) for f in files}


#: Ручные графы договора (app/basis.py, MANUAL) хранятся отдельно от документа
#: и возвращаются поверх при каждом разборе — удаление документа их не
#: снимает; реестр отдаёт их списком «manual». Надбавки и трудоёмкость,
#: введённые руками, помечены источником.
MANUAL_SOURCE = "введено вручную"


def pack_codes():
    """Шифры договоров и коды сотрудников комплекта."""
    import create_demo_pack as gen
    return {c[0] for c in gen.contracts()}, {e[0] for e in gen.employees()}


def clear_manual():
    """Снять ручные правки реестра по договорам и сотрудникам комплекта:
    ручные графы договоров, ручные надбавки 120 и строки трудоёмкости.
    Комплект показывает документы — поверх них правок быть не должно."""
    codes, people = pack_codes()
    data = run.call("/api/case/0/data")
    for c in data["contracts"]:
        if c["code"] in codes and c.get("manual"):
            got = run.call("/api/contracts/%s" % c["code"],
                           {"fields": {col: "" for col in c["manual"]}})
            if got.get("changed"):
                print("сняты ручные графы %s: %s" % (c["code"], ", ".join(sorted(got["changed"]))))
    for s in data["secret"]:
        if s.get("source") == MANUAL_SOURCE and s.get("employee") in people:
            run.call("/api/secret_allowances/%d" % s["id"], method="DELETE")
            print("снята ручная надбавка 120: %s / %s" % (s["employee"], s["contract"]))
    for l in data["labor"]:
        if l.get("source") == MANUAL_SOURCE and l.get("contract") in codes:
            run.call("/api/labor/%d" % l["id"], method="DELETE")
            print("снята ручная строка трудоёмкости: %s / %s" % (l["contract"], l["position"] or "—"))


def clean(pack):
    clear_manual()
    names = pack_names(pack) | set(run.FILES)
    for c in run.call("/api/cases"):
        if any(c["title"].startswith(t) for t in run.DEMO_TITLES):
            run.call("/api/case/%d" % c["id"], method="DELETE")
            print("удален план:", c["title"])
    for d in run.call("/api/documents"):
        if d["name"] in names:
            run.call("/api/document/%d" % d["id"], method="DELETE")
            print("удален документ:", d["name"])
    left = [d["name"] for d in run.call("/api/documents")]
    print("в реестре осталось документов: %d%s" % (len(left), (" — " + ", ".join(left)) if left else ""))
    codes, _people = pack_codes()
    extra = [c["code"] for c in run.call("/api/case/0/data")["contracts"] if c["code"] not in codes]
    if extra:
        # Чужие договоры идут в расчёт плана вместе с комплектом: договор с
        # трудоёмкостью, на который никому не разрешено, делает план
        # невыполнимым. Скрипт их не удаляет — это данные реестра.
        print("в реестре есть договоры не из комплекта:", ", ".join(extra))


def january(cid, rid):
    """Выплаты января из прогона: {код: [{договор, вид, сумма, ставка}]}."""
    pay = run.call("/api/case/%d/run/%d/payroll" % (cid, rid))
    data = run.call("/api/case/%d/data" % cid)
    by_fio = {e["fio"]: e["code"] for e in data["employees"]}
    out = {}
    for p in pay["люди"]:
        code = p.get("код") or by_fio.get(p["фио"])
        mon = (p["месяцы"] or [None])[0]
        if not mon or not code:
            continue
        for c in mon["договоры"]:
            for k in c["виды"]:
                out.setdefault(code, []).append({"договор": c["код"], "вид": k["вид"],
                                                 "сумма": k["сумма"], "ставка": c["ставка"]})
    return out


def load(pack, dump=None, no_fact=False, solve=True):
    case = run.call("/api/cases", {"title": TITLE, "year": 2026})
    cid = case["id"]
    print("план создан: №%d" % cid)
    fails = []
    for name, files in steps(pack):
        if no_fact and "Факт" in name:
            continue
        res = run.upload(cid, files)
        ids = res.get("documents") or []
        st = run.wait_parsed(cid, ids)
        print("%s:" % name)
        for d in st["documents"]:
            if d["id"] in ids:
                print("  %-42s %-12s %s" % (d["name"][:42], d["state"], (d["summary"] or "")[:150]))
                if d["state"] != "разобран":
                    fails.append("%s: %s" % (d["name"], d["state"]))
    st = run.call("/api/case/%d" % cid)
    for q in st["questions"]:
        if not q.get("answer"):
            print("  ВОПРОС АГЕНТА:", q["text"][:300].replace("\n", " "))
    data = run.call("/api/case/%d/data" % cid)
    print("реестр: сотрудников %d, договоров %d, строк трудоёмкости %d, строк зарплаты %d, "
          "факт %d" % (len(data["employees"]), len(data["contracts"]), len(data["labor"]),
                       len((data.get("salary") or {}).get("rows") or []),
                       len(data.get("facts") or [])))
    for l in data["labor"]:
        print("   ", l["contract"], l["position"], l["person_months"], "чел.-мес.", l["avg_cost"],
              "₽, людей", l.get("headcount"), "по месяцам" if l.get("months") else "")
    if not solve:
        return cid, None, fails
    r = run.call("/api/case/%d/solve" % cid, {})
    rid = r["run_id"]
    print("расчет запущен: прогон №%d" % rid)
    res, st = run.wait_run(cid, rid)
    print("расчет:", res["status"], res.get("seconds"), "с")
    if res["status"] != "OPTIMAL":
        for m in st["messages"][-6:]:
            print("  лента:", (m.get("text") or "")[:500].replace("\n", " "))
        fails.append("план не сошёлся: %s" % res["status"])
        return cid, rid, fails
    fails += run.check_plan(cid, rid, res, st, EXPECT_KINDS)
    if dump:
        with open(dump, "w", encoding="utf-8") as f:
            json.dump(january(cid, rid), f, ensure_ascii=False, indent=1)
        print("выплаты января записаны:", dump)
    return cid, rid, fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=("clean", "load"))
    ap.add_argument("--pack", default=PACK)
    ap.add_argument("--dump-january", default=None)
    ap.add_argument("--no-fact", action="store_true", help="не загружать факт начислений")
    ap.add_argument("--no-solve", action="store_true")
    args = ap.parse_args()
    if args.what == "clean":
        clean(args.pack)
        return 0
    t0 = time.time()
    cid, rid, fails = load(args.pack, args.dump_january, args.no_fact, not args.no_solve)
    print("заняло %.0f с" % (time.time() - t0))
    if fails:
        print("\nПРОВАЛ:")
        for f in fails:
            print("  ✗", f)
        return 1
    print("\nвсе проверки пройдены; план №%d, прогон №%s" % (cid, rid))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
