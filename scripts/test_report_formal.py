"""Отчёт «План ФОТ»: формальные названия и соизмеримые величины.

Владелец продукта 30.09.2026: «в результатах расчёта есть таблицы, например —
нехватка, очень плохо названа неформально. Нужно жёстко формализовать»;
«в 11 таблице вид неудобен вроде»; «в 6.2 какая-то фигня, несоизмеримые
величины сравниваются вроде, что-то с лимитом на ставку»; «просмотри все
таблицы в План ФОТ, нужно чтобы там были величины соизмеримые, то есть если
мы сравниваем, чтобы было понятно что сравниваем»; о БЭП: «не критично если
меньше получается, но лучше ровно… делай ровно среднюю».

Проверяется сквозь настоящие функции отчёта (report.py, rules.py,
export_report.py) на книгах входа и результата, собранных здесь же, и
разметка раздела 11 — в node на настоящем app.js.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "src"))

import build_input  # noqa: E402
import export_report  # noqa: E402
import reference  # noqa: E402
import report  # noqa: E402
import rules  # noqa: E402
from openpyxl import Workbook, load_workbook  # noqa: E402

JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
EXPORT = (ROOT / "app" / "export_report.py").read_text(encoding="utf-8")
YEAR = 2026
MONTHS = rules.MONTHS

CONTRACT_HEAD = ["код", "название", "номер", "тип договора", "счет", "ГОЗ", "дата начала",
                 "дата окончания", "фот", "оклад разрешен", "120 разрешена", "122 разрешена",
                 "124 разрешена", "152 разрешена", "стимулирующая приказом разрешена", "Приоритет",
                 "основное место разрешено", "совместительство разрешено",
                 "конечная дата выплат оклада", "конечная дата выплат надбавок", "подразделение"]
EMPLOYEE_HEAD = ["код строки", "табельный номер", "фио", "должность", "подразделение", "ставка",
                 "тип занятости", "категория занятости", "зарплата", "дата начала", "дата окончания"]
LIMIT_HEAD = ["должность", "категория персонала", "оклад", "П2556", "П4", "БЭП"]
LABOR_HEAD = ["договор", "год", "трудоемкость", "должность",
              "средняя стоимость выполнения работ в месяц"] + MONTHS


def contract(code, fot, start="01.01.2026", end="31.12.2026", goz=False, salary_to=None,
             allowance_to=None, kinds=("оклад", "120", "122", "124", "152", "приказ")):
    yes = lambda k: "да" if k in kinds else "нет"  # noqa: E731
    return [code, "Договор " + code, "", "НИР", "20576У" + code, "да" if goz else "нет", start, end,
            fot, yes("оклад"), yes("120"), yes("122"), yes("124"), yes("152"), yes("приказ"), "нет",
            "да", "да", salary_to, allowance_to, None]


def employee(code, fio, position, rate=1.0, kind="основное", category="основной",
             start="11.02.2019", end="31.12.2026"):
    return [code, "Т" + code, fio, position, "Отдел", rate, kind, category, 100000, start, end]


LIMITS = [["Инженер", "НТП", 40000, 70000, 150000, 110000],
          ["Ведущий инженер", "НТП", 47000, 140000, 150000, None],
          ["Лаборант", "ВП", 38300, 60000, 100000, None]]


def make_input(folder, contracts, employees, labor=(), settings=None):
    """Вход решателя без листа «фот_по_месяцам» — как его собирает сервис
    с 18.09.2026 (SOURCE-009)."""
    wb = Workbook()
    wb.remove(wb.active)
    for name, head, rows in (("сотрудники", EMPLOYEE_HEAD, employees),
                             ("договоры", CONTRACT_HEAD, contracts),
                             ("лимиты_по_должностям", LIMIT_HEAD, LIMITS),
                             ("трудоемкость_по_договорам", LABOR_HEAD, labor)):
        ws = wb.create_sheet(name)
        ws.append(head)
        for row in rows:
            ws.append(list(row))
    ws = wb.create_sheet("настройки")
    head = ["год", "разрешить дефицит", "макс договоров оклада в год", "допуск трудоёмкости"]
    values = dict({"год": YEAR, "разрешить дефицит": "нет", "макс договоров оклада в год": 3,
                   "допуск трудоёмкости": 0.05}, **(settings or {}))
    ws.append(head)
    ws.append([values[h] for h in head])
    path = os.path.join(folder, "input.xlsx")
    wb.save(path)
    return path


def pay(emp, fio, position, month, contract_code, kind, amount):
    return [emp, fio, position, MONTHS[month - 1], contract_code, kind, amount]


def make_result(folder, plan, rates=()):
    wb = Workbook()
    ws = wb.active
    ws.title = "План выплат"
    ws.append(["код строки", "ФИО", "должность", "месяц", "договор", "вид выплаты", "сумма"])
    for row in plan:
        ws.append(list(row))
    ws = wb.create_sheet("открытые_ставки")
    ws.append(["код строки", "договор", "месяц", "ставка", "основное"])
    for row in rates:
        ws.append(list(row))
    path = os.path.join(folder, "result.xlsx")
    wb.save(path)
    return path


class Case:
    """Стенд: временная папка и отчёт по маленьким книгам. Примесь, а не
    TestCase: наследники объявлены прямо от unittest.TestCase — так их тесты
    видит защита изменений (scripts/change_guard.py, test_node)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, contracts, employees, plan, rates=(), labor=(), settings=None):
        src = make_input(self.dir, contracts, employees, labor, settings)
        out = make_result(self.dir, plan, rates)
        return src, out, report.report(src, out)


