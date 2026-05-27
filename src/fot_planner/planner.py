"""Оркестрация: валидация → оптимизация → экспорт."""

from __future__ import annotations

from pathlib import Path

from fot_planner.excel_io import export_result, load_context
from fot_planner.models import ManualAssignment, PlanningResult
from fot_planner.optimizer import solve
from fot_planner.models import ConflictRecord
from fot_planner.validation import validate_context


def _blocking_validation(conflicts: list[ConflictRecord]) -> list[ConflictRecord]:
    """Только ошибки останавливают расчёт; коды *_WARNING — предупреждения."""
    return [c for c in conflicts if not c.code.endswith("_WARNING")]


def run_planning(
    input_path: str | Path,
    output_path: str | Path,
    plan_override_path: str | Path | None = None,
    time_limit_sec: int = 120,
) -> PlanningResult:
    override = plan_override_path or output_path
    ctx = load_context(input_path, plan_path=override)
    ctx = _merge_locked_plan(ctx)

    validation_issues = validate_context(ctx)
    blocking = _blocking_validation(validation_issues)
    if blocking:
        result = PlanningResult(
            year=ctx.year,
            allocations=[],
            deficits=[],
            conflicts=validation_issues,
            contract_balances=[],
            solver_status="VALIDATION_FAILED",
            objective_value=0.0,
            solve_time_sec=0.0,
        )
        export_result(output_path, ctx, result)
        return result

    result = solve(ctx, time_limit_sec=time_limit_sec)
    if validation_issues:
        result.conflicts = validation_issues + result.conflicts
    export_result(output_path, ctx, result)
    return result


def _merge_locked_plan(ctx):
    locked = ctx.baseline_plan or []
    if not locked:
        return ctx

    new_assignments = list(ctx.manual_assignments)
    seen = {
        (a.employee_id, a.contract_id, a.month_from, a.month_to, a.payment_kind)
        for a in new_assignments
    }
    for rec in locked:
        key = (rec.employee_id, rec.contract_id, rec.month, rec.month, rec.payment_kind)
        if key in seen:
            continue
        new_assignments.append(
            ManualAssignment(
                employee_id=rec.employee_id,
                contract_id=rec.contract_id,
                year=rec.year,
                month_from=rec.month,
                month_to=rec.month,
                payment_kind=rec.payment_kind,
                fixed_amount=rec.amount,
            )
        )
        seen.add(key)

    ctx.manual_assignments = new_assignments
    return ctx
