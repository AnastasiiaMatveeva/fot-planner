"""Валидация входных данных перед расчётом."""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from fot_planner.actuals import MAX_FROZEN_THROUGH, month_span
from fot_planner.contract_calendar import (
    payment_deadline_date,
    payment_kind_enabled,
    payment_month_count,
)
from fot_planner.models import (
    MAX_LABOR_TOLERANCE,
    ConflictRecord,
    PAYMENT_KINDS,
    PaymentKind,
    PlanningContext,
)
from fot_planner.payment_split import employee_monthly_payment_due
from fot_planner.open_rate_rules import (
    MAIN_QUARTERS_MAX,
    PART_QUARTERS_MAX,
    keeps_staff_rate_without_opening_extra,
    max_total_quarters,
    quarters_to_rate,
    staff_rate_min_quarters,
)
from fot_planner.salary_limits_2556 import p4_applies_to_category

_DEADLINE_LABELS = {
    "salary": "оклада",
    "flex": "надбавок",
}


def _defaults_used_conflicts(ctx: PlanningContext) -> list[ConflictRecord]:
    """Величины, взятые не из файла, а из встроенного справочника.

    Без встроенных умолчаний пустой шаблон не считается, поэтому убирать их
    нельзя. Но подстановка не должна быть молчаливой: в файле пусто, в расчете
    значение есть, и по самому файлу этого не увидеть. Для ГОЗ вопрос «откуда
    эта цифра» — первый, и ответ должен быть в отчете, а не в исходниках.

    Собираем по должности, а не по величине: тридцать три строки «оклад по
    умолчанию» — это шум, из которого ничего не вычитаешь. Код кончается на
    «_WARNING»: отчет по этому суффиксу отличает предупреждение от ошибки, а
    расчет такая подстановка не срывает.
    """
    if not ctx.defaults_used:
        return []
    by_position: dict[str, list[str]] = {}
    for position, title, _value in ctx.defaults_used:
        by_position.setdefault(position, [])
        if title not in by_position[position]:
            by_position[position].append(title)
    out: list[ConflictRecord] = []
    for position, titles in sorted(by_position.items()):
        if not position:
            out.append(
                ConflictRecord(
                    code="DEFAULT_REFERENCE_USED_WARNING",
                    message=(
                        "Листа «лимиты_по_должностям» во входном файле нет — "
                        "справочник должностей взят встроенный."
                    ),
                )
            )
            continue
        out.append(
            ConflictRecord(
                code="DEFAULT_VALUE_USED_WARNING",
                message=(
                    f"Должность «{position}»: во входном файле не задано — "
                    f"{', '.join(titles)}. Значение взято из встроенного "
                    f"справочника; проверьте, что оно действующее."
                ),
            )
        )
    return out


def _labor_rows_by_group_only(ctx: PlanningContext) -> list[ConflictRecord]:
    """Строки трудоёмкости, заданные одной окладной группой без должности.

    Окладная группа больше не решает, кто кого заменяет: группировка из
    положения об оплате труда устарела, взаимозаменяемость задают правила
    замещения. Значит такую строку теперь не закроет никто, и молчать об этом
    нельзя — раньше она работала.
    """
    out: list[ConflictRecord] = []
    for lp in ctx.labor_plans:
        if lp.position or not lp.equivalence_group:
            continue
        out.append(
            ConflictRecord(
                code="LABOR_ROW_GROUP_ONLY_WARNING",
                message=(
                    f"Строка трудоёмкости задана окладной группой "
                    f"«{lp.equivalence_group}» без должности. Окладная группа "
                    f"в подборе сотрудников больше не участвует — укажите "
                    f"должность или заведите правило замещения."
                ),
                contract_id=lp.contract_id,
            )
        )
    return out


def _person_key(employee) -> str:
    person_id = str(getattr(employee, "person_id", "") or "").strip().lower()
    if person_id:
        return person_id
    name = str(getattr(employee, "full_name", "") or "").strip().lower()
    return " ".join(name.split()) or employee.id


