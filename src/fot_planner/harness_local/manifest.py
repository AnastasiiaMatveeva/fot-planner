# -*- coding: utf-8 -*-
"""Снимок прогона (RunManifest) и версия кода решателя.

Снимок отвечает на вопрос «на чём именно считали»: байты входа, справочник,
настройки, документы-источники, версия кода решателя и зависимостей, а после
расчёта — байты результата и итог проверки. Всё материальное лежит в
хранилище артефактов по SHA-256, снимок ссылается на хеши и сам хранится
там же. Совпадение имени файла или модели снимком не считается (VER-002).

Канонический JSON — `fot-json-v1` (ADR-local-harness): ключи отсортированы,
без пробелов, UTF-8, NaN и бесконечность отклоняются. Это не RFC 8785; на
интеграционной границе преобразование делает адаптер.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
import sys
from copy import deepcopy

from fot_planner.harness_local.artifacts import ArtifactStore, sha256_bytes

CANON = "fot-json-v1"
#: 2 — добавлен раздел «attempt»: повтор чистого вычисления ссылается на
#: прогон, чей закреплённый вход он взял (RUN-002). Снимки схемы 1 читаются
#: и сверяются так же: раздел у них просто отсутствует.
#: 3 — закреплённый состав проверок и отдельная запись их состояний (QA-002).
MANIFEST_SCHEMA = 3


def canonical_json(obj) -> bytes:
    """Одинаковый объект — одинаковые байты, независимо от порядка ключей."""
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    return text.encode("utf-8")


def code_version(src_root: str) -> dict:
    """Версия кода решателя: хеш всех .py пакета в устойчивом порядке плюс
    версии интерпретатора и библиотек, от которых зависит ответ.

    Хеш меняется от любой правки любого файла пакета — этого и надо: план,
    посчитанный до правки, и план после — разные версии, даже если имя
    пакета и его SemVer не менялись (VER-001).
    """
    h = hashlib.sha256()
    files = []
    for base, _dirs, names in os.walk(src_root):
        for n in names:
            if n.endswith(".py"):
                files.append(os.path.join(base, n))
    for p in sorted(files):
        rel = os.path.relpath(p, src_root).replace(os.sep, "/")
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        with open(p, "rb") as f:
            h.update(f.read())
        h.update(b"\0")
    deps = {}
    for name in ("pyomo", "highspy", "openpyxl", "pandas"):
        try:
            mod = __import__(name)
            deps[name] = getattr(mod, "__version__", None) or getattr(getattr(mod, "version", None), "version", "?")
        except Exception:  # noqa: BLE001 — зависимость может отсутствовать
            deps[name] = None
    return {"code_sha256": h.hexdigest(), "files": len(files),
            "python": platform.python_version(), "dependencies": deps}


def build_manifest(*, run_id: int, case_id: int, executor: str, input_sha256: str,
                   reference_sha256: str | None, settings: dict, sources: dict,
                   code: dict, command: list[str], time_limit_sec,
                   attempt_of: int | None = None, required_checks: list[dict] | None = None) -> dict:
    """Черновик снимка до первого действия решателя (VER-002): вход закреплён.

    ``attempt_of`` — номер прогона, чей закреплённый вход взят для повтора
    (RUN-002): попытки связаны, а вход у них один и тот же по хешу.
    """
    return {
        "schema": MANIFEST_SCHEMA, "canonical": CANON,
        "run_id": run_id, "case_id": case_id, "executor": executor,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "input": {"sha256": input_sha256, "kind": "xlsx-template"},
        "reference": {"sha256": reference_sha256},
        "settings": settings or {},
        "sources": sources,
        "solver": {"command": list(command), "time_limit_sec": time_limit_sec, **code},
        "attempt": {"retry_of": attempt_of, "input": "из снимка прогона"} if attempt_of else None,
        "output": None, "audit": None, "finished_at": None,
        "evaluation_plan": {"required_checks": deepcopy(required_checks or [])},
        "evaluation": None,
    }


def finalize_manifest(manifest: dict, *, output_sha256: str | None, solver_status: str,
                      audit_status: str | None, audit_lines: list[str] | None,
                      seconds: float | None, audit_checks: list[dict] | None = None,
                      execution_status: str | None = None) -> dict:
    """Итог после всех преобразований результата: хеш берётся с конечного файла."""
    from fot_planner.harness_local.evaluation import record_checks
    done = deepcopy(manifest)
    done["schema"] = MANIFEST_SCHEMA
    done["output"] = {"sha256": output_sha256, "kind": "xlsx-result"} if output_sha256 else None
    done["solver"] = {**manifest["solver"], "status": solver_status, "seconds": seconds}
    done["audit"] = {"status": audit_status or "NOT_RUN", "lines": list(audit_lines or [])}
    done["execution"] = {"status": execution_status or solver_status}
    done["evaluation"] = record_checks(manifest, output_sha256=output_sha256,
                                        solver_status=solver_status, audit_status=audit_status,
                                        checks=audit_checks)
    done["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return done


def store_manifest(store: ArtifactStore, manifest: dict) -> str:
    return store.put_bytes(canonical_json(manifest))


def load_manifest(store: ArtifactStore, digest: str) -> dict:
    raw = store.read(digest)
    # Проверяем именно прочитанные байты, не отдельное чтение до JSON.
    if sha256_bytes(raw) != digest:
        raise ValueError("байты снимка не совпадают с SHA-256")
    manifest = json.loads(raw.decode("utf-8"))
    if (not isinstance(manifest, dict) or type(manifest.get("schema")) is not int
            or manifest['schema'] not in (1, 2, 3)):
        raise ValueError("неизвестная схема снимка")
    return manifest


def verify_manifest(store: ArtifactStore, manifest: dict) -> list[str]:
    """Что из снимка нельзя воспроизвести: отсутствующий или подменённый артефакт.

    Пустой список — материальные части целы, привязка заключения согласована.
    Это проверка целостности, не повторный расчёт и не числовая приёмка.
    """
    problems = []
    independent = manifest.get('independent_audit')
    if independent is not None:
        try:
            digest = independent['report_sha256']
            raw = store.read(digest)
            if sha256_bytes(raw) != digest or json.loads(raw) != {
                    k: v for k, v in independent.items() if k != 'report_sha256'}:
                problems.append('independent_audit: отчёт повреждён или не соответствует снимку')
            if independent.get('verdict') == 'passed' and (
                    independent.get('input_sha256') != (manifest.get('input') or {}).get('sha256')
                    or independent.get('output_sha256') != (manifest.get('output') or {}).get('sha256')):
                problems.append('independent_audit: отчёт относится к другому входу или выходу')
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            problems.append('independent_audit: отчёт недоступен или некорректен')
    evaluation = manifest.get("evaluation")
    if evaluation is not None:
        expected = {"run_id": manifest.get("run_id"),
                    "input_sha256": (manifest.get("input") or {}).get("sha256"),
                    "output_sha256": (manifest.get("output") or {}).get("sha256")}
        if not isinstance(evaluation, dict) or evaluation.get("subject") != expected:
            problems.append("evaluation: заключение относится к другому прогону, входу или выходу")
    elif manifest.get("schema") == 3 and manifest.get("finished_at"):
        problems.append("evaluation: у завершённого снимка нет записи проверок")
    for name in ("input", "output", "reference"):
        part = manifest.get(name)
        digest = part.get("sha256") if isinstance(part, dict) else None
        if not digest:
            if name == "input":
                problems.append("вход не закреплён")
            continue
        if not store.has(digest):
            problems.append("%s: артефакта %s… нет в хранилище" % (name, digest[:12]))
        elif not store.verify(digest):
            problems.append("%s: байты артефакта %s… не совпадают с хешем" % (name, digest[:12]))
    return problems


def manifest_digest(manifest: dict) -> str:
    return sha256_bytes(canonical_json(manifest))


def default_src_root() -> str:
    """Корень пакета fot_planner — то, чью версию считаем."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


__all__ = ["ArtifactStore", "canonical_json", "code_version", "build_manifest",
           "finalize_manifest", "store_manifest", "load_manifest", "verify_manifest",
           "manifest_digest", "default_src_root", "sys"]
