# -*- coding: utf-8 -*-
"""MEM-002: конфликт исправлений разрешает оператор явным решением.

Временная база, подставная модель. Две правки одного поля с разными
значениями исключаются из подсказок целиком; решение «оставить № N»
записывается с оператором и хешем предмета, остальные правки группы
считаются заменёнными, но остаются в истории; новое расходящееся значение
открывает конфликт заново; устаревшая форма и правка вне спора отклоняются.

    .venv/Scripts/python.exe scripts/test_correction_conflicts.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "harness"))


class Req:
    def __init__(self, body):
        self._body = json.dumps(body, ensure_ascii=False).encode("utf-8")

    async def body(self):
        return self._body

    async def json(self):
        return json.loads(self._body.decode("utf-8"))


class CorrectionConflictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lifecycle import App
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, FOT_OPERATOR="Матвеева А.И.")
        cls.env.start()
        cls.app = App(cls.temp.name)
        import intake
        import llm
        import main
        cls.intake, cls.llm, cls.main = intake, llm, main

    @classmethod
    def tearDownClass(cls):
        cls.app.db.close()
        cls.app.d.engine.dispose()
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.db, self.d = self.app.db, self.app.d
        for model in (self.d.Proposal, self.d.Correction, self.d.Decision, self.d.Document,
                      self.d.Message, self.d.Activity):
            self.db.query(model).delete()
        self.db.commit()
        path = Path(self.temp.name) / (self._testMethodName + ".txt")
        path.write_text("исходная версия", encoding="utf-8")
        self.doc = self.d.Document(name="договор.txt", path=str(path),
                                   sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                   kind="документ по договору", case_id=self.app.case.id, scope="план")
        self.db.add(self.doc)
        self.db.commit()

    # ── вспомогательное ──────────────────────────────────────────
    def remember(self, right, wrong='{"code": "C1", "fot": 100}'):
        row = self.intake.remember_correction(self.db, self.doc, entity="договор", wrong=wrong,
                                              right=right, note="правка")
        self.db.commit()
        return row

    def hints(self):
        return self.intake.correction_context(self.db, self.doc)["hints"]

    def conflicts(self):
        return self.intake.correction_conflicts(self.db, self.doc)

    def resolve(self, keep, sha=None):
        return asyncio.get_event_loop().run_until_complete(
            self.main.resolve_corrections(self.doc.id, Req({"keep": keep, "document_sha256": sha or self.doc.sha256})))

    def decisions(self):
        self.db.expire_all()
        return self.db.query(self.d.Decision).filter_by(subject_kind="конфликт правок").order_by(self.d.Decision.id).all()

    # ── случаи ───────────────────────────────────────────────────
    def test_conflict_excludes_both_and_lists_group(self):
        a, b = self.remember('{"code": "C1", "fot": 200}'), self.remember('{"code": "C1", "fot": 300}')
        self.assertEqual(self.hints(), [])
        self.assertEqual(self.conflicts(), [[a.id, b.id]])

    def test_resolution_keeps_chosen_not_latest_and_records_operator(self):
        a, b = self.remember('{"code": "C1", "fot": 200}'), self.remember('{"code": "C1", "fot": 300}')
        res = self.resolve(a.id)
        self.assertEqual((res["оставлено"], res["заменены"]), (a.id, [b.id]))
        dec = self.decisions()
        self.assertEqual(len(dec), 1)
        self.assertEqual((dec[0].actor, dec[0].outcome), ("Матвеева А.И.", "применено"))
        self.assertEqual(json.loads(dec[0].action)["заменены"], [b.id])
        self.assertEqual(dec[0].precondition, self.doc.sha256)
        hints = self.hints()
        self.assertEqual(len(hints), 1)
        self.assertIn('"fot": 200', hints[0], "действует не выбранная правка")
        self.assertEqual(self.conflicts(), [])
        # История цела: обе правки и их привязки остались.
        self.assertEqual(self.db.query(self.d.Correction).count(), 2)
        self.assertEqual(self.db.query(self.d.Decision).filter_by(subject_kind="правка").count(), 2)

    def test_extraction_receives_resolved_hint_without_conflict_message(self):
        a, b = self.remember('{"code": "C1", "fot": 200}'), self.remember('{"code": "C1", "fot": 300}')
        self.resolve(a.id)
        seen = []

        def fake(path, filename="", max_chunks=8, hints=None):
            seen.append(list(hints or []))
            return {"ok": True, "parts": 1, "contracts": [{"code": "C1", "name": "Д1", "fot": 200}]}
        with patch.object(self.llm, "freeform", side_effect=fake):
            self.intake.propose_entities(self.db, self.app.case, self.doc)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], self.hints())
        texts = [m.text for m in self.db.query(self.d.Message).all()]
        self.assertFalse(any("противоречащие исправления" in t for t in texts), texts)
        self.assertTrue(any("разрешён" in t for t in texts), "решение не отражено в ленте")

    def test_new_diverging_value_reopens_conflict_and_keeps_superseded(self):
        a, b = self.remember('{"code": "C1", "fot": 200}'), self.remember('{"code": "C1", "fot": 300}')
        self.resolve(a.id)
        c = self.remember('{"code": "C1", "fot": 400}')
        self.assertEqual(self.conflicts(), [[a.id, c.id]])
        self.assertEqual(self.hints(), [])
        listing = self.main.document_corrections(self.doc.id)
        status = {r["id"]: r["status"] for r in listing["corrections"]}
        self.assertEqual(status, {a.id: "конфликт", b.id: "заменена", c.id: "конфликт"})
        self.assertEqual(listing["superseded"], [b.id])

    def test_correction_outside_conflict_is_refused(self):
        a = self.remember('{"code": "C1", "fot": 200}')
        with self.assertRaises(self.main.HTTPException) as ctx:
            self.resolve(a.id)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.decisions(), [])
        self.assertEqual(len(self.hints()), 1)

    def test_stale_form_is_refused_and_recorded(self):
        a, b = self.remember('{"code": "C1", "fot": 200}'), self.remember('{"code": "C1", "fot": 300}')
        with self.assertRaises(self.main.HTTPException) as ctx:
            self.resolve(a.id, sha="старая-редакция")
        self.assertEqual(ctx.exception.status_code, 409)
        dec = self.decisions()
        self.assertEqual(len(dec), 1)
        self.assertTrue(dec[0].outcome.startswith("отклонено"))
        self.assertEqual(self.hints(), [])
        self.assertEqual(self.conflicts(), [[a.id, b.id]])

    def test_listing_shows_applied_and_conflict_statuses(self):
        a = self.remember('{"code": "C1", "fot": 200}')
        b, c = self.remember('{"code": "C2", "fot": 1}', wrong='{"code": "C2"}'), \
            self.remember('{"code": "C2", "fot": 2}', wrong='{"code": "C2"}')
        listing = self.main.document_corrections(self.doc.id)
        status = {r["id"]: r["status"] for r in listing["corrections"]}
        self.assertEqual(status, {a.id: "применяется", b.id: "конфликт", c.id: "конфликт"})
        self.assertEqual(listing["conflicts"], [[b.id, c.id]])
        self.assertEqual(listing["document_sha256"], self.doc.sha256)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
