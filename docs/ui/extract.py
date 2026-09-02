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
    passport = {"employees": [], "contracts": [], "inflow": {},
                "settings": {}, "source": path.split("\\")[-1].split("/")[-1]}
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
    if t in ("запуск", "лимиты_по_должностям", "120_надбавка",
             "трудоемкость_по_договорам", "фиксация_фот_по_месяцам",
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
        p["employees"].append({
            "code": str(code),
            "fio": str(ws.cell(r, hdr["фио"]).value or ""),
            "pos": str(ws.cell(r, hdr["должность"]).value or ""),
            "rate": _num(ws.cell(r, hdr["ставка"]).value) or 1.0,
            "sal": _num(ws.cell(r, hdr["зарплата"]).value) or 0.0,
            "from": str(ws.cell(r, hdr.get("дата начала", 9)).value or "")[:10],
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
        p["contracts"].append({
            "code": str(code),
            "name": str(ws.cell(r, hdr.get("название", 2)).value or ""),
            "goz": str(ws.cell(r, hdr.get("гоз", 6)).value or "нет"),
            "fot": _num(ws.cell(r, hdr["фот"]).value) or 0.0,
            "kinds": kinds,
        })
        n += 1
    log.append(f"Лист «договоры»: шаблон fot-planner, извлечено договоров: {n}")


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


def _read_fot_detail(ws, p, log, q):
    """«Расшифровка ФОТ»: строки этапов, проверка гр.6×гр.7=гр.8."""
    rows, bad, empty = 0, 0, 0
    for r in range(1, ws.max_row + 1):
        if _is_column_numbering(ws, r):
            continue
        pos = ws.cell(r, 4).value
        cm = _num(ws.cell(r, 7).value)
        sal = _num(ws.cell(r, 8).value)
        tot = _num(ws.cell(r, 9).value)
        if not pos or cm is None or sal is None or tot is None:
            continue
        if cm <= 0 or sal <= 0:
            empty += 1
            continue
        rows += 1
        calc = cm * sal
        if abs(calc - tot) > 1:
            bad += 1
            q.append({
                "field": f"Строка «{pos}» (строка листа {r})",
                "raw": f"{_fmt(cm)} чел.-мес по {_fmt(sal)} ₽, итог {_fmt(tot)} ₽",
                "why": (f"Соотношение формы: {_fmt(cm)} × {_fmt(sal)} = {_fmt(calc)} ₽, "
                        f"а в итоге строки {_fmt(tot)} ₽."),
                "options": [f"Принять расчётное {_fmt(calc)} ₽",
                            f"Оставить из документа {_fmt(tot)} ₽"],
            })
    if rows == 0 and empty:
        log.append(f"Лист «{ws.title}»: форма-образец, {empty} строк без значений — "
                   "заполненных данных нет")
    else:
        log.append(f"Лист «{ws.title}»: строк трудоёмкости: {rows}, "
                   f"несоответствий формуле: {bad}")


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
    """Форма 9д: гр.6 (чел.-мес) × гр.7 (стоимость) = гр.8 (ОЗП)."""
    rows, bad, empty = 0, 0, 0
    for r in range(1, ws.max_row + 1):
        if _is_column_numbering(ws, r):
            continue
        grp = ws.cell(r, 5).value
        cm = _num(ws.cell(r, 6).value)
        cost = _num(ws.cell(r, 7).value)
        ozp = _num(ws.cell(r, 8).value)
        if not grp or cm is None or cost is None or ozp is None:
            continue
        if cm <= 0 or cost <= 0:
            empty += 1
            continue
        rows += 1
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


def passport_to_input(passport, template_path, out_path):
    """Собрать вход fot-planner из паспорта с точным раскроем фондов."""
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
    fots = {c["code"]: c["fot"] for c in passport["contracts"]}
    for r in range(2, ws.max_row + 1):
        code = str(ws.cell(r, 1).value or "")
        if code in fots:
            ws.cell(r, fot_c).value = fots[code]

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

    wb.save(out_path)
    return out_path


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
