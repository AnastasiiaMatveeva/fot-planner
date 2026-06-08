"""Имена листов, алиасы колонок, подписи месяцев."""

from __future__ import annotations

SHEET_EMPLOYEES = "employees"
SHEET_CONTRACTS = "contracts"
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
    "надбавка разрешена": "allow_allowance",
    "стимулирующая разрешена": "allow_incentive",
    "срок выплат оклада": "salary_payment_deadline",
    "срок выплат надбавки": "allowance_payment_deadline",
    "срок выплат стимулирующей": "incentive_payment_deadline",
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
    "штраф компенсации оклада надбавкой": "weight_salary_compensation_via_flex",
    "вес штрафа смены оклада": "weight_salary_switch",
    "штраф смены договора оклада": "weight_salary_switch",
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
    "множитель выплаты на чел-мес": "labor_pm_payment_multiplier",
    "макс множитель средней на чел-мес": "labor_pm_payment_multiplier",
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

PAYMENT_KIND_RU = {
    "salary": "оклад",
    "allowance": "надбавка",
    "incentive": "стимулирующая",
}
