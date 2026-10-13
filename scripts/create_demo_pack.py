# -*- coding: utf-8 -*-
"""Демо-комплект для загрузки руками: документы экономиста, разложенные по
папкам, — как их приносят в отдел (пользователь 30.09.2026: «разложи по папкам
документы, чтобы в реальном времени я подгрузила все сама»).

    data/demo_pack/
      1 Штатное расписание/   штатное расписание (листы «сотрудники» и «120_надбавка»)
      2 Договоры/<шифр>/      карточка договора и документы, из которых берётся
                              его трудоёмкость, — у каждого договора своя форма
      3 Заработная плата/     зарплата по месяцам; у части людей с сентября рост
      4 Факт начислений/      факт января формой 6.3 «ШР на дату, детализация»

Справочник должностей с пределами П2556, П4 и БЭП и правила замещения уже
в реестре организации: их загружать не нужно.

Трудоёмкость договоров — разными таблицами (пользователь: «чтоб показать, что
поддерживаются разные виды»); из образцов «РКМ по ГОЗ» и «Структура цены»
взято только то, что выделено в них жёлтым:

* C_GOZ-26 — РКМ ГОЗ: форма 9 (9д) — чел.-мес. по должностям и стоимость
  чел.-мес., форма 22 (22д) — нормативы, форма 1 — срок цены;
* C_GRANT-26 — структура цены расширенная: «Расшифровка ФОТ» по этапам с
  датами, человек × месяцев на человека;
* C_SENS-26 — структура цены простая: «Расшифровка ФОТ» в человеко-часах с
  нормативом часов года из шапки формы;
* C_MON-26 — план работ по месяцам: своя загрузка на каждый месяц;
* C_BASE-26, C_COM-26, C_PRI-26 — без трудоёмкости: только карточка.

Суммы подобраны так же, как в data/demo (scripts/create_demo_docs.py), —
каждое правило организации видно на своём договоре. Факт января — месяц
посчитанного плана этого же комплекта: он согласован с остальными
документами, и план с ним сходится.

    .venv/Scripts/python.exe scripts/create_demo_pack.py
    .venv/Scripts/python.exe scripts/create_demo_pack.py --fact ""            # без факта

Файл --fact — выплаты января по людям и договорам ({код: [{договор, вид,
сумма, ставка}]}); его пишет scripts/demo_pack.py после расчёта комплекта без
факта. Без него папка факта не пишется.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys

import openpyxl
from openpyxl.cell.cell import MergedCell
from openpyxl.styles import Alignment, Font

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import create_demo_docs as base  # noqa: E402

OUT = os.path.join(ROOT, "data", "demo_pack")
#: Выплаты января из расчёта комплекта без факта (scripts/demo_pack.py load
#: --no-fact --dump-january): из них — факт января формой 6.3.
FACT_JSON = os.path.join(ROOT, "data", "demo", "jan_2026_payments.json")
FORMS = os.path.join(ROOT, "data", "demo", "forms")
YEAR = 2026
#: Среднемесячные рабочие часы 2026 года — норматив из шапки простой формы
#: структуры цены («Среднемесячное количество рабочих часов в 2026 г.- 164,25»).
HOURS = 164.25

STAFF_DIR = "1 Штатное расписание"
CONTRACTS_DIR = "2 Договоры"
SALARY_DIR = "3 Заработная плата"
FACT_DIR = "4 Факт начислений"

# ── люди ───────────────────────────────────────────────────────
#: Десять человек демо-набора и двое под новые договоры. Лебедев нанят под
#: хоздоговор «Сенсор-26» на его срок; Соколова — на госзадании с загрузкой
#: по месяцам, остаток ставки — на внебюджете.
NEW_EMPLOYEES = [
    ("E011", "Лебедев Лев Львович", "Ведущий инженер", "Отдел 12", 1.0,
     "основное", "основной", 120000, "C_SENS-26", None, "01.02.2026", "30.11.2026"),
    ("E012", "Соколова Мария Андреевна", "Научный сотрудник", "Лаборатория 3", 1.0,
     "основное", "основной", 95000, "C_MON-26, C_BASE-26", None, "01.01.2026", "31.12.2026"),
]


def employees():
    rows = [e + ("01.01.2026", "31.12.2026") for e in base.EMPLOYEES]
    return rows + NEW_EMPLOYEES


#: Рост зарплаты с сентября (пользователь: «чтоб зп была не постоянная, а
#: например 1 раз в году поменялась — увеличилась»). Люди выбраны те, у кого
#: рост не ломает замысел набора: пример лестницы надбавок (Сидорова),
#: предел П4 ровно (Орлов), студентка, приоритет и грант остаются как были.
RAISE_FROM = 9
RAISES = {"E001": 155000, "E002": 123000, "E008": 180000, "E010": 115000}


def salary_months(e):
    code, start, end = e[0], e[10], e[11]
    first, last = int(start[3:5]), int(end[3:5])
    out = []
    for m in range(1, 13):
        if m < first or m > last:
            out.append(None)
        elif code in RAISES and m >= RAISE_FROM:
            out.append(RAISES[code])
        else:
            out.append(e[7])
    return out


# ── договоры ───────────────────────────────────────────────────
NEW_CONTRACTS = [
    ("C_SENS-26", "Хоздоговор НИОКР «Сенсор-26»", "21/26", "коммерческий", "20576У17460",
     "нет", "01.02.2026", "30.11.2026", 1250000,
     ("да", "да", "да", "нет", "нет", "нет"), "нет", None),
    ("C_MON-26", "Госзадание «Материал-26»", "075-03-2026-112", "грант", "20576У17470",
     "нет", "01.01.2026", "31.12.2026", 1000000,
     ("да", "да", "да", "нет", "нет", "нет"), "нет", None),
]
#: Папка договора — шифр и короткое имя темы.
FOLDERS = {"C_BASE-26": "C_BASE-26 Стенд-26", "C_COM-26": "C_COM-26 Привод-26",
           "C_GOZ-26": "C_GOZ-26 Радар-26", "C_GRANT-26": "C_GRANT-26 Грант РНФ",
           "C_PRI-26": "C_PRI-26 Цифровой двойник", "C_SENS-26": "C_SENS-26 Сенсор-26",
           "C_MON-26": "C_MON-26 Материал-26"}


def contracts():
    return list(base.CONTRACTS) + NEW_CONTRACTS



#: «Сенсор-26»: два этапа, один ведущий инженер, трудоёмкость в часах.
SENS_STAGES = [("Этап 1", "01.02.2026", "31.05.2026", 4), ("Этап 2", "01.06.2026", "30.11.2026", 6)]
SENS_POSITION, SENS_SALARY = "Ведущий инженер", 120000
#: «Материал-26»: загрузка научного сотрудника по месяцам.
MON_POSITION, MON_COST = "Научный сотрудник", 95000
MON_PLAN = [0.5, 0.5, 0.5] + [1.0] * 9


def _bold(ws, row, cols):
    for c in range(1, cols + 1):
        ws.cell(row, c).font = Font(bold=True)


def staff():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "сотрудники"
    ws.append(["код строки", "фио", "должность", "подразделение", "ставка", "тип занятости",
               "категория занятости", "дата начала", "дата окончания",
               "разрешенные договоры", "запрещенные договоры"])
    _bold(ws, 1, 11)
    for e in employees():
        # Зарплаты в штатном нет: она по месяцам в своём документе.
        ws.append([e[0], e[1], e[2], e[3], e[4], e[5], e[6], e[10], e[11], e[8], e[9]])
    for col, w in zip("ABCDEFGHIJK", (10, 34, 28, 16, 8, 18, 20, 12, 14, 30, 22)):
        ws.column_dimensions[col].width = w
    ws2 = wb.create_sheet("120_надбавка")
    ws2.append(["сотрудник", "договор секретности", "ставка 120"])
    _bold(ws2, 1, 3)
    for row in base.SECRET:
        ws2.append(list(row))
    for col, w in zip("ABC", (14, 22, 12)):
        ws2.column_dimensions[col].width = w
    return wb


def contract_card(c):
    """Карточка одного договора: реквизиты, разрешённые выплаты, поступления."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "договоры"
    ws.append(["код", "название", "номер", "тип договора", "счет", "ГОЗ", "дата начала",
               "дата окончания", "фот", "оклад разрешен", "120 разрешена", "122 разрешена",
               "124 разрешена", "152 разрешена", "стимулирующая приказом разрешена",
               "Приоритет", "основное место разрешено", "совместительство разрешено",
               "конечная дата выплат оклада", "конечная дата выплат надбавок", "подразделение"])
    _bold(ws, 1, 21)
    ws.append([c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7], c[8], *c[9],
               c[10], "да", "да", c[11] or c[7], c[11] or c[7], ""])
    for col, w in zip("ABCDEFGHI", (12, 44, 16, 14, 14, 6, 12, 14, 12)):
        ws.column_dimensions[col].width = w
    # ФОТ — одной суммой за год: помесячных поступлений в карточке нет
    # (пользователь 30.09.2026: «фот по месяцам же тоже не нужен, только ФОТ
    # в целом»).
    ws3 = wb.create_sheet("настройки")
    ws3.append(["год"])
    ws3.append([YEAR])
    return wb


