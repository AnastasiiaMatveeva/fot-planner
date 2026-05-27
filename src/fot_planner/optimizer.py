"""Оптимизационный расчёт распределения ФОТ (MIP, Pyomo + HiGHS)."""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import date

import pyomo.environ as pyo
from pyomo.opt import SolverStatus, TerminationCondition

from fot_planner.fot_schedule import active_months_in_year, month_inflow_amount
from fot_planner.labor_rules import (
    contract_has_labor_plan,
    employee_compatible_with_contract_labor,
    employee_compatible_with_labor_row,
    labor_payment_cap_per_pm,
    labor_payment_terms_for_row,
    labor_average_balance_gap,
    labor_pm_terms_for_row,
    planned_labor_amount,
)
from fot_planner.payment_split import (
    employee_salary_due,
    required_salary_on_contract,
    salary_cap_on_contract,
)
from fot_planner.spend_plan import spent_or_zero
from fot_planner.reserve_rules import min_balance_for_month
from fot_planner.spend_rules import must_fully_spend_fot, required_full_spend_month
from fot_planner.models import (
    ADMIN_COMPLEXITY_FRAGMENT_FACTOR,
    ADMIN_COMPLEXITY_SCHEME_CHANGE_FACTOR,
    ADMIN_COMPLEXITY_YEAR_LINK_FACTOR,
    AllocationRecord,
    ConflictRecord,
    ContractBalanceRecord,
    DeficitRecord,
    LaborPaymentAttribution,
    LaborPmAttribution,
    ManualAssignment,
    ManualProhibition,
    MonthTransfer,
    PaymentKind,
    PlanningContext,
    PlanningResult,
    labor_row_id,
)
from fot_planner.contract_calendar import contract_allows_month
from fot_planner.validation import employee_active_in_month

PAYMENT_KINDS: tuple[PaymentKind, ...] = ("salary", "allowance", "incentive")
FLEX_PAYMENT_KINDS: tuple[PaymentKind, ...] = ("allowance", "incentive")
BIG_M = 1e7
# При use=1 переменная alloc должна быть положительной (исключение нулевых начислений при активном use).
MIN_USE_ALLOC = 1.0
# Минимальная сумма на договоре при use=1 для allowance/incentive (запрет хвостов 1–100 ₽).
MIN_FLEX_FRAGMENT_AMOUNT = 1_000.0
UNIFORM_SPEND_TOLERANCE_AMOUNT = 1_000.0
UNIFORM_SPEND_TOLERANCE_RATIO = 0.01
# Минимальная длительность блока оклада на одном договоре (между сменами — не меньше N месяцев).
MIN_SALARY_BLOCK_MONTHS = 3
DEFICIT_TOTAL_TOLERANCE = 1.0
LEX_STAGE_TOLERANCE = 1.0


def _month_inflow(ctx: PlanningContext, contract_id: str, month: int) -> float:
    """Поступление средств в месяце (физическая касса из fot_matrix)."""
    c = next(x for x in ctx.contracts if x.id == contract_id)
    if c.monthly_budgets:
        return month_inflow_amount(c, ctx.year, month)
    months = active_months_in_year(c, ctx.year)
    if months and month == months[0]:
        return c.total_fot
    return 0.0


def _month_budget(ctx: PlanningContext, contract_id: str, month: int) -> float:
    """Для обратной совместимости: поступление без учёта переноса."""
    return _month_inflow(ctx, contract_id, month)


def _allows_carryover(contract) -> bool:
    return contract.allow_monthly_carryover


def _sum_terms(terms):
    terms = list(terms)
    if not terms:
        return 0
    return sum(terms) if len(terms) > 1 else terms[0]


def _value(obj) -> float:
    val = pyo.value(obj, exception=False)
    return 0.0 if val is None else float(val)


def _contract_spent_expr(alloc, c_id: str, month: int):
    terms = [alloc[k] for k in alloc if k[1] == c_id and k[2] == month]
    if not terms:
        return None
    return _sum_terms(terms)


def _compatible_position_rules(contract, employee):
    """Договорные позиции, которые сотрудник может закрыть по группе взаимозаменяемости."""
    if not contract.position_rules:
        return []

    if employee.equivalence_group:
        rules = [
            pr
            for pr in contract.position_rules
            if pr.equivalence_group == employee.equivalence_group
        ]
        if rules:
            return rules

    return [pr for pr in contract.position_rules if pr.position == employee.position]


def _employee_can_cover_contract_positions(contract, employee) -> bool:
    """Если у договора есть contract_positions, выплаты разрешены только совместимым сотрудникам."""
    if not contract.position_rules:
        return True
    return bool(_compatible_position_rules(contract, employee))


def _contract_payment_cap(contract, employee) -> float | None:
    """Потолок месячного оклада (salary) с договора; None — потолок не задан."""
    return salary_cap_on_contract(contract, employee)


def _use_or_zero(
    uses,
    alloc_key_set: set,
    e_id: str,
    c_id: str,
    month: int,
    kind: PaymentKind = "salary",
):
    key = (e_id, c_id, month, kind)
    return uses[key] if key in alloc_key_set else 0


def _contract_allows_payment(contract, kind: PaymentKind) -> bool:
    if kind == "salary":
        return contract.allow_salary
    if kind == "allowance":
        return contract.allow_allowance
    return contract.allow_incentive


def _monthly_total_due(emp) -> float:
    return emp.monthly_wage + emp.incentive


def _payment_ub(employee, contract, kind: PaymentKind) -> float | None:
    """Верхняя граница alloc по виду выплаты; None — переменную не создавать."""
    if kind == "salary":
        if employee.monthly_wage <= 0:
            return None
        ub = employee.monthly_wage
        cap = _contract_payment_cap(contract, employee)
        if cap is not None:
            ub = min(ub, cap)
        return ub
    if kind == "allowance":
        if employee.monthly_wage <= 0:
            return None
        return employee.monthly_wage
    if employee.incentive <= 0:
        return None
    return employee.incentive


def _min_flex_fragment_when_used(ub: float) -> float:
    """Нижняя граница alloc при use=1 для надбавки/стимулирующей."""
    if ub <= 0:
        return MIN_USE_ALLOC
    return max(MIN_USE_ALLOC, min(MIN_FLEX_FRAGMENT_AMOUNT, ub))


def _is_forbidden(
    prohibitions: list[ManualProhibition],
    e_id: str,
    c_id: str,
    month: int,
    kind: PaymentKind,
) -> bool:
    for p in prohibitions:
        if p.employee_id != e_id or p.contract_id != c_id:
            continue
        if p.month_from <= month <= p.month_to:
            if p.payment_kind is None or p.payment_kind == kind:
                return True
    return False


def _manual_fixed_amount(
    assignments: list[ManualAssignment],
    e_id: str,
    c_id: str,
    month: int,
    kind: PaymentKind,
) -> float | None:
    total = 0.0
    found = False
    for a in assignments:
        if a.employee_id != e_id or a.contract_id != c_id or a.payment_kind != kind:
            continue
        if a.month_from <= month <= a.month_to:
            found = True
            if a.fixed_amount is not None:
                total += a.fixed_amount
            else:
                return None
    if found and total > 0:
        return total
    if found:
        return None
    return 0.0