class MissingInflowSheetTests(Case, unittest.TestCase):
    """P0-2: сборка входа удаляет лист «фот_по_месяцам» с 18.09.2026, а
    rules._read_input читал его без проверки — KeyError в /report, /rules,
    /summary и выгрузке каждого нового расчёта."""

    def test_service_input_without_inflow_sheet_is_read(self):
        data = {"year": YEAR, "contracts": [
            NS(code="C_MAR", name="C_MAR", number="", kind="", account="", goz=None,
               date_from="01.03.2026", date_to="31.12.2026", fund=1_200_000, kinds="",
               priority=None, allow_main=None, allow_part_time=None, salary_deadline=None,
               allowance_deadline=None, department=None)],
            "inflows": [NS(contract_code="C_MAR", year=YEAR, month=9, amount=1_200_000)]}
        path = os.path.join(self.dir, "service.xlsx")
        build_input.build(reference.TEMPLATE, path, data, [])
        wb = load_workbook(path, read_only=True)
        names = wb.sheetnames
        wb.close()
        self.assertNotIn("фот_по_месяцам", names)
        inp = rules._read_input(path)
        # Как у загрузчика решателя: весь ФОТ — в первый месяц окна выплат.
        self.assertEqual(inp["inflow"]["C_MAR"], [0.0, 0.0, 1_200_000] + [0.0] * 9)
        out = make_result(self.dir, [])
        report.report(path, out)
        rules.check(path, out)
        summary = rules.summary(path, out)
        self.assertNotIn("поступления", summary["итого"])
        export_report.build(path, out, os.path.join(self.dir, "book.xlsx"))

    def test_report_rules_summary_and_export_on_full_input(self):
        src, out, rep = self.build(
            [contract("C_COM", 600_000)], [employee("E1", "Иванов И. И.", "Инженер")],
            [pay("E1", "Иванов И. И.", "Инженер", 1, "C_COM", "оклад", 40000)],
            [("E1", "C_COM", 1, 1.0, "да")])
        self.assertNotIn("касса", rep)
        self.assertEqual(set(rep["итоги"]), {"ФОТ", "выплачено", "остаток ФОТ"})
        checks = rules.check(src, out)
        self.assertTrue(all(c.get("название") for c in checks))
        book = os.path.join(self.dir, "book.xlsx")
        export_report.build(src, out, book)
        wb = load_workbook(book, read_only=True)
        names = wb.sheetnames
        wb.close()
        self.assertEqual(names[:len(export_report.SHEETS)], list(export_report.SHEETS.values()))
        self.assertTrue(all(len(n) <= 31 for n in names))


class DatesTests(Case, unittest.TestCase):
    def test_multi_year_contract_and_old_hire_date_are_read_in_the_plan_year(self):
        """Договор 01.03.2025–30.06.2027 в 2026 году действует весь год, а
        сотрудник, принятый 11.02.2019, работает с января: по одному месяцу
        даты выходило «с марта по июнь» и «с февраля»."""
        src = make_input(self.dir, [contract("C_OLD", 120_000, "01.03.2025", "30.06.2027")],
                         [employee("E1", "Иванов И. И.", "Инженер")])
        inp = rules._read_input(src)
        c = inp["contracts"]["C_OLD"]
        self.assertEqual((c["from"], c["to"], c["window"]), (1, 12, (1, 12)))
        self.assertEqual((inp["employees"][0]["from"], inp["employees"][0]["to"]), (1, 12))

    def test_labor_row_of_another_year_is_not_reported(self):
        src, out, rep = self.build(
            [contract("C_COM", 600_000)], [employee("E1", "Иванов И. И.", "Инженер")], [],
            labor=[["C_COM", 2027, 8, "Инженер", 100000] + [None] * 12])
        self.assertEqual(rep["трудоемкость"], [])


class ContractTests(Case, unittest.TestCase):
    def test_each_kind_is_checked_against_its_own_deadline(self):
        """Надбавка после срока выплаты оклада, но в срок надбавок, —
        не нарушение; оклад после своего срока — нарушение."""
        c = contract("C_GOZ", 1_000_000, "01.03.2026", "31.12.2026", goz=True,
                     salary_to="31.10.2026", allowance_to="31.12.2026")
        e = employee("E1", "Иванов И. И.", "Инженер")
        plan = [pay("E1", "Иванов И. И.", "Инженер", m, "C_GOZ", "оклад", 40000) for m in range(3, 11)]
        plan.append(pay("E1", "Иванов И. И.", "Инженер", 11, "C_GOZ", "120", 2000))
        rep = self.build([c], [e], plan)[2]
        row = rep["договоры"][0]
        self.assertNotIn("Выплаты после срока выплаты надбавок", row["статусы"])
        self.assertNotIn("Выплаты после срока выплаты оклада", row["статусы"])
        self.assertEqual((row["срок выплаты оклада"], row["срок выплаты надбавок"]),
                         ("31.10.2026", "31.12.2026"))
        self.assertEqual(row["месяцев до срока оклада"], 8)
        plan.append(pay("E1", "Иванов И. И.", "Инженер", 12, "C_GOZ", "оклад", 40000))
        rep = self.build([c], [e], plan)[2]
        self.assertIn("Выплаты после срока выплаты оклада", rep["договоры"][0]["статусы"])

    def test_states_are_formal_words(self):
        rep = self.build([contract("C_COM", 600_000)], [employee("E1", "Иванов И. И.", "Инженер")], [])[2]
        self.assertEqual(rep["договоры"][0]["статусы"], ["Выплат нет"])
        for state in rep["договоры"][0]["статусы"]:
            self.assertIn(state, report.CONTRACT_STATES)


