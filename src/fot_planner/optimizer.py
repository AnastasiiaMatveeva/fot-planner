"""Оптимизационный расчёт распределения ФОТ (MIP, Pyomo + HiGHS)."""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import date

import pyomo.environ as pyo
from pyomo.opt import SolverStatus, TerminationCondition

from fot_planner.fot_schedule import active_months_in_year, uniform_monthly_spend_target
from fot_planner.labor_rules import (
    is_goz_contract,
    planned_labor_groups,
    person_month_terms_for_labor,
)
from fot_planner.spend_plan import labor_pm_or_zero, planned_labor_pm_total, spent_or_zero
from fot_planner.reserve_rules import (
    min_balance_for_month,
    monthly_salary_reserve_expr,
    monthly_salary_reserve_value,
    requires_salary_reserve,
)
from fot_planner.spend_rules import must_fully_spend_fot, required_full_spend_month
from fot_planner.models import (
    ADMIN_COMPLEXITY_SCHEME_CHANGE_FACTOR,
    ADMIN_COMPLEXITY_YEAR_LINK_FACTOR,
    AllocationRecord,
    ConflictRecord,
    ContractBalanceRecord,
    DeficitRecord,
    ManualAssignment,
    ManualProhibition,
    MonthTransfer,
    PaymentKind,
    PlanningContext,
    PlanningResult,
)
from fot_planner.contract_calendar import contract_allows_month
from fot_planner.validation import employee_active_in_month

PAYMENT_KINDS: tuple[PaymentKind, ...] = ("salary", "allowance", "incentive")
FLEX_PAYMENT_KINDS: tuple[PaymentKind, ...] = ("allowance", "incentive")
BIG_M = 1e7
# Если use=1, alloc не может быть нулём (иначе фиктивные чел.-мес. и отриц. «бонус» в цели).
MIN_USE_ALLOC = 1.0
# Минимальная сумма на договоре при use=1 для allowance/incentive (запрет хвостов 1–100 ₽).
MIN_FLEX_FRAGMENT_AMOUNT = 1_000.0


def _month_inflow(ctx: PlanningContext, contract_id: str, month: int) -> float:
    """Поступление средств в месяце (не путать с доступным пулом с переносом)."""
    c = next(x for x in ctx.contracts if x.id == contract_id)
    for mb in c.monthly_budgets:
        if mb.year == ctx.year and mb.month == month:
            return mb.inflow_amount
    if c.monthly_budgets:
        return 0.0
    return c.total_fot / 12.0


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
    """
    Потолок месячного оклада (salary) с договора: max_monthly_payment × ставка.

    Потолок берется только по совместимым договорным позициям.
    Для allowance/incentive этот потолок не применяется.
    """
    if not contract.position_rules:
        return None

    caps = [
        pr.max_monthly_payment * employee.rate
        for pr in _compatible_position_rules(contract, employee)
        if pr.max_monthly_payment is not None
    ]
    return max(caps) if caps else None


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
    return sum(_employee_payment(emp, kind) for kind in PAYMENT_KINDS)


def _monthly_flex_due(emp) -> float:
    return _employee_payment(emp, "allowance") + _employee_payment(emp, "incentive")


def _salary_cap_shortfall(contract, employee) -> float:
    """Сколько оклада не помещается в salary из‑за потолка contract_positions."""
    cap = _contract_payment_cap(contract, employee)
    if cap is None:
        return 0.0
    return max(0.0, _employee_payment(employee, "salary") - cap)


def _flex_payment_ub(contract, employee, kind: PaymentKind) -> float:
    """Верхняя граница flex-выплаты: справочник + компенсация срезанного оклада (allowance)."""
    pay = _employee_payment(employee, kind)
    if kind == "allowance":
        pay += _salary_cap_shortfall(contract, employee)
    return pay


