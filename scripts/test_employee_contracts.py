"""Договоры сотрудника одним списком (пользователь 30.09.2026: «может не
вводить две колонки? а просто сделать список с возможностью выбора? и сделать
выбрать все — чтоб можно было выбрать все и снять только галочку с тех,
которые не нужны, и также наоборот выбрать только те, что нужны»).

Графа «Разрешённые договоры» штатного расписания вместо «Разрешены» и
«Запрещены»; окно — список договоров с поиском и «Все договоры»; хранится
одно из двух, что короче.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "harness"))
JS = (ROOT / "app/static/app.js").read_text(encoding="utf-8")


class EmployeeContractsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lifecycle import App
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, FOT_OPERATOR="emp-contracts-operator")
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
        for model in (self.d.Employee, self.d.Contract):
            self.db.query(model).delete()
        self.db.add(self.d.Employee(code="E1", fio="Иванов Иван Иванович", position="Инженер",
                                    rate=1.0, allowed_contracts="C_A", forbidden_contracts=None))
        for code in ("C_A", "C_B", "C_C", "C_D"):
            self.db.add(self.d.Contract(code=code, name="Договор " + code))
        self.db.commit()

    def post(self, checked):
        from lifecycle import Req
        return asyncio.run(self.main.employee_contracts("E1", Req({"checked": checked})))

    def row(self):
        self.db.expire_all()
        return self.db.query(self.d.Employee).filter_by(code="E1").one()

    def test_shorter_list_is_kept(self):
        """Отмечено меньше половины — разрешены отмеченные; половина и больше —
        запрещены неотмеченные; все — без ограничений."""
        self.assertEqual(self.main.contract_choice(["C_A"], ["C_A", "C_B", "C_C", "C_D"]),
                         ("C_A", None))
        self.assertEqual(self.main.contract_choice(["C_A", "C_B"], ["C_A", "C_B", "C_C", "C_D"]),
                         (None, "C_C; C_D"))
        self.assertEqual(self.main.contract_choice(["C_A", "C_B", "C_C", "C_D"],
                                                   ["C_A", "C_B", "C_C", "C_D"]), (None, None))
        with self.assertRaises(Exception):
            self.main.contract_choice([], ["C_A", "C_B"])

    def test_all_but_one_is_stored_as_forbidden(self):
        got = self.post(["C_A", "C_B", "C_C"])
        self.assertEqual((got["allowed"], got["forbidden"]), (None, "C_D"))
        e = self.row()
        self.assertEqual((e.allowed_contracts, e.forbidden_contracts), (None, "C_D"))
        self.post(["C_B"])
        e = self.row()
        self.assertEqual((e.allowed_contracts, e.forbidden_contracts), ("C_B", None))

    def test_registry_grid_has_one_column_with_checklist(self):
        emp = JS[JS.index('body = regGrid("emp", ['):][:6000]
        self.assertNotIn('label: "Разрешены"', emp)
        self.assertNotIn('label: "Запрещены"', emp)
        self.assertIn('label: "Разрешённые договоры"', emp)
        self.assertIn("data-empctr", emp)
        dialog = JS[JS.index("function empContractsDialog("):][:5000]
        self.assertIn("Все договоры", dialog)
        self.assertIn("indeterminate", dialog)
        self.assertIn('"/api/employees/" + encodeURIComponent(code) + "/contracts"', dialog)


if __name__ == "__main__":
    unittest.main()
