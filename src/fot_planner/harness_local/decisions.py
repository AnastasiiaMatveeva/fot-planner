# -*- coding: utf-8 -*-
"""Локальное решение (LocalDecision): кто, что, над чем и на каком основании.

Принятая строка, записанная норма, исключённый документ, изменённое условие
плана — всё это решения человека, а не побочный эффект запроса. Раньше от них
оставались только следствия (строка в реестре, число в справочнике), и
спросить «кто это решил и над какой версией» было не у кого (VER-004).

Три правила модуля:

* действующее лицо — локальный оператор установки, а не строка из тела
  запроса или от модели: подпись «экономист» из клиента личностью не
  является (NFR-002);
* предмет решения закреплён хешем: решение относится к этим байтам документа
  и этим строкам, а не к имени файла;
* решение над устаревшей версией предмета не применяется молча — сравнение
  ожидаемого и текущего хеша делает вызывающий код, здесь только проверка.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os

KINDS = ("данные", "норма", "состав плана", "настройка плана")


def operator() -> tuple[str, str]:
    """Имя локального оператора и откуда оно взято.

    Однопользовательская установка: имя из FOT_OPERATOR, иначе учётная запись
    ОС. Многопользовательский режим с проверкой полномочий — не здесь.
    """
    name = (os.environ.get("FOT_OPERATOR") or "").strip()
    if name:
        return name, "FOT_OPERATOR"
    try:
        return getpass.getuser(), "учётная запись ОС"
    except Exception:  # noqa: BLE001 — без имени решение всё равно записывается
        return "неизвестный оператор", "не определён"


def digest(*parts) -> str:
    """Хеш предмета решения: JSON частей в устойчивом порядке."""
    text = json.dumps(list(parts), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_settings(settings: dict) -> str:
    """Хеш настроек плана: JSON с сортировкой ключей, как их читает стенд."""
    text = json.dumps(settings, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stale(expected: str | None, current: str | None) -> bool:
    """Форма показывала одну версию предмета, а сейчас другая.

    Без ожидания (старый клиент) проверить нечего — это не устаревание, но
    решение помечается как принятое без предусловия.
    """
    return bool(expected) and expected != current


def make(*, kind: str, subject_kind: str, subject_id, subject_digest: str,
         scope: str, action, grounds: str, precondition: str | None = None,
         outcome: str = "применено") -> dict:
    if kind not in KINDS:
        raise ValueError("неизвестный вид решения: %r" % kind)
    actor, source = operator()
    return {
        "actor": actor, "actor_source": source, "kind": kind,
        "subject_kind": subject_kind, "subject_id": str(subject_id),
        "subject_digest": subject_digest, "scope": scope,
        "action": json.dumps(action, ensure_ascii=False, sort_keys=True, default=str),
        "grounds": grounds[:400], "precondition": precondition, "outcome": outcome,
    }
