# -*- coding: utf-8 -*-
"""Матрица применимых проверок (QA-001): правило свода → вход → решатель →
независимый аудит → случаи → представление замечания.

Текст правил, их области и номера случаев читаются из свода
`docs/ПРАВИЛА_РЕШАТЕЛЯ.md` — они здесь не переписываются. Связь правила с
кодами валидации входа, карточками аудита и условиями отчёта — таблица
LINKS ниже, единственное место этой связи. Матрица `docs/qa-matrix.json` —
снимок, который собирается отсюда (`--write`) и в приёмке сверяется с
текущим состоянием (`--check`): изменилось правило, пропал случай,
переименовано условие отчёта, появилась карточка аудита — расхождение видно.

Пробел слоя — явное «gap», а не молчание: пустой набор проверок
положительного заключения не даёт. Обязательный набор проверок для
результата — карточки аудита из профиля каталога (`profile.fot-local`),
и матрица обязана с ним совпадать.

    .venv/Scripts/python.exe scripts/qa_matrix.py --write
    .venv/Scripts/python.exe scripts/qa_matrix.py --check
    .venv/Scripts/python.exe scripts/qa_matrix.py --check --only т03
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

RULES = os.path.join(ROOT, "docs", "ПРАВИЛА_РЕШАТЕЛЯ.md")
MATRIX = os.path.join(ROOT, "docs", "qa-matrix.json")
REPORT_RULES = os.path.join(ROOT, "app", "rules.py")
VALIDATION = os.path.join(ROOT, "src", "fot_planner", "validation.py")
STANDS = {"crisis": os.path.join(ROOT, "harness", "crisis.py"),
          "audit": os.path.join(ROOT, "harness", "audit.py")}

#: Правило свода → слои. audit — карточки result_audit; report — условия
#: app/rules.py (по названию); input — коды валидации входа; negative —
#: случаи, в которых правило нарушается и план отклоняется или называется
#: нарушение; source — откуда правило (ИСТОЧНИКИ.md, решения владельца).
LINKS = {
    1: dict(audit=["SALARY-001"], report=["Человек получает всю месячную зарплату", "Дефицит выплат разрешен настройками"],
            input=["INVALID_PAYMENT"], negative=["03", "36", "а10", "а19", "а25"],
            source="штатное расписание (зарплата строки); зарплата по месяцам — таблица «Заработная плата по месяцам», "
                   "решение пользователя 19.09.2026, карточка SALARY-001; фрагмент не меньше 1 000 ₽ — владелец продукта 11.09.2026"),
    2: dict(audit=["RATE-001"], report=["Оклад идет по ставке, а не произвольной суммой"],
            input=["UNKNOWN_POSITION", "DEFAULT_REFERENCE_USED_WARNING", "DEFAULT_VALUE_USED_WARNING"],
            negative=["а02", "а03", "а04"],
            source="справочник должностей (оклад); шаг ставки — слова экономиста, карточка RATE-001"),
    3: dict(audit=[], report=[], input=[], negative=[],
            source="слова экономиста; docs/architecture/ADR-labor-attribution.md"),
    4: dict(audit=[], report=["П2556: оклад и 122 не выше предела по должности"],
            input=["MISSING_ORG_SALARY_CAP_WARNING"], negative=["03"],
            source="приказ № 2556 через справочник должностей; приказ раз в месяц на строку — владелец продукта 11.09.2026"),
    5: dict(audit=[], report=["П4 при надбавке за интенсивность"],
            input=["P4_IGNORED_FOR_CATEGORY_WARNING"], negative=[],
            source="приказ № 4 через справочник; предел П4 — справочная информация_ЗП.xlsx, лист «пример»"),
    6: dict(audit=[], report=[], input=["P4_IGNORED_FOR_CATEGORY_WARNING"], negative=[],
            source="владелец продукта 11.09.2026 (средняя П4 по категории существует)"),
    7: dict(audit=[], report=["БЭП: средняя зарплата по ГОЗ-договору в пределах базовой"],
            input=[], negative=[],
            source="БЭП по справочнику должностей; структура цены ГОЗ"),
    8: dict(audit=["PAYMENT-001"], report=["Платим только теми видами, которые разрешены договором"],
            input=[], negative=["а12"],
            source="карточка договора (графы разрешений); SOURCE-002 (124 из типа договора не выводится)"),
    9: dict(audit=["PAYMENT-001"], report=["Надбавка 122 — с того же договора, что и оклад"], input=[], negative=["а17"],
            source="справочная информация_ЗП.xlsx, лист «виды надбавок»"),
    10: dict(audit=[], report=[], input=["UNKNOWN_EMPLOYEE", "UNKNOWN_CONTRACT", "INVALID_SECRET_ALLOWANCE_RATE"],
             negative=["05", "06"],
             source="реестр надбавки 120 (лист 120_надбавка); 120 без оклада — счёт 23 (ИСТОЧНИКИ.md); по человеку — владелец продукта 11.09.2026"),
    11: dict(audit=["PAYMENT-001"], report=["Режим «оклад плюс 152»"], input=[], negative=["а20"],
             source="справочная информация_ЗП.xlsx, лист «виды надбавок»; по строке — владелец продукта 11.09.2026"),
    12: dict(audit=["PAYMENT-001"], report=[], input=[], negative=["13", "а21"],
             source="условия договора программы «Приоритет 2030»"),
    13: dict(audit=["PERSON-001"], report=[], input=["PART_TIME_WITHOUT_MAIN", "SECOND_MAIN_ROW"], negative=["15", "а22"],
             source="штатное расписание (тип занятости)"),
    14: dict(audit=["PERSON-001"],
             report=["Суммарная ставка — не больше 1,5", "Совместительство — не больше 0,5 ставки"],
             input=["INVALID_RATE", "INVALID_EMPLOYMENT_RATE", "TOO_MANY_PART_TIME"], negative=["16", "а14", "а23", "а24"],
             source="ограничения по типу занятости и категории (штатное расписание); табельный номер — владелец продукта 11.09.2026"),
    15: dict(audit=[], report=[], input=[], negative=["27"],
             source="слова экономиста; вопрос 10 MODEL_REVIEW закрыт случаем 27"),
    16: dict(audit=[], report=["Работу закрывает подходящая должность"],
             input=["UNKNOWN_LABOR_POSITION_WARNING", "UNKNOWN_CONTRACT_POSITION_WARNING", "LABOR_ROW_GROUP_ONLY_WARNING"],
             negative=["20"],
             source="справочник замещения должностей; docs/architecture/ADR-labor-attribution.md"),
    17: dict(audit=[], report=[], input=[], negative=[],
             source="РКМ и структура цены; docs/architecture/ADR-labor-attribution.md"),
    18: dict(audit=[], report=["Сумма трудоемкости выбрана"], input=["MISSING_AVG_LABOR_COST_WARNING"], negative=[],
             source="РКМ и структура цены; приказ вне трудоёмкости — docs/architecture/ADR-labor-attribution.md"),
    19: dict(audit=["LABOR-001"], report=["Плановые человеко-месяцы закрыты", "Сумма трудоемкости выбрана"],
             input=["INVALID_LABOR", "INVALID_LABOR_TOLERANCE", "LABOR_WITHOUT_FOT"], negative=["а13"],
             source="РКМ (человеко-месяцы, сумма, специалисты); допуск 5 % жёсткий — владелец продукта 11.09.2026"),
    20: dict(audit=["LABOR-001"], report=["Плановые человеко-месяцы закрыты"], input=[], negative=["а16"],
             source="РКМ (этапы по месяцам)"),
    21: dict(audit=["FOT-001"], report=["Деньги нельзя потратить раньше, чем они пришли"],
             input=["INVALID_BUDGET_INFLOW"], negative=["23", "а15"],
             source="поступления по договору (карточка договора)"),
    22: dict(audit=["PAYMENT-001"], report=["Договор платит только в свои сроки"],
             input=["MISSING_CONTRACT_DATES", "INVALID_DATES", "MISSING_PAYMENT_DEADLINE", "EMPTY_PAYMENT_WINDOW"],
             negative=["24", "а12"],
             source="карточка договора (сроки, предел выплат); SOURCE-001 (пустой предел = дата окончания)"),
    23: dict(audit=["FOT-001"], report=["Выплаты не превышают ФОТ договора", "ФОТ договоров освоен"],
             input=["INVALID_FOT"], negative=["26", "а15"],
             source="карточка договора (ФОТ); остаток без порога — владелец продукта 11.09.2026"),
    24: dict(audit=["PAYMENT-001"], report=[], input=["CONTRACT_ALLOWED_AND_FORBIDDEN", "UNKNOWN_CONTRACT"],
             negative=["38", "39", "а18", "а26"],
             source="ручное решение экономиста в чате (настройки плана) и графы разрешённых/запрещённых договоров строки; "
                    "закрепление без вида выплаты — решение пользователя 19.09.2026, карточка MANUAL-ANY-001"),
    25: dict(audit=["SALARY-001"], report=["Дефицит выплат разрешен настройками"], input=["INVALID_LABOR_TOLERANCE"], negative=["26", "а19"],
             source="настройки расчёта; макс договоров по человеку и допуск не больше 5 % — владелец продукта 11.09.2026"),
    26: dict(audit=["PAYMENT-001", "LABOR-001", "FOT-001"], report=[],
             input=["INVALID_FROZEN_MONTHS", "FACT_AFTER_FROZEN_WARNING", "FACT_UNKNOWN_CONTRACT_WARNING",
                    "FACT_UNKNOWN_EMPLOYEE_WARNING", "FACT_RATE_DERIVED_WARNING", "FACT_MISSING_WARNING"],
             negative=["49", "а27"],
             source="пересчёт с факта — владелец продукта 30.09.2026 («не трогая январь и февраль, пересчитал… "
                    "Но учесть все правила про условия в квартал итд»); лист «факт_выплат» и графа «факт по месяц»"),
}

CASES = []


def case(num, title):
    def wrap(fn):
        CASES.append((num, title, fn))
        return fn
    return wrap


def read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


# ── источники фактов ────────────────────────────────────────────
def parse_rules():
    """Правила свода: номер, область, название, случаи и пометки о пробелах."""
    text = read(RULES)
    start = text.index("## Обязан: зарплата")
    end = text.index("## Карточки правил")
    blocks, current = [], None
    for line in text[start:end].splitlines():
        m = re.match(r"^(\d+)\. (.*)$", line)
        if m:
            current = [int(m.group(1)), m.group(2)]
            blocks.append(current)
        elif current is not None and line.startswith("   "):
            current[1] += " " + line.strip()
    rules = []
    for number, body in blocks:
        title = re.search(r"\*\*(.+?)\*\*", body)
        scope = re.search(r"`\[([^\]]+)\]`", body)
        refs = re.search(r"\(([^()]*)\)\s*$", body.strip())
        cases, note = [], ""
        if refs:
            inner = refs.group(1)
            head, _, tail = inner.partition(";")
            for token in re.split(r"[,\s]+", head.strip()):
                if re.fullmatch(r"\d{2}b?|а\d{2}", token):
                    cases.append(token)
                elif re.fullmatch(r"а\d{2}–а\d{2}", token):
                    a, b = token.split("–")
                    cases += ["а%02d" % n for n in range(int(a[1:]), int(b[1:]) + 1)]
            note = tail.strip() or ("" if cases else inner.strip())
        rules.append({"number": number, "scope": scope.group(1) if scope else "—",
                      "title": (title.group(1) if title else "").rstrip(".").strip(),
                      "cases": cases, "note": note})
    return rules


def stand_cases():
    return {name: set(re.findall(r'^@case\("([^"]+)"', read(path), re.M))
            for name, path in STANDS.items()}


def audit_cards():
    from fot_planner.result_audit import CHECKS
    return [rule_id for rule_id, _name, _fn in CHECKS]


def mandatory_cards():
    from fot_planner.harness_local.catalogue import Catalogue
    return [c["requirement"] for c in Catalogue.load(ROOT).required_checks("fot.local")]


def report_conditions():
    text = read(REPORT_RULES)
    names = re.findall(r'_(?:limit|count)_rule\(\s*\n?\s*"([^"]+)"', text)
    names += re.findall(r'"правило":\s*"([^"]+)"', text)
    return list(dict.fromkeys(names))


def validation_codes():
    return sorted(set(re.findall(r'code="([A-Z0-9_]+)"', read(VALIDATION))))


# ── сборка матрицы ──────────────────────────────────────────────
def build():
    rules = parse_rules()
    cards, mandatory = audit_cards(), mandatory_cards()
    conditions, codes = report_conditions(), validation_codes()
    rows, linked_codes, linked_conditions, linked_cards = [], set(), set(), set()
    for r in rules:
        link = LINKS.get(r["number"], {})
        crisis = [c for c in r["cases"] if not c.startswith("а")]
        audit = [c for c in r["cases"] if c.startswith("а")]
        layers = {
            "input": {"codes": link.get("input", []),
                      "status": "implemented" if link.get("input") else "gap"},
            "solver": {"cases": crisis,
                       "status": "implemented" if crisis else "unverified"},
            "audit": {"cards": link.get("audit", []), "cases": audit,
                      "status": "implemented" if link.get("audit") else "gap"},
            "report": {"conditions": link.get("report", []),
                       "status": "implemented" if link.get("report") else "gap"},
        }
        gaps = [name for name, layer in layers.items() if layer["status"] != "implemented"]
        if r["note"]:
            gaps.append("свод: " + r["note"])
        rows.append({"rule": r["number"], "scope": r["scope"], "title": r["title"],
                     "source": link.get("source", ""), "layers": layers,
                     "cases": {"positive": r["cases"], "negative": link.get("negative", [])},
                     "gaps": gaps,
                     "verdict": "полный" if not gaps else "частичный"})
        linked_codes |= set(link.get("input", []))
        linked_conditions |= set(link.get("report", []))
        linked_cards |= set(link.get("audit", []))
    return {
        "schema_version": 1,
        "title": "Матрица применимых проверок (QA-001): правило свода → вход → решатель → аудит → случаи → представление",
        "source": {"rules": "docs/ПРАВИЛА_РЕШАТЕЛЯ.md",
                   "rules_sha256": hashlib.sha256(read(RULES).encode("utf-8")).hexdigest(),
                   "links": "scripts/qa_matrix.py::LINKS"},
        "layers": {"input": "валидация входа: src/fot_planner/validation.py",
                   "solver": "модель решателя, случаи harness/crisis.py",
                   "audit": "независимый аудит результата: src/fot_planner/result_audit.py, случаи harness/audit.py",
                   "report": "условия отчёта app/rules.py и список замечаний app/static/issues.js"},
        "statuses": {"implemented": "слой есть и проверяется", "unverified": "правило в модели по своду, случая нет",
                     "gap": "слоя нет — положительного заключения по этому слою нет"},
        "mandatory_for_result": mandatory,
        "rules": rows,
        "unmapped": {"input_codes": sorted(set(codes) - linked_codes),
                     "report_conditions": [c for c in conditions if c not in linked_conditions],
                     "audit_cards": [c for c in cards if c not in linked_cards]},
        "summary": {"rules": len(rows),
                    "audit_gap": sum(1 for r in rows if r["layers"]["audit"]["status"] == "gap"),
                    "report_gap": sum(1 for r in rows if r["layers"]["report"]["status"] == "gap"),
                    "input_gap": sum(1 for r in rows if r["layers"]["input"]["status"] == "gap"),
                    "solver_unverified": sum(1 for r in rows if r["layers"]["solver"]["status"] == "unverified"),
                    "full": sum(1 for r in rows if r["verdict"] == "полный")},
    }


def dump(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


# ── проверки ────────────────────────────────────────────────────
@case("т01", "Свод разобран: 25 правил подряд, у каждого название; связи LINKS ровно для них")
def _c01():
    out, rules = [], parse_rules()
    numbers = [r["number"] for r in rules]
    if numbers != list(range(1, len(numbers) + 1)) or len(numbers) < 25:
        out.append("номера правил: %s" % numbers)
    for r in rules:
        if not r["title"]:
            out.append("правило %d без названия" % r["number"])
    missing, extra = sorted(set(numbers) - set(LINKS)), sorted(set(LINKS) - set(numbers))
    if missing or extra:
        out.append("LINKS: нет %s, лишние %s" % (missing, extra))
    return out


@case("т02", "Случаи из свода и отрицательные случаи существуют в стендах и относятся к своему правилу")
def _c02():
    out, stands = [], stand_cases()
    for r in parse_rules():
        for c in r["cases"]:
            stand = "audit" if c.startswith("а") else "crisis"
            if c not in stands[stand]:
                out.append("правило %d: случая %s нет в %s" % (r["number"], c, stand))
        for c in LINKS.get(r["number"], {}).get("negative", []):
            stand = "audit" if c.startswith("а") else "crisis"
            if c not in stands[stand]:
                out.append("правило %d: отрицательного случая %s нет в %s" % (r["number"], c, stand))
            elif c not in r["cases"]:
                out.append("правило %d: отрицательный случай %s не упомянут в своде" % (r["number"], c))
    return out


@case("т03", "Карточки аудита в LINKS реализованы; обязательный набор профиля равен всем реализованным карточкам")
def _c03():
    out, cards = [], set(audit_cards())
    linked = {c for link in LINKS.values() for c in link.get("audit", [])}
    for c in sorted(linked - cards):
        out.append("карточки %s нет в result_audit" % c)
    for c in sorted(cards - linked):
        out.append("карточка %s не связана ни с одним правилом" % c)
    mandatory = set(mandatory_cards())
    if mandatory != cards:
        out.append("профиль profile.fot-local: %s, реализовано: %s" % (sorted(mandatory), sorted(cards)))
    return out


@case("т04", "Условия отчёта app/rules.py существуют и каждое связано с правилом свода")
def _c04():
    out, conditions = [], set(report_conditions())
    linked = {c for link in LINKS.values() for c in link.get("report", [])}
    for c in sorted(linked - conditions):
        out.append("условия «%s» нет в app/rules.py" % c)
    for c in sorted(conditions - linked):
        out.append("условие «%s» не связано ни с одним правилом" % c)
    return out


@case("т05", "Коды валидации входа в LINKS существуют; несвязанные коды перечислены в матрице явно")
def _c05():
    out, codes = [], set(validation_codes())
    linked = {c for link in LINKS.values() for c in link.get("input", [])}
    for c in sorted(linked - codes):
        out.append("кода %s нет в validation.py" % c)
    data = build()
    if set(data["unmapped"]["input_codes"]) != codes - linked:
        out.append("несвязанные коды в матрице не совпадают с фактом")
    return out


@case("т06", "Пробелы явные: у каждого правила без слоя есть запись gap; правила без единого слоя нет")
def _c06():
    out = []
    for r in build()["rules"]:
        statuses = {name: layer["status"] for name, layer in r["layers"].items()}
        for name, status in statuses.items():
            if status != "implemented" and name not in r["gaps"]:
                out.append("правило %d: слой %s без записи gap" % (r["rule"], name))
        if all(s != "implemented" for s in statuses.values()):
            out.append("правило %d: ни одного проверяемого слоя" % r["rule"])
        if r["verdict"] == "полный" and r["gaps"]:
            out.append("правило %d: «полный» при пробелах %s" % (r["rule"], r["gaps"]))
    return out


@case("т07", "docs/qa-matrix.json совпадает с матрицей, собранной из свода и кода сейчас")
def _c07():
    if not os.path.exists(MATRIX):
        return ["матрицы нет: соберите --write"]
    current = json.loads(read(MATRIX))
    fresh = build()
    if current != fresh:
        diff = [k for k in fresh if current.get(k) != fresh.get(k)]
        return ["матрица устарела, разошлись разделы: %s — пересоберите --write и проверьте изменение" % diff]
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="собрать docs/qa-matrix.json")
    ap.add_argument("--check", action="store_true", help="проверить связи и актуальность матрицы")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    if args.write:
        data = build()
        io.open(MATRIX, "w", encoding="utf-8", newline="").write(dump(data))
        s = data["summary"]
        print("записано %s: правил %d, полных %d, без аудита %d, без условия отчёта %d, "
              "без валидации входа %d, без случая решателя %d"
              % (os.path.relpath(MATRIX, ROOT), s["rules"], s["full"], s["audit_gap"],
                 s["report_gap"], s["input_gap"], s["solver_unverified"]))
        if not args.check:
            return 0
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    failed = total = 0
    for num, title, fn in CASES:
        if only and num not in only:
            continue
        try:
            bad = fn() or []
        except Exception as exc:  # noqa: BLE001 — сбой проверки тоже результат
            bad = ["ошибка проверки: %s: %s" % (type(exc).__name__, str(exc)[:200])]
        total += 1
        failed += 1 if bad else 0
        print("[%s] %-9s %s" % (num, "ПРОВАЛ" if bad else "ОК", title))
        for b in bad[:8]:
            print("      ✗", b)
    print("\nитого: %d случаев, провалов %d" % (total, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
