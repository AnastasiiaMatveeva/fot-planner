# -*- coding: utf-8 -*-
"""Приказ о рабочей группе — вход плана (решение пользователя 28.09.2026).

Образец ниже повторяет форму настоящего кадрового приказа «О создании рабочей
группы» (№ …-к, соглашение, л/с, КФО, срок группы, таблица состава с
табельными номерами, приложение «Распределение обязанностей»), но люди и
номера выдуманы: персональные данные из присланного приказа в репозиторий не
кладутся.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "app"))

import workgroup  # noqa: E402

SAMPLE = "\n".join([
    "П Р И К А З",
    "12.03.2026 №  1234-к",
    "О создании рабочей группы",
    "В соответствии с «Положением об оплате труда работников» для выполнения работ по "
    " соглашению от  05.03.26 №  26-71-10042 « Тестовая тема о структурной оптимизации» "
    "(л/с  20576У17440) , КФО  2",
    "ПРИКАЗЫВАЮ:",
    "1. Создать рабочую группу на период с  12.03.2026 по  31.12.2027 в составе:",
    "№\tФИО\tТабельный номер\tДолжность\tПодразделение",
    "1\tПетров Пётр Петрович\t01234\tинженер\tЛаборатория испытаний",
    "2\tСидорова Анна Сергеевна\t05678\tведущий научный сотрудник\tОтдел расчётов",
    "2. Руководителем рабочей группы (далее руководителем) назначить  Сидорову Анну Сергеевну.",
    "7. Установить стимулирующие выплаты за интенсивность труда исполнителям работ в "
    "соответствии с распределением обязанностей (Приложение).",
    "Приложение к приказу",
    "Распределение обязанностей между членами рабочей группы",
    "№\tФИО\tВид работ",
    "1\tПетров Пётр Петрович\tПодготовка стенда, проведение испытаний",
    "2\tСидорова Анна Сергеевна\tПостановка задачи, анализ результатов, написание отчёта",
])


class ParseTests(unittest.TestCase):
    def test_form_is_recognised_and_read_field_by_field(self):
        self.assertTrue(workgroup.looks_like(SAMPLE))
        p = workgroup.parse(SAMPLE)
        self.assertEqual((p["вид"], p["номер"], p["дата"]), ("создание", "1234-к", "12.03.2026"))
        self.assertEqual((p["соглашение"], p["соглашение от"]), ("26-71-10042", "05.03.26"))
        self.assertEqual(p["тема"], "Тестовая тема о структурной оптимизации")
        self.assertEqual((p["счёт"], p["кфо"]), ("20576У17440", "2"))
        self.assertEqual((p["с"], p["по"]), ("12.03.2026", "31.12.2027"))
        # Табельный номер — текстом, с ведущим нулём.
        self.assertEqual([(r["фио"], r["табельный"], r["должность"]) for r in p["состав"]],
                         [("Петров Пётр Петрович", "01234", "инженер"),
                          ("Сидорова Анна Сергеевна", "05678", "ведущий научный сотрудник")])
        # Вид работ — из приложения, по ФИО.
        self.assertEqual(p["состав"][0]["работы"], "Подготовка стенда, проведение испытаний")
        # Руководитель назван в винительном падеже — сопоставлен по основе.
        self.assertEqual(p["руководитель"], "Сидорова Анна Сергеевна")

    def test_amendment_reads_base_order_and_actions(self):
        text = "\n".join([
            "П Р И К А З", "01.06.2026 № 2345-к",
            "О внесении изменений в приказ от 12.03.2026 № 1234-к «О создании рабочей группы»",
            "по соглашению от 05.03.26 № 26-71-10042 «Тестовая тема» (л/с 20576У17440)",
            "1. Включить в состав рабочей группы:",
            "1\tКозлов Кирилл Андреевич\t07777\tтехник\tЛаборатория испытаний",
            "2. Исключить из состава рабочей группы:",
            "1\tПетров Пётр Петрович\t01234\tинженер\tЛаборатория испытаний",
        ])
        p = workgroup.parse(text)
        self.assertEqual(p["вид"], "изменение")
        self.assertEqual(p["основание"], {"дата": "12.03.2026", "номер": "1234-к"})
        self.assertEqual([(r["фио"], r["действие"]) for r in p["состав"]],
                         [("Козлов Кирилл Андреевич", "включить"),
                          ("Петров Пётр Петрович", "исключить")])

    def test_other_documents_are_not_taken_for_an_order(self):
        self.assertFalse(workgroup.looks_like("Положение об оплате труда. Рабочая группа "
                                              "формируется руководителем."))
        self.assertFalse(workgroup.looks_like("ПРИКАЗ № 2556 О формировании заработной платы"))


class IntakeTests(unittest.TestCase):
    """Приказ ложится в реестр: договор по л/с, члены группы — по строкам."""

    @classmethod
    def setUpClass(cls):
        from lifecycle import App
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, FOT_OPERATOR="workgroup-test-operator")
        cls.env.start()
        cls.app = App(cls.temp.name)

    @classmethod
    def tearDownClass(cls):
        cls.app.db.close()
        cls.app.d.engine.dispose()
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        d, db = self.app.d, self.app.db
        for model in (d.WorkOrderMember, d.WorkOrder, d.Employee, d.Contract):
            db.query(model).delete()
        db.add(d.Contract(code="C_GRANT-26", number="26-71-10042", account="20576У17440"))
        # У Сидоровой две строки — основная и совместительство: в группу входит
        # та, что с должностью из приказа (строка, а не человек).
        db.add(d.Employee(code="E1", person_code="01234", fio="Петров Пётр Петрович",
                          position="Инженер"))
        db.add(d.Employee(code="E2", person_code="05678", fio="Сидорова Анна Сергеевна",
                          position="Ведущий научный сотрудник"))
        db.add(d.Employee(code="E3", person_code="05678", fio="Сидорова Анна Сергеевна",
                          position="Доцент"))
        path = Path(self.temp.name) / "Приказ 1234-к.doc"
        path.write_bytes(b"")
        self.doc = d.Document(case_id=self.app.case.id, name=path.name, path=str(path))
        db.add(self.doc)
        db.commit()

    def run_order(self, text):
        import intake
        with patch("docread.to_text", return_value=text):
            intake.run_workgroup(self.app.db, self.app.case, self.doc)
        return self.app.main._workgroup_rows(self.app.db)

    def test_order_lands_with_contract_and_rows(self):
        rows = self.run_order(SAMPLE)
        # Договор найден по лицевому счёту.
        self.assertEqual({r["contract"] for r in rows}, {"C_GRANT-26"})
        # Строки — по табельному номеру и должности: у Сидоровой из двух строк
        # взята та, что с должностью из приказа.
        self.assertEqual([(r["fio"], r["employee"]) for r in rows],
                         [("Петров Пётр Петрович", "E1"), ("Сидорова Анна Сергеевна", "E2")])
        self.assertEqual([r["leader"] for r in rows], [False, True])
        self.assertEqual(rows[0]["work"], "Подготовка стенда, проведение испытаний")
        self.assertEqual(self.app.db.get(self.app.d.Document, self.doc.id).kind, workgroup.KIND)
        # Общее слово разобранного документа — на экране «Обработан».
        self.assertEqual(self.app.db.get(self.app.d.Document, self.doc.id).state, "разобран")
        # Приказ строкой: договор, сколько людей, сколько не нашлось — для
        # сводки пачки в «Документах» (29.09.2026).
        orders = self.app.main._workorders(self.app.db)
        self.assertEqual([(o["number"], o["contract"], o["people"], o["lost"]) for o in orders],
                         [("1234-к", "C_GRANT-26", 2, 0)])

    def test_order_attaches_itself_when_its_contract_appears_later(self):
        """Приказ загрузили раньше договора — он ждёт и прикрепляется сам,
        когда договор с его л/с или номером соглашения появляется в реестре.
        Договор, выбранный экономистом, перепроверка не трогает."""
        import intake
        d, db = self.app.d, self.app.db
        db.query(d.Contract).delete()
        db.commit()
        rows = self.run_order(SAMPLE)
        self.assertEqual({r["contract"] for r in rows}, {None})
        # Договор появился — по номеру соглашения.
        db.add(d.Contract(code="C_LATE", number="26-71-10042"))
        db.commit()
        self.assertEqual(intake.rebind_workorders(db), 1)
        rows = self.app.main._workgroup_rows(db)
        self.assertEqual({r["contract"] for r in rows}, {"C_LATE"})
        self.assertEqual(db.get(d.Document, self.doc.id).contract_code, "C_LATE")
        # Уже привязанный — не перепривязывается, даже если нашёлся другой.
        db.add(d.Contract(code="C_OTHER", account="20576У17440"))
        db.commit()
        self.assertEqual(intake.rebind_workorders(db), 0)
        self.assertEqual({r["contract"] for r in self.app.main._workgroup_rows(db)}, {"C_LATE"})

    def test_unmatched_contract_and_row_are_chosen_by_hand(self):
        from main import workorder_contract, workorder_member

        class Req:
            def __init__(self, body):
                self.body = body

            async def json(self):
                return self.body

        # Л/с и номер соглашения не совпали ни с одним договором, а должность
        # второй сотрудницы — ни с одной её строкой: молча не угадываем.
        text = SAMPLE.replace("20576У17440", "000000").replace("26-71-10042", "00-00-00000") \
                     .replace("ведущий научный сотрудник", "профессор")
        rows = self.run_order(text)
        self.assertEqual({r["contract"] for r in rows}, {None})
        self.assertEqual([r["employee"] for r in rows], ["E1", None])
        orders = self.app.main._workorders(self.app.db)
        self.assertEqual([(o["contract"], o["people"], o["lost"]) for o in orders],
                         [(None, 2, 1)])
        # Экономист выбирает договор и строку списком.
        asyncio.run(workorder_contract(rows[0]["order_id"], Req({"code": "C_GRANT-26"})))
        asyncio.run(workorder_member(rows[1]["id"], Req({"employee": "E3"})))
        rows = self.app.main._workgroup_rows(self.app.db)
        self.assertEqual({r["contract"] for r in rows}, {"C_GRANT-26"})
        self.assertEqual([r["employee"] for r in rows], ["E1", "E3"])


class UiTests(unittest.TestCase):
    JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    CSS = (ROOT / "app" / "static" / "app.css").read_text(encoding="utf-8")

    def body(self, name):
        start = self.JS.index("function %s(" % name)
        return self.JS[start:self.JS.index("\nfunction ", start + 1)]

    def test_group_is_a_tab_of_the_contract(self):
        """Рабочая группа — вкладка договора, а не раздел реестра (решение
        пользователя 29.09.2026): приказ принадлежит договору, как
        трудоёмкость (18.09.2026). Вкладки реестра «Рабочие группы» нет."""
        self.assertNotIn('{ key: "wg", title: "Рабочие группы"', self.JS)
        self.assertNotIn('regTab === "wg"', self.JS)
        tabs = self.body("contractTabs")
        self.assertIn('["wg", "Рабочая группа", group.length]', tabs)
        self.assertLess(tabs.index('"Рабочая группа"'), tabs.index('"Трудоёмкость"'))
        self.assertIn("workgroupTable(group, x)", tabs)
        table = self.body("workgroupTable")
        # Приказ, загруженный во вкладке, привязывается к этому договору.
        self.assertIn("bar: { upload: code,", table)
        # Полосы сверки л/с и соглашения приказа с договором нет: договор
        # выбрал экономист (пользователь 29.09.2026: «зачем это окно?»).
        self.assertNotIn("Проверьте, тот ли это договор", table)
        self.assertNotIn("regnote", table)
        self.assertNotIn("function wgOrdersOf(", self.JS)

    def test_contract_tab_shows_basis_and_current_composition(self):
        """Приказы живут в договоре (29.09.2026): в таблице — действующий
        состав и строки, которые ещё надо сопоставить, без исключённых.
        Строки «Основание» над таблицей нет: приказ — ссылкой в графе «Приказ»
        своей строки (пользователь 29.09.2026: «может это убрать, а сделать
        переход к документу через гиперссылку в ячейке?»)."""
        table = self.body("workgroupTable")
        self.assertNotIn("wgbasis", table)
        self.assertNotIn("Основание: ", table)
        self.assertNotIn("function wgOrderLink(", self.JS)
        self.assertIn('{ k: "order", label: "Приказ",', table)
        self.assertIn("""'<span class="dname" data-doc="' + r.document_id""", table)
        self.assertIn('#regview .gridwrap[data-grid="ctr-wg"] td .dname{color:var(--acc)', self.CSS)
        shown = self.body("wgShownOf")
        self.assertIn("wgCurrent(rows).concat(rows.filter(function (r) { return !r.employee; }))",
                      shown)
        self.assertIn("var group = wgShownOf(code);", self.body("contractTabs"))

    def test_batch_is_sorted_out_in_documents(self):
        """Пачку приказов грузят общей загрузкой и разбирают во «Документах»
        как любые документы: графы у всех одни, своей графы и сводки у
        приказов нет (пользователь 29.09.2026: «в документах будут просто все
        вообще документы»). Договор указывают из меню строки — список встаёт
        под именем документа. Ссылки на группу — в договоре и в штатном
        расписании. Отдельной вкладки приказов в реестре нет."""
        docs = self.JS[self.JS.index('} else if (regTab === "docs") {'):
                       self.JS.index('} else if (regTab === "sub") {')]
        self.assertIn("catalogBlock(null, items.map(", docs)
        for gone in ("wgBatchNote", "wgDocGroup", "data-wgctr"):
            self.assertNotIn(gone, self.JS)
        cat = self.body("catalogBlock")
        self.assertNotIn("рабочая группа", cat)
        self.assertNotIn("workorders", cat)
        # В «Результате обработки» — только величина, части связаны словами
        # (UI-015), без поля выбора и без ссылки.
        self.assertIn("{ v: wgDocResult(v.id) ||", self.JS)
        result = self.body("wgDocResult")
        for gone in ("<select", " · ", "data-ctropen"):
            self.assertNotIn(gone, result)
        self.assertIn('"договор не найден"', result)
        self.assertIn('" в штатном расписании"', result)
        # Из меню строки — список под именем документа, а не у «⋮» в конце
        # строки (пользователь 29.09.2026: «криво окно появляется»).
        self.assertIn('label: wo && wo.contract ? "Сменить договор…" : "Указать договор…",', self.JS)
        self.assertIn('var at = (row && row.querySelector(".dname")) || b;', self.JS)
        self.assertIn("run: function () { docPickContract(at, +id, name, wo); } });", self.JS)
        pick = self.body("docPickContract")
        self.assertIn('pickFrom(anchor, items, "Найти договор"', pick)
        self.assertIn('o ? "/api/workorder/" + o.id + "/contract"', pick)
        # Приказ издают по договору, который уже есть: «Новый договор…» — не ему.
        self.assertIn('if (!o) items.push({ id: "", label: "Новый договор…", keep: true });', pick)
        # Похожие по л/с или соглашению — первыми в списке.
        self.assertIn("return (near(b) - near(a)) ||", pick)
        # Снизу не помещается — список над якорем, а не поверх него.
        self.assertIn("r.top - 4 - h", self.body("pickFrom"))
        tabs = self.JS[self.JS.index("var REG_TABS = ["):self.JS.index("];", self.JS.index("var REG_TABS"))]
        self.assertNotIn("Приказы", tabs)
        self.assertNotIn("Рабочие группы", tabs)

    def test_member_is_one_staff_row_chosen_by_one_field(self):
        """Табельный номер, ФИО и должность — одна строка штатного расписания:
        сотрудник выбирается одним списком по щелчку на ФИО, отдельной графы
        строки нет (пользователь 28.09.2026: «нельзя выбрать табельный номер
        одного сотрудника и ФИО другого»). Выбор — только явный: прежний
        раскрывающийся список сохранял его на любое изменение, и случайная
        стрелка записала Авдонюшкину строку Иванова (29.09.2026)."""
        table = self.body("workgroupTable")
        for gone in ("wgPicker", "select[data-wgrow]", 'addEventListener("change", function (e) {\n'
                     '  var t = e.target;\n  if (!t || !t.matches || !t.matches("select[data-wgrow]")'):
            self.assertNotIn(gone, self.JS)
        self.assertNotIn("Строка штатного расписания", table)
        self.assertLess(table.index('label: "Табельный номер"'), table.index('label: "ФИО"'))
        self.assertIn('data-wgpick="', table)
        self.assertIn("' data-miss title=\"Нет в штатном расписании — выбрать строку\">'", table)
        pick = self.body("wgPickRow")
        self.assertIn('pickFrom(anchor, items, "Найти сотрудника"', pick)
        # Снять ошибочный выбор можно тем же списком.
        self.assertIn('if (x.employee) items.push({ id: "", label: "Нет в штатном расписании", keep: true });',
                      pick)
        # Строки с табельным номером из приказа — первыми.
        self.assertIn("(mine(b) - mine(a))", pick)
        self.assertIn('e.target.closest("[data-wgpick]")', self.JS)
        self.assertIn('e.target.matches("[data-wgpick]")', self.JS)
        self.assertIn('#regview td .ed[data-miss]{color:var(--warn)}', self.CSS)
        # Приказ — ссылкой на документ; вид работ — целиком, с переносом, и
        # таблица встаёт в ширину панели (29.09.2026: «туда не помещается
        # описание вид работ»).
        self.assertIn('data-doc="', table)
        self.assertIn('{ k: "work", label: "Вид работ",', table)
        self.assertIn('{ v: esc(r.work), cls: "work" }', table)
        self.assertIn('#regview .gridwrap[data-grid="ctr-wg"] td.work{min-width:280px}', self.CSS)
        self.assertNotIn("clamp2", table)
        self.assertIn('#regview .gridwrap[data-grid="ctr-wg"] .tpanel td{white-space:normal}', self.CSS)

    def test_staff_shows_groups_of_each_row(self):
        """Со стороны людей: в штатном расписании графа «Рабочие группы» —
        договоры, в чьём действующем составе строка (пользователь
        29.09.2026). Последний приказ по строке решает: «исключить» убирает."""
        emp = self.JS[self.JS.index('regTab === "emp"'):self.JS.index('} else if (regTab === "zp")')]
        self.assertIn('{ k: "groups", label: "Рабочие группы",', emp)
        self.assertIn('data-ctropen="\' + esc(g.contract)', emp)
        # Шифр ведёт сразу во вкладку «Рабочая группа», а не на «Свойства»
        # (пользователь 29.09.2026: «будут ссылки на вложенные вкладки?»).
        self.assertIn("""data-ctropen="' + esc(g.contract) + '" data-ctrtab="wg\"""", emp)
        # Вкладка договора из ссылки уходит в переход с записью в адрес
        # (UI-021, 30.09.2026): «…/contracts/<шифр>/workgroup».
        self.assertIn('ctab: co.getAttribute("data-ctrtab") || "props" }, "push");', self.JS)
        of_row = self.body("wgOfRow")
        self.assertIn('if (r.action === "исключить") return null;', of_row)
        self.assertIn("r.to && wgDay(r.to) < now", of_row)


class RoutingTests(unittest.TestCase):
    def test_assigning_a_contract_rereads_the_order_by_its_own_form(self):
        """«Указать договор…» из очереди перечитывает приказ его разбором, а не
        общим: общий искал бы в нём договоры и сотрудников заново."""
        src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        at = src.index("async def assign_document_contract(")
        body = src[at:src.index("\n@app.", at)]
        self.assertIn("if doc.kind == workgroup.KIND:", body)
        self.assertIn("intake.run_workgroup(db, case, doc)", body)
        intake = (ROOT / "app" / "intake.py").read_text(encoding="utf-8")
        self.assertIn("contract_code=chosen or _find_contract(db, p),", intake)


if __name__ == "__main__":
    unittest.main()