def _employment_structure_conflicts(ctx: PlanningContext) -> list[ConflictRecord]:
    """У человека одно основное место и не больше двух совместительств.

    Совместительства без основного не бывает: внешний совместитель в модели
    не предусмотрен, основное место у каждого здесь. Строки одного человека
    узнаются по табельному номеру; ФИО используется только для старых файлов,
    в которых отдельного номера ещё нет.
    """
    by_person: dict[str, list] = {}
    for e in ctx.employees:
        by_person.setdefault(_person_key(e), []).append(e)
    out: list[ConflictRecord] = []
    # The model operates by month. Consecutive appointments represent changes
    # in employment conditions, not simultaneous additional jobs.
    for rows, month in (
        (rows, month) for rows in by_person.values() for month in range(1, 13)
    ):
        active_rows = [e for e in rows if employee_active_in_month(e, ctx.year, month)]
        mains = [e for e in active_rows if e.employment_type != "part_time"]
        parts = [e for e in active_rows if e.employment_type == "part_time"]
        if parts and not mains:
            for e in parts:
                out.append(ConflictRecord(
                    code="PART_TIME_WITHOUT_MAIN",
                    message=(f"Сотрудник {e.id}, месяц {month}: совместительство без основного места — "
                             "у каждого сотрудника должна быть строка основного места"),
                    employee_id=e.id,
                ))
        if len(mains) > 1:
            for e in mains[1:]:
                out.append(ConflictRecord(
                    code="SECOND_MAIN_ROW",
                    message=f"Сотрудник {e.id}, месяц {month}: второе основное место у одного человека",
                    employee_id=e.id,
                ))
        if len(parts) > 2:
            for e in parts[2:]:
                out.append(ConflictRecord(
                    code="TOO_MANY_PART_TIME",
                    message=f"Сотрудник {e.id}, месяц {month}: третье совместительство — допускается не больше двух",
                    employee_id=e.id,
                ))
    return out


