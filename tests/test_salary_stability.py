"""Стабильность оклада: минимальный блок 3 месяца и не более N договоров в год."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel import create_template
from fot_planner.planner import run_planning


def _two_contract_workbook(path: Path) -> None:
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
                "monthly_wage": 100000,
                "start_date": f"{year}-01-01",
                "end_date": "",
                "allowed_contracts": "",
                "forbidden_contracts": "",
            }
        ]
    )
    contracts = pd.DataFrame(
        [
            {
                "id": "C001",
                "name": "Договор 1",
                "number": "1",
                "contract_type": "minprom",
                "status": "active",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "total_fot": 2_000_000,
                "priority": 1,
                "allow_salary": True,
                "allow_incentive": True,
                "probability": 1,
                "use_after_end": False,
            },
            {
                "id": "C002",
                "name": "Договор 2",
                "number": "2",
                "contract_type": "minprom",
                "status": "active",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "total_fot": 2_000_000,
                "priority": 2,
                "allow_salary": True,
                "allow_incentive": True,
                "probability": 1,
                "use_after_end": False,
            },
        ]
    )
    positions = pd.DataFrame(
        [
            {"contract_id": "C001", "position": "инженер", "max_positions": 2, "max_monthly_payment": 150000},
            {"contract_id": "C002", "position": "инженер", "max_positions": 2, "max_monthly_payment": 150000},
        ]
    )
    budget = pd.DataFrame(
        [
            {
                "contract_id": cid,
                "year": year,
                "month": m,
                "inflow_amount": 50_000,
            }
            for cid in ("C001", "C002")
            for m in range(1, 13)
        ]
    )
    settings = pd.DataFrame(
        [
            {
                "year": year,
                "weight_deficit_amount": 1_000_000,
                "max_salary_contracts_per_year": 2,
                "weight_salary_switch": 500_000,
                "weight_admin_complexity": 200_000,
            }
        ]
    )

    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        positions.to_excel(w, sheet_name="contract_positions", index=False)
        budget.to_excel(w, sheet_name="contract_monthly_budget", index=False)
        settings.to_excel(w, sheet_name="settings", index=False)


def _ensure_adequate_fot(path: Path, monthly_wage: float = 100_000) -> None:
    """Достаточно кассы и лимит ФОТ согласован с полным освоением (requires_full_fot_spend)."""
    contracts = pd.read_excel(path, sheet_name="contracts")
    contract_ids = contracts["id"].tolist()
    n = max(len(contract_ids), 1)
    annual_wage = monthly_wage * 12
    per_contract_fot = annual_wage / n
    monthly_inflow = max(monthly_wage * 1.5, monthly_wage + 50_000)
    contracts["total_fot"] = per_contract_fot
    fot_matrix = pd.DataFrame({"contract_id": contract_ids})
    for m in range(1, 13):
        fot_matrix[str(m)] = [monthly_inflow] * len(contract_ids)
    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        contracts.to_excel(w, sheet_name="contracts", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)


def _set_contract_fot_and_inflow(
    path: Path,
    fot_by_contract: dict[str, float],
    monthly_inflow: float | None = None,
) -> None:
    """Задать total_fot и равномерные поступления (для requires_full_fot_spend)."""
    contracts = pd.read_excel(path, sheet_name="contracts")
    contract_ids = contracts["id"].tolist()
    for cid, fot in fot_by_contract.items():
        contracts.loc[contracts["id"] == cid, "total_fot"] = fot
    fot_matrix = pd.DataFrame({"contract_id": contract_ids})
    for m in range(1, 13):
        row = []
        for cid in contract_ids:
            total = float(fot_by_contract.get(cid, contracts.loc[contracts["id"] == cid, "total_fot"].iloc[0]))
            row.append(monthly_inflow if monthly_inflow is not None else total / 12)
        fot_matrix[str(m)] = row
    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        contracts.to_excel(w, sheet_name="contracts", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)


def _salary_contracts_per_month(result, employee_id: str) -> dict[int, set[str]]:
    by_month: dict[int, set[str]] = {}
    for a in result.allocations:
        if a.employee_id != employee_id or a.payment_kind != "salary" or a.amount < 0.01:
            continue
        by_month.setdefault(a.month, set()).add(a.contract_id)
    return by_month


def _quarter_switches(by_month: dict[int, set[str]], quarter: tuple[int, ...]) -> int:
    """Число смен договора оклада между соседними месяцами внутри квартала."""
    switches = 0
    q = list(quarter)
    for i in range(1, len(q)):
        prev_c = frozenset(by_month.get(q[i - 1], set()))
        cur_c = frozenset(by_month.get(q[i], set()))
        if prev_c and cur_c and prev_c != cur_c:
            switches += 1
    return switches


def _salary_change_months(by_month: dict[int, set[str]]) -> list[int]:
    """Месяцы (2–12), в которых договор оклада сменился относительно прошлого месяца."""
    changes: list[int] = []
    for m in range(2, 13):
        prev_c = frozenset(by_month.get(m - 1, set()))
        cur_c = frozenset(by_month.get(m, set()))
        if prev_c and cur_c and prev_c != cur_c:
            changes.append(m)
    return changes


def _assert_min_salary_block(by_month: dict[int, set[str]], min_block: int = 3) -> None:
    """Не более одной смены оклада в любом окне из min_block месяцев подряд."""
    changes = _salary_change_months(by_month)
    for start_m in range(2, 13 - min_block + 1):
        window = set(range(start_m, start_m + min_block))
        assert sum(1 for c in changes if c in window) <= 1


def test_at_most_max_salary_contracts_per_year(tmp_path: Path):
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    _two_contract_workbook(inp)
    year = date.today().year
    contracts = pd.read_excel(inp, sheet_name="contracts")
    contracts["total_fot"] = 600_000
    fot_matrix = pd.DataFrame({"contract_id": ["C001", "C002"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [100_000, 100_000]
    with pd.ExcelWriter(inp, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        contracts.to_excel(w, sheet_name="contracts", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)

    result = run_planning(inp, out, time_limit_sec=90)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    contracts_used: set[str] = set()
    for a in result.allocations:
        if a.employee_id != "E001" or a.payment_kind != "salary" or a.amount < 0.01:
            continue
        contracts_used.add(a.contract_id)
    assert len(contracts_used) <= 2


def test_manual_salary_sequence_grant_then_offbudget(tmp_path: Path):
    """Сотрудник 001: G01 (Q1), V03 (Q2), смена на границе Q2→Q3 при необходимости."""
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    year = date.today().year
    _two_contract_workbook(inp)

    contracts = pd.DataFrame(
        [
            {
                "id": "G01",
                "name": "Грант 1",
                "number": "1",
                "contract_type": "grant",
                "status": "active",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "total_fot": 600_000,
                "priority": 1,
                "allow_salary": True,
                "allow_incentive": True,
                "probability": 1,
                "use_after_end": False,
            },
            {
                "id": "V03",
                "name": "Внебюджет 3",
                "number": "2",
                "contract_type": "off_budget",
                "status": "active",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "total_fot": 600_000,
                "priority": 2,
                "allow_salary": True,
                "allow_incentive": True,
                "probability": 1,
                "use_after_end": False,
            },
            {
                "id": "V10",
                "name": "Внебюджет 10",
                "number": "3",
                "contract_type": "off_budget",
                "status": "active",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
                "total_fot": 600_000,
                "priority": 3,
                "allow_salary": True,
                "allow_incentive": True,
                "probability": 1,
                "use_after_end": False,
            },
        ]
    )
    fot_matrix = pd.DataFrame({"contract_id": ["G01", "V03", "V10"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [200_000, 200_000, 200_000]
    positions = pd.DataFrame(
        [
            {"contract_id": cid, "position": "инженер", "max_positions": 2, "max_monthly_payment": 150_000}
            for cid in ("G01", "V03", "V10")
        ]
    )
    manual = pd.DataFrame(
        [
            {"employee_id": "E001", "contract_id": "G01", "year": year, "month_from": 1, "month_to": 3, "payment_kind": "salary", "fixed_amount": ""},
            {"employee_id": "E001", "contract_id": "V03", "year": year, "month_from": 4, "month_to": 6, "payment_kind": "salary", "fixed_amount": ""},
            {"employee_id": "E001", "contract_id": "V10", "year": year, "month_from": 7, "month_to": 9, "payment_kind": "salary", "fixed_amount": ""},
        ]
    )
    settings = pd.DataFrame(
        [
            {
                "year": year,
                "weight_deficit_amount": 1_000_000,
                "max_salary_contracts_per_year": 3,
                "weight_salary_switch": 500_000,
            }
        ]
    )

    employees = pd.read_excel(inp, sheet_name="employees")
    employees.loc[0, "monthly_wage"] = float(employees.loc[0, "monthly_wage"]) + 50_000

    with pd.ExcelWriter(inp, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        employees.to_excel(w, sheet_name="employees", index=False)
        contracts.to_excel(w, sheet_name="contracts", index=False)
        positions.to_excel(w, sheet_name="contract_positions", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)
        manual.to_excel(w, sheet_name="manual_assignments", index=False)
        settings.to_excel(w, sheet_name="settings", index=False)

    result = run_planning(inp, out, time_limit_sec=120)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    by_month = _salary_contracts_per_month(result, "E001")
    assert "G01" in by_month.get(1, set())
    assert "G01" in by_month.get(3, set())
    assert "V03" in by_month.get(4, set())
    assert "V03" in by_month.get(5, set())
    assert "V03" in by_month.get(6, set())
    assert "V10" in by_month.get(7, set())
    assert "V10" in by_month.get(8, set())
    assert "V10" in by_month.get(9, set())
    assert _quarter_switches(by_month, (4, 5, 6)) == 0
    assert _quarter_switches(by_month, (7, 8, 9)) == 0


def test_at_most_two_contracts_per_year(tmp_path: Path):
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    _two_contract_workbook(inp)

    result = run_planning(inp, out, time_limit_sec=90)
    all_contracts = set()
    for contracts in _salary_contracts_per_month(result, "E001").values():
        all_contracts |= contracts
    assert len(all_contracts) <= 2


def test_salary_block_at_least_three_months(tmp_path: Path):
    """Любой результат соблюдает правило: не более одной смены оклада в окне 3 месяцев."""
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    _two_contract_workbook(inp)
    _ensure_adequate_fot(inp)

    result = run_planning(inp, out, time_limit_sec=90)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")
    _assert_min_salary_block(_salary_contracts_per_month(result, "E001"))


def test_too_frequent_manual_salary_switches_feasible_with_multi_salary(tmp_path: Path):
    """При нескольких окладах ручные назначения на разные договоры могут пересекаться — план выполним."""
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    year = date.today().year
    _two_contract_workbook(inp)
    _ensure_adequate_fot(inp)

    manual = pd.DataFrame(
        [
            {
                "employee_id": "E001",
                "contract_id": "C001",
                "year": year,
                "month_from": 1,
                "month_to": 3,
                "payment_kind": "salary",
            },
            {
                "employee_id": "E001",
                "contract_id": "C002",
                "year": year,
                "month_from": 4,
                "month_to": 5,
                "payment_kind": "salary",
            },
            {
                "employee_id": "E001",
                "contract_id": "C001",
                "year": year,
                "month_from": 6,
                "month_to": 12,
                "payment_kind": "salary",
            },
        ]
    )
    with pd.ExcelWriter(inp, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        manual.to_excel(w, sheet_name="manual_assignments", index=False)

    result = run_planning(inp, out, time_limit_sec=60)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")


def test_salary_switches_april_and_july_feasible(tmp_path: Path):
    """Смена в апреле и июле допустима: новый договор держится апрель–июнь."""
    inp = tmp_path / "input.xlsx"
    out = tmp_path / "result.xlsx"
    year = date.today().year
    _two_contract_workbook(inp)

    manual = pd.DataFrame(
        [
            {
                "employee_id": "E001",
                "contract_id": "C001",
                "year": year,
                "month_from": 1,
                "month_to": 3,
                "payment_kind": "salary",
            },
            {
                "employee_id": "E001",
                "contract_id": "C002",
                "year": year,
                "month_from": 4,
                "month_to": 6,
                "payment_kind": "salary",
            },
            {
                "employee_id": "E001",
                "contract_id": "C001",
                "year": year,
                "month_from": 7,
                "month_to": 12,
                "payment_kind": "salary",
            },
        ]
    )
    with pd.ExcelWriter(inp, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        manual.to_excel(w, sheet_name="manual_assignments", index=False)

    _set_contract_fot_and_inflow(inp, {"C001": 900_000, "C002": 300_000}, monthly_inflow=200_000)

    result = run_planning(inp, out, time_limit_sec=60)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")
    by_month = _salary_contracts_per_month(result, "E001")
    assert "C002" in by_month.get(4, set())
    assert "C002" in by_month.get(6, set())
    assert "C001" in by_month.get(7, set())
    _assert_min_salary_block(by_month)
