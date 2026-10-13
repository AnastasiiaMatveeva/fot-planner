# -*- coding: utf-8 -*-
"""Факт начислений: прошедшие месяцы — как были начислены.

Пересчёт по факту (решение пользователя 30.09.2026): «мы знаем, например, что
в январе и феврале вот такие суммы были, вот так сотрудники распределены, с
такими ставками и надбавками… нужно, чтобы оптимизатор, не трогая январь и
февраль, пересчитал». Факт один на организацию и год, как штатное
расписание: он живёт в реестре, и варианты плана видят один и тот же факт —
копия факта в каждом плане со временем разошлась бы с соседней. По какой
месяц факт в расчёте, не выбирают: это месяцы факта подряд с января
(through).

Форма — как таблица 6.1 отчёта (пользователь 30.09.2026: «требуется
подгрузить документ и предварительно отобразить то, что подгрузили, —
таблицу с сотрудниками и выплатами, как в 6-й таблице»): строка — сотрудник,
договор и вид выплаты, графы — месяцы с суммами. Месяцы, где суммы есть, — и
есть месяцы факта: выбирать их не нужно («зачем выбирать месяц, я не
поняла?»). Годятся и форма 6.3 «ШР на дату, детализация назначений» — её
выгружает 1С отдела кадров (назначения по срокам; срок режется датой «на
дату» из шапки или имени файла, иначе — месяцем до загрузки) — и плоская
таблица «факт_выплат» (код строки, договор, месяц, вид выплаты, сумма,
ставка).

Сумма строки 6.3 — сумма месяца по назначению: неполный месяц (отпуск,
больничный) она не отражает. Точные деньги дал бы отчёт 1С «Анализ
начислений» — отдельная форма, пока не разбирается.
"""
from __future__ import annotations

import datetime as dt
import os
import re

from db import ActualPayment, Case, Contract, Document, Employee

KIND = "факт начислений"
MONTH_WORDS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
               "сентябрь", "октябрь", "ноябрь", "декабрь")
_MONTH_STEMS = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт",
                "ноя", "дек")

#: Роль графы → начала её названий в шапке (после приведения: нижний
#: регистр, «ё» → «е», пробелы схлопнуты). Первое совпадение выигрывает.
_ROLES = (
    ("start", ("начало действия", "дата начала")),
    ("end", ("окончание действия", "дата окончания")),
    ("kind_code", ("код пар-ра", "код параметра", "код вида")),
    ("kind_name", ("название параметра", "вид выплаты", "вид начисления", "параметр")),
    ("contract_code", ("код шифра затрат", "шифр договора", "договор")),
    ("contract_name", ("шифр затрат", "наименование шифра")),
    ("account", ("лицевой счет", "лицевой счёт", "счет")),
    ("amount", ("сумма в руб", "сумма, руб", "сумма, ₽", "сумма")),
    ("rate", ("ставка",)),
    ("main", ("основное место",)),
    ("month", ("месяц",)),
    ("emp", ("таб.№", "таб. №", "таб №", "табельный", "код строки")),
    ("fio", ("фамилия", "фио", "ф.и.о", "сотрудник")),
    ("position", ("должность",)),
)

#: Вид выплаты по коду параметра (как в форме кадров и в разделе 6.3).
_KIND_BY_CODE = {"1": "оклад", "оклад": "оклад", "120": "120", "122": "122", "124": "124",
                 "152": "152", "приказ": "приказ"}
#: …и по словам названия, если кода нет или он чужой.
_KIND_WORDS = (("оклад", "оклад"), ("тайн", "120"), ("качеств", "122"),
               ("интенсивн", "124"), ("дополнительн", "152"), ("приказ", "приказ"),
               ("стимулир", "приказ"))


def _norm(text):
    return re.sub(r"\s+", " ", str(text or "").replace("ё", "е").replace("Ё", "Е")).strip().lower()


def _digits(text):
    return "".join(ch for ch in str(text or "") if ch.isdigit())


