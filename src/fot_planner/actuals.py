"""Пересчёт с факта: прошедшие месяцы плана — начисления листа «факт_выплат».

Владелец продукта 30.09.2026: «нужно срочно добавить пересчет с текущими
вводными. То есть мы знаем, например, что в январе и феврале вот такие суммы
были, вот так сотрудники распределены, с такими ставками и надбавками… нужно
чтобы оптимизатор не трогая январь и февраль, пересчитал, то есть он должен
зафиксировать эти переменные. Но учесть все правила про условия в квартал
итд. То есть не выкинуть просто эти данные, а именно учесть в расчете, просто
нельзя менять их. То есть если была смена, а правило — не более чем в
квартал, то пусть учтет это» (правило 26 свода).

Здесь то, что решатель, проверка входа и проверка результата обязаны читать
одинаково: какой факт расчёт держит, где у человека основное место в месяце
факта и насколько правило, тянущееся через месяцы, ослабляется, когда
история одна его уже нарушила. Разойдись эти вещи между слоями — проверка
результата ругала бы план за то, что решатель по правилу сделал верно.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from fot_planner.models import ActualPayment, PaymentKind

#: Последний месяц факта — от 0 (факта нет) до 11: хотя бы декабрь остаётся
#: расчёту, иначе считать нечего.
MAX_FROZEN_THROUGH = 11

MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль",
          "август", "сентябрь", "октябрь", "ноябрь", "декабрь")


def frozen_through(ctx) -> int:
    """Последний месяц факта F; 0 — факта нет.

    Значение вне 0…11 отклоняет проверка входа (INVALID_FROZEN_MONTHS), а
    здесь оно читается как «факта нет»: решатель и проверка результата не
    должны падать на входе, который до них не дойдёт.
    """
    try:
        value = int(getattr(ctx, "frozen_through", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return value if 0 <= value <= MAX_FROZEN_THROUGH else 0


def frozen_facts(ctx) -> list[ActualPayment]:
    """Факт, который расчёт держит: месяцы 1…F и договор, который есть во входе.

    Факт по сотруднику, которого нет в листе сотрудников (уволен или снят с
    расчёта), остаётся: его деньги уже потрачены с договора. Факт по
    чужому договору и после F не учитывается — об этом говорит проверка
    входа. Нулевая сумма — не выплата.
    """
    last = frozen_through(ctx)
    if not last:
        return []
    contract_ids = {c.id for c in getattr(ctx, "contracts", None) or []}
    return [a for a in getattr(ctx, "actuals", None) or []
            if 1 <= a.month <= last and a.contract_id in contract_ids
            and abs(float(a.amount)) > 0.005]


def fact_amounts(facts) -> dict[tuple[str, str, int, PaymentKind], float]:
    """(строка, договор, месяц, вид) → сумма факта; повторы складываются."""
    out: dict[tuple[str, str, int, PaymentKind], float] = defaultdict(float)
    for a in facts:
        out[(a.employee_id, a.contract_id, a.month, a.payment_kind)] += float(a.amount)
    return dict(out)


def fact_rates(facts) -> dict[tuple[str, str, int], float]:
    """(строка, договор, месяц) → открытая ставка оклада факта.

    Ставка бывает только у оклада. Две записи на одну тройку — одна ставка,
    записанная дважды (начисление и перерасчёт), поэтому берётся наибольшая,
    а не сумма.
    """
    out: dict[tuple[str, str, int], float] = {}
    for a in facts:
        if a.payment_kind is not PaymentKind.SALARY or a.rate is None or a.rate <= 0:
            continue
        key = (a.employee_id, a.contract_id, a.month)
        out[key] = max(out.get(key, 0.0), float(a.rate))
    return out


def person_key(employee) -> str:
    """Физический человек: табельный номер, для старых файлов — ФИО."""
    number = str(getattr(employee, "person_id", "") or "").strip().lower()
    if number:
        return number
    name = " ".join(str(getattr(employee, "full_name", "") or "").lower().split())
    return name or employee.id


def fact_main_flags(facts, employees: dict) -> dict[tuple[str, str, int], bool]:
    """(строка, договор, месяц) оклада факта → основное ли это место.

    Графа «основное место» решает, если заполнена. Пусто — основное место у
    оклада человека с наибольшей ставкой в этом месяце (так его читает и
    отчёт сервиса): у человека основное место одно на все его строки
    (правило 13), поэтому выбор — по человеку, а не по строке. Равные ставки
    различает строка не совместительства, затем большая сумма, затем код
    договора — чтобы выбор не зависел от порядка строк листа.
    """
    by_person_month: dict[tuple[str, int], list] = defaultdict(list)
    for a in facts:
        employee = employees.get(a.employee_id)
        if a.payment_kind is not PaymentKind.SALARY or employee is None:
            continue
        by_person_month[(person_key(employee), a.month)].append(a)
    flags: dict[tuple[str, str, int], bool] = {}
    for rows in by_person_month.values():
        for a in rows:
            flags[(a.employee_id, a.contract_id, a.month)] = a.is_main is True
        if any(a.is_main is True for a in rows):
            continue
        blank = [a for a in rows if a.is_main is None]
        if not blank:
            continue
        best = sorted(blank, key=lambda a: (
            -(a.rate or 0.0),
            getattr(employees[a.employee_id], "employment_type", "auto") == "part_time",
            -float(a.amount), a.contract_id))[0]
        flags[(best.employee_id, best.contract_id, best.month)] = True
    return flags


def clip_to_future(items, last: int) -> list:
    """Закрепления и запреты — только на месяцы после факта (F+1…12).

    Прошедший месяц уже равен факту, и требование к нему не выполнить и не
    нарушить: закрепление на январь–декабрь по договору, который в январе и
    феврале не платил, иначе сделало бы план нерешаемым. Сервис урезает срок
    сам; решатель повторяет это, чтобы вход не из сервиса не ломал расчёт.
    """
    if not last:
        return list(items)
    out = []
    for item in items:
        start = max(int(item.month_from), last + 1)
        if start > int(item.month_to):
            continue
        out.append(item if start == item.month_from else replace(item, month_from=start))
    return out


def relaxed_labor_band(low: float, high: float, frozen: float, *, tol: float,
                       monthly_future_plan: float | None = None,
                       has_future: bool = True) -> tuple[float, float]:
    """Годовой коридор строки РКМ с учётом факта.

    Факт входит в годовую сумму строки как есть. Ослабление — ровно на то,
    что уже сделала история:

    * месяцев после факта у строки нет — история и есть весь год, коридор
      расширяется до неё;
    * у строки план по месяцам — каждый месяц после факта держит свой
      коридор, а годовой не строже «факт плюс их коридоры»: недобор или
      перебор прошедших месяцев будущими не исправить;
    * годовая строка — перебор истории будущее не увеличивает (верх не ниже
      факта), а недобор будущие месяцы обязаны добрать.
    """
    if not has_future:
        return min(low, frozen), max(high, frozen)
    if monthly_future_plan is not None:
        return (min(low, frozen + (1.0 - tol) * monthly_future_plan),
                max(high, frozen + (1.0 + tol) * monthly_future_plan))
    return low, max(high, frozen)


def month_span(months) -> str:
    """«июль», «июль–август», «март, июль–август» — месяцы подряд диапазоном."""
    runs: list[list[int]] = []
    for m in sorted(set(months)):
        if runs and m == runs[-1][-1] + 1:
            runs[-1].append(m)
        else:
            runs.append([m])
    return ", ".join(
        MONTHS[r[0] - 1] if len(r) == 1 else f"{MONTHS[r[0] - 1]}–{MONTHS[r[-1] - 1]}"
        for r in runs)