def _keep_only(wb, names):
    """Оставить в образце только листы с жёлтыми ячейками. Формула, которая
    ссылается на удалённый лист, стала бы #ССЫЛКА! — её значение уже
    вписано или ячейка очищается."""
    for ws in list(wb.worksheets):
        if ws.title not in names:
            wb.remove(ws)
    kept = set(names)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if isinstance(v, str) and v.startswith("=") and "!" in v:
                    refs = re.findall(r"'([^']+)'!|([A-Za-zА-Яа-яё_]+)!", v)
                    if any((a or b) not in kept for a, b in refs):
                        cell.value = None


def rkm_goz():
    """РКМ ГОЗ «Радар-26»: формы 1, 9 (9д) и 22 (22д) — выделенное жёлтым."""
    wb = base.rkm()
    ws = wb["9 (9д) ОЗП НИР(ОКР)"]
    # В форме 9 — только 2026 год: строки 2027 года пусты.
    for r in range(18, 23):
        for c in range(2, 9):
            if not isinstance(ws.cell(r, c), MergedCell):
                ws.cell(r, c).value = None
    total_cm = sum(r[2] for r in base.FORM9)
    total_ozp = sum(r[2] * r[3] for r in base.FORM9)
    ws["D23"], ws["F23"], ws["G23"], ws["H23"] = (sum(r[0] for r in base.FORM9), total_cm,
                                                  round(total_ozp / total_cm, 2), total_ozp)
    ws1 = wb["Прил. № 5 Ф1(1д) прот"]
    ws1["B17"] = "НИР «Радар-26», шифр C_GOZ-26"
    ws1["H17"] = "01.03.2026"
    ws1["I17"] = "31.12.2026"
    ws1["J17"] = "-"
    ws22 = wb["22 (22д) свед. о норм_26"]
    ws22["D17"] = 8.6866
    ws22["D18"] = 0
    ws22["D19"] = 30.2
    ws22["D20"], ws22["D21"] = 30, 0.2
    _keep_only(wb, ["Прил. № 5 Ф1(1д) прот", "9 (9д) ОЗП НИР(ОКР)", "22 (22д) свед. о норм_26"])
    return wb


