"""Проверки результата планирования относительно демо-сценария."""

from __future__ import annotations

from collections import defaultdict
from collections import defaultdict

import math

import pandas as pd
import pytest

from fot_planner.models import AllocationRecord, PlanningResult
from fot_planner.optimizer import MIN_FLEX_FRAGMENT_AMOUNT
from fot_planner.validation import employee_active_in_month
from demo_business.scenario import (
    ANCHOR_CONTRACT,
    DEMO_YEAR,
    ENGINEER_GROUP,
    LAB_CONTRACT,
    LAB_GROUP,
    PROJECT_CONTRACT,
    DemoScenario,
    MIN_FLEX_FRAGMENT,
)


def _blocking_conflicts(result: PlanningResult) -> list:
    return [c for c in result.conflicts if not c.code.endswith("_WARNING")]


def assert_solver_success(result: PlanningResult) -> None:
    assert result.solver_status in ("OPTIMAL", "FEASIBLE"), result.solver_status
    assert not _blocking_conflicts(result), [c.message for c in _blocking_conflicts(result)]


def assert_no_deficits(result: PlanningResult) -> None:
    assert not result.deficits
    assert sum(d.amount for d in result.deficits) == pytest.approx(0, abs=0.01)


def _alloc_map(
    allocations: list[AllocationRecord],
) -> dict[tuple[str, int, str, str], float]:
    out: dict[tuple[str, int, str, str], float] = defaultdict(float)
    for a in allocations:
        if a.amount > 0.005:
            out[(a.employee_id, a.month, a.payment_kind, a.contract_id)] += a.amount
    return out


def _sum_kind(
    allocations: list[AllocationRecord],
    *,
    employee_id: str | None = None,
    month: int | None = None,
    kind: str | None = None,
    contract_id: str | None = None,
) -> float:
    total = 0.0
    for a in allocations:
        if employee_id and a.employee_id != employee_id:
            continue
        if month is not None and a.month != month:
            continue
        if kind and a.payment_kind != kind:
            continue
        if contract_id and a.contract_id != contract_id:
            continue
        total += a.amount
    return total


def salary_contracts_by_month(
    allocations: list[AllocationRecord], employee_id: str
) -> dict[int, str]:
    by_month: dict[int, str] = {}
    for a in allocations:
        if a.employee_id != employee_id or a.payment_kind != "salary" or a.amount < 0.01:
            continue
        assert a.month not in by_month, f"{employee_id}: два оклада в месяце {a.month}"
        by_month[a.month] = a.contract_id
    return by_month


def count_salary_changes(allocations: list[AllocationRecord], employee_id: str) -> int:
    by_month = salary_contracts_by_month(allocations, employee_id)
    changes = 0
    for m in range(2, 13):
        prev = by_month.get(m - 1)
        curr = by_month.get(m)
        if prev and curr and prev != curr:
            changes += 1
    return changes


def total_salary_changes(allocations: list[AllocationRecord], employee_ids) -> int:
    return sum(count_salary_changes(allocations, eid) for eid in employee_ids)


def assert_salary_mandatory(scenario: DemoScenario, result: PlanningResult) -> None:
    for emp in scenario.employees:
        if emp.monthly_wage <= 0:
            continue
        for m in range(1, 13):
            sal = _sum_kind(result.allocations, employee_id=emp.id, month=m, kind="salary")
            assert sal == pytest.approx(
                scenario.expected_monthly_salary(emp.id), rel=0, abs=1.0
            ), f"{emp.id} месяц {m}: оклад не выплачен полностью"


def assert_preferred_anchor_salary(scenario: DemoScenario, result: PlanningResult) -> None:
    """В success-сценарии предпочтительный grant-договор оптимален, но модель не обязана его выбирать."""
    anchor = scenario.anchor_contract_id()
    for emp in scenario.employees:
        by_month = salary_contracts_by_month(result.allocations, emp.id)
        for m in range(1, 13):
            assert by_month.get(m) == anchor, f"{emp.id} месяц {m}: ожидался {anchor}"
    assert total_salary_changes(
        result.allocations, (e.id for e in scenario.employees)
    ) == 0


def assert_allowance_routing(scenario: DemoScenario, result: PlanningResult) -> None:
    for emp in scenario.employees:
        if emp.allowance <= 0:
            continue
        for m in range(1, 13):
            expected_c = scenario.expected_allowance_contract(emp.id, m)
            if expected_c is None:
                continue
            amt = _sum_kind(
                result.allocations,
                employee_id=emp.id,
                month=m,
                kind="allowance",
                contract_id=expected_c,
            )
            assert amt == pytest.approx(
                scenario.expected_monthly_allowance(emp.id), rel=0, abs=1.0
            ), f"{emp.id} месяц {m}: allowance с {expected_c}"
            contracts_used = {
                c
                for (e, mo, k, c), v in _alloc_map(result.allocations).items()
                if e == emp.id and mo == m and k == "allowance" and v > 0.005
            }
            assert len(contracts_used) <= 1, f"{emp.id} месяц {m}: дробление allowance"


