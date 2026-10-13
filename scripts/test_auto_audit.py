"""Автоматический аудит: настоящий worker, временная БД, подставной solver."""
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'harness'))


class AutomaticAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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

    def scenario(self, kind):
        from openpyxl import Workbook, load_workbook
        from fot_planner.models import PlanningContext, PlanningResult, Contract, ContractBalanceRecord
        from fot_planner.harness_local.candidate_audit import bundle_bytes, workbook_digest
        from fot_planner.harness_local.manifest import load_manifest
        app, main = self.app, self.app.main
        run = app.run(executor=main.EXECUTOR)
        real_run = main.subprocess.run
        old_sources = main._write_sources_sheet
        def execute(command, **kw):
            if 'solve' not in command:
                return real_run(command, **kw)
            out = Path(command[command.index('-o') + 1])
            ctx = PlanningContext(2026, [], [Contract('C1', 'Тест', '1', 'commercial',
                dt.date(2026, 1, 1), dt.date(2026, 12, 31), 1000)])
            result = PlanningResult(2026, [], [], [], [], 'OPTIMAL', 0, 0)
            if kind == 'negative_cash':
                result.contract_balances = [ContractBalanceRecord('C1', 2026, 1, 0, 0, 100, -100)]
            from fot_planner.excel.export import export_result
            export_result(out, ctx, result)
            if kind != 'missing':
                target = Path(command[command.index('--audit-bundle') + 1])
                raw_input = Path(command[command.index('-i') + 1]).read_bytes()
                target.write_bytes(bundle_bytes(ctx, result, main._audit_requirements(),
                    input_sha256=hashlib.sha256(raw_input).hexdigest(),
                    export_data_sha256=workbook_digest(out.read_bytes())))
            checks = '\n'.join('Проверка: %s passed' % r for r in main._audit_requirements())
            return SimpleNamespace(returncode=0, stdout='Статус: OPTIMAL\nАудит: OK\n' + checks, stderr='')
        def sources(path, data):
            old_sources(path, data)
            if kind == 'changed_export':
                wb = load_workbook(path)
                wb.worksheets[0]['A1'] = 101
                wb.save(path)
        grounds = {'документы': [], 'агенты': {}, 'справочник': {'должностей': 0}}
        with patch.object(main, '_pinned_input', return_value=(b'input', grounds)), \
             patch.object(main, '_heartbeat'), patch.object(main, '_review_result'), \
             patch.object(main, '_read_goals', return_value=[]), \
             patch.object(main, '_write_sources_sheet', side_effect=sources), \
             patch.object(main.subprocess, 'run', side_effect=execute):
            main._solve(app.case.id, run.id, {}, pinned_from=1)
        run = app.fresh(run)
        manifest = load_manifest(main._artifacts(), run.manifest_sha256)
        return run, manifest

    def test_success_runs_worker_after_sources_and_saves_bound_report(self):
        from fot_planner.harness_local.manifest import verify_manifest
        run, manifest = self.scenario('valid')
        self.assertEqual(run.status, 'OPTIMAL', run.summary)
        report = manifest['independent_audit']
        self.assertEqual(report['verdict'], 'passed')
        self.assertEqual(report['output_sha256'], manifest['output']['sha256'])
        self.assertEqual(report['input_sha256'], manifest['input']['sha256'])
        self.assertTrue(self.app.main._artifacts().verify(report['report_sha256']))
        self.assertEqual(verify_manifest(self.app.main._artifacts(), manifest), [])
        report['output_sha256'] = '0' * 64
        self.assertTrue(any('independent_audit' in p for p in
                            verify_manifest(self.app.main._artifacts(), manifest)))

    def test_planner_exports_audit_bundle(self):
        from fot_planner import planner
        from fot_planner.models import PlanningContext, PlanningResult
        from fot_planner.harness_local.candidate_audit import AuditBundle, workbook_digest
        with tempfile.TemporaryDirectory() as tmp:
            src, out, bundle_path = [Path(tmp) / n for n in ('in.xlsx', 'out.xlsx', 'audit.json')]
            src.write_bytes(b'pinned input')
            ctx = PlanningContext(2026, [], [])
            result = PlanningResult(2026, [], [], [], [], 'OPTIMAL', 0, 0)
            with patch.object(planner, 'load_context', return_value=ctx), \
                 patch.object(planner, 'validate_context', return_value=[]), \
                 patch.object(planner, 'solve', return_value=result):
                planner.run_planning(src, out, required_checks=['FOT-001'], audit_bundle_path=bundle_path)
            bundle = AuditBundle.model_validate_json(bundle_path.read_bytes())
            self.assertEqual(bundle.input_sha256, hashlib.sha256(src.read_bytes()).hexdigest())
            self.assertEqual(bundle.export_data_sha256, workbook_digest(out.read_bytes()))

    def test_real_solver_export_and_worker(self):
        import crisis
        from fot_planner.planner import run_planning
        from audit_runner import run_audit
        main = self.app.main
        with tempfile.TemporaryDirectory() as tmp, patch.object(crisis, 'WORK', Path(tmp)):
            src = crisis.build('auto_audit', [crisis.emp('E1', 'Инженер', 100000)],
                [crisis.ctr('C_A', 1200000, order='да')], {'C_A': 100000})
            out = Path(tmp) / 'result.xlsx'
            bundle = Path(tmp) / 'audit.json'
            required = main._audit_requirements()
            result = run_planning(src, out, time_limit_sec=10, required_checks=required,
                                  audit_bundle_path=bundle)
            self.assertEqual(result.solver_status, 'OPTIMAL')
            run = self.app.run(executor=main.EXECUTOR)
            draft = main._manifest_draft(run, str(src), {}, {}, ['solver'], 10)
            main._write_sources_sheet(str(out), {'документы': [], 'агенты': {},
                                                 'справочник': {'должностей': 0}})
            report = run_audit(main._artifacts(), draft, str(bundle), str(out))
            self.assertEqual(report['verdict'], 'passed', report)

    def test_missing_bundle_changed_export_and_hidden_violation_block_ready(self):
        for kind in ('missing', 'changed_export', 'negative_cash'):
            with self.subTest(kind=kind):
                run, manifest = self.scenario(kind)
                self.assertEqual(run.status, 'не прошёл проверку', run.summary)
                self.assertNotEqual(manifest['independent_audit']['verdict'], 'passed')


if __name__ == '__main__':
    unittest.main(verbosity=2)
