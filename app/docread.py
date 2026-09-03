# -*- coding: utf-8 -*-
"""Документ любого вида — в текст для модели.

Нормативные документы приходят не книгами Excel: положение об оплате труда —
PDF на 76 страниц, письмо военного представительства — PDF, приказ № 2556 —
старый бинарный .doc. Разбирать их все по отдельности незачем: модели нужен
текст, и задача этого модуля — его добыть, честно сообщив, если не вышло.

Отдельный случай — скан без текстового слоя (справка П4 такая). Распознавание
изображений сюда не входит: модель на сервере текстовая. В этом случае
возвращается пустой текст с объяснением, и агент говорит об этом экономисту,
а не делает вид, что прочитал.
"""
from __future__ import annotations

import os
import re
import zipfile

MAX_CHARS = 60000          # больше в запрос все равно не имеет смысла слать


class Unreadable(Exception):
    """Файл не удалось привести к тексту. Сообщение — для экономиста."""


# ── книги Excel ─────────────────────────────────────────────────
def from_workbook(path):
    """Листы книги текстовой сеткой с координатами ячеек.

    Координаты нужны, чтобы объединенные и многоэтажные заголовки не теряли
    привязку к своим колонкам, а модель могла сослаться на место в документе.
    """
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append("### Лист: %s (строк %d, колонок %d)" % (ws.title, ws.max_row, ws.max_column))
        for r in range(1, min(ws.max_row, 400) + 1):
            cells = []
            for c in range(1, min(ws.max_column, 40) + 1):
                v = ws.cell(r, c).value
                if v is None or str(v).strip() == "":
                    continue
                cells.append("%s%d=%s" % (chr(64 + c) if c <= 26 else "?", r, v))
            if cells:
                out.append(" | ".join(cells))
        if ws.merged_cells.ranges:
            out.append("объединенные диапазоны: " +
                       ", ".join(str(x) for x in list(ws.merged_cells.ranges)[:40]))
    return "\n".join(out)


# ── PDF ─────────────────────────────────────────────────────────
def from_pdf(path):
    try:
        import pypdf
    except ImportError as e:
        raise Unreadable("для PDF нужен пакет pypdf: pip install pypdf") from e
    reader = pypdf.PdfReader(path)
    pages = []
    for i, page in enumerate(reader.pages, 1):
        t = (page.extract_text() or "").strip()
        if t:
            pages.append("### Страница %d\n%s" % (i, t))
    if not pages:
        raise Unreadable(
            "в файле нет текстового слоя — похоже, это скан. "
            "Распознавание изображений не подключено: приложите документ "
            "в текстовом виде или введите величины вручную")
    return "\n\n".join(pages)


# ── Word ────────────────────────────────────────────────────────
def from_docx(path):
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml)


def from_doc(path):
    """Старый бинарный .doc.

    Текст в потоке WordDocument лежит в UTF-16; вокруг него служебные
    структуры, поэтому берем длинные читаемые фрагменты, а не весь поток.
    Для приказа этого достаточно: нужны формулировки и суммы, а не верстка.
    """
    try:
        import olefile
    except ImportError as e:
        raise Unreadable("для .doc нужен пакет olefile: pip install olefile") from e
    if not olefile.isOleFile(path):
        raise Unreadable("файл не является документом Word")
    with olefile.OleFileIO(path) as ole:
        if not ole.exists("WordDocument"):
            raise Unreadable("в файле нет потока WordDocument")
        raw = ole.openstream("WordDocument").read()
    text = raw.decode("utf-16-le", "ignore")
    chunks = re.findall(r"[А-Яа-яЁёA-Za-z0-9 .,;:№%()«»\"'/\-–—\n\r\t]{40,}", text)
    if not chunks:
        raise Unreadable("не нашел читаемого текста; пересохраните файл как .docx или PDF")
    return "\n".join(c.strip() for c in chunks)


# ── выбор способа ───────────────────────────────────────────────
READERS = {
    ".xlsx": from_workbook, ".xlsm": from_workbook, ".xls": from_workbook,
    ".pdf": from_pdf,
    ".docx": from_docx, ".doc": from_doc,
}


def kind_of(path):
    return os.path.splitext(path)[1].lower()


def to_text(path, limit=MAX_CHARS):
    """Текст документа. Бросает Unreadable с внятным сообщением."""
    ext = kind_of(path)
    reader = READERS.get(ext)
    if reader is None:
        raise Unreadable("формат %s не поддерживается: нужны xlsx, xlsm, pdf, doc или docx"
                         % (ext or "без расширения"))
    try:
        text = reader(path)
    except Unreadable:
        raise
    except Exception as e:  # noqa: BLE001 — битый файл не должен ронять разбор
        raise Unreadable("файл не прочитан: %s" % e) from e
    # Word разделяет абзацы возвратом каретки, а не переводом строки: в тексте
    # приказа на три тысячи знаков было семнадцать «\n» и полсотни «\r».
    # Браузер такой текст показывает сплошной стеной, а нарезка на части для
    # модели идет по «\n» и режет где попало. Приводим к одному виду здесь,
    # чтобы и разбор, и предпросмотр видели одинаковые абзацы.
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise Unreadable("документ пуст")
    return text[:limit]
