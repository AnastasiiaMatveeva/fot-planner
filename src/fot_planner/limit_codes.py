"""Коды ограничений из Excel."""

from __future__ import annotations


def normalize_limit_code(value: object) -> str:
    text = str(value or "").strip().lower().replace("ё", "е")
    aliases = {
        "п2556": "2556",
        "приказ 2556": "2556",
        "order_2556": "2556",
        "п4": "p4",
        "п3": "p4",
        "p3": "p4",
        "бэп": "bep",
        "бэп 550 вп": "bep",
        "bep 550 vp": "bep",
    }
    return aliases.get(text, text)
