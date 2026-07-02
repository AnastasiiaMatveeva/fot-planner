"""Виды выплат: коды приказа и иерархия зарплаты.

``monthly_wage`` = оклад + надбавки (120/122/124/152) + отдельная стимулирующая приказом.
"""

from __future__ import annotations

from enum import IntEnum


class PaymentKind(IntEnum):
    ORDER_INCENTIVE = 0
    SALARY = 1
    K120 = 120
    K122 = 122
    K124 = 124
    K152 = 152

    @property
    def is_salary(self) -> bool:
        return self is PaymentKind.SALARY

    @property
    def is_supplement(self) -> bool:
        """Код надбавки: 120, 122, 124 или 152."""
        return self in SUPPLEMENT_KINDS

    @property
    def is_order_incentive(self) -> bool:
        return self is PaymentKind.ORDER_INCENTIVE

    @property
    def is_non_salary(self) -> bool:
        """Не оклад: надбавки или отдельная стимулирующая приказом."""
        return not self.is_salary


SUPPLEMENT_KINDS: tuple[PaymentKind, ...] = (
    PaymentKind.K120,
    PaymentKind.K122,
    PaymentKind.K124,
    PaymentKind.K152,
)

PAYMENT_KINDS: tuple[PaymentKind, ...] = (
    PaymentKind.SALARY,
    *SUPPLEMENT_KINDS,
    PaymentKind.ORDER_INCENTIVE,
)

# Выплаты, которые закрывают денежную часть трудоёмкости договора:
# оклад и кодовые выплаты с этого же договора. Стимулирующая приказом пока
# не относится на трудоёмкость.
LABOR_PAYMENT_KINDS: tuple[PaymentKind, ...] = (
    PaymentKind.SALARY,
    *SUPPLEMENT_KINDS,
)

# П4 и лимиты «оклад + 122» (П2556): только эти три вида.
STAFF_LIMIT_KINDS: tuple[PaymentKind, ...] = (
    PaymentKind.SALARY,
    PaymentKind.K122,
    PaymentKind.K124,
)
