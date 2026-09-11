# -*- coding: utf-8 -*-
"""Проверка правки сразу после неё: синтаксис Python и JavaScript.

Хуки `PostToolUse` и `PostToolUseFailure` следят за Edit, Write, Bash и
PowerShell. Смысл не в стиле, а в том, чтобы сломанный файл не дожил до конца
задачи: агент правит текстом, и опечатка в скобке обнаруживалась только при
следующем запуске приложения — иногда через полчаса и десяток правок.

Для Edit/Write проверяется путь из события. Команда оболочки пути изменённых
файлов не сообщает, поэтому после неё проверяются все изменения из Git. Это
ловит и случай, когда Python-скрипт успел записать файл, а затем завершился с
ошибкой. Код возврата 2 возвращает агенту текст ошибки, всё остальное молчит.
"""
from __future__ import annotations

import ast
import io
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parts as P  # noqa: E402


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


def _problem(path):
    """Вернуть описание синтаксической ошибки поддерживаемого файла."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".py":
        return check_python(path)
    if ext in (".js", ".cjs"):
        return check_js(path)
    if ext == ".json":
        try:
            json.load(io.open(path, encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            return "разбор JSON: %s" % str(e)[:200]
    return None


def _changed_paths():
    """Получить абсолютные пути изменённых файлов после команды оболочки."""
    try:
        names = P.changed_files()
    except P.GitUnavailable as e:
        raise RuntimeError("Git не ответил, изменения после Bash не проверены: %s"
                           % str(e)[:200]) from e
    root = os.path.normcase(os.path.abspath(P.ROOT))
    out = []
    for name in names:
        path = os.path.abspath(os.path.join(P.ROOT, name.replace("/", os.sep)))
        try:
            inside = os.path.commonpath((root, os.path.normcase(path))) == root
        except ValueError:
            inside = False
        if inside:
            out.append(path)
    return out


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 — без события проверять нечего
        return 0
    tool = event.get("tool_name") or ""
    path = ((event.get("tool_input") or {}).get("file_path")
            or (event.get("tool_response") or {}).get("filePath") or "")
    if path:
        if not os.path.isabs(path):
            path = os.path.join(event.get("cwd") or P.ROOT, path)
        paths = [os.path.abspath(path)]
    elif tool in ("Bash", "PowerShell"):
        try:
            paths = _changed_paths()
        except RuntimeError as e:
            _say(str(e) + "\n")
            return 2
    else:
        return 0

    problems = []
    for current in dict.fromkeys(paths):
        if not os.path.isfile(current):
            continue
        problem = _problem(current)
        if problem:
            problems.append("%s — %s" % (os.path.basename(current), problem))
    if problems:
        _say("\n".join(problems) + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
