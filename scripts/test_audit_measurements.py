"""QA-002: сохраняются реальные сравнения, а не только их общий статус."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from fot_planner import result_audit as RA
import test_numeric_auditor as numeric
import test_evaluation_snapshot as snapshots


class MeasurementTests(unittest.TestCase):
    def check(self, report, rule):
        return next(c for c in report.checks if c['rule_id'] == rule)

    def money_payload(self):
        payload = numeric.NumericAuditorTests().payload()
        payload['context']['contracts'] = [{'id': 'C1', 'name': 'Тест', 'number': '1',
            'contract_type': 'commercial', 'total_fot': 1000,
            'start_date': '2026-01-01', 'end_date': '2026-12-31'}]
        payload['candidate']['contract_balances'] = [{'contract_id': 'C1', 'year': 2026,
            'month': 1, 'opening_balance': 0, 'inflow': 0, 'spent': 0, 'closing_balance': 0}]
        return payload

    def test_worker_records_passed_and_failed_cash_boundaries(self):
        for actual, passed in ((0, True), (-1, True), (-1.01, False)):
            with self.subTest(actual=actual):
                payload = self.money_payload()
                payload['candidate']['contract_balances'][0]['closing_balance'] = actual
                code, report = numeric.NumericAuditorTests().run_payload(payload)
                check = next(c for c in report['checks'] if c['rule_id'] == 'FOT-001')
                m = next(m for m in check['measurements'] if m['metric'] == 'closing_balance')
                self.assertEqual((m['actual'], m['lower'], m['absolute_tolerance']), (actual, 0, 1))
                self.assertEqual((m['unit'], m['passed']), ('RUB', passed))
                self.assertEqual(m['coordinates'], {'contract_id': 'C1', 'month': 1})
                self.assertEqual(code, 0 if passed else 1)

    def test_salary_measurement_counts_deficit_without_claiming_it_paid(self):
        payload = self.money_payload()
        payload['context']['employees'] = [{'id': 'E1', 'full_name': 'Тест', 'position': 'Инженер',
            'department': 'Отдел', 'rate': 1, 'monthly_wage': 100,
            'start_date': '2026-01-01', 'end_date': '2026-01-31'}]
        from fot_planner.harness_local.candidate_audit import AuditBundle
        import json
        b = AuditBundle.model_validate_json(json.dumps(payload))
        b.context.allow_deficit = True
        b.candidate.deficits = [NS(employee_id='E1', month=1, amount=100)]
        row = next(m for m in self.check(RA.audit(b.context, b.candidate), 'SALARY-001')['measurements']
                   if m['metric'] == 'allocated_plus_deficit')
        self.assertEqual((row['metric'], row['actual'], row['lower'], row['upper']),
                         ('allocated_plus_deficit', 100, 100, 100))
        self.assertTrue(row['passed'])

    def test_labor_records_effective_tolerance_without_changing_boundary(self):
        from fot_planner.models import ContractLaborPlan, labor_row_id
        lp = ContractLaborPlan(contract_id='C1', year=2026, position='Инженер', person_months=10,
                               avg_monthly_labor_cost=100)
        ctx = NS(labor_plans=[lp], salary_stability=NS(goz_labor_tolerance=0.2))
        for actual, passed in ((10.5, True), (10.50001, False)):
            result = NS(labor_pm_attributions=[NS(labor_row_id=labor_row_id(lp), month=1,
                                                 person_months=actual)],
                        labor_payment_attributions=[NS(labor_row_id=labor_row_id(lp), amount=1000)])
            found = RA._labor_tolerance(ctx, result)
            m = found.measurements[0]
            self.assertEqual(m['relative_tolerance'], .05)
            self.assertAlmostEqual(m['absolute_tolerance'], .500001)
            self.assertEqual((m['unit'], m['passed']), ('person_month', passed))
            self.assertEqual(bool(found), not passed)

    def test_rate_person_and_payment_measurements(self):
        from fot_planner.harness_local.candidate_audit import AuditBundle
        from fot_planner.models import PaymentKind
        import json
        b = AuditBundle.model_validate_json(json.dumps(self.money_payload()))
        b.context.employees = [NS(id='E1', person_id='007', full_name='Тест')]
        b.candidate.open_rate_attributions = [NS(employee_id='E1', contract_id='C1', month=1, open_rate=.86)]
        rate = RA._rate_step(b.context, b.candidate)
        self.assertEqual(len(rate.measurements), 2)
        self.assertFalse(rate.measurements[1]['passed'])
        self.assertEqual(rate.measurements[1]['unit'], 'quarter_step')
        person = RA._person_limits(b.context, b.candidate)
        self.assertEqual(person.measurements[0]['coordinates']['person_id'], '007')
        b.candidate.allocations = [NS(employee_id='E1', contract_id='C1', month=1,
                                      amount=10, payment_kind=PaymentKind.SALARY)]
        payments = RA._payment_window(b.context, b.candidate)
        self.assertGreaterEqual(len(payments.measurements), 1)
        self.assertTrue(all(m['unit'] == 'boolean' for m in payments.measurements))

    def test_measurements_survive_cli_and_snapshot(self):
        row = {'rule_id': 'TEST-001', 'status': 'passed', 'measurement_schema': 1,
               'measurements': [{'metric': 'cash', 'actual': 0, 'lower': 0, 'upper': None,
                 'unit': 'RUB', 'absolute_tolerance': 1, 'passed': True, 'coordinates': {}}]}
        parsed = RA.parse_check_line(RA.format_checks([row])[0])
        record = snapshots.EvaluationSnapshotTests().finish([parsed])['evaluation']
        self.assertEqual(record['checks'][0]['measurements'], row['measurements'])
        self.assertIsNone(record['checks'][0]['measurement_gap'])
        self.assertEqual(record['verdict'], 'inconclusive')
        parsed['measurements'][0]['actual'] = 5
        self.assertEqual(record['checks'][0]['measurements'][0]['actual'], 0)

    def test_bad_measurements_cannot_accompany_passed(self):
        good = {'metric': 'cash', 'actual': 0, 'lower': 0, 'upper': None, 'unit': 'RUB',
                'absolute_tolerance': 1, 'passed': True, 'coordinates': {}}
        for changed in ({'passed': False}, {'actual': float('nan')}, {'absolute_tolerance': -1},
                        {'unit': ''}, {'passed': 'yes'}, {'actual': float('inf')}):
            row = {'rule_id': 'TEST-001', 'status': 'passed', 'measurement_schema': 1,
                   'measurements': [{**good, **changed}]}
            record = snapshots.EvaluationSnapshotTests().finish([row])['evaluation']
            self.assertEqual(record['reported_verdict'], 'error', changed)

    def test_empty_and_legacy_measurements_keep_explicit_gap(self):
        for row in ({'rule_id': 'TEST-001', 'status': 'passed'},
                    {'rule_id': 'TEST-001', 'status': 'passed',
                     'measurement_schema': 1, 'measurements': []}):
            record = snapshots.EvaluationSnapshotTests().finish([row])['evaluation']
            self.assertTrue(record['checks'][0]['measurement_gap'])
            self.assertNotEqual(record['verdict'], 'passed')
        for schema in (2, True, None):
            row = {'rule_id': 'TEST-001', 'status': 'passed',
                   'measurement_schema': schema, 'measurements': []}
            self.assertEqual(snapshots.EvaluationSnapshotTests().finish([row])
                             ['evaluation']['reported_verdict'], 'error')


if __name__ == '__main__':
    unittest.main(verbosity=2)
