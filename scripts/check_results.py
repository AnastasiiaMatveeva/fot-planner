# -*- coding: utf-8 -*-
"""Общий вердикт CLI и Stop: известная ошибка не превращается в успех."""
from __future__ import annotations

import re

CASE_SCRIPTS = {"harness/crisis.py", "harness/chat.py", "harness/audit.py",
                "harness/lifecycle.py", "scripts/check_requirements_map.py"}
CASE = re.compile(r"^\[(\S+)\]\s+(ОК|ПРОВАЛ)(?:\s|$)", re.M)
SUMMARY = re.compile(r"^итого: (\d+) случаев, провалов (\d+)\s*$", re.M)


def assess(args, returncode, output, known, *, strict=False):
    """Оценить наблюдаемый протокол, не выдавая его за полноту требований.

    Три стенда публикуют отдельные случаи. У остальных пока только контракт
    процесса: непустой вывод и exit 0. Реестр known_red относится к crisis.
    """
    script = str(args[0]).replace("\\", "/") if args else ""
    scoped_known = known if script == "harness/crisis.py" else {}
    cases = [{"id": num, "verdict": "passed" if verdict == "ОК" else "failed",
              "known_reason": scoped_known[num][0] if num in scoped_known and verdict == "ПРОВАЛ" else None}
             for num, verdict in CASE.findall(output)]
    failed = [case for case in cases if case["verdict"] == "failed"]
    result = {"runtime_status": "completed", "evaluation_status": "not_run",
              "acceptance_status": "not_run", "cases": cases,
              "protocol": "cases-v1" if script in CASE_SCRIPTS else "process-exit",
              "reason": ""}

    def finish(status, reason, *, runtime_error=False):
        result["evaluation_status"] = status
        result["acceptance_status"] = status
        result["reason"] = reason
        if runtime_error:
            result["runtime_status"] = "error"
        return result

    if returncode is None:
        return finish("error", "исполнитель не запущен", runtime_error=True)
    if "Traceback (most recent call last):" in output:
        return finish("error", "необработанное исключение стенда", runtime_error=True)
    if script in CASE_SCRIPTS:
        summaries = SUMMARY.findall(output)
        if len(summaries) != 1:
            return finish("error", "нет единственного полного итога", runtime_error=True)
        total, count_failed = map(int, summaries[0])
        last = output.strip().splitlines()[-1]
        if (total != len(cases) or count_failed != len(failed)
                or len({case["id"] for case in cases}) != total
                or not SUMMARY.fullmatch(last)):
            return finish("error", "случаи и итог не согласованы", runtime_error=True)
        if returncode != (1 if failed else 0):
            return finish("error", "код возврата противоречит итогу", runtime_error=True)
        if total == 0:
            return finish("not_run", "стенд не выполнил ни одного случая")
    elif returncode != 0:
        return finish("error", "процесс завершился с ошибкой", runtime_error=True)
    elif failed:
        return finish("error", "код 0 при напечатанном провале", runtime_error=True)
    elif not output.strip():
        return finish("not_run", "нет свидетельства выполнения")

    if failed:
        finish("failed", "есть проваленные случаи")
        if not strict and all(case["known_reason"] for case in failed):
            result["acceptance_status"] = "baseline_compatible"
            result["reason"] = "только известные провалы; это не успешная проверка"
        return result
    return finish("passed", "заявленный протокол стенда выполнен")


def overall(rows):
    """Даже одна непроверенная выбранная часть не даёт общий passed."""
    statuses = {row["acceptance_status"] for row in rows}
    for status in ("error", "failed", "not_run", "baseline_compatible"):
        if status in statuses:
            return status
    return "passed" if rows else "not_run"
