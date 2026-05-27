"""Остаток с прошлого года на январь и лимит переноса суммой (без долей)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CarryoverLimits:
    max_amount: float | None
    max_december: float | None


def carryover_limits(contract, rules) -> CarryoverLimits:
    def pick(cval, rval):
        return cval if cval is not None else rval

    return CarryoverLimits(
        max_amount=pick(contract.max_carryover_amount, rules.max_carryover_amount),
        max_december=pick(contract.max_december_carryover_amount, rules.max_december_carryover_amount),
    )


def capped_january_opening(contract, rules) -> float:
    """Входящий остаток на январь (с декабря прошлого года), с учётом лимита."""
    limits = carryover_limits(contract, rules)
    amount = contract.opening_balance
    if limits.max_amount is not None:
        amount = min(amount, limits.max_amount)
    if limits.max_december is not None:
        amount = min(amount, limits.max_december)
    return amount
