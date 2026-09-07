# -*- coding: utf-8 -*-
"""Работа агентов над загруженными документами.

Здесь живет то, что экономист видит в ленте: агент получает файл, определяет,
что это, разбирает и отчитывается. Разбор не изобретается заново — берется
рабочий код: якорный разбор РКМ и структур цены из ``extract`` и разбор
нормативных документов моделью из ``llm``.

Маршрутизация простая и намеренно объяснимая: по содержимому книги решается,
чей это документ — агента ввода данных или агента нормативной базы. Если
уверенности нет, документ не пропадает молча: агент задает вопрос.
"""
from __future__ import annotations

import json
import os
import re
import sys

import docread

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
# Разбор РКМ и структур цены уже написан и проверен на образцах экономистов.
sys.path.insert(0, os.path.join(ROOT, "docs", "ui"))

import extract          # noqa: E402
import llm              # noqa: E402
from agents import handoff, say, working  # noqa: E402
from db import (  # noqa: E402
    Contract, Correction, Document, Employee, Inflow, LaborRow, Proposal,
    Question, SecretAllowance, Substitution, now)

# Слова, по которым книга опознается как нормативный документ, а не как
# расчетно-калькуляционные материалы по договору.
SUBST_HINTS = ("исходная должность", "может быть замещена", "замещение",
               "взаимозаменяем")
NORM_HINTS = ("приказ", "оклад", "предельн", "положение об оплате",
              "должностной оклад", "2556", "тарифн")
CONTRACT_HINTS = ("ркм", "калькуляц", "структура цены", "трудоемк",
                  "договор", "шифр", "смета", "форма 1", "форма 9")


def _peek(path, limit=4000):
    """Начало документа текстом — чтобы понять, что это.

    Раньше умели заглядывать только в книги Excel, но нормативные документы
    приходят PDF и старым .doc, а решать, чей это документ, надо и по ним.
    """
    try:
        text = docread.to_text(path, limit)
    except docread.Unreadable:
        return "", None
    return text.lower(), None


#: Обработчик -> агент, который его выполняет. Правила замещения читает тот же
#: агент ввода данных, поэтому передачи между агентами там не возникает.
AGENT_OF = {"intake": "intake", "substitutions": "intake", "norms": "norms"}
AGENT_TITLE = {"intake": "Извлечение данных", "norms": "Нормативная база"}

#: Вид документа -> кто его обрабатывает. «штатное расписание» и документы по
#: договору идут одному агенту: разбор у них общий.
OWNER = {
    "документ по договору": "intake",
    "штатное расписание": "intake",
    "нормативный документ": "norms",
    "правила замещения должностей": "substitutions",
}


def classify_by_words(path):
    """Запасной способ: счет ключевых слов. Работает, когда модель недоступна."""
    text, _ = _peek(path)
    if not text:
        return "не прочитан", None, 0.0
    flat = re.sub(r"\s+", " ", text)          # в шапках попадаются переносы строк
    if sum(1 for w in SUBST_HINTS if w in flat) >= 2:
        return "правила замещения должностей", "substitutions", 1.0
    norm = sum(1 for w in NORM_HINTS if w in flat)
    contract = sum(1 for w in CONTRACT_HINTS if w in flat)
    if norm == contract == 0:
        return "не опознан", None, 0.0
    if norm > contract:
        return "нормативный документ", "norms", norm / (norm + contract)
    return "документ по договору", "intake", contract / (norm + contract)


def classify(path, filename=""):
    """(вид документа, чей он, чем определили, каша ли текст).

    Вид определяет модель: счет ключевых слов на настоящих документах путается
    — в приказе об оплате труда слово «договор» встречается не реже, чем в
    расчетно-калькуляционных материалах. Если модель недоступна или не смогла,
    остается счет слов.
    """
    res = llm.classify(path, filename)
    if res.get("ok"):
        kind = res["kind"]
        garbled = (res.get("garbled_why") or "").strip() if res.get("garbled") else None
        if res.get("garbled") and not garbled:
            # Модель не назвала причину — обходимся без нее, лишь бы не
            # повторять в сообщении одно и то же дважды.
            garbled = ""
        owner = None if kind == "иное" else OWNER.get(kind)
        return kind, owner, "модель " + res["model"], garbled

    if res.get("unavailable"):
        kind, owner, _ = classify_by_words(path)
        return kind, owner, "разбор по заголовкам", None

    # Файл не прочитан — про это надо сказать прямо, а не гадать по словам.
    return res.get("error") or "не прочитан", None, None, None


