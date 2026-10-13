# -*- coding: utf-8 -*-
"""План ФОТ на год — данные для отчёта в формах экономистов.

Отчёт собран по прототипу, согласованному с экономистами: пятнадцать
разделов, в каждом только числа и графы. Считается по входному файлу и
готовому плану выплат, без слов решателя — так же, как проверки в rules.py.

Названия разделов и граф формальные, а сравниваемые величины соизмеримы
(владелец продукта 30.09.2026: «в результатах расчёта есть таблицы,
например — нехватка, очень плохо названа неформально. Нужно жёстко
формализовать»; «просмотри все таблицы в План ФОТ, нужно чтобы там были
величины соизмеримые, то есть если мы сравниваем, чтобы было понятно что
сравниваем»). Поэтому у суммы есть единица и период, а предел сравнивается
ровно с той частью выплат, которую он ограничивает: П2556 — с окладом и 122,
БЭП — с окладом и 122 на открытые ставки, П4 — с окладом, 122 и 124.

Разделы: 1 сводные показатели; 2 договоры (реквизиты, исполнение по ФОТ);
3 освоение ФОТ помесячно против равномерного графика; 4 освоение ФОТ
нарастающим итогом; 5 выплаты по договорам и видам; 6 выплаты сотрудникам
(помесячно, регистр назначений, ШР детализация); 7 сотрудники и ставки;
8 средняя оклада и 122 против БЭП; 9 приказы № 2556 и № 4; 10 трудоёмкость
по строкам РКМ; 11 распределение трудоёмкости по исполнителям; 12
незакрытая трудоёмкость; 13 параметры расчёта и 14 недоплата — данными
отсюда, версии расчёта (13) и протокол контроля (15) — в интерфейсе по
rules.py.
"""
from __future__ import annotations

import datetime as dt

import rules
from rules import SHORT, _by, _num, _rows_of, _sum, _yes

#: Порог сравнения денег: копейки в плане ФОТ ничего не решают.
EPS = rules.EPS
#: Порог сравнения человеко-месяцев — половина последнего показанного знака.
#: Это округление, а не допуск: чел.-мес. показываются с двумя знаками.
PM_EPS = 0.005
#: Порядок видов выплат — как в заработной плате: оклад, надбавки, приказ.
KIND_ORDER = ("оклад", "120", "122", "124", "152", "приказ")
_KIND_ORDER = {k: i for i, k in enumerate(KIND_ORDER)}

#: Код и название параметра в форме кадров («ШР детализация»). Это же
#: название вида выплаты в разделе 5: прежние «120 — гостайна» склеивали код
#: и обиходное имя тире (UI-015).
_PARAM = {"оклад": ("1", "Оклад (должностной оклад)"),
          "120": ("120", "За работу со сведениями, составляющими государственную тайну"),
          "122": ("122", "За качество выполняемых работ"),
          "124": ("124", "За интенсивность и высокие результаты работы"),
          "152": ("152", "За выполнение дополнительной работы"),
          "приказ": ("приказ", "Стимулирующая выплата приказом")}

#: Состояния договора на конец года (раздел 2.2): слово и цвет.
CONTRACT_STATES = {
    "Выплат нет": "er",
    "Выплаты после срока выплаты оклада": "er",
    "Выплаты после срока выплаты надбавок": "er",
    "Перерыв в выплатах": "wr",
    "ФОТ освоен до срока выплат": "wr",
    "ФОТ освоен не полностью": "wr",
    "Замечаний нет": "ok",
}


def _date(v):
    """«dd.mm.yyyy» из даты, строки или порядкового номера Excel."""
    d = rules._date_of(v)
    if d is not None:
        return d.strftime("%d.%m.%Y")
    return None if v in (None, "") else str(v).strip()[:10]


def _r(v, d=2):
    return None if v is None else round(float(v), d)


def _pct(a, b):
    return round(a / b, 4) if b and a is not None else None


def _extra_input(path):
    """То, что rules._read_input не хранит: даты строкой, счета, категории."""
    from openpyxl import load_workbook
    from fot_planner.position_reference import normalize_position

    wb = load_workbook(path, data_only=True)
    ctr, cat, head, emp = {}, {}, {}, {}
    for row in _rows_of(wb["договоры"]):
        code = row.get("код")
        if not code:
            continue
        end = _date(row.get("дата окончания"))
        ctr[str(code)] = {
            "дата начала": _date(row.get("дата начала")),
            "дата окончания": end,
            # Пустой срок выплат равен окончанию договора (SOURCE-001), как у
            # загрузчика решателя. Сроков два: оклад и надбавки платятся до
            # своих дат, и одна графа «срок выплат» показывала только первый.
            "срок выплаты оклада": _date(row.get("конечная дата выплат оклада")) or end,
            "срок выплаты надбавок": _date(row.get("конечная дата выплат надбавок")) or end,
            "счет": row.get("счет") or None,
            "тип": row.get("тип договора") or None,
            "подразделение": row.get("подразделение") or None,
            # Признак договора — «ГОЗ» или «Приоритет 2030» (UI-014); без них —
            # вид работ из реестра.
            "признак": ", ".join(
                [x for x in ("ГОЗ" if _yes(row.get("ГОЗ")) else None,
                             "Приоритет 2030" if _yes(row.get("Приоритет")) else None) if x]
            ) or (row.get("тип договора") or None),
        }
    if "лимиты_по_должностям" in wb.sheetnames:
        for row in _rows_of(wb["лимиты_по_должностям"]):
            if row.get("должность"):
                cat[normalize_position(str(row["должность"]))] = row.get("категория персонала") or None
    if "трудоемкость_по_договорам" in wb.sheetnames:
        for row in _rows_of(wb["трудоемкость_по_договорам"]):
            if row.get("договор"):
                key = (str(row["договор"]), normalize_position(str(row.get("должность") or "")))
                head[key] = _num(row.get("количество человек"))
    for row in _rows_of(wb["сотрудники"]):
        if row.get("код строки"):
            emp[str(row["код строки"])] = {
                "табельный": str(row.get("табельный номер") or row.get("код строки")),
                "категория занятости": str(row.get("категория занятости") or "").strip().lower(),
                "тип занятости": str(row.get("тип занятости") or "").strip().lower(),
            }
    return ctr, cat, head, emp


NO_DEPT = "Без подразделения"


def _split_kopecks(total, weights):
    """Сумма по весам в копейках методом наибольшего остатка: части в сумме
    ровно равны целому."""
    cents = int(round(total * 100))
    w = sum(weights)
    if not w:
        return [0.0] * len(weights)
    raw = [cents * x / w for x in weights]
    base = [int(v) for v in raw]
    rest = cents - sum(base)
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - base[i], reverse=True)[:rest]:
        base[i] += 1
    return [b / 100 for b in base]


