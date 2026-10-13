"""QA-002: неизменяемые сведения проверок, без ложного допуска результата."""
import copy
import io
from contextlib import redirect_stdout
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'harness'))
from fot_planner.harness_local import manifest as M


class EvaluationSnapshotTests(unittest.TestCase):
    def stored_result(self, root):
        store = M.ArtifactStore(root)
        draft = self.draft()
        draft['input']['sha256'] = store.put_bytes(b'input')
        done = self.finish(draft=draft, output_sha256=store.put_bytes(b'output'))
        return store, done, M.store_manifest(store, done)

    def test_changed_manifest_bytes_are_rejected_at_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, done, digest = self.stored_result(tmp)
            done['run_id'] = 99
            Path(store.path(digest)).write_bytes(M.canonical_json(done))
            with self.assertRaises(ValueError):
                M.load_manifest(store, digest)

    def test_foreign_evaluation_subject_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, done, digest = self.stored_result(tmp)
            self.assertEqual(M.verify_manifest(store, done), [])
            for key, value in (('run_id', 99), ('input_sha256', 'c' * 64),
                               ('output_sha256', store.put_bytes(b'other'))):
                broken = copy.deepcopy(done)
                broken['evaluation']['subject'][key] = value
                with self.subTest(key=key):
                    self.assertTrue(any('evaluation' in e for e in M.verify_manifest(store, broken)))

    def test_standalone_verifier_never_grants_acceptance(self):
        import subprocess
        import json
        with tempfile.TemporaryDirectory() as tmp:
            store, done, digest = self.stored_result(tmp)
            command = [sys.executable, str(ROOT / 'scripts/verify_run.py'),
                       '--artifacts', tmp, '--manifest', digest]
            p = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(p.returncode, 2, p.stderr)
            result = json.loads(p.stdout)
            self.assertEqual(result['integrity'], 'verified')
            self.assertEqual(result['acceptance'], 'not_run')
            Path(store.path(done['output']['sha256'])).write_bytes(b'changed')
            p = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(p.returncode, 1, p.stderr)
            self.assertEqual(json.loads(p.stdout)['integrity'], 'failed')

    def test_numeric_findings_roundtrip_without_twenty_row_limit(self):
        from fot_planner import result_audit as RA
        from fot_planner.models import AuditViolation
        findings = [AuditViolation(rule_id='TEST-001', severity='error', message='нарушение',
                                  actual=i, expected='не больше 0', month=1) for i in range(25)]
        with patch.object(RA, 'CHECKS', [('TEST-001', 'Тест', lambda ctx, result: findings)]), \
             patch.object(RA, 'APPLIES', {}):
            report = RA.audit(None, None)
        parsed = [RA.parse_check_line(line) for line in RA.format_checks(report.checks)]
        record = self.finish(parsed)['evaluation']
        self.assertEqual(len(record['checks'][0]['findings']), 25)
        self.assertEqual(record['checks'][0]['findings'][-1]['actual'], 24)
        self.assertEqual(record['checks'][0]['findings'][-1]['expected'], 'не больше 0')
        self.assertEqual(record['reported_verdict'], 'failed')

    def test_error_finding_cannot_accompany_passed(self):
        row = {'rule_id': 'TEST-001', 'status': 'passed',
               'findings': [{'severity': 'error', 'actual': 5, 'expected': '0'}]}
        self.assertEqual(self.finish([row])['evaluation']['reported_verdict'], 'error')

    def test_broken_json_check_is_not_silently_ignored(self):
        from fot_planner import result_audit as RA
        for text in ('{', '{"schema":999,"check":{}}', '{"schema":1,"check":[]}',
                     '{"schema":1,"check":{"rule_id":"TEST-001","status":"passed","x":NaN}}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                RA.parse_check_line('Проверка: ' + text)

    def draft(self, required=None):
        return M.build_manifest(run_id=7, case_id=2, executor='test',
            input_sha256='a' * 64, reference_sha256=None, settings={}, sources={},
            code={'code_sha256': 'c' * 64, 'python': 'test', 'dependencies': {}},
            command=['solver', '--require', 'TEST-001'], time_limit_sec=1,
            required_checks=required if required is not None else [
                {'id': 'check.test', 'version': '1.0.0', 'requirement': 'TEST-001',
                 'descriptor_sha256': 'd' * 64}])

    def finish(self, checks=None, draft=None, **kw):
        args = dict(output_sha256='b' * 64, solver_status='OPTIMAL',
                    audit_status='OK', audit_lines=[], seconds=1,
                    execution_status='OPTIMAL', audit_checks=checks if checks is not None else [
                        {'rule_id': 'TEST-001', 'status': 'passed', 'reason': ''}])
        args.update(kw)
        return M.finalize_manifest(draft or self.draft(), **args)

    def test_draft_freezes_required_checks(self):
        required = [{'id': 'x', 'version': '1', 'requirement': 'TEST-001'}]
        draft = self.draft(required)
        required[0]['requirement'] = 'OTHER'
        self.assertEqual(draft['evaluation_plan']['required_checks'][0]['requirement'], 'TEST-001')
        self.assertIsNone(draft['evaluation'])

    def test_record_binds_versions_and_separates_acceptance(self):
        done = self.finish()
        record = done['evaluation']
        self.assertEqual(record['subject'], {'run_id': 7, 'input_sha256': 'a' * 64, 'output_sha256': 'b' * 64})
        self.assertEqual(record['method']['code_sha256'], 'c' * 64)
        self.assertEqual(record['checks'][0]['descriptor_sha256'], 'd' * 64)
        self.assertEqual(record['reported_verdict'], 'passed')
        self.assertEqual(record['verdict'], 'inconclusive')
        self.assertEqual(record['local_acceptance'], 'not_run')
        self.assertFalse(record['independent'])
        self.assertIsNone(record['checks'][0]['measurements'])

    def test_mandatory_nonpass_cannot_become_passed(self):
        for status in ('failed', 'error', 'not_run', 'inconclusive'):
            with self.subTest(status=status):
                record = self.finish([{'rule_id': 'TEST-001', 'status': status}])['evaluation']
                self.assertEqual(record['reported_verdict'], status)
                self.assertNotEqual(record['verdict'], 'passed')
        self.assertEqual(self.finish([])['evaluation']['reported_verdict'], 'not_run')

    def test_empty_or_only_inapplicable_is_not_passed(self):
        self.assertEqual(self.finish([], draft=self.draft([]))['evaluation']['reported_verdict'], 'inconclusive')
        rows = [{'rule_id': 'TEST-001', 'status': 'not_applicable', 'reason': 'нет объектов'}]
        self.assertEqual(self.finish(rows)['evaluation']['reported_verdict'], 'inconclusive')
        rows[0]['reason'] = ''
        self.assertEqual(self.finish(rows)['evaluation']['reported_verdict'], 'error')

    def test_unknown_duplicate_and_malformed_are_errors(self):
        valid = {'rule_id': 'TEST-001', 'status': 'passed'}
        for rows in ([{**valid, 'status': 'oops'}], [valid, valid], ['bad'],
                     [valid, {'rule_id': 'EXTRA', 'status': 'failed'}]):
            with self.subTest(rows=rows):
                self.assertEqual(self.finish(rows)['evaluation']['reported_verdict'], 'error')

    def test_missing_output_or_audit_cannot_pass(self):
        self.assertNotEqual(self.finish(output_sha256=None)['evaluation']['reported_verdict'], 'passed')
        self.assertNotEqual(self.finish(audit_status='')['evaluation']['reported_verdict'], 'passed')
        self.assertEqual(self.finish(audit_status='FAIL')['evaluation']['reported_verdict'], 'failed')

    def test_final_does_not_mutate_draft_or_observed_checks(self):
        draft = self.draft()
        before = copy.deepcopy(draft)
        checks = [{'rule_id': 'TEST-001', 'status': 'passed'}]
        done = self.finish(checks, draft=draft)
        checks[0]['status'] = 'failed'
        self.assertEqual(draft, before)
        self.assertEqual(done['evaluation']['checks'][0]['status'], 'passed')
        self.assertNotEqual(M.manifest_digest(draft), M.manifest_digest(done))

    def test_old_manifest_has_no_invented_evaluation_plan(self):
        draft = self.draft()
        draft.pop('evaluation_plan')
        draft.pop('evaluation')
        draft['schema'] = 2
        record = self.finish(draft=draft)['evaluation']
        self.assertNotEqual(record['reported_verdict'], 'passed')
        self.assertEqual(record['checks'], [])


class EvaluationServiceTests(unittest.TestCase):
    def test_cli_to_service_preserves_numeric_finding(self):
        from fot_planner import cli
        check = {'rule_id': 'FOT-001', 'status': 'failed', 'reason': '',
                 'findings': [{'severity': 'error', 'actual': 123.45, 'expected': '0', 'month': 6}]}
        result = SimpleNamespace(solver_status='OPTIMAL', audit_status='FAIL',
                                 audit_violations=[], audit_checks=[check], allocations=[],
                                 deficits=[], objective_value=0.0, solve_time_sec=0)
        output = io.StringIO()
        with patch.object(cli, 'run_planning', return_value=result), redirect_stdout(output):
            code = cli.main(['solve', '-i', 'unused-input.xlsx', '-o', 'unused-output.xlsx'])
        self.assertEqual(code, 3)
        self.assertEqual(self.app.main._read_solver_output(output.getvalue())['checks'], [check])

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

    def test_failure_keeps_solver_status_checks_and_immutable_final(self):
        app = self.app
        run = app.run(executor=app.main.EXECUTOR)
        src = Path(self.temp.name) / 'input.xlsx'
        src.write_bytes(b'input')
        draft = app.main._manifest_draft(run, str(src), {}, {}, ['solver'], '1')
        digest = run.manifest_sha256
        required = draft['evaluation_plan']['required_checks']
        self.assertTrue(required)
        self.assertTrue(all(len(c['descriptor_sha256']) == 64 for c in required))
        run.status = 'не прошёл проверку'
        app.db.commit()
        checks = [{'rule_id': c['requirement'], 'status': 'failed'} for c in required]
        done = app.main._manifest_close(app.db, run.id, draft, digest, None, 'FAIL', [], 1,
                                        audit_checks=checks, solver_status='OPTIMAL')
        self.assertEqual(done['solver']['status'], 'OPTIMAL')
        self.assertEqual(done['execution']['status'], 'не прошёл проверку')
        self.assertEqual(len(done['evaluation']['checks']), len(required))
        self.assertEqual(done['evaluation']['verdict'], 'failed')
        frozen = run.manifest_sha256
        self.assertEqual(M.load_manifest(app.main._artifacts(), frozen), done)
        self.assertIsNone(app.main._manifest_close(app.db, run.id, draft, digest, None, '', [], 1))
        self.assertEqual(run.manifest_sha256, frozen)

    def test_solve_persists_checks_on_success_and_rejection(self):
        app, main = self.app, self.app.main
        for audit_status in ('OK', 'FAIL'):
            with self.subTest(audit=audit_status):
                run = app.run(executor=main.EXECUTOR)
                status = 'passed' if audit_status == 'OK' else 'failed'
                lines = ['Статус: OPTIMAL', 'Аудит: ' + audit_status]
                lines += ['Проверка: %s %s' % (r, status) for r in main._audit_requirements()]

                def solver(command, **kwargs):
                    Path(command[command.index('-o') + 1]).write_bytes(b'candidate')
                    return SimpleNamespace(returncode=0, stdout='\n'.join(lines), stderr='')

                def transform(path, sources):
                    Path(path).write_bytes(b'final candidate')

                with patch.object(main, '_pinned_input', return_value=(b'input', {})), \
                     patch.object(main.subprocess, 'run', side_effect=solver), \
                     patch.object(main, '_heartbeat'), \
                     patch.object(main, '_write_sources_sheet', side_effect=transform), \
                     patch.object(main, '_read_goals', return_value=[]), \
                     patch.object(main, '_review_result'):
                    main._solve(app.case.id, run.id, {}, pinned_from=1)
                run = app.fresh(run)
                done = M.load_manifest(main._artifacts(), run.manifest_sha256)
                self.assertEqual(done['solver']['status'], 'OPTIMAL')
                self.assertEqual(done['evaluation']['reported_verdict'], status)
                self.assertEqual(len(done['evaluation']['checks']), len(main._audit_requirements()))
                self.assertEqual(main._artifacts().read(done['output']['sha256']),
                                 b'final candidate' if audit_status == 'OK' else b'candidate')

    def test_interruption_does_not_invent_solver_status(self):
        app, main = self.app, self.app.main
        run = app.run(executor=main.EXECUTOR)
        with patch.object(main, '_pinned_input', return_value=(b'input', {})), \
             patch.object(main, '_heartbeat'), \
             patch.object(main.subprocess, 'run', return_value=SimpleNamespace(
                 returncode=1, stdout='', stderr='interrupted')):
            main._solve(app.case.id, run.id, {}, pinned_from=1)
        run = app.fresh(run)
        done = M.load_manifest(main._artifacts(), run.manifest_sha256)
        self.assertEqual(done['execution']['status'], 'прерван')
        self.assertEqual(done['solver']['status'], 'UNKNOWN')
        self.assertEqual(done['evaluation']['reported_verdict'], 'not_run')


if __name__ == '__main__':
    unittest.main(verbosity=2)
