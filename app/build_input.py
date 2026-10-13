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

import json

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


def _codes(text):
    return [c.strip() for c in str(text or "").replace(";", ",").split(",") if c.strip()]


def group_contracts(rows, groups):
    """Разрешённые и запрещённые договоры строк с учётом рабочих групп.

    Член рабочей группы получает с договора группы выплату (закрепление), и
    договор ему разрешён приказом, какой бы ни была графа штатного
    расписания (пользователь 01.10.2026: «если он есть в рабочем приказе, он
    должен сразу галочку ставить»). Иначе закрепление и разрешение спорили, и
    расчёт кончался «решения нет». Возвращает {код строки: (разрешены,
    запрещены, [добавленные договоры])}; строки без групп — как есть."""
    by_emp = {}
    for g in groups or []:
        if g.get("сотрудник") and g.get("договор"):
            by_emp.setdefault(g["сотрудник"], []).append(g["договор"])
    out = {}
    for e in rows:
        allowed, forbidden = _codes(e.allowed_contracts), _codes(e.forbidden_contracts)
        added = []
        for code in by_emp.get(e.code, []):
            if code in forbidden:
                forbidden.remove(code)
                added.append(code)
            if allowed and code not in allowed:
                allowed.append(code)
                if code not in added:
                    added.append(code)
        out[e.code] = (", ".join(allowed) or None, ", ".join(forbidden) or None, added)
    return out


def fill_employees(ws, rows, warn, year=None, groups=None):
    """Лист «сотрудники» из штатного расписания реестра. groups — действующий
    состав рабочих групп: договор группы члену разрешён (group_contracts)."""
    if not rows:
        warn.append("Штатного расписания в реестре нет — сотрудники в расчете "
                    "остались от шаблона.")
        return
    no_dates = [e.code for e in rows if not (e.date_from and e.date_to)]
    if no_dates:
        warn.append("У сотрудников %s не задан срок работы — приняты границы "
                    "года плана."
                     % ", ".join(sorted(no_dates)[:6]))
    headers = [str(c.value or "").strip().lower() for c in ws[1]]
    if "табельный номер" not in headers:
        ws.insert_cols(2)
        ws.cell(1, 2).value = "табельный номер"
        headers = [str(c.value or "").strip().lower() for c in ws[1]]
    _clear(ws)
    access = group_contracts(rows, groups)
    opened = ["%s — %s" % (e.fio or e.code, ", ".join(access[e.code][2]))
              for e in rows if access[e.code][2]]
    if opened:
        warn.append("Договоры рабочих групп разрешены членам групп по приказу: %s."
                    % "; ".join(opened))
    for e in rows:
        values = {
            "код строки": e.code,
            "табельный номер": e.person_code or e.code,
            "фио": e.fio,
            "должность": e.position,
            "подразделение": e.department or "",
            "ставка": e.rate,
            # Тип и категория занятости раньше проставлялись жестко: любой
            # сотрудник уходил в расчет как основное место. Совместительство
            # так выразить нельзя, а на нем держится половина модели.
            "тип занятости": e.employment_type or "по расчету",
            "категория занятости": e.employment_category or "основной",
            # Зарплаты в штатном расписании нет (решение пользователя
            # 19.09.2026): графу заполняет fill_salary_months из таблицы
            # «Заработная плата по месяцам».
            "зарплата": None,
            "дата начала": e.date_from or ("01.01.%d" % year if year else ""),
            "дата окончания": e.date_to or ("31.12.%d" % year if year else ""),
            "разрешенные договоры": access[e.code][0],
            "запрещенные договоры": access[e.code][1],
        }
        ws.append([values.get(name) for name in headers])


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
            c.department or None,
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


def _put(ws, values):
    """Дописать строку, раскладывая значения по именам граф шаблона.

    Строка по порядку граф ломается при первом же расхождении с шаблоном:
    трудоемкость встала на место должности, и решатель упал на «Специалист».
    Имена граф — контракт входного файла, порядок — нет.
    """
    hdr = _headers(ws)
    row = [None] * len(hdr)
    for names, value in values:
        for name in names:
            if name in hdr:
                row[hdr.index(name)] = value
                break
    ws.append(row)


#: Графы плана по месяцам на листе трудоёмкости — по именам месяцев.
MONTH_NAMES = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль",
               "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]


def _pay_window(contract, year):
    """Месяцы года, в которых договор может платить: срок действия, а если
    задан предел выплат оклада — не позже его месяца."""
    months = _months_of(contract, year)
    parts = str(contract.salary_deadline or "").split(".")
    try:
        d_month, d_year = int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        return months
    if year and d_year < year:
        return []
    if year and d_year > year:
        return months
    return [m for m in months if m <= d_month]