class SpendingTests(Case, unittest.TestCase):
    """Разделы 3 и 4: равномерный график по окну выплат, позиция на конец месяца."""

    def setUp(self):
        super().setUp()
        goz = contract("C_GOZ", 800_000, "01.03.2026", "31.12.2026", goz=True,
                       salary_to="31.10.2026", allowance_to="31.10.2026")
        com = contract("C_COM", 600_000)
        e = employee("E1", "Иванов И. И.", "Инженер")
        plan = [pay("E1", "Иванов И. И.", "Инженер", 3, "C_GOZ", "оклад", 150_000),
                pay("E1", "Иванов И. И.", "Инженер", 4, "C_GOZ", "оклад", 100_000),
                pay("E1", "Иванов И. И.", "Инженер", 1, "C_COM", "оклад", 50_000)]
        self.rep = self.build([goz, com], [e], plan)[2]

    def row(self, part, code):
        return [r for r in self.rep[part]["договоры"] if r["код"] == code][0]

    def test_uniform_schedule_uses_the_payment_window(self):
        """800 000 на март–октябрь (срок выплат 31.10) — по 100 000, а не
        800 000 / 10 месяцев действия договора."""
        u = self.row("освоение", "C_GOZ")
        self.assertEqual(u["график"], [None, None] + [100_000.0] * 8 + [None, None])
        self.assertEqual(u["отклонение"][2:4], [50_000.0, None])
        self.assertEqual(u["отклонение"][4], -100_000.0)

    def test_cumulative_balance_is_end_of_month(self):
        n = self.row("нарастающим", "C_GOZ")
        self.assertEqual(n["выплаты"][:5], [None, None, 150_000.0, 250_000.0, 250_000.0])
        self.assertEqual(n["остаток"][:4], [None, None, 650_000.0, 550_000.0])
        self.assertEqual(n["освоение"][3], 0.3125)

    def test_organisation_total_counts_only_started_contracts(self):
        t = self.rep["нарастающим"]["итого"]
        # Январь: только C_COM (600 000 − 50 000); ФОТ C_GOZ с марта.
        self.assertEqual(t["остаток"][0], 550_000.0)
        self.assertEqual(t["остаток"][2], 550_000.0 + 650_000.0)

    def test_no_row_repeats_between_sections_3_and_4(self):
        self.assertEqual(set(self.row("освоение", "C_GOZ")) & {"остаток", "остатки", "поступление"}, set())
        self.assertNotIn("выплаты нарастающим", json.dumps(self.rep["освоение"], ensure_ascii=False))


class RegisterTests(Case, unittest.TestCase):
    """6.2: предел П2556 сравнивается только с окладом и 122 (правило 4)."""

    def test_122_apart_and_reserve_is_salary_and_122(self):
        plan = [pay("E1", "Иванов И. И.", "Инженер", m, "C_COM", k, a)
                for m in (1, 2) for k, a in (("оклад", 40000), ("122", 30000), ("124", 20000))]
        plan += [pay("E1", "Иванов И. И.", "Инженер", 3, "C_COM", k, a)
                 for k, a in (("оклад", 40000), ("122", 30000), ("приказ", 5000))]
        rates = [("E1", "C_COM", m, 1.0, "да") for m in (1, 2, 3)]
        rep = self.build([contract("C_COM", 600_000)], [employee("E1", "Иванов И. И.", "Инженер")],
                         plan, rates)[2]
        p1, p2 = rep["регистр"][0]["периоды"]
        self.assertEqual((p1["с"], p1["по"], p1["оклад"], p1["122"], p1["прочие"], p1["виды"]),
                         (1, 2, 40000.0, 30000.0, 20000.0, "оклад, 122, 124"))
        # Предел 70 000 на ставку 1,0 и оклад с 122 ровно 70 000: запас 0, а
        # не «−20 000», как когда из предела вычитались все надбавки.
        self.assertEqual((p1["лимит"], p1["запас"], p1["итого"]), (70000.0, 0.0, 180000.0))
        self.assertEqual((p2["виды"], p2["прочие"], p2["запас"]), ("оклад, 122, приказ", 5000.0, 0.0))

    def test_allowance_only_contract_has_no_rate_in_the_hr_form(self):
        plan = [pay("E1", "Иванов И. И.", "Инженер", 1, "C_COM", "оклад", 40000),
                pay("E1", "Иванов И. И.", "Инженер", 1, "C_SEC", "120", 2000)]
        rates = [("E1", "C_COM", 1, 1.0, "да")]
        rep = self.build([contract("C_COM", 600_000), contract("C_SEC", 50_000)],
                         [employee("E1", "Иванов И. И.", "Инженер")], plan, rates)[2]
        sec = [r for r in rep["шр"] if r["код_шифра"] == "C_SEC"][0]
        self.assertIsNone(sec["ставка"])
        self.assertEqual(sec["должность"], "Инженер")
        reg = [r for r in rep["регистр"] if r["договор"] == "C_SEC"][0]
        self.assertEqual((reg["занятость"], reg["лицевой счет оклада"]), ("Без открытой ставки", None))


class RateLimitTests(Case, unittest.TestCase):
    def test_total_rate_limit_is_the_solvers(self):
        """Предел суммарной ставки — как open_rate_rules.row_max_total_quarters:
        студент и лаборант — штатная ставка, совместитель — 0,5, иначе 1,5."""
        people = [employee("E1", "Иванов И. И.", "Инженер"),
                  employee("E2", "Петров П. П.", "Лаборант", rate=1.0),
                  employee("E3", "Сидорова С. С.", "Инженер", rate=0.5, category="студент"),
                  employee("E4", "Орлов О. О.", "Инженер", rate=0.5, kind="совместительство")]
        plan = [pay(e[0], e[2], e[3], 1, "C_COM", "оклад", 1000) for e in people]
        rep = self.build([contract("C_COM", 600_000)], people, plan)[2]
        limits = {r["фио"]: r["предел"] for r in rep["ставки"]}
        self.assertEqual(limits, {"Иванов И. И.": 1.5, "Петров П. П.": 1.0,
                                  "Сидорова С. С.": 0.5, "Орлов О. О.": 0.5})


