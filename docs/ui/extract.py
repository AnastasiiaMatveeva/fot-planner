# -*- coding: utf-8 -*-
"""Якорный разбор документов для агента извлечения данных.

Детерминированный слой: листы шаблона сервиса опознаются по названию, листы
документов экономистов — по смыслу граф (SOURCE-004, `fields.py`): номер формы
разбор не выбирает. Извлечённое проверяется расчётными соотношениями самих
форм. Возвращает паспорт, журнал действий на русском и вопросы специалисту.
"""
import io
import os
import re
import sys

import openpyxl
from openpyxl.utils import get_column_letter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fields  # noqa: E402

MONTHS = 12


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(" ", "").replace("\u00a0", "").replace(",", ".")
    s = re.sub(r"[^\d.\-]", "", s)
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _fmt(v):
    if v is None:
        return "—"
    s = f"{v:,.2f}".replace(",", " ").replace(".00", "")
    return s.replace(".", ",")


class Passport(dict):
    """Паспорт: employees, contracts, inflow, notes."""


def extract(path, contract=None):
    """Разобрать книгу. contract — договор, в который загружен документ:
    тогда всё извлечённое относится к нему, шифр из шапки не нужен."""
    wb = openpyxl.load_workbook(path, data_only=True)
    log, questions = [], []
    passport = {"employees": [], "contracts": [], "inflow": {}, "labor": [],
                "secret": [], "settings": {}, "values": [], "contract_hint": None,
                "source": path.split("\\")[-1].split("/")[-1]}
    recognized = 0
    other = []

    for ws in wb.worksheets:
        kind = _classify(ws)
        if kind == "skip":
            continue
        if kind is None:
            other.append(ws)
            continue
        recognized += 1
        handler = {
            "employees": _read_employees,
            "contracts": _read_contracts,
            "inflow": _read_inflow,
            "settings": _read_settings,
            "labor": _read_labor,
            "secret": _read_secret,
        }[kind]
        handler(ws, passport, log, questions)
    if other:
        recognized += _read_by_meaning(other, passport, log, questions, contract)

    if not recognized:
        log.append("Нужных граф не найдено: ни трудоёмкости, ни ФОТ по этапам, "
                   "ни нормативов, ни листов шаблона fot-planner")
    return {"passport": passport, "log": log, "questions": questions,
            "recognized": recognized,
            "ready": _readiness(passport)}


def _classify(ws):
    t = ws.title.strip().lower()
    if t in ("сотрудники",):
        return "employees"
    if t in ("договоры",):
        return "contracts"
    if t in ("фот_по_месяцам",):
        return "inflow"
    if t in ("настройки",):
        return "settings"
    if t in ("трудоемкость_по_договорам", "трудоёмкость_по_договорам"):
        return "labor"
    if t in ("120_надбавка", "надбавка 120", "120 надбавка"):
        return "secret"
    if t in ("запуск", "лимиты_по_должностям",
             "фиксация_фот_по_месяцам",
             "минимальные_остатки", "ручные_назначения", "ручные_запреты"):
        return "skip"
    # Остальные листы — документы экономистов. Их не опознаём по названию или
    # номеру формы: читаются графы с нужным смыслом (_read_by_meaning).
    return None


def _read_employees(ws, p, log, q):
    hdr = {str(ws.cell(1, c).value or "").strip().lower(): c
           for c in range(1, ws.max_column + 1)}
    # Зарплаты в штатном расписании нет: она по месяцам в своём документе
    # (решение пользователя 19.09.2026, повторено 30.09.2026: «в штатном
    # расписании же нет зарплаты, мы же договорились»). Графа, если есть,
    # читается, но не требуется.
    need = ["код строки", "фио", "должность", "ставка"]
    if not all(k in hdr for k in need):
        log.append(f"Лист «{ws.title}»: заголовки не совпали с шаблоном, пропущен")
        return
    n = 0
    for r in range(2, ws.max_row + 1):
        code = ws.cell(r, hdr["код строки"]).value
        if not code:
            continue
        def text(name):
            v = ws.cell(r, hdr[name]).value if name in hdr else None
            return None if v in (None, "") else str(v).strip()

        p["employees"].append({
            "code": str(code),
            "fio": str(ws.cell(r, hdr["фио"]).value or ""),
            "pos": str(ws.cell(r, hdr["должность"]).value or ""),
            "rate": _num(ws.cell(r, hdr["ставка"]).value) or 1.0,
            "sal": (_num(ws.cell(r, hdr["зарплата"]).value) if "зарплата" in hdr else None) or 0.0,
            "from": (text("дата начала") or "")[:10] or None,
            "to": (text("дата окончания") or "")[:10] or None,
            "department": text("подразделение"),
            "employment_type": text("тип занятости"),
            "employment_category": text("категория занятости"),
            # Ограничения по договорам — графы шаблона: без них человек,
            # которому на ГОЗ нельзя, в расчёте туда попадёт.
            "allowed": text("разрешенные договоры") or text("разрешённые договоры"),
            "forbidden": text("запрещенные договоры") or text("запрещённые договоры"),
        })
        n += 1
    log.append(f"Лист «сотрудники»: шаблон fot-planner, извлечено строк: {n}")


def _read_contracts(ws, p, log, q):
    hdr = {str(ws.cell(1, c).value or "").strip().lower(): c
           for c in range(1, ws.max_column + 1)}
    if "код" not in hdr or "фот" not in hdr:
        log.append(f"Лист «{ws.title}»: заголовки не совпали с шаблоном, пропущен")
        return
    kinds_cols = [("оклад разрешен", "оклад"), ("120 разрешена", "120"),
                  ("122 разрешена", "122"), ("124 разрешена", "124"),
                  ("152 разрешена", "152"),
                  ("стимулирующая приказом разрешена", "приказ")]
    n = 0
    for r in range(2, ws.max_row + 1):
        code = ws.cell(r, hdr["код"]).value
        if not code:
            continue
        kinds = [nm for col, nm in kinds_cols
                 if col in hdr and str(ws.cell(r, hdr[col]).value or "").strip().lower() == "да"]

        def cell(name):
            return ws.cell(r, hdr[name]).value if name in hdr else None

        def text(name):
            v = cell(name)
            return None if v in (None, "") else str(v).strip()

        p["contracts"].append({
            "code": str(code),
            "name": str(ws.cell(r, hdr.get("название", 2)).value or ""),
            "goz": str(ws.cell(r, hdr.get("гоз", 6)).value or "нет"),
            "fot": _num(ws.cell(r, hdr["фот"]).value) or 0.0,
            "kinds": kinds,
            # Графы шаблона, без которых договор в реестре неполный: срок,
            # предел выплат, счёт, номер, тип. Раньше разбор брал шесть граф
            # из двадцати, и договор «с июня» действовал весь год.
            "num": text("номер"), "type": text("тип договора"), "account": text("счет"),
            "department": text("подразделение"),
            "from": text("дата начала"), "to": text("дата окончания"),
            "salary_deadline": text("конечная дата выплат оклада"),
            "allowance_deadline": text("конечная дата выплат надбавок"),
            "priority": text("приоритет"),
            "allow_main": text("основное место разрешено"),
            "allow_part_time": text("совместительство разрешено"),
        })
        n += 1
    log.append(f"Лист «договоры»: шаблон fot-planner, извлечено договоров: {n}")