def assert_no_flex_tails(result: PlanningResult) -> None:
    floor = min(MIN_FLEX_FRAGMENT_AMOUNT, MIN_FLEX_FRAGMENT)
    for a in result.allocations:
        if a.payment_kind not in ("allowance", "incentive"):
            continue
        if a.amount <= 0.005:
            continue
        if a.amount < 0.02:
            continue
        assert a.amount >= floor - 0.01, (
            f"хвост {a.amount} у {a.employee_id} {a.contract_id} м{a.month}"
        )


def assert_cash_non_negative(result: PlanningResult) -> None:
    for b in result.contract_balances:
        assert b.closing_balance >= -0.01, f"{b.contract_id} м{b.month}: отрицательный остаток"


def assert_forward_cashflow_only(result: PlanningResult) -> None:
    for b in result.contract_balances:
        assert b.carried_forward >= -0.01


def simulate_forward_cashflow(
    scenario: DemoScenario, allocations: list[AllocationRecord], contract_id: str
) -> dict[int, dict[str, float]]:
    c = scenario.contract(contract_id)
    opening = 0.0
    trace: dict[int, dict[str, float]] = {}
    for m in range(1, 13):
        inflow = c.inflow(m) if m in c.active_months(scenario.year) else 0.0
        spent = _sum_kind(allocations, month=m, contract_id=contract_id)
        closing = opening + inflow - spent
        trace[m] = {
            "inflow": inflow,
            "spent": spent,
            "opening": opening,
            "closing": closing,
        }
        opening = closing if m in c.active_months(scenario.year) else 0.0
    return trace


def assert_contract_cashflow(
    scenario: DemoScenario,
    result: PlanningResult,
    contract_id: str,
    active_months: tuple[int, ...],
) -> None:
    trace = simulate_forward_cashflow(scenario, result.allocations, contract_id)
    for m in active_months:
        assert trace[m]["closing"] >= -0.01, f"{contract_id} м{m}"
    for m in range(1, 13):
        if m not in active_months:
            spent = trace[m]["spent"]
            assert spent == pytest.approx(0, abs=0.01), f"{contract_id} м{m}: траты вне периода"


def assert_labor_row(
    labor_df: pd.DataFrame,
    *,
    contract_id: str,
    position: str,
    plan_pm: float,
    plan_amount: float,
    avg: float,
    expect_done: bool = True,
    check_pm: bool = True,
) -> None:
    pos_col = "должность / категория" if "должность / категория" in labor_df.columns else "должность"
    plan_amt_col = "плановая сумма" if "плановая сумма" in labor_df.columns else "плановая сумма по строке"
    fact_amt_col = "фактическая сумма" if "фактическая сумма" in labor_df.columns else "факт сумма по строке"
    row = labor_df[
        (labor_df["договор"] == contract_id)
        & (labor_df[pos_col] == position)
        & (~labor_df[pos_col].astype(str).str.contains("итого по группе", na=False))
    ]
    assert len(row) == 1, f"строка {contract_id}/{position} не найдена"
    r = row.iloc[0]
    pm_tol = max(0.05, 0.05 * plan_pm) + 0.01
    amount_tol = math.ceil(0.05 * plan_amount) + 100.0
    avg_tol = max(1000.0, 0.05 * avg) if avg else 1000.0
    if check_pm:
        assert r["план чел.-мес."] == pytest.approx(plan_pm, abs=0.05)
        assert r["факт чел.-мес."] == pytest.approx(plan_pm, abs=pm_tol)
    assert r[plan_amt_col] == pytest.approx(plan_amount, abs=1.0)
    if expect_done:
        fact_amount = float(r[fact_amt_col])
        assert abs(fact_amount - plan_amount) <= amount_tol + 1.0
        plan_avg_col = "плановая средняя" if "плановая средняя" in labor_df.columns else None
        if plan_avg_col and r[plan_avg_col] != "":
            assert r[plan_avg_col] == pytest.approx(avg, abs=avg_tol)
        if r["фактическая средняя"] != "":
            assert r["фактическая средняя"] == pytest.approx(avg, rel=0, abs=avg_tol + 5000)
    else:
        assert r[fact_amt_col] <= plan_amount + 0.01


