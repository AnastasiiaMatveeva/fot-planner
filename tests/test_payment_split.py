"""Разделение зарплаты: доля оклада и доля надбавки."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel_io import SHEET_CONTRACT_LABOR, create_template
from fot_planner.planner import run_planning
from fot_planner.models import Contract, Employee
from fot_planner.payment_split import (
    allowance_part,
    salary_cap_on_contract,
    salary_part_on_contract,
)


def _employee(rate: float = 1.0, wage: float = 150_000) -> Employee:
    return Employee(
        id="E1",
        full_name="Test",
        position="инженер",
        department="lab",
        rate=rate,
        monthly_wage=wage,
    )


def _contract_with_cap(cap: float | None) -> Contract:
    from fot_planner.models import ContractPositionRule

    rules = []
    if cap is not None:
        rules.append(
            ContractPositionRule(
                contract_id="C1",
                position="инженер",
                equivalence_group="eng",
                max_monthly_payment=cap,
            )
        )
    return Contract(
        id="C1",
        name="Test",
        number="1",
        contract_type="grant",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        spend_deadline=None,
        total_fot=2_000_000,
        position_rules=rules,
    )


def test_salary_part_uses_cap():
    e = _employee(wage=150_000)
    c = _contract_with_cap(100_000)
    assert salary_part_on_contract(c, e) == 100_000
    assert allowance_part(e, 100_000) == 50_000


def test_salary_part_full_wage_when_below_cap():
    e = _employee(wage=80_000)
    c = _contract_with_cap(100_000)
    assert salary_part_on_contract(c, e) == 80_000
    assert allowance_part(e, 80_000) == 0


def test_salary_cap_none_means_full_wage():
    e = _employee(wage=120_000)
    c = _contract_with_cap(None)
    assert salary_cap_on_contract(c, e) is None
    assert salary_part_on_contract(c, e) == 120_000
    assert allowance_part(e, 120_000) == 0


def _workbook_with_cap(path: Path, *, wage: float, cap: float) -> None:
    create_template(path)
    year = date.today().year
    employees = pd.DataFrame(
        [
            {
                "id": "E001",
                "full_name": "Иванов",
                "position": "инженер",
                "department": "лаб",
                "rate": 1.0,
                "monthly_wage": wage,
                "incentive": 0,
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
                "spend_deadline": "",
                "total_fot": wage * 12,
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": False,
                "allow_monthly_carryover": False,
                "require_salary_reserve": False,
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
                "avg_monthly_labor_cost": 60_000,
            }
        ]
    )
    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        positions.to_excel(w, sheet_name="contract_positions", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)
        labor.to_excel(w, sheet_name=SHEET_CONTRACT_LABOR, index=False)


def test_salary_cap_prefers_exact_position():
    """Потолок оклада берётся по должности сотрудника, а не max по всей группе."""
    from fot_planner.models import ContractPositionRule

    e = Employee(
        id="E2",
        full_name="Petrov",
        position="инженер",
        department="RND",
        rate=1.0,
        monthly_wage=100_000,
        equivalence_group="инженеры",
    )
    c = Contract(
        id="C_BASE",
        name="Base",
        number="1",
        contract_type="grant",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        spend_deadline=None,
        total_fot=5_000_000,
        position_rules=[
            ContractPositionRule(
                contract_id="C_BASE",
                position="инженер-программист",
                equivalence_group="инженеры",
                max_monthly_payment=100_000,
            ),
            ContractPositionRule(
                contract_id="C_BASE",
                position="инженер",
                equivalence_group="инженеры",
                max_monthly_payment=80_000,
            ),
        ],
    )
    assert salary_cap_on_contract(c, e) == 80_000
    assert salary_part_on_contract(c, e) == 80_000


def test_salary_and_allowance_split_in_plan(tmp_path: Path):
    """Оклад = min(зарплата, потолок), надбавка = остаток зарплаты."""
    inp = tmp_path / "in.xlsx"
    out = tmp_path / "out.xlsx"
    _workbook_with_cap(inp, wage=150_000, cap=100_000)

    result = run_planning(inp, out, time_limit_sec=90)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    jan = [a for a in result.allocations if a.employee_id == "E001" and a.month == 1]
    salary = sum(a.amount for a in jan if a.payment_kind == "salary")
    allowance = sum(a.amount for a in jan if a.payment_kind == "allowance")
    assert salary == pytest.approx(100_000, abs=1)
    assert allowance == pytest.approx(50_000, abs=1)
    assert salary + allowance == pytest.approx(150_000, abs=1)
