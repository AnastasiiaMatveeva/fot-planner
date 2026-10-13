# -*- coding: utf-8 -*-
"""Проекты приказов из расчёта плана — документы Word.

Экономист планирует выплаты, а дальше ему нужны подписанные документы: в 1С
надбавка и стимулирующая выплата начисляются на основании приказа, и
перебивать суммы руками из плана — лишняя работа и лишняя ошибка (решение
пользователя 28.09.2026). С 30.09.2026 приказы собираются из расчёта: строка
вкладки «Проекты приказов» — приказ вида × договора × месяца со списком
сотрудников (форма Т-11а), у каждого сотрудника — свой приказ на одного
(Т-11) тем же бланком (спецификация дизайнера по словам пользователя: «по
конкретному договору выбрать тип — приказ, в выбранный месяц для выбранного
сотрудника, чтоб в шаблон подставилось и выгрузилось; правда, шаблона пока
нет, но нужна заготовка»).

Форма черновая: настоящего бланка организации ещё нет. Поэтому весь текст
собран в ``ORDER_TYPES`` одним местом, а бланк организации можно положить в
data/templates/<вид>.docx с полями {Имя} — тогда приказ заполняется по нему.
Файл собирается без сторонних пакетов: .docx — это zip с несколькими
XML-частями, и для формы из заголовка, абзацев и таблицы их хватает.
"""
from __future__ import annotations

import io
import zipfile
from datetime import date

MONTHS_GEN = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль",
              "август", "сентябрь", "октябрь", "ноябрь", "декабрь")

#: Род. падеж месяца для «с 1 марта».
MONTHS_OF = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
             "сентября", "октября", "ноября", "декабря")

#: Пункты после таблицы — одинаковые у всех видов.
TAIL = [
    "2. Главному бухгалтеру обеспечить начисление и выплату указанных сумм в сроки, "
    "установленные для выплаты заработной платы.",
    "3. Контроль за исполнением настоящего приказа оставляю за собой.",
]

BLANK = "____________"