def _read_secret(ws, p, log, q):
    """Лист «120_надбавка»: сотрудник, договор секретности, ставка 120.

    Надбавка за гостайну — надбавка сотрудника, а не договора: пока действует
    договор секретности, она обязательна и считается процентом от оклада.
    Графы те же, что во входном файле решателя; строка без сотрудника или
    без договора пропускается — половина записи в реестре хуже её отсутствия.
    """
    hdr = {str(ws.cell(1, c).value or "").strip().lower(): c
           for c in range(1, ws.max_column + 1)}
    if "сотрудник" not in hdr or "договор секретности" not in hdr:
        log.append(f"Лист «{ws.title}»: заголовки не совпали с шаблоном, пропущен")
        return
    n = 0
    for r in range(2, ws.max_row + 1):
        who = ws.cell(r, hdr["сотрудник"]).value
        ctr = ws.cell(r, hdr["договор секретности"]).value
        if not who or not ctr:
            continue
        raw = ws.cell(r, hdr["ставка 120"]).value if "ставка 120" in hdr else None
        rate = _num(raw)
        if rate is None:
            rate = 0.05
        elif rate > 1:
            rate = rate / 100.0      # «10 %» и «10» — доля 0,1
        p["secret"].append({"employee": str(who).strip(), "contract": str(ctr).strip(),
                            "rate": rate})
        n += 1
    log.append(f"Лист «120_надбавка»: надбавок за гостайну: {n}")


def _read_inflow(ws, p, log, q):
    n = 0
    for r in range(2, ws.max_row + 1):
        code = ws.cell(r, 1).value
        if not code:
            continue
        p["inflow"][str(code)] = [
            _num(ws.cell(r, c).value) or 0.0 for c in range(2, 2 + MONTHS)]
        n += 1
    log.append(f"Лист «фот_по_месяцам»: поступления по договорам: {n}")
    for code, row in p["inflow"].items():
        fot = next((c["fot"] for c in p["contracts"] if c["code"] == code), None)
        if fot is not None and abs(sum(row) - fot) > 0.5:
            q.append({
                "field": f"Поступления по договору {code}",
                "raw": f"сумма за год {_fmt(sum(row))} ₽",
                "why": (f"Сумма поступлений {_fmt(sum(row))} ₽ не равна ФОТ договора "
                        f"{_fmt(fot)} ₽ с листа «договоры»."),
                "options": [f"Принять ФОТ {_fmt(fot)} ₽, поступления скорректировать",
                            f"Принять поступления {_fmt(sum(row))} ₽, исправить ФОТ"],
            })
            log.append(f"Проверка {code}: поступления ≠ ФОТ — вопрос специалисту")


def _read_settings(ws, p, log, q):
    hdr = [str(ws.cell(1, c).value or "") for c in range(1, ws.max_column + 1)]
    for c, name in enumerate(hdr, 1):
        if name:
            p["settings"][name] = ws.cell(2, c).value
    log.append(f"Лист «настройки»: параметров: {len(p['settings'])}")


def _contract_code(ws, p):
    """Шифр договора из шапки формы: «шифр C_GOZ-26», «договор № 12/26».

    Форма ФАС и структура цены называют тему и шифр в первых строках. Если в
    шапке шифра нет, строка трудоёмкости получает единственный договор
    паспорта, а если и его нет — остаётся без договора, и это видно в реестре.
    """
    head = _head_text(ws)
    m = (re.search(r"шифр[^A-Za-zА-Яа-я0-9]{0,4}([A-Za-z][A-Za-z0-9_\-./]{1,30})", head, re.I)
         or re.search(r"договор[а-я]*\s*№?\s*([A-Za-z0-9][A-Za-z0-9_\-./]{1,30})", head, re.I))
    if m:
        return m.group(1).strip(".,;")
    if len(p.get("contracts") or []) == 1:
        return p["contracts"][0]["code"]
    return None


#: Заглушка пустого образца: «ТЕМА», «хх.хх.20хх», прочерки, «Х».
_FILLER = re.compile(r"^[\W_0-9xхXХ\s]*$")
_FILLER_WORDS = ("тема", "наименование", "шифр", "код", "образец", "не указано")


def _filler(text):
    """Значение из пустого образца, а не настоящее (SOURCE-004, SOURCE-005).

    Образцы форм заполнены заглушками, и договор, заведённый из «ТЕМА», хуже
    отсутствующего: его потом ищут глазами среди настоящих.
    """
    t = re.sub(r"\s+", " ", str(text or "")).strip(" «»\"'.,;:()")
    if len(t) < 3:
        return True
    return bool(_FILLER.match(t)) or t.lower().replace("ё", "е") in _FILLER_WORDS


def _head_text(ws):
    """Шапка формы: первые строки, где стоят тема и шифр."""
    return " ".join(str(ws.cell(r, c).value or "")
                    for r in range(1, min(14, ws.max_row) + 1)
                    for c in range(1, min(12, ws.max_column) + 1))


def _contract_subject(ws):
    """Тема работы из шапки формы (SOURCE-005).

    Структура цены пишет «на выполнение … работы по теме:» и тему в ёлочках,
    форма 9 РКМ — «основной заработной платы на» и тему в соседней ячейке под
    пояснением «(НИР, ОКР, ее наименование, шифр или код)». Общее у них —
    ёлочки или отдельная ячейка, а не фиксированный адрес, поэтому тема
    ищется по смыслу, как и остальные поля пакета.
    """
    head = _head_text(ws)
    m = (re.search(r"по\s+теме[^«\"]{0,24}[«\"]([^»\"]{3,300})[»\"]", head, re.I)
         or re.search(r"«([^»]{3,300})»", head))
    return re.sub(r"\s+", " ", m.group(1)).strip() if m and not _filler(m.group(1)) else None


def _year_of(text, default=None):
    m = re.search(r"(20\d\d)", str(text or ""))
    return int(m.group(1)) if m else default


MONTH_NAMES = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
               "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def _date_of(v):
    """Дата из ячейки: datetime, строка «01.06.2026» или порядковый номер Excel."""
    import datetime as _dt

    if v is None or v == "":
        return None
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    s = str(v).strip()
    try:
        n = float(s)
        if 1 <= n <= 80000:
            return _dt.date(1899, 12, 30) + _dt.timedelta(days=int(n))
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return _dt.datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _stage_months(cm, date_from, date_to, year):
    """Чел.-мес. этапа — поровну по его месяцам, только месяцы этого года.

    «4 чел.-мес. с 01.06 по 30.09» — по 1,0 в июне, июле, августе и сентябре.
    Без дат — None: строка годовая, закрывается в любом месяце договора.
    """
    d1, d2 = _date_of(date_from), _date_of(date_to)
    if not d1 or not d2 or d2 < d1:
        return None
    span = [(y, m) for y in range(d1.year, d2.year + 1) for m in range(1, 13)
            if (d1.year, d1.month) <= (y, m) <= (d2.year, d2.month)]
    each = cm / len(span)
    year = year or d1.year
    months = [m for y, m in span if y == year]
    if not months:
        return None
    # Округление до 4 знаков теряло десятитысячные: 4 чел.-мес. на три месяца
    # давали 3,9999. Остаток кладётся в последний месяц — сумма сходится.
    out = {str(m): round(each, 4) for m in months}
    out[str(months[-1])] = round(each * len(months) - sum(out[str(m)] for m in months[:-1]), 4)
    return out


