from __future__ import annotations

from datetime import date

import pandas as pd

from fot_planner.excel import create_template, load_context
from fot_planner.models import (
    Contract,
    ContractLaborPlan,
    PlanningContext,
    SalaryStabilityRules,
)
from fot_planner.validation import validate_context


def test_goz_average_salary_limit_is_editable_in_settings(tmp_path):
    path = tmp_path / "input.xlsx"
    create_template(path)

    contracts = pd.read_excel(path, sheet_name="contracts")
    assert "ГОЗ / оборонный заказ" in contracts.columns
    assert load_context(path).contracts[0].is_goz_defense_order is True

    settings = pd.read_excel(path, sheet_name="settings")
    assert settings.loc[0, "средняя зарплата ГОЗ"] == 112_261

    settings.loc[0, "средняя зарплата ГОЗ"] = 123_456
    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
        settings.to_excel(writer, sheet_name="settings", index=False)

    assert load_context(path).salary_stability.goz_average_salary_limit == 123_456


def _goz_context(total_fot: float) -> PlanningContext:
    year = date.today().year
    contract = Contract(
        id="GOZ-1",
        name="ГОЗ",
        number="1",
        contract_type="goszakaz",
        start_date=date(year, 1, 1),
        end_date=date(year, 12, 31),
        total_fot=total_fot,
        is_goz_defense_order=True,
    )
    labor = ContractLaborPlan(
        contract_id=contract.id,
        year=year,
        person_months=12,
        avg_monthly_labor_cost=100_000,
    )
    return PlanningContext(
        year=year,
        employees=[],
        contracts=[contract],
        labor_plans=[labor],
        salary_stability=SalaryStabilityRules(goz_average_salary_limit=112_261),
    )


def test_goz_fot_cannot_exceed_bep_average_times_person_months():
    ctx = _goz_context(total_fot=1_400_000)
    issues = validate_context(ctx)

    issue = next(x for x in issues if x.code == "GOZ_FOT_ABOVE_BEP_LIMIT")
    assert "1347132.00" in issue.message


def test_goz_fot_below_bep_limit_is_allowed():
    ctx = _goz_context(total_fot=1_300_000)
    issues = validate_context(ctx)
    assert not any(x.code == "GOZ_FOT_ABOVE_BEP_LIMIT" for x in issues)


def test_regular_goszakaz_without_defense_flag_is_not_limited_by_bep():
    ctx = _goz_context(total_fot=1_400_000)
    ctx.contracts[0].is_goz_defense_order = False

    issues = validate_context(ctx)

    assert not any(x.code == "GOZ_FOT_ABOVE_BEP_LIMIT" for x in issues)
