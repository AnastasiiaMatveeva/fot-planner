"""Дефолты по типу договора (пустые ячейки на листе contracts)."""

from __future__ import annotations

CONTRACT_TYPE_DEFAULTS: dict[str, dict[str, bool | int | None]] = {
    "goszakaz": {
        "allow_monthly_carryover": True,
        "months_after_end": 0,
        "spend_complete_days_before_end": 20,
        "allow_salary": True,
        "allow_allowance": True,
        "allow_incentive": True,
    },
    "grant": {
        "allow_monthly_carryover": True,
        "months_after_end": 0,
        "spend_complete_days_before_end": None,
        "allow_salary": True,
        "allow_allowance": True,
        "allow_incentive": True,
    },
    "minprom": {
        "allow_monthly_carryover": True,
        "months_after_end": 0,
        "spend_complete_days_before_end": None,
        "allow_salary": True,
        "allow_allowance": True,
        "allow_incentive": True,
    },
    "base": {
        "allow_monthly_carryover": True,
        "months_after_end": 0,
        "spend_complete_days_before_end": None,
        "allow_salary": True,
        "allow_allowance": True,
        "allow_incentive": True,
    },
    "off_budget": {
        "allow_monthly_carryover": True,
        "months_after_end": 2,
        "spend_complete_days_before_end": None,
        "allow_salary": True,
        "allow_allowance": True,
        "allow_incentive": True,
    },
}

KNOWN_CONTRACT_TYPES = frozenset(CONTRACT_TYPE_DEFAULTS)