def _stage_month_count(date_from, date_to):
    d1, d2 = _date_of(date_from), _date_of(date_to)
    if not d1 or not d2 or d2 < d1:
        return None
    return (d2.year - d1.year) * 12 + d2.month - d1.month + 1


def _working_hour_norms(ws):
    """Норматив среднемесячных часов прямо из примечания формы.

    В используемой форме он написан в шапке: «в 2026 г. — 164,25».
    Берём норматив из документа, а не зашиваем календарь в программу.
    """
    text = " ".join(str(ws.cell(r, c).value or "")
                    for r in range(1, min(6, ws.max_row) + 1)
                    for c in range(1, ws.max_column + 1))
    # Буква «г» обязательна: иначе дата вроде 30.06.2026 принимается за
    # норматив 30,06 часа и незаметно искажает пересчёт человеко-часов.
    found = re.findall(r"(20\d{2})\s*г\.?\s*[-–—:]?\s*(\d{2,3}(?:[,.]\d+)?)", text,
                       flags=re.I)
    return {int(year): float(value.replace(",", ".")) for year, value in found}


def _fot_labor_unit(per_person, unit_cost, date_from, date_to, hours_per_month):
    """Определить, чем заполнена двусмысленная графа «мес ЛИБО час».

    Стоимость 100 000 обычно является месячной, а 600 — часовой: после
    умножения на норматив обе должны давать правдоподобную месячную стоимость.
    Длительность этапа служит второй независимой проверкой. Если признаки не
    дают единственного ответа, возвращаем None и просим человека уточнить.
    """
    cost = _num(unit_cost)
    norm = _num(hours_per_month)
    monthly_ok = cost is not None and 10_000 <= cost <= 1_000_000
    hourly_ok = (cost is not None and norm is not None
                 and 10_000 <= cost * norm <= 1_000_000)
    if monthly_ok != hourly_ok:
        return "чел.-мес." if monthly_ok else "чел.-ч"

    per = _num(per_person)
    span = _stage_month_count(date_from, date_to)
    if per is not None and span is not None and per > span + 0.05:
        return "чел.-ч" if norm else None
    return None


def _add_labor(p, code, year, position, cm, cost, headcount, where,
               date_from=None, date_to=None, months=None, stage=None,
               work_type=None, total_cost=None, labor_per_person=None,
               cells=None, labor_unit="чел.-мес.", source_total_labor=None,
               source_unit_cost=None, hours_per_month=None):
    """Строка трудоёмкости паспорта; одинаковые должности одного договора
    складываются: в «Расшифровке ФОТ» одна должность идёт по этапам, и у
    каждого этапа свои месяцы — они складываются в один план по месяцам."""
    # Расчёту нужна одна помесячная позиция по должности, а проверяющему —
    # каждая исходная строка формы. Сохраняем обе проекции: агрегат остаётся
    # входом оптимизатора, details отвечает, что именно прочитано в документе.
    def text(v):
        return str(v).strip() if v not in (None, "") else None

    def day(v):
        d = _date_of(v)
        return d.strftime("%d.%m.%Y") if d else text(v)

    detail = {"stage": text(stage), "work_type": text(work_type),
              "position": text(position), "headcount": headcount or None,
              "labor_unit": labor_unit,
              "labor_per_person": labor_per_person,
              "source_total_labor": source_total_labor,
              "source_unit_cost": source_unit_cost,
              "hours_per_month": hours_per_month,
              "person_months": cm, "avg_cost": cost,
              "total_cost": total_cost, "from": day(date_from),
              "to": day(date_to), "where": where, "cells": cells}
    detail = {k: v for k, v in detail.items() if v is not None}

    if months is None:
        months = _stage_months(cm, date_from, date_to, year)
    if months:
        cm = round(sum(months.values()), 4)
    key = (code, year, (position or "").strip().lower())
    for row in p["labor"]:
        if (row["contract"], row["year"], (row["position"] or "").strip().lower()) == key:
            total = row["person_months"] + cm
            row["avg_cost"] = ((row["avg_cost"] or 0) * row["person_months"] + cost * cm) / total
            row["person_months"] = total
            row["headcount"] = max(row["headcount"] or 0, headcount or 0) or None
            # План по месяцам складывается, только если он есть у обеих строк:
            # иначе неизвестно, куда положить чел.-мес. этапа без дат.
            if row.get("months") and months:
                for m, v in months.items():
                    row["months"][m] = round(row["months"].get(m, 0.0) + v, 4)
            else:
                row["months"] = None
            row.setdefault("details", []).append(detail)
            return
    p["labor"].append({"contract": code, "year": year, "position": (position or "").strip(),
                       "person_months": cm, "avg_cost": cost,
                       "headcount": headcount or None, "months": months,
                       "details": [detail], "место": where})


def _read_labor(ws, p, log, q):
    """Лист «трудоемкость_по_договорам» шаблона: строка = договор, должность,
    чел.-мес., стоимость чел.-мес., количество человек."""
    hdr = {str(ws.cell(1, c).value or "").strip().lower(): c
           for c in range(1, ws.max_column + 1)}
    pm_col = hdr.get("трудоемкость") or hdr.get("трудоёмкость") or hdr.get("чел-мес")
    cost_col = (hdr.get("средняя стоимость выполнения работ в месяц")
                or hdr.get("средняя зарплата") or hdr.get("стоимость 1 чел-мес"))
    if "договор" not in hdr or not pm_col:
        log.append(f"Лист «{ws.title}»: заголовки не совпали с шаблоном, пропущен")
        return
    n = 0
    for r in range(2, ws.max_row + 1):
        code = ws.cell(r, hdr["договор"]).value
        cm = _num(ws.cell(r, pm_col).value)
        if not code or not cm:
            continue
        head_col = hdr.get("количество человек") or hdr.get("кол-во человек")
        months = {}
        for m, name in enumerate(MONTH_NAMES, start=1):
            if name in hdr:
                v = _num(ws.cell(r, hdr[name]).value)
                if v:
                    months[str(m)] = v
        _add_labor(p, str(code), _year_of(ws.cell(r, hdr["год"]).value) if "год" in hdr else None,
                   str(ws.cell(r, hdr["должность"]).value or "") if "должность" in hdr else "",
                   cm, _num(ws.cell(r, cost_col).value) if cost_col else None,
                   _num(ws.cell(r, head_col).value) if head_col else None,
                   f"лист «{ws.title}», строка {r}", months=months or None)
        n += 1
    log.append(f"Лист «{ws.title}»: шаблон fot-planner, строк трудоёмкости: {n}")


# ── пакет договора по смыслу граф (SOURCE-004) ─────────────────
#: Ключи адресов ячеек — те же, что читает маска «Извлечённые данные».
_CELL_KEYS = {"position": "position", "headcount": "headcount",
              "per_person": "labor_per_person", "person_months": "person_months",
              "unit_cost": "avg_cost", "total": "total_cost", "date_from": "from",
              "date_to": "to", "stage": "stage", "work_type": "work_type"}


def _addr(ws, r, c):
    return "%s!%s%d" % (ws.title, get_column_letter(c), r)


