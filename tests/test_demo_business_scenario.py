"""Интеграционные демонстрационные тесты оптимизатора."""

from __future__ import annotations

from pathlib import Path

import pytest

from demo_business.checks import (
    assert_deficit_scenario,
    assert_no_backward_money_scenario,
    assert_success_scenario,
)
from demo_business.scenario import build_backward_money_scenario, build_demo_scenario
from demo_business.workbook import write_demo_workbook
from fot_planner.planner import run_planning

SOLVE_SEC = 240


@pytest.fixture(scope="module")
def success_workbook(tmp_path_factory) -> Path:
    scenario = build_demo_scenario(allow_deficit=False)
    path = tmp_path_factory.mktemp("demo_business") / "success_input.xlsx"
    write_demo_workbook(scenario, path)
    return path


def test_demo_business_scenario_success(success_workbook: Path, tmp_path: Path):
    out = tmp_path / "success_result.xlsx"
    scenario = build_demo_scenario(allow_deficit=False)
    result = run_planning(success_workbook, out, time_limit_sec=SOLVE_SEC)
    assert_success_scenario(scenario, result, out)


def test_demo_business_scenario_deficit_goes_to_end(tmp_path: Path):
    scenario = build_demo_scenario(allow_deficit=False)
    deficit_scenario = scenario.with_deficit_variant(project_fot=450_000)

    inp = tmp_path / "deficit_input.xlsx"
    out = tmp_path / "deficit_result.xlsx"
    write_demo_workbook(deficit_scenario, inp)

    result = run_planning(inp, out, time_limit_sec=SOLVE_SEC)
    assert_deficit_scenario(deficit_scenario, result, out)


def test_no_backward_money(tmp_path: Path):
    scenario = build_backward_money_scenario()
    inp = tmp_path / "backward_input.xlsx"
    out = tmp_path / "backward_result.xlsx"
    write_demo_workbook(scenario, inp)

    result = run_planning(inp, out, time_limit_sec=SOLVE_SEC)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")
    assert_no_backward_money_scenario(scenario, result)