def _number(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def _date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    m = re.match(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", str(v or ""))
    if not m:
        return None
    try:
        return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _month(v):
    if isinstance(v, (int, float)) and 1 <= int(v) <= 12:
        return int(v)
    t = _norm(v)
    if t.isdigit() and 1 <= int(t) <= 12:
        return int(t)
    if t.startswith("мая"):
        return 5
    for i, stem in enumerate(_MONTH_STEMS):
        if t.startswith(stem):
            return i + 1
    d = _date(v)
    return d.month if d else None


def _month_head(v):
    """Номер месяца по заголовку графы («янв», «Январь», «янв, факт») или None."""
    if v is None or isinstance(v, (int, float)):
        return None
    t = _norm(v)
    if not t or t.startswith(("итог", "за год", "всего", "месяц")):
        return None
    word = re.split(r"[\s,.]", t)[0]
    if word.startswith("мая") or word == "май":
        return 5
    for i, stem in enumerate(_MONTH_STEMS):
        if word.startswith(stem) and len(word) <= 9:
            return i + 1
    return None


def _stated(ws, head_row, name):
    """Дата, названная в книге: в строках над шапкой («на дату 28.02.2026»),
    иначе в имени файла; None — не названа."""
    # Строки над шапкой; шапка в первой строке — их нет (max_row=0 openpyxl
    # читает как «все строки», и дата из данных сходила за дату выгрузки).
    above = ws.iter_rows(max_row=head_row - 1, values_only=True) if head_row > 1 else ()
    for values in above:
        for v in values or ():
            d = _date(v) if not isinstance(v, (int, float)) else None
            if d is None and isinstance(v, str):
                m = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4})", v)
                d = _date(m.group(1)) if m else None
            if d:
                return d
    m = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4})", str(name or ""))
    if m and _date(m.group(1)):
        return _date(m.group(1))
    return None


def _as_of(ws, head_row, name, uploaded):
    """Дата «на дату» выгрузки 6.3: названная в книге; нет — последний день
    месяца до загрузки. Назначение после неё — ещё не факт."""
    stated = _stated(ws, head_row, name)
    if stated:
        return stated
    if uploaded:
        first = dt.date(uploaded.year, uploaded.month, 1)
        return first - dt.timedelta(days=1)
    return None


def _not_yet(year, month, limit):
    """Месяц позже даты выгрузки (6.1 и плоская форма): начислений за него
    ещё быть не может — суммы в нём план, а не факт. Месяц самой даты
    выгрузки берётся: зарплату за месяц начисляют в его последние дни."""
    return limit is not None and (year, month) > (limit.year, limit.month)


def _limit(ws, head_row, name, uploaded):
    """Дата, после которой суммы 6.1 и плоской формы — не факт: названная в
    книге, иначе день загрузки."""
    stated = _stated(ws, head_row, name)
    if stated:
        return stated
    if uploaded:
        return uploaded.date() if isinstance(uploaded, dt.datetime) else uploaded
    return None


def _numbering(values):
    """Строка номеров граф (1, 2, 3 …) под шапкой, как в шаблоне кадров."""
    nums = [v for v in values or () if v not in (None, "")]
    return len(nums) >= 3 and all(isinstance(v, (int, float)) and v == i
                                  for i, v in enumerate(nums, start=1))


def kind_of(code, name):
    """Вид выплаты сервиса по коду параметра или по названию; None — чужой."""
    c = _norm(code)
    if isinstance(code, float) and code.is_integer():
        c = str(int(code))
    if c in _KIND_BY_CODE:
        return _KIND_BY_CODE[c]
    n = _norm(name)
    if n in _KIND_BY_CODE:
        return _KIND_BY_CODE[n]
    if n[:3] in ("120", "122", "124", "152"):
        return n[:3]
    for word, kind in _KIND_WORDS:
        if word in n:
            return kind
    return None


def _starts(head, word):
    """Название графы начинается с word целым словом: «Месяц» — да,
    «Месяцев» — нет, «Сумма в руб.» — да."""
    return head.startswith(word) and (len(head) == len(word) or not head[len(word)].isalpha())


def _roles(values):
    found = {}
    for i, v in enumerate(values or ()):
        head = _norm(v)
        if not head:
            continue
        for role, starts in _ROLES:
            if role in found:
                continue
            if any(_starts(head, s) for s in starts):
                found[role] = i
                break
    return found


