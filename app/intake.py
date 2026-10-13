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
    Case, Contract, ContractValue, Correction, Document, Employee, Inflow, LaborRow, Proposal,
    Question, SalaryMonth, SalaryRow, SecretAllowance, Substitution, WorkOrder, WorkOrderMember,
    now)
import workgroup  # noqa: E402
import facts  # noqa: E402

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
AGENT_OF = {"intake": "intake", "substitutions": "intake", "norms": "norms",
            "positions": "norms", "salary": "intake", "workgroup": "intake",
            "facts": "intake"}
AGENT_TITLE = {"intake": "Извлечение данных", "norms": "Нормативная база"}

#: Вид документа -> кто его обрабатывает. «штатное расписание» и документы по
#: договору идут одному агенту: разбор у них общий.
OWNER = {
    "документ по договору": "intake",
    "штатное расписание": "intake",
    "нормативный документ": "norms",
    "правила замещения должностей": "substitutions",
    "перечень должностей": "positions",
    "зарплата по месяцам": "salary",
    workgroup.KIND: "workgroup",
    facts.KIND: "facts",
}

SALARY_KIND = "зарплата по месяцам"
POSITIONS_KIND = "перечень должностей"
#: Коды категорий персонала, как они стоят в справочнике должностей.
CATEGORY_CODES = ("НТП", "НР", "АУП", "ППС", "ПП")
MONTH_WORDS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
               "сентябрь", "октябрь", "ноябрь", "декабрь")
#: Начала названий месяцев в шапке: «январь», «янв», «янв.», «Январь 2026».
_MONTH_STEMS = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт",
                "ноя", "дек")


def _month_of_header(text):
    t = str(text or "").strip().lower()
    if t.startswith("мая"):
        return 5
    for i, stem in enumerate(_MONTH_STEMS):
        if t.startswith(stem):
            return i + 1
    return None


def salary_layout(path):
    """Где в книге таблица зарплаты по месяцам: лист, строка шапки, графы
    сотрудника и месяцев. None — книга не такая.

    Опознаётся по шапке, а не моделью: графа сотрудника (код строки,
    табельный номер или ФИО) и не меньше шести граф с названиями месяцев.
    Штатное расписание с одной графой «Зарплата» сюда не попадает."""
    if os.path.splitext(path)[1].lower() not in (".xlsx", ".xlsm"):
        return None
    import openpyxl
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception:  # noqa: BLE001 — не книга: пусть решает общий разбор
        return None
    try:
        for ws in wb.worksheets:
            for r, values in enumerate(ws.iter_rows(max_row=12, values_only=True), start=1):
                months, code, fio, dept = {}, None, None, None
                for c, v in enumerate(values or ()):
                    head = str(v or "").strip().lower()
                    month = _month_of_header(head)
                    if month and month not in months.values():
                        months[c] = month
                    elif "управленческ" in head or "подразделение управ" in head:
                        # Управленческое подразделение строки (01.10.2026).
                        dept = c if dept is None else dept
                    elif "код строки" in head or "табельн" in head:
                        code = c if code is None else code
                    elif "фио" in head or "ф.и.о" in head or head == "сотрудник":
                        fio = c if fio is None else fio
                if len(months) >= 6 and (code is not None or fio is not None):
                    return {"sheet": ws.title, "row": r, "code": code, "fio": fio,
                            "months": months, "dept": dept}
        return None
    finally:
        wb.close()


def positions_layout(path):
    """Строки перечня должностей [(должность, категория)]; None — книга не такая.

    Узнаётся по форме, а не моделью (SOURCE-013): первый лист, в графе A
    должность, в графе B код категории персонала. Шапка необязательна — в
    файле организации её нет. Читается только первый лист, как у правил
    замещения: книга с обоими листами не должна молча стать перечнем."""
    if os.path.splitext(path)[1].lower() not in (".xlsx", ".xlsm"):
        return None
    import openpyxl
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception:  # noqa: BLE001 — не книга: пусть решает общий разбор
        return None
    try:
        rows, other = [], 0
        for values in wb.worksheets[0].iter_rows(values_only=True):
            values = tuple(values or ())
            name = str(values[0]).strip() if values and values[0] is not None else ""
            cat = (str(values[1]).strip().upper()
                   if len(values) > 1 and values[1] is not None else "")
            if not name and not cat:
                continue
            if name and cat in CATEGORY_CODES:
                rows.append((name, cat))
            else:
                other += 1
        # Одна строка не по форме — шапка. Больше — это другая таблица, где
        # категория во второй графе встретилась случайно.
        return rows if len(rows) >= 3 and other <= 1 else None
    finally:
        wb.close()


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


def template_kind(path):
    """Распознавание известной формы через закреплённый локальный каталог.

    Права задаёт сервис для конкретного уже выбранного документа. Это не
    новый HTTP-вход с произвольным путём. Неизвестная форма возвращает None
    и продолжает прежнюю классификацию, ошибки каталога не скрываются.
    """
    from pathlib import Path
    from fot_planner.harness_local.catalogue import Authority, Task, default_catalogue
    from fot_planner.harness_local.document_tools import template_kind as handler

    authority = Authority(permissions=frozenset({'document.read'}),
                          resources=frozenset({'python', 'openpyxl'}),
                          files=(Path(path),))
    result = default_catalogue().invoke(
        Task(kind='document.template_kind', domain='fot.local'),
        {'path': str(path)}, authority, {'document.template_kind': handler})
    return result['kind']


def classify(path, filename=""):
    """(вид документа, чей он, чем определили, каша ли текст).

    Вид определяет модель: счет ключевых слов на настоящих документах путается
    — в приказе об оплате труда слово «договор» встречается не реже, чем в
    расчетно-калькуляционных материалах. Если модель недоступна или не смогла,
    остается счет слов. Книгу по шаблону сервиса узнаём по листам сами.
    """
    by_template = template_kind(path)
    if by_template is not None:
        return by_template, OWNER[by_template], "разбор по листам шаблона", None
    # Факт начислений (форма 6.3 «ШР на дату, детализация назначений» или
    # плоская «факт_выплат», 30.09.2026) — по шапке и раньше зарплаты по
    # месяцам: полная выгрузка отчёта сервиса содержит и лист 6.1 с графами
    # месяцев, и лист 6.3 — это факт, а не зарплата.
    if facts.layout(path) is not None:
        return facts.KIND, OWNER[facts.KIND], "разбор по графам факта", None
    # Таблица зарплаты по месяцам узнаётся по шапке: модель её вида не знает
    # и отдала бы штатному расписанию, где сумма у строки одна.
    if salary_layout(path) is not None:
        return SALARY_KIND, OWNER[SALARY_KIND], "разбор по графам месяцев", None
    if positions_layout(path) is not None:
        return POSITIONS_KIND, OWNER[POSITIONS_KIND], "разбор по графам перечня", None
    # Приказ о рабочей группе узнаётся по форме: заголовок и таблица состава с
    # табельными номерами. Модель такого вида не знает (28.09.2026).
    if docread.kind_of(path) in (".doc", ".docx", ".pdf"):
        try:
            text = docread.to_text(path)
        except Exception:  # noqa: BLE001 — не прочитался: решит модель
            text = ""
        if isinstance(text, tuple):
            text = text[0]
        if workgroup.looks_like(text):
            return workgroup.KIND, OWNER[workgroup.KIND], "разбор по форме приказа", None
    res = llm.classify(path, filename)
    # Каша в текстовом слое — не приговор: у PDF есть сами страницы. Читаем
    # их картинками и определяем вид заново по распознанному тексту.
    if res.get("ok") and res.get("garbled") and docread.kind_of(path) == ".pdf":
        scanned = docread.scan_text(path)
        if scanned:
            again = llm.classify_text(scanned, filename)
            if again.get("ok") and not again.get("garbled"):
                res = again
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


