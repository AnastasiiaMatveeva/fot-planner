"""Таблицы для технического/внутреннего экспорта Excel."""

from __future__ import annotations

from collections import defaultdict

import pandas as pd

from fot_planner.excel.constants import PAYMENT_KIND_RU, RU_MONTHS
from fot_planner.labor_rules import labor_rows_for_contract, planned_labor_groups
from fot_planner.models import PlanningContext, PlanningResult, labor_row_id

def position_control_dataframe(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    employees = {e.id: e for e in ctx.employees}
    contracts = {c.id: c for c in ctx.contracts}
    rows: list[dict] = []

    for allocation in result.allocations:
        employee = employees.get(allocation.employee_id)
        contract = contracts.get(allocation.contract_id)
        if employee is None or contract is None:
            continue

        compatible_rules = []
        if contract.position_rules and employee.equivalence_group:
            compatible_rules = [
                pr
                for pr in contract.position_rules
                if pr.equivalence_group == employee.equivalence_group
            ]
        elif contract.position_rules:
            compatible_rules = [
                pr for pr in contract.position_rules if pr.position == employee.position
            ]

        if contract.position_rules:
            compatible = bool(compatible_rules)
        else:
            compatible = True

        if compatible_rules:
            salary_cap_1_rate = max(
                (pr.max_monthly_payment or pr.reference_salary_for_rate or 0.0)
                for pr in compatible_rules
            )
            contract_positions = "; ".join(sorted({pr.position for pr in compatible_rules}))
            contract_groups = "; ".join(
                sorted({pr.equivalence_group or "" for pr in compatible_rules if pr.equivalence_group})
            )
        else:
            salary_cap_1_rate = None
            contract_positions = ""
            contract_groups = ""

        rows.append(
            {
                "сотрудник": allocation.employee_id,
                "фио": employee.full_name,
                "должность сотрудника": employee.position,
                "группа сотрудника": employee.equivalence_group or "",
                "договор": allocation.contract_id,
                "должность по договору": contract_positions,
                "группа по договору": contract_groups,
                "месяц": RU_MONTHS.get(allocation.month, allocation.month),
                "вид выплаты": PAYMENT_KIND_RU.get(
                    allocation.payment_kind, allocation.payment_kind
                ),
                "сумма": allocation.amount,
                "совместимость": "да" if compatible else "нет",
                "окладный потолок за 1 ставку": salary_cap_1_rate if salary_cap_1_rate is not None else "",
                "ставка": employee.rate,
                "потолок с учетом ставки": (
                    salary_cap_1_rate * employee.rate
                    if salary_cap_1_rate is not None
                    else ""
                ),
            }
        )

    return pd.DataFrame(rows)


def labor_by_group_dataframe(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    employees = {e.id: e for e in ctx.employees}
    facts: dict[tuple[str, str | None], float] = {}
    for allocation in result.allocations:
        if allocation.payment_kind != "salary" or allocation.amount < 0.01:
            continue
        employee = employees.get(allocation.employee_id)
        if employee is None:
            continue
        key = (allocation.contract_id, employee.equivalence_group)
        facts[key] = facts.get(key, 0.0) + employee.rate

    rows: list[dict] = []
    for contract in ctx.contracts:
        for group, plan_pm in planned_labor_groups(ctx, contract.id):
            fact_pm = facts.get((contract.id, group), 0.0)
            deviation = fact_pm - plan_pm
            rows.append(
                {
                    "договор": contract.id,
                    "группа взаимозаменяемости": group or "",
                    "план чел.-мес.": round(plan_pm, 4),
                    "факт чел.-мес.": round(fact_pm, 4),
                    "отклонение": round(deviation, 4),
                    "статус": "выполнено" if abs(deviation) <= 0.01 else "отклонение",
                }
            )

    return pd.DataFrame(rows)


def labor_by_row_dataframe(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    row_id_to_idx: dict[str, int] = {}
    for contract in ctx.contracts:
        for lp_idx, lp in labor_rows_for_contract(ctx, contract.id):
            row_id_to_idx[labor_row_id(lp)] = lp_idx

    fact_pm: dict[int, float] = defaultdict(float)
    for rec in result.labor_pm_attributions:
        lp_idx = row_id_to_idx.get(rec.labor_row_id)
        if lp_idx is not None:
            fact_pm[lp_idx] += rec.person_months

    fact_amount: dict[int, float] = defaultdict(float)
    for rec in result.labor_payment_attributions:
        lp_idx = row_id_to_idx.get(rec.labor_row_id)
        if lp_idx is not None:
            fact_amount[lp_idx] += rec.amount

    rows: list[dict] = []
    for contract in ctx.contracts:
        for lp_idx, lp in labor_rows_for_contract(ctx, contract.id):
            plan_pm = lp.person_months
            avg_cost = lp.avg_monthly_labor_cost or 0.0
            plan_amount = plan_pm * avg_cost if avg_cost else 0.0
            f_pm = fact_pm.get(lp_idx, 0.0)
            f_amount = fact_amount.get(lp_idx, 0.0)
            f_avg = f_amount / f_pm if f_pm > 0.01 else None
            pm_dev = f_pm - plan_pm
            amount_dev = f_amount - plan_amount if plan_amount else None
            tol = ctx.salary_stability.goz_labor_tolerance
            pm_ok = plan_pm <= 0 or abs(pm_dev) <= tol * plan_pm
            amount_ok = (
                plan_amount <= 0
                or amount_dev is None
                or abs(amount_dev) <= tol * plan_amount
            )
            rows.append(
                {
                    "договор": contract.id,
                    "должность": lp.position or "",
                    "группа взаимозаменяемости": lp.equivalence_group or "",
                    "план чел.-мес.": round(plan_pm, 4),
                    "средняя стоимость выполнения работ в месяц": round(avg_cost, 2)
                    if avg_cost
                    else "",
                    "плановая сумма по строке": round(plan_amount, 2) if plan_amount else "",
                    "факт чел.-мес.": round(f_pm, 4),
                    "факт сумма по строке": round(f_amount, 2),
                    "фактическая средняя": round(f_avg, 2) if f_avg is not None else "",
                    "отклонение чел.-мес.": round(pm_dev, 4),
                    "отклонение суммы": round(amount_dev, 2) if amount_dev is not None else "",
                    "статус": "выполнено" if pm_ok and amount_ok else "отклонение",
                }
            )
    return pd.DataFrame(rows)


def labor_payment_report_dataframe(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    employees = {e.id: e for e in ctx.employees}
    payment_totals: dict[tuple[str, str, int, str], float] = defaultdict(float)
    for allocation in result.allocations:
        if allocation.amount > 0:
            key = (
                allocation.employee_id,
                allocation.contract_id,
                allocation.month,
                allocation.payment_kind,
            )
            payment_totals[key] += allocation.amount

    kind_labels = {"salary": "оклад", "allowance": "надбавка", "incentive": "стимулирующая"}
    rows: list[dict] = []
    for rec in result.labor_payment_attributions:
        employee = employees.get(rec.employee_id)
        total_paid = payment_totals.get(
            (rec.employee_id, rec.contract_id, rec.month, rec.payment_kind), 0.0
        )
        rows.append(
            {
                "сотрудник": rec.employee_id,
                "фио": employee.full_name if employee else "",
                "договор": rec.contract_id,
                "месяц": RU_MONTHS.get(rec.month, rec.month),
                "вид выплаты": kind_labels.get(rec.payment_kind, rec.payment_kind),
                "выплачено всего": round(total_paid, 2),
                "на строку трудоёмкости": round(rec.amount, 2),
                "должность строки": rec.position or "",
                "группа строки": rec.equivalence_group or "",
            }
        )
    return pd.DataFrame(rows)


def project_monthly_grid(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    """Остаток лимита ФОТ договора после плана выплат (не кассовый остаток)."""
    planned_by_contract_month: dict[tuple[str, int], float] = {}
    for allocation in result.allocations:
        key = (allocation.contract_id, allocation.month)
        planned_by_contract_month[key] = planned_by_contract_month.get(key, 0.0) + allocation.amount

    rows = []
    short_year = str(result.year)[-2:]
    fot_remainder_label = "Ост. лимита ФОТ"
    for contract in ctx.contracts:
        remaining_fot = contract.total_fot
        row = {
            "код": contract.id,
            "проект": contract.name,
            "тип договора": contract.contract_type,
        }
        for month in range(1, 13):
            if month == 1:
                row[f"{fot_remainder_label} на 01.01.{result.year}"] = remaining_fot
            month_name = RU_MONTHS[month]
            month_plan = planned_by_contract_month.get((contract.id, month), 0.0)
            row[f"{month_name} {short_year} (план)"] = month_plan
            remaining_fot -= month_plan
            next_month = month + 1
            next_year = result.year
            if next_month == 13:
                next_month = 1
                next_year += 1
            row[f"{fot_remainder_label} на 01.{next_month:02d}.{next_year}"] = remaining_fot
        rows.append(row)
    return pd.DataFrame(rows)


def result_readable_tables(
    ctx: PlanningContext, result: PlanningResult
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    employees = {e.id: e for e in ctx.employees}
    contracts = {c.id: c for c in ctx.contracts}
    outgoing_transfers: dict[tuple[str, int], list[str]] = {}
    incoming_transfers: dict[tuple[str, int], list[str]] = {}
    for transfer in result.month_transfers:
        outgoing_transfers.setdefault((transfer.contract_id, transfer.from_month), []).append(
            f"{RU_MONTHS[transfer.to_month]}: {transfer.amount:,.0f}".replace(",", " ")
        )
        incoming_transfers.setdefault((transfer.contract_id, transfer.to_month), []).append(
            f"{RU_MONTHS[transfer.from_month]}: {transfer.amount:,.0f}".replace(",", " ")
        )

    plan_rows = []
    for allocation in result.allocations:
        employee = employees.get(allocation.employee_id)
        contract = contracts.get(allocation.contract_id)
        plan_rows.append(
            {
                "табельный номер": allocation.employee_id,
                "фио": employee.full_name if employee else "",
                "должность": employee.position if employee else "",
                "договор": allocation.contract_id,
                "проект": contract.name if contract else "",
                "год": allocation.year,
                "месяц": RU_MONTHS[allocation.month],
                "вид выплаты": PAYMENT_KIND_RU.get(
                    allocation.payment_kind, allocation.payment_kind
                ),
                "сумма": allocation.amount,
                "зафиксировано": "да" if allocation.is_manual else "нет",
                "источник": allocation.source,
            }
        )

    balance_rows = []
    for balance in result.contract_balances:
        contract = contracts.get(balance.contract_id)
        row = {
            "договор": balance.contract_id,
            "проект": contract.name if contract else "",
            "год": balance.year,
            "месяц": RU_MONTHS[balance.month],
            "остаток на начало": balance.opening_balance,
            "поступление": balance.inflow,
            "потрачено": balance.spent,
            "остаток на конец": balance.closing_balance,
            "перенос на будущий месяц": balance.carried_forward,
        }
        if balance.min_balance_required > 0.005:
            row["мин. остаток на конец"] = balance.min_balance_required
        if balance.transfer_in > 0.005 or balance.transfer_out > 0.005:
            row["перенос пришел"] = balance.transfer_in
            row["откуда пришло"] = "; ".join(
                incoming_transfers.get((balance.contract_id, balance.month), [])
            )
            row["перенос ушел"] = balance.transfer_out
            row["куда перенесено"] = "; ".join(
                outgoing_transfers.get((balance.contract_id, balance.month), [])
            )
        balance_rows.append(row)

    transfer_rows = []
    matrix_rows: dict[tuple[str, int], dict] = {}
    for transfer in result.month_transfers:
        contract = contracts.get(transfer.contract_id)
        transfer_rows.append(
            {
                "договор": transfer.contract_id,
                "проект": contract.name if contract else "",
                "из месяца": RU_MONTHS[transfer.from_month],
                "в месяц": RU_MONTHS[transfer.to_month],
                "сумма": transfer.amount,
            }
        )
        key = (transfer.contract_id, transfer.from_month)
        row = matrix_rows.setdefault(
            key,
            {
                "договор": transfer.contract_id,
                "проект": contract.name if contract else "",
                "из месяца": RU_MONTHS[transfer.from_month],
            },
        )
        col = f"в {RU_MONTHS[transfer.to_month]}"
        row[col] = row.get(col, 0.0) + transfer.amount

    month_cols = [f"в {RU_MONTHS[month]}" for month in range(1, 13)]
    matrix_df = pd.DataFrame(matrix_rows.values())
    if matrix_df.empty:
        matrix_df = pd.DataFrame(columns=["договор", "проект", "из месяца", *month_cols])
    else:
        for col in month_cols:
            if col not in matrix_df.columns:
                matrix_df[col] = 0.0
        matrix_df = matrix_df[["договор", "проект", "из месяца", *month_cols]]

    transfers_df = pd.DataFrame(transfer_rows)
    if transfers_df.empty:
        transfers_df = pd.DataFrame(columns=["договор", "проект", "из месяца", "в месяц", "сумма"])

    return pd.DataFrame(plan_rows), pd.DataFrame(balance_rows), transfers_df, matrix_df


_BALANCE_CORE_COLUMNS = [
    "договор",
    "проект",
    "год",
    "месяц",
    "остаток на начало",
    "поступление",
    "потрачено",
    "остаток на конец",
    "перенос на будущий месяц",
]

_LEGACY_BACKWARD_TRANSFER_COLUMNS = [
    "перенос пришел",
    "откуда пришло",
    "перенос ушел",
    "куда перенесено",
]


def balance_export_columns(balances_df: pd.DataFrame) -> list[str]:
    cols = list(_BALANCE_CORE_COLUMNS)
    if balances_df.empty:
        return cols
    if "мин. остаток на конец" in balances_df.columns:
        cols.append("мин. остаток на конец")
    for col in _LEGACY_BACKWARD_TRANSFER_COLUMNS:
        if col in balances_df.columns:
            cols.append(col)
    return cols


def export_balances_sheet(writer: pd.ExcelWriter, balances_df: pd.DataFrame) -> None:
    export_cols = _balance_export_columns(balances_df)
    if balances_df.empty:
        pd.DataFrame(columns=export_cols).to_excel(
            writer, sheet_name="остатки_и_переносы", index=False
        )
        return
    balances_df[export_cols].to_excel(
        writer, sheet_name="остатки_и_переносы", index=False
    )

