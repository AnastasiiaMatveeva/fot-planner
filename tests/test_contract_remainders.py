"""Цель «меньше договоров с остатком ФОТ»: порога остатка нет, число минимизируется."""
from types import SimpleNamespace

import pyomo.environ as pyo
import pytest

from fot_planner.optimizer import (
    CONTRACT_REMAINDER_EPS, _add_contract_remainder_rules, _run_minimize_stage,
)


def model_for(budgets, paid):
    m = pyo.ConcreteModel()
    m.cons = pyo.ConstraintList()
    contracts = {str(i): SimpleNamespace(total_fot=b) for i, b in enumerate(budgets)}
    keys = [('employee', cid, 1, 'salary') for cid in contracts]
    m.alloc = pyo.Var(keys, domain=pyo.NonNegativeReals)
    m.cons.add(sum(m.alloc.values()) == paid)
    count = _add_contract_remainder_rules(m, contracts, m.alloc)
    return m, count


@pytest.mark.parametrize('remainder', [0, 0.5, 300, 999.99, 1000, 500000])
def test_any_remainder_is_allowed(remainder):
    # Порог «ноль либо не меньше 1 000 ₽» снят 11.09.2026: хвост любого размера
    # допустим, до копейки он считается нулём.
    m, count = model_for([10000 + remainder], 10000)
    status, _, _ = _run_minimize_stage(m, count, 10, integer_objective=True)
    assert status == 'OPTIMAL'
    assert pyo.value(count) == pytest.approx(int(remainder > CONTRACT_REMAINDER_EPS))


def test_count_is_minimized_and_cannot_increase_in_later_stage():
    m, count = model_for([10000, 10000], 15000)
    status, _, best = _run_minimize_stage(m, count, 10, integer_objective=True)
    assert status == 'OPTIMAL' and best == 1
    # Следующая цель хочет остаток на обоих договорах, но зафиксированное число
    # договоров с остатком она увеличить не может.
    status, _, _ = _run_minimize_stage(m, 2 - count, 10)
    assert status == 'OPTIMAL' and pyo.value(count) == pytest.approx(1)
    remaining = sorted(10000 - pyo.value(v) for v in m.alloc.values())
    # Решатель вправе встать ровно на границу допуска, а плавающая точка
    # перешагивает её на 1e-13: сравниваем с допуском плюс погрешность счёта.
    assert remaining == pytest.approx([0, 5000], abs=CONTRACT_REMAINDER_EPS + 1e-6)