def _text_cells(ws, r, upto=30):
    """Текстовые ячейки строки слева направо: (колонка, текст)."""
    out = []
    for c in range(1, min(ws.max_column, upto) + 1):
        v = ws.cell(r, c).value
        if isinstance(v, str) and re.search(r"[а-яa-z]", v.lower()):
            out.append((c, v.strip()))
    return out


def _real_date(v):
    """Дата из ячейки, но не номер графы: «8.1» Excel принял бы за январь 1900."""
    d = _date_of(v)
    return d if d and d.year >= 2000 else None


def _sheet_year(ws):
    """Год листа: «…_26», «(26г.)», «2026» в названии, иначе «на 2026 год» в шапке."""
    m = re.search(r"(20\d\d)", ws.title)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:_|\()\s*(\d\d)\s*(?:г\.?)?\s*\)?\s*$", ws.title)
    if m:
        return 2000 + int(m.group(1))
    head = " ".join(str(ws.cell(r, c).value or "")
                    for r in range(1, min(12, ws.max_row) + 1)
                    for c in range(1, min(12, ws.max_column) + 1))
    m = re.search(r"на\s+(20\d\d)\s*г", head)
    return int(m.group(1)) if m else None


def _day(d):
    return d.strftime("%d.%m.%Y") if d else None


def _labor_data_row(ws, r, cols):
    """Строка с данными: должность словами и число в графах трудоёмкости или
    стоимости (ноль образца — тоже число). Номера граф «4 | 5 | 6» — не данные."""
    c, _row = cols["position"]
    pos = ws.cell(r, c).value
    if not isinstance(pos, str) or not re.search(r"[а-яa-z]", pos.lower()):
        return False
    return any(isinstance(ws.cell(r, cols[f][0]).value, (int, float))
               for f in ("person_months", "per_person", "unit_cost", "headcount") if f in cols)


def _labor_by_meaning(ws, code, p, log, q, periods):
    """Трудоёмкость из таблицы любой формы: должность, люди, чел.-мес., стоимость.

    Шапка находится по подписям граф. Строки групп («2026 год, в т.ч.»,
    «Этап 1») задают период следующих строк, итоги пропускаются — их
    содержимое уже разложено по должностям. Возвращает число строк или None,
    если таблицы трудоёмкости на листе нет.
    """
    head = None
    for top in range(1, min(ws.max_row, 60) + 1):
        cols = fields.header_columns(ws, top, min(top + 2, ws.max_row), fields.LABOR)
        if all(need & cols.keys() for need in fields.LABOR_REQUIRED):
            # Шапка продолжается до первой строки с данными: в расшифровке ФОТ
            # подписи «НАЧАЛО ЭТАПА» стоят в строке номеров граф, ниже основной.
            last = min(top + 2, ws.max_row)
            while last < min(top + 6, ws.max_row) and not _labor_data_row(ws, last + 1, cols):
                last += 1
            head = fields.header_columns(ws, top, last, fields.LABOR)
            break
    if head is None:
        return None
    col = {f: c for f, (c, _r) in head.items()}
    bottom = max(r for _c, r in head.values())
    # Единицу трудоёмкости называет подпись графы: «чел./мес.», «час» или
    # двусмысленное «мес ЛИБО час» — тогда решает правдоподобие стоимости.
    unit_head = fields.norm(" ".join(str(ws.cell(r, c).value or "")
                                     for f, (c, r) in head.items()
                                     if f in ("per_person", "person_months")))
    hour_norms = _working_hour_norms(ws)
    sheet_year = _sheet_year(ws)
    ctx = {"stage": None, "year": None, "from": None, "to": None}
    rows = empty = bad = 0
    for r in range(bottom + 1, ws.max_row + 1):
        def get(f):
            return ws.cell(r, col[f]).value if f in col else None

        stage_text = str(get("stage") or "")
        ym = re.search(r"(20\d\d)\s*год", stage_text, re.I)
        sm = re.search(r"этап\w*\s*(\d+)", stage_text, re.I)
        label = ("%s год" % ym.group(1)) if ym else (("Этап %s" % sm.group(1)) if sm else None)
        if (label and label != ctx["stage"]
                and not fields.norm(stage_text).startswith(("итого", "всего"))):
            ctx.update({"stage": label, "from": None, "to": None})
            if ym:
                ctx["year"] = int(ym.group(1))
        span = re.findall(r"\d{2}\.\d{2}\.20\d{2}", str(get("period") or ""))
        if len(span) >= 2:
            ctx["from"], ctx["to"] = span[0], span[1]

        pos = get("position")
        if not isinstance(pos, str) or not re.search(r"[а-яa-z]", pos.lower()):
            continue
        row_norm = fields.norm(" ".join(t for _c, t in _text_cells(ws, r)))
        if fields.norm(pos) in ("х", "x") or any(m in row_norm for m in fields.TOTAL_MARKS):
            continue
        hc, per, pm, cost, tot = (_num(get(f)) for f in
                                  ("headcount", "per_person", "person_months",
                                   "unit_cost", "total"))
        if pm is None and per is not None and hc:
            pm = per * hc
        if cost is None and tot and pm:
            cost = tot / pm
        if not pm or not cost or pm <= 0 or cost <= 0:
            if any(v is not None for v in (hc, per, pm, cost, tot)):
                empty += 1
            continue
        d_from = get("date_from") if _real_date(get("date_from")) else ctx["from"]
        d_to = get("date_to") if _real_date(get("date_to")) else ctx["to"]
        year = ((_real_date(d_from).year if _real_date(d_from) else None)
                or ctx["year"] or sheet_year)
        norm_h = hour_norms.get(year)
        if "час" in unit_head and "мес" in unit_head:
            unit = _fot_labor_unit(per, cost, d_from, d_to, norm_h)
        elif "час" in unit_head:
            unit = "чел.-ч"
        else:
            unit = "чел.-мес."
        if unit is None:
            q.append({
                "field": f"Единица трудоёмкости строки «{pos}» (лист «{ws.title}», строка {r})",
                "raw": f"{_fmt(pm)}; подпись графы допускает месяцы или часы",
                "why": "Без единицы одно и то же число меняет загрузку сотрудника в сотни раз.",
                "options": ["Это человеко-месяцы", "Это человеко-часы"],
            })
            continue
        if unit == "чел.-ч" and not norm_h:
            q.append({
                "field": f"Норматив рабочих часов за {year or 'год'}",
                "raw": f"{_fmt(pm)} чел.-ч",
                "why": "Для перевода человеко-часов в человеко-месяцы нужен норматив года.",
                "options": ["Указать среднемесячные рабочие часы"],
            })
            continue
        cm, sal = (pm / norm_h, cost * norm_h) if unit == "чел.-ч" else (pm, cost)
        if tot is not None and abs(pm * cost - tot) > 1:
            bad += 1
            q.append({
                "field": f"Строка «{pos}» (лист «{ws.title}», строка {r})",
                "raw": f"{_fmt(pm)} {unit} по {_fmt(cost)} ₽, итог {_fmt(tot)} ₽",
                "why": (f"Соотношение формы: {_fmt(pm)} × {_fmt(cost)} = {_fmt(pm * cost)} ₽, "
                        f"а в итоге строки {_fmt(tot)} ₽."),
                "options": [f"Принять расчётное {_fmt(pm * cost)} ₽",
                            f"Оставить из документа {_fmt(tot)} ₽"],
            })
        _add_labor(p, code, year, pos.strip(), cm, sal, hc,
                   f"лист «{ws.title}», строка {r}",
                   date_from=d_from, date_to=d_to, stage=ctx["stage"],
                   work_type=get("work_type"),
                   total_cost=tot if tot is not None else round(pm * cost, 2),
                   labor_per_person=(per if per is not None
                                     else (round(pm / hc, 4) if hc else None)),
                   cells={_CELL_KEYS[f]: _addr(ws, r, c) for f, c in col.items()
                          if f in _CELL_KEYS},
                   labor_unit=unit, source_total_labor=pm, source_unit_cost=cost,
                   hours_per_month=norm_h if unit == "чел.-ч" else None)
        rows += 1
        periods.append({"stage": ctx["stage"], "year": year,
                        "from": _real_date(d_from), "to": _real_date(d_to),
                        "ozp": tot if tot is not None else pm * cost,
                        "sheet": ws.title, "row": r})
    if rows:
        log.append(f"Лист «{ws.title}»: трудоёмкость — строк {rows}"
                   + (f", несоответствий формуле {bad}" if bad else "")
                   + (f", договор {code}" if code else ", договор не определён"))
    elif empty:
        log.append(f"Лист «{ws.title}»: трудоёмкость — образец без значений, строк {empty}")
    return rows


