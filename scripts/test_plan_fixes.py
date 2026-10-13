"""Закрепления сотрудников в условиях договора (решение пользователя 19.09.2026).

Экономист закрепляет сотрудника за договором плана на срок, может задать вид
выплаты и сумму, а может не задавать вид — «просто хочет, чтоб сотруднику с
этого договора платили, но неважно какие виды надбавок». Список тот же, что
заполняет чат (plan_settings, «назначения»); правится в строке условий
договора на вкладке «Договоры» плана.

Стенд — временная база `lifecycle.App`. Запускать отдельным процессом
(DEV-STAND-ISOLATION-001).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "harness"))

JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "static" / "app.css").read_text(encoding="utf-8")


class Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def function(name):
    start = JS.index("function " + name + "(")
    return JS[start:JS.index("\nfunction ", start + 1)]


class PlanFixesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lifecycle import App
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, FOT_OPERATOR="fixes-test-operator")
        cls.env.start()
        cls.app = App(cls.temp.name)
        import actuality
        import chat
        cls.actuality, cls.chat = actuality, chat

    @classmethod
    def tearDownClass(cls):
        cls.app.db.close()
        cls.app.d.engine.dispose()
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.db, self.d, self.main = self.app.db, self.app.d, self.app.main
        # Закрепления пишет сервер своей сессией: без сброса кэша план в этой
        # сессии помнил старое значение, «= None» ничего не менял, и
        # закрепления прошлого случая доживали до следующего.
        self.db.expire_all()
        for model in (self.d.Employee, self.d.Contract, self.d.Decision, self.d.Run):
            self.db.query(model).delete()
        self.app.case.plan_settings = None
        self.db.add(self.d.Employee(code="E001", fio="Иванов И. И.", position="Инженер",
                                    rate=1.0, date_from="01.01.2026", date_to="31.12.2026"))
        self.db.add(self.d.Contract(code="C_A", name="Договор А", date_from="01.03.2026",
                                    date_to="31.12.2026", kinds="оклад, 122"))
        self.db.commit()

    # ── помощники ──
    def add(self, **body):
        body.setdefault("employee", "E001")
        body.setdefault("contract", "C_A")
        body.setdefault("from", 3)
        body.setdefault("to", 5)
        return asyncio.run(self.main.add_fix(self.app.case.id, Req(body)))

    def fixes(self):
        self.db.expire_all()
        return self.chat.plan_settings(self.db.get(self.d.Case, self.app.case.id))["назначения"]

    # ── случаи ──
    def test_fix_without_kind_is_any_kind(self):
        self.add(kind="")
        self.assertEqual(self.fixes(), [{"сотрудник": "E001", "фио": "Иванов И. И.",
                                         "договор": "C_A", "с": 3, "по": 5,
                                         "вид": "любой", "сумма": None}])
        # Основание плана отдаёт закрепления строке условий договора.
        self.assertEqual(self.main.plan_basis(self.app.case.id)["fixes"], self.fixes())

    def test_amount_only_with_kind(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self.add(kind="", amount="50 000")
        self.add(kind="122", amount="50 000")
        self.assertEqual(self.fixes()[0]["сумма"], 50000.0)
        self.assertEqual(self.fixes()[0]["вид"], "122")

    def test_bad_term_or_kind_is_refused(self):
        from fastapi import HTTPException
        for body in ({"from": 6, "to": 3}, {"from": 0, "to": 3}, {"from": 3, "to": 13},
                     {"kind": "премия"}, {"employee": "НЕТ"}, {"contract": "НЕТ"}):
            with self.subTest(body=body), self.assertRaises(HTTPException):
                self.add(**body)
        self.assertEqual(self.fixes(), [])

    def test_same_pair_and_kind_is_replaced_and_logged(self):
        run = self.app.run(status="OPTIMAL")
        self.add(kind="")
        self.add(kind="", **{"from": 4, "to": 12})
        self.assertEqual([(f["с"], f["по"]) for f in self.fixes()], [(4, 12)])
        self.db.refresh(run)
        self.assertEqual(run.review, self.actuality.REVIEW)
        self.assertTrue(self.db.query(self.d.Decision).count() >= 1)

    def test_delete_removes_the_fix(self):
        from fastapi import HTTPException
        self.add(kind="")
        body = {"employee": "E001", "contract": "C_A", "kind": "любой"}
        asyncio.run(self.main.delete_fix(self.app.case.id, Req(body)))
        self.assertEqual(self.fixes(), [])
        with self.assertRaises(HTTPException):
            asyncio.run(self.main.delete_fix(self.app.case.id, Req(body)))

    def test_any_kind_goes_to_solver_with_empty_kind(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["сотрудник", "договор", "год", "месяц с", "месяц по", "вид выплаты",
                   "фикс сумма"])
        self.main.build_input.fill_manual(ws, [
            {"сотрудник": "E001", "договор": "C_A", "год": 2026, "с": 3, "по": 5,
             "вид": "любой", "сумма": 1},
            {"сотрудник": "E001", "договор": "C_A", "год": 2026, "с": 6, "по": 6,
             "вид": "122", "сумма": 50000}])
        rows = list(ws.values)[1:]
        self.assertEqual(rows[0], ("E001", "C_A", 2026, 3, 5, None, None))
        self.assertEqual(rows[1], ("E001", "C_A", 2026, 6, 6, "122", 50000))

    def test_impossible_fix_is_named_even_without_any_plan(self):
        # Закрепление, которое договор не может выполнить, — причина «нет
        # решения»; при незакрытых строках трудоёмкости она не теряется.
        descr = ("Закрепление без вида выплаты не выполнить (сотрудник E1 закреплён за "
                 "договором C_B на июль–август, а договор платит только с 01.01.2026 "
                 "по 30.06.2026)")
        got = self.main._conflict_reason(descr, no_plan=True)
        self.assertEqual(got, "Закрепление без вида выплаты не выполнить: сотрудник E1 "
                              "закреплён за договором C_B на июль–август, а договор платит "
                              "только с 01.01.2026 по 30.06.2026.")
        # Прочее ограничение при «ни одна строка не закрыта» — следствие.
        self.assertIsNone(self.main._conflict_reason("Нарушено (баланс фонда C_B)", True))
        self.assertIn("баланс фонда C_B",
                      self.main._conflict_reason("Нарушено (баланс фонда C_B)", False))

    def test_edit_changes_kind_in_one_save(self):
        # Правка строки в таблице сменила «любая» на 122: прежняя запись снята
        # тем же сохранением, двух закреплений одной пары не остаётся.
        self.add(kind="")
        self.add(kind="122", amount="50 000", replaces="любой", **{"from": 4, "to": 6})
        self.assertEqual([(f["вид"], f["с"], f["по"], f["сумма"]) for f in self.fixes()],
                         [("122", 4, 6, 50000.0)])
        # Без replaces другой вид той же пары — отдельное закрепление.
        self.add(kind="")
        self.assertEqual(sorted(f["вид"] for f in self.fixes()), ["122", "любой"])

    def test_several_kinds_are_one_row(self):
        # Строка таблицы — сотрудник на договоре: названные виды приходят
        # списком и заменяют все прежние записи пары (решение пользователя
        # 21.09.2026: «множественный выбор»).
        self.add(kinds=["оклад", "122"])
        self.assertEqual(sorted(f["вид"] for f in self.fixes()), ["122", "оклад"])
        self.assertEqual({(f["с"], f["по"], f["сумма"]) for f in self.fixes()},
                         {(3, 5, None)})
        # Пустой список — «любой»: закрепление одно.
        self.add(kinds=[])
        self.assertEqual([f["вид"] for f in self.fixes()], ["любой"])
        # Сумма — только когда назван один вид.
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self.add(kinds=["оклад", "122"], amount="50 000")
        self.add(kinds=["122"], amount="50 000")
        self.assertEqual([(f["вид"], f["сумма"]) for f in self.fixes()], [("122", 50000.0)])

    def test_whole_salary_bans_other_contracts(self):
        # «Вся зарплата с этого договора» — закрепление любым видом плюс
        # запрет платить этому человеку с прочих договоров плана в те же
        # месяцы (решение пользователя 21.09.2026). Запреты не хранятся —
        # выводятся перед расчётом, иначе договор, добавленный позже, остался
        # бы без запрета, а снятие признака оставило бы их за собой.
        self.db.add(self.d.Contract(code="C_B", name="Договор Б", date_from="01.01.2026",
                                    date_to="31.12.2026", kinds="оклад"))
        self.db.commit()
        self.add(kinds=[], only=True)
        self.assertEqual([(f["вид"], f.get("только")) for f in self.fixes()],
                         [("любой", True)])
        case = self.db.get(self.d.Case, self.app.case.id)
        data = {"contracts": self.db.query(self.d.Contract).order_by(self.d.Contract.code).all()}
        self.main._apply_plan_settings(data, case)
        self.assertEqual([(b["договор"], b["с"], b["по"], b["вид"])
                          for b in data["manual_prohibitions"]],
                         [("C_B", 3, 5, "любой")])
        # Названные виды и «вся зарплата» друг друга исключают.
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self.add(kinds=["оклад"], only=True)

    def test_pin_months_are_clamped_to_the_contract_period(self):
        # Закрепление действует только в месяцах договора в году плана
        # (пользователь 29.09.2026: «чтоб можно было выбрать месяцы в период
        # договора»): срок урезается до периода договора с правками плана, а
        # целиком вне его закрепление в расчёт не идёт — иначе решатель
        # получал невыполнимое требование.
        self.add(kind="", **{"from": 1, "to": 12})
        # Хранится выбранное экономистом: урезается только вход расчёта.
        self.assertEqual([(f["с"], f["по"]) for f in self.fixes()], [(1, 12)])
        case = self.db.get(self.d.Case, self.app.case.id)

        def clamped(date_from, date_to):
            # Договор — копией строки с правками плана, как её отдаёт basis.apply.
            data = {"year": 2026, "contracts": [{"code": "C_A", "date_from": date_from,
                                                 "date_to": date_to}]}
            self.main._apply_plan_settings(data, case)
            return [(f["договор"], f["с"], f["по"]) for f in data["manual_assignments"]]

        self.assertEqual(clamped("01.03.2026", "31.12.2026"), [("C_A", 3, 12)])
        # Правка плана сократила договор до февраля — месяц один.
        self.assertEqual(clamped("01.02.2026", "28.02.2026"), [("C_A", 2, 2)])
        # Договор в году плана не действует — закрепление в расчёт не идёт.
        self.assertEqual(clamped("01.01.2025", "31.12.2025"), [])
        # Срок не задан — весь год.
        self.assertEqual(clamped("", ""), [("C_A", 1, 12)])

    def test_closed_months_ban_the_contract_for_everyone(self):
        # Месяцы без выплат договора в плане (владелец продукта 01.10.2026:
        # снятая галочка выключает договор на весь срок, а нужен «конкретный
        # месяц»): запрет каждому сотруднику плана, закрепления на договоре
        # теряют эти месяцы.
        import json
        case = self.db.get(self.d.Case, self.app.case.id)
        was = case.plan_basis
        case.plan_basis = json.dumps({"договоры": {"C_A": {"месяцы без выплат": "7, 8, 11"}}},
                                     ensure_ascii=False)
        self.db.commit()
        try:
            self.assertEqual(self.main.basis.closed_months(case), {"C_A": [7, 8, 11]})
            data = {"year": 2026, "workgroups": [{"договор": "C_A", "сотрудник": "E001",
                                                   "фио": "Иванов", "с": "01.01.2026",
                                                   "по": "31.12.2026", "приказ": "№ 1"}],
                    "contracts": [{"code": "C_A", "date_from": "01.01.2026",
                                   "date_to": "31.12.2026"}],
                    "employees": [{"code": "E001"}, {"code": "E002"}]}
            warn = []
            self.main._apply_plan_settings(data, case, warn)
            self.assertEqual(sorted((b["сотрудник"], b["с"], b["по"])
                                    for b in data["manual_prohibitions"]),
                             [("E001", 7, 8), ("E001", 11, 11), ("E002", 7, 8), ("E002", 11, 11)])
            self.assertEqual([(f["с"], f["по"]) for f in data["manual_assignments"]],
                             [(1, 6), (9, 10), (12, 12)])
            self.assertTrue(any("Месяцы без выплат" in w for w in warn))
            js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
            # Вид — лентой месяцев в таблице договоров плана (выбор владельца
            # продукта 01.10.2026 по совету дизайнера), не флажками в панели.
            self.assertIn('{ k: "months", label: "Выплаты по месяцам"', js)
            self.assertIn('data-pmon=', js[js.index('function pmonthsCell('):][:2500])
        finally:
            case.plan_basis = was
            self.db.commit()

    def test_workgroup_members_are_pinned_from_the_order(self):
        # Члены рабочей группы договора — закрепления без вида на срок группы
        # (пользователь 30.09.2026: «их же тоже нужно отобразить, просто не
        # дать удалить»). Живут в приказах: в хранимые закрепления плана не
        # пишутся, в расчёт уходят каждый раз, урезанные до срока договора.
        d = self.d
        self.db.query(d.WorkOrderMember).delete()
        self.db.query(d.WorkOrder).delete()
        made = d.WorkOrder(kind="создание", number="1-к", date="10.01.2026", contract_code="C_A",
                           date_from="01.01.2026", date_to="30.06.2026")
        self.db.add(made)
        self.db.flush()
        self.db.add(d.WorkOrderMember(order_id=made.id, n=1, fio="Иванов И. И.",
                                      person_code="E001", employee_code="E001"))
        self.db.add(d.WorkOrderMember(order_id=made.id, n=2, fio="Не найден", person_code="999"))
        self.db.commit()
        group = self.main._workgroup_current(self.db)
        self.assertEqual([(g["договор"], g["сотрудник"], g["приказ"]) for g in group],
                         [("C_A", "E001", "№ 1-к от 10.01.2026")])
        case = self.db.get(d.Case, self.app.case.id)
        data = {"year": 2026, "workgroups": group,
                "contracts": [{"code": "C_A", "date_from": "01.03.2026", "date_to": "31.12.2026"}]}
        self.main._apply_plan_settings(data, case)
        self.assertEqual([(f["договор"], f["сотрудник"], f["с"], f["по"], f["вид"])
                          for f in data["manual_assignments"]], [("C_A", "E001", 3, 6, "любой")])
        self.assertEqual(self.fixes(), [])
        # Договор не в расчёте — его группа тоже.
        data = {"year": 2026, "workgroups": group, "contracts": []}
        self.main._apply_plan_settings(data, case)
        self.assertEqual(data["manual_assignments"], [])
        # Приказ об изменении исключил человека — из группы он ушёл.
        later = d.WorkOrder(kind="изменение", number="2-к", date="01.04.2026", contract_code="C_A")
        self.db.add(later)
        self.db.flush()
        self.db.add(d.WorkOrderMember(order_id=later.id, n=1, action="исключить",
                                      fio="Иванов И. И.", person_code="E001", employee_code="E001"))
        self.db.commit()
        self.assertEqual(self.main._workgroup_current(self.db), [])
        self.db.query(d.WorkOrderMember).delete()
        self.db.query(d.WorkOrder).delete()
        self.db.commit()

    def test_workgroup_member_pay_can_be_set_but_member_stays(self):
        # Выплату члену рабочей группы можно задать, снять его — нельзя
        # (пользователь 30.09.2026: «какую выплату ему дать, решает расчёт —
        # это можно задать, только удалить нельзя, если не задано — то по
        # возможности ВСЮ»).
        from fastapi import HTTPException
        d = self.d
        self.db.query(d.WorkOrderMember).delete()
        self.db.query(d.WorkOrder).delete()
        made = d.WorkOrder(kind="создание", number="1-к", date="10.01.2026", contract_code="C_A",
                           date_from="01.01.2026", date_to="31.12.2026")
        self.db.add(made)
        self.db.flush()
        self.db.add(d.WorkOrderMember(order_id=made.id, n=1, fio="Иванов И. И.",
                                      person_code="E001", employee_code="E001"))
        self.db.commit()
        self.addCleanup(lambda: (self.db.query(d.WorkOrderMember).delete(),
                                 self.db.query(d.WorkOrder).delete(), self.db.commit()))
        group = self.main._workgroup_current(self.db)
        post = lambda body: asyncio.run(self.main.set_workgroup_pay(  # noqa: E731
            self.app.case.id, Req({"employee": "E001", "contract": "C_A", **body})))

        def pins():
            self.db.expire_all()
            data = {"year": 2026, "workgroups": group,
                    "contracts": [{"code": "C_A", "date_from": "01.03.2026",
                                   "date_to": "31.12.2026"},
                                  {"code": "C_B", "date_from": "01.01.2026",
                                   "date_to": "31.12.2026"}]}
            self.main._apply_plan_settings(data, self.db.get(d.Case, self.app.case.id))
            return data

        # Не задано — вся зарплата с договора, по возможности.
        data = pins()
        self.assertEqual([(f["вид"], f.get("по возможности вся"), f["с"], f["по"])
                          for f in data["manual_assignments"]], [("любой", True, 3, 12)])
        # Названный вид — закрепление этим видом, с суммой.
        post({"kinds": ["оклад"], "amount": "40000"})
        data = pins()
        self.assertEqual([(f["вид"], f["сумма"], f.get("по возможности вся"))
                          for f in data["manual_assignments"]], [("оклад", 40000.0, None)])
        # «Вся зарплата» — прочие договоры плана ему запрещены.
        post({"kinds": [], "only": True})
        data = pins()
        self.assertEqual([(f["вид"], f.get("только")) for f in data["manual_assignments"]],
                         [("любой", True)])
        self.assertEqual([b["договор"] for b in data["manual_prohibitions"]], ["C_B"])
        # Пусто — снова по умолчанию; в хранимые закрепления член группы не
        # попадает никогда: снять его можно только приказом.
        post({"kinds": []})
        self.assertEqual([f.get("по возможности вся") for f in pins()["manual_assignments"]],
                         [True])
        self.assertEqual(self.fixes(), [])
        with self.assertRaises(HTTPException):
            post({"employee": "E999", "kinds": []})
        with self.assertRaises(HTTPException):
            post({"kinds": ["оклад", "122"], "amount": "1000"})
        # Признак уходит во вход решателя своей графой.
        import build_input
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["сотрудник", "договор", "год", "месяц с", "месяц по", "вид выплаты",
                   "фикс сумма"])
        build_input.fill_manual(ws, [{"сотрудник": "E001", "договор": "C_A", "год": 2026,
                                      "с": 3, "по": 12, "вид": "любой", "сумма": None,
                                      "по возможности вся": True}])
        head = [c.value for c in ws[1]]
        self.assertEqual(ws.cell(2, head.index("по возможности вся") + 1).value, "да")

    def test_pin_on_contract_outside_the_plan_is_left_out(self):
        # Закрепление на договоре, снятом с расчёта плана, решатель выполнить
        # не может — расчёт кончался «решения нет» (MANUAL_ASSIGNMENT_NO_PAYMENT,
        # сквозная проверка 30.09.2026). Хранится выбранное экономистом.
        self.add(kinds=[])
        self.db.expire_all()
        case = self.db.get(self.d.Case, self.app.case.id)
        warn = []
        data = {"year": 2026, "workgroups": [],
                "contracts": [{"code": "C_B", "date_from": "01.01.2026", "date_to": "31.12.2026"}]}
        self.main._apply_plan_settings(data, case, warn)
        self.assertEqual(data["manual_assignments"], [])
        self.assertTrue(any("вне расчёта плана" in w and "C_A" in w for w in warn), warn)
        self.assertEqual([f["договор"] for f in self.fixes()], ["C_A"])

    def test_whole_salary_conflicts_with_another_contract(self):
        # Человек не может получать всю зарплату с одного договора и быть
        # закреплён на другом: сервис называет договор, а не строит спорный
        # план.
        from fastapi import HTTPException
        self.db.add(self.d.Contract(code="C_B", name="Договор Б", date_from="01.01.2026",
                                    date_to="31.12.2026", kinds="оклад"))
        self.db.commit()
        self.add(contract="C_B", kinds=[])
        with self.assertRaises(HTTPException) as e:
            self.add(kinds=[], only=True)
        self.assertIn("C_B", e.exception.detail)
        self.assertEqual([f["договор"] for f in self.fixes()], ["C_B"])

    def test_delete_removes_the_whole_row(self):
        # Строка — сотрудник на договоре: снимается со всеми видами разом.
        self.add(kinds=["оклад", "122"])
        asyncio.run(self.main.delete_fix(self.app.case.id, Req(
            {"employee": "E001", "contract": "C_A", "all": True})))
        self.assertEqual(self.fixes(), [])

    def test_saving_conditions_keeps_the_fixes(self):
        # Правка условий договора возвращает основание плана целиком, вместе
        # с закреплениями: раньше ответ был каталогом без них, и таблица
        # закреплений в панели пустела до следующей загрузки страницы
        # (пользователь 21.09.2026: «я меняю настройки тут и меняются
        # настройки в закрепить сотрудника»).
        self.add(kinds=["оклад"])
        # Значение — не как в договоре («оклад, 122»): правку, равную реестру,
        # план снимает сам (пользователь 30.09.2026: «реестр сравнялся с
        # правкой плана — снимать правку автоматически»).
        out = asyncio.run(self.main.change_plan_basis(self.app.case.id, Req(
            {"contract": "C_A", "fields": {"разрешенные выплаты": "оклад"}})))
        self.assertIn("fixes", out)
        self.assertEqual([(f["сотрудник"], f["вид"]) for f in out["fixes"]], [("E001", "оклад")])
        self.assertEqual(out["overrides"]["C_A"]["разрешенные выплаты"], "оклад")

    def test_chat_without_kind_fixes_any_kind(self):
        # Раньше вид по умолчанию был «оклад» — закрепление сужалось молча.
        case = self.db.get(self.d.Case, self.app.case.id)
        lines, _ = self.chat.apply_fixes(self.db, case, [
            {"employee": "E001", "contract": "C_A", "month_from": 3, "month_to": 5,
             "kind": None, "mode": "назначить"}], "закрепи Иванова на C_A с марта по май")
        self.db.commit()
        self.assertEqual(self.fixes()[0]["вид"], "любой")
        self.assertIn("любой вид выплаты", " ".join(lines))


class PlanOrderUiTests(unittest.TestCase):
    """Вкладка «Проекты приказов» плана: одна таблица приказов из расчёта."""

    def test_tab_lists_orders_of_the_plan(self):
        # Строка — приказ вида × договора × месяца из расчёта (спецификация
        # дизайнера 30.09.2026); прежний список из закреплений «приказ» снят:
        # закрепление расчёт исполняет, и его выплата приходит сюда
        # стимулирующей — с той суммой, что расчёт платит.
        html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
        # Выход плана — «Проекты приказов»: приказы как вход живут в
        # договоре, и два «Приказа» путались бы (29.09.2026).
        self.assertIn('data-view="ord">Проекты приказов<', html)
        self.assertEqual(html.count('data-view="ord"'), 1)
        js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        branch = js[js.index('if (view === "ord") {'):
                    js.index('if (view === "ctr") {\n    // Вкладка')]
        self.assertIn("el.innerHTML = ordersView();", branch)
        for gone in ('if (f["вид"] !== "приказ") return;', '"/order?contract="', "сумма не задана",
                     "Приказов пока нет"):
            self.assertNotIn(gone, js)
        self.assertIn('"/api/case/" + caseId + "/orders/file?run="', js)
        # Данные вкладки — список приказов расчёта, а не строки реестра.
        self.assertIn('var need = v === "ctr" || v === "emp"', js)


class PlanFixesUiTests(unittest.TestCase):
    """Закрепления — в строке условий договора, как решил пользователь."""

    def node(self, script):
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                             encoding="utf-8", check=True)
        return json.loads(out.stdout)

    def test_conditions_row_lists_and_adds_fixes(self):
        self.assertIn("fixesBlock(x, now)", function("conditionsBlock"))
        block = function("fixesBlock")
        # Таблица в условиях договора (решение пользователя 19.09.2026), с
        # 20.09.2026 — обычная таблица, без клеток-месяцев («я хочу чтобы это
        # была традиционная таблица»).
        for head in ("<th>Закреплённый сотрудник</th>", "<th>Начало</th>", "<th>Окончание</th>",
                     "<th>Выплата</th>", '<th class="n">Сумма в месяц, ₽</th>'):
            self.assertIn(head, block)
        for gone in ("<th>С</th>", "<th>По</th>", "mstrip", "data-fixm", 'class="mrow"'):
            self.assertNotIn(gone, JS)
        self.assertIn('data-fixcell="', block)
        self.assertIn('iconBtn("del", "data-fixdel"', block)
        # Новое закрепление — последней строкой таблицы; окна и кнопок
        # «Закрепить/Отмена» в строке нет (пользователь: «неуместно тут эти
        # кнопки»).
        self.assertIn('<tr class="addrow"><td colspan="\' + (sums ? 6 : 5) + \'"><button', block)
        for gone in ("function fixAddDialog(", "function fixEditorRow(", "data-fixsave",
                     "data-fixcancel"):
            self.assertNotIn(gone, JS)
        # «+ Закрепить сотрудника» — список ФИО с поиском; выбор закрепляет
        # сразу на все месяцы договора с любой выплатой.
        add = function("fixAdd")
        self.assertIn("pickFrom(anchor,", add)
        self.assertIn('var span = fixDefaultSpan(fixActive(x, now));', add)
        self.assertIn('kinds: []', add)
        # Сумма бывает только у одного названного вида; графа суммы — только
        # когда у кого-то вид назван, иначе ячейка молчит с подсказкой.
        self.assertIn('var sums = list.some(function (f) { return f.kinds.length > 0; });', block)
        self.assertIn("Сумма задаётся у названного вида выплаты", block)
        self.assertIn("Сумма задаётся, когда назван один вид выплаты", block)
        # Что в строке правится, показывает сама строка: под курсором все
        # правимые значения получают пунктир, под курсором значения — акцент
        # (пользователь 21.09.2026: «не очевидно для нажатия ячейка с суммой
        # в месяц»). Слова в ячейке для этого не заводим.
        self.assertIn('f["сумма"] ? mo(f["сумма"]) : ""', block)
        self.assertNotIn("Указать", block)
        css = (ROOT / "app" / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".ovprops .fixes tbody tr:hover .ed{border-bottom-color:var(--line3)}", css)
        self.assertIn(".ovprops .fixes tbody tr:hover .ed:hover{border-bottom-color:var(--acc)}", css)
        # Пустая сумма держит ширину графы — пунктир под ней такой же, как
        # под числом.
        self.assertIn(".ovprops .fixes td.n .ed:empty{min-width:72px}", css)
        # Пока в ячейке поле, пунктир не двоится с его рамкой.
        self.assertIn(".ovprops .fixes tbody tr:hover .ed:has(input,select)"
                      "{border-bottom-color:transparent}", css)
        # Выплата — окошком с флажками: видов несколько, плюс «вся зарплата»
        # (решение пользователя 21.09.2026).
        edit = function("fixCellEdit")
        self.assertIn("fixKindsPick(span, f, x, now, function (chosen, only) {", edit)
        self.assertNotIn('<option value="любой">любая</option>', JS)
        pick = function("fixKindsPick")
        self.assertIn('data-fixonly', pick)
        self.assertIn('class="ovkinds" role="group"', pick)
        self.assertIn("if (save && changed) onSave(all ? [] : out, all);", pick)
        # Строка — сотрудник на договоре: снимается целиком.
        self.assertIn("all: true", function("fixDelete"))
        self.assertIn('"/api/case/" + caseId + "/fixes", { method: "POST"', function("fixPost"))
        self.assertIn('method: "DELETE"', function("fixDelete"))

    def test_cells_are_edited_in_place(self):
        # Значение правится щелчком по ячейке, как в таблицах реестра: месяц
        # и вид — выбором (сохраняется сразу), сумма — полем (Enter или уход
        # из поля); Esc — как было, панель при этом не закрывается.
        edit = function("fixCellEdit")
        # Месяц — календарём месяцев в пределах года плана (пользователь
        # 20.09.2026: «выбор календаря»).
        self.assertIn('el.type = "month";', edit)
        # Месяцы — только в сроке договора (пользователь 29.09.2026).
        self.assertIn("el.min = ym(lo);", edit)
        self.assertIn("el.max = ym(hi);", edit)
        self.assertIn("var act = fixActive(x, now), lo = act.indexOf(true) + 1", edit)
        self.assertIn("if (field === \"from\") { a = m; if (b < a) b = a; } else { b = m;"
                      " if (a > b) a = b; }", edit)
        self.assertIn('el.addEventListener("blur", commit);', edit)
        self.assertIn('if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); restore(true); }',
                      edit)
        keys = JS[JS.index("/* Закрепление с клавиатуры"):][:900]
        self.assertIn('if (e.target.matches("[data-fixcell]") && (e.key === "Enter" || e.key === " ")) {',
                      keys)
        # Поле правки — по ширине ячейки: открытое, оно не раздвигает графу.
        self.assertIn(".ovprops .fixes select.ovf{width:100%;", CSS)

    def _removed_month_cells(self):
        # Первый щелчок — начало срока, второй — конец; сохраняется один раз.
        month = function("fixMonth")
        self.assertIn("fixPick = { caseId: caseId, key: key, start: m };", month)
        self.assertIn("var a = Math.min(fixPick.start, m), b = Math.max(fixPick.start, m);", month)
        self.assertIn('from: a, to: b, kind: f["вид"]', month)
        # Клетка — кнопка без своего класса (UI-007: класс ради одного места
        # не заводят); месяц вне договора не выбирается.
        block = function("fixesBlock")
        self.assertIn("'<button type=\"button\" data-fixm=\"'", block)
        self.assertIn("(on[i] ? \"\" : ' aria-disabled=\"true\"')", block)
        self.assertIn('if (!row || cell.getAttribute("aria-disabled") === "true") return;', month)
        # С клавиатуры: одна остановка Tab на строку, стрелки ходят по месяцам,
        # Esc снимает выбранное начало.
        keys = JS[JS.index("/* Закрепление с клавиатуры"):][:2200]
        self.assertIn("{ ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: cells.length - 1 }", keys)
        self.assertIn('if (e.key === "Escape" && fixPick) {', keys)
        # Выплата сохраняется уходом фокуса с её полей, а не со значения,
        # которое перерисовка заменяет полями.
        self.assertIn('var field = e.target.closest("[data-fixkind], [data-fixsum]");', keys)
        # Поле выплаты — не основание плана: его изменение не уходит в setBasis.
        change = JS[JS.index('$("view").addEventListener("change"'):][:2000]
        self.assertLess(change.index('if (e.target.closest("td[data-fixpaycell]")) '
                                     '{ fixPayField(e.target); return; }'),
                        change.index('var ov = e.target.closest("[data-ov]")'))
        # Перечитывается только основание: строка условий остаётся открытой.
        self.assertIn('api("/api/case/" + caseId + "/basis")', function("fixReload"))
        self.assertNotIn('setView("ctr")', function("fixPost") + function("fixDelete"))

    def test_panel_does_not_jump_on_edit(self):
        # «Строки скачут»: панель появляется только из закрытого состояния;
        # смена договора и
        # перерисовка после правки — без анимации.
        self.assertIn("ctrPanel(open, bs, !pctrShown)", JS)
        self.assertIn(".ctrpanel.reveal{animation:conditionReveal", CSS)
        self.assertNotRegex(CSS, r"\.ctrpanel\{[^}]*animation")
        # Прокрутка списка, панели и таблицы вбок переживает перерисовку.
        self.assertIn("      hsNow.scrollLeft = hsLeft;", JS)
        self.assertIn("if (panelNow) panelNow.scrollTop = panelTop;", JS)
        self.assertIn("if (mainNow) mainNow.scrollTop = top; else el.scrollTop = top;", JS)

    def test_term_and_payment_read_in_words(self):
        # «Сроки включительно или нет?» — подсказка и диктор называют оба края.
        consts = "".join(re.search(r"var %s = \[[^\]]+\];" % name, JS).group(0)
                         for name in ("MONTH_NAMES", "MONTH_GEN"))
        script = (consts + "var casesCache = [{id: 1, year: 2026}], caseId = 1;" +
                  function("fixYear") + function("fixSpanWords") + function("fixAmount") +
                  "console.log(JSON.stringify([fixSpanWords(3, 5), fixSpanWords(2, 2),"
                  " fixAmount('50\u00a0000'), fixAmount('1 500,5'), fixAmount('')]));")
        # Сумма в поле — с разрядами, как её показывает приложение; сравнение
        # с сохранённой — числом.
        self.assertEqual(self.node(script),
                         ["с 1 марта по 31 мая 2026 включительно",
                          "с 1 февраля по 28 февраля 2026 включительно",
                          50000, 1500.5, None])

    def test_rows_keep_their_order_after_edit(self):
        # Сервер дописывает изменённое закрепление в конец списка; строки идут
        # по ФИО, и правка строку из-под курсора не уводит (проект дизайнера
        # 20.09.2026). Проверка — настоящей отрисовкой в node.
        consts = "".join(re.search(r"var %s = \[[^\]]+\];" % name, JS).group(0)
                         for name in ("FIX_KINDS", "MONTH_NAMES", "MONTH_GEN", "MONTH_SHORT"))
        # Значок скачивания — у «Приказ…» в строке добавления (30.09.2026).
        consts += re.search(r"var TB_DOWN = [\s\S]*?;\n", JS).group(0)
        funcs = "".join(function(name) for name in (
            "esc", "cap", "mo", "iconBtn", "fixKey", "fixRows", "fixYear", "fixActive",
            "fixSpanWords", "fixMonthText", "wgDay", "fixGroupRows", "staffOffMap",
            "fixesBlock"))
        script = (consts + "var casesCache = [{id: 1, year: 2026}], caseId = 1, fixPick = null, vdata = null;" +
                  funcs +
                  "var pin = function (who, fio) { return {'сотрудник': who, 'фио': fio,"
                  " 'договор': 'C', 'с': 1, 'по': 12, 'вид': 'любой', 'сумма': null}; };"
                  "var vbasis = {fixes: [pin('E8', 'Зайцев Захар'), pin('E7', 'Волкова Вера')],"
                  " overrides: {}};"
                  "var html = fixesBlock({code: 'C', kinds: '', from: '01.01.2026',"
                  " to: '31.10.2026'}, {});"
                  "console.log(JSON.stringify([html.match(/data-fixrow=\"[^\"]+\"/g),"
                  " (html.match(/вне срока договора/g) || []).length,"
                  " (html.match(/01\.26/g) || []).length,"
                  " (html.match(/10\.26/g) || []).length,"
                  " (html.match(/<thead>/g) || []).length,"
                  " html.indexOf('Сумма в месяц') >= 0]));")
        rows, outside, dates, ends, heads, sums = self.node(script)
        self.assertEqual(rows, ['data-fixrow="E7|C|1-12"', 'data-fixrow="E8|C|1-12"'])
        # Договор до 31.10: срок «январь–декабрь» показан и уходит в расчёт
        # урезанным до срока договора — окончание «10.26», без приписки «вне
        # срока договора» (пользователь 29.09.2026: «зачем ты добавил сюда
        # подпись?»).
        self.assertEqual(outside, 0)
        self.assertEqual(ends, 2)
        # Даты — месяц и год числами: «01.26» (пользователь 20.09.2026).
        self.assertEqual(dates, 2)
        self.assertEqual(heads, 1)
        # У обоих «любая» — графы суммы нет.
        self.assertFalse(sums)

    def test_row_shows_kinds_and_whole_salary(self):
        # Сотрудник на договоре — одна строка: названные виды перечислены в
        # графе «Выплата», «вся зарплата» названа словами (решение
        # пользователя 21.09.2026). Проверка — настоящей отрисовкой в node.
        consts = "".join(re.search(r"var %s = \[[^\]]+\];" % name, JS).group(0)
                         for name in ("FIX_KINDS", "MONTH_NAMES", "MONTH_GEN", "MONTH_SHORT"))
        # Значок скачивания — у «Приказ…» в строке добавления (30.09.2026).
        consts += re.search(r"var TB_DOWN = [\s\S]*?;\n", JS).group(0)
        funcs = "".join(function(name) for name in (
            "esc", "cap", "mo", "iconBtn", "fixKey", "fixRows", "fixYear", "fixActive",
            "fixSpanWords", "fixMonthText", "wgDay", "fixGroupRows", "staffOffMap",
            "fixesBlock"))
        script = (consts + "var casesCache = [{id: 1, year: 2026}], caseId = 1, vdata = null;" + funcs +
                  "var pin = function (who, kind, extra) { return Object.assign({'сотрудник': who,"
                  " 'фио': who === 'E7' ? 'Волкова Вера' : 'Зайцев Захар', 'договор': 'C',"
                  " 'с': 1, 'по': 12, 'вид': kind, 'сумма': null}, extra || {}); };"
                  "var vbasis = {fixes: [pin('E7', 'оклад'), pin('E7', '122'),"
                  " pin('E8', 'любой', {'только': true})], overrides: {}};"
                  "var html = fixesBlock({code: 'C', kinds: 'оклад, 122', from: '01.01.2026',"
                  " to: '31.12.2026'}, {'разрешенные выплаты': 'оклад'});"
                  "console.log(JSON.stringify([html.match(/data-fixrow=\"[^\"]+\"/g),"
                  " (html.match(/>(оклад, 122|вся зарплата|любая)</g) || []),"
                  " html.indexOf('Сумма задаётся, когда назван один вид') >= 0,"
                  " html.indexOf('платить ему с прочих договоров плана запрещено') >= 0,"
                  " (html.match(/договор не платит: [^<]+/g) || []),"
                  " html.indexOf('>оклад, 122 <span class=\"st warn\">') >= 0]));")
        rows, cells, note, tip, gone, kinds_cell = self.node(script)
        # Две записи одной пары с общим сроком — одна строка.
        self.assertEqual(rows, ['data-fixrow="E7|C|1-12"', 'data-fixrow="E8|C|1-12"'])
        # У строки с двумя видами они перечислены через запятую, у строки
        # «вся зарплата» — названы словами.
        self.assertEqual(cells, [">вся зарплата<"])
        self.assertTrue(kinds_cell, "виды не перечислены в графе «Выплата»")
        # У строки с двумя видами сумма не правится, и ячейка говорит почему.
        self.assertTrue(note)
        # «Вся зарплата» объясняет себя подсказкой, а не текстом на экране.
        self.assertTrue(tip)
        # В плане у договора оставили только оклад: 122 в закреплении назван
        # словом «договор не платит», а не молчит до расчёта (UI-016).
        self.assertEqual(gone, ["договор не платит: 122"])

    def test_workgroup_member_pay_is_editable_but_not_removable(self):
        # Выплату члену рабочей группы можно задать, снять его — нельзя
        # (пользователь 30.09.2026: «какую выплату ему дать, решает расчёт —
        # это можно задать, только удалить нельзя, если не задано — то по
        # возможности ВСЮ»). Проверка — настоящей отрисовкой в node.
        consts = "".join(re.search(r"var %s = \[[^\]]+\];" % name, JS).group(0)
                         for name in ("FIX_KINDS", "MONTH_NAMES", "MONTH_GEN", "MONTH_SHORT"))
        consts += re.search(r"var TB_DOWN = [\s\S]*?;\n", JS).group(0)
        funcs = "".join(function(name) for name in (
            "esc", "cap", "mo", "iconBtn", "fixKey", "fixRows", "fixYear", "fixActive",
            "fixSpanWords", "fixMonthText", "wgDay", "fixGroupRows", "staffOffMap",
            "fixesBlock"))
        script = (consts + "var casesCache = [{id: 1, year: 2026}], caseId = 1;" + funcs +
                  "var vdata = {employees: [{code: 'E7', fio: 'Волкова Вера'},"
                  " {code: 'E8', fio: 'Зайцев Захар'}],"
                  " workgroups: [{id: 1, contract: 'C', employee: 'E7', date: '10.01.2026',"
                  " from: '01.01.2026', to: '31.12.2026', number: '1-к'},"
                  " {id: 2, contract: 'C', employee: 'E8', date: '10.01.2026',"
                  " from: '01.01.2026', to: '31.12.2026', number: '1-к'}]};"
                  "var vbasis = {fixes: [], overrides: {},"
                  " workgroup_pay: {'C|E8': {'виды': ['оклад'], 'только': false}}};"
                  "var html = fixesBlock({code: 'C', kinds: 'оклад, 122', from: '01.01.2026',"
                  " to: '31.12.2026'}, {});"
                  "console.log(JSON.stringify([(html.match(/data-wgkind=\"[^\"]+\"[^>]*>[^<]+/g) || []),"
                  " (html.match(/data-fixdel/g) || []).length,"
                  " html.indexOf('Приказ') >= 0]));")
        cells, trash, order = self.node(script)
        self.assertEqual([c.split('"')[1] + ":" + c.split(">")[-1] for c in cells],
                         ["E7:вся, по возможности", "E8:оклад"])
        # Корзины у членов группы нет: состав меняется приказом.
        self.assertEqual(trash, 0)
        # Приказа во входных данных нет (пользователь 30.09.2026: «не логично
        # делать опцию „скачать приказ“ во вкладке входных данных, когда ещё
        # ничего не посчитано») — он на «Проектах приказов».
        self.assertFalse(order)
        edit = function("wgKindEdit")
        self.assertIn('"/fixes/workgroup"', edit)
        self.assertIn("JSON.stringify({ employee: emp, contract: code, kinds: kinds, only: all })",
                      edit)
        self.assertIn("Ни одного вида — вся, по возможности", edit)
        self.assertIn("function fixKindsPick(anchor, f, x, now, onSave, onCancel, note)", JS)

    def test_pick_list_skips_already_fixed(self):
        # Закрепить можно только того, у кого на этом договоре строки ещё нет:
        # выбор уже закреплённого добавлял ему вторую строку «любая» рядом с
        # прежней (пользователь 21.09.2026: «выбрала сотрудника и появились две
        # строки сразу»). Вид выплаты прежней строки роли не играет.
        consts = re.search(r"var MONTH_NAMES = \[[^\]]+\];", JS).group(0)
        script = (consts + "var casesCache = [{id: 1, year: 2026}], caseId = 1, picked = null;" +
                  function("fixYear") + function("fixActive") + function("fixDefaultSpan") +
                  function("staffOffMap") + function("fixAdd") +
                  "function pickFrom(a, items, title, empty, onPick) {"
                  " picked = [items.map(function (i) { return i.id; }), empty]; }"
                  "function fixPost() { throw new Error('выбора не было'); }"
                  "var vdata = {contracts: [{code: 'C', from: '01.01.2026', to: '31.12.2026'}],"
                  " employees: [{code: 'E7', fio: 'Волкова Вера'}, {code: 'E8', fio: 'Зайцев Захар'},"
                  " {code: 'E9', fio: 'Абрамов Артём'}]};"
                  "var pin = function (who, ctr, kind) { return {'сотрудник': who, 'договор': ctr,"
                  " 'с': 1, 'по': 12, 'вид': kind, 'сумма': null}; };"
                  "var vbasis = {fixes: [pin('E7', 'C', 'оклад'), pin('E8', 'C', 'любой'),"
                  " pin('E9', 'C2', 'любой')], overrides: {}};"
                  "fixAdd({}, 'C');"
                  "console.log(JSON.stringify(picked));")
        ids, empty = self.node(script)
        # E7 закреплён с видом «оклад», E8 — «любой»: в списке нет обоих.
        # E9 закреплён на другом договоре — он в списке.
        self.assertEqual(ids, ["E9"])
        self.assertEqual(empty, "Все сотрудники уже закреплены")

    def test_contract_months_limit_the_cells(self):
        # Серые клетки — месяцы, в которые договор не платит; срок нового
        # закрепления — все месяцы договора в году плана, с правкой плана.
        consts = re.search(r"var MONTH_NAMES = \[[^\]]+\];", JS).group(0)
        script = (consts + "var casesCache = [{id: 1, year: 2026}], caseId = 1;" +
                  function("fixYear") + function("fixActive") + function("fixDefaultSpan") +
                  "var x = {from: '01.03.2026', to: '31.12.2026'};"
                  "var long = {from: '15.06.2025', to: '30.06.2027'};"
                  "console.log(JSON.stringify([fixActive(x, {}), "
                  "fixActive(x, {'начало': '01.05.2026'}), fixActive(long, {}),"
                  " fixDefaultSpan(fixActive(x, {})), fixDefaultSpan(fixActive(long, {}))]));")
        on, moved, long, span, full = self.node(script)
        self.assertEqual(on, [False, False] + [True] * 10)
        self.assertEqual(moved, [False] * 4 + [True] * 8)
        self.assertEqual(long, [True] * 12)
        self.assertEqual(span, [3, 12])
        self.assertEqual(full, [1, 12])


if __name__ == "__main__":
    unittest.main()
