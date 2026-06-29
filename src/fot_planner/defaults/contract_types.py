"""Свободная метка типа договора.

Тип договора больше не задаёт правила и лимиты: правила читаются из явных
колонок листа «договоры» и листа «договоры_ограничения».
"""

from __future__ import annotations


def normalize_contract_type(value: object) -> str:
    return str(value or "").strip().lower().replace("ё", "е")
