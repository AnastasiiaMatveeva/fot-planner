"""Зарплата строки по месяцам (решение пользователя 19.09.2026).

Вкладка реестра «Заработная плата по месяцам»: у строки штатного расписания в
месяце может быть своя сумма, в остальных месяцах действует зарплата штатного
расписания. Правка пишется в журнал и помечает к пересмотру посчитанные планы
этого года; свои суммы уходят во вход решателя листом «зарплата_по_месяцам».
Из штатного расписания графа «Зарплата» убрана.

Стенд — временная база `lifecycle.App`. Запускать отдельным процессом
(DEV-STAND-ISOLATION-001): стенд откажется работать, если модуль базы уже
привязан к другой папке.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "harness"))

JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")


class Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def function(name):
    """Функция app.js целиком: от объявления до следующей на верхнем уровне."""
    start = JS.index("function " + name + "(")
    return JS[start:JS.index("\nfunction ", start + 1)]


def branch(tab):
    """Ветка вкладки реестра в renderRegistry."""
    start = JS.index('} else if (regTab === "%s") {' % tab)
    return JS[start:JS.index("} else if", start + 10)]


class SalaryMonthsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lifecycle import App
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, FOT_OPERATOR="salary-test-operator")
        cls.env.start()
        cls.app = App(cls.temp.name)
        import actuality
        cls.actuality = actuality

    @classmethod
    def tearDownClass(cls):
        cls.app.db.close()
        cls.app.d.engine.dispose()
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.db, self.d, self.main = self.app.db, self.app.d, self.app.main
        for model in (self.d.SalaryMonth, self.d.SalaryRow, self.d.Employee, self.d.Decision,
                      self.d.Run, self.d.Message, self.d.Activity, self.d.Document):
            self.db.query(model).delete()
        self.db.query(self.d.Case).filter(self.d.Case.id != self.app.case.id).delete()
        # Принят в марте: январь и февраль вне срока строки.
        self.db.add(self.d.Employee(code="E001", fio="Иванов И. И.", position="Инженер",
                                    rate=1.0, salary=100000, date_from="01.03.2026",
                                    date_to="31.12.2026"))
        self.db.commit()

    # ── помощники ──
    def put(self, **body):
        body.setdefault("code", "E001")
        body.setdefault("year", 2026)
        return asyncio.run(self.main.set_salary_month(Req(body)))

    def table(self):
        # Обработчики пишут своей сессией: без сброса сессия теста отдала бы
        # из своего кэша строки в состоянии до правки.
        self.db.expire_all()
        return self.main.case_data(self.app.case.id)["salary"]

    # ── случаи ──
    def test_month_shows_only_table_sums(self):
        # Зарплаты в штатном расписании нет (решение пользователя 19.09.2026):
        # месяц без суммы пуст, 100 000 из графы штатки не подставляется.
        self.put(month=4, amount="120 000,50")
        t = self.table()
        row = t["rows"][0]
        self.assertEqual(t["year"], 2026)
        self.assertEqual(row["months"][:5], [None, None, None, 120000.5, None])
        self.assertEqual(row["active"], list(range(3, 13)))
        # Итог месяца без единой суммы пуст, а не 0.
        self.assertEqual(t["totals"][:5], [None, None, None, 120000.5, None])

    def test_empty_amount_removes_the_sum(self):
        self.put(month=4, amount=120000)
        self.put(month=4, amount="")
        self.assertEqual(self.table()["rows"][0]["months"][3], None)
        self.assertEqual(self.db.query(self.d.SalaryMonth).count(), 0)

    def test_bad_input_is_refused(self):
        from fastapi import HTTPException
        # Ноль пока запрещён (решение пользователя 19.09.2026).
        for body in ({"month": 13, "amount": 1}, {"month": 4, "amount": -5},
                     {"month": 4, "amount": 0}, {"month": 4, "amount": "0,00"},
                     {"month": 4, "amount": "много"},
                     {"code": "НЕТ", "month": 4, "amount": 1}):
            with self.subTest(body=body), self.assertRaises(HTTPException):
                self.put(**body)
        self.assertEqual(self.db.query(self.d.SalaryMonth).count(), 0)

    def test_change_marks_plans_of_that_year_and_is_logged(self):
        run = self.app.run(status="OPTIMAL")
        other = self.d.Case(title="Другой год", year=2027, stage="сбор данных")
        self.db.add(other)
        self.db.commit()
        run27 = self.d.Run(case_id=other.id, status="OPTIMAL", settings="{}")
        self.db.add(run27)
        self.db.commit()
        self.put(month=4, amount=120000)
        self.db.refresh(run)
        self.db.refresh(run27)
        self.assertEqual(run.review, self.actuality.REVIEW)
        self.assertNotEqual(run27.review, self.actuality.REVIEW)
        kinds = [d.subject_kind for d in self.db.query(self.d.Decision).all()]
        self.assertIn("зарплата по месяцам", kinds)

    def test_own_months_go_to_solver_input(self):
        self.put(month=4, amount=120000)
        # Сумма строки, которой нет на листе сотрудников, во вход не идёт:
        # загрузчик решателя остановился бы на неизвестном коде строки.
        self.db.add(self.d.SalaryMonth(employee_code="GONE", year=2026, month=5, amount=1))
        self.db.commit()
        data = self.main._registry_data(self.db, self.app.case)
        self.assertIn(("E001", 4, 120000.0), data["salary_months"])
        wb = openpyxl.Workbook()
        staff = wb.active
        staff.title = "сотрудники"
        staff.append(["код строки", "зарплата"])
        staff.append(["E001", 100000])
        self.main.build_input.fill_salary_months(wb, data["salary_months"])
        rows = list(wb["зарплата_по_месяцам"].values)
        self.assertEqual(rows, [("код строки", "месяц", "зарплата"), ("E001", 4, 120000.0)])

    def test_no_own_months_no_sheet(self):
        wb = openpyxl.Workbook()
        wb.active.title = "сотрудники"
        self.main.build_input.fill_salary_months(wb, [])
        self.assertNotIn("зарплата_по_месяцам", wb.sheetnames)

    def test_missing_month_salary_stops_the_input(self):
        # Подставить нечего: месяц срока без суммы — вход не собирается, а
        # сообщение называет строку и месяцы. Окно перед расчётом говорит то же.
        e = self.db.query(self.d.Employee).one()
        wb = openpyxl.Workbook()
        staff = wb.active
        staff.title = "сотрудники"
        staff.append(["код строки", "зарплата"])
        staff.append(["E001", None])
        rows = [("E001", m, 100000.0) for m in range(3, 13) if m not in (6, 7, 8)]
        with self.assertRaises(ValueError) as got:
            self.main.build_input.fill_salary_months(wb, rows, [e], 2026)
        self.assertIn("Иванов И. И. (E001) — июнь–август", str(got.exception))
        why = self.main._preflight(self.db, self.app.case)
        self.assertTrue(why["стоит"])
        self.assertTrue(any("Не задана зарплата по месяцам" in t and "весь год" not in t
                            for t in why["почему"]), why["почему"])
        # Все месяцы срока заданы — вход собран, графа «зарплата» заполнена
        # первой суммой: загрузчик решателя её требует.
        full = [("E001", m, 100000.0 + m) for m in range(3, 13)]
        self.main.build_input.fill_salary_months(wb, full, [e], 2026)
        self.assertEqual(staff.cell(2, 2).value, 100003.0)
        self.assertEqual(wb["зарплата_по_месяцам"].max_row, 11)

    def test_rules_summary_matches_the_code(self):
        # Свод правил решателя обещал, что месяц без своей суммы возьмёт
        # зарплату из штатного расписания. Код так не делает и не может: с
        # 19.09.2026 зарплаты в штатном расписании нет. Расхождение нашёл
        # пользователь 28.09.2026 — здесь оно и закрыто.
        rules = (ROOT / "docs" / "ПРАВИЛА_РЕШАТЕЛЯ.md").read_text(encoding="utf-8")
        one = rules[rules.index("1. `[строка]` **Зарплата выплачивается целиком"):]
        one = one[:one.index("2. `[строка]`")]
        self.assertNotIn("месяц без своей суммы получает\n   зарплату из штатного расписания", one)
        self.assertIn("в штатном расписании зарплаты нет", one)
        self.assertIn("build_input.salary_gaps", one)
        self.assertIn("явным\n   нулём", one)

    def test_rules_report_reads_month_wages(self):
        # Отчёт «Человек получает всю месячную зарплату» сверяет с суммой
        # месяца, а не с зарплатой штатного расписания (SALARY-001).
        import rules
        path = os.path.join(self.temp.name, "вход.xlsx")
        wb = openpyxl.Workbook()
        staff = wb.active
        staff.title = "сотрудники"
        staff.append(["код строки", "фио", "зарплата"])
        staff.append(["E001", "Иванов И. И.", 100000])
        wb.create_sheet("договоры").append(["код"])
        wb.create_sheet("фот_по_месяцам").append(["код"])
        months = wb.create_sheet("зарплата_по_месяцам")
        months.append(["код строки", "месяц", "зарплата"])
        months.append(["E001", 4, 120000])
        wb.save(path)
        self.assertEqual(rules._read_input(path)["wages"], {("E001", 4): 120000.0})

    def test_chat_salary_goes_to_month_table(self):
        # «у Иванова оклад 90 000» — в таблицу по месяцам, в месяцы его работы
        # (принят в марте), а не в штатное расписание (решение 19.09.2026).
        import chat
        case = self.db.get(self.d.Case, self.app.case.id)
        out = chat.apply_edits(self.db, case, [{"entity": "сотрудник", "key": "E001",
                                                "field": "оклад", "value": "90 000"}])
        self.db.expire_all()
        got = sorted((m.month, m.amount) for m in self.db.query(self.d.SalaryMonth).all())
        self.assertEqual(got, [(m, 90000.0) for m in range(3, 13)])
        self.assertEqual(self.db.query(self.d.Employee).one().salary, 100000)
        self.assertIn("мар–дек 2026", " ".join(out))
        # Ноль пока запрещён — ничего не пишется.
        out = chat.apply_edits(self.db, case, [{"entity": "сотрудник", "key": "E001",
                                                "field": "зарплата", "value": "0"}])
        self.assertIn("больше нуля", " ".join(out))

    # ── строки таблицы ──
    def add_row(self, code="E001"):
        return asyncio.run(self.main.add_salary_row(Req({"code": code, "year": 2026})))

    def test_table_lists_only_added_rows(self):
        # Строки приносит документ или экономист: сама штатка в таблицу не
        # попадает, её сотрудники — в списке для «Добавить сотрудника».
        t = self.table()
        self.assertEqual(t["rows"], [])
        self.assertEqual([c["code"] for c in t["candidates"]], ["E001"])
        self.add_row()
        t = self.table()
        self.assertEqual(t["rows"][0]["months"], [None] * 12)
        self.assertEqual(t["candidates"], [])
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self.add_row()

    def test_adding_row_does_not_touch_plans(self):
        run = self.app.run(status="OPTIMAL")
        self.add_row()
        self.db.refresh(run)
        self.assertNotEqual(run.review, self.actuality.REVIEW)

    def test_deleting_row_removes_its_sums(self):
        run = self.app.run(status="OPTIMAL")
        self.put(month=4, amount=120000)
        rid = self.table()["rows"][0]["id"]
        self.main.delete_salary_row(rid)
        self.db.expire_all()
        self.assertEqual(self.db.query(self.d.SalaryMonth).count(), 0)
        self.assertEqual(self.table()["rows"], [])
        self.db.refresh(run)
        self.assertEqual(run.review, self.actuality.REVIEW)

    # ── документ ──
    def salary_book(self, rows, name="Зарплата 2026.xlsx"):
        path = os.path.join(self.temp.name, name)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Табельный", "ФИО", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                   "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"])
        for r in rows:
            ws.append(r)
        wb.save(path)
        doc = self.d.Document(name=name, path=path, state="загружен", scope="реестр")
        self.db.add(doc)
        self.db.commit()
        return doc

    def test_document_is_recognised_by_month_columns(self):
        import intake
        doc = self.salary_book([["E001", "Иванов И. И."] + [100000] * 12])
        self.assertIsNotNone(intake.salary_layout(doc.path))
        self.assertEqual(intake.classify(doc.path, doc.name)[:2],
                         ("зарплата по месяцам", "salary"))
        # Штатное расписание с одной графой «Зарплата» — не таблица по месяцам.
        staff = os.path.join(self.temp.name, "Штатное.xlsx")
        wb = openpyxl.Workbook()
        wb.active.append(["Табельный", "ФИО", "Должность", "Ставка", "Зарплата"])
        wb.save(staff)
        self.assertIsNone(intake.salary_layout(staff))

    def test_document_fills_rows_and_keeps_manual_sums(self):
        import intake
        run = self.app.run(status="OPTIMAL")
        self.put(month=4, amount=999)          # правка в ячейке важнее файла
        doc = self.salary_book([
            ["E001", "Иванов И. И.", 1, 2, 110000, 110000, 0] + [120000] * 7,
            ["", "Петров П. П.", 5] + [5] * 11,
        ])
        intake.run_salary_months(self.db, self.app.case, doc)
        self.db.expire_all()
        row = self.table()["rows"][0]
        # Апрель — ручная сумма; май с нулём не принят и остался пустым.
        self.assertEqual(row["months"][:6], [1, 2, 110000, 999, None, 120000])
        self.assertEqual(self.db.get(self.d.Document, doc.id).summary, "строк 1, сумм 10")
        said = " ".join(m.text for m in self.db.query(self.d.Message).all())
        self.assertIn("Петров П. П.", said)
        self.assertIn("Нулевые суммы не принял: Иванов И. И. — май", said)
        self.db.refresh(run)
        self.assertEqual(run.review, self.actuality.REVIEW)
        # Новая редакция файла заменяет суммы прежней, ручная остаётся.
        again = self.salary_book([["E001", "", 1, 1, 130000] + [130000] * 9], "Зарплата v2.xlsx")
        intake.run_salary_months(self.db, self.app.case, again)
        self.db.expire_all()
        self.assertEqual(self.table()["rows"][0]["months"][2:5], [130000, 999, 130000])
        self.assertEqual(self.db.get(self.d.Document, doc.id).state, "заменен")


class SalaryMonthsUiTests(unittest.TestCase):
    """Вкладка стоит сразу за штатным расписанием, как в макете «Кадры»."""

    def test_tab_follows_staff_and_staff_has_no_salary(self):
        start = JS.index("var REG_TABS = [")
        tabs = JS[start:JS.index("];", start)]
        self.assertLess(tabs.index('key: "emp"'), tabs.index('key: "zp"'))
        self.assertIn('title: "Заработная плата по месяцам"', tabs)
        self.assertNotIn('k: "salary"', branch("emp"))

    def test_month_cells_are_editable_and_total_comes_from_server(self):
        zp = branch("zp")
        self.assertIn('regGrid("zp"', zp)
        self.assertIn("totals: ztot", zp)
        self.assertIn("zpCell(x, i + 1)", zp)
        cell = function("zpCell")
        self.assertIn('data-zpedit="', cell)
        self.assertIn('role="button" tabindex="0"', cell)
        # Пустой месяц срока правится: в него вписывают сумму; вне срока — нет.
        self.assertIn("if (v == null && x.active.indexOf(m) < 0) return { v: null };", cell)
        self.assertNotIn("штатного расписания", cell)
        # Окно перед расчётом называет причину словами сервера.
        self.assertIn('pf["почему"]', function("solve"))
        self.assertIn('"/api/salary_months", { method: "PUT"', function("zpCellEdit"))
        # Правка открывается и с клавиатуры, не только щелчком.
        self.assertIn('var zk = e.target.closest("[data-zpedit]")', JS)
        # Итог — строкой подвала общего движка и только без поиска и отбора.
        # Пустой таблице итог не нужен: она показывается шапкой граф (29.09.2026).
        self.assertIn("if (opts.totals && rows.length && list.length === rows.length)",
                      function("regGrid"))

    def test_rows_come_from_document_or_chosen_employee(self):
        zp = branch("zp")
        self.assertIn('add: "data-zpadd", addLabel: "Добавить сотрудника"', zp)
        # Файл — строкой каталога под таблицей: второй загрузки в шапке нет.
        self.assertIn('catalogFor("salary", "Документы зарплаты по месяцам")', zp)
        self.assertNotIn("upload:", zp)
        dialog = function("zpAddDialog")
        self.assertIn("<select data-zpwho>", dialog)
        self.assertIn('"/api/salary_rows", { method: "POST"', dialog)
        self.assertIn("holdFocus(wrap", dialog)
        self.assertIn("Удалить строку", function("openZpMenu"))
        self.assertIn('var trz = e.target.closest("tr[data-zprow]")', JS)


if __name__ == "__main__":
    unittest.main()