class BepTests(Case, unittest.TestCase):
    """Раздел 8: равно БЭП — цель, ниже — замечание, выше — нарушение."""

    def setUp(self):
        super().setUp()
        goz = contract("C_GOZ", 5_000_000, goz=True)
        people = [employee("E1", "Иванов И. И.", "Инженер"),
                  employee("E3", "Орлов О. О.", "Ведущий инженер")]
        plan, rates = [], []
        # Инженер: БЭП 110 000; ведущий инженер без БЭП в справочнике —
        # значение решателя: наименьший БЭП справочника, 110 000.
        for m, (a, b) in {1: (70000, 107000), 2: (70000, 160000), 3: (110000, 110000)}.items():
            plan += [pay("E1", "Иванов И. И.", "Инженер", m, "C_GOZ", "оклад", 40000),
                     pay("E1", "Иванов И. И.", "Инженер", m, "C_GOZ", "122", a - 40000),
                     pay("E3", "Орлов О. О.", "Ведущий инженер", m, "C_GOZ", "оклад", 47000),
                     pay("E3", "Орлов О. О.", "Ведущий инженер", m, "C_GOZ", "122", b - 47000)]
            rates += [("E1", "C_GOZ", m, 1.0, "да"), ("E3", "C_GOZ", m, 1.0, "да")]
        self.src, self.out, self.rep = self.build([goz], people, plan, rates)

    def test_three_states(self):
        rows = {b["месяц"]: b for b in self.rep["бэп"]}
        self.assertEqual({m: b["состояние"] for m, b in rows.items()},
                         {1: "Ниже БЭП", 2: "Выше БЭП", 3: "Равно БЭП"})
        jan = rows[1]
        # Должность без БЭП не обнуляет предел: 110 000 × 2 ставки.
        self.assertEqual((jan["ставок"], jan["БЭП"], jan["средняя"]), (2.0, 110000.0, 88500.0))
        self.assertEqual((jan["отклонение"], jan["недобор"]), (-21500.0, 43000.0))
        self.assertEqual(rows[3]["отклонение"], 0.0)

    def test_check_warns_below_and_fails_above(self):
        bep = [c for c in rules.check(self.src, self.out) if c["правило"].startswith("БЭП")][0]
        self.assertEqual(bep["состояние"], "нарушено")
        self.assertEqual(bep["факт"], "Выше БЭП: 1, ниже БЭП: 1, равно БЭП: 1")

    def test_check_state_is_attention_when_only_below(self):
        rows = [r for r in self.rep["бэп"] if r["месяц"] != 2]
        self.assertTrue(rows)
        goz = contract("C_GOZ", 5_000_000, goz=True)
        people = [employee("E1", "Иванов И. И.", "Инженер")]
        plan = [pay("E1", "Иванов И. И.", "Инженер", 1, "C_GOZ", "оклад", 40000)]
        src, out, _ = self.build([goz], people, plan, [("E1", "C_GOZ", 1, 1.0, "да")])
        bep = [c for c in rules.check(src, out) if c["правило"].startswith("БЭП")][0]
        self.assertEqual(bep["состояние"], "внимание")
        self.assertTrue(bep["строки"][0]["внимание"])


class OrderLimitTests(Case, unittest.TestCase):
    """Раздел 9: П4 — норма; выплата приказом — не выше П2556 на штатную ставку."""

    def test_p4_below_level_is_a_violation_and_only_for_ntp_nr(self):
        people = [employee("E1", "Иванов И. И.", "Инженер"),
                  employee("E2", "Петров П. П.", "Лаборант")]
        plan = [pay("E1", "Иванов И. И.", "Инженер", 1, "C_COM", k, a)
                for k, a in (("оклад", 40000), ("122", 30000), ("124", 20000))]
        plan += [pay("E2", "Петров П. П.", "Лаборант", 1, "C_COM", k, a)
                 for k, a in (("оклад", 38300), ("124", 10000))]
        rates = [("E1", "C_COM", 1, 1.0, "да"), ("E2", "C_COM", 1, 1.0, "да")]
        src, out, rep = self.build([contract("C_COM", 600_000)], people, plan, rates)
        self.assertEqual(len(rep["п4"]), 1)      # лаборант (ВП) под П4 не подпадает
        row = rep["п4"][0]
        self.assertEqual((row["итого"], row["уровень"], row["отклонение"], row["состояние"]),
                         (90000.0, 150000.0, -60000.0, "Ниже уровня П4"))
        p4 = [c for c in rules.check(src, out) if c["правило"].startswith("П4")][0]
        self.assertEqual((p4["состояние"], p4["режим"], p4["подпись предела"]),
                         ("нарушено", "равно", "норма"))

    def test_order_incentive_is_limited_on_the_staff_rate(self):
        people = [employee("E2", "Петров П. П.", "Лаборант", rate=0.5)]
        plan = [pay("E2", "Петров П. П.", "Лаборант", 2, "C_COM", "приказ", 20000),
                pay("E2", "Петров П. П.", "Лаборант", 2, "C_X", "приказ", 15000),
                pay("E2", "Петров П. П.", "Лаборант", 3, "C_COM", "приказ", 10000)]
        rep = self.build([contract("C_COM", 600_000), contract("C_X", 100_000)], people, plan)[2]
        rows = {r["месяц"]: r for r in rep["п2556 приказ"]}
        self.assertEqual((rows[2]["приказ"], rows[2]["предел"], rows[2]["запас"], rows[2]["состояние"],
                          rows[2]["договоры"]), (35000.0, 30000.0, -5000.0, "Выше предела", "C_COM, C_X"))
        self.assertEqual(rows[3]["состояние"], "Не выше предела")