# ── правила замещения должностей ────────────────────────────────
def run_substitutions(db, case, doc):
    """Прочитать таблицу «кого кем можно заместить» и положить в дело.

    Эти правила направленные: главного инженера проекта можно заместить
    инженером, обратное неверно. Модель сервиса пока оперирует симметричными
    группами взаимозаменяемости, поэтому правила сохраняются и показываются,
    и уходят в расчет отдельным листом входного файла.
    """
    import openpyxl

    with working(db, case.id, "intake", "читает правила замещения «%s»" % doc.name) as w:
        wb = openpyxl.load_workbook(doc.path, data_only=True)
        ws = wb.worksheets[0]
        # Правила общие для организации: новая редакция заменяет прежнюю
        # целиком, а не добавляется к делу. Документ, который эти правила
        # принес раньше, после этого не отвечает ни за одну строку реестра —
        # помечаем его замененным. Иначе он остается в списке с пустой графой
        # «внесено в реестр» и выглядит как неудавшийся разбор.
        sources = {s.document_id for s in db.query(Substitution).all()
                   if s.document_id and s.document_id != doc.id}
        db.query(Substitution).delete()
        replaced = []
        for old_id in sources:
            prev = db.get(Document, old_id)
            if prev is not None and prev.state != "заменен":
                prev.state = "заменен"
                replaced.append(prev.name)
                if doc.supersedes_id is None:
                    doc.supersedes_id = prev.id
        pairs = 0
        for r in range(2, ws.max_row + 1):
            src = ws.cell(r, 1).value
            dst = ws.cell(r, 2).value
            if not src:
                continue
            db.add(Substitution(position=str(src).strip(),
                                replaced_by=str(dst).strip() if dst else "",
                                source=doc.name, document_id=doc.id))
            pairs += 1
        doc.state = "разобран"
        doc.kind = "правила замещения должностей"
        doc.parsed_by = "разбор по заголовкам"
        doc.summary = "правил %d" % pairs
        w["detail"] = doc.summary
        w["artifact"] = {"файл": doc.name, "правил замещения": pairs,
                         "куда записано": "нормативная база организации",
                         "заменил прежнюю редакцию": ", ".join(replaced) or None,
                         "применяется в расчете": True,
                         "как": "лист «правила_замещения» входного файла; "
                                "правила направленные — кого кем можно "
                                "заместить, не наоборот"}
        unknown = _unknown_positions(db, doc.id)
        if unknown:
            w["artifact"]["не сопоставлено со справочником"] = unknown
        db.commit()

    if unknown:
        # Правило с незнакомым названием не срабатывает и никак себя не
        # проявляет: расчет проходит, просто замещение не применяется. Об этом
        # надо сказать сразу, пока экономист держит документ в руках.
        say(db, case.id,
            "В правилах есть должности, которых нет в справочнике: %s. Такие "
            "правила в расчете не сработают — название должно совпадать со "
            "справочником. Проверьте написание в документе."
            % ", ".join("«%s»" % u for u in unknown[:8]), agent="intake",
            document_id=doc.id)
        db.commit()

    say(db, case.id,
        "Прочитал «%s»: %d %s замещения должностей. Записал в нормативную базу "
        "организации — правила общие для всех дел, перезагружать их в каждое "
        "не нужно. Правила направленные: слева должность сотрудника, справа "
        "должности, которые ему можно дать дополнительно. В расчет уходят: "
        "работу по такой должности сотрудник выполнить может, обратное — нет."
        % (doc.name, pairs, _plural(pairs, "правило", "правила", "правил"))
        + (" Прежняя редакция правил (%s) помечена замененной."
           % ", ".join("«%s»" % n for n in replaced) if replaced else ""),
        document_id=doc.id,
        agent="intake")
    db.commit()


def _excel_date(v):
    """Даты в книгах приходят порядковым номером — приводим к читаемому виду."""
    if v is None or v == "":
        return None
    s = str(v).strip()
    try:
        n = float(s)
    except ValueError:
        return s                      # уже строка вида 01.01.2026
    if not (1 <= n <= 80000):
        return s
    import datetime as _dt
    # Excel считает от 30.12.1899: 1900 год ошибочно високосный, эпоха сдвинута.
    return (_dt.date(1899, 12, 30) + _dt.timedelta(days=int(n))).strftime("%d.%m.%Y")


def _store_passport(db, case, passport, doc):
    """Разложить разобранное по реестрам организации.

    Ни штатка, ни договоры не принадлежат плану: договор заключается на
    несколько лет и обслуживает столько же планов, штатка меняется приказами,
    а не с каждым расчетом. Поэтому строки общие, а план на год берет из
    реестра то, что в этом году действует.

    Строки заменяются в пределах одного документа: повторная загрузка того же
    файла обновляет только то, что из него пришло, и не трогает остальное.
    """
    for model in (Employee, Contract, LaborRow, Inflow, SecretAllowance, Substitution):
        db.query(model).filter_by(document_id=doc.id).delete()
    # Файл по шаблону читаем загрузчиком решателя — тем же кодом, что читает
    # входной файл расчета. Тогда в реестр попадают все графы контракта по
    # построению: сроки и тип договора, разрешения на виды выплат и на
    # совместительство, подразделение и тип занятости, поступления по
    # месяцам, трудоемкость, надбавки, правила замещения. Разбор по
    # заголовкам брал шесть граф из двадцати, и расчет из реестра расходился
    # с расчетом по самому файлу: договор «с июня» действовал весь год.
    ctx = _solver_context(doc.path)
    if ctx is not None:
        return _store_context(db, ctx, doc)
    for e in passport.get("employees") or []:
        db.add(Employee(code=str(e.get("code") or ""),
                        fio=e.get("fio"), position=e.get("pos"),
                        department=e.get("department"),
                        employment_type=e.get("employment_type"),
                        employment_category=e.get("employment_category"),
                        rate=e.get("rate"), salary=e.get("sal"),
                        date_from=_excel_date(e.get("from")),
                        date_to=_excel_date(e.get("to")),
                        source=doc.name, document_id=doc.id))
    for c in passport.get("contracts") or []:
        kinds = c.get("kinds")
        db.add(Contract(code=str(c.get("code") or ""),
                        name=c.get("name"), number=c.get("num"), kind=c.get("type"),
                        account=c.get("account"), department=c.get("department"),
                        goz=c.get("goz"), fund=c.get("fot"),
                        kinds=", ".join(kinds) if isinstance(kinds, list) else kinds,
                        priority=c.get("priority"), allow_main=c.get("allow_main"),
                        allow_part_time=c.get("allow_part_time"),
                        salary_deadline=_excel_date(c.get("salary_deadline")),
                        allowance_deadline=_excel_date(c.get("allowance_deadline")),
                        date_from=_excel_date(c.get("from")),
                        date_to=_excel_date(c.get("to")),
                        source=doc.name, document_id=doc.id))
    inflows = 0
    year = _num(passport.get("settings", {}).get("год")) if passport.get("settings") else None
    for code, months in (passport.get("inflow") or {}).items():
        for m, amount in enumerate(months or [], start=1):
            if amount:
                db.add(Inflow(contract_code=str(code), year=int(year) if year else None,
                              month=m, amount=float(amount),
                              source=doc.name, document_id=doc.id))
                inflows += 1
    # Трудоёмкость из форм: Ф9 и «Расшифровка ФОТ» дают строки по должностям.
    for lp in passport.get("labor") or []:
        if not lp.get("person_months"):
            continue
        db.add(LaborRow(contract_code=str(lp.get("contract") or ""),
                        year=int(lp["year"]) if lp.get("year") else (int(year) if year else None),
                        position=lp.get("position") or None,
                        person_months=lp.get("person_months"),
                        avg_cost=lp.get("avg_cost"), headcount=lp.get("headcount"),
                        source=doc.name, document_id=doc.id))
    db.commit()
    counts = {"сотрудников": len(passport.get("employees") or []),
              "договоров": len(passport.get("contracts") or []),
              "поступлений": inflows,
              "строк трудоемкости": len([x for x in passport.get("labor") or []
                                         if x.get("person_months")])}
    return {k: v for k, v in counts.items() if v}


