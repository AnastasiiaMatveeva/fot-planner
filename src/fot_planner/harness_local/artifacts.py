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
import io
import os
import tempfile


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
        return self._put_stream(io.BytesIO(data))

    def put_file(self, path: str) -> str:
        # Хешируем именно скопированные байты за один проход: между прежними
        # sha256_of и copyfile исходник мог уже стать другим файлом.
        with open(path, "rb") as source:
            return self._put_stream(source)

    def _put_stream(self, source) -> str:
        os.makedirs(self.root, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, prefix=".artifact-",
                                             suffix=".part", delete=False) as file:
                tmp = file.name
                h = hashlib.sha256()
                for chunk in iter(lambda: source.read(1 << 20), b""):
                    file.write(chunk)
                    h.update(chunk)
                file.flush()
                os.fsync(file.fileno())
            digest = h.hexdigest()
            target = self.path(digest)
            if os.path.isfile(target):
                if not self.verify(digest):
                    raise ValueError("повреждён существующий артефакт: " + digest)
                return digest
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # Конкуренты имеют разные временные файлы на том же диске.
            # Если оба публикуют один digest, байты одинаковы; один не может
            # удалить временный файл другого вызова.
            os.replace(tmp, target)
            return digest
        finally:
            if tmp is not None and os.path.exists(tmp):
                os.unlink(tmp)

    def read(self, digest: str) -> bytes:
        with open(self.path(digest), "rb") as f:
            return f.read()

    def verify(self, digest: str) -> bool:
        """Артефакт на месте и его байты дают тот же хеш. Подмена файла на
        диске или битый сектор — False, а не тихая ссылка в никуда."""
        p = self.path(digest)
        return os.path.isfile(p) and sha256_of(p) == digest
