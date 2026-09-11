"""Доменные модели для планирования ФОТ."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from fot_planner.salary_limits_2556 import PositionLimit, PositionSalaryLimit
from fot_planner.defaults.settings import (
    DEFAULT_GOZ_AVERAGE_SALARY_LIMIT,
    DEFAULT_GOZ_LABOR_TOLERANCE,
)

from fot_planner.payment_kind import (
    PAYMENT_KINDS,
    STAFF_LIMIT_KINDS,
    SUPPLEMENT_KINDS,
    PaymentKind,
)

EmploymentCategory = Literal["regular", "student", "graduate_student"]
EmploymentType = Literal["auto", "main", "part_time"]
MAX_LABOR_TOLERANCE = 0.05


@dataclass
class Employee:
    """Сотрудник: месячная зарплата (итого в месяц), договоры, запреты."""

    id: str
    full_name: str
    position: str
    department: str
    rate: float
    monthly_wage: float
    # Идентификатор физического человека. ``id`` идентифицирует строку
    # штатного расписания; несколько строк одного человека имеют один
    # табельный номер.
    person_id: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    allowed_contracts: list[str] = field(default_factory=list)
    forbidden_contracts: list[str] = field(default_factory=list)
    equivalence_group: str | None = None
    # Должности, которые этому сотруднику можно дать дополнительно к его
    # собственной (нормализованные названия). Правила направленные: директор
    # может взять ставку научного сотрудника, обратное неверно. Двух ставок по
    # одной должности в одном подразделении у человека не бывает — поэтому в
    # списке у «инженера» стоит «инженер (в другом подразделении)».
    extra_positions: frozenset[str] = field(default_factory=frozenset)
    position_level: int | None = None
    reference_salary_for_rate: float | None = None
    # auto — оптимизатор выбирает; main — основное место; part_time — совместительство
    employment_type: EmploymentType = "auto"
    # regular — до 1.5 в сумме; student — входная ставка фиксируется без открытия доп. ставок;
    # graduate_student — до 0.75.
    employment_category: EmploymentCategory = "regular"


@dataclass
class PositionReference:
    """Строка справочника должностей для совместимости и справочного оклада."""

    position: str
    equivalence_group: str  # окладная группа: страница + номер группы + номер уровня
    level: int | None = None
    reference_salary_for_rate: float | None = None
    salary_page: str | None = None
    salary_group_number: int | None = None


@dataclass
class ContractPositionRule:
    """По договору и должности: потолок месячного оклада для ставки 1.0 (× rate сотрудника)."""

    contract_id: str
    position: str
    max_monthly_payment: float | None = None  # не используется: потолок оклада — в справочнике должностей
    max_positions: float | None = None
    equivalence_group: str | None = None
    position_level: int | None = None
    reference_salary_for_rate: float | None = None


@dataclass
class ContractMonthlyBudget:
    contract_id: str
    year: int
    month: int
    inflow_amount: float
    lock: bool = False
    min_balance: float | None = None


@dataclass
class Contract:
    id: str
    name: str
    number: str
    contract_type: str  # свободная метка; правила задаются колонками договора
    start_date: date
    end_date: date
    total_fot: float
    account: str = ""
    # Подразделение, в котором открываются ставки по договору. Нужно для
    # правила «одна должность в одном подразделении у человека один раз»:
    # вторую ставку по той же должности можно открыть только на договоре
    # другого подразделения. Пусто — правило к договору не применяется.
    department: str = ""
    is_goz_defense_order: bool = False
    allow_salary: bool = True
    allow_secret: bool = False
    allow_allowance: bool = True  # код 122
    allow_incentive: bool = True  # код 124
    allow_extra_work: bool = False
    allow_order_incentive: bool = False  # стимулирующая приказом (верхний уровень зарплаты)
    priority_payment_mode: bool = False
    # Конечная дата выплат с договора (последний допустимый день; None → end_date при загрузке).
    salary_payment_deadline: date | None = None
    allowances_payment_deadline: date | None = None
    position_rules: list[ContractPositionRule] = field(default_factory=list)
    monthly_budgets: list[ContractMonthlyBudget] = field(default_factory=list)
    allow_main_employment: bool = True
    allow_part_time: bool = True

    @property
    def requires_full_fot_spend(self) -> bool:
        """Legacy-флаг: полное освоение больше не является жёстким требованием."""
        return False


@dataclass
class SecretAllowance:
    """Обязательная 120 надбавка сотруднику на период договора секретности."""

    employee_id: str
    secret_contract_id: str
    rate: float = 0.05


@dataclass
class ContractLaborPlan:
    """Строка плановой трудоёмкости по договору и должности/окладной группе."""

    contract_id: str
    year: int
    person_months: float
    position: str | None = None
    equivalence_group: str | None = None
    position_level: int | None = None
    avg_monthly_labor_cost: float | None = None
    # Число привлекаемых специалистов (Ф9, «Расшифровка ФОТ»): сколько людей
    # одновременно может сидеть на строке. Не то же, что чел.-мес.: 10,5
    # чел.-мес. закрывают и трое по 0,5, и один на 1,5.
    headcount: float | None = None
    # План по месяцам {месяц: чел.-мес.}. Этап «4 чел.-мес. с июня по
    # сентябрь» — это 1,0 в каждом из четырёх месяцев, и закрыть их в октябре
    # нельзя. None — строка годовая: чел.-мес. закрываются в любом месяце
    # действия договора. При заданном плане person_months равен его сумме.
    monthly: dict[int, float] | None = None


def labor_row_id(lp: ContractLaborPlan) -> str:
    """Устойчивый идентификатор строки трудоёмкости."""
    return f"{lp.contract_id}|{lp.position or ''}|{lp.equivalence_group or ''}"


@dataclass
class OpenRateAttribution:
    """Открытая ставка сотрудника на договоре в месяце (для отчёта)."""

    employee_id: str
    contract_id: str
    year: int
    month: int
    open_rate: float
    is_main: bool
    position: str | None = None
    equivalence_group: str | None = None


@dataclass
class LaborPmAttribution:
    """Распределение чел.-мес. сотрудника на строку трудоёмкости (для отчёта)."""

    employee_id: str
    contract_id: str
    year: int
    month: int
    labor_row_id: str
    position: str | None
    equivalence_group: str | None
    person_months: float


@dataclass
class LaborPaymentAttribution:
    """Распределение суммы выплаты на строку трудоёмкости (для отчёта)."""

    employee_id: str
    contract_id: str
    year: int
    month: int
    payment_kind: PaymentKind
    labor_row_id: str
    position: str | None
    equivalence_group: str | None
    amount: float


@dataclass
class ManualAssignment:
    """Фиксация: сотрудник на договоре в периоде (оклад и/или 122/124)."""

    employee_id: str
    contract_id: str
    year: int
    month_from: int
    month_to: int
    payment_kind: PaymentKind = PaymentKind.SALARY
    fixed_amount: float | None = None


@dataclass
class ManualProhibition:
    employee_id: str
    contract_id: str
    year: int
    month_from: int = 1
    month_to: int = 12
    payment_kind: PaymentKind | None = None


# Доли одного веса «административная сложность» по техническим компонентам (не настраиваются в Excel).
ADMIN_COMPLEXITY_YEAR_LINK_FACTOR = 1.0
ADMIN_COMPLEXITY_SCHEME_CHANGE_FACTOR = 0.5
ADMIN_COMPLEXITY_FRAGMENT_FACTOR = 0.1


@dataclass
class OptimizationWeights:
    # Веса «вкусовых» целей — одной взвешенной стадии после стадий-правил.
    # Каждая цель нормирована (переводы на число людей, отклонение освоения
    # на ФОТ), поэтому значим только масштаб весов друг относительно друга:
    # «×2» — вдвое важнее. Абсолютные величины ничего не значат.
    salary_contract_switch: float = 500_000.0
    # Связи сотрудник–договор, смены схемы между месяцами, дробления выплат
    admin_complexity: float = 200_000.0
    # Отклонение месячных выплат договора от равномерного освоения
    uniform_spend_deviation: float = 50_000.0
    # Изменение суммы одной и той же выплаты от месяца к месяцу
    payment_change: float = 10_000.0
    # Отклонение от прошлого плана штрафом не держим (0 — стадия выключена):
    # финансист даёт претензию к плану, а не просит его не менять.
    plan_deviation: float = 0.0
    # Веса стадий-правил: там важен только ноль (стадия выключена), масштаб
    # внутри стадии на минимум не влияет.
    labor_deviation: float = 50_000.0
    order_incentive_use: float = 1_000_000.0


@dataclass
class GoalMetric:
    """Одна цель решателя в готовом плане: что вышло и какой вес держал."""

    code: str
    name: str
    value: float          # в естественных единицах: штук, ₽, чел.-мес.
    unit: str
    priority: str         # «стадия 3» или «вес»
    weight: float | None = None
    normalized: float | None = None   # значение, поделённое на масштаб
    contribution: float | None = None  # вес × нормированное — доля в сумме


@dataclass
class AuditViolation:
    """Нарушение правила в готовом плане, найденное проверкой результата.

    Решатель говорит, нашлось ли решение его модели; аудит говорит, годится ли
    это решение по правилам организации. Это разные вопросы: ошибка в модели
    даёт план со статусом OPTIMAL и дробной ставкой, и статус решателя об этом
    молчит. Поэтому нарушение — запись с номером правила, чтобы от него можно
    было дойти до строки свода и до случая стенда.
    """

    rule_id: str          # RATE-001 — номер правила в docs/ПРАВИЛА_РЕШАТЕЛЯ.md
    severity: str         # error — план не годится; warning — требует внимания
    message: str          # человеку: кто, когда, что не так
    employee_id: str | None = None
    contract_id: str | None = None
    month: int | None = None
    actual: float | str | None = None
    expected: str | None = None


@dataclass
class SalaryStabilityRules:
    """Ограничения и допуски по окладу (salary)."""

    # Опциональное жёсткое ограничение: сколько договоров с окладом допустимо за год.
    max_contracts_per_year: int | None = None
    # Допуск ±% по чел.-мес. и сумме строки трудоёмкости (все типы договоров)
    goz_labor_tolerance: float = DEFAULT_GOZ_LABOR_TOLERANCE
    # БЭП 550 ВП: средний лимит штатной части (оклад + 122) для ГОЗ.
    goz_average_salary_limit: float = DEFAULT_GOZ_AVERAGE_SALARY_LIMIT


@dataclass
class PlanningContext:
    year: int
    employees: list[Employee]
    contracts: list[Contract]
    position_reference: list[PositionReference] = field(default_factory=list)
    position_salary_limits: list[PositionSalaryLimit] = field(default_factory=list)
    position_limit_tables: dict[str, list[PositionLimit]] = field(default_factory=dict)
    manual_assignments: list[ManualAssignment] = field(default_factory=list)
    manual_prohibitions: list[ManualProhibition] = field(default_factory=list)
    weights: OptimizationWeights = field(default_factory=OptimizationWeights)
    salary_stability: SalaryStabilityRules = field(default_factory=SalaryStabilityRules)
    labor_plans: list[ContractLaborPlan] = field(default_factory=list)
    # Правила замещения: нормализованная должность сотрудника →
    # нормализованные должности, которые ему можно дать дополнительно. Нужны
    # отчетам, чтобы объяснить, почему сотрудник закрывает чужую строку
    # трудоемкости.
    substitution_rules: dict[str, frozenset[str]] = field(default_factory=dict)
    # Величины, взятые не из входного файла, а из встроенного справочника:
    # (должность, что именно, значение). Без встроенных умолчаний пустой
    # шаблон не считается, но подстановка не должна быть молчаливой — в файле
    # пусто, а в расчете значение есть, и по файлу этого не увидеть.
    defaults_used: list[tuple[str, str, object]] = field(default_factory=list)
    secret_allowances: list[SecretAllowance] = field(default_factory=list)
    baseline_plan: list[AllocationRecord] | None = None
    # Диагностический режим: разрешить недоплату с большим штрафом (см. allow_deficit)
    allow_deficit: bool = False


@dataclass
class AllocationRecord:
    """Одна строка плана: с какого договора сколько заплатили сотруднику в месяце."""

    employee_id: str  # код строки штатного расписания
    contract_id: str  # код договора / проекта
    year: int
    month: int  # 1–12
    payment_kind: PaymentKind  # оклад, 122, 124, 120, 152
    amount: float  # сумма с этого договора
    is_manual: bool = False  # зафиксировано вручную через «ручные_назначения» или «План выплат»
    source: str = "optimizer"


@dataclass
class DeficitRecord:
    """Недоплата сотруднику за месяц (лист «дефициты»); только при allow_deficit."""

    employee_id: str
    year: int
    month: int
    due_amount: float
    paid_amount: float
    amount: float
    payment_kind: PaymentKind | None = None  # None — общий дефицит за месяц
    reasons: list[str] = field(default_factory=list)


@dataclass
class ConflictRecord:
    """Ошибка в данных или нерешаемая задача — расчёт остановлен или план неполный."""

    code: str  # машинный код, напр. UNKNOWN_CONTRACT
    message: str  # текст для человека
    employee_id: str | None = None
    contract_id: str | None = None
    year: int | None = None
    month: int | None = None


@dataclass
class ContractBalanceRecord:
    """
    Касса договора за месяц (не лимит ФОТ по договору).

    Поступления из «фот_по_месяцам», расход по плану и остаток между месяцами.
    В отчёте «проекты_помесячно» другая величина — остаток лимита total_fot.
    """

    contract_id: str
    year: int
    month: int
    opening_balance: float  # на начало месяца (с переноса)
    inflow: float  # поступило в месяце («фот_по_месяцам»)
    spent: float  # ушло на выплаты по плану
    closing_balance: float  # на конец месяца на «счёте» договора
    carried_forward: float = 0.0
    forfeited: float = 0.0
    min_balance_required: float = 0.0  # минимальный остаток на конец месяца («минимальные_остатки»)

    @property
    def balance(self) -> float:
        """Остаток на конец месяца (alias для closing_balance)."""
        return self.closing_balance


@dataclass
class PlanningResult:
    """Итог solve: план выплат, недоплаты, касса по договорам, статус решателя."""

    year: int
    allocations: list[AllocationRecord]  # план_выплат
    deficits: list[DeficitRecord]  # дефициты — не закрыли оклад/надбавку с договоров
    conflicts: list[ConflictRecord]  # conflicts — ошибки
    contract_balances: list[ContractBalanceRecord]  # касса по месяцам
    solver_status: str  # OPTIMAL, FEASIBLE, INFEASIBLE, …
    objective_value: float  # значение целевой функции (сумма штрафов)
    solve_time_sec: float
    payroll_limit_mode: str = "average"
    labor_pm_attributions: list[LaborPmAttribution] = field(default_factory=list)
    labor_payment_attributions: list[LaborPaymentAttribution] = field(default_factory=list)
    open_rate_attributions: list[OpenRateAttribution] = field(default_factory=list)
    # Цели решателя с их значениями в готовом плане — лист «цели» в выгрузке.
    goals: list[GoalMetric] = field(default_factory=list)
    # Проверка результата по правилам организации, отдельно от статуса
    # решателя: «решение модели найдено» и «план годится» — разные вопросы.
    audit_status: str = "NOT_RUN"  # OK | FAIL | NOT_RUN
    audit_violations: list[AuditViolation] = field(default_factory=list)
