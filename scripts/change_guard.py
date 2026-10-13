"""DEV-GUARD-001: исправление связано с карточкой и исполняемым тестом.

Изменения старой защиты требуют ревью, а не трактуются как доказанная
регрессия. Самоподписанного поля approved и выключателя проверки здесь нет.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path

import repo_state as S

REGISTRY = "harness/changes.json"
RULES = {"AGENTS.md", "CLAUDE.md", "docs/ПРАВИЛА_РЕШАТЕЛЯ.md", "docs/ЧАТ_ПРАВИЛА.md",
         "docs/ИНТЕРФЕЙС.md", "docs/ИСТОЧНИКИ.md", "docs/ТЕСТИРОВАНИЕ.md",
         "docs/ШАБЛОН_ТРЕБОВАНИЯ.md", "docs/requirements-map.json", REGISTRY}


def protected(path):
    return (path in RULES or path.startswith(("tests/", ".github/", ".claude/", "capabilities/", "schemas/local-harness/"))
            or (path.startswith("harness/") and path != REGISTRY)
            or path.startswith(("scripts/test", "scripts/check", "scripts/lint"))
            or path in {"scripts/parts.py", "scripts/stop_gate.py", "scripts/change_guard.py",
                        "scripts/repo_state.py", "pyproject.toml", ".gitignore"})


def product(path):
    return S.allowed(path) and (path.startswith(("app/", "src/", "docs/ui/", "migrations/", "capabilities/", "schemas/local-harness/"))
                                or path == "scripts/migrate.py")


def test_node(data, symbol):
    """Ссылка именно на case или unittest test, а не произвольный текст файла."""
    if data is None:
        return None
    tree = ast.parse(data.decode("utf-8-sig"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name)
                        and decorator.func.id == "case" and decorator.args
                        and isinstance(decorator.args[0], ast.Constant)
                        and symbol == "case:" + str(decorator.args[0].value)):
                    return ast.dump(node, include_attributes=False)
        if isinstance(node, ast.ClassDef) and any(
            (isinstance(base, ast.Attribute) and base.attr == "TestCase")
            or (isinstance(base, ast.Name) and base.id == "TestCase") for base in node.bases):
            for method in node.body:
                if (isinstance(method, ast.FunctionDef) and method.name.startswith("test_")
                        and symbol == node.name + "." + method.name):
                    return ast.dump(method, include_attributes=False)
    return None


def inspect(root, base, runnable):
    root, base = Path(root), S.revision(root, base)
    paths = sorted(p for p in S.changed(root, base) if S.allowed(p))
    tracked = S.names(S.git(root, "ls-tree", "-r", "--name-only", "-z", base))
    errors, review, records, covered = [], [], [], set()
    old = lambda name: S.baseline(root, base, name, tracked)
    current = lambda name: S.local_path(root, name).read_bytes()
    for path in paths:
        before = old(path)
        if protected(path) and before is not None:
            after = current(path) if S.local_path(root, path).is_file() else None
            # Реестр пополняется, но удаление/изменение старой записи требует ревью.
            if path == REGISTRY and after is not None:
                try:
                    previous = json.loads(before)["changes"]
                    now = json.loads(after)["changes"]
                    by_id = {item["id"]: item for item in now}
                    if all(by_id.get(item["id"]) == item for item in previous):
                        continue
                except (ValueError, KeyError, TypeError):
                    pass
            if before != after:
                review.append(path)
    try:
        data = json.loads(current(REGISTRY))
        if data.get("schema_version") != 1 or not isinstance(data.get("changes"), list):
            raise ValueError("ожидались schema_version=1 и список changes")
        entries = data["changes"]
        previous = json.loads(old(REGISTRY) or b'{"changes": []}')["changes"]
        previous = {item["id"]: item for item in previous}
        seen = set()
        for item in entries:
            key = item.get("id")
            if not isinstance(key, str) or not key.strip() or key in seen:
                errors.append("пустой/повторный ID записи")
                continue
            seen.add(key)
            files = item.get("files", [])
            if not isinstance(files, list) or not files or not all(isinstance(p, str) and S.allowed(p) for p in files):
                errors.append(key + ": нужен точный список файлов проекта")
                continue
            for path in files:
                S.local_path(root, path)
            active = set(files) & set(paths)
            status = item.get("status")
            if status not in {"open", "in_progress", "implemented", "conflict"}:
                errors.append(key + ": неизвестное состояние")
            records.append({"id": key, "status": status, "files": files})
            if not active:
                continue
            if status != "implemented":
                # Другой баг в том же файле не запрещает независимое исправление.
                # Незавершённая запись просто не даёт покрытия новой правке.
                if status == "conflict" and previous.get(key) != item:
                    errors.append(key + ": затронутая работа содержит конфликт")
                continue
            if previous.get(key) == item:
                # История не запрещает новую задачу, но и не покрывает её:
                # новая правка обязана иметь свою карточку и регрессию.
                continue
            for field in ("summary", "expected", "reproduction"):
                if not isinstance(item.get(field), str) or not item[field].strip():
                    errors.append(key + ": отсутствует " + field)
            requirement = item.get("requirement", {})
            card, req_id = requirement.get("path", ""), requirement.get("id", "")
            if card not in RULES - {REGISTRY, "docs/requirements-map.json"} or not req_id:
                errors.append(key + ": нужна ссылка на каноническую карточку")
            elif card not in paths or req_id not in current(card).decode("utf-8-sig"):
                errors.append(key + ": карточка не обновлена или ID не найден")
            tests = item.get("tests", [])
            if not isinstance(tests, list) or not tests:
                errors.append(key + ": отсутствует регрессионный тест")
                continue
            for test in tests:
                path, symbol = test.get("path", ""), test.get("symbol", "")
                if path not in runnable or path not in paths or not path.endswith(".py"):
                    errors.append(key + ": тест не изменён или не подключён к приёмке: " + path)
                    continue
                after = test_node(current(path), symbol)
                if after is None or after == test_node(old(path), symbol):
                    errors.append(key + ": нет нового/изменённого теста " + path + "::" + symbol)
            covered.update(active)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, SyntaxError, S.RepositoryError) as exc:
        errors.append("не удалось проверить очередь изменений: " + str(exc))
    for path in paths:
        if product(path) and path not in covered:
            errors.append("нет завершённой записи изменения для " + path)
    return {"schema_version": 1, "base": base, "changed": paths, "records": records,
            "errors": errors, "review_required": review,
            "status": "failed" if errors else "review_required" if review else "passed"}


def main():
    import parts as P
    from check import write_report
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("FOT_CHANGE_BASE", "HEAD"))
    ap.add_argument("--report", default="outputs/change-guard.json")
    args = ap.parse_args()
    runnable = {arg for _, command, _ in P.PARTS for arg in command if arg.endswith(".py")}
    try:
        result = inspect(P.ROOT, args.base, runnable)
    except (S.RepositoryError, OSError, ValueError) as exc:
        result = {"status": "error", "errors": [str(exc)], "review_required": []}
    print("Защита изменений: " + result["status"])
    for error in result["errors"]:
        print("  ошибка: " + error)
    for path in result["review_required"]:
        print("  требуется ревью разработчика: " + path)
    try:
        write_report(args.report, result)
    except OSError as exc:
        print("Не удалось сохранить отчёт: " + str(exc))
        return 1
    return 0 if result["status"] == "passed" else 3 if result["status"] == "review_required" else 1


if __name__ == "__main__":
    raise SystemExit(main())
