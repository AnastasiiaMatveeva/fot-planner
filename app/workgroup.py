# -*- coding: utf-8 -*-
"""Приказ о рабочей группе — вход плана (решение пользователя 28.09.2026).

Для каждого договора руководитель темы формирует временный научный коллектив
(рабочую группу) — «Положение об оплате труда», п. 4.1.3. Приказ называет
строки штатного расписания, которые сидят на договоре: ФИО, табельный номер,
должность и подразделение, а в приложении — вид работ каждого. Сумм в нём нет:
выплаты производят по отдельным приказам о стимулирующих выплатах (п. 7 и 9
приказа), а состав меняют приказом о внесении изменений («по необходимости»,
п. 4.1.3 Положения).

Форма у приказа стабильная — это кадровый документ из одной системы, — поэтому
он читается по полям, а не моделью: так разбор воспроизводим и не выдумывает
людей. Чего в тексте не нашлось, остаётся пустым, а не угадывается: договор и
строку штатного расписания экономист выбирает сам.
"""
from __future__ import annotations

import re

#: Вид документа для классификатора и реестра.
KIND = "приказ о рабочей группе"

_DATE = r"\d{2}\.\d{2}\.\d{2,4}"
_NUMBER = r"[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё\-/]*"


def _clean(v):
    return re.sub(r"\s+", " ", str(v or "")).strip()


def looks_like(text):
    """Похож ли текст на приказ о рабочей группе: заголовок и таблица состава."""
    flat = _clean(text).lower()
    if "рабоч" not in flat or "групп" not in flat:
        return False
    if "приказ" not in flat and "п р и к а з" not in flat:
        return False
    return bool(_members(text))


def _members(text):
    """Строки таблицы состава: № · ФИО · табельный номер · должность · подразделение.

    Табельный номер — текстом: у него бывает ведущий ноль (06043)."""
    out = []
    for line in str(text or "").splitlines():
        cells = [c.strip() for c in line.split("\t")]
        cells = [c for c in cells if c != ""]
        if len(cells) < 4 or not cells[0].isdigit():
            continue
        tab = cells[2] if len(cells) > 2 else ""
        if not re.fullmatch(r"\d{3,10}", tab):
            continue
        out.append({"n": int(cells[0]), "фио": _clean(cells[1]), "табельный": tab,
                    "должность": _clean(cells[3]) if len(cells) > 3 else "",
                    "подразделение": _clean(cells[4]) if len(cells) > 4 else ""})
    return out


def _duties(text):
    """Приложение «Распределение обязанностей»: ФИО → вид работ."""
    t = str(text or "")
    at = t.lower().find("распределение обязанностей")
    if at < 0:
        return {}
    out = {}
    for line in t[at:].splitlines():
        cells = [c.strip() for c in line.split("\t") if c.strip()]
        if len(cells) >= 3 and cells[0].isdigit() and not re.fullmatch(r"\d{3,10}", cells[2]):
            out[_clean(cells[1]).lower()] = _clean(" ".join(cells[2:]))
    return out


def _same_word(a, b):
    """Одно ли это слово в разных падежах: «Новокшенова» и «Новокшенов»,
    «Анну» и «Анна». Общее начало — всё слово, кроме двух последних букв."""
    a, b = str(a or "").lower().replace("ё", "е"), str(b or "").lower().replace("ё", "е")
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return common >= max(3, min(len(a), len(b)) - 2)


def _leader(text, members):
    """Руководитель группы — одна из строк состава.

    В приказе он назван в винительном падеже («назначить Новокшенова Алексея
    Дмитриевича», «назначить Сидорову Анну»), поэтому фамилия и имя сверяются
    по общему началу слова, а не строкой."""
    m = re.search(r"Руководител\w*\s+рабочей\s+группы.*?назначить\s+([А-ЯЁ][а-яё\-]+)\s+"
                  r"([А-ЯЁ][а-яё\-]+)", _clean(text), re.S)
    if not m:
        return None
    for r in members:
        parts = r["фио"].split()
        if (len(parts) >= 2 and _same_word(parts[0], m.group(1))
                and _same_word(parts[1], m.group(2))):
            return r["фио"]
    return None


