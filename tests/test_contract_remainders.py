"""Boundary and lexicographic tests for end-of-plan FOT remainders."""
from types import SimpleNamespace

import pyomo.environ as pyo
import pytest

from fot_planner.optimizer import _add_contract_remainder_rules, _run_minimize_stage, _invalid_rounded_remainders


def model_for(budgets, paid):
    m = pyo.ConcreteModel()
    m.cons = pyo.ConstraintList()
    contracts = {str(i): SimpleNamespace(total_fot=b) for i, b in enumerate(budgets)}
    keys = [('employee', cid, 1, 'salary') for cid in contracts]
    m.alloc = pyo.Var(keys, domain=pyo.NonNegativeReals)
    m.cons.add(sum(m.alloc.values()) == paid)
    count = _add_contract_remainder_rules(m, contracts, m.alloc)
    return m, count


@pytest.mark.parametrize('remainder', [0, 1000, 500000])
def test_allowed_remainder(remainder):
    m, count = model_for([10000 + remainder], 10000)
    status, _, _ = _run_minimize_stage(m, count, 10, integer_objective=True)
    assert status == 'OPTIMAL'
    assert pyo.value(count) == pytest.approx(int(remainder > 0))


@pytest.mark.parametrize('remainder', [0.01, 1, 999, 999.99])
def test_small_remainder_is_infeasible(remainder):
    m, count = model_for([10000 + remainder], 10000)
    status, _, _ = _run_minimize_stage(m, count, 10, integer_objective=True)
    assert status == 'INFEASIBLE'


def test_count_is_minimized_and_cannot_increase_in_later_stage():
    m, count = model_for([10000, 10000], 15000)
    status, _, best = _run_minimize_stage(m, count, 10, integer_objective=True)
    assert status == 'OPTIMAL' and best == 1
    # A subsequent objective wants both contracts to have remainders.
    status, _, _ = _run_minimize_stage(m, 2 - count, 10)
    assert status == 'OPTIMAL' and pyo.value(count) == pytest.approx(1)
    remaining = sorted(10000 - pyo.value(v) for v in m.alloc.values())
    assert remaining == pytest.approx([0, 5000])


@pytest.mark.parametrize('paid,invalid', [(10000, False), (9000, False), (9999.99, True), (9000.01, True), (10000.01, True)])
def test_rounded_output_cannot_bypass_rule(paid, invalid):
    contracts = [SimpleNamespace(id='C', total_fot=10000)]
    rows = [SimpleNamespace(contract_id='C', amount=paid)]
    assert bool(_invalid_rounded_remainders(contracts, rows)) is invalid
