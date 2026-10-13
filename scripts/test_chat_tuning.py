# -*- coding: utf-8 -*-
"""Настройка расчёта словами (CHAT-TUNING-001): матрица формулировок.

Агент «Настройка расчёта» работает со слабой моделью, поэтому всё, что можно
понять кодом, понимается кодом: команда или вопрос, глагол о дефиците, цель
претензии по основе слова, режим и вид закрепления, месяц словом. Здесь —
чистые функции `app/chat.py` на десятках реплик, без базы и без модели.
Поведение сервиса с этими разборами — `harness/chat.py` (ч18–ч26).
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Модуль чата тянет реестр агентов, а тот — базу; путь к базе фиксируется при
# импорте (DEV-STAND-ISOLATION-001), поэтому временная папка — до импорта.
os.environ.setdefault("FOT_DATA_DIR", tempfile.mkdtemp(prefix="chat_tuning_"))
os.environ.setdefault("FOT_SKIP_ORPHANS", "1")
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "src"))

import chat  # noqa: E402


def settings_of(text):
    return {d["name"]: str(d["value"]) for d in chat.parse_settings(text)}


class QuestionGateTests(unittest.TestCase):
    """Вопрос — не команда: ни настройки, ни претензии из него не читаются."""

    QUESTIONS = (
        "почему в плане дефицит?", "откуда дефицит у Петрова в марте", "зачем столько переводов",
        "какой сейчас допуск трудоёмкости?", "сколько договоров оклада у Петрова",
        "почему у Петрова 3 договора оклада?", "что с равномерностью освоения",
        "разрешён ли дефицит", "переводов стало меньше?", "где скачут суммы",
    )

    def test_questions_give_no_settings(self):
        for text in self.QUESTIONS:
            with self.subTest(text=text):
                self.assertTrue(chat.is_question(text))
                self.assertEqual(chat.parse_settings(text), [])

    def test_questions_give_no_claims(self):
        for text in self.QUESTIONS:
            with self.subTest(text=text):
                self.assertEqual(chat.parse_claims(text), [])

    def test_commands_are_not_questions(self):
        for text in ("разреши дефицит", "слишком много переводов", "допуск трудоёмкости 3 %",
                     "не больше трёх договоров оклада в год", "суммы скачут от месяца к месяцу"):
            with self.subTest(text=text):
                self.assertFalse(chat.is_question(text))


class SettingsPhrasingTests(unittest.TestCase):
    def test_deficit_polarity(self):
        cases = {
            "разреши дефицит": "да", "допусти дефицит": "да", "дефицит выплат допускается": "да",
            "можно с дефицитом": "да", "включи дефицит": "да", "пусть будет дефицит": "да",
            "запрети дефицит": "нет", "без дефицита": "нет", "дефицит нельзя": "нет",
            "убери дефицит": "нет", "отключи дефицит": "нет", "выключи дефицит": "нет",
            "дефицит разрешать не надо": "нет", "дефицит не нужен": "нет", "сними дефицит": "нет",
            "не разрешай дефицит": "нет", "не допускать дефицит": "нет",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(settings_of(text).get("разрешить дефицит"), want)

    def test_deficit_mention_without_verb_is_not_a_setting(self):
        for text in ("у Петрова в марте дефицит", "дефицит 12 000 ₽ в мае", "в плане дефицит по гранту"):
            with self.subTest(text=text):
                self.assertNotIn("разрешить дефицит", settings_of(text))

    def test_labor_tolerance_phrasings(self):
        cases = {
            "допуск трудоёмкости 3 %": "3", "допуск трудоемкости 3%": "3",
            "поставь допуск по трудоёмкости в 3 процента": "3", "допуск трудоёмкости 0,05": "0.05",
            "допуск трудоёмкости 10 %": "10", "в 2026 году допуск трудоёмкости 4%": "4",
            "допуск трудоёмкости ноль": "0", "допуск трудоёмкости нулевой": "0",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(settings_of(text).get("допуск трудоёмкости"), want)
        self.assertNotIn("допуск трудоёмкости", settings_of("допуск 5 % по деньгам"))

    def test_salary_contract_limit_needs_a_limiting_word(self):
        cases = {
            "не больше трёх договоров оклада в год": "3", "максимум два договора оклада": "2",
            "не более 2 договоров оклада": "2", "ограничь договоры оклада двумя": "2",
            "договоров оклада не больше одного": "1", "только один договор оклада": "1",
            "до 4 договоров оклада в год": "4",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(settings_of(text).get("макс договоров оклада в год"), want)
        for text in ("в прошлом году было 4 договора оклада", "у Петрова 2 договора оклада",
                     "договор оклада 1234 закрыт", "оставь Петрова на 2 договорах оклада"):
            with self.subTest(text=text):
                self.assertNotIn("макс договоров оклада в год", settings_of(text))

    def test_setting_names_match_by_stem(self):
        for raw, want in (("допуск трудоемкости", "допуск трудоёмкости"),
                          ("Допуск трудоёмкости", "допуск трудоёмкости"),
                          ("допуск по трудоёмкости, %", "допуск трудоёмкости"),
                          ("разрешить дефицит", "разрешить дефицит"),
                          ("Дефицит", "разрешить дефицит"),
                          ("макс договоров оклада в год", "макс договоров оклада в год"),
                          ("максимум договоров оклада", "макс договоров оклада в год"),
                          ("год", None), ("", None), (None, None)):
            with self.subTest(raw=raw):
                self.assertEqual(chat.setting_name(raw), want)


class GoalWordsTests(unittest.TestCase):
    def test_goal_words_map_to_solver_goals(self):
        cases = {
            "переводы": "переводы", "Переводы": "переводы", "переводов": "переводы",
            "смена договора оклада": "переводы", "не дёргать оклад": "переводы",
            "административная сложность": "административная сложность",
            "админ сложность": "административная сложность", "связки с договорами": "административная сложность",
            "дробление выплат": "административная сложность",
            "равномерность": "равномерность", "равномерность освоения": "равномерность",
            "освоение по месяцам": "равномерность",
            "стабильность сумм": "стабильность сумм", "стабильность": "стабильность сумм",
            "скачки сумм": "стабильность сумм", "одинаковые суммы": "стабильность сумм",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(chat.goal_of(raw), (want, None))

    def test_rule_phenomena_are_named_not_weighted(self):
        for raw in ("приказы", "стимулирующая приказом", "трудоёмкость", "трудоемкость",
                    "зарплата целиком", "недоплата", "предел 2556", "БЭП", "сроки договора"):
            with self.subTest(raw=raw):
                goal, why = chat.goal_of(raw)
                self.assertIsNone(goal)
                self.assertTrue(why)
        self.assertEqual(chat.goal_of("погода"), (None, None))
        self.assertEqual(chat.goal_of(""), (None, None))

    def test_claims_from_economist_words(self):
        cases = {
            "слишком много переводов": [("переводы", "меньше", 2.0)],
            "много переводов оклада между договорами": [("переводы", "меньше", 2.0)],
            "не дёргай оклад между договорами": [("переводы", "меньше", 2.0)],
            "переводы втрое важнее": [("переводы", "меньше", 3.0)],
            "в 4 раза меньше переводов": [("переводы", "меньше", 4.0)],
            "можно больше переводов": [("переводы", "больше", 2.0)],
            "много связок с договорами": [("административная сложность", "меньше", 2.0)],
            "всё потрачено в первые месяцы": [("равномерность", "больше", 2.0)],
            "освоение рваное": [("равномерность", "больше", 2.0)],
            "освоение по месяцам неравномерное": [("равномерность", "больше", 2.0)],
            "равномерность не важна": [("равномерность", "меньше", 2.0)],
            "суммы скачут от месяца к месяцу": [("стабильность сумм", "больше", 2.0)],
            "суммы прыгают": [("стабильность сумм", "больше", 2.0)],
            "посчитай план": [], "закрепи Петрова на гранте": [], "": [],
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                got = [(c["goal"], c["direction"], c["factor"]) for c in chat.parse_claims(text)]
                self.assertEqual(got, want)

    def test_claim_settings_scale_weights_by_direction(self):
        new, lines = chat.claim_settings({}, [{"goal": "Переводов", "direction": "меньше", "factor": 2}], "t")
        self.assertEqual(new["штраф смены договора оклада"], 1000000.0)
        self.assertEqual(len(new["претензии"]), 1)
        new, _ = chat.claim_settings(new, [{"goal": "переводы", "direction": "больше", "factor": 4}], "t")
        self.assertEqual(new["штраф смены договора оклада"], 250000.0)
        new, _ = chat.claim_settings({}, [{"goal": "скачки сумм", "direction": "больше", "factor": "3"}], "t")
        self.assertEqual(new["штраф нестабильности сумм выплат"], 30000.0)
        new, _ = chat.claim_settings({}, [{"goal": "равномерность", "direction": "меньше", "factor": "вдвое"}], "t")
        self.assertEqual(new["штраф отклонения от равномерного освоения"], 5000.0)
        new, _ = chat.claim_settings({}, [{"goal": "переводы", "direction": "меньше", "factor": 1000}], "t")
        self.assertEqual(new["штраф смены договора оклада"], 50000000.0)

    def test_claim_settings_explain_rules_and_unknown_goals(self):
        new, lines = chat.claim_settings({}, [{"goal": "приказы", "direction": "меньше", "factor": 2},
                                              {"goal": "погода", "direction": "меньше", "factor": 2}], "t")
        self.assertNotIn("штраф использования приказа", new)
        self.assertIn("весом не лечится", lines[0])
        self.assertIn("Цели «погода» у решателя нет", lines[1])
        self.assertEqual(new["претензии"], [])


class FixWordsTests(unittest.TestCase):
    def test_fix_mode_words(self):
        for raw, want in (("назначить", "назначить"), ("закрепить", "назначить"), ("закрепи", "назначить"),
                          ("оставь", "назначить"), ("посадить", "назначить"), ("assign", None),
                          ("запретить", "запретить"), ("запрет", "запретить"), ("не сажать", "запретить"),
                          ("исключить", "запретить"), ("убери с договора", "запретить"),
                          ("снять", "снять"), ("сними", "снять"), ("снять закрепление", "снять"),
                          ("убери закрепление", "снять"), ("отменить", "снять"),
                          ("", "назначить"), (None, "назначить"), ("удвоить", None)):
            with self.subTest(raw=raw):
                self.assertEqual(chat.fix_mode(raw), want)

    def test_fix_kind_words(self):
        for raw, want in (("оклад", "оклад"), ("Оклад", "оклад"), ("ставка", "оклад"),
                          ("122", "122"), ("надбавка 122", "122"), ("надбавка за качество 122", "122"),
                          ("152", "152"), ("приказ", "приказ"), ("стимулирующая приказом", "приказ"),
                          (None, "любой"), ("", "любой"), ("любой", "любой"), ("все", "любой"),
                          ("null", "любой"), ("премия", None), ("1220", None)):
            with self.subTest(raw=raw):
                self.assertEqual(chat.fix_kind(raw), want)

    def test_fix_month_values(self):
        for raw, want in ((6, 6), ("6", 6), ("12", 12), (6.0, 6), ("июнь", 6), ("Декабрь", 12),
                          ("с июня", 6), (0, None), (13, None), ("", None), (None, None), ("зима", None)):
            with self.subTest(raw=raw):
                self.assertEqual(chat.fix_month(raw), want)

    def test_word_stems_meet_across_cases(self):
        for a, b in (("Иванову", "Иванов"), ("Петровой", "Петров"), ("Сидорова", "Сидоров"),
                     ("гранте", "Грант"), ("внебюджете", "Внебюджет"), ("Ивановым", "Иванов")):
            with self.subTest(a=a, b=b):
                self.assertEqual(chat._stem(a), chat._stem(b))
        self.assertNotEqual(chat._stem("Иванченко"), chat._stem("Иванов"))


if __name__ == "__main__":
    unittest.main()
