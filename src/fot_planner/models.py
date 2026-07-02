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


@dataclass
class Employee:
    """Сотрудник: месячная зарплата (итого в месяц), договоры, запреты."""

    id: str
    full_name: str
    position: str
    department: str
    rate: float
    monthly_wage: float
    start_date: date | None = None
    end_date: date | None = None
    allowed_contracts: list[str] = field(default_factory=list)
    forbidden_contracts: list[str] = field(default_factory=list)
    equivalence_group: str | None = None
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
    equivalence_group: str  # окладная группа: раздел положения + уровень + оклад
    level: int | None = None
    reference_salary_for_rate: float | None = None
    salary_source: str | None = None
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
    contract_type: str  # свободная метка; правила задаются колонками договора и листом договоры_ограничения
    start_date: date
    end_date: date
    total_fot: float
    account: str = ""
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
        """Договор с положительным ФОТ должен быть полностью освоен моделью."""
        return self.total_fot > 0


@dataclass
class ContractPaymentLimit:
    """Связка договора, вида выплаты и применяемого ограничения/документа."""

    contract_id: str
    payment_kind: PaymentKind
    limit_code: str


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
    # Штрафы в целевой функции (не жёсткие ограничения)
    # Мягкий штраф смены договора оклада
    salary_contract_switch: float = 500_000.0
    # Связи сотрудник–договор и смены схемы между месяцами
    admin_complexity: float = 200_000.0
    plan_deviation: float = 10.0
    # Штраф за 100% отклонения от идеала (actual−ideal)/ideal; см. optimizer.UNIFORM_SPEND_TOLERANCE_*.
    uniform_spend_deviation: float = 50_000.0
    labor_deviation: float = 50_000.0
    # Приказ — крайний инструмент: минимизируем число приказов сотрудник-месяц.
    order_incentive_use: float = 1_000_000.0


@dataclass
class SalaryStabilityRules:
    """Ограничения и допуски по окладу (salary)."""

    # Опциональное жёсткое ограничение: сколько договоров с окладом допустимо за год.
    max_contracts_per_year: int | None = None
    # Допуск ±% по чел.-мес. и сумме строки трудоёмкости (все типы договоров)
    goz_labor_tolerance: float = DEFAULT_GOZ_LABOR_TOLERANCE
    # БЭП 550 ВП: предельная средняя зарплата на 1 чел.-мес. по договору ГОЗ.
    goz_average_salary_limit: float = DEFAULT_GOZ_AVERAGE_SALARY_LIMIT
    # Открытые ставки и мультиоклад — всегда включены (не настраиваются).
    enable_open_rates: bool = True


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
    contract_payment_limits: list[ContractPaymentLimit] = field(default_factory=list)
    secret_allowances: list[SecretAllowance] = field(default_factory=list)
    baseline_plan: list[AllocationRecord] | None = None
    # Диагностический режим: разрешить недоплату с большим штрафом (см. allow_deficit)
    allow_deficit: bool = False


@dataclass
class AllocationRecord:
    """Одна строка плана: с какого договора сколько заплатили сотруднику в месяце."""

    employee_id: str  # табельный номер
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
    carryover_allowed: bool = True

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
