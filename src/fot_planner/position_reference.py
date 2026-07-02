"""Справочник должностей и окладных групп.

Справочник использует только данные, нужные оптимизатору:
должность, источник оклада, номер группы, номер уровня, оклад за 1 ставку.
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
    salary_source: str | None = None
    salary_group_number: int | None = None


def normalize_position(value: str | None) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower().replace("ё", "е")
    text = text.replace("—", "-").replace("–", "-")
    text = re.sub(r"[()\[\],;]+", " ", text)
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _format_salary_for_group(value: float | None) -> str | None:
    if value is None:
        return None
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def salary_equivalence_group(
    salary_source: str | None,
    salary_group_number: int | None,
    level: int | None,
    reference_salary_for_rate: float | None,
) -> str:
    """Низкоуровневая окладная группа для совместимости должностей.

    Широкие категории вроде НР/НТП/АУП нужны для справок и лимитов, но для
    открытия ставок нужна более мелкая группировка из положения об оплате труда:
    источник + номер группы + номер уровня + оклад. Поэтому «инженер» и
    «ведущий инженер» оказываются в разных группах, а «инженер» и
    «программист» — в одной.
    """

    parts: list[str] = []
    if salary_source:
        parts.append(str(salary_source).strip())
    if salary_group_number is not None:
        parts.append(f"группа {salary_group_number}")
    if level is not None:
        parts.append(f"уровень {level}")
    salary = _format_salary_for_group(reference_salary_for_rate)
    if salary is not None:
        parts.append(f"оклад {salary}")
    return " / ".join(parts) if parts else "без окладной группы"


def default_position_reference() -> list[PositionReferenceRow]:
    rows: list[PositionReferenceRow] = []
    for (
        position,
        salary_source,
        salary_group_number,
        level,
        reference_salary_for_rate,
    ) in POSITION_REFERENCE_ROWS:
        group = salary_equivalence_group(
            salary_source,
            salary_group_number,
            level,
            reference_salary_for_rate,
        )
        if group == "без окладной группы":
            group = f"должность: {position}"
        rows.append(
            PositionReferenceRow(
                position=position,
                equivalence_group=group,
                level=level,
                reference_salary_for_rate=reference_salary_for_rate,
                salary_source=salary_source,
                salary_group_number=salary_group_number,
            )
        )
    return rows


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
