# -*- coding: utf-8 -*-
"""Карта требований не должна врать: ссылки, номера случаев и полнота списка.

Карта `docs/requirements-map.json` — единственное место, где требования ТЗ,
способности каталога и предметные правила сведены с кодом и проверками.
Такая таблица врёт молча: файл переименовали, случай удалили, ID из ТЗ
забыли — и карта показывает покрытие, которого нет. Поэтому она входит в
приёмку и проверяется по фактам, а не по своему содержимому:

* каждый путь в `code` существует (функция после `::` не проверяется);
* каждый номер случая существует в стенде, которому принадлежит по префиксу
  (число — crisis, «а» — audit, «ч» — chat);
* все ID требований из раздела 5 ТЗ есть в карте, и лишних нет;
* ключи способностей совпадают с каталогом `app/agents.py`;
* поле `catalogue_real` совпадает с `real` каталога — расхождение между
  каталогом и картой должно быть видно, а не заглажено;
* каждый случай, упомянутый в своде правил решателя, существует.

Выход в протоколе стенда: `[ключ] ОК|ПРОВАЛ` и строка «итого», как у прочих.
"""
from __future__ import annotations

import ast
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
MAP = os.path.join(ROOT, "docs", "requirements-map.json")
TZ = os.path.join(ROOT, "docs", "ТЗ_РАЗВИТИЕ_АГЕНТОВ.md")
RULES = os.path.join(ROOT, "docs", "ПРАВИЛА_РЕШАТЕЛЯ.md")
STANDS = {"crisis": "harness/crisis.py", "audit": "harness/audit.py", "chat": "harness/chat.py",
          "lifecycle": "harness/lifecycle.py", "migrations": "harness/migrations.py",
          "manifest": "harness/manifest.py", "decisions": "harness/decisions.py"}


def read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def stand_cases():
    out = {}
    for name, rel in STANDS.items():
        out[name] = set(re.findall(r'^@case\("([^"]+)"', read(os.path.join(ROOT, rel)), re.M))
    return out


def stand_of(case_id):
    if case_id.startswith("а"):
        return "audit"
    if case_id.startswith("ч"):
        return "chat"
    if case_id.startswith("л"):
        return "lifecycle"
    if case_id.startswith("м"):
        return "migrations"
    if case_id.startswith("с"):
        return "manifest"
    if case_id.startswith("р"):
        return "decisions"
    return "crisis"


def catalogue():
    """Ключи и признак real из app/agents.py без импорта приложения."""
    tree = ast.parse(read(os.path.join(ROOT, "app", "agents.py")))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "AGENTS":
            data = ast.literal_eval(node.value)
            return {k: v.get("real") for k, v in data.items()}
    raise RuntimeError("в app/agents.py нет словаря AGENTS")


def tz_ids():
    return set(re.findall(r"^\*\*([A-Z]{2,4}-\d{3})\.", read(TZ), re.M))


def rule_case_refs():
    """Случаи из скобок в конце правил свода: «(01, 03, 23; про фрагмент случая нет)»."""
    refs = []
    for m in re.finditer(r"\(([^()]*)\)", read(RULES)):
        inner = m.group(1)
        for token in re.split(r"[,\s;]+", inner.split(";")[0]):
            if re.fullmatch(r"\d{2}b?|а\d{2}|ч\d{2}б?|л\d{2}|м\d{2}|с\d{2}|р\d{2}", token):
                refs.append(token)
            elif re.fullmatch(r"а\d{2}–а\d{2}", token):
                a, b = token.split("–")
                for n in range(int(a[1:]), int(b[1:]) + 1):
                    refs.append("а%02d" % n)
    return refs