def _by_department(input_path, plan, inp, emp_extra):
    """Раздел 16 — ФОТ по управленческим подразделениям помесячно (владелец
    продукта 01.10.2026: «как у управленческих подразделений распределяются
    деньги»; свод в стиле 1С).

    Выплаты человека в месяце (план, в месяцах факта — факт: план расчёта
    их уже несёт) делятся между его строками зарплаты пропорционально их
    суммам этого месяца — по снимку листа «управленческие_подразделения»
    входа. Строк в месяце нет — сумма в «Без подразделения». Деление — в
    копейках, итог подразделений за месяц ровно равен итогу организации."""
    from openpyxl import load_workbook
    wb = load_workbook(input_path, data_only=True, read_only=True)
    shares, order = {}, {}
    try:
        if "управленческие_подразделения" in wb.sheetnames:
            for row in _rows_of(wb["управленческие_подразделения"]):
                code, month = row.get("код строки"), _num(row.get("месяц"))
                if not code or not month:
                    continue
                dept = str(row.get("подразделение") or "").strip() or NO_DEPT
                key = (str(code), int(month))
                got = shares.setdefault(key, {})
                got[dept] = got.get(dept, 0.0) + (_num(row.get("сумма")) or 0.0)
                if dept != NO_DEPT:
                    pos = _num(row.get("порядок"))
                    order[dept] = pos if pos is not None else order.get(dept)
    finally:
        wb.close()
    fio = {e["code"]: e.get("fio") or e["code"] for e in inp["employees"]}
    paid = {}
    for p in plan:
        if p["amount"]:
            key = (p["emp"], p["month"])
            paid[key] = paid.get(key, 0.0) + p["amount"]
    depts = {}
    for (code, month), amount in paid.items():
        got = shares.get((code, month)) or {NO_DEPT: 1.0}
        names = sorted(got)
        for name, part in zip(names, _split_kopecks(amount, [got[n] for n in names])):
            if not part:
                continue
            d = depts.setdefault(name, {"люди": {}, "месяцы": [0.0] * 12})
            d["месяцы"][month - 1] += part
            person = d["люди"].setdefault(code, [0.0] * 12)
            person[month - 1] += part
    out = []
    for name in sorted(depts, key=lambda n: (n == NO_DEPT, order.get(n) is None,
                                             order.get(n) or 0, n.lower())):
        d = depts[name]
        people = [{"табельный": (emp_extra.get(c) or {}).get("табельный") or c,
                   "фио": fio.get(c, c),
                   "месяцы": [_r(v) if v else None for v in vals],
                   "год": _r(sum(vals))}
                  for c, vals in sorted(d["люди"].items(), key=lambda kv: str(fio.get(kv[0], kv[0])))]
        out.append({"подразделение": name, "люди": people,
                    "месяцы": [_r(v) if v else None for v in d["месяцы"]],
                    "год": _r(sum(d["месяцы"]))})
    org = [0.0] * 12
    for d in depts.values():
        for i, v in enumerate(d["месяцы"]):
            org[i] += v
    return {"подразделения": out, "итого": [_r(v) if v else None for v in org],
            "год": _r(sum(org))}


