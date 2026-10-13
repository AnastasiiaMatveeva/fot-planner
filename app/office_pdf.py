# -*- coding: utf-8 -*-
"""Печать документа в PDF через Microsoft Office (COM).

Живёт отдельным модулем и запускается дочерним процессом (main._office_to_pdf):
12.10.2026 прогон пятидесяти документов ронял сервер — Office отклонял вызов
(0x80010001), и следом pywin32 падал с 0xC0000005 вместе со всем процессом.
Упавшая печать в своём процессе — просто «PDF не готов».

    python office_pdf.py <исходный файл> <pdf>
    python office_pdf.py --html <документ Word> <htm>   — отфильтрованный HTML
"""
from __future__ import annotations

import os
import sys

#: Книги Excel; остальное открывает Word. Тот же список, что в main.
WORKBOOK_TYPES = (".xlsx", ".xlsm", ".xltx", ".xltm")


def unclip_columns(ws):
    """Раздвинуть столбцы и строки, где текст обрезан.

    Excel печатает лист как есть: «Наименование должности» в узком столбце
    обрывается на «Наименов», если справа тоже что-то написано, а строка с
    фиксированной высотой режет текст по верху. Читать такое нельзя.

    Каждое обращение к ячейке через COM — отдельный вызов в Excel, и на
    книге в тысячу ячеек это секунды. Поэтому значения читаем разом
    (``UsedRange.Value``), свойства спрашиваем у столбцов, а не у ячеек, а
    высоту строк подбираем одним вызовом на весь лист. Файл не меняется:
    книга открыта только для чтения.
    """
    try:
        ur = ws.UsedRange
        if ur.Cells.Count > 20000:
            return                      # огромный лист: печатаем как есть
        rows, cols = ur.Rows.Count, ur.Columns.Count
        vals = ur.Value
        if rows == 1 and cols == 1:
            return
        if rows == 1:
            vals = (vals,)
        if cols == 1:
            vals = tuple((v,) for v in vals)
        # Свойства столбца целиком: None — в столбце они разные, тогда
        # столбец не трогаем, чтобы не разъехалась авторская вёрстка.
        widths, wrap, merged = [], [], []
        for c in range(cols):
            col = ur.Columns(c + 1)
            widths.append(float(col.ColumnWidth or 0))
            wrap.append(col.WrapText)
            merged.append(col.MergeCells)
        need = {}
        for r in range(rows):
            row = vals[r]
            for c in range(cols - 1):
                v, nxt = row[c], row[c + 1]
                if not (isinstance(v, str) and v.strip()) or nxt in (None, ""):
                    continue
                if wrap[c] is not False or merged[c] is not False:
                    continue
                if len(v) > widths[c]:
                    need[c] = max(need.get(c, 0), len(v))
        # Ширина — по обрезанным ячейкам, а не по всему столбцу: длинный
        # заголовок в A1 раздвинул бы первый столбец на полстраницы.
        for c, chars in need.items():
            ur.Columns(c + 1).ColumnWidth = min(60, max(widths[c], chars + 2))
        # Высота — одним вызовом на весь лист: строка с фиксированной
        # высотой 13 pt и шрифтом 11 pt печаталась с обрезанным текстом.
        ur.Rows.AutoFit()
    except Exception:  # noqa: BLE001 — не вышло раздвинуть: печатаем как есть
        pass


#: В PDF печатаем тем, что стоит на машине: сначала Office (он рисует ровно
#: так, как документ выглядит у экономиста), потом LibreOffice.
def convert(src, out):
    try:
        import pythoncom
        import win32com.client as win32
    except ImportError:
        return False
    ext = os.path.splitext(src)[1].lower()
    excel = ext in WORKBOOK_TYPES
    pythoncom.CoInitialize()
    app = None
    try:
        # DispatchEx — свой процесс: Dispatch цепляется к открытому Word
        # экономиста, и Quit() ниже закрывает его вместе с документами.
        app = win32.DispatchEx("Excel.Application" if excel else "Word.Application")
        app.Visible = False
        app.DisplayAlerts = 0
        if excel:
            book = app.Workbooks.Open(src, ReadOnly=True, UpdateLinks=0)
            try:
                for ws in book.Worksheets:
                    unclip_columns(ws)
                book.ExportAsFixedFormat(0, out)      # xlTypePDF
            finally:
                book.Close(False)
        else:
            d = app.Documents.Open(src, ReadOnly=True, AddToRecentFiles=False,
                                   Visible=False, ConfirmConversions=False)
            try:
                d.SaveAs2(out, FileFormat=17)         # wdFormatPDF
            finally:
                d.Close(False)
        return os.path.exists(out)
    except Exception:  # noqa: BLE001 — офиса нет или файл не открылся
        return False
    finally:
        try:
            if app is not None:
                app.Quit()
        except Exception:  # noqa: BLE001
            pass
        pythoncom.CoUninitialize()


def to_html(src, out):
    """Документ Word — в отфильтрованный HTML (вид карточки документа)."""
    try:
        import pythoncom
        import win32com.client as win32
    except ImportError:
        return False
    pythoncom.CoInitialize()
    app = None
    try:
        app = win32.DispatchEx("Word.Application")
        app.Visible = False
        app.DisplayAlerts = 0
        d = app.Documents.Open(src, ReadOnly=True, AddToRecentFiles=False,
                               Visible=False, ConfirmConversions=False)
        try:
            d.WebOptions.AllowPNG = True
            d.SaveAs2(out, FileFormat=10)      # wdFormatFilteredHTML
        finally:
            d.Close(False)
        return os.path.exists(out)
    except Exception:  # noqa: BLE001 — Word недоступен
        return False
    finally:
        try:
            if app is not None:
                app.Quit()
        except Exception:  # noqa: BLE001
            pass
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    args = sys.argv[1:]
    run = to_html if args and args[0] == "--html" else convert
    if args and args[0] == "--html":
        args = args[1:]
    sys.exit(0 if run(os.path.abspath(args[0]), os.path.abspath(args[1])) else 1)