def _must_use_pair(
    assignments: list[ManualAssignment],
    e_id: str,
    c_id: str,
    month: int,
    kind: PaymentKind,
) -> bool:
    for a in assignments:
        if (
            a.employee_id == e_id
            and a.contract_id == c_id
            and a.payment_kind == kind
            and a.month_from <= month <= a.month_to
            and a.fixed_amount is None
        ):
            return True
    return False


def _create_solver(time_limit_sec: int):
    solver = pyo.SolverFactory("appsi_highs")
    if not solver.available(False):
        solver = pyo.SolverFactory("highs")
    if not solver.available(False):
        raise RuntimeError("Не удалось создать решатель Pyomo HiGHS (установите highspy)")

    if hasattr(solver, "config") and hasattr(solver.config, "time_limit"):
        solver.config.time_limit = time_limit_sec
    else:
        solver.options["time_limit"] = time_limit_sec
    return solver


def _status_name(results) -> str:
    status = results.solver.status
    term = results.solver.termination_condition
    if term == TerminationCondition.optimal:
        return "OPTIMAL"
    if term == TerminationCondition.infeasible:
        return "INFEASIBLE"
    if term in (
        TerminationCondition.maxTimeLimit,
        TerminationCondition.feasible,
        TerminationCondition.other,
    ) and status in (SolverStatus.ok, SolverStatus.warning):
        return "FEASIBLE"
    return "NOT_SOLVED"


def _employee_cumulative_inflow(ctx: PlanningContext, employee, through_month: int) -> float:
    """Сумма поступлений по доступным договорам сотрудника с января по through_month."""
    year = ctx.year
    total = 0.0
    for c in ctx.contracts:
        if employee.allowed_contracts and c.id not in employee.allowed_contracts:
            continue
        if c.id in employee.forbidden_contracts:
            continue
        for m in range(1, through_month + 1):
            if contract_allows_month(c, year, m):
                total += _month_inflow(ctx, c.id, m)
    return total


def _employee_cumulative_due(employee, year: int, through_month: int) -> float:
    total = 0.0
    for m in range(1, through_month + 1):
        if employee_active_in_month(employee, year, m):
            total += _monthly_total_due(employee)
    return total


def _preferred_salary_anchor_contract_id(ctx: PlanningContext) -> str | None:
    """Предпочтительный договор для окладов: grant с максимальным сроком, иначе самый длинный."""
    candidates = [c for c in ctx.contracts if c.allow_salary is not False]
    if not candidates:
        return None
    grants = [c for c in candidates if c.contract_type == "grant"]
    pool = grants or candidates

    def span_days(contract) -> int:
        if contract.start_date and contract.end_date:
            return (contract.end_date - contract.start_date).days
        return 0

    anchor = max(pool, key=lambda c: (span_days(c), c.total_fot))
    return anchor.id


def _early_deficit_month_weight(month: int) -> int:
    """Январь = 12, декабрь = 1 — ранний дефицит штрафуется сильнее."""
    return 13 - month


def _emp_contract_month_used_or_zero(model, month_used_set: set, e_id: str, c_id: str, m: int):
    key = (e_id, c_id, m)
    if key in month_used_set:
        return model.emp_contract_month_used[key]
    return 0


def _expr_salary_compensation(model, salary_compensation_keys):
    if not salary_compensation_keys:
        return None
    return _sum_terms(model.salary_compensation[k] for k in salary_compensation_keys)


def _expr_admin_complexity(
    model,
    *,
    employee_contract_keys,
    scheme_change_keys,
    alloc_keys,
    uses,
    admin_weight: float,
):
    if admin_weight <= 0:
        return None
    terms = []
    if employee_contract_keys:
        terms.extend(
            admin_weight
            * ADMIN_COMPLEXITY_YEAR_LINK_FACTOR
            * model.emp_contract_used[k]
            for k in employee_contract_keys
        )
    if scheme_change_keys:
        terms.extend(
            admin_weight
            * ADMIN_COMPLEXITY_SCHEME_CHANGE_FACTOR
            * model.emp_contract_scheme_changed[k]
            for k in scheme_change_keys
        )
    flex_keys = [k for k in alloc_keys if k[3] in FLEX_PAYMENT_KINDS]
    if flex_keys:
        terms.extend(
            admin_weight * ADMIN_COMPLEXITY_FRAGMENT_FACTOR * uses[k] for k in flex_keys
        )
    return _sum_terms(terms) if terms else None


def _expr_salary_switch(model, salary_change_keys, weight: float):
    if not salary_change_keys or weight <= 0:
        return None
    return _sum_terms(weight * model.salary_change[k] for k in salary_change_keys)


def _expr_non_anchor_salary_penalty(
    uses, alloc_keys, preferred_anchor_id: str | None, weight: float
):
    if not preferred_anchor_id or weight <= 0:
        return None
    terms = [
        weight * uses[k]
        for k in alloc_keys
        if k[3] == "salary" and k[1] != preferred_anchor_id
    ]
    return _sum_terms(terms) if terms else None


def _expr_labor_deviations(
    model,
    *,
    labor_dev_keys,
    labor_amount_dev_keys,
    labor_balance_dev_keys,
    labor_plan_amount: dict[int, float],
    labor_soft_indices: set[int],
    weight: float,
):
    if weight <= 0:
        return None
    terms = []
    for k in labor_dev_keys:
        if k in labor_soft_indices:
            terms.append(weight * model.labor_dev[k])
    for k in labor_amount_dev_keys:
        if k in labor_soft_indices:
            scale = max(labor_plan_amount.get(k, 0.0), 1.0)
            terms.append(weight * model.labor_amount_dev[k] / scale)
    for k in labor_balance_dev_keys:
        if k in labor_soft_indices:
            scale = max(labor_plan_amount.get(k, 0.0), 1.0)
            terms.append(weight * model.labor_balance_dev[k] / scale)
    return _sum_terms(terms) if terms else None


def _expr_uniform_deviation(
    model,
    uniform_dev_keys,
    uniform_penalty_params,
    weight: float,
):
    if not uniform_dev_keys or weight <= 0:
        return None
    return _sum_terms(
        (weight / uniform_penalty_params[k][2])
        * (model.uniform_penalty_pos[k] + model.uniform_penalty_neg[k])
        for k in uniform_dev_keys
    )


def _run_minimize_stage(
    model,
    expr,
    time_limit_sec: int,
    *,
    tolerance: float = LEX_STAGE_TOLERANCE,
    label: str = "",
) -> tuple[str, float, float | None]:
    """Минимизировать expr, зафиксировать результат ограничением expr <= best + tolerance."""
    if expr is None:
        return "OPTIMAL", 0.0, None
    if hasattr(model, "lex_stage_obj"):
        model.del_component("lex_stage_obj")
    model.lex_stage_obj = pyo.Objective(expr=expr, sense=pyo.minimize)
    _, status_name, elapsed = _run_solver(model, time_limit_sec)
    if status_name not in ("OPTIMAL", "FEASIBLE"):
        model.lex_stage_obj.deactivate()
        return status_name, elapsed, None
    best = _value(expr)
    model.lex_stage_obj.deactivate()
    model.cons.add(expr <= best + tolerance)
    return status_name, elapsed, best


