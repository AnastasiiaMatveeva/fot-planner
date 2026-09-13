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
когда изменены файлы, от которых он зависит. Кеш на десять минут применим
только к тому же содержимому дерева и тому же набору команд.
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
import check_results as R  # noqa: E402
import repo_state as S  # noqa: E402

STATE = os.path.join(P.ROOT, "outputs", "stop_gate.json")
PAUSE = 600  # секунд между прогонами
#: Разбор документов зовёт модель и идёт минуты — в ворота он не помещается.
SKIP = ("разбор документов",)


def _say(text):
    """Написать агенту. Поток читают как utf-8: кодировка консоли Windows
    превращала кириллицу в кашу из вопросительных знаков."""
    sys.stderr.buffer.write(text.encode("utf-8", "replace"))
    sys.stderr.flush()


def run(args, known):
    """Тот же разработческий вердикт, что у CLI; ошибки не списываются."""
    try:
        p = subprocess.run(P.command(args), cwd=P.ROOT, text=True,
                           encoding="utf-8", errors="replace",
                           env={**os.environ, "FOT_SKIP_ORPHANS": "1",
                                "PYTHONIOENCODING": "utf-8"},
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as exc:
        # Нет исполнителя — часть не проверена, и ворота об этом говорят.
        return R.assess(args, None, "", known)
    out = p.stdout or ""
    return R.assess(args, p.returncode, out, known)


def load():
    try:
        with io.open(STATE, encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — первого запуска ещё не было
        return {}


def save(data):
    from check import write_report
    write_report(STATE, data)


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
    try:
        fingerprint = S.fingerprint(P.ROOT, chosen)
    except S.RepositoryError as exc:
        _say("Не удалось закрепить проверяемое состояние: %s\n" % exc)
        return 2
    state = load()
    try:
        recent = 0 <= time.time() - float(state.get("at") or 0) < PAUSE
    except (TypeError, ValueError):
        recent = False
    if (not blind and state.get("fingerprint") == fingerprint
            and recent and state.get("ok") is True):
        return 0

    known, fresh, lines = P.known_red(), [], []
    for title, args in chosen:
        result = run(args, known)
        status = result["acceptance_status"]
        if status not in {"passed", "baseline_compatible"}:
            fresh.append("%s: %s" % (title, result["reason"]))
        lines.append("  %-18s %s: %s" % (title, status, result["reason"]))
    try:
        if S.fingerprint(P.ROOT, chosen) != fingerprint:
            fresh.append("файлы изменились во время приёмки: нужен новый прогон")
    except S.RepositoryError as exc:
        fresh.append("состояние после проверки неизвестно: %s" % exc)
    try:
        save({"at": time.time(), "ok": not fresh, "fingerprint": fingerprint})
    except OSError as exc:
        _say("Не удалось сохранить состояние приёмки: %s\n" % exc)
        return 2
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
