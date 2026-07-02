"""Имена листов, sheet-aware алиасы колонок, подписи месяцев."""

from __future__ import annotations

from fot_planner.payment_kind import PaymentKind

SHEET_EMPLOYEES = "сотрудники"
SHEET_CONTRACTS = "договоры"
SHEET_CONTRACT_PAYMENT_LIMITS = "договоры_ограничения"
SHEET_SECRET_ALLOWANCES = "120_надбавка"
SHEET_CONTRACT_POSITIONS = "должности"
SHEET_CONTRACT_LABOR = "трудоемкость_по_договорам"
SHEET_FOT_MATRIX = "фот_по_месяцам"
SHEET_FOT_LOCK_MATRIX = "фиксация_фот_по_месяцам"
SHEET_MIN_BALANCE_MATRIX = "минимальные_остатки"
SHEET_MANUAL_ASSIGNMENTS = "ручные_назначения"
SHEET_MANUAL_PROHIBITIONS = "ручные_запреты"
SHEET_SETTINGS = "настройки"
SHEET_PLAN = "План выплат"
SHEET_POSITION_LIMITS = "лимиты_по_должностям"

EMPLOYEE_COLUMN_ALIASES: dict[str, str] = {
    # Лист SHEET_EMPLOYEES / «сотрудники».
    "код строки": "id",
    "фио": "full_name",
    "должность": "position",
    "подразделение": "department",
    "ставка": "rate",
    "тип занятости": "employment_type",
    "категория занятости": "employment_category",
    "зарплата": "monthly_wage",
    "месячная зарплата": "monthly_wage",
    "monthly_wage": "monthly_wage",
    "дата начала": "start_date",
    "дата окончания": "end_date",
    "разрешенные договоры": "allowed_contracts",
    "запрещенные договоры": "forbidden_contracts",
}

CONTRACT_COLUMN_ALIASES: dict[str, str] = {
    # Лист SHEET_CONTRACTS / «договоры».
    "код": "id",
    "номер договора": "id",
    "название": "name",
    "номер": "number",
    "тип договора": "contract_type",
    "счет": "account",
    "гоз": "is_goz_defense_order",
    "дата начала": "start_date",
    "дата окончания": "end_date",
    "фот": "total_fot",
    "оклад разрешен": "allow_salary",
    "120 разрешена": "allow_secret",
    "122 разрешена": "allow_allowance",
    "124 разрешена": "allow_incentive",
    "152 разрешена": "allow_extra_work",
    "стимулирующая приказом разрешена": "allow_order_incentive",
    "проект Приоритет": "priority_payment_mode",
    "основное место разрешено": "allow_main_employment",
    "совместительство разрешено": "allow_part_time",
    "конечная дата выплат оклада": "salary_payment_deadline",
    "конечная дата выплат надбавок": "allowances_payment_deadline",
}

CONTRACT_PAYMENT_LIMIT_COLUMN_ALIASES: dict[str, str] = {
    # Лист SHEET_CONTRACT_PAYMENT_LIMITS / «договоры_ограничения».
    "договор": "contract_id",
    "выплата": "payment_kind",
    "ограничение": "limit_code",
}

SECRET_ALLOWANCE_COLUMN_ALIASES: dict[str, str] = {
    # Лист SHEET_SECRET_ALLOWANCES / «120_надбавка».
    "сотрудник": "employee_id",
    "договор секретности": "secret_contract_id",
    "ставка 120": "rate",
}

CONTRACT_POSITION_COLUMN_ALIASES: dict[str, str] = {
    # Лист SHEET_CONTRACT_POSITIONS / «должности».
    "договор": "contract_id",
    "проект": "contract_id",
    "должность": "position",
    "макс выплата": "max_monthly_payment",
    "макс ставки": "max_positions",
    "максимум ставок": "max_positions",
    "окладная группа": "equivalence_group",
    "группа взаимозаменяемости": "equivalence_group",
    "источник оклада": "salary_source",
    "страница": "salary_source",
    "номер группы": "salary_group_number",
    "номер уровня": "position_level",
    "уровень": "position_level",
    "оклад": "reference_salary_for_rate",
    "оклад по справочнику за 1 ставку": "reference_salary_for_rate",
    "оклад по справочнику": "reference_salary_for_rate",
    "должностной оклад за 1 ставку": "reference_salary_for_rate",
    "как написано": "raw_position",
    "должность из справочника": "canonical_position",
}

POSITION_LIMITS_COLUMN_ALIASES: dict[str, str] = {
    # Лист SHEET_POSITION_LIMITS / «лимиты_по_должностям».
    "должность": "position",
    "категория персонала": "personnel_category",
    "источник оклада": "salary_source",
    "страница": "salary_source",
    "номер группы": "salary_group_number",
    "номер уровня": "position_level",
    "окладная группа": "equivalence_group",
    "группа взаимозаменяемости": "equivalence_group",
    "уровень": "position_level",
    "оклад": "reference_salary_for_rate",
    "п2556": "order_2556_limit",
    "п4": "p4_limit",
    "бэп": "bep_limit",
    "примечание": "note",
}