def _collect_soft_objective_terms(
    model,
    *,
    w,
    alloc_keys,
    salary_change_keys,
    employee_contract_keys,
    scheme_change_keys,
    uniform_dev_keys,
    uniform_penalty_params,
    baseline_dev_keys,
    labor_dev_keys,
    labor_amount_dev_keys,
    labor_balance_dev_keys,
    labor_plan_amount: dict[int, float],
    labor_soft_indices: set[int],
    salary_compensation_keys,
    uses,
    include_uniform: bool = True,
):
    terms = []
    comp = _expr_salary_compensation(model, salary_compensation_keys)
    if comp is not None and w.salary_compensation_via_flex:
        terms.append(w.salary_compensation_via_flex * comp)
    admin_part = _expr_admin_complexity(
        model,
        employee_contract_keys=employee_contract_keys,
        scheme_change_keys=scheme_change_keys,
        alloc_keys=alloc_keys,
        uses=uses,
        admin_weight=1.0,
    )
    if admin_part is not None and w.admin_complexity:
        terms.append(w.admin_complexity * admin_part)
    switch_part = _expr_salary_switch(model, salary_change_keys, w.salary_contract_switch)
    if switch_part is not None:
        terms.append(switch_part)
    if include_uniform and uniform_dev_keys and w.uniform_spend_deviation:
        uniform_part = _expr_uniform_deviation(
            model, uniform_dev_keys, uniform_penalty_params, w.uniform_spend_deviation
        )
        if uniform_part is not None:
            terms.append(uniform_part)
    terms.extend(w.plan_deviation * model.baseline_dev[k] for k in baseline_dev_keys)
    labor_part = _expr_labor_deviations(
        model,
        labor_dev_keys=labor_dev_keys,
        labor_amount_dev_keys=labor_amount_dev_keys,
        labor_balance_dev_keys=labor_balance_dev_keys,
        labor_plan_amount=labor_plan_amount,
        labor_soft_indices=labor_soft_indices,
        weight=w.labor_deviation,
    )
    if labor_part is not None:
        terms.append(labor_part)
    return terms


def _run_solver(model, time_limit_sec: int):
    t0 = time.perf_counter()
    solver = _create_solver(time_limit_sec)
    if hasattr(solver, "config") and hasattr(solver.config, "load_solution"):
        solver.config.load_solution = False
    results = solver.solve(model, load_solutions=False)
    status_name = _status_name(results)
    if status_name in ("OPTIMAL", "FEASIBLE"):
        if hasattr(results, "solution_loader") and results.solution_loader is not None:
            results.solution_loader.load_vars()
        elif hasattr(solver, "config") and hasattr(solver.config, "load_solution"):
            solver.config.load_solution = True
            solver.load_vars()
    return results, status_name, time.perf_counter() - t0


