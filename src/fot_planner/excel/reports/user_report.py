"""Пользовательские листы выходного Excel (отчёт для финансиста)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd

from fot_planner.labor_rules import (
    contract_has_labor_plan,
    employee_compatible_with_labor_row,
    labor_rows_for_contract,
    planned_labor_amount,
)
from fot_planner.models import PlanningContext, PlanningResult, labor_row_id
from fot_planner.optimizer import MIN_FLEX_FRAGMENT_AMOUNT
from fot_planner.payment_split import employee_monthly_payment_due
from fot_planner.excel.reports.spend_plan import build_spend_plan_fact_dataframe
from fot_planner.validation import employee_active_in_month

FLEX_KINDS = frozenset({"allowance", "incentive"})

MONTH_SHORT: dict[int, str] = {
    1: "Янв",
    2: "Фев",
    3: "Мар",
    4: "Апр",
    5: "Май",
    6: "Июн",
    7: "Июл",
    8: "Авг",
    9: "Сен",
    10: "Окт",
    11: "Ноя",
    12: "Дек",
}
MONTH_COLS = [MONTH_SHORT[m] for m in range(1, 13)] + ["Итого"]

RU_MONTHS: dict[int, str] = {
    1: "Январь",
    2: "Февраль",
    3: "Март",
    4: "Апрель",
    5: "Май",
    6: "Июнь",
    7: "Июль",
    8: "Август",
    9: "Сентябрь",
    10: "Октябрь",
    11: "Ноябрь",
    12: "Декабрь",
}
MONTH_RU_TO_SHORT = {v: MONTH_SHORT[k] for k, v in RU_MONTHS.items()}
MONTH_RU_TO_NUM = {v: k for k, v in RU_MONTHS.items()}

PAYMENT_KIND_RU = {
    "salary": "оклад",
    "allowance": "надбавка",
    "incentive": "стимулирующая",
}

SHEET_README = "Как читать файл"
SHEET_SUMMARY = "Итог расчета"
SHEET_ISSUES = "Проблемы и предупреждения"
SHEET_EMPLOYEE_PAYMENTS = "Выплаты по сотрудникам"
SHEET_CONTRACT_PAYMENTS = "Выплаты по договорам"
SHEET_BALANCES = "Остатки по договорам"
SHEET_SPEND_PLAN = "Освоение план-факт"
SHEET_LABOR_CONTROL = "Контроль трудоёмкости"
SHEET_LABOR = "Трудоёмкость по строкам"  # legacy, не экспортируется
SHEET_LABOR_BREAKDOWN = "Расшифровка трудоёмкости"  # legacy, не экспортируется
SHEET_PLAN = "План выплат"
SHEET_LABOR_PAYMENTS = "Выплаты в трудоёмкость"
SHEET_POSITION = "Контроль должностей"
SHEET_ADMIN = "Административная сложность"
SHEET_SPLIT = "Проверка дробления выплат"
SHEET_SCHEME = "Смены схемы выплат"
SHEET_DEFICITS = "Дефициты"
SHEET_DEFICIT_MONTH = "Дефицит по месяцам"


def _single_message(message: str) -> pd.DataFrame:
    return pd.DataFrame([{"": message}])


def _empty_matrix_row(id_cols: list[str], message: str) -> pd.DataFrame:
    row = {id_cols[0]: message}
    for col in id_cols[1:]:
        row[col] = ""
    for col in MONTH_COLS:
        row[col] = ""
    return pd.DataFrame([row])


def _sum_matrix_row(values: dict[int, float]) -> dict[str, float | str]:
    row: dict[str, float | str] = {}
    total = 0.0
    for m in range(1, 13):
        v = values.get(m, 0.0)
        row[MONTH_SHORT[m]] = round(v, 2) if abs(v) > 0.005 else ""
        total += v
    row["Итого"] = round(total, 2) if abs(total) > 0.005 else ""
    return row


def _salary_contracts_by_month(allocations, employee_id: str) -> dict[int, str]:
    by_month: dict[int, str] = {}
    for a in allocations:
        if a.employee_id != employee_id or a.payment_kind != "salary" or a.amount < 0.01:
            continue
        by_month[a.month] = a.contract_id
    return by_month


def _count_salary_switches(allocations, employee_id: str) -> int:
    by_month = _salary_contracts_by_month(allocations, employee_id)
    changes = 0
    for m in range(2, 13):
        prev = by_month.get(m - 1)
        curr = by_month.get(m)
        if prev and curr and prev != curr:
            changes += 1
    return changes


def _contracts_by_month(allocations, employee_id: str, month: int) -> set[str]:
    return {
        a.contract_id
        for a in allocations
        if a.employee_id == employee_id and a.month == month and a.amount > 0.005
    }


def _flex_contracts_text(allocations, employee_id: str, month: int, kind: str) -> str:
    parts = []
    for a in allocations:
        if (
            a.employee_id == employee_id
            and a.month == month
            and a.payment_kind == kind
            and a.amount > 0.005
        ):
            parts.append(a.contract_id)
    return "; ".join(sorted(set(parts)))


def _flex_amount_text(allocations, employee_id: str, month: int, kind: str) -> str:
    by_c: dict[str, float] = defaultdict(float)
    for a in allocations:
        if a.employee_id == employee_id and a.month == month and a.payment_kind == kind:
            by_c[a.contract_id] += a.amount
    parts = [f"{c}: {round(v, 0):,.0f}".replace(",", " ") for c, v in sorted(by_c.items()) if v > 0.005]
    return "; ".join(parts)


def _flex_total(allocations, employee_id: str, month: int, kind: str) -> float:
    return sum(
        a.amount
        for a in allocations
        if a.employee_id == employee_id and a.month == month and a.payment_kind == kind
    )


def build_readme_sheet() -> pd.DataFrame:
    rows = [
        {"раздел": "Что смотреть в первую очередь", "описание": ""},
        {"раздел": "1", "описание": "Итог расчета — общий статус расчёта."},
        {"раздел": "2", "описание": "Проблемы и предупреждения — все ошибки и предупреждения в одном месте."},
        {"раздел": "3", "описание": "Выплаты по сотрудникам — кто с каких договоров получает выплаты по месяцам."},
        {"раздел": "4", "описание": "Остатки по договорам — хватает ли денег по договорам."},
        {"раздел": "5", "описание": "Контроль трудоёмкости — главный лист: план/факт по договорам, месяцам и сотрудникам."},
        {"раздел": "6", "описание": "Дефициты — кому, когда и сколько не хватило, если дефицит разрешён."},
        {"раздел": "", "описание": ""},
        {"раздел": "Термины", "описание": ""},
        {
            "раздел": "Оклад",
            "описание": "Обязательная часть зарплаты. Должен быть назначен каждый месяц активному сотруднику.",
        },
        {
            "раздел": "Надбавка / стимулирующая",
            "описание": "Переменная часть выплаты. Может идти с другого договора; дробление нежелательно.",
        },
        {
            "раздел": "Остаток договора",
            "описание": "Физический остаток денег по договору. Деньги переносятся только вперёд.",
        },
        {
            "раздел": "Освоение план-факт",
            "описание": "Сравнение фактических выплат с равномерным планом освоения (не физический перенос).",
        },
        {
            "раздел": "Трудоёмкость по строке",
            "описание": "Плановая и фактическая средняя = сумма / чел.-мес. Статус «выполнено по группе» — отклонение компенсировано внутри группы взаимозаменяемости.",
        },
        {
            "раздел": "Административная сложность",
            "описание": "Насколько план трудоёмок для сопровождения: связки сотрудник–договор, смены схем, дробления.",
        },
    ]
    return pd.DataFrame(rows)


PAYMENT_WITHOUT_PM_MSG = "Есть выплата без трудоёмкости в этом месяце"


def _payment_on_labor_row(
    result: PlanningResult,
    employee_id: str,
    contract_id: str,
    month: int,
    row_id: str,
) -> float:
    return sum(
        rec.amount
        for rec in result.labor_payment_attributions
        if rec.employee_id == employee_id
        and rec.contract_id == contract_id
        and rec.month == month
        and rec.labor_row_id == row_id
    )


def _pm_on_labor_row(
    result: PlanningResult,
    employee_id: str,
    contract_id: str,
    month: int,
    row_id: str,
) -> float:
    return sum(
        rec.person_months
        for rec in result.labor_pm_attributions
        if rec.employee_id == employee_id
        and rec.contract_id == contract_id
        and rec.month == month
        and rec.labor_row_id == row_id
    )


def _labor_payment_without_pm_issues(ctx: PlanningContext, result: PlanningResult) -> list[dict]:
    employees = {e.id: e for e in ctx.employees}
    seen: set[tuple[str, str, str, int, str]] = set()
    issues: list[dict] = []
    for rec in result.labor_payment_attributions:
        if rec.amount <= 0.005:
            continue
        key = (rec.employee_id, rec.contract_id, rec.labor_row_id, rec.month, rec.position or "")
        if key in seen:
            continue
        pm = _pm_on_labor_row(
            result, rec.employee_id, rec.contract_id, rec.month, rec.labor_row_id
        )
        if pm > 0.005:
            continue
        seen.add(key)
        total = _payment_on_labor_row(
            result, rec.employee_id, rec.contract_id, rec.month, rec.labor_row_id
        )
        if total <= 0.005:
            continue
        emp = employees.get(rec.employee_id)
        issues.append(
            {
                "уровень": "Ошибка",
                "раздел": "Трудоёмкость",
                "объект": f"{rec.contract_id} / {rec.position or rec.labor_row_id} / {emp.full_name if emp else rec.employee_id}",
                "месяц": RU_MONTHS[rec.month],
                "описание проблемы": PAYMENT_WITHOUT_PM_MSG,
                "отклонение / сумма": round(total, 2),
                "куда смотреть": SHEET_LABOR_CONTROL,
                "рекомендация": "Связать выплату и чел.-мес. в одном месяце по строке договора",
            }
        )
    return issues


def _labor_attribution_gaps(ctx: PlanningContext, result: PlanningResult) -> list[dict]:
    """Выплаты с labor-договоров без полного отнесения на трудоёмкость."""
    from fot_planner.labor_rules import contract_has_labor_plan as has_labor

    attributed: dict[tuple[str, str, int, str], float] = defaultdict(float)
    for rec in result.labor_payment_attributions:
        key = (rec.employee_id, rec.contract_id, rec.month, rec.payment_kind)
        attributed[key] += rec.amount

    gaps: list[dict] = []
    for a in result.allocations:
        if a.amount <= 0.005 or not has_labor(ctx, a.contract_id):
            continue
        key = (a.employee_id, a.contract_id, a.month, a.payment_kind)
        attr = attributed.get(key, 0.0)
        if abs(attr - a.amount) > 0.02:
            emp = next((e for e in ctx.employees if e.id == a.employee_id), None)
            gaps.append(
                {
                    "уровень": "Ошибка",
                    "раздел": "Трудоёмкость",
                    "объект": f"{a.contract_id} / {emp.full_name if emp else a.employee_id}",
                    "месяц": RU_MONTHS[a.month],
                    "описание проблемы": "Выплата не полностью отнесена на строку трудоёмкости",
                    "отклонение / сумма": round(a.amount - attr, 2),
                    "куда смотреть": SHEET_LABOR_CONTROL,
                    "рекомендация": "Проверить совместимость должности и строки договора",
                }
            )
    return gaps


def _labor_row_issues(ctx: PlanningContext, result: PlanningResult) -> list[dict]:
    issues: list[dict] = []
    tol = ctx.salary_stability.goz_labor_tolerance
    contracts = {c.id: c for c in ctx.contracts}
    for row in _build_labor_summary_rows(ctx, result):
        if GROUP_ROW_SUFFIX in str(row["должность / категория"]):
            continue
        if row["статус"] in ("выполнено", "выполнено по группе"):
            continue
        plan_amt = float(row["плановая сумма"] or 0)
        amt_dev = abs(float(row["отклонение суммы"] or 0))
        level = "Ошибка" if plan_amt > 0 and amt_dev > tol * plan_amt + 1 else "Предупреждение"
        issues.append(
            {
                "уровень": level,
                "раздел": "Трудоёмкость",
                "объект": f"{row['договор']} / {row['должность / категория']}",
                "месяц": "год",
                "описание проблемы": "Отклонение по строке трудоёмкости",
                "отклонение / сумма": round(float(row["отклонение суммы"] or 0), 2),
                "куда смотреть": SHEET_LABOR_CONTROL,
                "рекомендация": "Проверить ФОТ договора или распределение выплат",
            }
        )
    return issues


def _split_payment_issues(ctx: PlanningContext, result: PlanningResult) -> list[dict]:
    grouped: dict[tuple[str, int, str], list] = defaultdict(list)
    employees = {e.id: e for e in ctx.employees}
    for a in result.allocations:
        if a.payment_kind not in FLEX_KINDS or a.amount <= 0.005:
            continue
        grouped[(a.employee_id, a.month, a.payment_kind)].append(a)

    issues: list[dict] = []
    for (eid, month, kind), allocs in grouped.items():
        contracts = {a.contract_id for a in allocs}
        if len(contracts) <= 1:
            continue
        emp = employees.get(eid)
        total = sum(a.amount for a in allocs)
        parts = "; ".join(
            f"{a.contract_id}: {round(a.amount, 0):,.0f}".replace(",", " ")
            for a in sorted(allocs, key=lambda x: x.contract_id)
        )
        issues.append(
            {
                "уровень": "Предупреждение",
                "раздел": "Дробление выплат",
                "объект": emp.full_name if emp else eid,
                "месяц": RU_MONTHS[month],
                "описание проблемы": f"{PAYMENT_KIND_RU[kind]} разбита между {len(contracts)} договорами",
                "отклонение / сумма": len(contracts),
                "куда смотреть": SHEET_SPLIT,
                "рекомендация": "Проверить необходимость дробления",
            }
        )
    return issues


def _cash_issues(result: PlanningResult, contracts: dict) -> list[dict]:
    issues: list[dict] = []
    for b in result.contract_balances:
        if b.closing_balance < -0.01:
            c = contracts.get(b.contract_id)
            issues.append(
                {
                    "уровень": "Ошибка",
                    "раздел": "Касса",
                    "объект": b.contract_id,
                    "месяц": RU_MONTHS[b.month],
                    "описание проблемы": "Отрицательный остаток договора",
                    "отклонение / сумма": round(b.closing_balance, 2),
                    "куда смотреть": SHEET_BALANCES,
                    "рекомендация": "Проверить поступления и выплаты по договору",
                }
            )
        if b.min_balance_required > 0.005 and b.closing_balance < b.min_balance_required - 0.01:
            issues.append(
                {
                    "уровень": "Предупреждение",
                    "раздел": "Касса",
                    "объект": b.contract_id,
                    "месяц": RU_MONTHS[b.month],
                    "описание проблемы": "Остаток ниже минимального",
                    "отклонение / сумма": round(b.closing_balance - b.min_balance_required, 2),
                    "куда смотреть": SHEET_BALANCES,
                    "рекомендация": "Проверить min_balance_matrix",
                }
            )
    return issues


def _deficit_issues(ctx: PlanningContext, result: PlanningResult) -> list[dict]:
    employees = {e.id: e for e in ctx.employees}
    issues: list[dict] = []
    for d in result.deficits:
        if d.amount <= 0.005:
            continue
        emp = employees.get(d.employee_id)
        issues.append(
            {
                "уровень": "Предупреждение",
                "раздел": "Дефицит",
                "объект": emp.full_name if emp else d.employee_id,
                "месяц": RU_MONTHS[d.month],
                "описание проблемы": "Не хватает выплаты",
                "отклонение / сумма": round(d.amount, 2),
                "куда смотреть": SHEET_DEFICITS,
                "рекомендация": "Добавить источник финансирования или пересмотреть ставку",
            }
        )
    return issues


def _conflict_issues(result: PlanningResult) -> list[dict]:
    issues: list[dict] = []
    for c in result.conflicts:
        level = "Предупреждение" if c.code.endswith("_WARNING") else "Ошибка"
        issues.append(
            {
                "уровень": level,
                "раздел": "Конфликт",
                "объект": c.contract_id or c.employee_id or "—",
                "месяц": RU_MONTHS[c.month] if c.month else "—",
                "описание проблемы": c.message,
                "отклонение / сумма": c.code,
                "куда смотреть": SHEET_PLAN,
                "рекомендация": "Исправить входные данные",
            }
        )
    return issues


def collect_issues(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    cols = [
        "уровень",
        "раздел",
        "объект",
        "месяц",
        "описание проблемы",
        "отклонение / сумма",
        "куда смотреть",
        "рекомендация",
    ]
    contracts = {c.id: c for c in ctx.contracts}
    rows: list[dict] = []
    rows.extend(_conflict_issues(result))
    rows.extend(_deficit_issues(ctx, result))
    rows.extend(_cash_issues(result, contracts))
    rows.extend(_labor_attribution_gaps(ctx, result))
    rows.extend(_labor_payment_without_pm_issues(ctx, result))
    rows.extend(_labor_row_issues(ctx, result))
    rows.extend(_split_payment_issues(ctx, result))

    if not rows:
        return pd.DataFrame(
            [
                {
                    "уровень": "ОК",
                    "раздел": "Все разделы",
                    "объект": "—",
                    "месяц": "—",
                    "описание проблемы": "Проблем и предупреждений не найдено",
                    "отклонение / сумма": 0,
                    "куда смотреть": "—",
                    "рекомендация": "—",
                }
            ]
        )
    return pd.DataFrame(rows, columns=cols)


def build_summary_sheet(
    ctx: PlanningContext, result: PlanningResult, issues_df: pd.DataFrame
) -> pd.DataFrame:
    contracts = {c.id: c for c in ctx.contracts}
    total_paid = sum(a.amount for a in result.allocations)
    total_deficit = sum(d.amount for d in result.deficits)
    def_months = [d.month for d in result.deficits if d.amount > 0.005]
    first_def_month = RU_MONTHS[min(def_months)] if def_months else "—"
    emp_with_def = len({d.employee_id for d in result.deficits if d.amount > 0.005})
    cash_problems = sum(1 for b in result.contract_balances if b.closing_balance < -0.01)

    labor_rows = _build_labor_summary_rows(ctx, result)
    labor_deviations = sum(
        1
        for row in labor_rows
        if GROUP_ROW_SUFFIX not in str(row["должность / категория"])
        and row["статус"] not in ("выполнено", "выполнено по группе")
    )

    split_issues = _split_payment_issues(ctx, result)
    scheme_rows = build_scheme_changes_sheet(ctx, result)
    scheme_count = 0 if scheme_rows.iloc[0, 0] == "Смен схемы выплат не найдено" else len(scheme_rows)

    salary_switches = sum(_count_salary_switches(result.allocations, e.id) for e in ctx.employees)

    emp_contract_links = len(
        {(a.employee_id, a.contract_id) for a in result.allocations if a.amount > 0.005}
    )

    position_df = build_position_control_sheet(ctx, result)
    incompat = 0
    if "совместимость" in position_df.columns:
        incompat = sum(1 for v in position_df["совместимость"] if str(v).lower() in ("нет", "no"))

    errors = sum(1 for _, r in issues_df.iterrows() if r.get("уровень") == "Ошибка")
    warnings = sum(1 for _, r in issues_df.iterrows() if r.get("уровень") == "Предупреждение")
    if errors:
        overall = "Ошибка"
        overall_comment = f"Найдено ошибок: {errors}, предупреждений: {warnings}"
    elif warnings:
        overall = "Предупреждение"
        overall_comment = f"Предупреждений: {warnings}"
    elif result.solver_status not in ("OPTIMAL", "FEASIBLE"):
        overall = "Ошибка"
        overall_comment = f"Решатель: {result.solver_status}"
    else:
        overall = "Выполнено"
        overall_comment = "Расчёт завершён без критичных проблем"

    def row(indicator, value, status, comment):
        return {
            "показатель": indicator,
            "значение": value,
            "статус": status,
            "комментарий": comment,
        }

    rows = [
        row("Статус решателя", result.solver_status, "", ""),
        row("Год", result.year, "", ""),
        row("Время расчёта, сек", round(result.solve_time_sec, 1), "", ""),
        row("Общий статус", overall, overall, overall_comment),
        row("Всего выплат", round(total_paid, 0), "", ""),
        row(
            "Общий дефицит",
            round(total_deficit, 0) if total_deficit > 0.005 else 0,
            "Предупреждение" if total_deficit > 0.005 else "Выполнено",
            f"Дефицит {round(total_deficit, 0):,.0f} отнесён на {first_def_month}".replace(",", " ")
            if total_deficit > 0.005
            else "Дефицит отсутствует",
        ),
        row("Первый месяц дефицита", first_def_month, "", ""),
        row("Сотрудников с дефицитом", emp_with_def, "", ""),
        row(
            "Договоров с кассовыми проблемами",
            cash_problems,
            "Ошибка" if cash_problems else "Выполнено",
            "",
        ),
        row(
            "Строк трудоёмкости с отклонением",
            labor_deviations,
            "Предупреждение" if labor_deviations else "Выполнено",
            "Смотреть лист «Контроль трудоёмкости»" if labor_deviations else "",
        ),
        row(
            "Дроблений переменных выплат",
            len(split_issues),
            "Предупреждение" if split_issues else "Выполнено",
            "Дробления не найдено" if not split_issues else "",
        ),
        row("Смен схемы выплат", scheme_count, "", ""),
        row("Смен договора оклада", salary_switches, "", ""),
        row("Связок сотрудник–договор", emp_contract_links, "", ""),
        row(
            "Несовместимых должностных назначений",
            incompat,
            "Ошибка" if incompat else "Выполнено",
            "",
        ),
    ]
    return pd.DataFrame(rows)


def build_employee_payments_matrix(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    id_cols = ["сотрудник", "ФИО", "должность", "показатель"]
    rows: list[dict] = []
    for e in ctx.employees:
        base = {"сотрудник": e.id, "ФИО": e.full_name, "должность": e.position}
        due: dict[int, float] = {}
        paid: dict[int, float] = {}
        deficit: dict[int, float] = {}
        for m in range(1, 13):
            if employee_active_in_month(e, ctx.year, m):
                due[m] = employee_monthly_payment_due(e)
            paid[m] = sum(a.amount for a in result.allocations if a.employee_id == e.id and a.month == m)
            deficit[m] = sum(d.amount for d in result.deficits if d.employee_id == e.id and d.month == m)

        for label, data in [
            ("Всего к выплате", due),
            ("Выплачено", paid),
            ("Дефицит", deficit),
        ]:
            row = {**base, "показатель": label, **_sum_matrix_row(data)}
            rows.append(row)

        sal_c = _salary_contracts_by_month(result.allocations, e.id)
        sal_amt = {
            m: sum(
                a.amount
                for a in result.allocations
                if a.employee_id == e.id and a.month == m and a.payment_kind == "salary"
            )
            for m in range(1, 13)
        }
        for label, fn in [
            ("Оклад: договор", lambda m: sal_c.get(m, "")),
            ("Оклад: сумма", lambda m: sal_amt.get(m, 0.0)),
            ("Надбавка: договор", lambda m: _flex_contracts_text(result.allocations, e.id, m, "allowance")),
            ("Надбавка: сумма", lambda m: _flex_total(result.allocations, e.id, m, "allowance")),
            (
                "Стимулирующая: договор",
                lambda m: _flex_contracts_text(result.allocations, e.id, m, "incentive"),
            ),
            ("Стимулирующая: сумма", lambda m: _flex_total(result.allocations, e.id, m, "incentive")),
            ("Количество договоров в месяце", lambda m: len(_contracts_by_month(result.allocations, e.id, m))),
        ]:
            vals = {m: fn(m) for m in range(1, 13)}
            if label.endswith("сумма") or "Количество" in label:
                row = {**base, "показатель": label, **_sum_matrix_row({m: float(vals[m] or 0) for m in range(1, 13)})}
            else:
                row = {**base, "показатель": label}
                for m in range(1, 13):
                    v = vals[m]
                    row[MONTH_SHORT[m]] = v if v != "" and v != 0 else ""
                row["Итого"] = ""
            rows.append(row)

    if not rows:
        return _empty_matrix_row(id_cols, "Нет данных о выплатах")
    cols = id_cols + MONTH_COLS
    return pd.DataFrame(rows)[cols]


def build_contract_payments_matrix(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    id_cols = ["договор", "название договора", "показатель"]
    rows: list[dict] = []
    for c in ctx.contracts:
        base = {"договор": c.id, "название договора": c.name}
        inflow = {b.month: b.inflow for b in result.contract_balances if b.contract_id == c.id}
        spent = {b.month: b.spent for b in result.contract_balances if b.contract_id == c.id}
        closing = {b.month: b.closing_balance for b in result.contract_balances if b.contract_id == c.id}
        emp_count: dict[int, int] = {}
        pay_count: dict[int, int] = {}
        for m in range(1, 13):
            month_allocs = [a for a in result.allocations if a.contract_id == c.id and a.month == m and a.amount > 0.005]
            emp_count[m] = len({a.employee_id for a in month_allocs})
            pay_count[m] = len(month_allocs)

        for label, data in [
            ("Поступление", inflow),
            ("Выплаты", spent),
            ("Остаток на конец", closing),
            ("Количество сотрудников", emp_count),
            ("Количество выплат", pay_count),
        ]:
            row = {**base, "показатель": label, **_sum_matrix_row({m: float(data.get(m, 0)) for m in range(1, 13)})}
            rows.append(row)

    cols = id_cols + MONTH_COLS
    return pd.DataFrame(rows)[cols] if rows else _empty_matrix_row(id_cols, "Нет договоров")


def build_balances_matrix(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    id_cols = ["договор", "название", "показатель"]
    rows: list[dict] = []
    contracts = {c.id: c for c in ctx.contracts}
    for c in ctx.contracts:
        base = {"договор": c.id, "название": c.name}
        bals = [b for b in result.contract_balances if b.contract_id == c.id]
        metrics = {
            "Остаток на начало": {b.month: b.opening_balance for b in bals},
            "Поступление": {b.month: b.inflow for b in bals},
            "Выплаты": {b.month: b.spent for b in bals},
            "Остаток на конец": {b.month: b.closing_balance for b in bals},
            "Минимальный остаток": {b.month: b.min_balance_required for b in bals if b.min_balance_required > 0.005},
            "Доступно сверх минимума": {
                b.month: max(0.0, b.closing_balance - b.min_balance_required) for b in bals
            },
        }
        for label, data in metrics.items():
            if not data and label.startswith("Миним"):
                continue
            row = {**base, "показатель": label, **_sum_matrix_row({m: float(data.get(m, 0)) for m in range(1, 13)})}
            rows.append(row)
    cols = id_cols + MONTH_COLS
    return pd.DataFrame(rows)[cols] if rows else _empty_matrix_row(id_cols, "Нет данных по остаткам")


def build_spend_plan_matrix(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    long_df = build_spend_plan_fact_dataframe(ctx, result)
    if long_df.empty:
        return _empty_matrix_row(["договор", "название", "показатель"], "Нет данных по освоению")

    contracts = {c.id: c for c in ctx.contracts}
    id_cols = ["договор", "название", "показатель"]
    rows: list[dict] = []
    metric_map = {
        "Равномерный план": "равномерный план",
        "Факт выплат": "факт выплат",
        "Отклонение месяца": "отклонение от равномерного плана",
        "Накопленный план": "накопленный план",
        "Накопленный факт": "накопленный факт",
        "Накопленное отклонение": "накопленное отклонение",
    }
    for c in ctx.contracts:
        sub = long_df[long_df["договор"] == c.id]
        if sub.empty:
            continue
        base = {"договор": c.id, "название": c.name}
        by_month = {int(r["месяц"]): r for _, r in sub.iterrows()}
        for label, col in metric_map.items():
            data = {}
            for m in range(1, 13):
                val = by_month.get(m, {}).get(col, "")
                data[m] = float(val) if val != "" and pd.notna(val) else 0.0
            row = {**base, "показатель": label, **_sum_matrix_row(data)}
            rows.append(row)
    cols = id_cols + MONTH_COLS
    return pd.DataFrame(rows)[cols]


LABOR_SUMMARY_COLUMNS = [
    "договор",
    "название договора",
    "должность / категория",
    "группа взаимозаменяемости",
    "план чел.-мес.",
    "факт чел.-мес.",
    "отклонение чел.-мес.",
    "плановая средняя",
    "плановая сумма",
    "фактическая сумма",
    "отклонение суммы",
    "фактическая средняя",
    "статус",
    "комментарий",
]

LABOR_BREAKDOWN_COLUMNS = [
    "договор",
    "название договора",
    "должность / категория строки",
    "группа строки",
    "месяц",
    "табельный номер",
    "ФИО",
    "должность сотрудника",
    "группа сотрудника",
    "ставка сотрудника",
    "закрыто чел.-мес.",
    "оклад с договора",
    "надбавка с договора",
    "стимулирующая с договора",
    "всего отнесено на строку",
    "плановая средняя",
    "фактическая средняя",
    "статус",
    "комментарий",
]

GROUP_ROW_SUFFIX = " (итого по группе)"
GROUP_DONE_COMMENT = (
    "По строке есть отклонение, но оно компенсировано другой строкой той же группы"
)


def _labor_avg_value(amount: float, pm: float) -> float | None:
    return amount / pm if pm > 0.01 else None


def _round_avg(value: float | None) -> float | str:
    return round(value, 2) if value is not None else ""


def _labor_row_checks(
    plan_pm: float,
    plan_amt: float,
    fact_pm: float,
    fact_amt: float,
    tol: float,
) -> dict:
    pm_dev = fact_pm - plan_pm
    amt_dev = fact_amt - plan_amt
    plan_avg = _labor_avg_value(plan_amt, plan_pm)
    fact_avg = _labor_avg_value(fact_amt, fact_pm)
    pm_ok = plan_pm <= 0 or abs(pm_dev) <= tol * plan_pm
    amount_ok = plan_amt <= 0 or abs(amt_dev) <= tol * plan_amt
    avg_ok = (
        plan_avg is None
        or fact_avg is None
        or plan_avg <= 0
        or abs(fact_avg - plan_avg) <= tol * plan_avg
    )
    return {
        "plan_pm": plan_pm,
        "fact_pm": fact_pm,
        "plan_amt": plan_amt,
        "fact_amt": fact_amt,
        "pm_dev": pm_dev,
        "amt_dev": amt_dev,
        "plan_avg": plan_avg,
        "fact_avg": fact_avg,
        "pm_ok": pm_ok,
        "amount_ok": amount_ok,
        "avg_ok": avg_ok,
        "row_ok": pm_ok and amount_ok and avg_ok,
    }


def _labor_status_from_checks(
    checks: dict,
    *,
    group_ok: bool = False,
) -> tuple[str, str]:
    if checks["row_ok"]:
        return "выполнено", "Выполнено"
    if group_ok:
        return "выполнено по группе", GROUP_DONE_COMMENT
    if checks["pm_ok"] and not checks["amount_ok"]:
        return "отклонение", "Чел.-мес. закрыты, но денег по строке не хватает"
    if not checks["pm_ok"] and checks["amount_ok"]:
        return "отклонение", "Деньги потрачены, но чел.-мес. не закрыты"
    if not checks["avg_ok"]:
        return "отклонение", "Фактическая средняя отличается от плановой"
    return "отклонение", "Отклонение по строке"


def _labor_summary_row_dict(
    *,
    contract_id: str,
    contract_name: str,
    position: str,
    group: str,
    checks: dict,
    status: str,
    comment: str,
) -> dict:
    return {
        "договор": contract_id,
        "название договора": contract_name,
        "должность / категория": position,
        "группа взаимозаменяемости": group,
        "план чел.-мес.": round(checks["plan_pm"], 4),
        "факт чел.-мес.": round(checks["fact_pm"], 4),
        "отклонение чел.-мес.": round(checks["pm_dev"], 4),
        "плановая средняя": _round_avg(checks["plan_avg"]),
        "плановая сумма": round(checks["plan_amt"], 2) if checks["plan_amt"] else "",
        "фактическая сумма": round(checks["fact_amt"], 2),
        "отклонение суммы": round(checks["amt_dev"], 2) if checks["plan_amt"] else "",
        "фактическая средняя": _round_avg(checks["fact_avg"]),
        "статус": status,
        "комментарий": comment,
    }


def _build_labor_summary_rows(ctx: PlanningContext, result: PlanningResult) -> list[dict]:
    from fot_planner.excel.export_tables import labor_by_row_dataframe

    df = labor_by_row_dataframe(ctx, result)
    if df.empty:
        return []

    contracts = {c.id: c for c in ctx.contracts}
    tol = ctx.salary_stability.goz_labor_tolerance
    row_records: list[dict] = []
    group_members: dict[tuple[str, str], list[int]] = defaultdict(list)

    for idx, r in df.iterrows():
        plan_pm = float(r["план чел.-мес."] or 0)
        plan_amt = float(r["плановая сумма по строке"] or 0)
        fact_pm = float(r["факт чел.-мес."] or 0)
        fact_amt = float(r["факт сумма по строке"] or 0)
        group = str(r["группа взаимозаменяемости"] or "")
        checks = _labor_row_checks(plan_pm, plan_amt, fact_pm, fact_amt, tol)
        c = contracts.get(r["договор"])
        row_records.append(
            {
                "contract_id": r["договор"],
                "contract_name": c.name if c else "",
                "position": r["должность"],
                "group": group,
                "checks": checks,
            }
        )
        if group:
            group_members[(r["договор"], group)].append(len(row_records) - 1)

    group_ok_map: dict[tuple[str, str], bool] = {}
    group_all_row_ok: dict[tuple[str, str], bool] = {}
    for key, indices in group_members.items():
        if len(indices) < 2:
            continue
        plan_pm = sum(row_records[i]["checks"]["plan_pm"] for i in indices)
        plan_amt = sum(row_records[i]["checks"]["plan_amt"] for i in indices)
        fact_pm = sum(row_records[i]["checks"]["fact_pm"] for i in indices)
        fact_amt = sum(row_records[i]["checks"]["fact_amt"] for i in indices)
        group_ok_map[key] = _labor_row_checks(plan_pm, plan_amt, fact_pm, fact_amt, tol)["row_ok"]
        group_all_row_ok[key] = all(row_records[i]["checks"]["row_ok"] for i in indices)

    rows: list[dict] = []
    group_rows_added: set[tuple[str, str]] = set()
    current_contract: str | None = None

    for rec in row_records:
        if current_contract is not None and rec["contract_id"] != current_contract:
            for (cid, group), ok in sorted(group_ok_map.items()):
                if cid != current_contract or (cid, group) in group_rows_added or len(group_members[(cid, group)]) < 2:
                    continue
                indices = group_members[(cid, group)]
                plan_pm = sum(row_records[i]["checks"]["plan_pm"] for i in indices)
                plan_amt = sum(row_records[i]["checks"]["plan_amt"] for i in indices)
                fact_pm = sum(row_records[i]["checks"]["fact_pm"] for i in indices)
                fact_amt = sum(row_records[i]["checks"]["fact_amt"] for i in indices)
                checks = _labor_row_checks(plan_pm, plan_amt, fact_pm, fact_amt, tol)
                status, comment = _labor_status_from_checks(checks)
                rows.append(
                    _labor_summary_row_dict(
                        contract_id=cid,
                        contract_name=row_records[indices[0]]["contract_name"],
                        position=f"{group}{GROUP_ROW_SUFFIX}",
                        group=group,
                        checks=checks,
                        status=status,
                        comment=comment,
                    )
                )
                group_rows_added.add((cid, group))

        current_contract = rec["contract_id"]
        group_key = (rec["contract_id"], rec["group"]) if rec["group"] else None
        if (
            group_key
            and group_ok_map.get(group_key, False)
            and not group_all_row_ok.get(group_key, True)
        ):
            status, comment = "выполнено по группе", GROUP_DONE_COMMENT
        else:
            status, comment = _labor_status_from_checks(rec["checks"])
        rows.append(
            _labor_summary_row_dict(
                contract_id=rec["contract_id"],
                contract_name=rec["contract_name"],
                position=rec["position"],
                group=rec["group"],
                checks=rec["checks"],
                status=status,
                comment=comment,
            )
        )

    if current_contract is not None:
        for (cid, group), ok in sorted(group_ok_map.items()):
            if cid != current_contract or (cid, group) in group_rows_added or len(group_members[(cid, group)]) < 2:
                continue
            indices = group_members[(cid, group)]
            plan_pm = sum(row_records[i]["checks"]["plan_pm"] for i in indices)
            plan_amt = sum(row_records[i]["checks"]["plan_amt"] for i in indices)
            fact_pm = sum(row_records[i]["checks"]["fact_pm"] for i in indices)
            fact_amt = sum(row_records[i]["checks"]["fact_amt"] for i in indices)
            checks = _labor_row_checks(plan_pm, plan_amt, fact_pm, fact_amt, tol)
            status, comment = _labor_status_from_checks(checks)
            rows.append(
                _labor_summary_row_dict(
                    contract_id=cid,
                    contract_name=row_records[indices[0]]["contract_name"],
                    position=f"{group}{GROUP_ROW_SUFFIX}",
                    group=group,
                    checks=checks,
                    status=status,
                    comment=comment,
                )
            )

    return rows


def build_labor_summary_block(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    rows = _build_labor_summary_rows(ctx, result)
    if not rows:
        return pd.DataFrame(columns=LABOR_SUMMARY_COLUMNS)
    return pd.DataFrame(rows, columns=LABOR_SUMMARY_COLUMNS)


def _attributed_on_contract(
    result: PlanningResult, employee_id: str, contract_id: str, month: int, kind: str
) -> float:
    return sum(
        rec.amount
        for rec in result.labor_payment_attributions
        if rec.employee_id == employee_id
        and rec.contract_id == contract_id
        and rec.month == month
        and rec.payment_kind == kind
    )


def _paid_on_contract(
    result: PlanningResult, employee_id: str, contract_id: str, month: int, kind: str
) -> float:
    return sum(
        a.amount
        for a in result.allocations
        if a.employee_id == employee_id
        and a.contract_id == contract_id
        and a.month == month
        and a.payment_kind == kind
        and a.amount > 0.005
    )


def _detail_attribution_status(
    ctx: PlanningContext,
    result: PlanningResult,
    employee_id: str,
    contract_id: str,
    month: int,
    *,
    labor_row_id: str = "",
    pm_on_row: float = 0.0,
) -> tuple[str, str]:
    if labor_row_id:
        total_on_row = _payment_on_labor_row(
            result, employee_id, contract_id, month, labor_row_id
        )
        if total_on_row > 0.005 and pm_on_row <= 0.005:
            return (
                PAYMENT_WITHOUT_PM_MSG,
                f"Отнесено {round(total_on_row, 0):,.0f} ₽, закрыто 0 чел.-мес.".replace(",", " "),
            )
    if not contract_has_labor_plan(ctx, contract_id):
        return "ОК", ""
    for kind in ("salary", "allowance", "incentive"):
        paid = _paid_on_contract(result, employee_id, contract_id, month, kind)
        if paid <= 0.005:
            continue
        attr = _attributed_on_contract(result, employee_id, contract_id, month, kind)
        if paid > attr + 0.02:
            label = PAYMENT_KIND_RU[kind]
            return (
                "выплата не полностью отнесена на трудоёмкость",
                f"{label}: выплачено {round(paid, 0):,.0f}, отнесено {round(attr, 0):,.0f}".replace(
                    ",", " "
                ),
            )
    return "ОК", ""


def build_labor_breakdown_sheet(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    """Детализация трудоёмкости: сотрудник × месяц × строка договора + строки ИТОГО."""
    employees = {e.id: e for e in ctx.employees}
    contracts = {c.id: c for c in ctx.contracts}
    tol = ctx.salary_stability.goz_labor_tolerance

    pm_by: dict[tuple[str, str, int], float] = defaultdict(float)
    for rec in result.labor_pm_attributions:
        pm_by[(rec.labor_row_id, rec.employee_id, rec.month)] += rec.person_months

    pay_by: dict[tuple[str, str, int, str], float] = defaultdict(float)
    for rec in result.labor_payment_attributions:
        pay_by[(rec.labor_row_id, rec.employee_id, rec.month, rec.payment_kind)] += rec.amount

    row_specs: list[dict] = []
    group_members: dict[tuple[str, str], list[int]] = defaultdict(list)

    for contract in ctx.contracts:
        for _lp_idx, lp in labor_rows_for_contract(ctx, contract.id):
            row_id = labor_row_id(lp)
            group = lp.equivalence_group or ""
            plan_pm = lp.person_months
            plan_amount = planned_labor_amount(lp)
            rec_idx = len(row_specs)
            row_specs.append(
                {
                    "contract": contract,
                    "lp": lp,
                    "row_id": row_id,
                    "group": group,
                    "plan_pm": plan_pm,
                    "plan_amount": plan_amount,
                    "plan_avg": _labor_avg_value(plan_amount, plan_pm),
                }
            )
            if group:
                group_members[(contract.id, group)].append(rec_idx)

    for rec_idx, spec in enumerate(row_specs):
        row_id = spec["row_id"]
        detail_keys = {
            (row_id, e_id, month)
            for (rid, e_id, month) in pm_by
            if rid == row_id and pm_by[(rid, e_id, month)] > 0.005
        }
        detail_keys |= {
            (row_id, e_id, month)
            for (rid, e_id, month, _kind) in pay_by
            if rid == row_id and pay_by[(rid, e_id, month, _kind)] > 0.005
        }
        total_pm = 0.0
        total_amount = 0.0
        total_salary = 0.0
        total_allowance = 0.0
        total_incentive = 0.0
        for (_rid, e_id, month) in detail_keys:
            total_pm += pm_by.get((row_id, e_id, month), 0.0)
            total_salary += pay_by.get((row_id, e_id, month, "salary"), 0.0)
            total_allowance += pay_by.get((row_id, e_id, month, "allowance"), 0.0)
            total_incentive += pay_by.get((row_id, e_id, month, "incentive"), 0.0)
        total_amount = total_salary + total_allowance + total_incentive
        spec["detail_keys"] = detail_keys
        spec["checks"] = _labor_row_checks(
            spec["plan_pm"], spec["plan_amount"], total_pm, total_amount, tol
        )
        spec["total_salary"] = total_salary
        spec["total_allowance"] = total_allowance
        spec["total_incentive"] = total_incentive

    group_ok_map: dict[tuple[str, str], bool] = {}
    group_all_row_ok: dict[tuple[str, str], bool] = {}
    for key, indices in group_members.items():
        if len(indices) < 2:
            continue
        plan_pm = sum(row_specs[i]["checks"]["plan_pm"] for i in indices)
        plan_amt = sum(row_specs[i]["checks"]["plan_amt"] for i in indices)
        fact_pm = sum(row_specs[i]["checks"]["fact_pm"] for i in indices)
        fact_amt = sum(row_specs[i]["checks"]["fact_amt"] for i in indices)
        group_ok_map[key] = _labor_row_checks(plan_pm, plan_amt, fact_pm, fact_amt, tol)["row_ok"]
        group_all_row_ok[key] = all(row_specs[i]["checks"]["row_ok"] for i in indices)

    rows: list[dict] = []
    for spec in row_specs:
        contract = spec["contract"]
        lp = spec["lp"]
        row_id = spec["row_id"]
        pos = lp.position or ""
        group = spec["group"]
        c = contracts.get(contract.id)

        for _rid, e_id, month in sorted(spec["detail_keys"], key=lambda x: (x[2], x[1])):
            pm = pm_by.get((row_id, e_id, month), 0.0)
            salary = pay_by.get((row_id, e_id, month, "salary"), 0.0)
            allowance = pay_by.get((row_id, e_id, month, "allowance"), 0.0)
            incentive = pay_by.get((row_id, e_id, month, "incentive"), 0.0)
            total = salary + allowance + incentive
            if pm <= 0.005 and total <= 0.005:
                continue

            emp = employees.get(e_id)
            status, comment = _detail_attribution_status(
                ctx,
                result,
                e_id,
                contract.id,
                month,
                labor_row_id=row_id,
                pm_on_row=pm,
            )
            rows.append(
                {
                    "договор": contract.id,
                    "название договора": c.name if c else "",
                    "должность / категория строки": pos,
                    "группа строки": group,
                    "месяц": RU_MONTHS[month],
                    "табельный номер": e_id,
                    "ФИО": emp.full_name if emp else "",
                    "должность сотрудника": emp.position if emp else "",
                    "группа сотрудника": emp.equivalence_group if emp else "",
                    "ставка сотрудника": emp.rate if emp else "",
                    "закрыто чел.-мес.": round(pm, 4),
                    "оклад с договора": round(salary, 2) if salary > 0.005 else "",
                    "надбавка с договора": round(allowance, 2) if allowance > 0.005 else "",
                    "стимулирующая с договора": round(incentive, 2) if incentive > 0.005 else "",
                    "всего отнесено на строку": round(total, 2),
                    "плановая средняя": "",
                    "фактическая средняя": "",
                    "статус": status,
                    "комментарий": comment,
                }
            )

        checks = spec["checks"]
        group_key = (contract.id, group) if group else None
        if (
            group_key
            and group_ok_map.get(group_key, False)
            and not group_all_row_ok.get(group_key, True)
        ):
            status, comment = "выполнено по группе", GROUP_DONE_COMMENT
        else:
            status, comment = _labor_status_from_checks(checks)
        if spec["detail_keys"] or spec["plan_pm"] > 0:
            rows.append(
                {
                    "договор": contract.id,
                    "название договора": c.name if c else "",
                    "должность / категория строки": pos,
                    "группа строки": group,
                    "месяц": "ИТОГО",
                    "табельный номер": "",
                    "ФИО": "",
                    "должность сотрудника": "",
                    "группа сотрудника": "",
                    "ставка сотрудника": "",
                    "закрыто чел.-мес.": round(checks["fact_pm"], 4),
                    "оклад с договора": round(spec["total_salary"], 2) if spec["total_salary"] > 0.005 else "",
                    "надбавка с договора": round(spec["total_allowance"], 2) if spec["total_allowance"] > 0.005 else "",
                    "стимулирующая с договора": round(spec["total_incentive"], 2) if spec["total_incentive"] > 0.005 else "",
                    "всего отнесено на строку": round(checks["fact_amt"], 2),
                    "плановая средняя": _round_avg(spec["plan_avg"]),
                    "фактическая средняя": _round_avg(checks["fact_avg"]),
                    "статус": status,
                    "комментарий": comment,
                }
            )

    if not rows:
        return pd.DataFrame([{"договор": "Нет строк трудоёмкости для расшифровки"}])
    return pd.DataFrame(rows, columns=LABOR_BREAKDOWN_COLUMNS)


def build_plan_payments_sheet(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    employees = {e.id: e for e in ctx.employees}
    contracts = {c.id: c for c in ctx.contracts}
    attributed: dict[tuple[str, str, int, str], float] = defaultdict(float)
    for rec in result.labor_payment_attributions:
        key = (rec.employee_id, rec.contract_id, rec.month, rec.payment_kind)
        attributed[key] += rec.amount

    deficit_keys = {(d.employee_id, d.month) for d in result.deficits if d.amount > 0.005}

    rows: list[dict] = []
    for a in result.allocations:
        if a.amount <= 0.005:
            continue
        emp = employees.get(a.employee_id)
        c = contracts.get(a.contract_id)
        status = "ОК"
        if (a.employee_id, a.month) in deficit_keys:
            status = "дефицит"
        elif a.payment_kind in FLEX_KINDS and a.amount < MIN_FLEX_FRAGMENT_AMOUNT - 0.01:
            status = "ниже минимального фрагмента"
        elif contract_has_labor_plan(ctx, a.contract_id):
            key = (a.employee_id, a.contract_id, a.month, a.payment_kind)
            if abs(attributed.get(key, 0.0) - a.amount) > 0.02:
                status = "не отнесено на трудоёмкость"

        rows.append(
            {
                "табельный номер": a.employee_id,
                "ФИО": emp.full_name if emp else "",
                "должность": emp.position if emp else "",
                "группа должности": emp.equivalence_group if emp else "",
                "год": a.year,
                "месяц": RU_MONTHS[a.month],
                "договор": a.contract_id,
                "название договора": c.name if c else "",
                "вид выплаты": PAYMENT_KIND_RU.get(a.payment_kind, a.payment_kind),
                "сумма": round(a.amount, 2),
                "источник решения": a.source,
                "зафиксировано": "да" if a.is_manual else "нет",
                "статус": status,
            }
        )
    return pd.DataFrame(rows)


def build_labor_payments_sheet(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    employees = {e.id: e for e in ctx.employees}
    contracts = {c.id: c for c in ctx.contracts}
    payment_totals: dict[tuple[str, str, int, str], float] = defaultdict(float)
    for a in result.allocations:
        if a.amount > 0:
            payment_totals[(a.employee_id, a.contract_id, a.month, a.payment_kind)] += a.amount

    pm_by_key: dict[tuple[str, str, int, str], float] = defaultdict(float)
    for rec in result.labor_pm_attributions:
        pm_by_key[(rec.employee_id, rec.contract_id, rec.month, rec.labor_row_id)] += rec.person_months

    rows: list[dict] = []
    for rec in result.labor_payment_attributions:
        emp = employees.get(rec.employee_id)
        c = contracts.get(rec.contract_id)
        total = payment_totals.get(
            (rec.employee_id, rec.contract_id, rec.month, rec.payment_kind), 0.0
        )
        attr = rec.amount
        if contract_has_labor_plan(ctx, rec.contract_id):
            if abs(attr - total) < 0.02:
                st = "ОК"
            elif attr > 0.005:
                st = "выплата отнесена частично"
            else:
                st = "выплата не отнесена"
        else:
            st = "ОК"
        pm = pm_by_key.get((rec.employee_id, rec.contract_id, rec.month, rec.labor_row_id), 0.0)
        rows.append(
            {
                "табельный номер": rec.employee_id,
                "ФИО": emp.full_name if emp else "",
                "договор": rec.contract_id,
                "название договора": c.name if c else "",
                "месяц": RU_MONTHS[rec.month],
                "вид выплаты": PAYMENT_KIND_RU.get(rec.payment_kind, rec.payment_kind),
                "выплачено всего": round(total, 2),
                "сумма, отнесенная на трудоёмкость": round(attr, 2),
                "должность строки": rec.position or "",
                "группа строки": rec.equivalence_group or "",
                "чел.-мес., отнесенные на строку": round(pm, 4),
                "статус": st,
            }
        )

    # выплаты без отнесения
    attributed_sum: dict[tuple[str, str, int, str], float] = defaultdict(float)
    for rec in result.labor_payment_attributions:
        key = (rec.employee_id, rec.contract_id, rec.month, rec.payment_kind)
        attributed_sum[key] += rec.amount
    for a in result.allocations:
        if a.amount <= 0.005 or not contract_has_labor_plan(ctx, a.contract_id):
            continue
        key = (a.employee_id, a.contract_id, a.month, a.payment_kind)
        if attributed_sum.get(key, 0.0) < a.amount - 0.02:
            emp = employees.get(a.employee_id)
            c = contracts.get(a.contract_id)
            rows.append(
                {
                    "табельный номер": a.employee_id,
                    "ФИО": emp.full_name if emp else "",
                    "договор": a.contract_id,
                    "название договора": c.name if c else "",
                    "месяц": RU_MONTHS[a.month],
                    "вид выплаты": PAYMENT_KIND_RU.get(a.payment_kind, a.payment_kind),
                    "выплачено всего": round(a.amount, 2),
                    "сумма, отнесенная на трудоёмкость": round(attributed_sum.get(key, 0.0), 2),
                    "должность строки": "",
                    "группа строки": "",
                    "чел.-мес., отнесенные на строку": "",
                    "статус": "нет совместимой строки"
                    if not any(
                        employee_compatible_with_labor_row(emp, lp)
                        for _i, lp in labor_rows_for_contract(ctx, a.contract_id)
                    )
                    else "выплата не отнесена",
                }
            )

    if not rows:
        return _single_message("Нет отнесений на трудоёмкость")
    return pd.DataFrame(rows)


def build_position_control_sheet(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    from fot_planner.excel.export_tables import position_control_dataframe

    df = position_control_dataframe(ctx, result)
    if df.empty:
        return _single_message("Нет назначений для контроля")
    rename = {
        "сотрудник": "табельный номер",
        "должность сотрудника": "должность сотрудника",
        "причина совместимости / несовместимости": "причина",
    }
    out = df.rename(columns=rename)
    if "причина" not in out.columns:
        out["причина совместимости / несовместимости"] = out["совместимость"].map(
            lambda x: "группа должностей совпадает" if str(x).lower() in ("да", "yes") else "нет подходящей строки"
        )
    return out


def build_admin_sheet(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    from fot_planner.excel.reports.admin_complexity import build_admin_complexity_dataframe

    summary, scheme, _fragments = build_admin_complexity_dataframe(ctx, result)
    if summary.empty:
        return _single_message("Нет данных по административной сложности")

    split_count: dict[str, int] = defaultdict(int)
    for a in result.allocations:
        if a.payment_kind in FLEX_KINDS and a.amount > 0.005:
            split_count[(a.employee_id, a.month, a.payment_kind)] += 1

    rows: list[dict] = []
    for _, r in summary.iterrows():
        eid = r["табельный номер"]
        splits = sum(
            1
            for (emp, _m, _k), cnt in split_count.items()
            if emp == eid and cnt > 1
        )
        rows.append(
            {
                "табельный номер": eid,
                "ФИО": r["сотрудник"],
                "количество договоров за год": r["договоров за год"],
                "список договоров": r["договоры"],
                "количество смен схемы выплат": r["смен схемы за год"],
                "количество смен договора оклада": _count_salary_switches(result.allocations, eid),
                "количество дроблений переменных выплат": splits,
                "количество переменных выплат": r.get("фрагментов allowance/incentive", 0),
                "оценка сложности": "высокая"
                if r["договоров за год"] > 2 or r["смен схемы за год"] > 2
                else "нормальная",
                "комментарий": "",
            }
        )
    note = (
        "Чем меньше договоров и смен схемы, тем проще план для сопровождения. "
        "Смена схемы — изменение набора договоров сотрудника между соседними месяцами. "
        "Дробление — когда одна надбавка или стимулирующая в месяце выплачена с нескольких договоров."
    )
    rows.insert(
        0,
        {"табельный номер": "—", "ФИО": note, "комментарий": "Пояснение к листу"},
    )
    return pd.DataFrame(rows)


def build_split_check_sheet(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    employees = {e.id: e for e in ctx.employees}
    grouped: dict[tuple[str, int, str], list] = defaultdict(list)
    for a in result.allocations:
        if a.payment_kind in FLEX_KINDS and a.amount > 0.005:
            grouped[(a.employee_id, a.month, a.payment_kind)].append(a)

    rows: list[dict] = []
    for (eid, month, kind), allocs in sorted(grouped.items()):
        emp = employees.get(eid)
        contracts = {a.contract_id for a in allocs}
        total = sum(a.amount for a in allocs)
        parts = "; ".join(
            f"{a.contract_id}: {round(a.amount, 0):,.0f}".replace(",", " ")
            for a in sorted(allocs, key=lambda x: x.contract_id)
        )
        min_frag = min(a.amount for a in allocs)
        if len(contracts) > 1:
            st, comment = "предупреждение", "Выплата разделена между договорами"
        else:
            st, comment = "ОК", "Выплата не дробится"
        rows.append(
            {
                "табельный номер": eid,
                "ФИО": emp.full_name if emp else "",
                "месяц": RU_MONTHS[month],
                "вид выплаты": PAYMENT_KIND_RU[kind],
                "общая сумма выплаты": round(total, 2),
                "количество договоров": len(contracts),
                "договоры и суммы": parts,
                "минимальный фрагмент": round(min_frag, 2),
                "статус": st,
                "комментарий": comment,
            }
        )

    if not rows:
        return _single_message("Дробления переменных выплат не найдено")
    if not any(r["статус"] == "предупреждение" for r in rows):
        return pd.DataFrame(
            [
                {
                    "табельный номер": "ОК",
                    "ФИО": "Дробления переменных выплат не найдено",
                    "месяц": "",
                    "вид выплаты": "",
                    "общая сумма выплаты": "",
                    "количество договоров": "",
                    "договоры и суммы": "",
                    "минимальный фрагмент": "",
                    "статус": "ОК",
                    "комментарий": "",
                }
            ]
        )
    return pd.DataFrame(rows)


def build_scheme_changes_sheet(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    from fot_planner.excel.reports.admin_complexity import build_admin_complexity_dataframe

    _summary, scheme, _f = build_admin_complexity_dataframe(ctx, result)
    if scheme.empty:
        return _single_message("Смен схемы выплат не найдено")

    rows: list[dict] = []
    for _, r in scheme.iterrows():
        prev = set(str(r["договоры было"]).replace("—", "").split("; "))
        curr = set(str(r["договоры стало"]).replace("—", "").split("; "))
        prev.discard("")
        curr.discard("")
        added = curr - prev
        removed = prev - curr
        if added and removed:
            change_type = "замена договора"
        elif added:
            change_type = "добавился договор"
        elif removed:
            change_type = "ушел договор"
        else:
            change_type = "изменение вида выплаты"
        comment = ""
        if prev & curr and (added or removed):
            comment = "Частичное изменение набора договоров"
        rows.append(
            {
                "табельный номер": r["табельный номер"],
                "ФИО": r["сотрудник"],
                "месяц изменения": r["месяц"],
                "схема до": r["договоры было"],
                "схема после": r["договоры стало"],
                "что изменилось": f"+{','.join(sorted(added))} -{','.join(sorted(removed))}".strip(),
                "тип изменения": change_type,
                "комментарий": comment,
            }
        )
    return pd.DataFrame(rows)


def build_deficits_sheet(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    from fot_planner.excel.reports.deficit import build_deficits_detail_dataframe

    df = build_deficits_detail_dataframe(ctx, result)
    if df.empty:
        return _single_message("Дефицитов нет")

    first_by_emp: dict[str, int] = {}
    for d in sorted(result.deficits, key=lambda x: (x.employee_id, x.month)):
        if d.amount > 0.005 and d.employee_id not in first_by_emp:
            first_by_emp[d.employee_id] = d.month

    rows: list[dict] = []
    for _, r in df.iterrows():
        eid = r["табельный номер"]
        fm = first_by_emp.get(eid)
        rows.append(
            {
                "табельный номер": eid,
                "ФИО": r["сотрудник"],
                "месяц": r["месяц"],
                "требовалось выплатить": r["требовалось выплатить"],
                "выплачено": r["выплачено"],
                "дефицит": r["дефицит"],
                "первый месяц дефицита по сотруднику": RU_MONTHS.get(fm, "") if fm else "",
                "причина": r.get("причина / комментарий", ""),
                "рекомендация": "Добавить договор / финансирование",
            }
        )
    return pd.DataFrame(rows)


def build_deficit_by_month_matrix(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    need: dict[int, float] = {}
    paid: dict[int, float] = {}
    deficit: dict[int, float] = {}
    cum = 0.0
    cum_def: dict[int, float] = {}
    for m in range(1, 13):
        total_need = sum(
            employee_monthly_payment_due(e)
            for e in ctx.employees
            if employee_active_in_month(e, ctx.year, m)
        )
        need[m] = total_need
        paid[m] = sum(a.amount for a in result.allocations if a.month == m)
        deficit[m] = sum(d.amount for d in result.deficits if d.month == m)
        cum += deficit[m]
        cum_def[m] = cum

    rows = []
    for label, data in [
        ("Потребность к выплате", need),
        ("Выплачено", paid),
        ("Дефицит", deficit),
        ("Накопленный дефицит", cum_def),
    ]:
        row = {"показатель": label, **_sum_matrix_row(data)}
        rows.append(row)
    return pd.DataFrame(rows)[["показатель"] + MONTH_COLS]


@dataclass
class UserExcelReport:
    readme: pd.DataFrame
    summary: pd.DataFrame
    issues: pd.DataFrame
    employee_payments: pd.DataFrame
    contract_payments: pd.DataFrame
    balances: pd.DataFrame
    spend_plan: pd.DataFrame
    plan: pd.DataFrame
    labor_payments: pd.DataFrame
    position_control: pd.DataFrame
    admin: pd.DataFrame
    split_check: pd.DataFrame
    scheme_changes: pd.DataFrame
    deficits: pd.DataFrame
    deficit_by_month: pd.DataFrame


def build_user_excel_report(ctx: PlanningContext, result: PlanningResult) -> UserExcelReport:
    issues = collect_issues(ctx, result)
    return UserExcelReport(
        readme=build_readme_sheet(),
        summary=build_summary_sheet(ctx, result, issues),
        issues=issues,
        employee_payments=build_employee_payments_matrix(ctx, result),
        contract_payments=build_contract_payments_matrix(ctx, result),
        balances=build_balances_matrix(ctx, result),
        spend_plan=build_spend_plan_matrix(ctx, result),
        plan=build_plan_payments_sheet(ctx, result),
        labor_payments=build_labor_payments_sheet(ctx, result),
        position_control=build_position_control_sheet(ctx, result),
        admin=build_admin_sheet(ctx, result),
        split_check=build_split_check_sheet(ctx, result),
        scheme_changes=build_scheme_changes_sheet(ctx, result),
        deficits=build_deficits_sheet(ctx, result),
        deficit_by_month=build_deficit_by_month_matrix(ctx, result),
    )
