# -*- coding: utf-8 -*-
"""Проверка правки сразу после неё: синтаксис Python и JavaScript.

Хук `PostToolUse` на Edit и Write. Смысл не в стиле, а в том, чтобы сломанный
файл не дожил до конца задачи: агент правит текстом, и опечатка в скобке
обнаруживалась только при следующем запуске приложения — иногда через
полчаса и десяток правок.

Читает событие хука из stdin, берёт путь файла, проверяет его. Код возврата 2
возвращает агенту текст ошибки, всё остальное молчит.
"""
from __future__ import annotations

import ast
import io
import json
import os
import shutil
import subprocess
import sys


def check_python(path):
    src = io.open(path, encoding="utf-8", errors="replace").read()
    try:
        ast.parse(src)
    except SyntaxError as e:
        return "синтаксис Python: строка %s, %s" % (e.lineno, e.msg)
    return None


def check_js(path):
    node = shutil.which("node")
    if not node:
        return None
    src = io.open(path, encoding="utf-8", errors="replace").read()
    # Файлы приложения — обычные скрипты, поэтому проверяем как тело функции:
    # так ловится опечатка, но не мешают обращения к window и document.
    code = "new Function(require('fs').readFileSync(process.argv[1],'utf8'))"
    p = subprocess.run([node, "-e", code, path], text=True, encoding="utf-8",
                       errors="replace", stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    if p.returncode != 0:
        first = [l for l in (p.stdout or "").splitlines() if l.strip()][:3]
        return "синтаксис JavaScript: " + " ".join(first)
    return None


def _say(text):
    """Написать агенту. Поток читают как utf-8: кодировка консоли Windows
    превращала кириллицу в кашу из вопросительных знаков."""
    sys.stderr.buffer.write(text.encode("utf-8", "replace"))
    sys.stderr.flush()


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 — без события проверять нечего
        return 0
    path = ((event.get("tool_input") or {}).get("file_path")
            or (event.get("tool_response") or {}).get("filePath") or "")
    if not path or not os.path.exists(path):
        return 0
    ext = os.path.splitext(path)[1].lower()
    problem = None
    if ext == ".py":
        problem = check_python(path)
    elif ext == ".js":
        problem = check_js(path)
    elif ext == ".json":
        try:
            json.load(io.open(path, encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            problem = "разбор JSON: %s" % str(e)[:200]
    if problem:
        _say("%s — %s\n" % (os.path.basename(path), problem))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
