"""Оптимизационный расчёт распределения ФОТ (MIP, Pyomo + HiGHS)."""

from __future__ import annotations

import os
import sys
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
from fot_planner.position_reference import normalize_position
from fot_planner.labor_rules import (
    contract_has_labor_plan,
    employee_can_place_on_contract,
    employee_compatible_with_labor_row,
    labor_average_balance_gap,
    labor_payment_terms_for_row,
    labor_pm_terms_for_row,
    labor_row_allows_month,
    planned_labor_amount,
)
from fot_planner.payment_split import (
    employee_monthly_payment_due,
    employee_reference_salary_cap,
    max_salary_amount_if_contract_used,
    salary_position_options,
)
from fot_planner.payment_kind import LABOR_PAYMENT_KINDS
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
    keeps_staff_rate_without_opening_extra,
    max_total_quarters,
    quarters_to_rate,
    row_max_total_quarters,
    staff_rate_min_quarters,
)
from fot_planner.payroll_rules import (
    AVERAGE_LIMIT_MODE,
    PayrollRuleContext,
    apply_payroll_rules,
)
from fot_planner.contract_calendar import (
    contract_allows_month,
    contract_allows_payment_month,
    contract_payment_window_includes_month,
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
#: Цена недобора по строке РКМ внутри допуска. Была 0,01 — «решить ничью в
#: пользу полного закрытия»; при такой цене строка спокойно закрывалась на
#: нижней границе допуска (15,2 из 16). Экономисту нужно ровное закрытие,
#: поэтому недобор стоит столько же, сколько отклонение сверх допуска.
LABOR_CLOSE_TIEBREAK = 1.0
LEX_STAGE_TOLERANCE = 1.0   # рубль: стадии считаются в рублях и усл. ед., а нулевую стадию с допуском 0,01 MIP не удерживал
MIP_REL_GAP = 0.005


def _month_inflow(ctx: PlanningContext, contract_id: str, month: int) -> float:
    """Поступление средств в месяце (физическая касса из «фот_по_месяцам»)."""
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


def _employee_person_key(employee) -> str:
    name = str(getattr(employee, "full_name", "") or "").strip().lower()
    return " ".join(name.split()) or employee.id


def _row_max_total_quarters(employee) -> int:
    return row_max_total_quarters(
        employee.employment_category,
        getattr(employee, "employment_type", "auto"),
        employee.rate,
        employee.position,
    )


def _person_max_total_quarters(employees_for_person) -> int:
    max_values = [
        max_total_quarters(employee.employment_category)
        for employee in employees_for_person
    ]
    category_max = min(max_values) if max_values else max_total_quarters("regular")
    fixed_staff_q = sum(
        staff_rate_min_quarters(employee.rate)
        for employee in employees_for_person
        if keeps_staff_rate_without_opening_extra(
            employee.employment_category,
            employee.position,
        )
    )
    return max(category_max, fixed_staff_q)


def _account_starts_with_23(contract) -> bool:
    account = str(getattr(contract, "account", "") or "")
    account = "".join(ch for ch in account if ch.isdigit())
    return account.startswith("23")


def _active_secret_allowance_rates(
    ctx: PlanningContext,
    contracts: dict,
    employee_id: str,
    year: int,
    month: int,
) -> list[float]:
    rates: list[float] = []
    for row in ctx.secret_allowances:
        if row.employee_id != employee_id:
            continue
        secret_contract = contracts.get(row.secret_contract_id)
        if secret_contract is None:
            continue
        if contract_payment_window_includes_month(
            secret_contract,
            year,
            month,
            PaymentKind.K120,
        ):
            rates.append(row.rate)
    return rates


def _employee_has_active_secret_allowance(
    ctx: PlanningContext,
    contracts: dict,
    employee_id: str,
    year: int,
    month: int,
) -> bool:
    return bool(
        _active_secret_allowance_rates(ctx, contracts, employee_id, year, month)
    )


def _contract_can_pay_secret_allowance(contract, year: int, month: int) -> bool:
    """120 идёт с договора оклада, и только если договор её разрешает.

    Графа «120 разрешена» — выключатель экономиста: гостайну с этого источника
    платить нельзя, значит оклад человека из листа 120 должен сидеть на другом
    договоре. Раньше графа для договора оклада не проверялась и 120 платилась
    всегда. Счёт «23…» — исключение: 120 без оклада на нём.
    """
    if contract.allow_salary and contract.allow_secret and contract_allows_payment_month(
        contract,
        year,
        month,
        PaymentKind.SALARY,
    ):
        return True
    if _account_starts_with_23(contract) and contract_payment_window_includes_month(
        contract,
        year,
        month,
        PaymentKind.K120,
    ):
        return True
    return False


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
    # Доказывать оптимальность до последней копейки незачем: стадии
    # фиксируются с относительным допуском 1e-4, зазор 0,1 % его не превышает.
    try:
        solver.highs_options = {"mip_rel_gap": MIP_REL_GAP}
    except Exception:  # noqa: BLE001
        pass
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
    # Лимит времени: appsi отдаёт статус «aborted», хотя допустимый план у
    # HiGHS может быть. Признак — конечная верхняя граница целевой функции.
    if term == TerminationCondition.maxTimeLimit:
        try:
            ub = float(results.problem.upper_bound)
            if ub == ub and abs(ub) != float("inf"):
                return "FEASIBLE"
        except (AttributeError, TypeError, ValueError):
            pass
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


def _expr_order_incentive_usage(alloc_keys, alloc, weight: float):
    if weight <= 0:
        return None
    order_terms = [
        alloc[key]
        for key in alloc_keys
        if key[3] is PaymentKind.ORDER_INCENTIVE
    ]
    if not order_terms:
        return None
    return _sum_terms(order_terms)


def _expr_model_var_sum(model, attr_name: str):
    var = getattr(model, attr_name, None)
    if var is None:
        return None
    terms = [var[key] for key in var]
    return _sum_terms(terms) if terms else None


def _taste_goal_terms(
    model,
    w,
    *,
    employee_contract_keys,
    scheme_change_keys,
    salary_change_keys,
    alloc_keys,
    uses,
    uniform_dev_keys,
    uniform_penalty_params,
    n_employees: int,
    total_fot: float,
) -> list[dict]:
    """Цели взвешенной стадии: сырое выражение, масштаб, вес, взвешенный член.

    Сырое выражение — в естественных единицах (переводов, рублей), масштаб
    переводит его в долю: переводы на число людей, рубли на ФОТ. Взвешенный
    член = вес × сырое / масштаб; из них складывается целевая функция стадии.
    Те же записи после решения дают метрики для листа «цели».
    """
    people = max(1, n_employees)
    fot = max(1.0, total_fot)
    out = []

    switch_raw = (
        _sum_terms(model.salary_change[k] for k in salary_change_keys)
        if salary_change_keys and hasattr(model, "salary_change") else None
    )
    # Считается любая смена набора договоров оклада от месяца к месяцу: и
    # перевод, и открытие второго договора. Для финансиста это одно и то же
    # «дёрнули человека», но название должно это говорить.
    out.append({"code": "switch", "name": "Смены договора оклада (переводы и открытия)",
                "unit": "шт", "raw": switch_raw, "scale": people,
                "weight": w.salary_contract_switch})

    admin_raw = _expr_admin_complexity(
        model,
        employee_contract_keys=employee_contract_keys,
        scheme_change_keys=scheme_change_keys,
        alloc_keys=alloc_keys,
        uses=uses,
        admin_weight=1.0,
    )
    out.append({"code": "admin", "name": "Административная сложность выплат",
                "unit": "усл. ед.", "raw": admin_raw, "scale": people,
                "weight": w.admin_complexity})

    uniform_raw = None
    if uniform_dev_keys and hasattr(model, "uniform_penalty_pos"):
        uniform_raw = _sum_terms(
            model.uniform_penalty_pos[k] + model.uniform_penalty_neg[k]
            for k in uniform_dev_keys
        )
    out.append({"code": "uniform", "name": "Отклонение от равномерного освоения",
                "unit": "₽", "raw": uniform_raw, "scale": fot,
                "weight": w.uniform_spend_deviation})

    out.append({"code": "payment_change", "name": "Изменение сумм выплат между месяцами",
                "unit": "₽", "raw": _expr_model_var_sum(model, "payment_change"),
                "scale": fot, "weight": w.payment_change})

    for t in out:
        t["weighted"] = (
            t["weight"] * t["raw"] / t["scale"]
            if t["raw"] is not None and t["weight"] and t["weight"] > 0 else None
        )
    return out


def _goal_metrics(stage_values: dict[str, float], taste: list[dict]) -> list:
    """Лист «цели»: стадии-правила со значениями и взвешенные цели с вкладом."""
    from fot_planner.models import GoalMetric

    goals = []
    rubles = {"сумма приказов"}
    for no, (label, value) in enumerate(stage_values.items(), start=1):
        goals.append(GoalMetric(
            code="stage%d" % no, name=label, value=round(value, 2),
            unit="₽" if label in rubles else "усл. ед.", priority="стадия %d" % no,
        ))
    for t in taste:
        if t["raw"] is None:
            continue
        raw = _value(t["raw"])
        norm = raw / t["scale"]
        goals.append(GoalMetric(
            code=t["code"], name=t["name"], value=round(raw, 2), unit=t["unit"],
            priority="вес", weight=t["weight"], normalized=round(norm, 6),
            contribution=round((t["weight"] or 0.0) * norm, 4),
        ))
    return goals


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
    labor_month_dev_keys=(),
):
    if weight <= 0:
        return None
    terms = []
    for k in labor_dev_keys:
        if k in labor_soft_indices:
            terms.append(weight * model.labor_dev[k])
            # Недобор внутри допуска: стоит в сто раз меньше настоящего
            # отклонения, поэтому только разрешает ничью в пользу полного
            # закрытия строки и никогда не перевешивает само отклонение.
            terms.append(weight * LABOR_CLOSE_TIEBREAK * model.labor_gap[k])
    for k in labor_amount_dev_keys:
        if k in labor_soft_indices:
            scale = max(labor_plan_amount.get(k, 0.0), 1.0)
            terms.append(weight * model.labor_amount_dev[k] / scale)
    for k in labor_balance_dev_keys:
        if k in labor_soft_indices:
            scale = max(labor_plan_amount.get(k, 0.0), 1.0)
            terms.append(weight * model.labor_balance_dev[k] / scale)
    for k in labor_month_dev_keys:
        if k[0] in labor_soft_indices:
            terms.append(weight * model.labor_month_dev[k])
    return _sum_terms(terms) if terms else None