#: Виды приказов — те, что план порождает в практике 1С:ЗУП (консультация по
#: 1С, 30.09.2026): стимулирующая выплата приказом («Премия»), установление,
#: изменение или прекращение надбавки («Изменение плановых начислений») и
#: смена источника финансирования (распределение по статьям финансирования).
#: Кадровый перевод и приём по совместительству печатает сама 1С. Приказ —
#: списком сотрудников (Т-11а); приказ на одного — тот же бланк с одной
#: строкой. %(кому)s — «работнику» или «работникам», %(кого)s — «работника»
#: или «работников». Графы: (ширина в твипах, заголовок, поле строки).
ORDER_TYPES = {
    "стимулирующая": {
        "name": "О стимулирующей выплате",
        "title": "О стимулирующей выплате по договору %(код)s",
        "lead": ("В связи с выполнением работ по договору %(код)s%(название)s "
                 "ПРИКАЗЫВАЮ:"),
        "first": ("1. Установить %(кому)s стимулирующую выплату за %(месяц)s %(год)s года "
                  "за счёт средств договора %(код)s (лицевой счёт %(счёт)s):"),
        "fields": [(450, "№", "Номер_п_п"), (3100, "Фамилия, имя, отчество", "ФИО"),
                   (1100, "Таб. №", "Таб_номер"), (2700, "Должность", "Должность"),
                   (2005, "Сумма, руб.", "Размер")],
    },
    "надбавка": {
        "name": "Об установлении, изменении и прекращении надбавок",
        "title": "Об установлении, изменении и прекращении надбавок",
        "lead": ("В связи с изменением условий оплаты труда по договору %(код)s%(название)s "
                 "ПРИКАЗЫВАЮ:"),
        "first": ("1. С 1 %(месяц_род)s %(год)s года установить, изменить или прекратить "
                  "%(кому)s надбавки за счёт средств договора %(код)s (лицевой счёт %(счёт)s) "
                  "в следующих размерах:"),
        "fields": [(450, "№", "Номер_п_п"), (2300, "Фамилия, имя, отчество", "ФИО"),
                   (900, "Таб. №", "Таб_номер"), (2000, "Вид выплаты", "Вид_выплаты"),
                   (650, "Код", "Код_начисления"), (1155, "Действие", "Действие"),
                   (950, "Было в месяц, руб.", "Размер_было"),
                   (950, "Стало в месяц, руб.", "Размер")],
    },
    "источник": {
        "name": "Об изменении источника финансирования",
        "title": "Об изменении источника финансирования оплаты труда",
        "lead": "В связи с распределением оплаты труда по договорам ПРИКАЗЫВАЮ:",
        "first": ("1. С 1 %(месяц_род)s %(год)s года оплату труда %(кого)s производить "
                  "за счёт следующих источников:"),
        "fields": [(450, "№", "Номер_п_п"), (2100, "Фамилия, имя, отчество", "ФИО"),
                   (850, "Таб. №", "Таб_номер"), (1500, "Вид выплаты", "Вид_выплаты"),
                   (1500, "Договор", "Договор_строки"),
                   (1055, "Лицевой счёт", "Лицевой_счёт_строки"),
                   (950, "Было в месяц, руб.", "Размер_было"),
                   (950, "Стало в месяц, руб.", "Размер")],
    },
    # Изменение состава рабочей группы (UI-023, 12.10.2026): приказ о группе —
    # вход, а кого расчёт посадил на договор или снял с него — выход. Текст
    # держит то, что читает разбор приказа-входа (app/workgroup.py): «дата №
    # номер» в шапке, «в приказ от … № …», «л/с», «на период с … по …»,
    # абзацы «включить»/«исключить» и строки «№ · ФИО · таб. № · должность ·
    # подразделение».
    "состав группы": {
        "name": "О внесении изменений в состав рабочей группы",
        "title": "О внесении изменений в приказ от %(осн_дата)s № %(осн_номер)s",
        "lead": ("В связи с изменением состава исполнителей работ по договору "
                 "%(код)s%(название)s%(соглашение)s (л/с %(счёт)s) ПРИКАЗЫВАЮ:"),
        "first": ("1. Внести изменения в состав рабочей группы, утверждённый приказом от "
                  "%(осн_дата)s № %(осн_номер)s, на период с %(с)s по %(по)s:"),
        "fields": [(450, "№", "Номер_п_п"), (3000, "Фамилия, имя, отчество", "ФИО"),
                   (1100, "Таб. №", "Таб_номер"), (2400, "Должность", "Должность"),
                   (2405, "Подразделение", "Подразделение")],
    },
}

#: Абзацы над строками приказа о составе группы — по действию.
GROUP_ACTIONS = (("включить", "включить в состав рабочей группы:"),
                 ("исключить", "исключить из состава рабочей группы:"))

#: Пункт после таблиц у приказа о составе группы: денег в нём нет.
GROUP_TAIL = ["2. Контроль за исполнением настоящего приказа оставляю за собой."]

#: Названия видов выплат и коды начислений — как в форме кадров (раздел 6.3).
KIND_NAMES = {"оклад": ("Оклад (должностной оклад)", "1"),
              "120": ("За работу со сведениями, составляющими государственную тайну", "120"),
              "122": ("За качество выполняемых работ", "122"),
              "124": ("За интенсивность и высокие результаты работы", "124"),
              "152": ("За выполнение дополнительной работы", "152"),
              "приказ": ("Стимулирующая выплата приказом", "приказ")}

#: Поля шаблона: {Имя} в тексте .docx заменяется значением. Строка таблицы,
#: где стоит поле строки, повторяется на каждую строку приказа.
PLACEHOLDERS = ("Организация", "Номер", "Дата", "Договор_шифр", "Договор_номер",
                "Договор_наименование", "Лицевой_счёт", "Месяц", "Месяц_род", "Год", "Итого",
                "Подписант", "Должность_подписанта", "Основание_номер", "Основание_дата",
                "Соглашение", "Соглашение_дата", "Тема", "Период_с", "Период_по")
ROW_PLACEHOLDERS = ("Номер_п_п", "ФИО", "Таб_номер", "Должность", "Подразделение", "Ставка",
                    "Вид_выплаты", "Код_начисления", "Действие", "Размер_было", "Размер",
                    "Договор_строки", "Лицевой_счёт_строки", "Период")