def assert_position_compatibility(
    scenario: DemoScenario, position_df: pd.DataFrame
) -> None:
    id_col = "табельный номер" if "табельный номер" in position_df.columns else "сотрудник"
    for emp in scenario.employees:
        sub = position_df[position_df[id_col] == emp.id]
        if sub.empty:
            sub = position_df[position_df["сотрудник"] == emp.full_name]
        assert not sub.empty, f"нет контроля должностей для {emp.id}"
        assert (sub["совместимость"] == "да").all(), sub[sub["совместимость"] != "да"]
    bad_pairs = []
    for _, r in position_df.iterrows():
        eg = r["группа сотрудника"]
        cg = r["группа по договору"]
        if eg == ENGINEER_GROUP and LAB_GROUP in str(cg):
            bad_pairs.append(r)
        if eg == LAB_GROUP and ENGINEER_GROUP in str(cg):
            bad_pairs.append(r)
    assert not bad_pairs, "несовместимые группы в выплатах"


def assert_admin_complexity(scenario: DemoScenario, admin_df: pd.DataFrame) -> None:
    contracts_col = "список договоров" if "список договоров" in admin_df.columns else "договоры"
    for emp in scenario.employees:
        row = admin_df[admin_df["табельный номер"] == emp.id]
        assert len(row) == 1
        contracts = set(str(row.iloc[0][contracts_col]).split("; "))
        contracts.discard("—")
        assert scenario.anchor_contract_id() in contracts
        if emp.group == ENGINEER_GROUP:
            assert PROJECT_CONTRACT in contracts
        if emp.group == LAB_GROUP:
            assert LAB_CONTRACT in contracts


def assert_uniform_spend_in_period(
    result: PlanningResult,
    contract_id: str,
    months: tuple[int, ...],
    monthly_spend: float,
) -> None:
    spent = {
        b.month: b.spent
        for b in result.contract_balances
        if b.contract_id == contract_id
    }
    for m in months:
        assert spent.get(m, 0.0) == pytest.approx(monthly_spend, abs=1.0)


def assert_no_payment_without_pm(result: PlanningResult) -> None:
    pm_by: dict[tuple[str, str, int, str], float] = defaultdict(float)
    for rec in result.labor_pm_attributions:
        key = (rec.employee_id, rec.contract_id, rec.month, rec.labor_row_id)
        pm_by[key] += rec.person_months
    for rec in result.labor_payment_attributions:
        if rec.amount <= 0.005:
            continue
        key = (rec.employee_id, rec.contract_id, rec.month, rec.labor_row_id)
        assert pm_by.get(key, 0.0) > 0.005, (
            f"выплата без трудоёмкости в {key}: {rec.amount}"
        )


def assert_success_scenario(
    scenario: DemoScenario, result: PlanningResult, out_path, ctx=None
) -> None:
    assert_solver_success(result)
    assert_no_deficits(result)
    assert_salary_mandatory(scenario, result)
    assert_preferred_anchor_salary(scenario, result)
    assert_allowance_routing(scenario, result)
    assert_no_flex_tails(result)
    assert_cash_non_negative(result)
    assert_forward_cashflow_only(result)
    assert_no_payment_without_pm(result)

    assert_contract_cashflow(
        scenario, result, PROJECT_CONTRACT, scenario.project_months()
    )
    assert_contract_cashflow(scenario, result, LAB_CONTRACT, scenario.lab_months())

    xl = pd.ExcelFile(out_path)
    assert "Контроль трудоёмкости" in xl.sheet_names
    assert "Трудоёмкость по строкам" not in xl.sheet_names
    from fot_planner.excel.reports.user_report import build_labor_summary_block

    labor_df = build_labor_summary_block(ctx, result)
    labor_df = labor_df[labor_df["план чел.-мес."].notna()].copy()
    labor_df = labor_df[
        ~labor_df["должность / категория"].astype(str).str.contains("итого по группе", na=False)
    ]
    for row in scenario.contract(ANCHOR_CONTRACT).labor:
        assert_labor_row(
            labor_df,
            contract_id=ANCHOR_CONTRACT,
            position=row.position,
            plan_pm=row.person_months,
            plan_amount=row.plan_amount,
            avg=row.avg_monthly_cost,
        )
    project_labor = scenario.contract(PROJECT_CONTRACT).labor[0]
    lab_labor = scenario.contract(LAB_CONTRACT).labor[0]
    assert_labor_row(
        labor_df,
        contract_id=PROJECT_CONTRACT,
        position=project_labor.position,
        plan_pm=project_labor.person_months,
        plan_amount=project_labor.plan_amount,
        avg=project_labor.avg_monthly_cost,
    )
    assert_labor_row(
        labor_df,
        contract_id=LAB_CONTRACT,
        position=lab_labor.position,
        plan_pm=lab_labor.person_months,
        plan_amount=lab_labor.plan_amount,
        avg=lab_labor.avg_monthly_cost,
    )

    position_df = pd.read_excel(xl, "Контроль должностей")
    assert_position_compatibility(scenario, position_df)

    base_plan_pm = sum(r.person_months for r in scenario.contract(ANCHOR_CONTRACT).labor)
    dec_cum = sum(
        rec.person_months
        for rec in result.labor_pm_attributions
        if rec.contract_id == ANCHOR_CONTRACT
    )
    pm_cum_tol = max(0.05, 0.05 * base_plan_pm) + 0.01
    assert dec_cum == pytest.approx(base_plan_pm, abs=pm_cum_tol)
    engineer_allow = scenario.monthly_engineer_allowance_total()
    assert_uniform_spend_in_period(
        result, PROJECT_CONTRACT, scenario.project_months(), engineer_allow
    )
    lab_emp = next(e for e in scenario.employees if e.group == LAB_GROUP)
    assert_uniform_spend_in_period(
        result, LAB_CONTRACT, scenario.lab_months(), lab_emp.allowance
    )

    if "Административная сложность" in xl.sheet_names:
        admin_df = pd.read_excel(xl, "Административная сложность")
        emp_ids = {e.id for e in scenario.employees}
        admin_df = admin_df[admin_df["табельный номер"].isin(emp_ids)]
        assert_admin_complexity(scenario, admin_df)


