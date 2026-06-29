"""Оптимизационный расчёт распределения ФОТ (MIP, Pyomo + HiGHS)."""

from __future__ import annotations

import time
from collections import defaultdict

import pyomo.environ as pyo
from pyomo.opt import SolverStatus, TerminationCondition

from fot_planner.fot_schedule import (
    active_months_in_year,
    min_balance_for_month,
    month_inflow_amount,
    monthly_spend_targets,
)
from fot_planner.labor_rules import (
    contract_has_labor_plan,
    employee_can_place_on_contract,
    employee_compatible_with_labor_row,
    labor_average_balance_gap,
    labor_payment_terms_for_row,
    labor_pm_terms_for_row,
    planned_labor_amount,
)
from fot_planner.payment_split import (
    employee_monthly_payment_due,
    employee_reference_salary_cap,
    max_salary_amount_if_contract_used,
    salary_position_options,
)
from fot_planner.models import (
    ADMIN_COMPLEXITY_FRAGMENT_FACTOR,
    ADMIN_COMPLEXITY_SCHEME_CHANGE_FACTOR,
    ADMIN_COMPLEXITY_YEAR_LINK_FACTOR,
    PAYMENT_KINDS,
    AllocationRecord,
    ConflictRecord,
    ContractBalanceRecord,
    DeficitRecord,
    LaborPaymentAttribution,
    LaborPmAttribution,
    ManualAssignment,
    ManualProhibition,
    OpenRateAttribution,
    PaymentKind,
    PlanningContext,
    PlanningResult,
    labor_row_id,
)
from fot_planner.open_rate_rules import (
    MAIN_QUARTERS_MAX,
    OPEN_RATE_STEP,
    PART_QUARTERS_MAX,
    TOTAL_RATE_MAX_REGULAR,
    max_total_quarters,
    quarters_to_rate,
    staff_rate_min_quarters,
)
from fot_planner.payroll_rules import (
    PLANNING_CAP_LIMIT_MODE,
    PayrollRuleContext,
    apply_payroll_rules,
)
from fot_planner.contract_calendar import (
    contract_allows_month,
    contract_allows_payment_month,
    payment_kind_enabled,
)
from fot_planner.validation import employee_active_in_month

BIG_M = 1e7
# При use=1 переменная alloc должна быть положительной (исключение нулевых начислений при активном use).
MIN_USE_ALLOC = 1.0
# Минимальная сумма на договоре при use=1 для переменной выплаты (запрет хвостов 1–100 ₽).
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


def _sum_terms(terms):
    terms = list(terms)
    if not terms:
        return 0
    return sum(terms) if len(terms) > 1 else terms[0]


def _value(obj) -> float:
    val = pyo.value(obj, exception=False)
    return 0.0 if val is None else float(val)


def _contract_month_spent(alloc, contract_id: str, month: int):
    """Сумма выплат с договора в месяце (все виды) или 0 для Pyomo."""
    terms = [alloc[k] for k in alloc if k[1] == contract_id and k[2] == month]
    if not terms:
        return 0
    return _sum_terms(terms)


def _use_or_zero(
    uses,
    alloc_key_set: set,
    e_id: str,
    c_id: str,
    month: int,
    kind: PaymentKind = PaymentKind.SALARY,
):
    key = (e_id, c_id, month, kind)
    return uses[key] if key in alloc_key_set else 0


def _contract_allows_payment(contract, kind: PaymentKind) -> bool:
    return payment_kind_enabled(contract, kind)


def _payment_ub(employee, contract, kind: PaymentKind) -> float | None:
    """Верхняя граница alloc по виду выплаты; None — переменную не создавать."""
    if kind == PaymentKind.SALARY:
        amount = max_salary_amount_if_contract_used(contract, employee)
        if amount <= 0:
            return None
        return amount
    if kind.is_non_salary:
        if employee.monthly_wage <= 0:
            return None
        return employee.monthly_wage
    return None


def _min_flex_fragment_when_used(ub: float) -> float:
    """Нижняя граница alloc при use=1 для надбавок 122/124."""
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


