# -*- coding: utf-8 -*-
"""Ворота на конце задачи: новых поломок быть не должно.

Хук `Stop`. Слова в подсказке «всегда проверяй перед тем, как сказать готово»
— это надежда; хук — это условие.

Ворота смотрят не на «всё ли зелёное», а на «не стало ли хуже». Случаи,
которые падают по разобранной причине и ждут решения человека, перечислены в
`harness/known_red.txt`, и на них ворота не спотыкаются: недостижимое условие
приучает не читать сообщения. Всё остальное красное задачу закрывать не даёт.

Незнание — не повод пропустить. Если git не отвечает и какие файлы изменены
неизвестно, проверяется всё: раньше ошибка git уходила в никуда, список
изменений выходил пустым, и задача закрывалась вообще без единого прогона.

Чтобы не гонять стенд после каждой реплики, проверка запускается только
когда изменены файлы, от которых он зависит, и не чаще раза в десять минут.
Состояние — в `outputs/stop_gate.json`.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parts as P  # noqa: E402

STATE = os.path.join(P.ROOT, "outputs", "stop_gate.json")
PAUSE = 600  # секунд между прогонами
#: Разбор документов зовёт модель и идёт минуты — в ворота он не помещается.
SKIP = ("разбор документов",)


def _say(text):
    """Написать агенту. Поток читают как utf-8: кодировка консоли Windows
    превращала кириллицу в кашу из вопросительных знаков."""
    sys.stderr.buffer.write(text.encode("utf-8", "replace"))
    sys.stderr.flush()


def run(args):
    """Прогнать часть стенда. Возвращает (номера упавших случаев, строка итога)."""
    try:
        p = subprocess.run(P.command(args), cwd=P.ROOT, text=True,
                           encoding="utf-8", errors="replace",
                           env={**os.environ, "FOT_SKIP_ORPHANS": "1",
                                "PYTHONIOENCODING": "utf-8"},
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as exc:
        # Нет исполнителя — часть не проверена, и ворота об этом говорят.
        return ["не запустилось"], "%s не найден: %s" % (args[0], exc)
    out = p.stdout or ""
    tail = [l for l in out.splitlines() if l.startswith("итого")]
    if p.returncode != 0 and not tail:
        # Стенд упал целиком: это провал, а не «случаев нет».
        last = [l for l in out.splitlines() if l.strip()][-1:]
        return ["стенд упал целиком"], (last[0][:120] if last else "без вывода")
    return P.failed_cases(out), (tail[-1] if tail else "итога нет")


def load():
    try:
        return json.load(io.open(STATE, encoding="utf-8"))
    except Exception:  # noqa: BLE001 — первого запуска ещё не было
        return {}


def save(data):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    io.open(STATE, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False))


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        event = {}
    # Хук, вызванный из-за собственной остановки, второй раз не запускаем.
    if event.get("stop_hook_active"):
        return 0
    blind = ""
    try:
        chosen = P.touched(P.changed_files())
    except P.GitUnavailable as e:
        blind = str(e)
        chosen = [(t, a) for t, a, _ in P.PARTS]
    chosen = [(t, a) for t, a in chosen if t not in SKIP]
    if not chosen:
        return 0
    state = load()
    if time.time() - float(state.get("at") or 0) < PAUSE and state.get("ok"):
        return 0

    known, fresh, lines = P.known_red(), [], []
    for title, args in chosen:
        bad, tail = run(args)
        fresh += ["%s: %s" % (title, b) for b in bad if b not in known]
        old = [b for b in bad if b in known]
        lines.append("  %-18s %s%s" % (
            title, tail, "" if not old else "; из них известных %d (%s)"
            % (len(old), ", ".join(old))))
    save({"at": time.time(), "ok": not fresh})
    if not fresh:
        return 0
    _say("Приёмка нашла новые поломки, задача не закончена:\n%s\n%s\n%s"
         "Разбор: .venv/Scripts/python.exe scripts/check.py --changed\n"
         % ("\n".join("  " + f for f in fresh), "\n".join(lines),
            "" if not blind else
            "Git не ответил (%s), поэтому проверено всё подряд.\n" % blind[:120]))
    return 2


if __name__ == "__main__":
    sys.exit(main())