def solve(ctx: PlanningContext, time_limit_sec: int = 120) -> PlanningResult:
    t0 = time.perf_counter()
    year = ctx.year
    months = list(range(1, 13))

    employees = {e.id: e for e in ctx.employees}
    contracts = {c.id: c for c in ctx.contracts}
    contract_list = list(contracts.keys())

    labor_row_indices_pre = [
        idx
        for idx, lp in enumerate(ctx.labor_plans)
        if lp.year == year and lp.person_months > 0 and lp.avg_monthly_labor_cost
    ]
    contracts_with_labor = {
        ctx.labor_plans[idx].contract_id for idx in labor_row_indices_pre
    }

    alloc_keys: list[tuple[str, str, int, PaymentKind]] = []
    alloc_ub: dict[tuple[str, str, int, PaymentKind], float] = {}

    for e in ctx.employees:
        for c_id in contract_list:
            c = contracts[c_id]
            for m in months:
                if not employee_active_in_month(e, year, m):
                    continue
                if not contract_allows_month(c, year, m):
                    continue
                if e.allowed_contracts and c_id not in e.allowed_contracts:
                    continue
                if c_id in e.forbidden_contracts:
                    continue
                for kind in PAYMENT_KINDS:
                    if not _contract_allows_payment(c, kind):
                        continue
                    if _is_forbidden(ctx.manual_prohibitions, e.id, c_id, m, kind):
                        continue
                    if not _employee_can_cover_contract_positions(c, e):
                        continue
                    if c_id in contracts_with_labor and not employee_compatible_with_contract_labor(
                        ctx, e, c_id
                    ):
                        continue
                    ub = _payment_ub(e, c, kind)
                    if ub is None or ub <= 0:
                        continue

                    key = (e.id, c_id, m, kind)
                    alloc_keys.append(key)
                    alloc_ub[key] = ub

    allow_carry_by_contract: dict[str, bool] = {}
    close_keys: list[tuple[str, int]] = []
    carry_keys: list[tuple[str, int]] = []
    for c_id in contract_list:
        c = contracts[c_id]
        allow_carry = _allows_carryover(c)
        allow_carry_by_contract[c_id] = allow_carry
        if allow_carry:
            close_keys.extend((c_id, m) for m in months)
            carry_keys.extend((c_id, m) for m in months if m < 12)

    year_contract_keys = []
    for e in ctx.employees:
        for c_id in contract_list:
            if any(k[0] == e.id and k[1] == c_id and k[3] == "salary" for k in alloc_keys):
                year_contract_keys.append((e.id, c_id))

    employee_contract_keys: list[tuple[str, str]] = []
    for e in ctx.employees:
        for c_id in contract_list:
            if any(k[0] == e.id and k[1] == c_id for k in alloc_keys):
                employee_contract_keys.append((e.id, c_id))

    employee_contract_month_keys: list[tuple[str, str, int]] = []
    for e in ctx.employees:
        for c_id in contract_list:
            for m in months:
                if any(k[0] == e.id and k[1] == c_id and k[2] == m for k in alloc_keys):
                    employee_contract_month_keys.append((e.id, c_id, m))

    alloc_key_set = set(alloc_keys)

    required_salary: dict[tuple[str, str], float] = {}
    for e_id, c_id, _m, kind in alloc_keys:
        if kind != "salary":
            continue
        pair = (e_id, c_id)
        if pair in required_salary:
            continue
        required_salary[pair] = required_salary_on_contract(contracts[c_id], employees[e_id])

    stab = ctx.salary_stability
    month_used_set = set(employee_contract_month_keys)

    salary_change_keys: list[tuple[str, int]] = []
    for e in ctx.employees:
        for m in range(2, 13):
            if not employee_active_in_month(e, year, m) and not employee_active_in_month(
                e, year, m - 1
            ):
                continue
            if any(
                (e.id, c_id, m, "salary") in alloc_key_set
                or (e.id, c_id, m - 1, "salary") in alloc_key_set
                for c_id in contract_list
            ):
                salary_change_keys.append((e.id, m))

    scheme_change_keys: list[tuple[str, int]] = []
    for e in ctx.employees:
        for m in range(2, 13):
            if not employee_active_in_month(e, year, m) and not employee_active_in_month(
                e, year, m - 1
            ):
                continue
            if any(
                (e.id, c_id, mo) in month_used_set
                for c_id in contract_list
                for mo in (m, m - 1)
            ):
                scheme_change_keys.append((e.id, m))

    uniform_dev_keys: list[tuple[str, int]] = []
    uniform_penalty_params: dict[tuple[str, int], tuple[float, float, float]] = {}
    for c in ctx.contracts:
        active = active_months_in_year(c, year)
        if not active or c.total_fot <= 0:
            continue
        ideal = c.total_fot / len(active)
        tolerance = max(
            UNIFORM_SPEND_TOLERANCE_AMOUNT,
            UNIFORM_SPEND_TOLERANCE_RATIO * ideal,
        )
        scale = max(ideal, 1.0)
        for m in active:
            key = (c.id, m)
            uniform_dev_keys.append(key)
            uniform_penalty_params[key] = (ideal, tolerance, scale)

    labor_row_indices = [
        idx
        for idx, lp in enumerate(ctx.labor_plans)
        if lp.year == ctx.year and lp.person_months > 0 and lp.avg_monthly_labor_cost
    ]
    labor_plan_amount: dict[int, float] = {
        idx: planned_labor_amount(ctx.labor_plans[idx]) for idx in labor_row_indices
    }
    labor_soft_indices = set(labor_row_indices)

    labor_pm_keys: list[tuple[str, str, int, int]] = []
    labor_payment_keys: list[tuple[str, str, int, PaymentKind, int]] = []
    for e in ctx.employees:
        for lp_idx in labor_row_indices:
            lp = ctx.labor_plans[lp_idx]
            if not employee_compatible_with_labor_row(e, lp):
                continue
            c_id = lp.contract_id
            c = contracts.get(c_id)
            if c is None:
                continue
            for m in months:
                if not employee_active_in_month(e, year, m):
                    continue
                if not contract_allows_month(c, year, m):
                    continue
                labor_pm_keys.append((e.id, c_id, m, lp_idx))
                for kind in PAYMENT_KINDS:
                    if (e.id, c_id, m, kind) in alloc_key_set:
                        labor_payment_keys.append((e.id, c_id, m, kind, lp_idx))

    labor_pm_key_set = set(labor_pm_keys)
    labor_payment_key_set = set(labor_payment_keys)

    baseline_dev_keys = alloc_keys if ctx.baseline_plan else []
    labor_dev_keys = list(labor_row_indices)
    labor_amount_dev_keys = list(labor_dev_keys)
    labor_balance_dev_keys = list(labor_dev_keys)

    allow_deficit = ctx.allow_deficit
    deficit_agg_keys: list[tuple[str, int]] = []
    deficit_due_ub: dict[tuple[str, int], float] = {}
    salary_deficit_keys: list[tuple[str, int]] = []
    salary_compensation_keys: list[tuple[str, int]] = []
    for e in ctx.employees:
        if employee_salary_due(e) <= 0:
            continue
        for m in months:
            if not employee_active_in_month(e, year, m):
                continue
            if not any(k[0] == e.id and k[2] == m and k[3] == "salary" for k in alloc_keys):
                continue
            salary_compensation_keys.append((e.id, m))
            if allow_deficit:
                salary_deficit_keys.append((e.id, m))
    if allow_deficit:
        for e in ctx.employees:
            for m in months:
                if not employee_active_in_month(e, year, m):
                    continue
                total_due = _monthly_total_due(e)
                if total_due <= 0:
                    continue
                has_payment_vars = any(k[0] == e.id and k[2] == m for k in alloc_keys)
                if not has_payment_vars:
                    continue
                agg_key = (e.id, m)
                deficit_agg_keys.append(agg_key)
                deficit_due_ub[agg_key] = total_due
    deficit_agg_key_set = set(deficit_agg_keys)
    salary_deficit_key_set = set(salary_deficit_keys)

    model = pyo.ConcreteModel()
    model.alloc = pyo.Var(
        alloc_keys,
        domain=pyo.NonNegativeReals,
        bounds=lambda _, e, c, m, k: (0, alloc_ub[(e, c, m, k)]),
    )
    model.use = pyo.Var(alloc_keys, domain=pyo.Binary)
    model.close = pyo.Var(close_keys, domain=pyo.NonNegativeReals)
    model.carry = pyo.Var(carry_keys, domain=pyo.NonNegativeReals)
    model.year_use = pyo.Var(year_contract_keys, domain=pyo.Binary)
    if employee_contract_keys:
        model.emp_contract_used = pyo.Var(employee_contract_keys, domain=pyo.Binary)
    if employee_contract_month_keys:
        model.emp_contract_month_used = pyo.Var(
            employee_contract_month_keys, domain=pyo.Binary
        )
    if salary_change_keys:
        model.salary_change = pyo.Var(salary_change_keys, domain=pyo.Binary)
    if scheme_change_keys:
        model.emp_contract_scheme_changed = pyo.Var(
            scheme_change_keys, domain=pyo.Binary
        )
    if uniform_dev_keys:
        model.uniform_penalty_pos = pyo.Var(
            uniform_dev_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M)
        )
        model.uniform_penalty_neg = pyo.Var(
            uniform_dev_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M)
        )
    if labor_pm_keys:
        model.labor_pm = pyo.Var(
            labor_pm_keys,
            domain=pyo.NonNegativeReals,
            bounds=lambda _m, e_id, _c, _mo, _lp: (0, employees[e_id].rate),
        )
    if labor_payment_keys:
        model.labor_pay = pyo.Var(
            labor_payment_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M)
        )
    model.baseline_dev = pyo.Var(baseline_dev_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M))
    model.labor_dev = pyo.Var(labor_dev_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M))
    if labor_amount_dev_keys:
        model.labor_amount_dev = pyo.Var(
            labor_amount_dev_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M)
        )
    if labor_balance_dev_keys:
        model.labor_balance_dev = pyo.Var(
            labor_balance_dev_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M)
        )
    model.cons = pyo.ConstraintList()

    if deficit_agg_keys:
        model.deficit = pyo.Var(
            deficit_agg_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M)
        )
        model.deficit_used = pyo.Var(deficit_agg_keys, domain=pyo.Binary)
        for d_key in deficit_agg_keys:
            model.cons.add(
                model.deficit[d_key] <= deficit_due_ub[d_key] * model.deficit_used[d_key]
            )
    if salary_deficit_keys:
        model.salary_deficit = pyo.Var(
            salary_deficit_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M)
        )
    if salary_compensation_keys:
        model.salary_compensation = pyo.Var(
            salary_compensation_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M)
        )

    alloc = model.alloc
    uses = model.use
    deficit = getattr(model, "deficit", {})
    salary_deficit = getattr(model, "salary_deficit", {})
    salary_compensation = getattr(model, "salary_compensation", {})
    labor_pm = getattr(model, "labor_pm", {})
    labor_pay = getattr(model, "labor_pay", {})

    for key in alloc_keys:
        e_id, _c_id, _m, kind = key
        ub = alloc_ub[key]
        model.cons.add(alloc[key] <= ub * uses[key])
        e = employees[e_id]
        c = contracts[_c_id]
        if kind == "salary" and employee_salary_due(e) > 0:
            req = required_salary.get((e_id, _c_id), 0.0)
            if allow_deficit:
                model.cons.add(alloc[key] <= req * uses[key])
            else:
                model.cons.add(alloc[key] == req * uses[key])
        elif kind in FLEX_PAYMENT_KINDS:
            floor = _min_flex_fragment_when_used(ub)
            model.cons.add(alloc[key] >= floor * uses[key])
        else:
            model.cons.add(alloc[key] >= MIN_USE_ALLOC * uses[key])

    for key in alloc_keys:
        e_id, c_id, m, kind = key
        fixed = _manual_fixed_amount(ctx.manual_assignments, e_id, c_id, m, kind)
        if fixed is not None and fixed > 0:
            model.cons.add(alloc[key] == fixed)
        elif _must_use_pair(ctx.manual_assignments, e_id, c_id, m, kind):
            model.cons.add(uses[key] == 1)

    if labor_pm_keys:
        for e in ctx.employees:
            for m in months:
                if not employee_active_in_month(e, year, m):
                    continue
                pm_terms = [
                    labor_pm[(e.id, c_id, m, lp_idx)]
                    for (e_id, c_id, mo, lp_idx) in labor_pm_key_set
                    if e_id == e.id and mo == m
                ]
                if pm_terms:
                    model.cons.add(_sum_terms(pm_terms) <= e.rate)

        payment_pm_linked: set[tuple[str, str, int]] = set()
        for (e_id, c_id, m, _lp_idx) in labor_pm_keys:
            link_key = (e_id, c_id, m)
            if link_key in payment_pm_linked:
                continue
            pm_on_contract = [
                labor_pm[(e_id, c_id, m, row_idx)]
                for row_idx in labor_row_indices
                if (e_id, c_id, m, row_idx) in labor_pm_key_set
            ]
            if not pm_on_contract:
                continue
            pay_uses = [
                uses[(e_id, c_id, m, kind)]
                for kind in PAYMENT_KINDS
                if (e_id, c_id, m, kind) in uses
            ]
            if not pay_uses:
                continue
            payment_pm_linked.add(link_key)
            rate = employees[e_id].rate
            pay_sum = _sum_terms(pay_uses)
            model.cons.add(_sum_terms(pm_on_contract) <= rate * pay_sum)

    if labor_payment_keys:
        for key in alloc_keys:
            e_id, c_id, m, kind = key
            pay_terms = [
                labor_pay[(e_id, c_id, m, kind, lp_idx)]
                for lp_idx in labor_row_indices
                if (e_id, c_id, m, kind, lp_idx) in labor_payment_key_set
            ]
            if not pay_terms:
                continue
            if c_id in contracts_with_labor:
                model.cons.add(_sum_terms(pay_terms) == alloc[key])
            else:
                model.cons.add(_sum_terms(pay_terms) <= alloc[key])

        pm_payment_multiplier = ctx.salary_stability.labor_pm_payment_multiplier
        pm_pay_row_keys = {
            (e_id, c_id, m, lp_idx)
            for (e_id, c_id, m, lp_idx) in labor_pm_key_set
        } | {
            (e_id, c_id, m, lp_idx)
            for (e_id, c_id, m, _kind, lp_idx) in labor_payment_key_set
        }
        for e_id, c_id, m, lp_idx in sorted(pm_pay_row_keys):
            lp = ctx.labor_plans[lp_idx]
            cap_per_pm = labor_payment_cap_per_pm(lp, pm_payment_multiplier)
            if cap_per_pm <= 0:
                continue
            pay_terms = [
                labor_pay[(e_id, c_id, m, kind, lp_idx)]
                for kind in PAYMENT_KINDS
                if (e_id, c_id, m, kind, lp_idx) in labor_payment_key_set
            ]
            if not pay_terms:
                continue
            pm_key = (e_id, c_id, m, lp_idx)
            if pm_key not in labor_pm_key_set:
                model.cons.add(_sum_terms(pay_terms) == 0)
                continue
            model.cons.add(_sum_terms(pay_terms) <= cap_per_pm * labor_pm[pm_key])

    for e in ctx.employees:
        total_due = _monthly_total_due(e)
        if total_due <= 0:
            continue
        for m in months:
            if not employee_active_in_month(e, year, m):
                continue
            salary_terms = [
                alloc[k] for k in alloc_keys if k[0] == e.id and k[2] == m and k[3] == "salary"
            ]
            allowance_terms = [
                alloc[k]
                for k in alloc_keys
                if k[0] == e.id and k[2] == m and k[3] == "allowance"
            ]
            incentive_terms = [
                alloc[k]
                for k in alloc_keys
                if k[0] == e.id and k[2] == m and k[3] == "incentive"
            ]
            all_allocs = salary_terms + allowance_terms + incentive_terms
            if not all_allocs:
                continue

            salary_uses = [
                uses[k]
                for k in alloc_keys
                if k[0] == e.id and k[2] == m and k[3] == "salary"
            ]
            if employee_salary_due(e) > 0 and salary_uses:
                model.cons.add(_sum_terms(salary_uses) == 1)

            if allow_deficit:
                agg_key = (e.id, m)
                if agg_key in deficit_agg_key_set:
                    model.cons.add(_sum_terms(all_allocs) + deficit[agg_key] == total_due)
                if agg_key in salary_deficit_key_set and salary_terms:
                    salary_obligation = _sum_terms(
                        required_salary.get((e.id, c_id), 0.0)
                        * uses[(e.id, c_id, m, "salary")]
                        for c_id in contract_list
                        if (e.id, c_id, m, "salary") in alloc_key_set
                    )
                    model.cons.add(
                        _sum_terms(salary_terms) + salary_deficit[agg_key] == salary_obligation
                    )
            else:
                model.cons.add(_sum_terms(all_allocs) == total_due)

            comp_key = (e.id, m)
            if comp_key in salary_compensation_keys and salary_terms:
                model.cons.add(
                    salary_compensation[comp_key]
                    >= employee_salary_due(e) - _sum_terms(salary_terms)
                )

    for c_id in contract_list:
        c = contracts[c_id]
        allow_carry = allow_carry_by_contract.get(c_id, True)
        year_spent_terms = []

        for m in months:
            spent_expr = spent_or_zero(alloc, c_id, m)
            year_spent_terms.append(spent_expr)

            inflow = _month_inflow(ctx, c_id, m)
            min_bal = min_balance_for_month(c, m)

            if not allow_carry:
                model.cons.add(spent_expr <= inflow)
                if min_bal > 0:
                    model.cons.add(inflow - spent_expr >= min_bal)
                continue

            opening = 0 if m == 1 else model.carry[(c_id, m - 1)]
            model.cons.add(model.close[(c_id, m)] == opening + inflow - spent_expr)
            if min_bal > 0:
                model.cons.add(model.close[(c_id, m)] >= min_bal)
            if m < 12:
                model.cons.add(model.carry[(c_id, m)] == model.close[(c_id, m)])

        if year_spent_terms and c.total_fot > 0:
            model.cons.add(_sum_terms(year_spent_terms) <= c.total_fot)

        if must_fully_spend_fot(c) and c.total_fot > 0:
            last_m = required_full_spend_month(c, year)
            if last_m is not None:
                spend_by_deadline = [
                    _contract_spent_expr(alloc, c_id, m)
                    for m in range(1, last_m + 1)
                    if contract_allows_month(c, year, m)
                ]
                spend_by_deadline = [t for t in spend_by_deadline if t is not None]
                if spend_by_deadline:
                    model.cons.add(
                        _sum_terms(spend_by_deadline) >= c.total_fot
                    )

    for c_id in contract_list:
        c = contracts[c_id]
        if not c.position_rules:
            continue

        max_rate_by_group: dict[str, float] = defaultdict(float)
        for pr in c.position_rules:
            if pr.max_positions is None or pr.equivalence_group is None:
                continue
            max_rate_by_group[pr.equivalence_group] += pr.max_positions

        for group, max_rate in max_rate_by_group.items():
            for m in months:
                group_uses = [
                    employees[e_id].rate * uvar
                    for (e_id, cid, mo, kind), uvar in uses.items()
                    if cid == c_id
                    and mo == m
                    and kind == "salary"
                    and employees[e_id].equivalence_group == group
                ]
                if group_uses:
                    model.cons.add(_sum_terms(group_uses) <= max_rate)

    if employee_contract_keys:
        for e_id, c_id in employee_contract_keys:
            related_uses = [uses[k] for k in alloc_keys if k[0] == e_id and k[1] == c_id]
            for uvar in related_uses:
                model.cons.add(model.emp_contract_used[(e_id, c_id)] >= uvar)
            model.cons.add(model.emp_contract_used[(e_id, c_id)] <= _sum_terms(related_uses))

    if employee_contract_month_keys:
        for e_id, c_id, m in employee_contract_month_keys:
            related_uses = [
                uses[k]
                for k in alloc_keys
                if k[0] == e_id and k[1] == c_id and k[2] == m
            ]
            for uvar in related_uses:
                model.cons.add(model.emp_contract_month_used[(e_id, c_id, m)] >= uvar)
            model.cons.add(
                model.emp_contract_month_used[(e_id, c_id, m)] <= _sum_terms(related_uses)
            )

    if salary_change_keys:
        salary_change_key_set = set(salary_change_keys)
        for e_id, m in salary_change_keys:
            m_prev = m - 1
            for c_id in contract_list:
                u_prev = _use_or_zero(uses, alloc_key_set, e_id, c_id, m_prev, "salary")
                u_curr = _use_or_zero(uses, alloc_key_set, e_id, c_id, m, "salary")
                model.cons.add(model.salary_change[(e_id, m)] >= u_prev - u_curr)
                model.cons.add(model.salary_change[(e_id, m)] >= u_curr - u_prev)

        for e in ctx.employees:
            for start_m in range(2, 13 - MIN_SALARY_BLOCK_MONTHS + 1):
                window_months = range(start_m, start_m + MIN_SALARY_BLOCK_MONTHS)
                changes = [
                    model.salary_change[(e.id, m)]
                    for m in window_months
                    if (e.id, m) in salary_change_key_set
                ]
                if changes:
                    model.cons.add(_sum_terms(changes) <= 1)

    if scheme_change_keys:
        for e_id, m in scheme_change_keys:
            m_prev = m - 1
            for c_id in contract_list:
                u_curr = _emp_contract_month_used_or_zero(
                    model, month_used_set, e_id, c_id, m
                )
                u_prev = _emp_contract_month_used_or_zero(
                    model, month_used_set, e_id, c_id, m_prev
                )
                model.cons.add(
                    model.emp_contract_scheme_changed[(e_id, m)] >= u_curr - u_prev
                )
                model.cons.add(
                    model.emp_contract_scheme_changed[(e_id, m)] >= u_prev - u_curr
                )

    max_contracts_legacy = stab.max_contracts_per_year
    if max_contracts_legacy is not None and max_contracts_legacy > 0:
        for e_id, c_id in year_contract_keys:
            month_vars = [
                uses[(e_id, c_id, m, "salary")]
                for m in months
                if (e_id, c_id, m, "salary") in model.use
            ]
            for uvar in month_vars:
                model.cons.add(model.year_use[(e_id, c_id)] >= uvar)
            model.cons.add(model.year_use[(e_id, c_id)] <= _sum_terms(month_vars))

        for e in ctx.employees:
            flags = [model.year_use[(e.id, c_id)] for e_id, c_id in year_contract_keys if e_id == e.id]
            if flags:
                model.cons.add(_sum_terms(flags) <= max_contracts_legacy)

    if uniform_dev_keys:
        for key in uniform_dev_keys:
            ideal, tolerance, _scale = uniform_penalty_params[key]
            c_id, m = key
            actual = spent_or_zero(alloc, c_id, m)
            model.cons.add(
                model.uniform_penalty_pos[key] >= actual - ideal - tolerance
            )
            model.cons.add(
                model.uniform_penalty_neg[key] >= ideal - actual - tolerance
            )

    for lp_idx in labor_row_indices:
        lp = ctx.labor_plans[lp_idx]
        c = contracts[lp.contract_id]
        plan_pm = lp.person_months
        plan_amount = planned_labor_amount(lp)
        if plan_pm <= 0:
            continue

        pm_expr = labor_pm_terms_for_row(labor_pm, lp_idx, months) if labor_pm_keys else None
        amount_expr = (
            labor_payment_terms_for_row(labor_pay, lp_idx, months) if labor_payment_keys else None
        )
        if pm_expr is None and amount_expr is None:
            continue

        tol = stab.goz_labor_tolerance
        pm_low = (1.0 - tol) * plan_pm
        pm_high = (1.0 + tol) * plan_pm
        if pm_expr is not None and lp_idx in labor_dev_keys:
            model.cons.add(model.labor_dev[lp_idx] >= pm_expr - pm_high)
            model.cons.add(model.labor_dev[lp_idx] >= pm_low - pm_expr)
        if amount_expr is not None and lp_idx in labor_amount_dev_keys and plan_amount > 0:
            amt_low = (1.0 - tol) * plan_amount
            amt_high = (1.0 + tol) * plan_amount
            model.cons.add(model.labor_amount_dev[lp_idx] >= amount_expr - amt_high)
            model.cons.add(model.labor_amount_dev[lp_idx] >= amt_low - amount_expr)
        if (
            pm_expr is not None
            and amount_expr is not None
            and lp_idx in labor_balance_dev_keys
            and plan_pm > 0
            and plan_amount > 0
        ):
            balance = labor_average_balance_gap(amount_expr, pm_expr, plan_pm, plan_amount)
            balance_tol = tol * plan_pm * plan_amount
            model.cons.add(model.labor_balance_dev[lp_idx] >= balance - balance_tol)
            model.cons.add(model.labor_balance_dev[lp_idx] >= -balance - balance_tol)

    baseline_map: dict[tuple[str, str, int, PaymentKind], float] = defaultdict(float)
    if ctx.baseline_plan:
        for rec in ctx.baseline_plan:
            baseline_map[(rec.employee_id, rec.contract_id, rec.month, rec.payment_kind)] += rec.amount
        for key in alloc_keys:
            base = baseline_map.get(key, 0.0)
            model.cons.add(model.baseline_dev[key] >= alloc[key] - base)
            model.cons.add(model.baseline_dev[key] >= base - alloc[key])

    w = ctx.weights
    solve_time = 0.0
    status_name = "NOT_SOLVED"
    last_objective_value = 0.0
    per_stage_limit = max(20, time_limit_sec // 8)

    def _solver_failed(stage_label: str) -> PlanningResult:
        return PlanningResult(
            year=year,
            allocations=[],
            deficits=[],
            conflicts=[
                ConflictRecord(
                    code="SOLVER_FAILED",
                    message=f"Решатель завершился со статусом {status_name} ({stage_label})",
                )
            ],
            contract_balances=[],
            solver_status=status_name,
            objective_value=0.0,
            solve_time_sec=round(solve_time, 3),
            month_transfers=[],
        )

    if allow_deficit and deficit_agg_keys:
        deficit_total_expr = _sum_terms(deficit[k] for k in deficit_agg_keys)
        early_expr = _sum_terms(
            _early_deficit_month_weight(m) * deficit[(e_id, m)]
            for (e_id, m) in deficit_agg_keys
        )
        status_name, elapsed, stage_obj = _run_minimize_stage(
            model,
            deficit_total_expr,
            per_stage_limit,
            tolerance=DEFICIT_TOTAL_TOLERANCE,
        )
        solve_time += elapsed
        if stage_obj is not None:
            last_objective_value = stage_obj
        if status_name not in ("OPTIMAL", "FEASIBLE"):
            return _solver_failed("этап суммы дефицита")

        status_name, elapsed, stage_obj = _run_minimize_stage(
            model,
            early_expr,
            per_stage_limit,
            tolerance=DEFICIT_TOTAL_TOLERANCE,
        )
        solve_time += elapsed
        if stage_obj is not None:
            last_objective_value = stage_obj
        if status_name not in ("OPTIMAL", "FEASIBLE"):
            return _solver_failed("этап раннего дефицита")

    soft_stage_exprs: list[tuple[object, str]] = []
    preferred_anchor = _preferred_salary_anchor_contract_id(ctx)
    non_anchor_part = _expr_non_anchor_salary_penalty(
        uses, alloc_keys, preferred_anchor, w.salary_contract_switch
    )
    if non_anchor_part is not None:
        soft_stage_exprs.append(
            (non_anchor_part, "оклад не на предпочтительном договоре")
        )
    comp = _expr_salary_compensation(model, salary_compensation_keys)
    if comp is not None and w.salary_compensation_via_flex:
        soft_stage_exprs.append((w.salary_compensation_via_flex * comp, "компенсация оклада"))
    admin_part = _expr_admin_complexity(
        model,
        employee_contract_keys=employee_contract_keys,
        scheme_change_keys=scheme_change_keys,
        alloc_keys=alloc_keys,
        uses=uses,
        admin_weight=1.0,
    )
    if admin_part is not None and w.admin_complexity:
        soft_stage_exprs.append((w.admin_complexity * admin_part, "административная сложность"))
    switch_part = _expr_salary_switch(model, salary_change_keys, w.salary_contract_switch)
    if switch_part is not None:
        soft_stage_exprs.append((switch_part, "смена договора оклада"))
    labor_part = _expr_labor_deviations(
        model,
        labor_dev_keys=labor_dev_keys,
        labor_amount_dev_keys=labor_amount_dev_keys,
        labor_balance_dev_keys=labor_balance_dev_keys,
        labor_plan_amount=labor_plan_amount,
        labor_soft_indices=labor_soft_indices,
        weight=w.labor_deviation,
    )
    if labor_part is not None:
        soft_stage_exprs.append((labor_part, "отклонения трудоёмкости"))
    uniform_part = _expr_uniform_deviation(
        model, uniform_dev_keys, uniform_penalty_params, w.uniform_spend_deviation
    )
    if uniform_part is not None:
        soft_stage_exprs.append((uniform_part, "равномерное освоение"))

    for expr, stage_label in soft_stage_exprs:
        status_name, elapsed, stage_obj = _run_minimize_stage(model, expr, per_stage_limit)
        solve_time += elapsed
        if stage_obj is not None:
            last_objective_value = stage_obj
        if status_name not in ("OPTIMAL", "FEASIBLE"):
            return _solver_failed(stage_label)

    if baseline_dev_keys and w.plan_deviation:
        baseline_expr = _sum_terms(w.plan_deviation * model.baseline_dev[k] for k in baseline_dev_keys)
        status_name, elapsed, stage_obj = _run_minimize_stage(model, baseline_expr, per_stage_limit)
        solve_time += elapsed
        if stage_obj is not None:
            last_objective_value = stage_obj
        if status_name not in ("OPTIMAL", "FEASIBLE"):
            return _solver_failed("отклонение от базового плана")

    if status_name == "NOT_SOLVED":
        _, status_name, elapsed = _run_solver(model, time_limit_sec)
        solve_time += elapsed

    if status_name not in ("OPTIMAL", "FEASIBLE"):
        return PlanningResult(
            year=year,
            allocations=[],
            deficits=[],
            conflicts=[
                ConflictRecord(
                    code="SOLVER_FAILED",
                    message=f"Решатель завершился со статусом {status_name}",
                )
            ],
            contract_balances=[],
            solver_status=status_name,
            objective_value=0.0,
            solve_time_sec=round(solve_time, 3),
            month_transfers=[],
        )

    allocations: list[AllocationRecord] = []
    manual_keys = {
        (a.employee_id, a.contract_id, m, a.payment_kind)
        for a in ctx.manual_assignments
        for m in range(a.month_from, a.month_to + 1)
    }
    for key in alloc_keys:
        val = _value(model.alloc[key])
        if val > 0.005:
            e_id, c_id, m, kind = key
            is_manual = (e_id, c_id, m, kind) in manual_keys
            allocations.append(
                AllocationRecord(
                    employee_id=e_id,
                    contract_id=c_id,
                    year=year,
                    month=m,
                    payment_kind=kind,
                    amount=round(val, 2),
                    is_manual=is_manual,
                    source="manual" if is_manual else "optimizer",
                )
            )

    deficits: list[DeficitRecord] = []
    if deficit_agg_keys:
        paid_by_emp_month: dict[tuple[str, int], float] = defaultdict(float)
        for a in allocations:
            paid_by_emp_month[(a.employee_id, a.month)] += a.amount
        for d_key in deficit_agg_keys:
            e_id, m = d_key
            d_amt = _value(deficit[d_key])
            if d_amt <= 0.005:
                continue
            paid = paid_by_emp_month.get(d_key, 0.0)
            due = paid + d_amt
            employee = employees[e_id]
            deficits.append(
                DeficitRecord(
                    employee_id=e_id,
                    year=year,
                    month=m,
                    due_amount=round(due, 2),
                    paid_amount=round(paid, 2),
                    amount=round(d_amt, 2),
                    reasons=_explain_deficit(ctx, employee, m),
                )
            )

    labor_pm_attributions: list[LaborPmAttribution] = []
    if labor_pm_keys and hasattr(model, "labor_pm"):
        for key in labor_pm_keys:
            val = _value(model.labor_pm[key])
            if val <= 0.005:
                continue
            e_id, c_id, m, lp_idx = key
            lp = ctx.labor_plans[lp_idx]
            labor_pm_attributions.append(
                LaborPmAttribution(
                    employee_id=e_id,
                    contract_id=c_id,
                    year=year,
                    month=m,
                    labor_row_id=labor_row_id(lp),
                    position=lp.position,
                    equivalence_group=lp.equivalence_group,
                    person_months=round(val, 4),
                )
            )

    labor_payment_attributions: list[LaborPaymentAttribution] = []
    if labor_payment_keys and hasattr(model, "labor_pay"):
        for key in labor_payment_keys:
            val = _value(model.labor_pay[key])
            if val <= 0.005:
                continue
            e_id, c_id, m, kind, lp_idx = key
            lp = ctx.labor_plans[lp_idx]
            labor_payment_attributions.append(
                LaborPaymentAttribution(
                    employee_id=e_id,
                    contract_id=c_id,
                    year=year,
                    month=m,
                    payment_kind=kind,
                    labor_row_id=labor_row_id(lp),
                    position=lp.position,
                    equivalence_group=lp.equivalence_group,
                    amount=round(val, 2),
                )
            )

    transfers: list[MonthTransfer] = []
    balances = _compute_balances(
        ctx,
        allocations,
        model.close,
        model.carry,
        contracts,
        employees,
        uses,
    )
    conflicts: list[ConflictRecord] = []
    if status_name not in ("OPTIMAL", "FEASIBLE"):
        conflicts.append(
            ConflictRecord(
                code="SOLVER_FAILED",
                message=f"Решатель завершился со статусом {status_name}",
            )
        )

    objective_value = last_objective_value if status_name in ("OPTIMAL", "FEASIBLE") else 0.0
    return PlanningResult(
        year=year,
        allocations=allocations,
        deficits=deficits,
        conflicts=conflicts,
        contract_balances=balances,
        solver_status=status_name,
        objective_value=objective_value,
        solve_time_sec=round(time.perf_counter() - t0, 3),
        month_transfers=transfers,
        labor_pm_attributions=labor_pm_attributions,
        labor_payment_attributions=labor_payment_attributions,
    )


def _compute_balances(
    ctx: PlanningContext,
    allocations: list[AllocationRecord],
    balance_close,
    carry_forward,
    contracts: dict,
    employees: dict,
    uses: dict,
) -> list[ContractBalanceRecord]:
    records: list[ContractBalanceRecord] = []
    spent: dict[tuple[str, int], float] = defaultdict(float)
    for a in allocations:
        spent[(a.contract_id, a.month)] += a.amount

    for c in ctx.contracts:
        allow_carry = _allows_carryover(c)
        opening = 0.0

        for m in range(1, 13):
            inflow = _month_inflow(ctx, c.id, m)
            sp = spent.get((c.id, m), 0.0)
            key = (c.id, m)

            if key in balance_close:
                closing = _value(balance_close[key])
            elif allow_carry:
                closing = opening + inflow - sp
            else:
                closing = inflow - sp

            if allow_carry and key in carry_forward:
                carried = _value(carry_forward[key])
            elif allow_carry:
                carried = max(0.0, closing)
            else:
                carried = 0.0

            salary_reserve_req = 0.0
            min_bal = min_balance_for_month(c, m)

            records.append(
                ContractBalanceRecord(
                    contract_id=c.id,
                    year=ctx.year,
                    month=m,
                    opening_balance=round(opening, 2),
                    inflow=round(inflow, 2),
                    spent=round(sp, 2),
                    closing_balance=round(closing, 2),
                    carried_forward=round(carried, 2),
                    forfeited=0.0,
                    salary_reserve_required=round(salary_reserve_req, 2),
                    min_balance_required=round(min_bal, 2),
                    transfer_in=0.0,
                    transfer_out=0.0,
                    movable_balance=round(max(0.0, closing - min_bal), 2),
                    carryover_allowed=allow_carry,
                )
            )
            opening = carried if allow_carry else 0.0
    return records


def _monthly_pool(ctx: PlanningContext, contract, month: int) -> float:
    """Доступно на месяц: входящий остаток + поступление (для пояснения дефицита)."""
    if not _allows_carryover(contract):
        return _month_inflow(ctx, contract.id, month)
    pool = 0.0
    for m in range(1, month + 1):
        pool += _month_inflow(ctx, contract.id, m)
    return pool


def _explain_deficit(ctx: PlanningContext, employee, month: int) -> list[str]:
    reasons: list[str] = []
    year = ctx.year
    total_due = _monthly_total_due(employee)
    possible_month = 0.0
    possible_cumulative = 0.0
    for c in ctx.contracts:
        if not contract_allows_month(c, year, month):
            reasons.append(f"договор {c.id}: недоступен в месяце {month}")
            continue
        if employee.allowed_contracts and c.id not in employee.allowed_contracts:
            continue
        if c.id in employee.forbidden_contracts:
            reasons.append(f"договор {c.id}: запрещён для сотрудника")
            continue
        allows_any = any(_contract_allows_payment(c, k) for k in PAYMENT_KINDS)
        if not allows_any:
            continue
        inflow_m = _month_inflow(ctx, c.id, month)
        possible_month += inflow_m
        if _allows_carryover(c):
            possible_cumulative += _monthly_pool(ctx, c, month)
        else:
            possible_cumulative += inflow_m

    if possible_month < total_due:
        reasons.append(
            f"поступления в месяце {month} ({possible_month:.0f}) меньше дохода ({total_due:.0f})"
        )
        if possible_cumulative > possible_month:
            reasons.append(
                f"с учётом переноса с прошлых месяцев по договорам доступно ~{possible_cumulative:.0f}"
            )
    elif possible_cumulative < total_due:
        reasons.append(
            f"даже с переносом остатка доступно ~{possible_cumulative:.0f}, нужно {total_due:.0f}"
        )
    if not reasons:
        reasons.append("не удалось распределить при текущих ограничениях (ставки, лимиты, фиксации)")
    return reasons[:8]