def _preferred_salary_anchor_contract_id(ctx: PlanningContext) -> str | None:
    """Предпочтительный договор для окладов: самый длительный допустимый, затем больший ФОТ."""
    candidates = [c for c in ctx.contracts if c.allow_salary]
    if not candidates:
        return None

    def span_days(contract) -> int:
        if contract.start_date and contract.end_date:
            return (contract.end_date - contract.start_date).days
        return 0

    anchor = max(
        candidates,
        key=lambda c: (span_days(c), c.total_fot),
    )
    return anchor.id


def _early_deficit_month_weight(month: int) -> int:
    """Январь = 12, декабрь = 1 — ранний дефицит штрафуется сильнее."""
    return 13 - month


def _emp_contract_month_used_or_zero(model, month_used_set: set, e_id: str, c_id: str, m: int):
    key = (e_id, c_id, m)
    if key in month_used_set:
        return model.emp_contract_month_used[key]
    return 0


def _expr_rate_below_staff(model, rate_below_staff_keys, weight: float):
    if not rate_below_staff_keys or weight <= 0:
        return None
    return _sum_terms(weight * model.rate_below_staff_dev[k] for k in rate_below_staff_keys)


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
    flex_keys = [k for k in alloc_keys if k[3].is_non_salary]
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
        if k[3] == PaymentKind.SALARY and k[1] != preferred_anchor_id
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


def _mccormick_binary_times_int(
    model,
    cons,
    product_var,
    binary_var,
    int_var,
    big_m: int,
):
    """product_var = binary_var * int_var (int_var ∈ [0, big_m])."""
    cons.add(product_var <= int_var)
    cons.add(product_var <= big_m * binary_var)
    cons.add(product_var >= int_var - big_m * (1 - binary_var))


