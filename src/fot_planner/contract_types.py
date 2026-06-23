"""Дефолты по типу договора (пустые ячейки на листе contracts)."""

from __future__ import annotations

CONTRACT_TYPE_DEFAULTS: dict[str, dict[str, bool]] = {
    "goszakaz": {
        "allow_salary": True,
        "allow_allowance": True,
        "allow_incentive": True,
    },
    "goz": {
        "allow_salary": True,
        "allow_allowance": True,
        "allow_incentive": True,
    },
    "grant": {
        "allow_salary": True,
        "allow_allowance": True,
        "allow_incentive": True,
    },
    "minprom": {
        "allow_salary": True,
        "allow_allowance": True,
        "allow_incentive": True,
    },
    "base": {
        "allow_salary": True,
        "allow_allowance": True,
        "allow_incentive": True,
    },
    "off_budget": {
        "allow_salary": True,
        "allow_allowance": True,
        "allow_incentive": True,
    },
}

KNOWN_CONTRACT_TYPES = frozenset(CONTRACT_TYPE_DEFAULTS)
