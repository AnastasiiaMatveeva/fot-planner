from __future__ import annotations

import pyomo.environ as pyo

from fot_planner.models import Employee, PaymentKind, PlanningContext
from fot_planner.payment_kind import LABOR_PAYMENT_KINDS
from fot_planner.payroll_rules import (
    PayrollRuleContext,
    rule_order_incentive_2556_amount,
    rule_order_incentive_once_per_month,
)
from fot_planner.salary_limits_2556 import PositionLimit


def _rule_context():
    employee = Employee(
        id="E001",
        full_name="Иванов",
        position="Инженер",
        department="",
        rate=1.0,
        monthly_wage=200_000,
        equivalence_group="инженеры",
        reference_salary_for_rate=40_400,
    )
    ctx = PlanningContext(
        year=2026,
        employees=[employee],
        contracts=[],
        position_limit_tables={
            "2556": [
                PositionLimit(
                    limit_code="2556",
                    position="Инженер",
                    personnel_category="НТП",
                    limit=110_000,
                )
            ]
        },
    )
    alloc_keys = [
        ("E001", "C001", 1, PaymentKind.ORDER_INCENTIVE),
        ("E001", "C002", 1, PaymentKind.ORDER_INCENTIVE),
    ]
    model = pyo.ConcreteModel()
    model.alloc = pyo.Var(alloc_keys, domain=pyo.NonNegativeReals)
    model.use = pyo.Var(alloc_keys, domain=pyo.Binary)
    model.cons = pyo.ConstraintList()
    return PayrollRuleContext(
        ctx=ctx,
        model=model,
        alloc=model.alloc,
        uses=model.use,
        alloc_keys=alloc_keys,
        alloc_key_set=set(alloc_keys),
        contracts={},
        employees={"E001": employee},
        months=[1],
        contract_list=["C001", "C002"],
        sum_terms=sum,
        contract_allows_month=lambda _contract, _year, _month: True,
        employee_active_in_month=lambda _employee, _year, _month: True,
    )


def test_order_incentive_has_separate_2556_amount_limit():
    rule_ctx = _rule_context()

    rule_order_incentive_2556_amount(rule_ctx)

    assert len(rule_ctx.model.cons) == 1


def test_order_incentive_is_once_per_employee_month():
    rule_ctx = _rule_context()

    rule_order_incentive_once_per_month(rule_ctx)

    assert len(rule_ctx.model.cons) == 1


def test_order_incentive_does_not_close_labor_amount():
    assert PaymentKind.SALARY in LABOR_PAYMENT_KINDS
    assert PaymentKind.K122 in LABOR_PAYMENT_KINDS
    assert PaymentKind.K152 in LABOR_PAYMENT_KINDS
    assert PaymentKind.ORDER_INCENTIVE not in LABOR_PAYMENT_KINDS
