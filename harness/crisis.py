# -*- coding: utf-8 -*-
"""Кризис-тесты правил решателя.

Каждый случай — маленький входной файл под одно правило, собранный из
шаблона демо (справочник должностей и правила замещения берутся оттуда, все
переменные листы очищаются). Решатель вызывается напрямую, без сервиса.
Проверка двух видов:

* сценарные — что именно должно получиться в этом случае;
* инварианты — правила, которые обязаны выполняться в любом удачном плане
  (полная зарплата, шаг ставки, 122 при окладе, П2556, П4, БЭП, 152, …).

Запуск:  python harness/crisis.py [--only 07,09] [--limit 60]
Выход:   таблица «случай — вердикт — что не так» и код возврата 1 при провалах.
"""
from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from openpyxl import load_workbook  # noqa: E402

from fot_planner import optimizer as opt  # noqa: E402
from fot_planner.excel.load import load_context  # noqa: E402
from fot_planner.models import PaymentKind as K  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "harness" / "crisis_template.xlsx"
WORK = ROOT / "harness" / "_crisis"
YEAR = 2026
EPS = 1.0  # рубль
MONTH_NAMES = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль",
               "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

# Справочник (лист лимиты_по_должностям демо): оклад, П2556, П4 на ставку
REF = {
    "Инженер": (40400, 110000, 149648.9),
    "Ведущий инженер": (47000, 140000, 149648.9),
    "Программист": (40400, 110000, 149648.9),
    "Аналитик": (40400, 110000, 149648.9),
    "Лаборант": (38300, 95000, 149648.9),
    "Старший научный сотрудник": (74100, 120000, 257643.19),
}
BEP = 112261.0

VARIABLE_SHEETS = (
    "сотрудники", "договоры", "фот_по_месяцам", "120_надбавка",
    "трудоемкость_по_договорам", "фиксация_фот_по_месяцам",
    "минимальные_остатки", "ручные_назначения", "ручные_запреты",
)
# Необязательный лист входа; имя — договорённость о входе, а не константа
# загрузчика: переименование в коде стенд заметит.
WAGES_SHEET = "зарплата_по_месяцам"
# Пересчёт с факта (правило 26, 30.09.2026): лист начислений прошедших
# месяцев и графа настроек с последним месяцем факта — тоже договорённость о
# входе, которую пишет сервис.
FACTS_SHEET = "факт_выплат"
FACTS_HEADER = ["код строки", "договор", "месяц", "вид выплаты", "сумма", "ставка", "основное место"]
FROZEN_COLUMN = "факт по месяц"
WHOLE_COLUMN = "по возможности вся"


# ── сборка входа ─────────────────────────────────────────────────────────
def emp(code, position, wage, *, rate=1.0, etype="основное", cat="основной",
        dep="Отдел 12", start="01.01.2026", end="31.12.2026", allowed=None, forbidden=None):
    return [code, code, position, dep, rate, etype, cat, wage, start, end, allowed, forbidden]


def ctr(code, fot, *, start="01.01.2026", end="31.12.2026", account="20576У17400",
        goz="нет", salary="да", k120="нет", k122="да", k124="нет", k152="нет",
        order="нет", priority="нет", main="да", part="да", salary_deadline=None,
        allowance_deadline=None, dep=None, kind="НИР"):
    return {
        "код": code, "название": code, "номер": "1/26", "тип договора": kind, "счет": account,
        "ГОЗ": goz, "дата начала": start, "дата окончания": end, "фот": fot,
        "оклад разрешен": salary, "120 разрешена": k120, "122 разрешена": k122,
        "124 разрешена": k124, "152 разрешена": k152, "стимулирующая приказом разрешена": order,
        "Приоритет": priority, "основное место разрешено": main, "совместительство разрешено": part,
        "конечная дата выплат оклада": salary_deadline or end,
        "конечная дата выплат надбавок": allowance_deadline or end,
        "подразделение": dep,
    }


def build(name, employees, contracts, inflow, *, labor=(), secret=(), manual=(),
          forbid=(), deficit="нет", max_contracts=3, tolerance=0.05, bep=None, wages=None,
          facts=None, frozen=None):
    """inflow: {код: сумма в месяц | список из 12}.

    wages — строки листа «зарплата_по_месяцам» (код строки, месяц, зарплата):
    None — листа во входе нет, как в прежних случаях; [] — лист из одних
    заголовков.

    manual — (сотрудник, договор, с, по, вид, сумма[, по возможности вся]).
    facts — строки листа «факт_выплат» (код строки, договор, месяц, вид,
    сумма[, ставка, основное место]): None — листа нет. frozen — «факт по
    месяц» в настройках: None — графы нет, как в прежних случаях.
    """
    WORK.mkdir(exist_ok=True)
    path = WORK / f"{name}.xlsx"
    wb = load_workbook(TEMPLATE)
    for sheet in VARIABLE_SHEETS:
        ws = wb[sheet]
        if ws.max_row > 1:
            ws.delete_rows(2, ws.max_row - 1)
    # Зарплата по месяцам (правило 1, 19.09.2026). Лист собирается заново:
    # если он появится в шаблоне демо, его строки не попадут в чужой случай.
    if WAGES_SHEET in wb.sheetnames:
        del wb[WAGES_SHEET]
    if wages is not None:
        ws_w = wb.create_sheet(WAGES_SHEET)
        ws_w.append(["код строки", "месяц", "зарплата"])
        for row in wages:
            ws_w.append(list(row))
    if FACTS_SHEET in wb.sheetnames:
        del wb[FACTS_SHEET]
    if facts is not None:
        ws_f = wb.create_sheet(FACTS_SHEET)
        ws_f.append(FACTS_HEADER)
        for row in facts:
            ws_f.append(list(row) + [None] * (len(FACTS_HEADER) - len(row)))
    ws = wb["договоры"]
    hdr = [c.value for c in ws[1]]
    if "подразделение" not in hdr:
        ws.cell(1, len(hdr) + 1).value = "подразделение"
        hdr.append("подразделение")
    for c in contracts:
        ws.append([c.get(h) for h in hdr])
    for e in employees:
        wb["сотрудники"].append(e)
    for code, v in inflow.items():
        row = v if isinstance(v, (list, tuple)) else [v] * 12
        wb["фот_по_месяцам"].append([code] + list(row))
    # Шестой элемент строки трудоёмкости — план по месяцам {месяц: чел.-мес.}.
    ws_l = wb["трудоемкость_по_договорам"]
    if any(len(row) > 5 for row in labor):
        for name in MONTH_NAMES:
            ws_l.cell(1, ws_l.max_column + 1).value = name
    for row in labor:
        code, pm, position, avg, heads = row[:5]
        line = [code, YEAR, pm, position, None, None, None, avg, heads]
        if len(row) > 5:
            line += [row[5].get(m) for m in range(1, 13)]
        ws_l.append(line)
    # Строка 120: (сотрудник, договор секретности, ставка[, начало, окончание,
    # договор выплаты]) — окно и договор выплаты с 18.09.2026 (правило 10).
    ws_s = wb["120_надбавка"]
    if any(len(row) > 3 for row in secret):
        for name in ("начало 120", "окончание 120", "договор выплаты 120"):
            if name not in [c.value for c in ws_s[1]]:
                ws_s.cell(1, ws_s.max_column + 1).value = name
    for row in secret:
        ws_s.append(list(row) + [None] * (6 - len(row)))
    ws_m = wb["ручные_назначения"]
    whole = any(len(row) > 6 for row in manual)
    if whole and WHOLE_COLUMN not in [c.value for c in ws_m[1]]:
        ws_m.cell(1, ws_m.max_column + 1).value = WHOLE_COLUMN
    for row in manual:
        e_id, c_id, m1, m2, kind, fixed = row[:6]
        ws_m.append([e_id, c_id, YEAR, m1, m2, kind, fixed] + ([row[6] if len(row) > 6 else None] if whole else []))
    for e_id, c_id, m1, m2, kind in forbid:
        wb["ручные_запреты"].append([e_id, c_id, YEAR, m1, m2, kind])
    if bep:
        wl = wb["лимиты_по_должностям"]
        for r in wl.iter_rows(min_row=2):
            if r[0].value in bep:
                r[8].value = bep[r[0].value]
    ws = wb["настройки"]
    ws.cell(2, 2).value = deficit
    ws.cell(2, 3).value = max_contracts
    ws.cell(2, 8).value = tolerance
    if frozen is not None:
        head = [c.value for c in ws[1]]
        col = head.index(FROZEN_COLUMN) + 1 if FROZEN_COLUMN in head else ws.max_column + 1
        ws.cell(1, col).value = FROZEN_COLUMN
        ws.cell(2, col).value = frozen
    wb.save(path)
    return path


def solve(path, limit):
    ctx = load_context(path)
    return ctx, opt.solve(ctx, time_limit_sec=limit)


# ── разбор результата ────────────────────────────────────────────────────
class Plan:
    def __init__(self, ctx, res):
        self.ctx, self.res = ctx, res
        self.status = res.solver_status
        self.pay = defaultdict(float)          # (e, c, m, kind) → сумма
        self.by_em = defaultdict(float)        # (e, m) → всего
        self.kinds = defaultdict(set)          # (e, c, m) → виды
        for a in res.allocations:
            if a.amount <= 0.5:
                continue
            self.pay[(a.employee_id, a.contract_id, a.month, a.payment_kind)] += a.amount
            self.by_em[(a.employee_id, a.month)] += a.amount
            self.kinds[(a.employee_id, a.contract_id, a.month)].add(a.payment_kind)
        self.rate = {}                          # (e, c, m) → (ставка, основное)
        self.rate_pos = {}                      # (e, c, m) → должность ставки
        for r in res.open_rate_attributions:
            if r.open_rate > 0:
                self.rate[(r.employee_id, r.contract_id, r.month)] = (r.open_rate, r.is_main)
                self.rate_pos[(r.employee_id, r.contract_id, r.month)] = r.position
        self.pm = defaultdict(float)            # (e, c, m) → чел.-мес.
        self.pm_row = defaultdict(float)        # (c, должность) → чел.-мес. за год
        self.people_row = defaultdict(set)      # (c, должность, m) → люди
        for r in res.labor_pm_attributions:
            if r.person_months > 1e-6:
                self.pm[(r.employee_id, r.contract_id, r.month)] += r.person_months
                self.pm_row[(r.contract_id, r.position)] += r.person_months
                self.people_row[(r.contract_id, r.position, r.month)].add(r.employee_id)
        self.lpay = defaultdict(float)          # (c, должность) → выплаты за год
        for r in res.labor_payment_attributions:
            self.lpay[(r.contract_id, r.position)] += r.amount
        self.deficit = defaultdict(float)
        for d in res.deficits:
            self.deficit[(d.employee_id, d.month)] += d.amount
        self.emp = {e.id: e for e in ctx.employees}
        self.ctr = {c.id: c for c in ctx.contracts}

    def amount(self, e, c, m, kind):
        return self.pay.get((e, c, m, kind), 0.0)

    def total(self, e, m, kinds, c=None):
        return sum(v for (ee, cc, mm, k), v in self.pay.items()
                   if ee == e and mm == m and k in kinds and (c is None or cc == c))

    def rate_total(self, e, m):
        return sum(v for (ee, cc, mm), (v, _) in self.rate.items() if ee == e and mm == m)

    def months_with(self, e, kind, c=None):
        return sorted({mm for (ee, cc, mm, k) in self.pay if ee == e and k == kind and (c is None or cc == c)})


