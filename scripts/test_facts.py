"""Факт начислений (пересчёт по факту, 30.09.2026).

Стенд на временной базе, без сервера, модели и решателя. Проверяет, что книга
факта узнаётся по шапке — форма 6.1 (графы месяцев), 6.3 «ШР на дату,
детализация назначений» (срок режется датой выгрузки) и форма для заполнения;
что сотрудник и договор находятся в реестре; что новый файл заменяет только
свои месяцы; что месяцы факта берутся из документа — подряд с января, — и во
вход решателя уходят лист «факт_выплат» и «факт по месяц», зарплата месяцев
факта не требуется, а закрепления урезаются до месяцев после факта; что на
экране факт — та же таблица 6.1, что в отчёте.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "harness"))

HEAD_63 = ["Таб.№", "Назначение", "Фамилия И.О., уч. ст., уч. зван.", "Код подр.",
           "Подразделение", "Должность", "Категория персонала", "Код пар-ра",
           "Название параметра", "Ставка", "Номинальное значение параметра",
           "Значение параметра по ставке", "Сумма в руб.", "Начало действия",
           "Окончание действия", "Код шифра затрат", "Шифр затрат", "Лицевой счет"]


def row_63(tab, fio, position, code, name, rate, amount, start, end, contract, account=None):
    return [tab, None, fio, None, None, position, None, code, name, rate, amount, amount, amount,
            start, end, contract, None, account]


class FactsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lifecycle import App
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, FOT_OPERATOR="facts-test-operator")
        cls.env.start()
        cls.app = App(cls.temp.name)
        import basis
        import build_input
        import facts
        import intake
        cls.basis, cls.build_input, cls.facts, cls.intake = basis, build_input, facts, intake

    @classmethod
    def tearDownClass(cls):
        cls.app.db.close()
        cls.app.d.engine.dispose()
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.db, self.d, self.main = self.app.db, self.app.d, self.app.main
        d = self.d
        for model in (d.ActualPayment, d.Employee, d.Contract, d.SalaryMonth, d.SalaryRow,
                      d.Document, d.Decision, d.Message, d.Run):
            self.db.query(model).delete()
        self.case = self.app.case
        self.case.plan_basis = None
        self.case.plan_settings = None
        self.db.commit()
        staff = self.doc("Штатка.xlsx")
        for code, fio in (("E1", "Иванов Иван Иванович"), ("E2", "Петров Пётр Петрович")):
            self.db.add(d.Employee(code=code, person_code="T" + code[1:], fio=fio,
                                   position="Инженер", rate=1.0, date_from="01.01.2026",
                                   date_to="31.12.2026", document_id=staff.id))
            for m in range(1, 13):
                self.db.add(d.SalaryMonth(employee_code=code, year=2026, month=m,
                                          amount=100000, source="стенд"))
        for code, acc in (("C_A", "20576У17400"), ("C_B", "20576У17401")):
            self.db.add(d.Contract(code=code, name="Договор " + code, fund=2400000,
                                   account=acc, date_from="01.01.2026", date_to="31.12.2026"))
        self.db.commit()

    def doc(self, name, path="нет"):
        row = self.d.Document(name=name, path=path, state="разобран", kind="штатное расписание",
                              version=1, scope="реестр")
        self.db.add(row)
        self.db.commit()
        return row

    def book(self, name, head, rows):
        import openpyxl
        path = os.path.join(self.temp.name, name)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "6.3 ШР детализация"
        ws.append(head)
        for r in rows:
            ws.append(r)
        wb.save(path)
        return path

    def load(self, name, rows, head=HEAD_63, path=None):
        path = path or self.book(name, head, rows)
        doc = self.d.Document(name=name, path=path, state="загружен", scope="реестр", version=1)
        self.db.add(doc)
        self.db.commit()
        self.intake.run_facts(self.db, self.case, doc)
        return doc

    def test_form_63_is_recognised_before_salary_months(self):
        path = self.book("факт.xlsx", HEAD_63, [
            row_63("E1", "Иванов И. И.", "Инженер, 1 ставк.", 1, "Оклад", 1, 40400,
                   "01.01.2026", "28.02.2026", "C_A")])
        self.assertEqual(self.facts.layout(path)["form"], "6.3")
        self.assertEqual(self.intake.classify(path, "факт.xlsx")[0], self.facts.KIND)
        # Таблица зарплаты по месяцам фактом не считается: нет вида и договора.
        salary = self.book("зарплата.xlsx", ["Код строки", "ФИО"] + [
            "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь"], [["E1", "Иванов"] + [1] * 6])
        self.assertIsNone(self.facts.layout(salary))

    def test_rows_expand_to_months_and_match_registry(self):
        """Назначение с января по февраль — две строки факта; сотрудник — по
        табельному номеру, договор — по коду шифра или лицевому счёту."""
        self.load("факт.xlsx", [
            row_63("T1", "Иванов И. И.", "Инженер, 1 ставк.", 1, "Оклад", 1, 40400,
                   "01.01.2026", "28.02.2026", "C_A"),
            row_63("E1", "Иванов И. И.", "Инженер, 1 ставк.", 122, "За качество", None, 59600,
                   "01.01.2026", "28.02.2026", None, "20576У17401"),
            row_63("X9", "Сидоров С. С.", "Инженер, 0,5 ставк.", 1, "Оклад", 0.5, 20200,
                   "01.02.2026", "28.02.2026", "C_A"),
        ])
        got = sorted((r.employee_code, r.contract_code, r.month, r.kind, r.amount, r.rate)
                     for r in self.db.query(self.d.ActualPayment).all())
        self.assertEqual(got, [("E1", "C_A", 1, "оклад", 40400.0, 1.0),
                               ("E1", "C_A", 2, "оклад", 40400.0, 1.0),
                               ("E1", "C_B", 1, "122", 59600.0, None),
                               ("E1", "C_B", 2, "122", 59600.0, None),
                               ("X9", "C_A", 2, "оклад", 20200.0, 0.5)])
        # Кого нет в штатном расписании — строка остаётся: деньги с договора
        # уже потрачены; но она помечена несопоставленной.
        x9 = self.db.query(self.d.ActualPayment).filter_by(employee_code="X9").one()
        self.assertFalse(x9.matched)
        self.assertEqual(self.facts.months_available(self.db, 2026), [1, 2])

    def test_new_file_replaces_only_its_months(self):
        first = self.load("январь-февраль.xlsx", [
            row_63("E1", "Иванов", "Инженер", 1, "Оклад", 1, 40400, "01.01.2026", "28.02.2026",
                   "C_A")])
        self.load("февраль.xlsx", [
            row_63("E1", "Иванов", "Инженер", 1, "Оклад", 1, 41000, "01.02.2026", "28.02.2026",
                   "C_A")])
        got = sorted((r.month, r.amount) for r in self.db.query(self.d.ActualPayment).all())
        self.assertEqual(got, [(1, 40400.0), (2, 41000.0)])
        # Январь по-прежнему из первого файла — он не заменён.
        self.assertNotEqual(self.db.get(self.d.Document, first.id).state, "заменен")

    def test_fact_months_come_from_the_document_and_feed_the_input(self):
        """Месяц факта выбирать не нужно — он из самого документа
        (пользователь 30.09.2026: «зачем выбирать месяц, я не поняла?
        требуется подгрузить документ»): январь–февраль загружены — расчёт
        берёт их как были и пересчитывает с марта."""
        self.load("факт.xlsx", [
            row_63("E1", "Иванов", "Инженер", 1, "Оклад", 1, 40400, "01.01.2026", "28.02.2026",
                   "C_A"),
            row_63("E2", "Петров", "Инженер", 1, "Оклад", 1, 40400, "01.01.2026", "28.02.2026",
                   "C_B")])
        self.assertEqual(self.main._basis_out(self.db, self.case)["fact_through"], 2)
        # Договор, снятый с расчёта плана, свой факт во вход не несёт.
        self.basis.set_included(self.db, self.case, ["C_B"], False, "стенд")
        import json
        pin = {"с": 1, "по": 12, "вид": "любой", "сумма": None}
        self.case.plan_settings = json.dumps({"назначения": [
            {**pin, "сотрудник": "E1", "договор": "C_A"},
            {**pin, "сотрудник": "E2", "договор": "C_A", "по": 2}]}, ensure_ascii=False)
        self.db.commit()
        warn = []
        data = self.main._registry_data(self.db, self.case, warn)
        self.assertEqual(data["fact_through"], 2)
        self.assertEqual(sorted((f.employee_code, f.contract_code, f.month) for f in data["facts"]),
                         [("E1", "C_A", 1), ("E1", "C_A", 2)])
        self.main._apply_plan_settings(data, self.case, warn)
        # Закрепление урезано до марта, закрепление целиком в факте ушло.
        self.assertEqual([(f["сотрудник"], f["с"], f["по"]) for f in data["manual_assignments"]],
                         [("E1", 3, 12)])
        # Зарплата месяцев факта не требуется: снимем январь и февраль.
        self.db.query(self.d.SalaryMonth).filter(self.d.SalaryMonth.month <= 2).delete()
        self.db.commit()
        data = self.main._registry_data(self.db, self.case, warn)
        self.assertEqual(self.build_input.salary_gaps(data["employees"], data["salary_months"],
                                                      2026, data["fact_through"]), [])
        import openpyxl
        import reference
        out = os.path.join(self.temp.name, "вход.xlsx")
        self.build_input.build(reference.TEMPLATE, out, data, warn)
        wb = openpyxl.load_workbook(out)
        rows = list(wb["факт_выплат"].iter_rows(values_only=True))
        self.assertEqual(list(rows[0]), self.build_input.FACT_HEAD)
        self.assertEqual(sorted((r[0], r[1], r[2], r[3], r[4]) for r in rows[1:]),
                         [("E1", "C_A", 1, "оклад", 40400), ("E1", "C_A", 2, "оклад", 40400)])
        head = [c.value for c in wb["настройки"][1]]
        self.assertEqual(wb["настройки"].cell(2, head.index("факт по месяц") + 1).value, 2)
        self.assertTrue(any("Факт начислений по февраль" in w for w in warn), warn)

    def test_solved_plan_with_warnings_is_not_no_solution(self):
        """Решатель отдаёт код 1 и при найденном плане, если есть
        предупреждения — а с фактом они обычны (FACT_*_WARNING): такой план
        посчитан, а не «решения нет» (30.09.2026)."""
        self.assertFalse(self.main._no_solution(1, "OPTIMAL"))
        self.assertFalse(self.main._no_solution(1, "FEASIBLE"))
        self.assertFalse(self.main._no_solution(0, "OPTIMAL"))
        self.assertTrue(self.main._no_solution(1, "INFEASIBLE"))
        self.assertTrue(self.main._no_solution(2, "VALIDATION_FAILED"))
        self.assertTrue(self.main._no_solution(1, ""))

    def test_form_61_is_read_by_months(self):
        """Форма 6.1: строка — сотрудник, договор и вид выплаты, графы —
        месяцы; ФИО и договор в строках группы бывают пустыми (объединённые
        ячейки), строки итогов пропускаются. Месяцы с суммами — месяцы факта."""
        head = ["Табельный номер", "ФИО", "Договор", "Вид выплаты"] + [
            "янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек",
            "За год, ₽"]
        self.load("начисления 6.1.xlsx", [
            ["E1", "Иванов Иван Иванович", "C_A", "оклад", 40400, 40400] + [None] * 10 + [80800],
            [None, None, None, "122", 59600, 59600] + [None] * 10 + [119200],
            [None, None, "C_B", "приказ", None, 5000] + [None] * 10 + [5000],
            [None, None, "Итого по сотруднику", None, 100000, 105000] + [None] * 10 + [205000],
        ], head=head)
        got = sorted((r.employee_code, r.contract_code, r.month, r.kind, r.amount)
                     for r in self.db.query(self.d.ActualPayment).all())
        self.assertEqual(got, [("E1", "C_A", 1, "122", 59600.0), ("E1", "C_A", 1, "оклад", 40400.0),
                               ("E1", "C_A", 2, "122", 59600.0), ("E1", "C_A", 2, "оклад", 40400.0),
                               ("E1", "C_B", 2, "приказ", 5000.0)])
        self.assertEqual(self.facts.through(self.db, 2026), 2)

    def test_form_63_is_cut_at_the_report_date(self):
        """«ШР на дату» — назначения со сроками до конца года: фактом они
        становятся только по дату выгрузки из шапки."""
        import openpyxl
        path = os.path.join(self.temp.name, "ШР.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["ШР на дату 28.02.2026, детализация назначений"])
        ws.append(HEAD_63)
        ws.append(row_63("E1", "Иванов", "Инженер, 1 ставк.", 1, "Оклад", 1, 40400,
                         "01.01.2026", "31.12.2026", "C_A"))
        wb.save(path)
        doc = self.d.Document(name="ШР.xlsx", path=path, state="загружен", scope="реестр",
                              version=1)
        self.db.add(doc)
        self.db.commit()
        self.intake.run_facts(self.db, self.case, doc)
        self.assertEqual(self.facts.months_available(self.db, 2026), [1, 2])

    def test_future_months_of_form_61_are_not_facts(self):
        """Суммы месяцев позже даты загрузки — план, а не факт: начислений за
        них ещё быть не может. Дата в заголовке книги сильнее даты загрузки."""
        import datetime as dt
        head = ["Табельный номер", "ФИО", "Договор", "Вид выплаты", "Январь", "Февраль",
                "Март"]
        path = self.book("6.1.xlsx", head, [
            ["T1", "Иванов Иван Иванович", "C_A", "оклад", 40400, 40400, 40400],
            ["T1", "Иванов Иван Иванович", "C_A", "122", 59600, None, None]])
        rows, skipped = self.facts.parse(path, 2026, uploaded=dt.datetime(2026, 2, 20, 10, 0))
        self.assertEqual(sorted((r["kind"], r["month"], r["amount"]) for r in rows),
                         [("122", 1, 59600.0), ("оклад", 1, 40400.0), ("оклад", 2, 40400.0)])
        self.assertEqual(skipped, ["суммы позже 20.02.2026 — план, а не факт: март"])
        path = self.book("6.1 на 31.03.2026.xlsx", head, [
            ["T1", "Иванов Иван Иванович", "C_A", "оклад", 40400, 40400, 40400]])
        rows, skipped = self.facts.parse(path, 2026, uploaded=dt.datetime(2026, 2, 20, 10, 0))
        self.assertEqual((len(rows), skipped), (3, []))

    def test_facts_are_shown_and_given_back_in_form_63(self):
        """Факт на экране и в форме для заполнения — формой 6.3 «ШР на дату,
        детализация назначений», как раздел 6.3 отчёта и шаблон кадров
        пользователя (30.09.2026: «вот этот шаблон, я же скидывала»):
        назначение — строка на период с одной суммой и ставкой. Форма — в
        раскладке шаблона: заголовок в четыре строки, шапка в седьмой, под
        ней номера граф; без факта — одна шапка, с фактом — его строки и
        «Дата построения» — последний день его месяцев. Загруженная обратно,
        она даёт тот же факт, а строку номеров граф разбор пропускает."""
        import io
        import openpyxl

        def sheet():
            return openpyxl.load_workbook(io.BytesIO(self.main.facts_form(self.case.id).body)).active

        ws = sheet()
        self.assertEqual([ws.cell(r, 1).value for r in range(1, 5)], [
            "Подразделение - по всем подразделениям", "Дата построения __.__.____",
            "Лицевой счет - по всем ЛС", "Параметры назначений на дату,"])
        self.assertEqual([c.value for c in ws[7]], self.facts.SHR_HEAD)
        self.assertEqual([c.value for c in ws[8]], list(range(1, 24)))
        self.assertEqual(ws.max_row, 8)
        self.load("факт.xlsx", [
            row_63("E1", "Иванов", "Инженер, 1 ставк.", 1, "Оклад", 1, 40400, "01.01.2026",
                   "28.02.2026", "C_A"),
            row_63("E1", "Иванов", "Инженер", 122, "За качество", None, 59600, "01.01.2026",
                   "31.01.2026", "C_A"),
            row_63("E1", "Иванов", "Инженер", 122, "За качество", None, 30000, "01.02.2026",
                   "28.02.2026", "C_A")])
        got = [(r["таб"], r["фио"], r["должность"], r["код"], r["ставка"], r["сумма"],
                r["начало"], r["окончание"], r["код_шифра"], r["шифр"], r["счет"])
               for r in self.facts.detail_rows(self.db, 2026)]
        self.assertEqual(got, [
            ("E1", "Иванов Иван Иванович", "Инженер, 1 ставк.", "1", 1.0, 40400.0,
             "01.01.2026", "28.02.2026", "C_A", "Договор C_A", "20576У17400"),
            ("E1", "Иванов Иван Иванович", "Инженер", "122", None, 59600.0,
             "01.01.2026", "31.01.2026", "C_A", "Договор C_A", "20576У17400"),
            ("E1", "Иванов Иван Иванович", "Инженер", "122", None, 30000.0,
             "01.02.2026", "28.02.2026", "C_A", "Договор C_A", "20576У17400")])
        ws = sheet()
        self.assertEqual(ws["A2"].value, "Дата построения 28.02.2026")
        self.assertEqual(ws.max_row, 11)
        path = os.path.join(self.temp.name, "форма 6.3.xlsx")
        ws.parent.save(path)
        self.assertEqual((self.facts.layout(path)["form"], self.facts.layout(path)["row"]),
                         ("6.3", 7))
        rows, skipped = self.facts.parse(path, 2026)
        self.assertEqual((len(rows), skipped), (4, []))
        before = sorted((r.month, r.kind, r.amount)
                        for r in self.db.query(self.d.ActualPayment).all())
        self.load("форма 6.3.xlsx", [], head=None, path=path)
        after = sorted((r.month, r.kind, r.amount)
                       for r in self.db.query(self.d.ActualPayment).all())
        self.assertEqual(after, before)

    def test_no_facts_means_no_fact_sheet(self):
        data = self.main._registry_data(self.db, self.case, [])
        self.assertEqual((data["fact_through"], data["facts"]), (0, []))
        import openpyxl
        import reference
        out = os.path.join(self.temp.name, "вход0.xlsx")
        self.build_input.build(reference.TEMPLATE, out, data, [])
        self.assertNotIn("факт_выплат", openpyxl.load_workbook(out).sheetnames)


class ResultJsonTests(unittest.TestCase):
    """Разбор результата для карточки расчёта (docs/ui/result2json.py) — без
    листа «фот_по_месяцам»: сервис его не пишет с 18.09.2026 (SOURCE-009), а
    разбор на нём падал — у удачного расчёта пропадала сводка, у «решения
    нет» — проверки решателя (найдено сквозной проверкой 30.09.2026)."""

    def test_input_without_inflow_sheet_is_converted(self):
        import json
        import openpyxl
        sys.path.insert(0, str(ROOT / "docs" / "ui"))
        import result2json
        with tempfile.TemporaryDirectory() as tmp:
            inp, res, out = (os.path.join(tmp, n) for n in ("in.xlsx", "res.xlsx", "out.json"))
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "сотрудники"
            ws.append(["код строки", "фио", "должность", "подразделение", "ставка", "зарплата",
                       "дата начала"])
            ws.append(["E1", "Иванов", "Инженер", "Отдел", 1, 100000, "2026-01-01"])
            ws = wb.create_sheet("договоры")
            ws.append(["код", "название", "номер", "тип договора", "ГОЗ", "фот"])
            ws.append(["C_A", "Договор А", "1", "НИР", "нет", 1200000])
            wb.save(inp)
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "План выплат"
            ws.append(["код строки", "месяц", "договор", "вид выплаты", "сумма"])
            ws.append(["E1", "Январь", "C_A", "оклад", 40400])
            ws = wb.create_sheet("Итог расчета")
            ws.append(["показатель", "значение", "статус", "комментарий"])
            ws.append(["Статус", "OPTIMAL", None, None])
            wb.save(res)
            result2json.convert(inp, res, out)
            with open(out, encoding="utf-8") as f:
                got = json.load(f)
        self.assertEqual(got["inflow"], {})
        self.assertEqual([(r["emp"], r["sum"]) for r in got["plan"]], [("E1", 40400.0)])


JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")


class FactsUiTests(unittest.TestCase):
    """Факт начислений на экране (30.09.2026): месяц не выбирается — он из
    документа («зачем выбирать месяц, я не поняла?»); документ загружают на
    вкладке плана или реестра и сразу видят ту же таблицу 6.3 «ШР на дату,
    детализация назначений», что в отчёте (о плоской таблице и о 6.1: «очень
    похожа, но не та», «всё ещё не та таблица»; «и как загружать её?»)."""
    HTML = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")

    def body(self, name):
        start = JS.index("function %s(" % name)
        return JS[start:JS.index("\nfunction ", start + 1)]

    def test_no_month_choice_in_the_plan_header(self):
        self.assertNotIn('data-sel="fact"', JS)
        self.assertNotIn("fact_through: +", JS)

    def test_plan_input_tab_and_registry_tab(self):
        self.assertLess(self.HTML.index('data-view="zp"'), self.HTML.index('data-view="fact"'))
        self.assertIn('data-view="fact">Факт начислений</button>', self.HTML)
        self.assertIn('fact: "facts"', JS[JS.index("var ROUTE_VIEW"):])
        self.assertIn('v === "fact"', JS[JS.index("var need = v ==="):][:200])
        self.assertIn('{ key: "fact", title: "Факт начислений", group: "Данные" }', JS)
        plan = self.body("factPlanView")
        self.assertIn('factsGrid("pfact", (vdata && vdata.facts_detail) || [], renderView)', plan)
        # Строка статуса — одна и только на вкладке плана: реестр о планах не
        # знает (UI-005); пояснений «— факт берётся подряд с января» нет.
        self.assertIn("Факт в расчёте: ", plan)
        self.assertIn("не в расчёте: ", plan)
        self.assertNotIn("факт берётся", JS)
        branch = JS[JS.index('} else if (regTab === "fact") {'):
                    JS.index('} else if (regTab === "secret") {')]
        self.assertIn('body = factsGrid("fact", d.facts_detail || []);', branch)
        # Каталог документов — как у соседних вкладок: с загрузкой и поиском
        # (пользователь 30.09.2026: «ты забыл добавить в таблицу документов
        # загрузку документа и поиск»).
        self.assertIn('catalogFor("facts", "Документы факта начислений")', branch)
        for gone in ("function factsView(", "function factsTable61(", "function factsTable6(",
                     "function staffDetailTable(", "data-facts"):
            self.assertNotIn(gone, JS)
        # Загрузки из плана нет: факт принадлежит реестру, план поверх него
        # правит только своё (пользователь 30.09.2026: «в реестре — то, что
        # переиспользуется, но также мы можем перезаписать внутри плана»).
        self.assertNotIn("/* Вкладка плана «Факт начислений»: файл, выбранный", JS)
        self.assertIn('bar: render ? { placeholder: "Таб.№ или ФИО", right: form }',
                      self.body("factsGrid"))

    def test_facts_table_is_a_registry_grid_with_template_columns(self):
        """Вкладка собрана из деталей реестра, как «Штатное расписание»
        (разбор дизайнера 30.09.2026 по словам пользователя «здесь что-то
        странное с интерфейсом»): regGrid в панели, в шапке — загрузка, лупа,
        «Форма для заполнения» и «Графы»; графы — ровно шаблона кадров, тот же
        перечень, что на сервере (facts.SHR_HEAD). Строка без договора в
        реестре или без сотрудника — тише, причина в подсказке. Проверка — в
        node с заглушкой движка таблиц."""
        import json
        import re
        import subprocess
        # Перечень сервера — из исходника: импорт facts поднял бы базу.
        import ast
        tree = ast.parse((ROOT / "app" / "facts.py").read_text(encoding="utf-8"))
        shr = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                   and any(getattr(x, "id", "") == "SHR_HEAD" for x in n.targets))
        js_head = json.loads(re.search(r"var SHR_HEAD = (\[[^\]]+\]);", JS).group(1))
        self.assertEqual(js_head, shr)
        consts = (re.search(r"var SHR_HEAD = \[[^\]]+\];", JS).group(0) +
                  re.search(r"var TB_DOWN = [\s\S]*?;\n", JS).group(0))
        funcs = "".join(self.body(n) for n in ("esc", "mo", "rf", "factsGrid"))
        script = (consts + funcs + "var caseId = 7;"
                  "function regGrid(id, cols, rows, opts) { return JSON.stringify({id: id,"
                  " cols: cols.map(function (c) { return [c.label, c.kind || '', !!c.always]; }),"
                  " bar: opts.bar, render: typeof opts.render,"
                  " cls: rows.map(opts.rowCls), attr: rows.map(opts.rowAttr),"
                  " sum: cols[12].val(rows[0])}); }"
                  "function renderView() {}"
                  "var rows = [{таб: 'T1', сумма: 40400, known: true, matched: true},"
                  " {таб: 'T2', сумма: 1, known: false, matched: true},"
                  " {таб: 'T3', сумма: 1, known: true, matched: false}];"
                  "console.log(JSON.stringify([factsGrid('fact', rows),"
                  " factsGrid('pfact', rows, renderView)]));")
        got, plan = [json.loads(x) for x in json.loads(subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, encoding="utf-8",
            check=True).stdout)]
        # Вкладка плана — без загрузки, «Форма для заполнения» — и там
        # («нет, форму оставь»).
        self.assertNotIn("upload", plan["bar"])
        self.assertNotIn("addLabel", plan["bar"])
        self.assertIn("Форма для заполнения", plan["bar"]["right"])
        self.assertEqual(plan["render"], "function")
        self.assertEqual([c[0] for c in got["cols"]], shr)
        self.assertEqual(got["cols"][0][2], True)
        self.assertEqual([c[1] for c in got["cols"][9:15]],
                         ["number", "number", "number", "number", "date", "date"])
        self.assertEqual(got["bar"]["upload"], "")
        self.assertEqual(got["bar"]["addLabel"], "Загрузить факт начислений")
        self.assertEqual(got["bar"]["placeholder"], "Таб.№ или ФИО")
        self.assertIn('href="/api/case/7/facts/form" download', got["bar"]["right"])
        self.assertIn("Форма для заполнения", got["bar"]["right"])
        self.assertEqual(got["render"], "undefined")
        self.assertEqual(got["cls"], ["", "off", "off"])
        self.assertEqual(got["attr"][0], "")
        self.assertIn("Договора нет в реестре", got["attr"][1])
        self.assertIn("Нет в штатном расписании", got["attr"][2])
        self.assertRegex(got["sum"], r"^40\D400$")
        # «Форма для заполнения» — ссылка тем же видом, что «Графы»: без
        # подчёркивания.
        workspace = (ROOT / "app" / "static" / "workspace.css").read_text(encoding="utf-8")
        for rule in ("#view .tbar [data-tb]{",
                     "#regview .tbar [data-tb],#view :where(.gridwrap) .tbar [data-tb]{"):
            body = workspace[workspace.index(rule):]
            self.assertIn("text-decoration:none", body[:body.index("}")], rule)


if __name__ == "__main__":
    unittest.main()