def fill_labor(ws, rows, warn, contracts=None, year=None):
    """Лист «трудоемкость_по_договорам» — план в чел.-мес. и стоимость.

    Этап РКМ, разложенный по месяцам поровну, может заходить за предел
    выплат договора (этап до 31.12, выплаты до 31.10). Работу, которую
    договор уже не оплатит, планировать нельзя — эти чел.-мес. переносятся
    поровну в месяцы окна выплат. Иначе на каждый месяц окна приходилась
    норма 0,8 человека, а решатель ставил людей на полную ставку и закрывал
    строку на нижней границе допуска: 15,2 из 16 при двух людях на 8 месяцев.
    """
    if not rows:
        return
    windows = {c.code: _pay_window(c, year) for c in (contracts or [])}
    _clear(ws)
    # Графа числа людей появилась позже шаблона приложения — дописываем
    # справа, загрузчик решателя читает лист по заголовкам.
    if _col(ws, "количество человек") is None:
        ws.cell(1, ws.max_column + 1).value = "количество человек"
    # План по месяцам — графами «Январь» … «Декабрь», тоже по именам.
    for name in MONTH_NAMES:
        if _col(ws, name) is None:
            ws.cell(1, ws.max_column + 1).value = name
    for r in rows:
        months = {}
        raw = getattr(r, "months", None)
        if raw:
            try:
                months = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except ValueError:
                months = {}
        months = {int(k): float(v) for k, v in months.items() if v}
        window = windows.get(r.contract_code)
        # Строка другого года (РКМ на 2026–2027) решателю этого года не нужна —
        # он берёт год плана, — а окно выплат этого года к ней не относится:
        # её январь не «после предела», а другой январь.
        if year and getattr(r, "year", None) and int(r.year) != int(year):
            window = None
        late = {m: v for m, v in months.items() if window is not None and m not in window}
        if late:
            keep = {m: v for m, v in months.items() if m not in late}
            cells = sorted(keep) or sorted(window)
            if cells:
                extra = sum(late.values()) / len(cells)
                months = {m: round(keep.get(m, 0.0) + extra, 4) for m in cells}
                warn.append("Трудоёмкость %s / %s: %.2f чел.-мес. приходились на месяцы "
                            "после предела выплат договора (%s) и перенесены в месяцы "
                            "выплат — по %.2f в месяц."
                            % (r.contract_code, r.position or "", sum(late.values()),
                               ", ".join(MONTH_NAMES[m - 1].lower() for m in sorted(late)),
                               months[cells[0]]))
        _put(ws, [
            (("договор", "проект"), r.contract_code),
            (("год",), r.year),
            (("должность",), r.position or ""),
            (("страница",), r.salary_page or ""),
            (("номер группы",), r.salary_group),
            (("номер уровня", "уровень"), r.position_level),
            (("трудоемкость", "трудоёмкость", "чел-мес"), r.person_months),
            (("средняя стоимость выполнения работ в месяц", "средняя зарплата",
              "стоимость 1 чел-мес", "стоимость чел мес"), r.avg_cost),
            (("количество человек", "кол-во человек"), getattr(r, "headcount", None)),
        ] + [((MONTH_NAMES[m - 1].lower(),), months.get(m)) for m in range(1, 13)])


def fill_secret(ws, rows, warn):
    """Лист «120_надбавка» — кому платится и по какому договору секретности."""
    if not rows:
        return
    _clear(ws)
    # Окно и договор выплаты появились 18.09.2026 (правило 10): в шаблонах
    # прежней редакции этих граф нет — дописываем шапку, а не теряем значения.
    hdr = _headers(ws)
    for name in ("начало 120", "окончание 120", "договор выплаты 120"):
        if name not in hdr:
            ws.cell(1, len(hdr) + 1, name)
            hdr.append(name)
    for r in rows:
        _put(ws, [(("сотрудник",), r.employee_code),
                  (("договор секретности",), r.secret_contract_code or ""),
                  (("ставка 120",), r.rate),
                  (("начало 120",), getattr(r, "date_from", None) or None),
                  (("окончание 120",), getattr(r, "date_to", None) or None),
                  (("договор выплаты 120",), getattr(r, "pay_contract_code", None) or None)])


