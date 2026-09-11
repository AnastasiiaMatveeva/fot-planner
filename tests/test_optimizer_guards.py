from datetime import date

from fot_planner.models import Contract, Employee, PlanningContext
from fot_planner.optimizer import solve


def test_active_salary_without_allowed_payment_source_is_not_optimal():
    ctx = PlanningContext(
        year=2026,
        employees=[Employee(
            id="ROW-1", person_id="TAB-1", full_name="Иванов",
            position="инженер", department="отдел", rate=1.0,
            monthly_wage=100000,
        )],
        contracts=[Contract(
            id="C-EMPTY", name="", number="", contract_type="grant",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
            total_fot=1200000, allow_salary=False, allow_secret=False,
            allow_allowance=False, allow_incentive=False,
            allow_extra_work=False, allow_order_incentive=False,
        )],
    )
    result = solve(ctx, time_limit_sec=30)
    assert result.solver_status not in ("OPTIMAL", "FEASIBLE")
