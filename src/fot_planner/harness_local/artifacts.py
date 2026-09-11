# -*- coding: utf-8 -*-
"""Неизменяемые артефакты по SHA-256.

Зачем. Прогон хранил пути к файлам входа и результата в рабочей папке —
а путь не снимок: файл можно перезаписать, и прогон будет ссылаться на
другие байты, ничего об этом не зная (VER-002). Артефакт кладётся под именем
своего хеша и больше не меняется: одинаковые байты — один файл, отличие в
один байт — другой хеш. Сверка сводится к пересчёту хеша.

Раскладка: `<root>/<ab>/<abcdef…>` — первые два знака хеша как папка, чтобы
десятки тысяч файлов не лежали в одном каталоге.
"""
from __future__ import annotations

import hashlib
import os
import shutil


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ArtifactStore:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def path(self, digest: str) -> str:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("не SHA-256: %r" % digest)
        return os.path.join(self.root, digest[:2], digest)

    def has(self, digest: str) -> bool:
        return os.path.isfile(self.path(digest))

    def put_bytes(self, data: bytes) -> str:
        digest = sha256_bytes(data)
        target = self.path(digest)
        if os.path.isfile(target):
            # Один раз записанный артефакт не переписывается: те же байты
            # уже лежат, а «обновить» их — значит потерять снимок.
            return digest
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
        return digest

    def put_file(self, path: str) -> str:
        digest = sha256_of(path)
        target = self.path(digest)
        if os.path.isfile(target):
            return digest
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".part"
        shutil.copyfile(path, tmp)
        os.replace(tmp, target)
        return digest

    def read(self, digest: str) -> bytes:
        with open(self.path(digest), "rb") as f:
            return f.read()

    def verify(self, digest: str) -> bool:
        """Артефакт на месте и его байты дают тот же хеш. Подмена файла на
        диске или битый сектор — False, а не тихая ссылка в никуда."""
        p = self.path(digest)
        return os.path.isfile(p) and sha256_of(p) == digest