# ── инварианты: обязаны выполняться в любом удачном плане ────────────────
def invariants(p: Plan):
    out = []
    # Шаг ставки и прочее, что умеет проверка результата, стенд не повторяет:
    # он требует, чтобы её поймал сам сервис. Правило, живущее только в
    # стенде, в работе не работает.
    if p.res.audit_status == "FAIL":
        out += ["проверка результата, %s: %s" % (v.rule_id, v.message)
                for v in p.res.audit_violations]
    ctx = p.ctx
    # Пересчёт с факта (правило 26): месяцы 1…F — история с листа
    # «факт_выплат». Правила месяца к ним не относятся — история может их
    # нарушать; зато месяц факта обязан совпасть с листом до копейки, и это
    # стенд сверяет сам, по входу, а не через код решателя.
    frozen = int(getattr(ctx, "frozen_through", 0) or 0)
    out += frozen_months_match_facts(p, frozen)
    months = range(frozen + 1, 13)
    active = lambda e, m: opt.employee_active_in_month(e, YEAR, m)  # noqa: E731
    for e in ctx.employees:
        for m in months:
            if not active(e, m):
                continue
            paid = p.by_em.get((e.id, m), 0.0) + p.deficit.get((e.id, m), 0.0)
            # Зарплата месяца: своя сумма из листа «зарплата_по_месяцам» или
            # зарплата штатного расписания (правило 1, 19.09.2026). Стенд
            # читает вход сам, а не через решатель: ошибка общего кода не
            # спрячется одновременно в модели и в проверке.
            due = e.monthly_wages.get(m, e.monthly_wage)
            if abs(paid - due) > EPS:
                out.append(f"зарплата {e.id} м{m}: выплачено {paid:.0f} + дефицит ≠ {due:.0f}")
            # ставки
            rates = [(c, v, main) for (ee, c, mm), (v, main) in p.rate.items() if ee == e.id and mm == m]
            mains = [c for c, v, main in rates if main]
            if rates:
                # Основное — одно на человека, а не на строку: у второй
                # строки (совместительство) основного быть не должно.
                person = (str(getattr(e, "person_id", "") or "").strip().lower()
                          or " ".join(str(e.full_name or "").lower().split()) or e.id)
                pm_main = [c for (ee, c, mm), (v, main) in p.rate.items()
                           if mm == m and main and
                           ((str(getattr(p.emp[ee], "person_id", "") or "").strip().lower()
                             or " ".join(str(p.emp[ee].full_name or "").lower().split()) or ee)
                            == person)]
                if len(pm_main) != 1:
                    out.append(f"основное место {e.id} м{m}: у человека {len(pm_main)} вместо 1")
                if e.employment_type == "part_time" and mains:
                    out.append(f"строка совместительства {e.id} м{m} помечена основным")
                # Основное место не трогаем: ставка ровно штатная, должность своя.
                # Сверх неё только совместительство.
                for c, v, main in rates:
                    if main and abs(v - e.rate) > 1e-6:
                        out.append(f"основная ставка изменена {e.id} {c} м{m}: {v} вместо штатной {e.rate}")
                    pos = p.rate_pos.get((e.id, c, m))
                    if main and pos and pos.strip().lower() != e.position.strip().lower():
                        out.append(f"должность основного места изменена {e.id} {c} м{m}: {pos} вместо {e.position}")
                tot = sum(v for _, v, _ in rates)
                if tot + 1e-6 < e.rate:
                    out.append(f"штатная ставка {e.id} м{m}: открыто {tot} < {e.rate}")
                cap = {"student": 0.5, "graduate_student": 0.75}.get(e.employment_category, 1.5)
                if e.employment_type == "part_time":
                    cap = 0.5
                if tot > cap + 1e-6:
                    out.append(f"суммарная ставка {e.id} м{m}: {tot} > {cap}")
                part = sum(v for _, v, main in rates if not main)
                if part > 0.5 + 1e-6:
                    out.append(f"совместительство {e.id} м{m}: {part} > 0,5")
            # оклад = справочный × ставка на договоре
            for (ee, c, mm, k), v in list(p.pay.items()):
                if ee != e.id or mm != m or k is not K.SALARY:
                    continue
                r = p.rate.get((e.id, c, m))
                if r is None:
                    out.append(f"оклад без ставки {e.id} {c} м{m}")
                    continue
                ref = REF.get(e.position)
                if ref and abs(v - ref[0] * r[0]) > EPS:
                    out.append(f"оклад {e.id} {c} м{m}: {v:.0f} ≠ {ref[0]}×{r[0]}")
            # 122 только с окладом на том же договоре
            for (ee, c, mm), kinds in p.kinds.items():
                if ee != e.id or mm != m:
                    continue
                if K.K122 in kinds and K.SALARY not in kinds:
                    out.append(f"122 без оклада {e.id} {c} м{m}")
                if K.K152 in kinds and kinds & {K.K120, K.K122, K.K124, K.ORDER_INCENTIVE}:
                    out.append(f"152 вместе с другими надбавками {e.id} {c} м{m}")
                ctr = p.ctr[c]
                for k, flag in ((K.K120, "allow_secret"), (K.K122, "allow_allowance"),
                                (K.K124, "allow_incentive"), (K.K152, "allow_extra_work"),
                                (K.ORDER_INCENTIVE, "allow_order_incentive"), (K.SALARY, "allow_salary")):
                    if k in kinds and not getattr(ctr, flag) and not (k is K.K120):
                        out.append(f"{k.name} с договора, где он запрещён: {e.id} {c} м{m}")
                if ctr.priority_payment_mode and K.K152 in kinds and kinds & {K.SALARY, K.K120, K.K122, K.K124}:
                    out.append(f"приоритет: штатные и 152 вместе {e.id} {c} м{m}")
                if ctr.priority_payment_mode and kinds & {K.SALARY, K.K120, K.K122, K.K124}:
                    other = [cc for (e2, cc, m2), kk in p.kinds.items()
                             if e2 == e.id and m2 == m and cc != c and K.SALARY in kk]
                    if other:
                        out.append(f"приоритет: штатные на {c} при окладе на {other} {e.id} м{m}")
            # П2556 по договору и открытой ставке
            ref = REF.get(e.position)
            if ref:
                for (ee, c, mm), (rv, _) in p.rate.items():
                    if ee != e.id or mm != m:
                        continue
                    staff = p.total(e.id, m, {K.SALARY, K.K122}, c)
                    if staff > ref[1] * rv + EPS:
                        out.append(f"П2556 {e.id} {c} м{m}: оклад+122 {staff:.0f} > {ref[1]}×{rv}")
                order = p.total(e.id, m, {K.ORDER_INCENTIVE})
                if order > ref[1] * e.rate + EPS:
                    out.append(f"П2556 приказ {e.id} м{m}: {order:.0f} > {ref[1]}×{e.rate}")
                n_orders = sum(1 for (ee, c, mm, k) in p.pay if ee == e.id and mm == m and k is K.ORDER_INCENTIVE)
                if n_orders > 1:
                    out.append(f"приказ дважды в месяц {e.id} м{m}: {n_orders}")
                # П4: в месяце со 124 штатная часть ровно предел × суммарная ставка
                if p.total(e.id, m, {K.K124}) > 0:
                    staff = p.total(e.id, m, {K.SALARY, K.K122, K.K124})
                    lim = ref[2] * p.rate_total(e.id, m)
                    if staff > lim + EPS:
                        out.append(f"П4 {e.id} м{m}: {staff:.0f} > {lim:.0f}")
                    elif staff < lim - EPS:
                        out.append(f"П4 не ровно {e.id} м{m}: {staff:.0f} < {lim:.0f}")
            # чел.-мес. ≤ открытая ставка на договоре
            for (ee, c, mm), pm in p.pm.items():
                if ee != e.id or mm != m:
                    continue
                rv = p.rate.get((e.id, c, m), (0.0, False))[0]
                if pm > rv + 1e-6:
                    out.append(f"чел.-мес. {e.id} {c} м{m}: {pm} > ставка {rv}")
    # 120: только по листу, в окне строки (своём или договора секретности),
    # ровно процент от оклада; закреплённый договор выплаты — только он.
    from fot_planner.contract_calendar import (secret_allowance_active,
                                               secret_allowance_pay_contracts)
    listed = {}
    for s in ctx.secret_allowances:
        listed.setdefault(s.employee_id, []).append(s)
    for e in ctx.employees:
        for m in months:
            k120 = p.total(e.id, m, {K.K120})
            rows = listed.get(e.id)
            if not rows:
                if k120 > 0:
                    out.append(f"120 не по листу {e.id} м{m}")
                continue
            active = [s for s in rows if secret_allowance_active(s, p.ctr, YEAR, m)]
            inwin = bool(active)
            rate = max((s.rate for s in active), default=0.0)
            sal = p.total(e.id, m, {K.SALARY})
            if inwin and abs(k120 - rate * sal) > EPS:
                out.append(f"120 {e.id} м{m}: {k120:.0f} ≠ {rate}×{sal:.0f}")
            if not inwin and k120 > 0:
                out.append(f"120 вне окна секретности {e.id} м{m}")
            pinned = secret_allowance_pay_contracts(rows, p.ctr, e.id, YEAR, m)
            for (ee, c, mm, k), v in p.pay.items():
                if ee == e.id and mm == m and k is K.K120 and pinned and c not in pinned:
                    out.append(f"120 не с закреплённого договора: {e.id} {c} м{m}")
            for (ee, c, mm, k), v in p.pay.items():
                if ee == e.id and mm == m and k is K.K120:
                    ctr = p.ctr[c]
                    acc = "".join(ch for ch in ctr.account if ch.isdigit())
                    if not acc.startswith("23") and K.SALARY not in p.kinds[(e.id, c, m)]:
                        out.append(f"120 без оклада на договоре не 23: {e.id} {c} м{m}")
    # БЭП: оклад+122 по ГОЗ-договору ≤ Σ БЭП должности × ставка
    bep_by_pos = {}
    for row in (ctx.position_limit_tables.get("bep") or []):
        if row.limit:
            bep_by_pos[row.position.strip().lower()] = float(row.limit)
    for c in ctx.contracts:
        if not c.is_goz_defense_order:
            continue
        for m in months:
            staff = sum(v for (ee, cc, mm, k), v in p.pay.items()
                        if cc == c.id and mm == m and k in (K.SALARY, K.K122))
            cap = sum(bep_by_pos.get(p.emp[ee].position.strip().lower(), BEP) * v
                      for (ee, cc, mm), (v, _) in p.rate.items() if cc == c.id and mm == m)
            if staff > cap + EPS:
                out.append(f"БЭП {c.id} м{m}: {staff:.0f} > {cap:.0f}")
    # касса: остаток не отрицательный, расход не раньше поступления
    for b in p.res.contract_balances:
        if b.month > frozen and getattr(b, "closing_balance", 0) < -EPS:
            out.append(f"касса {b.contract_id} м{b.month}: остаток {b.closing_balance:.0f}")
    # сроки договора
    for (e, c, m, k), v in p.pay.items():
        if m <= frozen:
            continue
        ctr = p.ctr[c]
        from fot_planner.contract_calendar import contract_allows_payment_month
        if not contract_allows_payment_month(ctr, YEAR, m, k) and k is not K.K120:
            out.append(f"выплата вне срока {e} {c} м{m} {k.name}")
    return out


def frozen_months_match_facts(p: Plan, frozen: int):
    """Месяцы 1…F плана — ровно лист «факт_выплат» и помечены фактом.

    Ожидание стенд собирает сам: строки листа за 1…F по договорам входа,
    повторы сложены, нулевые отброшены. Лишняя выплата в прошедшем месяце,
    потерянная или изменённая — провал, какой бы статус ни дал решатель.
    """
    if not frozen:
        return []
    out = []
    known = {c.id for c in p.ctx.contracts}
    want = defaultdict(float)
    for a in p.ctx.actuals:
        if 1 <= a.month <= frozen and a.contract_id in known:
            want[(a.employee_id, a.contract_id, a.month, a.payment_kind)] += a.amount
    want = {k: v for k, v in want.items() if abs(v) > 0.005}
    got = defaultdict(float)
    for a in p.res.allocations:
        if a.month <= frozen:
            got[(a.employee_id, a.contract_id, a.month, a.payment_kind)] += a.amount
            if a.source != "fact":
                out.append(f"месяц факта {a.month}: выплата {a.employee_id} {a.contract_id} "
                           f"{a.payment_kind.name} с источником {a.source}, а не fact")
    for key in sorted(set(want) | set(got), key=str):
        if abs(want.get(key, 0.0) - got.get(key, 0.0)) > 0.01:
            out.append(f"месяц факта изменён {key[0]} {key[1]} м{key[2]} {key[3].name}: "
                       f"в плане {got.get(key, 0.0):.2f}, в факте {want.get(key, 0.0):.2f}")
    if p.res.frozen_through != frozen:
        out.append(f"в результате «факт по месяц» {p.res.frozen_through}, во входе {frozen}")
    return out


# ── случаи ───────────────────────────────────────────────────────────────
CASES = []


def case(num, title):
    def wrap(fn):
        CASES.append((num, title, fn))
        return fn
    return wrap


def expect(cond, msg, out):
    if not cond:
        out.append(msg)


@case("01", "Зарплата целиком: оклад по справочнику, остаток надбавкой 122, приказ не нужен")
def c01(limit):
    path = build("c01", [emp("E1", "Инженер", 100000)],
                 [ctr("C_A", 1200000, order="да")], {"C_A": 100000})
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in range(1, 13):
        expect(abs(p.amount("E1", "C_A", m, K.SALARY) - 40400) < EPS, f"оклад м{m} ≠ 40400", out)
        expect(abs(p.amount("E1", "C_A", m, K.K122) - 59600) < EPS, f"122 м{m} ≠ 59600", out)
        expect(p.amount("E1", "C_A", m, K.ORDER_INCENTIVE) < EPS, f"приказ м{m} без нужды", out)
    return p, out


@case("02", "П2556: оклад+122 не выше предела на ставку, остаток — приказом, приказ раз в месяц")
def c02(limit):
    path = build("c02", [emp("E1", "Инженер", 130000)],
                 [ctr("C_A", 1600000, order="да"), ctr("C_B", 600000, salary="нет", k122="нет", order="да")],
                 {"C_A": 130000, "C_B": 50000})
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in range(1, 13):
        staff = p.total("E1", m, {K.SALARY, K.K122})
        expect(abs(staff - 110000) < EPS, f"м{m}: оклад+122 {staff:.0f} ≠ предел 110000", out)
        expect(abs(p.total("E1", m, {K.ORDER_INCENTIVE}) - 20000) < EPS, f"м{m}: приказ ≠ 20000", out)
    return p, out


