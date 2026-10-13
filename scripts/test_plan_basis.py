"""SOURCE-003 и UI-005: план выбирает договоры и версии документов из реестра.

Стенд на временной базе, без сервера, модели и решателя. Проверяет, что снятый
договор и выбранная прежняя версия меняют вход только своего плана, реестр не
переписывается, каталог собирает документы по договорам, а выбор оставляет
решение и отметку пересмотра. Интерфейс реестра проверяется по исходникам;
внешний вид — вручную на контрольных ширинах (docs/ИНТЕРФЕЙС.md).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "harness"))


class PlanBasisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lifecycle import App
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, FOT_OPERATOR="basis-test-operator")
        cls.env.start()
        cls.app = App(cls.temp.name)
        import basis
        cls.basis = basis

    @classmethod
    def tearDownClass(cls):
        cls.app.db.close()
        cls.app.d.engine.dispose()
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.db, self.d, self.main = self.app.db, self.app.d, self.app.main
        d = self.d
        for model in (d.Employee, d.Contract, d.LaborRow, d.Inflow, d.SecretAllowance,
                      d.Substitution, d.Proposal, d.Document, d.Decision, d.Message, d.Run,
                      d.SalaryMonth, d.SalaryRow):
            self.db.query(model).delete()
        self.db.query(d.Case).filter(d.Case.id != self.app.case.id).delete()
        self.case = self.app.case
        self.case.plan_basis = None
        self.case2 = d.Case(title="Соседний вариант", year=2026, stage="сбор данных")
        self.db.add(self.case2)
        self.db.commit()

    # ── реестр стенда ──
    def doc(self, name, version=1, supersedes=None, contract=None, state="разобран",
            kind="документ по договору", scope="реестр"):
        row = self.d.Document(name=name, path="нет", state=state, kind=kind, version=version,
                              supersedes_id=supersedes, scope=scope, contract_code=contract)
        self.db.add(row)
        self.db.commit()
        return row

    def contract(self, doc, code, fund):
        self.db.add(self.d.Contract(code=code, name="Договор " + code, fund=fund,
                                    date_from="01.01.2026", date_to="31.12.2026",
                                    document_id=doc.id))

    def labor(self, doc, code, months):
        self.db.add(self.d.LaborRow(contract_code=code, year=2026, position="Инженер",
                                    person_months=months, avg_cost=150000, headcount=1,
                                    document_id=doc.id))

    def inflow(self, doc, code, amount):
        self.db.add(self.d.Inflow(contract_code=code, year=2026, month=1, amount=amount,
                                  document_id=doc.id))

    def employee(self, doc, code, fio, allowed=None):
        self.db.add(self.d.Employee(code=code, person_code=code, fio=fio, position="Инженер",
                                    rate=1.0, salary=100000, date_from="01.01.2026",
                                    date_to="31.12.2026", allowed_contracts=allowed,
                                    document_id=doc.id))
        # Зарплата — из таблицы по месяцам (правило 1, 19.09.2026): в штатном
        # расписании её нет, без сумм месяцев вход не собирается.
        self.db.add(self.d.SalaryRow(employee_code=code, year=2026, source="стенд"))
        for month in range(1, 13):
            self.db.add(self.d.SalaryMonth(employee_code=code, year=2026, month=month,
                                           amount=100000, source="стенд"))

    def registry(self):
        ctr = self.doc("Договоры 2026.xlsx")
        self.contract(ctr, "C_A", 1200000)
        self.contract(ctr, "C_B", 2400000)
        self.inflow(ctr, "C_A", 600000)
        self.inflow(ctr, "C_B", 1200000)
        rkm = self.doc("РКМ А.xlsx")
        self.labor(rkm, "C_A", 4.0)
        staff = self.doc("Штатка.xlsx", kind="штатное расписание")
        self.employee(staff, "E1", "Иванов", "C_A")
        self.employee(staff, "E2", "Петров", "C_A; C_B")
        self.employee(staff, "E3", "Сидоров")
        self.db.add(self.d.SecretAllowance(employee_code="E1", secret_contract_code="C_A",
                                           rate=0.1, document_id=staff.id))
        self.db.commit()
        return ctr, rkm, staff

    # ── реестр без плана (план 0) ──
    def test_catalog_without_plan_has_no_choice_but_full_shape(self):
        """Библиотека открывается без плана: каталог и пакеты те же, выбора нет.

        Запасной словарь основания однажды отстал от read() на ключ «договоры»,
        и реестр отвечал 500 — форма основания должна быть одна.
        """
        self.registry()
        got = self.basis.catalog(self.db, None)
        self.assertEqual((got["plan"], got["excluded"], got["overrides"]), (None, [], {}))
        self.assertEqual(set(got["contracts"]), {"C_A", "C_B"})
        self.assertEqual(got["overridable"], list(self.basis.OVERRIDE))

    # ── условия договора задаёт план (SOURCE-006) ──
    def test_override_reaches_input_of_its_plan_only(self):
        """Соседний план обязан видеть договор таким, какой он в реестре."""
        self.registry()
        self.assertTrue(self.basis.set_overrides(
            self.db, self.case, "C_A", {"разрешенные выплаты": "оклад, 120"}, "стенд"))
        mine = self.main._registry_data(self.db, self.case, [])
        other = self.main._registry_data(self.db, self.case2, [])
        self.assertEqual({c.code: c.kinds for c in mine["contracts"]}["C_A"], "оклад, 120")
        self.assertIsNone({c.code: c.kinds for c in other["contracts"]}["C_A"])
        # Реестр не переписан: правка живёт у плана, а не у договора.
        self.assertIsNone(self.db.query(self.d.Contract)
                          .filter_by(code="C_A").first().kinds)

    # ── строка штатного расписания в плане (30.09.2026) ──
    def test_employee_override_reaches_input_of_its_plan_only(self):
        """«Для нового плана возможно они захотят менять штатное расписание и
        с разными зарплатами пересчитывать» (пользователь 30.09.2026): ставка,
        срок и зарплата месяцев — у плана, реестр и соседний план не меняются."""
        self.registry()
        self.assertTrue(self.basis.set_employee_overrides(
            self.db, self.case, "E1",
            {"ставка": "0,5", "окончание": "30.06.2026", "зарплата": {"3": "60 000", 4: 70000}},
            "стенд"))
        warn = []
        mine = self.main._registry_data(self.db, self.case, warn)
        other = self.main._registry_data(self.db, self.case2, [])
        e1 = {e.code: e for e in mine["employees"]}["E1"]
        self.assertEqual((e1.rate, e1.date_to), (0.5, "30.06.2026"))
        self.assertEqual({e.code: e.rate for e in other["employees"]}["E1"], 1.0)
        pay = {(c, m): a for c, m, a in mine["salary_months"]}
        self.assertEqual((pay[("E1", 3)], pay[("E1", 4)], pay[("E1", 5)]), (60000.0, 70000.0, 100000))
        self.assertEqual({(c, m): a for c, m, a in other["salary_months"]}[("E1", 3)], 100000)
        # Реестр не переписан, правка названа в допущениях.
        self.assertEqual(self.db.query(self.d.Employee).filter_by(code="E1").first().rate, 1.0)
        self.assertTrue(any("E1" in w and "ставка" in w for w in warn), warn)
        self.assertTrue(any("Зарплата по месяцам в этом плане" in w for w in warn), warn)
        # Основание отдаёт правки экрану.
        got = self.basis.catalog(self.db, self.case)["staff_overrides"]["E1"]
        self.assertEqual((got["ставка"], got["зарплата"]["3"]), ("0,5", 60000.0))

    def test_employee_override_is_cleared_by_empty_value_and_checked(self):
        self.registry()
        self.basis.set_employee_overrides(self.db, self.case, "E1",
                                          {"ставка": "0,5", "зарплата": {"3": "60000"}}, "стенд")
        self.assertTrue(self.basis.set_employee_overrides(
            self.db, self.case, "E1", {"ставка": "", "зарплата": {"3": ""}}, "стенд"))
        self.assertEqual(self.basis.read(self.case)["сотрудники"], {})
        for bad in ({"ставка": "три"}, {"ставка": "3"}, {"начало": "31.02.2026"},
                    {"зарплата": {"1": "-5"}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.basis.set_employee_overrides(self.db, self.case, "E1", bad, "стенд")

    def test_excluded_employee_leaves_only_this_plan_input(self):
        """«Добавь опцию выбора галочками нужных сотрудников, вдруг уволить
        захотят кого-то» (пользователь 30.09.2026): снятая строка уходит из
        входа своего плана вместе с надбавкой 120, суммами месяцев и
        закреплениями; реестр и соседний план её видят, вернул — вернулись и
        закрепления."""
        self.registry()
        self.basis.set_employee_overrides(self.db, self.case, "E1",
                                          {"зарплата": {"3": "60000"}}, "стенд")
        self.assertTrue(self.basis.set_staff_included(self.db, self.case, ["E1"], False, "стенд"))
        pin = {"с": 1, "по": 12, "вид": "любой", "сумма": None}
        self.case.plan_settings = json.dumps({
            "назначения": [{**pin, "сотрудник": "E1", "фио": "Иванов", "договор": "C_A"},
                           {**pin, "сотрудник": "E2", "фио": "Петров", "договор": "C_A"}],
            "запреты": [{**pin, "сотрудник": "E1", "фио": "Иванов", "договор": "C_B"}]},
            ensure_ascii=False)
        self.addCleanup(setattr, self.case, "plan_settings", None)
        self.db.commit()
        warn = []
        data = self.main._registry_data(self.db, self.case, warn)
        self.main._apply_plan_settings(data, self.case, warn)
        self.assertEqual({e.code for e in data["employees"]}, {"E2", "E3"})
        self.assertEqual(data["secret"], [])
        self.assertNotIn("E1", {c for c, _m, _a in data["salary_months"]})
        self.assertEqual([f["сотрудник"] for f in data["manual_assignments"]], ["E2"])
        self.assertEqual(data["manual_prohibitions"], [])
        self.assertIn("Сотрудники вне расчёта этого плана: Иванов.", warn)
        self.assertIn("Закрепления сотрудников вне расчёта плана не учтены: Иванов.", warn)
        # Правка зарплаты снятой строки в допущениях не называется: её нет во входе.
        self.assertFalse(any("Зарплата по месяцам в этом плане" in w for w in warn), warn)
        self.assertEqual(self.basis.catalog(self.db, self.case)["staff_excluded"], ["E1"])

        # Вход собирается и не ссылается на снятую строку.
        from fot_planner.excel.load import load_context
        import build_input
        import reference
        src = os.path.join(self.temp.name, "staff_off_input.xlsx")
        build_input.build(reference.TEMPLATE, src, data, warn)
        ctx = load_context(src)
        self.assertEqual({e.id for e in ctx.employees}, {"E2", "E3"})
        self.assertEqual({a.employee_id for a in ctx.manual_assignments}, {"E2"})

        other = self.main._registry_data(self.db, self.case2, [])
        self.assertEqual({e.code for e in other["employees"]}, {"E1", "E2", "E3"})
        self.assertEqual(self.db.query(self.d.Employee).filter_by(code="E1").count(), 1)

        self.assertTrue(self.basis.set_staff_included(self.db, self.case, ["E1"], True, "стенд"))
        data = self.main._registry_data(self.db, self.case, [])
        self.main._apply_plan_settings(data, self.case)
        self.assertEqual(sorted(f["сотрудник"] for f in data["manual_assignments"]), ["E1", "E2"])
        self.assertIn("E1", {e.code for e in data["employees"]})

    def test_staff_inclusion_records_decision_and_marks_only_own_runs(self):
        self.registry()
        own = self.d.Run(case_id=self.case.id, status="OPTIMAL", settings="{}")
        other = self.d.Run(case_id=self.case2.id, status="OPTIMAL", settings="{}")
        self.db.add_all([own, other])
        self.db.commit()
        self.assertTrue(self.basis.set_staff_included(self.db, self.case, ["E1"], False, "стенд"))
        self.db.expire_all()
        self.assertEqual(self.db.get(self.d.Run, own.id).review, "review_required")
        self.assertNotEqual(self.db.get(self.d.Run, other.id).review, "review_required")
        rows = (self.db.query(self.d.Decision)
                .filter_by(kind="состав плана", subject_id=str(self.case.id)).all())
        self.assertEqual([json.loads(r.action) for r in rows],
                         [{"сотрудники": ["E1"], "в расчёте": False}])
        # Тот же выбор повторно — не решение.
        self.assertFalse(self.basis.set_staff_included(self.db, self.case, ["E1"], False, "стенд"))
        self.assertEqual(self.db.query(self.d.Decision).count(), 1)

    def test_override_remembers_registry_and_names_orphans(self):
        """Синхронизация с реестром (30.09.2026): правка помнит, поверх какого
        значения реестра она сделана, — экран покажет, что реестр с тех пор
        изменился; правки строк, которых в реестре уже нет, называются."""
        self.registry()
        self.basis.set_employee_overrides(self.db, self.case, "E1",
                                          {"ставка": "0,5", "зарплата": {"3": "60000"}}, "стенд")
        got = self.basis.catalog(self.db, self.case)["staff_overrides"]["E1"]
        self.assertEqual(got["реестр"], {"ставка": "1", "м3": 100000.0})
        # Реестр изменился — правка по-прежнему у плана, «реестр» прежний.
        self.db.query(self.d.Employee).filter_by(code="E1").update({"rate": 0.75})
        self.db.commit()
        self.assertEqual(self.basis.catalog(self.db, self.case)["staff_overrides"]["E1"]
                         ["реестр"]["ставка"], "1")
        # Снятая правка забывает и значение реестра.
        self.basis.set_employee_overrides(self.db, self.case, "E1", {"ставка": ""}, "стенд")
        got = self.basis.catalog(self.db, self.case)["staff_overrides"]["E1"]
        self.assertEqual(got["реестр"], {"м3": 100000.0})
        # Строка ушла из реестра — правка и флажок её называются.
        self.basis.set_staff_included(self.db, self.case, ["E9"], False, "стенд")
        self.assertEqual(self.basis.catalog(self.db, self.case)["staff_orphans"], ["E9"])
        warn = []
        self.main._registry_data(self.db, self.case, warn)
        self.assertTrue(any("E9" in w and "нет в штатном расписании" in w for w in warn), warn)

    def test_override_equal_to_registry_is_removed_automatically(self):
        """«Реестр сравнялся с правкой плана — снимать правку автоматически»
        (пользователь 30.09.2026): новая штатка принесла ту же ставку — правки
        больше нет, дальше план идёт за реестром. Расчёт от этого не меняется
        и к пересмотру не помечается; снятое — в журнале и в допущениях."""
        self.registry()
        self.basis.set_employee_overrides(
            self.db, self.case, "E1",
            {"ставка": "0,5", "окончание": "30.06.2026", "зарплата": {"3": "60000"}}, "стенд")
        self.basis.set_overrides(self.db, self.case, "C_A", {"окончание": "30.06.2026"}, "стенд")
        # Правка условия помнит, что было в договоре.
        self.assertEqual(self.basis.catalog(self.db, self.case)["overrides_base"],
                         {"C_A": {"окончание": "31.12.2026"}})
        own = self.d.Run(case_id=self.case.id, status="OPTIMAL", settings="{}")
        self.db.add(own)
        self.db.commit()
        # Реестр догнал план: ставка 0,5, март — 60 000, договор до 30.06.
        self.db.query(self.d.Employee).filter_by(code="E1").update({"rate": 0.5})
        self.db.query(self.d.SalaryMonth).filter_by(employee_code="E1", month=3).update(
            {"amount": 60000})
        self.db.query(self.d.Contract).filter_by(code="C_A").update({"date_to": "30.06.2026"})
        self.db.commit()
        warn = []
        self.main._registry_data(self.db, self.case, warn)
        chosen = self.basis.read(self.case)
        self.assertEqual(chosen["сотрудники"]["E1"],
                         {"окончание": "30.06.2026", "реестр": {"окончание": "31.12.2026"}})
        self.assertEqual((chosen["договоры"], chosen["условия реестра"]), ({}, {}))
        self.assertTrue(any("реестр сравнялся" in w and "E1: ставка" in w
                            and "C_A: окончание" in w for w in warn), warn)
        self.db.expire_all()
        self.assertNotEqual(self.db.get(self.d.Run, own.id).review, "review_required")
        self.assertEqual(self.basis.dissolve_caught_up(self.db, self.case), [])
        # Экран получает снятое вместе с основанием плана.
        self.db.query(self.d.Employee).filter_by(code="E1").update({"date_to": "30.06.2026"})
        self.db.commit()
        out = self.main._basis_out(self.db, self.case)
        self.assertEqual(out["dissolved"], ["E1: окончание"])
        self.assertNotIn("E1", out["staff_overrides"])
        acts = [json.loads(r.action) for r in self.db.query(self.d.Decision)
                .filter_by(kind="состав плана", subject_id=str(self.case.id)).all()]
        self.assertIn({"сняты правки: реестр сравнялся": ["E1: окончание"]}, acts)

    def test_override_is_named_in_assumptions(self):
        """Молчаливое переопределение неотличимо от данных документа."""
        self.registry()
        self.basis.set_overrides(self.db, self.case, "C_A", {"окончание": "30.06.2026"}, "стенд")
        warn = []
        self.main._registry_data(self.db, self.case, warn)
        self.assertTrue(any("C_A" in w and "30.06.2026" in w for w in warn), warn)

    def test_empty_value_clears_override(self):
        """Пустое поле возвращает реестровое значение, а не пишет пустоту."""
        self.registry()
        self.basis.set_overrides(self.db, self.case, "C_A", {"начало": "01.03.2026"}, "стенд")
        self.assertTrue(self.basis.set_overrides(
            self.db, self.case, "C_A", {"начало": ""}, "стенд"))
        self.assertEqual(self.basis.read(self.case)["договоры"], {})
        data = self.main._registry_data(self.db, self.case, [])
        self.assertEqual({c.code: c.date_from for c in data["contracts"]}["C_A"], "01.01.2026")

    def test_requisite_is_not_overridable(self):
        """Реквизит — свойство договора: номер планом не переопределяется."""
        self.registry()
        self.assertFalse(self.basis.set_overrides(
            self.db, self.case, "C_A", {"номер": "17/26", "приоритет": "1"}, "стенд"))
        self.assertEqual(self.basis.read(self.case)["договоры"], {})

    def test_override_marks_only_own_runs_for_review(self):
        """Условия изменились — прежний расчёт этого плана больше не актуален,
        а соседнего плана правка не касается."""
        self.registry()
        own = self.d.Run(case_id=self.case.id, status="OPTIMAL", settings="{}")
        other = self.d.Run(case_id=self.case2.id, status="OPTIMAL", settings="{}")
        self.db.add_all([own, other])
        self.db.commit()
        self.basis.set_overrides(self.db, self.case, "C_A", {"основное": "нет"}, "стенд")
        self.db.expire_all()
        self.assertEqual(self.db.get(self.d.Run, own.id).review, "review_required")
        self.assertNotEqual(self.db.get(self.d.Run, other.id).review, "review_required")
        self.assertIn("C_A", self.db.get(self.d.Run, own.id).review_log or "")

    # ── договор снят с расчёта ──
    def test_excluded_contract_leaves_only_this_plan_input(self):
        self.registry()
        self.assertTrue(self.basis.set_included(self.db, self.case, ["C_A"], False, "стенд"))
        warn = []
        data = self.main._registry_data(self.db, self.case, warn)
        self.assertEqual({c.code for c in data["contracts"]}, {"C_B"})
        self.assertEqual({r.contract_code for r in data["inflows"]}, {"C_B"})
        self.assertEqual(data["labor"], [])
        self.assertEqual(data["secret"], [])
        people = {e.code: e for e in data["employees"]}
        self.assertEqual(people["E2"].allowed_contracts, "C_B")
        # Пустой перечень разрешённых значит «можно всюду» — сотруднику
        # единственного снятого договора остальные договоры запрещаются.
        self.assertIsNone(people["E1"].allowed_contracts)
        self.assertEqual(people["E1"].forbidden_contracts, "C_B")
        self.assertIsNone(people["E3"].forbidden_contracts)
        self.assertTrue(any("C_A" in w for w in warn), warn)

        other = self.main._registry_data(self.db, self.case2)
        self.assertEqual({c.code for c in other["contracts"]}, {"C_A", "C_B"})
        self.assertEqual(len(other["labor"]), 1)

        # Вход собирается и не ссылается на снятый договор.
        from fot_planner.excel.load import load_context
        from fot_planner.validation import validate_context
        import build_input
        import reference
        src = os.path.join(self.temp.name, "basis_input.xlsx")
        build_input.build(reference.TEMPLATE, src, data, warn)
        unknown = [c.message for c in validate_context(load_context(src))
                   if c.code == "UNKNOWN_CONTRACT"]
        self.assertEqual(unknown, [])

        # Реестр не переписан копиями для плана.
        self.db.commit()
        self.db.expire_all()
        e1 = self.db.query(self.d.Employee).filter_by(code="E1").one()
        self.assertEqual((e1.allowed_contracts, e1.forbidden_contracts), ("C_A", None))
        self.assertEqual(self.db.query(self.d.Contract).count(), 2)

    # ── прежняя версия документа ──
    def two_versions(self):
        v1 = self.doc("Договоры 2026.xlsx")
        self.contract(v1, "C_A", 1000000)
        self.inflow(v1, "C_A", 500000)
        self.db.commit()
        self.main._retire_document(self.db, v1, new_version=2)
        v2 = self.doc("Договоры 2026.xlsx", version=2, supersedes=v1.id)
        self.contract(v2, "C_A", 1500000)
        self.inflow(v2, "C_A", 750000)
        self.db.commit()
        return v1, v2

    def test_previous_version_feeds_only_choosing_plan(self):
        v1, v2 = self.two_versions()
        funds = lambda data: [c.fund for c in data["contracts"]]
        self.assertEqual(funds(self.main._registry_data(self.db, self.case)), [1500000])

        self.assertTrue(self.basis.set_version(self.db, self.case, v1.id, v1.id, "стенд"))
        data = self.main._registry_data(self.db, self.case, [])
        self.assertEqual(funds(data), [1000000])
        self.assertEqual([r.amount for r in data["inflows"]], [500000])
        self.assertEqual({c.document_id for c in data["contracts"]}, {v1.id})
        self.assertEqual(funds(self.main._registry_data(self.db, self.case2)), [1500000])
        sources = self.main._run_sources(self.db, data)
        self.assertEqual([d["id"] for d in sources["документы"]], [v1.id])

        self.db.commit()
        self.db.expire_all()
        self.assertEqual([c.fund for c in self.db.query(self.d.Contract).all()], [1500000])

        # Выбор последней версии не записывается: план снова идёт за последней.
        self.assertTrue(self.basis.set_version(self.db, self.case, v1.id, v2.id, "стенд"))
        self.assertEqual(self.basis.read(self.case)["версии"], {})

    def test_version_without_snapshot_cannot_be_chosen(self):
        old = self.doc("Штатка.xlsx", state="заменен", kind="штатное расписание")
        new = self.doc("Штатка.xlsx", version=2, supersedes=old.id, kind="штатное расписание")
        with self.assertRaises(ValueError):
            self.basis.set_version(self.db, self.case, old.id, old.id, "стенд")
        entry = self.basis.catalog(self.db, self.case)["staff"][0]
        self.assertEqual([(v["id"], v["selectable"]) for v in entry["versions"]],
                         [(new.id, True), (old.id, False)])
        self.assertEqual(entry["used"], new.id)

    # ── каталог ──
    def test_catalog_groups_documents_by_contract(self):
        self.registry()
        self.doc("Письмо заказчика.pdf", contract="C_B", kind=None)
        self.doc("Приказ 2556.doc", kind="нормативный документ")
        self.doc("скан.pdf", state="не прочитан", kind=None)
        self.doc("Черновик плана.xlsx", scope="план")
        cat = self.basis.catalog(self.db, self.case)
        names = lambda entries: sorted(e["name"] for e in entries)
        self.assertEqual(names(cat["contracts"]["C_A"]), ["Договоры 2026.xlsx", "РКМ А.xlsx"])
        self.assertEqual(names(cat["contracts"]["C_B"]),
                         ["Договоры 2026.xlsx", "Письмо заказчика.pdf"])
        # Надбавка 120 ссылается на C_A, но штатка — не документ договора.
        self.assertEqual(names(cat["staff"]), ["Штатка.xlsx"])
        self.assertEqual(names(cat["reference"]), ["Приказ 2556.doc"])
        self.assertEqual(names(cat["unlinked"]), ["скан.pdf"])
        shared = [e for e in cat["contracts"]["C_A"] if e["name"] == "Договоры 2026.xlsx"][0]
        self.assertEqual(shared["contracts"], ["C_A", "C_B"])
        self.assertEqual(shared["versions"][0]["produced"], {"договоров": 2, "поступлений": 2})

        v1, v2 = self.two_versions()
        entry = [e for e in self.basis.catalog(self.db, self.case)["contracts"]["C_A"]
                 if e["root"] == v1.id][0]
        self.assertEqual([(v["id"], v["selectable"]) for v in entry["versions"]],
                         [(v2.id, True), (v1.id, True)])
        self.assertEqual(entry["versions"][1]["produced"], {"договоров": 1, "поступлений": 1})

    # ── решение и пересмотр ──
    def test_basis_change_records_decision_and_marks_only_own_runs(self):
        self.registry()
        own = self.d.Run(case_id=self.case.id, status="OPTIMAL", settings="{}")
        other = self.d.Run(case_id=self.case2.id, status="OPTIMAL", settings="{}")
        self.db.add_all([own, other])
        self.db.commit()
        self.assertTrue(self.basis.set_included(self.db, self.case, ["C_A"], False, "стенд"))
        self.db.expire_all()
        self.assertEqual(self.db.get(self.d.Run, own.id).review, "review_required")
        self.assertNotEqual(self.db.get(self.d.Run, other.id).review, "review_required")
        rows = (self.db.query(self.d.Decision)
                .filter_by(kind="состав плана", subject_id=str(self.case.id)).all())
        self.assertEqual([json.loads(r.action) for r in rows],
                         [{"договоры": ["C_A"], "в расчёте": False}])
        self.assertEqual(rows[0].actor, "basis-test-operator")
        # Тот же выбор повторно — не решение и не новая отметка.
        self.assertFalse(self.basis.set_included(self.db, self.case, ["C_A"], False, "стенд"))
        self.assertEqual(self.db.query(self.d.Decision).count(), 1)

    # ── удаление версии ──
    def test_deleting_last_version_restores_previous_rows(self):
        v1 = self.doc("РКМ А.xlsx")
        self.labor(v1, "C_A", 4.0)
        self.db.commit()
        self.main._retire_document(self.db, v1, new_version=2)
        v2 = self.doc("РКМ А.xlsx", version=2, supersedes=v1.id)
        self.labor(v2, "C_A", 6.0)
        self.db.commit()
        self.main._forget_document(self.db, v2)
        self.db.expire_all()
        self.assertEqual([(r.person_months, r.document_id)
                          for r in self.db.query(self.d.LaborRow).all()], [(4.0, v1.id)])
        self.assertEqual(self.db.get(self.d.Document, v1.id).state, "разобран")
        self.assertEqual([d.id for d in self.basis.chains(self.db)[v1.id]], [v1.id])

    def test_deleting_middle_version_keeps_chain(self):
        v1 = self.doc("Штатка.xlsx", kind="штатное расписание")
        self.main._retire_document(self.db, v1, new_version=2)
        v2 = self.doc("Штатка.xlsx", version=2, supersedes=v1.id, kind="штатное расписание")
        self.main._retire_document(self.db, v2, new_version=3)
        v3 = self.doc("Штатка.xlsx", version=3, supersedes=v2.id, kind="штатное расписание")
        self.main._forget_document(self.db, v2)
        self.db.expire_all()
        self.assertEqual(self.db.get(self.d.Document, v3.id).supersedes_id, v1.id)
        self.assertEqual([d.id for d in self.basis.chains(self.db)[v1.id]], [v3.id, v1.id])

    # ── загрузка в договор ──
    def test_upload_into_contract_looks_for_versions_in_that_contract(self):
        rkm = self.doc("РКМ.xlsx")
        self.labor(rkm, "C_A", 4.0)
        letter = self.doc("Письмо.pdf", contract="C_B", kind=None)
        self.db.commit()
        self.assertEqual(self.main._previous_version(self.db, "РКМ.xlsx", "C_A").id, rkm.id)
        self.assertIsNone(self.main._previous_version(self.db, "РКМ.xlsx", "C_B"))
        self.assertEqual(self.main._previous_version(self.db, "РКМ.xlsx").id, rkm.id)
        self.assertEqual(self.main._previous_version(self.db, "Письмо.pdf", "C_B").id, letter.id)
        self.assertIsNone(self.main._previous_version(self.db, "Письмо.pdf", "C_A"))

    # ── лента плана после расчёта (02.10.2026) ──
    def test_review_notice_goes_to_feed_once_per_run(self):
        """Шесть одинаковых «Расчёт № 1 требует пересмотра» подряд на одну
        правку договора: реплика — когда прогон впервые потерял актуальность,
        следующие причины только дописываются в его журнал."""
        import actuality
        run = self.d.Run(case_id=self.case.id, status="OPTIMAL", settings="{}")
        self.db.add(run)
        self.db.commit()
        for reason in ("изменены виды выплат C_BASE-26", "изменён ФОТ C_BASE-26"):
            actuality.mark_review(self.db, [run], reason)
            self.db.commit()
        notes = [m for m in self.db.query(self.d.Message).filter_by(case_id=self.case.id)
                 if m.payload and json.loads(m.payload).get("kind") == "review_required"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(len(json.loads(run.review_log)), 2)

    def test_offline_question_gets_last_run_state_not_command_parser(self):
        """«решения нет?» без модели получало «Не понял правку… LLM-слой
        отключён»: теперь — состояние последнего расчёта и причина без модели."""
        run = self.d.Run(case_id=self.case.id, status="нет решения", settings="{}",
                         summary=json.dumps({"анализ": ["Мешает закрепление плана."]},
                                            ensure_ascii=False))
        self.db.add(run)
        self.db.commit()
        text = self.main._offline_reply(self.db, self.case, "нет ключа")
        self.assertIn("Расчёт № %d — решения нет" % run.id, text)
        self.assertIn("Мешает закрепление плана.", text)
        self.assertIn("Модель недоступна (нет ключа)", text)
        self.assertNotIn("Не понял правку", text)
        extract = self.main.intake.extract
        self.assertNotIn("LLM-слой", extract.apply_chat({"employees": [], "contracts": []},
                                                          "решения нет?")[0])

    def test_infeasible_analysis_names_the_blocking_pin(self):
        """«Вся зарплата» Зайцева с C_BASE-26 запрещала ему C_GOZ-26, а разбор
        говорил о деньгах и сроках. Опыт снимает закрепления по одному и
        называет то, без которого план есть; мягкий выход — первым."""
        pin = {"сотрудник": "E008", "фио": "Зайцев Захар Захарович", "договор": "C_BASE-26",
               "с": 1, "по": 12, "вид": "любой", "сумма": None, "только": True}
        self.case.plan_settings = json.dumps({"назначения": [pin]}, ensure_ascii=False)
        self.addCleanup(setattr, self.case, "plan_settings", None)
        run = self.d.Run(case_id=self.case.id, status="нет решения", settings="{}")
        self.db.add(run)
        self.db.commit()
        labels = [name for name, _ in self.main._pin_trials(self.db, self.main.chat.plan_settings(self.case))]
        self.assertEqual(labels[0], "у закрепления Зайцев Захар Захарович на C_BASE-26, "
                                    "январь–декабрь снять «вся зарплата»")
        self.assertTrue(labels[1].startswith("снять закрепление Зайцев"))

        def solves(db, case, run, ps, tag):
            return not any(f.get("только") for f in ps.get("назначения") or [])
        with patch.object(self.main, "_solves_with", solves):
            self.assertEqual(self.main._try_without_pins(self.db, self.case, run), labels[0])
        with patch.object(self.main, "_solves_with", lambda *a: False):
            self.assertIsNone(self.main._try_without_pins(self.db, self.case, run))

    def test_readiness_does_not_require_inflows(self):
        """Поступления в расчёт не подаются (правило 21): «Для расчета не
        хватает: поступления по месяцам» после каждого файла — ложная тревога."""
        extract = self.main.intake.extract
        ready = extract._readiness({"employees": [{}], "contracts": [{}], "inflow": []})
        self.assertTrue(ready["ok"], ready)
        # Реестр стенда: люди и договоры без поступлений.
        self.db.add(self.d.Employee(code="E1", fio="Иванов И. И.", position="Инженер", rate=1.0))
        self.db.add(self.d.Contract(code="C1", name="Договор"))
        self.db.commit()
        self.assertTrue(self.main._registry_ready(self.db))


JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "static" / "app.css").read_text(encoding="utf-8")


def between(text, start, end):
    i = text.index(start)
    return text[i:text.index(end, i + len(start))]


class PlanStaffUiTests(unittest.TestCase):
    """Вкладки плана «Штатное расписание» и «Заработная плата по месяцам»
    (30.09.2026): ставка и срок, суммы месяцев правятся в плане поверх
    реестра; правка — акцентом, пустое или равное реестровому снимает её,
    стрелка возвращает строку своей вкладки. Не одна вкладка «Сотрудники»:
    «это не штатка и не зарплата — гибрид, им не понравится»."""
    JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    HTML = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")

    def body(self, name):
        start = self.JS.index("function %s(" % name)
        return self.JS[start:self.JS.index("\nfunction ", start + 1)]

    def test_tab_and_table(self):
        self.assertIn('data-view="emp">Штатное расписание</button>', self.HTML)
        self.assertIn('data-view="zp">Заработная плата по месяцам</button>', self.HTML)
        self.assertNotIn(">Сотрудники</button>", self.HTML)
        self.assertLess(self.HTML.index('data-view="ctr"'), self.HTML.index('data-view="emp"'))
        self.assertLess(self.HTML.index('data-view="emp"'), self.HTML.index('data-view="zp"'))
        # «Проекты приказов» с 30.09.2026 берут свой список по расчёту
        # (loadOrders), а не строки реестра — в этой ветке их нет.
        self.assertIn('var need = v === "ctr" || v === "emp" || v === "zp"', self.JS)
        table = self.body("staffPlanTable")
        self.assertIn("(vbasis && vbasis.staff_overrides)", table)
        self.assertIn('(has ? " data-set" : "")', table)
        self.assertIn('iconBtn("", "data-empreset", esc(e.code + "|" + kind)', table)
        self.assertIn('var zp = kind === "zp";', table)

    def test_inclusion_checkbox_like_contracts(self):
        """Флажок «в расчёте» у строки — на «Штатном расписании» плана, как у
        договоров; снятая строка тише и на «Заработной плате»."""
        table = self.body("staffPlanTable")
        self.assertIn("var off = staffOffMap();", table)
        self.assertIn('if (!zp) cells.unshift({ v: \'<input type="checkbox" data-empincl="\'', table)
        self.assertIn("data-empall", table)
        self.assertIn('rowCls: function (i) { return off[emps[i].code] ? "off" : ""; }', table)
        # Строка «В расчёте N из M» — в шапке панели таблицы, как в реестре
        # (пользователь 30.09.2026: «все таблицы в одном стиле — как в реестре»).
        self.assertIn('"В расчёте " + left + " из " + emps.length', table)
        self.assertIn("'<div class=\"tbar\"><div class=\"preflight\">' + line", table)
        self.assertIn("(vbasis && vbasis.staff_excluded)", self.body("staffOffMap"))
        change = between(self.JS, '$("view").addEventListener("change"', "\n});")
        self.assertIn('basisLocal(ecodes, on, esel, "staff_excluded");', change)
        self.assertIn("setBasis({ employees: ecodes, included: on }, esel);", change)
        # Закрепления снятого — тише, в «Закрепить сотрудника» его нет.
        self.assertIn("var staffOff = staffOffMap();", self.body("fixesBlock"))
        self.assertIn("!taken[e.code] && !gone[e.code]", self.body("fixAdd"))
        self.assertIn(".ovprops .fixes tr[data-off] td", CSS)

    def test_changed_registry_is_flagged_and_equal_override_is_not_an_override(self):
        """«Реестр изменил то, что план уже переписал — подсвечивать» и «реестр
        сравнялся с правкой — снимать» (пользователь 30.09.2026: «ну сделай»).
        Проверка — настоящей отрисовкой staffPlanTable в node."""
        import subprocess
        consts = "".join(re.search(r"var %s = \[[^\]]+\];" % name, self.JS).group(0)
                         for name in ("MONTH_NAMES", "MONTH_SHORT", "EMP_FIELDS"))
        funcs = "".join(self.body(n) for n in ("esc", "cap", "mo", "iconBtn", "table",
                                                 "staffOffMap", "staffPlanTable"))
        script = (consts + "var ICON_RESET = '';" + funcs +
                  "var vdata = {employees: [{code: 'E1', fio: 'Иванов', position: 'Инженер',"
                  " rate: 0.75, from: '01.01.2026', to: '31.12.2026'},"
                  " {code: 'E2', fio: 'Петров', position: 'Инженер', rate: 1,"
                  " from: '01.01.2026', to: '31.12.2026'}], salary: {rows: []}};"
                  "var vbasis = {staff_excluded: [], staff_orphans: ['E9'], staff_overrides: {"
                  " E1: {'ставка': '0,5', 'реестр': {'ставка': '1'}},"
                  " E2: {'ставка': '1', 'реестр': {'ставка': '0,5'}}}};"
                  "var html = staffPlanTable('emp');"
                  "var cell = function (c) { var i = html.indexOf('data-empov=\"' + c +"
                  " '|ставка\"'); return i < 0 ? '' : html.slice(i, html.indexOf('>', i) + 1); };"
                  "console.log(JSON.stringify([cell('E1'), cell('E2'),"
                  " html.indexOf('data-emporphans') >= 0, html.indexOf('E9') >= 0]));")
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                             encoding="utf-8", check=True).stdout
        e1, e2, orphans_button, orphan_named = json.loads(out)
        # Реестр 0,75, при правке было 1, в плане 0,5: правка и пометка.
        self.assertIn("data-set", e1)
        self.assertIn("data-stale", e1)
        self.assertIn("при правке в реестре было 1", e1)
        # В плане 1 и в реестре 1 — это не правка: ни акцента, ни пометки.
        self.assertNotIn("data-set", e2)
        self.assertNotIn("data-stale", e2)
        self.assertTrue(orphans_button and orphan_named)
        # Условие договора: изменившийся договор отмечен в графе «В договоре».
        cond = self.body("conditionsBlock")
        self.assertIn("(bs.overrides_base || {})", cond)
        self.assertIn("data-stale", cond)
        # Снятые сервером правки называет сообщение при загрузке вкладки.
        self.assertIn("Реестр сравнялся с правкой плана, правка снята", self.JS)

    def test_edit_posts_employee_fields_and_clears_equal_values(self):
        edit = self.body("empCellEdit")
        self.assertIn('body: JSON.stringify({ employee: code, fields: fields })', edit)
        self.assertIn("if (norm(value) === norm(base)) value = \"\";", edit)
        self.assertIn('fields["зарплата"][month[1]] = value;', edit)


class RegistryUiTests(unittest.TestCase):
    def test_staff_shows_current_account_and_links_to_contract(self):
        """Лицевой счёт сотрудника — графа штатного расписания, и она же путь
        в договор.

        Пользователь 28.09.2026: «в штатку добавь текущий лицевой счёт, потому
        что сотрудник привязан уже к какому-то, и можно его сделать
        гиперссылкой? …а если штатку раньше загрузили, чем договоры, то он
        должен сначала сделать просто текстом счёта, а потом обновлять и
        узнавать, можно ли подтянуть». Отсюда два состояния одной графы:
        договора с таким счётом нет — текст, появился — ссылка.
        """
        emp = between(JS, 'regTab === "emp"', 'regTab === "zp"')
        self.assertIn('{ k: "account", label: "Лицевой счёт",', emp)
        self.assertIn("var code = ctrByAccount()[acctKey(x.account)];", emp)
        self.assertIn("""'<button type="button" data-ctropen="' + esc(code)""", emp)
        self.assertIn(": esc(x.account) };", emp)
        # Разметку ячейка отдаёт объектом { v }: строкой движок её экранирует,
        # и кнопка вышла бы текстом.
        self.assertIn("return { v: code", emp)
        # Соответствие строится из реестра договоров на каждую отрисовку,
        # поэтому загрузка договоров сама превращает текст в ссылку.
        table = between(JS, "function ctrByAccount() {", "\n}")
        self.assertIn("(d.contracts || []).forEach", table)
        self.assertIn("acctKey(c.account)", table)
        # Счёт людьми пишется по-разному: сравниваем без пробелов и регистра.
        key = between(JS, "function acctKey(v) {", "\n}")
        self.assertIn('replace(/\\s+/g, "").toLowerCase()', key)
        # Реестр отдаёт счёт экрану, модель его хранит.
        src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('"account": e.account,', src)
        db = (ROOT / "app" / "db.py").read_text(encoding="utf-8")
        self.assertIn('("account", "VARCHAR(60)"),', db)
        intake = (ROOT / "app" / "intake.py").read_text(encoding="utf-8")
        self.assertIn('account=e.get("account") or e.get("лицевой счет") or None,', intake)

    def test_documents_live_in_the_sources_group(self):
        """«Документы» — единственная вкладка группы «Источники».

        Решение пользователя 18.09.2026 по прототипу «ФОТ · прототип
        интерфейса»: вкладки реестра разведены на данные, справочники и
        источники. Прежний отдельный список документов дублировал каталоги
        договоров; нынешняя вкладка собирает те же каталоги в одно место и
        служит очередью непривязанных, а на «Договорах» остаётся строка со
        ссылкой туда. Договоры по-прежнему первая вкладка."""
        tabs = between(JS, "var REG_TABS = [", "];")
        self.assertIn('key: "docs", title: "Документы", group: "Источники"', tabs)
        self.assertTrue(tabs.split("{", 2)[1].startswith(' key: "ctr"'), tabs)
        self.assertIn('regTab = "ctr"', JS)
        self.assertIn("regDocGroups()", JS)

    def test_inclusion_checkbox_is_in_the_plan_not_the_registry(self):
        """Реестр — библиотека (16.09.2026): флажок участия живёт на вкладке
        «Договоры» плана, а в реестре его нет вовсе."""
        registry = between(JS, 'regTab === "ctr"', 'regTab === "emp"')
        self.assertNotIn("data-incl", registry)
        self.assertNotIn("inclall", registry)
        render = JS[JS.index("function renderView()"):]
        plan = between(render, '  if (view === "ctr") {', '\n  if (view === ')
        self.assertIn("data-incl", plan)
        self.assertIn("inclall", plan)
        # Флажок участия — состояние, а не отметка для действия: он виден
        # всегда, а не только при наведении, как номер-флажок списка.
        for rule in re.findall(r"([^{}]*\bincl\b[^{}]*)\{([^}]*)\}", CSS):
            self.assertNotRegex(rule[1], r"opacity:\s*0(?![.\d])", rule[0])
        self.assertIn('api("/api/case/" + caseId + "/basis")', JS)

    def test_versions_chosen_in_plan_upload_goes_into_contract(self):
        # Версия «в расчёте» выбирается в условиях плана, не в каталоге реестра.
        self.assertIn("data-use", between(JS, "function conditionsBlock(", "\nfunction "))
        self.assertNotIn("data-use", between(JS, "function catalogBlock(", "\nfunction "))
        self.assertIn('fd.append("contract"', JS)
        self.assertIn("data-contract", JS)


if __name__ == "__main__":
    unittest.main()