# ── перечень должностей ─────────────────────────────────────────
def run_positions(db, case, doc):
    """Перечень должностей с категориями — основа справочника (SOURCE-013).

    Приказы и справки правят величины только у должностей, которые в
    справочнике уже есть. Перечень заводит недостающие и ставит категорию.
    Должности, которых в перечне нет, не удаляются, а называются: молча
    удалить строку справочника — потерять её величины.
    """
    import reference
    from fot_planner.position_reference import normalize_position

    with working(db, case.id, "norms", "читает перечень должностей «%s»" % doc.name) as w:
        rows = positions_layout(doc.path) or []
        added, changed = [], []
        for name, cat in rows:
            what, diff = reference.upsert_position(name, cat=cat)
            if what == "заведена":
                added.append(name)
            elif diff:
                changed.append("%s: %s → %s" % (name, diff[0][1] or "—", cat))
        listed = {normalize_position(name) for name, _ in rows}
        extra = []
        for r in reference.read_rows():
            if normalize_position(r["pos"]) not in listed and r["pos"] not in extra:
                extra.append(r["pos"])
        doc.state = "разобран"
        doc.kind = POSITIONS_KIND
        doc.parsed_by = "разбор по графам перечня"
        doc.summary = "должностей %d: заведено %d, категория изменена у %d" % (
            len(rows), len(added), len(changed))
        doc.gave = json.dumps({"сверен": True}, ensure_ascii=False)
        w["detail"] = doc.summary
        w["artifact"] = {"файл": doc.name, "должностей в перечне": len(rows),
                         "заведено": added or None, "категория изменена": changed or None,
                         "в справочнике, но не в перечне": extra or None,
                         "куда записано": "справочник должностей"}
        db.commit()
    parts = ["Перечень должностей «%s»: %s." % (doc.name, doc.summary)]
    if extra:
        parts.append("В справочнике есть должности, которых нет в перечне: %s. "
                     "Я их не удалял." % ", ".join(extra))
    say(db, case.id, " ".join(parts), agent="norms", document_id=doc.id)
    db.commit()


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
        # Заменяются только правила из документов. Правила, введённые или
        # исправленные на вкладке реестра, остаются, и для их должностей
        # правило из файла не пишется: ручное важнее (SOURCE-010). До
        # 18.09.2026 новый файл стирал все правила целиком.
        db.query(Substitution).filter(Substitution.document_id.isnot(None)).delete(
            synchronize_session=False)
        manual = {s.position for s in db.query(Substitution).all()}
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
            if not src or str(src).strip() in manual:
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


# ── зарплата по месяцам ─────────────────────────────────────────
def _key(text):
    return re.sub(r"\s+", " ", str(text or "").replace("ё", "е").replace("Ё", "Е")).strip().lower()


