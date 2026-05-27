"""Демонстрационный бизнес-сценарий: данные и ожидаемая логика (без хардкода в тестах)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Iterable

DEMO_YEAR = 2026
ANCHOR_CONTRACT = "C_BASE"
PROJECT_CONTRACT = "C_PROJECT"
LAB_CONTRACT = "C_LAB"
ENGINEER_GROUP = "инженеры"
LAB_GROUP = "лаборанты"
MIN_FLEX_FRAGMENT = 1_000.0


@dataclass(frozen=True)
class PositionRef:
    position: str
    group: str
    level: int
    reference_salary: float


@dataclass(frozen=True)
class EmployeeSpec:
    id: str
    full_name: str
    position: str
    department: str
    salary: float
    allowance: float
    group: str

    @property
    def monthly_wage(self) -> float:
        return self.salary + self.allowance


@dataclass(frozen=True)
class ContractPositionSpec:
    position: str
    max_positions: float
    max_monthly_payment: float


@dataclass(frozen=True)
class LaborRowSpec:
    position: str
    person_months: float
    avg_monthly_cost: float

    @property
    def plan_amount(self) -> float:
        return self.person_months * self.avg_monthly_cost


@dataclass(frozen=True)
class ContractSpec:
    id: str
    name: str
    number: str
    contract_type: str
    start: date
    end: date
    total_fot: float
    inflows: dict[int, float]
    positions: tuple[ContractPositionSpec, ...]
    labor: tuple[LaborRowSpec, ...] = ()

    def active_months(self, year: int) -> tuple[int, ...]:
        months = []
        for m in range(1, 13):
            d = date(year, m, 15)
            if self.start <= d <= self.end:
                months.append(m)
        return tuple(months)

    def inflow(self, month: int) -> float:
        return self.inflows.get(month, 0.0)


@dataclass(frozen=True)
class SettingsSpec:
    allow_deficit: bool = False
    admin_complexity_weight: float = 200_000.0
    salary_switch_weight: float = 500_000.0
    uniform_weight: float = 50_000.0


@dataclass(frozen=True)
class DemoScenario:
    year: int
    settings: SettingsSpec
    positions: tuple[PositionRef, ...]
    employees: tuple[EmployeeSpec, ...]
    contracts: tuple[ContractSpec, ...]

    def contract(self, contract_id: str) -> ContractSpec:
        for c in self.contracts:
            if c.id == contract_id:
                return c
        raise KeyError(contract_id)

    def employee(self, employee_id: str) -> EmployeeSpec:
        for e in self.employees:
            if e.id == employee_id:
                return e
        raise KeyError(employee_id)

    def salary_cap(self, employee_id: str, contract_id: str) -> float | None:
        emp = self.employee(employee_id)
        contract = self.contract(contract_id)
        exact = [
            pos
            for pos in contract.positions
            if pos.position == emp.position
        ]
        if exact:
            caps = [p.max_monthly_payment for p in exact if p.max_monthly_payment is not None]
            if caps:
                return max(caps)
        group = [
            pos
            for pos in contract.positions
            if emp.group
            and pos.position != emp.position
            and self._position_group(pos.position) == emp.group
        ]
        caps = [p.max_monthly_payment for p in group if p.max_monthly_payment is not None]
        return max(caps) if caps else None

    def _position_group(self, position: str) -> str | None:
        for p in self.positions:
            if p.position == position:
                return p.group
        return None

    def required_salary(self, employee_id: str, contract_id: str) -> float:
        emp = self.employee(employee_id)
        cap = self.salary_cap(employee_id, contract_id)
        if cap is None:
            return emp.salary
        return min(emp.salary, cap)

    def anchor_contract_id(self) -> str:
        return ANCHOR_CONTRACT

    def engineer_ids(self) -> tuple[str, ...]:
        return tuple(e.id for e in self.employees if e.group == ENGINEER_GROUP)

    def is_engineer(self, employee_id: str) -> bool:
        return self.employee(employee_id).group == ENGINEER_GROUP

    def project_months(self) -> tuple[int, ...]:
        return self.contract(PROJECT_CONTRACT).active_months(self.year)

    def lab_months(self) -> tuple[int, ...]:
        return self.contract(LAB_CONTRACT).active_months(self.year)

    def expected_salary_contract(self, employee_id: str) -> str:
        return self.anchor_contract_id()

    def expected_allowance_contract(self, employee_id: str, month: int) -> str | None:
        emp = self.employee(employee_id)
        if emp.allowance <= 0:
            return None
        if emp.group == ENGINEER_GROUP:
            if month in self.project_months():
                return PROJECT_CONTRACT
            return ANCHOR_CONTRACT
        if emp.group == LAB_GROUP:
            if month in self.lab_months():
                return LAB_CONTRACT
            return ANCHOR_CONTRACT
        return ANCHOR_CONTRACT

    def expected_monthly_allowance(self, employee_id: str) -> float:
        return self.employee(employee_id).allowance

    def expected_monthly_salary(self, employee_id: str) -> float:
        return self.required_salary(employee_id, self.anchor_contract_id())

    def expected_monthly_due(self, employee_id: str) -> float:
        return self.employee(employee_id).monthly_wage

    def monthly_engineer_allowance_total(self) -> float:
        return sum(self.expected_monthly_allowance(eid) for eid in self.engineer_ids())

    def compute_base_labor(self) -> tuple[LaborRowSpec, ...]:
        """Строки трудоёмкости C_BASE: оклады + надбавки вне проектов, с учётом доли на других договорах."""
        anchor = self.anchor_contract_id()
        project_months = set(self.project_months())
        lab_months = set(self.lab_months())
        project_pm_total = sum(r.person_months for r in self.contract(PROJECT_CONTRACT).labor)
        lab_pm_total = sum(r.person_months for r in self.contract(LAB_CONTRACT).labor)
        project_month_count = len(project_months) or 1
        lab_month_count = len(lab_months) or 1
        engineer_count = len(self.engineer_ids()) or 1

        pm_by_position: dict[str, float] = {}
        amount_by_position: dict[str, float] = {}
        for emp in self.employees:
            emp_pm = 0.0
            for m in range(1, 13):
                elsewhere = 0.0
                if emp.group == ENGINEER_GROUP and m in project_months and project_pm_total > 0:
                    elsewhere += project_pm_total / project_month_count / engineer_count
                if emp.group == LAB_GROUP and m in lab_months and lab_pm_total > 0:
                    elsewhere += lab_pm_total / lab_month_count
                emp_pm += max(0.0, 1.0 - elsewhere)
            pm_by_position[emp.position] = pm_by_position.get(emp.position, 0.0) + emp_pm
            amount_by_position[emp.position] = (
                amount_by_position.get(emp.position, 0.0)
                + self.expected_monthly_salary(emp.id) * 12
            )
            for m in range(1, 13):
                if self.expected_allowance_contract(emp.id, m) == anchor:
                    amount_by_position[emp.position] = (
                        amount_by_position.get(emp.position, 0.0) + emp.allowance
                    )
        rows: list[LaborRowSpec] = []
        for pos in sorted(pm_by_position):
            pm = pm_by_position[pos]
            amount = amount_by_position[pos]
            rows.append(LaborRowSpec(pos, pm, amount / pm if pm > 0 else 0.0))
        return tuple(rows)

    def compute_anchor_total_fot(self) -> float:
        """ФОТ якорного договора: годовые оклады + надбавки вне проектных договоров."""
        total = 0.0
        project_months = set(self.project_months())
        lab_months = set(self.lab_months())
        for emp in self.employees:
            total += self.expected_monthly_salary(emp.id) * 12
            for m in range(1, 13):
                ac = self.expected_allowance_contract(emp.id, m)
                if ac == ANCHOR_CONTRACT:
                    total += emp.allowance
        return total

    def compute_project_fot(self) -> float:
        return self.monthly_engineer_allowance_total() * len(self.project_months())

    def compute_lab_fot(self) -> float:
        emp = next(e for e in self.employees if e.group == LAB_GROUP)
        return emp.allowance * len(self.lab_months())

    def expected_labor_shortfall(self) -> float:
        """Недостаток денег на проектной строке относительно плановой суммы."""
        project = self.contract(PROJECT_CONTRACT)
        if not project.labor:
            return 0.0
        plan = sum(row.plan_amount for row in project.labor)
        available = project.total_fot
        return max(0.0, plan - available)

    def with_deficit_variant(self, project_fot: float, project_inflow_month: int = 4) -> DemoScenario:
        contracts = []
        for c in self.contracts:
            if c.id == PROJECT_CONTRACT:
                contracts.append(
                    replace(
                        c,
                        total_fot=project_fot,
                        inflows={project_inflow_month: project_fot},
                    )
                )
            else:
                contracts.append(c)
        return replace(
            self,
            settings=replace(self.settings, allow_deficit=True),
            contracts=tuple(contracts),
        )


def build_demo_scenario(*, allow_deficit: bool = False, project_fot: float | None = None) -> DemoScenario:
    year = DEMO_YEAR
    positions = (
        PositionRef("инженер-программист", ENGINEER_GROUP, 1, 100_000),
        PositionRef("инженер", ENGINEER_GROUP, 1, 80_000),
        PositionRef("лаборант", LAB_GROUP, 1, 60_000),
    )
    employees = (
        EmployeeSpec("E001", "Иванов Иван Иванович", "инженер-программист", "РНД", 100_000, 30_000, ENGINEER_GROUP),
        EmployeeSpec("E002", "Петров Петр Петрович", "инженер", "РНД", 80_000, 20_000, ENGINEER_GROUP),
        EmployeeSpec("E003", "Сидоров Сергей Сергеевич", "инженер", "РНД", 80_000, 40_000, ENGINEER_GROUP),
        EmployeeSpec("E004", "Орлова Ольга Олеговна", "лаборант", "РНД", 60_000, 15_000, LAB_GROUP),
    )
    base = ContractSpec(
        id=ANCHOR_CONTRACT,
        name="Базовый длинный договор",
        number="B-001",
        contract_type="grant",
        start=date(year, 1, 1),
        end=date(year, 12, 31),
        total_fot=0.0,
        inflows={1: 0.0},
        positions=(
            ContractPositionSpec("инженер-программист", 1, 100_000),
            ContractPositionSpec("инженер", 2, 80_000),
            ContractPositionSpec("лаборант", 1, 60_000),
        ),
    )
    project = ContractSpec(
        id=PROJECT_CONTRACT,
        name="Проект инженеров",
        number="P-001",
        contract_type="minprom",
        start=date(year, 4, 1),
        end=date(year, 9, 30),
        total_fot=0.0,
        inflows={4: 0.0},
        positions=(ContractPositionSpec("инженер", 3, 80_000),),
        labor=(LaborRowSpec("инженер", 9, 60_000),),
    )
    lab = ContractSpec(
        id=LAB_CONTRACT,
        name="Проект лаборантов",
        number="L-001",
        contract_type="minprom",
        start=date(year, 7, 1),
        end=date(year, 12, 31),
        total_fot=0.0,
        inflows={7: 0.0},
        positions=(ContractPositionSpec("лаборант", 1, 60_000),),
        labor=(LaborRowSpec("лаборант", 3, 30_000),),
    )

    scenario = DemoScenario(
        year=year,
        settings=SettingsSpec(allow_deficit=allow_deficit),
        positions=positions,
        employees=employees,
        contracts=(base, project, lab),
    )

    base_fot = scenario.compute_anchor_total_fot()
    base_labor = scenario.compute_base_labor()
    project_fot_val = project_fot if project_fot is not None else scenario.compute_project_fot()
    lab_fot = scenario.compute_lab_fot()

    contracts = (
        replace(base, total_fot=base_fot, inflows={1: base_fot}, labor=base_labor),
        replace(project, total_fot=project_fot_val, inflows={4: project_fot_val}),
        replace(lab, total_fot=lab_fot, inflows={7: lab_fot}),
    )
    return replace(scenario, contracts=contracts)


def build_backward_money_scenario() -> DemoScenario:
    """Проект: деньги только в сентябре; ФОТ = один месяц инженерных надбавок."""
    scenario = build_demo_scenario()
    monthly_allowance = scenario.monthly_engineer_allowance_total()
    contracts = []
    for c in scenario.contracts:
        if c.id == PROJECT_CONTRACT:
            contracts.append(
                replace(
                    c,
                    total_fot=monthly_allowance,
                    inflows={9: monthly_allowance},
                    labor=(LaborRowSpec("инженер", 1, monthly_allowance),),
                )
            )
        else:
            contracts.append(c)
    scenario = replace(
        scenario,
        contracts=tuple(contracts),
        settings=replace(scenario.settings, allow_deficit=True),
    )
    base_labor = scenario.compute_base_labor()
    contracts = [
        replace(c, labor=base_labor) if c.id == ANCHOR_CONTRACT else c
        for c in scenario.contracts
    ]
    return replace(scenario, contracts=tuple(contracts))