LIMIT_TABLE_COLUMN_ALIASES: dict[str, str] = {
    # Листы ограничений, имя листа = код ограничения: «2556», «p4», «grant_technet» и т.п.
    "должность": "position",
    "категория персонала": "personnel_category",
    "лимит на 1 ставку": "limit",
    "лимит": "limit",
    "сумма": "limit",
    "значение": "limit",
    "примечание": "note",
}

CONTRACT_LABOR_COLUMN_ALIASES: dict[str, str] = {
    # Лист SHEET_CONTRACT_LABOR.
    "договор": "contract_id",
    "проект": "contract_id",
    "год": "year",
    "должность": "position",
    "окладная группа": "equivalence_group",
    "источник оклада": "salary_source",
    "страница": "salary_source",
    "номер группы": "salary_group_number",
    "номер уровня": "position_level",
    "трудоемкость": "person_months",
    "трудоёмкость": "person_months",
    "чел-мес": "person_months",
    "оклад": "reference_salary_for_rate",
    "средняя стоимость выполнения работ в месяц": "avg_monthly_labor_cost",
    "средняя зарплата": "avg_monthly_labor_cost",
    "стоимость 1 чел-мес": "avg_monthly_labor_cost",
    "стоимость чел мес": "avg_monthly_labor_cost",
}

MANUAL_RULE_COLUMN_ALIASES: dict[str, str] = {
    # Листы SHEET_MANUAL_ASSIGNMENTS / SHEET_MANUAL_PROHIBITIONS.
    # Здесь «сотрудник» — табельный номер, а не ФИО.
    "код": "employee_id",
    "табельный номер": "employee_id",
    "таб. номер": "employee_id",
    "id": "employee_id",
    "сотрудник": "employee_id",
    "договор": "contract_id",
    "проект": "contract_id",
    "год": "year",
    "месяц с": "month_from",
    "месяц по": "month_to",
    "вид выплаты": "payment_kind",
    "фикс сумма": "fixed_amount",
    "сумма": "amount",
}

BUDGET_COLUMN_ALIASES: dict[str, str] = {
    # Листы с поступлениями/фиксацией ФОТ.
    "договор": "contract_id",
    "проект": "contract_id",
    "год": "year",
    "месяц": "month",
    "фикс": "lock",
    "поступление": "inflow_amount",
}

PLAN_OVERRIDE_COLUMN_ALIASES: dict[str, str] = {
    # Лист SHEET_PLAN / «План выплат» при пересчёте с фиксацией результата.
    "код строки": "employee_id",
    "код": "employee_id",
    "табельный номер": "employee_id",
    "таб. номер": "employee_id",
    "id": "employee_id",
    "сотрудник": "employee_id",
    "договор": "contract_id",
    "проект": "contract_id",
    "год": "year",
    "месяц": "month",
    "вид выплаты": "payment_kind",
    "сумма": "amount",
    "фикс": "lock",
    "зафиксировано": "lock",
}

SETTINGS_COLUMN_ALIASES: dict[str, str] = {
    # Лист SHEET_SETTINGS.
    "год": "year",
    "разрешить дефицит": "allow_deficit",
    "штраф дефицита": "weight_deficit_amount",
    "штраф раннего дефицита": "weight_early_deficit",
    "вес штрафа смены оклада": "weight_salary_switch",
    "штраф смены договора оклада": "weight_salary_switch",
    "штраф использования приказа": "weight_order_incentive_use",
    "штраф приказа": "weight_order_incentive_use",
    "штраф стимулирующей приказом": "weight_order_incentive_use",
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

COLUMN_ALIASES_BY_SHEET: dict[str, dict[str, str]] = {
    SHEET_EMPLOYEES: EMPLOYEE_COLUMN_ALIASES,
    SHEET_CONTRACTS: CONTRACT_COLUMN_ALIASES,
    SHEET_CONTRACT_PAYMENT_LIMITS: CONTRACT_PAYMENT_LIMIT_COLUMN_ALIASES,
    SHEET_SECRET_ALLOWANCES: SECRET_ALLOWANCE_COLUMN_ALIASES,
    SHEET_CONTRACT_POSITIONS: CONTRACT_POSITION_COLUMN_ALIASES,
    SHEET_CONTRACT_LABOR: CONTRACT_LABOR_COLUMN_ALIASES,
    SHEET_FOT_MATRIX: BUDGET_COLUMN_ALIASES,
    SHEET_FOT_LOCK_MATRIX: BUDGET_COLUMN_ALIASES,
    SHEET_MIN_BALANCE_MATRIX: BUDGET_COLUMN_ALIASES,
    SHEET_MANUAL_ASSIGNMENTS: MANUAL_RULE_COLUMN_ALIASES,
    SHEET_MANUAL_PROHIBITIONS: MANUAL_RULE_COLUMN_ALIASES,
    SHEET_SETTINGS: SETTINGS_COLUMN_ALIASES,
    SHEET_PLAN: PLAN_OVERRIDE_COLUMN_ALIASES,
    SHEET_POSITION_LIMITS: POSITION_LIMITS_COLUMN_ALIASES,
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
