"""Лёгкий демо-пример: 3 договора, 5 сотрудников."""

from datetime import date
from pathlib import Path

import pytest

from fot_planner.excel_io import load_context
from fot_planner.planner import run_planning
from fot_planner.reserve_rules import is_long_fot_project
from fot_planner.validation import contract_allows_month

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from create_demo_light import create_demo_light  # noqa: E402


@pytest.fixture(scope="module")
def demo_light_path(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("demo_light") / "demo_light.xlsx"
    create_demo_light(path)
    return path


def test_demo_light_loads_five_employees_three_contracts(demo_light_path: Path):
    ctx = load_context(demo_light_path)
    assert len(ctx.employees) == 5
    assert len(ctx.contracts) == 3
    assert {c.id for c in ctx.contracts} == {"C_GOS", "C_GRANT", "C_VB"}


def test_demo_light_solve_covers_main_rules(demo_light_path: Path, tmp_path: Path):
    out = tmp_path / "result.xlsx"
    result = run_planning(demo_light_path, out, time_limit_sec=120)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    year = date.today().year
    ctx = load_context(demo_light_path)
    grant_contract = next(c for c in ctx.contracts if c.id == "C_GRANT")

    # ГОЗ: нет выплат до марта
    assert not any(a.contract_id == "C_GOS" and a.month < 3 for a in result.allocations)
    gos_contract = next(c for c in ctx.contracts if c.id == "C_GOS")
    assert not contract_allows_month(gos_contract, year, 1)

    # Полное освоение ФОТ по договорам
    for cid, fot in (("C_GOS", 580_000), ("C_GRANT", 1_064_000), ("C_VB", 600_000)):
        spent = sum(a.amount for a in result.allocations if a.contract_id == cid)
        assert spent >= fot - 2_000, f"{cid}: освоено {spent}, ожидалось ~{fot}"

    # Переносы ГОЗ не в январь/февраль (договор с марта)
    bad_xfer = [t for t in result.month_transfers if t.contract_id == "C_GOS" and t.to_month in (1, 2)]
    assert not bad_xfer

    # Грант — длинный ФОТ (12 месяцев поступлений → резерв в модели, если не выключен явно)
    assert is_long_fot_project(grant_contract, min_fot_months=6)

    # Ручная привязка E001 → грант в Q1
    e001_q1 = [
        a
        for a in result.allocations
        if a.employee_id == "E001" and a.contract_id == "C_GRANT" and a.month <= 3 and a.payment_kind == "salary"
    ]
    assert e001_q1
    assert all(a.contract_id == "C_GRANT" for a in e001_q1)

    # E003 (лаборант) не на внебюджете
    assert not any(
        a.employee_id == "E003" and a.contract_id == "C_VB" and a.amount > 0 for a in result.allocations
    )
