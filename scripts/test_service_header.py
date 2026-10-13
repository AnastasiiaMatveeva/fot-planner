"""Регрессии UI-012: шапка сервиса — знак в колонке, отдельной полосы нет.

Проверка по исходникам интерфейса, без браузера и живой базы. Она ловит
возврат полосы над приложением, второе имя продукта рядом со знаком, знак,
привязанный к ширине колонки заливкой, и переключатель правой колонки вне
заголовка плана. Внешний вид на контрольных ширинах и при колонке 180 px
по-прежнему проверяется вручную (docs/ИНТЕРФЕЙС.md).
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "static" / "app.css").read_text(encoding="utf-8")
JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")


def block(tag, attr):
    """Разметка элемента с атрибутом: от него до закрывающего тега."""
    start = HTML.index(attr)
    start = HTML.rindex("<" + tag, 0, start)
    end = HTML.index("</" + tag + ">", start)
    return HTML[start:end]


def css_rule(selector):
    """Текст правила CSS с точным селектором, либо пустая строка."""
    found = re.search(r"(?:^|\})\s*" + re.escape(selector) + r"\s*\{([^}]*)\}",
                      CSS, re.M)
    return found.group(1) if found else ""


class ServiceHeaderTests(unittest.TestCase):
    def test_mark_and_module_name_live_in_the_column(self):
        """Знак и название модуля стоят в голове колонки навигации."""
        aside = block("aside", 'class="cases"')
        self.assertIn('class="bmark"', aside)
        self.assertIn("/logo.svg", aside)
        self.assertIn('class="bname"', aside)
        self.assertIn("Планирование ФОТ", aside)
        # Знак — рисунок с заданным размером: без них место под него прыгает.
        mark = re.search(r"<img[^>]*class=\"logo\"[^>]*>", aside)
        self.assertTrue(mark, "нет знака в голове колонки")
        self.assertIn("width=", mark.group(0))
        self.assertIn("height=", mark.group(0))

    def test_mark_shrinks_with_the_column(self):
        """Колонку тянут от 180 px, и знак сжимается по её ширине."""
        rule = css_rule(".bmark .logo")
        self.assertTrue(rule, "нет правила .bmark .logo")
        self.assertIn("width:100%", rule)
        self.assertIn("max-width:162px", rule)
        # Высота ровная, пропорцию бережёт вписывание: height:auto давало
        # 17,05 px и сдвигало колонку на доли пикселя.
        self.assertIn("height:17px", rule)
        self.assertIn("object-fit:contain", rule)
        # Заливкой и шириной блока знак к --aside не привязан.
        self.assertNotIn("--aside", css_rule(".bmark"))
        self.assertNotIn("background", css_rule(".bmark"))

    def test_module_name_is_a_caption_not_a_second_title(self):
        """Название модуля названо один раз и подписью под знаком."""
        rule = css_rule(".bname")
        self.assertTrue(rule, "нет правила .bname")
        self.assertIn("display:block", rule)
        self.assertIn("var(--ink2)", rule)
        self.assertNotIn("Сервис планирования фонда оплаты труда", HTML)

    def test_side_toggle_sits_in_the_plan_header(self):
        """Правая колонка — чат с агентами (30.09.2026: колонки «Документы в
        этом плане» больше нет), переключатель — в заголовке плана."""
        head = block("header", 'class="phead"')
        self.assertIn('id="togglechat"', head)
        self.assertIn('class="iconbtn"', head)
        self.assertIn('aria-label="Чат с агентами"', head)
        self.assertNotIn('id="toggleside"', HTML)
        self.assertNotIn("<h3>Документы в этом плане</h3>", HTML)
        # С 30.09.2026 такой же значок стоит в строке заголовка реестра
        # («чтоб чат с агентами можно было из любого места открыть»): оба
        # помечены data-chattoggle, и щелчок по любому ловит один обработчик
        # на документе, а не свой у #togglechat.
        self.assertIn('id="togglechat" data-chattoggle', head)
        self.assertIn('e.target.closest("[data-chattoggle]")', JS)
        self.assertNotIn('$("togglechat").addEventListener("click"', JS)

    def test_no_bar_above_the_application(self):
        """Полосы над колонкой и рабочей областью нет ни в разметке, ни в стилях."""
        self.assertNotIn("brandbar", HTML)
        self.assertNotIn("brandbar", CSS)

    def test_column_width_has_a_default_and_narrow_grid_has_no_empty_track(self):
        """Ширина колонки планов задана в стилях: без сохранённой ширины
        var(--aside) был пуст, и сетка рассыпалась в столбец — список планов
        вставал над планом. До 1240 px правой колонки нет — нет ни её дорожки,
        ни переключателя, иначе справа пустая полоса в 300 px."""
        roots = re.findall(r"(?:^|\})\s*:root\s*\{([^}]*)\}", CSS, re.M)
        self.assertTrue(any(re.search(r"--aside:\s*\d+px", r) for r in roots))
        start = CSS.index("@media (max-width:1240px){")
        narrow = CSS[start:CSS.index("\n}", start)]
        # Узко дорожки чата нет: он выдвигается поверх (30.09.2026).
        self.assertIn(".side{position:fixed;", narrow)
        self.assertIn(".shell{grid-template-columns:var(--aside) minmax(0,1fr)}", narrow)


if __name__ == "__main__":
    unittest.main()