def main():
    # --map ПУТЬ: проверить другую карту. Нужен отрицательному случаю в
    # журнале реализации — валидатор обязан краснеть на выдуманном номере.
    global MAP
    if len(sys.argv) > 2 and sys.argv[1] == "--map":
        MAP = os.path.abspath(sys.argv[2])
    checks, failed = [], 0

    def verdict(key, problems, title):
        nonlocal failed
        ok = not problems
        failed += 0 if ok else 1
        print("[%s] %-9s %s" % (key, "ОК" if ok else "ПРОВАЛ", title))
        for p in problems[:8]:
            print("      ✗", p)
        checks.append(ok)

    data = json.load(io.open(MAP, encoding="utf-8"))
    cases = stand_cases()

    # 1. Пути к файлам
    missing = []
    for section in ("capabilities", "tz_requirements"):
        for row in data[section]:
            for ref in row.get("code", []) + row.get("handlers", []):
                path = ref.split("::")[0].split(" (")[0].strip()
                if "/" in path and not os.path.exists(os.path.join(ROOT, path)):
                    missing.append("%s: %s" % (row.get("id") or row.get("key"), path))
    verdict("к01", missing, "Каждый путь в карте существует")

    # 2. Номера случаев способностей
    bad = []
    for cap in data["capabilities"]:
        for c in cap["tests"]["cases"]:
            if c not in cases[stand_of(c)]:
                bad.append("%s: случая %s нет в %s" % (cap["key"], c, STANDS[stand_of(c)]))
    verdict("к02", bad, "Случаи стендов у способностей существуют")

    # 3. Полнота ID ТЗ
    want, have = tz_ids(), {r["id"] for r in data["tz_requirements"]}
    problems = ["в карте нет %s" % i for i in sorted(want - have)] + \
               ["в ТЗ нет %s" % i for i in sorted(have - want)]
    verdict("к03", problems, "Все ID раздела 5 ТЗ в карте, лишних нет (%d)" % len(want))

    # 4. Статусы из разрешённых списков
    allowed = data["statuses"]
    bad = []
    for r in data["tz_requirements"]:
        if r["implementation"] not in allowed["implementation"]:
            bad.append("%s: implementation=%s" % (r["id"], r["implementation"]))
        if r["tests"] not in allowed["tests"]:
            bad.append("%s: tests=%s" % (r["id"], r["tests"]))
        if r["implementation"] in ("implemented", "partial", "in_progress") and not r["evidence"]:
            bad.append("%s: статус %s без evidence" % (r["id"], r["implementation"]))
    for cap in data["capabilities"]:
        if cap["maturity"] not in allowed["maturity"] or cap["llm"] not in allowed["llm"]:
            bad.append("%s: maturity/llm вне списка" % cap["key"])
        if cap["maturity"] == "implemented" and not cap["handlers"]:
            bad.append("%s: implemented без обработчика" % cap["key"])
    verdict("к04", bad, "Статусы из разрешённых списков; implemented — с обработчиком и evidence")

    # 5. Каталог app/agents.py и карта
    cat = catalogue()
    keys_map = {c["key"] for c in data["capabilities"]} - {"solver"}
    bad = ["в карте нет %s" % k for k in sorted(set(cat) - keys_map)] + \
          ["в каталоге нет %s" % k for k in sorted(keys_map - set(cat))]
    for cap in data["capabilities"]:
        if cap["key"] in cat and cap["catalogue_real"] != cat[cap["key"]]:
            bad.append("%s: real в каталоге %s, в карте %s" % (cap["key"], cat[cap["key"]], cap["catalogue_real"]))
    verdict("к05", bad, "Ключи и real совпадают с каталогом app/agents.py")

    # 6. Свод правил ссылается на существующие случаи
    refs = rule_case_refs()
    bad = sorted({"%s нет в %s" % (c, STANDS[stand_of(c)]) for c in refs if c not in cases[stand_of(c)]})
    verdict("к06", bad, "Случаи из свода правил существуют (%d ссылок)" % len(refs))

    print("\nитого: %d случаев, провалов %d" % (len(checks), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