def fill_manual(ws, rows, prohibit=False):
    """Листы «ручные_назначения» и «ручные_запреты» — переменные решателя,
    зафиксированные экономистом в чате."""
    _clear(ws)
    # «По возможности вся» — член рабочей группы без выбранной выплаты
    # (30.09.2026): решатель стремится платить ему всё с этого договора.
    # Графы нет в старых шаблонах — дописываем справа, загрузчик читает лист
    # по заголовкам.
    flag = "по возможности вся"
    if not prohibit and any(r.get(flag) for r in rows or []) and _col(ws, flag) is None:
        ws.cell(1, ws.max_column + 1).value = flag
    for r in rows or []:
        # «любой» — вид не задан (решение пользователя 19.09.2026): у
        # закрепления — хотя бы одна выплата с договора любого вида, у
        # запрета — все виды. Решатель читает пустую графу именно так.
        kind = r.get("вид") or "оклад"
        kind = None if kind == "любой" else kind
        vals = [(("сотрудник",), r["сотрудник"]), (("договор",), r["договор"]),
                (("год",), r.get("год")), (("месяц с",), r["с"]), (("месяц по",), r["по"]),
                (("вид выплаты",), kind)]
        if not prohibit:
            vals.append((("фикс сумма",), r.get("сумма") if kind else None))
            vals.append((("по возможности вся",), "да" if r.get("по возможности вся") else None))
        _put(ws, vals)


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


def active_months(date_from, date_to, year):
    """Месяцы года, в которых строка штатного расписания действует: с месяца
    приёма по месяц увольнения; без даты — с начала или до конца года."""
    def index(text, default):
        parts = str(text or "").strip().split(".")
        try:
            return int(parts[2]) * 12 + int(parts[1]) - 1
        except (ValueError, IndexError):
            return default
    first, last = index(date_from, year * 12), index(date_to, year * 12 + 11)
    return [m for m in range(1, 13) if first <= year * 12 + m - 1 <= last]


def salary_gaps(employees, rows, year, fact_through=0):
    """Строки, у которых в месяцах срока нет зарплаты: [(строка, [месяцы])].

    Зарплата — только из таблицы «Заработная плата по месяцам»: в штатном
    расписании её нет (решение пользователя 19.09.2026), подставить нечего.
    Месяцы факта (по fact_through включительно) не нужны: их расчёт берёт как
    были начислены (30.09.2026)."""
    have = {(code, month) for code, month, _ in rows or []}
    out = []
    for e in employees or []:
        missing = [m for m in active_months(e.date_from, e.date_to, year)
                   if (e.code, m) not in have and m > (fact_through or 0)]
        if missing:
            out.append((e, missing))
    return out


def _spans(months):
    """[3, 4, 5, 9] → «март–май, сентябрь»."""
    parts, start = [], None
    for i, m in enumerate(months):
        if start is None:
            start = m
        if i + 1 == len(months) or months[i + 1] != m + 1:
            parts.append(MONTH_NAMES[start - 1].lower() if start == m else
                         "%s–%s" % (MONTH_NAMES[start - 1].lower(), MONTH_NAMES[m - 1].lower()))
            start = None
    return ", ".join(parts)


def gaps_text(gaps):
    """Сообщение о строках без зарплаты: кто и в какие месяцы."""
    shown = ["%s (%s) — %s" % (e.fio or e.code, e.code,
                               "весь год" if len(months) == 12 else _spans(months))
             for e, months in gaps[:6]]
    more = " и ещё %d" % (len(gaps) - 6) if len(gaps) > 6 else ""
    return ("Не задана зарплата по месяцам: %s%s. Заполните вкладку реестра "
            "«Заработная плата по месяцам»." % ("; ".join(shown), more))


def fill_salary_months(wb, rows, employees=None, year=None, fact_through=0):
    """Лист «зарплата_по_месяцам» и графа «зарплата» листа сотрудников — из
    таблицы «Заработная плата по месяцам» (правило 1, SALARY-001).

    Зарплаты в штатном расписании нет, подставить её нечем: если у строки в
    месяце срока нет суммы, вход не собирается, а ошибка называет строки и
    месяцы. В лист попадают только строки листа сотрудников: сумма строки,
    не ушедшей в расчёт, остановила бы загрузку входа. Графа «зарплата»
    обязательна загрузчику, а месяцы срока все заданы листом, поэтому в ней —
    первая сумма строки."""
    name = "зарплата_по_месяцам"
    if name in wb.sheetnames:
        del wb[name]
    staff = wb["сотрудники"]
    col = _col(staff, "код строки")
    codes = {str(staff.cell(r, col).value).strip() for r in range(2, staff.max_row + 1)
             if col and staff.cell(r, col).value not in (None, "")}
    if employees is not None:
        gaps = salary_gaps(employees, rows, year, fact_through)
        if gaps:
            raise ValueError(gaps_text(gaps))
        first = {}
        for code, _month, amount in sorted(rows or [], key=lambda r: (r[0], r[1])):
            first.setdefault(code, amount)
        wage = _col(staff, "зарплата")
        if col and wage:
            for r in range(2, staff.max_row + 1):
                code = str(staff.cell(r, col).value or "").strip()
                if code:
                    staff.cell(r, wage).value = first.get(code, 0)
    if not rows:
        return
    ws = wb.create_sheet(name)
    ws.append(["код строки", "месяц", "зарплата"])
    for code, month, amount in rows:
        if code in codes:
            ws.append([code, month, amount])


