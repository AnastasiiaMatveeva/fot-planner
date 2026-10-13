"""QA-002: отдельный процесс действительно пересчитывает проверки кандидата."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


class NumericAuditorTests(unittest.TestCase):
    def test_export_binding_allows_sources_but_rejects_changed_number(self):
        from io import BytesIO
        from openpyxl import Workbook
        from fot_planner.harness_local.candidate_audit import workbook_digest, evaluate_bytes
        book = Workbook()
        book.active['A1'] = 100
        def raw_book():
            stream = BytesIO()
            book.save(stream)
            return stream.getvalue()
        original = workbook_digest(raw_book())
        book.create_sheet('источники')['A1'] = 'служебная запись'
        self.assertEqual(workbook_digest(raw_book()), original)
        payload = self.payload()
        payload.update(input_sha256='a' * 64, export_data_sha256=original)
        raw = json.dumps(payload).encode()
        sha = hashlib.sha256(raw).hexdigest()
        evaluate_bytes(raw, sha, input_sha256='a' * 64, workbook_bytes=raw_book())
        book.active['A1'] = 101
        with self.assertRaises(ValueError):
            evaluate_bytes(raw, sha, input_sha256='a' * 64, workbook_bytes=raw_book())
        with self.assertRaises(ValueError):
            evaluate_bytes(raw, sha, input_sha256='b' * 64, workbook_bytes=raw_book())

    def payload(self):
        return {'schema': 1, 'required_checks': ['FOT-001'],
                'context': {'year': 2026, 'employees': [], 'contracts': []},
                'candidate': {'year': 2026, 'allocations': [], 'deficits': [], 'conflicts': [],
                              'contract_balances': [], 'solver_status': 'OPTIMAL',
                              'objective_value': 0, 'solve_time_sec': 0, 'audit_status': 'OK'}}

    def run_payload(self, payload, tamper=False):
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'candidate.json'
            path.write_bytes(raw + (b' ' if tamper else b''))
            p = subprocess.run([sys.executable, str(ROOT / 'scripts/audit_candidate.py'),
                                '--bundle', str(path), '--sha256', digest],
                               capture_output=True, text=True, encoding='utf-8')
        self.assertTrue(p.stdout, p.stderr)
        return p.returncode, json.loads(p.stdout)

    def test_changed_byte_is_rejected(self):
        code, report = self.run_payload(self.payload(), tamper=True)
        self.assertEqual((code, report['verdict']), (1, 'error'))
        self.assertEqual(report['checks'], [])

    def test_empty_and_unknown_checks_do_not_pass(self):
        for required in ([], ['FOT-001'], ['UNIMPLEMENTED-001']):
            with self.subTest(required=required):
                payload = self.payload()
                payload['required_checks'] = required
                code, report = self.run_payload(payload)
                self.assertNotEqual(code, 0)
                self.assertNotEqual(report['verdict'], 'passed')

    def test_claimed_ok_does_not_hide_negative_cash(self):
        payload = self.payload()
        # Реальная карточка ФОТ: договор и касса, без запуска оптимизатора.
        payload['context']['contracts'] = [{'id': 'C1', 'name': 'Договор', 'number': '1',
                                            'contract_type': 'commercial', 'total_fot': 1000,
                                            'start_date': '2026-01-01', 'end_date': '2026-12-31'}]
        payload['candidate']['contract_balances'] = [{'contract_id': 'C1', 'year': 2026,
            'month': 1, 'opening_balance': 0, 'inflow': 0,
            'spent': 100, 'closing_balance': -100}]
        code, report = self.run_payload(payload)
        self.assertEqual((code, report['verdict']), (1, 'failed'), report)
        self.assertTrue(any(v['actual'] == -100 for v in report['violations']))
        self.assertEqual(report['acceptance'], 'not_run')
        payload['candidate']['contract_balances'][0]['closing_balance'] = 0
        payload['candidate']['contract_balances'][0]['spent'] = 0
        code, report = self.run_payload(payload)
        self.assertEqual((code, report['verdict']), (0, 'passed'), report)
        self.assertEqual(len(report['bundle_sha256']), 64)

    def test_missing_salary_is_recomputed_despite_claimed_ok(self):
        payload = self.payload()
        payload['required_checks'] = ['SALARY-001', 'FOT-001']
        payload['context']['employees'] = [{'id': 'E1', 'full_name': 'Тест',
            'position': 'Инженер', 'department': 'Отдел', 'rate': 1,
            'monthly_wage': 100, 'start_date': '2026-01-01', 'end_date': '2026-01-31'}]
        payload['context']['contracts'] = [{'id': 'C1', 'name': 'Договор', 'number': '1',
            'contract_type': 'commercial', 'total_fot': 1000,
            'start_date': '2026-01-01', 'end_date': '2026-12-31'}]
        code, report = self.run_payload(payload)
        self.assertEqual((code, report['verdict']), (1, 'failed'), report)
        self.assertTrue(any(v['rule_id'] == 'SALARY-001' and v['actual'] == 0
                            for v in report['violations']))

    def test_bundle_writer_produces_readable_typed_snapshot(self):
        from fot_planner.harness_local.candidate_audit import AuditBundle, bundle_bytes
        bundle = AuditBundle.model_validate_json(json.dumps(self.payload()))
        raw = bundle_bytes(bundle.context, bundle.candidate, bundle.required_checks)
        restored = AuditBundle.model_validate_json(raw, strict=True)
        self.assertEqual(restored.context.year, 2026)
        self.assertEqual(restored.candidate.audit_status, 'OK')

    def test_deficit_requires_explicit_permission_in_worker(self):
        payload = self.payload()
        payload['required_checks'] = ['SALARY-001', 'FOT-001']
        payload['context']['employees'] = [{'id': 'E1', 'full_name': 'Тест',
            'position': 'Инженер', 'department': 'Отдел', 'rate': 1,
            'monthly_wage': 100, 'start_date': '2026-01-01', 'end_date': '2026-01-31'}]
        payload['context']['contracts'] = [{'id': 'C1', 'name': 'Договор', 'number': '1',
            'contract_type': 'commercial', 'total_fot': 1000,
            'start_date': '2026-01-01', 'end_date': '2026-12-31'}]
        payload['candidate']['deficits'] = [{'employee_id': 'E1', 'year': 2026,
            'month': 1, 'due_amount': 100, 'paid_amount': 0, 'amount': 100}]
        for allowed in (False, True):
            payload['context']['allow_deficit'] = allowed
            code, report = self.run_payload(payload)
            self.assertEqual(code, 0 if allowed else 1, report)
            self.assertEqual(report['verdict'], 'passed' if allowed else 'failed')
            if not allowed:
                self.assertTrue(any(v['rule_id'] == 'SALARY-001' for v in report['violations']))

    def test_priority_mode_is_recomputed_in_worker(self):
        from fot_planner.models import PaymentKind
        payload = self.payload()
        payload['required_checks'] = ['PAYMENT-001', 'SALARY-001', 'FOT-001']
        payload['context']['employees'] = [{'id': 'E1', 'full_name': 'Тест',
            'position': 'Инженер', 'department': 'Отдел', 'rate': 1, 'monthly_wage': 100,
            'start_date': '2026-01-01', 'end_date': '2026-01-31'}]
        contract = {'id': 'C1', 'name': 'Договор', 'number': '1',
            'contract_type': 'commercial', 'total_fot': 1000,
            'allow_extra_work': True, 'start_date': '2026-01-01', 'end_date': '2026-12-31'}
        payload['context']['contracts'] = [contract]
        payload['candidate']['allocations'] = [
            {'employee_id': 'E1', 'contract_id': 'C1', 'year': 2026, 'month': 1,
             'payment_kind': kind.value, 'amount': amount}
            for kind, amount in ((PaymentKind.SALARY, 80), (PaymentKind.K152, 20))]
        payload['candidate']['open_rate_attributions'] = [{'employee_id': 'E1',
            'contract_id': 'C1', 'year': 2026, 'month': 1, 'open_rate': 1, 'is_main': True}]
        for enabled in (True, False):
            contract['priority_payment_mode'] = enabled
            code, report = self.run_payload(payload)
            self.assertEqual((code, report['verdict']),
                             (1, 'failed') if enabled else (0, 'passed'), report)
            if enabled:
                self.assertTrue(any(v['rule_id'] == 'PAYMENT-001' and 'приоритет' in v['message']
                                    for v in report['violations']))

    def test_salary_requires_main_place_in_worker(self):
        from fot_planner.models import PaymentKind
        payload = self.payload()
        payload['required_checks'] = ['PERSON-001', 'SALARY-001']
        payload['context']['employees'] = [{'id': 'E1', 'full_name': 'Тест',
            'position': 'Инженер', 'department': 'Отдел', 'rate': 1, 'monthly_wage': 100,
            'person_id': 'T-001', 'start_date': '2026-01-01', 'end_date': '2026-01-31'}]
        payload['context']['contracts'] = [{'id': 'C1', 'name': 'Договор', 'number': '1',
            'contract_type': 'commercial', 'total_fot': 1000,
            'start_date': '2026-01-01', 'end_date': '2026-12-31'}]
        payload['candidate']['allocations'] = [{'employee_id': 'E1', 'contract_id': 'C1',
            'year': 2026, 'month': 1, 'payment_kind': PaymentKind.SALARY.value, 'amount': 100}]
        rate = {'employee_id': 'E1', 'contract_id': 'C1', 'year': 2026,
                'month': 1, 'open_rate': 1, 'is_main': True}
        for rates, expected in (([], (1, 'failed')), ([rate], (0, 'passed'))):
            payload['candidate']['open_rate_attributions'] = rates
            code, report = self.run_payload(payload)
            self.assertEqual((code, report['verdict']), expected, report)
            if not rates:
                self.assertTrue(any(v['rule_id'] == 'PERSON-001' for v in report['violations']))

    def test_wrong_year_and_invalid_type_fail_closed(self):
        for change in ({'year': 2025}, {'allocations': 'not a list'}):
            payload = self.payload()
            payload['candidate'].update(change)
            code, report = self.run_payload(payload)
            self.assertEqual((code, report['verdict']), (1, 'error'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