def _actuals_conflicts(ctx: PlanningContext) -> list[ConflictRecord]:
    """Что из листа «факт_выплат» расчёт не учёл или учёл по догадке.

    Пересчёт с факта (правило 26, владелец продукта 30.09.2026) держит
    месяцы 1…F ровно по факту. Строка, которую расчёт отбросил или прочитал
    не так, как записана, не должна пропадать молча: экономист увидит план
    без неё и решит, что факт учтён. Всё здесь — предупреждения: история
    расчёт не останавливает; ошибка только в самом «факт по месяц».
    """
    raw = getattr(ctx, "frozen_through", 0) or 0
    try:
        last = int(raw)
    except (TypeError, ValueError):
        last = -1
    if not 0 <= last <= MAX_FROZEN_THROUGH:
        return [ConflictRecord(
            code="INVALID_FROZEN_MONTHS",
            message=(f"«Факт по месяц» {raw!r}: нужно целое от 0 до "
                     f"{MAX_FROZEN_THROUGH} — последний прошедший месяц"),
        )]
    facts = list(getattr(ctx, "actuals", None) or [])
    if not facts:
        if not last:
            return []
        # Месяцы 1…F расчёт не считает, а заполнить их нечем: план покажет
        # прошедшие месяцы пустыми, и об этом надо сказать, а не молчать.
        return [ConflictRecord(
            code="FACT_MISSING_WARNING",
            message=(f"«Факт по месяц» — {month_span([last])}, а листа «факт_выплат» во "
                     f"входе нет или он пуст: в плане {month_span(range(1, last + 1))} без выплат"),
        )]
    employees = {e.id: e for e in ctx.employees}
    contract_ids = {c.id for c in ctx.contracts}
    out: list[ConflictRecord] = []
    # Строка работала и получала зарплату, а в факте её месяца нет: скорее
    # всего, выгрузка факта неполная. Расчёт этот месяц не восполнит — он
    # считает только месяцы после F, — поэтому предупреждение.
    paid = {(a.employee_id, a.month) for a in facts
            if 1 <= a.month <= last and a.contract_id in contract_ids}
    for e in ctx.employees:
        empty = [m for m in range(1, last + 1)
                 if employee_active_in_month(e, ctx.year, m)
                 and employee_monthly_payment_due(e, m) > 0 and (e.id, m) not in paid]
        if empty:
            out.append(ConflictRecord(
                code="FACT_MISSING_WARNING",
                message=(f"Сотрудник {e.id} работал, а в факте выплат за "
                         f"{month_span(empty)} его нет: в плане эти месяцы у него без выплат"),
                employee_id=e.id, month=empty[0],
            ))
    later = sorted({a.month for a in facts if a.month > last})
    if later:
        why = (f"последний месяц факта — {month_span([last])}" if last
               else "в настройках не задан «факт по месяц»")
        out.append(ConflictRecord(
            code="FACT_AFTER_FROZEN_WARNING",
            message=(f"Факт выплат за {month_span(later)} не учтён: {why}; "
                     f"эти месяцы считает расчёт"),
            month=later[0],
        ))
    held = [a for a in facts if 1 <= a.month <= last]
    for cid in sorted({a.contract_id for a in held} - contract_ids):
        months = [a.month for a in held if a.contract_id == cid]
        out.append(ConflictRecord(
            code="FACT_UNKNOWN_CONTRACT_WARNING",
            message=(f"Факт выплат за {month_span(months)} по договору {cid}, которого "
                     f"нет в листе договоров, не учтён"),
            contract_id=cid, month=min(months),
        ))
    held = [a for a in held if a.contract_id in contract_ids]
    for eid in sorted({a.employee_id for a in held} - set(employees)):
        rows = [a for a in held if a.employee_id == eid]
        total = sum(a.amount for a in rows)
        cids = ", ".join(sorted({a.contract_id for a in rows}))
        rubles = f"{total:,.0f}".replace(",", " ")
        out.append(ConflictRecord(
            code="FACT_UNKNOWN_EMPLOYEE_WARNING",
            message=(f"Факт выплат строке {eid}, которой нет в листе сотрудников: "
                     f"{rubles} ₽ за {month_span([a.month for a in rows])} учтены "
                     f"в ФОТ договоров {cids}, в плане строки нет"),
            employee_id=eid, month=min(a.month for a in rows),
        ))
    unrated: dict[tuple[str, str], list[int]] = defaultdict(list)
    derived: dict[tuple[str, str], list[int]] = defaultdict(list)
    for a in held:
        if a.payment_kind is not PaymentKind.SALARY or a.employee_id not in employees:
            continue
        if getattr(a, "rate_derived", False):
            derived[(a.employee_id, a.contract_id)].append(a.month)
        elif a.rate is None:
            unrated[(a.employee_id, a.contract_id)].append(a.month)
    for (eid, cid), months in sorted(derived.items()):
        out.append(ConflictRecord(
            code="FACT_RATE_DERIVED_WARNING",
            message=(f"Факт выплат: у оклада строки {eid} по договору {cid} за "
                     f"{month_span(months)} ставка не указана — взята как оклад, делённый "
                     f"на оклад должности по справочнику"),
            employee_id=eid, contract_id=cid, month=min(months),
        ))
    for (eid, cid), months in sorted(unrated.items()):
        out.append(ConflictRecord(
            code="FACT_RATE_DERIVED_WARNING",
            message=(f"Факт выплат: у оклада строки {eid} по договору {cid} за "
                     f"{month_span(months)} ставка не указана и не выводится — у должности "
                     f"нет оклада в справочнике; человеко-месяцы этих месяцев не учтены"),
            employee_id=eid, contract_id=cid, month=min(months),
        ))
    return out


