# -*- coding: utf-8 -*-
"""MEM-003 / VER-002: снимок контекста извлечения и точная привязка приговора.

Временная база и подставная модель. Что модель «видела», записывается в
хранилище артефактов без секретов; приговор ссылается на редакцию документа,
предложение и снимок; стенд разбора проверяет те же байты и те же подсказки,
другая редакция под тем же именем приговор не наследует, без источника
проверка не проходит, а приговор без хеша — неполный, не эталон.

    .venv/Scripts/python.exe scripts/test_extraction_context.py
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "harness"))

SECRET = "SECRET-KEY-MUST-NOT-LEAK-7f3a"


class ExtractionContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lifecycle import App
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, FOT_OPERATOR="context-test-operator",
                             FOT_LLM_PROVIDER="local", FOT_LLM_BASE_URL="http://127.0.0.1:9",
                             FOT_LLM_MODEL="test-model", FOT_LLM_API_KEY=SECRET)
        cls.env.start()
        cls.app = App(cls.temp.name)
        import intake
        import llm
        import main
        import run as stand  # harness/run.py — стенд разбора документов
        cls.intake, cls.llm, cls.main, cls.stand = intake, llm, main, stand

    @classmethod
    def tearDownClass(cls):
        cls.app.db.close()
        cls.app.d.engine.dispose()
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.db, self.d = self.app.db, self.app.d
        # Один план на класс, но каждая проверка — на чистых таблицах: стенд
        # приговоров читает всю базу, и чужие приговоры смешали бы счёт.
        for model in (self.d.Verdict, self.d.Proposal, self.d.Correction, self.d.Decision,
                      self.d.Contract, self.d.Document, self.d.Message, self.d.Activity):
            self.db.query(model).delete()
        self.db.commit()
        self.calls = []
        self.answers = {}

        def fake_freeform(path, filename="", max_chunks=8, hints=None):
            self.calls.append({"path": str(path), "hints": list(hints or [])})
            return self.answers.get(str(path)) or {"ok": True, "model": "local test-model",
                                                   "parts": 1, "contracts": []}
        self.patcher = patch.object(self.llm, "freeform", side_effect=fake_freeform)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    # ── вспомогательное ──────────────────────────────────────────
    def document(self, name, text, suffix="", scope="план"):
        path = Path(self.temp.name) / (self._testMethodName + suffix + "-" + name)
        path.write_text(text, encoding="utf-8")
        doc = self.d.Document(name=name, path=str(path), size=len(text),
                              sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                              kind="документ по договору", state="ожидает",
                              case_id=self.app.case.id, scope=scope)
        self.db.add(doc)
        self.db.commit()
        return doc

    def store(self):
        from fot_planner.harness_local.artifacts import ArtifactStore
        return ArtifactStore(self.d.ARTIFACT_DIR)

    def artifacts(self):
        return sum(len(f) for _, _, f in os.walk(self.d.ARTIFACT_DIR)) if os.path.isdir(self.d.ARTIFACT_DIR) else 0

    def row(self, code, fot=100000):
        return {"code": code, "name": "Договор " + code, "fot": fot}

    def propose(self, doc, rows):
        self.answers[doc.path] = {"ok": True, "model": "local test-model", "parts": 1,
                                  "contracts": rows}
        self.intake.propose_entities(self.db, self.app.case, doc)
        self.db.expire_all()
        return self.db.query(self.d.Proposal).filter_by(document_id=doc.id).order_by(self.d.Proposal.id).all()

    def check(self):
        out = io.StringIO()
        with redirect_stdout(out):
            total, failed = self.stand.check_verdicts(self.db, {}, None, {})
        return total, failed, out.getvalue()

    def snapshot(self, digest):
        return json.loads(self.store().read(digest).decode("utf-8"))

    # ── снимок контекста ─────────────────────────────────────────
    def test_snapshot_records_actual_hints_prompt_and_model_without_secrets(self):
        doc = self.document("договор.txt", "версия 1")
        corr = self.intake.remember_correction(self.db, doc, entity="договор",
                                               wrong='{"code": "C-9", "fot": 1}',
                                               right='{"code": "C-9", "fot": 2}', note="проверка")
        self.db.commit()
        props = self.propose(doc, [self.row("C-1")])
        self.assertEqual(len(props), 1)
        self.assertTrue(props[0].context_sha256)
        store = self.store()
        raw = store.read(props[0].context_sha256)
        snap = json.loads(raw.decode("utf-8"))
        self.assertTrue(self.calls[-1]["hints"], "подсказки модели не переданы")
        self.assertEqual(snap["hints"]["texts"], self.calls[-1]["hints"])
        self.assertEqual(snap["hints"]["ids"], [corr.id])
        self.assertEqual(snap["document"]["sha256"], doc.sha256)
        self.assertEqual(snap["document"]["artifact_sha256"], doc.sha256)
        self.assertTrue(store.has(doc.sha256), "байты документа не закреплены в хранилище")
        self.assertEqual((snap["model"]["provider"], snap["model"]["name"]), ("local", "test-model"))
        self.assertIsNone(snap["model"]["revision"])
        self.assertEqual(snap["prompt"]["system"], self.llm.FREEFORM_SYSTEM)
        self.assertEqual(snap["prompt"]["system_sha256"],
                         hashlib.sha256(self.llm.FREEFORM_SYSTEM.encode("utf-8")).hexdigest())
        self.assertEqual(snap["result"]["counts"], {"contracts": 1})
        self.assertEqual(snap["params"]["max_tokens"], self.llm.FREEFORM_MAX_TOKENS)
        self.assertNotIn(SECRET.encode("utf-8"), raw)
        for key in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "FOT_LLM_API_KEY"):
            value = os.environ.get(key)
            if value:
                self.assertNotIn(value.encode("utf-8"), raw, key + " попал в снимок")

    def test_unavailable_model_leaves_no_snapshot_and_no_proposals(self):
        doc = self.document("нет модели.txt", "текст")
        before = self.artifacts()
        self.answers[doc.path] = {"ok": False, "unavailable": True, "error": "не настроен"}
        self.assertFalse(self.intake.propose_entities(self.db, self.app.case, doc))
        self.assertEqual(self.artifacts(), before, "снимок записан без запроса к модели")
        self.assertEqual(self.db.query(self.d.Proposal).filter_by(document_id=doc.id).count(), 0)

    def test_failed_call_still_gets_snapshot_with_error(self):
        doc = self.document("обрыв.txt", "текст")
        before = self.artifacts()
        self.answers[doc.path] = {"ok": False, "error": "сеть оборвалась"}
        self.assertFalse(self.intake.propose_entities(self.db, self.app.case, doc))
        self.assertGreater(self.artifacts(), before, "неудачный вызов не закреплён")

    # ── приговор ─────────────────────────────────────────────────
    def test_verdict_binds_exact_document_proposal_and_context(self):
        doc = self.document("приговор.txt", "версия 1")
        p1, p2 = self.propose(doc, [self.row("C-2"), self.row("C-3")])
        self.main._decide_do(self.db, doc.id, {p1.id}, {p2.id}, doc.sha256)
        self.db.expire_all()
        by_proposal = {v.proposal_id: v for v in
                       self.db.query(self.d.Verdict).filter_by(document_id=doc.id).all()}
        self.assertEqual(set(by_proposal), {p1.id, p2.id})
        self.assertEqual(by_proposal[p1.id].verdict, "принято")
        self.assertEqual(by_proposal[p2.id].verdict, "отклонено")
        for p in (p1, p2):
            v = by_proposal[p.id]
            self.assertEqual(v.document_sha256, doc.sha256)
            self.assertEqual(v.context_sha256, p.context_sha256)
            self.assertTrue(v.context_sha256)

    # ── стенд разбора ────────────────────────────────────────────
    def test_stand_checks_same_bytes_with_same_hints(self):
        doc = self.document("стенд.txt", "версия 1")
        self.intake.remember_correction(self.db, doc, entity="договор",
                                        wrong='{"code": "C-4", "fot": 1}',
                                        right='{"code": "C-4", "fot": 3}', note="подсказка")
        self.db.commit()
        p1, p2 = self.propose(doc, [self.row("C-4"), self.row("C-5")])
        self.main._decide_do(self.db, doc.id, {p1.id}, {p2.id}, doc.sha256)
        seen = self.snapshot(p1.context_sha256)["hints"]["texts"]
        self.assertTrue(seen)
        # Модель теперь находит принятую строку и не повторяет отклонённую.
        self.answers[doc.path] = {"ok": True, "parts": 1, "contracts": [self.row("C-4")]}
        self.calls.clear()
        total, failed, text = self.check()
        self.assertEqual((total, failed), (2, 0), text)
        self.assertEqual([c["path"] for c in self.calls], [doc.path])
        self.assertEqual(self.calls[0]["hints"], seen, "стенд не передал подсказки из снимка")

    def test_other_edition_same_name_does_not_inherit_verdict_and_missing_source_fails(self):
        old = self.document("штатка.txt", "редакция 1")
        (p1,) = self.propose(old, [self.row("C-6")])
        self.main._decide_do(self.db, old.id, {p1.id}, set(), old.sha256)
        old.state = "заменен"
        new = self.document("штатка.txt", "редакция 2", suffix="-v2")
        self.db.commit()
        self.answers[old.path] = {"ok": True, "parts": 1, "contracts": [self.row("C-6")]}
        self.answers[new.path] = {"ok": True, "parts": 1, "contracts": []}
        self.calls.clear()
        total, failed, text = self.check()
        self.assertEqual((total, failed), (1, 0), text)
        self.assertEqual([c["path"] for c in self.calls], [old.path],
                         "приговор проверен не на своей редакции")
        # Источник той редакции исчез: новая редакция приговор не наследует,
        # проверка не проходит, а не пропускается.
        os.remove(old.path)
        self.calls.clear()
        total, failed, text = self.check()
        self.assertEqual((total, failed), (1, 1), text)
        self.assertIn("нет в реестре или файл удалён", text)
        self.assertEqual(self.calls, [], "стенд подставил другую редакцию")

    def test_legacy_verdict_without_hash_is_incomplete_not_passed(self):
        self.db.add(self.d.Verdict(document_name="старый.txt", entity="договор",
                                   fields='{"code": "X-1"}', verdict="принято"))
        self.db.commit()
        total, failed, text = self.check()
        self.assertEqual((total, failed), (0, 0), text)
        self.assertIn("неполная", text)
        self.assertIn("неполных приговоров (не в счёт): 1", text)
        self.assertEqual(self.calls, [])

    def test_proposal_without_snapshot_is_incomplete(self):
        doc = self.document("без снимка.txt", "текст")
        self.db.add(self.d.Verdict(document_name=doc.name, entity="договор", fields='{"code": "X-2"}',
                                   verdict="принято", document_id=doc.id, document_sha256=doc.sha256))
        self.db.commit()
        total, failed, text = self.check()
        self.assertEqual((total, failed), (0, 0), text)
        self.assertIn("без снимка контекста", text)
        self.assertEqual(self.calls, [])

    def test_missing_snapshot_artifact_fails_not_skips(self):
        doc = self.document("потерян снимок.txt", "текст")
        self.db.add(self.d.Verdict(document_name=doc.name, entity="договор", fields='{"code": "X-3"}',
                                   verdict="принято", document_id=doc.id, document_sha256=doc.sha256,
                                   context_sha256="0" * 64))
        self.db.commit()
        total, failed, text = self.check()
        self.assertEqual((total, failed), (1, 1), text)
        self.assertIn("недоступен", text)
        self.assertEqual(self.calls, [])

    def test_changed_bytes_under_same_path_do_not_pass(self):
        doc = self.document("подмена.txt", "было")
        (p1,) = self.propose(doc, [self.row("C-7")])
        self.main._decide_do(self.db, doc.id, {p1.id}, set(), doc.sha256)
        Path(doc.path).write_text("стало", encoding="utf-8")
        self.calls.clear()
        total, failed, text = self.check()
        self.assertEqual((total, failed), (1, 1), text)
        self.assertEqual(self.calls, [])

    # ── миграция ─────────────────────────────────────────────────
    def test_migration_step_covers_new_columns(self):
        sys.path.insert(0, str(ROOT))
        import migrations as M
        step = [s for s in M.steps() if s.VERSION == 6][0]
        for table in ("proposals", "verdicts"):
            declared = {n for n, _ in step.COLUMNS.get(table, [])}
            wanted = {n for n, _ in self.d._ADDED_COLUMNS[table]} & {
                "context_sha256", "document_id", "document_sha256", "proposal_id"}
            self.assertEqual(wanted - declared, set(), table)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