@case("03", "П2556 без приказа и без 124/152 — решения нет; с дефицитом — дефицит 20 000")
def c03(limit):
    path = build("c03a", [emp("E1", "Инженер", 130000)], [ctr("C_A", 1600000)], {"C_A": 130000})
    ctx, res = solve(path, limit)
    out = []
    expect(res.solver_status != "OPTIMAL", f"без приказа статус {res.solver_status}, ждали INFEASIBLE", out)
    path = build("c03b", [emp("E1", "Инженер", 130000)], [ctr("C_A", 1600000)], {"C_A": 130000}, deficit="да")
    ctx, res = solve(path, limit)
    p = Plan(ctx, res)
    expect(p.status == "OPTIMAL", f"с дефицитом статус {p.status}", out)
    for m in range(1, 13):
        expect(abs(p.deficit.get(("E1", m), 0) - 20000) < EPS, f"м{m}: дефицит {p.deficit.get(('E1', m), 0):.0f} ≠ 20000", out)
    return p, out


@case("04", "122 только с окладом на том же договоре")
def c04(limit):
    path = build("c04", [emp("E1", "Инженер", 100000)],
                 [ctr("C_A", 500000), ctr("C_B", 900000, salary="нет", order="да")],
                 {"C_A": 41000, "C_B": 70000})
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in range(1, 13):
        expect(p.amount("E1", "C_B", m, K.K122) < EPS, f"м{m}: 122 на C_B без оклада", out)
        expect(p.amount("E1", "C_B", m, K.ORDER_INCENTIVE) > 0, f"м{m}: остаток не добран приказом с C_B", out)
    return p, out


@case("05", "120: только по листу, в окне договора секретности, ровно 5 % оклада; чужим — нет")
def c05(limit):
    path = build("c05", [emp("E1", "Инженер", 100000), emp("E2", "Инженер", 100000)],
                 [ctr("C_A", 2500000, k120="да"),
                  ctr("C_SEC", 60000, start="01.03.2026", end="31.08.2026", account="23000У00001",
                      salary="нет", k122="нет", k120="да")],
                 {"C_A": 200000, "C_SEC": [0, 0, 5000, 5000, 5000, 5000, 5000, 5000, 0, 0, 0, 0]},
                 secret=[("E1", "C_SEC", 0.05)])
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    expect(p.months_with("E1", K.K120) == [3, 4, 5, 6, 7, 8], f"120 у E1 в месяцах {p.months_with('E1', K.K120)}", out)
    expect(p.months_with("E2", K.K120) == [], "120 у E2, которого нет в листе", out)
    for m in range(3, 9):
        expect(abs(p.total("E1", m, {K.K120}) - 2020) < EPS, f"м{m}: 120 {p.total('E1', m, {K.K120}):.0f} ≠ 2020", out)
    return p, out


@case("33", "120: две строки секретности с разными окнами у одного сотрудника; своё окно строки важнее срока договора")
def c33(limit):
    path = build("c33", [emp("E1", "Инженер", 100000)],
                 [ctr("C_A", 2500000, k120="да"),
                  ctr("C_S1", 0, account="23000У00001", salary="нет", k122="нет", k120="да"),
                  ctr("C_S2", 0, account="23000У00002", salary="нет", k122="нет", k120="да")],
                 {"C_A": 200000},
                 secret=[("E1", "C_S1", 0.05, "01.02.2026", "31.03.2026", None),
                         ("E1", "C_S2", 0.05, "01.10.2026", "30.11.2026", None)])
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    expect(p.months_with("E1", K.K120) == [2, 3, 10, 11],
           f"120 у E1 в месяцах {p.months_with('E1', K.K120)}, ждали 2, 3, 10, 11", out)
    return p, out


@case("34", "120 с закреплённого договора: оклад уходит туда же; закреплён счёт 23 — 120 оттуда без оклада")
def c34(limit):
    path = build("c34a", [emp("E1", "Инженер", 100000)],
                 [ctr("C_A", 1300000, k120="да"), ctr("C_B", 1300000, k120="да"),
                  ctr("C_SEC", 0, account="23000У00001", salary="нет", k122="нет", k120="да")],
                 {"C_A": 110000, "C_B": 110000},
                 secret=[("E1", "C_SEC", 0.05, None, None, "C_B")])
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in range(1, 13):
        expect(p.amount("E1", "C_A", m, K.K120) < EPS, f"м{m}: 120 с незакреплённого C_A", out)
        expect(p.amount("E1", "C_SEC", m, K.K120) < EPS, f"м{m}: 120 с незакреплённого C_SEC", out)
        expect(abs(p.amount("E1", "C_B", m, K.K120) - 2020) < EPS,
               f"м{m}: 120 с C_B {p.amount('E1', 'C_B', m, K.K120):.0f} ≠ 2020", out)
        expect(p.amount("E1", "C_B", m, K.SALARY) > 0, f"м{m}: оклад не на закреплённом C_B", out)
    path = build("c34b", [emp("E1", "Инженер", 100000)],
                 [ctr("C_A", 2500000, k120="да"),
                  ctr("C_SEC", 30000, account="23000У00001", salary="нет", k122="нет", k120="да")],
                 {"C_A": 200000, "C_SEC": 2500},
                 secret=[("E1", "C_SEC", 0.05, None, None, "C_SEC")])
    ctx, res = solve(path, limit)
    p2 = Plan(ctx, res)
    expect(p2.status == "OPTIMAL", f"счёт 23: статус {p2.status}", out)
    for m in range(1, 13):
        expect(p2.amount("E1", "C_A", m, K.K120) < EPS, f"счёт 23 м{m}: 120 с C_A", out)
        expect(abs(p2.amount("E1", "C_SEC", m, K.K120) - 2020) < EPS,
               f"счёт 23 м{m}: 120 с C_SEC {p2.amount('E1', 'C_SEC', m, K.K120):.0f} ≠ 2020", out)
    return p, out


@case("06", "«120 разрешена = нет» на договоре оклада: оклад уходит туда, где 120 разрешена; одному такому — нет решения")
def c06(limit):
    path = build("c06a", [emp("E1", "Инженер", 100000)],
                 [ctr("C_A", 1300000, k120="нет"), ctr("C_B", 1300000, k120="да")],
                 {"C_A": 110000, "C_B": 110000},
                 secret=[("E1", "C_A", 0.05)])
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in range(1, 13):
        expect(p.amount("E1", "C_A", m, K.K120) < EPS, f"м{m}: 120 с договора, где она запрещена", out)
        expect(abs(p.amount("E1", "C_B", m, K.K120) - 2020) < EPS, f"м{m}: 120 с C_B {p.amount('E1', 'C_B', m, K.K120):.0f} ≠ 2020", out)
        expect(p.amount("E1", "C_B", m, K.SALARY) > 0, f"м{m}: оклад не на C_B", out)
    path = build("c06b", [emp("E1", "Инженер", 100000)],
                 [ctr("C_A", 1300000, k120="нет")], {"C_A": 110000},
                 secret=[("E1", "C_A", 0.05)])
    ctx, res = solve(path, limit)
    expect(res.solver_status != "OPTIMAL", f"единственный договор с запретом 120: статус {res.solver_status}, ждали INFEASIBLE", out)
    return p, out


@case("07", "152 вытесняет остальные надбавки в месяце (П2556 заставляет уйти в 152)")
def c07(limit):
    path = build("c07", [emp("E1", "Инженер", 130000)],
                 [ctr("C_A", 1600000, k152="да")], {"C_A": 130000})
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in range(1, 13):
        expect(p.total("E1", m, {K.K152}) > 0, f"м{m}: без 152 зарплата не закрывается", out)
        expect(p.total("E1", m, {K.K122, K.K124, K.K120, K.ORDER_INCENTIVE}) < EPS, f"м{m}: 152 вместе с другими", out)
        expect(abs(p.total("E1", m, {K.K152}) - 89600) < EPS, f"м{m}: 152 ≠ 89600", out)
    return p, out


@case("08", "П4: в месяце со 124 оклад+122+124 режется ровно по пределу, остаток приказом")
def c08(limit):
    path = build("c08", [emp("E1", "Ведущий инженер", 160000)],
                 [ctr("C_A", 2000000, k124="да", order="да")], {"C_A": 160000})
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in range(1, 13):
        k124 = p.total("E1", m, {K.K124})
        staff = p.total("E1", m, {K.SALARY, K.K122, K.K124})
        expect(k124 > 0, f"м{m}: 124 не назначена, хотя без неё П2556 не даёт закрыть", out)
        expect(abs(staff - 149648.9) < EPS, f"м{m}: оклад+122+124 {staff:.0f} ≠ 149 649", out)
        expect(abs(p.total("E1", m, {K.ORDER_INCENTIVE}) - (160000 - 149648.9)) < EPS, f"м{m}: приказ ≠ остаток", out)
    return p, out


@case("09", "П4 с трудоёмкостью: суммарная ставка не превышает 1,5")
def c09(limit):
    path = build("c09", [emp("E1", "Ведущий инженер", 230000)],
                 [ctr("C_A", 2400000, k124="да", order="да", dep="Отдел 12"),
                  ctr("C_B", 1200000, k124="да", order="да", dep="Лаборатория 3")],
                 {"C_A": 200000, "C_B": 100000},
                 # На C_B открывается совместительство 0,5: шесть месяцев
                 # по 0,5 чел.-мес. помещаются в общий предел 1,5.
                 labor=[("C_B", 3, "Ведущий инженер", 60000, 1)])
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in range(1, 13):
        rt = p.rate_total("E1", m)
        staff = p.total("E1", m, {K.SALARY, K.K122, K.K124})
        if p.total("E1", m, {K.K124}) > 0:
            expect(abs(staff - 149648.9 * rt) < EPS, f"м{m}: штатная {staff:.0f} ≠ 149 649×{rt}", out)
    for m in range(1, 13):
        expect(p.rate_total("E1", m) <= 1.5 + EPS,
               f"м{m}: суммарная ставка {p.rate_total('E1', m):.2f} > 1,5", out)
    return p, out


@case("10", "БЭП по ГОЗ: средняя оклад+122 на ставку не выше 112 261, остаток приказом")
def c10(limit):
    path = build("c10", [emp("E1", "Ведущий инженер", 150000), emp("E2", "Ведущий инженер", 150000)],
                 [ctr("C_G", 3600000, goz="да", order="да")], {"C_G": 300000})
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in range(1, 13):
        staff = sum(p.total(e, m, {K.SALARY, K.K122}, "C_G") for e in ("E1", "E2"))
        expect(staff <= 2 * BEP + EPS, f"м{m}: оклад+122 {staff:.0f} > 2×БЭП", out)
        expect(staff >= 2 * BEP - 1000, f"м{m}: БЭП не выбран ({staff:.0f}), приказ съел лишнее", out)
    return p, out


@case("10b", "БЭП: средняя ровно БЭП — недобор убирается второй стадией, сразу после трудоёмкости")
def c10b(limit):
    # Владелец продукта 30.09.2026: «по БЭП не критично, если меньше
    # получается, но лучше ровно… делай ровно среднюю». Выше БЭП нельзя
    # (правило 7), ниже — только если иначе не выходит: стадия недобора
    # стоит сразу за трудоёмкостью, раньше прочих.
    wage = round(BEP)
    path = build("c10b", [emp("E1", "Ведущий инженер", wage), emp("E2", "Ведущий инженер", wage)],
                 [ctr("C_G", 24 * wage, goz="да")], {"C_G": 2 * wage},
                 labor=[("C_G", 24, "Ведущий инженер", wage, 2)])
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    stages = [g.name for g in res.goals if str(g.priority).startswith("стадия")]
    bep, labor = "недобор средней БЭП по ГОЗ", "отклонения трудоёмкости"
    expect(bep in stages and labor in stages and stages.index(bep) == stages.index(labor) + 1,
           f"порядок стадий: {stages}", out)
    for m in range(1, 13):
        staff = sum(p.total(e, m, {K.SALARY, K.K122}, "C_G") for e in ("E1", "E2"))
        expect(abs(staff - 2 * BEP) < 1000, f"м{m}: оклад+122 {staff:.0f}, ждали ровно 2×БЭП", out)
    return p, out


@case("11", "БЭП: одна строка выше БЭП допустима, если средняя в пределе")
def c11(limit):
    path = build("c11", [emp("E1", "Ведущий инженер", 130000), emp("E2", "Инженер", 80000)],
                 [ctr("C_G", 2600000, goz="да", order="да")], {"C_G": 210000})
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    above = [m for m in range(1, 13) if p.total("E1", m, {K.SALARY, K.K122}, "C_G") > BEP + EPS]
    expect(len(above) == 12, f"E1 выше БЭП только в {len(above)} месяцах, ждали 12 (средняя позволяет)", out)
    return p, out


