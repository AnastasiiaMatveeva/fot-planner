# -*- coding: utf-8 -*-
"""Входной файл решателя — из того, что собрал агент.

Раньше файл собирался из паспорта дела и переносил в шаблон один фонд
договора: помесячные поступления масштабировались от образца, трудоемкость и
надбавка 120 не переносились вовсе, а сотрудникам жестко проставлялось
«основное» и «основной». То есть половина расчета шла на данных шаблона, а не
на данных организации, и по файлу этого было не видно.

Здесь лист заполняется целиком из реестра, если в реестре по нему что-то есть,
и остается от шаблона, если нет. О каждом таком случае возвращается
предупреждение: молчаливая подстановка в ГОЗ недопустима.
"""
from __future__ import annotations

import openpyxl


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("\u00a0", "").replace(" ", "").replace(",", ".")
    s = "".join(c for c in s if c.isdigit() or c in ".-")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _headers(ws):
    return [str(ws.cell(1, c).value or "").strip().lower()
            for c in range(1, ws.max_column + 1)]


def _col(ws, *names):
    """Номер колонки по любому из имен, либо None."""
    hdr = _headers(ws)
    for name in names:
        if name.lower() in hdr:
            return hdr.index(name.lower()) + 1
    return None


def _clear(ws):
    if ws.max_row > 1:
        ws.delete_rows(2, ws.max_row)


def _yes(value):
    return "да" if str(value or "").strip().lower() in ("да", "true", "1", "+") else "нет"


def fill_employees(ws, rows, warn, year=None):
    """Лист «сотрудники» из штатного расписания реестра."""
    if not rows:
        warn.append("Штатного расписания в реестре нет — сотрудники в расчете "
                    "остались от шаблона.")
        return
    no_dates = [e.code for e in rows if not (e.date_from and e.date_to)]
    if no_dates:
        warn.append("У сотрудников %s не задан срок работы — приняты границы "
                    "года плана."
                    % ", ".join(sorted(no_dates)[:6]))
    _clear(ws)
    for e in rows:
        ws.append([
            e.code, e.fio, e.position, e.department or "", e.rate,
            # Тип и категория занятости раньше проставлялись жестко: любой
            # сотрудник уходил в расчет как основное место. Совместительство
            # так выразить нельзя, а на нем держится половина модели.
            e.employment_type or "основное",
            e.employment_category or "основной",
            e.salary,
            e.date_from or ("01.01.%d" % year if year else ""),
            e.date_to or ("31.12.%d" % year if year else ""),
            e.allowed_contracts or None, e.forbidden_contracts or None,
        ])


def fill_contracts(ws, rows, warn, year=None):
    """Лист «договоры»: не только фонд, но и все разрешения и сроки."""
    if not rows:
        warn.append("Договоров в реестре нет — договоры в расчете остались "
                    "от шаблона.")
        return
    # Без срока решатель не знает, в каких месяцах договор действует. Берем
    # год плана целиком и говорим об этом вслух: подставить молча значит
    # посчитать по выдуманному сроку.
    # Пустая графа «разрешенные выплаты» значит «не сказано» — тогда не
    # запрещаем ничего. Но если виды перечислены, остальные запрещены, и это
    # решает исход расчета: сотруднику с зарплатой выше предела П2556 нечем
    # добрать сумму. Говорим до расчета, а не после невыполнимости.
    limited = [c.code for c in rows if str(c.kinds or '').strip()]
    if limited:
        warn.append('По договорам %s разрешены только эти виды выплат: %s. '
                    'Остальные в расчете запрещены — если это не так, '
                    'поправьте договор в реестре.'
                    % (', '.join(sorted(limited)),
                       '; '.join(sorted({str(c.kinds).strip() for c in rows if c.kinds}))))
    no_dates = [c.code for c in rows if not (c.date_from and c.date_to)]
    if no_dates:
        warn.append("У договоров %s не задан срок действия — приняты границы "
                    "года плана. Уточните сроки: от них зависит, в каких "
                    "месяцах договор может платить."
                    % ", ".join(sorted(no_dates)))
    kinds_of = {}
    for c in rows:
        kinds_of[c.code] = {k.strip().lower()
                            for k in str(c.kinds or "").split(",") if k.strip()}
    _clear(ws)
    for c in rows:
        got = kinds_of.get(c.code) or set()
        # Пусто в графе «разрешенные выплаты» значит «не сказано», а не
        # «запрещено»: запретить все — молча получить нерешаемую задачу.
        allow = (lambda name: "да") if not got else (
            lambda name: "да" if name in got else "нет")
        ws.append([
            c.code, c.name or "", c.number or "", c.kind or "",
            c.account or "", _yes(c.goz),
            c.date_from or ("01.01.%d" % year if year else ""),
            c.date_to or ("31.12.%d" % year if year else ""),
            c.fund,
            allow("оклад"), allow("120"), allow("122"), allow("124"),
            allow("152"), allow("приказ"),
            c.priority or None,
            _yes(c.allow_main) if c.allow_main else "да",
            _yes(c.allow_part_time) if c.allow_part_time else "да",
            c.salary_deadline or None, c.allowance_deadline or None,
        ])


