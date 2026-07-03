"""П4: помесячный лимит штатной части при выплате 124."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fot_planner.excel import SHEET_CONTRACT_LABOR, create_template, load_context
from fot_planner.excel.constants import SHEET_POSITION_LIMITS
from fot_planner.models import PaymentKind
from fot_planner.optimizer import solve
from fot_planner.payroll_rules import AVERAGE_LIMIT_MODE
from fot_planner.salary_limits_2556 import P4_LIMIT_BY_CATEGORY_2025

P4_NTP = P4_LIMIT_BY_CATEGORY_2025["НТП"]


def _one_engineer_workbook(path: Path, *, wage: float = 160_000) -> None:
    year = 2026
    create_template(path)
    position_limits = pd.read_excel(path, sheet_name=SHEET_POSITION_LIMITS)
    settings = pd.read_excel(path, sheet_name="настройки")
    settings.loc[0, "год"] = year

    employees = pd.DataFrame(
        [
            {
                "код строки": "E001-1",
                "фио": "Иванов И.И.",
                "должность": "инженер",
                "подразделение": "НИОКР",
                "ставка": 1.0,
                "тип занятости": "основное",
                "категория занятости": "основной",
                "зарплата": wage,
                "дата начала": f"{year}-01-01",
                "дата окончания": "",
                "разрешенные договоры": "",
                "запрещенные договоры": "",
            }
        ]
    )
    contracts = pd.DataFrame(
        [
            {
                "код": "C001",
                "название": "Грант",
                "номер": "1",
                "тип договора": "grant",
                "счет": "",
                "ГОЗ": False,
                "дата начала": f"{year}-01-01",
                "дата окончания": f"{year}-12-31",
                "фот": wage * 12,
                "оклад разрешен": True,
                "120 разрешена": False,
                "122 разрешена": True,
                "124 разрешена": True,
                "152 разрешена": False,
                "стимулирующая приказом разрешена": True,
                "конечная дата выплат оклада": f"{year}-12-31",
                "конечная дата выплат надбавок": f"{year}-12-31",
                "перенос остатков": True,
            }
        ]
    )
    positions = pd.DataFrame([{"договор": "C001", "должность": "инженер", "макс ставки": 1}])
    labor = pd.DataFrame(
        [
            {
                "договор": "C001",
                "год": year,
                "трудоемкость": 12,
                "должность": "инженер",
                "средняя зарплата": wage,
            }
        ]
    )
    fot_matrix = pd.DataFrame({"договор": ["C001"]})
    for month in range(1, 13):
        fot_matrix[str(month)] = [wage]

    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
        employees.to_excel(writer, sheet_name="сотрудники", index=False)
        contracts.to_excel(writer, sheet_name="договоры", index=False)
        positions.to_excel(writer, sheet_name="должности", index=False)
        labor.to_excel(writer, sheet_name=SHEET_CONTRACT_LABOR, index=False)
        fot_matrix.to_excel(writer, sheet_name="фот_по_месяцам", index=False)
        settings.to_excel(writer, sheet_name="настройки", index=False)
        position_limits.to_excel(writer, sheet_name=SHEET_POSITION_LIMITS, index=False)


def _staff_amount(allocations, *, employee_id: str, month: int) -> float:
    staff_kinds = {
        PaymentKind.SALARY,
        PaymentKind.K122,
        PaymentKind.K124,
    }
    return sum(
        allocation.amount
        for allocation in allocations
        if allocation.employee_id == employee_id
        and allocation.month == month
        and allocation.payment_kind in staff_kinds
    )


def test_p4_limits_staff_in_each_month_with_124(tmp_path: Path):
    path = tmp_path / "p4_monthly.xlsx"
    _one_engineer_workbook(path)

    result = solve(load_context(path), time_limit_sec=120, payroll_limit_mode=AVERAGE_LIMIT_MODE)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    monthly_totals = {
        month: sum(
            allocation.amount
            for allocation in result.allocations
            if allocation.employee_id == "E001-1" and allocation.month == month
        )
        for month in range(1, 13)
    }
    assert all(total == pytest.approx(160_000, abs=1) for total in monthly_totals.values())

    for month in range(1, 13):
        has_124 = any(
            allocation.payment_kind is PaymentKind.K124 and allocation.amount > 0.01
            for allocation in result.allocations
            if allocation.employee_id == "E001-1" and allocation.month == month
        )
        staff = _staff_amount(result.allocations, employee_id="E001-1", month=month)
        if has_124:
            assert staff <= P4_NTP + 1
        if staff > P4_NTP + 1:
            assert not has_124
