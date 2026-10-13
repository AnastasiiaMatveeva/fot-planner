"""SOURCE-004 и UI-006: пакет договора читается по смыслу граф.

Книги стенда собираются здесь же — в разных раскладках и с разными названиями
листов: разборщик обязан найти одни и те же данные по подписям граф, а не по
номеру формы. Демо-формы из data/demo сверяются с тем, что уже проверено в
harness/cases.py. Сервис — на временной базе, без модели и решателя.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "docs" / "ui"))

import extract  # noqa: E402

#: Трудоёмкость по году: шапка в две строки, строка группы «2026 год, в т.ч.»
#: со сроком, итог по группе и «в том числе» — как в расшифровке ОЗП.
YEARLY = {
    "A2": "Расшифровка основной заработной платы, шифр C_A-26",
    "B4": "Наименование этапа", "C4": "Срок выполнения этапа",
    "D4": "Количество привлекаемых специалистов",
    "E4": "Квалификационная группа основных работников",
    "F4": "Трудоемкость и заработная плата",
    "F5": "трудоемкость выполнения работ (чел./мес.)", "G5": "стоимость чел./мес. (руб.)",
    "H5": "основная заработная плата (гр.6*гр.7)",
    "A6": 1, "B6": 2, "C6": 3, "D6": 4, "E6": 5, "F6": 6, "G6": 7, "H6": 8,
    "B7": "2026 год, в т.ч.", "C7": "01.07.2026 - 31.12.2026",
    "E7": "Научные и инженерно-технические работники", "F7": 18, "G7": 100000, "H7": 1800000,
    "E8": "в том числе:",
    "D9": 1, "E9": "Ведущий инженер", "F9": 6, "G9": 120000, "H9": 720000,
    "D10": 2, "E10": "Инженер", "F10": 12, "G10": 90000, "H10": 1080000,
    "A11": "ИТОГО:", "F11": 18, "H11": 1800000,
}

#: Те же люди и деньги по этапам: одна строка шапки, графы в другом порядке,
#: даты этапа в каждой строке — как в расшифровке ФОТ структуры цены.
STAGED = {
    "B2": "Расшифровка расходов на оплату труда",
    "A4": "Должность", "B4": "Сумма месячной з/пл., руб.", "C4": "Кол-во человек",
    "D4": "Кол-во чел/мес.", "E4": "Итого ФОТ, руб.", "F4": "№ этапа",
    "G4": "Начало этапа", "H4": "Окончание этапа",
    "A5": "Ведущий инженер", "B5": 120000, "C5": 1, "D5": 3, "E5": 360000,
    "F5": "Этап 1", "G5": "01.07.2026", "H5": "30.09.2026",
    "A6": "Инженер", "B6": 90000, "C6": 2, "D6": 6, "E6": 540000,
    "F6": "Этап 1", "G6": "01.07.2026", "H6": "30.09.2026",
    "A7": "Ведущий инженер", "B7": 120000, "C7": 1, "D7": 3, "E7": 360000,
    "F7": "Этап 2", "G7": "01.10.2026", "H7": "31.12.2026",
    "A8": "Инженер", "B8": 90000, "C8": 2, "D8": 6, "E8": 540000,
    "F8": "Этап 2", "G8": "01.10.2026", "H8": "31.12.2026",
}

#: Лист без нужных граф: в пакете он есть, но оптимизатору не нужен.
MATERIALS = {
    "A2": "Расшифровка статьи «Материалы и комплектующие»",
    "A4": "№ п/п", "B4": "Наименование материала", "C4": "Кол-во, шт.",
    "D4": "Цена за ед., руб.", "E4": "Сумма, руб.",
    "A5": 1, "B5": "Кабель", "C5": 10, "D5": 500, "E5": 5000,
}

NORMS = {
    "A1": "Сведения о нормативах и экономических показателях",
    "A3": "№ п/п", "B3": "Наименование", "C3": "Единица измерения", "D3": "Значение",
    "A4": "1.1", "B4": "среднемесячная заработная плата основных работников",
    "C4": "руб.", "D4": 112261,
    "A5": "1.2", "B5": "дополнительная заработная плата основных работников",
    "C5": "%", "D5": 8.6866,
    "A6": "2.1", "B6": "страховые взносы", "C6": "%", "D6": 30,
    "A7": "2.2", "B7": "страховые взносы на обязательное страхование от несчастных случаев",
    "C7": "%", "D7": 0.2,
}

ARTICLES = {
    "A1": "СТРУКТУРА ЦЕНЫ",
    "A3": "№ п/п", "B3": "Предметные статьи расходов", "C3": "Сумма, руб.",
    "D3": "Этап 1", "E3": "Этап 2",
    "A4": "1.3.1", "B4": "основная заработная плата", "C4": 1800000, "D4": 900000, "E4": 900000,
    "A5": "1.3.2", "B5": "дополнительная заработная плата", "C5": 156359,
    "D5": 78179.5, "E5": 78179.5,
    "A6": "2.1", "B6": "расходы на оплату труда административно-управленческого аппарата",
    "D6": 5, "E6": 5,
}


def book(folder, name, sheets):
    """Книга из {лист: {адрес: значение}}."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, cells in sheets.items():
        ws = wb.create_sheet(title)
        for addr, value in cells.items():
            ws[addr] = value
    path = os.path.join(folder, name)
    wb.save(path)
    return path


