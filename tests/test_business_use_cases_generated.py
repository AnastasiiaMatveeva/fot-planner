"""Бизнес-юзкейсы из data/use_cases.

Тесты не пересобирают весь каталог data/use_cases, а проверяют, что
зафиксированные входные файлы дают ожидаемые выходные результаты в temp.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from fot_planner.models import PaymentKind
from fot_planner.planner import run_planning


ROOT = Path(__file__).resolve().parents[1]
USE_CASES = ROOT / "data" / "use_cases"
OK_STATUSES = {"OPTIMAL", "FEASIBLE"}


def _run_use_case(tmp_path: Path, name: str):
    src = USE_CASES / f"{name}_input.xlsx"
    assert src.exists(), src

    input_path = tmp_path / src.name
    output_path = tmp_path / f"{name}_result.xlsx"
    shutil.copyfile(src, input_path)

    result = run_planning(input_path, output_path, time_limit_sec=300)
    assert output_path.exists()
    assert result.solver_status in OK_STATUSES, result.conflicts
    assert not result.deficits
    return result


def _amount(
    result,
    *,
    employee_id: str | None = None,
    contract_id: str | None = None,
    month: int | None = None,
    kinds: tuple[PaymentKind, ...] = (),
) -> float:
    return sum(
        allocation.amount
        for allocation in result.allocations
        if (employee_id is None or allocation.employee_id == employee_id)
        and (contract_id is None or allocation.contract_id == contract_id)
        and (month is None or allocation.month == month)
        and (not kinds or allocation.payment_kind in kinds)
    )


def _open_rate(result, *, employee_id: str, contract_id: str, month: int) -> float:
    return sum(
        record.open_rate
        for record in result.open_rate_attributions
        if record.employee_id == employee_id
        and record.contract_id == contract_id
        and record.month == month
    )


def test_part_time_contract_opens_from_allowed_month_and_keeps_labor_average(tmp_path: Path):
    result = _run_use_case(tmp_path, "uc12_part_time_new_contract")

    assert _amount(result, contract_id="C_NEW", month=5) == pytest.approx(0)

    for employee_id in ("E001-1", "E002-1", "E003-1"):
        assert _amount(result, employee_id=employee_id, contract_id="C_BASE", month=6) == pytest.approx(
            110_000,
            abs=1,
        )
        assert _amount(result, employee_id=employee_id, contract_id="C_NEW", month=6) == pytest.approx(
            50_000,
            abs=1,
        )
        assert _open_rate(result, employee_id=employee_id, contract_id="C_NEW", month=6) == pytest.approx(
            0.5,
            abs=0.01,
        )


def test_bep_limit_uses_open_rate_on_goz_contract(tmp_path: Path):
    result = _run_use_case(tmp_path, "uc13_bep_open_rate")

    assert _amount(
        result,
        employee_id="E001-1",
        contract_id="C_NEW",
        month=6,
        kinds=(PaymentKind.SALARY, PaymentKind.K122),
    ) == pytest.approx(50_000, abs=1)


def test_p4_limit_uses_total_open_rate_when_124_is_paid(tmp_path: Path):
    result = _run_use_case(tmp_path, "uc14_p4_open_rate")

    assert _amount(
        result,
        employee_id="E001-1",
        month=6,
        kinds=(PaymentKind.SALARY, PaymentKind.K122, PaymentKind.K124),
    ) == pytest.approx(150_000, abs=1)
    assert _amount(result, employee_id="E001-1", contract_id="C_NEW", month=6) == pytest.approx(
        50_000,
        abs=1,
    )
