# -*- coding: utf-8 -*-
"""CAP-004: у обязательной проверки есть правило применимости и requirement ID.

Проверка результата сообщает состояние каждой карточки: passed, failed,
not_applicable с проверяемым основанием, not_run — обязательна, но не
реализована. Обязательный состав задаёт профиль каталога, а не факт наличия
проверки в коде; отсутствие реализации не делает проверку неприменимой.
Сервис передаёт состав решателю флагом --require и читает состояния обратно.

    .venv/Scripts/python.exe scripts/test_audit_applicability.py
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "harness"))

from fot_planner.harness_local.catalogue import Catalogue, CatalogueError  # noqa: E402
from fot_planner.models import PlanningResult  # noqa: E402
from fot_planner import result_audit as RA  # noqa: E402

RULES = ["RATE-001", "SALARY-001", "PAYMENT-001", "LABOR-001", "PERSON-001", "FOT-001"]


class Ctx:
    def __init__(self, employees=(), contracts=None, labor_plans=None):
        self.year = 2026
        self.employees = list(employees)
        if contracts is not None:
            self.contracts = contracts
        if labor_plans is not None:
            self.labor_plans = labor_plans


def result(rates=()):
    from fot_planner.models import OpenRateAttribution
    res = PlanningResult(year=2026, allocations=[], deficits=[], conflicts=[],
                         contract_balances=[], solver_status="OPTIMAL", objective_value=0.0,
                         solve_time_sec=0.0)
    res.open_rate_attributions = [
        OpenRateAttribution(employee_id=e, contract_id=c, year=2026, month=m, open_rate=v,
                            is_main=True, position="Инженер") for e, c, m, v in rates]
    return res


def by_rule(report):
    return {c["rule_id"]: c for c in report.checks}


class AuditApplicabilityTests(unittest.TestCase):
    def test_empty_plan_reports_not_applicable_with_reasons_not_passed(self):
        report = RA.audit(Ctx(), result())
        self.assertEqual(report.status, "OK")
        states = by_rule(report)
        self.assertEqual(set(states), set(RULES))
        for rule in RULES:
            self.assertEqual(states[rule]["status"], "not_applicable", rule)
            self.assertTrue(states[rule]["reason"], "нет основания у " + rule)
        self.assertEqual(report.checked, [])
        self.assertIn("checks", report.as_dict())

    def test_applicable_check_is_passed_or_failed(self):
        from fot_planner.models import Employee
        emp = Employee(id="E1", full_name="Иванов И.И.", position="Инженер", rate=1.0,
                       monthly_wage=100000.0, department="Отдел")
        good = RA.audit(Ctx([emp]), result([("E1", "C1", 6, 0.75)]))
        self.assertEqual(by_rule(good)["RATE-001"]["status"], "passed")
        self.assertEqual(by_rule(good)["PERSON-001"]["status"], "passed")
        self.assertEqual(by_rule(good)["FOT-001"]["status"], "not_applicable")
        bad = RA.audit(Ctx([emp]), result([("E1", "C1", 6, 0.86)]))
        self.assertEqual(bad.status, "FAIL")
        self.assertEqual(by_rule(bad)["RATE-001"]["status"], "failed")

    def test_required_unimplemented_check_is_not_run_and_fails(self):
        report = RA.audit(Ctx(), result(), required=["RATE-001", "P4-001"])
        states = by_rule(report)
        self.assertEqual(states["P4-001"]["status"], "not_run")
        self.assertEqual(states["RATE-001"]["status"], "not_applicable")
        self.assertEqual(report.status, "FAIL")
        self.assertTrue(any(v.rule_id == "P4-001" and "не выполнена" in v.message
                            for v in report.violations))

    def test_check_lines_roundtrip_between_solver_and_service(self):
        report = RA.audit(Ctx(), result(), required=["P4-001"])
        lines = RA.format_checks(report.checks)
        parsed = [RA.parse_check_line(line) for line in lines]
        self.assertEqual([p["rule_id"] for p in parsed], [c["rule_id"] for c in report.checks])
        self.assertEqual([p["status"] for p in parsed], [c["status"] for c in report.checks])
        self.assertEqual([p["reason"] for p in parsed], [c["reason"] for c in report.checks])
        self.assertIsNone(RA.parse_check_line("Аудит: OK"))
        self.assertIsNone(RA.parse_check_line("  [RATE-001] нарушение"))


class CatalogueChecksTests(unittest.TestCase):
    def setUp(self):
        self.catalogue = Catalogue.load(ROOT)
        self.rows = [d.model_dump(mode="json") for d in self.catalogue.descriptors]

    def modified(self, key, **changes):
        rows = copy.deepcopy(self.rows)
        row = next(d for d in rows if d["id"] == key)
        row.update(changes)
        return Catalogue.from_documents(rows)

    def test_profile_lists_mandatory_checks_with_requirement_and_applicability(self):
        required = self.catalogue.required_checks("fot.local")
        self.assertEqual({c["requirement"] for c in required}, set(RULES))
        for c in required:
            self.assertTrue(c["applicability"], c["id"])
            self.assertEqual(c["handler"], "audit." + c["requirement"].lower())
            self.assertEqual(c["maturity"], "implemented")
        implemented = {rule for rule, _name, _fn in RA.CHECKS}
        self.assertEqual({c["requirement"] for c in required} - implemented, set())
        self.assertEqual(self.catalogue.required_checks("другая.область"), [])

    def test_check_in_catalogue_alone_is_not_required(self):
        rows = copy.deepcopy(self.rows)
        rows.append({"schema_version": 1, "kind": "check", "id": "check.p4-001", "version": "1.0.0",
                     "owner": "экономический отдел", "purpose": "Средняя П4 по категории",
                     "maturity": "declared", "domains": ["fot.local"], "requirement": "P4-001",
                     "applicability": "Применяется при наличии предельного размера П4 у категории."})
        catalogue = Catalogue.from_documents(rows)
        self.assertNotIn("P4-001", [c["requirement"] for c in catalogue.required_checks("fot.local")])

    def test_declared_check_in_profile_is_required_and_not_run_not_applicable(self):
        rows = copy.deepcopy(self.rows)
        rows.append({"schema_version": 1, "kind": "check", "id": "check.p4-001", "version": "1.0.0",
                     "owner": "экономический отдел", "purpose": "Средняя П4 по категории",
                     "maturity": "declared", "domains": ["fot.local"], "requirement": "P4-001",
                     "applicability": "Применяется при наличии предельного размера П4 у категории."})
        profile = next(d for d in rows if d["id"] == "profile.fot-local")
        profile["checks"] = profile["checks"] + [{"id": "check.p4-001", "version": "1.0.0"}]
        catalogue = Catalogue.from_documents(rows)
        required = catalogue.required_checks("fot.local")
        self.assertIn("P4-001", [c["requirement"] for c in required])
        report = RA.audit(Ctx(), result(), required=[c["requirement"] for c in required])
        self.assertEqual(by_rule(report)["P4-001"]["status"], "not_run")
        self.assertEqual(report.status, "FAIL")

    def test_untrusted_handler_and_missing_requirement_are_rejected(self):
        with self.assertRaises(CatalogueError):
            self.modified("check.rate-001", handler="os.system")
        with self.assertRaises(CatalogueError):
            self.modified("check.rate-001", handler="audit.fot-001")
        with self.assertRaises(CatalogueError):
            self.modified("check.rate-001", requirement=None)
        with self.assertRaises(CatalogueError):
            self.modified("check.rate-001", applicability=None)
        with self.assertRaises(CatalogueError):
            self.modified("check.rate-001", maturity="qualified")

    def test_profile_reference_must_be_a_check(self):
        with self.assertRaises(CatalogueError):
            self.modified("profile.fot-local", checks=[{"id": "template-kind-tool", "version": "1.0.0"}])
        with self.assertRaises(CatalogueError):
            self.modified("profile.fot-local", checks=[])

    def test_registered_schema_matches_runtime_with_checks(self):
        import json
        from fot_planner.harness_local.catalogue import schemas
        for name, schema in schemas().items():
            path = ROOT / "schemas/local-harness" / (name + ".schema.json")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), schema, name)


class ServiceWiringTests(unittest.TestCase):
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

    def test_solver_command_carries_catalogue_requirements(self):
        main = self.app.main
        self.assertEqual(set(main._audit_requirements()), set(RULES))
        command = main._solver_command("in.xlsx", "out.xlsx", "240")
        self.assertIn("--require", command)
        self.assertEqual(set(command[command.index("--require") + 1].split(",")), set(RULES))

    def test_service_reads_check_states_from_solver_output(self):
        main = self.app.main
        parsed = main._read_solver_output(
            "Статус: OPTIMAL\nАудит: FAIL\n  [P4-001] обязательная проверка P4-001 не выполнена\n"
            "Проверка: RATE-001 passed\nПроверка: FOT-001 not_applicable — во входе нет договоров\n"
            "Проверка: P4-001 not_run — обязательная проверка не реализована в аудите\n")
        self.assertEqual((parsed["solver_status"], parsed["audit_status"]), ("OPTIMAL", "FAIL"))
        self.assertEqual(parsed["audit_lines"], ["[P4-001] обязательная проверка P4-001 не выполнена"])
        self.assertEqual([(c["rule_id"], c["status"]) for c in parsed["checks"]],
                         [("RATE-001", "passed"), ("FOT-001", "not_applicable"), ("P4-001", "not_run")])
        self.assertEqual(parsed["checks"][1]["reason"], "во входе нет договоров")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