def _expr_labor_employee_balance(
    model,
    *,
    labor_employee_balance_keys,
    labor_employee_balance_scale: dict[tuple[str, str, int, int], float],
    weight: float,
):
    if not labor_employee_balance_keys or weight <= 0:
        return None
    return _sum_terms(
        (weight / max(labor_employee_balance_scale.get(k, 0.0), 1.0))
        * model.labor_employee_balance_dev[k]
        for k in labor_employee_balance_keys
    )


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
    integer_objective: bool = False,
) -> tuple[str, float, float | None]:
    """Минимизировать expr, зафиксировать результат ограничением expr <= best + tolerance."""
    if expr is None:
        return "OPTIMAL", 0.0, None
    if hasattr(model, "lex_stage_obj"):
        model.del_component("lex_stage_obj")
    model.lex_stage_obj = pyo.Objective(expr=expr, sense=pyo.minimize)
    model.__dict__["_integer_stage"] = integer_objective
    # Фиксации прежних стадий: (ограничение, выражение, значение, допуск).
    # Если стадия вдруг неразрешима — а модель с теми же ограничениями только
    # что решалась, — виновата погрешность фиксации; ослабляем последнюю
    # в десять раз и пробуем ещё раз.
    fixes = model.__dict__.setdefault("_lex_fixes", [])
    elapsed = 0.0
    # Цели стадий — суммы неотрицательных штрафов и отклонений, ноль — их
    # нижняя грань. Если на решении предыдущей стадии цель уже ноль,
    # решать нечего: фиксируем ноль и идём дальше. Две стадии П4 с нулевой
    # целью тратили по 127 с на полный перебор (09.09.2026).
    if fixes and not integer_objective:
        try:
            current = _value(expr)
        except Exception:  # noqa: BLE001 — переменная без значения: решаем как обычно
            current = None
        if current is not None and current <= max(tolerance, 1e-6):
            print("[стадия] %s: уже 0 на текущем решении, без решателя" % (label or "?"),
                  file=sys.stderr)
            model.lex_stage_obj.deactivate()
            fixes.append((model.cons.add(expr <= tolerance), expr, 0.0, tolerance))
            return "OPTIMAL", 0.0, 0.0
    for attempt in range(3):
        _, status_name, dt = _run_solver(model, time_limit_sec)
        elapsed += dt
        if status_name in ("OPTIMAL", "FEASIBLE"):
            break
        if status_name == "INFEASIBLE" and fixes and fixes[-1][3] > 0 and attempt < 2:
            con, fexpr, fbest, ftol = fixes[-1]
            con.deactivate()
            ftol *= 10
            fixes[-1] = (model.cons.add(fexpr <= fbest + ftol), fexpr, fbest, ftol)
            continue
        model.lex_stage_obj.deactivate()
        print("[стадия] %s: %s, %.0f с" % (label or "?", status_name, elapsed), file=sys.stderr)
        return status_name, elapsed, None
    best = _value(expr)
    print("[стадия] %s: %s, %.0f с, значение %.2f" % (label or "?", status_name, elapsed, best),
          file=sys.stderr)
    model.lex_stage_obj.deactivate()
    # Допуск относительный: значения стадий — сотни тысяч рублей, а у MIP
    # есть собственная погрешность целочисленности. С абсолютным 0,01
    # следующая стадия иногда получала неразрешимую модель на ровном месте.
    tol = 0.0 if integer_objective else max(tolerance, abs(best) * 1e-4)
    if integer_objective:
        best = round(best)
    fixes.append((model.cons.add(expr <= best + tol), expr, best, tol))
    return status_name, elapsed, best


