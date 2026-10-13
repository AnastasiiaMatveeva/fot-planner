"""Регрессии UI-004: правка названия плана в строке списка.

Проверка по исходникам интерфейса, без браузера и живой базы. Она ловит
возврат к однострочному полю в месте названия (82 px, видно «зой прогон»),
подрезание последней строки рамкой и жирное начертание. Внешний вид на
контрольных ширинах по-прежнему проверяется вручную (docs/ИНТЕРФЕЙС.md).
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "static" / "app.css").read_text(encoding="utf-8")


def rename_body():
    """Тело startRename: от объявления до следующей функции."""
    start = JS.index("function startRename(row)")
    end = JS.index("\nfunction ", start + 1)
    return JS[start:end]


def css_rule(selector):
    """Текст правила CSS с точным селектором, либо пустая строка."""
    found = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    return found.group(1) if found else ""


class RenameFieldTests(unittest.TestCase):
    def test_rename_field_is_multiline_and_full_width(self):
        body = rename_body()
        self.assertIn('createElement("textarea")', body)
        self.assertIn('classList.add("renaming")', body)
        self.assertIn('classList.remove("renaming")', body)
        # С 18.09.2026 в строке плана кнопок нет — действия в меню по правому
        # щелчку, как в прототипе (решение пользователя). На время правки
        # точка состояния уходит, и всё место строки отдано полю.
        self.assertIn("display:none", css_rule(".case.renaming .cdot"))
        self.assertIn("padding-right", css_rule(".case.renaming"))
        self.assertIn("width:100%", css_rule(".case .rename"))
        # Enter сохраняет название, а не вставляет перенос строки.
        self.assertRegex(body, r'e\.key === "Enter"\)\s*\{\s*e\.preventDefault\(\);\s*finish\(true\)')

    def test_plan_actions_live_in_the_context_menu(self):
        """Действия плана — в меню по правому щелчку, а не иконками в строке.

        У меню есть клавиатурный дубль (клавиша меню и Shift+F10), удаление
        отделено чертой и по-прежнему спрашивает подтверждение."""
        start = JS.index("function loadCases()")
        rows = JS[start:JS.index("\nfunction ", start + 1)]
        self.assertNotIn('"data-rename"', rows)
        self.assertNotIn('"data-del"', rows)
        self.assertIn('"contextmenu"', rows)
        self.assertIn('e.key === "ContextMenu"', rows)
        self.assertIn('e.key === "F10"', rows)
        start = JS.index("function openPlanMenu(")
        menu = JS[start:JS.index("\nfunction ", start + 1)]
        self.assertIn("Переименовать", menu)
        self.assertIn("removeCase(id)", menu)
        self.assertIn('class="msep"', menu)

    def test_rename_height_accounts_for_border(self):
        body = rename_body()
        self.assertIn("inp.scrollHeight + inp.offsetHeight - inp.clientHeight", body)

    def test_rename_field_is_not_bold(self):
        rule = css_rule(".case .rename")
        self.assertTrue(rule, "нет правила .case .rename")
        self.assertNotRegex(rule, r"font:\s*(600|700|bold)\b")
        self.assertNotRegex(rule, r"font-weight:\s*(600|700|bold)\b")


if __name__ == "__main__":
    unittest.main()