@case("11b", "БЭП по должности: предел договора — сумма БЭП × ставка, а не наименьший для всех")
def c11b(limit):
    path = build("c11b", [emp("E1", "Ведущий инженер", 160000), emp("E2", "Инженер", 120000)],
                 [ctr("C_G", 3400000, goz="да", order="да")], {"C_G": 280000},
                 bep={"Ведущий инженер": 130000, "Инженер": 100000})
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in range(1, 13):
        staff = sum(p.total(e, m, {K.SALARY, K.K122}, "C_G") for e in ("E1", "E2"))
        expect(staff <= 230000 + EPS, f"м{m}: оклад+122 {staff:.0f} > 130 000 + 100 000", out)
        expect(staff >= 230000 - 1000, f"м{m}: {staff:.0f} — предел по должностям не выбран (старый общий был бы 200 000)", out)
    return p, out


@case("12", "ГОЗ с «124 разрешена = да» во входе: решатель 124 не запрещает")
def c12(limit):
    path = build("c12", [emp("E1", "Ведущий инженер", 160000)],
                 [ctr("C_G", 2000000, goz="да", k124="да", order="да")], {"C_G": 160000})
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    k124 = sum(p.total("E1", m, {K.K124}) for m in range(1, 13))
    out.append(f"справка: 124 на ГОЗ выплачено {k124:.0f} — запрет живёт только во входе (графа договора)")
    return p, out


@case("13", "Приоритет: оклад на другом договоре → на приоритете только 152")
def c13(limit):
    path = build("c13", [emp("E1", "Инженер", 100000)],
                 [ctr("C_BASE", 500000), ctr("C_PRIO", 900000, priority="да", k152="да")],
                 {"C_BASE": 41000, "C_PRIO": 70000},
                 manual=[("E1", "C_BASE", 1, 12, "оклад", None)])
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in range(1, 13):
        expect(p.total("E1", m, {K.SALARY, K.K122, K.K120, K.K124}, "C_PRIO") < EPS, f"м{m}: штатные на приоритете при окладе на C_BASE", out)
        expect(p.total("E1", m, {K.K152}, "C_PRIO") > 0, f"м{m}: 152 с приоритета нет", out)
    return p, out


@case("14", "Приоритет: на договоре в месяце либо штатные, либо 152")
def c14(limit):
    path = build("c14", [emp("E1", "Инженер", 130000)],
                 [ctr("C_BASE", 700000), ctr("C_PRIO", 1600000, priority="да", k152="да")],
                 {"C_BASE": 60000, "C_PRIO": 130000})
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in range(1, 13):
        kinds = p.kinds[("E1", "C_PRIO", m)]
        expect(not (K.K152 in kinds and kinds & {K.SALARY, K.K122}), f"м{m}: штатные и 152 вместе", out)
    return p, out


@case("15", "Строки человека: основное + совместительство; совместительство без основного — ошибка входа")
def c15(limit):
    from fot_planner.validation import validate_context
    path = build("c15a", [emp("E1", "Инженер", 50000, rate=0.5, etype="совместительство")],
                 [ctr("C_A", 700000, order="да")], {"C_A": 60000})
    codes = [c.code for c in validate_context(load_context(path))]
    out = []
    expect("PART_TIME_WITHOUT_MAIN" in codes, f"совместительство без основного не отклонено: {codes}", out)
    # две строки одного человека: основное 1,0 инженером и 0,25 аналитиком
    e1 = emp("E1-1", "Инженер", 100000); e1[1] = "Иванов Иван Иванович"
    e2 = emp("E1-2", "Аналитик", 20000, rate=0.25, etype="совместительство"); e2[1] = "Иванов Иван Иванович"
    path = build("c15b", [e1, e2], [ctr("C_A", 1600000, order="да")], {"C_A": 130000})
    ctx, res = solve(path, limit)
    p = Plan(ctx, res)
    expect(p.status == "OPTIMAL", f"две строки: статус {p.status}", out)
    for m in range(1, 13):
        expect(p.rate.get(("E1-1", "C_A", m), (0, False)) == (1.0, True), f"м{m}: основное E1-1 не 1,0", out)
        r2 = p.rate.get(("E1-2", "C_A", m))
        expect(r2 is not None and abs(r2[0] - 0.25) < 1e-6 and not r2[1], f"м{m}: строка E1-2 {r2}", out)
    return p, out


@case("16", "Совместительство сверх основного: не больше 0,5, всего 1,5; чужое подразделение")
def c16(limit):
    path = build("c16", [emp("E1", "Инженер", 100000)],
                 [ctr("C_A", 1300000, dep="Отдел 12"), ctr("C_B", 500000, dep="Лаборатория 3")],
                 {"C_A": 110000, "C_B": 40000},
                 labor=[("C_B", 9, "Инженер", 30000, 1)])
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    # При жёстком допуске 5 % план 9 чел.-мес. нельзя закрыть одной
    # строкой совместительства с пределом 0,5 в месяц (максимум 6 за год).
    expect(p.status == "INFEASIBLE", f"статус {p.status}", out)
    return p, out


@case("17", "Два совместительства по 0,25 на разных договорах")
def c17(limit):
    path = build("c17", [emp("E1", "Инженер", 100000)],
                 [ctr("C_A", 1300000, dep="Отдел 12"), ctr("C_B", 200000, dep="Лаборатория 3"),
                  ctr("C_C", 200000, dep="Лаборатория 4")],
                 {"C_A": 110000, "C_B": 15000, "C_C": 15000},
                 labor=[("C_B", 3, "Инженер", 45000, 1), ("C_C", 3, "Инженер", 45000, 1)])
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for c in ("C_B", "C_C"):
        pm = p.pm_row.get((c, "Инженер"), 0)
        expect(2.85 <= pm <= 3.15, f"{c}: закрыто {pm} чел.-мес. вместо 3", out)
    both = [m for m in range(1, 13) if p.rate.get(("E1", "C_B", m)) and p.rate.get(("E1", "C_C", m))]
    out.append(f"справка: месяцев с двумя совместительствами сразу — {len(both)}")
    return p, out


@case("18", "Студент ≤ 0,5, аспирант ≤ 0,75, лаборант без дополнительных ставок")
def c18(limit):
    # На C_B основное место запрещено, а строка РКМ «Инженер» — 3 чел.-мес. за
    # год: закрыть её может только аспирант совместительством 0,25 × 12.
    # Студент держит штатную ставку без доп. ставок, лаборант — тоже. Раньше
    # тут была ещё строка «Лаборант» на 6 чел.-мес.: при жёстком допуске 5 %
    # её некому закрыть, и план честно невозможен.
    path = build("c18", [emp("S1", "Инженер", 40000, rate=0.5, cat="студент", dep="Отдел 12"),
                         emp("A1", "Инженер", 40000, rate=0.5, cat="аспирант", dep="Отдел 12"),
                         emp("L1", "Лаборант", 38300, rate=1.0, dep="Отдел 12")],
                 [ctr("C_A", 1600000, dep="Отдел 12"),
                  ctr("C_B", 900000, dep="Лаборатория 3", main="нет")],
                 {"C_A": 130000, "C_B": 70000},
                 # Стоимость чел.-мес. — оклад инженера 40 400: с C_B идёт
                 # только оклад за 0,25, надбавки добираются с C_A.
                 labor=[("C_B", 3, "Инженер", 40400, 1)])
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in range(1, 13):
        expect(p.rate_total("S1", m) <= 0.5 + 1e-6, f"м{m}: студент {p.rate_total('S1', m)}", out)
        expect(p.rate_total("A1", m) <= 0.75 + 1e-6, f"м{m}: аспирант {p.rate_total('A1', m)}", out)
        expect(p.rate_total("L1", m) <= 1.0 + 1e-6, f"м{m}: лаборант {p.rate_total('L1', m)}", out)
    asp = sum(p.rate_total("A1", m) for m in range(1, 13))
    expect(asp > 6.0 + 1e-6, "аспирант не взял совместительство, хотя 0,25 разрешено", out)
    expect(abs(p.pm_row.get(("C_B", "Инженер"), 0) - 3) < 0.16, f"строка C_B/Инженер: {p.pm_row.get(('C_B', 'Инженер'), 0)} чел.-мес. вместо 3", out)
    return p, out


@case("19", "Замещение направленное: ведущий инженер закрывает программиста, аналитик — нет")
def c19(limit):
    path = build("c19", [emp("E1", "Ведущий инженер", 100000), emp("E2", "Аналитик", 90000)],
                 [ctr("C_A", 2500000), ctr("C_P", 700000, dep="Лаборатория 3")],
                 {"C_A": 200000, "C_P": 60000},
                 labor=[("C_P", 6, "Программист", 100000, 1)])
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    expect(not any(c == "C_P" for (e, c, m, k) in p.pay if e == "E2"), "аналитик получил выплаты с C_P", out)
    pm = p.pm_row.get(("C_P", "Программист"), 0)
    expect(pm >= 5.7, f"ведущий инженер закрыл {pm} чел.-мес. из 6", out)
    # чужую строку закрывает любой своей ставкой, но должность остаётся своей
    for m in range(1, 13):
        if p.rate.get(("E1", "C_P", m)):
            expect((p.rate_pos.get(("E1", "C_P", m)) or "").lower() in ("", "ведущий инженер") or not p.rate[("E1", "C_P", m)][1],
                   f"м{m}: основное место подписано чужой должностью", out)
    return p, out


@case("20", "Замещение в обратную сторону не работает: программист не закроет ведущего инженера")
def c20(limit):
    path = build("c20", [emp("E1", "Программист", 90000)],
                 [ctr("C_A", 1300000), ctr("C_P", 700000, dep="Лаборатория 3")],
                 {"C_A": 110000, "C_P": 60000},
                  labor=[("C_P", 6, "Ведущий инженер", 90000, 1)])
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "INFEASIBLE", f"статус {p.status}", out)
    return p, out


@case("21", "Трудоёмкость: ±5 % по чел.-мес. и сумме, людей не больше заданного")
def c21(limit):
    path = build("c21", [emp("E1", "Инженер", 100000), emp("E2", "Инженер", 100000)],
                 [ctr("C_A", 2500000, dep="Отдел 12"), ctr("C_L", 700000, dep="Лаборатория 3")],
                 {"C_A": 210000, "C_L": 60000},
                 labor=[("C_L", 6, "Инженер", 50000, 1)])
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    pm = p.pm_row.get(("C_L", "Инженер"), 0)
    expect(5.7 - 1e-6 <= pm <= 6.3 + 1e-6, f"чел.-мес. {pm} вне 6 ± 5 %", out)
    amt = p.lpay.get(("C_L", "Инженер"), 0)
    expect(285000 - EPS <= amt <= 315000 + EPS, f"сумма по строке {amt:.0f} вне 300 000 ± 5 %", out)
    for m in range(1, 13):
        n = len(p.people_row.get(("C_L", "Инженер", m), ()))
        expect(n <= 1, f"м{m}: на строке {n} человека при пределе 1", out)
    return p, out


@case("22", "Трудоёмкость: деньги строки только от людей со ставкой на договоре")
def c22(limit):
    path = build("c22", [emp("E1", "Инженер", 100000), emp("E2", "Ведущий инженер", 120000)],
                 [ctr("C_A", 3000000, dep="Отдел 12"), ctr("C_L", 700000, dep="Лаборатория 3", k124="да")],
                 {"C_A": 240000, "C_L": 60000},
                 labor=[("C_L", 6, "Инженер", 50000, 1)])
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for r in res.labor_payment_attributions:
        if r.amount > EPS:
            expect(p.rate.get((r.employee_id, r.contract_id, r.month)) is not None,
                   f"деньги строки от {r.employee_id} м{r.month} без ставки на {r.contract_id}", out)
    return p, out


@case("23", "Касса: деньги не раньше поступления — без дефицита решения нет, с дефицитом 11 месяцев")
def c23(limit):
    inflow = [0] * 11 + [1200000]
    path = build("c23a", [emp("E1", "Инженер", 100000)], [ctr("C_A", 1200000)], {"C_A": inflow})
    ctx, res = solve(path, limit)
    out = []
    expect(res.solver_status != "OPTIMAL", f"без дефицита статус {res.solver_status}", out)
    path = build("c23b", [emp("E1", "Инженер", 100000)], [ctr("C_A", 1200000)], {"C_A": inflow}, deficit="да")
    ctx, res = solve(path, limit)
    out.append(f"справка: пустая касса и дефицит разрешён — статус {res.solver_status}: "
               "оклад открытой ставки дефицитом не покрывается, дефицит бывает только сверх оклада")
    # частичный дефицит: денег хватает на оклад, но не на всю зарплату
    path = build("c23c", [emp("E1", "Инженер", 100000)], [ctr("C_A", 1200000)],
                 {"C_A": [60000] * 11 + [540000]}, deficit="да")
    ctx, res = solve(path, limit)
    p = Plan(ctx, res)
    expect(p.status == "OPTIMAL", f"частичный дефицит: статус {p.status}", out)
    months = sorted(m for (e, m), v in p.deficit.items() if v > EPS)
    expect(len(months) >= 6, f"дефицит в месяцах {months}", out)
    expect(abs(p.by_em.get(("E1", 12), 0) - 100000) < EPS, "декабрь не выплачен целиком", out)
    return p, out


