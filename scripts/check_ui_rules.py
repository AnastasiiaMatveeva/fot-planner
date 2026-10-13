# -*- coding: utf-8 -*-
"""UI-008: проверяемая часть правил интерфейса — красным до показа человеку.

Правила доступности и форм жили в обзоре: их применяли, когда о них вспоминали.
Работа владельца продукта из-за этого сводилась к «убери, исправь, удали» —
нарушение доходило до него, потому что до него его никто не ловил.

Здесь взята механически проверяемая часть Web Interface Guidelines (Vercel) —
та, где нарушение видно из текста и не требует суждения о вкусе. Остальное
(композиция, плотность, смысл) остаётся в `docs/ИНТЕРФЕЙС.md` и в обзоре
человеком: автомат про это врать не должен.

Проверка запускается не по команде, а сама: хуком после каждой правки
`app/static` и частью приёмки. Смысл в том, чтобы агент упёрся в правило до
того, как покажет результат.

Накопленный долг лежит в `docs/ui-debt.json` и считается принятым: иначе
первый же запуск красит всё и проверку выключают. Долг привязан не к строке,
а к отпечатку самого места — перенос кода его не теряет и не удваивает.
Новое нарушение красное всегда; счётчик долга виден в выводе и должен падать.

    scripts/check_ui_rules.py            # сверить, красное при новом нарушении
    scripts/check_ui_rules.py --write    # принять текущее состояние как долг
"""
from __future__ import annotations

import argparse
import collections
import glob
import hashlib
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
STATIC = os.path.join(ROOT, "app", "static")
DEBT = os.path.join(ROOT, "docs", "ui-debt.json")

#: Правило → что оно означает человеку.
RULES = {
    "кнопка-без-имени": "кнопка только со значком без aria-label: диктор её не назовёт",
    "поле-без-метки": "поле без aria-label и без id: не с чем связать подпись",
    "поле-без-типа": "поле без type: нет нужной клавиатуры и проверки ввода",
    "рисунок-без-размера": "img без width и height: страница дёргается при загрузке",
    "рисунок-без-alt": "img без alt",
    "ссылка-как-действие": "ссылка на решётку: действие должно быть button",
    "подсказка-без-многоточия": "placeholder не заканчивается многоточием",
    "три-точки": "три точки вместо знака многоточия",
    "фокус-снят": "outline снят на :focus без замены",
    "переход-all": "transition: all вместо перечисления свойств",
    "движение-без-оглядки": "бесконечная анимация вне prefers-reduced-motion",
    "масштаб-запрещён": "user-scalable=no или maximum-scale: запрещает увеличение",
}

TAG_WINDOW = 400
TEXT_WINDOW = 800


def read(path):
    with io.open(path, encoding="utf-8-sig") as stream:
        return stream.read()


def sources(*patterns):
    found = []
    for pattern in patterns:
        found += glob.glob(os.path.join(STATIC, pattern))
    return sorted(found, key=lambda p: os.path.basename(p))


def rel(path):
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def line_of(text, index):
    return text.count("\n", 0, index) + 1


def fingerprint(snippet):
    """Отпечаток места, не зависящий от переносов и отступов.

    К номеру строки привязываться нельзя: любая правка выше сдвигает номера,
    и весь принятый долг превращается в новые нарушения.
    """
    squeezed = re.sub(r"\s+", " ", snippet).strip()
    return hashlib.sha1(squeezed.encode("utf-8")).hexdigest()[:12]


def tag_window(text, start):
    """Текст открывающего тега: до закрывающей скобки или до предела."""
    head = text[start:start + TAG_WINDOW]
    closing = head.find(">")
    return head[:closing] if closing >= 0 else head


def has(attr, window):
    return re.search(r"\b" + attr + r"\s*=", window) is not None


