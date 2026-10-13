"""Демо-комплект для загрузки руками (пользователь 30.09.2026: «разложи по
папкам документы, чтобы в реальном времени я подгрузила все сама»; «я хочу,
чтобы ты взял разные таблицы для каждого договора по трудоемкости, чтоб
показать, что поддерживаются разные виды»).

Генератор scripts/create_demo_pack.py пишет папки во временный каталог;
каждый документ сервис читает тем разбором, каким его прочтёт при загрузке:
штатное — по листам, зарплата и факт — по шапке, документы договора — по
смыслу граф. Трудоёмкость четырёх договоров — четырьмя разными таблицами.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "docs" / "ui"))
sys.path.insert(0, str(ROOT / "harness"))
PY = str(ROOT / ".venv" / "Scripts" / "python.exe") if os.name == "nt" else sys.executable


class DemoPackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Разбор зарплаты и факта живёт в модулях приложения, а они тянут
        # базу: стенд на временной папке — до их импорта (DEV-STAND-ISOLATION-001).
        from lifecycle import App
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, FOT_OPERATOR="demo-pack-operator")
        cls.env.start()
        stand = os.path.join(cls.temp.name, "stand")
        os.makedirs(stand)
        cls.app = App(stand)
        cls.out = Path(cls.temp.name) / "pack"
        fact = Path(cls.temp.name) / "jan.json"
        fact.write_text(json.dumps({"E001": [
            {"договор": "C_BASE-26", "вид": "оклад", "сумма": 47000, "ставка": 1.0},
            {"договор": "C_BASE-26", "вид": "122", "сумма": 93000, "ставка": 1.0},
            {"договор": "C_BASE-26", "вид": "приказ", "сумма": 5000, "ставка": 1.0}]},
            ensure_ascii=False), encoding="utf-8")
        subprocess.run([PY, str(ROOT / "scripts" / "create_demo_pack.py"), "--out", str(cls.out),
                        "--fact", str(fact)], check=True, capture_output=True, cwd=ROOT,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.app.db.close()
        cls.app.d.engine.dispose()
        cls.env.stop()
        cls.temp.cleanup()

    def one(self, pattern):
        found = sorted(self.out.glob(pattern))
        self.assertEqual(len(found), 1, pattern)
        return str(found[0])

    def labor(self, pattern):
        import extract
        return extract.extract(self.one(pattern))["passport"]["labor"]

    def test_folders_in_upload_order(self):
        tops = sorted(p.name for p in self.out.iterdir() if p.is_dir())
        self.assertEqual(tops, ["1 Штатное расписание", "2 Договоры", "3 Заработная плата",
                                "4 Факт начислений"])
        ctrs = sorted(p.name.split()[0] for p in (self.out / "2 Договоры").iterdir())
        self.assertEqual(ctrs, ["C_BASE-26", "C_COM-26", "C_GOZ-26", "C_GRANT-26", "C_MON-26",
                                "C_PRI-26", "C_SENS-26"])
        import extract
        # Штатное — без зарплаты (она по месяцам в своём документе) и читается.
        staff = extract.extract(self.one("1 Штатное расписание/*.xlsx"))["passport"]
        self.assertEqual(len(staff["employees"]), 12)
        import openpyxl
        head = [c.value for c in openpyxl.load_workbook(
            self.one("1 Штатное расписание/*.xlsx"))["сотрудники"][1]]
        self.assertNotIn("зарплата", head)
        # В каждой папке договора — его карточка: ФОТ одной суммой, без
        # поступлений по месяцам.
        for folder in (self.out / "2 Договоры").iterdir():
            card = extract.extract(self.one("2 Договоры/%s/Договор *.xlsx" % folder.name))
            self.assertEqual([c["code"] for c in card["passport"]["contracts"]],
                             [folder.name.split()[0]])
            self.assertEqual(card["passport"]["inflow"], {})

    def test_labor_tables_of_four_kinds(self):
        # РКМ ГОЗ, форма 9 (9д): чел.-мес. по должностям.
        goz = self.labor("2 Договоры/C_GOZ-26*/РКМ*.xlsx")
        self.assertEqual(sorted(l["position"] for l in goz),
                         ["Ведущий инженер", "Инженер", "Старший научный сотрудник"])
        # Структура цены расширенная: этапы с датами — май–декабрь.
        grant = self.labor("2 Договоры/C_GRANT-26*/Структура*.xlsx")
        self.assertEqual({tuple(sorted(int(m) for m in l["months"])) for l in grant},
                         {tuple(range(5, 13))})
        # Структура цены простая: часы переведены в чел.-мес. по нормативу формы.
        sens = self.labor("2 Договоры/C_SENS-26*/Структура*.xlsx")
        self.assertEqual(len(sens), 1)
        self.assertAlmostEqual(sens[0]["person_months"], 10.0, places=3)
        self.assertEqual(sorted(int(m) for m in sens[0]["months"]), list(range(2, 12)))
        # План работ по месяцам: все двенадцать месяцев, своя загрузка.
        mon = self.labor("2 Договоры/C_MON-26*/План*.xlsx")
        self.assertEqual(len(mon), 1)
        self.assertEqual(sorted(int(m) for m in mon[0]["months"]), list(range(1, 13)))
        self.assertEqual(mon[0]["months"]["1"], 0.5)
        self.assertEqual(mon[0]["months"]["12"], 1.0)

    def test_only_yellow_parts_of_samples(self):
        import openpyxl
        rkm = openpyxl.load_workbook(self.one("2 Договоры/C_GOZ-26*/РКМ*.xlsx"))
        self.assertEqual(rkm.sheetnames, ["Прил. № 5 Ф1(1д) прот", "9 (9д) ОЗП НИР(ОКР)",
                                          "22 (22д) свед. о норм_26"])
        simple = openpyxl.load_workbook(self.one("2 Договоры/C_SENS-26*/Структура*.xlsx"))
        self.assertEqual(simple.sheetnames, ["структура цены", "Расшифровка ФОТ"])
        wide = openpyxl.load_workbook(self.one("2 Договоры/C_GRANT-26*/Структура*.xlsx"))
        self.assertEqual(wide.sheetnames, ["Структура цены", "Расшифровка ФОТ"])

    def test_salary_rises_once_and_is_read_by_months(self):
        import intake
        path = self.one("3 Заработная плата/*.xlsx")
        self.assertIsNotNone(intake.salary_layout(path))
        import openpyxl
        ws = openpyxl.load_workbook(path).active
        rows = {r[0]: r[2:] for r in ws.iter_rows(min_row=2, values_only=True)}
        self.assertEqual(rows["E001"][7], 145000)
        self.assertEqual(rows["E001"][8], 155000)
        self.assertEqual(len(set(rows["E001"])), 2)
        # Лебедев нанят на срок хоздоговора: январь и декабрь пусты.
        self.assertIsNone(rows["E011"][0])
        self.assertIsNone(rows["E011"][11])

    def test_fact_is_january_in_form_63(self):
        import facts
        path = self.one("4 Факт начислений/*.xlsx")
        where = facts.layout(path)
        self.assertEqual(where["form"], "6.3")
        rows, skipped = facts.parse(path, 2026, where)
        self.assertEqual(skipped, [])
        self.assertEqual({r["month"] for r in rows}, {1})
        self.assertEqual(round(sum(r["amount"] for r in rows)), 145000)

    def test_month_plan_reads_every_month(self):
        """Лист трудоёмкости по месяцам читает все двенадцать графах месяцев:
        основы месяцев фраз чата прежде затирали полные названия, и
        оставались март и август."""
        import extract
        self.assertEqual(len(extract.MONTH_NAMES), 12)
        self.assertEqual(extract.MONTH_NAMES[0], "январь")


if __name__ == "__main__":
    unittest.main()