def _norms_by_meaning(ws, code, p, log):
    """Нормативы года: подпись строки и число в графе «Значение»."""
    head_row = value_col = unit_col = None
    for r in range(1, min(ws.max_row, 40) + 1):
        for c, t in _text_cells(ws, r, 20):
            if fields.norm(t) == fields.NORMS_VALUE:
                head_row, value_col = r, c
                break
        if head_row:
            break
    if head_row is None:
        return 0
    for c, t in _text_cells(ws, head_row, 20):
        if fields.norm(t).startswith(fields.norm("единица")):
            unit_col = c
    year = _sheet_year(ws)
    n = 0
    for r in range(head_row + 1, ws.max_row + 1):
        got = label = None
        for _c, t in _text_cells(ws, r, value_col - 1):
            got = fields.best(t, fields.NORMS)
            if got:
                label = t
                break
        if not got:
            continue
        value = _num(ws.cell(r, value_col).value)
        if not value:
            continue
        unit = ws.cell(r, unit_col).value if unit_col else None
        p["values"].append({"contract": code, "field": got[0], "year": year, "stage": None,
                            "value": value, "text": None,
                            "unit": str(unit).strip() if unit else None,
                            "label": label[:300], "place": _addr(ws, r, value_col)})
        n += 1
    if n:
        log.append(f"Лист «{ws.title}»: нормативы" + (f" {year} года" if year else "")
                   + f" — величин {n}")
    return n


def _period_costs_by_meaning(ws, code, p, log):
    """ФОТ по этапам: статьи расходов строками, этапы графами."""
    head_row, stages = None, {}
    for r in range(1, min(ws.max_row, 30) + 1):
        cells = _text_cells(ws, r)
        found = {}
        for c, t in cells:
            m = fields.STAGE.match(t)
            if m:
                found[c] = "Этап %s" % m.group(1)
        # Строка данных тоже бывает «Этап 1» — у шапки статей рядом стоит
        # итог или название статей, либо этапов больше одного.
        headish = any(h in fields.norm(t) for c, t in cells if c not in found
                      for h in fields.PERIOD_COSTS_HEAD)
        if found and (len(found) >= 2 or headish):
            head_row, stages = r, found
            break
    if not stages:
        return 0
    first = min(stages)
    n = 0
    for r in range(head_row + 1, ws.max_row + 1):
        for _c, t in _text_cells(ws, r, first - 1):
            got = fields.best(t, fields.PERIOD_COSTS)
            if not got:
                continue
            if any(x in fields.norm(t) for x in fields.PERIOD_COSTS_EXCLUDE):
                break
            for sc, stage in sorted(stages.items()):
                value = _num(ws.cell(r, sc).value)
                if value:
                    p["values"].append({"contract": code, "field": got[0], "year": None,
                                        "stage": stage, "value": value, "text": None,
                                        "unit": "руб.", "label": t[:300],
                                        "place": _addr(ws, r, sc)})
                    n += 1
            break
    if n:
        log.append(f"Лист «{ws.title}»: ФОТ по этапам — величин {n}")
    return n


def _term_by_meaning(ws):
    """Срок договора: пара граф «начало» и «окончание» с датами под ними."""
    for r in range(1, min(ws.max_row, 40) + 1):
        heads = {fields.norm(t): c for c, t in _text_cells(ws, r)}
        c1, c2 = heads.get(fields.TERM_FROM), heads.get(fields.TERM_TO)
        if not (c1 and c2):
            continue
        for rr in range(r + 1, min(r + 6, ws.max_row) + 1):
            d1, d2 = _real_date(ws.cell(rr, c1).value), _real_date(ws.cell(rr, c2).value)
            if d1 and d2 and d2 >= d1:
                return d1, d2, "%s, %s" % (_addr(ws, rr, c1), _addr(ws, rr, c2))
    return None


def _year_shares(d1, d2):
    """Доли периода по годам — по числу месяцев в каждом году."""
    months = [(y, m) for y in range(d1.year, d2.year + 1) for m in range(1, 13)
              if (d1.year, d1.month) <= (y, m) <= (d2.year, d2.month)]
    count = {}
    for y, _m in months:
        count[y] = count.get(y, 0) + 1
    return [(y, n / len(months)) for y, n in sorted(count.items())]


def _package_fot(p, q, code, periods):
    """Основная зарплата периодов — в величины пакета по годам.

    Статьи расходов по этапам и строки трудоёмкости говорят об одних и тех же
    деньгах. Если статьи есть, год этапа берётся из дат его строк, а
    расхождение сумм спрашивается. Статей нет — основная зарплата периода
    складывается из строк трудоёмкости.
    """
    by_period, dates, rows = {}, {}, {}
    for x in periods:
        key = x["stage"] or ("%s год" % x["year"] if x["year"] else None)
        if not key:
            continue
        by_period[key] = by_period.get(key, 0.0) + (x["ozp"] or 0.0)
        rows.setdefault(key, []).append((x["sheet"], x["row"]))
        if x["from"] and x["to"]:
            d = dates.get(key)
            dates[key] = ((min(d[0], x["from"]), max(d[1], x["to"])) if d
                          else (x["from"], x["to"]))
    staged = [v for v in p["values"] if v["stage"] and v["year"] is None]
    articles = {v["stage"]: v["value"] for v in staged
                if v["field"] == "основная заработная плата"}
    for stage, total in sorted(articles.items()):
        if stage in by_period and abs(by_period[stage] - total) > 1:
            q.append({
                "field": f"Основная заработная плата, {stage}",
                "raw": (f"по статьям расходов {_fmt(total)} ₽, "
                        f"по строкам трудоёмкости {_fmt(by_period[stage])} ₽"),
                "why": "Одна и та же сумма этапа в двух местах пакета расходится.",
                "options": ["Верны статьи расходов", "Верны строки трудоёмкости"],
            })
    staged_ids = {id(v) for v in staged}
    out = []
    for v in p["values"]:
        span = dates.get(v["stage"]) if id(v) in staged_ids else None
        if span:
            for year, share in _year_shares(*span):
                out.append(dict(v, year=year, value=round(v["value"] * share, 2),
                                text="%s–%s" % (_day(span[0]), _day(span[1]))))
        else:
            out.append(v)
    if not articles:
        for key, total in sorted(by_period.items()):
            span = dates.get(key)
            year = int(key[:4]) if key.endswith("год") else None
            shares = _year_shares(*span) if span else [(year, 1.0)]
            sheet = rows[key][0][0]
            numbers = [r for _s, r in rows[key]]
            place = (f"лист «{sheet}», строки {min(numbers)}–{max(numbers)}"
                     if len(numbers) > 1 else f"лист «{sheet}», строка {numbers[0]}")
            for y, share in shares:
                out.append({"contract": code, "field": "основная заработная плата",
                            "year": y, "stage": key, "value": round(total * share, 2),
                            "text": ("%s–%s" % (_day(span[0]), _day(span[1]))) if span else None,
                            "unit": "руб.", "label": "итог строк трудоёмкости",
                            "place": place})
    p["values"] = out


