"""Независимая проверка данных выгрузки, включая ошибку самого экспортёра."""
import datetime as dt
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from fot_planner.models import (PlanningContext, PlanningResult, Contract, Employee,
    AllocationRecord, PaymentKind, OpenRateAttribution)


class ExportAuditTests(unittest.TestCase):
    def fixture(self):
        from fot_planner.excel.export import export_result
        ctx = PlanningContext(2026, [Employee('E1', 'Тест', 'Инженер', 'Отдел', 1, 100)],
            [Contract('C1', 'Договор', '1', 'commercial', dt.date(2026, 1, 1), dt.date(2026, 12, 31), 2000)])
        result = PlanningResult(2026, [AllocationRecord('E1', 'C1', 2026, 1, PaymentKind.SALARY, 100)],
                                [], [], [], 'OPTIMAL', 0, 0)
        result.open_rate_attributions = [OpenRateAttribution('E1', 'C1', 2026, 1, 1, True)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'out.xlsx'
            export_result(path, ctx, result)
            return ctx, result, path.read_bytes()

    def change(self, raw, action):
        from openpyxl import load_workbook
        book = load_workbook(BytesIO(raw))
        action(book)
        stream = BytesIO()
        book.save(stream)
        book.close()
        return stream.getvalue()

    def test_real_export_matches_candidate(self):
        from fot_planner.harness_local.export_audit import audit_export
        ctx, result, raw = self.fixture()
        report = audit_export(raw, ctx, result)
        self.assertEqual(report['verdict'], 'passed', report)
        self.assertEqual(report['scope'], 'export-core-tables-v1')

    def test_row_order_is_irrelevant_but_column_loss_is_not(self):
        from fot_planner.excel.export import export_result
        from fot_planner.harness_local.export_audit import audit_export
        ctx, result, _ = self.fixture()
        result.allocations.append(AllocationRecord('E1', 'C1', 2026, 2, PaymentKind.SALARY, 100.0))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'out.xlsx'
            export_result(path, ctx, result)
            raw = path.read_bytes()
        def swap(book):
            sheet = book['План выплат']
            a, b = [c.value for c in sheet[2]], [c.value for c in sheet[3]]
            for index, (x, y) in enumerate(zip(a, b), 1):
                sheet.cell(2, index).value, sheet.cell(3, index).value = y, x
        self.assertEqual(audit_export(self.change(raw, swap), ctx, result)['verdict'], 'passed')
        raw = self.change(raw, lambda b: b['План выплат'].cell(1, 10, 'переименованная сумма'))
        self.assertEqual(audit_export(raw, ctx, result)['verdict'], 'failed')

    def test_deficits_zero_filter_rounding_and_empty_export(self):
        from fot_planner.models import DeficitRecord
        from fot_planner.excel.export import export_result
        from fot_planner.harness_local.export_audit import audit_export
        ctx, result, _ = self.fixture()
        result.deficits = [DeficitRecord('E1', 2026, 1, 150, 100, 50)]
        result.allocations[0].amount = 100.123
        result.allocations.append(AllocationRecord('E1', 'C1', 2026, 2, PaymentKind.SALARY, .001))
        for empty in (False, True):
            if empty:
                ctx = PlanningContext(2026, [], [])
                result = PlanningResult(2026, [], [], [], [], 'OPTIMAL', 0, 0)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'out.xlsx'
                export_result(path, ctx, result)
                report = audit_export(path.read_bytes(), ctx, result)
                self.assertEqual(report['verdict'], 'passed', report)
                if not empty:
                    raw = self.change(path.read_bytes(), lambda b: b['Дефициты'].cell(2, 6, 0))
                    self.assertEqual(audit_export(raw, ctx, result)['verdict'], 'failed')

    def test_cash_matrix_and_goals_match_candidate(self):
        from fot_planner.models import ContractBalanceRecord, GoalMetric
        from fot_planner.excel.export import export_result
        from fot_planner.harness_local.export_audit import audit_export
        ctx, result, _ = self.fixture()
        result.contract_balances = [ContractBalanceRecord('C1', 2026, 1, 0, 500, 100, 400)]
        result.goals = [GoalMetric('test', 'Тест', 10, 'RUB', 'вес', 2, 3, 6)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'out.xlsx'
            export_result(path, ctx, result)
            raw = path.read_bytes()
        self.assertEqual(audit_export(raw, ctx, result)['verdict'], 'passed')
        for action in (lambda b: b['ФОТ по договорам'].cell(2, 5, 501),
                       lambda b: b['цели'].cell(2, 2, 999),
                       lambda b: b.remove(b['ФОТ по договорам'])):
            report = audit_export(self.change(raw, action), ctx, result)
            self.assertEqual(report['verdict'], 'failed', report)

    def test_changed_amount_identity_duplicate_missing_and_formula_fail(self):
        from fot_planner.harness_local.export_audit import audit_export
        ctx, result, raw = self.fixture()
        def set_field(book, field, value):
            sheet = book['План выплат']
            col = [c.value for c in sheet[1]].index(field) + 1
            sheet.cell(2, col).value = value
        def duplicate(book):
            sheet = book['План выплат']
            sheet.append([c.value for c in sheet[2]])
        for action in (lambda b: set_field(b, 'сумма', 100.01),
                       lambda b: set_field(b, 'код строки', 'E2'),
                       lambda b: set_field(b, 'сумма', '=100'), duplicate,
                       lambda b: b['План выплат'].delete_rows(2),
                       lambda b: b.remove(b['открытые_ставки'])):
            report = audit_export(self.change(raw, action), ctx, result)
            self.assertEqual(report['verdict'], 'failed', report)

    def test_fresh_digest_does_not_hide_wrong_export(self):
        from fot_planner.harness_local.candidate_audit import bundle_bytes, workbook_digest, evaluate_bytes
        ctx, result, raw = self.fixture()
        raw = self.change(raw, lambda b: b['План выплат'].cell(2, 10, 999))
        # Подмена ДО хеширования имитирует дефект экспортёра, а не порчу файла позже.
        bundle = bundle_bytes(ctx, result, ['FOT-001'], input_sha256='a' * 64,
                              export_data_sha256=workbook_digest(raw))
        report = evaluate_bytes(bundle, hashlib.sha256(bundle).hexdigest(),
                                input_sha256='a' * 64, workbook_bytes=raw)
        self.assertEqual(report['export_audit']['verdict'], 'failed')
        self.assertEqual(report['verdict'], 'failed')


if __name__ == '__main__':
    unittest.main(verbosity=2)