def run_salary_months(db, case, doc):
    """Прочитать таблицу зарплаты по месяцам (решение пользователя 19.09.2026).

    Строка файла — сотрудник штатного расписания: по коду строки или
    табельному номеру, иначе по ФИО; графы месяцев — суммы. Год — год плана,
    в который загружен файл. Новый файл заменяет строки и суммы прежних
    файлов этого года; суммы, исправленные на вкладке, остаются — ручное
    важнее, как у правил замещения (SOURCE-010)."""
    import openpyxl
    import actuality

    year = case.year
    layout = salary_layout(doc.path)
    if layout is None:
        raise ValueError("в книге нет графы сотрудника и граф месяцев")
    with working(db, case.id, "intake", "читает зарплату по месяцам «%s»" % doc.name) as w:
        by_code, by_person, by_fio = {}, {}, {}
        for e in db.query(Employee).all():
            by_code[_key(e.code)] = e
            if e.person_code:
                by_person.setdefault(_key(e.person_code), []).append(e)
            if e.fio:
                by_fio.setdefault(_key(e.fio), []).append(e)
        # Прежний файл мог дать только суммы: строку до него завела правка в
        # ячейке. Поэтому источники — и у строк, и у сумм.
        earlier = {r.document_id for model in (SalaryRow, SalaryMonth)
                   for r in db.query(model).filter_by(year=year).all()
                   if r.document_id and r.document_id != doc.id}
        db.query(SalaryMonth).filter(SalaryMonth.year == year,
                                     SalaryMonth.document_id.isnot(None)).delete(
            synchronize_session=False)
        db.query(SalaryRow).filter(SalaryRow.year == year,
                                   SalaryRow.document_id.isnot(None)).delete(
            synchronize_session=False)
        # Управленческие подразделения (01.10.2026): графа файла называет
        # подразделение строки; у человека строк столько, сколько подразделений.
        from db import MgmtDepartment
        depts = {d.name.lower(): d for d in db.query(MgmtDepartment).all()}

        def dept_of(raw):
            name = " ".join(str(raw or "").split())[:200]
            if not name:
                return None
            d = depts.get(name.lower())
            if d is None:
                d = MgmtDepartment(name=name)
                db.add(d)
                db.flush()
                depts[name.lower()] = d
            return d.id

        rows_by = {(r.employee_code, r.mgmt_department_id): r
                   for r in db.query(SalaryRow).filter_by(year=year).order_by(SalaryRow.id).all()}
        manual = {(m.salary_row_id, m.month)
                  for m in db.query(SalaryMonth).filter_by(year=year).all()}
        replaced = []
        for old_id in earlier:
            prev = db.get(Document, old_id)
            if prev is not None and prev.state != "заменен":
                prev.state = "заменен"
                replaced.append(prev.name)
                if doc.supersedes_id is None:
                    doc.supersedes_id = prev.id

        wb = openpyxl.load_workbook(doc.path, data_only=True, read_only=True)
        try:
            ws = wb[layout["sheet"]]
            rows, sums, unknown, seen, zeros, twice = 0, 0, [], set(), [], []
            for values in ws.iter_rows(min_row=layout["row"] + 1, values_only=True):
                cell = lambda c: values[c] if c is not None and c < len(values) else None  # noqa: E731
                code, fio = cell(layout["code"]), cell(layout["fio"])
                if code in (None, "") and fio in (None, ""):
                    continue
                e = by_code.get(_key(code)) if code not in (None, "") else None
                if e is None and code not in (None, ""):
                    same = by_person.get(_key(code)) or []
                    e = same[0] if len(same) == 1 else None
                if e is None and fio not in (None, ""):
                    same = by_fio.get(_key(fio)) or []
                    e = same[0] if len(same) == 1 else None
                if e is None:
                    unknown.append(str(fio or code).strip())
                    continue
                dept = dept_of(cell(layout.get("dept"))) if layout.get("dept") is not None else None
                if (e.code, dept) in seen:
                    # Одно и то же подразделение у человека дважды — вторая
                    # строка файла не читается, об этом скажет лента.
                    twice.append("%s — %s" % (e.fio or e.code, cell(layout.get("dept")) or "без подразделения"))
                    continue
                seen.add((e.code, dept))
                rows += 1
                srow = rows_by.get((e.code, dept))
                if srow is None and dept is None:
                    # Без графы подразделения — первая строка человека.
                    srow = next((r for (c, _d), r in rows_by.items() if c == e.code), None)
                if srow is None:
                    srow = SalaryRow(employee_code=e.code, year=year, source=doc.name,
                                     document_id=doc.id, mgmt_department_id=dept)
                    db.add(srow)
                    db.flush()
                    rows_by[(e.code, dept)] = srow
                for c, month in layout["months"].items():
                    amount = _num(cell(c))
                    if amount is None or amount < 0 or (srow.id, month) in manual:
                        continue
                    if amount == 0:
                        # Ноль пока запрещён (решение пользователя 19.09.2026):
                        # месяц без оклада среди оплачиваемых делает план
                        # нерешаемым. Такой месяц остаётся пустым.
                        zeros.append("%s — %s" % (e.fio or e.code, MONTH_WORDS[month - 1]))
                        continue
                    db.add(SalaryMonth(employee_code=e.code, year=year, month=month,
                                       amount=amount, source=doc.name, document_id=doc.id,
                                       salary_row_id=srow.id))
                    sums += 1
        finally:
            wb.close()
        doc.state = "разобран"
        doc.kind = SALARY_KIND
        doc.parsed_by = "разбор по графам месяцев"
        doc.summary = "строк %d, сумм %d" % (rows, sums)
        w["detail"] = doc.summary
        w["artifact"] = {"файл": doc.name, "год": year, "строк": rows, "сумм по месяцам": sums,
                         "не найдено в штатном расписании": unknown or None,
                         "заменил прежнюю редакцию": ", ".join(replaced) or None,
                         "куда записано": "вкладка «Заработная плата по месяцам»",
                         "применяется в расчете": "лист «зарплата_по_месяцам» входного файла"}
        # Зарплата года уходит в расчёт каждого плана этого года.
        years = {c.id: c.year for c in db.query(Case).all()}
        actuality.mark_review(db, [r for r in actuality.results(db)
                                   if years.get(r.case_id) == year],
                              "загружена зарплата по месяцам «%s»" % doc.name)
        db.commit()

    say(db, case.id,
        "Прочитал «%s»: зарплата по месяцам %d года для %d %s, %d %s. Суммы, "
        "исправленные на вкладке «Заработная плата по месяцам», остались как были."
        % (doc.name, year, rows, _plural(rows, "строки", "строк", "строк"), sums,
           _plural(sums, "сумма", "суммы", "сумм"))
        + (" Прежняя редакция (%s) помечена замененной."
           % ", ".join("«%s»" % n for n in replaced) if replaced else ""),
        document_id=doc.id, agent="intake")
    if unknown:
        say(db, case.id,
            "В штатном расписании не нашёл: %s. Их суммы в расчёт не ушли — "
            "проверьте табельный номер или ФИО в файле."
            % ", ".join("«%s»" % u for u in unknown[:8]), document_id=doc.id, agent="intake")
    if twice:
        say(db, case.id,
            "Подразделение у человека повторяется — вторую строку не взял: %s."
            % ", ".join(twice[:8]), document_id=doc.id, agent="intake")
    if zeros:
        say(db, case.id,
            "Нулевые суммы не принял: %s. Ноль в месяце пока не допускается — "
            "эти месяцы остались пустыми, задайте сумму на вкладке."
            % ", ".join(zeros[:8]), document_id=doc.id, agent="intake")
    db.commit()


# ── факт начислений ─────────────────────────────────────────────
def run_facts(db, case, doc):
    """Прочитать факт начислений (решение пользователя 30.09.2026).

    Год — год плана, в который загружен файл. Факт общий для всех планов
    года и уходит в расчёт каждого из них за те месяцы, что в нём есть,
    поэтому к пересмотру помечаются расчёты всех планов года."""
    import actuality

    year = case.year
    with working(db, case.id, "intake", "читает факт начислений «%s»" % doc.name) as w:
        rows, skipped = facts.parse(doc.path, year, uploaded=doc.uploaded, name=doc.name)
        got = facts.store(db, case, doc, rows)
        doc.state = "разобран"
        doc.kind = facts.KIND
        doc.parsed_by = "разбор по графам факта"
        doc.summary = "%s: строк %d" % (facts.months_text(got["месяцы"]) or "месяцев нет",
                                         got["строк"])
        w["detail"] = doc.summary
        w["artifact"] = {"файл": doc.name, "год": year,
                         "месяцы": facts.months_text(got["месяцы"]) or None,
                         "строк": got["строк"], "сотрудников": got["сотрудников"],
                         "договоров": got["договоров"],
                         "не найдено в штатном расписании":
                             got["не найдено в штатном расписании"] or None,
                         "не найдены договоры": got["не найдены договоры"] or None,
                         "не прочитано": skipped[:20] or None,
                         "заменил прежнюю редакцию": ", ".join(got["заменены"]) or None,
                         "куда записано": "вкладка реестра «Факт начислений»",
                         "применяется в расчете": "лист «факт_выплат» входного файла: "
                                                  "эти месяцы расчёт не пересчитывает"}
        runs = []
        for c in db.query(Case).filter_by(year=year).all():
            runs += actuality.runs_of_case(db, c.id)
        actuality.mark_review(db, runs, "загружен факт начислений «%s»" % doc.name)
        db.commit()

    say(db, case.id,
        "Прочитал «%s»: факт начислений %d года за %s — %d %s, %d %s, %d %s."
        % (doc.name, year, facts.months_text(got["месяцы"]) or "—", got["строк"],
           _plural(got["строк"], "строка", "строки", "строк"), got["сотрудников"],
           _plural(got["сотрудников"], "сотрудник", "сотрудника", "сотрудников"),
           got["договоров"], _plural(got["договоров"], "договор", "договора", "договоров"))
        + (" Прежняя редакция (%s) помечена замененной."
           % ", ".join("«%s»" % n for n in got["заменены"]) if got["заменены"] else ""),
        document_id=doc.id, agent="intake")
    if got["не найдены договоры"]:
        say(db, case.id,
            "Не нашёл договоры: %s. Строки с ними в расчёт не пойдут — проверьте шифр "
            "или лицевой счёт в файле." % ", ".join("«%s»" % u for u in got["не найдены договоры"][:8]),
            document_id=doc.id, agent="intake")
    if got["не найдено в штатном расписании"]:
        say(db, case.id,
            "В штатном расписании не нашёл: %s. Их деньги с договоров в расчёте учтены, "
            "а сами они в плане не участвуют."
            % ", ".join("«%s»" % u for u in got["не найдено в штатном расписании"][:8]),
            document_id=doc.id, agent="intake")
    if skipped:
        say(db, case.id, "Не прочитал строк: %d (%s)." % (len(skipped), "; ".join(skipped[:5])),
            document_id=doc.id, agent="intake")
    db.commit()


