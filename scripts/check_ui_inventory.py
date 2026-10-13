# -*- coding: utf-8 -*-
"""UI-007: инвентарь примитивов интерфейса — новый вариант кнопки виден сразу.

Правило плотного интерфейса (UI-003) запрещало лишние элементы словами, но
проверять его было нечем. За несколько сборок в `app/static` набралось больше
сорока классов кнопок, и среди них пары под одно назначение: `primary` и
`pri`, `iconbtn` и `ico`. Словами такую россыпь не остановить: каждая отдельная
правка выглядит безобидно, а расходится состав постепенно.

Поэтому состав ведётся списком в `docs/ui-inventory.json`, а эта проверка
сверяет список с исходниками. Новый класс кнопки или метки — красное, пока
человек не внесёт его в список осознанно. Исчезнувший класс — тоже красное:
список, разошедшийся с кодом, перестаёт быть ограничением и становится
украшением.

Что читается:

* кнопки — классы в атрибуте `class` у `<button>` в `app/static/*.html` и
  `*.js`;
* метки — классы CSS в `app/static/*.css`, в имени которых есть `tag`, `chip`,
  `pill`, `badge` или `dot`.

Пробел покрытия: класс, который целиком вычисляется выражением JavaScript
(`class="' + kind + '"`), в тексте не виден и в инвентарь не попадает. Такие
места считаются и печатаются числом — пробел должен быть известен, а не забыт.

    scripts/check_ui_inventory.py            # сверить, красное при расхождении
    scripts/check_ui_inventory.py --write    # записать текущий состав в список
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
STATIC = os.path.join(ROOT, "app", "static")
INVENTORY = os.path.join(ROOT, "docs", "ui-inventory.json")

#: Метка — класс, в имени которого есть одно из этих слов.
BADGE_WORDS = ("tag", "chip", "pill", "badge", "dot")

#: Следующий атрибут тега: на нём значение class заведомо закончилось.
NEXT_ATTR = re.compile(
    r"\s(?:data-[a-z-]+|title|aria-[a-z-]+|type|style|id|href|disabled|value"
    r"|placeholder|role|tabindex|name|for|checked|selected)\b|/?>")

#: Литерал, который дописывают к уже заданным классам: пробел и слова.
#: Ведущий пробел здесь обязателен и отделяет класс от значения сравнения:
#: в `srt.d === "a" ? " on" : ""` классом является только « on».
CLASS_LIST = re.compile(r"^\s+[a-z][a-z0-9_-]*(?:\s+[a-z][a-z0-9_-]*)*\s*$")


def spliced_classes(tail):
    """Классы из выражения, подставленного в значение атрибута `class`.

    Значение живёт внутри строки JavaScript в одинарных кавычках, поэтому
    после её закрытия идёт выражение, а классы в нём записаны двойными
    кавычками: `class="mi' + (row.danger ? " danger" : "") + '"`. Имя
    переменной классом не является и сюда не попадает: берутся только целые
    литералы, похожие на список классов.
    """
    stop = NEXT_ATTR.search(tail)
    region = tail[:stop.start()] if stop else tail[:200]
    out = set()
    for expression in re.findall(r"'([^']*)'", region):
        for literal in re.findall(r"\"([^\"]*)\"", expression):
            if CLASS_LIST.match(literal):
                out.update(literal.split())
    return out


def button_classes(text):
    """Классы кнопок и число мест, где класс вычисляется выражением."""
    names, dynamic = set(), 0
    for found in re.finditer(r"<button\b", text):
        head = text[found.end():found.end() + 400]
        # Границей служит конец самого тега: у кнопки без класса поиск иначе
        # доходил до соседнего элемента и записывал в кнопки чужой класс
        # (`<div class="empty">` после `<button data-issue-back>`).
        closing = head.find(">")
        at = head[:closing if closing >= 0 else len(head)].find("class=")
        if at < 0:
            continue
        quote = head[at + 6:at + 7]
        if quote not in "\"'":
            continue
        rest = head[at + 7:]
        end = rest.find(quote)
        literal = rest[:end] if end >= 0 else rest
        splice = literal.find("'")
        if splice >= 0:
            # Дальше идёт выражение: видимая часть кончилась, пробел засчитан.
            dynamic += 1
            names.update(spliced_classes(rest[splice:]))
            literal = literal[:splice]
        names.update(literal.split())
    return names, dynamic


def badge_classes(text):
    """Классы CSS, которые по имени относятся к меткам."""
    return {name for name in re.findall(r"\.([a-zA-Z][a-zA-Z0-9_-]*)", text)
            if any(word in name.lower() for word in BADGE_WORDS)}


def read(path):
    with io.open(path, encoding="utf-8-sig") as stream:
        return stream.read()


def sources(*patterns):
    """Файлы интерфейса по маскам, в устойчивом порядке."""
    found = []
    for pattern in patterns:
        found += glob.glob(os.path.join(STATIC, pattern))
    return sorted(found, key=lambda p: os.path.basename(p))


def collect():
    """Текущий состав примитивов по исходникам интерфейса."""
    buttons, dynamic, badges = set(), 0, set()
    for path in sources("*.html", "*.js"):
        names, count = button_classes(read(path))
        buttons |= names
        dynamic += count
    for path in sources("*.css"):
        badges |= badge_classes(read(path))
    return {"кнопки": sorted(buttons), "метки": sorted(badges),
            "вычисляемых_мест": dynamic}


def load():
    with io.open(INVENTORY, encoding="utf-8") as stream:
        data = json.load(stream)
    if data.get("schema_version") != 1:
        raise ValueError("ожидался schema_version=1")
    for key in ("кнопки", "метки"):
        if not isinstance(data.get(key), list):
            raise ValueError("ожидался список «%s»" % key)
    return data


def compare(declared, actual):
    """Расхождения списка и кода: чего не объявляли и что пропало."""
    problems = []
    for key in ("кнопки", "метки"):
        have, want = set(actual[key]), set(declared[key])
        for name in sorted(have - want):
            problems.append("%s: класс «%s» есть в коде, но не объявлен в %s"
                            % (key, name, os.path.relpath(INVENTORY, ROOT).replace(os.sep, "/")))
        for name in sorted(want - have):
            problems.append("%s: класс «%s» объявлен, но в коде его нет" % (key, name))
    return problems


def write(actual):
    data = load()
    data.update({key: actual[key] for key in ("кнопки", "метки")})
    data["вычисляемых_мест"] = actual["вычисляемых_мест"]
    with io.open(INVENTORY, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="записать текущий состав в список (осознанное решение человека)")
    args = ap.parse_args()
    actual = collect()
    if args.write:
        write(actual)
        print("записан состав: кнопок %d, меток %d"
              % (len(actual["кнопки"]), len(actual["метки"])))
        return 0
    try:
        declared = load()
    except (OSError, ValueError) as exc:
        print("инвентарь не прочитан: %s" % exc)
        return 1
    problems = compare(declared, actual)
    print("кнопок %d, меток %d, мест с вычисляемым классом %d"
          % (len(actual["кнопки"]), len(actual["метки"]), actual["вычисляемых_мест"]))
    for problem in problems:
        print("  " + problem)
    if problems:
        print("\nНовый примитив вносится решением: сначала причина в docs/ИНТЕРФЕЙС.md "
              "(UI-007), затем scripts/check_ui_inventory.py --write")
        return 1
    print("состав совпадает с docs/ui-inventory.json")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
