# -*- coding: utf-8 -*-
"""Регрессии UI-008: правила интерфейса ловят новое нарушение, но не старое.

Положительный случай — нынешнее состояние `app/static` целиком покрыто долгом
и новых нарушений нет. Отрицательные — кнопка без имени, поле без метки и
снятый фокус становятся красным.

Отдельно закреплены три ложных срабатывания, найденные при первом прогоне:
подпись, оборачивающая контрол, считалась отсутствующей; скрытое поле
требовало метки; расширение массива `[...rows]` принималось за многоточие в
тексте. Проверка, которая врёт, приучает её выключать.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_ui_rules as C  # noqa: E402


def rules_of(text, name="проверка.js"):
    """Правила, сработавшие на куске разметки."""
    return {rule for rule, _, _ in C.check_markup(name, text)}


class UiRulesTests(unittest.TestCase):
    def test_current_ui_has_no_new_violations(self):
        """Положительный: весь нынешний долг объявлен, нового нет."""
        fresh = C.split(C.collect(), C.load()["долг"])[0]
        self.assertEqual(fresh, [])

    def test_icon_button_without_name_is_detected(self):
        """Отрицательный: кнопка только со значком не называется диктором."""
        source = '<button type="button" class="ico"><svg></svg></button>'
        self.assertIn("кнопка-без-имени", rules_of(source))

    def test_named_icon_button_passes(self):
        source = '<button type="button" aria-label="Закрыть"><svg></svg></button>'
        self.assertNotIn("кнопка-без-имени", rules_of(source))

    def test_field_without_label_is_detected(self):
        """Отрицательный: поле без aria-label, id и обёртки."""
        self.assertIn("поле-без-метки", rules_of('<input type="text">'))

    def test_focus_outline_removal_is_detected(self):
        """Отрицательный: снятый фокус без замены."""
        rules = {rule for rule, _, _ in
                 C.check_style("проверка.css", ".x:focus{outline:0;color:red}")}
        self.assertIn("фокус-снят", rules)

    def test_focus_outline_replaced_passes(self):
        """Замена тенью — не нарушение: фокус по-прежнему виден."""
        rules = {rule for rule, _, _ in C.check_style(
            "проверка.css", ".x:focus{outline:0;box-shadow:0 0 0 2px blue}")}
        self.assertNotIn("фокус-снят", rules)

    def test_label_wrapping_control_is_not_a_violation(self):
        """Подпись может оборачивать контрол, а не ссылаться по id."""
        source = '<label class="fld">Группировка<select data-rg></select></label>'
        self.assertNotIn("поле-без-метки", rules_of(source))

    def test_hidden_field_needs_no_label(self):
        """Скрытое поле не показывается и вспомогательной технике."""
        source = '<input type="file" multiple hidden>'
        self.assertNotIn("поле-без-метки", rules_of(source))

    def test_spread_syntax_is_not_an_ellipsis(self):
        """`[...rows]` — расширение массива, а не многоточие в подписи."""
        self.assertNotIn("три-точки", rules_of("var a = [...rows];"))
        self.assertIn("три-точки", rules_of("var a = 'Загрузка...';"))

    def test_debt_absorbs_known_violation_once(self):
        """Долг гасит ровно одно известное место, а не все похожие."""
        found = [("кнопка-без-имени", "app/static/app.js", 10, "aaa"),
                 ("кнопка-без-имени", "app/static/app.js", 20, "aaa")]
        fresh, old, _ = C.split(found, {"кнопка-без-имени|app/static/app.js|aaa": 1})
        self.assertEqual((len(fresh), len(old)), (1, 1))


if __name__ == "__main__":
    unittest.main()
