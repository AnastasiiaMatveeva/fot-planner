"""Парсинг ячеек и канонизация колонок Excel."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from fot_planner.excel.constants import COLUMN_ALIASES

def _canonical_column_name(col) -> str:
    raw = str(col).strip()
    key = raw.lower().replace("ё", "е")
    return COLUMN_ALIASES.get(key, raw)


def _canonicalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    return df.rename(columns={col: _canonical_column_name(col) for col in df.columns})


def _canonicalize_manual_columns(df: pd.DataFrame) -> pd.DataFrame:
    """На листах manual «сотрудник» — табельный номер, не ФИО."""
    out = _canonicalize_columns(df)
    if "employee_id" not in out.columns and "full_name" in out.columns:
        out = out.rename(columns={"full_name": "employee_id"})
    return out


def _parse_date(val) -> date | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    s = str(val).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return pd.to_datetime(s).date()


def _resolve_monthly_wage(row: pd.Series) -> float:
    """Месячная зарплата (итого) из колонки `зарплата` / `monthly_wage`."""
    if "monthly_wage" not in row.index or pd.isna(row.get("monthly_wage")):
        raise ValueError("У сотрудника должна быть колонка «зарплата» (monthly_wage)")
    return float(row["monthly_wage"])


def _split_list(val) -> list[str]:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return []
    return [x.strip() for x in str(val).split(";") if x.strip()]


def _bool(val, default: bool = False) -> bool:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "да", "yes", "y")


def _optional_bool(val):
    if val is None or (isinstance(val, float) and pd.isna(val)) or str(val).strip() == "":
        return None
    return _bool(val)


def _optional_float(val) -> float | None:
    if val is None or (isinstance(val, float) and pd.isna(val)) or str(val).strip() == "":
        return None
    return float(val)


def _optional_int(val) -> int | None:
    if val is None or (isinstance(val, float) and pd.isna(val)) or str(val).strip() == "":
        return None
    return int(float(val))


