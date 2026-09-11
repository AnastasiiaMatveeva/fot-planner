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

Случай из `harness/known_red.txt` остаётся красным в выводе, но вердикт не
портит: причина у него разобрана и ждёт человека. Рядом печатается, сколько
дней он там лежит, — чтобы список разбирали, а не копили.

Код возврата 1, если хоть одна часть не прошла по новой причине.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parts as P  # noqa: E402

ROOT = P.ROOT


def run(title, args, known):
    """Прогнать часть обвязки. Возвращает (заголовок, ок, секунды, хвост)."""
    t0 = time.time()
    e = dict(os.environ)
    # Скрипты импортируют app/main.py; без этого импорт помечает живой расчёт
    # сервера «прерванным».
    e["FOT_SKIP_ORPHANS"] = "1"
    e["PYTHONIOENCODING"] = "utf-8"
    print("── %s ─────────────────────────────" % title, flush=True)
    try:
        p = subprocess.run(P.command(args), cwd=ROOT, env=e, text=True,
                           encoding="utf-8", errors="replace",
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as exc:
        # Нет исполнителя (например, Node): часть не проверена. Это провал, а
        # не пропуск — иначе приёмка молча позеленеет без проверки.
        print("не запустилось: %s" % exc, flush=True)
        return title, False, time.time() - t0, "не запустилось: %s не найден" % args[0]
    out = p.stdout or ""
    print(out.rstrip(), flush=True)
    tail = [l for l in out.splitlines() if l.startswith("итого")] or \
           [l for l in out.splitlines() if l.strip()][-1:]
    bad = P.failed_cases(out)
    fresh = [b for b in bad if b not in known]
    # Часть считается прошедшей, если всё красное в ней — разобранное и
    # записанное. Стенд, упавший целиком, случаев не печатает: тогда верим
    # коду возврата.
    ok = (p.returncode == 0) or (bool(bad) and not fresh)
    note = "" if not bad or fresh else "; известных %d (%s)" % (
        len(bad), ", ".join(bad))
    return title, ok, time.time() - t0, (tail[-1] if tail else "") + note


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
                return 0
    elif args.quick:
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
    rows = [run(t, a, known) for t, a in parts]
    print("\n══ приёмка ══")
    for title, ok, sec, tail in rows:
        print("  %-28s %-9s %5.0f с  %s" % (title, "прошло" if ok else "НЕ ПРОШЛО", sec, tail))
    red = P.known_red_report()
    if red:
        print("\nкрасное по разобранной причине, ждёт человека:")
        print("\n".join(red))
    bad = [r for r in rows if not r[1]]
    print("\n%s" % ("всё прошло" if not bad else
                    "не прошло: " + ", ".join(r[0] for r in bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
