# -*- coding: utf-8 -*-
"""Приёмка: один вход, один вердикт.

Обвязка в проекте уже есть, но её части запускались руками и по отдельности,
поэтому правка могла уйти в работу непроверенной. Здесь они собраны в одну
команду с общим итогом и кодом возврата.

Три скорости, по цене:

    scripts/check.py --changed   # только то, чего коснулась правка
    scripts/check.py --quick     # всё без модели, около минуты
    scripts/check.py             # плюс разбор документов моделью, минуты
    scripts/check.py --full      # плюс сквозной счёт демо-набора

Что гоняется:

* «хуки правок» — `scripts/test_lint_changed.py`: мгновенная проверка после
  Edit/Write и команд оболочки, включая неуспешные;
* «разбор» — `harness/run.py`: документы экономистов читаются в реестр, строки
  сверяются с ожиданиями (`harness/cases.py`);
* «чат» — `harness/chat.py`: что сервис делает с репликой экономиста; ответ
  модели подставляется записью, поэтому проверка идёт секунды и без сети;
* «проверка результата» — `harness/audit.py`: аудит готового плана по
  карточкам правил, миллисекунды;
* «список замечаний» — `scripts/test-issues.cjs` на Node: контракт замечания и
  переход к строке отчёта;
* «правила» — `harness/crisis.py`: тридцать маленьких задач, по одной на
  правило решателя (2556, П4, БЭП, 120, 152, приоритет, замещение, ставки);
* «сквозной» — `scripts/demo_offline.py`: демо-набор считается целиком, без
  сервиса и модели, и проверяется, что план сходится.

Известный провал остаётся failed. Разработческий режим может дать
baseline_compatible; --strict отклоняет и известные провалы.
--report PATH сохраняет JSON без содержимого документов и вывода стендов.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parts as P  # noqa: E402
import check_results as R  # noqa: E402

ROOT = P.ROOT


def run(title, args, known, *, strict=False):
    """Прогнать часть и сохранить измеренные факты отдельно от вердикта."""
    t0 = time.monotonic()
    started = datetime.now(timezone.utc).isoformat()
    command = P.command(args)
    # Это hash только точки входа, не версии всех транзитивных зависимостей.
    entry = Path(ROOT) / args[0]
    entry_hash = (hashlib.sha256(entry.read_bytes()).hexdigest()
                  if entry.suffix in {".py", ".cjs", ".js"} and entry.is_file() else None)
    e = dict(os.environ)
    # Скрипты импортируют app/main.py; без этого импорт помечает живой расчёт
    # сервера «прерванным».
    e["FOT_SKIP_ORPHANS"] = "1"
    e["PYTHONIOENCODING"] = "utf-8"
    print("── %s ─────────────────────────────" % title, flush=True)
    try:
        p = subprocess.run(command, cwd=ROOT, env=e, text=True,
                           encoding="utf-8", errors="replace",
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as exc:
        # Нет исполнителя (например, Node): часть не проверена. Это провал, а
        # не пропуск — иначе приёмка молча позеленеет без проверки.
        print("не запустилось: %s" % exc, flush=True)
        returncode, out = None, ""
    else:
        returncode, out = p.returncode, p.stdout or ""
    print(out.rstrip(), flush=True)
    return {**R.assess(args, returncode, out, known, strict=strict),
            "title": title, "command": command, "started_at": started,
            "duration_seconds": round(time.monotonic() - t0, 4),
            "returncode": returncode, "entrypoint_sha256": entry_hash,
            "stdout_sha256": hashlib.sha256(out.encode("utf-8")).hexdigest()}


def write_report(path, report):
    """Старый отчёт не должен заменяться наполовину записанным JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=".check-", suffix=".tmp", delete=False) as file:
            temp = Path(file.name)
            json.dump(report, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temp, target)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--changed", action="store_true",
                    help="только части, которых коснулись изменения с последнего коммита")
    ap.add_argument("--quick", action="store_true",
                    help="всё, кроме разбора документов: минута, без обращений к модели")
    ap.add_argument("--full", action="store_true",
                    help="добавить сквозной счёт демо-набора (минуты)")
    ap.add_argument("--only", default="", help="часть имени документа для стенда разбора")
    ap.add_argument("--limit", type=int, default=240, help="лимит решателя на стадию, с")
    ap.add_argument("--strict", action="store_true", help="известные провалы тоже блокируют приёмку")
    ap.add_argument("--report", help="путь к JSON-отчёту текущего запуска")
    args = ap.parse_args()

    parts = [(t, list(a)) for t, a, _ in P.PARTS]
    if args.changed:
        try:
            files = P.changed_files()
        except P.GitUnavailable as e:
            # Незнание — повод проверить всё, а не повод пропустить.
            print("git не ответил (%s): проверяется всё\n" % e, flush=True)
        else:
            parts = [(t, list(a)) for t, a in P.touched(files)]
            # Скорость этого режима — секунды после каждой правки, поэтому
            # разбор документов сюда не берётся: он зовёт модель и идёт
            # минуты. Его место — прогон перед коммитом.
            if any(t == "разбор документов" for t, _ in parts) and not args.full:
                parts = [(t, a) for t, a in parts if t != "разбор документов"]
                print("разбор документов пропущен: зовёт модель. "
                      "Перед коммитом — scripts/check.py\n", flush=True)
            if not parts:
                print("изменений в проверяемых местах нет")
    # Даже при недоступном git --quick не должен неожиданно вызвать модель.
    if args.quick or (args.changed and not args.full):
        parts = [(t, a) for t, a in parts if t != "разбор документов"]
    if args.only:
        parts = [(t, a + ["--only", args.only] if t == "разбор документов" else a)
                 for t, a in parts]
    if args.full and not args.quick:
        out = os.path.join(ROOT, "outputs", "check")
        os.makedirs(out, exist_ok=True)
        parts.append(("сквозной счёт демо-набора",
                      ["scripts/demo_offline.py", "--limit", str(args.limit),
                       "--out", out]))

    known = P.known_red()
    rows = [run(t, a, known, strict=args.strict) for t, a in parts]
    print("\n══ приёмка ══")
    for row in rows:
        print("  %-28s %-20s %5.0f с  %s" % (
            row["title"], row["acceptance_status"], row["duration_seconds"], row["reason"]))
    red = P.known_red_report()
    if red:
        print("\nкрасное по разобранной причине, ждёт человека:")
        print("\n".join(red))
    status = R.overall(rows)
    exit_code = 0 if status == "passed" or (not args.strict and (
        status == "baseline_compatible" or not rows)) else 1
    print("\nитог: %s" % status)
    if args.report:
        report = {"schema_version": 1, "run_id": str(uuid.uuid4()),
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "mode": "strict" if args.strict else "developer",
                  "selection": {key: getattr(args, key) for key in ("quick", "changed", "full", "only", "limit")},
                  "environment": {"python": platform.python_version(), "platform": platform.platform()},
                  "scope": "selected existing suites; not full capability qualification",
                  "acceptance_status": status, "exit_code": exit_code, "suites": rows}
        try:
            write_report(args.report, report)
        except OSError as exc:
            print("Не удалось сохранить отчёт: %s" % exc, file=sys.stderr)
            return 1
    return exit_code


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
