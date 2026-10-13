"""Правила замещения правятся в реестре (SOURCE-010).

Добавить, изменить и удалить правило можно на вкладке «Правила замещения».
Ручное правило — строка без документа; правка правила из документа отвязывает
его от документа; новый файл правил заменяет только правила из документов и не
пишет своё поверх ручного. Любая правка пишется в журнал и помечает
посчитанные планы к пересмотру: правила общие и уходят в каждый расчёт.

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


class SubstitutionEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lifecycle import App
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, FOT_OPERATOR="subst-test-operator")
        cls.env.start()
        cls.app = App(cls.temp.name)
        import intake
        import actuality
        cls.intake, cls.actuality = intake, actuality

    @classmethod
    def tearDownClass(cls):
        cls.app.db.close()
        cls.app.d.engine.dispose()
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.db, self.d, self.main = self.app.db, self.app.d, self.app.main
        for model in (self.d.Substitution, self.d.Document, self.d.Decision, self.d.Run):
            self.db.query(model).delete()
        self.db.commit()

    # ── помощники ──
    def rules(self):
        # Обработчики пишут своей сессией: без сброса сессия теста отдала
        # бы из своего кэша объект в состоянии до правки.
        self.db.expire_all()
        return {s.position: s for s in self.db.query(self.d.Substitution).all()}

    def rules_doc(self, pairs, name="Правила замещения.xlsx"):
        """Документ правил, разобранный так же, как при загрузке."""
        path = os.path.join(self.temp.name, name)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["должность", "может быть замещена"])
        for p, r in pairs:
            ws.append([p, r])
        wb.save(path)
        doc = self.d.Document(name=name, path=path, state="загружен", scope="реестр")
        self.db.add(doc)
        self.db.commit()
        self.intake.run_substitutions(self.db, self.app.case, doc)
        self.db.commit()
        return doc

    def add(self, position, replaced_by):
        return asyncio.run(self.main.add_substitution(
            Req({"position": position, "replaced_by": replaced_by})))

    # ── случаи ──
    def test_manual_rule_is_added_without_document(self):
        out = self.add("Инженер", ["Программист", "Лаборант"])
        rule = self.rules()["Инженер"]
        self.assertEqual(out["id"], rule.id)
        self.assertIsNone(rule.document_id)
        self.assertEqual(rule.source, "введено вручную")
        self.assertEqual(rule.replaced_by, "Лаборант, Программист")

    def test_empty_position_is_refused(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as caught:
            self.add("  ", ["Программист"])
        self.assertEqual(caught.exception.status_code, 400)

    def test_editing_document_rule_detaches_it(self):
        self.rules_doc([("Ведущий инженер", "Инженер")])
        rule = self.rules()["Ведущий инженер"]
        self.assertIsNotNone(rule.document_id)
        asyncio.run(self.main.edit_substitution(
            rule.id, Req({"replaced_by": ["Инженер", "Программист"]})))
        rule = self.rules()["Ведущий инженер"]
        self.assertIsNone(rule.document_id)
        self.assertEqual(rule.source, "введено вручную")
        self.assertEqual(rule.replaced_by, "Инженер, Программист")

    def test_delete_removes_rule(self):
        out = self.add("Лаборант", ["Инженер"])
        self.main.delete_substitution(out["id"])
        self.assertNotIn("Лаборант", self.rules())

    def test_new_rules_file_keeps_manual_rules(self):
        """Файл заменяет только правила из документов; ручное важнее файла.

        Отрицательный случай — прежнее поведение: новый файл стирал все
        правила, и ручная правка пропадала при следующей загрузке."""
        self.rules_doc([("Инженер", "Лаборант"), ("Директор", "Научный сотрудник")])
        self.add("Программист", ["Инженер"])
        manual = self.rules()["Инженер"]
        asyncio.run(self.main.edit_substitution(manual.id, Req({"replaced_by": ["Техник"]})))
        self.rules_doc([("Инженер", "Лаборант, Программист"), ("Аналитик", "Программист")],
                       name="Правила замещения v2.xlsx")
        rules = self.rules()
        self.assertEqual(rules["Инженер"].replaced_by, "Техник")          # ручное не перезаписано
        self.assertEqual(rules["Программист"].replaced_by, "Инженер")     # ручное осталось
        self.assertIn("Аналитик", rules)                                  # новое из файла
        self.assertNotIn("Директор", rules)                               # старое из файла ушло

    def test_change_marks_plans_for_review_and_is_logged(self):
        run = self.app.run(status="OPTIMAL")
        self.add("Инженер", ["Программист"])
        self.db.refresh(run)
        self.assertEqual(run.review, self.actuality.REVIEW)
        decisions = self.db.query(self.d.Decision).all()
        self.assertTrue(any("правило замещения" == d.subject_kind for d in decisions), decisions)

    def test_registry_payload_has_id_and_source(self):
        self.add("Инженер", ["Программист"])
        start = self.main.__file__
        text = Path(start).read_text(encoding="utf-8")
        self.assertIn('"substitutions": [{"id": s.id, "position": s.position', text)


class SubstitutionUiTests(unittest.TestCase):
    """Вкладка — как в прототипе, но без графы «сколько»."""

    def branch(self):
        start = JS.index('} else if (regTab === "sub") {')
        return JS[start:JS.index("} else if", start + 10)]

    def test_two_columns_without_count(self):
        body = self.branch()
        # Таблица на общем движке (grid); видимые графы — две. Названия по
        # смыслу правила (решение пользователя 18.09.2026): «может быть
        # замещена» читалось в обратную сторону. Источник — графа, скрытая
        # по умолчанию (off): её достают через «Выбрать графы».
        self.assertIn('regGrid("sub"', body)
        self.assertIn('label: "Должность работника", always: true', body)
        self.assertIn('label: "Допустимые должности для дополнительной ставки"', body)
        self.assertIn('label: "Источник", off: true', body)
        labels = [l for l in body.split('label: "')[1:]]
        self.assertFalse(any(l.lower().startswith("сколько") for l in labels))

    def test_add_edit_delete_are_reachable(self):
        self.assertIn('sub: "Добавить правило"', JS)
        start = JS.index("function openSubstMenu(")
        menu = JS[start:JS.index("\nfunction ", start + 1)]
        self.assertIn("Изменить набор должностей", menu)
        self.assertIn("Удалить правило", menu)
        start = JS.index("function substDialog(")
        dialog = JS[start:JS.index("\nfunction ", start + 1)]
        self.assertIn("<select", dialog)
        self.assertIn('type="checkbox"', dialog)
        self.assertIn("(в другом подразделении)", dialog)


if __name__ == "__main__":
    unittest.main()