def layout(path):
    """Где в книге таблица факта: лист, строка шапки, графы. None — не она.

    Опознаётся по шапке, а не моделью: сотрудник, сумма, вид выплаты, договор
    и либо срок назначения («Начало/Окончание действия»), либо месяц. У
    таблицы зарплаты по месяцам нет ни вида выплаты, ни договора, поэтому она
    сюда не попадает; у полной выгрузки отчёта сервиса находится лист 6.3."""
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
                roles = _roles(values)
                who = "emp" in roles or "fio" in roles
                what = "kind_code" in roles or "kind_name" in roles
                where = ("contract_code" in roles or "contract_name" in roles
                         or "account" in roles)
                when = ("start" in roles and "end" in roles) or "month" in roles
                if who and what and where and when and "amount" in roles:
                    return {"sheet": ws.title, "row": r, "cols": roles,
                            "form": "6.3" if "start" in roles else "плоская"}
                # Форма 6.1: суммы — в графах месяцев («янв», «Январь»,
                # «янв, факт»), графы суммы и срока нет.
                months = {}
                for i, v in enumerate(values or ()):
                    m = _month_head(v)
                    if m and m not in months.values():
                        months[i] = m
                if who and what and where and len(months) >= 2 and "amount" not in roles:
                    return {"sheet": ws.title, "row": r, "cols": roles, "months": months,
                            "form": "6.1"}
        return None
    finally:
        wb.close()


def parse(path, year, where=None, uploaded=None, name=None):
    """Строки факта из книги: по одной на месяц внутри года.

    Возвращает (строки, пропуски). Строка — словарь с полями файла как есть:
    сопоставление со штатным расписанием и договорами — дело store()."""
    import openpyxl
    where = where or layout(path)
    if where is not None and where.get("form") == "6.1":
        return _parse_61(path, year, where, uploaded, name or os.path.basename(path))
    if where is None:
        raise ValueError("в книге нет таблицы факта: нужны графы сотрудника, договора, "
                         "вида выплаты, суммы и срока назначения или месяца")
    cols = where["cols"]
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out, skipped = [], []
    try:
        ws = wb[where["sheet"]]
        as_of = (_as_of(ws, where["row"], name or os.path.basename(path), uploaded)
                 if where.get("form") == "6.3" else None)
        limit = (_limit(ws, where["row"], name or os.path.basename(path), uploaded)
                 if where.get("form") != "6.3" else None)
        later = set()
        for values in ws.iter_rows(min_row=where["row"] + 1, values_only=True):
            cell = lambda role: (values[cols[role]]  # noqa: E731
                                 if role in cols and cols[role] < len(values) else None)
            if _numbering(values):
                # Строка номеров граф под шапкой шаблона кадров: 1, 2, 3…
                continue
            emp, fio = cell("emp"), cell("fio")
            if emp in (None, "") and fio in (None, ""):
                continue
            kind = kind_of(cell("kind_code"), cell("kind_name"))
            who = str(fio or emp).strip()
            if kind is None:
                skipped.append("%s: вид «%s»" % (who, cell("kind_code") or cell("kind_name") or "?"))
                continue
            amount = _number(cell("amount"))
            if amount is None or amount == 0:
                continue
            if "month" in cols and "start" not in cols:
                m = _month(cell("month"))
                if m and _not_yet(year, m, limit):
                    later.add(m)
                    continue
                months = [m] if m else []
            else:
                a, b = _date(cell("start")), _date(cell("end"))
                if a is None:
                    skipped.append("%s: нет даты начала" % who)
                    continue
                b = b or dt.date(year, 12, 31)
                if as_of is not None and b > as_of:
                    # Назначение после даты выгрузки — план кадров, а не факт.
                    b = as_of
                lo = a.month if a.year == year else (1 if a.year < year else 13)
                hi = b.month if b.year == year else (12 if b.year > year else 0)
                if as_of is not None and b == as_of and b.year == year:
                    # Месяц даты выгрузки — факт, только если он закрыт целиком.
                    last = (dt.date(year, 12, 31) if b.month == 12
                            else dt.date(year, b.month + 1, 1) - dt.timedelta(days=1))
                    if b < last:
                        hi = b.month - 1
                months = list(range(lo, hi + 1))
            position = str(cell("position") or "")
            main_cell = cell("main")
            if main_cell not in (None, ""):
                main = _norm(main_cell) in ("да", "true", "1", "+", "основное")
            else:
                # В форме кадров совместительство сказано в должности:
                # «Инженер, внутр. совмест., 0,5 ставк.».
                main = "совмест" not in _norm(position)
            for m in months:
                out.append({"emp": str(emp).strip() if emp not in (None, "") else "",
                            "fio": str(fio).strip() if fio not in (None, "") else None,
                            "position": position.split(",")[0].strip() or None,
                            "kind": kind, "amount": round(amount, 2),
                            "rate": _number(cell("rate")) if kind == "оклад" else None,
                            "main": main if kind == "оклад" else None,
                            "contract": str(cell("contract_code") or "").strip(),
                            "contract_name": str(cell("contract_name") or "").strip(),
                            "account": str(cell("account") or "").strip(),
                            "month": m})
        if later:
            skipped.append(_later_note(limit, later))
    finally:
        wb.close()
    return out, skipped


