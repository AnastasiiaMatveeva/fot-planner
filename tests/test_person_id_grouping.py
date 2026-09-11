from fot_planner.models import Employee, PlanningContext
from fot_planner.validation import validate_context


def _employee(code, person_id):
    return Employee(
        id=code,
        person_id=person_id,
        full_name="Иванов Иван Иванович",
        position="инженер",
        department="отдел",
        rate=1.0,
        monthly_wage=100000,
        employment_type="main",
    )


def test_same_name_different_person_numbers_are_not_merged():
    errors = validate_context(PlanningContext(
        year=2026,
        employees=[_employee("ROW-1", "TAB-1"), _employee("ROW-2", "TAB-2")],
        contracts=[],
    ))
    assert not any(e.code == "SECOND_MAIN_ROW" for e in errors)


def test_same_person_number_is_grouped_across_rows():
    errors = validate_context(PlanningContext(
        year=2026,
        employees=[_employee("ROW-1", "TAB-1"), _employee("ROW-2", "TAB-1")],
        contracts=[],
    ))
    assert any(e.code == "SECOND_MAIN_ROW" for e in errors)