def _ddmmyyyy(text):
    try:
        d, m, y = str(text).strip()[:10].split(".")
        return dt.date(int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return None


def _staff_detail(inp, res, extra, cats, base_of, year, open_rate, main_at):
    """Форма кадров «ШР на дату, детализация»: назначение — строка на каждый
    непрерывный период, где у человека на договоре одна и та же выплата
    одной суммы и одной ставки. Графы те же, что в шаблоне отдела кадров;
    номер и дата приказа сервису неизвестны — пусто.

    Договор, где у человека нет открытой ставки (только надбавки), ставки и
    основного места не получает: прежде такой строке подставлялась штатная
    ставка и «основное место», которых на этом договоре нет.
    """
    year = year or dt.date.today().year
    emps = {e["code"]: e for e in inp["employees"]}
    ctr = inp["contracts"]
    groups = {}
    for (ecode, contract, month), rr in _by(res["plan"], "emp", "contract", "month").items():
        e = emps.get(ecode)
        if e is None:
            continue
        rate = open_rate(ecode, contract, month, rr)
        rate = rate if rate and rate > 0 else None
        main = (main_at.get((ecode, month)) == contract) if rate else None
        for p in rr:
            if p["kind"] not in _PARAM or not p["amount"]:
                continue
            base = base_of(p["position"], month) if p["kind"] == "оклад" else None
            nominal = base if base else (p["amount"] / rate if rate else p["amount"])
            key = (ecode, contract, p["kind"], main, p["position"],
                   round(rate, 4) if rate else None, round(nominal, 2), round(p["amount"], 2))
            groups.setdefault(key, set()).add(month)
    rows = []
    for key, months in groups.items():
        code, contract, kind, main, position, rate, nominal, amount = key
        e = emps[code]
        c = ctr.get(contract) or {}
        x = extra.get(contract, {})
        c_from = _ddmmyyyy(x.get("дата начала"))
        c_to = _ddmmyyyy(x.get("срок выплаты оклада") if kind == "оклад"
                         else x.get("срок выплаты надбавок"))
        ordered = sorted(months)
        spans, start = [], ordered[0]
        for prev, m in zip(ordered, ordered[1:]):
            if m != prev + 1:
                spans.append((start, prev))
                start = m
        spans.append((start, ordered[-1]))
        pcode, pname = _PARAM[kind]
        if rate:
            rate_text = ("%g" % rate).replace(".", ",")
            post = "%s, %s%s ставк." % (position, "" if main else "внутр. совмест., ", rate_text)
        else:
            post = position
        for m1, m2 in spans:
            d1 = dt.date(year, m1, 1)
            if c_from and c_from.year == year and c_from > d1:
                d1 = c_from
            last = 31 if m2 == 12 else (dt.date(year, m2 + 1, 1) - dt.timedelta(days=1)).day
            d2 = dt.date(year, m2, last)
            if c_to and c_to.year == year and c_to < d2:
                d2 = c_to
            rows.append({
                "таб": code, "назначение": None, "фио": e["fio"], "код_подр": None,
                "подразделение": e["department"] or None,
                "должность": post,
                "категория": cats.get(inp["norm"](position)),
                "код": pcode, "параметр": pname, "ставка": rate,
                "номинал": _r(nominal), "по_ставке": _r(amount), "сумма": _r(amount),
                "начало": d1.strftime("%d.%m.%Y"), "окончание": d2.strftime("%d.%m.%Y"),
                "код_шифра": contract, "шифр": c.get("name") or None, "счет": x.get("счет"),
                "_k": (e["fio"], d1, 0 if main else 1, _KIND_ORDER.get(kind, 9), contract),
            })
    rows.sort(key=lambda r: r["_k"])
    for r in rows:
        r.pop("_k")
    return rows


def limit_2556(plan, lim, norm, emp_extra, rate_of=None, last=0):
    """Раздел 9.1: приказ № 2556 — оклад и 122 не выше предела должности на
    открытую ставку (правило 4 свода; пользователь 30.09.2026: «в результатах
    там должна быть таблица по 2556»).

    Строка — сотрудник, месяц, договор, как в проверке «П2556: оклад и 122 не
    выше предела по должности» (rules.py): предел — П2556 должности на 1,0
    ставки, умноженный на открытую ставку договора. ``rate_of(rows)`` —
    открытая ставка набора строк одного договора и месяца (rules.open_rates);
    без неё — оклад на договоре, делённый на оклад должности. Запас — предел
    минус оклад и 122, в рублях и в доле предела, одним знаком: плюс — ниже
    предела, минус — выше. Должности без П2556 в справочнике в таблицу не
    попадают — сверять не с чем. Месяцы 1…``last`` — факт начислений:
    строка есть, состояние «Факт», не судится (правило 26)."""
    rows = []
    for (ecode, m, code), rr in sorted(_by(plan, "emp", "month", "contract").items()):
        p2556 = rules.lim_at(lim, norm, rr[0]["position"], "П2556", m)
        base = rules.lim_at(lim, norm, rr[0]["position"], "оклад", m)
        okl = _sum(rr, {"оклад"})
        if rate_of is not None:
            rate = rate_of(rr)
        else:
            rate = okl / base if base else None
        if not p2556 or not rate:
            continue
        fact, cap = _sum(rr, {"оклад", "122"}), p2556 * rate
        rows.append({"табельный": emp_extra.get(ecode, {}).get("табельный") or ecode,
                     "фио": rr[0]["fio"], "должность": rr[0]["position"],
                     "договор": code, "месяц": m,
                     "оклад": _r(okl), "122": _r(_sum(rr, {"122"})), "итого": _r(fact),
                     "ставка": _r(rate, 2), "П2556": _r(p2556), "предел": _r(cap),
                     "запас": _r(cap - fact), "запас %": _pct(cap - fact, cap),
                     "состояние": (FACT if m <= last else
                                   "Выше предела" if fact > cap + EPS else "Не выше предела")})
    return rows


def _state(diff, words):
    """Состояние сравнения с нормой с точностью до рубля: равно, ниже, выше."""
    if abs(diff) <= EPS:
        return words[0]
    return words[1] if diff < 0 else words[2]


#: Состояние строки месяца факта (правило 26): месяц показан, в годовые итоги
#: входит, но не судится — ни отметкой раздела, ни замечанием.
FACT = "Факт"


def report(input_path, result_path):
    from fot_planner.open_rate_rules import (OPEN_RATE_STEP, normalize_employment_category,
                                              normalize_employment_type, row_max_total_quarters)

    inp = rules._read_input(input_path)
    res = rules._read_result(result_path)
    extra, cats, heads, emp_extra = _extra_input(input_path)
    plan, ctr, lim, norm = res["plan"], inp["contracts"], inp["limits"], inp["norm"]
    year = int(inp["year"] or 0) or None
    emps = {e["code"]: e for e in inp["employees"]}
    open_rate = rules.open_rates(res, lim, norm)
    sheet_rates = res.get("rates")
    # Месяцы 1…F — факт начислений (правило 26; владелец продукта 30.09.2026:
    # «пересчёт с текущими вводными… не трогая январь и февраль»). Они
    # показаны и входят в итоги за год, в освоение ФОТ и в трудоёмкость, но не
    # судятся: состояние строки — «Факт», отметок раздела и замечаний нет.
    last = res.get("frozen") or 0

    def base_of(position, month=None):
        return rules.lim_at(lim, norm, position, "оклад", month)

    def tab(ecode):
        return emp_extra.get(ecode, {}).get("табельный") or ecode

    def rate_rows(rr):
        return open_rate(rr[0]["emp"], rr[0]["contract"], rr[0]["month"], rr) if rr else None

    paid = {c: [0.0] * 12 for c in ctr}
    for p in plan:
        if p["contract"] in paid:
            paid[p["contract"]][p["month"] - 1] += p["amount"]
    total_fot = sum(c["fot"] for c in ctr.values())
    total_paid = sum(sum(v) for v in paid.values())

    # ── 1. сводные показатели ─────────────────────────────────────────
    # Поступлений и «остатка на счетах» нет: поступления в расчёт не
    # подаются, а касса договора отдельной величиной не показывается
    # (SOURCE-009).
    totals = {"ФОТ": _r(total_fot), "выплачено": _r(total_paid),
              "остаток ФОТ": _r(total_fot - total_paid)}

    # ── 2. договоры: реквизиты и исполнение по ФОТ на конец года ─────
    contracts = []
    for code, c in sorted(ctr.items()):
        x = extra.get(code, {})
        pay_m = paid[code]
        salary_m, other_m = [0.0] * 12, [0.0] * 12
        for p in plan:
            if p["contract"] == code:
                (salary_m if p["kind"] == "оклад" else other_m)[p["month"] - 1] += p["amount"]
        with_pay = [m for m in range(1, 13) if pay_m[m - 1] > 0.5]
        cpaid = sum(pay_m)
        # Месяцы от начала договора до срока выплаты оклада: в них возможна
        # открытая ставка. Прежняя графа «Месяцев действия» считала их же.
        salary_months = max(0, min(c["to"], c["salary_to"]) - c["from"] + 1)
        status = []
        if not with_pay:
            status.append("Выплат нет")
        else:
            # Каждый вид — до своего срока: надбавка после срока оклада, но в
            # срок надбавок, нарушением не является.
            # Месяц факта — история: выплату после срока или перерыв в нём
            # план не исправит, и это не его нарушение (правило 26).
            if any(salary_m[m - 1] > 0.5 for m in range(max(c["salary_to"], last) + 1, 13)):
                status.append("Выплаты после срока выплаты оклада")
            if any(other_m[m - 1] > 0.5 for m in range(max(c["allowance_to"], last) + 1, 13)):
                status.append("Выплаты после срока выплаты надбавок")
            if any(pay_m[m - 1] <= 0.5 for m in range(max(with_pay[0], last + 1), with_pay[-1] + 1)):
                status.append("Перерыв в выплатах")
            if c["fot"] - cpaid <= EPS and with_pay[-1] < c["window"][1]:
                status.append("ФОТ освоен до срока выплат")
            if c["fot"] - cpaid > EPS:
                status.append("ФОТ освоен не полностью")
        contracts.append({
            "код": code, "название": c["name"], "ГОЗ": c["goz"],
            "признак": x.get("признак"), "счет": x.get("счет"), "тип": x.get("тип"),
            "выплаты": ", ".join(k for k in KIND_ORDER if c["kinds"].get(k)),
            "дата начала": x.get("дата начала"), "дата окончания": x.get("дата окончания"),
            "срок выплаты оклада": x.get("срок выплаты оклада"),
            "срок выплаты надбавок": x.get("срок выплаты надбавок"),
            "с": c["from"], "по": c["to"], "окно": list(c["window"]),
            "подразделение": x.get("подразделение"),
            "месяцев с выплатами": len(with_pay), "месяцев до срока оклада": salary_months,
            "ФОТ": _r(c["fot"]), "выплачено": _r(cpaid),
            "остаток": _r(c["fot"] - cpaid), "освоение": _pct(cpaid, c["fot"]),
            "статусы": status or ["Замечаний нет"],
            "статус": ", ".join(status) or "Замечаний нет",
        })

    # ── 3. освоение ФОТ помесячно против равномерного графика ────────
    # Равномерный график — ФОТ договора ровно по месяцам окна выплат (от
    # начала до позднего срока выплат), как план освоения решателя
    # (fot_schedule.monthly_spend_targets, стадия равномерности). Прежде ФОТ
    # делился на месяцы действия договора, и договор со сроком выплат в
    # октябре получал «план» на ноябрь и декабрь, которых у него нет.
    usage = []
    org3 = {"выплаты": [0.0] * 12, "график": [0.0] * 12}
    seen3 = [False] * 12
    for code, c in sorted(ctr.items()):
        first, end = c["window"]
        window = list(range(first, end + 1))
        # С фактом (правило 26) график — как у решателя
        # (optimizer._relevel_spend_targets): месяцы факта в нём не участвуют,
        # а ФОТ за вычетом факта ровно раскладывается на оставшиеся месяцы
        # окна. Договор, который начинается после факта и ничего не потратил,
        # график не меняет.
        spent = sum(paid[code][m - 1] for m in range(1, last + 1))
        future = [m for m in window if m > last]
        if last and (len(future) != len(window) or spent > 0.005):
            even = {m: max(0.0, c["fot"] - spent) / len(future) for m in future} if future else {}
        else:
            even = {m: c["fot"] / len(window) for m in window}
        pays, sched, dev = [], [], []
        for m in range(1, 13):
            inside = first <= m <= end
            v = paid[code][m - 1]
            # Выплата вне окна — нарушение срока; её видно, а не прячет штриховка.
            pays.append(_r(v) if inside or v > 0.5 else None)
            sched.append(_r(even[m]) if m in even else None)
            # Месяц факта не сравнивается с графиком: его выплаты — история.
            d = v - even.get(m, 0.0)
            dev.append(_r(d) if m > last and (m in even or v > 0.5) and abs(d) > 0.5 else None)
            if inside or v > 0.5:
                seen3[m - 1] = True
                org3["выплаты"][m - 1] += v
                org3["график"][m - 1] += even.get(m, 0.0)
        usage.append({"код": code, "название": c["name"], "признак": extra.get(code, {}).get("признак"),
                      "счет": extra.get(code, {}).get("счет"),
                      "выплаты": pays, "график": sched, "отклонение": dev})
    usage_total = {
        "выплаты": [_r(org3["выплаты"][i]) if seen3[i] else None for i in range(12)],
        "график": [_r(org3["график"][i]) if seen3[i] else None for i in range(12)],
        "отклонение": [_r(org3["выплаты"][i] - org3["график"][i])
                       if seen3[i] and i + 1 > last
                       and abs(org3["выплаты"][i] - org3["график"][i]) > 0.5 else None
                       for i in range(12)],
    }

    # ── 4. освоение ФОТ нарастающим итогом ───────────────────────────
    # Позиция договора на конец каждого месяца без поступлений: выплаты
    # нарастающим итогом, остаток ФОТ и доля освоения. ФОТ считается
    # поступившим в первый месяц окна выплат (правило 21, SOURCE-009). Остаток
    # — на конец месяца: прежняя таблица показывала остаток на начало месяца,
    # сдвинутый на месяц, и месяцы вне срока пустыми.
    cumul = []
    org4 = {"выплаты": [0.0] * 12, "остаток": [0.0] * 12, "ФОТ": [0.0] * 12}
    seen4 = [False] * 12
    for code, c in sorted(ctr.items()):
        first, fot = c["window"][0], c["fot"]
        cum, pays, rest, share, over = 0.0, [], [], [], []
        # Минус в месяцах факта — история; план её не покроет, но и
        # перерасход не растит (как решатель): судится остаток месяцев плана
        # против уровня, с которым их оставил факт.
        floor = 0.0
        for m in range(1, 13):
            cum += paid[code][m - 1]
            started = m >= first
            if not started and cum <= 0.5:
                pays.append(None)
                rest.append(None)
                share.append(None)
                over.append(False)
                continue
            got = fot if started else 0.0
            if m <= last:
                floor = min(0.0, got - cum)
            pays.append(_r(cum))
            rest.append(_r(got - cum))
            share.append(_pct(cum, fot) if started else None)
            over.append(m > last and got - cum < floor - 0.5)
            seen4[m - 1] = True
            org4["выплаты"][m - 1] += cum
            org4["остаток"][m - 1] += got - cum
            org4["ФОТ"][m - 1] += got
        cumul.append({"код": code, "название": c["name"], "признак": extra.get(code, {}).get("признак"),
                      "счет": extra.get(code, {}).get("счет"),
                      "выплаты": pays, "остаток": rest, "освоение": share, "сверх ФОТ": over})
    cumul_total = {
        "выплаты": [_r(org4["выплаты"][i]) if seen4[i] else None for i in range(12)],
        # Итог по организации — только по договорам, чьи выплаты уже начались:
        # ФОТ договора, начинающегося в мае, в январский остаток не входит.
        "остаток": [_r(org4["остаток"][i]) if seen4[i] else None for i in range(12)],
        "освоение": [_pct(org4["выплаты"][i], org4["ФОТ"][i]) if seen4[i] else None
                     for i in range(12)],
    }

    # ── 5. выплаты по договорам и видам ──────────────────────────────
    kinds = []
    for code, c in sorted(ctr.items()):
        crows = [p for p in plan if p["contract"] == code]
        cpaid = _sum(crows)
        for k in KIND_ORDER:
            s = _sum(crows, {k})
            allowed = c["kinds"].get(k)
            if not s and not allowed:
                continue
            months = len({p["month"] for p in crows if p["kind"] == k and p["amount"]})
            # «Доля фонда» была долей всех выплат плана, а не ФОТ: так её и
            # называем, чтобы не сравнивать с ФОТ договора.
            kinds.append({"договор": code, "вид": k, "наименование": _PARAM[k][1],
                          "сумма": _r(s), "доля договора": _pct(s, cpaid),
                          "доля плана": _pct(s, total_paid),
                          "месяцев": months, "разрешен": bool(allowed)})

    # ── 6.2, 7. регистр назначений и ставки по месяцам ───────────────
    # Основное место решатель выбирает на каждый месяц отдельно: ставка на
    # нем равна штатной, остальные ставки того же месяца — внутреннее
    # совместительство не больше 0,5. В сентябре человек может целиком
    # перейти с одного договора на другой — и основное место переедет с ним.
    # Внешний совместитель основного места здесь не имеет, его предел 0,5.
    register, rates_tbl, main_at = [], [], {}
    for e in inp["employees"]:
        erows = [p for p in plan if p["emp"] == e["code"]]
        if not erows:
            continue
        position = e["position"]
        cat = cats.get(norm(position))
        ex = emp_extra.get(e["code"], {})
        cat_emp, type_emp = ex.get("категория занятости"), ex.get("тип занятости")
        outer_part = "совмест" in (type_emp or "")
        by_c = _by(erows, "contract")
        person_rates, month_parts = {}, {}
        for (code,), crows in sorted(by_c.items()):
            rate_m, parts_m = [None] * 12, [None] * 12
            for m in range(1, 13):
                mr = [p for p in crows if p["month"] == m]
                if not mr:
                    continue
                okl, k122 = _sum(mr, {"оклад"}), _sum(mr, {"122"})
                r = open_rate(e["code"], code, m, mr)
                rate_m[m - 1] = _r(r, 2) if r else (0.0 if r is not None else None)
                kinds_m = tuple(sorted({p["kind"] for p in mr if p["kind"] and p["amount"]},
                                       key=lambda k: _KIND_ORDER.get(k, 9)))
                parts_m[m - 1] = (okl, k122, _sum(mr) - okl - k122, kinds_m)
            person_rates[code] = rate_m
            month_parts[code] = parts_m
        # Основное место — как его выбрал решатель (лист «открытые_ставки»).
        # Без листа (старый результат) — договор с наибольшей ставкой в месяце.
        main_m = [None] * 12
        if sheet_rates is not None:
            for r in sheet_rates:
                if r["emp"] == e["code"] and r["main"] and r["rate"]:
                    main_m[r["month"] - 1] = r["contract"]
        else:
            for i in range(12):
                cand = [(rm[i], code) for code, rm in person_rates.items() if rm[i]]
                if cand:
                    main_m[i] = max(cand)[1]
        for i, code in enumerate(main_m):
            if code:
                main_at[(e["code"], i + 1)] = code
        for code in sorted(person_rates):
            rate_m, parts_m = person_rates[code], month_parts[code]
            periods, cur = [], None
            for m in range(1, 13):
                part = parts_m[m - 1]
                if part is None:
                    cur = None
                    continue
                okl, k122, other, kinds_m = part
                is_main = main_m[m - 1] == code
                # Период — месяцы подряд с одними условиями оплаты: ставка,
                # основное место, оклад, 122, прочие выплаты и их состав.
                # 122 отдельно от прочих: предел П2556 ограничивает только
                # оклад с 122 (правило 4), а «фонд надбавок» сваливал её в одну
                # сумму с 120, 124, 152 и приказом.
                # Предел П2556 месяца — в ключе: приказ вступил с даты, и
                # период с новым пределом — отдельная строка (01.10.2026).
                key = (_r(okl), _r(k122), _r(other), rate_m[m - 1], is_main, kinds_m,
                       m <= last, rules.lim_at(lim, norm, position, "П2556", m))
                if cur and cur["ключ"] == key and cur["по"] == m - 1:
                    cur["по"], cur["месяцев"] = m, cur["месяцев"] + 1
                else:
                    cur = {"ключ": key, "с": m, "по": m, "месяцев": 1}
                    periods.append(cur)
            acc = extra.get(code, {}).get("счет")
            has_salary = any(p and p[0] for p in parts_m)
            months_main = sum(1 for i in range(12) if main_m[i] == code)
            months_on = sum(1 for i in range(12) if rate_m[i])
            how = ("Без открытой ставки" if not months_on
                   else ("Совместительство" if outer_part or not months_main
                         else ("Основное место работы" if months_main == months_on
                               else "Основное место работы и совместительство")))
            out_periods = []
            for p in periods:
                okl, k122, other, rate, is_main, kinds_m, fact, limit_m = p["ключ"]
                limit = limit_m * rate if limit_m and rate else None
                out_periods.append({
                    "с": p["с"], "по": p["по"], "месяцев": p["месяцев"], "ставка": rate,
                    "оклад": okl, "122": k122, "прочие": other,
                    "виды": ", ".join(kinds_m) or None,
                    "лицевой счет надбавки": acc if any(k != "оклад" for k in kinds_m) else None,
                    "занятость": ("Основное место работы" if is_main and not outer_part
                                  else ("Совместительство" if rate else None)),
                    "итого": _r((okl + k122 + other) * p["месяцев"]),
                    # Период факта: запас показан, но не судится (правило 26).
                    "факт": fact,
                    "лимит": _r(limit),
                    # Запас до предела — только по окладу и 122, которые
                    # предел и ограничивает; прочие выплаты в него не входят.
                    "запас": _r(limit - okl - k122) if limit is not None else None,
                })
            register.append({
                "табельный": e.get("person_code") or e["code"], "отдел": e["department"] or None,
                "фио": e["fio"], "категория персонала": cat, "должность": position,
                "занятость": how,
                "тип занятости": ("внешний совместитель" if outer_part else "основной"),
                "лицевой счет оклада": acc if has_salary else None, "договор": code,
                "периоды": out_periods,
            })
        # Предел суммарной открытой ставки сотрудника — как у решателя
        # (open_rate_rules.row_max_total_quarters): совместитель — 0,5;
        # студент и лаборант — штатная ставка без добавки; аспирант — 0,75;
        # остальные — 1,5. Прежде студенту ставилось 0,5, а лаборанту — 1,5.
        total_limit = OPEN_RATE_STEP * row_max_total_quarters(
            normalize_employment_category(cat_emp), normalize_employment_type(type_emp),
            float(e["rate"] or 0.0), position)
        total_m = [None] * 12
        for code, rm in person_rates.items():
            for i, v in enumerate(rm):
                if v is not None:
                    total_m[i] = (total_m[i] or 0.0) + v
        contracts_r = []
        for code, rm in sorted(person_rates.items()):
            if not any(rm):
                continue    # договор только с надбавкой: ставки на нем нет
            mains = [False] * 12 if outer_part else [main_m[i] == code for i in range(12)]
            months_main = sum(mains)
            months_on = sum(1 for v in rm if v)
            contracts_r.append({
                "код": code,
                "занятость": ("основное" if months_main and months_main == months_on
                              else ("совместительство" if not months_main else "смешанная")),
                "основное": mains, "месяцы": rm,
                "макс": max([v for v in rm if v is not None] or [None]),
            })
        rates_tbl.append({
            "табельный": e.get("person_code") or e["code"], "фио": e["fio"],
            "должность": position, "отдел": e["department"] or None,
            "категория персонала": cat, "штатная": e["rate"],
            "категория занятости": cat_emp or None,
            "тип занятости": ("внешний совместитель" if outer_part else "основной"),
            "договоры": contracts_r,
            "всего": [_r(v) for v in total_m], "предел": total_limit,
            "макс": max([v for v in total_m if v is not None] or [None]),
        })

    # ── 6.1 выплаты по месяцам: сотрудник → договор → вид выплаты ────
    # План выплат как он есть: каждая сумма в своём месяце. Регистр (6.2)
    # сворачивает месяцы в периоды, и помесячная картина в нём не видна.
    monthly, org_m = [], [0.0] * 12
    # Факт начислений бывает и у того, кого в листе сотрудников нет (уволен
    # или снят с расчёта): его деньги потрачены с договора, и без его строки
    # итог по организации не сходился бы с разделами 3 и 4.
    known = {e["code"] for e in inp["employees"]}
    gone = {}
    for p in plan:
        if p["emp"] not in known and p["emp"] not in gone:
            gone[p["emp"]] = {"code": p["emp"], "person_code": p["emp"], "fio": p["fio"]}
    for e in list(inp["employees"]) + [gone[k] for k in sorted(gone)]:
        erows = [p for p in plan if p["emp"] == e["code"]]
        if not erows:
            continue
        lines = []
        groups = sorted(_by(erows, "contract", "kind").items(),
                        key=lambda kv: (kv[0][0], _KIND_ORDER.get(kv[0][1], 99), str(kv[0][1])))
        for (code, kind), rr in groups:
            vals = [None] * 12
            for p in rr:
                if p["amount"]:
                    vals[p["month"] - 1] = (vals[p["month"] - 1] or 0.0) + p["amount"]
            if not any(vals):
                continue
            lines.append({"договор": code, "вид": kind,
                          "месяцы": [_r(v) if v is not None else None for v in vals],
                          "год": _r(sum(v or 0.0 for v in vals))})
        tot = [None] * 12
        for p in erows:
            if p["amount"]:
                tot[p["month"] - 1] = (tot[p["month"] - 1] or 0.0) + p["amount"]
                org_m[p["month"] - 1] += p["amount"]
        monthly.append({"табельный": e.get("person_code") or e["code"], "фио": e["fio"],
                        "строки": lines,
                        "итого": [_r(v) if v is not None else None for v in tot],
                        "год": _r(sum(v or 0.0 for v in tot))})

    # ── 8. средняя оклада и 122 на 1,0 ставки по ГОЗ против БЭП ──────
    # Считается теми же суммами, что у решателя и проверки (rules.check):
    # оклад с 122 строк договора против суммы «БЭП должности × открытая
    # ставка». Средняя и БЭП на 1,0 ставки — эти суммы, делённые на сумму
    # ставок. Должность без БЭП берёт значение решателя (rules.bep_of), а не
    # ноль. Состояние — с точностью до рубля по сумме: равно — цель расчёта,
    # ниже — допустимо, но нежелательно, выше — нарушение (владелец продукта
    # 30.09.2026: «не критично если меньше получается, но лучше ровно… делай
    # ровно среднюю»).
    bep_value = rules.bep_of(lim, norm)
    bep = []
    for (code, m), rr in sorted(_by(plan, "contract", "month").items()):
        c = ctr.get(code)
        if not c or not c["goz"]:
            continue
        cap = staff = rates = 0.0
        for (ecode,), er in _by(rr, "emp").items():
            part = _sum(er, {"оклад", "122"})
            if not part:
                continue
            r = rate_rows(er)
            if r is None:
                r = (emps.get(ecode) or {}).get("rate") or 1.0
            cap += bep_value(er[0]["position"], m) * r
            rates += r
            staff += part
        if not staff and not cap:
            continue
        avg = staff / rates if rates else None
        limit = cap / rates if rates else None
        dev = avg - limit if avg is not None and limit is not None else None
        bep.append({"договор": code, "месяц": m, "сумма": _r(staff), "БЭП сумма": _r(cap),
                    "ставок": _r(rates), "средняя": _r(avg), "БЭП": _r(limit),
                    "отклонение": _r(dev), "отклонение %": _pct(dev, limit),
                    "недобор": _r(max(0.0, cap - staff)),
                    "состояние": FACT if m <= last else
                    _state(staff - cap, ("Равно БЭП", "Ниже БЭП", "Выше БЭП"))})

    # ── 9.2 приказ № 4: штатная часть в месяцы 124 против уровня П4 ──
    # Уровень П4 — норма, а не потолок (правило 5): решатель держит равенство,
    # и отклонение в любую сторону сверх рубля — нарушение. Уровень действует
    # только для категорий НТП и НР, как у решателя.
    p4 = []
    for (ecode, m), rr in sorted(_by(plan, "emp", "month").items()):
        if not any(p["kind"] == "124" for p in rr):
            continue
        limits = lim.get(norm(rr[0]["position"])) or {}
        level = rules.lim_at(lim, norm, rr[0]["position"], "П4", m)
        if not level or not rules.p4_applies(limits.get("категория")):
            continue
        rate = sum(filter(None, (rate_rows(cr) for (_,), cr in _by(rr, "contract").items())))
        if not rate:
            rate = (emps.get(ecode) or {}).get("rate") or 0.0
        if not rate:
            continue
        total = _sum(rr, {"оклад", "122", "124"})
        norm_value = level * rate
        p4.append({"табельный": tab(ecode), "фио": rr[0]["fio"], "должность": rr[0]["position"],
                   "месяц": m, "оклад": _r(_sum(rr, {"оклад"})), "122": _r(_sum(rr, {"122"})),
                   "124": _r(_sum(rr, {"124"})), "итого": _r(total), "ставка": _r(rate),
                   "П4": _r(level), "уровень": _r(norm_value),
                   "отклонение": _r(total - norm_value),
                   "отклонение %": _pct(total - norm_value, norm_value),
                   "состояние": FACT if m <= last else
                   _state(total - norm_value, ("Равно уровню П4", "Ниже уровня П4", "Выше уровня П4"))})

    # ── 9.3 приказ № 2556: стимулирующая выплата приказом ────────────
    # Выплата приказом ограничена тем же приказом отдельно от оклада с 122:
    # по строке за месяц по всем договорам — не выше П2556 должности на
    # штатную ставку строки, как у решателя (payroll_rules.
    # rule_order_incentive_2556_amount, _employee_limit_by_code).
    orders = []
    for (ecode, m), rr in sorted(_by(plan, "emp", "month").items()):
        amount = _sum(rr, {"приказ"})
        if not amount:
            continue
        e = emps.get(ecode) or {}
        position = e.get("position") or rr[0]["position"]
        p2556 = rules.lim_at(lim, norm, position, "П2556", m)
        staff_rate = e.get("rate") or 0.0
        if not p2556 or not staff_rate:
            continue
        cap = p2556 * staff_rate
        orders.append({"табельный": tab(ecode), "фио": rr[0]["fio"], "должность": position,
                       "месяц": m,
                       "договоры": ", ".join(sorted({p["contract"] for p in rr
                                                     if p["kind"] == "приказ" and p["amount"]})),
                       "приказ": _r(amount), "ставка": _r(staff_rate), "П2556": _r(p2556),
                       "предел": _r(cap), "запас": _r(cap - amount),
                       "состояние": FACT if m <= last else
                       "Выше предела" if amount > cap + EPS else "Не выше предела"})

    # ── 10–12. трудоёмкость ──────────────────────────────────────────
    # Допуск — тот же, что у решателя, аудита LABOR-001 и проверки в
    # rules.py: доля плана строки, но не меньше 0,01 чел.-мес. и 1 ₽. Прежде
    # отчёт прибавлял к нему 0,5 чел.-мес. и ещё 0,05: строка в 4 чел.-мес.,
    # закрытая на 10 %, шла «в допуске», хотя отметка раздела говорила 5 %.
    # Разделы 10 и 12 теперь режут одним порогом.
    tol = rules.labor_tolerance(inp.get("settings"))
    labor, who, gaps = [], [], []
    code_by_fio = {}
    for p in plan:
        code_by_fio.setdefault(p["fio"], p["emp"])
    # Кто и сколько закрыл по строке — из разбивки решателя (лист «Контроль
    # трудоёмкости»): ведущий инженер может закрывать строку инженеров, по
    # совпадению должности этого не увидеть. Без разбивки (старый результат)
    # — по совпадению должности, как раньше.
    attributed = {}
    for a in res.get("labor_people") or []:
        code = str(a["contract"]).split("—")[0].strip()
        key = (code, norm(a["row"]) if a["row"] else None)
        attributed.setdefault(key, {}).setdefault(a["fio"], {"position": a["position"]})[a["kind"]] = a["months"]

    def people_by_position(code, want):
        crows = [p for p in plan if p["contract"] == code
                 and (want is None or norm(p["position"]) == want)]
        out = []
        for (ecode,), er in sorted(_by(crows, "emp").items()):
            pm, pay = [0.0] * 12, [0.0] * 12
            for m in range(1, 13):
                mr = [p for p in er if p["month"] == m]
                r = open_rate(ecode, code, m, mr) if mr else None
                if r:
                    # Чел.-мес. даёт только открытая ставка, а приказ в
                    # трудоёмкость не входит (правила 17 и 18).
                    pm[m - 1] = r
                    pay[m - 1] = _sum(mr) - _sum(mr, {"приказ"})
            out.append((ecode, er[0]["fio"], er[0]["position"], pm, pay))
        return out

    def people_by_solver(attr):
        out = []
        for fio, a in attr.items():
            pm = [float(v or 0.0) for v in (a.get("pm") or [0.0] * 12)]
            pay = [float(v or 0.0) for v in (a.get("pay") or [0.0] * 12)]
            if not any(pm) and not any(pay):
                continue
            out.append((code_by_fio.get(fio, ""), fio, a["position"], pm, pay))
        return out

    for lp in inp["labor"]:
        if not lp["person_months"]:
            continue
        code, want = lp["contract"], norm(lp["position"]) if lp["position"] else None
        attr = attributed.get((code, want))
        found = people_by_solver(attr) if attr is not None else people_by_position(code, want)
        # Суммы — по точным значениям, округление только при показе: сумма
        # округлённых по месяцам ставок уводила строку за край допуска.
        pm_m, sum_m, heads_m, people = [0.0] * 12, [0.0] * 12, [0] * 12, []
        for ecode, fio, position, pm, pay in found:
            for i in range(12):
                pm_m[i] += pm[i]
                sum_m[i] += pay[i]
                if pm[i] >= PM_EPS:
                    heads_m[i] += 1
            if sum(pm) >= PM_EPS:
                people.append({"табельный": tab(ecode) if ecode else None, "фио": fio,
                               "должность": position,
                               "ставка": [_r(v) if v >= PM_EPS else None for v in pm],
                               "ставка год": _r(sum(pm))})
        people.sort(key=lambda x: str(x["фио"]))
        plan_pm, avg = lp["person_months"], lp["avg"] or 0.0
        plan_sum = plan_pm * avg
        fact_pm, fact_sum = sum(pm_m), sum(sum_m)
        fact_avg = fact_sum / fact_pm if fact_pm else None
        pm_allow, sum_allow = max(0.01, tol * plan_pm), max(EPS, tol * plan_sum)
        plan_months = lp.get("months") or None
        # Год строки с фактом (правило 26): сделанное в месяцах 1…F входит в
        # сумму числом, а коридор ослабляется ровно на то, что история уже
        # сделала сверх него или недобрала там, где добрать нельзя, — как у
        # решателя и аудита (rules.labor_band). Отклонение в этом ослабленном
        # коридоре — «сложилось в месяцах факта», не нарушение плана.
        pm_band = (plan_pm - pm_allow, plan_pm + pm_allow)
        sum_band = (plan_sum - sum_allow, plan_sum + sum_allow)
        if last:
            if plan_months:
                ahead_pm = sum(v for m, v in plan_months.items() if m > last)
                has_future, monthly_ahead = ahead_pm > 0, ahead_pm
            else:
                w = (ctr.get(code) or {}).get("window") or (13, 0)
                has_future, monthly_ahead = w[0] <= w[1] and w[1] > last, None
            pm_band = rules.labor_band(plan_pm, pm_allow, sum(pm_m[:last]), tol,
                                       monthly_ahead, has_future)
            sum_band = rules.labor_band(plan_sum, sum_allow, sum(sum_m[:last]), tol,
                                        None if monthly_ahead is None else avg * monthly_ahead,
                                        has_future)
        pm_short = fact_pm < pm_band[0] - PM_EPS
        pm_over = fact_pm > pm_band[1] + PM_EPS
        sum_ok = not plan_sum or sum_band[0] - 1e-6 <= fact_sum <= sum_band[1] + 1e-6
        # Средняя — по месяцам плана: средняя месяцев факта сложилась до расчёта.
        ahead_pm_done = fact_pm - sum(pm_m[:last])
        ahead_avg = ((fact_sum - sum(sum_m[:last])) / ahead_pm_done
                     if ahead_pm_done >= PM_EPS else None)
        avg_ok = not avg or ahead_avg is None or abs(ahead_avg - avg) <= tol * avg + 1e-6
        by_fact = bool(last) and (abs(fact_pm - plan_pm) > pm_allow + PM_EPS
                                  or (plan_sum and abs(fact_sum - plan_sum) > sum_allow + 1e-6))
        # План по месяцам: чел.-мес. вне месяцев этапа — своё нарушение
        # (правило 20), его не видно ни по сумме за год, ни по средней. Месяц
        # факта — история: его этап не проверяется.
        plan_m = ([_r(plan_months.get(m)) if plan_months.get(m) else None for m in range(1, 13)]
                  if plan_months else None)
        stage_m = [bool(plan_months) and i + 1 > last and pm_m[i] >= PM_EPS
                   and not plan_months.get(i + 1) for i in range(12)]
        out_of_stage = any(stage_m)
        if out_of_stage:
            status = "Трудоёмкость вне месяцев этапа"
        elif pm_short:
            status = "Трудоёмкость закрыта не полностью"
        elif pm_over:
            status = "Трудоёмкость превышена"
        elif not sum_ok:
            status = "Отклонение стоимости сверх допуска"
        elif not avg_ok:
            status = "Отклонение средней стоимости сверх допуска"
        elif by_fact:
            status = "Отклонение сложилось в месяцах факта"
        else:
            status = "В пределах допуска"
        row_name = lp["position"] or "Любая должность"
        d_pm, d_sum = fact_pm - plan_pm, fact_sum - plan_sum
        d_avg = fact_avg - avg if avg and fact_avg is not None else None
        labor.append({"договор": code, "строка": row_name,
                      "план чел-мес": _r(plan_pm), "факт чел-мес": _r(fact_pm),
                      "д чел-мес": _r(d_pm), "д чел-мес %": _pct(d_pm, plan_pm),
                      "план сумма": _r(plan_sum), "факт сумма": _r(fact_sum),
                      "д сумма": _r(d_sum), "д сумма %": _pct(d_sum, plan_sum),
                      "средняя план": _r(avg) if avg else None, "средняя факт": _r(fact_avg),
                      "д средней": _r(d_avg), "д средней %": _pct(d_avg, avg),
                      "людей предел": heads.get((code, want or "")),
                      "людей макс": max(heads_m) if heads_m else 0,
                      # Предел числа исполнителей судится по месяцам плана.
                      "людей макс план": max(heads_m[last:]) if heads_m[last:] else 0,
                      "допуск": tol, "вне допуска": {"чел-мес": pm_short or pm_over,
                                                      "сумма": not sum_ok,
                                                      "средняя": not avg_ok,
                                                      "этап": out_of_stage},
                      "план по месяцам": plan_m, "статус": status})
        # Раздел 11: по строке РКМ — план, закрытое исполнителями и отклонение
        # по месяцам, под ними исполнители. Одна величина — чел.-мес.; деньги
        # строки — в разделе 10, по людям — в разделе 6.
        dev_m = [None] * 12
        if plan_months:
            for i in range(last, 12):
                d = pm_m[i] - (plan_months.get(i + 1) or 0.0)
                dev_m[i] = _r(d) if abs(d) >= PM_EPS else None
        who.append({"договор": code, "название договора": (ctr.get(code) or {}).get("name") or "",
                    "строка": row_name, "план по месяцам": plan_m, "план": _r(plan_pm),
                    "закрыто": [_r(v) if v >= PM_EPS else None for v in pm_m],
                    "закрыто год": _r(fact_pm),
                    "отклонение": dev_m,
                    "отклонение год": _r(d_pm) if abs(d_pm) >= PM_EPS else None,
                    "вне допуска": pm_short or pm_over or out_of_stage,
                    "вне этапа": stage_m, "люди": people})
        if pm_short:
            # Месяцы выполнения работ — месяцы этапа РКМ, а у годовой строки —
            # месяцы, где у договора возможна открытая ставка: от начала до
            # срока выплаты оклада. Тогда «ставок в месяц» — действительно в
            # месяц работы, а не в месяц года.
            # С фактом — только месяцы после него: добрать недостающее можно
            # лишь в них, как у решателя (правило 26).
            c = ctr.get(code)
            if plan_months:
                n = sum(1 for m, v in plan_months.items() if v and m > last)
            elif c:
                n = max(0, min(c["to"], c["salary_to"]) - max(c["from"], last + 1) + 1)
            else:
                n = 0
            gap = plan_pm - fact_pm
            gaps.append({"договор": code, "строка": row_name, "по РКМ": _r(plan_pm),
                         "закрыто": _r(fact_pm), "не закрыто": _r(gap), "месяцев": n,
                         "ставок в месяц": _r(gap / n) if n else None})

    # Параметры расчёта, от которых зависит результат: экономист должен видеть
    # их рядом с планом, а не искать в файле входа. Название — формальное,
    # ключ «имя» — как в файле входа.
    st = inp.get("settings") or {}
    settings = []
    for key, title, unit in (("допуск трудоёмкости", "Допуск трудоёмкости", "%"),
                             ("макс договоров оклада в год",
                              "Наибольшее число договоров оклада в год", "шт"),
                             ("разрешить дефицит", "Недоплата заработной платы разрешена", "")):
        if st.get(key) is not None:
            settings.append({"имя": key, "название": title, "значение": st.get(key),
                             "единица": unit})
    deficits = [{"табельный": tab(d["emp"]), "фио": d["fio"], "месяц": d["month"],
                 "положено": _r(d["due"]), "выплачено": _r(d["paid"]), "дефицит": _r(d["gap"]),
                 "причина": d["why"]} for d in (res.get("deficits") or []) if d["gap"] > 0.5]
    return {
        "по подразделениям": _by_department(input_path, plan, inp, emp_extra),
        "год": year, "итоги": totals, "договоры": contracts,
        "настройки": settings, "дефицит": deficits,
        "освоение": {"договоры": usage, "итого": usage_total},
        "нарастающим": {"договоры": cumul, "итого": cumul_total},
        "виды": kinds, "регистр": register, "ставки": rates_tbl,
        "шр": _staff_detail(inp, res, extra, cats, base_of, year, open_rate, main_at),
        "помесячно": {"сотрудники": monthly, "итого": [_r(v) for v in org_m],
                      "год": _r(sum(org_m))},
        "бэп": bep, "п2556": limit_2556(plan, lim, norm, emp_extra, rate_of=rate_rows, last=last),
        "п2556 приказ": orders, "п4": p4,
        "трудоемкость": labor, "кто": who, "незакрыто": gaps, "допуск": tol,
        "факт по": last, "месяцы": SHORT,
    }