def parse(text):
    """Поля приказа. Ненайденное — None, а не догадка.

    {вид: «создание» | «изменение», номер, дата, основание: {номер, дата} (у
    изменения), соглашение, соглашение от, тема, счёт, кфо, с, по,
    руководитель, состав: [{n, фио, табельный, должность, подразделение,
    работы, действие: «включить» | «исключить»}]}.
    """
    raw = str(text or "")
    flat = _clean(raw)
    low = flat.lower()
    out = {"вид": "изменение" if "внесении изменени" in low else "создание",
           "номер": None, "дата": None, "основание": None, "соглашение": None,
           "соглашение от": None, "тема": None, "счёт": None, "кфо": None,
           "с": None, "по": None, "руководитель": None, "состав": []}

    head = re.search(r"(%s)\s*№\s*(%s)" % (_DATE, _NUMBER), flat)
    if head:
        out["дата"], out["номер"] = head.group(1), head.group(2)

    if out["вид"] == "изменение":
        base = re.search(r"в\s+приказ\w*\s+от\s+(%s)\s*№\s*(%s)" % (_DATE, _NUMBER), flat, re.I)
        if base:
            out["основание"] = {"дата": base.group(1), "номер": base.group(2)}

    deal = re.search(r"(?:соглашени|договор)\w*\s+от\s+(%s)\s*№\s*(%s)" % (_DATE, _NUMBER),
                     flat, re.I)
    if deal:
        out["соглашение от"], out["соглашение"] = deal.group(1), deal.group(2)
        topic = re.search(r"«\s*([^»]+?)\s*»", flat[deal.end():deal.end() + 400])
        if topic:
            out["тема"] = topic.group(1)

    acc = re.search(r"л\s*/\s*с\.?\s*([0-9A-Za-zА-Яа-я]+)", flat, re.I)
    if acc:
        out["счёт"] = acc.group(1)
    kfo = re.search(r"КФО\s*(\d+)", flat)
    if kfo:
        out["кфо"] = kfo.group(1)

    span = re.search(r"на\s+период\s+с\s*(%s)\s*по\s*(%s)" % (_DATE, _DATE), flat, re.I)
    if span:
        out["с"], out["по"] = span.group(1), span.group(2)

    # Состав. У изменения строки идут под «включить» и «исключить»; у приказа
    # о создании — все «включить».
    action = "включить"
    rows = []
    for line in raw.splitlines():
        l = line.lower()
        if "исключить" in l and "\t" not in line:
            action = "исключить"
        elif "включить" in l and "\t" not in line:
            action = "включить"
        elif "распределение обязанностей" in l:
            break
        got = _members(line)
        if got:
            got[0]["действие"] = action
            rows.append(got[0])
    duties = _duties(raw)
    for r in rows:
        r["работы"] = duties.get(r["фио"].lower())
    out["состав"] = rows
    out["руководитель"] = _leader(raw, rows)
    return out


def summary(p):
    """Одна строка для реестра и ленты: что это за приказ и сколько в нём людей."""
    n = len(p.get("состав") or [])
    what = ("изменения в приказ № %s от %s" % (p["основание"]["номер"], p["основание"]["дата"])
            if p.get("вид") == "изменение" and p.get("основание")
            else "рабочая группа")
    deal = (" по соглашению № %s" % p["соглашение"]) if p.get("соглашение") else ""
    span = (", %s – %s" % (p["с"], p["по"])) if p.get("с") and p.get("по") else ""
    return "Приказ № %s от %s: %s%s, %d %s%s" % (
        p.get("номер") or "?", p.get("дата") or "?", what, deal, n,
        "человек" if n % 10 not in (2, 3, 4) or n % 100 in (12, 13, 14) else "человека", span)


def norm_account(v):
    """Лицевой счёт для сравнения: без пробелов, точек и регистра."""
    return re.sub(r"[\s.\-]+", "", str(v or "")).lower()


def norm_position(v):
    """Должность для сравнения: без регистра, лишних пробелов и «ё»."""
    return _clean(v).lower().replace("ё", "е")