def _later_note(limit, months):
    """Пропуск месяцев позже даты выгрузки — одной строкой."""
    return "суммы позже %s — план, а не факт: %s" % (
        limit.strftime("%d.%m.%Y"), months_text(sorted(months)))


def _parse_61(path, year, where, uploaded=None, name=None):
    """Форма 6.1: строка — сотрудник, договор и вид выплаты, графы — месяцы.
    Табельный номер, ФИО и договор в соседних строках одной группы бывают
    пустыми (объединённые ячейки) — берутся из строки выше. Строки итогов
    пропускаются; месяцы позже даты выгрузки — тоже (_not_yet)."""
    import openpyxl
    cols, months = where["cols"], where["months"]
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out, skipped, last = [], [], {"emp": "", "fio": None, "contract": ""}
    later = set()
    try:
        ws = wb[where["sheet"]]
        limit = _limit(ws, where["row"], name or os.path.basename(path), uploaded)
        for values in ws.iter_rows(min_row=where["row"] + 1, values_only=True):
            cell = lambda role: (values[cols[role]]  # noqa: E731
                                 if role in cols and cols[role] < len(values) else None)
            texts = " ".join(str(v) for v in values or () if isinstance(v, str)).lower()
            if "итого" in texts:
                continue
            emp, fio = cell("emp"), cell("fio")
            contract = cell("contract_code") or cell("contract_name") or cell("account")
            if emp not in (None, "") or fio not in (None, ""):
                last = {"emp": str(emp or "").strip(), "fio": str(fio).strip() if fio else None,
                        "contract": ""}
            if contract not in (None, ""):
                last["contract"] = str(contract).strip()
            kind = kind_of(cell("kind_code"), cell("kind_name"))
            if kind is None:
                raw = cell("kind_code") or cell("kind_name")
                if raw not in (None, ""):
                    skipped.append("%s: вид «%s»" % (last["fio"] or last["emp"], raw))
                continue
            if not (last["emp"] or last["fio"]) or not last["contract"]:
                continue
            for i, m in months.items():
                amount = _number(values[i]) if i < len(values) else None
                if amount is None or amount == 0:
                    continue
                if _not_yet(year, m, limit):
                    later.add(m)
                    continue
                out.append({"emp": last["emp"], "fio": last["fio"], "position": None,
                            "kind": kind, "amount": round(amount, 2), "rate": None,
                            "main": None, "contract": last["contract"],
                            "contract_name": "", "account": "", "month": m})
        if later:
            skipped.append(_later_note(limit, later))
    finally:
        wb.close()
    return out, skipped


#: Графы формы 6.3 «ШР на дату, детализация назначений» — шаблон отдела кадров
#: без изменений, как лист 6.3 выгрузки отчёта (export_report).
SHR_HEAD = ["Таб.№", "Назначение", "Фамилия И.О., уч. ст., уч. зван.", "Код подр.",
            "Подразделение", "Должность", "Категория персонала", "Код пар-ра",
            "Название параметра", "Ставка", "Номинальное значение параметра",
            "Значение параметра по ставке", "Сумма в руб.", "Начало действия",
            "Окончание действия", "Код шифра затрат", "Шифр затрат", "Лицевой счет", "УИ ПНИЭР",
            "Номер приказа ввода", "Дата приказа ввода", "Номер приказа закрытия",
            "Дата приказа закрытия"]