def labor_plan(passport):
    return sorted((r["contract"], r["year"], r["position"], r["person_months"], r["avg_cost"],
                   r["headcount"], tuple(sorted((r["months"] or {}).items(), key=lambda x: int(x[0]))))
                  for r in passport["labor"])


def values(passport, field):
    return [v for v in passport["values"] if v["field"] == field]


class ReadByMeaningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_same_labor_from_different_layouts_and_sheet_names(self):
        yearly = extract.extract(book(self.tmp.name, "по годам.xlsx",
                                      {"Трудозатраты по годам": YEARLY}))
        staged = extract.extract(book(self.tmp.name, "по этапам.xlsx", {"Лист1": STAGED}),
                                 contract="C_A-26")
        want = [("C_A-26", 2026, "Ведущий инженер", 6.0, 120000.0, 1.0,
                 tuple((str(m), 1.0) for m in range(7, 13))),
                ("C_A-26", 2026, "Инженер", 12.0, 90000.0, 2.0,
                 tuple((str(m), 2.0) for m in range(7, 13)))]
        self.assertEqual(labor_plan(yearly["passport"]), want)
        self.assertEqual(labor_plan(staged["passport"]), want)
        self.assertEqual(yearly["questions"], [])
        self.assertEqual(staged["questions"], [])
        # Адреса ячеек — для маски «Извлечённые данные» в карточке документа.
        cells = staged["passport"]["labor"][0]["details"][0]["cells"]
        self.assertEqual((cells["position"], cells["avg_cost"], cells["from"]),
                         ("Лист1!A5", "Лист1!B5", "Лист1!G5"))

    def test_sheets_without_needed_fields_are_not_read(self):
        out = extract.extract(book(self.tmp.name, "пакет.xlsx",
                                   {"Материалы": MATERIALS, "Лист1": STAGED}), contract="C_A-26")
        self.assertEqual(len(out["passport"]["labor"]), 2)
        self.assertEqual([line for line in out["log"] if "Материалы" in line], [])
        self.assertIn("Листов без нужных граф: 1 — не читались", out["log"])

    def test_blank_sample_gives_no_rows(self):
        blank = {k: (0 if isinstance(v, (int, float)) else v) for k, v in STAGED.items()}
        blank.update({"G5": "хх.хх.хххх", "H5": "хх.хх.хххх"})
        out = extract.extract(book(self.tmp.name, "образец.xlsx", {"Расшифровка": blank}),
                              contract="C_A-26")
        self.assertEqual(out["passport"]["labor"], [])
        self.assertEqual(values(out["passport"], "основная заработная плата"), [])
        self.assertTrue(any("образец без значений" in line for line in out["log"]), out["log"])

    def test_norms_by_year_and_period_fot_split_by_stage_dates(self):
        out = extract.extract(book(self.tmp.name, "полный.xlsx",
                                   {"Статьи": ARTICLES, "Лист1": STAGED, "нормативы_26": NORMS}),
                              contract="C_A-26")
        p = out["passport"]
        norms = {(v["field"], v["year"]): v["value"] for v in p["values"] if v["unit"] in ("%", "руб.")
                 and v["stage"] is None}
        self.assertEqual(norms[("среднемесячная заработная плата, руб.", 2026)], 112261)
        self.assertEqual(norms[("дополнительная заработная плата, %", 2026)], 8.6866)
        self.assertEqual(norms[("страховые взносы, %", 2026)], 30)
        self.assertEqual(norms[("взносы от несчастных случаев, %", 2026)], 0.2)
        ozp = values(p, "основная заработная плата")
        self.assertEqual(sorted((v["stage"], v["year"], v["value"], v["text"]) for v in ozp),
                         [("Этап 1", 2026, 900000, "01.07.2026–30.09.2026"),
                          ("Этап 2", 2026, 900000, "01.10.2026–31.12.2026")])
        # Оплата административного аппарата — не ФОТ исполнителей.
        self.assertFalse([v for v in p["values"] if v["value"] == 5])
        self.assertEqual(out["questions"], [])
        term = {v["field"]: v["text"] for v in p["values"] if v["field"].startswith("срок")}
        self.assertEqual(term, {"срок: начало": "01.07.2026", "срок: окончание": "31.12.2026"})

    def test_stage_fot_mismatch_between_articles_and_labor_is_asked(self):
        articles = dict(ARTICLES, D4=950000)
        out = extract.extract(book(self.tmp.name, "расхождение.xlsx",
                                   {"Статьи": articles, "Лист1": STAGED}), contract="C_A-26")
        self.assertEqual([q["field"] for q in out["questions"]],
                         ["Основная заработная плата, Этап 1"])

    def test_document_in_contract_uses_that_contract(self):
        out = extract.extract(book(self.tmp.name, "чужой шифр.xlsx",
                                   {"Трудозатраты": YEARLY, "нормативы_26": NORMS}),
                              contract="C_NEW-26")
        self.assertEqual({r["contract"] for r in out["passport"]["labor"]}, {"C_NEW-26"})
        self.assertEqual({v["contract"] for v in out["passport"]["values"]}, {"C_NEW-26"})

    def test_demo_forms_read_by_meaning(self):
        rkm = extract.extract(str(ROOT / "data" / "demo" / "РКМ ГОЗ Радар-26.xlsx"))["passport"]
        rows = {r["position"]: r for r in rkm["labor"]}
        self.assertEqual(len(rows), 3)
        self.assertEqual((rows["Инженер"]["person_months"], rows["Инженер"]["headcount"]), (16, 2))
        self.assertEqual(rows["Старший научный сотрудник"]["avg_cost"], 124000)
        self.assertEqual([(v["year"], v["value"]) for v in values(rkm, "основная заработная плата")],
                         [(2026, 3592352)])
        self.assertEqual([(v["year"], v["value"]) for v in
                          values(rkm, "среднемесячная заработная плата, руб.")], [(2026, 112261)])
        grant = extract.extract(str(ROOT / "data" / "demo" / "Структура цены Грант-26.xlsx"))
        scientist = [r for r in grant["passport"]["labor"] if r["position"] == "Научный сотрудник"][0]
        self.assertEqual((scientist["contract"], scientist["person_months"]), ("C_GRANT-26", 8))
        self.assertEqual(scientist["months"], {str(m): 1.0 for m in range(5, 13)})


class Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class PackageServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lifecycle import App
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, FOT_OPERATOR="package-test-operator")
        cls.env.start()
        cls.app = App(cls.temp.name)
        import basis
        import intake
        cls.basis, cls.intake = basis, intake

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
                      d.Substitution, d.ContractValue, d.Proposal, d.Question, d.Document,
                      d.Decision, d.Message, d.Run):
            self.db.query(model).delete()
        self.case = self.app.case
        self.case.year, self.case.plan_basis = 2026, None
        self.db.commit()

    def create(self, code, name="Договор"):
        return asyncio.run(self.main.create_contract(Req({"code": code, "name": name})))

    def package_doc(self, code, sheets, name="пакет.xlsx"):
        path = book(self.temp.name, name, sheets)
        doc = self.d.Document(name=name, path=path, state="ожидает", kind="документ по договору",
                              case_id=self.case.id, scope="реестр", contract_code=code)
        self.db.add(doc)
        self.db.commit()
        self.intake.run_intake(self.db, self.case, doc)
        return doc

    # ── договор заводится из шапки документа (SOURCE-005) ──
    def test_file_without_contract_creates_it_from_header(self):
        """Экономист не перепечатывает шифр, который написан в самом файле."""
        doc = self.package_doc(None, {"Трудозатраты": dict(
            YEARLY, A2="Расшифровка ОЗП, шифр C_SELF-26",
            A3="на выполнение работы по теме: «Разработка стенда нагрева»")},
            name="без договора.xlsx")
        row = self.db.query(self.d.Contract).filter_by(code="C_SELF-26").one()
        self.assertEqual(row.name, "Разработка стенда нагрева")
        # Документ уезжает в каталог заведённого договора, а не остаётся ничьим.
        self.assertEqual(doc.contract_code, "C_SELF-26")
        decision = (self.db.query(self.d.Decision)
                    .filter_by(subject_kind="договор", subject_id="C_SELF-26").one())
        self.assertEqual(decision.kind, "данные")

    def test_existing_name_is_not_replaced_by_next_document(self):
        """У пачки одного договора темы разные: последний файл не переименовывает."""
        self.create("C_SELF-26", "Рабочее название")
        self.package_doc(None, {"Трудозатраты": dict(
            YEARLY, A2="шифр C_SELF-26",
            A3="по теме: «Другая формулировка из формы»")}, name="второй.xlsx")
        self.assertEqual(self.db.query(self.d.Contract)
                         .filter_by(code="C_SELF-26").one().name, "Рабочее название")

    def test_blank_sample_creates_no_contract(self):
        """Заглушка образца договором не становится: его потом ищут глазами."""
        before = self.db.query(self.d.Contract).count()
        self.package_doc(None, {"Трудозатраты": dict(
            YEARLY, A2="Расшифровка ОЗП", A3="по теме: «ТЕМА»")}, name="образец.xlsx")
        self.assertEqual(self.db.query(self.d.Contract)
                         .filter(self.d.Contract.code.like("%ТЕМА%")).count(), 0)
        self.assertGreaterEqual(self.db.query(self.d.Contract).count(), before)

    def test_document_without_code_is_assigned_by_economist(self):
        """Единственный ручной ввод шифра — документ, в котором его нет:
        договор заводится, документ перечитывается уже под ним."""
        doc = self.package_doc(None, {"Трудозатраты": dict(YEARLY, A2="Расшифровка ОЗП")},
                               name="без шифра.xlsx")
        self.assertIsNone(doc.contract_code)
        asyncio.run(self.main.assign_document_contract(doc.id, Req({"code": "C_HAND-26"})))
        self.db.expire_all()
        doc = self.db.get(self.d.Document, doc.id)
        self.assertEqual(doc.contract_code, "C_HAND-26")
        self.assertIsNotNone(self.db.query(self.d.Contract).filter_by(code="C_HAND-26").first())
        self.assertEqual({r.contract_code for r in self.db.query(self.d.LaborRow)
                          .filter_by(document_id=doc.id)}, {"C_HAND-26"})
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            asyncio.run(self.main.assign_document_contract(doc.id, Req({"code": " "})))

    # ── графы, которых нет в документах, вводит экономист (SOURCE-007) ──
    def test_manual_field_is_saved_and_recorded(self):
        doc = self.package_doc(None, {"Трудозатраты": YEARLY}, name="рkm.xlsx")
        self.assertIsNone(self.db.query(self.d.Contract).filter_by(code="C_A-26").one().account)
        out = asyncio.run(self.main.rename_contract(
            "C_A-26", Req({"fields": {"account": "20576У17499", "goz": "да",
                                      "fund": "1 250 000,50", "kinds": "оклад, 120"}})))
        self.assertEqual(set(out["changed"]), {"account", "goz", "fund", "kinds"})
        row = self.db.query(self.d.Contract).filter_by(code="C_A-26").one()
        self.assertEqual((row.account, row.goz, row.fund, row.kinds),
                         ("20576У17499", "да", 1250000.5, "оклад, 120"))
        decision = (self.db.query(self.d.Decision)
                    .filter_by(subject_kind="договор", subject_id="C_A-26")
                    .order_by(self.d.Decision.id.desc()).first())
        self.assertIn("графы", decision.action)
        # Ручной ввод запомнен отдельно от строки документа.
        self.assertEqual({v.field for v in self.db.query(self.d.ContractValue)
                          .filter_by(contract_code="C_A-26", document_id=None)},
                         {"ручной ввод: account", "ручной ввод: goz",
                          "ручной ввод: fund", "ручной ввод: kinds"})

    def test_manual_field_survives_reparse(self):
        """Разбор стирает и создаёт строку договора заново — ручной ввод
        экономиста документом не отменяется."""
        self.package_doc(None, {"Трудозатраты": YEARLY}, name="рkm.xlsx")
        asyncio.run(self.main.rename_contract("C_A-26", Req({"fields": {"account": "20576У17499"}})))
        # Как после повторного разбора: строка договора пересоздана пустой.
        self.db.query(self.d.Contract).filter_by(code="C_A-26").delete()
        self.db.add(self.d.Contract(code="C_A-26", source="документ"))
        self.db.commit()
        self.package_doc(None, {"Трудозатраты": YEARLY}, name="рkm-2.xlsx")
        self.assertEqual(self.db.query(self.d.Contract).filter_by(code="C_A-26").one().account,
                         "20576У17499")

    def test_empty_manual_value_clears_field(self):
        self.package_doc(None, {"Трудозатраты": YEARLY}, name="рkm.xlsx")
        asyncio.run(self.main.rename_contract("C_A-26", Req({"fields": {"priority": "1"}})))
        asyncio.run(self.main.rename_contract("C_A-26", Req({"fields": {"priority": ""}})))
        self.assertIsNone(self.db.query(self.d.Contract).filter_by(code="C_A-26").one().priority)
        self.assertEqual(self.db.query(self.d.ContractValue)
                         .filter_by(contract_code="C_A-26", field="ручной ввод: priority").count(), 0)

    def test_rename_changes_name_but_not_code(self):
        """Шифр опознаёт договор и переименованием не меняется (UI-009)."""
        self.create("C_SELF-26", "Было")
        asyncio.run(self.main.rename_contract("C_SELF-26", Req({"name": "Стало"})))
        row = self.db.query(self.d.Contract).filter_by(code="C_SELF-26").one()
        self.assertEqual((row.code, row.name), ("C_SELF-26", "Стало"))

    def test_contract_is_created_before_documents(self):
        self.assertEqual(self.create("C_NEW-26", "НИР «Новая»")["code"], "C_NEW-26")
        row = self.db.query(self.d.Contract).filter_by(code="C_NEW-26").one()
        self.assertEqual((row.name, row.document_id, row.fund), ("НИР «Новая»", None, None))
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as dup:
            self.create("C_NEW-26")
        self.assertEqual(dup.exception.status_code, 409)
        with self.assertRaises(HTTPException) as blank:
            self.create("  ")
        self.assertEqual(blank.exception.status_code, 400)
        decision = self.db.query(self.d.Decision).filter_by(subject_kind="договор").one()
        self.assertEqual((decision.kind, decision.actor), ("данные", "package-test-operator"))

    def test_upload_into_contract_fills_empty_term_and_fund(self):
        self.create("C_NEW-26")
        doc = self.package_doc("C_NEW-26", {"Статьи": ARTICLES, "Лист1": STAGED,
                                            "нормативы_26": NORMS})
        labor = self.db.query(self.d.LaborRow).all()
        self.assertEqual({(r.contract_code, r.document_id) for r in labor}, {("C_NEW-26", doc.id)})
        warn = []
        data = self.main._registry_data(self.db, self.case, warn)
        ctr = [c for c in data["contracts"] if c.code == "C_NEW-26"][0]
        self.assertEqual((ctr.fund, ctr.date_from, ctr.date_to),
                         (1800000, "01.07.2026", "31.12.2026"))
        self.assertTrue(any("ФОТ договора C_NEW-26 на 2026 год — из пакета" in w for w in warn), warn)
        self.assertIn(doc.id, data["row_sources"])
        # Реестр не переписан: заполнена копия для сборки.
        self.db.commit()
        self.db.expire_all()
        row = self.db.query(self.d.Contract).filter_by(code="C_NEW-26").one()
        self.assertEqual((row.fund, row.date_from), (None, None))
        roles = {r["key"]: r for r in self.basis.package(self.db, "C_NEW-26", 2026)}
        self.assertEqual({k: r["state"] for k, r in roles.items()},
                         {"term": "ok", "fund": "ok", "labor": "ok", "norms": "ok",
                          })
        # Роли «Касса» нет с 18.09.2026: весь ФОТ договора считается
        # поступившим в первый месяц выплат (ПРАВИЛА_РЕШАТЕЛЯ, правило 21;
        # SOURCE-009), и допускать про кассу больше нечего.
        self.assertEqual(roles["labor"]["value"], "18 чел.-мес.")
        self.assertEqual(roles["fund"]["source"], "пакет.xlsx")
        # Значения, а не только состояние: экономист перед запуском смотрит на
        # числа, а имя файла на месте значения читалось как значение.
        self.assertEqual(roles["fund"]["origin"], "пакет")
        self.assertRegex(roles["fund"]["value"], r"^\d[\d ]* ₽$")
        self.assertEqual(roles["term"]["origin"], "пакет")
        self.assertRegex(roles["term"]["value"], r"^\d\d\.\d\d\.\d{4} – \d\d\.\d\d\.\d{4}$")

    def test_registry_values_win_and_contract_without_fund_is_left_out(self):
        self.db.add(self.d.Contract(code="C_REG-26", fund=1000000, date_from="01.01.2026",
                                    date_to="31.12.2026", source="Договоры 2026.xlsx"))
        self.db.commit()
        self.create("C_EMPTY-26")
        self.package_doc("C_REG-26", {"Статьи": ARTICLES, "Лист1": STAGED})
        warn = []
        data = self.main._registry_data(self.db, self.case, warn)
        by_code = {c.code: c for c in data["contracts"]}
        self.assertEqual(set(by_code), {"C_REG-26"})
        self.assertEqual((by_code["C_REG-26"].fund, by_code["C_REG-26"].date_from),
                         (1000000, "01.01.2026"))
        self.assertTrue(any("в реестре 1 000 000 ₽, в пакете на 2026 год 1 800 000 ₽" in w
                            for w in warn), warn)
        self.assertTrue(any("C_EMPTY-26: ФОТ на 2026 год нет" in w for w in warn), warn)
        roles = {r["key"]: r["state"] for r in self.basis.package(self.db, "C_EMPTY-26", 2026)}
        # Без «Кассы»: правило 21, решение пользователя 18.09.2026.
        self.assertEqual(roles, {"term": "missing", "fund": "missing", "labor": "missing",
                                 "norms": "missing"})

    # Несколько документов пакета и чужие годы: в расчёт — одна трудоёмкость на
    # договор и год, строки модели — под договором пакета. Методы того же класса:
    # второй стенд в одном процессе остался бы на базе первого.
    def test_labor_from_one_document_per_year(self):
        self.create("C_TWO-26")
        self.package_doc("C_TWO-26", {"Лист1": STAGED}, name="первый.xlsx")
        more = dict(STAGED, D5=4, E5=480000, D7=4, E7=480000)
        second = self.package_doc("C_TWO-26", {"Лист1": more}, name="второй.xlsx")
        warn = []
        data = self.main._registry_data(self.db, self.case, warn)
        rows = [r for r in data["labor"] if r.contract_code == "C_TWO-26"]
        self.assertEqual({r.document_id for r in rows}, {second.id})
        self.assertEqual(sorted((r.position, r.person_months) for r in rows),
                         [("Ведущий инженер", 8.0), ("Инженер", 12.0)])
        self.assertTrue(any("C_TWO-26 на 2026 год есть в нескольких документах пакета" in w
                            for w in warn), warn)
        roles = {r["key"]: r for r in self.basis.package(self.db, "C_TWO-26", 2026)}
        self.assertEqual((roles["labor"]["value"], roles["labor"]["source"]),
                         ("20 чел.-мес.", "второй.xlsx"))

    def test_labor_row_of_other_year_keeps_its_months(self):
        import build_input
        from types import SimpleNamespace as NS
        ws = openpyxl.load_workbook(ROOT / "docs" / "ui" / "demo_input.xlsx")["трудоемкость_по_договорам"]
        contract = NS(code="C_Y", date_from="01.04.2026", date_to="31.12.2027", salary_deadline=None)
        rows = [NS(contract_code="C_Y", year=year, position="Инженер", salary_page=None,
                   salary_group=None, position_level=None, person_months=6.0, avg_cost=100000.0,
                   headcount=1, months='{"1": 0.5, "2": 0.5, "3": 0.5, "4": 0.5, "5": 0.5, '
                                      '"6": 0.5, "7": 0.5, "8": 0.5, "9": 0.5, "10": 0.5, '
                                      '"11": 0.5, "12": 0.5}')
                for year in (2026, 2027)]
        warn = []
        build_input.fill_labor(ws, rows, warn, [contract], 2026)
        late = [w for w in warn if "после предела выплат" in w]
        # Сдвиг в окно выплат остаётся только у строки года плана.
        self.assertEqual(len(late), 1, warn)

    def test_model_rows_from_package_document_bind_to_its_contract(self):
        from types import SimpleNamespace as NS
        doc = NS(contract_code="C_PKG-26")
        bind = self.intake._bind_to_contract
        self.assertEqual(bind(doc, "трудоемкость", {"contract": "ТЕМА"})["contract"], "C_PKG-26")
        self.assertEqual(bind(doc, "поступление", {"contract": None})["contract"], "C_PKG-26")
        self.assertEqual(bind(doc, "договор", {"code": "ТЕМА"})["code"], "C_PKG-26")
        self.assertEqual(bind(doc, "надбавка 120", {"contract": "C_S"})["contract"], "C_S")
        self.assertEqual(bind(NS(contract_code=None), "трудоемкость", {"contract": "C_Z"})["contract"],
                         "C_Z")
        self.db.add(self.d.LaborRow(contract_code="C_PKG-26", year=2026, position="Инженер",
                                    person_months=6))
        self.db.commit()
        self.assertTrue(self.intake._known(self.db, "трудоемкость",
                                           bind(doc, "трудоемкость",
                                                {"contract": "ТЕМА", "position": "Инженер"})))


JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "static" / "app.css").read_text(encoding="utf-8")


class PackageUiTests(unittest.TestCase):
    def test_contract_is_created_from_files_not_typed_code(self):
        """«Создать договор» открывает выбор файлов, а не ввод шифра.

        Кнопка возвращена 17.09.2026 решением пользователя по макету
        NewContract.dc.html. Требование SOURCE-005 при этом сохраняется: шифр
        и наименование берутся из самих документов, поля ввода шифра в окне
        нет — перепечатывать написанное в документе экономист не должен.
        """
        start = JS.index("function newContractDialog()")
        body = JS[start:JS.index(chr(10) + "function ", start + 10)]
        self.assertIn("Создание договора", body)
        self.assertIn('type="file"', body)
        self.assertIn("sendFiles(picked)", body)
        # Ручного ввода шифра и наименования в окне нет.
        self.assertNotIn('name="code"', body)
        self.assertNotIn("нужен шифр", body)
        self.assertNotIn("regnew", JS)
        # Единственная загрузка в списке — общая; на экране договора — своя.
        self.assertIn("Договоры появятся сами", JS)
        # Пять рамок состава пакета отклонены 16.09.2026. С 17.09.2026 на
        # экране договора состав виден построчно в реквизитах — значение и его
        # источник; в списке остаётся состояние одной графой.
        self.assertIn("function requisitesBlock(x, roles)", JS)
        self.assertIn("requisitesBlock(x, (bs.packages || {})[code])", JS)
        # Файлы, добавленные при создании, не остаются без договора (решение
        # пользователя 18.09.2026): окно ждёт разбора и прикрепляет файл без
        # шифра к договору остальных файлов. Шифр вручную — только когда его
        # нет ни в одном документе: это исключение SOURCE-005, а не поле ввода
        # в окне заранее.
        attach = JS[JS.index("function attachCreated("):]
        attach = attach[:attach.index(chr(10) + "function ")]
        self.assertIn('"/api/document/" + d.id + "/contract"', attach)
        self.assertIn("found.length === 1", attach)
        self.assertIn("В документах шифра договора нет", attach)
        self.assertIn("attachCreated(wrap, ids, close)", body)
        # Графы «пакет» в списке больше нет — решение пользователя
        # 17.09.2026 («убери столбец пакет, это не нужно»); состав пакета
        # виден построчно в свойствах договора.
        self.assertNotIn("function packageState", JS)
        self.assertNotIn("function packageStrip", JS)


if __name__ == "__main__":
    unittest.main()
