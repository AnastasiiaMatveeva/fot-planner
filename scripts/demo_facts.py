# -*- coding: utf-8 -*-
"""Демонстрация: пересчёт с факта — январь и февраль как были, остальное заново.

Владелец продукта 30.09.2026: «мы знаем, например, что в январе и феврале вот
такие суммы были, вот так сотрудники распределены… нужно, чтобы оптимизатор,
не трогая январь и февраль, пересчитал… если была смена, а правило — не более
чем в квартал, то пусть учтёт это»; «я имею в виду тесты с результатами —
демонстрационное». Скрипт считает демо-план решателем трижды и печатает, что
вышло:

  1. план без факта;
  2. январь–февраль плана 1 загружены как факт («факт по месяц» = 2):
     январь–февраль в новом плане ровно те же, строки помечены «fact»;
  3. в факте февраля сотруднику перенесли оклад на другой договор, с
     которого ему можно платить (так было на деле): январь–февраль — ровно
     новый факт, а в марте и апреле оклад остаётся на новом договоре —
     правило «оклад меняет договор не чаще одного раза за три месяца» учло
     февральскую смену;
  4. в факте февраля оклад перенесли на договор, который строке сотрудника
     не разрешён: держать там оклад в марте нельзя, смена после факта
     вынужденна — план всё равно посчитан, а отступление от правила
     названо предупреждением «вынужденно».

Сервер не нужен: вход — книга решателя (по умолчанию outputs/demo/demo_input.xlsx),
считает тот же решатель, что и сервис. Лист «фот_по_месяцам» из входа
убирается: сервис его с 18.09.2026 не пишет (правило 21 свода — весь ФОТ
договора поступает в первый месяц выплат), а демо-вход от 14.09 его ещё
несёт. С поквартальными поступлениями у договора в марте нет денег, и
перенесённый в феврале оклад на нём не удержать никаким правилом блока —
демонстрация проверяла бы кассу, а не правило.

    .venv/Scripts/python.exe scripts/demo_facts.py [--input книга.xlsx] [--out папка]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import openpyxl

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август",
          "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
STAFF = ("оклад", "122")


def money(v):
    return "{:,.0f}".format(v).replace(",", " ")


def solve(inp, out):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(ROOT / "src"))
    p = subprocess.run([sys.executable, "-m", "fot_planner.cli", "solve", "-i", str(inp),
                        "-o", str(out), "--time-limit", "120"],
                       capture_output=True, text=True, encoding="utf-8", env=env, cwd=ROOT)
    status = next((ln.split(":", 1)[1].strip() for ln in p.stdout.splitlines()
                   if ln.startswith("Статус:")), "?")
    audit = next((ln.split(":", 1)[1].strip() for ln in p.stdout.splitlines()
                  if ln.startswith("Аудит:")), "?")
    warn = [ln.strip() for ln in p.stdout.splitlines() if "_WARNING]" in ln]
    return status, audit, warn


def kind_of(text):
    t = str(text or "").strip().lower()
    for k in ("оклад", "120", "122", "124", "152"):
        if t.startswith(k):
            return k
    return "приказ" if "приказ" in t else t


def plan(path):
    """{(сотрудник, договор, месяц, вид): (сумма, источник)} и ставки."""
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["План выплат"]
    head = [str(c.value or "").strip() for c in ws[1]]
    ix = {h: i for i, h in enumerate(head)}
    rows = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        if r[ix["месяц"]] not in MONTHS or not r[ix["сумма"]]:
            continue
        key = (str(r[ix["код строки"]]), str(r[ix["договор"]]), MONTHS.index(r[ix["месяц"]]) + 1,
               kind_of(r[ix["вид выплаты"]]))
        src = str(r[ix["источник решения"]]) if "источник решения" in ix else ""
        amount, _s = rows.get(key, (0.0, src))
        rows[key] = (round(amount + float(r[ix["сумма"]]), 2), src)
    rates = {}
    if "открытые_ставки" in wb.sheetnames:
        w2 = wb["открытые_ставки"]
        h2 = [str(c.value or "").strip() for c in w2[1]]
        j = {h: i for i, h in enumerate(h2)}
        for r in w2.iter_rows(min_row=2, values_only=True):
            if r[j["ставка"]]:
                rates[(str(r[j["код строки"]]), str(r[j["договор"]]), int(r[j["месяц"]]))] = (
                    float(r[j["ставка"]]), str(r[j["основное"]]).strip().lower() in ("да", "true", "1"))
    total = {}
    ws = wb["Итог расчета"]
    for r in ws.iter_rows(min_row=1, values_only=True):
        if r and r[0]:
            total[str(r[0])] = r[1]
    return rows, rates, total


def salary_contracts(rows, emp, month):
    return sorted({c for (e, c, m, k) in rows if e == emp and m == month and k == "оклад"})


def sheet_rows(wb, name):
    ws = wb[name]
    head = [str(c.value or "").strip().lower() for c in ws[1]]
    return [dict(zip(head, r)) for r in ws.iter_rows(min_row=2, values_only=True)
            if any(v is not None for v in r)]


def day(value):
    """Дата ячейки: datetime или «дд.мм.гггг»."""
    if hasattr(value, "year"):
        return (value.year, value.month, value.day)
    d, m, y = str(value).strip().split(".")
    return (int(y), int(m), int(d))


def codes(value):
    return {x.strip() for x in str(value or "").replace(";", ",").split(",") if x.strip()}


def salary_allowed(inp, emp, contract, year, months=(2, 3, 4)):
    """Можно ли по входу платить строке оклад с договора в эти месяцы:
    договор в «разрешенных» строки (или список пуст) и не в «запрещенных»,
    оклад договором разрешён, срок выплат оклада покрывает месяцы."""
    wb = openpyxl.load_workbook(inp, data_only=True)
    row = next(r for r in sheet_rows(wb, "сотрудники") if str(r["код строки"]) == emp)
    c = next(r for r in sheet_rows(wb, "договоры") if str(r["код"]) == contract)
    allowed, forbidden = codes(row.get("разрешенные договоры")), codes(row.get("запрещенные договоры"))
    if (allowed and contract not in allowed) or contract in forbidden:
        return False
    if str(c.get("оклад разрешен") or "").strip().lower() != "да":
        return False
    end = c.get("конечная дата выплат оклада") or c.get("дата окончания")
    return day(c["дата начала"]) <= (year, min(months), 1) and day(end) >= (year, max(months), 28)


def warnings_of(path):
    """Предупреждения расчёта — лист «Проблемы и предупреждения» результата:
    сервис читает их оттуда, а не из вывода решателя."""
    wb = openpyxl.load_workbook(path, data_only=True)
    if "Проблемы и предупреждения" not in wb.sheetnames:
        return []
    return ["[%s] %s" % (r.get("отклонение / сумма"), r.get("описание проблемы"))
            for r in sheet_rows(wb, "Проблемы и предупреждения")
            if str(r.get("отклонение / сумма") or "").endswith("_WARNING")]


def move_salary(facts, emp, old, new, rates):
    """Факт февраля: оклад и 122 строки перенесены с old на new."""
    moved = dict(facts)
    for k in list(moved):
        e, c, m, kind = k
        if e == emp and m == 2 and c == old and kind in STAFF:
            moved[(e, new, m, kind)] = moved.get((e, new, m, kind), 0) + moved.pop(k)
    moved_rates = dict(rates)
    if (emp, old, 2) in moved_rates:
        moved_rates[(emp, new, 2)] = moved_rates[(emp, old, 2)]
    return moved, moved_rates


def with_facts(src, dst, facts, rates, through):
    """Вход с листом «факт_выплат» и «факт по месяц» в «настройках» — как их
    пишет сервис (app/build_input.fill_facts)."""
    wb = openpyxl.load_workbook(src)
    if "факт_выплат" in wb.sheetnames:
        del wb["факт_выплат"]
    ws = wb.create_sheet("факт_выплат")
    ws.append(["код строки", "договор", "месяц", "вид выплаты", "сумма", "ставка", "основное место"])
    for (e, c, m, k), amount in sorted(facts.items()):
        rate, main = rates.get((e, c, m), (None, None))
        ws.append([e, c, m, k, amount, rate if k == "оклад" else None,
                   None if main is None or k != "оклад" else ("да" if main else "нет")])
    st = wb["настройки"]
    head = [str(c.value or "").strip().lower() for c in st[1]]
    col = head.index("факт по месяц") + 1 if "факт по месяц" in head else st.max_column + 1
    st.cell(1, col).value = "факт по месяц"
    st.cell(2, col).value = through
    wb.save(dst)


class Protocol:
    ok = True

    def check(self, cond, text):
        print("   %s %s" % ("✓" if cond else "✗", text))
        self.ok = self.ok and bool(cond)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--input", default=str(ROOT / "outputs" / "demo" / "demo_input.xlsx"))
    ap.add_argument("--out", default=None, help="куда сложить книги; по умолчанию — временная папка")
    args = ap.parse_args()
    work = Path(args.out or tempfile.mkdtemp(prefix="demo-facts-"))
    work.mkdir(parents=True, exist_ok=True)
    base_in = work / "1_вход.xlsx"
    shutil.copyfile(args.input, base_in)
    # Поступления по месяцам сервис с 18.09.2026 не пишет (правило 21): весь
    # ФОТ договора — в первый месяц выплат. Старый демо-вход их ещё несёт.
    wb = openpyxl.load_workbook(base_in)
    dropped = [n for n in ("фот_по_месяцам", "фиксация_фот_по_месяцам") if n in wb.sheetnames]
    for name in dropped:
        del wb[name]
    wb.save(base_in)
    year = int(sheet_rows(openpyxl.load_workbook(base_in, data_only=True), "настройки")[0]["год"])
    p = Protocol()
    if dropped:
        print("Вход: убраны листы %s — сервис их не пишет с 18.09.2026 (правило 21)\n"
              % ", ".join("«%s»" % n for n in dropped))

    print("1. План без факта")
    st, audit, _w = solve(base_in, work / "1_результат.xlsx")
    print("   решатель: %s, проверка результата: %s" % (st, audit))
    if st != "OPTIMAL":
        raise SystemExit("демо-вход не решается: %s" % st)
    r1, rates1, _t = plan(work / "1_результат.xlsx")
    facts = {k: a for k, (a, _s) in r1.items() if k[2] <= 2}
    print("   строк плана в январе–феврале: %d, выплачено за два месяца: %s ₽"
          % (len(facts), money(sum(facts.values()))))

    print("\n2. Январь–февраль плана 1 — факт, «факт по месяц» = 2")
    with_facts(base_in, work / "2_вход.xlsx", facts, rates1, 2)
    st, audit, warn = solve(work / "2_вход.xlsx", work / "2_результат.xlsx")
    print("   решатель: %s, проверка результата: %s" % (st, audit))
    p.check(st == "OPTIMAL", "план посчитан")
    r2, _r, total2 = plan(work / "2_результат.xlsx")
    got = {k: a for k, (a, _s) in r2.items() if k[2] <= 2}
    p.check(got == facts, "январь–февраль ровно равны факту: %d строк, %s ₽"
            % (len(got), money(sum(got.values()))))
    p.check(all(s == "fact" for k, (a, s) in r2.items() if k[2] <= 2),
            "строки января–февраля помечены «fact» — это история, не план")
    p.check(total2.get("Факт по месяц") == 2, "в «Итоге расчета»: Факт по месяц = 2")

    # 3. В феврале сотруднику на деле перенесли оклад (и 122 с ним) на другой
    # договор. Берём того, у кого оклад в январе–марте плана 1 на одном
    # договоре, и договор, с которого с февраля по апрель ему по входу можно
    # платить оклад, — где в феврале оклад платят и другим. До 30.09.2026
    # скрипт брал договор без проверки разрешений строки и переносил
    # оклад E002 на C_COM-26, которого нет среди его разрешённых: удержать
    # оклад там в марте нельзя по входу, и ожидание шага было неверным —
    # этот перенос теперь шаг 4.
    feb_salary = defaultdict(set)
    for (e, c, m, k) in facts:
        if m == 2 and k == "оклад":
            feb_salary[c].add(e)
    steady = []
    for (e, c, m, k) in sorted(facts):
        if m != 2 or k != "оклад":
            continue
        # Договор оклада — тот же, что в плане 1 и в марте, иначе переносить
        # не с чего.
        if all(salary_contracts(r1, e, mo) == [c] for mo in (1, 2, 3)):
            steady.append((e, c))
    pick = forced_pick = None
    for e, c in steady:
        for x in sorted(feb_salary):
            if x == c:
                continue
            if salary_allowed(base_in, e, x, year):
                pick = pick or (e, c, x)
            else:
                forced_pick = forced_pick or (e, c, x)
    if pick is None:
        raise SystemExit("в демо-плане нет сотрудника для переноса оклада")
    emp, old, new = pick
    moved, rates3 = move_salary(facts, emp, old, new, rates1)
    print("\n3. Факт февраля уточнён: оклад и 122 сотрудника %s перенесены с %s на %s"
          % (emp, old, new))
    print("   без факта в марте и апреле его оклад был бы на: %s, %s"
          % (", ".join(salary_contracts(r1, emp, 3)), ", ".join(salary_contracts(r1, emp, 4))))
    with_facts(base_in, work / "3_вход.xlsx", moved, rates3, 2)
    st, audit, _warn = solve(work / "3_вход.xlsx", work / "3_результат.xlsx")
    print("   решатель: %s, проверка результата: %s" % (st, audit))
    for w in warnings_of(work / "3_результат.xlsx")[:5]:
        print("   предупреждение: " + w)
    p.check(st == "OPTIMAL", "план посчитан")
    r3, _r, _t = plan(work / "3_результат.xlsx")
    got = {k: a for k, (a, _s) in r3.items() if k[2] <= 2}
    p.check(got == moved, "январь–февраль ровно равны уточнённому факту")
    mar, apr = salary_contracts(r3, emp, 3), salary_contracts(r3, emp, 4)
    print("   с учётом факта в марте и апреле оклад на: %s, %s" % (", ".join(mar), ", ".join(apr)))
    p.check(mar == [new] and apr == [new],
            "февральская смена договора учтена: в марте и апреле оклад не переносится обратно")

    # 4. Перенос на договор, который строке не разрешён: в марте оклад с него
    # уйти обязан, смена после факта вынужденна. План должен быть, а
    # отступление от правила — названо.
    if forced_pick is not None:
        emp4, old4, new4 = forced_pick
        moved4, rates4 = move_salary(facts, emp4, old4, new4, rates1)
        print("\n4. Факт февраля: оклад и 122 сотрудника %s перенесены с %s на %s — договор, "
              "который строке не разрешён" % (emp4, old4, new4))
        with_facts(base_in, work / "4_вход.xlsx", moved4, rates4, 2)
        st, audit, _warn = solve(work / "4_вход.xlsx", work / "4_результат.xlsx")
        print("   решатель: %s, проверка результата: %s" % (st, audit))
        warn4 = warnings_of(work / "4_результат.xlsx")
        for w in warn4[:5]:
            print("   предупреждение: " + w)
        p.check(st == "OPTIMAL", "план посчитан, хотя удержать оклад на %s нельзя" % new4)
        r4, _r, _t = plan(work / "4_результат.xlsx")
        got = {k: a for k, (a, _s) in r4.items() if k[2] <= 2}
        p.check(got == moved4, "январь–февраль ровно равны факту")
        mar = salary_contracts(r4, emp4, 3)
        print("   в марте оклад на: %s" % ", ".join(mar))
        p.check(new4 not in mar, "в марте оклад ушёл с неразрешённого %s" % new4)
        p.check(any("вынужденно" in w and emp4 in w for w in warn4),
                "вынужденная смена договора оклада названа предупреждением")
    print("\nКниги: %s" % work)
    print("ИТОГ: %s" % ("всё сошлось" if p.ok else "есть расхождения"))
    return 0 if p.ok else 1


if __name__ == "__main__":
    sys.exit(main())
