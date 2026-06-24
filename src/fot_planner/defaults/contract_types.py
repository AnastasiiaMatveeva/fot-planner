"""Типы договоров: пресеты для шаблона и дефолты при загрузке Excel."""

from __future__ import annotations

# Базовые значения для неизвестного типа договора (лист contracts без строки в contract_types).
GENERIC_CONTRACT_TYPE_DEFAULT: dict[str, object] = {
    "allow_salary": True,
    "allow_secret": False,
    "allow_allowance": True,
    "allow_incentive": False,
    "allow_extra_work": False,
    "allow_order_incentive": False,
    "staff_limit_sources": "",
    "salary_allowance_limit_sources": "",
    "agreement_staff_limit": None,
    "allowance_requires_salary_contract": True,
    "secret_rate": 0.05,
    "priority_payment_mode": False,
    "salary_anchor_priority": 0,
}

CONTRACT_TYPE_PRESETS: list[dict[str, object]] = [
    {
        "code": "goz",
        "name": "ГОЗ / оборонный заказ",
        "allow_salary": True,
        "allow_secret": False,
        "allow_allowance": True,
        "allow_incentive": False,
        "allow_extra_work": False,
        "allowance_requires_salary_contract": True,
        "staff_limit_sources": "bep",
        "salary_allowance_limit_sources": "",
        "salary_anchor_priority": 0,
    },
    {
        "code": "grant",
        "name": "Грант",
        "allow_salary": True,
        "allow_secret": False,
        "allow_allowance": True,
        "allow_incentive": False,
        "allow_extra_work": False,
        "allowance_requires_salary_contract": True,
        "staff_limit_sources": "",
        "salary_allowance_limit_sources": "2556",
        "salary_anchor_priority": 10,
    },
    {
        "code": "mpt_cooperation",
        "name": "Договор в кооперации с МПТ",
        "allow_salary": True,
        "allow_secret": False,
        "allow_allowance": True,
        "allow_incentive": True,
        "allow_extra_work": False,
        "allowance_requires_salary_contract": True,
        "staff_limit_sources": "p4",
        "salary_allowance_limit_sources": "",
        "salary_anchor_priority": 0,
    },
    {
        "code": "commercial",
        "name": "Коммерческий договор",
        "allow_salary": True,
        "allow_secret": False,
        "allow_allowance": True,
        "allow_incentive": True,
        "allow_extra_work": False,
        "allowance_requires_salary_contract": True,
        "staff_limit_sources": "p4",
        "salary_allowance_limit_sources": "",
        "salary_anchor_priority": 0,
    },
    {
        "code": "priority",
        "name": "Приоритет",
        "allow_salary": True,
        "allow_secret": False,
        "allow_allowance": True,
        "allow_incentive": False,
        "allow_extra_work": True,
        "allowance_requires_salary_contract": True,
        "staff_limit_sources": "",
        "salary_allowance_limit_sources": "2556",
        "priority_payment_mode": True,
        "salary_anchor_priority": 0,
    },
]


def contract_type_defaults_by_code() -> dict[str, dict[str, object]]:
    """Поля типа договора без code/name — для merge при загрузке Excel."""
    result: dict[str, dict[str, object]] = {}
    for preset in CONTRACT_TYPE_PRESETS:
        code = str(preset["code"])
        result[code] = {k: v for k, v in preset.items() if k not in ("code", "name")}
    return result


CONTRACT_TYPE_DEFAULTS: dict[str, dict[str, object]] = contract_type_defaults_by_code()


def normalize_contract_type(value: object) -> str:
    return str(value or "").strip().lower().replace("ё", "е")
