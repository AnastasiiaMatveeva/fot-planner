"""Имена листов, алиасы колонок, подписи месяцев."""

from __future__ import annotations

from fot_planner.payment_kind import PaymentKind

SHEET_EMPLOYEES = "employees"
SHEET_CONTRACTS = "contracts"
SHEET_CONTRACT_TYPES = "contract_types"
SHEET_CONTRACT_POSITIONS = "contract_positions"
SHEET_CONTRACT_LABOR = "contract_labor"
SHEET_CONTRACT_BUDGET = "contract_monthly_budget"
SHEET_FOT_MATRIX = "fot_matrix"
SHEET_FOT_LOCK_MATRIX = "fot_lock_matrix"
SHEET_MIN_BALANCE_MATRIX = "min_balance_matrix"
SHEET_MANUAL_ASSIGNMENTS = "manual_assignments"
SHEET_MANUAL_PROHIBITIONS = "manual_prohibitions"
SHEET_SETTINGS = "settings"
SHEET_PLAN = "plan"
SHEET_DEFICITS = "deficits"
SHEET_CONFLICTS = "conflicts"
SHEET_BALANCES = "contract_balances"
SHEET_META = "meta"
SHEET_POSITION_REFERENCE = "справочник_должностей"
SHEET_POSITION_SYNONYMS = "синонимы_должностей"
SHEET_POSITION_SALARY_LIMITS = "должности_лимиты"

COLUMN_ALIASES: dict[str, str] = {
    # Common IDs / dates
    "код": "id",
    "табельный номер": "id",
    "таб. номер": "id",
    "id": "id",
    "фио": "full_name",
    "сотрудник": "full_name",
    "должность": "position",
    "подразделение": "department",
    "кафедра": "department",
    "ставка": "rate",
    "категория занятости": "employment_category",
    "зарплата": "monthly_wage",
    "месячная зарплата": "monthly_wage",
    "дата начала": "start_date",
    "начало": "start_date",
    "дата окончания": "end_date",
    "окончание": "end_date",
    "разрешенные договоры": "allowed_contracts",
    "запрещенные договоры": "forbidden_contracts",
    # Contracts / contract types
    "название": "name",
    "номер": "number",
    "тип договора": "contract_type",
    "фот": "total_fot",
    "фот за год": "total_fot",
    "оклад разрешен": "allow_salary",
    "120 разрешена": "allow_secret",
    "гостайна разрешена": "allow_secret",
    "надбавка разрешена": "allow_allowance",
    "122 разрешена": "allow_allowance",
    "надбавка 122 разрешена": "allow_allowance",
    "124 разрешена": "allow_incentive",
    "надбавка 124 разрешена": "allow_incentive",
    "стимулирующая разрешена": "allow_incentive",  # устаревшее имя колонки
    "152 разрешена": "allow_extra_work",
    "доп работа разрешена": "allow_extra_work",
    "стимулирующая приказом разрешена": "allow_order_incentive",
    "стимулирующая приказ разрешена": "allow_order_incentive",
    "приказ разрешен": "allow_order_incentive",
    "источники лимита шр": "staff_limit_sources",
    "лимит шр": "staff_limit_sources",
    "staff_limit_sources": "staff_limit_sources",
    "источники лимита оклад+122": "salary_allowance_limit_sources",
    "лимит оклад+122": "salary_allowance_limit_sources",
    "salary_allowance_limit_sources": "salary_allowance_limit_sources",
    "лимит соглашения": "agreement_staff_limit",
    "лимит соглашения на ставку": "agreement_staff_limit",
    "agreement_staff_limit": "agreement_staff_limit",
    "122 тот же договор что оклад": "allowance_requires_salary_contract",
    "allowance_requires_salary_contract": "allowance_requires_salary_contract",
    "120 процент": "secret_rate",
    "secret_rate": "secret_rate",
    "режим приоритет": "priority_payment_mode",
    "priority_payment_mode": "priority_payment_mode",
    "приоритет окладного якоря": "salary_anchor_priority",
    "salary_anchor_priority": "salary_anchor_priority",
    "основное место": "allow_main_employment",
    "совместительство": "allow_part_time",
    "allow_main_employment": "allow_main_employment",
    "allow_part_time": "allow_part_time",
    "срок выплат оклада": "salary_payment_deadline",
    "конечная дата выплат оклада": "salary_payment_deadline",
    "конечная дата выплат надбавок": "allowances_payment_deadline",
    "срок выплат надбавок": "allowances_payment_deadline",
    "flex_payment_deadline": "allowances_payment_deadline",
    # Устаревшие колонки (читаются при отсутствии flex_payment_deadline)
    "срок выплат 120": "secret_payment_deadline",
    "срок выплат гостайны": "secret_payment_deadline",
    "срок выплат надбавки": "allowance_payment_deadline",
    "срок выплат 122": "allowance_payment_deadline",
    "срок выплат 124": "incentive_payment_deadline",
    "срок выплат стимулирующей": "incentive_payment_deadline",  # устаревшее
    "срок выплат 152": "extra_work_payment_deadline",
    "срок выплат доп работы": "extra_work_payment_deadline",
    # Contract links / limits
    "договор": "contract_id",
    "проект": "contract_id",
    "макс выплата": "max_monthly_payment",
    "макс ставки": "max_positions",
    "максимум ставок": "max_positions",
    "группа взаимозаменяемости": "equivalence_group",
    "уровень": "position_level",
    "оклад по справочнику за 1 ставку": "reference_salary_for_rate",
    "оклад по справочнику": "reference_salary_for_rate",
    "должностной оклад за 1 ставку": "reference_salary_for_rate",
    "как написано": "raw_position",
    "должность из справочника": "canonical_position",
    # Приказ № 2556
    "категория персонала": "personnel_category",
    "п2556": "order_2556_limit",
    "п3": "p4_limit",
    "п4": "p4_limit",
    "п4 (ср)": "p4_limit",
    "p4_limit": "p4_limit",
    "p3_average": "p4_limit",
    "примечание к п2556": "salary_limit_note",
    "трудоемкость": "person_months",
    "трудоёмкость": "person_months",
    "чел-мес": "person_months",
    "средняя стоимость выполнения работ в месяц": "avg_monthly_labor_cost",
    "средняя зарплата": "avg_monthly_labor_cost",
    "стоимость 1 чел-мес": "avg_monthly_labor_cost",
    "стоимость чел мес": "avg_monthly_labor_cost",
    "месяц": "month",
    # Manual rules / plan
    "месяц с": "month_from",
    "месяц по": "month_to",
    "вид выплаты": "payment_kind",
    "фикс сумма": "fixed_amount",
    "сумма": "amount",
    "фикс": "lock",
    "поступление": "inflow_amount",
    # Settings
    "год": "year",
    "разрешить дефицит": "allow_deficit",
    "штраф дефицита": "weight_deficit_amount",
    "штраф раннего дефицита": "weight_early_deficit",
    "вес штрафа смены оклада": "weight_salary_switch",
    "штраф смены договора оклада": "weight_salary_switch",
    "штраф снижения ставки ниже штатной": "weight_rate_below_staff",
    "макс договоров оклада в год": "max_salary_contracts_per_year",
    "штраф административной сложности выплат": "weight_admin_complexity",
    "штраф дробления переменных выплат": "weight_flex_fragment",
    "штраф дробления надбавок и стимулирующих": "weight_flex_fragment",
    "штраф дробления надбавок": "weight_flex_fragment",
    "фиксировать оклад в квартале": "salary_lock_within_quarter",
    "штраф смены оклада в квартале": "penalize_quarterly_salary_switch",
    "штраф договора сотрудника за год": "weight_employee_contract_year_count",
    "штраф договора сотрудника в месяце": "weight_employee_contract_month_count",
    "weight_flex_payment_fragment_count": "weight_flex_fragment",
    "допуск трудоемкости гоз": "goz_labor_tolerance",
    "допуск трудоемкости": "goz_labor_tolerance",
    "средняя зарплата гоз": "goz_average_salary_limit",
    "бэп 550 вп": "goz_average_salary_limit",
    "goz_average_salary_limit": "goz_average_salary_limit",
    "открытые ставки": "enable_open_rates",
    "enable_open_rates": "enable_open_rates",
    "вес отклонения равномерного освоения": "weight_uniform_spend_deviation",
    "штраф отклонения от равномерного освоения": "weight_uniform_spend_deviation",
    "вес штрафа смены надбавки": "weight_allowance_switch",
    "вес штрафа смены стимулирующей": "weight_incentive_switch",
}

