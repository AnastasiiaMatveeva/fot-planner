# -*- coding: utf-8 -*-
"""Ограничения работы экономиста: соблюдены ли они в посчитанном плане.

Это не диагностика решателя. Это правила, по которым живет планирование ФОТ,
записанные словами экономиста, и ответ по каждому: соблюдено, нарушено или
не применялось в этом плане. Считается независимо — по входному файлу и по
готовому плану выплат, а не со слов решателя. Если правило нарушено, названы
строки, где именно.

Порядок разделов повторяет порядок, в котором экономист проверяет план:
сначала люди получили свое, потом деньги договоров сошлись, потом лимиты,
потом ставки и трудоемкость.
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
#: С точностью до рубля: копейки в плане ФОТ ничего не решают.
EPS = 1.0
#: Сколько нарушений показывать: список нужен, чтобы пойти и посмотреть, а не
#: чтобы прокручивать его целиком.
SHOW = 12


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


def _kind_of(raw):
    """Вид выплаты по названию из плана."""
    s = str(raw or "").strip().lower()
    if s.startswith("оклад"):
        return "оклад"
    for k in ("120", "122", "124", "152"):
        if s.startswith(k):
            return k
    if "приказ" in s:
        return "приказ"
    return None


def _rows(ws):
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
            "labor": [], "secret": [], "settings": {}}
    for row in _rows(wb["сотрудники"]):
        code = row.get("код строки")
        if not code:
            continue
        data["employees"].append({
            "code": str(code), "fio": row.get("фио") or str(code),
            "position": str(row.get("должность") or ""),
            "rate": _num(row.get("ставка")) or 1.0,
            "salary": _num(row.get("зарплата")) or 0.0,
            "from": _month(row.get("дата начала")) or 1,
            "to": _month(row.get("дата окончания")) or 12,
        })
    for row in _rows(wb["договоры"]):
        code = row.get("код")
        if not code:
            continue
        data["contracts"][str(code)] = {
            "code": str(code), "name": row.get("название") or "",
            "goz": _yes(row.get("ГОЗ")), "fot": _num(row.get("фот")) or 0.0,
            "from": _month(row.get("дата начала")) or 1,
            "to": _month(row.get("дата окончания")) or 12,
            "kinds": {k: _yes(row.get(col)) for k, col in KINDS.items()},
        }
    if "лимиты_по_должностям" in wb.sheetnames:
        for row in _rows(wb["лимиты_по_должностям"]):
            pos = row.get("должность")
            if not pos:
                continue
            data["limits"][normalize_position(str(pos))] = {
                "оклад": _num(row.get("оклад")), "П2556": _num(row.get("П2556")),
                "П4": _num(row.get("П4")), "БЭП": _num(row.get("БЭП")),
            }
    ws = wb["фот_по_месяцам"]
    for r in range(2, ws.max_row + 1):
        code = ws.cell(r, 1).value
        if code:
            data["inflow"][str(code)] = [_num(ws.cell(r, c).value) or 0.0
                                         for c in range(2, 14)]
    if "трудоемкость_по_договорам" in wb.sheetnames:
        for row in _rows(wb["трудоемкость_по_договорам"]):
            if row.get("договор"):
                data["labor"].append({
                    "contract": str(row["договор"]),
                    "position": str(row.get("должность") or ""),
                    "person_months": _num(row.get("трудоемкость")),
                    "avg": _num(row.get("средняя стоимость выполнения работ в месяц")),
                })
    if "120_надбавка" in wb.sheetnames:
        for row in _rows(wb["120_надбавка"]):
            if row.get("сотрудник"):
                data["secret"].append(str(row["сотрудник"]))
    if "настройки" in wb.sheetnames:
        for row in _rows(wb["настройки"]):
            data["settings"] = {k: v for k, v in row.items()}
            break
    data["norm"] = normalize_position
    return data


def _read_result(path):
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    out = {"plan": [], "cash": {}, "labor_control": []}
    for row in _rows(wb["План выплат"]):
        code = row.get("код строки")
        month = RU_MONTH.get(str(row.get("месяц") or "").strip())
        amount = _num(row.get("сумма"))
        if not code or not month or not amount:
            continue
        out["plan"].append({
            "emp": str(code), "fio": row.get("ФИО") or str(code),
            "position": str(row.get("должность") or ""),
            "month": month, "contract": str(row.get("договор") or ""),
            "kind": _kind_of(row.get("вид выплаты")), "amount": amount,
        })
    if "ФОТ по договорам" in wb.sheetnames:
        for row in _rows(wb["ФОТ по договорам"]):
            metric = str(row.get("показатель") or "")
            code = row.get("договор")
            if not code or metric not in ("Поступление", "Выплаты", "Остаток на конец"):
                continue
            out["cash"].setdefault(str(code), {})[metric] = [
                _num(row.get(m)) or 0.0 for m in
                ["Янв", "Фев", "Мар", "Апр", "Май", "Июн",
                 "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]]
    if "Контроль трудоёмкости" in wb.sheetnames:
        # Лист устроен блоками: строка «ДОГОВОР: …», шапка «Строка / группа»,
        # затем строки трудоемкости, затем помесячная развертка под шапкой
        # «Показатель». Правила — только те строки, что идут сразу под первой
        # шапкой: помесячная развертка это те же числа в разрезе месяцев.
        ws = wb["Контроль трудоёмкости"]
        current, in_rules = None, False
        for r in range(1, ws.max_row + 1):
            first = ws.cell(r, 1).value
            text = str(first or "").strip()
            if text.startswith("ДОГОВОР:"):
                current = text.replace("ДОГОВОР:", "").strip()
                in_rules = False
                continue
            if text == "Строка / группа":
                in_rules = True
                continue
            if text == "Показатель" or not text:
                in_rules = False
                continue
            if not in_rules:
                continue
            marks = [str(ws.cell(r, c).value or "").strip() for c in (11, 12, 13)]
            out["labor_control"].append({
                "contract": current or "", "row": text,
                "plan_pm": _num(ws.cell(r, 2).value), "fact_pm": _num(ws.cell(r, 3).value),
                "plan_sum": _num(ws.cell(r, 5).value), "fact_sum": _num(ws.cell(r, 6).value),
                "marks": marks, "status": str(ws.cell(r, 14).value or ""),
                "note": str(ws.cell(r, 15).value or ""),
            })
    return out


def _by(plan, *keys):
    out = {}
    for p in plan:
        out.setdefault(tuple(p[k] for k in keys), []).append(p)
    return out


def _sum(rows, kinds=None):
    return sum(p["amount"] for p in rows if kinds is None or p["kind"] in kinds)


def _rule(name, meaning, where, state="соблюдено", fact="", broken=None):
    return {"правило": name, "смысл": meaning, "где": where, "состояние": state,
            "факт": fact, "нарушения": (broken or [])[:SHOW],
            "всего нарушений": len(broken or [])}


def _mark(out, section):
    """Отнести к разделу все правила, у которых он еще не проставлен."""
    for r in out:
        r.setdefault("раздел", section)


def _mo(v):
    return "{:,.0f}".format(round(v)).replace(",", " ")


def check(input_path, result_path):
    """Все правила и их состояние в этом плане."""
    inp = _read_input(input_path)
    res = _read_result(result_path)
    plan, emps = res["plan"], {e["code"]: e for e in inp["employees"]}
    ctr = inp["contracts"]
    norm = inp["norm"]
    out = []

    # ── зарплата и виды выплат ───────────────────────────────────────
    broken = []
    by_em = _by(plan, "emp", "month")
    for e in inp["employees"]:
        for m in range(e["from"], e["to"] + 1):
            paid = _sum(by_em.get((e["code"], m), []))
            if abs(paid - e["salary"]) > EPS:
                broken.append("%s, %s: выплачено %s ₽ вместо %s ₽"
                              % (e["fio"], SHORT[m - 1], _mo(paid), _mo(e["salary"])))
    out.append(_rule(
        "Человек получает всю зарплату каждый месяц",
        "Месячная сумма из штатного расписания должна быть закрыта целиком: "
        "окладом и надбавками. Недоплата возможна только если дефицит разрешен "
        "в настройках.",
        "План выплат", "нарушено" if broken else "соблюдено",
        "проверено строк-месяцев: %d" % sum(e["to"] - e["from"] + 1 for e in inp["employees"]),
        broken))

    broken, checked = [], 0
    for (code, m, c), rows in _by(plan, "emp", "month", "contract").items():
        oklad = _sum(rows, {"оклад"})
        if not oklad:
            continue
        base = (inp["limits"].get(norm(rows[0]["position"])) or {}).get("оклад")
        if not base:
            continue
        checked += 1
        rate = oklad / base
        # Шаг ставки — четверть. Оклад на договоре обязан быть окладом
        # должности, умноженным на открытую ставку, а не произвольной суммой.
        if abs(rate * 4 - round(rate * 4)) > 0.02:
            broken.append("%s, %s, %s: оклад %s ₽ — это %s ставки, а ставка "
                          "кратна четверти"
                          % (rows[0]["fio"], SHORT[m - 1], c, _mo(oklad), round(rate, 3)))
    out.append(_rule(
        "Оклад идет по ставке, а не произвольной суммой",
        "Оклад на договоре — это оклад должности, умноженный на открытую там "
        "ставку. Ставка кратна четверти, уменьшать оклад «для удобства» нельзя: "
        "остаток зарплаты добирается надбавками.",
        "Реестр → Справочник должностей",
        "нарушено" if broken else ("соблюдено" if checked else "не применялось"),
        "проверено назначений: %d" % checked if checked
        else "в справочнике нет окладов по этим должностям", broken))

    broken = []
    for (code, m), rows in by_em.items():
        salary_ctr = {p["contract"] for p in rows if p["kind"] == "оклад"}
        for p in rows:
            if p["kind"] == "122" and salary_ctr and p["contract"] not in salary_ctr:
                broken.append("%s, %s: 122 на %s, оклад на %s"
                              % (p["fio"], SHORT[m - 1], p["contract"],
                                 ", ".join(sorted(salary_ctr))))
    out.append(_rule(
        "Надбавка 122 — с того же договора, что и оклад",
        "За качество платят там же, где сидит оклад этой ставки.",
        "План выплат", "нарушено" if broken else "соблюдено", "", broken))

    broken = []
    for p in plan:
        c = ctr.get(p["contract"])
        if c and p["kind"] and not c["kinds"].get(p["kind"], False):
            broken.append("%s, %s: %s с договора %s, где этот вид запрещен"
                          % (p["fio"], SHORT[p["month"] - 1], p["kind"], p["contract"]))
    out.append(_rule(
        "Платим только теми видами, которые разрешены договором",
        "Договор перечисляет, что с него можно платить: оклад, 120, 122, 124, "
        "152, стимулирующую приказом. Остальное с него платить нельзя.",
        "Реестр → Договоры", "нарушено" if broken else "соблюдено", "", broken))

    broken = []
    for (code, m), rows in by_em.items():
        kinds = {p["kind"] for p in rows}
        if "152" in kinds and (kinds - {"оклад", "152"}):
            broken.append("%s, %s: вместе со 152 назначены %s"
                          % (emps.get(code, {}).get("fio", code), SHORT[m - 1],
                             ", ".join(sorted(kinds - {"оклад", "152"}))))
    used152 = any(p["kind"] == "152" for p in plan)
    out.append(_rule(
        "Режим «оклад плюс 152»",
        "Если в месяце строке сотрудника назначена 152 за дополнительную "
        "работу, других надбавок и приказа в этом месяце у нее нет.",
        "План выплат",
        ("нарушено" if broken else "соблюдено") if used152 else "не применялось",
        "" if used152 else "152 в этом плане не используется", broken))
    _mark(out, "Зарплата и выплаты")

    # ── договоры и деньги ────────────────────────────────────────────
    broken = []
    for p in plan:
        c = ctr.get(p["contract"])
        if c and not (c["from"] <= p["month"] <= c["to"]):
            broken.append("%s: выплата в %s, договор действует %s—%s"
                          % (p["contract"], SHORT[p["month"] - 1],
                             SHORT[c["from"] - 1], SHORT[c["to"] - 1]))
    out.append(_rule(
        "Договор платит только в свои сроки",
        "Ни рубля до начала и после окончания договора.",
        "Реестр → Договоры", "нарушено" if broken else "соблюдено", "",
        sorted(set(broken))))

    broken = []
    for code, c in ctr.items():
        got = inp["inflow"].get(code) or [0.0] * 12
        paid = [0.0] * 12
        for p in plan:
            if p["contract"] == code:
                paid[p["month"] - 1] += p["amount"]
        balance = 0.0
        for m in range(12):
            balance += got[m] - paid[m]
            if balance < -EPS:
                broken.append("%s, %s: потрачено на %s ₽ больше, чем поступило"
                              % (code, SHORT[m], _mo(-balance)))
                break
    out.append(_rule(
        "Деньги нельзя потратить раньше, чем они пришли",
        "Касса договора считается помесячно, остаток переносится только "
        "вперед. Минуса на договоре быть не может.",
        "Освоение", "нарушено" if broken else "соблюдено", "", broken))

    broken, unspent = [], []
    for code, c in ctr.items():
        paid = sum(p["amount"] for p in plan if p["contract"] == code)
        if c["fot"] and paid > c["fot"] + EPS:
            broken.append("%s: выплачено %s ₽ при ФОТ %s ₽"
                          % (code, _mo(paid), _mo(c["fot"])))
        elif c["fot"] and c["fot"] - paid > EPS:
            unspent.append("%s: не освоено %s ₽ из %s ₽"
                           % (code, _mo(c["fot"] - paid), _mo(c["fot"])))
    out.append(_rule(
        "Выплаты не превышают ФОТ договора",
        "Фонд оплаты труда договора — верхняя граница всех выплат с него за год.",
        "Реестр → Договоры", "нарушено" if broken else "соблюдено", "", broken))
    out.append(_rule(
        "ФОТ договоров освоен",
        "Неосвоенный остаток расчет не ломает, но это деньги, которые остались "
        "на договоре к концу года.",
        "Освоение", "внимание" if unspent else "соблюдено",
        "не освоено договоров: %d" % len(unspent) if unspent else "освоено полностью",
        unspent))
    _mark(out, "Договоры и деньги")

    # ── лимиты ───────────────────────────────────────────────────────
    def rate_of(rows):
        """Ставка на договоре: оклад, деленный на оклад должности по справочнику."""
        oklad = _sum(rows, {"оклад"})
        lim = inp["limits"].get(norm(rows[0]["position"])) if rows else None
        base = (lim or {}).get("оклад")
        return (oklad / base) if base else None

    broken, checked = [], 0
    for (code, m, c), rows in _by(plan, "emp", "month", "contract").items():
        lim = inp["limits"].get(norm(rows[0]["position"])) or {}
        p2556 = lim.get("П2556")
        if not p2556:
            continue
        rate = rate_of(rows)
        if rate is None:
            continue
        checked += 1
        staff = _sum(rows, {"оклад", "122"})
        if staff > p2556 * rate + EPS:
            broken.append("%s, %s, %s: оклад и 122 дают %s ₽ при пределе %s ₽ на "
                          "ставку %s" % (rows[0]["fio"], SHORT[m - 1], c, _mo(staff),
                                         _mo(p2556 * rate), round(rate, 2)))
    out.append(_rule(
        "П2556: оклад и 122 не выше предела по должности",
        "Приказ 2556 задает максимум штатной части на полную ставку по каждой "
        "должности. На доле ставки предел уменьшается пропорционально.",
        "Реестр → Справочник должностей",
        "нарушено" if broken else ("соблюдено" if checked else "не применялось"),
        "проверено назначений: %d" % checked if checked
        else "в справочнике нет П2556 по этим должностям", broken))

    broken, goz_months = [], 0
    for (c, m), rows in _by(plan, "contract", "month").items():
        contract = ctr.get(c)
        if not contract or not contract["goz"]:
            continue
        beps, staff, rates = [], 0.0, 0.0
        for (code,), er in _by(rows, "emp").items():
            lim = inp["limits"].get(norm(er[0]["position"])) or {}
            if lim.get("БЭП"):
                beps.append(lim["БЭП"])
            r = rate_of(er)
            if r:
                rates += r
            staff += _sum(er, {"оклад", "122"})
        if not beps or not rates:
            continue
        goz_months += 1
        limit = min(beps) * rates
        if staff > limit + EPS:
            broken.append("%s, %s: средняя штатная часть %s ₽ на ставку при БЭП %s ₽"
                          % (c, SHORT[m - 1], _mo(staff / rates), _mo(min(beps))))
    out.append(_rule(
        "БЭП: средняя зарплата по ГОЗ-договору в пределах базовой",
        "На гособоронзаказе ограничена не отдельная зарплата, а средняя по "
        "договору за месяц: сумма оклада и 122 делится на сумму ставок.",
        "Реестр → Справочник должностей",
        "нарушено" if broken else ("соблюдено" if goz_months else "не применялось"),
        "проверено договоро-месяцев: %d" % goz_months if goz_months
        else "ГОЗ-договоров в плане нет", broken))

    broken, p4_cases = [], 0
    for (code, m), rows in by_em.items():
        if not any(p["kind"] == "124" for p in rows):
            continue
        lim = inp["limits"].get(norm(rows[0]["position"])) or {}
        if not lim.get("П4"):
            continue
        rate = sum(filter(None, (rate_of(r) for (_, ), r in _by(rows, "contract").items())))
        if not rate:
            continue
        p4_cases += 1
        staff = _sum(rows, {"оклад", "122", "124"})
        if staff > lim["П4"] * rate + EPS:
            broken.append("%s, %s: оклад, 122 и 124 дают %s ₽ при пределе П4 %s ₽"
                          % (rows[0]["fio"], SHORT[m - 1], _mo(staff),
                             _mo(lim["П4"] * rate)))
    out.append(_rule(
        "П4 при надбавке за интенсивность",
        "Если сотруднику назначена 124, включается предел П4: он смотрит "
        "оклад, 122 и 124 этой строки за месяц целиком, по всем договорам.",
        "Реестр → Справочник должностей",
        "нарушено" if broken else ("соблюдено" if p4_cases else "не применялось"),
        "проверено случаев: %d" % p4_cases if p4_cases
        else "надбавка 124 в этом плане не назначалась", broken))
    _mark(out, "Лимиты по должностям")

    # ── ставки ───────────────────────────────────────────────────────
    broken, extra = [], 0
    for (code, m), rows in by_em.items():
        total = 0.0
        for (c,), cr in _by(rows, "contract").items():
            r = rate_of(cr)
            if r:
                total += r
        if not total:
            continue
        staff_rate = emps.get(code, {}).get("rate") or 1.0
        if total > staff_rate + 0.01:
            extra += 1
        if total > 1.5 + 0.01:
            broken.append("%s, %s: суммарная ставка %s"
                          % (rows[0]["fio"], SHORT[m - 1], round(total, 2)))
        elif total - staff_rate > 0.5 + 0.01:
            broken.append("%s, %s: по совместительству открыто %s сверх штатной"
                          % (rows[0]["fio"], SHORT[m - 1], round(total - staff_rate, 2)))
    out.append(_rule(
        "Ставки: всего не больше 1,5, по совместительству — не больше 0,5",
        "Штатная ставка сохраняется, сверх нее сервис может открыть "
        "совместительство. Обычному сотруднику — до полутора ставок в сумме.",
        "План выплат", "нарушено" if broken else "соблюдено",
        "открыто совместительств: %d" % extra if extra
        else "совместительство не открывалось", broken))
    _mark(out, "Ставки и совместительство")

    # ── трудоемкость ─────────────────────────────────────────────────
    broken = []
    for row in res["labor_control"]:
        bad = [m for m in row["marks"] if m and m.lower() not in ("ок", "выполнено")]
        if not bad:
            continue
        broken.append("%s / %s: %s. План %s чел.-мес. на %s ₽, вышло %s чел.-мес. "
                      "на %s ₽"
                      % (row["contract"], row["row"], row["status"] or "отклонение",
                         row["plan_pm"], _mo(row["plan_sum"] or 0),
                         row["fact_pm"], _mo(row["fact_sum"] or 0)))
    out.append(_rule(
        "Трудоемкость договоров закрыта",
        "По каждой строке расчетно-калькуляционных материалов должны сойтись "
        "и человеко-месяцы, и сумма: люди на договоре и деньги, которые им "
        "начислены.",
        "Реестр → Трудоемкость",
        "нарушено" if broken else ("соблюдено" if res["labor_control"] else "не применялось"),
        "строк трудоемкости: %d" % len(res["labor_control"]) if res["labor_control"]
        else "трудоемкость по договорам не задана", broken))

    broken = []
    for lp in inp["labor"]:
        if not lp["position"]:
            continue
        who = [p for p in plan if p["contract"] == lp["contract"]
               and norm(p["position"]) == norm(lp["position"])]
        if not who:
            broken.append("%s / %s: на договоре нет ни одной выплаты по этой должности"
                          % (lp["contract"], lp["position"]))
    out.append(_rule(
        "Работу закрывает подходящая должность",
        "Строку трудоемкости может закрыть сотрудник с той же должностью или "
        "тот, кому эта должность разрешена правилами замещения.",
        "Реестр → Правила замещения",
        "нарушено" if broken else ("соблюдено" if inp["labor"] else "не применялось"),
        "" if inp["labor"] else "трудоемкость по договорам не задана", broken))
    _mark(out, "Трудоемкость договоров")

    # Дефицит разрешен настройками — об этом надо сказать отдельно: тогда
    # первое правило проверяет не то, что все получили зарплату, а то, что
    # сервису разрешили недоплатить.
    if str(inp["settings"].get("разрешить дефицит") or "").strip().lower() in ("да", "true", "1"):
        soft = _rule(
            "Дефицит выплат разрешен настройками",
            "В настройках расчета разрешено оставить часть зарплаты "
            "невыплаченной. План при этом сходится, но люди получают меньше.",
            "Настройки расчета", "внимание",
            "правило «человек получает всю зарплату» смягчено")
        soft["раздел"] = "Настройки расчета"
        out.insert(0, soft)
    return out
