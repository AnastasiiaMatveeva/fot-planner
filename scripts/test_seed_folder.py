"""Начальная загрузка документов из папки при старте (SOURCE-015).

Разбор идёт без плана: план не создаётся, лента и журнал работ не пишутся,
расхождения нормативного документа записываются в справочник сразу.
Стенд — временная база `lifecycle.App` и временная копия справочника; модель
не вызывается. Запускать отдельным процессом.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "src"))


class SeedFolderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lifecycle import App
        cls.temp = tempfile.TemporaryDirectory()
        cls.app = App(cls.temp.name)
        import reference
        cls.reference = reference
        assert not os.path.samefile(reference.TEMPLATE,
                                    ROOT / "docs" / "ui" / "demo_input.xlsx")

    @classmethod
    def tearDownClass(cls):
        cls.app.db.close()
        cls.app.d.engine.dispose()
        cls.temp.cleanup()

    def setUp(self):
        self.db, self.d, self.main = self.app.db, self.app.d, self.app.main
        for model in (self.d.Substitution, self.d.Document, self.d.Decision):
            self.db.query(model).delete()
        self.db.commit()
        self.folder = tempfile.mkdtemp(dir=self.temp.name)

    # ── помощники ──
    def book(self, rows, name):
        path = os.path.join(self.folder, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        wb = openpyxl.Workbook()
        for row in rows:
            wb.active.append(list(row))
        wb.save(path)
        return path

    def counts(self):
        self.db.expire_all()
        return {m.__name__: self.db.query(m).count()
                for m in (self.d.Case, self.d.Message, self.d.Activity, self.d.Question)}

    def reference_row(self, pos):
        wb = openpyxl.load_workbook(self.reference.TEMPLATE)
        ws = wb[self.reference.SHEET]
        for r in range(2, ws.max_row + 1):
            if ws.cell(r, 1).value == pos:
                return [ws.cell(r, c).value for c in range(1, 10)]
        return None

    # ── случаи ──
    def test_folder_is_loaded_without_a_plan(self):
        self.book([("Ведущий эксперт", "НР"), ("Аналитик", "НТП"), ("Техник", "НТП")],
                  "1 Должности/перечень.xlsx")
        self.book([("Исходная должность", "Должность, которая может быть"),
                   ("Ведущий инженер", "Инженер 2 кат., Инженер")],
                  "1 Должности/замещение.xlsx")
        before = self.counts()
        added = self.main._load_seed(self.folder)
        self.assertEqual(len(added), 2)
        self.db.expire_all()
        docs = self.db.query(self.d.Document).all()
        self.assertTrue(all(d.case_id is None and d.state == "разобран" for d in docs),
                        [(d.name, d.case_id, d.state) for d in docs])
        self.assertEqual(self.reference_row("Ведущий эксперт")[1], "НР")
        self.assertEqual(self.db.query(self.d.Substitution).count(), 1)
        # Ни плана, ни ленты, ни журнала работ, ни вопросов.
        self.assertEqual(self.counts(), before)

    def test_second_start_loads_nothing(self):
        self.book([("Аналитик", "НТП"), ("Техник", "НТП"), ("Директор", "ППС")], "п.xlsx")
        self.assertEqual(len(self.main._load_seed(self.folder)), 1)
        self.assertEqual(self.main._load_seed(self.folder), [])

    def test_positions_list_goes_before_other_files(self):
        norms = self.book([("Должность", "Оклад"), ("Аналитик", 50000)], "1 Приказ.xlsx")
        listed = self.book([("Аналитик", "НТП"), ("Техник", "НТП"), ("Директор", "ППС")],
                           "9 Перечень.xlsx")
        self.assertEqual(self.main._seed_files(self.folder), [listed, norms])

    def test_norms_differences_are_written_without_confirmation(self):
        old = self.reference_row("Аналитик")[5]
        path = self.book([("Должность", "Оклад"), ("Аналитик", old + 1000)], "Положение.xlsx")
        change = {"pos": "Аналитик", "field": "оклад", "old": old, "new": old + 1000,
                  "from": None}

        def handle(db, case, doc):
            # Разбор нормативного документа без модели: тот же результат,
            # что оставляет run_norms, — расхождение в gave.
            self.assertIsNone(case.id)
            doc.kind, doc.state = "нормативный документ", "разобран"
            doc.gave = json.dumps({"расхождения": [change], "расхождений": 1},
                                  ensure_ascii=False)
            db.commit()

        with patch.object(self.main.intake, "handle_document", handle):
            self.main._load_seed(self.folder)
        self.assertEqual(self.reference_row("Аналитик")[5], old + 1000)
        self.db.expire_all()
        doc = self.db.query(self.d.Document).filter_by(name=os.path.basename(path)).one()
        self.assertEqual(doc.summary, "расхождений 0")
        decision = self.db.query(self.d.Decision).one()
        self.assertEqual(decision.grounds, "начальная загрузка")
        self.assertEqual(self.reference.read_sources()["оклад"]["документ"], "Положение.xlsx")

    def test_folder_is_read_only_at_server_start(self):
        # Импорт модуля (стенды, скрипты) папку не разбирает — только старт
        # сервера через lifespan.
        from fastapi.testclient import TestClient
        with patch.object(self.main, "_load_seed") as load:
            with TestClient(self.main.app):
                pass
            # Разбор идёт своим потоком: старт сервера его не ждёт.
            for _ in range(100):
                if load.called:
                    break
                time.sleep(0.05)
        load.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
