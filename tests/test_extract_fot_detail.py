"""Полнота извлечения строк листа «Расшифровка ФОТ»."""

from pathlib import Path
import sys

from openpyxl import Workbook


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docs" / "ui"))

import extract  # noqa: E402


def test_stage_rows_are_preserved_beside_aggregated_labor_plan():
    passport = extract.extract(
        str(ROOT / "data" / "demo" / "Структура цены Грант-26.xlsx")
    )["passport"]

    # Оптимизатор получает две позиции по должностям.
    assert len(passport["labor"]) == 2
    assert sum(row["person_months"] for row in passport["labor"]) == 14

    # Проверяющий видит все четыре строки исходной расшифровки.
    details = [detail for row in passport["labor"] for detail in row["details"]]
    assert len(details) == 4
    assert [row["stage"] for row in details] == ["Этап 1", "Этап 2", "Этап 1", "Этап 2"]
    assert {row["work_type"] for row in details} == {"НИР по гранту"}
    assert sum(row["total_cost"] for row in details) == 1_330_000
    assert {row["from"] for row in details} == {"01.06.2026", "01.10.2026"}
    assert {row["to"] for row in details} == {"30.09.2026", "31.12.2026"}
    assert {row["labor_unit"] for row in details} == {"чел.-мес."}
    assert sum(row["source_total_labor"] for row in details) == 14

    # Маска использует точные адреса: служебный номер строки A7 сюда не входит.
    first = details[0]["cells"]
    assert first["headcount"] == "Расшифровка ФОТ!E7"
    assert first["labor_per_person"] == "Расшифровка ФОТ!F7"
    assert first["person_months"] == "Расшифровка ФОТ!G7"
    assert first["from"] == "Расшифровка ФОТ!J7"
    assert first["to"] == "Расшифровка ФОТ!K7"


def test_person_hours_are_normalized_for_optimizer(tmp_path):
    book = Workbook()
    sheet = book.active
    sheet.title = "Расшифровка ФОТ"
    sheet["A1"] = "Расшифровка статьи «Расходы на оплату труда»"
    sheet["B2"] = "шифр C_HOURS-26"
    sheet["F4"] = "Трудоемкость на 1 чел., мес ЛИБО час"
    sheet["J4"] = "Среднемесячное количество рабочих часов в 2026 г. — 164,25"
    sheet["B7"] = "Этап 1"
    sheet["C7"] = "НИР"
    sheet["D7"] = "Инженер"
    sheet["E7"] = 2
    sheet["F7"] = 82.125
    sheet["G7"] = 164.25
    sheet["H7"] = 600
    sheet["I7"] = 98_550
    sheet["J7"] = "01.06.2026"
    sheet["K7"] = "30.06.2026"
    path = tmp_path / "hours.xlsx"
    book.save(path)

    result = extract.extract(str(path))
    assert not result["questions"]
    row = result["passport"]["labor"][0]
    detail = row["details"][0]
    assert row["person_months"] == 1
    assert row["avg_cost"] == 98_550
    assert detail["labor_unit"] == "чел.-ч"
    assert detail["source_total_labor"] == 164.25
    assert detail["source_unit_cost"] == 600
    assert detail["hours_per_month"] == 164.25