def _months_of(contract, year):
    """Месяцы года, в которых договор действует."""
    def month_of(text, default):
        try:
            parts = str(text or "").split(".")
            return int(parts[1]) if len(parts) >= 2 else default
        except (ValueError, IndexError):
            return default

    def year_of(text):
        try:
            return int(str(text or "").split(".")[-1])
        except ValueError:
            return None

    first = month_of(contract.date_from, 1) if year_of(contract.date_from) == year else 1
    last = month_of(contract.date_to, 12) if year_of(contract.date_to) == year else 12
    if first > last:
        first, last = 1, 12
    return list(range(first, last + 1))


def fill_inflows(ws, rows, contracts, warn, year=None):
    """Лист «фот_по_месяцам»: поступления как есть.

    Договору без графика фонд раскладывается ровно по месяцам его действия.
    Оставить его без кассы нельзя: решатель не может потратить деньги раньше,
    чем они поступили, и задача становится нерешаемой из-за отсутствующих
    данных, а не из-за существа. Раскладку называем вслух — это допущение
    сервиса, а не факт из документа.
    """
    by_code = {}
    for r in rows:
        by_code.setdefault(r.contract_code, {})[int(r.month or 0)] = r.amount

    guessed = []
    for c in contracts:
        if c.code in by_code or not c.fund:
            continue
        months = _months_of(c, year) if year else list(range(1, 13))
        share = round(float(c.fund) / len(months))
        plan = {m: share for m in months}
        # Остаток от деления кладем в последний месяц, чтобы сумма сошлась
        # с фондом до рубля.
        plan[months[-1]] = round(float(c.fund) - share * (len(months) - 1))
        by_code[c.code] = plan
        guessed.append(c.code)

    if not by_code:
        warn.append("Графика поступлений нет и фонды договоров не заданы — "
                    "помесячная разбивка в расчете осталась от шаблона.")
        return
    _clear(ws)
    for code, months in by_code.items():
        ws.append([code] + [months.get(m) for m in range(1, 13)])

    if guessed:
        warn.append("Графика поступлений по договорам %s в реестре нет — фонд "
                    "разложен ровно по месяцам действия. Это допущение сервиса: "
                    "приложите график, если деньги приходят иначе."
                    % ", ".join(sorted(guessed)))
    known = {c.code for c in contracts}
    lost = [c for c in by_code if c not in known]
    if lost:
        warn.append("Поступления есть по договорам, которых нет в реестре: %s."
                    % ", ".join(sorted(lost)))


def fill_labor(ws, rows, warn):
    """Лист «трудоемкость_по_договорам» — план в чел.-мес. и стоимость."""
    if not rows:
        return
    _clear(ws)
    for r in rows:
        ws.append([r.contract_code, r.year, r.position or "", r.salary_page or "",
                   r.salary_group, r.position_level, r.person_months, r.avg_cost])


def fill_secret(ws, rows, warn):
    """Лист «120_надбавка» — кому платится и по какому договору секретности."""
    if not rows:
        return
    _clear(ws)
    for r in rows:
        ws.append([r.employee_code, r.secret_contract_code or "", r.rate])


def fill_substitutions(wb, pairs):
    """Лист правил замещения. Создается, если файл собран старым шаблоном."""
    name = "правила_замещения"
    if name in wb.sheetnames:
        ws = wb[name]
        _clear(ws)
    else:
        ws = wb.create_sheet(name)
        ws.cell(1, 1).value = "должность"
        ws.cell(1, 2).value = "может быть замещена"
    for position, replaced_by in pairs:
        if position:
            ws.append([position, replaced_by or ""])


def build(template_path, out_path, data, warn):
    """Собрать входной файл. ``data`` — то, что лежит в реестре организации."""
    wb = openpyxl.load_workbook(template_path)

    fill_employees(wb["сотрудники"], data.get("employees") or [], warn,
                   data.get("year"))
    fill_contracts(wb["договоры"], data.get("contracts") or [], warn,
                   data.get("year"))
    fill_inflows(wb["фот_по_месяцам"], data.get("inflows") or [],
                 data.get("contracts") or [], warn, data.get("year"))
    if "трудоемкость_по_договорам" in wb.sheetnames:
        fill_labor(wb["трудоемкость_по_договорам"], data.get("labor") or [], warn)
    if "120_надбавка" in wb.sheetnames:
        fill_secret(wb["120_надбавка"], data.get("secret") or [], warn)
    fill_substitutions(wb, data.get("substitutions") or [])

    year = data.get("year")
    if year and "настройки" in wb.sheetnames:
        ws = wb["настройки"]
        col = _col(ws, "год")
        if col:
            ws.cell(2, col).value = year

    wb.save(out_path)
    return out_path