class LaborTests(Case, unittest.TestCase):
    """Разделы 10–12: допуск один — тот, что назван в отметке раздела."""

    def labor(self, closed, months=(1, 2, 3, 4)):
        people = [employee("E1", "Иванов И. И.", "Инженер"), employee("E5", "Абрамов А. А.", "Инженер")]
        plan, rates = [], []
        for m in months:
            for code, fio, r in (("E1", "Иванов И. И.", closed / 2), ("E5", "Абрамов А. А.", closed / 2)):
                plan.append(pay(code, fio, "Инженер", m, "C_COM", "оклад", 40000 * r))
                rates.append((code, "C_COM", m, r, "да"))
        labor = [["C_COM", YEAR, 4.0, "Инженер", 40000] + [1, 1, 1, 1] + [None] * 8]
        return self.build([contract("C_COM", 600_000)], people, plan, rates, labor)[2]

    def test_ten_percent_short_is_out_of_a_five_percent_tolerance(self):
        """3,6 из 4 чел.-мес. — 10 % при допуске 5 %: прежде отчёт прощал 0,5
        чел.-мес. сверх допуска и показывал «сходится»."""
        rep = self.labor(0.9)
        row = rep["трудоемкость"][0]
        self.assertEqual(row["статус"], "Трудоёмкость закрыта не полностью")
        self.assertEqual((row["факт чел-мес"], row["д чел-мес"], row["д чел-мес %"]), (3.6, -0.4, -0.1))
        self.assertEqual(rep["допуск"], 0.05)
        gap = rep["незакрыто"][0]
        # Месяцев выполнения работ — месяцы этапа РКМ, а не двенадцать.
        self.assertEqual((gap["по РКМ"], gap["закрыто"], gap["не закрыто"], gap["месяцев"],
                          gap["ставок в месяц"]), (4.0, 3.6, 0.4, 4, 0.1))

    def test_over_is_its_own_state_and_not_a_gap(self):
        rep = self.labor(1.25)
        self.assertEqual(rep["трудоемкость"][0]["статус"], "Трудоёмкость превышена")
        self.assertEqual(rep["незакрыто"], [])

    def test_within_tolerance(self):
        rep = self.labor(0.975)
        self.assertEqual(rep["трудоемкость"][0]["статус"], "В пределах допуска")

    def test_who_rows_are_plan_closed_deviation_then_people_by_name(self):
        rep = self.labor(0.9, months=(1, 2, 3, 4, 5))
        w = rep["кто"][0]
        self.assertEqual(w["план по месяцам"][:6], [1.0, 1.0, 1.0, 1.0, None, None])
        self.assertEqual(w["закрыто"][:6], [0.9, 0.9, 0.9, 0.9, 0.9, None])
        self.assertEqual(w["отклонение"][:5], [-0.1, -0.1, -0.1, -0.1, 0.9])
        self.assertEqual(w["вне этапа"][:6], [False] * 4 + [True, False])
        self.assertTrue(w["вне допуска"])
        self.assertEqual([p["фио"] for p in w["люди"]], ["Абрамов А. А.", "Иванов И. И."])
        self.assertEqual(rep["трудоемкость"][0]["статус"], "Трудоёмкость вне месяцев этапа")

    def test_tolerance_default_is_the_solvers(self):
        self.assertEqual(rules.labor_tolerance({}), 0.05)
        self.assertEqual(rules.labor_tolerance({"допуск трудоёмкости": 0.2}), 0.05)


class FormalNamesTests(unittest.TestCase):
    """Названия и графы: одни на экране и в выгрузке, без обиходных слов."""

    def report_js(self):
        start = JS.index("var REP_SHORT")
        return JS[start:JS.index("/* Меню автофильтра графы", start)]

    def test_headers_are_the_same_on_screen_and_in_export(self):
        headers = [
            "Месяцев до срока выплаты оклада", "Срок выплаты оклада", "Срок выплаты надбавок",
            "Нераспределённый ФОТ на 31.12, ₽", "Освоение ФОТ за год, %",
            "Выплаты по плану", "Равномерный график освоения", "Отклонение от равномерного графика",
            "Выплаты нарастающим итогом, ₽", "Остаток ФОТ на конец месяца, ₽",
            "Освоение ФОТ нарастающим итогом, %", "Наименование вида выплаты",
            "Доля в выплатах по плану, %", "Открытая ставка", "Оклад в месяц, ₽",
            "Надбавка 122 в месяц, ₽", "Прочие надбавки и выплата приказом в месяц, ₽",
            "Виды выплат в периоде", "Выплаты за период, ₽",
            "Предел П2556 на открытую ставку в месяц, ₽",
            "Запас до предела П2556 по окладу и 122 в месяц, ₽",
            "Предел суммарной открытой ставки сотрудника", "Наибольшая ставка за год",
            "Оклад и надбавка 122 по договору в месяц, ₽", "Средняя на 1,0 ставки в месяц, ₽",
            "БЭП на 1,0 ставки в месяц, ₽", "Отклонение средней от БЭП, ₽",
            "Недобор до БЭП по договору в месяц, ₽", "П2556 на 1,0 ставки, ₽",
            "Уровень П4 на суммарную ставку в месяц, ₽", "Отклонение от уровня П4, ₽",
            "Выплата приказом в месяц, ₽", "Предел П2556 на штатную ставку в месяц, ₽",
            "Трудоёмкость по РКМ за год, чел.-мес.", "Закрыто исполнителями за год, чел.-мес.",
            "Средняя стоимость 1 чел.-мес. по плану, ₽",
            "Число исполнителей в месяц, наибольшее, чел.", "Должность исполнителя",
            "За год, чел.-мес.", "Не закрыто, чел.-мес.", "Месяцев выполнения работ",
            "Требуется дополнительно ставок в месяц", "Заработная плата за месяц, ₽",
            "Недоплата, ₽", "Целевой показатель или параметр", "Вес в целевой функции"]
        js = self.report_js()
        for head in headers:
            self.assertIn('"%s"' % head, js, head)
            self.assertIn('"%s"' % head, EXPORT, head)

    def test_no_colloquial_words_in_report_titles(self):
        """Нехватка, касса, дефицит, запас без предмета, «нужно», «закрыли
        люди», «в пределах» без допуска — прежние названия разделов и граф."""
        js = self.report_js()
        for word in ('"Нехватка', "Касса договоров", '"Дефицит', '{ t: "Запас, ₽" }', '"Нужно,',
                     '"Закрыли люди"', '"в пределах"', "Поступило за год", "Поступления на счета",
                     "Остаток средств на счетах", '"Макс в плане"', '"Лимит на ставку, ₽"',
                     '"Фонд надбавок в месяц, ₽"', '"Закрыто по строке"'):
            self.assertNotIn(word, js, word)
        for word in ('"12 Нехватка"', '"14 Дефицит"', '"4 Касса"', '"Поступило, ₽"', '"Нужно, чел.-мес."'):
            self.assertNotIn(word, EXPORT, word)

    def test_section_numbers_are_kept(self):
        js = self.report_js()
        # Раздел 16 «По подразделениям» — ИНТЕРФЕЙС.md, управленческие подразделения.
        for n in range(1, 17):
            self.assertRegex(js, r"secH\(%d, " % n)
        for sub in ("2.1", "2.2", "6.1", "6.2", "6.3", "7.1", "7.2", "9.1", "9.2", "9.3", "13.1", "13.2"):
            self.assertIn('subH("%s", ' % sub, js)
        self.assertEqual(len(export_report.SHEETS), 24)
        self.assertTrue(all(len(v) <= 31 for v in export_report.SHEETS.values()))

    def test_short_names_are_one_in_navigation_and_export(self):
        """Краткое название раздела одно в оглавлении, в кнопке перехода
        протокола контроля и в имени листа выгрузки."""
        block = JS[JS.index("var REP_SHORT = {"):JS.index("};", JS.index("var REP_SHORT = {"))]
        short = {int(n): t for n, t in re.findall(r'(\d+): "([^"]+)"', block)}
        self.assertEqual(sorted(short), list(range(1, 17)))
        for key, sheet in export_report.SHEETS.items():
            if "." not in key:
                self.assertEqual(sheet, "%s %s" % (key, short[int(key)]))
        start = JS.index("function reviewRuleTarget(")
        target = JS[start:JS.index("return n ?", start) + 80]
        self.assertIn("PlanIssues.ruleSection", target)
        self.assertIn("REP_SHORT[n]", target)


