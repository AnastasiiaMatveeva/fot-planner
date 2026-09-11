# -*- coding: utf-8 -*-
"""Общая часть приёмки: что чем проверяется, что изменено, что красное давно.

Отдельный модуль появился потому, что ворота на Stop и команда приёмки
считали это по-своему, и таблицы разъезжались: ворота уже не запускали часть,
которую команда ещё гоняла. Теперь правда одна и лежит здесь.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
KNOWN = os.path.join(ROOT, "harness", "known_red.txt")
VENV = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
#: Стендам нужны зависимости проекта, а позвать скрипт могут системным Python.
PY = VENV if os.path.exists(VENV) else sys.executable

#: Часть приёмки → чем запускается → какие правки её касаются.
#: Часть без списка файлов касается всего и в выборке по изменениям не участвует.
PARTS = (
    ("вердикт приёмки", ["scripts/test_check_results.py"],
     ("scripts/check.py", "scripts/check_results.py", "scripts/test_check_results.py",
      "scripts/parts.py", "scripts/stop_gate.py", "harness/known_red.txt")),
    ("карта требований", ["scripts/check_requirements_map.py"],
     ("docs/requirements-map.json", "docs/ТЗ_РАЗВИТИЕ_АГЕНТОВ.md",
      "docs/ПРАВИЛА_РЕШАТЕЛЯ.md", "app/agents.py", "harness/crisis.py",
      "harness/audit.py", "harness/chat.py", "scripts/check_requirements_map.py")),
    ("хуки правок", ["scripts/test_lint_changed.py"],
     (".claude/settings.json", "scripts/lint_changed.py",
      "scripts/test_lint_changed.py")),
    ("разбор документов", ["harness/run.py"],
     ("app/docread.py", "app/intake.py", "app/reference.py", "app/llm.py",
      "harness/run.py", "harness/cases.py")),
    ("поведение чата", ["harness/chat.py"],
     ("app/chat.py", "app/main.py", "harness/chat.py")),
    ("проверка результата", ["harness/audit.py"],
     ("src/fot_planner/result_audit.py", "src/fot_planner/models.py",
      "harness/audit.py")),
    ("список замечаний", ["node", "scripts/test-issues.cjs"],
     ("app/static/issues.js", "app/static/issues-demo.js", "app/static/app.js",
      "scripts/test-issues.cjs")),
    ("правила решателя", ["harness/crisis.py", "--limit", "60"],
     ("src/fot_planner/", "app/build_input.py", "app/rules.py",
      "harness/crisis.py")),
)


def command(args):
    """Команда части приёмки.

    Стенды на Python идут интерпретатором проекта, остальное — как записано:
    проверка списка замечаний живёт на Node. Отдельная функция нужна, чтобы
    ворота и команда приёмки запускали части одинаково.
    """
    args = list(args)
    return [PY] + args if args and str(args[0]).endswith(".py") else args


class GitUnavailable(RuntimeError):
    """Git не отвечает: какие файлы изменены — неизвестно.

    Раньше ошибка уходила в `/dev/null`, список изменений выходил пустым, и
    ворота считали, что проверять нечего. Молчание в эту сторону недопустимо:
    незнание — повод прогнать всё, а не повод пропустить.
    """


def changed_files():
    """Файлы, изменённые с последнего коммита, включая новые."""
    try:
        p = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                           cwd=ROOT, text=True, encoding="utf-8", errors="replace",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:  # git не установлен или недоступен
        raise GitUnavailable(str(e)) from e
    if p.returncode != 0:
        raise GitUnavailable((p.stderr or "").strip() or "git вернул ошибку")
    out = []
    for line in (p.stdout or "").splitlines():
        name = line[3:].strip().strip('"')
        # Переименование: «старое -> новое», значение имеет новое имя.
        if " -> " in name:
            name = name.split(" -> ")[-1]
        if name:
            out.append(name.replace("\\", "/"))
    return out


def touched(files):
    """Части приёмки, которых касаются эти правки."""
    return [(title, args) for title, args, watch in PARTS
            if any(f.startswith(w) or f == w for f in files for w in watch)]


def known_red():
    """Случаи, которые падают по разобранной причине: номер → (причина, дата)."""
    out = {}
    try:
        with io.open(KNOWN, encoding="utf-8") as file:
            text = file.read()
    except OSError:
        return out
    num = None
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        head = re.match(r"(\S+)\s+(\S.*)", line)
        if head and not line.startswith((" ", "\t")):
            num = head.group(1)
            out[num] = [head.group(2).strip(), None]
        elif num and line.strip():
            out[num][0] += " " + line.strip()
    for num, row in out.items():
        found = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", row[0])
        row[1] = date(int(found.group(3)), int(found.group(2)),
                      int(found.group(1))) if found else None
    return {k: tuple(v) for k, v in out.items()}


def known_red_report():
    """Строки для вывода: что красное давно и сколько дней оно там лежит."""
    rows = []
    for num, (reason, since) in sorted(known_red().items()):
        days = (date.today() - since).days if since else None
        rows.append("  %-4s %s%s" % (
            num, reason[:96],
            "" if days is None else "  [лежит %d дн.]" % days))
    return rows


def failed_cases(output):
    """Номера случаев, которые стенд назвал провалившимися."""
    return re.findall(r"^\[(\S+)\]\s+ПРОВАЛ", output, flags=re.M)
