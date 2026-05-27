"""Доменные модели для планирования ФОТ."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

PaymentKind = Literal["salary", "allowance", "incentive"]


@dataclass
class Employee:
    """Сотрудник: оклад, надбавка, стимулирующие, договоры, запреты."""

    id: str
    full_name: str
    position: str
    department: str
    rate: float
    salary: float
    allowance: float = 0.0
    incentive: float = 0.0
    start_date: date | None = None
    end_date: date | None = None
    allowed_contracts: list[str] = field(default_factory=list)
    forbidden_contracts: list[str] = field(default_factory=list)
    equivalence_group: str | None = None
    position_level: int | None = None
    reference_salary_for_rate: float | None = None


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
    spend_deadline: date | None
    total_fot: float
    allow_salary: bool = True
    allow_allowance: bool = True
    allow_incentive: bool = True
    months_after_end: int = 0  # 0 — до end_date; 2 — +2 мес.; -1 в Excel → 0 + освоение за 20 дней
    allow_monthly_carryover: bool = True  # перенос кассы между месяцами
    require_salary_reserve: bool | None = None  # None — по settings (месяцев с ФОТ)
    # None — не требуем полное освоение; 0 — к сроку; N>0 — за N дней до срока
    spend_complete_days_before_end: int | None = None
    position_rules: list[ContractPositionRule] = field(default_factory=list)
    monthly_budgets: list[ContractMonthlyBudget] = field(default_factory=list)


@dataclass
class ContractLaborPlan:
    """Плановая трудоёмкость: чел.-мес. с привязкой к группе должностей."""

    contract_id: str
    year: int
    person_months: float
    position: str | None = None
    equivalence_group: str | None = None
    position_level: int | None = None


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


@dataclass
class OptimizationWeights:
    # Штрафы в целевой функции (не жёсткие ограничения)
    uncovered_salary: float = 10_000.0
    # Штраф за выплату сверх справочной надбавки/стимулирующей (компенсация срезанного оклада)
    salary_compensation_via_flex: float = 3_000.0
    # Мягкий штраф смены договора оклада (только если max_contracts_per_year > 1)
    salary_contract_switch: float = 500_000.0
    # Связи сотрудник–договор и смены схемы между месяцами
    admin_complexity: float = 200_000.0
    # Штраф за каждый use по allowance/incentive (число фрагментов, не сумма выплаты)
    flex_fragment: float = 200_000.0
    plan_deviation: float = 10.0
    uniform_spend_deviation: float = 10_000.0
    labor_deviation: float = 50_000.0


@dataclass
class SalaryStabilityRules:
    """Ограничения и допуски по окладу (salary)."""

    max_contracts_per_year: int = 2
    # Резерв и привязка окладов: если месяцев с поступлением в fot_matrix строго больше этого числа
    min_fot_months_for_salary_reserve: int = 6
    goz_labor_tolerance: float = 0.05


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
    # Физический перенос денег из будущего месяца в прошлый (xfer); по умолчанию выключен
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
    """
    Недоплата сотруднику за месяц (лист «дефициты» в Excel-результате).

    В карточке сотрудника, например, оклад 100 000 ₽/мес. С договоров в плане
    набралось только 70 000 ₽ — сюда пишут недостающие 30 000 ₽ (amount).
    Это не минус на договоре, а разрыв между «сколько должен получить» и
    «сколько удалось повесить на проекты».

    В модели: Σ выплат с договоров за месяц + deficit = оклад + надбавка + стимулирующая
    из справочника. Оптимизатор сильно штрафует deficit (вес uncovered_salary).
    """

    employee_id: str
    year: int
    month: int  # в каком месяце не хватило денег с договоров
    amount: float  # рублей, которые не назначили с договоров
    payment_kind: PaymentKind | None = None  # None — суммарный месячный дефицит
    reasons: list[str] = field(default_factory=list)  # подсказки: нет кассы, запрет, резерв…


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
    salary_reserve_required: float = 0.0
    min_balance_required: float = 0.0  # не отдавать на более ранние месяцы (min_balance_matrix)
    transfer_in: float = 0.0
    transfer_out: float = 0.0
    movable_balance: float = 0.0  # лимит переноса на месяцы до текущего
    carryover_allowed: bool = True

    @property
    def balance(self) -> float:
        """Остаток на конец месяца (alias для closing_balance)."""
        return self.closing_balance


@dataclass
class MonthTransfer:
    """Сколько денег перенесли с более позднего месяца на более ранний (для листа «переносы»)."""

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
