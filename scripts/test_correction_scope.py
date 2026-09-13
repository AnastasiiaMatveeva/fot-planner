"""MEM-001/002: исправления остаются в своём контексте, не меняют нормы."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'harness'))


class CorrectionScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lifecycle import App
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, FOT_OPERATOR='scope-test-operator')
        cls.env.start()
        cls.app = App(cls.temp.name)
        import intake
        import llm
        cls.intake, cls.llm = intake, llm

    @classmethod
    def tearDownClass(cls):
        cls.app.db.close()
        cls.app.d.engine.dispose()
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.db, self.d = self.app.db, self.app.d
        self.path = Path(self.temp.name) / (self._testMethodName + '.txt')
        self.path.write_text('исходная версия', encoding='utf-8')
        self.doc = self.d.Document(name='одинаковое имя.txt', path=str(self.path),
                                   sha256=hashlib.sha256(self.path.read_bytes()).hexdigest(),
                                   kind='документ по договору', case_id=self.app.case.id, scope='план')
        self.db.add(self.doc)
        self.db.commit()

    def remember(self, **overrides):
        fields = dict(entity='договор', wrong='{"fot": 100}', right='{"fot": 200}', note='правка')
        fields.update(overrides)
        row = self.intake.remember_correction(self.db, self.doc, **fields)
        self.db.commit()
        return row

    def hints(self, doc=None, limit=8):
        return self.intake.correction_hints(self.db, self.doc.kind, limit=limit, document=doc or self.doc)

    def test_bound_correction_uses_real_actor_and_own_source(self):
        row = self.remember()
        self.assertEqual(len(self.hints()), 1)
        decision = self.db.query(self.d.Decision).filter_by(subject_kind='правка', subject_id=str(row.id)).one()
        self.assertEqual(decision.actor, 'scope-test-operator')
        self.assertEqual(decision.kind, 'данные')
        self.assertEqual(json.loads(decision.action)['binding']['source']['sha256'], self.doc.sha256)

    def test_same_name_other_document_or_scope_gets_no_hint(self):
        self.remember()
        other = self.d.Document(name=self.doc.name, path=self.doc.path, sha256=self.doc.sha256,
                                kind=self.doc.kind, case_id=self.doc.case_id, scope='план')
        self.db.add(other)
        self.db.commit()
        self.assertEqual(self.hints(other), [])
        self.doc.scope = 'реестр'
        self.db.commit()
        self.assertEqual(self.hints(), [])

    def test_changed_bytes_and_revision_do_not_inherit_hint(self):
        self.remember()
        self.path.write_text('новая версия', encoding='utf-8')
        self.assertEqual(self.hints(), [])
        self.doc.sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.db.commit()
        self.assertEqual(self.hints(), [])

    def test_legacy_unknown_and_call_without_document_are_excluded(self):
        self.db.add(self.d.Correction(document_name=self.doc.name, document_kind=self.doc.kind,
                                      entity='договор', wrong='100', right='999'))
        self.db.commit()
        self.assertEqual(self.hints(), [])
        self.assertEqual(self.intake.correction_hints(self.db, self.doc.kind), [])

    def test_later_conflicting_correction_does_not_win_even_with_limit(self):
        first = self.remember()
        second = self.remember(right='{"fot": 300}')
        self.assertEqual(self.hints(limit=1), [])
        selection = self.intake.correction_context(self.db, self.doc, limit=1)
        self.assertEqual(set(selection['conflicts']), {first.id, second.id})

    def test_unconfirmed_note_is_history_not_rule(self):
        self.remember(wrong=None, right=None, note='всё неправильно', confirmed=False)
        self.assertEqual(self.hints(), [])

    def test_missing_source_is_not_reused(self):
        self.remember()
        self.path.unlink()
        self.assertEqual(self.hints(), [])

    def test_correction_content_mutation_invalidates_binding(self):
        row = self.remember()
        row.right = '99999'
        self.db.commit()
        self.assertEqual(self.hints(), [])

    def test_prompt_does_not_override_general_rules(self):
        text = self.llm._hinted('НОРМЫ', ['правка строки'])
        self.assertNotIn('они важнее общих правил', text)
        self.assertIn('не изменяют нормы', text)
        self.assertEqual(self.llm._hinted('НОРМЫ', []), 'НОРМЫ')

    def test_different_case_or_document_kind_excluded(self):
        self.remember()
        other = self.d.Case(title='другой план', year=2027)
        self.db.add(other)
        self.db.commit()
        self.doc.case_id = other.id
        self.db.commit()
        self.assertEqual(self.hints(), [])
        self.doc.case_id = self.app.case.id
        self.doc.kind = 'другая форма'
        self.db.commit()
        self.assertEqual(self.hints(), [])

    def test_duplicate_value_deduplicated_without_conflict(self):
        self.remember()
        self.remember(right='{"fot":200}')
        self.assertEqual(len(self.hints()), 1)
        self.assertEqual(self.intake.correction_context(self.db, self.doc)['conflicts'], [])

    def test_plan_year_change_does_not_inherit_hint(self):
        self.remember()
        previous = self.app.case.year
        try:
            self.app.case.year = previous + 1
            self.db.commit()
            self.assertEqual(self.hints(), [])
        finally:
            self.app.case.year = previous
            self.db.commit()

    def test_conflict_same_field_with_different_previous_values(self):
        self.remember(wrong='{"C1":100,"поле":"ФОТ"}', right='{"C1":200,"поле":"ФОТ"}')
        self.remember(wrong='{"C1":200,"поле":"ФОТ"}', right='{"C1":300,"поле":"ФОТ"}')
        self.assertEqual(self.hints(), [])

    def test_reject_route_binds_memory_in_same_decision(self):
        import asyncio
        from decisions import Req
        pr = self.d.Proposal(document_id=self.doc.id, entity='сотрудник',
                             payload='{"code":"TEST","salary":100}', state='предложено')
        self.db.add(pr)
        self.db.commit()
        before = self.db.query(self.d.Decision).count()
        asyncio.run(self.app.main.decide_proposals(self.doc.id, Req({'reject': [pr.id], 'document_sha256': self.doc.sha256})))
        self.db.expire_all()
        self.assertEqual(self.db.query(self.d.Decision).count(), before + 1)
        self.assertEqual(len(self.hints()), 1)
        self.assertEqual(self.db.get(self.d.Proposal, pr.id).state, 'отклонено')

    def test_extraction_receives_only_selected_hints(self):
        self.remember()
        with patch.object(self.llm, 'freeform', return_value={'ok': False, 'error': 'test'}) as parse:
            self.assertFalse(self.intake.propose_entities(self.db, self.app.case, self.doc))
        self.assertEqual(parse.call_args.kwargs['hints'], self.hints())

    def test_document_chat_does_not_bind_foreign_registry_row(self):
        from fastapi import BackgroundTasks
        key = 'C-FOREIGN-' + str(self.doc.id)
        row = self.d.Contract(code=key, fund=100, document_id=None)
        self.db.add(row)
        self.db.commit()
        reply = {'ok': True, 'reply': 'записано', 'action': 'заполнить поле',
                 'edits': [{'entity': 'договор', 'key': key, 'field': 'фонд', 'value': 200}]}
        with patch.object(self.app.main.chat, 'reply_doc', return_value=reply):
            self.app.main.document_message(self.doc.id, BackgroundTasks(), {'text': 'исправь ФОТ'})
        self.db.expire_all()
        self.assertEqual(self.db.get(self.d.Contract, row.id).fund, 200)
        self.assertEqual(self.hints(), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