#: Графы величин справочника — в листе «лимиты_по_должностям» и в
#: «лимиты_по_месяцам» (app/reference.COLS).
REF_FIELDS = ("оклад", "П2556", "П4", "БЭП")
REF_MONTHLY_SHEET = "лимиты_по_месяцам"
MONTH_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
             "сентября", "октября", "ноября", "декабря")


def fill_reference_months(wb, year, warn=None):
    """Величины справочника, действующие не весь год (01.10.2026).

    Шаблон входа — сам справочник, и в нём лист истории: величина должности
    с даты. Строка «лимиты_по_должностям» становится величиной января года
    плана, а месяцы, где действует другая, — строками листа
    «лимиты_по_месяцам» (должность, месяц и только отличающиеся графы). Без
    истории — листа нет, и весь год действует строка справочника."""
    import reference
    if not year:
        return
    months = reference.month_values(year, wb)
    if not months:
        if REF_MONTHLY_SHEET in wb.sheetnames:
            del wb[REF_MONTHLY_SHEET]
        return
    ws = wb[reference.SHEET]
    rows = {}
    for r in range(2, ws.max_row + 1):
        if ws.cell(r, 1).value:
            rows[str(ws.cell(r, 1).value)] = r
    cells = {}
    said = []
    for (pos, field), by_month in sorted(months.items()):
        r = rows.get(pos)
        if r is None or not by_month:
            continue
        col = reference.COLS[field]
        base = by_month.get(1)
        if base is not None:
            ws.cell(r, col).value = base
        else:
            base = reference._num(ws.cell(r, col).value)
        changed = sorted(m for m, v in by_month.items() if base is None or abs(v - base) > 0.005)
        for m in changed:
            cells.setdefault((pos, m), {})[field] = by_month[m]
        if changed:
            said.append("%s — %s с %s" % (pos, field, MONTH_GEN[changed[0] - 1]))
    if REF_MONTHLY_SHEET in wb.sheetnames:
        del wb[REF_MONTHLY_SHEET]
    if not cells:
        return
    out = wb.create_sheet(REF_MONTHLY_SHEET)
    out.append(["должность", "месяц"] + list(REF_FIELDS))
    for (pos, m), got in sorted(cells.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        out.append([pos, m] + [got.get(f) for f in REF_FIELDS])
    if warn is not None:
        warn.append("Справочник в году плана меняется: %s." % "; ".join(said))


MGMT_SHEET = "управленческие_подразделения"


def fill_mgmt(wb, rows):
    """Лист «управленческие_подразделения» (01.10.2026): строки зарплаты с
    подразделением — снимок для раздела 16 отчёта. Решатель его не читает:
    подразделение на план не влияет, только делит выплаты в отчёте."""
    if MGMT_SHEET in wb.sheetnames:
        del wb[MGMT_SHEET]
    if not rows:
        return
    ws = wb.create_sheet(MGMT_SHEET)
    ws.append(["код строки", "подразделение", "порядок", "месяц", "сумма"])
    for code, dept, order, month, amount in rows:
        ws.append([code, dept, order, month, amount])


def build(template_path, out_path, data, warn):
    """Собрать входной файл. ``data`` — то, что лежит в реестре организации."""
    wb = openpyxl.load_workbook(template_path)
    # Величины справочника по датам — по месяцам года плана (01.10.2026).
    fill_reference_months(wb, data.get("year"), warn)

    fill_employees(wb["сотрудники"], data.get("employees") or [], warn,
                   data.get("year"), data.get("workgroups"))
    if data.get("employees"):
        fill_salary_months(wb, data.get("salary_months") or [], data["employees"],
                           data.get("year"), data.get("fact_through") or 0)
    fill_mgmt(wb, data.get("mgmt_rows") or [])
    fill_contracts(wb["договоры"], data.get("contracts") or [], warn,
                   data.get("year"))
    # Поступления в расчёт не подаются (ПРАВИЛА_РЕШАТЕЛЯ, правило 21;
    # ИСТОЧНИКИ, SOURCE-009; решение пользователя 18.09.2026). Без листа
    # «фот_по_месяцам» загрузчик решателя сам считает весь ФОТ договора
    # поступившим в первый месяц его выплат (fot_schedule.
    # default_fot_inflow_at_start): раньше начала договора тратить нельзя, а
    # ровная разбивка по месяцам остаётся планом освоения, а не потолком.
    # Фиксации сумм живут на том же листе и уходят вместе с ним.
    for name in ("фот_по_месяцам", "фиксация_фот_по_месяцам"):
        if name in wb.sheetnames:
            del wb[name]
    if "трудоемкость_по_договорам" in wb.sheetnames:
        fill_labor(wb["трудоемкость_по_договорам"], data.get("labor") or [], warn,
                   data.get("contracts") or [], data.get("year"))
    if "120_надбавка" in wb.sheetnames:
        fill_secret(wb["120_надбавка"], data.get("secret") or [], warn)
    fill_substitutions(wb, data.get("substitutions") or [])
    year_for_manual = data.get("year")
    for name, key, prohibit in (("ручные_назначения", "manual_assignments", False),
                                ("ручные_запреты", "manual_prohibitions", True)):
        if name in wb.sheetnames:
            rows = [{**r, "год": r.get("год") or year_for_manual} for r in (data.get(key) or [])]
            fill_manual(wb[name], rows, prohibit)

    year = data.get("year")
    if year and "настройки" in wb.sheetnames:
        ws = wb["настройки"]
        col = _col(ws, "год")
        if col:
            ws.cell(2, col).value = year
    # Настройки расчета — из загруженного шаблона, если он их принес: допуск
    # трудоемкости и штрафы там выставлены под этот план, а не под демо.
    settings = data.get("settings") or {}
    if settings and "настройки" in wb.sheetnames:
        ws = wb["настройки"]
        taken = []
        for name, value in settings.items():
            if str(name).strip().lower() in ("год", "претензии") or value is None:
                continue
            col = _col(ws, name)
            if col is None:
                # Графы в шаблоне нет — веса целей появились позже него.
                # Загрузчик решателя читает лист по заголовкам, поэтому
                # достаточно дописать графу справа.
                col = ws.max_column + 1
                ws.cell(1, col).value = name
            ws.cell(2, col).value = value
            taken.append("%s: %s" % (name, value))
        if taken:
            # В ленту — только то, что решает исход: дефицит и допуск. Полный
            # перечень штрафов есть на листе «настройки» входного файла.
            key = [t for t in taken if t.split(":")[0] in ("разрешить дефицит", "допуск трудоёмкости")]
            rest = len(taken) - len(key)
            warn.append("Настройки расчета: %s%s."
                        % ("; ".join(key) or "%d параметров" % len(taken),
                           (" и еще %d" % rest) if key and rest else ""))

    fill_facts(wb, data.get("facts") or [], data.get("fact_through") or 0, warn)
    wb.save(out_path)
    return out_path


#: Лист факта начислений во входе решателя (30.09.2026) — договорённость о
#: входе: месяцы по «факт по месяц» расчёт берёт как были, не пересчитывая.
FACT_SHEET = "факт_выплат"
FACT_HEAD = ["код строки", "договор", "месяц", "вид выплаты", "сумма", "ставка",
             "основное место"]
FACT_MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
               "сентябрь", "октябрь", "ноябрь")


