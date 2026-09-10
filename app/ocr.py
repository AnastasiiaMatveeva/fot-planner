# -*- coding: utf-8 -*-
"""Чтение сканов: страница PDF → картинка → модель → текст.

Часть документов приходит сканами: у «Справки П4» нет текстового слоя вовсе,
у письма о БЭП он есть, но собран чужим распознаванием и годится только на
выброс — «МИН ИСТRJ>Сrво». Разбирать такие файлы по тексту нельзя, а величины
в них нужны: П4 и БЭП задают пределы выплат.

Здесь страница рисуется картинкой и уходит модели, которая читает изображения.
Ответ — обычный текст, дальше документ разбирается как всякий другой. Признание
модели «не разобрал» отличается от пустой страницы: пустую пропускаем, отказ
возвращаем как есть, чтобы разборщик не выдумывал.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os

#: Сколько страниц читаем: справки и письма умещаются в две-три, а платить
#: за каждую страницу многостраничной формы незачем.
MAX_PAGES = 8
#: Масштаб отрисовки: 2 ≈ 144 dpi — на таком тексты справок читаются, а
#: картинка остаётся в пределах мегабайта.
SCALE = 2

PROMPT = ("Перед тобой страница документа российской организации об оплате "
          "труда. Перепиши её текст: заголовок, реквизиты, все строки таблиц "
          "с числами. Числа переписывай точно, вместе с разрядами и копейками. "
          "Ничего не добавляй от себя. Если страница пустая, ответь «пусто».")


def cache_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "data", "previews")
    os.makedirs(path, exist_ok=True)
    return path


def _cached(path):
    """Имя файла с распознанным текстом: по содержимому, а не по имени."""
    with open(path, "rb") as f:
        digest = hashlib.md5(f.read()).hexdigest()
    return os.path.join(cache_dir(), "ocr_" + digest + ".txt")


def available():
    """Есть ли чем рисовать страницы и кому их показывать."""
    try:
        import pypdfium2  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    import llm

    return bool(llm.vision_model())


def pdf_pages_png(path, pages=MAX_PAGES, scale=SCALE):
    """Страницы PDF картинками PNG (base64), по порядку."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(path)
    out = []
    for i in range(min(len(doc), pages)):
        buf = io.BytesIO()
        doc[i].render(scale=scale).to_pil().save(buf, "PNG")
        out.append(base64.b64encode(buf.getvalue()).decode())
    return out


def text_from_pdf(path, pages=MAX_PAGES):
    """Текст скана постранично. None — распознавать нечем или не вышло.

    Результат кладётся рядом с предпросмотрами: распознавание платное и
    небыстрое, а документ читают несколько раз — при определении вида, при
    разборе и при показе в карточке.
    """
    import llm

    cached = _cached(path)
    if os.path.exists(cached):
        with open(cached, encoding="utf-8") as f:
            return f.read() or None
    if not available():
        return None
    parts = []
    for i, png in enumerate(pdf_pages_png(path, pages), start=1):
        text = llm.read_image(png, PROMPT)
        if not text:
            continue
        if text.strip().lower() in ("пусто", "пусто.", "empty"):
            continue
        parts.append("### Страница %d\n%s" % (i, text.strip()))
    if not parts:
        return None
    out = "\n\n".join(parts)
    with open(cached, "w", encoding="utf-8") as f:
        f.write(out)
    return out