def _package_term(p, code, term, periods):
    """Срок договора: из пары граф «начало — окончание», иначе по срокам этапов."""
    if term:
        d1, d2, place = term
        label = "начало и окончание"
    else:
        spans = [(x["from"], x["to"]) for x in periods if x["from"] and x["to"]]
        if not spans:
            return
        d1, d2 = min(a for a, _b in spans), max(b for _a, b in spans)
        place, label = None, "сроки этапов"
    for field, day in (("срок: начало", d1), ("срок: окончание", d2)):
        p["values"].append({"contract": code, "field": field, "year": None, "stage": None,
                            "value": None, "text": _day(day), "unit": None,
                            "label": label, "place": place})


def _read_by_meaning(sheets, p, log, q, contract=None):
    """Листы вне шаблона сервиса — по смыслу граф (SOURCE-004).

    Договор — тот, в который загружен документ; без него — шифр из шапки
    любого листа книги. Лист без нужных граф не читается и в журнале не
    перечисляется поимённо: в РКМ таких листов два десятка.
    """
    code = contract or next((c for ws in sheets for c in [_contract_code(ws, p)] if c), None)
    if not contract and code and not _filler(code):
        # Файл брошен в реестр без договора: шифр и тема из шапки заводят его
        # сами (SOURCE-005). Заглушка образца договором не становится.
        p["contract_hint"] = {
            "code": code,
            "subject": next((s for ws in sheets for s in [_contract_subject(ws)] if s), None)}
    periods, term = [], None
    used = skipped = 0
    for ws in sheets:
        n = _labor_by_meaning(ws, code, p, log, q, periods)
        k = _period_costs_by_meaning(ws, code, p, log)
        m = _norms_by_meaning(ws, code, p, log)
        t = _term_by_meaning(ws) if n is None and not k else None
        if t and term is None:
            term = t
        if n is None and not k and not m and not t:
            skipped += 1
        else:
            used += 1
    _package_fot(p, q, code, periods)
    _package_term(p, code, term, periods)
    if skipped:
        log.append(f"Листов без нужных граф: {skipped} — не читались")
    return used


def _readiness(p):
    """Достаточно ли данных для запуска решателя."""
    missing = []
    if not p["employees"]:
        missing.append("сотрудники")
    if not p["contracts"]:
        missing.append("договоры")
    hint = ""
    if missing:
        hint = ("Загрузите штатное расписание и договоры или заполненный "
                "шаблон fot-planner")
    return {"ok": not missing, "missing": missing, "hint": hint}


# ── правки паспорта командами чата ─────────────────────────────

HELP = ("Команды: «покажи договоры», «покажи сотрудников», "
        "«E001 зарплата 150000», «C_GOZ фот 5200000», «рассчитай»")


def apply_chat(passport, text):
    """Разбор простых правок. Возвращает (ответ, изменено?, запустить_расчёт?)."""
    t = (text or "").strip()
    low = t.lower()
    if not t:
        return HELP, False, False
    if low in ("помощь", "help", "?"):
        return HELP, False, False
    if low.startswith("покажи"):
        if "договор" in low:
            lines = [f"{c['code']}: ФОТ {_fmt(c['fot'])} ₽, виды: {', '.join(c['kinds']) or '—'}"
                     for c in passport["contracts"]]
            return "\n".join(lines) or "Договоров в паспорте нет", False, False
        if "сотрудник" in low:
            lines = [f"{e['code']} {e['fio']}: {e['pos']}, {_fmt(e['sal'])} ₽"
                     for e in passport["employees"]]
            return "\n".join(lines) or "Сотрудников в паспорте нет", False, False
        return HELP, False, False
    if low.startswith(("рассчитай", "расчет", "расчёт", "запусти")):
        return "Запускаю решатель на исправленном паспорте", False, True

    m = re.match(r"^(\S+)\s+(зарплата|фот|ставка)\s+([\d\s.,]+)$", t, re.I)
    if m:
        code, field, val = m.group(1), m.group(2).lower(), _num(m.group(3))
        if val is None:
            return "Не разобрал число в правке", False, False
        if field == "фот":
            for c in passport["contracts"]:
                if c["code"].lower() == code.lower():
                    old = c["fot"]
                    c["fot"] = val
                    return (f"Договор {c['code']}: ФОТ {_fmt(old)} → {_fmt(val)} ₽. "
                            "Поступления по месяцам пересоберу пропорционально."), True, False
            return f"Договор «{code}» в паспорте не найден", False, False
        for e in passport["employees"]:
            if e["code"].lower() == code.lower():
                old = e[{"зарплата": "sal", "ставка": "rate"}[field]]
                e[{"зарплата": "sal", "ставка": "rate"}[field]] = val
                return (f"{e['code']} {e['fio']}: {field} "
                        f"{_fmt(old)} → {_fmt(val)}"), True, False
        return f"Сотрудник «{code}» в паспорте не найден", False, False

    return "Не понял правку. " + HELP, False, False


# предельные размеры «оклад + 122» по должностям (приказ 2556)
LIMIT_2556 = {
    "инженер": 110000, "инженер 1 категории": 120000, "инженер 2 категории": 120000,
    "ведущий инженер": 140000, "программист": 110000, "ведущий программист": 140000,
    "научный сотрудник": 110000, "старший научный сотрудник": 120000,
    "аналитик": 110000, "ведущий специалист": 140000, "специалист": 110000,
}


def _months(e):
    """Сколько месяцев планируемого года сотрудник числится."""
    f = str(e.get("from") or "")
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", f)
    if m:
        mm, yy = int(m.group(2)), int(m.group(3))
    else:
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", f)
        if not m:
            return 12
        yy, mm = int(m.group(1)), int(m.group(2))
    if yy != 2026:
        return 12
    return max(0, 13 - mm)