def fill_facts(wb, rows, through, warn):
    """Лист «факт_выплат» и «факт по месяц» в «настройках».

    Без выбора в плане (through = 0) листа нет и графа пустая — расчёт идёт,
    как шёл. Строка — сотрудник, договор, месяц и вид выплаты; ставка — у
    оклада; основное место — да/нет, пусто — решатель определит сам."""
    if FACT_SHEET in wb.sheetnames:
        del wb[FACT_SHEET]
    if "настройки" in wb.sheetnames:
        ws = wb["настройки"]
        col = _col(ws, "факт по месяц")
        if col is None and through:
            col = ws.max_column + 1
            ws.cell(1, col).value = "факт по месяц"
        if col is not None:
            ws.cell(2, col).value = int(through or 0)
    if not through:
        return
    ws = wb.create_sheet(FACT_SHEET)
    ws.append(FACT_HEAD)
    for r in rows:
        ws.append([r.employee_code, r.contract_code, r.month, r.kind, r.amount,
                   r.rate if r.kind == "оклад" else None,
                   None if r.is_main is None else ("да" if r.is_main else "нет")])
    warn.append("Факт начислений по %s включительно: строк %d; эти месяцы расчёт не "
                "пересчитывает." % (FACT_MONTHS[through - 1], len(rows)))