def _solver_context(path):
    """Прочитать книгу загрузчиком решателя; None, если это не полный шаблон."""
    try:
        from fot_planner.excel.load import load_context
        return load_context(path)
    except Exception:  # noqa: BLE001 — не шаблон целиком: остается разбор по заголовкам
        return None


#: Разрешения договора → вид выплаты, как он записан в реестре.
_KIND_FLAGS = (("allow_salary", "оклад"), ("allow_secret", "120"),
               ("allow_allowance", "122"), ("allow_incentive", "124"),
               ("allow_extra_work", "152"), ("allow_order_incentive", "приказ"))
#: Значения решателя → слова реестра. Обратно их переводит сам загрузчик.
_EMPLOYMENT_RU = {"auto": "по расчету", "main": "основное", "part_time": "совместительство"}
_CATEGORY_RU = {"regular": "основной", "student": "студент", "graduate_student": "аспирант"}


def _store_context(db, ctx, doc):
    """Разложить прочитанное загрузчиком решателя по реестрам организации."""
    def d(v):
        return v.strftime("%d.%m.%Y") if v else None

    def yn(v):
        return "да" if v else "нет"

    counts = {}
    for e in ctx.employees:
        db.add(Employee(code=e.id, fio=e.full_name, position=e.position,
                        department=e.department or None, rate=e.rate,
                        salary=e.monthly_wage,
                        employment_type=_EMPLOYMENT_RU.get(e.employment_type),
                        employment_category=_CATEGORY_RU.get(e.employment_category),
                        allowed_contracts=", ".join(e.allowed_contracts) or None,
                        forbidden_contracts=", ".join(e.forbidden_contracts) or None,
                        date_from=d(e.start_date), date_to=d(e.end_date),
                        source=doc.name, document_id=doc.id))
    counts["сотрудников"] = len(ctx.employees)
    inflows = 0
    for c in ctx.contracts:
        kinds = [name for flag, name in _KIND_FLAGS if getattr(c, flag)]
        db.add(Contract(code=c.id, name=c.name or None, number=c.number or None,
                        kind=c.contract_type or None, account=c.account or None,
                        department=c.department or None,
                        goz=yn(c.is_goz_defense_order), fund=c.total_fot,
                        kinds=", ".join(kinds), priority=yn(c.priority_payment_mode),
                        allow_main=yn(c.allow_main_employment),
                        allow_part_time=yn(c.allow_part_time),
                        salary_deadline=d(c.salary_payment_deadline),
                        allowance_deadline=d(c.allowances_payment_deadline),
                        date_from=d(c.start_date), date_to=d(c.end_date),
                        source=doc.name, document_id=doc.id))
        for b in c.monthly_budgets or []:
            if b.inflow_amount:
                db.add(Inflow(contract_code=c.id, year=b.year, month=b.month,
                              amount=b.inflow_amount, source=doc.name, document_id=doc.id))
                inflows += 1
    counts["договоров"] = len(ctx.contracts)
    counts["поступлений"] = inflows
    for lp in ctx.labor_plans or []:
        # Группа эквивалентности — вычисленная строка вида «3.247н / группа 3»,
        # а графа «номер группы» в шаблоне числовая: в реестр идет только то,
        # что было в документе.
        db.add(LaborRow(contract_code=lp.contract_id, year=lp.year,
                        position=lp.position,
                        position_level=(lp.position_level
                                        if isinstance(lp.position_level, int) else None),
                        person_months=lp.person_months,
                        avg_cost=lp.avg_monthly_labor_cost,
                        headcount=getattr(lp, "headcount", None),
                        source=doc.name, document_id=doc.id))
    counts["строк трудоемкости"] = len(ctx.labor_plans or [])
    for s in ctx.secret_allowances or []:
        db.add(SecretAllowance(employee_code=s.employee_id, contract_code=s.secret_contract_id,
                               rate=s.rate, source=doc.name, document_id=doc.id))
    counts["надбавок 120"] = len(ctx.secret_allowances or [])
    for position, extra in (ctx.substitution_rules or {}).items():
        if extra:
            db.add(Substitution(position=position, replaced_by=", ".join(sorted(extra)),
                                source=doc.name, document_id=doc.id))
    counts["правил замещения"] = sum(1 for v in (ctx.substitution_rules or {}).values() if v)
    db.commit()
    return counts