def assert_deficit_scenario(
    scenario: DemoScenario, result: PlanningResult, out_path, ctx=None
) -> None:
    assert_solver_success(result)
    assert_salary_mandatory(scenario, result)
    assert_no_flex_tails(result)

    total_def = sum(d.amount for d in result.deficits)
    expected_short = scenario.expected_labor_shortfall()
    assert total_def == pytest.approx(expected_short, rel=0, abs=100.0)

    early = sum(d.amount for d in result.deficits if d.month <= 8)
    assert early == pytest.approx(0, abs=1.0), "ранний дефицит недопустим"

    first_months = [d.month for d in result.deficits if d.amount > 0.5]
    assert first_months, "дефицит должен быть"
    assert min(first_months) >= 9, f"дефицит слишком рано: {first_months}"

    xl = pd.ExcelFile(out_path)
    assert "Контроль трудоёмкости" in xl.sheet_names
    from fot_planner.excel.reports.user_report import build_labor_summary_block

    labor_df = build_labor_summary_block(ctx, result)
    labor_df = labor_df[labor_df["план чел.-мес."].notna()].copy()
    labor_df = labor_df[
        ~labor_df["должность / категория"].astype(str).str.contains("итого по группе", na=False)
    ]
    project_labor = scenario.contract(PROJECT_CONTRACT).labor[0]
    assert_labor_row(
        labor_df,
        contract_id=PROJECT_CONTRACT,
        position=project_labor.position,
        plan_pm=project_labor.person_months,
        plan_amount=project_labor.plan_amount,
        avg=project_labor.avg_monthly_cost,
        expect_done=False,
        check_pm=False,
    )
    pos_col = "должность / категория" if "должность / категория" in labor_df.columns else "должность"
    amt_dev_col = "отклонение суммы" if "отклонение суммы" in labor_df.columns else "отклонение суммы"
    row = labor_df[
        (labor_df["договор"] == PROJECT_CONTRACT)
        & (labor_df[pos_col] == project_labor.position)
    ].iloc[0]
    assert row["отклонение суммы"] == pytest.approx(-expected_short, abs=100.0)


def assert_no_backward_money_scenario(
    scenario: DemoScenario, result: PlanningResult
) -> None:
    """Деньги проекта поступают поздно — нельзя тратить их в более ранних месяцах."""
    assert_forward_cashflow_only(result)
    assert_cash_non_negative(result)

    project = scenario.contract(PROJECT_CONTRACT)
    inflow_month = max(project.inflows.keys())
    for m in range(1, inflow_month):
        bal = next(
            b for b in result.contract_balances
            if b.contract_id == PROJECT_CONTRACT and b.month == m
        )
        assert bal.inflow == pytest.approx(0, abs=0.01), f"месяц {m}: неожиданное поступление"
        assert bal.spent <= bal.opening_balance + bal.inflow + 0.01, (
            f"месяц {m}: траты без доступной кассы"
        )

    project_spent_early = sum(
        _sum_kind(result.allocations, month=m, contract_id=PROJECT_CONTRACT)
        for m in range(1, inflow_month)
    )
    assert project_spent_early == pytest.approx(0, abs=0.01), (
        "до поступления денег нельзя тратить с проектного договора"
    )

    sep_spent = next(
        b.spent
        for b in result.contract_balances
        if b.contract_id == PROJECT_CONTRACT and b.month == inflow_month
    )
    assert sep_spent == pytest.approx(project.total_fot, abs=1.0)
