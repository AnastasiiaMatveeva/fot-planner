"""Run explicitly with --noconftest while global collection is disabled."""
from dataclasses import replace
from datetime import date
from pathlib import Path
from fot_planner.excel import load_context
from fot_planner.validation import _employment_structure_conflicts


def context():
    ctx = load_context(Path(__file__).resolve().parents[1] / 'harness/crisis_template.xlsx')
    base = replace(ctx.employees[0], full_name='Тестовый Сотрудник', employment_type='main',
                   start_date=date(2026, 1, 1), end_date=date(2026, 3, 31))
    later = replace(base, id='LATER', start_date=date(2026, 4, 1), end_date=date(2026, 12, 31))
    ctx.employees = [base, later]
    return ctx


def test_consecutive_main_appointments_are_valid():
    assert _employment_structure_conflicts(context()) == []


def test_overlapping_main_appointments_are_rejected():
    ctx = context()
    ctx.employees[1] = replace(ctx.employees[1], start_date=date(2026, 3, 1))
    errors = _employment_structure_conflicts(ctx)
    assert len(errors) == 1 and errors[0].code == 'SECOND_MAIN_ROW'
    assert 'месяц 3:' in errors[0].message


def test_part_time_requires_main_in_its_active_months():
    ctx = context()
    ctx.employees[1] = replace(ctx.employees[1], employment_type='part_time')
    errors = _employment_structure_conflicts(ctx)
    assert len(errors) == 9
    assert all(e.code == 'PART_TIME_WITHOUT_MAIN' for e in errors)


def test_consecutive_part_time_assignments_do_not_count_as_three_jobs():
    ctx = context()
    main = replace(ctx.employees[0], end_date=date(2026, 12, 31))
    parts = [replace(main, id=f'P{q}', employment_type='part_time',
                     start_date=date(2026, q * 3 + 1, 1),
                     end_date=date(2026, q * 3 + 3, 28)) for q in range(4)]
    ctx.employees = [main, *parts]
    assert _employment_structure_conflicts(ctx) == []
