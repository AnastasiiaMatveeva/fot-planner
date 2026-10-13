"""Пределы по приказам в результате расчёта: таблица по приказу № 2556.

Пользователь 30.09.2026: «в результатах там должна быть таблица по 2556».
Раздел 9 — «Соблюдение уровней оплаты труда по приказам № 2556 и № 4»
(формальное название — решение владельца того же дня «нужно жёстко
формализовать»): 9.1 — оклад и 122 не выше предела должности на открытую
ставку (правило 4), 9.2 — П4, 9.3 — выплата приказом. Строки 9.1 те же, что
у проверки «П2556: оклад и 122 не выше предела по должности».
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "src"))

import report  # noqa: E402

JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
ISSUES = (ROOT / "app" / "static" / "issues.js").read_text(encoding="utf-8")
EXPORT = (ROOT / "app" / "export_report.py").read_text(encoding="utf-8")


def row(emp, month, contract, kind, amount, position="Инженер", fio="Иванов И. И."):
    return {"emp": emp, "fio": fio, "position": position, "contract": contract,
            "month": month, "kind": kind, "amount": amount}


class Limit2556Tests(unittest.TestCase):
    LIM = {"инженер": {"оклад": 50000.0, "П2556": 70000.0}}

    def rows(self, plan):
        return report.limit_2556(plan, self.LIM, lambda s: str(s).strip().lower(),
                                 {"E1": {"табельный": "1001"}})

    def test_row_per_employee_month_and_contract(self):
        # Полставки: оклад 25 000 из 50 000 — предел 35 000; оклад и 122 —
        # 33 000, запас 2 000. Должность и П2556 на 1,0 ставки — в строке
        # (30.09.2026): без них предел на ставку не с чем было сверить.
        got = self.rows([row("E1", 3, "C_A", "оклад", 25000.0), row("E1", 3, "C_A", "122", 8000.0)])
        self.assertEqual(len(got), 1)
        r = got[0]
        self.assertEqual((r["табельный"], r["договор"], r["месяц"], r["должность"]),
                         ("1001", "C_A", 3, "Инженер"))
        self.assertEqual((r["оклад"], r["122"], r["итого"], r["ставка"], r["П2556"], r["предел"],
                          r["запас"]), (25000.0, 8000.0, 33000.0, 0.5, 70000.0, 35000.0, 2000.0))
        # Запас в рублях и в доле предела — одним знаком: плюс — ниже предела.
        # Прежде «запас» был положительным, а «отклонение» того же случая —
        # отрицательным.
        self.assertGreater(r["запас %"], 0)

    def test_excess_is_a_negative_reserve(self):
        got = self.rows([row("E1", 1, "C_A", "оклад", 50000.0), row("E1", 1, "C_A", "122", 30000.0)])
        self.assertEqual(got[0]["запас"], -10000.0)
        self.assertLess(got[0]["запас %"], 0)

    def test_open_rate_comes_from_the_solver(self):
        """Ставка — та, что открыл решатель (rules.open_rates), если она дана."""
        plan = [row("E1", 1, "C_A", "оклад", 50000.0), row("E1", 1, "C_A", "122", 30000.0)]
        got = report.limit_2556(plan, self.LIM, lambda s: str(s).strip().lower(),
                                {"E1": {"табельный": "1001"}}, rate_of=lambda rr: 1.25)
        self.assertEqual((got[0]["ставка"], got[0]["предел"]), (1.25, 87500.0))

    def test_position_without_limit_is_not_listed(self):
        got = self.rows([row("E1", 1, "C_A", "оклад", 50000.0, position="Лаборант")])
        self.assertEqual(got, [])

    def test_screen_export_and_issues_show_the_table(self):
        lim = JS[JS.index("function repLimits()"):JS.index("\nfunction ", JS.index("function repLimits()") + 1)]
        # Названия формальные (владелец продукта 30.09.2026), номера
        # подразделов прежние: по ним владелец называет таблицы.
        self.assertIn('secH(9, "Соблюдение уровней оплаты труда по приказам № 2556 и № 4"', lim)
        self.assertIn('subH("9.1", "Приказ № 2556: оклад и надбавка 122 в сравнении с предельным уровнем"', lim)
        self.assertIn('subH("9.2", "Приказ № 4: штатная часть в месяцы надбавки 124 в сравнении с уровнем П4"', lim)
        self.assertIn('subH("9.3", "Приказ № 2556: стимулирующая выплата приказом"', lim)
        self.assertIn('var p25 = vrep["п2556"] || []', lim)
        self.assertIn("'П2556: оклад и 122 не выше предела по должности':9,", ISSUES)
        self.assertIn('"9.1": "9.1 Приказ 2556 оклад и 122"', EXPORT)
        self.assertIn('"9.2": "9.2 Приказ 4 уровень П4"', EXPORT)
        self.assertIn('"9.3": "9.3 Выплата приказом П2556"', EXPORT)
        self.assertIn('_sheet(wb, SHEETS["9.1"]', EXPORT)


if __name__ == "__main__":
    unittest.main()