#: Что значат поля — в архив заготовок, тому, кто делает бланк организации.
FIELD_NOTES = {
    "Организация": "наименование организации", "Номер": "номер приказа",
    "Дата": "дата приказа", "Договор_шифр": "шифр договора приказа",
    "Договор_номер": "номер договора", "Договор_наименование": "наименование договора",
    "Лицевой_счёт": "лицевой счёт договора", "Месяц": "месяц словом: «март»",
    "Месяц_род": "месяц в родительном падеже: «марта» — для «с 1 марта»",
    "Год": "год плана", "Итого": "сумма графы «Стало» по приказу",
    "Подписант": "ФИО подписанта", "Должность_подписанта": "должность подписанта",
    "Основание_номер": "номер приказа о рабочей группе, в который вносятся изменения",
    "Основание_дата": "дата приказа о рабочей группе, в который вносятся изменения",
    "Соглашение": "номер соглашения из приказа о рабочей группе",
    "Соглашение_дата": "дата соглашения из приказа о рабочей группе",
    "Тема": "тема работ из приказа о рабочей группе",
    "Период_с": "начало действия изменений: 1-е число месяца приказа",
    "Период_по": "окончание срока рабочей группы",
    "Номер_п_п": "номер сотрудника в приказе", "ФИО": "фамилия, имя, отчество сотрудника",
    "Таб_номер": "табельный номер", "Должность": "должность", "Подразделение": "подразделение",
    "Ставка": "ставка на договоре строки в месяце приказа",
    "Вид_выплаты": "название вида выплаты", "Код_начисления": "код начисления (1, 120…152)",
    "Действие": ("установить, изменить или прекратить (надбавка); включить или исключить "
                 "(состав группы)"),
    "Размер_было": "сумма в месяц до месяца приказа", "Размер": "сумма в месяц с месяца приказа",
    "Договор_строки": "договор строки (источник финансирования)",
    "Лицевой_счёт_строки": "лицевой счёт договора строки", "Период": "месяц и год строки",
}