# ── приказ о рабочей группе ────────────────────────────────────────────
def _find_contract(db, p):
    """Договор приказа: по лицевому счёту, иначе по номеру соглашения.

    Оба ключа написаны людьми, поэтому сравниваются без пробелов и регистра.
    Не нашлось или нашлось больше одного — None: выбирает экономист."""
    ctrs = db.query(Contract).all()
    for key, get in ((p.get("счёт"), lambda c: c.account),
                     (p.get("соглашение"), lambda c: c.number)):
        k = workgroup.norm_account(key)
        if not k:
            continue
        hit = {c.code for c in ctrs if workgroup.norm_account(get(c)) == k}
        if len(hit) == 1:
            return hit.pop()
    return None


def _find_row(db, member):
    """Строка штатного расписания члена группы: табельный номер и должность.

    Рабочая группа — связка «человек + должность», а не человек: у одного
    табельного номера бывают две строки (основное место и совместительство).
    Однозначно не нашлось — None, строку выбирает экономист."""
    rows = [e for e in db.query(Employee).all()
            if (e.person_code or e.code) == member.get("табельный")]
    if len(rows) == 1:
        return rows[0].code
    pos = workgroup.norm_position(member.get("должность"))
    same = [e for e in rows if workgroup.norm_position(e.position) == pos]
    return same[0].code if len(same) == 1 else None


def rebind_workorders(db):
    """Приказы без договора — к договору, который появился в реестре позже.

    Приказ, загруженный раньше договора, иначе так и висел бы без договора:
    договор ищется по л/с и номеру соглашения только при разборе. Поэтому
    после каждой загрузки и правки договора непривязанные приказы
    перепроверяются — как лицевой счёт в штатке становится ссылкой, когда
    появляется договор (решение пользователя 28.09.2026). Договор, выбранный
    экономистом, не трогается: перепроверяются только пустые."""
    n = 0
    for o in db.query(WorkOrder).filter(WorkOrder.contract_code.is_(None)).all():
        code = _find_contract(db, {"счёт": o.account, "соглашение": o.agreement})
        if not code:
            continue
        o.contract_code = code
        d = db.get(Document, o.document_id) if o.document_id else None
        if d is not None and not d.contract_code:
            d.contract_code = code
        n += 1
    if n:
        db.commit()
    return n


def run_workgroup(db, case, doc):
    """Приказ о рабочей группе — в реестр: приказ, договор, состав по строкам."""
    from docread import to_text
    text = to_text(doc.path)
    if isinstance(text, tuple):
        text = text[0]
    p = workgroup.parse(text)
    for old in db.query(WorkOrder).filter_by(document_id=doc.id).all():
        db.query(WorkOrderMember).filter_by(order_id=old.id).delete()
        db.delete(old)
    base = p.get("основание") or {}
    # Приказ, загруженный внутрь договора (или привязанный из очереди), держит
    # этот договор: экономист выбрал его сам. По лицевому счёту и номеру
    # соглашения договор ищется, только когда приказ пришёл без него.
    chosen = doc.contract_code or None
    order = WorkOrder(kind=p["вид"], number=p.get("номер"), date=p.get("дата"),
                      base_number=base.get("номер"), base_date=base.get("дата"),
                      contract_code=chosen or _find_contract(db, p),
                      agreement=p.get("соглашение"),
                      agreement_date=p.get("соглашение от"), topic=p.get("тема"),
                      account=p.get("счёт"), kfo=p.get("кфо"), date_from=p.get("с"),
                      date_to=p.get("по"), leader=p.get("руководитель"),
                      source=doc.name, document_id=doc.id)
    db.add(order)
    db.flush()
    lost = 0
    for m in p.get("состав") or []:
        code = _find_row(db, m)
        lost += code is None
        db.add(WorkOrderMember(order_id=order.id, n=m.get("n"), action=m.get("действие"),
                               fio=m.get("фио"), person_code=m.get("табельный"),
                               position=m.get("должность"), department=m.get("подразделение"),
                               work=m.get("работы"),
                               leader="да" if m.get("фио") == p.get("руководитель") else None,
                               employee_code=code))
    # «разобран» — общее состояние разобранного документа (на экране —
    # «Обработан»); своё слово показывалось бы сырым и строчной буквой.
    doc.state = "разобран"
    doc.kind = workgroup.KIND
    doc.contract_code = order.contract_code
    doc.summary = workgroup.summary(p)
    db.commit()
    # Чего не нашлось — сказано прямо и где это выбрать; молча не угадываем.
    # Пересказа приказа в ленте нет (29.09.2026): каждое пересказанное поле —
    # лишнее место для ошибки, а прочитанное видно в самом документе.
    n = len(p.get("состав") or [])
    people = "%d %s" % (n, _px(n, "человек", "человека", "человек"))
    todo = []
    if order.contract_code is None:
        todo.append("договор не найден — укажите его в меню строки приказа во «Документах»")
    if lost:
        todo.append("%d %s в штатном расписании — выберите во вкладке «Рабочая группа» договора"
                    % (lost, _px(lost, "не найден", "не найдены", "не найдены")))
    say(db, case.id,
        "Прочитал «%s»: %s%s." % (
            doc.name, people,
            "; " + "; ".join(todo) if todo else " в группе договора %s" % order.contract_code),
        agent="intake", document_id=doc.id)
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