class WhoTableMarkupTests(unittest.TestCase):
    """Раздел 11 на экране: дерево договор → строка РКМ → план, закрытое,
    отклонение, исполнители; над ключевыми графами — «Чел.-мес.»."""

    def test_rows(self):
        start = JS.index("function repLimits()")
        body = JS[start:JS.index("/* Меню автофильтра графы", start)]
        vrep = {"год": YEAR, "договоры": [{"код": "C_A", "название": "НИР «А»"}], "бэп": [], "п4": [],
                "п2556": [], "п2556 приказ": [], "незакрыто": [],
                "трудоемкость": [{"договор": "C_A", "строка": "Инженер", "вне допуска": {}}],
                "кто": [{"договор": "C_A", "строка": "Инженер", "план": 2.0,
                         "план по месяцам": [1.0, 1.0] + [None] * 10,
                         "закрыто": [1.0, 0.5, 0.5] + [None] * 9, "закрыто год": 2.0,
                         "отклонение": [None, -0.5, 0.5] + [None] * 9, "отклонение год": None,
                         "вне допуска": True, "вне этапа": [False, False, True] + [False] * 9,
                         "люди": [{"фио": "Абрамов А. А.", "должность": "Инженер",
                                   "ставка": [1.0, 0.5, 0.5] + [None] * 9, "ставка год": 2.0}]}]}
        script = (
            "var grids=[];var MON=['янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек'];"
            "var repMon=[1,2,3,4,5,6,7,8,9,10,11,12];"
            "function esc(s){return String(s);} function secH(){return '';} function subH(){return '';}"
            "function chip(){return '';} function repGoalsHtml(){return '';} function rp(v){return String(v);}"
            "function rpn(v){return String(v);} function rf(v){return v==null?'':String(v);}"
            "function rm(v){return String(v);} function rs(v){return (v>0?'+':'')+v;}"
            "function nc(v,c){return {v:v,cls:'n '+(c||'')};} function gc(){return {v:'',cls:'gray'};}"
            "function mc(v){return {v:v?String(v):'',cls:'n'};} function monHead(){return repMon.map(function(m){return {t:MON[m-1]};});}"
            "function monGroupRow(a,b,c,d,e){return [{t:'',span:a,lead:e||''}];} function repFactTo(){return +vrep['факт по']||0;}"
            "function repT(h,r,x,o){grids.push({h:h,r:r,o:o});return '';}"
            "var vrep=" + json.dumps(vrep, ensure_ascii=False) + ";" + body +
            "repLimits();var g=grids[grids.length-1];"
            "console.log(JSON.stringify({head:g.h,lead:g.o.groups[0].lead,rows:g.r.map(function(r){"
            "return {grp:!!r.grp,cls:r.cls||'',key:r.reviewKey||'',cells:r.cells.map(function(c){"
            "return c&&typeof c==='object'?[c.v,c.cls||'']:[c,''];})};})}));")
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                             encoding="utf-8", check=True)
        got = json.loads(out.stdout)
        self.assertEqual(got["head"][:2], ["Исполнитель", "Должность исполнителя"])
        self.assertEqual(got["head"][-1], {"t": "За год, чел.-мес."})
        self.assertEqual(got["lead"], "Чел.-мес.")
        rows = got["rows"]
        self.assertTrue(rows[0]["grp"] and "НИР «А»" in rows[0]["cells"][0][0])
        self.assertEqual((rows[1]["grp"], rows[1]["key"], rows[1]["cells"][0][0]), (True, "who:0", "Инженер"))
        # Строки РКМ и договора — без чисел: плана в строке РКМ больше нет.
        self.assertEqual(rows[1]["cells"][-1][0], "")
        labels = [r["cells"][0][0] for r in rows[2:5]]
        self.assertEqual(labels, ["План по РКМ", "Закрыто исполнителями", "Отклонение от плана"])
        dev = rows[4]["cells"]
        self.assertIn("err", dev[4][1])                          # март: закрыто вне этапа
        self.assertIn("Закрыто вне этапа РКМ", dev[4][0])
        self.assertIn("err", dev[-1][1])                         # за год: строка вне допуска
        self.assertEqual(rows[5]["cells"][0][0], "Абрамов А. А.")
        self.assertEqual(rows[5]["cells"][1], ["Инженер", "pos"])
        self.assertNotIn("Закрыто по строке", json.dumps(rows, ensure_ascii=False))


