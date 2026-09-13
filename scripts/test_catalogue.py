"""L2.1: каталог проверяется до вызова обработчика, без сети и живой базы."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from fot_planner.harness_local.catalogue import Catalogue, CatalogueError, Task, Authority
from fot_planner.harness_local.document_tools import template_kind


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.catalogue = Catalogue.load(ROOT)
        self.task = Task(kind='document.template_kind', domain='fot.local')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'input.xlsx'
        self.authority = Authority(permissions=frozenset({'document.read'}),
                                   resources=frozenset({'python', 'openpyxl'}),
                                   files=(self.path,))

    def modified(self, key, **changes):
        rows = [d.model_dump(mode='json') for d in self.catalogue.descriptors]
        row = next(d for d in rows if d['id'] == key)
        row.update(changes)
        return Catalogue.from_documents(rows)

    def test_exact_version_and_unknown_version(self):
        self.assertEqual(self.catalogue.resolve('template-kind', '1.0.0').kind, 'capability')
        with self.assertRaises(CatalogueError):
            self.catalogue.resolve('template-kind', '2.0.0')

    def test_unknown_handler_rejected(self):
        with self.assertRaises(CatalogueError):
            self.modified('template-kind-tool', handler='os.system')

    def test_missing_schema_rejected(self):
        with self.assertRaises(CatalogueError):
            self.modified('template-kind-tool', input_schema='missing')

    def test_unknown_reference_rejected(self):
        with self.assertRaises(CatalogueError):
            self.modified('template-kind-skill', tools=[{'id': 'missing', 'version': '1.0.0'}])

    def test_self_qualification_rejected(self):
        with self.assertRaises(CatalogueError):
            self.modified('template-kind', maturity='qualified')

    def test_legacy_roles_are_honest(self):
        roles = self.catalogue.agent_view()
        self.assertEqual(set(roles), {'intake', 'tuning', 'infeasible', 'scenario',
                                     'reconcile', 'memo', 'checker', 'norms'})
        for key in ('scenario', 'memo', 'reconcile'):
            self.assertFalse(roles[key]['real'])
        self.assertTrue(roles['intake']['real'])
        self.assertEqual(roles['intake']['name'], 'Извлечение данных')

    def test_rename_does_not_change_selection(self):
        changed = self.modified('intake', name='Другое имя')
        self.assertEqual(self.catalogue.select(self.task, self.authority),
                         changed.select(self.task, self.authority))

    def test_missing_capability_is_not_success(self):
        result = self.catalogue.select(Task(kind='absent', domain='fot.local'), self.authority)
        self.assertEqual(result.status, 'missing_capability')
        self.assertTrue(result.action)
        self.assertIsNone(result.tool)

    def test_missing_resource_is_not_missing_capability(self):
        result = self.catalogue.select(self.task, Authority(permissions=frozenset({'document.read'})))
        self.assertEqual(result.status, 'unavailable')
        self.assertTrue(result.action)
        self.assertEqual(self.catalogue.resolve('template-kind', '1.0.0').maturity, 'implemented')

    def test_no_permission_or_domain_no_execution(self):
        handler = Mock()
        for task, auth in ((self.task, Authority()),
                           (Task(kind='document.template_kind', domain='other'), self.authority)):
            with self.assertRaises(CatalogueError):
                self.catalogue.invoke(task, {'path': str(self.path)}, auth,
                                      {'document.template_kind': handler})
        handler.assert_not_called()

    def test_experimental_only_in_trial(self):
        catalogue = self.modified('template-kind', experimental=True)
        self.assertEqual(catalogue.select(self.task, self.authority).status, 'trial_only')
        auth = self.authority.model_copy(update={'trial': True})
        self.assertEqual(catalogue.select(self.task, auth).status, 'ready')

    def test_invalid_input_or_ungranted_file_never_called(self):
        handler = Mock()
        for payload in ({'path': 23}, {'path': str(self.path), 'exec': 'bad'},
                        {'path': str(self.path.parent / 'other.xlsx')}):
            with self.assertRaises(CatalogueError):
                self.catalogue.invoke(self.task, payload, self.authority,
                                      {'document.template_kind': handler})
        handler.assert_not_called()

    def test_invalid_output_is_error(self):
        with self.assertRaises(CatalogueError):
            self.catalogue.invoke(self.task, {'path': str(self.path)}, self.authority,
                                  {'document.template_kind': lambda **_: {'kind': 'invented'}})

    def test_known_form_and_unknown_without_model(self):
        import openpyxl
        for sheet, expected in [('Договоры', 'документ по договору'),
                                ('Сотрудники', 'штатное расписание'), ('Лист1', None)]:
            wb = openpyxl.Workbook()
            wb.active.title = sheet
            wb.save(self.path)
            wb.close()
            result = self.catalogue.invoke(self.task, {'path': str(self.path)}, self.authority,
                                           {'document.template_kind': template_kind})
            self.assertEqual(result, {'kind': expected})

    def test_registered_schema_matches_runtime(self):
        from fot_planner.harness_local.catalogue import schemas
        for name, schema in schemas().items():
            self.assertEqual(json.loads((ROOT / 'schemas/local-harness' / (name + '.schema.json')).read_text(encoding='utf-8')), schema)

    def test_duplicate_route_and_version_rejected(self):
        rows = [d.model_dump(mode='json') for d in self.catalogue.descriptors]
        with self.assertRaises(CatalogueError):
            Catalogue.from_documents(rows + [copy.deepcopy(rows[0])])
        duplicate = copy.deepcopy(next(r for r in rows if r['id'] == 'template-kind'))
        duplicate['id'] = 'ambiguous'
        catalogue = Catalogue.from_documents(rows + [duplicate])
        self.assertEqual(catalogue.select(self.task, self.authority).status, 'ambiguous')

    def test_suspended_tool_is_not_run(self):
        catalogue = self.modified('template-kind-tool', maturity='suspended')
        self.assertEqual(catalogue.select(self.task, self.authority).status, 'unavailable')

    def test_manifest_cannot_remove_file_protection(self):
        for changes in ({'effects': 'none'}, {'permissions': []}, {'resources': []},
                        {'input_schema': 'document-kind'}):
            with self.assertRaises(CatalogueError):
                self.modified('template-kind-tool', **changes)

    def test_required_unimplemented_check_blocks_catalogue(self):
        with self.assertRaises(CatalogueError):
            self.modified('template-kind', checks=[{'id': 'missing-check', 'version': '1.0.0'}])

    def test_missing_callable_never_looks_successful(self):
        with self.assertRaises(CatalogueError):
            self.catalogue.invoke(self.task, {'path': str(self.path)}, self.authority, {})

    def test_schema_drift_rejected_at_load(self):
        original = Path.read_text
        def altered(path, *args, **kwargs):
            return '{}' if path.name == 'catalogue.schema.json' else original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', altered), self.assertRaises(CatalogueError):
            Catalogue.load(ROOT)

    def test_descriptor_digest_is_full_and_tracks_changes(self):
        digest = self.catalogue.descriptor_digest('intake', '1.0.0')
        self.assertEqual(len(digest), 64)
        changed = self.modified('intake', purpose='Уточнённая ответственность')
        self.assertNotEqual(digest, changed.descriptor_digest('intake', '1.0.0'))


class CatalogueIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / 'harness'))
        from lifecycle import App
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ)
        cls.env.start()
        cls.app = App(cls.temp.name)

    @classmethod
    def tearDownClass(cls):
        cls.app.db.close()
        cls.app.d.engine.dispose()
        cls.env.stop()
        cls.temp.cleanup()

    def test_app_classification_uses_resolver_without_llm(self):
        import openpyxl
        import intake
        path = Path(self.temp.name) / 'known.xlsx'
        wb = openpyxl.Workbook()
        wb.active.title = 'Договоры'
        wb.save(path)
        wb.close()
        catalogue = self.app.main.agents.default_catalogue()
        with patch.object(catalogue, 'invoke', wraps=catalogue.invoke) as invoked, \
             patch.object(intake.llm, 'classify', side_effect=AssertionError('LLM запрещена')):
            kind, owner, source, garbled = intake.classify(str(path), 'Любое название')
        self.assertEqual((kind, owner, garbled), ('документ по договору', 'intake', None))
        self.assertEqual(invoked.call_count, 1)

    def test_app_and_map_use_same_catalogue_without_database_import(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import check_requirements_map
        agents = self.app.main.agents
        expected = Catalogue.load(ROOT).agent_view()
        self.assertEqual(agents.AGENTS, expected)
        self.assertEqual(check_requirements_map.catalogue(), {k: v['real'] for k, v in expected.items()})
        self.assertEqual([r['key'] for r in agents.agent_list()],
                         ['intake', 'tuning', 'infeasible', 'scenario', 'reconcile', 'memo', 'norms', 'checker'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
