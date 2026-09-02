# -*- coding: utf-8 -*-
"""Совместимость: разбор документов переехал в app/llm.py.

Демонстрация из docs/ui продолжает работать, но реализация теперь одна —
в приложении. Здесь только переадресация, чтобы не держать две копии.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "app"))

from llm import (  # noqa: E402,F401
    FIELDS, JSON_SHAPE, MAX_CHARS, PROVIDER_LABEL, RESPONSE_SCHEMA, SYSTEM,
    available, parse, provider, workbook_to_text,
)
