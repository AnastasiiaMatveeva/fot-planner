"""Разделение зарплаты: доля оклада и доля надбавки."""

from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
import pytest

from fot_planner.excel import SHEET_CONTRACT_LABOR, create_template
from fot_planner.planner import run_planning
from fot_planner.models import Contract, Employee
from fot_planner.payment_split import (
    employee_monthly_payment_due,
    flex_remainder_after_salary,
    max_salary_amount_if_contract_used,
    salary_position_options,
)

_ORG_CAP_ENGINEER = 40_400.0


def _employee(
    rate: float = 1.0,
    wage: float = 150_000,
    *,
    org_cap_for_rate: Optional[float] = _ORG_CAP_ENGINEER,
) -> Employee:
    return Employee(
        id="E1",
        full_name="Test",
        position="инженер",
        department="lab",
        rate=rate,
        monthly_wage=wage,
        reference_salary_for_rate=org_cap_for_rate,
    )


def test_employee_monthly_payment_due_equals_wage():
    assert employee_monthly_payment_due(_employee(wage=120_000)) == 120_000


def _contract_with_cap(cap: Optional[float], *, org_cap_for_rate: Optional[float] = _ORG_CAP_ENGINEER) -> Contract:
    from fot_planner.models import ContractPositionRule

    rules = []
    if cap is not None:
        rules.append(
            ContractPositionRule(
                contract_id="C1",
                position="инженер",
                equivalence_group="eng",
                max_monthly_payment=cap,
                reference_salary_for_rate=org_cap_for_rate,
            )
        )
    return Contract(
        id="C1",
        name="Test",
        number="1",
        contract_type="grant",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        total_fot=2_000_000,
        position_rules=rules,
    )


def test_salary_position_options_apply_min_of_org_and_contract():
    e = _employee(wage=200_000, org_cap_for_rate=999_999)  # employee field must not matter
    c = _contract_with_cap(100_000, org_cap_for_rate=_ORG_CAP_ENGINEER)
    opts = salary_position_options(c, e)
    assert len(opts) == 1
    assert opts[0].contract_cap == 100_000
    assert opts[0].organization_cap == _ORG_CAP_ENGINEER
    assert opts[0].final_cap == _ORG_CAP_ENGINEER
    assert max_salary_amount_if_contract_used(c, e) == _ORG_CAP_ENGINEER


def test_salary_amount_limited_by_contract_when_org_higher():
    e = _employee(wage=150_000, org_cap_for_rate=200_000)
    c = _contract_with_cap(100_000, org_cap_for_rate=200_000)
    assert max_salary_amount_if_contract_used(c, e) == 100_000


def test_salary_amount_full_wage_when_below_both_caps():
    e = _employee(wage=80_000, org_cap_for_rate=200_000)
    c = _contract_with_cap(100_000, org_cap_for_rate=200_000)
    assert max_salary_amount_if_contract_used(c, e) == 80_000


def test_salary_cap_without_contract_positions_is_zero():
    e = _employee(wage=120_000)
    c = _contract_with_cap(None)
    assert max_salary_amount_if_contract_used(c, e) == 0.0


def _workbook_with_cap(path: Path, *, wage: float, cap: float) -> None:
    create_template(path)
    year = date.today().year
    settings = pd.read_excel(path, sheet_name="settings")
    # Этот helper проверяет разбиение оклад/надбавка, а не норматив БЭП.
    settings["средняя зарплата ГОЗ"] = max(112_261, wage)
    employees = pd.DataFrame(
        [
            {
                "id": "E001",
                "full_name": "Иванов",
                "position": "инженер",
                "department": "лаб",
                "rate": 1.0,
                "monthly_wage": wage,
                "start_date": f"{year}-01-01",
                "end_date": "",
            }
        ]
    )
    contracts = pd.DataFrame(
        [
            {
                "id": "C001",
                "name": "ГОЗ",
                "contract_type": "goszakaz",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "total_fot": wage * 12,
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": False,
            }
        ]
    )
    fot_matrix = pd.DataFrame({"contract_id": ["C001"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [wage]
    positions = pd.DataFrame(
        [{"contract_id": "C001", "position": "инженер", "max_monthly_payment": cap}]
    )
    labor = pd.DataFrame(
        [
            {
                "contract_id": "C001",
                "year": year,
                "person_months": 12,
                "position": "инженер",
                # Средняя ≈ месячная зарплата, иначе модель с трудоёмкостью часто INFEASIBLE.
                "avg_monthly_labor_cost": wage,
            }
        ]
    )
    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        positions.to_excel(w, sheet_name="contract_positions", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)
        labor.to_excel(w, sheet_name=SHEET_CONTRACT_LABOR, index=False)
        settings.to_excel(w, sheet_name="settings", index=False)


def test_contract_cap_is_max_on_contract_not_by_employee_position():
    """Потолок договора — max по строкам, без фильтра по должности сотрудника."""
    from fot_planner.models import ContractPositionRule

    e = Employee(
        id="E2",
        full_name="Petrov",
        position="инженер",
        department="RND",
        rate=1.0,
        monthly_wage=100_000,
        equivalence_group="инженеры",
        reference_salary_for_rate=200_000,
    )
    c = Contract(
        id="C_BASE",
        name="Base",
        number="1",
        contract_type="grant",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        total_fot=5_000_000,
        position_rules=[
            ContractPositionRule(
                contract_id="C_BASE",
                position="инженер-программист",
                equivalence_group="инженеры",
                max_monthly_payment=100_000,
                reference_salary_for_rate=90_000,
            ),
            ContractPositionRule(
                contract_id="C_BASE",
                position="инженер",
                equivalence_group="инженеры",
                max_monthly_payment=80_000,
                reference_salary_for_rate=130_000,
            ),
        ],
    )
    # Итоговые варианты: min(org, contract) по каждой строке.
    opts = salary_position_options(c, e)
    assert sorted(o.final_cap for o in opts) == [80_000, 90_000]
    # Верхняя граница до выбора позиции — максимум по вариантам, но не выше полной зарплаты.
    assert max_salary_amount_if_contract_used(c, e) == 90_000


def test_salary_and_allowance_split_in_plan(tmp_path: Path):
    """Оклад = min(орг. справочник, договор, зарплата), надбавка = остаток."""
    inp = tmp_path / "in.xlsx"
    out = tmp_path / "out.xlsx"
    _workbook_with_cap(inp, wage=150_000, cap=100_000)

    result = run_planning(inp, out, time_limit_sec=90)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    jan = [a for a in result.allocations if a.employee_id == "E001" and a.month == 1]
    salary = sum(a.amount for a in jan if a.payment_kind == "salary")
    allowance = sum(a.amount for a in jan if a.payment_kind == "allowance")
    assert salary == pytest.approx(_ORG_CAP_ENGINEER, abs=1)
    assert allowance == pytest.approx(150_000 - _ORG_CAP_ENGINEER, abs=1)
    assert salary + allowance == pytest.approx(150_000, abs=1)