#: Какие форматы читает каждый обработчик. Разбор документов по договору и
#: правил замещения читает ячейки книги: PDF и .doc туда отдавать бессмысленно.
FORMATS = {
    "intake": (".xlsx", ".xlsm", ".xls"),
    "substitutions": (".xlsx", ".xlsm", ".xls"),
    "norms": (".xlsx", ".xlsm", ".xls", ".pdf", ".doc", ".docx"),
}


def _wrong_format(doc, owner):
    """Человеческая причина, если формат обработчику не подходит."""
    ext = os.path.splitext(doc.path)[1].lower()
    allowed = FORMATS.get(owner, ())
    if not allowed or ext in allowed:
        return None
    return ("файл %s, а такие документы читаются только из %s"
            % (ext or "без расширения", ", ".join(allowed)))


# ── документ незнакомой формы ───────────────────────────────────
def _known(db, entity, f):
    """Уже есть такая строка в реестре? Тогда предлагать ее незачем."""
    if entity == "сотрудник":
        code = str(f.get("code") or "").strip()
        fio = str(f.get("fio") or "").strip()
        rows = db.query(Employee).all()
        return any((code and (e.code or "").strip().lower() == code.lower())
                   or (fio and (e.fio or "").strip().lower() == fio.lower())
                   for e in rows)
    if entity == "договор":
        code = str(f.get("code") or "").strip()
        return bool(code) and any((c.code or "").strip().lower() == code.lower()
                                  for c in db.query(Contract).all())
    if entity == "трудоемкость":
        code = str(f.get("contract") or "").strip().lower()
        pos = str(f.get("position") or "").strip().lower()
        return any((r.contract_code or "").strip().lower() == code
                   and (r.position or "").strip().lower() == pos
                   for r in db.query(LaborRow).all())
    if entity == "поступление":
        code = str(f.get("contract") or "").strip().lower()
        return any((r.contract_code or "").strip().lower() == code
                   and r.month == f.get("month") for r in db.query(Inflow).all())
    if entity == "надбавка 120":
        who = str(f.get("employee") or "").strip().lower()
        return any((r.employee_code or "").strip().lower() == who
                   for r in db.query(SecretAllowance).all())
    if entity == "должность":
        # Должность считается известной, только если она уже в справочнике и
        # документ не приносит по ней других величин: перечень должностей
        # обычно совпадает со справочником целиком, а вот новая редакция
        # приказа меняет оклад — про это молчать нельзя.
        import reference
        from fot_planner.position_reference import normalize_position
        name = normalize_position(str(f.get("pos") or "").strip())
        if not name:
            return True
        for row in reference.read_rows():
            if normalize_position(row["pos"]) != name:
                continue
            for key in ("sal", "p2556", "p4"):
                v = f.get(key)
                if v is not None and row.get(key) != v:
                    return False        # величина расходится — предложить
            cat = str(f.get("cat") or "").strip()
            if cat and str(row.get("cat") or "").strip() != cat:
                return False
            return True
        return False                     # должности в справочнике нет
    pos = str(f.get("position") or "").strip().lower()
    return bool(pos) and any((s.position or "").strip().lower() == pos
                             for s in db.query(Substitution).all())


def correction_hints(db, kind, limit=8):
    """Правки экономиста по документам этого вида — строками для подсказки.

    Последние важнее: если экономист поправлял одно и то же дважды, второй
    раз он был точнее. Правка без верного значения тоже полезна — «это
    неверно» уже отсекает вариант.
    """
    q = db.query(Correction)
    if kind:
        q = q.filter(Correction.document_kind == kind)
    rows = q.order_by(Correction.id.desc()).limit(limit).all()
    out = []
    for c in rows:
        what = c.entity or "строка"
        if c.right and c.wrong:
            out.append("%s: было %s — верно %s%s" % (
                what, c.wrong[:160], c.right[:160],
                (" (%s)" % c.note[:120]) if c.note else ""))
        elif c.wrong:
            out.append("%s: %s — неверно%s" % (
                what, c.wrong[:160], (", %s" % c.note[:120]) if c.note else ""))
        elif c.note:
            out.append("%s: %s" % (what, c.note[:200]))
    return out


