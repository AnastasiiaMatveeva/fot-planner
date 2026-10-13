"""Проекты приказов из расчёта плана (решения пользователя 28–30.09.2026).

«По конкретному договору выбрать тип — приказ, в выбранный месяц для
выбранного сотрудника, чтоб в шаблон подставилось и выгрузилось; правда,
шаблона пока нет, но нужна заготовка»; о каскаде выборов: «форма выбора
приказа на сотрудника не продуманная»; о кнопке заготовки: «зачем эта
кнопка?». Стенд на временной базе, без сервера и решателя: расчёт — маленькая
книга «План выплат». Проверяет правило «одно изменение — одна строка одного
приказа» (стимулирующая выплата; установление, изменение и прекращение
надбавки; смена источника у договора с наибольшим приростом), месяцы факта,
список приказов вкладки, файл приказа — весь и на одного сотрудника — по
бланку сервиса и по шаблону организации, заготовки одним архивом и то, что
вкладка — одна таблица без каскада выборов.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "harness"))

MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август",
          "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]


def doc_text(raw):
    return zipfile.ZipFile(io.BytesIO(raw)).read("word/document.xml").decode("utf-8")


class OrderListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lifecycle import App
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, FOT_OPERATOR="order-test-operator")
        cls.env.start()
        cls.app = App(cls.temp.name)
        import order
        cls.order = order

    @classmethod
    def tearDownClass(cls):
        cls.app.db.close()
        cls.app.d.engine.dispose()
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.db, self.d, self.main = self.app.db, self.app.d, self.app.main
        for model in (self.d.Employee, self.d.Contract, self.d.Run, self.d.WorkOrderMember,
                      self.d.WorkOrder):
            self.db.query(model).delete()
        for code, tab, fio in (("E1", "1001", "Иванов Иван Иванович"),
                               ("E2", "1002", "Петров Пётр Петрович")):
            self.db.add(self.d.Employee(code=code, person_code=tab, fio=fio, position="Инженер",
                                        department="Отдел 12", rate=1.0))
        for code in ("C_A", "C_B"):
            self.db.add(self.d.Contract(code=code, name="Договор " + code, number="1/26",
                                        account="2057" + code[-1]))
        self.db.commit()

    # План: оклад с C_A январь–февраль, с марта — с C_B; 122 с C_A в январе
    # 50 000, в феврале 60 000, в марте нет; приказ с C_A в феврале 7 000.
    PLAN = [("E1", "C_A", 1, "оклад", 40400), ("E1", "C_A", 2, "оклад", 40400),
            ("E1", "C_B", 3, "оклад", 40400), ("E1", "C_A", 1, "122", 50000),
            ("E1", "C_A", 2, "122", 60000), ("E1", "C_A", 2, "приказ", 7000)]

    def plan(self, rows=None):
        return [{"emp": e, "contract": c, "month": m, "kind": k, "amount": a}
                for e, c, m, k, a in (rows or self.PLAN)]

    def orders(self, rows=None, frozen=0):
        return self.main._orders(self.db, self.main._order_changes(self.plan(rows), frozen))

    def run_with_plan(self, rows=None):
        path = os.path.join(self.temp.name, "result.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "План выплат"
        ws.append(["код строки", "ФИО", "должность", "месяц", "договор", "вид выплаты", "сумма"])
        for e, c, m, k, a in rows or self.PLAN:
            ws.append([e, "Иванов Иван Иванович", "Инженер", MONTHS[m - 1], c, k, a])
        wb.save(path)
        run = self.d.Run(case_id=self.app.case.id, status="OPTIMAL", settings="{}",
                         input_path=path, result_path=path)
        self.db.add(run)
        self.db.commit()
        return run

    def test_one_change_is_one_line_of_one_order(self):
        """Строка вкладки — приказ вида × договора × месяца; одно изменение —
        одна строка одного приказа (спецификация дизайнера 30.09.2026).
        Переход оклада с C_A на C_B — один приказ об источнике, у C_B."""
        got = [(o["key"], [(ln["employee"], ln["kind"], ln["contract"], ln["action"],
                            ln["before"], ln["after"]) for ln in o["lines"]])
               for o in self.orders()]
        self.assertEqual(got, [
            ("надбавка|C_A|1", [("E1", "122", "C_A", "установить", None, 50000)]),
            ("стимулирующая|C_A|2", [("E1", "приказ", "C_A", None, None, 7000)]),
            ("надбавка|C_A|2", [("E1", "122", "C_A", "изменить", 50000, 60000)]),
            ("надбавка|C_A|3", [("E1", "122", "C_A", "прекратить", 60000, None)]),
            ("источник|C_B|3", [("E1", "оклад", "C_A", None, 40400, None),
                                ("E1", "оклад", "C_B", None, None, 40400)])])
        src = [o for o in self.orders() if o["kind"] == "источник"][0]
        self.assertEqual((src["title"], src["people"], src["kinds"], src["before"], src["after"],
                          src["contract_name"]),
                         ("Об изменении источника финансирования", 1, ["оклад"], 40400, 40400,
                          "Договор C_B"))

    def test_allowance_moving_between_contracts_is_only_a_source_change(self):
        """Переход 122 с A на B — только строка источника: не «прекратить» на
        A, «установить» на B и источник дважды, как прежде."""
        got = [o["key"] for o in self.orders([("E1", "C_A", 1, "122", 50000),
                                              ("E1", "C_B", 2, "122", 50000)])]
        # В марте 122 на C_B больше нет — это уже прекращение надбавки.
        self.assertEqual(got, ["надбавка|C_A|1", "источник|C_B|2", "надбавка|C_B|3"])

    def test_fact_months_give_no_orders(self):
        """Месяцы факта прошли — приказов по ним нет; первый месяц плана
        сравнивается с последним фактическим."""
        self.assertEqual([o["key"] for o in self.orders(frozen=2)],
                         ["надбавка|C_A|3", "источник|C_B|3"])

    def test_orders_and_files_from_the_run(self):
        """Список — по расчёту; «Скачать» у приказа — все люди одним файлом,
        у строки сотрудника — один человек."""
        from fastapi import HTTPException
        self.assertEqual(self.main.plan_orders(self.app.case.id),
                         {"run": None, "frozen": 0, "orders": []})
        run = self.run_with_plan()
        got = self.main.plan_orders(self.app.case.id)
        self.assertEqual((got["run"], len(got["orders"])), (run.id, 5))
        text = doc_text(self.main.plan_order_file(self.app.case.id, "источник", "C_B", 3).body)
        for part in ("Об изменении источника финансирования оплаты труда",
                     "1. С 1 марта 2026 года оплату труда работника производить",
                     "Иванов Иван Иванович", "1001", "C_A", "2057A", "C_B", "40 400,00",
                     "С приказом ознакомлен(а)"):
            self.assertIn(part, text, part)
        resp = self.main.plan_order_file(self.app.case.id, "стимулирующая", "C_A", 2,
                                         employee="E1")
        text = doc_text(resp.body)
        self.assertIn("О стимулирующей выплате по договору C_A", text)
        self.assertIn("1. Установить работнику стимулирующую выплату за февраль 2026 года", text)
        self.assertIn("7 000,00", text)
        from urllib.parse import quote
        self.assertIn("filename*=UTF-8''" + quote(
            "Приказ о стимулирующей выплате C_A Иванов И. И. февраль 2026.docx"),
            resp.headers["content-disposition"])
        with self.assertRaises(HTTPException) as e:
            self.main.plan_order_file(self.app.case.id, "стимулирующая", "C_A", 5)
        self.assertEqual(e.exception.status_code, 404)

    def test_order_for_several_people_lists_them(self):
        rows = self.PLAN + [("E2", "C_A", 2, "приказ", 3000)]
        self.run_with_plan(rows)
        order = [o for o in self.main.plan_orders(self.app.case.id)["orders"]
                 if o["key"] == "стимулирующая|C_A|2"][0]
        self.assertEqual((order["people"], order["after"]), (2, 10000))
        text = doc_text(self.main.plan_order_file(self.app.case.id, "стимулирующая", "C_A",
                                                  2).body)
        for part in ("Установить работникам", "Иванов Иван Иванович", "Петров Пётр Петрович",
                     "Итого", "10 000,00", "С приказом ознакомлены:"):
            self.assertIn(part, text, part)

    def test_run_without_files_is_an_error(self):
        """Расчёт без файлов — сообщение, а не молчаливая подмена закреплениями."""
        from fastapi import HTTPException
        run = self.d.Run(case_id=self.app.case.id, status="OPTIMAL", settings="{}",
                         input_path="нет", result_path=os.path.join(self.temp.name, "нет.xlsx"))
        self.db.add(run)
        self.db.commit()
        with self.assertRaises(HTTPException) as e:
            self.main.plan_orders(self.app.case.id, run.id)
        self.assertEqual((e.exception.status_code, e.exception.detail),
                         (409, "Проекты приказов недоступны: файлы прогона не сохранены"))

    def test_organisation_template_is_filled(self):
        """Шаблон организации — заготовка сервиса с полями {Имя}, положенная в
        data/templates/<вид>.docx: приказ заполняется по нему. Заготовки —
        одним архивом с перечнем полей."""
        self.run_with_plan()
        stub = self.order.stub("стимулирующая")
        for field in ("{ФИО}", "{Таб_номер}", "{Размер}", "{Договор_шифр}", "{Месяц}"):
            self.assertIn(field, doc_text(stub), field)
        folder = os.path.join(self.app.d.DATA_DIR, "templates")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "стимулирующая.docx")
        with open(path, "wb") as f:
            f.write(stub)
        self.addCleanup(os.remove, path)
        text = doc_text(self.main.plan_order_file(self.app.case.id, "стимулирующая", "C_A",
                                                  2).body)
        self.assertNotIn("{", text)
        self.assertIn("Иванов Иван Иванович", text)
        self.assertIn("7 000,00", text)
        pack = zipfile.ZipFile(io.BytesIO(self.main.order_templates().body))
        self.assertEqual(sorted(pack.namelist()),
                         sorted(["стимулирующая.docx", "надбавка.docx", "источник.docx",
                                 "состав группы.docx", "Поля шаблонов.txt"]))
        self.assertIn("{Размер_было}", doc_text(pack.read("надбавка.docx")))
        self.assertIn("{Месяц_род}", pack.read("Поля шаблонов.txt").decode("utf-8"))

    # Приказ о внесении изменений в состав рабочей группы (UI-023, решения
    # пользователя 12.10.2026).
    def with_group(self):
        """Группа договора C_A: приказ 7017-к, в составе Е1 (UI-023)."""
        base = self.d.WorkOrder(kind="создание", number="7017-к", date="28.07.2026",
                                contract_code="C_A", agreement="075-15", agreement_date="01.02.2026",
                                topic="Разработка схемы", account="2057A",
                                date_from="01.01.2026", date_to="31.12.2026")
        self.db.add(base)
        self.db.flush()
        self.db.add(self.d.WorkOrderMember(order_id=base.id, n=1, action="включить",
                                           fio="Иванов Иван Иванович", person_code="1001",
                                           position="Инженер", employee_code="E1"))
        self.db.commit()

    def group(self, rows, frozen=0):
        return [(o["key"], [(ln["employee"], ln["action"]) for ln in o["lines"]], o["kinds"])
                for o in self.group_orders(rows, frozen) if o["kind"] == "состав группы"]

    def group_orders(self, rows=None, frozen=0):
        lines = self.main._order_lines(self.db, self.app.case, self.plan(rows), frozen)
        return self.main._orders(self.db, lines)

    def test_include_from_first_payment_and_exclude_after_last(self):
        """Е2 не в группе, получает с C_A с апреля — «включить» с апреля; Е1 в
        группе, выплаты с C_A кончились в мае — «исключить» с июня. C_B без
        группы — приказа нет, хотя Е2 получает и с него."""
        self.with_group()
        rows = ([("E1", "C_A", m, "оклад", 40000) for m in range(1, 6)] +
                [("E2", "C_A", m, "приказ", 5000) for m in range(4, 13)] +
                [("E2", "C_B", m, "оклад", 30000) for m in range(1, 13)])
        self.assertEqual(self.group(rows), [
            ("состав группы|C_A|4", [("E2", "включить")], []),
            ("состав группы|C_A|6", [("E1", "исключить")], [])])

    def test_member_paid_to_the_end_and_fact_months(self):
        """Член группы с выплатами до конца срока остаётся. Месяцы факта
        приказов не дают: выплата с февраля при факте январь–март — «включить»
        с апреля; член без выплат — «исключить» с первого месяца плана."""
        self.with_group()
        rows = [("E1", "C_A", m, "оклад", 40000) for m in range(1, 13)]
        self.assertEqual(self.group(rows), [])
        rows = [("E2", "C_A", m, "оклад", 30000) for m in range(2, 13)]
        self.assertEqual(self.group(rows, frozen=3), [
            ("состав группы|C_A|4", [("E1", "исключить"), ("E2", "включить")], [])])

    def test_file_reads_back_as_a_change_order(self):
        """Файл приказа — бланк сервиса с полями приказа-входа: подписанный
        (дата и номер вписаны), он разбирается как приказ о внесении
        изменений в приказ 7017-к с теми же людьми и действиями."""
        self.with_group()
        import docread
        import workgroup
        rows = ([("E1", "C_A", m, "оклад", 40000) for m in range(1, 4)] +
                [("E2", "C_A", m, "оклад", 30000) for m in range(4, 13)])
        self.run_with_plan(rows)
        year = self.app.case.year
        resp = self.main.plan_order_file(self.app.case.id, "состав группы", "C_A", 4)
        text = doc_text(resp.body)
        for part in ("О внесении изменений в приказ от 28.07.2026 № 7017-к",
                     "соглашение от 01.02.2026 № 075-15 «Разработка схемы» (л/с 2057A)",
                     "на период с 01.04.%d по 31.12.2026:" % year,
                     "включить в состав рабочей группы:", "исключить из состава рабочей группы:",
                     "Петров Пётр Петрович", "1002", "Отдел 12"):
            self.assertIn(part, text, part)
        self.assertNotIn("Главному бухгалтеру", text)
        from urllib.parse import quote
        self.assertIn(quote("Приказ о внесении изменений в состав рабочей группы C_A апрель"),
                      resp.headers["content-disposition"])
        signed = zipfile.ZipFile(io.BytesIO(resp.body))
        path = os.path.join(self.temp.name, "подписанный.docx")
        with zipfile.ZipFile(path, "w") as z:
            for item in signed.infolist():
                body = signed.read(item.filename)
                if item.filename == "word/document.xml":
                    body = body.decode("utf-8").replace(
                        "____________ № ____________", "05.04.%d № 123-к" % year, 1).encode("utf-8")
                z.writestr(item, body)
        got = workgroup.parse(docread.from_docx(path))
        self.assertEqual((got["вид"], got["номер"], got["дата"], got["основание"], got["счёт"],
                          got["соглашение"], got["с"], got["по"]),
                         ("изменение", "123-к", "05.04.%d" % year,
                          {"дата": "28.07.2026", "номер": "7017-к"}, "2057A", "075-15",
                          "01.04.%d" % year, "31.12.2026"))
        self.assertEqual([(r["табельный"], r["действие"]) for r in got["состав"]],
                         [("1002", "включить"), ("1001", "исключить")])


class OrderUiTests(unittest.TestCase):
    """Вкладка «Проекты приказов» — одна таблица приказов (спецификация
    дизайнера 30.09.2026): каскада выборов, строки «Приказов N», текста
    «…пока нет» и кнопки заготовки на ней нет."""
    JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

    def body(self, name):
        start = self.JS.index("function %s(" % name)
        return self.JS[start:self.JS.index("\nfunction ", start + 1)]

    def test_tab_is_one_table_of_orders(self):
        branch = self.JS[self.JS.index('  if (view === "ord") {'):]
        branch = branch[:branch.index("    return;\n  }")]
        self.assertIn("el.innerHTML = ordersView();", branch)
        view = self.body("ordersView")
        self.assertIn('regGrid("ord", cols, vorders.orders, {', view)
        self.assertIn("render: renderView", view)
        for gone in ("data-oe", "orderEmployeeBlock", "Приказов пока нет", '"Приказов "',
                     "/order/employee", "/api/order/template?", "Заготовк"):
            self.assertNotIn(gone, self.JS if gone != "Заготовк" else view, gone)
        self.assertIn("План ещё не рассчитан", view)
        # Данные вкладки — список приказов расчёта из шапки; смена расчёта
        # оставляет на вкладке.
        self.assertIn('? stateReady().then(loadOrders)', self.JS)
        self.assertIn('view: view === "ord" ? "ord" : "sum"', self.JS)
        self.assertIn('api("/api/case/" + caseId + "/orders?run=" + run.id)', self.body("loadOrders"))

    def test_rows_and_employee_lines(self):
        """Отрисовка в node с заглушкой движка таблиц: графы, строка приказа и
        строки сотрудников раскрытого приказа — ФИО и «Скачать» только в
        первой строке человека, договор строки — только у источника."""
        consts = (re.search(r"var MONTHS_OF = \[[^\]]+\];", self.JS).group(0) +
                  re.search(r"var TB_DOWN = [\s\S]*?;\n", self.JS).group(0))
        funcs = "".join(self.body(n) for n in ("esc", "mo", "cap", "ordersRun", "ordersView"))
        script = (consts + funcs +
                  "var caseId = 7, chosenRun = null, tbQuery = {}, ordOpen = {'источник|C_B|3': true};"
                  "var state = {runs: [{id: 5, status: 'OPTIMAL'}]};"
                  "function fixMonthText(m) { return ('0' + m).slice(-2) + '.26'; }"
                  "function renderView() {}"
                  "function regGrid(id, cols, rows, opts) { return JSON.stringify({id: id,"
                  " heads: cols.map(function (c) { return c.label || ''; }),"
                  " row: cols.map(function (c) { var r = c.cell ? c.cell(rows[0]) : null;"
                  "   return r ? r.v : c.val ? c.val(rows[0]) : rows[0][c.k]; }),"
                  " sub: (opts.sub(rows[0], cols) || []).map(function (s) {"
                  "   return s.cells.map(function (x) { return x.v; }); }),"
                  " text: opts.text(rows[0])}); }"
                  "var vorders = {run: 5, orders: [{key: 'источник|C_B|3', kind: 'источник',"
                  " title: 'Об изменении источника финансирования', contract: 'C_B',"
                  " contract_name: 'Договор C_B', month: 3, kinds: ['оклад'], people: 1,"
                  " before: 40400, after: 40400, lines: ["
                  " {employee: 'E1', fio: 'Иванов Иван Иванович', person_code: '1001',"
                  "  position: 'Инженер', kind: 'оклад', contract: 'C_A', action: null,"
                  "  before: 40400, after: null},"
                  " {employee: 'E1', fio: 'Иванов Иван Иванович', person_code: '1001',"
                  "  position: 'Инженер', kind: 'оклад', contract: 'C_B', action: null,"
                  "  before: null, after: 40400}]}]};"
                  "console.log(ordersView());")
        got = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True,
                                        encoding="utf-8", check=True).stdout)
        self.assertEqual(got["heads"], ["Приказ", "Договор", "Наименование договора", "Месяц",
                                        "Вид выплаты", "Сотрудники", "Было в месяц, ₽",
                                        "Стало в месяц, ₽", ""])
        self.assertIn("▾", got["row"][0])
        self.assertIn('aria-expanded="true"', got["row"][0])
        self.assertEqual(got["row"][3:6], ["03.26", "оклад", 1])
        self.assertIn("kind=%D0%B8%D1%81%D1%82%D0%BE%D1%87%D0%BD%D0%B8%D0%BA", got["row"][8])
        self.assertNotIn("employee=", got["row"][8])
        first, second = got["sub"]
        self.assertIn("Иванов Иван Иванович", first[0])
        self.assertIn("employee=E1", first[8])
        self.assertEqual((first[1], second[1]), ("C_A", "C_B"))
        self.assertEqual((second[0], second[8]), ("", ""))
        self.assertIn("Иванов", got["text"])


if __name__ == "__main__":
    unittest.main()
