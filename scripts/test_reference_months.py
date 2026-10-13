"""Справочник должностей по датам (SOURCE-012; владелец продукта 01.10.2026:
«не всегда эти приказы будут выходить в одно и то же время. Нужно иметь
возможность подгрузить и чтоб программа поняла по датам»).

Справочник — копия во временной папке: запись в историю не трогает файл
репозитория. Проверяется вся цепочка: запись с даты, величины по месяцам,
лист входа «лимиты_по_месяцам», величина месяца у решателя и у проверок.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "app"))


class ReferenceMonthsTests(unittest.TestCase):
    def setUp(self):
        import reference
        self.reference = reference
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "reference.xlsx")
        shutil.copyfile(ROOT / "docs" / "ui" / "demo_input.xlsx", self.path)
        self.was = reference.TEMPLATE
        reference.TEMPLATE = self.path
        reference._INDEX["for"] = None

    def tearDown(self):
        self.reference.TEMPLATE = self.was
        self.reference._INDEX["for"] = None
        self.temp.cleanup()

    def salary(self):
        return [r["sal"] for r in self.reference.read_rows() if r["pos"] == "Ведущий инженер"][0]

    def test_value_from_date_keeps_the_old_one_before_it(self):
        R = self.reference
        was = self.salary()
        got = R.apply([{"pos": "Ведущий инженер", "field": "оклад", "value": was + 2000}],
                      effective="01.09.2026")
        self.assertEqual(got[0]["from"], "01.09.2026")
        months = R.month_values(2026)[("Ведущий инженер", "оклад")]
        self.assertEqual(months[8], was)
        self.assertEqual(months[9], was + 2000)
        # Сверка документа — с величиной на его дату, а не с сегодняшней.
        rows = [{"pos": "Ведущий инженер", "field": "оклад", "value": was + 2000}]
        self.assertEqual(R.compare(rows, "01.09.2026")[0], [])
        self.assertEqual(len(R.compare(rows, "01.03.2026")[0]), 1)
        # Запись без даты — величина на все даты, история снимается.
        R.apply([{"pos": "Ведущий инженер", "field": "оклад", "value": was}])
        self.assertNotIn(("Ведущий инженер", "оклад"), R.month_values(2026))

    def test_input_gets_january_value_and_monthly_sheet(self):
        import build_input
        R = self.reference
        was = self.salary()
        R.apply([{"pos": "Ведущий инженер", "field": "оклад", "value": was + 2000}],
                effective="01.09.2026")
        R.apply([{"pos": "Ведущий инженер", "field": "П2556", "value": 150000}],
                effective="01.12.2025")
        wb = openpyxl.load_workbook(self.path)
        warn = []
        build_input.fill_reference_months(wb, 2026, warn)
        rows = list(wb["лимиты_по_месяцам"].iter_rows(values_only=True))
        self.assertEqual(rows[0], ("должность", "месяц", "оклад", "П2556", "П4", "БЭП"))
        self.assertEqual([(r[0], r[1], r[2]) for r in rows[1:]],
                         [("Ведущий инженер", m, was + 2000) for m in range(9, 13)])
        ws = wb["лимиты_по_должностям"]
        row = [r for r in ws.iter_rows(min_row=2, values_only=True) if r[0] == "Ведущий инженер"][0]
        # Январь года плана: старый оклад и П2556, вступивший в декабре 2025.
        self.assertEqual((row[5], row[6]), (was, 150000))
        self.assertTrue(any("с сентября" in w for w in warn))

    def test_solver_and_checks_take_the_month_value(self):
        from fot_planner.payment_split import employee_reference_salary_cap
        from fot_planner.salary_limits_2556 import PositionLimit
        from fot_planner import payroll_rules
        import rules
        e = types.SimpleNamespace(reference_salary_for_rate=47000.0, rate=0.5,
                                  reference_salary_by_month={9: 49000.0})
        self.assertEqual(employee_reference_salary_cap(e, 8), 23500.0)
        self.assertEqual(employee_reference_salary_cap(e, 9), 24500.0)
        limit = PositionLimit("2556", "Ведущий инженер", limit=140000.0,
                              limit_by_month={12: 150000.0})
        self.assertEqual(limit.limit_in(11), 140000.0)
        self.assertEqual(payroll_rules._limit_in(limit, 12), 150000.0)
        lim = {"ведущий инженер": {"оклад": 47000.0, "_months": {9: {"оклад": 49000.0}}}}
        norm = lambda t: t.lower()  # noqa: E731
        self.assertEqual(rules.lim_at(lim, norm, "Ведущий инженер", "оклад", 8), 47000.0)
        self.assertEqual(rules.lim_at(lim, norm, "Ведущий инженер", "оклад", 9), 49000.0)


class ReferenceOnDateUiTests(unittest.TestCase):
    def test_table_on_date_with_history_rows(self):
        """Вид справочника на дату (вариант дизайнера, 01.10.2026): поле «На
        дату» в шапке панели, величины на дату, ▸ раскрывает историю."""
        js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        ref = js[js.index('} else if (regTab === "ref") {'):][:4000]
        self.assertIn("refRowOn(x, refHist, refDay)", ref)
        self.assertIn("data-refon", ref)
        self.assertIn("data-refopen", ref)
        self.assertIn('if (r.screen === "registry" && r.on) q.push("on=" + r.on);', js)
        self.assertIn('"reference_history": reference.read_history()',
                      (ROOT / "app/main.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