@case("24", "Сроки: договор до 30.06, оклад до 31.05 — после этих дат с него не платят")
def c24(limit):
    path = build("c24", [emp("E1", "Инженер", 100000)],
                 [ctr("C_A", 600000, end="30.06.2026", salary_deadline="31.05.2026", allowance_deadline="30.06.2026"),
                  ctr("C_B", 800000, start="01.06.2026")],
                 {"C_A": [100000] * 6 + [0] * 6, "C_B": [0] * 5 + [110000] * 7})
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    expect(p.months_with("E1", K.SALARY, "C_A") == [1, 2, 3, 4, 5], f"оклад с C_A в {p.months_with('E1', K.SALARY, 'C_A')}", out)
    expect(max(p.months_with("E1", K.K122, "C_A") or [0]) <= 6, "надбавка с C_A после июня", out)
    expect(min(p.months_with("E1", K.SALARY, "C_B") or [13]) >= 6, "оклад с C_B до начала договора", out)
    return p, out


@case("25", "Ручные назначения и запреты")
def c25(limit):
    path = build("c25", [emp("E1", "Инженер", 100000)],
                 [ctr("C_A", 1300000), ctr("C_B", 1300000)], {"C_A": 110000, "C_B": 110000},
                 manual=[("E1", "C_B", 3, 6, "оклад", None)],
                 forbid=[("E1", "C_A", 3, 6, None)])
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in (3, 4, 5, 6):
        expect(p.amount("E1", "C_B", m, K.SALARY) > 0, f"м{m}: оклад не на C_B", out)
        expect(p.total("E1", m, set(K), "C_A") < EPS, f"м{m}: выплата с запрещённого C_A", out)
    return p, out


@case("26", "Макс договоров оклада в год = 1: несовместимые годовые лимиты делают план невозможным")
def c26(limit):
    path = build("c26", [emp("E1", "Инженер", 100000)],
                 [ctr("C_A", 700000), ctr("C_B", 700000), ctr("C_C", 700000)],
                 {"C_A": [110000] * 6 + [0] * 6, "C_B": [0] * 6 + [110000] * 6, "C_C": 110000},
                 max_contracts=1)
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "INFEASIBLE", f"статус {p.status}", out)
    return p, out


@case("27", "Одна должность в одном подразделении: совместительство инженером только в чужом отделе")
def c27(limit):
    out, plans = [], []
    for dep, want in (("Отдел 12", 0), ("Лаборатория 3", 1)):
        path = build("c27_" + ("own" if want == 0 else "other"), [emp("E1", "Инженер", 100000, dep="Отдел 12")],
                      [ctr("C_A", 1300000, dep="Отдел 12"), ctr("C_B", 700000, dep=dep)],
                      {"C_A": 110000, "C_B": 60000}, labor=[("C_B", 6, "Инженер", 100000, 1)],
                     manual=[("E1", "C_A", 1, 12, "оклад", None)])
        ctx, res = solve(path, limit)
        p = Plan(ctx, res)
        plans.append(p)
        expect(p.status == ("INFEASIBLE" if want == 0 else "OPTIMAL"),
               f"{dep}: статус {p.status}", out)
        part = sum(v for (e, c, m), (v, main) in p.rate.items() if c == "C_B")
        if want == 0:
            expect(part < 1e-6, f"{dep}: совместительство инженером в своём отделе {part}", out)
        else:
            expect(part > 5.7, f"{dep}: в чужом отделе совместительство {part} чел.-мес. вместо ~6", out)
    return plans[-1], out


@case("28", "Приказ ограничен П2556 отдельно и один в месяц при двух договорах")
def c28(limit):
    path = build("c28", [emp("E1", "Инженер", 200000)],
                 [ctr("C_A", 1600000, order="да"), ctr("C_B", 1600000, salary="нет", k122="нет", order="да")],
                 {"C_A": 110000, "C_B": 100000}, deficit="да")
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in range(1, 13):
        n = sum(1 for (e, c, mm, k) in p.pay if mm == m and k is K.ORDER_INCENTIVE)
        expect(n <= 1, f"м{m}: приказов {n}", out)
        expect(p.total("E1", m, {K.ORDER_INCENTIVE}) <= 110000 + EPS, f"м{m}: приказ выше П2556", out)
    return p, out


@case("31", "Трудоёмкость по месяцам: этап июнь–сентябрь закрывается по 1,0 в своих месяцах и никогда вне их")
def c31(limit):
    path = build("c31", [emp("E1", "Инженер", 100000)],
                 [ctr("C_A", 1200000, order="да"), ctr("C_B", 500000, order="да")],
                 {"C_A": 100000, "C_B": 100000},
                 labor=[("C_B", 4, "Инженер", 100000, 1, {6: 1.0, 7: 1.0, 8: 1.0, 9: 1.0})])
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in range(1, 13):
        got = p.pm.get(("E1", "C_B", m), 0.0)
        if 6 <= m <= 9:
            expect(abs(got - 1.0) < 0.05, f"м{m}: закрыто {got:.2f} ≠ 1,0", out)
        else:
            expect(got < 1e-6, f"м{m}: {got:.2f} чел.-мес. вне этапа", out)
    return p, out


@case("32", "Остаток ФОТ без порога: хвост 300 ₽ допустим, договор с остатком один")
def c32(limit):
    # Денег на 300 ₽ больше, чем зарплаты за год: хвост неизбежен. Прежний
    # порог «ноль либо не меньше 1 000 ₽» делал такой план неразрешимым; теперь
    # хвост допустим, а стадия оставляет его на одном договоре, а не на двух.
    path = build("c32", [emp("E1", "Инженер", 100000)],
                 [ctr("C_A", 600000), ctr("C_B", 600300)],
                 {"C_A": [600000] + [0] * 11, "C_B": [600300] + [0] * 11})
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    if p.status != "OPTIMAL":
        return p, out
    fot = {c.id: c.total_fot for c in ctx.contracts}
    paid = {c: 0.0 for c in fot}
    for (e, c, m, k), v in p.pay.items():
        paid[c] += v
    left = {c: round(fot[c] - paid[c], 2) for c in fot}
    with_rest = [c for c, v in left.items() if v > 1.0]
    expect(len(with_rest) == 1, f"договоров с остатком {len(with_rest)}: {left}", out)
    expect(abs(sum(left.values()) - 300) < 2, f"общий остаток {sum(left.values()):.2f} вместо 300", out)
    return p, out


