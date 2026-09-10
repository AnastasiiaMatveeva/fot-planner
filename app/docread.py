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
        # Текстового слоя нет — это скан. Читаем страницы картинками той
        # моделью, которая понимает изображения.
        text = _read_scan(path)
        if text:
            return text
        raise Unreadable(
            "в файле нет текстового слоя — похоже, это скан, а модель, "
            "читающая изображения, недоступна: приложите документ в "
            "текстовом виде или введите величины вручную")
    return "\n\n".join(pages)


def _read_scan(path):
    """Скан страницами через модель; None — распознавать нечем."""
    try:
        import ocr
    except ImportError:
        return None
    try:
        return ocr.text_from_pdf(path)
    except Exception:  # noqa: BLE001 — не вышло распознать: читаем как есть
        return None


def scan_text(path):
    """Перечитать PDF картинками, минуя текстовый слой.

    Нужно там, где слой есть, но он каша: чужое распознавание выглядит
    прочитанным и подсовывает «МИН ИСТRJ>Сrво».
    """
    return _read_scan(path)


# ── Word ────────────────────────────────────────────────────────
def from_docx(path):
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml)


def _doc_pieces(wd, tbl):
    """Текст .doc по таблице кусков: FIB → CLX → PlcPcd → куски потока.

    Куски бывают двух видов: UTF-16 и однобайтовый cp1251 — на это указывает
    старший бит fc. Порядок кусков и есть порядок текста, поэтому таблицы
    собираются целиком, а не выпадают.
    """
    import struct

    # fcClx/lcbClx — 33-е поле FibRgFcLcb97; структура идёт после FibBase(32)
    # + csw(2) + FibRgW97(28) + cslw(2) + FibRgLw97(88) + cbRgFcLcb(2).
    fc_clx, lcb_clx = struct.unpack_from("<II", wd, 154 + 33 * 8)
    clx = tbl[fc_clx:fc_clx + lcb_clx]
    i = 0
    while i < len(clx) and clx[i] == 0x01:          # блоки свойств пропускаем
        i += 3 + struct.unpack_from("<H", clx, i + 1)[0]
    if i >= len(clx) or clx[i] != 0x02:
        raise ValueError("в файле нет таблицы кусков")
    lcb = struct.unpack_from("<I", clx, i + 1)[0]
    plc = clx[i + 5:i + 5 + lcb]
    n = (len(plc) - 4) // 12
    if n <= 0:
        raise ValueError("таблица кусков пуста")
    cps = list(struct.unpack_from("<%dI" % (n + 1), plc, 0))
    out = []
    for k in range(n):
        fc = struct.unpack_from("<I", plc, 4 * (n + 1) + k * 8 + 2)[0]
        length = cps[k + 1] - cps[k]
        if fc & 0x40000000:
            start_at = (fc & ~0x40000000) // 2
            out.append(wd[start_at:start_at + length].decode("cp1251", "ignore"))
        else:
            out.append(wd[fc:fc + length * 2].decode("utf-16-le", "ignore"))
    return "".join(out)


def _doc_clean(text):
    """Ячейки — через табуляцию, строки таблиц — по строке, поля Word — прочь."""
    # \x07 — конец ячейки; два подряд — конец строки таблицы.
    text = text.replace("\x07\x07", "\n").replace("\x07", "\t")
    text = text.replace("\r", "\n").replace("\x0b", "\n").replace("\xa0", " ")
    # Поля Word (DOCPROPERTY "…" \* MERGEFORMAT, PAGE и прочие) — разметка.
    text = re.sub(r'\s*[A-Z]{3,}[A-Z ]*\s*(?:"[^"]*")?\s*(?:\\\*\s*\w+)?\s*', " ", text)
    text = re.sub(r"[\x00-\x08\x0c\x0e-\x1f]", "", text)
    rows = []
    for line in text.split("\n"):
        cells = [c.strip() for c in line.split("\t")]
        cells = [c for c in cells if c]
        if cells:
            rows.append("\t".join(cells))
    return "\n".join(rows)


def from_doc(path):
    """Старый бинарный .doc.

    Сначала по таблице кусков: так достаются и абзацы, и таблицы — прежний
    способ брал из потока куски длиннее сорока знаков, и таблица пределов из
    приказа целиком пропадала. Если файл нестандартный, откатываемся к
    прежнему способу: текст будет, таблиц не будет.
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
        names = ["/".join(s) for s in ole.listdir()]
        table = "1Table" if "1Table" in names else ("0Table" if "0Table" in names else None)
        tbl = ole.openstream(table).read() if table else b""
    if tbl:
        try:
            text = _doc_clean(_doc_pieces(raw, tbl))
            if len(text.strip()) > 40:
                return text
        except Exception:  # noqa: BLE001 — нестандартный файл: пробуем по-старому
            pass
    text = raw.decode("utf-16-le", "ignore")
    chunks = re.findall(r"[А-Яа-яЁёA-Za-z0-9 .,;:№%()«»\"\'/\-–—\n\r\t]{40,}", text)
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
