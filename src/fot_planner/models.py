"""Доменные модели для планирования ФОТ."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

PaymentKind = Literal["salary", "allowance", "incentive"]
PAYMENT_KINDS: tuple[PaymentKind, ...] = ("salary", "allowance", "incentive")
EmploymentCategory = Literal["regular", "student", "graduate_student"]


@dataclass
class PaymentKindTerms:
    """Срок выплат по виду с договора (последний допустимый день)."""

    payment_deadline: date | None = None


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
    # regular — до 1.5 в сумме; student — 0.5; graduate_student — 0.75
    employment_category: EmploymentCategory = "regular"


@dataclass
class PositionReference:
    """Строка справочника должностей для совместимости и справочного оклада."""

    position: str
    equivalence_group: str
    level: int | None = None
    reference_salary_for_rate: float | None = None


@dataclass
class ContractPositionRule:
    """По договору и должности: потолок месячного оклада для ставки 1.0 (× rate сотрудника)."""

    contract_id: str
    position: str
    max_monthly_payment: float | None = None
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
    contract_type: str  # справочная метка (goszakaz, grant, off_budget…); правила — в колонках ниже
    start_date: date
    end_date: date
    total_fot: float
    allow_salary: bool = True
    allow_allowance: bool = True
    allow_incentive: bool = True
    require_salary_reserve: bool | None = None  # legacy: читается из старых Excel
    salary_terms: PaymentKindTerms = field(default_factory=PaymentKindTerms)
    allowance_terms: PaymentKindTerms = field(default_factory=PaymentKindTerms)
    incentive_terms: PaymentKindTerms = field(default_factory=PaymentKindTerms)
    position_rules: list[ContractPositionRule] = field(default_factory=list)
    monthly_budgets: list[ContractMonthlyBudget] = field(default_factory=list)
    allow_main_employment: bool = True
    allow_part_time: bool = True

    @property
    def requires_full_fot_spend(self) -> bool:
        """Договор с положительным ФОТ должен быть полностью освоен моделью."""
        return self.total_fot > 0


@dataclass
class ContractLaborPlan:
    """Строка плановой трудоёмкости по договору и должности/группе."""

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
    """Фиксация: сотрудник на договоре в периоде (оклад и/или стимулирующие)."""

    employee_id: str
    contract_id: str
    year: int
    month_from: int
    month_to: int
    payment_kind: PaymentKind = "salary"
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
    # Диагностический дефицит (allow_deficit): этап 1 — сумма; этап 2 — ранний дефицит
    deficit_amount: float = 1_000_000_000.0
    early_deficit: float = 10_000_000.0
    # Штраф за вынужденный добор оклада через allowance/incentive (низкий salary_cap)
    salary_compensation_via_flex: float = 3_000.0
    # Мягкий штраф смены договора оклада
    salary_contract_switch: float = 500_000.0
    # Связи сотрудник–договор и смены схемы между месяцами
    admin_complexity: float = 200_000.0
    # Legacy: читается из старых Excel, в оптимизаторе не используется (фрагменты — через admin_complexity).
    flex_fragment: float = 200_000.0
    plan_deviation: float = 10.0
    # Штраф за 100% отклонения от идеала (actual−ideal)/ideal; см. optimizer.UNIFORM_SPEND_TOLERANCE_*.
    uniform_spend_deviation: float = 50_000.0
    labor_deviation: float = 50_000.0


@dataclass
class SalaryStabilityRules:
    """Ограничения и допуски по окладу (salary)."""

    # Legacy: опциональное жёсткое ограничение из старых Excel; None — выключено
    max_contracts_per_year: int | None = None
    # Legacy: читается из старых Excel (ограничение в модели снято)
    min_fot_months_for_salary_reserve: int = 6
    # Допуск ±% по чел.-мес. и сумме строки трудоёмкости (все типы договоров)
    goz_labor_tolerance: float = 0.05
    # Верхняя граница отнесённой суммы на строку в месяце: multiplier × средняя × чел.-мес.
    labor_pm_payment_multiplier: float = 5.0
    # Открытые ставки: основное место + совместительство (см. open_rate_rules).
    enable_open_rates: bool = False


@dataclass
class PlanningContext:
    year: int
    employees: list[Employee]
    contracts: list[Contract]
    position_reference: list[PositionReference] = field(default_factory=list)
    manual_assignments: list[ManualAssignment] = field(default_factory=list)
    manual_prohibitions: list[ManualProhibition] = field(default_factory=list)
    weights: OptimizationWeights = field(default_factory=OptimizationWeights)
    salary_stability: SalaryStabilityRules = field(default_factory=SalaryStabilityRules)
    labor_plans: list[ContractLaborPlan] = field(default_factory=list)
    baseline_plan: list[AllocationRecord] | None = None
    # Диагностический режим: разрешить недоплату с большим штрафом (см. allow_deficit)
    allow_deficit: bool = False
    # Legacy: перенос из будущего в прошлое отключён; флаг читается из старых Excel
    allow_backward_reallocation: bool = False


@dataclass
class AllocationRecord:
    """Одна строка плана: с какого договора сколько заплатили сотруднику в месяце."""

    employee_id: str  # табельный номер
    contract_id: str  # код договора / проекта
    year: int
    month: int  # 1–12
    payment_kind: PaymentKind  # оклад, надбавка или стимулирующая
    amount: float  # сумма с этого договора
    is_manual: bool = False  # зафиксировано вручную (manual_assignments / lock в plan)
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

    Поступления из fot_matrix, расход по плану, остаток и переносы между месяцами.
    В отчёте «проекты_помесячно» другая величина — остаток лимита total_fot.
    """

    contract_id: str
    year: int
    month: int
    opening_balance: float  # на начало месяца (с переноса)
    inflow: float  # поступило в месяце (fot_matrix)
    spent: float  # ушло на выплаты по плану
    closing_balance: float  # на конец месяца на «счёте» договора
    carried_forward: float = 0.0
    forfeited: float = 0.0
    salary_reserve_required: float = 0.0  # legacy: в отчёте не выводится
    min_balance_required: float = 0.0  # минимальный остаток на конец месяца (min_balance_matrix)
    transfer_in: float = 0.0  # legacy: перенос назад отключён
    transfer_out: float = 0.0  # legacy: перенос назад отключён
    movable_balance: float = 0.0  # legacy: лимит переноса назад
    carryover_allowed: bool = True

    @property
    def balance(self) -> float:
        """Остаток на конец месяца (alias для closing_balance)."""
        return self.closing_balance


@dataclass
class MonthTransfer:
    """Legacy: перенос из будущего в прошлое; в новых отчётах не выводится."""

    contract_id: str
    year: int
    from_month: int  # откуда «взяли» кассу
    to_month: int  # куда зачли (обычно to_month < from_month)
    amount: float


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
    month_transfers: list[MonthTransfer] = field(default_factory=list)
    labor_pm_attributions: list[LaborPmAttribution] = field(default_factory=list)
    labor_payment_attributions: list[LaborPaymentAttribution] = field(default_factory=list)
    open_rate_attributions: list[OpenRateAttribution] = field(default_factory=list)