def propose_entities(db, case, doc):
    """Прочитать документ без шаблона и предложить найденное экономисту.

    Записать прочитанное прямо в реестр нельзя: у формы не по шаблону величина
    лежит не там, где ее ждут, и модель может взять оклад из соседней графы.
    Расчет от этого не упадет — он выдаст правдоподобный неверный план, а это
    ГОЗ. Поэтому строки кладутся в отдельную таблицу и ждут, пока экономист
    их посмотрит: до подтверждения их нет ни в реестре, ни в паспорте дела,
    ни во входном файле решателя.

    Возвращает True, если что-то предложено.
    """
    with working(db, case.id, "intake", "перечитывает «%s» без шаблона" % doc.name) as w:
        hints = correction_hints(db, doc.kind)
        res = llm.freeform(doc.path, doc.name, hints=hints)
        db.query(Proposal).filter_by(document_id=doc.id, state="предложено").delete()

        if not res.get("ok"):
            w["detail"] = res.get("error") or "не разобрал"
            db.commit()
            return False

        buckets = (("employees", "сотрудник"), ("contracts", "договор"),
                   ("labor", "трудоемкость"), ("inflows", "поступление"),
                   ("secret", "надбавка 120"),
                   ("substitutions", "правило замещения"),
                   ("positions", "должность"))
        counts, skipped = {}, 0
        grades = {"надежно": 0, "проверить": 0, "сомнительно": 0}
        ctx = _grade_context(db, res)
        for key, entity in buckets:
            counts[entity] = 0
            for item in res.get(key) or []:
                where = item.pop("место", None)
                if _known(db, entity, item):
                    # Знакомый обработчик уже записал эту строку — предлагать
                    # ее второй раз значит просить подтвердить проверенное.
                    skipped += 1
                    continue
                g, why = grade(entity, item, where, ctx)
                grades[g] += 1
                db.add(Proposal(document_id=doc.id, entity=entity,
                                payload=json.dumps(item, ensure_ascii=False),
                                evidence=str(where)[:400] if where else None,
                                grade=g, reason=why or None))
                counts[entity] += 1
        total = sum(counts.values())
        w["detail"] = ("предложено %d, уже в реестре %d" % (total, skipped)
                       if total or skipped else "сверх разобранного ничего")
        w["artifact"] = {"файл": doc.name, "прочитано частями": res.get("parts"),
                         "учтено правок экономиста": len(hints),
                         "предложено строк": total,
                         "из них надежных": grades["надежно"],
                         "проверить": grades["проверить"],
                         "сомнительных": grades["сомнительно"],
                         "пропущено как уже известные": skipped,
                         "в расчет пойдет": "только после подтверждения"}
        # Обрыв ответа по длине больше не теряет часть документа, но молчать
        # о нем нельзя: часть строк из обрезанного куска могла не дойти.
        if res.get("обрезано частей"):
            w["artifact"]["ответ обрывался по длине, частей"] = res["обрезано частей"]
        if total:
            doc.state = "ждет подтверждения"
            doc.summary = ", ".join("%s %d" % (k, v) for k, v in counts.items() if v)
        db.commit()

    if not total:
        return False

    tail = ""
    if grades["сомнительно"]:
        tail = (" %s — сверьте с документом особенно внимательно."
                % _px(grades["сомнительно"], "строка сомнительная",
                      "строки сомнительные", "строк сомнительных"))
    say(db, case.id,
        "В «%s» нашлось сверх разобранного: %s. В реестр и в расчет это пока "
        "не пошло — откройте документ в реестре, проверьте строки и "
        "подтвердите те, что верны. У каждой написано, откуда она взята и "
        "насколько ей можно верить.%s"
        % (doc.name, doc.summary, tail), agent="intake", document_id=doc.id)
    db.commit()
    return True


#: Обычные пределы оклада, когда справочник пуст. Величина вне их — не
#: обязательно ошибка, но почти всегда соседняя графа: сумма за год, ФОТ
#: договора, табельный номер. При заполненном справочнике верхний предел —
#: два с половиной наибольших оклада из него: годовая сумма его не пройдет.
_SALARY_RANGE = (10_000, 3_000_000)


def _px(n, one, few, many):
    d, h = n % 10, n % 100
    word = many if 11 <= h <= 14 else one if d == 1 else few if 2 <= d <= 4 else many
    return "%d %s" % (n, word)


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(" ", "").replace("\xa0", "").replace(",", "."))
    except ValueError:
        return None


def _money(v):
    return "{:,.0f}".format(v).replace(",", " ")


def _grade_context(db, res):
    """Что считается известным при оценке строк одного документа.

    Договор из того же документа — известный: структура цены приносит и
    договор, и его трудоемкость разом, и вторая не должна считаться
    сомнительной из-за того, что первый еще не подтвержден.
    """
    import reference
    from fot_planner.position_reference import normalize_position

    def low(v):
        return str(v or "").strip().lower()

    ref = reference.read_rows()
    positions = {normalize_position(r["pos"]) for r in ref if r.get("pos")}
    top = max([_num(r.get("sal")) or 0 for r in ref] or [0])
    salary = (_SALARY_RANGE[0], max(top * 2.5, 300_000) if top else _SALARY_RANGE[1])
    contracts = {low(c.code) for c in db.query(Contract).all() if c.code}
    contracts |= {low(c.get("code")) for c in (res.get("contracts") or [])
                  if c.get("code")}
    employees = set()
    for e in db.query(Employee).all():
        employees |= {low(e.code), low(e.fio)}
    for e in res.get("employees") or []:
        employees |= {low(e.get("code")), low(e.get("fio"))}
    employees.discard("")
    return {"positions": positions, "contracts": contracts, "employees": employees,
            "salary": salary, "norm": normalize_position, "low": low}