#: Код и название параметра — как в разделе 6.3 отчёта (report._PARAM).
_PARAM = {"оклад": ("1", "Оклад (должностной оклад)"),
          "120": ("120", "За работу со сведениями, составляющими государственную тайну"),
          "122": ("122", "За качество выполняемых работ"),
          "124": ("124", "За интенсивность и высокие результаты работы"),
          "152": ("152", "За выполнение дополнительной работы"),
          "приказ": ("приказ", "Стимулирующая выплата приказом")}
_KIND_RANK = {k: i for i, k in enumerate(("оклад", "120", "122", "124", "152", "приказ"))}


def _last_day(year, month):
    return (dt.date(year, 12, 31) if month == 12
            else dt.date(year, month + 1, 1) - dt.timedelta(days=1))


def detail_rows(db, year):
    """Факт года в форме 6.3 — той же, что раздел 6.3 отчёта
    (report._staff_detail): назначение — строка на непрерывный период, где у
    человека на договоре одна выплата одной суммы и одной ставки. Табельный
    номер — из файла факта, должность, подразделение и категория — из
    штатного расписания и справочника должностей, шифр и лицевой счёт — из
    реестра договоров."""
    import reference
    from fot_planner.position_reference import normalize_position
    try:
        ref = reference._index(reference.read_rows())
    except Exception:  # noqa: BLE001 — без справочника категория просто пуста
        ref = {}
    staff = {e.code: e for e in db.query(Employee).all()}
    ctrs = {c.code: c for c in db.query(Contract).all()}
    groups = {}
    for r in db.query(ActualPayment).filter_by(year=year).all():
        if not r.amount:
            continue
        rate = round(r.rate, 4) if r.rate and r.rate > 0 else None
        key = (r.employee_code or "", r.person_code or "", r.fio or "",
               r.contract_code or r.contract_text or "", r.kind, r.is_main, rate,
               round(r.amount, 2))
        groups.setdefault(key, set()).add(r.month)
    rows = []
    for (code, person, fio, contract, kind, main, rate, amount), months in groups.items():
        e, c = staff.get(code), ctrs.get(contract)
        position = (e.position if e else None) or ""
        if rate and kind == "оклад":
            post = "%s, %s%s ставк." % (position, "внутр. совмест., " if main is False else "",
                                        ("%g" % rate).replace(".", ","))
        else:
            post = position
        pcode, pname = _PARAM.get(kind, (kind, kind))
        ordered = sorted(months)
        spans, start = [], ordered[0]
        for prev, m in zip(ordered, ordered[1:]):
            if m != prev + 1:
                spans.append((start, prev))
                start = m
        spans.append((start, ordered[-1]))
        for m1, m2 in spans:
            rows.append({
                "таб": person or code, "назначение": None, "фио": fio or (e.fio if e else code),
                "код_подр": None, "подразделение": (e.department if e else None) or None,
                "должность": post or None,
                "категория": (ref.get(normalize_position(position)) or {}).get("cat") or None
                if position else None,
                "код": pcode, "параметр": pname, "ставка": rate,
                "номинал": round(amount / rate, 2) if rate else amount,
                "по_ставке": amount, "сумма": amount,
                "начало": dt.date(year, m1, 1).strftime("%d.%m.%Y"),
                "окончание": _last_day(year, m2).strftime("%d.%m.%Y"),
                "код_шифра": contract, "шифр": (c.name if c else None) or None,
                "счет": (c.account if c else None) or None,
                # Договор есть в реестре, сотрудник — в штатном расписании:
                # иначе строка на экране тише, причина — в подсказке.
                "known": c is not None, "matched": e is not None,
                "_k": (fio, m1, 0 if main is not False else 1, _KIND_RANK.get(kind, 9), contract)})
    rows.sort(key=lambda r: r["_k"])
    for r in rows:
        r.pop("_k")
    return rows