def price_grant():
    """Структура цены расширенная, грант РНФ: этапы с датами — выделенное жёлтым:
    строки оплаты труда структуры цены и «Расшифровка ФОТ»."""
    wb = base.price()
    wp = wb["Структура цены"]
    stages = {}
    for stage, _pos, people, months, sal, _d1, _d2 in base.FOT_DETAIL:
        stages[stage] = stages.get(stage, 0) + people * months * sal
    s1, s2 = stages["Этап 1"], stages["Этап 2"]
    # Графы: C — всего по НИР, D — этап 1, E — этап 2. У гранта два этапа:
    # графы этапов 3–6 образца сняты.
    for r in range(3, 23):
        for c in range(6, 10):
            wp.cell(r, c).value = None
    dop = 0.086866
    rows = {}
    for col, ozp in (("D", s1), ("E", s2)):
        d = round(ozp * dop, 2)
        pay = ozp + d
        ins = round(pay * 0.302, 2)
        rows[col] = {5: 0, 6: 0, 7: pay, 8: ozp, 9: d, 10: ins, 11: 0, 12: 0, 13: 0,
                     14: 0, 15: 0}
        direct = pay + ins
        rows[col][4] = direct
        rows[col][16] = direct
        rows[col][17] = 0
        rows[col][18] = direct
        rows[col][19] = 0
        rows[col][20] = direct
        rows[col][21] = 0
        rows[col][22] = direct
    for r in range(4, 23):
        wp["D%d" % r] = rows["D"][r]
        wp["E%d" % r] = rows["E"][r]
        wp["C%d" % r] = round(rows["D"][r] + rows["E"][r], 2)
    _keep_only(wb, ["Структура цены", "Расшифровка ФОТ"])
    return wb