def wrapped_in_label(text, start):
    """Подпись может оборачивать контрол, а не ссылаться на него по id.

    `<label class="fld">Группировка<select …></label>` — правильная разметка:
    подпись кликабельна и связана. Без этой поблажки проверка требовала бы
    лишний aria-label там, где связь уже есть.
    """
    before = text[max(0, start - 300):start]
    return before.rfind("<label") > before.rfind("</label")


def literals(text):
    """Строковые литералы файла: смещение и содержимое.

    Нужно, чтобы `...` искалось в тексте для человека, а не в коде: `[...rows]`
    — это расширение массива, а не многоточие в подписи.
    """
    pattern = r"'([^'\\\n]*(?:\\.[^'\\\n]*)*)'|\"([^\"\\\n]*(?:\\.[^\"\\\n]*)*)\""
    for found in re.finditer(pattern, text):
        yield found.start(), found.group(1) or found.group(2) or ""


def button_text(text, start, window):
    """Видимая подпись кнопки или None, если её собирает выражение."""
    after = text[start + len(window):start + len(window) + TEXT_WINDOW]
    end = after.find("</button")
    if end < 0:
        return None
    body = after[:end]
    if "' +" in body or '" +' in body:
        return None  # подпись вычисляется: судить о ней по тексту нельзя
    return re.sub(r"<[^>]*>|&[a-z]+;|&#\d+;", " ", body)


def check_markup(path, text):
    """Правила разметки: кнопки, поля, рисунки, ссылки, подсказки."""
    out = []

    def add(rule, index, snippet):
        out.append((rule, line_of(text, index), fingerprint(snippet)))

    for found in re.finditer(r"<button\b", text):
        window = tag_window(text, found.start())
        if has("aria-label", window):
            continue
        label = button_text(text, found.start(), window)
        if label is not None and not re.search(r"[0-9A-Za-zА-Яа-яЁё]", label):
            add("кнопка-без-имени", found.start(), window)

    for found in re.finditer(r"<(input|select|textarea)\b", text):
        window = tag_window(text, found.start())
        if re.search(r"\bhidden\b", window):
            continue  # скрытое поле не показывается и вспомогательной технике
        if found.group(1) == "input" and not has("type", window):
            add("поле-без-типа", found.start(), window)
        if not (has("aria-label", window) or has("id", window)
                or wrapped_in_label(text, found.start())):
            add("поле-без-метки", found.start(), window)
        placeholder = re.search(r'placeholder\s*=\s*"([^"]*)"', window)
        if placeholder and placeholder.group(1) \
                and not placeholder.group(1).rstrip().endswith("…"):
            add("подсказка-без-многоточия", found.start(), window)

    for found in re.finditer(r"<img\b", text):
        window = tag_window(text, found.start())
        if not has("alt", window):
            add("рисунок-без-alt", found.start(), window)
        if not (has("width", window) and has("height", window)):
            add("рисунок-без-размера", found.start(), window)

    for found in re.finditer(r'<a\b[^>]{0,200}?href\s*=\s*"#"', text):
        add("ссылка-как-действие", found.start(), found.group(0))

    # В разметке весь текст виден человеку, в коде — только строковые литералы.
    spans = ([(0, text)] if path.endswith(".html") else list(literals(text)))
    for offset, value in spans:
        for found in re.finditer(r"\.\.\.", value):
            add("три-точки", offset + found.start(),
                value[max(0, found.start() - 30):found.start() + 10])

    for found in re.finditer(r"user-scalable\s*=\s*no|maximum-scale\s*=", text):
        add("масштаб-запрещён", found.start(), found.group(0))

    return out


