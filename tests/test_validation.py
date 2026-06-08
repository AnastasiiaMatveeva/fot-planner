from datetime import date

from fot_planner.models import Contract, ContractLaborPlan, Employee, PlanningContext
from fot_planner.validation import employee_active_in_month, validate_context


def test_employee_active():
    e = Employee(
        id="E1",
        full_name="Test",
        position="инженер",
        department="",
        rate=1.0,
        monthly_wage=100,
        start_date=date(2025, 3, 1),
        end_date=date(2025, 6, 30),
    )
    assert not employee_active_in_month(e, 2025, 1)
    assert employee_active_in_month(e, 2025, 4)
    assert not employee_active_in_month(e, 2025, 7)


def test_validate_unknown_contract():
    ctx = PlanningContext(
        year=2025,
        employees=[
            Employee(
                id="E1",
                full_name="A",
                position="инженер",
                department="",
                rate=1,
                monthly_wage=100,
                allowed_contracts=["C999"],
            )
        ],
        contracts=[],
    )
    errors = validate_context(ctx)
    assert any(e.code == "UNKNOWN_CONTRACT" for e in errors)


def test_fot_below_planned_labor_cost_warning():
    year = 2026
    ctx = PlanningContext(
        year=year,
        employees=[],
        contracts=[
            Contract(
                id="C1",
                name="",
                number="",
                contract_type="grant",
                start_date=date(year, 1, 1),
                end_date=date(year, 12, 31),
                total_fot=900_000,
            )
        ],
        labor_plans=[
            ContractLaborPlan(
                contract_id="C1",
                year=year,
                person_months=10,
                position="инженер",
                avg_monthly_labor_cost=120_000,
            )
        ],
    )
    warnings = [c for c in validate_context(ctx) if c.code == "FOT_BELOW_PLANNED_LABOR_COST_WARNING"]
    assert len(warnings) == 1
    assert "1 200 000" in warnings[0].message or "1200000" in warnings[0].message.replace(" ", "")