def form_book(year, rows, through=0):
    """Форма для заполнения факта — книга .xlsx в форме 6.3 «ШР на дату,
    детализация назначений»: так её выгружает 1С отдела кадров, так её
    показывает вкладка «Факт начислений». rows — строки detail_rows (уже
    загруженный факт) или пусто. Дата в заголовке — последний день месяца
    факта: по неё назначения — факт; без факта её вписывают."""
    import io
    import openpyxl
    from openpyxl.styles import Alignment, Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Страница 1"
    when = _last_day(year, through).strftime("%d.%m.%Y") if through else "__.__.____"
    # Раскладка шаблона кадров («ШР на 01.06.2026_детализация_ШАБЛОН»):
    # заголовок в четыре строки, шапка — в седьмой, под ней номера граф.
    for line in ("Подразделение - по всем подразделениям", "Дата построения %s" % when,
                 "Лицевой счет - по всем ЛС", "Параметры назначений на дату,", None, None):
        ws.append([line])
    ws.append(SHR_HEAD)
    for cell in ws[7]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.append(list(range(1, len(SHR_HEAD) + 1)))
    for r in rows:
        ws.append([r["таб"], r["назначение"], r["фио"], r["код_подр"], r["подразделение"],
                   r["должность"], r["категория"], r["код"], r["параметр"], r["ставка"],
                   r["номинал"], r["по_ставке"], r["сумма"], r["начало"], r["окончание"],
                   r["код_шифра"], r["шифр"], r["счет"], None, None, None, None, None])
    for row in ws.iter_rows(min_row=9, min_col=10, max_col=13):
        for cell in row:
            cell.number_format = "0.00" if cell.column == 10 else "# ##0.00"
    for i, width in enumerate((10, 12, 30, 9, 22, 30, 16, 9, 36, 8, 14, 14, 14, 12, 12, 14,
                               30, 16, 10, 12, 12, 12, 12), start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = width
    ws.freeze_panes = "D9"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def through(db, year):
    """Месяц, по который факт в расчёте: месяцы факта подряд с января, не
    дальше ноября (декабрь целиком из факта пересчитывать было бы нечего)."""
    have = set(months_available(db, year))
    m = 0
    while m < 11 and (m + 1) in have:
        m += 1
    return m


def _matchers(db):
    by_code, by_person, by_fio = {}, {}, {}
    for e in db.query(Employee).all():
        by_code[_norm(e.code)] = e
        if e.person_code:
            by_person.setdefault(_norm(e.person_code), []).append(e)
        if e.fio:
            by_fio.setdefault(_norm(e.fio), []).append(e)
    ctr_code, ctr_acc, ctr_name = {}, {}, {}
    for c in db.query(Contract).all():
        ctr_code.setdefault(_norm(c.code), c.code)
        if _digits(c.account):
            ctr_acc.setdefault(_digits(c.account), c.code)
        if c.name:
            ctr_name.setdefault(_norm(c.name), c.code)

    def employee(row):
        e = by_code.get(_norm(row["emp"])) if row["emp"] else None
        if e is None and row["emp"]:
            same = by_person.get(_norm(row["emp"])) or []
            if len(same) > 1 and row.get("position"):
                # Две строки одного человека (основное и совместительство) —
                # различаем по должности и месту работы.
                pos = [x for x in same if _norm(x.position) == _norm(row["position"])]
                same = pos or same
                if len(same) > 1 and row.get("main") is not None:
                    part = [x for x in same
                            if ("совмест" in _norm(x.employment_type)) != bool(row["main"])]
                    same = part or same
            e = same[0] if same else None
        if e is None and row.get("fio"):
            same = by_fio.get(_norm(row["fio"])) or []
            e = same[0] if len(same) == 1 else None
        return e

    def contract(row):
        return (ctr_code.get(_norm(row["contract"])) if row["contract"] else None) \
            or (ctr_acc.get(_digits(row["account"])) if _digits(row["account"]) else None) \
            or (ctr_name.get(_norm(row["contract_name"])) if row["contract_name"] else None)

    return employee, contract


def store(db, case, doc, rows):
    """Записать факт в реестр. Возвращает сводку для ленты.

    Новый файл заменяет факт тех месяцев, которые в нём есть, какой бы
    документ их ни принёс: месяц закрывают и уточняют по одному, и вторая
    выгрузка за январь — это новая редакция января, а не второй январь.
    Прочие месяцы остаются из прежних файлов."""
    year = case.year
    employee, contract = _matchers(db)
    agg, unknown_emp, unknown_ctr = {}, set(), set()
    for r in rows:
        e = employee(r)
        code = e.code if e is not None else (r["emp"] or r["fio"] or "")
        if e is None:
            unknown_emp.add(r["fio"] or r["emp"])
        c = contract(r)
        if c is None:
            unknown_ctr.add(r["contract"] or r["account"] or r["contract_name"] or "?")
        key = (code, c, r["month"], r["kind"])
        x = agg.setdefault(key, {"amount": 0.0, "rate": None, "main": None,
                                 "person": r["emp"] or None, "fio": (e.fio if e else r["fio"]),
                                 "contract_text": r["contract"] or r["account"]
                                 or r["contract_name"] or None,
                                 "matched": e is not None})
        x["amount"] = round(x["amount"] + r["amount"], 2)
        if r["rate"] is not None:
            x["rate"] = round((x["rate"] or 0.0) + r["rate"], 4)
        if r["main"] is not None:
            x["main"] = bool(x["main"]) or bool(r["main"])
    months = sorted({k[2] for k in agg})
    earlier = {r.document_id for r in db.query(ActualPayment)
               .filter(ActualPayment.year == year, ActualPayment.month.in_(months or [0])).all()
               if r.document_id and r.document_id != doc.id}
    db.query(ActualPayment).filter(ActualPayment.year == year,
                                   ActualPayment.month.in_(months or [0])).delete(
        synchronize_session=False)
    for (code, c, month, kind), x in sorted(agg.items(), key=lambda kv: (kv[0][2], str(kv[0][0]))):
        db.add(ActualPayment(employee_code=code or None, person_code=x["person"], fio=x["fio"],
                             contract_code=c, contract_text=x["contract_text"], year=year,
                             month=month, kind=kind, amount=x["amount"], rate=x["rate"],
                             is_main=x["main"], matched=x["matched"], source=doc.name,
                             document_id=doc.id))
    db.flush()
    replaced = []
    for old_id in earlier:
        left = db.query(ActualPayment).filter_by(document_id=old_id).count()
        prev = db.get(Document, old_id)
        if prev is not None and not left and prev.state != "заменен":
            prev.state = "заменен"
            replaced.append(prev.name)
    people = {k[0] for k in agg}
    return {"год": year, "месяцы": months, "строк": len(agg), "сотрудников": len(people),
            "договоров": len({k[1] for k in agg if k[1]}),
            "не найдено в штатном расписании": sorted(x for x in unknown_emp if x),
            "не найдены договоры": sorted(unknown_ctr), "заменены": replaced}


def months_text(months):
    """«январь–февраль», «январь, март»."""
    months = sorted(set(months))
    if not months:
        return ""
    spans, start = [], months[0]
    for prev, m in zip(months, months[1:]):
        if m != prev + 1:
            spans.append((start, prev))
            start = m
    spans.append((start, months[-1]))
    return ", ".join(MONTH_WORDS[a - 1] if a == b else "%s–%s" % (MONTH_WORDS[a - 1], MONTH_WORDS[b - 1])
                     for a, b in spans)


def months_available(db, year):
    """Месяцы года, по которым в реестре есть факт."""
    return sorted({m for (m,) in db.query(ActualPayment.month)
                   .filter(ActualPayment.year == year).distinct()})


def rows_for(db, year, through, skip_contracts=()):
    """Факт для входа расчёта: месяцы по through включительно.

    Строки без договора (не сопоставлены) во вход не идут: решателю их не к
    чему отнести. Договоры, снятые с расчёта плана, — тоже: их деньги к
    плану не относятся. Сотрудник, которого нет в штатном расписании (уволен
    или снят с расчёта плана), остаётся: его деньги уже потрачены с договора."""
    if not through:
        return []
    skip = set(skip_contracts or ())
    q = (db.query(ActualPayment)
         .filter(ActualPayment.year == year, ActualPayment.month <= through)
         .order_by(ActualPayment.month, ActualPayment.employee_code, ActualPayment.id))
    return [r for r in q.all() if r.contract_code and r.contract_code not in skip]


def registry_rows(db, year):
    """Строки вкладки реестра «Факт начислений» за год."""
    return [{"id": r.id, "code": r.employee_code, "person_code": r.person_code, "fio": r.fio,
             "contract": r.contract_code, "contract_text": r.contract_text, "month": r.month,
             "kind": r.kind, "amount": r.amount, "rate": r.rate, "main": r.is_main,
             "matched": bool(r.matched), "source": r.source}
            for r in db.query(ActualPayment).filter_by(year=year)
            .order_by(ActualPayment.month, ActualPayment.fio, ActualPayment.id).all()]


def case_year(db, case_id):
    case = db.get(Case, case_id)
    return case.year if case is not None and case.year else dt.date.today().year
