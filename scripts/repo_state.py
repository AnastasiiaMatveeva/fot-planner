"""Состояние проверяемого дерева: NUL-пути Git и отпечаток содержимого."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath


class RepositoryError(RuntimeError):
    pass


def git(root, *args):
    root = Path(root).resolve()
    try:
        result = subprocess.run(["git", "-c", "safe.directory=" + root.as_posix(), *args],
                                cwd=root, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepositoryError(str(exc)) from exc
    if result.returncode:
        raise RepositoryError(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def names(raw):
    return {item.decode("utf-8") for item in raw.split(b"\0") if item}


def revision(root, ref):
    if not ref or str(ref).startswith("-"):
        raise RepositoryError("базовый commit не указан")
    return git(root, "rev-parse", "--verify", str(ref) + "^{commit}").decode().strip()


def changed(root, base="HEAD"):
    base = revision(root, base)
    return names(git(root, "diff", "--name-only", "--no-renames", "-z", base, "--")) | names(
        git(root, "ls-files", "--others", "--exclude-standard", "-z"))


def allowed(path):
    path = path.replace("\\", "/").lower()
    parts = PurePosixPath(path).parts
    if any(part.startswith(".env") or part == "env" for part in parts):
        return False
    if path.startswith(("app/data/", "outputs/", "docs/evidence/", ".git/", ".venv/")):
        return False
    return path.startswith(("app/", "src/", "scripts/", "harness/", "tests/", "migrations/",
                            "docs/", ".github/", "capabilities/", "schemas/local-harness/")) or path in {
        "agents.md", "claude.md", "pyproject.toml", "requirements.txt", "uv.lock",
        ".claude/settings.json", ".gitignore"}


def local_path(root, name):
    parts = PurePosixPath(name).parts
    if "\\" in name or ":" in name or ".." in parts or PurePosixPath(name).is_absolute():
        raise RepositoryError("недопустимый путь: " + name)
    root = Path(root).resolve()
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root) or path.is_symlink():
        raise RepositoryError("путь вне проекта или символическая ссылка: " + name)
    return path


def baseline(root, base, name, tracked=None):
    if not allowed(name):
        raise RepositoryError("путь не относится к проверяемому коду: " + name)
    tracked = tracked if tracked is not None else names(git(root, "ls-tree", "-r", "--name-only", "-z", base))
    return git(root, "show", base + ":" + name) if name in tracked else None


def fingerprint(root, commands):
    """Содержимое, HEAD, команды и Python; живые данные и секреты не читаются."""
    try:
        paths = names(git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"))
        h = hashlib.sha256()
        h.update(json.dumps([revision(root, "HEAD"), commands, sys.executable, sys.version],
                            ensure_ascii=False, sort_keys=True).encode("utf-8"))
        for name in sorted(p for p in paths if allowed(p)):
            path = local_path(root, name)
            h.update(name.encode("utf-8") + b"\0")
            if not path.exists():
                h.update(b"deleted\0")
                continue
            h.update(b"file\0")
            with path.open("rb") as file:
                for chunk in iter(lambda: file.read(1024 * 1024), b""):
                    h.update(chunk)
            h.update(b"\0")
        return h.hexdigest()
    except (OSError, ValueError) as exc:
        raise RepositoryError(str(exc)) from exc
