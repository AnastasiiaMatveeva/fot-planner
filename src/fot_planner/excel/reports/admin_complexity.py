"""Отчёт по административной сложности выплат."""

from __future__ import annotations

from collections import defaultdict

from fot_planner.models import PlanningContext, PlanningResult
from fot_planner.validation import employee_active_in_month


def build_admin_complexity_dataframe(ctx: PlanningContext, result: PlanningResult):
    import pandas as pd

    from fot_planner.excel.constants import PAYMENT_KIND_RU, RU_MONTHS

    employees = {e.id: e for e in ctx.employees}
    year = ctx.year

    year_contracts: dict[str, set[str]] = defaultdict(set)
    month_contracts: dict[tuple[str, int], set[str]] = defaultdict(set)
    flex_fragments: list[dict] = []

    for a in result.allocations:
        if a.amount <= 0.005:
            continue
        year_contracts[a.employee_id].add(a.contract_id)
        month_contracts[(a.employee_id, a.month)].add(a.contract_id)
        if a.payment_kind.is_non_salary:
            emp = employees.get(a.employee_id)
            flex_fragments.append(
                {
                    "сотрудник": emp.full_name if emp else a.employee_id,
                    "табельный номер": a.employee_id,
                    "месяц": RU_MONTHS.get(a.month, a.month),
                    "договор": a.contract_id,
                    "вид выплаты": PAYMENT_KIND_RU.get(a.payment_kind, str(a.payment_kind.value)),
                    "сумма": round(a.amount, 2),
                }
            )

    scheme_rows: list[dict] = []
    for e in ctx.employees:
        for m in range(2, 13):
            if not employee_active_in_month(e, year, m) and not employee_active_in_month(
                e, year, m - 1
            ):
                continue
            prev = month_contracts.get((e.id, m - 1), set())
            curr = month_contracts.get((e.id, m), set())
            all_c = prev | curr
            changes = 0
            for c_id in all_c:
                if (c_id in curr) != (c_id in prev):
                    changes += 1
            if changes:
                scheme_rows.append(
                    {
                        "сотрудник": e.full_name,
                        "табельный номер": e.id,
                        "месяц": RU_MONTHS.get(m, m),
                        "смен схемы": changes // 2 if changes % 2 == 0 else changes,
                        "договоры было": "; ".join(sorted(prev)) or "—",
                        "договоры стало": "; ".join(sorted(curr)) or "—",
                    }
                )

    summary_rows: list[dict] = []
    for e in ctx.employees:
        contracts = sorted(year_contracts.get(e.id, set()))
        summary_rows.append(
            {
                "сотрудник": e.full_name,
                "табельный номер": e.id,
                "договоров за год": len(contracts),
                "договоры": "; ".join(contracts) if contracts else "—",
                "фрагментов переменных выплат": sum(
                    1
                    for a in result.allocations
                    if a.employee_id == e.id
                    and a.payment_kind.is_non_salary
                    and a.amount > 0.005
                ),
                "смен схемы за год": sum(
                    1 for r in scheme_rows if r["табельный номер"] == e.id
                ),
            }
        )

    return pd.DataFrame(summary_rows), pd.DataFrame(scheme_rows), pd.DataFrame(flex_fragments)
