"""Имена листов, sheet-aware алиасы колонок, подписи месяцев."""

from __future__ import annotations

from fot_planner.payment_kind import PaymentKind

SHEET_EMPLOYEES = "сотрудники"
SHEET_CONTRACTS = "договоры"
SHEET_SECRET_ALLOWANCES = "120_надбавка"
SHEET_CONTRACT_LABOR = "трудоемкость_по_договорам"
SHEET_FOT_MATRIX = "фот_по_месяцам"
SHEET_FOT_LOCK_MATRIX = "фиксация_фот_по_месяцам"
SHEET_MIN_BALANCE_MATRIX = "минимальные_остатки"
SHEET_MANUAL_ASSIGNMENTS = "ручные_назначения"
SHEET_MANUAL_PROHIBITIONS = "ручные_запреты"
SHEET_SETTINGS = "настройки"
SHEET_PLAN = "План выплат"
SHEET_POSITION_LIMITS = "лимиты_по_должностям"
SHEET_SUBSTITUTIONS = "правила_замещения"

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
    "подразделение": "department",
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
    "проект приоритет": "priority_payment_mode",
    # Так графа называется в шаблоне сервиса и в файлах экономистов. Без
    # этого имени «Приоритет: да» до решателя не доходил, и правила
    # приоритета не включались.
    "приоритет": "priority_payment_mode",
    "основное место разрешено": "allow_main_employment",
    "совместительство разрешено": "allow_part_time",
    "конечная дата выплат оклада": "salary_payment_deadline",
    "конечная дата выплат надбавок": "allowances_payment_deadline",
}

SECRET_ALLOWANCE_COLUMN_ALIASES: dict[str, str] = {
    # Лист SHEET_SECRET_ALLOWANCES / «120_надбавка».
    "сотрудник": "employee_id",
    "договор секретности": "secret_contract_id",
    "ставка 120": "rate",
}

POSITION_LIMITS_COLUMN_ALIASES: dict[str, str] = {
    # Лист SHEET_POSITION_LIMITS / «лимиты_по_должностям».
    "должность": "position",
    "категория персонала": "personnel_category",
    "страница": "salary_page",
    "номер группы": "salary_group_number",
    "номер уровня": "position_level",
    "уровень": "position_level",
    "оклад": "reference_salary_for_rate",
    "п2556": "order_2556_limit",
    "п4": "p4_limit",
    "бэп": "bep_limit",
    "примечание": "note",
}

SUBSTITUTION_COLUMN_ALIASES: dict[str, str] = {
    # Лист SHEET_SUBSTITUTIONS / «правила_замещения».
    "должность": "position",
    "может быть замещена": "substitutes",
    "кем может быть замещена": "substitutes",
    "замещающие должности": "substitutes",
}

CONTRACT_LABOR_COLUMN_ALIASES: dict[str, str] = {
    # Лист SHEET_CONTRACT_LABOR.
    "договор": "contract_id",
    "проект": "contract_id",
    "год": "year",
    "должность": "position",
    "страница": "salary_page",
    "номер группы": "salary_group_number",
    "номер уровня": "position_level",
    "трудоемкость": "person_months",
    "трудоёмкость": "person_months",
    "чел-мес": "person_months",
    "средняя стоимость выполнения работ в месяц": "avg_monthly_labor_cost",
    "средняя зарплата": "avg_monthly_labor_cost",
    "стоимость 1 чел-мес": "avg_monthly_labor_cost",
    "стоимость чел мес": "avg_monthly_labor_cost",
    "количество человек": "headcount",
    "кол-во человек": "headcount",
    "количество привлекаемых специалистов": "headcount",
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
    "штраф смены договора оклада": "weight_salary_switch",
    "макс договоров оклада в год": "max_salary_contracts_per_year",
    "штраф административной сложности выплат": "weight_admin_complexity",
    "штраф отклонения от равномерного освоения": "weight_uniform_spend_deviation",
    "штраф использования приказа": "weight_order_incentive_use",
    "допуск трудоемкости": "goz_labor_tolerance",
    "штраф отклонения трудоемкости": "weight_labor_deviation",
    "штраф нестабильности сумм выплат": "weight_payment_change",
}

COLUMN_ALIASES_BY_SHEET: dict[str, dict[str, str]] = {
    SHEET_EMPLOYEES: EMPLOYEE_COLUMN_ALIASES,
    SHEET_CONTRACTS: CONTRACT_COLUMN_ALIASES,
    SHEET_SECRET_ALLOWANCES: SECRET_ALLOWANCE_COLUMN_ALIASES,
    SHEET_CONTRACT_LABOR: CONTRACT_LABOR_COLUMN_ALIASES,
    SHEET_FOT_MATRIX: BUDGET_COLUMN_ALIASES,
    SHEET_FOT_LOCK_MATRIX: BUDGET_COLUMN_ALIASES,
    SHEET_MIN_BALANCE_MATRIX: BUDGET_COLUMN_ALIASES,
    SHEET_MANUAL_ASSIGNMENTS: MANUAL_RULE_COLUMN_ALIASES,
    SHEET_MANUAL_PROHIBITIONS: MANUAL_RULE_COLUMN_ALIASES,
    SHEET_SETTINGS: SETTINGS_COLUMN_ALIASES,
    SHEET_PLAN: PLAN_OVERRIDE_COLUMN_ALIASES,
    SHEET_POSITION_LIMITS: POSITION_LIMITS_COLUMN_ALIASES,
    SHEET_SUBSTITUTIONS: SUBSTITUTION_COLUMN_ALIASES,
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

# Официальные текстовые значения ячеек Excel → PaymentKind.
# Числовые коды 0/1/120/122/124/152 разбираются отдельно в parse_payment_kind.
PAYMENT_KIND_INPUT_ALIASES: dict[str, PaymentKind] = {
    label.lower().replace("ё", "е"): kind
    for kind, label in PAYMENT_KIND_RU.items()
}