def grade(entity, f, evidence, ctx):
    """Насколько предложенной строке можно верить — и почему.

    Не самооценка модели: она уверена всегда одинаково. Это проверки, которые
    экономист сделал бы первыми: обязательные графы на месте, величины в
    обычных пределах, ссылки ведут на то, что есть в реестре и справочнике,
    указано, откуда строка взята.

    «надежно» — все сошлось; «проверить» — противоречий нет, но что-то не
    сошлось или не указано; «сомнительно» — дыра в обязательной графе или
    противоречие с реестром. Сомнительные строки в карточке не отмечены
    заранее, остальные отмечены. В реестр все равно попадает только то, что
    экономист принял: оценка подсказывает, куда смотреть, а не решает.
    """
    bad, doubt = [], []
    low, norm = ctx["low"], ctx["norm"]
    lo, hi = ctx.get("salary", _SALARY_RANGE)

    def pos_known(name):
        return not name or norm(str(name).strip()) in ctx["positions"]

    if entity == "сотрудник":
        for k, name in (("fio", "ФИО"), ("position", "должность"), ("salary", "оклад")):
            if f.get(k) in (None, ""):
                bad.append("нет графы «%s»" % name)
        s = _num(f.get("salary"))
        if s is not None and not lo <= s <= hi:
            bad.append("оклад %s вне обычных пределов (до %s)" % (_money(s), _money(hi)))
        r = _num(f.get("rate"))
        if r is not None and not 0 < r <= 2:
            bad.append("ставка %s вне обычных пределов" % r)
        if not pos_known(f.get("position")):
            doubt.append("должности «%s» нет в справочнике" % f.get("position"))
    elif entity == "договор":
        if not f.get("code"):
            bad.append("нет шифра договора")
        fot = _num(f.get("fot"))
        if fot is not None and fot <= 0:
            bad.append("ФОТ не положительный")
        if not f.get("from") or not f.get("to"):
            doubt.append("нет сроков договора — подставится плановый год")
        if not f.get("type") and f.get("goz") in (None, ""):
            doubt.append("не указано, ГОЗ это или нет")
    elif entity == "трудоемкость":
        if low(f.get("contract")) not in ctx["contracts"]:
            bad.append("договора «%s» нет в реестре" % f.get("contract"))
        pm = _num(f.get("person_months"))
        if pm is None or pm <= 0:
            bad.append("нет человеко-месяцев")
        if not pos_known(f.get("position")):
            doubt.append("должности «%s» нет в справочнике" % f.get("position"))
    elif entity == "поступление":
        if low(f.get("contract")) not in ctx["contracts"]:
            bad.append("договора «%s» нет в реестре" % f.get("contract"))
        m = _num(f.get("month"))
        if m is None or not 1 <= m <= 12:
            bad.append("месяц %s вне 1–12" % f.get("month"))
        a = _num(f.get("amount"))
        if a is None or a <= 0:
            bad.append("нет суммы")
        y = _num(f.get("year"))
        if y is not None and not 2000 <= y <= 2100:
            doubt.append("год %s выглядит неверно" % f.get("year"))
    elif entity == "надбавка 120":
        if low(f.get("employee")) not in ctx["employees"]:
            bad.append("сотрудника «%s» нет в реестре" % f.get("employee"))
        if low(f.get("contract")) not in ctx["contracts"]:
            bad.append("договора «%s» нет в реестре" % f.get("contract"))
        r = _num(f.get("rate"))
        if r is None or not 0 < r <= 1:
            bad.append("доля надбавки %s вне 0–1" % f.get("rate"))
    elif entity == "правило замещения":
        for k in ("position", "replaced_by"):
            for name in str(f.get(k) or "").split(","):
                if name.strip() and not pos_known(name):
                    doubt.append("должности «%s» нет в справочнике" % name.strip())
    elif entity == "должность":
        s = _num(f.get("salary_for_rate"))
        if s is not None and not lo <= s <= hi:
            bad.append("оклад %s вне обычных пределов (до %s)" % (_money(s), _money(hi)))
        if not f.get("category"):
            doubt.append("нет категории")
    if not evidence:
        doubt.append("не указано, откуда взято")
    g = "сомнительно" if bad else "проверить" if doubt else "надежно"
    return g, "; ".join(bad + doubt)


def _ask_kind(db, case, doc):
    """Спросить вид документа — когда прочитать содержимое не удалось."""
    db.add(Question(
        case_id=case.id, agent="intake",
        text="Что за документ «%s»? Прочитал его, но по содержанию это не "
             "похоже ни на один вид, с которым я работаю." % doc.name,
        options=json.dumps(["документ по договору", "нормативный документ",
                            "правила замещения должностей",
                            "не нужен, удалить"], ensure_ascii=False)))
    say(db, case.id,
        "Прочитал «%s», но не понял, что это за документ, и строк из него не "
        "достал. Подскажите вид — разберу заново." % doc.name, agent="intake",
        document_id=doc.id)
    db.commit()


def _unknown_positions(db, document_id):
    """Замещающие должности, которых нет в справочнике.

    Такое правило не срабатывает и молчит: расчет проходит, замещение просто
    не применяется. Причина обычно в написании — «Инженер 1 кат.» вместо
    «Инженер 1 категории» — или в опечатке источника.

    Варианты «в другом подразделении» не считаем ошибкой: это та же должность,
    и обычная строка с этим названием правило уже покрывает.
    """
    import reference
    from fot_planner.position_reference import normalize_position

    known = {normalize_position(r["pos"]) for r in reference.read_rows()}
    bad = []
    for s in db.query(Substitution).filter_by(document_id=document_id).all():
        for name in str(s.replaced_by or "").replace(";", ",").split(","):
            name = name.strip()
            if not name or "в другом подразделении" in name.lower():
                continue
            if normalize_position(name) not in known and name not in bad:
                bad.append(name)
    return bad


