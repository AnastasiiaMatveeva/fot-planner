# -*- coding: utf-8 -*-
"""Ограничения работы экономиста: числа, а не формулировки.

По каждому условию считается, насколько план к нему подошел: факт, предел,
запас и доля предела. Экономисту нужно не «соблюдено», а «использовано 92 %
предела, узкое место — Иванов, июнь: осталось 13 000 ₽». Тогда видно не
только то, что план допустим, но и где он держится на волоске.

Считается независимо от решателя: по входному файлу и по готовому плану
выплат. Условия «нарушать нельзя» — жесткие: нарушение такого условия значит
ошибку в данных или в сервисе, а не выбор между вариантами.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

#: Виды выплат: как названы в плане и в графе разрешений договора.
KINDS = {
    "оклад": "оклад разрешен",
    "120": "120 разрешена",
    "122": "122 разрешена",
    "124": "124 разрешена",
    "152": "152 разрешена",
    "приказ": "стимулирующая приказом разрешена",
}
MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль",
          "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
RU_MONTH = {m: i + 1 for i, m in enumerate(MONTHS)}
SHORT = ["янв", "фев", "мар", "апр", "май", "июн",
         "июл", "авг", "сен", "окт", "ноя", "дек"]

#: Тип условия. «Нарушать нельзя» — жесткое правило. «С допуском» —
#: отклонение возможно и штрафуется целевой функцией. «Показатель» — не
#: условие, а цифра, на которую смотрят.
HARD, SOFT, INFO = "нарушать нельзя", "с допуском", "показатель"
#: С точностью до рубля: копейки в плане ФОТ ничего не решают.
EPS = 1.0
#: Порог для человеко-месяцев. EPS — рублевый; с ним ставка за месяц (число
#: около единицы) считалась нулем, и закрытая строка выглядела незакрытой.
PM_EPS = 0.001
#: Сколько строк показывать в таблице узких мест.
SHOW = 8


def _num(v):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("\xa0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _yes(v):
    return str(v or "").strip().lower() in ("да", "true", "1", "yes", "+")


def _month(v):
    """Месяц из даты входного файла: число, дата или «01.06.2026»."""
    if v in (None, ""):
        return None
    if isinstance(v, (dt.datetime, dt.date)):
        return v.month
    s = str(v).strip()
    if "." in s:
        parts = s.split(".")
        if len(parts) >= 2 and parts[1].isdigit():
            return int(parts[1])
    n = _num(s)
    if n and 1 <= n <= 80000:      # порядковый номер даты Excel
        return (dt.date(1899, 12, 30) + dt.timedelta(days=int(n))).month
    return None


def _date_of(v):
    """Дата из ячейки входа: дата, «dd.mm.yyyy», «yyyy-mm-dd» или номер Excel.

    Месяца мало: договор 2025–2027 годов в плане на 2026 год действует с
    января по декабрь, а по одному месяцу даты начала и окончания он выходил
    действующим с месяца начала по месяц окончания — не в том году. Сотрудник,
    принятый 11.02.2019, по месяцу даты работал в 2026 году только с февраля.
    """
    if v in (None, ""):
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = str(v).strip()
    for fmt, size in (("%d.%m.%Y", 10), ("%Y-%m-%d", 10)):
        try:
            return dt.datetime.strptime(s[:size], fmt).date()
        except ValueError:
            pass
    n = _num(s)
    if n and 1 <= n <= 80000:      # порядковый номер даты Excel
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(n))
    return None


def _months_between(start, end, year):
    """Первый и последний месяц года плана внутри отрезка [start; end].

    Пустой отрезок — (13, 0): range(13, 1) пуст, и договор, не действующий в
    году плана, не получает ни одного месяца. Без года плана (старый вход без
    листа «настройки») — прежнее чтение по месяцам дат.
    """
    if year is None:
        return (start.month if start else 1), (end.month if end else 12)
    if start is None or start.year < year:
        first = 1
    else:
        first = start.month if start.year == year else 13
    if end is None or end.year > year:
        last = 12
    else:
        last = end.month if end.year == year else 0
    return first, last


def span_text(first, last):
    """Месяцы отрезка словами: «с янв по дек»; пустой — «в году плана нет»."""
    if first > last:
        return "в году плана нет"
    return "с %s по %s" % (SHORT[first - 1], SHORT[last - 1])


def _kind_of(raw):
    s = str(raw or "").strip().lower()
    if s.startswith("оклад"):
        return "оклад"
    for k in ("120", "122", "124", "152"):
        if s.startswith(k):
            return k
    if "приказ" in s:
        return "приказ"
    return None


MONTH_NAMES = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль",
               "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]


def _months_of_row(row):
    """План по месяцам из граф «Январь» … «Декабрь»; None — строка годовая."""
    out = {}
    for m, name in enumerate(MONTH_NAMES, start=1):
        v = _num(row.get(name))
        if v is None:
            v = _num(row.get(name.lower()))
        if v:
            out[m] = v
    return out or None


def _rows_of(ws):
    hdr = [str(ws.cell(1, c).value or "").strip() for c in range(1, ws.max_column + 1)]
    for r in range(2, ws.max_row + 1):
        row = {hdr[c - 1]: ws.cell(r, c).value for c in range(1, ws.max_column + 1)
               if hdr[c - 1]}
        if any(v not in (None, "") for v in row.values()):
            yield row


def _read_input(path):
    from openpyxl import load_workbook
    from fot_planner.position_reference import normalize_position

    wb = load_workbook(path, data_only=True)
    data = {"employees": [], "contracts": {}, "limits": {}, "inflow": {},
            "labor": [], "settings": {}, "year": None}
    # Год плана нужен раньше дат: по нему даты договоров и сотрудников
    # читаются месяцами именно этого года (_months_between).
    if "настройки" in wb.sheetnames:
        for row in _rows_of(wb["настройки"]):
            data["settings"] = dict(row)
            data["year"] = _num(row.get("год"))
            break
    year = int(data["year"]) if data["year"] else None
    for row in _rows_of(wb["сотрудники"]):
        code = row.get("код строки")
        if not code:
            continue
        first, last = _months_between(_date_of(row.get("дата начала")),
                                      _date_of(row.get("дата окончания")), year)
        data["employees"].append({
            "code": str(code),
            "person_code": str(row.get("табельный номер") or code),
            "fio": row.get("фио") or str(code),
            "position": str(row.get("должность") or ""),
            "department": str(row.get("подразделение") or ""),
            "rate": _num(row.get("ставка")) or 1.0,
            "salary": _num(row.get("зарплата")) or 0.0,
            "from": first,
            "to": last,
        })
    # Своя зарплата строки в месяце (правило 1, SALARY-001, 19.09.2026): в
    # остальных месяцах — зарплата листа «сотрудники».
    data["wages"] = {}
    if "зарплата_по_месяцам" in wb.sheetnames:
        for row in _rows_of(wb["зарплата_по_месяцам"]):
            code, month = row.get("код строки"), _num(row.get("месяц"))
            if code and month:
                data["wages"][(str(code), int(month))] = _num(row.get("зарплата")) or 0.0
    for row in _rows_of(wb["договоры"]):
        code = row.get("код")
        if not code:
            continue
        kinds = {k: _yes(row.get(col)) for k, col in KINDS.items()}
        start, end = _date_of(row.get("дата начала")), _date_of(row.get("дата окончания"))
        # Пустой срок выплат равен окончанию договора (SOURCE-001), как у
        # загрузчика решателя (excel/load._apply_payment_deadlines).
        salary = _date_of(row.get("конечная дата выплат оклада")) or end
        allowance = _date_of(row.get("конечная дата выплат надбавок")) or end
        m_from, m_to = _months_between(start, end, year)
        _, salary_to = _months_between(start, salary, year)
        _, allowance_to = _months_between(start, allowance, year)
        # Окно выплат — месяцы, где договор может платить хоть одним
        # разрешённым видом: от начала до позднего из сроков выплат, как
        # contract_calendar.contract_allows_month решателя. По нему решатель
        # считает ФОТ поступившим (первый месяц окна) и ровный план освоения
        # (fot_schedule.monthly_spend_targets).
        ends = ([salary_to] if kinds["оклад"] else []) + (
            [allowance_to] if any(v for k, v in kinds.items() if k != "оклад") else [])
        last = max(ends) if ends else 0
        data["contracts"][str(code)] = {
            "code": str(code), "name": row.get("название") or "",
            "goz": _yes(row.get("ГОЗ")), "fot": _num(row.get("фот")) or 0.0,
            "from": m_from, "to": m_to,
            "salary_to": salary_to, "allowance_to": allowance_to,
            "window": (m_from, last) if m_from <= last else (13, 0),
            "kinds": kinds,
        }
    if "лимиты_по_должностям" in wb.sheetnames:
        for row in _rows_of(wb["лимиты_по_должностям"]):
            pos = row.get("должность")
            if pos:
                data["limits"][normalize_position(str(pos))] = {
                    "оклад": _num(row.get("оклад")), "П2556": _num(row.get("П2556")),
                    "П4": _num(row.get("П4")), "БЭП": _num(row.get("БЭП")),
                    "категория": str(row.get("категория персонала") or "").strip() or None,
                }
    # Величины справочника, действующие не весь год (01.10.2026): месяц →
    # графы, отличные от строки. Читает их lim_at — с месяцем.
    if "лимиты_по_месяцам" in wb.sheetnames:
        for row in _rows_of(wb["лимиты_по_месяцам"]):
            pos, month = row.get("должность"), _num(row.get("месяц"))
            if not pos or not month:
                continue
            got = data["limits"].setdefault(normalize_position(str(pos)), {})
            cell = got.setdefault("_months", {}).setdefault(int(month), {})
            for field in ("оклад", "П2556", "П4", "БЭП"):
                v = _num(row.get(field))
                if v is not None:
                    cell[field] = v
    # Поступления в расчёт не подаются (SOURCE-009, правило 21): сборка входа
    # с 18.09.2026 удаляет лист «фот_по_месяцам», и чтение его без проверки
    # роняло отчёт, проверки, сводку и выгрузку каждого нового расчёта
    # (KeyError). Без листа — как у загрузчика решателя
    # (fot_schedule.default_fot_inflow_at_start): весь ФОТ договора поступает в
    # первый месяц его окна выплат. Старый вход с листом читается как был:
    # по нему тот расчёт и считался.
    if "фот_по_месяцам" in wb.sheetnames:
        ws = wb["фот_по_месяцам"]
        for r in range(2, ws.max_row + 1):
            code = ws.cell(r, 1).value
            if code:
                data["inflow"][str(code)] = [_num(ws.cell(r, c).value) or 0.0
                                             for c in range(2, 14)]
    else:
        for code, c in data["contracts"].items():
            first = c["window"][0]
            if c["fot"] > 0 and first <= 12:
                data["inflow"][code] = [c["fot"] if m == first else 0.0
                                        for m in range(1, 13)]
    if "трудоемкость_по_договорам" in wb.sheetnames:
        for row in _rows_of(wb["трудоемкость_по_договорам"]):
            if not row.get("договор"):
                continue
            # Строка РКМ другого года (этап 2026–2027) решателю этого года не
            # нужна (lp.year == ctx.year): без отбора отчёт показывал её
            # незакрытой.
            row_year = _num(row.get("год"))
            if year and row_year and int(row_year) != year:
                continue
            data["labor"].append({
                "contract": str(row["договор"]),
                "position": str(row.get("должность") or ""),
                "person_months": _num(row.get("трудоемкость")),
                "avg": _num(row.get("средняя стоимость выполнения работ в месяц")),
                "months": _months_of_row(row),
            })
    data["norm"] = normalize_position
    return data


def labor_tolerance(settings):
    """Допуск трудоёмкости расчёта: настройка, иначе значение решателя.

    Раньше отчёт брал 5 % по умолчанию, а проверки — ноль, и одна строка РКМ
    бывала «в допуске» в разделе 10 и «нарушена» в разделе 15. Больше 5 %
    допуска не бывает (правило 25) — решатель и аудит режут так же.
    """
    from fot_planner.defaults import DEFAULT_GOZ_LABOR_TOLERANCE
    from fot_planner.models import MAX_LABOR_TOLERANCE

    raw = None
    for key in ("допуск трудоёмкости", "допуск трудоемкости"):
        raw = _num((settings or {}).get(key))
        if raw is not None:
            break
    tol = DEFAULT_GOZ_LABOR_TOLERANCE if raw is None else raw
    return min(max(float(tol), 0.0), MAX_LABOR_TOLERANCE)


def open_rates(res, lim, norm):
    """Открытая ставка строки сотрудника на договоре в месяце.

    Ставку и основное место выбирает решатель и пишет листом
    «открытые_ставки»: отчёт и проверки берут её оттуда, чтобы предел П2556,
    БЭП и П4 считались на ту же ставку, что в расчёте. Без листа (старый
    результат) — оклад на договоре, делённый на оклад должности за полную
    ставку (правило 2); оклада должности в справочнике нет — ставка
    неизвестна (None). Договор, где у строки только надбавки, ставки не
    даёт: 0.
    """
    sheet = res.get("rates")
    table = None
    if sheet is not None:
        table = {}
        for r in sheet:
            key = (r["emp"], r["contract"], r["month"])
            table[key] = table.get(key, 0.0) + (r["rate"] or 0.0)

    def rate(emp, contract, month, rows):
        if table is not None:
            return table.get((emp, contract, month), 0.0)
        base = lim_at(lim, norm, rows[0]["position"], "оклад", month) if rows else None
        return (_sum(rows, {"оклад"}) / base) if base else None
    return rate


def bep_of(lim, norm):
    """БЭП должности на 1,0 ставки — так же, как у решателя.

    Должность без БЭП в справочнике берёт общее значение
    (payroll_rules.rule_goz_bep_staff_total, bep_of): наименьший БЭП
    справочника, а без единого — значение настроек решателя по умолчанию.
    Раньше такая должность прибавляла ставку в знаменатель средней, а к
    БЭП договора — ноль, и средняя по договору выходила «выше БЭП»."""
    from fot_planner.defaults import DEFAULT_GOZ_AVERAGE_SALARY_LIMIT

    known = [v["БЭП"] for v in lim.values() if v.get("БЭП") and v["БЭП"] > 0]
    default = min(known) if known else DEFAULT_GOZ_AVERAGE_SALARY_LIMIT

    def value(position, month=None):
        own = lim_at(lim, norm, position, "БЭП", month)
        return own if own and own > 0 else default
    return value


def lim_at(lim, norm, position, field, month=None):
    """Величина справочника должности в месяце: своя у месяца (лист
    «лимиты_по_месяцам»), иначе строка справочника."""
    row = lim.get(norm(position)) or {}
    if month is not None:
        v = ((row.get("_months") or {}).get(month) or {}).get(field)
        if v is not None:
            return v
    return row.get(field)


def p4_applies(category):
    """Уровень П4 действует только для категорий НТП и НР — как у решателя
    (salary_limits_2556.p4_applies_to_category)."""
    from fot_planner.salary_limits_2556 import p4_applies_to_category

    return p4_applies_to_category(category)


def fact_note(last, total=False):
    """Оговорка о месяцах факта для текста условия (правило 26).

    Условие одного месяца месяцы 1…F не проверяет: это история начислений, и
    нарушение в ней — не ошибка плана. Условие через месяцы или за год
    включает факт числом. Без факта оговорки нет.
    """
    if not last:
        return ""
    month = MONTHS[last - 1].lower()
    if total:
        return " Факт начислений по %s включительно входит в сумму числом." % month
    return " Месяцы по %s включительно — факт начислений, не проверяются." % month


def labor_band(plan, allowed, done, tol, ahead, has_future):
    """Годовой коридор строки РКМ, когда месяцы 1…F — факт (правило 26).

    Факт входит в год строки числом, а коридор ослабляется ровно на то, что
    история уже сделала сверх него или недобрала там, где добрать нельзя, —
    как у решателя и аудита результата (actuals.relaxed_labor_band). ``ahead``
    — план строки на месяцы после факта, у годовой строки — None.
    """
    from fot_planner.actuals import relaxed_labor_band

    return relaxed_labor_band(plan - allowed, plan + allowed, done, tol=tol,
                              monthly_future_plan=ahead, has_future=has_future)


def _read_result(path):
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    out = {"plan": [], "labor_control": [], "frozen": 0}
    # Пересчёт с факта (правило 26; владелец продукта 30.09.2026: «пересчёт с
    # текущими вводными… не трогая январь и февраль»): месяцы 1…F результата —
    # история начислений, а не план. Решатель пишет F строкой «Факт по месяц»
    # листа «Итог расчета», строки факта в «Плане выплат» — «источник
    # решения» fact. Старый результат без этой строки — факта нет.
    if "Итог расчета" in wb.sheetnames:
        for row in _rows_of(wb["Итог расчета"]):
            if str(row.get("показатель") or "").strip() == "Факт по месяц":
                value = _num(row.get("значение"))
                out["frozen"] = int(value) if value and 0 < value <= 11 else 0
                break
    for row in _rows_of(wb["План выплат"]):
        code = row.get("код строки")
        month = RU_MONTH.get(str(row.get("месяц") or "").strip())
        amount = _num(row.get("сумма"))
        if not code or not month or not amount:
            continue
        # Строка факта бывает и у того, кого нет в листе сотрудников (уволен
        # или снят с расчёта): ФИО и должность у неё пустые, деньги — есть.
        out["plan"].append({
            "emp": str(code), "fio": row.get("ФИО") or str(code),
            "position": str(row.get("должность") or ""),
            "month": month, "contract": str(row.get("договор") or ""),
            "kind": _kind_of(row.get("вид выплаты")), "amount": amount,
            "fact": str(row.get("источник решения") or "").strip().lower() == "fact",
        })
    # Дефицит выплат: если экономист разрешил дефицит, решатель пишет, кому и
    # сколько недоплачено; отчёт должен это показать, а не спрятать.
    if "Дефициты" in wb.sheetnames:
        out["deficits"] = []
        for row in _rows_of(wb["Дефициты"]):
            code = row.get("код строки")
            month = RU_MONTH.get(str(row.get("месяц") or "").strip()) or _num(row.get("месяц"))
            if not code or not month:
                continue
            out["deficits"].append({
                "emp": str(code), "fio": row.get("ФИО") or str(code), "month": int(month),
                "due": _num(row.get("требовалось выплатить")) or 0.0,
                "paid": _num(row.get("выплачено")) or 0.0,
                "gap": _num(row.get("дефицит")) or 0.0,
                "why": row.get("причина") or "",
            })
    # Открытые ставки решателя: ставка и признак основного места по месяцам.
    if "открытые_ставки" in wb.sheetnames:
        out["rates"] = []
        for row in _rows_of(wb["открытые_ставки"]):
            code, month = row.get("код строки"), _num(row.get("месяц"))
            if not code or not month:
                continue
            out["rates"].append({
                "emp": str(code), "contract": str(row.get("договор") or ""),
                "month": int(month), "rate": _num(row.get("ставка")) or 0.0,
                "main": _yes(row.get("основное")),
            })
    if "Контроль трудоёмкости" in wb.sheetnames:
        ws = wb["Контроль трудоёмкости"]
        # У договора несколько строк РКМ, и под каждой — своя таблица по
        # месяцам («Показатель»). Раньше эта таблица выключала разбор строк
        # насовсем, и от договора читалась только первая строка: контроль
        # человеко-месяцев проверял «Старшего научного сотрудника» и молчал
        # про инженеров.
        current, in_rules, in_months = None, False, False
        for r in range(1, ws.max_row + 1):
            text = str(ws.cell(r, 1).value or "").strip()
            if text.startswith("ДОГОВОР:"):
                current, in_rules, in_months = (
                    text.replace("ДОГОВОР:", "").strip(), False, False)
                continue
            if text == "Строка / группа":
                in_rules, in_months = True, False
                continue
            if text == "Показатель":
                in_months = True
                continue
            if not text:
                in_months = False
                continue
            # Разбивка решателя: кто и сколько закрыл по каждой строке.
            if text == "Договор" and str(ws.cell(r, 4).value or "").strip() == "Сотрудник":
                in_rules, in_months = False, False
                continue
            ind = str(ws.cell(r, 6).value or "").strip()
            if ind in ("Закрыто чел.-мес.", "Всего денег на строку"):
                vals = [_num(ws.cell(r, 7 + i).value) or 0.0 for i in range(12)]
                out.setdefault("labor_people", []).append({
                    "contract": text, "row": str(ws.cell(r, 2).value or "").strip(),
                    "fio": str(ws.cell(r, 4).value or "").strip(),
                    "position": str(ws.cell(r, 5).value or "").strip(),
                    "kind": "pm" if ind.startswith("Закрыто") else "pay", "months": vals,
                })
                continue
            # Заголовок разбивки («Что сформировало трудоёмкость») стоит в той
            # же графе, что и строки, но чисел под ним нет.
            if in_rules and not in_months and _num(ws.cell(r, 2).value) is not None:
                out["labor_control"].append({
                    "contract": current or "", "row": text,
                    "plan_pm": _num(ws.cell(r, 2).value),
                    "fact_pm": _num(ws.cell(r, 3).value),
                    "plan_sum": _num(ws.cell(r, 5).value),
                    "fact_sum": _num(ws.cell(r, 6).value),
                })
    return out


def _by(plan, *keys):
    out = {}
    for p in plan:
        out.setdefault(tuple(p[k] for k in keys), []).append(p)
    return out


def _sum(rows, kinds=None):
    return sum(p["amount"] for p in rows if kinds is None or p["kind"] in kinds)


def _mo(v):
    if v is None:
        return "—"
    v = float(v)
    if abs(v) < 100 and abs(v - round(v)) > 0.001:
        s = ("%.2f" % abs(v)).rstrip("0").rstrip(".").replace(".", ",")
    else:
        s = "{:,.0f}".format(round(abs(v))).replace(",", " ")
    return ("−" if v < -0.0001 else "") + s


def _mark(out, section):
    for r in out:
        r.setdefault("раздел", section)


# ── сборка условия ───────────────────────────────────────────────────
def _limit_rule(name, meaning, where, unit, rows, kind=HARD, mode="не больше",
                empty="", tolerance=0.0, absolute_tolerance=None):
    """Условие с числовым пределом: факт против предела в каждом случае.

    ``rows`` — [{"объект", "факт", "предел"}]. Считается доля предела, запас
    и самые узкие места: не только «соблюдено», но и насколько близко план
    подошел к границе и что сломается от первой же правки.
    """
    checked = []
    for r in rows:
        limit, fact = r.get("предел"), r.get("факт")
        if limit in (None, 0) or fact is None:
            continue
        allowed = max(EPS if absolute_tolerance is None else absolute_tolerance,
                      abs(limit) * tolerance)
        over = (fact > limit + EPS) if mode == "не больше" else abs(fact - limit) > allowed + 1e-7
        # Свой коридор строки — годовая трудоёмкость, ослабленная фактом
        # прошедших месяцев (labor_band): норма та же, допуск шире ровно на
        # сделанное историей.
        if r.get("коридор") is not None:
            low, high = r["коридор"]
            over = not (low - 1e-6 <= fact <= high + 1e-6)
        checked.append({"объект": r["объект"], "факт": round(fact, 2),
                        "предел": round(limit, 2), "доля": round(fact / limit, 4),
                        "запас": round(limit - fact, 2), "нарушено": over})
    if not checked:
        return {"правило": name, "смысл": meaning, "где": where, "тип": kind,
                "состояние": "не применялось", "единица": unit, "проверено": 0,
                "факт": empty, "строки": [], "нарушений": 0, "предел_общий": None,
                "режим": mode}
    broken = [c for c in checked if c["нарушено"]]
    tight = sorted(checked, key=lambda c: -c["доля"])
    worst = tight[0]
    # Итог — формальной фразой с единицей (решение владельца 30.09.2026:
    # «нужно жёстко формализовать»): прежние «узкое место» и «запас» были
    # разговорными и для нормы-равенства говорили не то.
    if mode == "не больше":
        fact_text = ("Наибольшая доля предела %d %%: %s; %s %s %s"
                     % (round(worst["доля"] * 100), worst["объект"],
                        "до предела" if worst["запас"] >= 0 else "сверх предела",
                        _mo(abs(worst["запас"])), unit))
    else:
        big = max(checked, key=lambda c: abs(c["запас"]))
        fact_text = ("Отклонений от нормы сверх допуска: %d из %d"
                     % (len(broken), len(checked)))
        if abs(big["запас"]) >= 0.005:
            fact_text += ("; наибольшее отклонение %s: %s %s"
                          % (big["объект"], _mo(-big["запас"]), unit))
    # Что показывать в таблице. При равенстве интересны только расхождения:
    # тридцать шесть одинаковых строк «160 000 из 160 000, запас 0» — шум.
    # При пределе интересны самые узкие места, но одинаковые случаи
    # сворачиваются в один со счетчиком.
    candidates = broken if mode == "равно" else broken + [c for c in tight
                                                          if not c["нарушено"]]
    shown, seen = [], {}
    for c in candidates:
        key = (c["факт"], c["предел"])
        if key in seen:
            seen[key]["таких же"] = seen[key].get("таких же", 1) + 1
            continue
        seen[key] = c
        shown.append(c)
    limits = {c["предел"] for c in checked}
    # «Нужно» — обиходное слово; у нормы-равенства (П4, зарплата месяца,
    # трудоёмкость) вторая величина — норма, у потолка — предел.
    return {"правило": name, "смысл": meaning, "где": where, "тип": kind,
            "состояние": "нарушено" if broken else "соблюдено", "единица": unit,
            "проверено": len(checked), "факт": fact_text,
            "использовано": round(worst["доля"], 4),
            "подпись предела": "норма" if mode == "равно" else "предел",
            "режим": mode,
            "предел_общий": checked[0]["предел"] if len(limits) == 1 else None,
            "строки": shown[:SHOW], "нарушений": len(broken)}


def _count_rule(name, meaning, where, checked, bad, kind=HARD, empty="", unit=""):
    """Условие без числового предела: сошлось или нет и в скольких случаях."""
    if not checked:
        return {"правило": name, "смысл": meaning, "где": where, "тип": kind,
                "состояние": "не применялось", "единица": unit, "проверено": 0,
                "факт": empty, "строки": [], "нарушений": 0, "предел_общий": None}
    return {"правило": name, "смысл": meaning, "где": where, "тип": kind,
            "состояние": "нарушено" if bad else "соблюдено", "единица": unit,
            "проверено": checked, "нарушений": len(bad), "предел_общий": None,
            "факт": "Проверено случаев: %d, нарушений: %d" % (checked, len(bad)),
            "строки": bad[:SHOW]}


#: Формальное название условия для протокола контроля (раздел 15) и выгрузки.
#: Ключ — «правило»: по нему условие знают матрица проверок
#: (scripts/qa_matrix.py, LINKS), список замечаний (issues.js, RULE_SECTION)
#: и чат, поэтому ключ не переименовывается, а экран показывает название
#: (владелец продукта 30.09.2026: «нужно жёстко формализовать»).
TITLES = {
    "Человек получает всю месячную зарплату":
        "Заработная плата за месяц выплачена полностью",
    "Оклад идет по ставке, а не произвольной суммой":
        "Оклад равен окладу должности на открытую ставку",
    "Надбавка 122 — с того же договора, что и оклад":
        "Надбавка 122 выплачивается с договора оклада",
    "Платим только теми видами, которые разрешены договором":
        "Виды выплат разрешены договором",
    "Режим «оклад плюс 152»": "Надбавка 152 без других надбавок в месяце",
    "Договор платит только в свои сроки": "Выплаты в срок действия договора",
    "Деньги нельзя потратить раньше, чем они пришли":
        "Остаток ФОТ договора на конец месяца не отрицательный",
    "Выплаты не превышают ФОТ договора": "Выплаты не превышают ФОТ договора",
    "ФОТ договоров освоен": "ФОТ договоров освоен полностью",
    "П2556: оклад и 122 не выше предела по должности":
        "Приказ № 2556: оклад и надбавка 122 не выше предела на открытую ставку",
    "БЭП: средняя зарплата по ГОЗ-договору в пределах базовой":
        "БЭП: оклад и надбавка 122 по ГОЗ-договору не выше БЭП на открытые ставки",
    "П4 при надбавке за интенсивность":
        "Приказ № 4: штатная часть в месяц надбавки 124 равна уровню П4",
    "Суммарная ставка — не больше 1,5": "Суммарная открытая ставка не больше 1,5",
    "Совместительство — не больше 0,5 ставки": "Совместительство не больше 0,5 ставки",
    "Плановые человеко-месяцы закрыты":
        "Трудоёмкость строки РКМ закрыта с учётом допуска",
    "Сумма трудоемкости выбрана":
        "Стоимость трудоёмкости строки РКМ выплачена с учётом допуска",
    "Работу закрывает подходящая должность": "Строку РКМ закрывает допустимая должность",
    "Дефицит выплат разрешен настройками":
        "Недоплата заработной платы разрешена параметрами расчёта",
}


def check(input_path, result_path):
    """Все условия и их состояние в этом плане, в числах."""
    inp = _read_input(input_path)
    res = _read_result(result_path)
    plan = res["plan"]
    emps = {e["code"]: e for e in inp["employees"]}
    ctr, norm, lim = inp["contracts"], inp["norm"], inp["limits"]
    # Месяцы 1…F — факт начислений (правило 26; владелец продукта 30.09.2026:
    # «пересчёт с текущими вводными… не трогая январь и февраль»). Условия
    # одного месяца их не проверяют: нарушение в истории — не ошибка плана.
    # Условия через месяцы и за год берут факт числом.
    last = res.get("frozen") or 0
    ahead = [p for p in plan if p["month"] > last]
    by_em = _by(ahead, "emp", "month")
    fact_single, fact_total = fact_note(last), fact_note(last, total=True)
    out = []

    def base_of(position, month=None):
        return lim_at(lim, norm, position, "оклад", month)

    open_rate = open_rates(res, lim, norm)

    def rate_of(rows):
        """Открытая ставка строки на договоре в месяце (rows — один такой
        набор): та же, что в разделах 8 и 9 отчёта (open_rates)."""
        if not rows:
            return None
        return open_rate(rows[0]["emp"], rows[0]["contract"], rows[0]["month"], rows)

    # ── зарплата и выплаты ───────────────────────────────────────────
    rows = []
    wages = inp.get("wages") or {}
    for e in inp["employees"]:
        for m in range(max(e["from"], last + 1), e["to"] + 1):
            # Своя сумма месяца проверяется и нулевой: её задали явно.
            own = (e["code"], m) in wages
            due = wages[(e["code"], m)] if own else e["salary"]
            if own or due:
                rows.append({"объект": "%s, %s" % (e["fio"], SHORT[m - 1]),
                             "факт": _sum(by_em.get((e["code"], m), [])),
                             "предел": due})
    out.append(_limit_rule(
        "Человек получает всю месячную зарплату",
        "Заработная плата месяца — сумма из таблицы «Заработная плата по "
        "месяцам» — выплачивается целиком окладом и надбавками (правило 1); "
        "недоплата — только если её разрешают параметры расчёта." + fact_single,
        "План выплат", "₽", rows, mode="равно"))

    checked, bad = 0, []
    for (code, m, c), rr in _by(ahead, "emp", "month", "contract").items():
        oklad, base = _sum(rr, {"оклад"}), base_of(rr[0]["position"], m)
        if not oklad or not base:
            continue
        checked += 1
        rate = oklad / base
        if abs(rate * 4 - round(rate * 4)) > 0.02:
            bad.append({"объект": "%s, %s, %s" % (rr[0]["fio"], SHORT[m - 1], c),
                        "что": "оклад %s ₽ соответствует ставке %s, шаг ставки 0,25"
                               % (_mo(oklad), _mo(round(rate, 3)))})
    out.append(_count_rule(
        "Оклад идет по ставке, а не произвольной суммой",
        "Оклад на договоре — оклад должности, умноженный на открытую на нём "
        "ставку с шагом 0,25 (правило 2, RATE-001); остаток заработной платы "
        "выплачивается надбавками." + fact_single,
        "Реестр → Справочник должностей", checked, bad,
        empty="в справочнике нет окладов по этим должностям"))

    checked, bad = 0, []
    for (code, m), rr in by_em.items():
        salary_ctr = {p["contract"] for p in rr if p["kind"] == "оклад"}
        for p in rr:
            if p["kind"] != "122":
                continue
            checked += 1
            if salary_ctr and p["contract"] not in salary_ctr:
                bad.append({"объект": "%s, %s" % (p["fio"], SHORT[m - 1]),
                            "что": "122 на %s, оклад на %s"
                                   % (p["contract"], ", ".join(sorted(salary_ctr)))})
    out.append(_count_rule(
        "Надбавка 122 — с того же договора, что и оклад",
        "Надбавка 122 выплачивается с того договора, с которого в этом месяце "
        "выплачивается оклад строки (правило 9)." + fact_single,
        "План выплат", checked, bad, empty="122 в этом плане не назначалась"))

    bad = []
    for p in ahead:
        c = ctr.get(p["contract"])
        if c and p["kind"] and not c["kinds"].get(p["kind"], False):
            bad.append({"объект": "%s, %s, %s" % (p["fio"], SHORT[p["month"] - 1],
                                                  p["contract"]),
                        "что": "вид «%s» договором запрещен" % p["kind"]})
    out.append(_count_rule(
        "Платим только теми видами, которые разрешены договором",
        "С договора выплачиваются только разрешённые им виды выплат: оклад, "
        "120, 122, 124, 152, выплата приказом (правило 8)." + fact_single,
        "Реестр → Договоры", len(ahead), bad))

    checked, bad = 0, []
    for (code, m), rr in by_em.items():
        kinds = {p["kind"] for p in rr}
        if "152" not in kinds:
            continue
        checked += 1
        extra = kinds - {"оклад", "152"}
        if extra:
            bad.append({"объект": "%s, %s" % (rr[0]["fio"], SHORT[m - 1]),
                        "что": "вместе со 152 назначены %s" % ", ".join(sorted(extra))})
    out.append(_count_rule(
        "Режим «оклад плюс 152»",
        "В месяце, где строке назначена надбавка 152, других надбавок и выплаты "
        "приказом у этой строки нет (правило 11)." + fact_single,
        "План выплат", checked, bad, empty="152 в этом плане не используется"))
    _mark(out, "Зарплата и выплаты")

    # ── договоры и деньги ────────────────────────────────────────────
    bad, seen = [], set()
    for p in ahead:
        c = ctr.get(p["contract"])
        if c and not (c["from"] <= p["month"] <= c["to"]):
            key = "%s, %s" % (p["contract"], SHORT[p["month"] - 1])
            if key not in seen:
                seen.add(key)
                bad.append({"объект": key,
                            "что": "договор действует %s" % span_text(c["from"], c["to"])})
    out.append(_count_rule(
        "Договор платит только в свои сроки",
        "Выплаты с договора — только в месяцы от даты начала до даты окончания "
        "договора в году плана." + fact_single,
        "Реестр → Договоры", len(ahead), bad))

    # Остаток ФОТ договора на конец месяца: ФОТ, поступивший к этому месяцу,
    # за вычетом выплат нарастающим итогом. Поступления в расчёт не подаются
    # (SOURCE-009): весь ФОТ считается поступившим в первый месяц окна выплат
    # (_read_input), поэтому минус значит выплату раньше начала выплат по
    # договору или выплаты сверх ФОТ. Касса договора отдельной величиной не
    # показывается — это запрещено тем же решением.
    cash_rows, worst = [], None
    for code in sorted(ctr):
        got = inp["inflow"].get(code) or [0.0] * 12
        paid = [0.0] * 12
        for p in plan:
            if p["contract"] == code:
                paid[p["month"] - 1] += p["amount"]
        if not any(got) and not any(paid):
            continue
        # Месяцы факта входят в остаток числом, но не судятся: история могла
        # уйти в минус, и план её не покроет — он лишь не растит перерасход,
        # как решатель (ФОТ после факта — не меньше нуля). Судится остаток
        # месяцев плана против уровня, с которым история их оставила.
        bal, floor, low, low_m, low_bal = 0.0, 0.0, None, 1, 0.0
        for m in range(12):
            bal += got[m] - paid[m]
            if m + 1 <= last:
                floor = min(0.0, bal)
                continue
            if low is None or bal - floor < low:
                low, low_m, low_bal = bal - floor, m + 1, bal
        if low is None:
            continue
        cash_rows.append({"объект": "%s, %s" % (code, SHORT[low_m - 1]),
                          "что": ("наименьший остаток ФОТ на конец месяца %s ₽"
                                  % _mo(low_bal)) if low >= -EPS else
                                 ("выплаты нарастающим итогом больше поступившего "
                                  "ФОТ на %s ₽" % _mo(-low)),
                          "низ": round(low_bal, 2), "нарушено": low < -EPS})
        if worst is None or low < worst["низ"]:
            worst = cash_rows[-1]
    cash = {
        "правило": "Деньги нельзя потратить раньше, чем они пришли",
        "смысл": "ФОТ договора считается поступившим целиком в первый месяц "
                 "выплат по договору (правило 21, SOURCE-009). Остаток ФОТ на "
                 "конец месяца — ФОТ за вычетом выплат нарастающим итогом; меньше "
                 "нуля он не бывает." + fact_total + fact_single,
        "где": "Освоение ФОТ нарастающим итогом", "тип": HARD, "единица": "₽",
        "предел_общий": 0.0, "проверено": len(cash_rows),
        "состояние": ("не применялось" if not cash_rows else
                      ("нарушено" if any(r["нарушено"] for r in cash_rows) else "соблюдено")),
        "нарушений": sum(1 for r in cash_rows if r["нарушено"]),
        "факт": ("Наименьший остаток ФОТ на конец месяца: %s, %s ₽"
                 % (worst["объект"], _mo(worst["низ"]))
                 if worst else "Выплат и ФОТ нет"),
        "строки": sorted(cash_rows, key=lambda r: r["низ"])[:SHOW],
    }
    out.append(cash)

    fact_paid = {}
    for p in plan:
        if p["month"] <= last:
            fact_paid[p["contract"]] = fact_paid.get(p["contract"], 0.0) + p["amount"]
    rows = [{"объект": code, "факт": sum(p["amount"] for p in plan if p["contract"] == code),
             "предел": c["fot"]} for code, c in sorted(ctr.items()) if c["fot"]]
    out.append(_limit_rule(
        "Выплаты не превышают ФОТ договора",
        "ФОТ договора — верхняя граница всех выплат с него за год (правило 23); "
        "если факт прошедших месяцев один уже больше ФОТ, после факта с договора "
        "не платится ничего." + fact_total,
        "Реестр → Договоры", "₽",
        [dict(r, предел=max(r["предел"], fact_paid.get(r["объект"], 0.0))) for r in rows]))

    spend = _limit_rule(
        "ФОТ договоров освоен",
        "Нераспределённый ФОТ на конец года расчёт не останавливает (правило 23): "
        "это ФОТ договора, не выплаченный за год." + fact_total,
        "Договоры", "₽", rows, kind=INFO)
    if spend["состояние"] != "не применялось":
        left = [r for r in spend["строки"] if r["запас"] > EPS]
        fot = sum(r["предел"] for r in rows)
        paid = sum(r["факт"] for r in rows)
        spend["состояние"] = "внимание" if left else "соблюдено"
        spend["использовано"] = round(paid / fot, 4) if fot else None
        spend["факт"] = ("Выплаты по плану %s ₽ из ФОТ %s ₽, освоение %d %%; "
                         "нераспределённый ФОТ %s ₽"
                         % (_mo(paid), _mo(fot), round(100 * paid / fot) if fot else 0,
                            _mo(fot - paid)))
        spend["строки"] = sorted(spend["строки"], key=lambda r: -r["запас"])[:SHOW]
    out.append(spend)
    _mark(out, "Договоры и деньги")

    # ── лимиты по должностям ─────────────────────────────────────────
    rows = []
    for (code, m, c), rr in _by(ahead, "emp", "month", "contract").items():
        p2556 = lim_at(lim, norm, rr[0]["position"], "П2556", m)
        rate = rate_of(rr)
        if p2556 and rate:
            rows.append({"объект": "%s, %s, %s" % (rr[0]["fio"], SHORT[m - 1], c),
                         "факт": _sum(rr, {"оклад", "122"}), "предел": p2556 * rate})
    out.append(_limit_rule(
        "П2556: оклад и 122 не выше предела по должности",
        "Приказ № 2556 задаёт предел оклада с надбавкой 122 на 1,0 ставки по "
        "каждой должности; на открытой ставке договора предел — П2556, "
        "умноженный на эту ставку (правило 4)." + fact_single,
        "Реестр → Справочник должностей", "₽", rows,
        empty="в справочнике нет П2556 по этим должностям"))

    # БЭП по ГОЗ (правило 7): оклад с 122 по договору в месяце против суммы
    # «БЭП должности × открытая ставка» — теми же суммами, что у решателя
    # (payroll_rules.rule_goz_bep_staff_total), а не средними: равенство
    # «с точностью до рубля» у сумм и у средних расходится, когда ставок
    # больше одной. Выше БЭП — нарушение; ниже — допустимо, но цель расчёта —
    # ровно БЭП (владелец продукта 30.09.2026: «не критично если меньше
    # получается, но лучше ровно… делай ровно среднюю»), поэтому «внимание».
    bep_value = bep_of(lim, norm)
    rows = []
    for (c, m), rr in sorted(_by(ahead, "contract", "month").items()):
        contract = ctr.get(c)
        if not contract or not contract["goz"]:
            continue
        cap = staff = 0.0
        for (code,), er in _by(rr, "emp").items():
            part = _sum(er, {"оклад", "122"})
            if not part:
                continue
            r = rate_of(er)
            if r is None:
                # Ставка неизвестна (нет листа ставок и оклада должности) —
                # как у решателя без открытой ставки: штатная ставка строки.
                r = (emps.get(code) or {}).get("rate") or 1.0
            cap += bep_value(er[0]["position"], m) * r
            staff += part
        if not staff and not cap:
            continue
        rows.append({"объект": "%s, %s" % (c, SHORT[m - 1]), "факт": staff, "предел": cap})
    bep = _limit_rule(
        "БЭП: средняя зарплата по ГОЗ-договору в пределах базовой",
        "По ГОЗ-договору в месяце оклад с надбавкой 122 всех строк договора не "
        "выше суммы «БЭП должности × открытая ставка» (правило 7); цель расчёта "
        "— ровно БЭП, ниже — замечание." + fact_single,
        "Реестр → Справочник должностей", "₽", rows,
        empty="ГОЗ-договоров в плане нет")
    if bep["состояние"] != "не применялось":
        above = [r for r in rows if r["факт"] > r["предел"] + EPS]
        below = [r for r in rows if r["факт"] < r["предел"] - EPS]
        equal = len(rows) - len(above) - len(below)
        bep["факт"] = ("Выше БЭП: %d, ниже БЭП: %d, равно БЭП: %d"
                       % (len(above), len(below), equal))
        if not above and below:
            bep["состояние"] = "внимание"
            bep["строки"] = [
                {"объект": r["объект"], "факт": round(r["факт"], 2),
                 "предел": round(r["предел"], 2),
                 "доля": round(r["факт"] / r["предел"], 4) if r["предел"] else None,
                 "запас": round(r["предел"] - r["факт"], 2), "нарушено": False,
                 "внимание": True}
                for r in sorted(below, key=lambda r: r["факт"] - r["предел"])][:SHOW]
    out.append(bep)

    # Приказ № 4 (правило 5): в месяц с 124 штатная часть строки равна уровню
    # П4 на её суммарную открытую ставку — норма, а не потолок: решатель
    # держит равенство, и отклонение в любую сторону сверх рубля — нарушение.
    # Уровень действует только для категорий НТП и НР, как у решателя.
    rows = []
    for (code, m), rr in by_em.items():
        if not any(p["kind"] == "124" for p in rr):
            continue
        limits = lim.get(norm(rr[0]["position"])) or {}
        p4 = lim_at(lim, norm, rr[0]["position"], "П4", m)
        if not p4 or not p4_applies(limits.get("категория")):
            continue
        rate = sum(filter(None, (rate_of(cr) for (_,), cr in _by(rr, "contract").items())))
        if not rate:
            rate = (emps.get(code) or {}).get("rate") or 0.0
        if rate:
            rows.append({"объект": "%s, %s" % (rr[0]["fio"], SHORT[m - 1]),
                         "факт": _sum(rr, {"оклад", "122", "124"}), "предел": p4 * rate})
    out.append(_limit_rule(
        "П4 при надбавке за интенсивность",
        "В месяце, где строке начислена надбавка 124, оклад, 122 и 124 этой "
        "строки по всем договорам равны уровню П4 на её суммарную открытую "
        "ставку (правило 5); уровень действует для категорий НТП и НР." + fact_single,
        "Реестр → Справочник должностей", "₽", rows, mode="равно",
        empty="надбавка 124 в этом плане не назначалась"))
    _mark(out, "Лимиты по должностям")

    # ── ставки ───────────────────────────────────────────────────────
    rows, extra = [], []
    for (code, m), rr in by_em.items():
        total = sum(filter(None, (rate_of(cr) for (_,), cr in _by(rr, "contract").items())))
        if not total:
            continue
        staff = emps.get(code, {}).get("rate") or 1.0
        rows.append({"объект": "%s, %s" % (rr[0]["fio"], SHORT[m - 1]),
                     "факт": total, "предел": 1.5})
        if total - staff > 0.01:
            extra.append({"объект": "%s, %s" % (rr[0]["fio"], SHORT[m - 1]),
                          "факт": total - staff, "предел": 0.5})
    out.append(_limit_rule(
        "Суммарная ставка — не больше 1,5",
        "Суммарная открытая ставка строки в месяце не больше 1,5: штатная "
        "ставка и совместительство сверх неё (правило 14)." + fact_single,
        "План выплат", "ставки", rows))
    out.append(_limit_rule(
        "Совместительство — не больше 0,5 ставки",
        "Совместительство сверх штатной ставки — не больше 0,5 ставки и только "
        "по должности, разрешённой правилами замещения (правило 14)." + fact_single,
        "Реестр → Правила замещения", "ставки", extra,
        empty="совместительство в этом плане не открывалось"))
    _mark(out, "Ставки и совместительство")

    # ── трудоемкость ─────────────────────────────────────────────────
    # Допуск — тот же, что у решателя, аудита LABOR-001 и раздела 10 отчёта:
    # доля от плана строки, но не меньше 0,01 чел.-мес. и 1 ₽. Прежние 0,5
    # чел.-мес. без оглядки на долю пропускали строку в 4 чел.-мес.,
    # закрытую на 10 %, хотя допуск 5 %.
    tol = labor_tolerance(inp["settings"])
    note = " Допуск расчёта: %s %%." % _mo(tol * 100)
    # Сделанное в месяцах факта — по разбивке решателя (лист «Контроль
    # трудоёмкости»): чел.-мес. и деньги строки РКМ за 1…F.
    done = {}
    for a in res.get("labor_people") or []:
        key = (str(a["contract"]).split("—")[0].strip(), norm(a["row"]) if a["row"] else "", a["kind"])
        done[key] = done.get(key, 0.0) + sum(float(v or 0) for v in (a["months"] or [])[:last])
    labor_by = {(lp["contract"], norm(lp["position"]) if lp["position"] else ""): lp
                for lp in inp["labor"]}

    def band(code, name, plan_value, allowed, kind, avg=None):
        """Коридор строки РКМ за год с фактом (labor_band) или None без факта."""
        if not last:
            return None
        lp = labor_by.get((code, norm(name) if name else "")) or {}
        months = lp.get("months")
        if months:
            ahead_plan = sum(v for m, v in months.items() if m > last)
            has_future, monthly = ahead_plan > 0, ahead_plan
            if kind == "pay":
                monthly = (avg or 0.0) * ahead_plan
        else:
            window = (ctr.get(code) or {}).get("window") or (13, 0)
            has_future, monthly = window[0] <= window[1] and window[1] > last, None
        return labor_band(plan_value, allowed, done.get((code, norm(name) if name else "", kind), 0.0),
                          tol, monthly, has_future)

    pm_rows, sum_rows = [], []
    for row in res["labor_control"]:
        code = row["contract"].split("—")[0].strip()
        # Договор и строка РКМ — через запятую, без знака-разделителя (UI-015).
        obj = "%s, %s" % (code, row["row"])
        avg = (labor_by.get((code, norm(row["row"]) if row["row"] else "")) or {}).get("avg")
        if row["plan_pm"]:
            pm_rows.append({"объект": obj, "факт": row["fact_pm"] or 0.0,
                            "предел": row["plan_pm"],
                            "коридор": band(code, row["row"], row["plan_pm"],
                                            max(0.01, tol * row["plan_pm"]), "pm")})
        if row["plan_sum"]:
            sum_rows.append({"объект": obj, "факт": row["fact_sum"] or 0.0,
                             "предел": row["plan_sum"],
                             "коридор": band(code, row["row"], row["plan_sum"],
                                             max(1.0, tol * row["plan_sum"]), "pay", avg)})
    out.append(_limit_rule(
        "Плановые человеко-месяцы закрыты",
        "Строка РКМ договора закрывается открытыми ставками исполнителей на "
        "трудоёмкость РКМ с допуском расчёта (правило 19)." + note + fact_total +
        (" Отклонение, которое сложилось в месяцах факта, допуск не нарушает "
         "(правило 26)." if last else ""),
        "Реестр → Трудоемкость", "чел.-мес.", pm_rows, kind=SOFT, mode="равно",
        tolerance=tol, absolute_tolerance=0.01,
        empty="трудоемкость по договорам не задана"))
    out.append(_limit_rule(
        "Сумма трудоемкости выбрана",
        "Стоимость трудоёмкости строки РКМ — трудоёмкость, умноженная на "
        "среднюю стоимость 1 чел.-мес.; выплаты исполнителям строки равны ей с "
        "допуском расчёта (правило 19)." + note + fact_total +
        (" Отклонение, которое сложилось в месяцах факта, допуск не нарушает "
         "(правило 26)." if last else ""),
        "Реестр → Трудоемкость", "₽", sum_rows, kind=SOFT, mode="равно",
        tolerance=tol, absolute_tolerance=1.0,
        empty="трудоемкость по договорам не задана"))

    bad = []
    for lp in inp["labor"]:
        if not lp["position"]:
            continue
        # A labor row may be closed by an employee through an approved
        # substitution (for example, a leading engineer performs a
        # programmer's work).  The detailed attribution sheet is authoritative
        # for this check; requiring an exact position here falsely reports a
        # valid substitution as an uncovered row.
        attributions = [p for p in res.get("labor_people", [])
                        if p.get("contract") == lp["contract"]
                        and p.get("kind") == "pm"
                        and any(float(v or 0) > PM_EPS for v in (p.get("months") or []))]
        who = [p for p in plan if p["contract"] == lp["contract"]
               and norm(p["position"]) == norm(lp["position"])]
        if not who and not attributions:
            bad.append({"объект": "%s, %s" % (lp["contract"], lp["position"]),
                        "что": "на договоре нет выплат по этой должности"})
    out.append(_count_rule(
        "Работу закрывает подходящая должность",
        "Строку РКМ закрывает сотрудник этой должности или тот, кому она "
        "разрешена правилами замещения (правило 16)." + fact_total,
        "Реестр → Правила замещения", len(inp["labor"]), bad,
        empty="трудоемкость по договорам не задана"))
    _mark(out, "Трудоемкость договоров")

    # Дефицит — единственная настройка, смягчающая жесткое условие.
    if _yes(inp["settings"].get("разрешить дефицит")):
        for r in out:
            if r["правило"].startswith("Человек получает"):
                r["тип"] = "смягчено настройкой"
        out.insert(0, {
            "правило": "Дефицит выплат разрешен настройками",
            "смысл": "Параметры расчёта разрешают недоплату заработной платы: "
                     "план рассчитан, но часть заработной платы не выплачена.",
            "где": "Настройки расчета", "тип": INFO, "состояние": "внимание",
            "единица": "", "проверено": 0, "нарушений": 0, "строки": [],
            "предел_общий": None,
            "факт": "Условие «Заработная плата за месяц выплачена полностью» смягчено",
            "раздел": "Настройки расчета"})
    for r in out:
        r["название"] = TITLES.get(r["правило"], r["правило"])
    return out


# ── сводка для финансиста ────────────────────────────────────────────
def summary(input_path, result_path):
    """План года цифрами: бюджет, освоение, структура выплат, люди.

    То, с чего финансист начинает разговор о годовом плане: сколько денег
    заложено и сколько разошлось по людям, как выплаты ложатся на месяцы, из
    чего складывается сумма, кто где сидит. Поступлений здесь нет: в расчёт
    они не подаются (SOURCE-009), остаток по месяцам — остаток ФОТ.
    """
    inp = _read_input(input_path)
    res = _read_result(result_path)
    plan = res["plan"]
    ctr = inp["contracts"]

    paid_m = [0.0] * 12
    for p in plan:
        paid_m[p["month"] - 1] += p["amount"]
    # ФОТ договора поступил к месяцу — с первого месяца его окна выплат
    # (_read_input): остаток ФОТ на конец месяца считается от него.
    fot_m = [sum(c["fot"] for c in ctr.values() if c["window"][0] <= m + 1)
             for m in range(12)]

    kinds = {}
    for p in plan:
        kinds[p["kind"] or "прочее"] = kinds.get(p["kind"] or "прочее", 0.0) + p["amount"]

    by_ctr = []
    for code, c in sorted(ctr.items()):
        paid = sum(p["amount"] for p in plan if p["contract"] == code)
        by_ctr.append({"договор": code, "название": c["name"], "ГОЗ": c["goz"],
                       "ФОТ": round(c["fot"], 2), "выплаты": round(paid, 2),
                       "остаток": round(c["fot"] - paid, 2),
                       "освоение": round(paid / c["fot"], 4) if c["fot"] else None,
                       "месяцы": [round(sum(p["amount"] for p in plan
                                            if p["contract"] == code and p["month"] == m + 1), 2)
                                  for m in range(12)]})

    people = []
    for e in inp["employees"]:
        rows = [p for p in plan if p["emp"] == e["code"]]
        paid = sum(p["amount"] for p in rows)
        months = sorted({p["month"] for p in rows})
        contracts = sorted({p["contract"] for p in rows})
        people.append({"код": e["code"], "фио": e["fio"], "должность": e["position"],
                       "подразделение": e["department"], "ставка": e["rate"],
                       "зарплата": e["salary"], "за год": round(paid, 2),
                       "месяцев": len(months), "договоров": len(contracts),
                       "договоры": contracts})

    total_fot = sum(c["fot"] for c in ctr.values())
    total_paid = sum(paid_m)
    return {
        "год": int(inp["year"] or 0) or None,
        "итого": {
            "ФОТ договоров": round(total_fot, 2),
            "распределено планом": round(total_paid, 2),
            "не распределено": round(total_fot - total_paid, 2),
            "освоение": round(total_paid / total_fot, 4) if total_fot else None,
            "сотрудников": len(inp["employees"]),
            "договоров": len(ctr),
            "средняя выплата в месяц на человека":
                round(total_paid / max(1, len(inp["employees"])) / 12, 2),
        },
        "месяцы": {"подписи": SHORT, "выплаты": [round(v, 2) for v in paid_m],
                   "остаток ФОТ": [round(fot_m[i] - sum(paid_m[:i + 1]), 2)
                                   for i in range(12)]},
        "виды выплат": [{"вид": k, "сумма": round(v, 2),
                         "доля": round(v / total_paid, 4) if total_paid else 0}
                        for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])],
        "договоры": by_ctr,
        "люди": sorted(people, key=lambda p: -p["за год"]),
    }


def _why_rates(inp, res):
    """Кто закрывает строку трудоёмкости — по разбивке решателя, а не по счёту
    должностей.

    Человеко-месяцы РКМ закрывает только открытая ставка сотрудника на этом
    договоре: надбавки их не создают. Кого решатель отнёс к какой строке, он
    говорит сам — лист «Контроль трудоёмкости» результата. Раньше здесь
    считались люди с совпадающей должностью, и объяснение выходило неверным
    сразу дважды: замещение (главный инженер проекта на строке инженера) не
    попадало в счёт, а ставка человека на договоре приписывалась каждой
    строке этого договора с подходящей должностью. По строке «Инженер»
    (16 чел.-мес., закрывают трое) выходило «1 человек, 8 чел.-мес.» — и
    агент в ленте пересказывал эту неправду.
    """
    out = []
    by_row = {}
    for p in res.get("labor_people") or []:
        if p.get("kind") != "pm":
            continue
        months = [(i + 1, float(v or 0)) for i, v in enumerate(p.get("months") or [])
                  if float(v or 0) > PM_EPS]
        if not months:
            continue
        code = str(p.get("contract") or "").split("—")[0].strip()
        by_row.setdefault((code, str(p.get("row") or "")), []).append((p, months))
    for lp in inp["labor"]:
        if not lp["person_months"]:
            continue
        code, pos = lp["contract"], lp["position"] or ""
        got = by_row.get((code, pos)) or []
        if not got:
            out.append("Договор %s: трудоемкость %s чел.-мес. по должности «%s» "
                       "не закрыта — ставок на этом договоре не открыто."
                       % (code, _mo(lp["person_months"]), pos or "любая"))
            continue
        parts, total = [], 0.0
        for p, months in got:
            amount = sum(v for _m, v in months)
            total += amount
            nums = [m for m, _v in months]
            span = ("%s—%s" % (SHORT[nums[0] - 1], SHORT[nums[-1] - 1])
                    if len(nums) > 1 else SHORT[nums[0] - 1])
            rate = amount / len(nums)
            same = " (замещение)" if norm_position_differs(inp, p, pos) else ""
            parts.append("%s%s — по %s ставки, %s (%s мес. × %s = %s чел.-мес.)"
                         % (p.get("fio") or "?", same, _mo(rate), span,
                            len(nums), _mo(rate), _mo(amount)))
        out.append("Договор %s: трудоемкость %s чел.-мес. по должности «%s», "
                   "закрыто %s чел.-мес. Человеко-месяцы закрывает только "
                   "открытая ставка на этом договоре, и решатель отнёс к этой "
                   "строке: %s."
                   % (code, _mo(lp["person_months"]), pos or "любая",
                      _mo(total), "; ".join(parts)))
    return out


def norm_position_differs(inp, person, position):
    """Человек закрывает строку по правилу замещения, а не своей должностью."""
    norm = inp["norm"]
    have, want = person.get("position") or "", position or ""
    return bool(have and want and norm(have) != norm(want))


def _px(n, one, few, many):
    d, hh = n % 10, n % 100
    if 11 <= hh <= 14:
        return many
    if d == 1:
        return one
    if 2 <= d <= 4:
        return few
    return many


# ── из чего складывается зарплата ────────────────────────────────────
def payroll(input_path, result_path):
    """Состав зарплаты по месяцам: договоры, ставки, виды выплат и смены схемы.

    Матрица «ФИО на месяцы» ничего не говорит: сумма в каждой клетке равна
    месячной зарплате, потому что это жесткое условие. Экономисту нужно то,
    что за этой суммой: с каких договоров она финансируется, на каких
    ставках человек сидит, из каких видов выплат складывается — и как часто
    схема меняется. Смена договора оклада и смена состава выплат — это
    приказы, допники и разговоры с людьми; оптимизатор их штрафует, а
    экономист должен видеть, сколько их получилось.
    """
    inp = _read_input(input_path)
    res = _read_result(result_path)
    plan, lim, norm = res["plan"], inp["limits"], inp["norm"]
    ctr = inp["contracts"]
    order = sorted({p["contract"] for p in plan})

    people = []
    total_switch = total_mix = total_extra = total_opened = total_struct = 0
    for e in inp["employees"]:
        rows = [p for p in plan if p["emp"] == e["code"]]
        if not rows:
            continue
        months, prev_ctr, prev_mix, prev_oklad, prev_rate = [], None, None, None, None
        switch = mix_change = extra_months = opened = struct_change = 0
        rates, oklad_share = [], []
        for m in range(1, 13):
            mr = [p for p in rows if p["month"] == m]
            # Оклад должности в этом месяце (индексация с даты, 01.10.2026).
            base = lim_at(lim, norm, e["position"], "оклад", m)
            if not mr:
                months.append(None)
                continue
            by_ctr = []
            for code in order:
                cr = [p for p in mr if p["contract"] == code]
                if not cr:
                    continue
                oklad = _sum(cr, {"оклад"})
                by_ctr.append({
                    "код": code,
                    "ставка": round(oklad / base, 2) if base and oklad else None,
                    "сумма": round(_sum(cr), 2),
                    "виды": [{"вид": k, "сумма": round(_sum(cr, {k}), 2)}
                             for k in ("оклад", "120", "122", "124", "152", "приказ")
                             if _sum(cr, {k})],
                })
            salary_ctr = sorted({p["contract"] for p in mr if p["kind"] == "оклад"})
            mix = sorted({p["kind"] for p in mr if p["kind"]})
            rate = sum(c["ставка"] or 0 for c in by_ctr)
            change = []
            if prev_ctr is not None and salary_ctr != prev_ctr:
                # Появился второй договор оклада — это открытая ставка, а не
                # смена: человек остался там, где был, и взял работу еще. А вот
                # когда договор из-под оклада ушел, это перевод: приказ,
                # допник, разговор с человеком. Считаем их по отдельности.
                added = [c for c in salary_ctr if c not in prev_ctr]
                gone = [c for c in prev_ctr if c not in salary_ctr]
                if gone:
                    change.append("оклад переехал: %s → %s"
                                  % (", ".join(prev_ctr), ", ".join(salary_ctr) or "—"))
                    switch += 1
                if added and not gone:
                    change.append("добавился оклад на %s" % ", ".join(added))
                    opened += 1
            if prev_mix is not None and mix != prev_mix:
                change.append("состав: %s → %s" % (", ".join(prev_mix),
                                                   ", ".join(mix)))
                mix_change += 1
            # Набор видов может не меняться, а доли — меняться: открылась
            # половина ставки, оклад вырос, надбавка ужалась. Экономисту это
            # такая же смена структуры, и молчать о ней нельзя.
            oklad = _sum(mr, {"оклад"})
            if prev_oklad is not None and abs(oklad - prev_oklad) > EPS:
                change.append("оклад %s → %s ₽ (ставка %s → %s)"
                              % (_mo(prev_oklad), _mo(oklad),
                                 _mo(prev_rate), _mo(rate)))
            if change:
                struct_change += 1
            if rate > (e["rate"] or 1.0) + 0.01:
                extra_months += 1
            total_m = _sum(mr)
            rates.append(rate)
            if total_m:
                oklad_share.append(oklad / total_m)
            months.append({"м": m, "всего": round(total_m, 2), "ставка": round(rate, 2),
                           "договоры": by_ctr, "виды": mix, "изменилось": change})
            prev_ctr, prev_mix, prev_oklad, prev_rate = salary_ctr, mix, oklad, rate
        year = _sum(rows)
        total_switch += switch
        total_mix += mix_change
        total_extra += extra_months
        total_opened += opened
        total_struct += struct_change
        people.append({
            "код": e["code"], "фио": e["fio"], "должность": e["position"],
            "подразделение": e["department"], "ставка": e["rate"],
            "зарплата": e["salary"], "за год": round(year, 2), "месяцы": months,
            "смен договора оклада": switch, "смен состава выплат": mix_change,
            "открыто ставок": opened, "месяцев со сменой структуры": struct_change,
            "ставка макс": round(max(rates), 2) if rates else None,
            "доля оклада": ({"мин": round(min(oklad_share), 4),
                             "макс": round(max(oklad_share), 4)}
                            if oklad_share else None),
            "месяцев с совместительством": extra_months,
            "договоры": sorted({p["contract"] for p in rows}),
            "доли видов": [{"вид": k, "доля": round(_sum(rows, {k}) / year, 4)}
                           for k in ("оклад", "120", "122", "124", "152", "приказ")
                           if year and _sum(rows, {k})],
        })
    return {
        "договоры": [{"код": c, "название": (ctr.get(c) or {}).get("name", ""),
                      "ГОЗ": bool((ctr.get(c) or {}).get("goz"))} for c in order],
        "почему": _why_rates(inp, res),
        "итого": {"человек": len(people),
                  "смен договора оклада": total_switch,
                  "открыто ставок": total_opened,
                  "смен состава выплат": total_mix,
                  "месяцев со сменой структуры": total_struct,
                  "месяцев с совместительством": total_extra,
                  "за год": round(sum(p["за год"] for p in people), 2)},
        "люди": people,
    }