def _run_solver(model, time_limit_sec: int):
    t0 = time.perf_counter()
    # Один решатель на всю модель: appsi переводит модель в HiGHS один раз, а
    # дальше передаёт только изменения — новую цель и фиксации стадий.
    # Новый решатель на каждой стадии переводил модель заново, и на демо из
    # восьми человек тривиальная стадия занимала минуты (09.09: 33 минуты
    # на девять стадий вместо нескольких).
    solver = model.__dict__.get("_lex_solver")
    if solver is None:
        solver = _create_solver(time_limit_sec)
        model.__dict__["_lex_solver"] = solver
        # Между стадиями меняются только цель и фиксации (новые или снятые
        # ограничения); переменные, параметры и выражения старых ограничений
        # неизменны. Проверять их заново на каждой стадии — это и есть
        # минуты на стадию с нулевой целью.
        if os.environ.get("FOT_APPSI_FAST", "0") == "1" and hasattr(solver, "update_config"):
            uc = solver.update_config
            uc.check_for_new_or_removed_constraints = True
            uc.check_for_new_or_removed_vars = False
            uc.check_for_new_or_removed_params = False
            uc.check_for_new_objective = True
            uc.update_constraints = False
            uc.update_vars = False
            uc.update_params = False
            uc.update_named_expressions = False
            uc.update_objective = False
    if hasattr(solver, "config") and hasattr(solver.config, "time_limit"):
        solver.config.time_limit = time_limit_sec
    if hasattr(solver, "config") and hasattr(solver.config, "load_solution"):
        solver.config.load_solution = False
    # Для количества договоров доказываем целочисленный минимум без зазора.
    if hasattr(solver, "highs_options"):
        solver.highs_options["mip_rel_gap"] = 0.0 if model.__dict__.get("_integer_stage") else MIP_REL_GAP
    # Обёртка Pyomo (LegacySolverInterface.solve) на каждом вызове пишет в
    # config.time_limit свой аргумент timelimit — по умолчанию None. Поэтому
    # выставленный выше config.time_limit не действовал, и стадия с лимитом
    # 120 с шла 230 с (09.09.2026). Лимит передаём аргументом.
    # Тёплый старт: решение предыдущей стадии — допустимый план для
    # следующей (фиксации только сужают область), и HiGHS начинает с него,
    # а не ищет допустимый план заново.
    warm = bool(model.__dict__.get("_lex_warm"))
    results = solver.solve(model, load_solutions=False, timelimit=time_limit_sec,
                           warmstart=warm)
    status_name = _status_name(results)
    if status_name in ("OPTIMAL", "FEASIBLE"):
        if hasattr(results, "solution_loader") and results.solution_loader is not None:
            results.solution_loader.load_vars()
        elif hasattr(solver, "config") and hasattr(solver.config, "load_solution"):
            solver.config.load_solution = True
            solver.load_vars()
        model.__dict__["_lex_warm"] = True
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
    payroll_limit_mode: str = AVERAGE_LIMIT_MODE,
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
                    if kind is PaymentKind.K120:
                        if not _employee_has_active_secret_allowance(
                            ctx,
                            contracts,
                            e.id,
                            year,
                            m,
                        ):
                            continue
                        if not _contract_can_pay_secret_allowance(c, year, m):
                            continue
                    else:
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
    salary_position_specs: dict[tuple[str, str, int, int], tuple[str | None, str | None]] = {}
    for (e_id, c_id, m, _kind) in salary_alloc_keys:
        e = employees[e_id]
        c = contracts[c_id]
        for opt in salary_position_options(c, e):
            k = (e_id, c_id, m, opt.position_rule_index)
            salary_position_keys.append(k)
            salary_position_cap[k] = employee_reference_salary_cap(e)
            salary_position_specs[k] = (opt.position, opt.equivalence_group)
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
                # План по месяцам: вне месяцев этапа строку закрывать нельзя.
                if not labor_row_allows_month(lp, m):
                    continue
                if not employee_compatible_with_labor_row(e, lp):
                    continue
                pay_kinds = [kind for kind in LABOR_PAYMENT_KINDS
                             if (e.id, c_id, m, kind) in alloc_key_set]
                # Договор человеку не разрешён (графы «разрешенные» и
                # «запрещенные договоры») — выплат с него нет, значит нет и
                # человеко-месяцев. Без этого переменная оставалась свободной,
                # и строку РКМ «закрывал» тот, кого на договоре нет.
                if not pay_kinds:
                    continue
                labor_pm_keys.append((e.id, c_id, m, lp_idx))
                for kind in pay_kinds:
                    labor_payment_keys.append((e.id, c_id, m, kind, lp_idx))

    labor_pm_key_set = set(labor_pm_keys)
    labor_payment_key_set = set(labor_payment_keys)

    baseline_dev_keys = alloc_keys if ctx.baseline_plan else []
    labor_dev_keys = list(labor_row_indices)
    labor_amount_dev_keys = list(labor_dev_keys)
    labor_balance_dev_keys = list(labor_dev_keys)
    # План по месяцам: отклонение считается и в каждом месяце — иначе 4
    # чел.-мес. на июнь–сентябрь закрылись бы как 2+2+0+0.
    labor_month_dev_keys = [
        (lp_idx, m)
        for lp_idx in labor_row_indices
        if ctx.labor_plans[lp_idx].monthly
        for m in months
        if ctx.labor_plans[lp_idx].monthly.get(m, 0.0) > 0
    ]
    labor_month_dev_key_set = set(labor_month_dev_keys)
    labor_employee_balance_keys = [
        key
        for key in labor_pm_keys
        if ctx.labor_plans[key[3]].avg_monthly_labor_cost
    ]
    labor_employee_balance_scale = {
        key: float(ctx.labor_plans[key[3]].avg_monthly_labor_cost or 0.0)
        for key in labor_employee_balance_keys
    }

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
    payment_change_keys: list[tuple[str, str, PaymentKind, int]] = [
        (e_id, c_id, kind, m)
        for (e_id, c_id, m, kind) in alloc_keys
        if m > 1 and (e_id, c_id, m - 1, kind) in alloc_key_set
    ]
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
    if payment_change_keys:
        model.payment_change = pyo.Var(
            payment_change_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M)
        )
    model.labor_dev = pyo.Var(labor_dev_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M))
    model.labor_gap = pyo.Var(labor_dev_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M))
    if labor_month_dev_keys:
        model.labor_month_dev = pyo.Var(
            labor_month_dev_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M)
        )
    if labor_amount_dev_keys:
        model.labor_amount_dev = pyo.Var(
            labor_amount_dev_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M)
        )
    if labor_balance_dev_keys:
        model.labor_balance_dev = pyo.Var(
            labor_balance_dev_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M)
        )
    if labor_employee_balance_keys:
        model.labor_employee_balance_dev = pyo.Var(
            labor_employee_balance_keys, domain=pyo.NonNegativeReals, bounds=(0, BIG_M)
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

    if payment_change_keys:
        for e_id, c_id, kind, month in payment_change_keys:
            prev_key = (e_id, c_id, month - 1, kind)
            curr_key = (e_id, c_id, month, kind)
            change = model.payment_change[(e_id, c_id, kind, month)]
            model.cons.add(change >= alloc[curr_key] - alloc[prev_key])
            model.cons.add(change >= alloc[prev_key] - alloc[curr_key])

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
            open_rate_q=open_rate_q,
            open_rate_key_set=open_rate_key_set,
        )
    )

    for key in alloc_keys:
        e_id, c_id, m, kind = key
        fixed = _manual_fixed_amount(ctx.manual_assignments, e_id, c_id, m, kind)
        if fixed is not None and fixed > 0:
            model.cons.add(alloc[key] == fixed)
        elif _must_use_pair(ctx.manual_assignments, e_id, c_id, m, kind):
            model.cons.add(uses[key] == 1)

    # У кого из людей есть строка, которая может быть основным местом.
    persons_with_main_row = {
        _employee_person_key(x) for x in ctx.employees
        if getattr(x, "employment_type", None) != "part_time"
    }
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
            if e.employment_type == "part_time":
                # Строка совместительства не становится основным местом —
                # но только если основное у человека есть в другой строке.
                # Безусловный запрет делал модель неразрешимой там, где все
                # строки человека помечены совместительством: правило «у
                # получающего оклад ровно одно основное место» тогда не
                # выполнить (кризис-стенд, случаи 09 и 26).
                if _employee_person_key(e) in persons_with_main_row:
                    model.cons.add(w == 0)
                model.cons.add(q <= PART_QUARTERS_MAX)

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
            max_q = _row_max_total_quarters(e)
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
                if has_salary_month:
                    # Основное место — у человека, а не у каждой его строки:
                    # у второй строки (например, аналитик на 0,25 сверх
                    # инженера) основного быть не может. Здесь только «не
                    # больше одного», равенство единице требуется ниже, по
                    # человеку целиком.
                    is_main_row = _sum_terms(is_main[k] for k in main_keys)
                    model.cons.add(is_main_row <= 1)
                    main_q_expr = _sum_terms(rate_on_main_q[k] for k in keys_em)
                    # Если строка основная, её основная ставка равна штатной;
                    # если нет — вся ставка строки идёт совместительством.
                    model.cons.add(main_q_expr == staff_q_min * is_main_row)
                    model.cons.add(
                        _sum_terms(open_rate_q[k] for k in keys_em) >= staff_q_min
                    )
                    model.cons.add(
                        _sum_terms(open_rate_q[k] for k in keys_em) - main_q_expr
                        <= PART_QUARTERS_MAX
                    )
                elif not has_salary_month:
                    # Только flex-выплаты: штатная ставка на договоре без деления основное/совместительство.
                    for k in keys_em:
                        model.cons.add(is_main[k] == 0)
                        model.cons.add(open_rate_q[k] <= MAIN_QUARTERS_MAX)

        employees_by_person: dict[str, list] = defaultdict(list)
        for employee in ctx.employees:
            employees_by_person[_employee_person_key(employee)].append(employee)
        for person_employees in employees_by_person.values():
            max_q = _person_max_total_quarters(person_employees)
            for m in months:
                person_keys = [
                    k
                    for employee in person_employees
                    for k in open_rate_keys
                    if k[0] == employee.id and k[2] == m
                ]
                if not person_keys:
                    continue
                model.cons.add(
                    _sum_terms(open_rate_q[k] for k in person_keys) <= max_q
                )
                person_main_keys = [k for k in person_keys if k in main_eligible_keys]
                if person_main_keys:
                    # Совместительство только поверх основного: если человек в
                    # этом месяце получает оклад, ровно одна его ставка —
                    # основное место, остальные идут сверх неё.
                    needs_main = any(
                        employee_monthly_payment_due(employee) > 0
                        and any(k[0] == employee.id for k in person_main_keys)
                        for employee in person_employees
                    )
                    model.cons.add(
                        _sum_terms(is_main[k] for k in person_main_keys)
                        == (1 if needs_main else 0)
                    )

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
                for kind in LABOR_PAYMENT_KINDS
                if (e_id, c_id, m, kind) in uses
            ]
            payment_pm_linked.add(link_key)
            if not pay_uses:
                # Договор человеку не разрешён — выплат с него нет вовсе.
                # Раньше такая пара просто пропускалась, и человеко-месяцы
                # оставались свободной переменной: строку РКМ «закрывал» тот,
                # кого на договоре нет (Орлов, 0,4 чел.-мес. и 0 ₽).
                model.cons.add(_sum_terms(pm_on_contract) == 0)
                continue
            pay_sum = _sum_terms(pay_uses)
            if link_key in open_rate_key_set:
                # Чел.-мес. строк договора не больше открытой на нём ставки.
                # Жёсткое равенство («ставка вся уходит в строки») закрывало
                # строки ровно, но делало модель неразрешимой там, где строка
                # уже занята по числу специалистов: кризис-стенд ловил это
                # случаями 09 и 26. Полноту закрытия обеспечивает не запрет, а
                # цена недобора — вес LABOR_CLOSE_TIEBREAK в целевой функции.
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
                for kind in LABOR_PAYMENT_KINDS
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

    if labor_employee_balance_keys:
        for e_id, c_id, m, lp_idx in labor_employee_balance_keys:
            lp = ctx.labor_plans[lp_idx]
            avg_cost = lp.avg_monthly_labor_cost or 0.0
            if avg_cost <= 0:
                continue
            pm_key = (e_id, c_id, m, lp_idx)
            if pm_key not in labor_pm_key_set:
                continue
            pay_terms = [
                labor_pay[(e_id, c_id, m, kind, lp_idx)]
                for kind in LABOR_PAYMENT_KINDS
                if (e_id, c_id, m, kind, lp_idx) in labor_payment_key_set
            ]
            if not pay_terms:
                continue
            amount_expr = _sum_terms(pay_terms)
            target_expr = avg_cost * labor_pm[pm_key]
            dev = model.labor_employee_balance_dev[(e_id, c_id, m, lp_idx)]
            model.cons.add(dev >= amount_expr - target_expr)
            model.cons.add(dev >= target_expr - amount_expr)

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

    # Число привлекаемых специалистов по строке РКМ: людей, чьи человеко-месяцы
    # или выплаты отнесены на строку, в каждом месяце не больше, чем задано.
    # Жёсткое условие: РКМ обосновывает штат, а не только человеко-месяцы.
    # Считаем именно отнесённых на строку, а не всех подходящих под неё с
    # окладом на договоре: ведущий инженер, закрывающий свою строку, подходит
    # и под строку инженеров, но место в ней не занимает.
    # Человек в месяце закрывает одну строку РКМ. Без этого решатель делил
    # месяц ведущего инженера между его строкой и строкой инженеров (0,48 и
    # 0,52), подгоняя среднюю стоимость, — в отчёте экономиста такой строки
    # быть не может: ставка на договоре стоит на одной должности.
    # Новые двоичные переменные заводим только там, где у человека на договоре
    # в месяце больше одной подходящей строки; с одной строкой признаком
    # «занят на строке» служит уже существующий uses[оклад] — лишние
    # двоичные переменные заметно замедляли решатель.
    if labor_pm_keys:
        rows_by_ecm: dict[tuple[str, str, int], list] = defaultdict(list)
        for k in labor_pm_keys:
            rows_by_ecm[(k[0], k[1], k[2])].append(k)
        multi_keys = [k for k in labor_pm_keys if len(rows_by_ecm[(k[0], k[1], k[2])]) > 1]
        multi_set = set(multi_keys)
        if multi_keys:
            model.labor_on = pyo.Var(multi_keys, domain=pyo.Binary)
        labor_on: dict = {}
        for k in labor_pm_keys:
            if k in multi_set:
                labor_on[k] = model.labor_on[k]
                model.cons.add(labor_pm[k] <= TOTAL_RATE_MAX_REGULAR * model.labor_on[k])
                for kind in LABOR_PAYMENT_KINDS:
                    pay_key = (k[0], k[1], k[2], kind, k[3])
                    if pay_key in labor_payment_key_set:
                        model.cons.add(labor_pay[pay_key] <= BIG_M * model.labor_on[k])
            else:
                salary_key = (k[0], k[1], k[2], PaymentKind.SALARY)
                labor_on[k] = uses[salary_key] if salary_key in alloc_key_set else None
        for ecm_keys in rows_by_ecm.values():
            if len(ecm_keys) > 1:
                model.cons.add(_sum_terms(model.labor_on[k] for k in ecm_keys) <= 1)
        for lp_idx, lp in enumerate(ctx.labor_plans):
            if not lp.headcount or lp.year != year:
                continue
            for m in months:
                on = [labor_on[k] for k in labor_pm_keys
                      if k[3] == lp_idx and k[2] == m and labor_on.get(k) is not None]
                if on:
                    model.cons.add(_sum_terms(on) <= lp.headcount)

    # Чужую строку РКМ (по правилам замещения) человек закрывает любой своей
    # ставкой — основной или открытым совместительством: «либо открыть 0,5
    # по совместительству, либо закрыть 1,0». Должность и оклад при этом
    # остаются своими; в выгрузке основное место всегда своей должностью.

    # Одна должность в одном подразделении у человека бывает только один раз.
    # Инженер на ставку в отделе не откроет там же совместительство инженером:
    # в своём подразделении вторая ставка идёт по соседней должности из правил
    # замещения, а инженером — только на договоре другого подразделения.
    # Договор без подразделения под правило не попадает.
    if open_rate_keys:
        for e in ctx.employees:
            primary_rows = [
                row for row in employees_by_person[_employee_person_key(e)]
                if row.employment_type != "part_time"
                and normalize_position(row.position) == normalize_position(e.position)
            ]
            for c in ctx.contracts:
                c_dep = (getattr(c, "department", "") or "").strip().lower()
                if not c_dep:
                    continue
                # Под какие строки РКМ этого договора человек вообще подходит
                # и есть ли среди них чужая должность. Если чужой нет, вторая
                # ставка здесь была бы второй ставкой по своей же должности в
                # своём подразделении — такой не бывает. Ограничиваем саму
                # ставку, а не только закрытие строки: иначе человек открывал
                # совместительство и просто не закрывал им строку.
                fit_rows = [
                    lp_idx for lp_idx in labor_row_indices
                    if ctx.labor_plans[lp_idx].contract_id == c.id
                    and employee_compatible_with_labor_row(e, ctx.labor_plans[lp_idx])
                ]
                own_only = all(
                    normalize_position(ctx.labor_plans[lp_idx].position)
                    == normalize_position(e.position)
                    for lp_idx in fit_rows
                )
                for m in months:
                    key = (e.id, c.id, m)
                    if key not in open_rate_key_set:
                        continue
                    # Compare with the person's active PRIMARY appointment,
                    # not with the department written on their part-time row.
                    if not any(
                        (row.department or "").strip().lower() == c_dep
                        and employee_active_in_month(row, year, m)
                        for row in primary_rows
                    ):
                        continue
                    if own_only:
                        # Своя должность (или РКМ нет вовсе) — ставка на этом
                        # договоре возможна только как основное место.
                        model.cons.add(open_rate_q[key] <= MAIN_QUARTERS_MAX * is_main[key])
                        continue
                    # Есть чужая строка: совместительство можно, но закрывать
                    # им строку своей должности нельзя.
                    for lp_idx in fit_rows:
                        pm_key = (e.id, c.id, m, lp_idx)
                        if pm_key not in labor_pm_key_set:
                            continue
                        if (normalize_position(ctx.labor_plans[lp_idx].position)
                                == normalize_position(e.position)):
                            model.cons.add(
                                labor_pm[pm_key] <= TOTAL_RATE_MAX_REGULAR * is_main[key]
                            )

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
            model.cons.add(model.labor_gap[lp_idx] >= plan_pm - pm_expr)
        if pm_expr is not None and lp.monthly:
            for m in months:
                key = (lp_idx, m)
                if key not in labor_month_dev_key_set:
                    continue
                plan_m = lp.monthly.get(m, 0.0)
                month_expr = labor_pm_terms_for_row(labor_pm, lp_idx, [m])
                if month_expr is None:
                    continue
                model.cons.add(model.labor_month_dev[key] >= month_expr - (1.0 + tol) * plan_m)
                model.cons.add(model.labor_month_dev[key] >= (1.0 - tol) * plan_m - month_expr)
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
    # Лимит — на стадию, как и написано в справке CLI («--time-limit»).
    # Раньше делился на восемь, но до 09.09.2026 обёртка Pyomo его вовсе не
    # передавала в HiGHS; когда лимит заработал, 20 с на стадию давали
    # заведомо плохой план (стадия обрывалась до оптимума и фиксировалась).
    per_stage_limit = max(20, time_limit_sec)

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
            label="сумма дефицита",
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
            label="ранний дефицит",
        )
        solve_time += elapsed
        if stage_obj is not None:
            last_objective_value = stage_obj
        if status_name not in ("OPTIMAL", "FEASIBLE"):
            return _solver_failed("этап раннего дефицита")

    soft_stage_exprs: list[tuple[object, str]] = []
    labor_part = _expr_labor_deviations(
        model,
        labor_dev_keys=labor_dev_keys,
        labor_amount_dev_keys=labor_amount_dev_keys,
        labor_balance_dev_keys=labor_balance_dev_keys,
        labor_plan_amount=labor_plan_amount,
        labor_soft_indices=labor_soft_indices,
        weight=w.labor_deviation,
        labor_month_dev_keys=labor_month_dev_keys,
    )
    if labor_part is not None:
        soft_stage_exprs.append((labor_part, "отклонения трудоёмкости"))
    labor_employee_balance_part = _expr_labor_employee_balance(
        model,
        labor_employee_balance_keys=labor_employee_balance_keys,
        labor_employee_balance_scale=labor_employee_balance_scale,
        weight=w.labor_deviation,
    )
    if labor_employee_balance_part is not None:
        soft_stage_exprs.append(
            (
                labor_employee_balance_part,
                "равномерность стоимости трудоёмкости по сотрудникам",
            )
        )
    preferred_anchor = _preferred_salary_anchor_contract_id(ctx)
    non_anchor_part = _expr_non_anchor_salary_penalty(
        uses, alloc_keys, preferred_anchor, w.salary_contract_switch
    )
    if non_anchor_part is not None:
        soft_stage_exprs.append(
            (non_anchor_part, "оклад не на предпочтительном договоре")
        )
    order_part = _expr_order_incentive_usage(
        alloc_keys,
        alloc,
        w.order_incentive_use,
    )
    if order_part is not None:
        soft_stage_exprs.append((order_part, "сумма приказов"))
    staff_2556_deviation_part = _expr_model_var_sum(
        model,
        "staff_2556_limit_deviation",
    )
    if staff_2556_deviation_part is not None:
        soft_stage_exprs.append(
            (staff_2556_deviation_part, "отклонение штатной части от П2556")
        )
    goz_bep_under_part = _expr_model_var_sum(model, "goz_bep_average_under_limit")
    if goz_bep_under_part is not None:
        soft_stage_exprs.append(
            (goz_bep_under_part, "недобор средней БЭП по ГОЗ")
        )
    p4_under_part = _expr_model_var_sum(model, "p4_group_under_limit")
    if p4_under_part is not None:
        soft_stage_exprs.append((p4_under_part, "недобор средней П4"))
    # Предел П4 не потолок, а норма: месяц с надбавкой 124 режется ровно по
    # пределу на ставку. Превышение запрещено жёстко, а эта стадия убирает
    # недобор — вместе они дают в отчёте нулевое отклонение.
    p4_deviation_part = _expr_model_var_sum(model, "p4_employee_limit_deviation")
    if p4_deviation_part is not None:
        soft_stage_exprs.append((p4_deviation_part, "отклонение сотрудников от П4"))
    # Стадии выше — правила: трудоёмкость, приказы, лимиты. Их порядок задан
    # здесь и финансистом не обсуждается.
    stage_values: dict[str, float] = {}
    for stage_no, (expr, stage_label) in enumerate(soft_stage_exprs, start=1):
        status_name, elapsed, stage_obj = _run_minimize_stage(
            model, expr, per_stage_limit, label=stage_label)
        solve_time += elapsed
        if stage_obj is not None:
            last_objective_value = stage_obj
            stage_values[stage_label] = stage_obj
        if status_name not in ("OPTIMAL", "FEASIBLE"):
            return _solver_failed(stage_label)

    # Одна взвешенная стадия для «вкусовых» целей — того, на что финансист
    # даёт претензии. Каждое слагаемое нормировано на свой масштаб, поэтому
    # веса — чистые приоритеты: значимо только их отношение друг к другу.
    taste = _taste_goal_terms(
        model, w,
        employee_contract_keys=employee_contract_keys,
        scheme_change_keys=scheme_change_keys,
        salary_change_keys=salary_change_keys,
        alloc_keys=alloc_keys,
        uses=uses,
        uniform_dev_keys=uniform_dev_keys,
        uniform_penalty_params=uniform_penalty_params,
        n_employees=len(ctx.employees),
        total_fot=sum(c.total_fot for c in ctx.contracts),
    )
    weighted = [t["weighted"] for t in taste if t["weighted"] is not None]
    if weighted:
        status_name, elapsed, stage_obj = _run_minimize_stage(
            model, _sum_terms(weighted), per_stage_limit, label="взвешенные цели"
        )
        solve_time += elapsed
        if stage_obj is not None:
            last_objective_value = stage_obj
        if status_name not in ("OPTIMAL", "FEASIBLE"):
            return _solver_failed("взвешенные цели")

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
            employee = employees[e_id]
            assigned_position = employee.position
            assigned_group = employee.equivalence_group
            assigned_from_position_rule = False
            for pos_key, spec in salary_position_specs.items():
                ee, cc, mm, _pr_idx = pos_key
                if ee != e_id or cc != c_id or mm != m:
                    continue
                if pos_key in pos_used and _value(pos_used[pos_key]) > 0.5:
                    assigned_position = spec[0] or employee.position
                    assigned_group = spec[1] or employee.equivalence_group
                    assigned_from_position_rule = bool(contracts[c_id].position_rules)
                    break
            if (
                not assigned_from_position_rule
                and labor_pm_keys
                and hasattr(model, "labor_pm")
                and not bool(round(_value(is_main[key])))
            ):
                best_labor_value = 0.0
                for lp_key in labor_pm_keys:
                    ee, cc, mm, lp_idx = lp_key
                    if ee != e_id or cc != c_id or mm != m:
                        continue
                    lp = ctx.labor_plans[lp_idx]
                    if not lp.position and not lp.equivalence_group:
                        continue
                    labor_value = _value(model.labor_pm[lp_key])
                    if labor_value > best_labor_value:
                        best_labor_value = labor_value
                        assigned_position = lp.position or employee.position
                        assigned_group = lp.equivalence_group or employee.equivalence_group
            open_rate_attributions.append(
                OpenRateAttribution(
                    employee_id=e_id,
                    contract_id=c_id,
                    year=year,
                    month=m,
                    open_rate=round(quarters_to_rate(q), 4),
                    is_main=bool(round(_value(is_main[key]))),
                    position=assigned_position,
                    equivalence_group=assigned_group,
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
    goals = _goal_metrics(stage_values, taste) if status_name in ("OPTIMAL", "FEASIBLE") else []
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
        goals=goals,
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
