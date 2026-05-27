from datetime import date

from fot_planner.models import Employee, PlanningContext
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