RU_MONTHS = {
    1: "Январь",
    2: "Февраль",
    3: "Март",
    4: "Апрель",
    5: "Май",
    6: "Июнь",
    7: "Июль",
    8: "Август",
    9: "Сентябрь",
    10: "Октябрь",
    11: "Ноябрь",
    12: "Декабрь",
}

PAYMENT_KIND_RU: dict[PaymentKind, str] = {
    PaymentKind.SALARY: "оклад",
    PaymentKind.K120: "120 надбавка (гостайна)",
    PaymentKind.K122: "122 надбавка (качество)",
    PaymentKind.K124: "124 надбавка (интенсивность)",
    PaymentKind.K152: "152 надбавка (доп. работа)",
    PaymentKind.ORDER_INCENTIVE: "стимулирующая приказом",
}

# Синонимы ячеек Excel (коды и русские названия) → PaymentKind.
PAYMENT_KIND_INPUT_ALIASES: dict[str, PaymentKind] = {
    "1": PaymentKind.SALARY,
    "оклад": PaymentKind.SALARY,
    "120": PaymentKind.K120,
    "гостайна": PaymentKind.K120,
    "122": PaymentKind.K122,
    "надбавка": PaymentKind.K122,
    "надбавка 122": PaymentKind.K122,
    "качество": PaymentKind.K122,
    "124": PaymentKind.K124,
    "надбавка 124": PaymentKind.K124,
    "интенсивность": PaymentKind.K124,
    "152": PaymentKind.K152,
    "доп работа": PaymentKind.K152,
    "дополнительная работа": PaymentKind.K152,
    "приказ": PaymentKind.ORDER_INCENTIVE,
    "стимулирующая приказом": PaymentKind.ORDER_INCENTIVE,
    "стимулирующая приказ": PaymentKind.ORDER_INCENTIVE,
}