def rebuild_funds(passport):
    """Точный раскрой фондов под текущие зарплаты паспорта.

    Штатная часть (оклад и 122) идёт на договор, где разрешён оклад;
    остаток сверх предельного размера — на договор с надбавкой 124 и приказом.
    Пропорциональное масштабирование здесь не годится: решатель фиксирует
    стадии лексикографически и на неточном раскрое возвращает INFEASIBLE.
    """
    contracts = passport["contracts"]
    if not contracts:
        return None
    host = next((c for c in contracts if "оклад" in c["kinds"]), contracts[0])
    flex = next((c for c in contracts if "приказ" in c["kinds"] or "124" in c["kinds"]),
                None)
    fund = {c["code"]: 0.0 for c in contracts}
    for e in passport["employees"]:
        sal = e["sal"] or 0
        months = _months(e)
        lim = LIMIT_2556.get(str(e["pos"]).strip().lower(), sal)
        staff = min(sal, lim)
        rest = max(0.0, sal - staff)
        fund[host["code"]] += staff * months
        if rest > 0:
            if flex is None:
                return {"error": "Нет договора, где разрешены 124 или приказ: "
                                 "остаток сверх предельного размера выплатить нечем"}
            fund[flex["code"]] += rest * months
    for c in contracts:
        c["fot"] = round(fund[c["code"]])
    return {"host": host["code"], "flex": flex["code"] if flex else None,
            "funds": {k: round(v) for k, v in fund.items()}}


def passport_to_input(passport, template_path, out_path, warnings=None,
                      substitutions=None):
    """Собрать вход fot-planner из паспорта с точным раскроем фондов.

    ``warnings`` — список, куда складываются предупреждения для экономиста:
    расчет идет, но о том, что в него не попало, надо сказать вслух.

    ``substitutions`` — правила замещения из реестра организации, парами
    (должность, кем можно заместить). Раньше они лежали в сервисе и в расчет
    не попадали: листа для них во входном файле не было вовсе.
    """
    info = rebuild_funds(passport)
    if info and info.get("error"):
        raise ValueError(info["error"])

    wb = openpyxl.load_workbook(template_path)

    ws = wb["сотрудники"]
    ws.delete_rows(2, ws.max_row)
    for e in passport["employees"]:
        ws.append([e["code"], e["fio"], e["pos"], "", e["rate"], "основное",
                   "основной", e["sal"], e.get("from") or "01.01.2026",
                   "31.12.2026", None, None])

    ws = wb["договоры"]
    chdr = [str(ws.cell(1, c).value or "") for c in range(1, ws.max_column + 1)]
    fot_c = chdr.index("фот") + 1
    # Не только фонд: признак ГОЗ и срок действия тоже приходят из паспорта.
    # Раньше переносился один фонд, а ГОЗ брался из шаблона — и правка,
    # сделанная экономистом, до расчета не доходила, хотя в таблице стояла.
    also = {}
    for key, column in (("goz", "гоз"), ("from", "дата начала"),
                        ("to", "дата окончания"), ("name", "название"),
                        ("num", "номер"), ("type", "тип договора")):
        if column in chdr:
            also[key] = chdr.index(column) + 1
    by_code = {c["code"]: c for c in passport["contracts"]}
    fots = {code: c["fot"] for code, c in by_code.items()}
    seen = set()
    for r in range(2, ws.max_row + 1):
        code = str(ws.cell(r, 1).value or "")
        c = by_code.get(code)
        if c is None:
            continue
        seen.add(code)
        ws.cell(r, fot_c).value = c["fot"]
        for key, col in also.items():
            if c.get(key) not in (None, ""):
                ws.cell(r, col).value = c[key]
    missing = [code for code in by_code if code not in seen]
    if missing and warnings is not None:
        # Строки в шаблоне нет — значит договор в расчет не вошел, ни фондом,
        # ни признаком ГОЗ. Раньше это происходило молча и давало правдоподобный,
        # но чужой ответ. Расчет не срываем: у шаблона свои договоры, и он
        # считается. Но молчать об этом нельзя.
        warnings.append(
            "В шаблоне расчета нет договоров: %s. Их условия в расчет не вошли — "
            "считалось по договорам шаблона." % ", ".join(sorted(missing)))

    ws = wb["фот_по_месяцам"]
    for r in range(2, ws.max_row + 1):
        code = str(ws.cell(r, 1).value or "")
        if code not in fots:
            continue
        old_row = [_num(ws.cell(r, c).value) or 0 for c in range(2, 14)]
        total = sum(old_row)
        target = fots[code]
        if total > 0:
            k = target / total
            acc = 0
            for i, col in enumerate(range(2, 13)):
                v = round(old_row[i] * k)
                ws.cell(r, col).value = v
                acc += v
            ws.cell(r, 13).value = round(target - acc)
        else:
            base = target // 12
            for col in range(2, 13):
                ws.cell(r, col).value = base
            ws.cell(r, 13).value = target - base * 11

    if substitutions is not None:
        _write_substitutions(wb, substitutions)

    wb.save(out_path)
    return out_path


def _write_substitutions(wb, pairs):
    """Переписать лист правил замещения содержимым реестра.

    Правила общие для организации и заменяются целиком: новая редакция — новый
    перечень, а не добавка к прежнему. Лист создается, если его нет: файл мог
    быть собран старым шаблоном.
    """
    name = "правила_замещения"
    if name in wb.sheetnames:
        ws = wb[name]
        if ws.max_row > 1:
            ws.delete_rows(2, ws.max_row)
    else:
        ws = wb.create_sheet(name)
        ws.cell(1, 1).value = "должность"
        ws.cell(1, 2).value = "может быть замещена"
    for position, replaced_by in pairs:
        if position:
            ws.append([position, replaced_by or ""])


# ── сценарные запросы ──────────────────────────────────────────

#: Основы названий месяцев для фраз чата. Имя своё: прежнее «MONTH_NAMES»
#: здесь затирало список полных названий выше, и лист трудоёмкости по
#: месяцам читал только «март» и «август» (найдено демо-комплектом
#: 30.09.2026).
MONTH_STEMS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}


def _find_month(text):
    low = text.lower()
    best = None
    for stem, num in MONTH_STEMS.items():
        i = low.find(stem)
        if i >= 0 and (best is None or i < best[0]):
            best = (i, num)
    return best[1] if best else None


def _find_contract(text, passport):
    """Договор по коду, номеру, типу или признаку ГОЗ."""
    low = text.lower()
    for c in passport.get("contracts", []):
        if c["code"].lower() in low:
            return c
    if "гоз" in low or "оборон" in low:
        for c in passport.get("contracts", []):
            if str(c.get("goz", "")).lower() == "да":
                return c
    for key, words in (("грант", ("грант",)), ("внебюджет", ("внебюджет", "вб")),
                       ("госзадан", ("госзадан",))):
        if any(w in low for w in words):
            for c in passport.get("contracts", []):
                if key in str(c.get("name", "")).lower() or key in str(c.get("code", "")).lower():
                    return c
    return None


def _find_employee(text, passport):
    low = text.lower()
    for e in passport.get("employees", []):
        if e["code"].lower() in low:
            return e
        fam = str(e.get("fio", "")).split(" ")[0].lower()
        if fam and len(fam) > 3 and fam[:-1] in low:
            return e
    return None


