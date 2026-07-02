from __future__ import annotations

from fot_planner.models import Employee, PlanningContext
from fot_planner.validation import validate_context


def _employee(
    employee_id: str,
    position: str,
    category: str,
) -> Employee:
    return Employee(
        id=employee_id,
        full_name=employee_id,
        position=position,
        department="",
        rate=0.5,
        monthly_wage=10_000,
        employment_category=category,
        reference_salary_for_rate=10_000,
        equivalence_group="test",
    )


def test_validate_student_position_does_not_block_calculation():
    ctx = PlanningContext(
        year=2026,
        employees=[_employee("E001", "инженер", "student")],
        contracts=[],
    )

    conflicts = validate_context(ctx)

    assert conflicts == []


def test_validate_student_input_rate_is_trusted():
    employee = _employee("E001", "аналитик", "student")
    employee.rate = 1.0
    ctx = PlanningContext(year=2026, employees=[employee], contracts=[])

    assert validate_context(ctx) == []


def test_validate_graduate_student_position_does_not_block_calculation():
    ctx = PlanningContext(
        year=2026,
        employees=[_employee("E001", "лаборант", "graduate_student")],
        contracts=[],
    )

    conflicts = validate_context(ctx)

    assert conflicts == []


def test_validate_allowed_category_positions():
    ctx = PlanningContext(
        year=2026,
        employees=[
            _employee("E001", "лаборант", "student"),
            _employee("E002", "техник", "student"),
            _employee("E003", "инженер", "graduate_student"),
        ],
        contracts=[],
    )

    assert validate_context(ctx) == []


def test_validate_main_rate_cannot_exceed_one():
    employee = _employee("E001", "инженер", "regular")
    employee.rate = 1.5

    conflicts = validate_context(
        PlanningContext(year=2026, employees=[employee], contracts=[])
    )

    assert [c.code for c in conflicts] == ["INVALID_EMPLOYMENT_RATE"]


def test_validate_part_time_rate_cannot_exceed_half():
    employee = _employee("E001", "инженер", "regular")
    employee.rate = 0.75
    employee.employment_type = "part_time"

    conflicts = validate_context(
        PlanningContext(year=2026, employees=[employee], contracts=[])
    )

    assert [c.code for c in conflicts] == ["INVALID_EMPLOYMENT_RATE"]
