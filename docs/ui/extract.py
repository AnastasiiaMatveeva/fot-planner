# -*- coding: utf-8 -*-
"""Якорный разбор документов для агента извлечения данных.

Детерминированный слой: опознание листа по маркерам, извлечение целевых
показателей, проверка расчётными соотношениями самих форм. Возвращает
паспорт, журнал действий на русском и вопросы специалисту.
"""
import io
import re

import openpyxl

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


def extract(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    log, questions = [], []
    passport = {"employees": [], "contracts": [], "inflow": {}, "labor": [],
                "secret": [], "settings": {},
                "source": path.split("\\")[-1].split("/")[-1]}
    recognized = 0

    for ws in wb.worksheets:
        kind = _classify(ws)
        if kind == "skip":
            continue
        if kind is None:
            log.append(f"Лист «{ws.title}»: маркер формы не найден, пропущен")
            continue
        recognized += 1
        handler = {
            "employees": _read_employees,
            "contracts": _read_contracts,
            "inflow": _read_inflow,
            "settings": _read_settings,
            "price": _read_price,
            "fot_detail": _read_fot_detail,
            "form9": _read_form9,
            "labor": _read_labor,
            "secret": _read_secret,
        }[kind]
        handler(ws, passport, log, questions)

    if not recognized:
        log.append("Ни один лист не опознан: ожидались листы шаблона "
                   "fot-planner, «Структура цены», «Расшифровка ФОТ» или форма 9д")
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
    head = " ".join(str(ws.cell(r, c).value or "")
                    for r in range(1, min(12, ws.max_row) + 1)
                    for c in range(1, min(8, ws.max_column) + 1)).lower()
    if "структура цены" in head:
        return "price"
    if "расшифровка" in head and "оплату труда" in head:
        return "fot_detail"
    if "форма № 9" in head or "9 (9д)" in head or "основной заработной платы" in head:
        return "form9"
    return None


def _read_employees(ws, p, log, q):
    hdr = {str(ws.cell(1, c).value or "").strip().lower(): c
           for c in range(1, ws.max_column + 1)}
    need = ["код строки", "фио", "должность", "ставка", "зарплата"]
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
            "sal": _num(ws.cell(r, hdr["зарплата"]).value) or 0.0,
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


def _read_price(ws, p, log, q):
    """«Структура цены»: строка 1.3.1 — основная заработная плата."""
    found, has_row = None, False
    for r in range(1, ws.max_row + 1):
        a = str(ws.cell(r, 1).value or "").strip()
        b = str(ws.cell(r, 2).value or "").lower()
        if a == "1.3.1" or "основная заработная плата" in b:
            has_row = True
            for c in range(3, min(ws.max_column, 12) + 1):
                v = _num(ws.cell(r, c).value)
                if v:
                    found = (r, v)
                    break
            if found:
                break
    if found:
        p.setdefault("price", {})["ozp"] = found[1]
        log.append(f"Лист «{ws.title}»: маркер «СТРУКТУРА ЦЕНЫ», "
                   f"строка 1.3.1 = {_fmt(found[1])} ₽ (строка {found[0]})")
    elif has_row:
        log.append(f"Лист «{ws.title}»: форма-образец, строка 1.3.1 найдена, "
                   "но суммы не заполнены")
    else:
        log.append(f"Лист «{ws.title}»: маркер найден, "
                   "строка 1.3.1 не обнаружена")


def _contract_code(ws, p):
    """Шифр договора из шапки формы: «шифр C_GOZ-26», «договор № 12/26».

    Форма ФАС и структура цены называют тему и шифр в первых строках. Если в
    шапке шифра нет, строка трудоёмкости получает единственный договор
    паспорта, а если и его нет — остаётся без договора, и это видно в реестре.
    """
    head = " ".join(str(ws.cell(r, c).value or "")
                    for r in range(1, min(14, ws.max_row) + 1)
                    for c in range(1, min(12, ws.max_column) + 1))
    m = (re.search(r"шифр[^A-Za-zА-Яа-я0-9]{0,4}([A-Za-z][A-Za-z0-9_\-./]{1,30})", head, re.I)
         or re.search(r"договор[а-я]*\s*№?\s*([A-Za-z0-9][A-Za-z0-9_\-./]{1,30})", head, re.I))
    if m:
        return m.group(1).strip(".,;")
    if len(p.get("contracts") or []) == 1:
        return p["contracts"][0]["code"]
    return None


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
    out = {str(m): round(each, 4) for y, m in span if y == year}
    return out or None


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


def _read_fot_detail(ws, p, log, q):
    """«Расшифровка ФОТ»: строки этапов, проверка гр.6×гр.7=гр.8.

    Из каждой строки берётся должность, кол-во человек, чел.-мес. и месячная
    зарплата — это трудоёмкость договора по должностям. Даты этапа (графы
    «начало» и «окончание») раскладывают его чел.-мес. по месяцам; этапы
    одной должности складываются в один план по месяцам. Год — из дат этапа,
    если они есть, иначе из шапки.
    """
    rows, bad, empty, hours_rows = 0, 0, 0, 0
    code = _contract_code(ws, p)
    hour_norms = _working_hour_norms(ws)
    head_year = _year_of(" ".join(str(ws.cell(r, c).value or "")
                                  for r in range(1, min(6, ws.max_row) + 1)
                                  for c in range(1, min(12, ws.max_column) + 1)))
    for r in range(1, ws.max_row + 1):
        if _is_column_numbering(ws, r):
            continue
        pos = ws.cell(r, 4).value
        source_total = _num(ws.cell(r, 7).value)
        source_cost = _num(ws.cell(r, 8).value)
        tot = _num(ws.cell(r, 9).value)
        if not pos or source_total is None or source_cost is None or tot is None:
            continue
        if source_total <= 0 or source_cost <= 0:
            empty += 1
            continue
        if str(pos).strip().lower() in ("должность", "4"):
            continue
        date_from, date_to = ws.cell(r, 10).value, ws.cell(r, 11).value
        year = _year_of(date_from) or _year_of(date_to) or head_year
        norm = hour_norms.get(year)
        per_person = _num(ws.cell(r, 6).value)
        unit = _fot_labor_unit(per_person, source_cost, date_from, date_to, norm)
        if unit is None:
            q.append({
                "field": f"Единица трудоёмкости строки «{pos}» (строка листа {r})",
                "raw": f"{_fmt(source_total)}; заголовок формы допускает месяцы или часы",
                "why": "Без единицы одно и то же число меняет загрузку сотрудника в сотни раз.",
                "options": ["Это человеко-месяцы", "Это человеко-часы"],
            })
            continue
        if unit == "чел.-ч" and not norm:
            q.append({
                "field": f"Норматив рабочих часов за {year or 'год'}",
                "raw": f"{_fmt(source_total)} чел.-ч",
                "why": "Для перевода человеко-часов в человеко-месяцы нужен норматив года.",
                "options": ["Указать среднемесячные рабочие часы"],
            })
            continue

        rows += 1
        cm = source_total
        sal = source_cost
        if unit == "чел.-ч":
            hours_rows += 1
            cm = source_total / norm
            sal = source_cost * norm
        calc = source_total * source_cost
        if abs(calc - tot) > 1:
            bad += 1
            q.append({
                "field": f"Строка «{pos}» (строка листа {r})",
                "raw": f"{_fmt(source_total)} {unit} по {_fmt(source_cost)} ₽, итог {_fmt(tot)} ₽",
                "why": (f"Соотношение формы: {_fmt(source_total)} × {_fmt(source_cost)} = {_fmt(calc)} ₽, "
                        f"а в итоге строки {_fmt(tot)} ₽."),
                "options": [f"Принять расчётное {_fmt(calc)} ₽",
                            f"Оставить из документа {_fmt(tot)} ₽"],
            })
        _add_labor(p, code, year, str(pos), cm, sal, _num(ws.cell(r, 5).value),
                   f"лист «{ws.title}», строка {r}",
                   date_from=date_from, date_to=date_to,
                   stage=ws.cell(r, 2).value, work_type=ws.cell(r, 3).value,
                   total_cost=tot, labor_per_person=per_person,
                   labor_unit=unit, source_total_labor=source_total,
                   source_unit_cost=source_cost,
                   hours_per_month=norm if unit == "чел.-ч" else None,
                   cells={"stage": f"{ws.title}!B{r}",
                          "work_type": f"{ws.title}!C{r}",
                          "position": f"{ws.title}!D{r}",
                          "headcount": f"{ws.title}!E{r}",
                          "labor_per_person": f"{ws.title}!F{r}",
                          "person_months": f"{ws.title}!G{r}",
                          "avg_cost": f"{ws.title}!H{r}",
                          "total_cost": f"{ws.title}!I{r}",
                          "from": f"{ws.title}!J{r}",
                          "to": f"{ws.title}!K{r}"})
    if rows == 0 and empty:
        log.append(f"Лист «{ws.title}»: форма-образец, {empty} строк без значений — "
                   "заполненных данных нет")
    else:
        log.append(f"Лист «{ws.title}»: строк трудоёмкости: {rows}, "
                   f"несоответствий формуле: {bad}"
                   + (f", из них в человеко-часах: {hours_rows}" if hours_rows else "")
                   + (f", договор {code}" if code else ", шифр договора в шапке не найден"))


def _is_column_numbering(ws, r, width=12):
    """Строка с номерами колонок: «1 | 2 | 3 | …».

    В формах ФАС такая строка идёт под шапкой и данными не является. Без этой
    проверки разборщик принимал номера колонок 6, 7 и 8 за трудоёмкость,
    стоимость и ОЗП и задавал экономисту вопрос, почему 6 × 7 не равно 8.
    """
    seen = []
    for c in range(1, width + 1):
        v = ws.cell(r, c).value
        if v is None or str(v).strip() == "":
            continue
        n = _num(v)
        if n is None or n != c:
            return False
        seen.append(c)
    return len(seen) >= 4


def _read_form9(ws, p, log, q):
    """Форма 9д: гр.6 (чел.-мес) × гр.7 (стоимость) = гр.8 (ОЗП).

    Строки должностей внутри года становятся трудоёмкостью договора: должность
    (гр.5), число специалистов (гр.4), чел.-мес. (гр.6), стоимость (гр.7).
    Итоговые строки года («Научные и инженерно-технические работники») и
    строка темы пропускаются — их содержимое уже разложено по должностям.
    """
    rows, bad, empty = 0, 0, 0
    code = _contract_code(ws, p)
    year = None
    date_from = date_to = None
    total_markers = ("научные и инженерно", "итого", "всего")
    for r in range(1, ws.max_row + 1):
        if _is_column_numbering(ws, r):
            continue
        b = str(ws.cell(r, 2).value or "")
        if re.search(r"20\d\d\s*год", b, re.I):
            year = _year_of(b)
        # В форме 9д общий интервал этапа/года записан над строками
        # должностей. Он ограничивает месяцы, в которых оптимизатор вправе
        # распределять указанную трудоёмкость.
        row_text = " | ".join(str(ws.cell(r, c).value or "")
                              for c in range(1, min(ws.max_column, 12) + 1))
        dates = re.findall(r"\b\d{2}\.\d{2}\.20\d{2}\b", row_text)
        if len(dates) >= 2 and ("год" in row_text.lower() or year):
            d1, d2 = _date_of(dates[0]), _date_of(dates[1])
            if d1 and d2 and d2 >= d1:
                date_from, date_to = d1, d2
        grp = ws.cell(r, 5).value
        cm = _num(ws.cell(r, 6).value)
        cost = _num(ws.cell(r, 7).value)
        ozp = _num(ws.cell(r, 8).value)
        if not grp or cm is None or cost is None or ozp is None:
            continue
        if cm <= 0 or cost <= 0:
            empty += 1
            continue
        if str(grp).strip().lower() in ("х", "x") or any(
                m in str(grp).lower() for m in total_markers):
            continue
        rows += 1
        _add_labor(p, code, year, str(grp), cm, cost, _num(ws.cell(r, 4).value),
                   f"лист «{ws.title}», строка {r}",
                   date_from=date_from, date_to=date_to,
                   stage=(f"{year} год" if year else None), total_cost=ozp,
                   labor_per_person=(cm / _num(ws.cell(r, 4).value)
                                     if _num(ws.cell(r, 4).value) else None),
                   source_total_labor=cm, source_unit_cost=cost,
                   cells={"stage": f"B{r}", "position": f"E{r}",
                          "headcount": f"D{r}", "labor_per_person": f"F{r}",
                          "source_total_labor": f"F{r}", "source_unit_cost": f"G{r}",
                          "total_cost": f"H{r}"})
        calc = cm * cost
        if abs(calc - ozp) > 1:
            bad += 1
            q.append({
                "field": f"Форма 9д, «{grp}» (строка {r})",
                "raw": f"{_fmt(cm)} чел.-мес, стоимость {_fmt(cost)} ₽, ОЗП {_fmt(ozp)} ₽",
                "why": (f"Соотношение формы: {_fmt(cm)} × {_fmt(cost)} = {_fmt(calc)} ₽, "
                        f"а ОЗП в строке {_fmt(ozp)} ₽."),
                "options": [f"Принять стоимость {_fmt(ozp / cm if cm else 0)} ₽ (из ОЗП)",
                            f"Оставить стоимость {_fmt(cost)} ₽"],
            })
    if rows == 0 and empty:
        log.append(f"Лист «{ws.title}»: форма 9д — образец, {empty} строк без значений")
    else:
        log.append(f"Лист «{ws.title}»: форма 9д, строк: {rows}, "
                   f"несоответствий формуле: {bad}")


def _readiness(p):
    """Достаточно ли данных для запуска решателя."""
    missing = []
    if not p["employees"]:
        missing.append("сотрудники")
    if not p["contracts"]:
        missing.append("договоры")
    if not p["inflow"]:
        missing.append("поступления по месяцам")
    hint = ""
    if missing:
        hint = ("Загруженные формы описывают цену договора, но не содержат "
                "штатного расписания и графика поступлений — добавьте эти данные "
                "или загрузите заполненный шаблон fot-planner")
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

    return ("Не понял правку. " + HELP +
            ". Свободные формулировки разбирает LLM-слой — в этой сборке отключён."), False, False


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

MONTH_NAMES = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}


def _find_month(text):
    low = text.lower()
    best = None
    for stem, num in MONTH_NAMES.items():
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