def check_style(path, text):
    """Правила стилей: фокус, переходы, бесконечное движение."""
    out = []

    def add(rule, index, snippet):
        out.append((rule, line_of(text, index), fingerprint(snippet)))

    # Что уже прикрыто оглядкой на предпочтение пользователя.
    calm = " ".join(re.findall(
        r"@media[^{]*prefers-reduced-motion[^{]*\{(.*?)\}\s*\}", text, re.S))

    for found in re.finditer(r"([^{}]+)\{([^}]*)\}", text):
        selector, body = found.group(1).strip(), found.group(2)
        place = selector + "{" + body + "}"
        # Захват начинается на переносе после прошлого правила: без сдвига
        # на первый непробельный символ ссылка ведёт строкой выше.
        found_start = found.start(1) + len(found.group(1)) - len(found.group(1).lstrip())
        if re.search(r"outline\s*:\s*(0|none)\b", body) and ":focus" in selector \
                and ":focus-visible" not in selector and "box-shadow" not in body:
            add("фокус-снят", found_start, place)
        if re.search(r"transition\s*:[^;]*\ball\b", body):
            add("переход-all", found_start, place)
        if "infinite" in body:
            # Селектор считается прикрытым, если его класс упомянут в блоке
            # prefers-reduced-motion: точное сопоставление тут невозможно.
            names = re.findall(r"[.#]([A-Za-z][\w-]*)", selector)
            if not names or names[-1] not in calm:
                add("движение-без-оглядки", found_start, place)
    return out


def collect():
    """Все нарушения: правило, файл, строка, отпечаток места."""
    found = []
    for path in sources("*.html", "*.js"):
        found += [(rule, rel(path), line, mark)
                  for rule, line, mark in check_markup(path, read(path))]
    for path in sources("*.css"):
        found += [(rule, rel(path), line, mark)
                  for rule, line, mark in check_style(path, read(path))]
    return sorted(found, key=lambda row: (row[1], row[2]))


def key(rule, path, mark):
    return "%s|%s|%s" % (rule, path, mark)


def load():
    with io.open(DEBT, encoding="utf-8") as stream:
        data = json.load(stream)
    if data.get("schema_version") != 1 or not isinstance(data.get("долг"), dict):
        raise ValueError("ожидались schema_version=1 и словарь «долг»")
    return data


def split(found, debt):
    """Разделить найденное на принятый долг и новое нарушение."""
    left = collections.Counter(debt)
    fresh, old = [], []
    for rule, path, line, mark in found:
        entry = key(rule, path, mark)
        if left[entry] > 0:
            left[entry] -= 1
            old.append((rule, path, line, mark))
        else:
            fresh.append((rule, path, line, mark))
    return fresh, old, {k: n for k, n in left.items() if n > 0}


def write(found):
    data = load() if os.path.exists(DEBT) else {"schema_version": 1}
    counts = collections.Counter(key(rule, path, mark) for rule, path, _, mark in found)
    data["долг"] = dict(sorted(counts.items()))
    data["всего"] = sum(counts.values())
    with io.open(DEBT, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def report(rows):
    """Вывод в виде путь:строка — так место открывается щелчком."""
    lines = []
    for path in sorted({row[1] for row in rows}):
        lines.append("")
        for rule, file_path, line, _ in rows:
            if file_path == path:
                lines.append("%s:%d — %s" % (path, line, RULES.get(rule, rule)))
    return "\n".join(lines).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="принять текущее состояние как долг (решение человека)")
    args = ap.parse_args()
    found = collect()
    if args.write:
        write(found)
        print("принято в долг: %d нарушений" % len(found))
        return 0
    try:
        debt = load()["долг"]
    except (OSError, ValueError) as exc:
        print("долг не прочитан: %s" % exc)
        return 1
    fresh, old, gone = split(found, debt)
    print("нарушений всего %d: новых %d, принятых в долг %d"
          % (len(found), len(fresh), len(old)))
    if gone:
        print("долг уменьшился на %d — запиши это: scripts/check_ui_rules.py --write"
              % sum(gone.values()))
    if fresh:
        print("\n" + report(fresh))
        print("\nПравило интерфейса нарушено новым кодом. Исправить, а не вносить в долг.")
        return 1
    print("новых нарушений нет")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