def _esc(v):
    return (str("" if v is None else v).replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _money(v):
    """125000 → «125 000,00»; пусто → место для руки."""
    if v in (None, ""):
        return BLANK
    try:
        n = round(float(v), 2)
    except (TypeError, ValueError):
        return _esc(v)
    whole, cents = divmod(int(round(n * 100)), 100)
    out = "{:,}".format(whole).replace(",", " ")
    return "%s,%02d" % (out, cents)


def _p(text, *, bold=False, align=None, size=24, after=120, ind=0):
    """Абзац. size — половины пункта, как в OOXML: 24 = 12 пт."""
    pr = ['<w:spacing w:after="%d"/>' % after]
    if align:
        pr.append('<w:jc w:val="%s"/>' % align)
    if ind:
        pr.append('<w:ind w:firstLine="%d"/>' % ind)
    rpr = '<w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman"/>' \
          '<w:sz w:val="%d"/>%s</w:rPr>' % (size, "<w:b/>" if bold else "")
    return ('<w:p><w:pPr>%s</w:pPr><w:r>%s<w:t xml:space="preserve">%s</w:t></w:r></w:p>'
            % ("".join(pr), rpr, _esc(text)))


def _cell(text, width, *, bold=False, align="left"):
    return ('<w:tc><w:tcPr><w:tcW w:w="%d" w:type="dxa"/></w:tcPr>%s</w:tc>'
            % (width, _p(text, bold=bold, align=align, size=22, after=0)))


def _table(fields, rows, total=None):
    """Таблица приказа: шапка граф, строки {поле строки: текст}, итог."""
    head = "<w:tr>%s</w:tr>" % "".join(
        _cell(title, width, bold=True, align="center") for width, title, _ in fields)
    body = ["<w:tr>%s</w:tr>" % "".join(
        _cell(r.get(key, ""), width, align="right" if title.endswith("руб.") else "left")
        for width, title, key in fields) for r in rows]
    if total is not None:
        body.append("<w:tr>%s</w:tr>" % "".join(
            _cell("Итого" if i == 1 else total if title.endswith("руб.") else "", width,
                  bold=True, align="right" if title.endswith("руб.") else "left")
            for i, (width, title, _) in enumerate(fields)))
    borders = ("<w:tblBorders>" + "".join(
        '<w:%s w:val="single" w:sz="4" w:space="0" w:color="000000"/>' % side
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")) +
        "</w:tblBorders>")
    return ('<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/>%s</w:tblPr>%s%s</w:tbl>'
            % (borders, head, "".join(body)))


def _month_words(month, names):
    return names[month - 1] if isinstance(month, int) and 1 <= month <= 12 else (month or BLANK)


def _amount(v):
    """Сумма ячейки: число — деньгами, пусто — прочерк."""
    if v in (None, ""):
        return "—"
    return _money(v)


def order_values(data, grouped=True):
    """Поля приказа: ({поле шапки: текст}, [{поле строки: текст}], [ФИО]).

    data: {тип, организация, номер, дата, договор, наименование, номер договора,
    счёт, месяц (1–12), год, подписант, должность подписанта, строки: [{фио,
    табельный, должность, подразделение, ставка, вид, действие, было, стало,
    договор, счёт}]} — строки одного сотрудника подряд. grouped — номер, ФИО,
    табельный номер и должность только в первой строке сотрудника, как в
    бланке сервиса; в шаблоне организации строка плоская, и они в каждой."""
    month = data.get("месяц")
    lines = data.get("строки") or []
    people, rows, total = [], [], 0.0
    for r in lines:
        first = r.get("фио") not in people
        if first:
            people.append(r.get("фио"))
        name, code = KIND_NAMES.get(r.get("вид"), (r.get("вид") or "", r.get("вид") or ""))
        rate = r.get("ставка")
        show = first or not grouped
        rows.append({
            "Номер_п_п": str(len(people)) if show else "",
            "ФИО": (r.get("фио") or "") if show else "",
            "Таб_номер": (r.get("табельный") or "") if show else "",
            "Должность": (r.get("должность") or "") if show else "",
            "Подразделение": (r.get("подразделение") or "") if show else "",
            "Ставка": (("%g" % rate).replace(".", ",")
                       if isinstance(rate, (int, float)) else ""),
            "Вид_выплаты": name, "Код_начисления": code,
            "Действие": r.get("действие") or "",
            "Размер_было": _amount(r.get("было")), "Размер": _amount(r.get("стало")),
            "Договор_строки": r.get("договор") or "", "Лицевой_счёт_строки": r.get("счёт") or "",
            "Период": "%s %s" % (_month_words(month, MONTHS_GEN), data.get("год") or ""),
        })
        total += float(r.get("стало") or 0)
    head = {"Организация": data.get("организация") or "", "Номер": data.get("номер") or "",
            "Дата": data.get("дата") or "", "Договор_шифр": data.get("договор") or "",
            "Договор_номер": data.get("номер договора") or "",
            "Договор_наименование": data.get("наименование") or "",
            "Лицевой_счёт": data.get("счёт") or "",
            "Месяц": _month_words(month, MONTHS_GEN) if isinstance(month, int) else "",
            "Месяц_род": _month_words(month, MONTHS_OF) if isinstance(month, int) else "",
            "Год": str(data.get("год") or ""), "Итого": _money(total) if total else "",
            "Подписант": data.get("подписант") or "",
            "Должность_подписанта": data.get("должность подписанта") or ""}
    group = data.get("группа") or {}
    head.update({"Основание_номер": group.get("основание номер") or "",
                 "Основание_дата": group.get("основание дата") or "",
                 "Соглашение": group.get("соглашение") or "",
                 "Соглашение_дата": group.get("соглашение дата") or "",
                 "Тема": group.get("тема") or "",
                 "Период_с": group.get("с") or "", "Период_по": group.get("по") or ""})
    return head, rows, people


def _group_body(head, rows, ctx):
    """Пункт 1 приказа о составе группы: абзац действия и таблица его строк.

    Номер строки — свой в каждой таблице. Строки с иным действием (поле
    {Действие} у заготовки) — одной таблицей без абзаца."""
    t = ORDER_TYPES["состав группы"]
    parts = [_p(t["first"] % ctx, ind=709, after=180)]
    known = [a for a, _ in GROUP_ACTIONS]
    sections = [(text, [r for r in rows if r.get("Действие") == a]) for a, text in GROUP_ACTIONS]
    sections.append((None, [r for r in rows if r.get("Действие") not in known]))
    for text, part in sections:
        if not part:
            continue
        if text:
            parts.append(_p(text, ind=709, after=120))
        part = [dict(r, Номер_п_п=str(i) if str(r.get("Номер_п_п")).isdigit()
                     else r.get("Номер_п_п")) for i, r in enumerate(part, 1)]
        parts += [_table(t["fields"], part), _p("", after=180)]
    return parts


def order_document(kind, head, rows, people, many=None):
    """XML тела приказа: бланк сервиса. head и rows — из order_values (или
    поля {Имя} у заготовки); people — ФИО для строки ознакомления; many —
    приказ на нескольких (по умолчанию — больше одного в people)."""
    kind = kind if kind in ORDER_TYPES else "стимулирующая"
    t = ORDER_TYPES[kind]
    many = len(people) != 1 if many is None else many
    name = head.get("Договор_наименование")
    ctx = {"код": head.get("Договор_шифр") or BLANK,
           "название": (" «%s»" % name) if name else "",
           "месяц": head.get("Месяц") or BLANK, "месяц_род": head.get("Месяц_род") or BLANK,
           "год": head.get("Год") or date.today().year,
           "счёт": head.get("Лицевой_счёт") or BLANK,
           "кому": "работникам" if many else "работнику",
           "кого": "работников" if many else "работника"}
    group = kind == "состав группы"
    if group:
        deal = head.get("Соглашение")
        ctx.update({
            "осн_дата": head.get("Основание_дата") or BLANK,
            "осн_номер": head.get("Основание_номер") or BLANK,
            "соглашение": ((", соглашение от %s № %s" % (head.get("Соглашение_дата"), deal)
                            if head.get("Соглашение_дата") else ", соглашение № %s" % deal)
                           + (" «%s»" % head["Тема"] if head.get("Тема") else "")) if deal else "",
            "с": head.get("Период_с") or BLANK, "по": head.get("Период_по") or BLANK})
    parts = [
        _p(head.get("Организация") or BLANK, bold=True, align="center", after=60),
        _p("ПРИКАЗ", bold=True, align="center", after=240),
        # Кадровый приказ о группе — «дата № номер»: так его шапку читает разбор.
        _p(("%s № %s" if group else "№ %s от %s")
           % ((head.get("Дата") or BLANK, head.get("Номер") or BLANK) if group
              else (head.get("Номер") or BLANK, head.get("Дата") or BLANK)),
           align="center", after=240),
        _p(t["title"] % ctx, bold=True, after=240),
        _p(t["lead"] % ctx, ind=709),
    ]
    if group:
        parts += _group_body(head, rows, ctx)
    else:
        parts += [
            _p(t["first"] % ctx, ind=709, after=180),
            _table(t["fields"], rows,
                   head.get("Итого") or BLANK if kind == "стимулирующая" and len(rows) > 1
                   else None),
            _p("", after=180),
        ]
    parts += [_p(x, ind=709) for x in (GROUP_TAIL if group else TAIL)]
    parts.append(_p("", after=360))
    parts.append(_p("%s\t\t%s\t\t%s"
                    % (head.get("Должность_подписанта") or "Директор",
                       BLANK, head.get("Подписант") or BLANK), after=0))
    parts.append(_p("", after=240))
    if len(people) == 1:
        parts.append(_p("С приказом ознакомлен(а): %s\t%s" % (BLANK, people[0] or BLANK),
                        after=0))
    else:
        parts.append(_p("С приказом ознакомлены:", after=120))
        parts += [_p("%s\t%s" % (BLANK, x or BLANK), after=60) for x in people]
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body>%s<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1134" w:right="850" w:bottom="1134" w:left="1701"/>'
            '</w:sectPr></w:body></w:document>' % "".join(parts))


def fill_template(raw, data):
    """Заполнить .docx-шаблон организации полями {Имя}.

    Поле должно стоять в тексте одним куском: Word делит слово на части, если
    внутри меняли оформление, — такое поле не найдётся и останется как есть.
    Строка таблицы, где есть поле строки ({ФИО}, {Размер}…), повторяется на
    каждую строку приказа. Поле строки вне таблицы (приказ на одного по
    бланку «Работник: {ФИО}») берёт значение единственного сотрудника."""
    import re
    head, rows, people = order_values(data, grouped=False)
    marks = tuple("{%s}" % n for n in ROW_PLACEHOLDERS)
    single = rows[0] if len(people) == 1 and rows else {}

    def repeat(m):
        tr = m.group(0)
        if not any(x in tr for x in marks):
            return tr
        made = []
        for r in rows or [{}]:
            one = tr
            for n in ROW_PLACEHOLDERS:
                one = one.replace("{%s}" % n, _esc(r.get(n, "")))
            made.append(one)
        return "".join(made)

    src = zipfile.ZipFile(io.BytesIO(raw))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            body = src.read(item.filename)
            if item.filename.startswith("word/") and item.filename.endswith(".xml"):
                text = re.sub(r"<w:tr[ >].*?</w:tr>", repeat, body.decode("utf-8"), flags=re.S)
                for n in PLACEHOLDERS:
                    text = text.replace("{%s}" % n, _esc(head.get(n, "")))
                for n in ROW_PLACEHOLDERS:
                    text = text.replace("{%s}" % n, _esc(single.get(n, "")))
                body = text.encode("utf-8")
            out.writestr(item, body)
    return buf.getvalue()


CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
                 'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
                 '<Override PartName="/word/document.xml" ContentType="application/vnd.'
                 'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')

RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>')


def _docx(xml):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def build_order(data, template_dir=None):
    """Приказ как .docx (байты): список сотрудников или один сотрудник.

    Если в template_dir лежит «<тип>.docx» — это бланк организации с полями
    {Имя}; иначе — бланк сервиса."""
    import os
    kind = data.get("тип") if data.get("тип") in ORDER_TYPES else "стимулирующая"
    if template_dir:
        path = os.path.join(template_dir, "%s.docx" % kind)
        if os.path.exists(path):
            with open(path, "rb") as f:
                return fill_template(f.read(), data)
    head, rows, people = order_values(data)
    return _docx(order_document(kind, head, rows, people))


def stub(kind):
    """Заготовка шаблона: бланк сервиса с полями {Имя} вместо значений. Её
    отдают тому, кто сделает настоящий бланк, и кладут обратно в
    data/templates/<вид>.docx — дальше приказ заполняется по ней."""
    kind = kind if kind in ORDER_TYPES else "стимулирующая"
    head = {n: "{%s}" % n for n in PLACEHOLDERS}
    row = {n: "{%s}" % n for n in ROW_PLACEHOLDERS}
    return _docx(order_document(kind, head, [row], ["{ФИО}"], many=True))


def templates_zip():
    """Заготовки всех видов одним архивом: имена файлов — те, что сервис ищет
    в data/templates, и перечень полей с тем, что они значат."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for kind in ORDER_TYPES:
            z.writestr("%s.docx" % kind, stub(kind))
        notes = ["Заготовки бланков приказов. Бланк организации кладут в data/templates",
                 "под тем же именем файла; поля {Имя} заменяются значениями приказа.",
                 "Строка таблицы, где есть поле строки, повторяется на каждую строку приказа.",
                 "", "Поля шапки:"]
        notes += ["  {%s} — %s" % (n, FIELD_NOTES[n]) for n in PLACEHOLDERS]
        notes += ["", "Поля строки:"]
        notes += ["  {%s} — %s" % (n, FIELD_NOTES[n]) for n in ROW_PLACEHOLDERS]
        z.writestr("Поля шаблонов.txt", "\r\n".join(notes).encode("utf-8"))
    return buf.getvalue()


def file_name(data, one=False):
    """«Приказ о стимулирующей выплате C_BASE-26 март 2026.docx»; приказ на
    одного — с фамилией и инициалами."""
    kind = data.get("тип") if data.get("тип") in ORDER_TYPES else "стимулирующая"
    name = ORDER_TYPES[kind]["name"]
    title = "Приказ " + name[0].lower() + name[1:]
    short = ""
    if one and data.get("строки"):
        fio = str(data["строки"][0].get("фио") or "").split()
        short = (fio[0] + " " + " ".join(x[0] + "." for x in fio[1:3])).strip() if fio else ""
    month = data.get("месяц")
    return " ".join(x for x in (title, data.get("договор"), short,
                                MONTHS_GEN[month - 1] if isinstance(month, int) else "",
                                str(data.get("год") or "")) if x) + ".docx"