@case("35", "Зарплата по месяцам: с апреля новая сумма — январь–март прежняя, апрель–декабрь новая; дефицит от суммы месяца")
def c35(limit):
    # Лист «зарплата_по_месяцам» (правило 1, решение пользователя 19.09.2026):
    # своя сумма только у апреля–декабря, январь–март без записи получают
    # зарплату штатного расписания. Оклад по ставке прежний, прибавка уходит
    # в 122: 40 400 + 59 600 до апреля и 40 400 + 67 600 с апреля.
    path = build("c35a", [emp("E1", "Инженер", 100000)],
                 [ctr("C_A", 1300000)], {"C_A": 110000},
                 wages=[("E1", m, 108000) for m in range(4, 13)])
    ctx, res = solve(path, limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in range(1, 13):
        want = 100000 if m < 4 else 108000
        got = p.by_em.get(("E1", m), 0.0)
        expect(abs(got - want) < EPS, f"м{m}: выплачено {got:.0f} ≠ {want}", out)
        expect(abs(p.amount("E1", "C_A", m, K.SALARY) - 40400) < EPS, f"м{m}: оклад ≠ 40400", out)
    # Дефицит считается от суммы месяца: с апреля 130 000, а П2556 без
    # приказа даёт не больше 110 000 — недоплата 20 000 только с апреля.
    path = build("c35b", [emp("E1", "Инженер", 100000)],
                 [ctr("C_A", 1600000)], {"C_A": 130000}, deficit="да",
                 wages=[("E1", m, 130000) for m in range(4, 13)])
    ctx, res = solve(path, limit)
    p2 = Plan(ctx, res)
    expect(p2.status == "OPTIMAL", f"с дефицитом: статус {p2.status}", out)
    for m in range(1, 13):
        want = 0 if m < 4 else 20000
        got = p2.deficit.get(("E1", m), 0.0)
        expect(abs(got - want) < EPS, f"с дефицитом м{m}: дефицит {got:.0f} ≠ {want}", out)
    if p2.status == "OPTIMAL":
        out += ["с дефицитом: " + x for x in invariants(p2)]
    return p, out


@case("36", "Лист «зарплата_по_месяцам»: нет листа или пуст — как раньше; чужой код строки, месяц вне 1–12, отрицательная или нулевая сумма — ошибка входа")
def c36(limit):
    from fot_planner.validation import validate_context
    out = []
    staff = [emp("E1", "Инженер", 100000), emp("E2", "Инженер", 90000)]

    def load(name, wages):
        return load_context(build(name, staff, [ctr("C_A", 2500000)], {"C_A": 200000},
                                  wages=wages))

    for name, wages in (("c36_none", None), ("c36_empty", [])):
        got = {e.id: e.monthly_wages for e in load(name, wages).employees}
        expect(got == {"E1": {}, "E2": {}}, f"{name}: зарплата по месяцам {got}, ждали пусто", out)
    # Своя сумма — только у названных месяцев.
    ctx = load("c36_ok", [("E1", 4, 108000), ("E1", 12.0, 110000)])
    got = {e.id: e.monthly_wages for e in ctx.employees}
    expect(got == {"E1": {4: 108000.0, 12: 110000.0}, "E2": {}},
           f"лист прочитан как {got}", out)
    # Отрицательная сумма месяца, пришедшая не из листа, — ошибка валидации.
    ctx.employees[0].monthly_wages[6] = -1.0
    codes = [(c.code, c.employee_id, c.month) for c in validate_context(ctx)]
    expect(("INVALID_PAYMENT", "E1", 6) in codes, f"отрицательная сумма месяца не отклонена: {codes}", out)
    # Ошибка в листе — ошибка входа с названием листа, а не молчаливый пропуск.
    for name, wages, words in (
        ("c36_code", [("E9", 4, 108000)], ("«E9»", "не найден")),
        ("c36_month", [("E1", 13, 108000)], ("«13»", "от 1 до 12")),
        ("c36_frac", [("E1", 4.5, 108000)], ("«4.5»", "от 1 до 12")),
        ("c36_word", [("E1", "апрель", 108000)], ("«апрель»", "от 1 до 12")),
        ("c36_neg", [("E1", 4, -5000)], ("отрицательная", "«E1»")),
        # Ноль пока запрещён (решение пользователя 19.09.2026): месяц без
        # оклада среди оплачиваемых делает план нерешаемым.
        ("c36_zero", [("E1", 5, 0)], ("нулевая", "«E1»")),
        ("c36_blank", [("E1", 4, None)], ("не указана зарплата",)),
        ("c36_twice", [("E1", 4, 108000), ("E1", 4, 109000)], ("две суммы",)),
    ):
        try:
            load(name, wages)
        except ValueError as exc:
            text = str(exc)
            missing = [w for w in (WAGES_SHEET,) + words if w not in text]
            expect(not missing, f"{name}: в ошибке нет {missing}: {text}", out)
        else:
            out.append(f"{name}: строки {wages} приняты молча, ошибки входа нет")
    return None, out


@case("37", "Закрепление без вида на март–май: в каждом месяце выплата с договора, вид выбирает расчёт; без закрепления договор не платит")
def c37(limit):
    # MANUAL-ANY-001 (решение пользователя 19.09.2026): экономист закрепляет
    # сотрудника за договором, не называя вида выплаты. C_B разрешает только
    # приказ: без закрепления с него не платят (сумма приказов — стадия 6), а
    # пустой вид, прочитанный по-старому как оклад, закрепление терял — оклада
    # на C_B нет.
    staff = [emp("E1", "Инженер", 100000)]
    contracts = [ctr("C_A", 1300000), ctr("C_B", 300000, salary="нет", k122="нет", order="да")]
    inflow = {"C_A": 110000, "C_B": 30000}
    out = []
    ctx, res = solve(build("c37_free", staff, contracts, inflow), limit)
    free = Plan(ctx, res)
    expect(free.status == "OPTIMAL", f"без закрепления: статус {free.status}", out)
    for m in (3, 4, 5):
        expect(free.total("E1", m, set(K), "C_B") < EPS,
               f"без закрепления м{m}: C_B и так платит — случай ничего не доказывает", out)
    ctx, res = solve(build("c37", staff, contracts, inflow,
                           manual=[("E1", "C_B", 3, 5, None, None)]), limit)
    p = Plan(ctx, res)
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in (3, 4, 5):
        expect(p.total("E1", m, set(K), "C_B") > EPS, f"м{m}: с закреплённого C_B не заплачено", out)
    # Выплату пары в месяцах закрепления потребовал экономист — она ручная,
    # какой бы вид ни выбрал расчёт; остальные выплаты — нет.
    manual = sorted({(a.contract_id, a.month) for a in res.allocations if a.is_manual})
    expect(manual == [("C_B", 3), ("C_B", 4), ("C_B", 5)], f"ручными помечены {manual}", out)
    kinds = sorted({k.name for (e, c, m, k) in p.pay if c == "C_B"})
    out.append(f"справка: вид с C_B выбран расчётом — {', '.join(kinds) or 'нет'}")
    return p, out


@case("38", "Закрепление без вида, а договор в эти месяцы не платит или выплаты запрещены: решения нет, причина названа по-русски")
def c38(limit):
    # Месяц, где у пары нет ни одного допустимого вида, закрепление не
    # выполнит. Пропустить его молча нельзя, а голое «INFEASIBLE» экономисту
    # ничего не говорит: решатель называет пару, месяцы и причину.
    staff = [emp("E1", "Инженер", 100000)]
    contracts = [ctr("C_A", 1300000), ctr("C_B", 700000, end="30.06.2026")]
    inflow = {"C_A": 110000, "C_B": 60000}
    out = []
    for name, manual, forbid, words in (
        ("c38_window", [("E1", "C_B", 5, 8, None, None)], (),
         ("на июль–август", "платит только с 01.01.2026 по 30.06.2026")),
        ("c38_forbid", [("E1", "C_B", 3, 4, None, None)], [("E1", "C_B", 3, 4, None)],
         ("на март–апрель", "ручным запретом")),
    ):
        ctx, res = solve(build(name, staff, contracts, inflow, manual=manual, forbid=forbid), limit)
        expect(res.solver_status == "INFEASIBLE", f"{name}: статус {res.solver_status}, ждали INFEASIBLE", out)
        found = [c for c in res.conflicts if c.code == "MANUAL_ASSIGNMENT_NO_PAYMENT"]
        text = " ".join(c.message for c in found)
        missing = [w for w in ("Закрепление без вида", "E1", "C_B") + words if w not in text]
        expect(found and not missing,
               f"{name}: в причине нет {missing}: {[c.message for c in res.conflicts]}", out)
    return None, out


@case("39", "Лист «ручные_назначения»: пустой вид — закрепление без вида; сумма без вида — ошибка входа")
def c39(limit):
    staff = [emp("E1", "Инженер", 100000)]
    contracts = [ctr("C_A", 1300000), ctr("C_B", 300000)]
    inflow = {"C_A": 110000, "C_B": 30000}
    out = []
    ctx = load_context(build("c39_ok", staff, contracts, inflow, manual=[
        ("E1", "C_B", 3, 5, None, None), ("E1", "C_A", 1, 2, "оклад", None),
        ("E1", "C_A", 6, 6, "122", 5000)]))
    got = [(a.contract_id, a.month_from, a.month_to, a.payment_kind, a.fixed_amount)
           for a in ctx.manual_assignments]
    want = [("C_B", 3, 5, None, None), ("C_A", 1, 2, K.SALARY, None), ("C_A", 6, 6, K.K122, 5000.0)]
    expect(got == want, f"лист прочитан как {got}", out)
    # Сумма бывает только у конкретного вида: без вида её не с чем связать.
    try:
        load_context(build("c39_amount", staff, contracts, inflow,
                           manual=[("E1", "C_B", 3, 5, None, 20000)]))
    except ValueError as exc:
        text = str(exc)
        missing = [w for w in ("ручные_назначения", "сумма закрепления без вида выплаты",
                               "«E1»", "«C_B»") if w not in text]
        expect(not missing, f"в ошибке нет {missing}: {text}", out)
    else:
        out.append("сумма без вида принята молча, ошибки входа нет")
    return None, out


@case("40", "Закрепление видом «приказ» с суммой: лист загружается, в каждом месяце срока приказ ровно на сумму")
def c40(limit):
    # Сервис пишет вид закрепления коротким словом «приказ»
    # (app/build_input.fill_manual). До 30.09.2026 загрузчик знал только
    # «стимулирующая приказом» и падал: «Неизвестный вид выплаты: 'приказ'»,
    # и план с таким закреплением не считался вовсе.
    staff = [emp("E1", "Инженер", 100000)]
    contracts = [ctr("C_A", 1300000), ctr("C_B", 300000, salary="нет", k122="нет", order="да")]
    inflow = {"C_A": 110000, "C_B": 30000}
    out = []
    ctx, res = solve(build("c40", staff, contracts, inflow,
                           manual=[("E1", "C_B", 3, 5, "приказ", 20000)]), limit)
    p = Plan(ctx, res)
    got = [(a.contract_id, a.month_from, a.month_to, a.payment_kind, a.fixed_amount)
           for a in ctx.manual_assignments]
    expect(got == [("C_B", 3, 5, K.ORDER_INCENTIVE, 20000.0)], f"лист прочитан как {got}", out)
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in (3, 4, 5):
        got_m = p.amount("E1", "C_B", m, K.ORDER_INCENTIVE)
        expect(abs(got_m - 20000) < EPS, f"м{m}: приказ с C_B {got_m:.0f} ≠ 20 000", out)
    manual = sorted({(a.contract_id, a.month) for a in res.allocations if a.is_manual})
    expect(manual == [("C_B", 3), ("C_B", 4), ("C_B", 5)], f"ручными помечены {manual}", out)
    # Тем же словом пишется и запрет.
    ctx2 = load_context(build("c40_forbid", staff, contracts, inflow,
                              forbid=[("E1", "C_B", 1, 12, "приказ")]))
    kinds = [x.payment_kind for x in ctx2.manual_prohibitions]
    expect(kinds == [K.ORDER_INCENTIVE], f"запрет «приказ» прочитан как {kinds}", out)
    return p, out


# ── пересчёт с факта (правило 26, владелец продукта 30.09.2026) ─────────
# «нужно чтобы оптимизатор не трогая январь и февраль, пересчитал… Но учесть
# все правила про условия в квартал итд… если была смена, а правило — не
# более чем в квартал, то пусть учтет это». Факт — строки листа
# «факт_выплат»: (код строки, договор, месяц, вид, сумма, ставка, основное).
def fact_month(code, contract, month, salary=40400, k122=59600, rate=1.0, main="да"):
    """Факт месяца «оклад по ставке плюс 122» — как платили инженеру."""
    rows = [(code, contract, month, "оклад", salary, rate, main)]
    if k122:
        rows.append((code, contract, month, "122", k122))
    return rows


def salary_contracts(p: Plan, e, m):
    return sorted(c for (ee, c, mm, k) in p.pay if ee == e and mm == m and k is K.SALARY)


@case("41", "Факт январь–февраль: прошедшие месяцы в плане ровно по листу, помечены фактом, ничего не добавлено; «факт по месяц» в результате")
def c41(limit):
    from openpyxl import load_workbook as open_book
    from fot_planner.excel import export_result
    staff = [emp("E1", "Инженер", 100000)]
    contracts = [ctr("C_A", 1400000), ctr("C_B", 1300000)]
    inflow = {"C_A": 120000, "C_B": 110000}
    facts = fact_month("E1", "C_B", 1) + fact_month("E1", "C_B", 2)
    out = []
    # Без факта оклад с первого месяца на предпочтительном C_A: значит, в
    # январе и феврале план держит именно факт, а не свой выбор.
    ctx0, res0 = solve(build("c41_free", staff, contracts, inflow), limit)
    free = Plan(ctx0, res0)
    expect(salary_contracts(free, "E1", 1) == ["C_A"],
           f"без факта январь с {salary_contracts(free, 'E1', 1)} — случай ничего не доказывает", out)
    ctx, res = solve(build("c41", staff, contracts, inflow, facts=facts, frozen=2), limit)
    p = Plan(ctx, res)
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    expect(res.audit_status == "OK", f"аудит {res.audit_status}", out)
    expect(res.frozen_through == 2, f"в результате факт по месяц {res.frozen_through}", out)
    for m in (1, 2):
        rows = sorted((a.contract_id, a.payment_kind.name, a.amount, a.source, a.is_manual)
                      for a in res.allocations if a.month == m)
        want = [("C_B", "K122", 59600.0, "fact", True), ("C_B", "SALARY", 40400.0, "fact", True)]
        expect(rows == want, f"м{m}: в плане {rows}, ждали только факт {want}", out)
        expect(p.rate.get(("E1", "C_B", m)) == (1.0, True), f"м{m}: ставка {p.rate.get(('E1', 'C_B', m))}", out)
    # Выгрузка: сервис отличает факт по «источнику решения», а последний месяц
    # факта читает в «Итоге расчета» по названию показателя.
    book = WORK / "c41_result.xlsx"
    export_result(book, ctx, res)
    wb = open_book(book, read_only=True)
    try:
        head, *rows = list(wb["План выплат"].iter_rows(values_only=True))
        col = {name: i for i, name in enumerate(head)}
        sources = {(r[col["месяц"]], r[col["источник решения"]]) for r in rows}
        expect(sources >= {("Январь", "fact"), ("Февраль", "fact"), ("Март", "optimizer")},
               f"«источник решения» в «Плане выплат»: {sorted(sources)}", out)
        summary = {r[0]: r[1] for r in wb["Итог расчета"].iter_rows(values_only=True) if r and r[0]}
        expect(summary.get("Факт по месяц") == 2,
               f"«Факт по месяц» в «Итоге расчета»: {summary.get('Факт по месяц')!r}", out)
    finally:
        wb.close()
    # Пересчёт поверх прошлого результата: строки факта в нём «зафиксированы»,
    # но закреплениями не становятся — прошедшие месяцы даёт лист факта.
    again = load_context(WORK / "c41.xlsx", plan_path=book)
    expect(not again.baseline_plan,
           f"строки факта прочитаны как закрепления: {again.baseline_plan}", out)
    return p, out


@case("42", "Факт нарушает правила месяца — ставка 0,3, 122 на 500 ₽, оклад выше справочного, 124 где запрещена, П2556 — план всё равно строится, аудит чист")
def c42(limit):
    from fot_planner.validation import validate_context
    staff = [emp("E1", "Инженер", 100000), emp("E2", "Инженер", 100000)]
    contracts = [ctr("C_A", 2600000), ctr("C_B", 1300000)]
    inflow = {"C_A": 220000, "C_B": 110000}
    facts = [
        # январь: 0,3 ставки, хвост 122 в 500 ₽, зарплата не целиком
        ("E1", "C_A", 1, "оклад", 12120, 0.3, "да"), ("E1", "C_A", 1, "122", 500),
        # оклад выше справочного на ставку, 124 с договора, где она запрещена,
        # 122 с договора без оклада
        ("E2", "C_A", 1, "оклад", 50000, 1.0), ("E2", "C_A", 1, "124", 30000),
        ("E2", "C_B", 1, "122", 20000),
        # февраль: ставка не указана — выводится из оклада; П2556 превышен
        ("E1", "C_A", 2, "оклад", 40400), ("E1", "C_A", 2, "122", 59600),
        ("E2", "C_B", 2, "оклад", 40400, 1.0), ("E2", "C_B", 2, "122", 80000),
    ]
    ctx, res = solve(build("c42", staff, contracts, inflow, facts=facts, frozen=2), limit)
    p, out = Plan(ctx, res), []
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    expect(res.audit_status == "OK",
           f"аудит {res.audit_status}: {[v.message for v in res.audit_violations]}", out)
    expect(p.rate.get(("E1", "C_A", 1)) == (0.3, True), f"ставка января {p.rate.get(('E1', 'C_A', 1))}", out)
    expect(p.rate.get(("E1", "C_A", 2), (0, False))[0] == 1.0,
           f"ставка февраля не выведена из оклада: {p.rate.get(('E1', 'C_A', 2))}", out)
    codes = [(c.code, c.employee_id, c.contract_id) for c in validate_context(ctx)]
    expect(("FACT_RATE_DERIVED_WARNING", "E1", "C_A") in codes,
           f"о выведенной ставке не предупреждено: {codes}", out)
    return p, out


@case("43", "Смена договора оклада в феврале по факту: в марте и апреле смен нет (не чаще раза в три месяца); без смены в феврале расчёт меняет договор в марте")
def c43(limit):
    # Пример владельца продукта 30.09.2026: «если была смена, а правило — не
    # более чем в квартал, то пусть учтет это». C_A предпочтительный: без
    # запрета расчёт вернул бы оклад на него сразу.
    staff = [emp("E1", "Инженер", 100000)]
    contracts = [ctr("C_A", 1400000), ctr("C_B", 1300000)]
    inflow = {"C_A": 120000, "C_B": 110000}
    out = []
    ctx, res = solve(build("c43", staff, contracts, inflow, frozen=2,
                           facts=fact_month("E1", "C_A", 1) + fact_month("E1", "C_B", 2)), limit)
    p = Plan(ctx, res)
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in (3, 4):
        expect(salary_contracts(p, "E1", m) == ["C_B"],
               f"м{m}: оклад с {salary_contracts(p, 'E1', m)} — смена сразу после февральской", out)
    expect(salary_contracts(p, "E1", 5) == ["C_A"],
           f"май: оклад с {salary_contracts(p, 'E1', 5)} — после окна смена разрешена", out)
    # Контроль: в феврале смены не было — расчёт меняет договор уже в марте.
    ctx, res = solve(build("c43_control", staff, contracts, inflow, frozen=2,
                           facts=fact_month("E1", "C_B", 1) + fact_month("E1", "C_B", 2)), limit)
    control = Plan(ctx, res)
    expect(control.status == "OPTIMAL", f"контроль: статус {control.status}", out)
    expect(salary_contracts(control, "E1", 3) == ["C_A"],
           f"контроль: март с {salary_contracts(control, 'E1', 3)} — без смены в феврале ждали C_A", out)
    out += ["контроль: " + x for x in invariants(control)]
    # Вынужденная смена: C_B, на который перешли в феврале, в марте уже не
    # платит. История заняла окно, но из-за неё плана не может не быть —
    # смена в марте берётся с предупреждением, лишних смен нет.
    ctx, res = solve(build("c43_forced", staff, [contracts[0], ctr("C_B", 300000, end="28.02.2026")],
                           inflow, frozen=2,
                           facts=fact_month("E1", "C_A", 1) + fact_month("E1", "C_B", 2)), limit)
    forced = Plan(ctx, res)
    expect(forced.status == "OPTIMAL", f"вынужденная смена: статус {forced.status}", out)
    notes = [c.message for c in res.conflicts if c.code == "FROZEN_SALARY_BLOCK_WARNING"]
    expect(any("вынужденно" in n and "март" in n for n in notes),
           f"вынужденная смена не названа: {[c.message for c in res.conflicts]}", out)
    expect([salary_contracts(forced, "E1", m) for m in (3, 4, 5)] == [["C_A"]] * 3,
           f"вынужденная смена: март–май с {[salary_contracts(forced, 'E1', m) for m in (3, 4, 5)]}", out)
    out += ["вынужденная смена: " + x for x in invariants(forced)]
    return p, out


@case("44", "Договоры оклада за год считают факт: предел 1 и оклад с C_A в факте — предпочтительный C_C закрыт; факт с двух договоров при пределе 1 — не отказ, а предупреждение")
def c44(limit):
    staff = [emp("E1", "Инженер", 100000)]
    contracts = [ctr("C_A", 1300000), ctr("C_B", 1300000), ctr("C_C", 1500000)]
    inflow = {"C_A": 110000, "C_B": 110000, "C_C": 130000}
    out = []
    ctx, res = solve(build("c44_free", staff, contracts, inflow, max_contracts=1), limit)
    free = Plan(ctx, res)
    expect(salary_contracts(free, "E1", 6) == ["C_C"],
           f"без факта оклад с {salary_contracts(free, 'E1', 6)} — ждали предпочтительный C_C", out)
    ctx, res = solve(build("c44", staff, contracts, inflow, max_contracts=1, frozen=2,
                           facts=fact_month("E1", "C_A", 1) + fact_month("E1", "C_A", 2)), limit)
    p = Plan(ctx, res)
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in range(3, 13):
        expect(salary_contracts(p, "E1", m) == ["C_A"],
               f"м{m}: оклад с {salary_contracts(p, 'E1', m)} — C_A из факта уже занял предел", out)
    # История одна превысила предел: он поднимается до неё, новых договоров
    # оклада нет, отказа нет.
    ctx, res = solve(build("c44_over", staff, contracts, inflow, max_contracts=1, frozen=2,
                           facts=fact_month("E1", "C_A", 1) + fact_month("E1", "C_B", 2)), limit)
    over = Plan(ctx, res)
    expect(over.status == "OPTIMAL", f"факт сверх предела: статус {over.status}", out)
    used = sorted({c for (e, c, m, k) in over.pay if k is K.SALARY and m > 2})
    expect("C_C" not in used, f"факт сверх предела: после факта оклад и с C_C ({used})", out)
    notes = [c.code for c in res.conflicts]
    expect("FROZEN_MAX_CONTRACTS_WARNING" in notes, f"нет предупреждения о пределе: {notes}", out)
    out += ["сверх предела: " + x for x in invariants(over)]
    # Договор из факта кончился в феврале, предел 1 уже выбран: новый
    # договор берётся вынужденно и называется, а не «решения нет».
    ctx, res = solve(build("c44_forced", staff, [ctr("C_A", 300000, end="28.02.2026")] + contracts[1:],
                           inflow, max_contracts=1, frozen=2,
                           facts=fact_month("E1", "C_A", 1) + fact_month("E1", "C_A", 2)), limit)
    forced = Plan(ctx, res)
    expect(forced.status == "OPTIMAL", f"договор из факта кончился: статус {forced.status}", out)
    used = sorted({c for (e, c, m, k) in forced.pay if k is K.SALARY and m > 2})
    expect(len(used) == 1, f"договор из факта кончился: после факта оклад с {used}, ждали один новый", out)
    notes = [c.message for c in res.conflicts if c.code == "FROZEN_MAX_CONTRACTS_WARNING"]
    expect(any("вынужденно" in n for n in notes),
           f"вынужденный договор не назван: {[c.message for c in res.conflicts]}", out)
    out += ["договор из факта кончился: " + x for x in invariants(forced)]
    return p, out


@case("45", "ФОТ договора считает факт: остаток ФОТ за вычетом факта; факт сверх ФОТ — после него с договора ни рубля, предупреждение, а не отказ")
def c45(limit):
    # C_A предпочтительный (дольше C_B): расчёт хочет платить с него.
    staff = [emp("E1", "Инженер", 100000)]
    out = []
    facts = fact_month("E1", "C_A", 1) + fact_month("E1", "C_A", 2)
    # ФОТ 500 000, из них 200 000 ушло в январе–феврале. Касса (600 000 в
    # январе) дала бы после факта 400 000 — держит именно ФОТ: 300 000.
    ctx, res = solve(build("c45", staff, [ctr("C_A", 500000), ctr("C_B", 1500000, start="01.02.2026")],
                           {"C_A": [600000] + [0] * 11, "C_B": 130000}, facts=facts, frozen=2), limit)
    p = Plan(ctx, res)
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    ahead = sum(v for (e, c, m, k), v in p.pay.items() if c == "C_A" and m > 2)
    expect(abs(ahead - 300000) < EPS, f"после факта с C_A {ahead:.0f}, ждали 300 000 = ФОТ − факт", out)
    # ФОТ 150 000, а факт — 200 000: перерасход истории.
    ctx, res = solve(build("c45_over", staff, [ctr("C_A", 150000), ctr("C_B", 1500000, start="01.02.2026")],
                           {"C_A": [600000] + [0] * 11, "C_B": 130000}, facts=facts, frozen=2), limit)
    over = Plan(ctx, res)
    expect(over.status == "OPTIMAL", f"перерасход в факте: статус {over.status}", out)
    ahead = sum(v for (e, c, m, k), v in over.pay.items() if c == "C_A" and m > 2)
    expect(ahead < EPS, f"перерасход в факте: после факта с C_A ещё {ahead:.0f}", out)
    notes = [c.code for c in res.conflicts]
    expect("FROZEN_FOT_OVERSPENT_WARNING" in notes, f"нет предупреждения о перерасходе: {notes}", out)
    fot = [v for v in res.audit_violations if v.rule_id == "FOT-001"]
    expect(res.audit_status == "OK" and fot and all(v.severity == "warning" for v in fot),
           f"аудит {res.audit_status}: {[(v.severity, v.message) for v in fot]}", out)
    out += ["перерасход: " + x for x in invariants(over)]
    return p, out


@case("46", "Трудоёмкость РКМ за год считает факт: 2 чел.-мес. факта — после него 4 из 6; факт сверх коридора — будущее не добавляет, предупреждение")
def c46(limit):
    staff = [emp("E1", "Инженер", 100000, dep="Отдел 12")]
    contracts = [ctr("C_A", 1300000, dep="Отдел 12"), ctr("C_L", 1000000, dep="Лаборатория 3")]
    inflow = {"C_A": 110000, "C_L": 100000}
    facts = fact_month("E1", "C_L", 1) + fact_month("E1", "C_L", 2)
    out = []
    ctx, res = solve(build("c46", staff, contracts, inflow, facts=facts, frozen=2,
                           labor=[("C_L", 6, "Инженер", 100000, 1)]), limit)
    p = Plan(ctx, res)
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    before = sum(p.pm.get(("E1", "C_L", m), 0.0) for m in (1, 2))
    ahead = sum(p.pm.get(("E1", "C_L", m), 0.0) for m in range(3, 13))
    expect(abs(before - 2.0) < 1e-6, f"чел.-мес. факта на строке {before}, ждали 2", out)
    expect(3.7 - 1e-6 <= ahead <= 4.3 + 1e-6, f"после факта закрыто {ahead:.2f}, ждали 4 ± 5 % от 6", out)
    expect(5.7 - 1e-6 <= before + ahead <= 6.3 + 1e-6, f"за год {before + ahead:.2f} вне 6 ± 5 %", out)
    # Строка на 1,5 чел.-мес., а факт уже закрыл 2: будущее ничего не
    # добавляет, отказа нет.
    ctx, res = solve(build("c46_over", staff, contracts, inflow, facts=facts, frozen=2,
                           labor=[("C_L", 1.5, "Инженер", 100000, 1)]), limit)
    over = Plan(ctx, res)
    expect(over.status == "OPTIMAL", f"факт сверх коридора: статус {over.status}", out)
    ahead = sum(v for (e, c, m, k), v in over.pay.items() if c == "C_L" and m > 2)
    expect(ahead < EPS, f"факт сверх коридора: после факта с C_L ещё {ahead:.0f}", out)
    notes = [c.code for c in res.conflicts]
    expect("FROZEN_LABOR_BAND_WARNING" in notes, f"нет предупреждения о строке РКМ: {notes}", out)
    expect(res.audit_status == "OK", f"аудит {res.audit_status}: {[v.message for v in res.audit_violations]}", out)
    out += ["сверх коридора: " + x for x in invariants(over)]
    # Годовая строка на 12 чел.-мес., а в январе–феврале человек работал на
    # другом договоре: за десять месяцев больше 10 не закрыть. План есть,
    # недобор назван решателем, а проверка результата честно говорит, что
    # строка не закрыта.
    ctx, res = solve(build("c46_short", staff, contracts, inflow, frozen=2,
                           facts=fact_month("E1", "C_A", 1) + fact_month("E1", "C_A", 2),
                           labor=[("C_L", 12, "Инженер", 100000, 1)]), limit)
    expect(res.solver_status == "OPTIMAL", f"недобор факта: статус {res.solver_status}", out)
    notes = [c.message for c in res.conflicts if c.code == "FROZEN_LABOR_BAND_WARNING"]
    expect(any("не добрать" in n for n in notes),
           f"недобор строки РКМ не назван: {[c.message for c in res.conflicts]}", out)
    expect(res.audit_status == "FAIL" and any(v.rule_id == "LABOR-001" for v in res.audit_violations),
           f"недобор факта: аудит {res.audit_status}, {[v.rule_id for v in res.audit_violations]}", out)
    return p, out


@case("47", "Закрепления на январь–декабрь при факте за январь–февраль урезаются до марта: без вида и приказом с суммой — план есть, закреплён только расчёт")
def c47(limit):
    staff = [emp("E1", "Инженер", 100000)]
    contracts = [ctr("C_A", 1300000), ctr("C_B", 300000, salary="нет", k122="нет", order="да")]
    inflow = {"C_A": 110000, "C_B": 30000}
    ctx, res = solve(build("c47", staff, contracts, inflow, frozen=2,
                           facts=fact_month("E1", "C_A", 1) + fact_month("E1", "C_A", 2),
                           manual=[("E1", "C_B", 1, 12, None, None),
                                   ("E1", "C_B", 1, 12, "приказ", 5000)]), limit)
    p, out = Plan(ctx, res), []
    # В январе–феврале с C_B не платили: без урезания закрепление не
    # выполнить — MANUAL_ASSIGNMENT_NO_PAYMENT и «решения нет».
    expect(p.status == "OPTIMAL", f"статус {p.status}: {[c.message for c in res.conflicts]}", out)
    for m in range(3, 13):
        got = p.amount("E1", "C_B", m, K.ORDER_INCENTIVE)
        expect(abs(got - 5000) < EPS, f"м{m}: приказ с C_B {got:.0f} ≠ 5 000", out)
    for m in (1, 2):
        expect(p.total("E1", m, set(K), "C_B") < EPS, f"м{m}: в месяц факта добавлена выплата с C_B", out)
    expect(res.audit_status == "OK", f"аудит {res.audit_status}: {[v.message for v in res.audit_violations]}", out)
    return p, out


@case("48", "Член рабочей группы: по возможности вся зарплата с договора группы; не хватает денег у группы — остаток с другого договора, план есть")
def c48(limit):
    # Владелец продукта 30.09.2026: «какую выплату ему дать, решает расчёт —
    # это можно задать, только удалить нельзя, если не задано — то по
    # возможности ВСЮ». C_B предпочтительный для оклада: без этой цели с C_A
    # идёт только обязательная выплата закрепления.
    staff = [emp("E1", "Инженер", 100000)]
    contracts = [ctr("C_A", 1300000, order="да"), ctr("C_B", 1400000, order="да")]
    inflow = {"C_A": 110000, "C_B": 120000}
    out = []
    ctx, res = solve(build("c48_plain", staff, contracts, inflow,
                           manual=[("E1", "C_A", 1, 12, None, None)]), limit)
    plain = Plan(ctx, res)
    from_b = sum(v for (e, c, m, k), v in plain.pay.items() if c == "C_B")
    expect(from_b > EPS, "без «по возможности вся» C_B и так не платит — случай ничего не доказывает", out)
    ctx, res = solve(build("c48", staff, contracts, inflow,
                           manual=[("E1", "C_A", 1, 12, None, None, "да")]), limit)
    p = Plan(ctx, res)
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    for m in range(1, 13):
        got = p.total("E1", m, set(K), "C_A")
        expect(abs(got - 100000) < EPS, f"м{m}: с договора группы {got:.0f} из 100 000", out)
    goal = [g for g in res.goals if g.name == "выплаты членам рабочих групп с других договоров"]
    expect(goal and goal[0].unit == "₽" and abs(goal[0].value) < 1,
           f"стадия в целях: {[(g.name, g.value, g.unit) for g in goal]}", out)
    # У договора группы 600 000 на год: остальное — с C_B, и план есть.
    ctx, res = solve(build("c48_short", staff, [ctr("C_A", 600000, order="да"), contracts[1]],
                           {"C_A": [600000] + [0] * 11, "C_B": 120000},
                           manual=[("E1", "C_A", 1, 12, None, None, "да")]), limit)
    short = Plan(ctx, res)
    expect(short.status == "OPTIMAL", f"денег группы мало: статус {short.status}", out)
    from_a = sum(v for (e, c, m, k), v in short.pay.items() if c == "C_A")
    expect(abs(from_a - 600000) < EPS, f"денег группы мало: с C_A {from_a:.0f}, ждали все 600 000", out)
    out += ["денег группы мало: " + x for x in invariants(short)]
    # Место стадии: сразу за трудоёмкостью и БЭП.
    wage = round(BEP)
    ctx, res = solve(build("c48_order", [emp("E1", "Ведущий инженер", wage), emp("E2", "Ведущий инженер", wage)],
                           [ctr("C_G", 24 * wage, goz="да"), ctr("C_X", 300000)],
                           {"C_G": 2 * wage, "C_X": 25000},
                           labor=[("C_G", 24, "Ведущий инженер", wage, 2)],
                           manual=[("E1", "C_G", 1, 12, None, None, "да")]), limit)
    stages = [g.name for g in res.goals if str(g.priority).startswith("стадия")]
    order = ["отклонения трудоёмкости", "недобор средней БЭП по ГОЗ",
             "выплаты членам рабочих групп с других договоров"]
    expect(res.solver_status == "OPTIMAL" and stages[:3] == order, f"порядок стадий: {stages}", out)
    return p, out


@case("49", "Лист «факт_выплат» и «факт по месяц»: повторы складываются, ставка выводится, чужие строки и месяцы после факта — предупреждения; деньги уволенного считаются в ФОТ; ошибки входа названы")
def c49(limit):
    from fot_planner.validation import validate_context
    staff = [emp("E1", "Инженер", 100000)]
    # C_A предпочтительный: без учёта денег уволенного X9 расчёт потратил бы
    # с него больше ФОТ.
    contracts = [ctr("C_A", 1200000), ctr("C_B", 1500000, start="01.02.2026")]
    inflow = {"C_A": 110000, "C_B": 130000}
    out = []
    ctx = load_context(build("c49_none", staff, contracts, inflow))
    expect((ctx.frozen_through, ctx.actuals) == (0, []),
           f"без листа и графы: факт по месяц {ctx.frozen_through}, факт {ctx.actuals}", out)
    # «Факт по месяц» есть, а факта нет: прошедшие месяцы в плане пусты — об
    # этом предупреждение, а не молчание.
    codes = {c.code for c in validate_context(load_context(
        build("c49_nofacts", staff, contracts, inflow, frozen=2)))}
    expect("FACT_MISSING_WARNING" in codes, f"факт по месяц без факта: {sorted(codes)}", out)
    facts = [
        ("E1", "C_A", 1, "оклад", 20200, 0.5), ("E1", "C_A", 1, "оклад", 20200, 0.5),
        ("E1", "C_A", 1, 122, 59600),
        ("E1", "C_A", 2, "оклад", 40400), ("E1", "C_A", 2, "122", 59600),
        ("X9", "C_A", 1, "оклад", 150000, 1.0), ("X9", "C_A", 2, "приказ", 150000),
        ("E1", "C_Z", 2, "приказ", 1000),
        ("E1", "C_A", 3, "оклад", 99999, 1.0),
    ]
    ctx = load_context(build("c49", staff, contracts, inflow, facts=facts, frozen=2))
    got = {(a.employee_id, a.contract_id, a.month, a.payment_kind): (a.amount, a.rate, a.rate_derived)
           for a in ctx.actuals}
    for key, want in ((("E1", "C_A", 1, K.SALARY), (40400.0, 0.5, False)),
                      (("E1", "C_A", 1, K.K122), (59600.0, None, False)),
                      (("E1", "C_A", 2, K.SALARY), (40400.0, 1.0, True)),
                      (("X9", "C_A", 2, K.ORDER_INCENTIVE), (150000.0, None, False))):
        expect(got.get(key) == want, f"факт {key}: {got.get(key)}, ждали {want}", out)
    codes = {c.code for c in validate_context(ctx)}
    for code in ("FACT_AFTER_FROZEN_WARNING", "FACT_UNKNOWN_EMPLOYEE_WARNING",
                 "FACT_UNKNOWN_CONTRACT_WARNING", "FACT_RATE_DERIVED_WARNING"):
        expect(code in codes, f"нет {code}: {sorted(codes)}", out)
    blocking = sorted(c for c in codes if not c.endswith("_WARNING"))
    expect(not blocking, f"факт остановил расчёт: {blocking}", out)
    res = opt.solve(ctx, time_limit_sec=limit)
    p = Plan(ctx, res)
    expect(p.status == "OPTIMAL", f"статус {p.status}", out)
    spent_a = sum(a.amount for a in res.allocations if a.contract_id == "C_A")
    expect(spent_a <= 1200000 + EPS, f"с C_A {spent_a:.0f} при ФОТ 1 200 000 — деньги X9 не учтены", out)
    x9 = sorted((a.month, a.source) for a in res.allocations if a.employee_id == "X9")
    expect(x9 == [(1, "fact"), (2, "fact")], f"выплаты X9 в плане: {x9}", out)
    march = p.total("E1", 3, set(K))
    expect(abs(march - 100000) < EPS, f"март считает расчёт, а не факт после F: {march:.0f}", out)
    # Ошибки входа — с листом и строкой, а не молчаливый пропуск.
    for name, rows, frozen, words in (
        ("c49_month", [("E1", "C_A", 13, "оклад", 40400)], 2, ("факт_выплат", "«13»", "от 1 до 12")),
        ("c49_kind", [("E1", "C_A", 1, "премия", 40400)], 2, ("факт_выплат", "«премия»", "приказ")),
        ("c49_negative", [("E1", "C_A", 1, "оклад", 40400), ("E1", "C_A", 1, "оклад", -50000)], 2,
         ("факт_выплат", "отрицательная")),
        ("c49_frozen", [], 12, ("настройки", "«12»", "от 0 до 11")),
        ("c49_frozen_word", [], "март", ("настройки", "«март»", "от 0 до 11")),
    ):
        try:
            load_context(build(name, staff, contracts, inflow, facts=rows, frozen=frozen))
        except ValueError as exc:
            missing = [w for w in words if w not in str(exc)]
            expect(not missing, f"{name}: в ошибке нет {missing}: {exc}", out)
        else:
            out.append(f"{name}: принято молча, ошибки входа нет")
    return p, out


@case("50", "Оклад в последнем месяце факта переведён на договор, который строке потом не разрешён, а деньги договора факт уже израсходовал: план есть, вынужденные отступления названы")
def c50(limit):
    # Демо 30.09.2026 (scripts/demo_facts.py): в феврале оклад перевели на
    # договор, которого нет среди разрешённых строке. В марте оклад обязан
    # уйти с него — смена F→F+1 вынужденна, — а в мае человек садится на
    # строку РКМ гранта: две смены в окне март–май. Без запаса на смену
    # F→F+1 план был нерешаем уже на первой стадии.
    staff = [emp("E1", "Инженер", 100000, allowed="C_A, C_G", dep="Отдел 12")]
    contracts = [ctr("C_A", 1300000), ctr("C_X", 1300000),
                 ctr("C_G", 800000, start="01.05.2026", dep="Лаборатория 3")]
    inflow = {"C_A": 110000, "C_X": 110000, "C_G": [0, 0, 0, 0, 800000] + [0] * 7}
    labor = [("C_G", 8, "Инженер", 100000, 1, {m: 1.0 for m in range(5, 13)})]
    out = []
    ctx, res = solve(build("c50", staff, contracts, inflow, labor=labor, frozen=2,
                           facts=fact_month("E1", "C_A", 1) + fact_month("E1", "C_X", 2)), limit)
    p = Plan(ctx, res)
    expect(p.status == "OPTIMAL", f"статус {p.status}: {[c.message for c in res.conflicts]}", out)
    expect(salary_contracts(p, "E1", 3) == ["C_A"],
           f"март: оклад с {salary_contracts(p, 'E1', 3)} — с неразрешённого C_X уйти обязан", out)
    notes = [c.message for c in res.conflicts if c.code == "FROZEN_SALARY_BLOCK_WARNING"]
    expect(any("вынужденно" in n and "март" in n and "май" in n for n in notes),
           f"вынужденные смены не названы: {[c.message for c in res.conflicts]}", out)
    # Перерасход кассы: у C_B деньги приходят раз в квартал, а факт за
    # январь–февраль потратил больше поступившего — и чужой оклад в феврале
    # тоже. E2 платить больше не с чего: в марте касса C_B уходит в минус
    # вынужденно, план есть, проверка результата честно называет минус.
    staff2 = staff + [emp("E2", "Инженер", 100000, allowed="C_B")]
    contracts2 = contracts + [ctr("C_B", 1500000)]
    inflow2 = {**inflow, "C_B": [150000, 0, 0, 450000, 0, 0, 450000, 0, 0, 450000, 0, 0]}
    facts2 = (fact_month("E1", "C_A", 1) + fact_month("E1", "C_B", 2)
              + fact_month("E2", "C_B", 1) + fact_month("E2", "C_B", 2))
    ctx, res = solve(build("c50_cash", staff2, contracts2, inflow2, labor=labor, frozen=2,
                           facts=facts2), limit)
    cash = Plan(ctx, res)
    expect(cash.status == "OPTIMAL", f"перерасход кассы: статус {cash.status}", out)
    march = cash.total("E2", 3, set(K), "C_B")
    expect(abs(march - 100000) < EPS, f"перерасход кассы: март E2 с C_B {march:.0f}", out)
    notes = [c.message for c in res.conflicts if c.code == "FROZEN_FOT_OVERSPENT_WARNING"]
    expect(any("вынужденно" in n and "март" in n for n in notes),
           f"вынужденный минус кассы не назван: {[c.message for c in res.conflicts]}", out)
    minus = sorted(b.month for b in res.contract_balances
                   if b.contract_id == "C_B" and b.month > 2 and b.closing_balance < -EPS)
    expect(minus == [3], f"перерасход кассы: минус C_B в месяцах {minus}, ждали только март", out)
    expect(any(v.rule_id == "FOT-001" and v.month == 3 for v in res.audit_violations),
           f"проверка результата не назвала минус марта: {[v.message for v in res.audit_violations]}", out)
    return p, out


# ── запуск ───────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=60)
    args = ap.parse_args()
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    rows, failed = [], 0
    for num, title, fn in CASES:
        if only and num not in only:
            continue
        t0 = time.perf_counter()
        try:
            plan, notes = fn(args.limit)
            inv = invariants(plan) if plan is not None and plan.status == "OPTIMAL" else []
        except Exception as e:  # noqa: BLE001
            plan, notes, inv = None, [f"ошибка стенда: {type(e).__name__}: {e}"], []
        dt = time.perf_counter() - t0
        info = [n for n in notes if n.startswith("справка")]
        bad = [n for n in notes if not n.startswith("справка")] + inv
        verdict = "ОК" if not bad else "ПРОВАЛ"
        failed += bool(bad)
        rows.append((num, verdict, title, dt, bad, info))
        print(f"[{num}] {verdict:6} {dt:5.0f} с  {title}")
        for b in bad[:12]:
            print("      ✗", b)
        if len(bad) > 12:
            print(f"      … ещё {len(bad) - 12}")
        for i in info:
            print("      ·", i)
        sys.stdout.flush()
    print(f"\nитого: {len(rows)} случаев, провалов {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
