"""Справочник должностей и групп взаимозаменяемости.

Справочник использует только данные, нужные оптимизатору:
должность, группа взаимозаменяемости, уровень, оклад по справочнику за 1 ставку.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from fot_planner.defaults.position_reference import (
    PERSONNEL_CATEGORIES_BY_POSITION,
    POSITION_REFERENCE_ROWS,
    POSITION_SYNONYMS,
)


@dataclass(frozen=True)
class PositionReferenceRow:
    position: str
    equivalence_group: str
    level: int | None
    reference_salary_for_rate: float | None


def normalize_position(value: str | None) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower().replace("ё", "е")
    text = text.replace("—", "-").replace("–", "-")
    text = re.sub(r"[()\[\],;]+", " ", text)
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def default_position_reference() -> list[PositionReferenceRow]:
    return [PositionReferenceRow(*row) for row in POSITION_REFERENCE_ROWS]


def default_position_synonyms() -> dict[str, str]:
    return {normalize_position(k): normalize_position(v) for k, v in POSITION_SYNONYMS.items()}


def personnel_category_for_position(value: str | None) -> str | None:
    """Категория персонала ШР для известной должности."""
    key = normalize_position(value)
    synonyms = default_position_synonyms()
    canonical = synonyms.get(key, key)
    return PERSONNEL_CATEGORIES_BY_POSITION.get(canonical)


def build_position_index(
    rows: list[PositionReferenceRow],
    synonyms: dict[str, str] | None = None,
) -> dict[str, PositionReferenceRow]:
    synonyms = synonyms or {}
    index: dict[str, PositionReferenceRow] = {}
    for row in rows:
        index[normalize_position(row.position)] = row
    for raw, canonical in synonyms.items():
        target = index.get(normalize_position(canonical))
        if target is not None:
            index[normalize_position(raw)] = target
    return index


def resolve_position(
    value: str | None,
    index: dict[str, PositionReferenceRow],
) -> PositionReferenceRow | None:
    return index.get(normalize_position(value))
