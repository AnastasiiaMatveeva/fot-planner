# -*- coding: utf-8 -*-
"""Регрессии UI-007: инвентарь примитивов интерфейса ловит новый вариант.

Положительный случай — состав `app/static` совпадает с `docs/ui-inventory.json`.
Отрицательные — добавленный и исчезнувший класс становятся красным.

Отдельно закреплены две ошибки разбора, найденные при первом сборе инвентаря:
операнд сравнения принимался за класс кнопки, а у кнопки без атрибута `class`
в инвентарь попадал класс соседнего элемента. Обе давали список, который
описывает не то, что есть в интерфейсе, и потому не ограничивает ничего.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_ui_inventory as C  # noqa: E402


class UiInventoryTests(unittest.TestCase):
    def test_current_ui_matches_inventory(self):
        """Положительный: интерфейс в сборке объявлен целиком."""
        self.assertEqual(C.compare(C.load(), C.collect()), [])

    def test_new_button_class_is_detected(self):
        """Отрицательный: кнопка нового вида — красное до решения человека."""
        declared = C.load()
        actual = dict(declared, кнопки=list(declared["кнопки"]) + ["sparkle"])
        problems = C.compare(declared, actual)
        self.assertTrue(any("sparkle" in p and "не объявлен" in p for p in problems),
                        problems)

    def test_removed_class_is_detected(self):
        """Отрицательный: список, разошедшийся с кодом, тоже красный."""
        declared = C.load()
        actual = dict(declared, метки=[n for n in declared["метки"] if n != "pill"])
        problems = C.compare(declared, actual)
        self.assertTrue(any("pill" in p and "в коде его нет" in p for p in problems),
                        problems)

    def test_expression_operand_is_not_a_class(self):
        """Значение сравнения — не класс: в инвентарь идёт только « on»."""
        source = ('<button type="button" class="fmi\' + (srt.d === "a" ? " on" : "")'
                  ' + \'" data-fm="asc">')
        names, dynamic = C.button_classes(source)
        self.assertEqual(names, {"fmi", "on"})
        self.assertEqual(dynamic, 1)

    def test_class_of_neighbour_element_is_not_taken(self):
        """У кнопки без класса поиск не должен доходить до соседнего элемента."""
        source = '<button type="button" data-issue-back></button><div class="empty">'
        names, dynamic = C.button_classes(source)
        self.assertEqual((names, dynamic), (set(), 0))


if __name__ == "__main__":
    unittest.main()