def make_fact_result(folder, plan, rates=(), frozen=2):
    """Результат пересчёта с факта (правило 26): «Факт по месяц» в листе
    «Итог расчета», строки месяцев 1…F — «источник решения» fact."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Итог расчета"
    ws.append(["показатель", "значение", "статус", "комментарий"])
    ws.append(["Статус решателя", "OPTIMAL", "", ""])
    ws.append(["Год", YEAR, "", ""])
    ws.append(["Факт по месяц", frozen, "", ""])
    ws = wb.create_sheet("План выплат")
    ws.append(["код строки", "ФИО", "должность", "месяц", "договор", "вид выплаты", "сумма",
               "источник решения", "зафиксировано", "статус"])
    for row in plan:
        fact = MONTHS.index(row[3]) + 1 <= frozen
        ws.append(list(row) + ["fact" if fact else "optimizer", "да" if fact else "нет",
                               "факт" if fact else "ОК"])
    ws = wb.create_sheet("открытые_ставки")
    ws.append(["код строки", "договор", "месяц", "ставка", "основное"])
    for row in rates:
        ws.append(list(row))
    path = os.path.join(folder, "fact_result.xlsx")
    wb.save(path)
    return path


class FactMonthsTests(Case, unittest.TestCase):
    """Месяцы 1…F — факт начислений (правило 26; владелец продукта
    30.09.2026: «пересчёт с текущими вводными… не трогая январь и
    февраль»): показаны и входят в итоги за год, но не судятся — ни
    состоянием, ни отметкой раздела, ни замечанием."""

    def setUp(self):
        super().setUp()
        goz = contract("C_GOZ", 5_000_000, goz=True)
        people = [employee("E3", "Орлов О. О.", "Ведущий инженер")]
        who = ("E3", "Орлов О. О.", "Ведущий инженер")
        # Январь по факту — оклад и 122 выше П2556 (140 000) и выше БЭП;
        # февраль — ставка 0,3, не кратная четверти. План с марта — ровно БЭП.
        plan = [pay(*who, 1, "C_GOZ", "оклад", 47000), pay(*who, 1, "C_GOZ", "122", 113000),
                pay(*who, 2, "C_GOZ", "оклад", 14100),
                # Факт уволенного: в листе сотрудников его нет, ФИО пусто.
                pay("E9", None, None, 1, "C_GOZ", "120", 5000)]
        rates = [("E3", "C_GOZ", 1, 1.0, "да"), ("E3", "C_GOZ", 2, 0.3, "да")]
        for m in range(3, 13):
            plan += [pay(*who, m, "C_GOZ", "оклад", 47000), pay(*who, m, "C_GOZ", "122", 63000)]
            rates.append(("E3", "C_GOZ", m, 1.0, "да"))
        labor = [["C_GOZ", YEAR, 12.0, "Ведущий инженер", 110000] + [1] * 12]
        self.src = make_input(self.dir, [goz], people, labor)
        self.out = make_fact_result(self.dir, plan, rates)
        self.rep = report.report(self.src, self.out)
        self.checks = rules.check(self.src, self.out)

    def test_result_is_read_with_fact_months(self):
        res = rules._read_result(self.out)
        self.assertEqual(res["frozen"], 2)
        self.assertTrue(all(p["fact"] == (p["month"] <= 2) for p in res["plan"]))
        self.assertEqual(self.rep["факт по"], 2)
        # Старый результат без строки «Факт по месяц» — факта нет.
        self.assertEqual(rules._read_result(make_result(self.dir, []))["frozen"], 0)

    def test_fact_rows_have_a_neutral_state_and_no_issue(self):
        bep = {b["месяц"]: b["состояние"] for b in self.rep["бэп"]}
        self.assertEqual((bep[1], bep[2]), ("Факт", "Факт"))
        self.assertEqual({bep[m] for m in range(3, 13)}, {"Равно БЭП"})
        p25 = {r["месяц"]: r for r in self.rep["п2556"]}
        self.assertEqual((p25[1]["состояние"], p25[1]["запас"]), ("Факт", -20000.0))
        self.assertEqual(p25[3]["состояние"], "Не выше предела")
        issues = collect_issues(self.rep, self.checks)
        self.assertEqual([i for i in issues if (i["target"].get("month") or 99) <= 2], [], issues)
        self.assertFalse([i for i in issues if i["rule"].startswith(("bep.", "p4."))], issues)

    def test_single_month_checks_skip_fact_months_and_say_so(self):
        by = {c["правило"]: c for c in self.checks}
        for name in ("П2556: оклад и 122 не выше предела по должности",
                     "Оклад идет по ставке, а не произвольной суммой",
                     "БЭП: средняя зарплата по ГОЗ-договору в пределах базовой"):
            self.assertEqual(by[name]["состояние"], "соблюдено", name)
            self.assertIn("Месяцы по февраль включительно — факт начислений, не проверяются.",
                          by[name]["смысл"], name)
        self.assertIn("Факт начислений по февраль включительно входит в сумму числом.",
                      by["Выплаты не превышают ФОТ договора"]["смысл"])

    def test_yearly_totals_include_facts(self):
        total = 47000 + 113000 + 14100 + 5000 + 10 * 110000
        self.assertEqual(self.rep["итоги"]["выплачено"], float(total))
        self.assertIn("E9", [p["табельный"] for p in self.rep["помесячно"]["сотрудники"]])
        cum = self.rep["нарастающим"]["договоры"][0]
        self.assertEqual(cum["выплаты"][1], 47000 + 113000 + 5000 + 14100.0)
        # Трудоёмкость года — с фактом: 1 + 0,3 + 10 чел.-мес.; недобор
        # февраля — история, отклонение не нарушение.
        row = self.rep["трудоемкость"][0]
        self.assertEqual(row["факт чел-мес"], 11.3)
        self.assertEqual(row["статус"], "Отклонение сложилось в месяцах факта")
        self.assertFalse(any(row["вне допуска"].values()))
        self.assertEqual(self.rep["незакрыто"], [])
        self.assertEqual(self.rep["кто"][0]["отклонение"][:3], [None, None, None])

    def test_uniform_schedule_relevels_on_the_remaining_months(self):
        """Как у решателя (_relevel_spend_targets): ФОТ за вычетом факта
        ровно на оставшиеся месяцы окна; месяц факта с графиком не
        сравнивается."""
        u = self.rep["освоение"]["договоры"][0]
        spent = 47000 + 113000 + 14100 + 5000
        self.assertEqual(u["график"][:3], [None, None, round((5_000_000 - spent) / 10, 2)])
        self.assertEqual(u["отклонение"][:2], [None, None])

    def test_export_marks_fact_months(self):
        book = os.path.join(self.dir, "fact_book.xlsx")
        export_report.build(self.src, self.out, book)
        wb = load_workbook(book, read_only=True)
        head = [c.value for c in next(wb[export_report.SHEETS["3"]].iter_rows(min_row=1, max_row=1))]
        states = [r[13] for r in wb[export_report.SHEETS["9.1"]].iter_rows(min_row=2, values_only=True)]
        wb.close()
        self.assertEqual(head[4:7], ["янв, факт", "фев, факт", "мар"])
        self.assertEqual(states[0], "Факт")

    def test_screen_marks_fact_months_and_keeps_them_out_of_chips(self):
        def js_fn(name):
            at = JS.index("function %s(" % name)
            return JS[at:JS.index("\n}\n", at) + 3]

        start = JS.index("var REP_FACT_ATTR")
        helpers = JS[start:JS.index("\n/* Верхний ярус шапки", start) + 1] + js_fn("monGroupRow")
        names = JS.index("var MONTH_NAMES = [")
        helpers += JS[names:JS.index("];", names) + 2] + "\n"
        mon_head, th_cell = js_fn("monHead"), js_fn("thCell")
        body = JS[JS.index("function repLimits()"):JS.index("/* Меню автофильтра графы")]
        script = (
            "var chips=[];var MON=['янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек'];"
            "var repMon=[1,2,3,4,5,6,7,8,9,10,11,12];"
            "var vrep=" + json.dumps(self.rep, ensure_ascii=False) + ";"
            "function esc(s){return String(s);} function secH(){return '';} function subH(){return '';}"
            "function chip(v,c){chips.push([v,c||'']);return '';} function repGoalsHtml(){return '';}"
            "function rp(v){return String(v);} function rpn(v){return String(v);}"
            "function rf(v){return v==null?'':String(v);} function rm(v){return String(v);}"
            "function rs(v){return String(v);} function nc(v,c){return {v:v,cls:'n '+(c||'')};}"
            "function gc(){return {v:'',cls:'gray'};} function mc(v){return {v:v?String(v):'',cls:'n'};}"
            "function repT(){return '';} function repOpenMonths(){return null;}"
            + helpers + mon_head + th_cell + body +
            "repLimits();"
            "console.log(JSON.stringify({chips:chips,group:monGroupRow(4,1,0,'quarter'),"
            "head:monHead().slice(0,3),th:thCell(monHead()[0],5,2,false,null)}));")
        # Программа — через stdin: с отчётом внутри она длиннее предела
        # командной строки Windows.
        out = subprocess.run(["node", "-"], input=script, capture_output=True, text=True,
                             encoding="utf-8", check=True)
        got = json.loads(out.stdout)
        texts = [c[0] for c in got["chips"]]
        self.assertNotIn("Выше БЭП: 2", texts)
        self.assertFalse([t for t in texts if t.startswith("Выше")], texts)
        self.assertIn(["Равно БЭП", "ok"], got["chips"])
        self.assertIn(["Превышений предела П2556 нет", "ok"], got["chips"])
        self.assertEqual(got["group"][1], {"t": "Факт по февраль", "span": 2, "fact": True,
                                           "tip": "Факт начислений: месяцы не пересчитываются"})
        self.assertEqual(got["group"][2]["t"], "I квартал")
        self.assertEqual(got["group"][2]["span"], 1)
        self.assertEqual([h["fact"] for h in got["head"]], [True, True, False])
        self.assertIn('data-fact="1"', got["th"])
        self.assertIn('title="Факт начислений: месяц не пересчитывается"', got["th"])


def collect_issues(rep, checks):
    """Замечания списка (issues.js) по отчёту и проверкам — в node."""
    script = ("const issues=require(%s);const d=JSON.parse(require('fs').readFileSync(0,'utf8'));"
              "console.log(JSON.stringify(issues.collect(d.rep,d.rules,{},'t')));"
              % json.dumps(str(ROOT / "app" / "static" / "issues.js")))
    out = subprocess.run(["node", "-e", script], input=json.dumps({"rep": rep, "rules": checks},
                                                                  ensure_ascii=False),
                         capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(out.stdout)


if __name__ == "__main__":
    unittest.main()