def _full_alloc_when_used(pay: float, ub: float) -> float:
    """Нижняя граница alloc при use=1: полная выплата вида, не «1 рубль»."""
    if pay > 0:
        return min(pay, ub)
    if ub > MIN_USE_ALLOC:
        return ub
    return MIN_USE_ALLOC


def _min_flex_fragment_when_used(_employee, kind: PaymentKind, ub: float) -> float:
    """Нижняя граница alloc при use=1 для allowance/incentive (без хвостов в 1 ₽)."""
    if _employee_payment(_employee, kind) <= 0:
        return MIN_USE_ALLOC
    return max(MIN_USE_ALLOC, min(MIN_FLEX_FRAGMENT_AMOUNT, ub))


def _employee_payment(emp, kind: PaymentKind) -> float:
    if kind == "salary":
        return emp.salary
    if kind == "allowance":
        return emp.allowance
    return emp.incentive


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


def solve(ctx: PlanningContext, time_limit_sec: int = 120) -> PlanningResult:
    t0 = time.perf_counter()
    year = ctx.year
    months = list(range(1, 13))

    employees = {e.id: e for e in ctx.employees}
    contracts = {c.id: c for c in ctx.contracts}
    contract_list = list(contracts.keys())
    min_fot_months = ctx.salary_stability.min_fot_months_for_salary_reserve

    alloc_keys: list[tuple[str, str, int, PaymentKind]] = []
    alloc_ub: dict[tuple[str, str, int, PaymentKind], float] = {}
    deficit_keys: list[tuple[str, int]] = []
    deficit_ub: dict[tuple[str, int], float] = {}
    flex_comp_keys: list[tuple[str, int]] = []

    for e in ctx.employees:
        total_due = _monthly_total_due(e)
        if total_due <= 0:
            continue
        for m in months:
            if employee_active_in_month(e, year, m):
                deficit_keys.append((e.id, m))
                deficit_ub[(e.id, m)] = total_due
                if _monthly_flex_due(e) < total_due:
                    flex_comp_keys.append((e.id, m))

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
                    if kind in FLEX_PAYMENT_KINDS:
                        pay = _flex_payment_ub(c, e, kind)
                    else:
                        pay = _employee_payment(e, kind)
                    if pay <= 0:
                        continue

                    key = (e.id, c_id, m, kind)
                    ub = pay
                    if kind == "salary":
                        cap = _contract_payment_cap(c, e)
                        if cap is not None:
                            ub = min(pay, cap)
                    alloc_keys.append(key)
                    alloc_ub[key] = ub

    allow_carry_by_contract: dict[str, bool] = {}
    close_keys: list[tuple[str, int]] = []
    carry_keys: list[tuple[str, int]] = []
    month_xfer_keys: list[tuple[str, int, int]] = []
    for c_id in contract_list:
        c = contracts[c_id]
        allow_carry = _allows_carryover(c)
        allow_carry_by_contract[c_id] = allow_carry
        if allow_carry:
            close_keys.extend((c_id, m) for m in months)
            carry_keys.extend((c_id, m) for m in months if m < 12)
            if ctx.allow_backward_reallocation:
                month_xfer_keys.extend(
                    (c_id, f, t)
                    for f in months
                    for t in months
                    if f > t
                    and contract_allows_month(c, year, f)
                    and contract_allows_month(c, year, t)
                )

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
    stab = ctx.salary_stability
    month_used_set = set(employee_contract_month_keys)

    salary_change_keys: list[tuple[str, int]] = []
    if stab.max_contracts_per_year > 1:
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
    for c in ctx.contracts:
        active = active_months_in_year(c, year)
        if not active or c.total_fot <= 0:
            continue
        for m in active:
            uniform_dev_keys.append((c.id, m))

    contract_labor_plan_pm: dict[tuple[str, str | None], float] = {}
    labor_reserve_keys: list[tuple[str, str | None, int]] = []
    for c in ctx.contracts:
        for group, plan_pm in planned_labor_groups(ctx, c.id):
            if plan_pm > 0 and c.total_fot > 0:
                contract_labor_plan_pm[(c.id, group)] = plan_pm
                for m in months:
                    if contract_allows_month(c, year, m):
                        labor_reserve_keys.append((c.id, group, m))

    baseline_dev_keys = alloc_keys if ctx.baseline_plan else []
    labor_dev_keys = [
        (c.id, group or "")
        for c in ctx.contracts
        if not is_goz_contract(c)
        for group, plan_pm in planned_labor_groups(ctx, c.id)
        if plan_pm > 0
    ]

    model = pyo.ConcreteModel()
    model.alloc = pyo.Var(
        alloc_keys,
        domain=pyo.NonNegativeReals,
        bounds=lambda _, e, c, m, k: (0, alloc_ub[(e, c, m, k)]),
    )
    model.use = pyo.Var(alloc_keys, domain=pyo.Binary)
    model.deficit = pyo.Var(
        deficit_keys,
        domain=pyo.NonNegativeReals,
        bounds=lambda _, e, m: (0, deficit_ub[(e, m)]),
    )
    if flex_comp_keys:
        model.flex_compensation = pyo.Var(
            flex_comp_keys,
            domain=pyo.NonNegativeReals,
            bounds=(0, BIG_M),
        )
    model.close = pyo.Var(close_keys, domain=pyo.NonNegativeReals)
    model.carry = pyo.Var(carry_keys, domain=pyo.NonNegativeReals)
    if month_xfer_keys:
        model.xfer = pyo.Var(month_xfer_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M))
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
        model.uniform_dev_pos = pyo.Var(
            uniform_dev_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M)
        )
        model.uniform_dev_neg = pyo.Var(
            uniform_dev_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M)
        )
    if labor_reserve_keys:
        model.remaining_labor = pyo.Var(
            labor_reserve_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M)
        )
    model.baseline_dev = pyo.Var(baseline_dev_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M))
    model.labor_dev = pyo.Var(labor_dev_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M))
    model.cons = pyo.ConstraintList()

    alloc = model.alloc
    uses = model.use

    for key in alloc_keys:
        e_id, _c_id, _m, kind = key
        ub = alloc_ub[key]
        model.cons.add(alloc[key] <= ub * uses[key])
        pay = _employee_payment(employees[e_id], kind)
        if kind == "salary" and pay > 0:
            floor = _full_alloc_when_used(pay, ub)
            if floor > 0:
                model.cons.add(alloc[key] >= floor * uses[key])
        elif kind in FLEX_PAYMENT_KINDS:
            floor = _min_flex_fragment_when_used(employees[e_id], kind, ub)
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

    for e in ctx.employees:
        total_due = _monthly_total_due(e)
        if total_due <= 0:
            continue
        flex_due = _monthly_flex_due(e)
        for m in months:
            if not employee_active_in_month(e, year, m):
                continue
            dkey = (e.id, m)
            if dkey not in model.deficit:
                continue
            all_allocs = [alloc[k] for k in alloc_keys if k[0] == e.id and k[2] == m]
            model.cons.add(_sum_terms(all_allocs) + model.deficit[dkey] == total_due)
            if dkey in flex_comp_keys:
                flex_allocs = [
                    alloc[k]
                    for k in alloc_keys
                    if k[0] == e.id and k[2] == m and k[3] in FLEX_PAYMENT_KINDS
                ]
                if flex_allocs:
                    model.cons.add(
                        model.flex_compensation[dkey]
                        >= _sum_terms(flex_allocs) - flex_due
                    )

    for c_id in contract_list:
        c = contracts[c_id]
        allow_carry = allow_carry_by_contract.get(c_id, True)
        need_reserve = requires_salary_reserve(c, min_fot_months=min_fot_months)
        year_spent_terms = []

        for m in months:
            spent_expr = spent_or_zero(alloc, c_id, m)
            year_spent_terms.append(spent_expr)

            inflow = _month_inflow(ctx, c_id, m)
            salary_reserve_expr = (
                monthly_salary_reserve_expr(employees, uses, c_id, m) if need_reserve else None
            )
            locked_backward = min_balance_for_month(c, m)

            if not allow_carry:
                if salary_reserve_expr is None:
                    model.cons.add(spent_expr <= inflow)
                else:
                    model.cons.add(spent_expr + salary_reserve_expr <= inflow)
                continue

            if month_xfer_keys:
                out_sum = _sum_terms(
                    model.xfer[(c_id, m, t)]
                    for t in months
                    if t < m and (c_id, m, t) in month_xfer_keys
                )
                in_sum = _sum_terms(
                    model.xfer[(c_id, f, m)]
                    for f in months
                    if f > m and (c_id, f, m) in month_xfer_keys
                )
            else:
                out_sum = 0
                in_sum = 0
            opening = 0 if m == 1 else model.carry[(c_id, m - 1)]

            model.cons.add(model.close[(c_id, m)] == opening + inflow - spent_expr + in_sum - out_sum)
            if locked_backward > 0:
                model.cons.add(out_sum <= opening + inflow + in_sum - spent_expr - locked_backward)
            if salary_reserve_expr is not None:
                model.cons.add(model.close[(c_id, m)] >= salary_reserve_expr)
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
                    model.cons.add(_sum_terms(spend_by_deadline) >= c.total_fot - 1.0)

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

    for m in months:
        for e in ctx.employees:
            salary_uses = [
                uses[k] for k in alloc_keys if k[0] == e.id and k[2] == m and k[3] == "salary"
            ]
            if len(salary_uses) > 1:
                model.cons.add(_sum_terms(salary_uses) <= 1)

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
        for e_id, m in salary_change_keys:
            m_prev = m - 1
            for c_id in contract_list:
                u_prev = _use_or_zero(uses, alloc_key_set, e_id, c_id, m_prev, "salary")
                u_curr = _use_or_zero(uses, alloc_key_set, e_id, c_id, m, "salary")
                model.cons.add(model.salary_change[(e_id, m)] >= u_prev - u_curr)
                model.cons.add(model.salary_change[(e_id, m)] >= u_curr - u_prev)

    if scheme_change_keys:
        for e_id, m in scheme_change_keys:
            m_prev = m - 1
            for c_id in contract_list:
                key_curr = (e_id, c_id, m)
                key_prev = (e_id, c_id, m_prev)
                if key_curr not in month_used_set or key_prev not in month_used_set:
                    continue
                u_curr = model.emp_contract_month_used[key_curr]
                u_prev = model.emp_contract_month_used[key_prev]
                model.cons.add(
                    model.emp_contract_scheme_changed[(e_id, m)] >= u_curr - u_prev
                )
                model.cons.add(
                    model.emp_contract_scheme_changed[(e_id, m)] >= u_prev - u_curr
                )

    if stab.max_contracts_per_year > 0:
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
                model.cons.add(_sum_terms(flags) <= stab.max_contracts_per_year)

    if uniform_dev_keys:
        for c_id, m in uniform_dev_keys:
            c = contracts[c_id]
            active = active_months_in_year(c, year)
            if not active:
                continue
            ideal = c.total_fot / len(active)
            actual = spent_or_zero(alloc, c_id, m)
            key = (c_id, m)
            model.cons.add(model.uniform_dev_pos[key] >= actual - ideal)
            model.cons.add(model.uniform_dev_neg[key] >= ideal - actual)

    if labor_reserve_keys:
        total_plan_by_contract = {
            c.id: planned_labor_pm_total(ctx, c.id)
            for c in ctx.contracts
        }
        for c_id, group, m in labor_reserve_keys:
            c = contracts[c_id]
            plan_pm = contract_labor_plan_pm[(c_id, group)]
            total_plan_pm = total_plan_by_contract.get(c_id, 0.0)
            if total_plan_pm <= 0:
                continue
            active_months = [mo for mo in months if contract_allows_month(c, year, mo)]
            cost_per_pm = c.total_fot / total_plan_pm
            months_to_m = [mo for mo in active_months if mo <= m]
            cumulative_spent = _sum_terms(
                spent_or_zero(alloc, c_id, mo) for mo in months_to_m
            )
            cumulative_labor = labor_pm_or_zero(employees, uses, c_id, months_to_m, group)
            rem_key = (c_id, group, m)
            model.cons.add(model.remaining_labor[rem_key] >= plan_pm - cumulative_labor)
            model.cons.add(
                c.total_fot - cumulative_spent
                >= cost_per_pm * model.remaining_labor[rem_key] - 1.0
            )

    for c in ctx.contracts:
        for group, plan_pm in planned_labor_groups(ctx, c.id):
            if plan_pm <= 0:
                continue

            pm_expr = labor_pm_or_zero(employees, uses, c.id, months, group)

            if is_goz_contract(c):
                model.cons.add(pm_expr >= (1.0 - stab.goz_labor_tolerance) * plan_pm)
                model.cons.add(pm_expr <= (1.0 + stab.goz_labor_tolerance) * plan_pm)
            else:
                dev_key = (c.id, group or "")
                if dev_key in model.labor_dev:
                    model.cons.add(model.labor_dev[dev_key] >= pm_expr - plan_pm)
                    model.cons.add(model.labor_dev[dev_key] >= plan_pm - pm_expr)

    baseline_map: dict[tuple[str, str, int, PaymentKind], float] = defaultdict(float)
    if ctx.baseline_plan:
        for rec in ctx.baseline_plan:
            baseline_map[(rec.employee_id, rec.contract_id, rec.month, rec.payment_kind)] += rec.amount
        for key in alloc_keys:
            base = baseline_map.get(key, 0.0)
            model.cons.add(model.baseline_dev[key] >= alloc[key] - base)
            model.cons.add(model.baseline_dev[key] >= base - alloc[key])

    w = ctx.weights
    # Целевая функция (мягкие штрафы). Жёсткие правила — в model.cons выше.
    objective_terms = []
    objective_terms.extend(w.uncovered_salary * model.deficit[k] for k in deficit_keys)
    if flex_comp_keys:
        objective_terms.extend(
            w.salary_compensation_via_flex * model.flex_compensation[k] for k in flex_comp_keys
        )
    if stab.max_contracts_per_year > 1 and salary_change_keys and w.salary_contract_switch:
        objective_terms.extend(
            w.salary_contract_switch * model.salary_change[k] for k in salary_change_keys
        )
    admin = w.admin_complexity
    if admin > 0:
        if employee_contract_keys:
            objective_terms.extend(
                admin
                * ADMIN_COMPLEXITY_YEAR_LINK_FACTOR
                * model.emp_contract_used[k]
                for k in employee_contract_keys
            )
        if scheme_change_keys:
            objective_terms.extend(
                admin
                * ADMIN_COMPLEXITY_SCHEME_CHANGE_FACTOR
                * model.emp_contract_scheme_changed[k]
                for k in scheme_change_keys
            )
    if w.flex_fragment:
        objective_terms.extend(
            w.flex_fragment * uses[k]
            for k in alloc_keys
            if k[3] in FLEX_PAYMENT_KINDS
        )
    if uniform_dev_keys:
        objective_terms.extend(
            w.uniform_spend_deviation
            * (model.uniform_dev_pos[k] + model.uniform_dev_neg[k])
            for k in uniform_dev_keys
        )
    objective_terms.extend(w.plan_deviation * model.baseline_dev[k] for k in baseline_dev_keys)
    objective_terms.extend(w.labor_deviation * model.labor_dev[k] for k in labor_dev_keys)
    if ctx.allow_backward_reallocation and month_xfer_keys:
        objective_terms.extend(model.xfer[k] for k in month_xfer_keys)
    model.objective = pyo.Objective(expr=_sum_terms(objective_terms), sense=pyo.minimize)

    solver = _create_solver(time_limit_sec)
    if hasattr(solver, "config") and hasattr(solver.config, "load_solution"):
        solver.config.load_solution = False
    results = solver.solve(model, load_solutions=False)
    solve_time = time.perf_counter() - t0
    status_name = _status_name(results)

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

    if hasattr(results, "solution_loader") and results.solution_loader is not None:
        results.solution_loader.load_vars()
    elif hasattr(solver, "config") and hasattr(solver.config, "load_solution"):
        solver.config.load_solution = True
        solver.load_vars()

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
    for e_id, m in deficit_keys:
        val = _value(model.deficit[(e_id, m)])
        if val > 0.005:
            deficits.append(
                DeficitRecord(
                    employee_id=e_id,
                    year=year,
                    month=m,
                    payment_kind=None,
                    amount=round(val, 2),
                    reasons=_explain_deficit(ctx, employees[e_id], m),
                )
            )

    transfers = (
        _extract_transfers(model.xfer, month_xfer_keys, year) if month_xfer_keys else []
    )
    balances = _compute_balances(
        ctx,
        allocations,
        model.close,
        model.carry,
        getattr(model, "xfer", None),
        month_xfer_keys,
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

    objective_value = _value(model.objective) if status_name in ("OPTIMAL", "FEASIBLE") else 0.0
    return PlanningResult(
        year=year,
        allocations=allocations,
        deficits=deficits,
        conflicts=conflicts,
        contract_balances=balances,
        solver_status=status_name,
        objective_value=objective_value,
        solve_time_sec=round(solve_time, 3),
        month_transfers=transfers,
    )


def _extract_transfers(month_xfer, keys: list[tuple[str, int, int]], year: int) -> list[MonthTransfer]:
    rows: list[MonthTransfer] = []
    for c_id, f, t in keys:
        amt = _value(month_xfer[(c_id, f, t)])
        if amt > 0.5:
            rows.append(
                MonthTransfer(
                    contract_id=c_id,
                    year=year,
                    from_month=f,
                    to_month=t,
                    amount=round(amt, 2),
                )
            )
    return rows


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
        if requires_salary_reserve(c, min_fot_months=ctx.salary_stability.min_fot_months_for_salary_reserve):
            reasons.append(
                f"договор {c.id}: требуется резерв по окладам назначенных сотрудников (× ставка)"
            )
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


def _compute_balances(
    ctx: PlanningContext,
    allocations: list[AllocationRecord],
    balance_close,
    carry_forward,
    month_xfer,
    month_xfer_keys: list[tuple[str, int, int]],
    contracts: dict,
    employees: dict,
    uses: dict,
) -> list[ContractBalanceRecord]:
    records: list[ContractBalanceRecord] = []
    spent: dict[tuple[str, int], float] = defaultdict(float)
    xfer_key_set = set(month_xfer_keys)
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

            salary_reserve_req = (
                monthly_salary_reserve_value(employees, uses, c.id, m)
                if requires_salary_reserve(
                    c, min_fot_months=ctx.salary_stability.min_fot_months_for_salary_reserve
                )
                else 0.0
            )
            min_bal = min_balance_for_month(c, m)
            if xfer_key_set and month_xfer is not None:
                t_in = sum(
                    _value(month_xfer[(c.id, f, m)])
                    for f in range(m + 1, 13)
                    if (c.id, f, m) in xfer_key_set
                )
                t_out = sum(
                    _value(month_xfer[(c.id, m, t)])
                    for t in range(1, m)
                    if (c.id, m, t) in xfer_key_set
                )
            else:
                t_in = 0.0
                t_out = 0.0
            backward_capacity = max(0.0, opening + inflow + t_in - sp - min_bal)

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
                    transfer_in=round(t_in, 2),
                    transfer_out=round(t_out, 2),
                    movable_balance=round(backward_capacity, 2),
                    carryover_allowed=allow_carry,
                )
            )
            opening = carried if allow_carry else 0.0
    return records
