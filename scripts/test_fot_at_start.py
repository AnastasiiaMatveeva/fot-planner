"""Весь ФОТ договора — с начала договора; поступлений в расчёте нет.

Правило 21 «Касса» (`docs/ПРАВИЛА_РЕШАТЕЛЯ.md`) и SOURCE-009
(`docs/ИСТОЧНИКИ.md`), решение пользователя 18.09.2026. До этого сервис
раскладывал фонд договора без графика поступлений ровно по месяцам как деньги
на счёт — и к марту нельзя было потратить больше 3/12 фонда, хотя договор
весь фонд уже дал. Теперь в файл входа поступления не пишутся, и решатель сам
считает весь ФОТ поступившим в первый месяц выплат договора.

Проверяется сквозь настоящий путь: сборка входа из данных реестра, в которых
поступления есть, → загрузчик решателя → касса договора по месяцам.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "src"))

import build_input  # noqa: E402
import reference  # noqa: E402
from fot_planner.excel.load import load_context  # noqa: E402

JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
BASIS = (ROOT / "app" / "basis.py").read_text(encoding="utf-8")
YEAR = 2026


def contract(code, fund, date_from, date_to):
    """Договор так, как его отдаёт реестр: графы, которые читает сборка входа."""
    return NS(code=code, name=code, number="", kind="", account="", goz=None,
              date_from=date_from, date_to=date_to, fund=fund, kinds="",
              priority=None, allow_main=None, allow_part_time=None,
              salary_deadline=None, allowance_deadline=None, department=None)


def inflow(code, month, amount):
    return NS(contract_code=code, year=YEAR, month=month, amount=amount)


class FundAtStartTests(unittest.TestCase):
    """Сборка входа и загрузчик решателя: касса договора с его начала."""

    @classmethod
    def setUpClass(cls):
        # Поступления в данных есть и нарочно неровные: если они просочатся
        # в расчёт, касса по месяцам это покажет.
        data = {
            "year": YEAR,
            "contracts": [contract("C_MAR", 1_200_000, "01.03.2026", "31.12.2026"),
                          contract("C_YEAR", 600_000, "01.01.2026", "31.12.2026")],
            "inflows": [inflow("C_MAR", 3, 100_000), inflow("C_MAR", 9, 1_100_000),
                        inflow("C_YEAR", 1, 50_000), inflow("C_YEAR", 6, 550_000)],
        }
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.tmp.name, "input.xlsx")
        cls.warn = []
        build_input.build(reference.TEMPLATE, cls.path, data, cls.warn)
        cls.ctx = load_context(cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def cash(self, code):
        found = [c for c in self.ctx.contracts if c.id == code]
        self.assertTrue(found, code)
        return {mb.month: mb.inflow_amount for mb in found[0].monthly_budgets
                if mb.year == YEAR}

    def test_input_has_no_monthly_inflows(self):
        """Лист поступлений в файл входа не пишется — ни сам, ни его фиксации."""
        import openpyxl
        wb = openpyxl.load_workbook(self.path, read_only=True)
        names = wb.sheetnames
        wb.close()  # иначе Windows не даст убрать временный каталог
        self.assertNotIn("фот_по_месяцам", names)
        self.assertNotIn("фиксация_фот_по_месяцам", names)

    def test_whole_fund_arrives_in_first_payment_month(self):
        """Весь фонд — в первый месяц выплат, в остальных поступлений нет.

        Отрицательный случай — прежнее поведение: 100 000 в марте и 1 100 000
        в сентябре по графику, или ровно по 120 000 в месяц без него.
        """
        cash = self.cash("C_MAR")
        self.assertEqual(cash.get(3), 1_200_000)
        self.assertEqual(sum(cash.values()), 1_200_000)
        self.assertEqual([m for m, v in cash.items() if v and m != 3], [])

    def test_nothing_before_contract_start(self):
        """Потратить деньги раньше начала договора нельзя: до марта кассы нет."""
        cash = self.cash("C_MAR")
        for month in (1, 2):
            self.assertFalse(cash.get(month), month)

    def test_year_contract_gets_fund_in_january(self):
        cash = self.cash("C_YEAR")
        self.assertEqual(cash.get(1), 600_000)
        self.assertEqual(sum(cash.values()), 600_000)

    def test_no_even_split_assumption_is_announced(self):
        """Ровной раскладки как денег на счёт больше нет — и допущения о ней."""
        self.assertFalse([w for w in self.warn if "поступлени" in w.lower()], self.warn)


class NoCashAnywhereTests(unittest.TestCase):
    """Кассы как отдельной величины договора больше нет ни в пакете, ни на экране."""

    def test_package_has_no_cash_role(self):
        self.assertNotIn('"key": "cash"', BASIS)

    def test_contract_properties_have_no_cash(self):
        start = JS.index("function requisitesBlock")
        body = JS[start:JS.index("\nfunction ", start + 10)]
        self.assertNotIn('fromPkg("Касса"', body)

    def test_registry_has_no_inflows_tab(self):
        tabs = JS[JS.index("var REG_TABS"):][:900]
        self.assertNotIn('key: "inflow"', tabs)


if __name__ == "__main__":
    unittest.main()