# ── агент 1: данные договоров ───────────────────────────────────
def run_intake(db, case, doc):
    """Разобрать документ по договору и доложить в ленту."""
    with working(db, case.id, "intake", "разбирает «%s»" % doc.name) as w:
        out = extract.extract(doc.path)
        passport = out["passport"]
        case.passport = json.dumps(passport, ensure_ascii=False)
        doc.state = "разобран"
        counts = _store_passport(db, case, passport, doc)
        emp, ctr = counts.get("сотрудников", 0), counts.get("договоров", 0)
        doc.summary = ", ".join("%s %d" % (k, v) for k, v in counts.items() if v)
        w["detail"] = doc.summary
        w["artifact"] = {"файл": doc.name, **counts,
                         "настроек расчета": len(passport.get("settings") or {}),
                         "прочитано в документе": out.get("log") or [],
                         "спорных значений": len(out.get("questions") or []),
                         "готово к расчету": bool((out.get("ready") or {}).get("ok"))}
        db.commit()

    # Готовность считается по реестру организации, а не по одному документу:
    # штатка и договоры приходят разными файлами, и после каждого сервис
    # раньше жаловался, что «не хватает сотрудников».
    missing = []
    if not db.query(Employee).count():
        missing.append("сотрудники")
    if not db.query(Contract).count():
        missing.append("договоры")
    if not db.query(Inflow).count():
        missing.append("поступления по месяцам")
    ready = {"ok": not missing, "missing": missing,
             "hint": "Загрузите документ с этими данными." if missing else ""}
    say(db, case.id,
        "Разобрал «%s»: %s.%s" % (doc.name, doc.summary or "строк реестра нет",
                                  (" Настройки расчета — из этого файла."
                                   if passport.get("settings") else "")),
        agent="intake", document_id=doc.id,
        payload={"kind": "passport", "employees": emp, "contracts": ctr,
                 "log": out.get("log") or []})

    for q in (out.get("questions") or [])[:5]:
        text, opts = _question_text(q)
        db.add(Question(case_id=case.id, agent="intake", text=text,
                        options=json.dumps(opts, ensure_ascii=False) if opts else None))
    db.commit()

    if ready.get("ok"):
        # Предложение считать — один раз, когда данных стало достаточно; после
        # каждого следующего документа оно повторялось в ленте.
        was_ready = case.stage in ("готово к расчету", "посчитано")
        case.stage = "готово к расчету"
        if not was_ready:
            say(db, case.id, "Данных достаточно для расчета. Могу считать.",
                agent="intake", payload={"kind": "offer_solve"})
    else:
        missing = ", ".join(ready.get("missing") or []) or "часть показателей"
        say(db, case.id, "Для расчета не хватает: %s. %s" % (missing, ready.get("hint") or ""),
            agent="intake")
    db.commit()


# ── агент 7: нормативная база ───────────────────────────────────
def run_norms(db, case, doc):
    """Сверить нормативный документ со справочником и предложить изменения."""
    import reference

    with working(db, case.id, "norms", "сверяет «%s» со справочником" % doc.name) as w:
        res = llm.parse(doc.path, doc.name)
        if res.get("ok"):
            rows, by = res["rows"], "модель " + res["model"]
        elif docread.kind_of(doc.path) in (".xlsx", ".xlsm", ".xls"):
            # Запасной разбор читает ячейки книги — PDF и .doc ему не по зубам.
            rows, by = reference.rows_by_anchors(doc.path), "разбор по заголовкам"
        else:
            raise ValueError(res.get("error") or "документ не разобран")
        changes, unknown, seen = reference.compare(rows)
        doc.state = "разобран"
        doc.parsed_by = by
        doc.summary = "расхождений %d" % len(changes)
        w["detail"] = doc.summary
        w["artifact"] = {"файл": doc.name, "чем разобрано": by,
                         "извлечено величин": len(rows or []),
                         "величины": sorted(seen) or [],
                         "основание": res.get("basis"),
                         "действует с": res.get("effective_from"),
                         "расхождений со справочником": len(changes),
                         "должностей вне справочника": len(unknown),
                         "примечание модели": res.get("notes")}
        db.commit()

    if not changes:
        say(db, case.id,
            "Сверил «%s» со справочником — расхождений нет." % doc.name,
            agent="norms", document_id=doc.id)
        db.commit()
        return

    say(db, case.id,
        "В «%s» нашел %d %s со справочником организации. Он общий для всех дел, "
        "поэтому изменения затронут и другие планы. Показываю, что изменится; "
        "запишу только после вашего подтверждения."
        % (doc.name, len(changes), _plural(len(changes), "расхождение", "расхождения", "расхождений")),
        agent="norms", document_id=doc.id,
        payload={"kind": "reference_diff", "changes": changes, "unknown": unknown,
                 "basis": res.get("basis"), "effective_from": res.get("effective_from"),
                 "by": by})
    db.commit()


