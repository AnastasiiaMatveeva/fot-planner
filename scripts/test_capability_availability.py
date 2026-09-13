# -*- coding: utf-8 -*-
"""CAP-002 / CAP-003: доступность отдельно от зрелости; причина и действие.

Разбор документа моделью — описанные процедуры каталога с ресурсом «llm».
Модель не настроена: способность недоступна с причиной и требуемым действием,
сервис не спрашивает вид документа и не изображает разбор; книга Excel по
нормам разбирается чистым инструментом без модели и говорит об этом.
Панель ролей отличает «не реализован» от «сейчас недоступен».

    .venv/Scripts/python.exe scripts/test_capability_availability.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "harness"))

MODEL_KEYS = ("FOT_LLM_PROVIDER", "FOT_LLM_BASE_URL", "FOT_LLM_MODEL", "FOT_LLM_API_KEY",
              "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY")


def without_model():
    env = {k: v for k, v in os.environ.items() if k not in MODEL_KEYS}
    return patch.dict(os.environ, env, clear=True)


def with_model():
    return patch.dict(os.environ, FOT_LLM_PROVIDER="local", FOT_LLM_BASE_URL="http://127.0.0.1:9",
                      FOT_LLM_MODEL="test-model")


class CatalogueAvailabilityTests(unittest.TestCase):
    def setUp(self):
        from fot_planner.harness_local.catalogue import Authority, Catalogue, Task
        self.catalogue = Catalogue.load(ROOT)
        self.Authority, self.Task = Authority, Task
        self.path = Path(tempfile.mkdtemp()) / "doc.txt"
        self.path.write_text("текст", encoding="utf-8")
        self.addCleanup(shutil.rmtree, self.path.parent, True)

    def authority(self, llm):
        return self.Authority(permissions=frozenset({"document.read", "model.call"}),
                              resources=frozenset({"python", "openpyxl"} | ({"llm"} if llm else set())),
                              files=(self.path,))

    def test_model_procedures_are_described_and_need_llm_resource(self):
        for task, cap, handler in (("document.freeform_extract", "freeform-extract", "llm.freeform"),
                                   ("document.norms_extract", "norms-extract", "llm.parse")):
            ready = self.catalogue.select(self.Task(kind=task, domain="fot.local"), self.authority(True))
            self.assertEqual((ready.status, ready.capability.id), ("ready", cap))
            tool = self.catalogue.resolve(ready.tool.id, ready.tool.version)
            self.assertEqual(tool.handler, handler)
            self.assertIn("llm", tool.resources)
            missing = self.catalogue.select(self.Task(kind=task, domain="fot.local"), self.authority(False))
            self.assertEqual(missing.status, "unavailable")
            self.assertIn("llm", missing.reason)
            self.assertIn("восстановить", missing.action)
            self.assertIn("новая способность не требуется", missing.action)

    def test_availability_is_separate_from_maturity(self):
        now = self.catalogue.availability({"python", "openpyxl"})
        self.assertFalse(now["intake"]["available"])
        self.assertIn("llm", now["intake"]["why"])
        self.assertFalse(now["norms"]["available"])
        self.assertFalse(now["scenario"]["available"])
        self.assertIn("не реализована", now["scenario"]["why"])
        later = self.catalogue.availability({"python", "openpyxl", "llm"})
        self.assertTrue(later["intake"]["available"] and later["norms"]["available"])
        self.assertFalse(later["scenario"]["available"])
        self.assertEqual(self.catalogue.agent_view()["intake"]["real"], True)

    def test_model_handler_contract_cannot_be_weakened(self):
        from fot_planner.harness_local.catalogue import Catalogue, CatalogueError
        rows = [d.model_dump(mode="json") for d in self.catalogue.descriptors]
        for changes in ({"resources": ["python"]}, {"permissions": ["document.read"]},
                        {"effects": "none"}, {"handler": "os.system"}):
            edited = [dict(r) for r in rows]
            next(r for r in edited if r["id"] == "freeform-extract-tool").update(changes)
            with self.assertRaises(CatalogueError, msg=str(changes)):
                Catalogue.from_documents(edited)


class ServiceAvailabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lifecycle import App
        cls.temp = tempfile.TemporaryDirectory()
        ref = os.path.join(cls.temp.name, "demo_input.xlsx")
        shutil.copyfile(os.path.join(ROOT, "docs", "ui", "demo_input.xlsx"), ref)
        cls.env = patch.dict(os.environ, FOT_OPERATOR="Матвеева А.И.", FOT_REFERENCE_TEMPLATE=ref)
        cls.env.start()
        cls.app = App(cls.temp.name)
        import agents
        import intake
        import llm
        cls.agents, cls.intake, cls.llm = agents, intake, llm

    @classmethod
    def tearDownClass(cls):
        cls.app.db.close()
        cls.app.d.engine.dispose()
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.db, self.d = self.app.db, self.app.d
        for model in (self.d.Proposal, self.d.Question, self.d.Document, self.d.Message, self.d.Activity):
            self.db.query(model).delete()
        self.db.commit()

    def document(self, name, text, kind=None):
        path = Path(self.temp.name) / (self._testMethodName + "-" + name)
        if name.endswith(".xlsx"):
            import openpyxl
            wb = openpyxl.Workbook()
            wb.active["A1"] = text
            wb.save(path)
            wb.close()
        else:
            path.write_text(text, encoding="utf-8")
        doc = self.d.Document(name=name, path=str(path), size=path.stat().st_size,
                              sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                              kind=kind, state="ожидает", case_id=self.app.case.id, scope="реестр")
        self.db.add(doc)
        self.db.commit()
        return doc

    def messages(self):
        return [m for m in self.db.query(self.d.Message).order_by(self.d.Message.id).all()]

    def test_missing_model_gives_reason_and_action_instead_of_question(self):
        doc = self.document("письмо.txt", "текст без формы", kind="иное")
        with without_model(), patch.object(self.intake, "classify",
                                           return_value=("иное", None, "разбор по заголовкам", None)):
            self.intake.handle_document(self.db, self.app.case, doc)
        self.db.expire_all()
        self.assertEqual(self.db.query(self.d.Question).count(), 0, "спрошен вид вместо причины")
        self.assertEqual(self.db.query(self.d.Proposal).count(), 0)
        doc = self.db.get(self.d.Document, doc.id)
        self.assertEqual(doc.state, "не распознан")
        self.assertIn("модель недоступна", doc.summary)
        texts = [m.text for m in self.messages()]
        told = [t for t in texts if "способность недоступна" in t]
        self.assertEqual(len(told), 1, texts)
        self.assertIn("FOT_LLM_BASE_URL", told[0])
        self.assertIn("новая способность для этого не нужна", told[0])
        payloads = [m.payload for m in self.messages() if m.payload and "capability_missing" in m.payload]
        self.assertEqual(len(payloads), 1)

    def test_available_model_goes_through_catalogue_with_hints(self):
        doc = self.document("договор.txt", "текст", kind="документ по договору")
        seen = {}

        def fake(path, filename="", max_chunks=8, hints=None):
            seen["path"], seen["hints"] = str(path), list(hints or [])
            return {"ok": True, "model": "local test-model", "parts": 1,
                    "contracts": [{"code": "C-1", "name": "Д1", "fot": 100}]}
        catalogue = self.agents.default_catalogue()
        with with_model(), patch.object(self.llm, "freeform", side_effect=fake), \
                patch.object(catalogue, "invoke", wraps=catalogue.invoke) as invoked:
            found = self.intake.propose_entities(self.db, self.app.case, doc)
        self.assertTrue(found)
        self.assertEqual(invoked.call_count, 1)
        self.assertEqual(seen["path"], doc.path)
        self.assertEqual(self.db.query(self.d.Proposal).filter_by(document_id=doc.id).count(), 1)

    def test_norms_without_model_uses_anchor_parser_for_workbook_and_says_so(self):
        doc = self.document("приказ.xlsx", "Приказ", kind="нормативный документ")
        with without_model():
            self.intake.run_norms(self.db, self.app.case, doc)
        self.db.expire_all()
        self.assertEqual(self.db.get(self.d.Document, doc.id).parsed_by, "разбор по заголовкам")
        texts = [m.text for m in self.messages()]
        self.assertTrue(any("Модель недоступна" in t and "по заголовкам" in t for t in texts), texts)

    def test_norms_without_model_for_text_explains_required_action(self):
        doc = self.document("приказ.txt", "Приказ", kind="нормативный документ")
        with without_model(), self.assertRaises(ValueError) as ctx:
            self.intake.run_norms(self.db, self.app.case, doc)
        self.assertIn("недоступна", str(ctx.exception))
        self.assertIn("Что сделать", str(ctx.exception))

    def test_agent_list_reports_availability_separately_from_real(self):
        with without_model():
            rows = {r["key"]: r for r in self.agents.agent_list()}
        self.assertTrue(rows["intake"]["real"])
        self.assertFalse(rows["intake"]["available"])
        self.assertIn("модель", rows["intake"]["why"])
        self.assertFalse(rows["scenario"]["real"])
        with with_model():
            rows = {r["key"]: r for r in self.agents.agent_list()}
        self.assertTrue(rows["intake"]["available"] and rows["norms"]["available"])
        self.assertEqual(rows["intake"]["why"], "")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