def parse_scenario(text, passport):
    """Разобрать требование в набор изменений входа.

    Возвращает {ops, title, what} либо {error, hint}.
    Понимает: изменение фонда договора в процентах, увольнение сотрудника
    с месяца, перенос поступления, изменение зарплаты, запрет вида выплаты.
    """
    t = (text or "").strip()
    low = t.lower()
    if not t:
        return {"error": "Пустой запрос", "hint": SCENARIO_HINT}

    ops = []

    # сокращение или увеличение фонда договора
    m = re.search(r"(сократ|урез|уменьш|снизи|увелич|подним|добав)\w*[^\d%]{0,40}"
                  r"(\d{1,3})\s*%", low)
    if m:
        pct = int(m.group(2))
        sign = -1 if m.group(1)[:5] in ("сокра", "урез", "уменьш", "снизи") else 1
        c = _find_contract(t, passport)
        if c is None:
            return {"error": "Не понял, какой договор менять",
                    "hint": "Укажите код договора, например " +
                            (passport["contracts"][0]["code"] if passport.get("contracts") else "C_GOZ")}
        ops.append({"kind": "fund", "contract": c["code"], "factor": 1 + sign * pct / 100.0})
        return {"ops": ops,
                "title": ("Сокращение" if sign < 0 else "Увеличение") +
                         " фонда договора " + c["code"] + " на " + str(pct) + " %",
                "what": ("Фонд и поступления договора " + c["code"] + " " +
                         ("уменьшены" if sign < 0 else "увеличены") + " на " + str(pct) +
                         " %, дефицит разрешён, остальные условия неизменны.")}

    # увольнение сотрудника
    if re.search(r"уволь|увольн|уход\w*|выбыва", low):
        e = _find_employee(t, passport)
        mon = _find_month(t) or 7
        if e is None:
            return {"error": "Не понял, кто увольняется",
                    "hint": "Укажите код сотрудника, например " +
                            (passport["employees"][0]["code"] if passport.get("employees") else "E001")}
        ops.append({"kind": "dismiss", "employee": e["code"], "month": mon})
        return {"ops": ops,
                "title": "Увольнение " + e["code"] + " с " + str(mon) + "-го месяца",
                "what": ("У сотрудника " + e["code"] + " (" + e.get("fio", "") +
                         ") установлена дата окончания в " + str(mon) +
                         "-м месяце, дефицит разрешён.")}

    # перенос поступления
    if re.search(r"перенес|перенос|задерж|сдвин|позже", low):
        c = _find_contract(t, passport)
        if c is None:
            return {"error": "Не понял, по какому договору переносить поступление",
                    "hint": "Укажите код договора"}
        shift = 1
        m2 = re.search(r"на\s+(\d+)\s*мес", low)
        if m2:
            shift = int(m2.group(1))
        ops.append({"kind": "delay", "contract": c["code"], "months": shift})
        return {"ops": ops,
                "title": "Задержка первого поступления " + c["code"] + " на " + str(shift) + " мес.",
                "what": ("Первое поступление договора " + c["code"] + " перенесено на " +
                         str(shift) + " мес. позже, дефицит разрешён.")}

    # изменение зарплаты
    m3 = re.search(r"(\S+)\s+зарплат\w*\s+([\d\s]+)", low)
    if m3:
        e = _find_employee(t, passport)
        val = _num(m3.group(2))
        if e and val:
            ops.append({"kind": "salary", "employee": e["code"], "value": val})
            return {"ops": ops,
                    "title": "Зарплата " + e["code"] + " равна " + _fmt(val) + " руб.",
                    "what": ("Месячная оплата труда сотрудника " + e["code"] +
                             " изменена на " + _fmt(val) + " руб., фонды пересобраны.")}

    # запрет или разрешение вида выплаты
    m4 = re.search(r"(запрет|разреш)\w*\s+(оклад|120|122|124|152|приказ)", low)
    if m4:
        c = _find_contract(t, passport)
        if c is None:
            return {"error": "Не понял, на каком договоре менять виды выплат",
                    "hint": "Укажите код договора"}
        allow = m4.group(1).startswith("разреш")
        kind = m4.group(2)
        ops.append({"kind": "allow", "contract": c["code"], "payment": kind, "allow": allow})
        return {"ops": ops,
                "title": ("Разрешение" if allow else "Запрет") + " выплаты " + kind +
                         " на договоре " + c["code"],
                "what": ("Для договора " + c["code"] + " вид выплаты " + kind + " " +
                         ("разрешён" if allow else "запрещён") + ", дефицит разрешён.")}

    return {"error": "Требование не разобрано", "hint": SCENARIO_HINT}


SCENARIO_HINT = (
    "Понимаю запросы вида: «сократить фонд ГОЗ на 15 %», "
    "«увольнение E002 в июне», «перенести поступление C_GOZ на месяц», "
    "«E001 зарплата 150000», «запретить приказ на C_FLEX»."
)


def apply_scenario(src_path, ops, out_path):
    """Применить операции сценария к входному файлу."""
    wb = openpyxl.load_workbook(src_path)
    emp, ctr, fm = wb["сотрудники"], wb["договоры"], wb["фот_по_месяцам"]
    ehdr = {str(emp.cell(1, c).value or "").strip().lower(): c
            for c in range(1, emp.max_column + 1)}
    chdr = {str(ctr.cell(1, c).value or "").strip().lower(): c
            for c in range(1, ctr.max_column + 1)}

    for op in ops:
        if op["kind"] == "fund":
            k = op["factor"]
            for r in range(2, ctr.max_row + 1):
                if str(ctr.cell(r, 1).value or "") == op["contract"]:
                    ctr.cell(r, chdr["фот"]).value = round(
                        (_num(ctr.cell(r, chdr["фот"]).value) or 0) * k)
            for r in range(2, fm.max_row + 1):
                if str(fm.cell(r, 1).value or "") == op["contract"]:
                    for c in range(2, 14):
                        fm.cell(r, c).value = round((_num(fm.cell(r, c).value) or 0) * k)
        elif op["kind"] == "dismiss":
            col = ehdr.get("дата окончания", 10)
            last_day = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30,
                        7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}[op["month"]]
            for r in range(2, emp.max_row + 1):
                if str(emp.cell(r, 1).value or "") == op["employee"]:
                    emp.cell(r, col).value = "%02d.%02d.2026" % (last_day, op["month"])
        elif op["kind"] == "delay":
            for r in range(2, fm.max_row + 1):
                if str(fm.cell(r, 1).value or "") != op["contract"]:
                    continue
                for c in range(2, 14):
                    v = _num(fm.cell(r, c).value) or 0
                    if v > 0:
                        tgt = min(13, c + op["months"])
                        fm.cell(r, c).value = 0
                        fm.cell(r, tgt).value = (_num(fm.cell(r, tgt).value) or 0) + v
                        break
        elif op["kind"] == "salary":
            for r in range(2, emp.max_row + 1):
                if str(emp.cell(r, 1).value or "") == op["employee"]:
                    emp.cell(r, ehdr["зарплата"]).value = op["value"]
        elif op["kind"] == "allow":
            names = {"оклад": "оклад разрешен", "120": "120 разрешена",
                     "122": "122 разрешена", "124": "124 разрешена",
                     "152": "152 разрешена", "приказ": "стимулирующая приказом разрешена"}
            col = chdr.get(names.get(op["payment"], ""))
            if col:
                for r in range(2, ctr.max_row + 1):
                    if str(ctr.cell(r, 1).value or "") == op["contract"]:
                        ctr.cell(r, col).value = "да" if op["allow"] else "нет"

    # дефицит разрешаем, чтобы сценарий давал план, а не отказ
    st = wb["настройки"]
    shdr = {str(st.cell(1, c).value or "").strip(): c for c in range(1, st.max_column + 1)}
    if "разрешить дефицит" in shdr:
        st.cell(2, shdr["разрешить дефицит"]).value = "да"

    wb.save(out_path)
    return out_path