def solve(
    ctx: PlanningContext,
    time_limit_sec: int = 120,
    payroll_limit_mode: str = PLANNING_CAP_LIMIT_MODE,
) -> PlanningResult:
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
            if contract_has_labor_plan(ctx, c_id) and not employee_can_place_on_contract(
                e, c, ctx
            ):
                continue
            for m in months:
                if not employee_active_in_month(e, year, m):
                    continue
                if e.allowed_contracts and c_id not in e.allowed_contracts:
                    continue
                if c_id in e.forbidden_contracts:
                    continue
                for kind in PAYMENT_KINDS:
                    if not contract_allows_payment_month(c, year, m, kind):
                        continue
                    if not _contract_allows_payment(c, kind):
                        continue
                    if _is_forbidden(ctx.manual_prohibitions, e.id, c_id, m, kind):
                        continue
                    ub = _payment_ub(e, c, kind)
                    if ub is None or ub <= 0:
                        continue

                    key = (e.id, c_id, m, kind)
                    alloc_keys.append(key)
                    alloc_ub[key] = ub

    close_keys: list[tuple[str, int]] = [
        (c_id, m) for c_id in contract_list for m in months
    ]

    carry_keys: list[tuple[str, int]] = [
        (c_id, m) for c_id in contract_list for m in months if m < 12
    ]
    year_contract_keys = []
    for e in ctx.employees:
        for c_id in contract_list:
            if any(k[0] == e.id and k[1] == c_id and k[3] == PaymentKind.SALARY for k in alloc_keys):
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

    # Выбор позиции для salary: position_used[e, c, m, pr_idx] ∈ {0,1}
    salary_alloc_keys = [k for k in alloc_keys if k[3] == PaymentKind.SALARY]

    salary_position_keys: list[tuple[str, str, int, int]] = []
    salary_position_cap: dict[tuple[str, str, int, int], float] = {}
    for (e_id, c_id, m, _kind) in salary_alloc_keys:
        e = employees[e_id]
        c = contracts[c_id]
        for opt in salary_position_options(c, e):
            k = (e_id, c_id, m, opt.position_rule_index)
            salary_position_keys.append(k)
            salary_position_cap[k] = employee_reference_salary_cap(e)
    salary_position_key_set = set(salary_position_keys)

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
                (e.id, c_id, m, PaymentKind.SALARY) in alloc_key_set
                or (e.id, c_id, m - 1, PaymentKind.SALARY) in alloc_key_set
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
        targets = monthly_spend_targets(c, year)

        for m, ideal in targets.items():
            if ideal <= 0:
                continue

            tolerance = max(
                UNIFORM_SPEND_TOLERANCE_AMOUNT,
                UNIFORM_SPEND_TOLERANCE_RATIO * ideal,
            )
            scale = max(ideal, 1.0)

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
            c_id = lp.contract_id
            c = contracts.get(c_id)
            if c is None:
                continue
            for m in months:
                if not employee_active_in_month(e, year, m):
                    continue
                if not contract_allows_month(c, year, m):
                    continue
                if not employee_compatible_with_labor_row(e, lp):
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
    if allow_deficit:
        for e in ctx.employees:
            for m in months:
                if not employee_active_in_month(e, year, m):
                    continue
                total_due = employee_monthly_payment_due(e)
                if total_due <= 0:
                    continue
                has_payment_vars = any(k[0] == e.id and k[2] == m for k in alloc_keys)
                if not has_payment_vars:
                    continue
                agg_key = (e.id, m)
                deficit_agg_keys.append(agg_key)
    deficit_agg_key_set = set(deficit_agg_keys)

    open_rate_keys: list[tuple[str, str, int]] = sorted(
        {
            (e_id, c_id, m)
            for (e_id, c_id, m, kind) in alloc_keys
            if kind == PaymentKind.SALARY
        }
    )
    open_rate_keys = [k for k in open_rate_keys if employees[k[0]].rate > 0]
    open_rate_key_set = set(open_rate_keys)
    main_eligible_keys = {
        (e_id, c_id, m)
        for (e_id, c_id, m, kind) in alloc_keys
        if kind == PaymentKind.SALARY
    }
    salary_rate_pos_keys: list[tuple[str, str, int, int]] = []
    salary_open_q_keys: list[tuple[str, str, int]] = []
    for (e_id, c_id, m, pr_idx) in salary_position_keys:
        if (e_id, c_id, m) in open_rate_key_set:
            salary_rate_pos_keys.append((e_id, c_id, m, pr_idx))
    # Для ограничений по группам (max_positions) и отчётов: открытая ставка по договору,
    # если salary используется (основное место и совместительство).
    salary_open_q_keys = [
        k for k in open_rate_keys if (k[0], k[1], k[2], PaymentKind.SALARY) in alloc_key_set
    ]
    rate_below_staff_keys: list[tuple[str, int]] = []
    for e in ctx.employees:
        if e.rate <= 0:
            continue
        for m in months:
            if not employee_active_in_month(e, year, m):
                continue
            if any(k[0] == e.id and k[2] == m for k in open_rate_keys):
                rate_below_staff_keys.append((e.id, m))

    model = pyo.ConcreteModel()
    model.alloc = pyo.Var(
        alloc_keys,
        domain=pyo.NonNegativeReals,
        bounds=lambda _, e, c, m, k: (0, alloc_ub[(e, c, m, k)]),
    )
    model.use = pyo.Var(alloc_keys, domain=pyo.Binary)
    if salary_position_keys:
        model.salary_position_used = pyo.Var(salary_position_keys, domain=pyo.Binary)
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
            bounds=(
                lambda _m, e_id, _c, _mo, _lp: (
                    0,
                    TOTAL_RATE_MAX_REGULAR,
                )
            ),
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

    is_main = {}
    open_rate_q = {}
    rate_on_main_q = {}
    salary_rate_pos = {}
    if open_rate_keys:
        model.open_rate_q = pyo.Var(
            open_rate_keys,
            domain=pyo.NonNegativeIntegers,
            bounds=(0, MAIN_QUARTERS_MAX),
        )
        model.is_main = pyo.Var(open_rate_keys, domain=pyo.Binary)
        model.rate_on_main_q = pyo.Var(
            open_rate_keys,
            domain=pyo.NonNegativeIntegers,
            bounds=(0, MAIN_QUARTERS_MAX),
        )
        if salary_rate_pos_keys:
            model.salary_rate_pos = pyo.Var(
                salary_rate_pos_keys,
                domain=pyo.NonNegativeIntegers,
                bounds=(0, MAIN_QUARTERS_MAX),
            )
        if salary_open_q_keys:
            model.salary_open_q = pyo.Var(
                salary_open_q_keys,
                domain=pyo.NonNegativeIntegers,
                bounds=(0, MAIN_QUARTERS_MAX),
            )
        if rate_below_staff_keys:
            model.rate_below_staff_dev = pyo.Var(
                rate_below_staff_keys,
                domain=pyo.NonNegativeIntegers,
                bounds=(0, MAIN_QUARTERS_MAX),
            )
        open_rate_q = model.open_rate_q
        is_main = model.is_main
        rate_on_main_q = model.rate_on_main_q
        salary_rate_pos = getattr(model, "salary_rate_pos", {})
    salary_open_q = getattr(model, "salary_open_q", {})

    if deficit_agg_keys:
        model.deficit = pyo.Var(
            deficit_agg_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M)
        )

    alloc = model.alloc
    uses = model.use
    pos_used = getattr(model, "salary_position_used", {})
    deficit = getattr(model, "deficit", {})
    labor_pm = getattr(model, "labor_pm", {})
    labor_pay = getattr(model, "labor_pay", {})

    for key in alloc_keys:
        e_id, _c_id, _m, kind = key
        ub = alloc_ub[key]
        model.cons.add(alloc[key] <= ub * uses[key])
        e = employees[e_id]
        if kind == PaymentKind.SALARY and employee_monthly_payment_due(e) > 0:
            # Конкретная сумма оклада задаётся ниже выбранной должностью и открытой ставкой.
            # Остаток полной зарплаты добирается надбавками/приказными выплатами.
            model.cons.add(alloc[key] >= MIN_USE_ALLOC * uses[key])
        elif kind.is_non_salary:
            floor = _min_flex_fragment_when_used(ub)
            model.cons.add(alloc[key] >= floor * uses[key])
        else:
            model.cons.add(alloc[key] >= MIN_USE_ALLOC * uses[key])

    if salary_position_keys:
        # Связываем бинарный выбор позиции с использованием salary.
        # Оклад равен должностному окладу, умноженному на открытую ставку договора.
        for (e_id, c_id, m, _kind) in salary_alloc_keys:
            salary_key = (e_id, c_id, m, PaymentKind.SALARY)
            if salary_key not in alloc_key_set:
                continue
            related_pos = [
                pos_used[(e_id, c_id, m, pr_idx)]
                for (ee, cc, mm, pr_idx) in salary_position_key_set
                if ee == e_id and cc == c_id and mm == m
            ]
            if not related_pos:
                # Нет вариантов позиций → salary запрещён.
                model.cons.add(uses[salary_key] == 0)
                continue
            model.cons.add(_sum_terms(related_pos) == uses[salary_key])
            if (e_id, c_id, m) in open_rate_key_set:
                staff_rate = employees[e_id].rate
                scale = OPEN_RATE_STEP / staff_rate if staff_rate > 0 else 1.0
                cap_terms = []
                for (ee, cc, mm, pr_idx) in salary_position_key_set:
                    if ee != e_id or cc != c_id or mm != m:
                        continue
                    sp_key = (e_id, c_id, m, pr_idx)
                    if sp_key not in salary_rate_pos:
                        continue
                    cap_terms.append(
                        scale
                        * salary_position_cap[(e_id, c_id, m, pr_idx)]
                        * salary_rate_pos[sp_key]
                    )
                    _mccormick_binary_times_int(
                        model,
                        model.cons,
                        salary_rate_pos[sp_key],
                        pos_used[(e_id, c_id, m, pr_idx)],
                        open_rate_q[(e_id, c_id, m)],
                        MAIN_QUARTERS_MAX,
                    )
                if cap_terms:
                    model.cons.add(alloc[salary_key] == _sum_terms(cap_terms))
                else:
                    model.cons.add(uses[salary_key] == 0)
            else:
                model.cons.add(
                    alloc[salary_key]
                    == _sum_terms(
                        salary_position_cap[(e_id, c_id, m, pr_idx)]
                        * pos_used[(e_id, c_id, m, pr_idx)]
                        for (ee, cc, mm, pr_idx) in salary_position_key_set
                        if ee == e_id and cc == c_id and mm == m
                    )
                )

    apply_payroll_rules(
        PayrollRuleContext(
            ctx=ctx,
            model=model,
            alloc=alloc,
            uses=uses,
            alloc_keys=alloc_keys,
            alloc_key_set=alloc_key_set,
            contracts=contracts,
            employees=employees,
            months=months,
            contract_list=contract_list,
            sum_terms=_sum_terms,
            contract_allows_month=contract_allows_month,
            employee_active_in_month=employee_active_in_month,
            limit_mode=payroll_limit_mode,
        )
    )

    for key in alloc_keys:
        e_id, c_id, m, kind = key
        fixed = _manual_fixed_amount(ctx.manual_assignments, e_id, c_id, m, kind)
        if fixed is not None and fixed > 0:
            model.cons.add(alloc[key] == fixed)
        elif _must_use_pair(ctx.manual_assignments, e_id, c_id, m, kind):
            model.cons.add(uses[key] == 1)

    if open_rate_keys:
        for key in open_rate_keys:
            e_id, c_id, m = key
            e = employees[e_id]
            c = contracts[c_id]
            w = is_main[key]
            q = open_rate_q[key]
            mq = rate_on_main_q[key]

            _mccormick_binary_times_int(model, model.cons, mq, w, q, MAIN_QUARTERS_MAX)
            model.cons.add(q <= MAIN_QUARTERS_MAX * w + PART_QUARTERS_MAX * (1 - w))
            model.cons.add(q >= w)

            if not c.allow_main_employment:
                model.cons.add(w == 0)
            if not c.allow_part_time:
                model.cons.add(q <= MAIN_QUARTERS_MAX * w)

            # Бизнес-правило: открытая ставка на договоре подразумевает оклад с этого договора.
            if employee_monthly_payment_due(e) > 0:
                salary_key = (e_id, c_id, m, PaymentKind.SALARY)
                if salary_key in alloc_key_set:
                    model.cons.add(q <= MAIN_QUARTERS_MAX * uses[salary_key])
                    model.cons.add(uses[salary_key] <= q)
                else:
                    # Если salary на договоре невозможен, то и ставка на нём запрещена.
                    model.cons.add(q == 0)

        for e in ctx.employees:
            if e.rate <= 0:
                continue
            staff_q_min = staff_rate_min_quarters(e.rate)
            max_q = max_total_quarters(e.employment_category)
            for m in months:
                if not employee_active_in_month(e, year, m):
                    continue
                keys_em = [k for k in open_rate_keys if k[0] == e.id and k[2] == m]
                if not keys_em:
                    continue
                main_keys = [k for k in keys_em if k in main_eligible_keys]
                has_salary_month = bool(main_keys) and employee_monthly_payment_due(e) > 0

                model.cons.add(
                    _sum_terms(open_rate_q[k] for k in keys_em) <= max_q
                )
                if (e.id, m) in rate_below_staff_keys:
                    model.cons.add(
                        model.rate_below_staff_dev[(e.id, m)]
                        >= staff_q_min - _sum_terms(open_rate_q[k] for k in keys_em)
                    )

                if has_salary_month:
                    model.cons.add(_sum_terms(is_main[k] for k in main_keys) == 1)
                    main_q_expr = _sum_terms(rate_on_main_q[k] for k in keys_em)
                    model.cons.add(
                        _sum_terms(open_rate_q[k] for k in keys_em) - main_q_expr
                        <= PART_QUARTERS_MAX
                    )
                else:
                    # Только flex-выплаты: штатная ставка на договоре без деления основное/совместительство.
                    for k in keys_em:
                        model.cons.add(is_main[k] == 0)
                        model.cons.add(open_rate_q[k] <= MAIN_QUARTERS_MAX)

        for key in salary_open_q_keys:
            salary_key = (key[0], key[1], key[2], PaymentKind.SALARY)
            if salary_key in alloc_key_set:
                _mccormick_binary_times_int(
                    model,
                    model.cons,
                    salary_open_q[key],
                    uses[salary_key],
                    open_rate_q[key],
                    MAIN_QUARTERS_MAX,
                )

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
                    keys_em = [
                        k for k in open_rate_keys if k[0] == e.id and k[2] == m
                    ]
                    if keys_em:
                        model.cons.add(
                            _sum_terms(pm_terms)
                            <= OPEN_RATE_STEP
                            * _sum_terms(open_rate_q[k] for k in keys_em)
                        )
                    else:
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
            pay_sum = _sum_terms(pay_uses)
            if link_key in open_rate_key_set:
                model.cons.add(
                    _sum_terms(pm_on_contract)
                    <= OPEN_RATE_STEP * open_rate_q[link_key]
                )
                model.cons.add(
                    _sum_terms(pm_on_contract)
                    <= TOTAL_RATE_MAX_REGULAR * pay_sum
                )
            else:
                model.cons.add(_sum_terms(pm_on_contract) == 0)

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

        for (e_id, c_id, m, _kind, lp_idx) in labor_payment_key_set:
            pm_key = (e_id, c_id, m, lp_idx)
            if pm_key in labor_pm_key_set:
                continue
            pay_terms = [
                labor_pay[(e_id, c_id, m, kind, lp_idx)]
                for kind in PAYMENT_KINDS
                if (e_id, c_id, m, kind, lp_idx) in labor_payment_key_set
            ]
            if pay_terms:
                model.cons.add(_sum_terms(pay_terms) == 0)

        if open_rate_keys:
            for (e_id, c_id, m, _lp_idx) in labor_pm_keys:
                key = (e_id, c_id, m)
                if key not in open_rate_key_set:
                    continue
                pm_terms = [
                    labor_pm[(e_id, c_id, m, row_idx)]
                    for row_idx in labor_row_indices
                    if (e_id, c_id, m, row_idx) in labor_pm_key_set
                ]
                if pm_terms:
                    model.cons.add(
                        _sum_terms(pm_terms)
                        <= OPEN_RATE_STEP * open_rate_q[key]
                    )

    for e in ctx.employees:
        total_due = employee_monthly_payment_due(e)
        if total_due <= 0:
            continue
        for m in months:
            if not employee_active_in_month(e, year, m):
                continue
            all_allocs = [
                alloc[k] for k in alloc_keys if k[0] == e.id and k[2] == m
            ]
            if not all_allocs:
                continue

            if allow_deficit:
                agg_key = (e.id, m)
                if agg_key in deficit_agg_key_set:
                    model.cons.add(_sum_terms(all_allocs) + deficit[agg_key] == total_due)
            else:
                model.cons.add(_sum_terms(all_allocs) == total_due)

    for c_id in contract_list:
        c = contracts[c_id]

        for m in months:
            spent_expr = _contract_month_spent(alloc, c_id, m)

            inflow = _month_inflow(ctx, c_id, m)
            min_bal = min_balance_for_month(c, m)

            opening = 0 if m == 1 else model.carry[(c_id, m - 1)]
            model.cons.add(model.close[(c_id, m)] == opening + inflow - spent_expr)

            if min_bal > 0:
                model.cons.add(model.close[(c_id, m)] >= min_bal)

            if m < 12:
                model.cons.add(model.carry[(c_id, m)] == model.close[(c_id, m)])

        if c.requires_full_fot_spend:
            spend_full_fot = [
                _contract_month_spent(alloc, c_id, m)
                for m in months
                if contract_allows_month(c, year, m)
            ]
            if spend_full_fot:
                model.cons.add(_sum_terms(spend_full_fot) == c.total_fot)

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
                group_terms = []
                for (e_id, cid, mo, kind), uvar in uses.items():
                    if (
                        cid != c_id
                        or mo != m
                        or kind != PaymentKind.SALARY
                        or employees[e_id].equivalence_group != group
                    ):
                        continue
                    key = (e_id, cid, mo)
                    if key in salary_open_q:
                        group_terms.append(OPEN_RATE_STEP * salary_open_q[key])
                    elif key in open_rate_key_set:
                        group_terms.append(OPEN_RATE_STEP * open_rate_q[key] * uvar)
                    else:
                        group_terms.append(employees[e_id].rate * uvar)
                if group_terms:
                    model.cons.add(_sum_terms(group_terms) <= max_rate)

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
                u_prev = _use_or_zero(uses, alloc_key_set, e_id, c_id, m_prev, PaymentKind.SALARY)
                u_curr = _use_or_zero(uses, alloc_key_set, e_id, c_id, m, PaymentKind.SALARY)
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

    max_contracts_per_year = stab.max_contracts_per_year
    if max_contracts_per_year is not None and max_contracts_per_year > 0:
        for e_id, c_id in year_contract_keys:
            month_vars = [
                uses[(e_id, c_id, m, PaymentKind.SALARY)]
                for m in months
                if (e_id, c_id, m, PaymentKind.SALARY) in model.use
            ]
            for uvar in month_vars:
                model.cons.add(model.year_use[(e_id, c_id)] >= uvar)
            model.cons.add(model.year_use[(e_id, c_id)] <= _sum_terms(month_vars))

        for e in ctx.employees:
            flags = [model.year_use[(e.id, c_id)] for e_id, c_id in year_contract_keys if e_id == e.id]
            if flags:
                model.cons.add(_sum_terms(flags) <= max_contracts_per_year)

    if uniform_dev_keys:
        for key in uniform_dev_keys:
            ideal, tolerance, _scale = uniform_penalty_params[key]
            c_id, m = key
            actual = _contract_month_spent(alloc, c_id, m)
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
            payroll_limit_mode=payroll_limit_mode,
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
    rate_below_part = _expr_rate_below_staff(
        model, rate_below_staff_keys, w.rate_below_staff
    )
    if rate_below_part is not None:
        soft_stage_exprs.append((rate_below_part, "снижение ниже штатной ставки"))
    preferred_anchor = _preferred_salary_anchor_contract_id(ctx)
    non_anchor_part = _expr_non_anchor_salary_penalty(
        uses, alloc_keys, preferred_anchor, w.salary_contract_switch
    )
    if non_anchor_part is not None:
        soft_stage_exprs.append(
            (non_anchor_part, "оклад не на предпочтительном договоре")
        )
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
            payroll_limit_mode=payroll_limit_mode,
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

    open_rate_attributions: list[OpenRateAttribution] = []
    if open_rate_keys and hasattr(model, "open_rate_q"):
        for key in open_rate_keys:
            q = int(round(_value(open_rate_q[key])))
            if q <= 0:
                continue
            e_id, c_id, m = key
            open_rate_attributions.append(
                OpenRateAttribution(
                    employee_id=e_id,
                    contract_id=c_id,
                    year=year,
                    month=m,
                    open_rate=round(quarters_to_rate(q), 4),
                    is_main=bool(round(_value(is_main[key]))),
                )
            )

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
        payroll_limit_mode=payroll_limit_mode,
        labor_pm_attributions=labor_pm_attributions,
        labor_payment_attributions=labor_payment_attributions,
        open_rate_attributions=open_rate_attributions,
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
        opening = 0.0

        for m in range(1, 13):
            inflow = _month_inflow(ctx, c.id, m)
            sp = spent.get((c.id, m), 0.0)
            key = (c.id, m)

            if key in balance_close:
                closing = _value(balance_close[key])
            else:
                closing = opening + inflow - sp
            
            if key in carry_forward:
                carried = _value(carry_forward[key])
            else:
                carried = max(0.0, closing)

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
                    min_balance_required=round(min_bal, 2),
                    carryover_allowed=True,
                )
            )
            opening = carried
    return records


def _monthly_pool(ctx: PlanningContext, contract, month: int) -> float:
    """Доступно на месяц: входящий остаток + поступление (для пояснения дефицита)."""
    pool = 0.0
    for m in range(1, month + 1):
        pool += _month_inflow(ctx, contract.id, m)
    return pool


def _explain_deficit(ctx: PlanningContext, employee, month: int) -> list[str]:
    reasons: list[str] = []
    year = ctx.year
    total_due = employee_monthly_payment_due(employee)
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
        possible_cumulative += _monthly_pool(ctx, c, month)

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
