"""Демо: команда на 4 договорах, поступления в месяц старта, carry вперёд."""

from pathlib import Path

import pytest

from fot_planner.planner import run_planning

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from create_demo_team import create_demo_team  # noqa: E402


@pytest.fixture(scope="module")
def demo_team_path(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("demo_team") / "demo_team.xlsx"
    create_demo_team(path)
    return path


def _salary_by_month(result, employee_id: str) -> dict[int, str]:
    by_month: dict[int, str] = {}
    for a in result.allocations:
        if a.employee_id != employee_id or a.payment_kind != "salary" or a.amount < 0.01:
            continue
        assert a.month not in by_month, f"{employee_id}: два оклада в месяце {a.month}"
        by_month[a.month] = a.contract_id
    return by_month


def _expect_blocks(by_month: dict[int, str], blocks: list[tuple[int, int, str]]) -> None:
    for month_from, month_to, contract_id in blocks:
        for m in range(month_from, month_to + 1):
            assert by_month.get(m) == contract_id, f"месяц {m}: ожидался {contract_id}, факт {by_month.get(m)}"


def test_demo_team_solve(demo_team_path: Path, tmp_path: Path):
    out = tmp_path / "demo_team_result.xlsx"
    result = run_planning(demo_team_path, out, time_limit_sec=180)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    for cid, fot in (
        ("C_BASE", 600_000),
        ("C_GOS", 1_500_000),
        ("C_GRANT", 1_320_000),
        ("C_MINPROM", 2_600_000),
    ):
        spent = sum(a.amount for a in result.allocations if a.contract_id == cid)
        assert spent >= fot - 3_000, f"{cid}: освоено {spent}, ожидалось ~{fot}"

    _expect_blocks(
        _salary_by_month(result, "E001"),
        [(1, 2, "C_BASE"), (3, 7, "C_GOS"), (8, 12, "C_MINPROM")],
    )
    _expect_blocks(
        _salary_by_month(result, "E002"),
        [(1, 2, "C_BASE"), (3, 7, "C_GOS"), (8, 12, "C_MINPROM")],
    )
    _expect_blocks(_salary_by_month(result, "E003"), [(1, 12, "C_GRANT")])
    _expect_blocks(_salary_by_month(result, "E004"), [(4, 12, "C_MINPROM")])
    kuz = _salary_by_month(result, "E005")
    assert set(kuz.keys()) == set(range(4, 8))
    _expect_blocks(kuz, [(4, 7, "C_MINPROM")])

    gos_spent_by_month = {
        m: sum(a.amount for a in result.allocations if a.contract_id == "C_GOS" and a.month == m)
        for m in range(1, 13)
    }
    assert gos_spent_by_month.get(1, 0) == 0
    assert gos_spent_by_month.get(2, 0) == 0
    assert sum(gos_spent_by_month.get(m, 0) for m in range(3, 8)) >= 1_400_000
    assert sum(gos_spent_by_month.get(m, 0) for m in range(8, 13)) < 5_000

    gos_mar = next(b for b in result.contract_balances if b.contract_id == "C_GOS" and b.month == 3)
    assert gos_mar.inflow == pytest.approx(1_500_000, rel=0.01)
    assert gos_mar.closing_balance > gos_mar.spent

    grant_jan = next(b for b in result.contract_balances if b.contract_id == "C_GRANT" and b.month == 1)
    assert grant_jan.inflow == pytest.approx(1_320_000, rel=0.01)
    assert grant_jan.closing_balance > grant_jan.spent

    minp_apr = next(b for b in result.contract_balances if b.contract_id == "C_MINPROM" and b.month == 4)
    assert minp_apr.inflow == pytest.approx(2_600_000, rel=0.01)

    # Потолок по договору: оклад и надбавка — отдельные строки на одном договоре в месяце
    ivanov_mar = [
        a
        for a in result.allocations
        if a.employee_id == "E001" and a.month == 3 and a.contract_id == "C_GOS" and a.amount > 0
    ]
    kinds = {a.payment_kind for a in ivanov_mar}
    assert "salary" in kinds and "allowance" in kinds
    assert sum(a.amount for a in ivanov_mar if a.payment_kind == "salary") == pytest.approx(150_000, rel=0.01)
    assert sum(a.amount for a in ivanov_mar if a.payment_kind == "allowance") == pytest.approx(30_000, rel=0.01)