def _day(v):
    """Дата из ответа модели или ячейки — в date; None, если это не дата."""
    import datetime as _dt

    if v is None or v == "":
        return None
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    s = str(_excel_date(v) or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _labor_rows_monthly(rows, default_year):
    """Строки трудоёмкости из ответа модели — в план по месяцам.

    Модель отдаёт строку на каждый этап с датами начала и окончания либо
    сразу двенадцать чисел по месяцам. Этап раскладываем поровну по его
    месяцам: «4 чел.-мес. с июня по сентябрь» — по 1,0 в каждом. Этапы одной
    должности на одном договоре складываем в одну строку: план по месяцам
    сам говорит, когда что закрывать, отдельная сущность «этап» не нужна;
    средняя стоимость взвешивается по чел.-мес. Строки без дат остаются
    годовыми и не складываются — как раньше.
    """
    out, merged = [], {}
    for lp in rows:
        pm = _num(lp.get("person_months"))
        if not pm:
            continue
        year = (int(lp["year"]) if lp.get("year")
                else (int(default_year) if default_year else None))
        months = None
        raw = lp.get("months")
        if isinstance(raw, dict) and any(_num(v) for v in raw.values()):
            months = {str(int(k)): round(float(_num(v)), 4)
                      for k, v in raw.items() if _num(v)}
            pm = round(sum(months.values()), 4)
        elif isinstance(raw, list) and len(raw) == 12 and any(_num(v) for v in raw):
            months = {str(m): round(float(_num(v)), 4)
                      for m, v in enumerate(raw, start=1) if _num(v)}
            pm = round(sum(months.values()), 4)
        else:
            d1, d2 = _day(lp.get("from")), _day(lp.get("to"))
            if d1 and d2 and d2 >= d1:
                span = [(y, m) for y in range(d1.year, d2.year + 1) for m in range(1, 13)
                        if (d1.year, d1.month) <= (y, m) <= (d2.year, d2.month)]
                if year is None:
                    year = d1.year
                each = pm / len(span)
                months = {str(m): round(each, 4) for y, m in span if y == year}
                if not months:
                    continue                      # этап целиком в другом году
                pm = round(sum(months.values()), 4)
        row = {"contract": str(lp.get("contract") or ""), "year": year,
               "position": lp.get("position") or None, "person_months": pm,
               "avg_cost": _num(lp.get("avg_cost")), "headcount": _num(lp.get("headcount")),
               "months": months,
               "details": [dict(x) for x in (lp.get("details") or [])
                           if isinstance(x, dict)]}
        if not months:
            out.append(row)
            continue
        key = (row["contract"], year, (row["position"] or "").strip().lower())
        prev = merged.get(key)
        if prev is None:
            merged[key] = row
            out.append(row)
            continue
        for m, v in months.items():
            prev["months"][m] = round(prev["months"].get(m, 0.0) + v, 4)
        total = prev["person_months"] + pm
        if prev["avg_cost"] and row["avg_cost"]:
            prev["avg_cost"] = round((prev["avg_cost"] * prev["person_months"]
                                      + row["avg_cost"] * pm) / total, 2)
        elif row["avg_cost"]:
            prev["avg_cost"] = row["avg_cost"]
        prev["person_months"] = round(total, 4)
        if row["headcount"] and (not prev["headcount"] or row["headcount"] > prev["headcount"]):
            prev["headcount"] = row["headcount"]
        prev.setdefault("details", []).extend(row.get("details") or [])
    return out


def _adopt_contract(db, passport, doc):
    """Договор из шапки документа, если файл брошен в реестр без договора.

    SOURCE-005: экономист не вводит шифр руками. Пока ввод был обязателен,
    пачка файлов ложилась в «Документы без договора» и ждала, когда человек
    перепечатает шифр, который в этих же файлах и написан.

    Наименование ставится только пустому договору. Уже заданное имя следующий
    документ не переписывает: у пачки одного договора темы формулируются
    по-разному, и последний загруженный молча переименовывал бы договор.
    """
    hint = passport.get("contract_hint") or {}
    code = str(hint.get("code") or "").strip()[:60]
    if not code or getattr(doc, "contract_code", None):
        return None
    subject = (hint.get("subject") or "").strip()[:300] or None
    found = db.query(Contract).filter_by(code=code).first()
    if found is None:
        db.add(Contract(code=code, name=subject,
                        source="шифр из документа «%s»" % doc.name, document_id=doc.id))
    elif subject and not found.name:
        found.name = subject
    # Документ уезжает в каталог этого договора: иначе он остаётся «без
    # договора» и заведённый договор выглядит пустым.
    doc.contract_code = code
    from db import record_decision
    from fot_planner.harness_local import decisions as D
    record_decision(db, kind="данные", subject_kind="договор", subject_id=code,
                    subject_digest=D.digest(code, subject), scope="реестр",
                    action={"договор из документа": code, "наименование": subject,
                            "документ": doc.name},
                    grounds="шифр и тема из шапки документа (SOURCE-005)")
    return code


def _store_passport(db, case, passport, doc):
    """Разложить разобранное по реестрам организации.

    Ни штатка, ни договоры не принадлежат плану: договор заключается на
    несколько лет и обслуживает столько же планов, штатка меняется приказами,
    а не с каждым расчетом. Поэтому строки общие, а план на год берет из
    реестра то, что в этом году действует.

    Строки заменяются в пределах одного документа: повторная загрузка того же
    файла обновляет только то, что из него пришло, и не трогает остальное.
    """
    for model in (Employee, Contract, LaborRow, Inflow, SecretAllowance, Substitution,
                  ContractValue):
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
    _adopt_contract(db, passport, doc)
    for e in passport.get("employees") or []:
        db.add(Employee(code=str(e.get("code") or ""),
                        person_code=str(e.get("person_code") or e.get("code") or ""),
                        fio=e.get("fio"), position=e.get("pos"),
                        department=e.get("department"),
                        employment_type=e.get("employment_type"),
                        employment_category=e.get("employment_category"),
                        rate=e.get("rate"), salary=e.get("sal"),
                        allowed_contracts=e.get("allowed") or None,
                        forbidden_contracts=e.get("forbidden") or None,
                        # Лицевой счёт сотрудника: графа есть не во всяком
                        # штатном расписании, поэтому берём как есть и не
                        # требуем (28.09.2026).
                        account=e.get("account") or e.get("лицевой счет") or None,
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
    # Трудоёмкость из форм: Ф9 и «Расшифровка ФОТ» дают строки по должностям;
    # этапы одной должности складываются в один план по месяцам.
    labor_rows = _labor_rows_monthly(passport.get("labor") or [], year)
    for lp in labor_rows:
        db.add(LaborRow(contract_code=lp["contract"], year=lp["year"],
                        position=lp["position"], person_months=lp["person_months"],
                        avg_cost=lp["avg_cost"], headcount=lp["headcount"],
                        months=(json.dumps(lp["months"], ensure_ascii=False)
                                if lp["months"] else None),
                        details=(json.dumps(lp["details"], ensure_ascii=False)
                                 if lp.get("details") else None),
                        source=doc.name, document_id=doc.id))
    # Величины пакета договора: ФОТ периодов, нормативы года, срок (SOURCE-004).
    # Договор документа — тот, в который его загрузили; иначе шифр из шапки.
    for v in passport.get("values") or []:
        db.add(ContractValue(contract_code=str(v.get("contract") or ""), field=v["field"],
                             year=v.get("year"), stage=v.get("stage"), value=v.get("value"),
                             text=v.get("text"), unit=v.get("unit"), label=v.get("label"),
                             place=v.get("place"), source=doc.name, document_id=doc.id))
    # Надбавка 120 из листа «120_надбавка» штатки: без неё решатель не знает,
    # кому гостайна обязательна, и план по такому человеку выйдет неверным.
    for s in passport.get("secret") or []:
        if not (s.get("employee") and s.get("contract")):
            continue
        db.add(SecretAllowance(employee_code=str(s["employee"]),
                               secret_contract_code=str(s["contract"]),
                               rate=float(s.get("rate") or 0.05),
                               source=doc.name, document_id=doc.id))
    # Строки договоров созданы заново — ручной ввод экономиста возвращается
    # поверх них (SOURCE-007): документ его не отменяет.
    import basis as B
    B.reapply_manual(db)
    db.commit()
    counts = {"сотрудников": len(passport.get("employees") or []),
              "договоров": len(passport.get("contracts") or []),
              "поступлений": inflows,
              "строк трудоемкости": len(labor_rows),
              "надбавок 120": len(passport.get("secret") or []),
              "величин пакета": len(passport.get("values") or [])}
    source_labor = sum(len(row.get("details") or []) or 1 for row in labor_rows)
    if source_labor > len(labor_rows):
        counts["исходных строк трудоемкости"] = source_labor
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
        db.add(Employee(code=e.id, person_code=e.person_id or e.id,
                        fio=e.full_name, position=e.position,
                        department=e.department or None, rate=e.rate,
                        salary=e.monthly_wage,
                        employment_type=_EMPLOYMENT_RU.get(e.employment_type),
                        employment_category=_CATEGORY_RU.get(e.employment_category),
                        account=getattr(e, "account", None),
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
                        months=(json.dumps({str(m): v for m, v in lp.monthly.items()})
                                if getattr(lp, "monthly", None) else None),
                        source=doc.name, document_id=doc.id))
    counts["строк трудоемкости"] = len(ctx.labor_plans or [])
    for s in ctx.secret_allowances or []:
        db.add(SecretAllowance(employee_code=s.employee_id, secret_contract_code=s.secret_contract_id,
                               rate=s.rate, source=doc.name, document_id=doc.id))
    counts["надбавок 120"] = len(ctx.secret_allowances or [])
    # Ручное правило важнее правила из файла (SOURCE-010).
    manual_subst = {s.position for s in db.query(Substitution)
                    .filter(Substitution.document_id.is_(None)).all()}
    for position, extra in (ctx.substitution_rules or {}).items():
        if extra and position not in manual_subst:
            db.add(Substitution(position=position, replaced_by=", ".join(sorted(extra)),
                                source=doc.name, document_id=doc.id))
    counts["правил замещения"] = sum(1 for v in (ctx.substitution_rules or {}).values() if v)
    import basis as B
    B.reapply_manual(db)
    db.commit()
    return counts


#: Какие форматы читает каждый обработчик. Разбор документов по договору и
#: правил замещения читает ячейки книги: PDF и .doc туда отдавать бессмысленно.
FORMATS = {
    "intake": (".xlsx", ".xlsm", ".xls"),
    "substitutions": (".xlsx", ".xlsm", ".xls"),
    "norms": (".xlsx", ".xlsm", ".xls", ".pdf", ".doc", ".docx"),
    "salary": (".xlsx", ".xlsm"),
    "facts": (".xlsx", ".xlsm"),
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
def _bind_to_contract(doc, entity, item):
    """Строка модели из документа пакета договора — строка этого договора.

    Разборщик по смыслу граф пишет данные в договор, в который загружен
    документ (SOURCE-004). Модель берёт шифр из шапки или не находит его, и
    та же трудоёмкость предлагалась экономисту второй раз под чужим шифром.
    Договор секретности у надбавки 120 — свой, его не трогаем.
    """
    code = getattr(doc, "contract_code", None)
    if code and entity in ("трудоемкость", "поступление"):
        item["contract"] = code
    elif code and entity == "договор":
        item["code"] = code
    return item


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
        # Та же сумма по тому же договору — это та же строка, даже если модель
        # назвала соседний месяц: в графике по месяцам она легко ошибается
        # колонкой. Предлагать экономисту подтвердить сдвинутую копию уже
        # разобранного транша незачем — он только запутает.
        code = str(f.get("contract") or "").strip().lower()
        amount = f.get("amount")
        rows = [r for r in db.query(Inflow).all()
                if (r.contract_code or "").strip().lower() == code]
        if any(r.month == f.get("month") for r in rows):
            return True
        return amount is not None and any(
            r.amount is not None and abs(float(r.amount) - float(amount)) < 0.5
            for r in rows)
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


def _correction_source(db, doc):
    from db import Case
    from fot_planner.harness_local import corrections as C
    case = db.get(Case, doc.case_id) if doc.scope == 'план' and doc.case_id else None
    return C.source_of(doc, period=case.year if case else None)


def remember_correction(db, doc, *, confirmed=True, decision_bindings=None, **fields):
    """Правка и её область в одной транзакции; автор — локальный оператор.

    Ни модель, ни вызывающий клиент не задают actor/широкую область.
    Старая таблица Correction сохраняется; binding хранится в LocalDecision.
    """
    from db import record_decision
    from fot_planner.harness_local import corrections as C
    from fot_planner.harness_local.decisions import digest

    row = Correction(document_name=doc.name, document_kind=doc.kind, **fields)
    db.add(row)
    db.flush()
    binding = C.binding_for(_correction_source(db, doc), confirmed)
    subject_digest = digest(binding, C.content_of(row))
    if decision_bindings is not None:
        # Пакет принятия строк — одно пользовательское решение. Области его
        # отдельных исправлений входят в это же событие, не дублируют решение.
        decision_bindings.append({'id': row.id, 'binding': binding,
                                  'subject_digest': subject_digest})
        return row
    record_decision(db, kind="данные", subject_kind="правка", subject_id=row.id,
                    subject_digest=subject_digest,
                    scope="документ %d" % doc.id, action={"binding": binding},
                    grounds="исправление конкретного документа",
                    outcome="записано: " + binding["status"])
    return row


def _correction_records(db, document):
    from db import Decision
    from sqlalchemy import and_, or_
    from fot_planner.harness_local import corrections as C

    records = []
    rows = (db.query(Decision).filter(Decision.kind == 'данные', or_(
        and_(Decision.subject_kind == 'правка', Decision.scope == 'документ %d' % document.id),
        and_(Decision.subject_kind == 'документ', Decision.subject_id == str(document.id)))).all())
    for decision in rows:
        try:
            action = json.loads(decision.action or "{}")
            bindings = ([{'id': int(decision.subject_id), 'binding': action.get('binding'),
                          'subject_digest': decision.subject_digest}]
                        if decision.subject_kind == 'правка' else action.get('correction_bindings', []))
            if not isinstance(bindings, list):
                continue
        except (ValueError, AttributeError):
            continue
        for item in bindings:
            if not isinstance(item, dict) or type(item.get('id')) is not int:
                continue
            correction = db.get(Correction, item['id'])
            if correction is not None:
                records.append({"id": correction.id, "binding": item.get('binding'),
                                "content": C.content_of(correction), "actor": decision.actor,
                                "actor_source": decision.actor_source,
                                "subject_digest": item.get('subject_digest')})
    return records


def _superseded_ids(db, document):
    """Правки, заменённые явным решением оператора по этому документу."""
    from db import Decision
    out = set()
    rows = db.query(Decision).filter(Decision.kind == 'данные',
                                     Decision.subject_kind == 'конфликт правок',
                                     Decision.scope == 'документ %d' % document.id,
                                     Decision.outcome == 'применено').all()
    for decision in rows:
        try:
            action = json.loads(decision.action or "{}")
        except ValueError:
            continue
        out.update(i for i in action.get('заменены', []) if type(i) is int)
    return out


def correction_context(db, document, limit=8):
    from fot_planner.harness_local import corrections as C
    return C.select(_correction_records(db, document), _correction_source(db, document),
                    limit=limit, superseded=_superseded_ids(db, document))


def correction_conflicts(db, document):
    """Группы противоречащих правок (списки ID), кроме уже разрешённых."""
    from fot_planner.harness_local import corrections as C
    return C.conflict_groups(_correction_records(db, document), _correction_source(db, document),
                             superseded=_superseded_ids(db, document))


def resolve_correction_conflict(db, document, keep_id, precondition=None):
    """Явное решение оператора: какая из противоречащих правок действует.

    Остальные правки группы помечаются заменёнными в решении, а не
    удаляются: история сохраняется, поздняя правка не побеждает сама
    (MEM-002). Новое расходящееся значение позже откроет конфликт заново.
    ValueError — правка не участвует в конфликте.
    """
    from db import record_decision
    from fot_planner.harness_local import corrections as C
    from fot_planner.harness_local.decisions import digest
    action = C.resolution(int(keep_id), correction_conflicts(db, document))
    source = _correction_source(db, document)
    record_decision(db, kind="данные", subject_kind="конфликт правок", subject_id=int(keep_id),
                    subject_digest=digest(source, action), scope="документ %d" % document.id,
                    action={**action, "источник": source},
                    grounds="разрешение конфликта исправлений оператором",
                    precondition=precondition)
    return action


def correction_hints(db, kind, limit=8, *, document=None):
    """Без exact документа нет доказанной области — подсказок нет.

    Параметр kind оставлен для совместимости; сам по себе вид формы не даёт
    права переносить правку на другой документ. Legacy-записи не удаляются.
    """
    if document is None or document.kind != kind:
        return []
    return correction_context(db, document, limit=limit)["hints"]


def _capability(task_kind, doc):
    """Выбор процедуры по каталогу для этого документа.

    Доступность считается по ресурсам сервиса сейчас, а не по зрелости
    описания: модель не настроена — способность «недоступна» с причиной и
    действием, а не «не реализована» и не повод для новой роли (CAP-002/003).
    """
    from pathlib import Path
    from fot_planner.harness_local.catalogue import Authority, Task, default_catalogue
    resources = {'python', 'openpyxl'} | ({'llm'} if llm.available()[0] else set())
    authority = Authority(permissions=frozenset({'document.read', 'model.call'}),
                          resources=frozenset(resources), files=(Path(doc.path),))
    catalogue = default_catalogue()
    task = Task(kind=task_kind, domain='fot.local')
    return catalogue, task, authority, catalogue.select(task, authority)


MODEL_ACTION = ("задайте подключение модели в .env (FOT_LLM_BASE_URL и FOT_LLM_MODEL либо "
                "ключ провайдера) и перезапустите сервис; новая способность для этого не нужна")


def _missing_capability_text(doc, selection, what):
    _ok, why = llm.available()
    return ("Разбор «%s» %s сейчас невозможен: способность недоступна — %s%s. Что сделать: %s. "
            "Документ остаётся в реестре, строки можно ввести вручную."
            % (doc.name, what, selection.reason, (" (%s)" % why) if why else "", MODEL_ACTION))


def _extraction_snapshot(doc, res, hints, selection):
    """Закрепить контекст извлечения в хранилище артефактов (VER-002, MEM-003).

    Байты документа, тексты подсказки, ID применённых и исключённых
    исправлений, провайдер, модель, параметры вызова и итог ответа — по
    SHA-256, без ключей провайдера. Возвращает хеш снимка; None, если
    запроса к модели не было (провайдер не настроен) — закреплять нечего.
    """
    if res.get("unavailable"):
        return None
    import agents as _agents
    from db import ARTIFACT_DIR
    from fot_planner.harness_local import extraction as X
    from fot_planner.harness_local.artifacts import ArtifactStore
    store = ArtifactStore(ARTIFACT_DIR)
    try:
        artifact = store.put_file(doc.path) if doc.path and os.path.isfile(doc.path) else None
    except (OSError, ValueError):
        artifact = None
    provider, model, _why = llm.provider()
    snapshot = X.build_snapshot(
        document={"id": doc.id, "name": doc.name, "sha256": doc.sha256,
                  "artifact_sha256": artifact, "version": doc.version or 1,
                  "kind": doc.kind, "scope": doc.scope, "case_id": doc.case_id},
        provider=provider, model=model,
        prompt={"system": llm.FREEFORM_SYSTEM, "shape": llm.FREEFORM_SHAPE,
                "schema": llm.FREEFORM_SCHEMA,
                "hinted_system": llm._hinted(llm.FREEFORM_SYSTEM, hints)},
        hints={"ids": selection.get("correction_ids"), "texts": hints,
               "conflicts": selection.get("conflicts"), "source": selection.get("source")},
        params={"max_chunks": 8, "chunk_chars": llm.CHUNK,
                "max_tokens": llm.FREEFORM_MAX_TOKENS},
        result=res, agent_version=_agents.version_of("intake"))
    return X.store_snapshot(store, snapshot)


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
        catalogue, task, authority, selection = _capability('document.freeform_extract', doc)
        if selection.status != 'ready':
            # Обязательной способности нет: причина и требуемое действие, а не
            # вопрос «что за документ?» и не имитация разбора (CAP-003).
            w["detail"] = "процедура недоступна: %s" % selection.reason
            w["artifact"] = {"файл": doc.name, "способность": "freeform-extract",
                             "состояние": selection.status, "причина": selection.reason,
                             "действие": selection.action}
            doc.state = "не распознан"
            doc.summary = "модель недоступна: %s" % (llm.available()[1] or selection.reason)
            db.commit()
            say(db, case.id, _missing_capability_text(doc, selection, "без шаблона"),
                agent="intake", document_id=doc.id,
                payload={"kind": "capability_missing", "capability": "freeform-extract",
                         "status": selection.status, "reason": selection.reason,
                         "action": selection.action})
            db.commit()
            return None
        correction_selection = correction_context(db, doc)
        hints = correction_selection['hints']
        if correction_selection['conflicts']:
            say(db, case.id, 'Есть противоречащие исправления по этому документу '
                '(№ %s). Не применяю их автоматически; уточните верное значение.'
                % ', '.join(map(str, correction_selection['conflicts'])),
                agent='intake', document_id=doc.id)
        # Подсказки уходят списком, как и раньше: контракт каталога хранит их
        # кортежем, а вызов модели и стенды ждут прежний вид аргумента.
        res = catalogue.invoke(task, {"path": doc.path, "filename": doc.name, "hints": list(hints)},
                               authority, {"llm.freeform": lambda path, filename="", hints=():
                                           llm.freeform(path, filename, hints=list(hints))})
        # Что именно видела модель — в хранилище артефактов; предложения и
        # приговоры ссылаются на этот снимок (VER-002, MEM-003).
        context_sha = _extraction_snapshot(doc, res, hints, correction_selection)
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
                item = _bind_to_contract(doc, entity, item)
                if _known(db, entity, item):
                    # Знакомый обработчик уже записал эту строку — предлагать
                    # ее второй раз значит просить подтвердить проверенное.
                    skipped += 1
                    continue
                g, why = grade(entity, item, where, ctx)
                grades[g] += 1
                db.add(Proposal(document_id=doc.id, entity=entity,
                                context_sha256=context_sha,
                                payload=json.dumps(item, ensure_ascii=False),
                                evidence=str(where)[:400] if where else None,
                                grade=g, reason=why or None))
                counts[entity] += 1
        total = sum(counts.values())
        w["detail"] = ("предложено %d, уже в реестре %d" % (total, skipped)
                       if total or skipped else "сверх разобранного ничего")
        w["artifact"] = {"файл": doc.name, "прочитано частями": res.get("parts"),
                         "учтено правок экономиста": len(hints),
                         "снимок контекста": (context_sha[:12] + "…") if context_sha
                                             else "нет: запроса к модели не было",
                         "ID применённых правок": correction_selection['correction_ids'],
                         "конфликтующие правки": correction_selection['conflicts'],
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
    # Без плана (начальная загрузка) спросить некого: вопрос — в лог.
    if case.id is not None:
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
        # Документ из пакета договора пишет данные этого договора (SOURCE-004).
        out = extract.extract(doc.path, contract=doc.contract_code)
        passport = out["passport"]
        case.passport = json.dumps(passport, ensure_ascii=False)
        doc.state = "разобран"
        counts = _store_passport(db, case, passport, doc)
        emp, ctr = counts.get("сотрудников", 0), counts.get("договоров", 0)
        source_labor = counts.get("исходных строк трудоемкости", 0)
        summary = []
        for key, value in counts.items():
            if not value or key == "исходных строк трудоемкости":
                continue
            if key == "строк трудоемкости" and source_labor > value:
                summary.append("строк источника %d → позиций плана %d"
                               % (source_labor, value))
            else:
                summary.append("%s %d" % (key, value))
        doc.summary = ", ".join(summary)
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
    # Поступления в расчёт не подаются (ПРАВИЛА_РЕШАТЕЛЯ, правило 21): ФОТ
    # договора задан одной суммой, и их отсутствие — не повод не считать.
    ready = {"ok": not missing, "missing": missing,
             "hint": "Загрузите документ с этими данными." if missing else ""}
    # Без приписки «Настройки расчета — из этого файла»: лист настроек есть в
    # каждой карточке договора, из него берётся только год поступлений, и
    # приписка повторялась после каждого файла, ничего не сообщая.
    say(db, case.id,
        "Разобрал «%s»: %s." % (doc.name, doc.summary or "строк реестра нет"),
        agent="intake", document_id=doc.id,
        payload={"kind": "passport", "employees": emp, "contracts": ctr,
                 "log": out.get("log") or []})

    # Без плана (начальная загрузка) вопросы задавать некому.
    for q in (out.get("questions") or [])[:5] if case.id is not None else ():
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
        catalogue, task, authority, selection = _capability('document.norms_extract', doc)
        if selection.status == 'ready':
            res = catalogue.invoke(task, {"path": doc.path, "filename": doc.name}, authority,
                                   {"llm.parse": lambda path, filename='', hints=(): llm.parse(path, filename)})
        else:
            res = {"ok": False, "unavailable": True, "error": llm.available()[1] or selection.reason}
        if res.get("ok"):
            rows, by = res["rows"], "модель " + res["model"]
        elif docread.kind_of(doc.path) in (".xlsx", ".xlsm", ".xls"):
            # Запасной разбор читает ячейки книги — PDF и .doc ему не по зубам.
            # Это чистый инструмент без модели (CAP-003), и он об этом говорит.
            rows, by = reference.rows_by_anchors(doc.path), "разбор по заголовкам"
            if res.get("unavailable"):
                say(db, case.id, "Модель недоступна (%s) — «%s» разобран по заголовкам книги без "
                    "неё, величины стоит сверить глазами. Чтобы вернуть разбор моделью: %s."
                    % (res.get("error"), doc.name, MODEL_ACTION), agent="norms", document_id=doc.id)
                db.commit()
        elif res.get("unavailable"):
            raise ValueError("способность «norms-extract» недоступна: %s. Что сделать: %s. "
                             "Для книги Excel есть разбор по заголовкам без модели."
                             % (res.get("error"), MODEL_ACTION))
        else:
            raise ValueError(res.get("error") or "документ не разобран")
        # Сравнение — с величинами на дату документа (01.10.2026): новая
        # редакция приказа сверяется с той, что действовала тогда.
        changes, unknown, seen, given = reference.compare(rows, res.get("effective_from"))
        doc.state = "разобран"
        doc.parsed_by = by
        # Что документ дал реестру: величин извлечено, сколько сошлось со
        # справочником, сколько расходится. Без этого в перечне документов
        # у нормативных стоял прочерк — будто разбор ничего не дал.
        doc.summary = "расхождений %d" % len(changes)
        # Связь «источник для» ставится отдельно (вручную или при записи
        # величин) и повторный разбор её не отменяет.
        try:
            was_source = (json.loads(doc.gave) if doc.gave else {}).get("источник для")
        except ValueError:
            was_source = None
        doc.gave = json.dumps({"источник для": was_source,
                               "величин": len(rows or []),
                               "расхождений": len(changes),
                               "должностей вне справочника": len(unknown),
                               "значения": given["значения"],
                               "строки": given["строки"],
                               "вне справочника": given["без должности"],
                               "расхождения": changes,
                               "основание": res.get("basis"),
                               "действует с": res.get("effective_from")},
                              ensure_ascii=False)
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
        found = propose_entities(db, case, doc)
        if found is None:
            return          # способности нет: причина и действие уже в ленте
        if not found:
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
        if propose_entities(db, case, doc) is False:
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
        elif owner == "positions":
            # Перечень — таблица должностей целиком: искать в нём сотрудников
            # и договоры незачем, а предложения «должность» повторили бы его.
            run_positions(db, case, doc)
            return
        elif owner == "workgroup":
            # Кадровый приказ читается по форме целиком; искать в нём
            # договоры и сотрудников заново без шаблона незачем.
            run_workgroup(db, case, doc)
            return
        elif owner == "salary":
            # Таблица зарплаты читается по графам целиком: повторное чтение
            # без шаблона искало бы в ней сотрудников и договоры заново.
            run_salary_months(db, case, doc)
            return
        elif owner == "facts":
            # Факт начислений — тоже по графам целиком.
            run_facts(db, case, doc)
            return
        else:
            run_intake(db, case, doc)
        # Вид документа не обещает, что в нем нет ничего сверх этого вида.
        # Служебная записка проходит как нормативный документ, а внутри —
        # трое сотрудников и договор, и знакомый обработчик их не заметит.
        # Поэтому после него документ читается еще раз, уже без шаблона.
        propose_entities(db, case, doc)
        # Документ мог принести договоры: приказы, ждавшие своего договора,
        # прикрепляются к нему сами.
        rebind_workorders(db)
    except Exception as exc:  # noqa: BLE001 — сообщение вместо падения фона
        # Текст исключения — для ленты и карточки, но не для графы реестра:
        # там нужно состояние, а не английская диагностика библиотеки.
        doc.state = "не распознан"
        doc.summary = "разбор не удался"
        db.commit()
        say(db, case.id, "Не смог разобрать «%s»: %s" % (doc.name, exc), agent=owner,
            document_id=doc.id)
        db.commit()