def price_sens():
    """Структура цены простая, «Сенсор-26»: трудоёмкость в человеко-часах —
    выделенное жёлтым: строки оплаты труда и «Расшифровка ФОТ»."""
    wb = openpyxl.load_workbook(os.path.join(FORMS, "Структура цены_ОБРАЗЕЦ_простой.xlsx"))
    wp = wb["структура цены"]
    wp["A5"] = "на выполнение научно-исследовательской работы по теме: «Сенсор-26», шифр C_SENS-26"
    rate = round(SENS_SALARY / HOURS, 2)
    per_stage = []
    for stage, d1, d2, months in SENS_STAGES:
        hours = round(months * HOURS, 2)
        per_stage.append((stage, d1, d2, hours, round(hours * rate, 2)))
    dop = 0.086866
    cols = {"D": per_stage[0][4], "E": per_stage[1][4]}
    for col, ozp in cols.items():
        d = round(ozp * dop, 2)
        ins = round((ozp + d) * 0.302, 2)
        vals = {10: 0, 11: 0, 12: ozp + d, 13: ozp, 14: d, 15: ins, 16: 0, 17: 0, 18: 0,
                19: 0, 20: 0}
        vals[9] = ozp + d + ins
        vals[21] = vals[9]
        vals[22] = 0
        vals[23] = vals[9]
        vals[24] = 0
        vals[25] = vals[9]
        vals[26] = 0
        vals[27] = vals[9]
        for r, v in vals.items():
            wp["%s%d" % (col, r)] = v
    for r in range(9, 28):
        wp["C%d" % r] = round((wp["D%d" % r].value or 0) + (wp["E%d" % r].value or 0), 2)
    ws = wb["Расшифровка ФОТ"]
    ws["A3"] = "Хоздоговор НИОКР «Сенсор-26», шифр C_SENS-26"
    # Трудоёмкость — в часах: подписи граф называют единицу, норматив часов
    # года — в шапке формы (K5), как в образце.
    ws["F4"] = "Трудоемкость на 1 чел., час"
    ws["G4"] = "Кол-во чел.-час."
    ws["H4"] = "Стоимость 1 чел.-часа, руб."
    ws["I5"] = "(гр6*гр7)"
    for rng in list(ws.merged_cells.ranges):
        if rng.min_row >= 7 and rng.max_row <= 13:
            ws.unmerge_cells(str(rng))
    for r in range(7, 14):
        for c in range(1, 13):
            ws.cell(r, c).value = None
    r = 7
    for i, (stage, d1, d2, hours, ozp) in enumerate(per_stage, start=1):
        ws.cell(r, 1).value = i
        ws.cell(r, 2).value = stage
        ws.cell(r, 3).value = "Разработка и испытания макета датчика" if i == 1 else \
            "Доработка и приёмочные испытания"
        ws.cell(r, 4).value = SENS_POSITION
        ws.cell(r, 5).value = 1
        ws.cell(r, 6).value = hours
        ws.cell(r, 7).value = hours
        ws.cell(r, 8).value = rate
        ws.cell(r, 9).value = ozp
        ws.cell(r, 11).value = d1
        ws.cell(r, 12).value = d2
        r += 1
    ws.cell(r, 2).value = "ИТОГО:"
    ws.cell(r, 9).value = round(sum(x[4] for x in per_stage), 2)
    _keep_only(wb, ["структура цены", "Расшифровка ФОТ"])
    return wb


