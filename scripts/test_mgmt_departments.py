"""Управленческие подразделения (владелец продукта 01.10.2026: «просто хотят
видеть, как у управленческих подразделений распределяются деньги»; вариант
дизайнера «как в 1С» — со справочником, у человека в двух подразделениях две
строки зарплаты со своими суммами, перевод с месяца, раздел 16 отчёта).

Стенд на временной базе (lifecycle.App), без решателя: раздел 16 проверяется
на маленьком входе и плане.
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
JS = (ROOT / "app/static/app.js").read_text(encoding="utf-8")


class MgmtDepartmentsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lifecycle import App
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, FOT_OPERATOR="mgmt-test-operator")
        cls.env.start()
        cls.app = App(cls.temp.name)

    @classmethod
    def tearDownClass(cls):
        cls.app.db.close()
        cls.app.d.engine.dispose()
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.db, self.d, self.main = self.app.db, self.app.d, self.app.main
        for model in (self.d.SalaryMonth, self.d.SalaryRow, self.d.MgmtDepartment,
                      self.d.Employee):
            self.db.query(model).delete()
        self.db.add(self.d.Employee(code="E1", fio="Иванов Иван Иванович", position="Инженер",
                                    rate=1.0, date_from="01.01.2026", date_to="31.12.2026"))
        self.db.commit()

    def call(self, fn, *args, body=None):
        from lifecycle import Req
        if body is None:
            return fn(*args)
        return asyncio.run(fn(*args, Req(body)))

    def dept(self, name):
        return self.call(self.main.add_mgmt_department, body={"name": name})["id"]

    def test_two_rows_sum_into_one_salary_and_transfer_splits(self):
        import salary
        a, b = self.dept("Финансово-экономический отдел"), self.dept("Обязательства")
        main_row = self.call(self.main.add_salary_row, body={"code": "E1", "year": 2026, "dept": a})["id"]
        extra = self.call(self.main.add_salary_row, body={"code": "E1", "year": 2026, "dept": b})["id"]
        for m in range(1, 13):
            salary.set_month(self.db, "E1", 2026, m, 100000, row_id=main_row)
        for m in range(3, 13):
            salary.set_month(self.db, "E1", 2026, m, 5000, row_id=extra)
        self.db.commit()
        totals = salary.month_totals(self.db, 2026)
        self.assertEqual(totals[("E1", 1)], 100000)
        self.assertEqual(totals[("E1", 3)], 105000)
        # Та же строка в том же подразделении — отказ.
        with self.assertRaises(Exception):
            self.call(self.main.add_salary_row, body={"code": "E1", "year": 2026, "dept": a})
        # Перевод основной строки в новое подразделение с июля.
        c = self.dept("Лаборатория 3")
        moved = self.call(self.main.set_salary_row_department, main_row,
                          body={"dept": c, "from": 7})["id"]
        self.db.expire_all()
        rows = {r.id: r for r in self.db.query(self.d.SalaryRow).all()}
        self.assertEqual(rows[main_row].mgmt_department_id, a)
        self.assertEqual(rows[moved].mgmt_department_id, c)
        months = sorted(m.month for m in self.db.query(self.d.SalaryMonth)
                        .filter_by(salary_row_id=moved).all())
        self.assertEqual(months, list(range(7, 13)))
        # Перевод в подразделение, где в тех же месяцах уже суммы, — отказ.
        with self.assertRaises(Exception):
            self.call(self.main.set_salary_row_department, main_row, body={"dept": b, "from": 3})
        # Удаление строки — одной, а не всех строк человека.
        self.call(self.main.delete_salary_row, extra)
        self.assertEqual(self.db.query(self.d.SalaryRow).filter_by(employee_code="E1").count(), 2)

    def test_report_splits_pay_by_rows_in_kopecks(self):
        import report
        path = os.path.join(self.temp.name, "in.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "управленческие_подразделения"
        ws.append(["код строки", "подразделение", "порядок", "месяц", "сумма"])
        ws.append(["E1", "Отдел", 1, 3, 100000])
        ws.append(["E1", "Обязательства", 2, 3, 5000])
        wb.save(path)
        plan = [{"emp": "E1", "month": 3, "amount": 105000.01},
                {"emp": "E2", "month": 3, "amount": 50000}]
        inp = {"employees": [{"code": "E1", "fio": "Иванов"}, {"code": "E2", "fio": "Петров"}]}
        got = report._by_department(path, plan, inp, {})
        names = [d["подразделение"] for d in got["подразделения"]]
        self.assertEqual(names, ["Отдел", "Обязательства", "Без подразделения"])
        march = [d["месяцы"][2] for d in got["подразделения"]]
        self.assertAlmostEqual(sum(march), 155000.01, places=2)
        self.assertEqual(got["итого"][2], 155000.01)

    def test_screens(self):
        self.assertIn('{ key: "mgmt", title: "Управленческие подразделения"', JS)
        self.assertIn('{ k: "dept", label: "Управленческое подразделение"', JS)
        self.assertIn("Добавить строку в другом подразделении…", JS)
        self.assertIn('16: "По подразделениям"', JS)
        self.assertIn("function repDepartments()", JS)


if __name__ == "__main__":
    unittest.main()