def validate_context(ctx: PlanningContext) -> list[ConflictRecord]:
    conflicts: list[ConflictRecord] = _defaults_used_conflicts(ctx)
    conflicts += _labor_rows_by_group_only(ctx)
    conflicts += _employment_structure_conflicts(ctx)
    conflicts += _actuals_conflicts(ctx)
    emp_ids = {e.id for e in ctx.employees}
    contract_ids = {c.id for c in ctx.contracts}

    tolerance = float(ctx.salary_stability.goz_labor_tolerance)
    if not 0 <= tolerance <= MAX_LABOR_TOLERANCE:
        conflicts.append(ConflictRecord(
            code="INVALID_LABOR_TOLERANCE",
            message=("Допуск трудоёмкости должен быть от 0 до 5 %, "
                     f"получено {tolerance * 100:g} %"),
        ))

    for e in ctx.employees:
        if e.rate <= 0:
            conflicts.append(
                ConflictRecord(
                    code="INVALID_RATE",
                    message=f"Сотрудник {e.id}: ставка должна быть > 0",
                    employee_id=e.id,
                )
            )
        if e.monthly_wage < 0:
            conflicts.append(
                ConflictRecord(
                    code="INVALID_PAYMENT",
                    message=f"Сотрудник {e.id}: отрицательная зарплата",
                    employee_id=e.id,
                )
            )
        # Зарплата по месяцам (правило 1, решение пользователя 19.09.2026):
        # сумма месяца заменяет зарплату штатного расписания, и отрицательная
        # в ней так же недопустима.
        for month, wage in sorted((getattr(e, "monthly_wages", None) or {}).items()):
            if wage < 0:
                conflicts.append(
                    ConflictRecord(
                        code="INVALID_PAYMENT",
                        message=f"Сотрудник {e.id}, месяц {month}: отрицательная зарплата",
                        employee_id=e.id,
                        month=month,
                    )
                )
        staff_q = staff_rate_min_quarters(e.rate)
        employment_max_q = (
            PART_QUARTERS_MAX
            if e.employment_type == "part_time"
            else MAIN_QUARTERS_MAX
        )
        if keeps_staff_rate_without_opening_extra(e.employment_category, e.position):
            max_q = employment_max_q
        else:
            max_q = min(employment_max_q, max_total_quarters(e.employment_category))
        if staff_q > max_q:
            conflicts.append(
                ConflictRecord(
                    code="INVALID_EMPLOYMENT_RATE",
                    message=(
                        f"Сотрудник {e.id}: ставка {e.rate:g} превышает максимум "
                        f"{quarters_to_rate(max_q):g} для указанного типа и категории "
                        "занятости"
                    ),
                    employee_id=e.id,
                )
            )
        overlapping_contracts = sorted(
            set(e.allowed_contracts) & set(e.forbidden_contracts)
        )
        for cid in overlapping_contracts:
            conflicts.append(
                ConflictRecord(
                    code="CONTRACT_ALLOWED_AND_FORBIDDEN",
                    message=(
                        f"Сотрудник {e.id}: договор {cid} одновременно указан "
                        "в разрешённых и запрещённых договорах"
                    ),
                    employee_id=e.id,
                    contract_id=cid,
                )
            )
        for cid in e.allowed_contracts:
            if cid not in contract_ids:
                conflicts.append(
                    ConflictRecord(
                        code="UNKNOWN_CONTRACT",
                        message=f"Сотрудник {e.id}: неизвестный договор {cid}",
                        employee_id=e.id,
                        contract_id=cid,
                    )
                )
        for cid in e.forbidden_contracts:
            if cid not in contract_ids:
                conflicts.append(
                    ConflictRecord(
                        code="UNKNOWN_CONTRACT",
                        message=f"Сотрудник {e.id}: неизвестный запрещённый договор {cid}",
                        employee_id=e.id,
                        contract_id=cid,
                    )
                )

    for c in ctx.contracts:
        # Пустой срок разбирался сравнением None с None и ронял проверку
        # TypeError: экономист получал трассировку вместо ответа, что не так
        # во входном файле.
        if c.start_date is None or c.end_date is None:
            conflicts.append(
                ConflictRecord(
                    code="MISSING_CONTRACT_DATES",
                    message=(
                        f"Договор {c.id}: не задан срок действия "
                        f"(дата начала и дата окончания)"
                    ),
                    contract_id=c.id,
                )
            )
        elif c.end_date < c.start_date:
            conflicts.append(
                ConflictRecord(
                    code="INVALID_DATES",
                    message=f"Договор {c.id}: дата окончания раньше начала",
                    contract_id=c.id,
                )
            )
        if c.total_fot < 0:
            conflicts.append(
                ConflictRecord(
                    code="INVALID_FOT",
                    message=f"Договор {c.id}: отрицательный ФОТ",
                    contract_id=c.id,
                )
            )
        if c.allow_salary:
            if c.salary_payment_deadline is None:
                conflicts.append(
                    ConflictRecord(
                        code="MISSING_PAYMENT_DEADLINE",
                        message=(
                            f"Договор {c.id}: укажите конечную дату выплат "
                            f"({_DEADLINE_LABELS['salary']}) — колонка "
                            f"«конечная дата выплат оклада»"
                        ),
                        contract_id=c.id,
                    )
                )
            elif payment_month_count(c, ctx.year, PaymentKind.SALARY) <= 0:
                conflicts.append(
                    ConflictRecord(
                        code="EMPTY_PAYMENT_WINDOW",
                        message=(
                            f"Договор {c.id}: нет месяцев в {ctx.year} для выплат "
                            f"({_DEADLINE_LABELS['salary']}) до "
                            f"{payment_deadline_date(c, 'salary').isoformat()}"
                        ),
                        contract_id=c.id,
                    )
                )
        if any(payment_kind_enabled(c, kind) for kind in PAYMENT_KINDS if kind.is_non_salary):
            if c.allowances_payment_deadline is None:
                conflicts.append(
                    ConflictRecord(
                        code="MISSING_PAYMENT_DEADLINE",
                        message=(
                            f"Договор {c.id}: укажите конечную дату выплат "
                            f"({_DEADLINE_LABELS['flex']}) — колонка "
                            f"«конечная дата выплат надбавок»"
                        ),
                        contract_id=c.id,
                    )
                )
            else:
                flex_kinds = [
                    kind for kind in PAYMENT_KINDS if kind.is_non_salary and payment_kind_enabled(c, kind)
                ]
                if flex_kinds and all(
                    payment_month_count(c, ctx.year, kind) <= 0 for kind in flex_kinds
                ):
                    conflicts.append(
                        ConflictRecord(
                            code="EMPTY_PAYMENT_WINDOW",
                            message=(
                                f"Договор {c.id}: нет месяцев в {ctx.year} для выплат "
                                f"({_DEADLINE_LABELS['flex']}) до "
                                f"{payment_deadline_date(c, flex_kinds[0]).isoformat()}"
                            ),
                            contract_id=c.id,
                        )
                    )
    for c in ctx.contracts:
        for mb in c.monthly_budgets:
            if mb.inflow_amount < 0:
                conflicts.append(
                    ConflictRecord(
                        code="INVALID_BUDGET_INFLOW",
                        message=(
                            f"Договор {c.id}, месяц {mb.month}: "
                            f"отрицательное поступление ({mb.inflow_amount})"
                        ),
                        contract_id=c.id,
                        month=mb.month,
                    )
                )

    for row in ctx.secret_allowances:
        if row.employee_id not in emp_ids:
            conflicts.append(
                ConflictRecord(
                    code="UNKNOWN_EMPLOYEE",
                    message=f"Лист 120_надбавка: неизвестный сотрудник {row.employee_id}",
                    employee_id=row.employee_id,
                )
            )
        if row.secret_contract_id not in contract_ids:
            conflicts.append(
                ConflictRecord(
                    code="UNKNOWN_CONTRACT",
                    message=f"Лист 120_надбавка: неизвестный договор секретности {row.secret_contract_id}",
                    employee_id=row.employee_id,
                    contract_id=row.secret_contract_id,
                )
            )
        if row.rate <= 0:
            conflicts.append(
                ConflictRecord(
                    code="INVALID_SECRET_ALLOWANCE_RATE",
                    message=(
                        f"Лист 120_надбавка: ставка 120 для сотрудника {row.employee_id} "
                        f"должна быть > 0"
                    ),
                    employee_id=row.employee_id,
                    contract_id=row.secret_contract_id,
                )
            )

    for e in ctx.employees:
        # Зарплата бывает и только в отдельных месяцах (лист зарплаты по
        # месяцам): оклад нужен и такой строке.
        has_wage = e.monthly_wage > 0 or any(
            wage > 0 for wage in (getattr(e, "monthly_wages", None) or {}).values()
        )
        if has_wage and (
            e.reference_salary_for_rate is None or e.reference_salary_for_rate <= 0
        ):
            conflicts.append(
                ConflictRecord(
                    code="MISSING_ORG_SALARY_CAP_WARNING",
                    message=(
                        f"Сотрудник {e.id}: нет оклада по справочнику должностей "
                        f"(организационный потолок оклада) — salary может быть 0"
                    ),
                    employee_id=e.id,
                )
            )
        if e.position and e.equivalence_group is None:
            conflicts.append(
                ConflictRecord(
                    code="UNKNOWN_POSITION",
                    message=f"Сотрудник {e.id}: должность {e.position!r} не найдена в справочнике должностей",
                    employee_id=e.id,
                )
            )

    for row in ctx.position_salary_limits:
        if (
            row.p4_limit is not None
            and row.p4_limit > 0
            and not p4_applies_to_category(row.personnel_category)
        ):
            conflicts.append(
                ConflictRecord(
                    code="P4_IGNORED_FOR_CATEGORY_WARNING",
                    message=(
                        f"Должность {row.position!r} ({row.personnel_category}): "
                        f"П4 не применяется — только для категорий НТП и НР"
                    ),
                )
            )

    for c in ctx.contracts:
        for pr in c.position_rules:
            if pr.position and pr.equivalence_group is None:
                conflicts.append(
                    ConflictRecord(
                        code="UNKNOWN_CONTRACT_POSITION_WARNING",
                        message=(
                            f"Договор {c.id}: договорная должность {pr.position!r} "
                            f"не найдена в справочнике должностей"
                        ),
                        contract_id=c.id,
                    )
                )

    labor_by_contract: dict[str, float] = {}
    for lp in ctx.labor_plans:
        if lp.year != ctx.year:
            continue
        if lp.position and lp.equivalence_group is None:
            conflicts.append(
                ConflictRecord(
                    code="UNKNOWN_LABOR_POSITION_WARNING",
                    message=(
                        f"Договор {lp.contract_id}: должность {lp.position!r} из contract_labor "
                        f"не найдена в справочнике должностей"
                    ),
                    contract_id=lp.contract_id,
                )
            )
        if lp.person_months > 0 and (lp.avg_monthly_labor_cost is None or lp.avg_monthly_labor_cost <= 0):
            conflicts.append(
                ConflictRecord(
                    code="MISSING_AVG_LABOR_COST_WARNING",
                    message=(
                        f"Договор {lp.contract_id}: для строки трудоёмкости "
                        f"({lp.position or lp.equivalence_group or 'без должности'}) "
                        f"нужна «средняя стоимость выполнения работ в месяц» > 0"
                    ),
                    contract_id=lp.contract_id,
                )
            )
        labor_by_contract[lp.contract_id] = labor_by_contract.get(lp.contract_id, 0.0) + lp.person_months

    for cid, plan_pm in labor_by_contract.items():
        contract = next((c for c in ctx.contracts if c.id == cid), None)
        if contract is None:
            continue
        if plan_pm <= 0:
            conflicts.append(
                ConflictRecord(
                    code="INVALID_LABOR",
                    message=f"Договор {cid}: трудоёмкость должна быть > 0",
                    contract_id=cid,
                )
            )
            continue
        if contract.total_fot <= 0:
            conflicts.append(
                ConflictRecord(
                    code="LABOR_WITHOUT_FOT",
                    message=f"Договор {cid}: задана трудоёмкость, но ФОТ ≤ 0",
                    contract_id=cid,
                )
            )

    for mp in ctx.manual_prohibitions:
        if mp.employee_id not in emp_ids:
            conflicts.append(
                ConflictRecord(
                    code="UNKNOWN_EMPLOYEE",
                    message=f"Запрет: неизвестный сотрудник {mp.employee_id}",
                    employee_id=mp.employee_id,
                )
            )
        if mp.contract_id not in contract_ids:
            conflicts.append(
                ConflictRecord(
                    code="UNKNOWN_CONTRACT",
                    message=f"Запрет: неизвестный договор {mp.contract_id}",
                    contract_id=mp.contract_id,
                )
            )

    return conflicts


def employee_active_in_month(employee, year: int, month: int) -> bool:
    period_start = date(year, month, 1)
    if month == 12:
        period_end = date(year, 12, 31)
    else:
        period_end = date(year, month + 1, 1)
        period_end = date.fromordinal(period_end.toordinal() - 1)

    if employee.start_date and employee.start_date > period_end:
        return False
    if employee.end_date and employee.end_date < period_start:
        return False
    return True