def _question_text(q):
    """Вопрос разборщика — во фразу, которую можно прочитать.

    extract возвращает объект с полями «где нашли», «что было в документе» и
    «почему не сошлось». Показывать его как есть нельзя: экономист видит
    словарь вместо вопроса.
    """
    if isinstance(q, str):
        return q, None
    if not isinstance(q, dict):
        return str(q), None
    parts = []
    if q.get("field"):
        parts.append("%s" % q["field"])
    if q.get("raw"):
        parts.append("в документе: %s" % q["raw"])
    if q.get("why"):
        parts.append(str(q["why"]))
    text = ". ".join(parts) if parts else (q.get("text") or str(q))
    return text, q.get("options")


def _plural(n, one, few, many):
    d, h = n % 10, n % 100
    if 11 <= h <= 14:
        return many
    if d == 1:
        return one
    if 2 <= d <= 4:
        return few
    return many


def handle_document(db, case, doc):
    """Определить, чей документ, и передать нужному агенту."""
    with working(db, case.id, "intake", "определяет вид «%s»" % doc.name) as w:
        kind, owner, by, garbled = classify(doc.path, doc.name)
        w["detail"] = "текст нечитаемый" if garbled is not None else kind
        w["artifact"] = {"файл": doc.name, "формат": docread.kind_of(doc.path),
                         "размер, байт": doc.size, "определен вид": kind,
                         "чем определен": by or "не удалось прочитать",
                         "передан агенту": AGENT_TITLE.get(AGENT_OF.get(owner), "—")}
        if garbled is not None:
            w["artifact"]["текст нечитаемый"] = garbled or "да"
        db.commit()

    # Плохой текстовый слой опаснее его отсутствия: скан честно говорит, что не
    # читается, а PDF, собранный чужим распознаванием, выглядит прочитанным и
    # подсовывает кашу — «МИН ИСТRJ>Сrво». Извлекать из нее величины нельзя:
    # получится правдоподобная неправда, а это ГОЗ. Останавливаемся здесь.
    if garbled is not None:
        doc.state = "текст нечитаемый"
        doc.kind = kind
        doc.parsed_by = by
        doc.summary = ("текст в файле нечитаемый — похоже, это распознанный "
                       "скан плохого качества")
        db.commit()
        say(db, case.id,
            "«%s» открылся, но текст в нем нечитаемый — похоже, это "
            "распознанный скан плохого качества%s. Извлекать величины из "
            "такого текста я не стану: выйдет правдоподобная неправда. "
            "Приложите документ в текстовом виде или введите величины вручную."
            % (doc.name, ": " + garbled if garbled else ""), agent="intake",
            document_id=doc.id)
        db.commit()
        return

    # Два разных отказа, и путать их нельзя. Файл, который не читается,
    # не станет читаемым от того, что экономист назовет его вид: спрашивать
    # тут нечего, надо сказать, чем помочь. Вопрос уместен только когда
    # документ прочитан, а вид непонятен — тогда ответ меняет обработчик.
    if by is None:
        doc.state = "не прочитан"
        doc.summary = kind                      # без by в kind лежит причина
        db.commit()
        say(db, case.id, "Не смог прочитать «%s». %s" % (doc.name, kind),
            agent="intake", document_id=doc.id)
        db.commit()
        return

    if owner is None:
        # Форма незнакомая — это не повод не читать документ. Раньше здесь
        # разбор кончался вопросом «что это?», и документ не давал ничего.
        doc.kind = kind
        doc.parsed_by = by
        doc.state = "не распознан"
        db.commit()
        if not propose_entities(db, case, doc):
            _ask_kind(db, case, doc)
        return

    doc.kind = kind
    doc.parsed_by = by
    db.commit()

    # Формат проверяем до разбора. Иначе openpyxl роняет PDF и его английское
    # «does not support .pdf file format» уходит прямо в реестр — финансисту
    # это не сообщение, а шум.
    bad = _wrong_format(doc, owner)
    if bad:
        doc.state = "не распознан"
        doc.summary = bad
        db.commit()
        say(db, case.id,
            "«%s»: %s. Прочитаю его без шаблона." % (doc.name, bad), agent="intake",
            document_id=doc.id)
        db.commit()
        if not propose_entities(db, case, doc):
            doc.summary = "%s; сверх этого данных не нашлось" % bad
            db.commit()
        return

    target = AGENT_OF.get(owner, owner)
    if target != "intake":
        handoff(db, case.id, "intake", target,
                "«%s» — это %s, передаю агенту «%s»."
                % (doc.name, kind, AGENT_TITLE.get(target, target)),
                payload={"kind": "handoff", "document": doc.name, "as": kind})

    try:
        if owner == "norms":
            run_norms(db, case, doc)
        elif owner == "substitutions":
            run_substitutions(db, case, doc)
        else:
            run_intake(db, case, doc)
        # Вид документа не обещает, что в нем нет ничего сверх этого вида.
        # Служебная записка проходит как нормативный документ, а внутри —
        # трое сотрудников и договор, и знакомый обработчик их не заметит.
        # Поэтому после него документ читается еще раз, уже без шаблона.
        propose_entities(db, case, doc)
    except Exception as exc:  # noqa: BLE001 — сообщение вместо падения фона
        # Текст исключения — для ленты и карточки, но не для графы реестра:
        # там нужно состояние, а не английская диагностика библиотеки.
        doc.state = "не распознан"
        doc.summary = "разбор не удался"
        db.commit()
        say(db, case.id, "Не смог разобрать «%s»: %s" % (doc.name, exc), agent=owner,
            document_id=doc.id)
        db.commit()