def plan_mon():
    """План работ «Материал-26» по месяцам — лист трудоёмкости шаблона сервиса."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "трудоемкость_по_договорам"
    names = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
             "сентябрь", "октябрь", "ноябрь", "декабрь"]
    ws.append(["договор", "год", "должность", "трудоемкость", "средняя стоимость выполнения работ в месяц",
               "количество человек"] + names)
    _bold(ws, 1, 6 + 12)
    ws.append(["C_MON-26", YEAR, MON_POSITION, sum(MON_PLAN), MON_COST, 1] + MON_PLAN)
    for col, w in zip("ABCDEF", (12, 6, 22, 14, 22, 12)):
        ws.column_dimensions[col].width = w
    return wb


def salary():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Заработная плата"
    names = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август",
             "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
    ws.append(["Табельный номер", "ФИО"] + names)
    _bold(ws, 1, 14)
    for e in employees():
        ws.append([e[0], e[1]] + salary_months(e))
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 34
    for i in range(3, 15):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = 12
        for row in ws.iter_rows(min_row=2, min_col=i, max_col=i):
            for cell in row:
                cell.number_format = "# ##0.00"
    return wb


#: Код и название параметра формы 6.3 — как в app/facts.py (_PARAM).
PARAM = {"оклад": ("1", "Оклад (должностной оклад)"),
         "120": ("120", "За работу со сведениями, составляющими государственную тайну"),
         "122": ("122", "За качество выполняемых работ"),
         "124": ("124", "За интенсивность и высокие результаты работы"),
         "152": ("152", "За выполнение дополнительной работы"),
         "приказ": ("приказ", "Стимулирующая выплата приказом")}
SHR_HEAD = ["Таб.№", "Назначение", "Фамилия И.О., уч. ст., уч. зван.", "Код подр.",
            "Подразделение", "Должность", "Категория персонала", "Код пар-ра",
            "Название параметра", "Ставка", "Номинальное значение параметра",
            "Значение параметра по ставке", "Сумма в руб.", "Начало действия",
            "Окончание действия", "Код шифра затрат", "Шифр затрат", "Лицевой счет", "УИ ПНИЭР",
            "Номер приказа ввода", "Дата приказа ввода", "Номер приказа закрытия",
            "Дата приказа закрытия"]


def _short(fio):
    parts = fio.split()
    return parts[0] + " " + "".join(p[0] + "." for p in parts[1:])


def fact_january(pays, reference):
    """Факт января в форме 6.3 «ШР на дату, детализация назначений» —
    раскладка шаблона кадров (пользователь: «ШР на 01.06.2026_детализация_
    ШАБЛОН»): заголовок, шапка в седьмой строке, номера граф — в восьмой.
    pays — {код: [{договор, вид, сумма, ставка}]} января из расчёта."""
    ctr = {c[0]: c for c in contracts()}
    emps = {e[0]: e for e in employees()}
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Страница 1"
    for line in ("Подразделение - по всем подразделениям", "Дата построения 31.01.2026",
                 "Лицевой счет - по всем ЛС", "Параметры назначений на дату,", None, None):
        ws.append([line])
    ws.append(SHR_HEAD)
    for cell in ws[7]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.append(list(range(1, len(SHR_HEAD) + 1)))
    dept_code = {"Отдел 12": "0412", "Лаборатория 3": "0303"}
    n = 0
    rank = {k: i for i, k in enumerate(PARAM)}
    for code in sorted(pays):
        e = emps[code]
        ref = reference.get(e[2], {})
        seen = {}
        for p in sorted(pays[code], key=lambda x: (x["договор"], rank.get(x["вид"], 9))):
            c = ctr[p["договор"]]
            key = p["договор"]
            if key not in seen:
                n += 1
                seen[key] = "%s/%d" % (code, n)
            k, name = PARAM[p["вид"]]
            rate = p.get("ставка") if p["вид"] == "оклад" else None
            nominal = ref.get("оклад") if p["вид"] == "оклад" else None
            end = e[11]
            ws.append([code, seen[key], _short(e[1]), dept_code.get(e[3], ""), e[3],
                       e[2] + ("" if e[5] == "основное" else ", внутр. совмест."),
                       ref.get("категория") or "", k, name, rate, nominal,
                       round(p["сумма"], 2), round(p["сумма"], 2), e[10], end, c[0], c[1], c[4],
                       None, None, None, None, None])
    for row in ws.iter_rows(min_row=9, min_col=10, max_col=13):
        for cell in row:
            cell.number_format = "0.00" if cell.column == 10 else "# ##0.00"
    for i, width in enumerate((10, 12, 22, 9, 16, 28, 16, 9, 36, 8, 14, 14, 14, 12, 12, 14,
                               40, 16, 10, 12, 12, 12, 12), start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = width
    ws.freeze_panes = "D9"
    return wb


def _reference():
    wb = openpyxl.load_workbook(os.path.join(ROOT, "docs", "ui", "demo_input.xlsx"),
                                data_only=True, read_only=True)
    out = {}
    for r in wb["лимиты_по_должностям"].iter_rows(min_row=2, values_only=True):
        if r[0]:
            out[r[0]] = {"категория": r[1], "оклад": r[5]}
    wb.close()
    return out


README = """Демо-комплект документов на 2026 год — инструкция к показу.

