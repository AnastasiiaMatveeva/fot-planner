"""Перечень должностей заводит справочник (SOURCE-013); «кат.» в названии
должности читается как «категории» (SOURCE-014).

Стенд — временная база `lifecycle.App` и временная копия справочника.
Запускать отдельным процессом: модуль базы фиксирует путь при импорте.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "src"))


class PositionsListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lifecycle import App
        cls.temp = tempfile.TemporaryDirectory()
        cls.app = App(cls.temp.name)
        import intake
        import reference
        cls.intake, cls.reference = intake, reference
        # Справочник обязан быть временной копией: разбор перечня его пишет.
        assert not os.path.samefile(reference.TEMPLATE,
                                    ROOT / "docs" / "ui" / "demo_input.xlsx")

    @classmethod
    def tearDownClass(cls):
        cls.app.db.close()
        cls.app.d.engine.dispose()
        cls.temp.cleanup()

    def setUp(self):
        self.db, self.d = self.app.db, self.app.d
        for model in (self.d.Substitution, self.d.Document):
            self.db.query(model).delete()
        self.db.commit()

    # ── помощники ──
    def book(self, rows, name):
        path = os.path.join(self.temp.name, name)
        wb = openpyxl.Workbook()
        for row in rows:
            wb.active.append(list(row))
        wb.save(path)
        return path

    def document(self, path):
        doc = self.d.Document(name=os.path.basename(path), path=path, state="загружен",
                              scope="реестр")
        self.db.add(doc)
        self.db.commit()
        return doc

    def categories(self):
        wb = openpyxl.load_workbook(self.reference.TEMPLATE)
        ws = wb[self.reference.SHEET]
        return {ws.cell(r, 1).value: ws.cell(r, 2).value
                for r in range(2, ws.max_row + 1) if ws.cell(r, 1).value}

    # ── SOURCE-014 ──
    def test_abbreviated_category_is_the_same_position(self):
        from fot_planner.position_reference import normalize_position as n
        self.assertEqual(n("Инженер 2 кат."), n("Инженер 2 категории"))
        self.assertEqual(n("инженер 1 кат"), n("Инженер 1 категории"))
        # «кат» внутри слова — не сокращение.
        self.assertEqual(n("Каталогизатор"), "каталогизатор")

    def test_substitution_with_abbreviation_is_matched(self):
        path = self.book([("Исходная должность", "Должность, которая может быть"),
                          ("Ведущий инженер", "Инженер 2 кат., Инженер 1 кат., Инженер")],
                         "замещение.xlsx")
        doc = self.document(path)
        self.intake.run_substitutions(self.db, self.app.case, doc)
        self.db.commit()
        self.assertEqual(self.intake._unknown_positions(self.db, doc.id), [])

    # ── SOURCE-013 ──
    def test_list_without_header_is_recognised_by_form(self):
        path = self.book([("Аналитик", "НТП"), ("Директор", "ППС"),
                          ("Научный сотрудник", "НР"), ("Редактор", "ПП")], "перечень.xlsx")
        kind, owner, by, _ = self.intake.classify(path, "перечень.xlsx")
        self.assertEqual((kind, owner), ("перечень должностей", "positions"))
        self.assertEqual(by, "разбор по графам перечня")

    def test_other_two_column_tables_are_not_a_list(self):
        subst = self.book([("Исходная должность", "Должность, которая может быть"),
                           ("Инженер", "Программист, Лаборант"),
                           ("Программист", "Инженер"), ("Лаборант", "Лаборант")], "з.xlsx")
        self.assertIsNone(self.intake.positions_layout(subst))
        mixed = self.book([("Аналитик", "НТП"), ("Директор", "ППС"), ("Техник", "НТП"),
                           ("Итого", 3), ("Примечание", "см. приказ")], "смесь.xlsx")
        self.assertIsNone(self.intake.positions_layout(mixed))

    def test_list_adds_missing_fixes_category_and_keeps_the_rest(self):
        before = self.categories()
        self.assertEqual(before["Редактор"], "ПП")
        self.assertNotIn("Ведущий эксперт", before)
        path = self.book([("Должность", "Категория"), ("Аналитик", "НТП"),
                          ("Редактор", "НТП"), ("Ведущий эксперт", "НР")], "перечень.xlsx")
        doc = self.document(path)
        self.intake.run_positions(self.db, self.app.case, doc)
        self.db.expire_all()
        after = self.categories()
        self.assertEqual(after["Ведущий эксперт"], "НР")
        self.assertEqual(after["Редактор"], "НТП")
        # Должности вне перечня остаются со своими величинами.
        self.assertEqual(after["Директор"], before["Директор"])
        self.assertEqual(len(after), len(before) + 1)
        doc = self.db.get(self.d.Document, doc.id)
        self.assertEqual(doc.state, "разобран")
        self.assertEqual(doc.summary, "должностей 3: заведено 1, категория изменена у 1")


if __name__ == "__main__":
    unittest.main()
