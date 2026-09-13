# -*- coding: utf-8 -*-
"""Снимок контекста извлечения и точная привязка приговора (MEM-003, VER-002).

Что именно видела модель, когда предложила строку: байты документа, тексты
подсказки, подтверждённые исправления с их ID, провайдер и модель, параметры
вызова и итог ответа. Всё это — канонический JSON в хранилище артефактов по
SHA-256; предложение и приговор ссылаются на его хеш. Секретов здесь нет:
ключи провайдера в снимок не попадают, и тест это сторожит.

Приговор без хеша документа — legacy: он остаётся историей, но строгим
эталоном не становится. Другая редакция под тем же именем приговор не
наследует; если документа той редакции больше нет, проверка не проходит
как «источник недоступен», а не пропускается молча.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

from fot_planner.harness_local.manifest import canonical_json

SNAPSHOT_SCHEMA = 1
PURPOSE = "extraction"
#: Провайдеры неизменяемую ревизию модели не сообщают; имя модели
#: воспроизводимость не доказывает (VER-002) — это ограничение записывается.
REVISION_NOTE = ("провайдер неизменяемую ревизию модели не сообщает; совпадение "
                 "имени модели полную воспроизводимость не доказывает")


def text_digest(text) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _plain(value):
    """JSON-безопасная копия: ответ модели уже JSON, но чужие типы бывают."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else str(value)
    return str(value)


def result_digest(result: dict) -> str:
    return hashlib.sha256(canonical_json(_plain(result))).hexdigest()


def build_snapshot(*, document: dict, provider, model, prompt: dict, hints: dict,
                   params: dict, result: dict, agent_version=None) -> dict:
    """Снимок одного вызова извлечения.

    ``document`` — id, имя, sha256 записи, sha256 артефакта (байты, ушедшие
    в хранилище), редакция, вид, область; ``prompt`` — фактические тексты
    подсказки; ``hints`` — что из исправлений передано и что исключено как
    конфликт; ``params`` — параметры вызова; ``result`` — ответ модели.
    """
    result = _plain(result or {})
    counts = {k: len(v) for k, v in result.items() if isinstance(v, list)}
    return {
        "schema": SNAPSHOT_SCHEMA, "purpose": PURPOSE,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "document": _plain(document),
        "model": {"provider": provider, "name": model, "revision": None,
                  "revision_note": REVISION_NOTE, "reported": result.get("model")},
        "prompt": {
            "system": prompt.get("system"), "shape": prompt.get("shape"),
            "schema": _plain(prompt.get("schema")),
            "hinted_system_sha256": text_digest(prompt.get("hinted_system")),
            "system_sha256": text_digest(prompt.get("system")),
            "shape_sha256": text_digest(prompt.get("shape")),
            "schema_sha256": hashlib.sha256(canonical_json(_plain(prompt.get("schema")))).hexdigest(),
            "legacy_prompt_mark": agent_version,
        },
        "hints": {"ids": list(hints.get("ids") or []), "texts": list(hints.get("texts") or []),
                  "conflicts": list(hints.get("conflicts") or []),
                  "source": _plain(hints.get("source"))},
        "params": _plain(params),
        "result": {"ok": bool(result.get("ok")), "error": result.get("error"),
                   "parts": result.get("parts"),
                   "truncated_parts": result.get("обрезано частей") or 0,
                   "counts": counts, "sha256": result_digest(result)},
    }


def store_snapshot(store, snapshot: dict) -> str:
    return store.put_bytes(canonical_json(snapshot))


def load_snapshot(store, digest: str) -> dict:
    return json.loads(store.read(digest).decode("utf-8"))


def snapshot_hints(store, digest):
    """Подсказки, которые видела модель; None — снимка нет или он повреждён."""
    if not digest:
        return None
    try:
        if not store.has(digest) or not store.verify(digest):
            return None
        snap = load_snapshot(store, digest)
    except (OSError, ValueError):
        return None
    if snap.get("purpose") != PURPOSE:
        return None
    return list((snap.get("hints") or {}).get("texts") or [])


def source_status(document_sha256, path, sha256_of):
    """Годится ли документ на диске как источник приговора этой редакции.

    exact — байты те же; missing — документа этой редакции нет; changed —
    файл под этим именем есть, но байты другие. Имя файла эталоном не
    является (MEM-003).
    """
    if not document_sha256:
        return "legacy", "приговор без хеша документа: привязка неполная, строгим эталоном не считается"
    if not path:
        return "missing", ("документа редакции %s… нет в реестре или файл удалён: приговор не "
                           "проверить, другая редакция под тем же именем его не наследует"
                           % document_sha256[:12])
    try:
        actual = sha256_of(path)
    except OSError:
        return "missing", "файл документа редакции %s… не читается" % document_sha256[:12]
    if actual != document_sha256:
        return "changed", ("байты файла не совпадают с редакцией приговора %s…: другая редакция "
                           "приговор не наследует" % document_sha256[:12])
    return "exact", ""


__all__ = ["SNAPSHOT_SCHEMA", "PURPOSE", "REVISION_NOTE", "text_digest", "result_digest",
           "build_snapshot", "store_snapshot", "load_snapshot", "snapshot_hints", "source_status"]