Уже в реестре организации, загружать не нужно:
  • справочник должностей с окладами и пределами П2556, П4, БЭП;
  • правила замещения должностей.

ПЕРЕД ПОКАЗОМ — очистить реестр от прежнего демо (из папки проекта):
  .venv/Scripts/python.exe scripts/demo_pack.py clean
Снимает документы комплекта и прежнего демо, демо-план и ручные правки по
договорам и людям комплекта. Чужие договоры скрипт не трогает, только
называет — договор с трудоёмкостью, на который никому не разрешено, делает
план невыполнимым; его удалить в реестре руками.

ПОКАЗ — в приложении (http://127.0.0.1:8770):
  1. «Реестр организации» → вкладка «Документы» → загрузить файлы папки
     «1 Штатное расписание». Дождаться состояния «Разобран».
  2. Папка «2 Договоры» — по одной папке договора за раз: карточка договора
     и документы по нему. У договоров трудоёмкость задана разными таблицами:
       C_GOZ-26   — РКМ ГОЗ: форма 9 (9д), чел.-мес. по должностям;
       C_GRANT-26 — структура цены расширенная: этапы с датами;
       C_SENS-26  — структура цены простая: трудоёмкость в часах;
       C_MON-26   — план работ по месяцам;
       C_BASE-26, C_COM-26, C_PRI-26 — только карточка, ФОТ одной суммой.
  3. «3 Заработная плата» — по месяцам; у четверых с сентября рост.
  4. «4 Факт начислений» — январь, форма 6.3 «ШР на дату, детализация».
  5. Создать план на 2026 год («+» у «Планы») и нажать «Рассчитать план»:
     расчёт идёт около минуты, январь в плане — факт.

Порядок важен: зарплата и факт находят сотрудников и договоры в реестре по
табельному номеру и шифру, поэтому идут после штатного и договоров. Разбор
файла — минута-две.

Всё то же одной командой (загрузка, расчёт и проверки плана):
  .venv/Scripts/python.exe scripts/demo_pack.py load
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--fact", default=FACT_JSON,
                    help="выплаты января из расчёта комплекта (json); пусто — без факта")
    args = ap.parse_args()
    out = args.out
    if os.path.isdir(out):
        shutil.rmtree(out)
    books = {os.path.join(STAFF_DIR, "Штатное расписание 2026.xlsx"): staff(),
             os.path.join(SALARY_DIR, "Заработная плата по месяцам 2026.xlsx"): salary()}
    for c in contracts():
        folder = os.path.join(CONTRACTS_DIR, FOLDERS[c[0]])
        books[os.path.join(folder, "Договор %s %s.xlsx" % (c[2].replace("/", "-"),
                                                          FOLDERS[c[0]].split(" ", 1)[1]))] = \
            contract_card(c)
    books[os.path.join(CONTRACTS_DIR, FOLDERS["C_GOZ-26"], "РКМ ГОЗ Радар-26.xlsx")] = rkm_goz()
    books[os.path.join(CONTRACTS_DIR, FOLDERS["C_GRANT-26"],
                       "Структура цены Грант-26.xlsx")] = price_grant()
    books[os.path.join(CONTRACTS_DIR, FOLDERS["C_SENS-26"],
                       "Структура цены Сенсор-26.xlsx")] = price_sens()
    books[os.path.join(CONTRACTS_DIR, FOLDERS["C_MON-26"],
                       "План работ Материал-26.xlsx")] = plan_mon()
    if args.fact:
        with open(args.fact, encoding="utf-8") as f:
            pays = json.load(f)
        books[os.path.join(FACT_DIR, "ШР на 31.01.2026_детализация.xlsx")] = \
            fact_january(pays, _reference())
    for rel, wb in books.items():
        path = os.path.join(out, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        wb.save(path)
        # Папка вывода бывает на другом диске (временная — на C:, проект — на
        # E:), и relpath там падает: печатаем полный путь.
        try:
            shown = os.path.relpath(path, ROOT)
        except ValueError:
            shown = path
        print("записан", shown)
    with open(os.path.join(out, "Порядок загрузки.txt"), "w", encoding="utf-8") as f:
        f.write(README)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
