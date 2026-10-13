"""Вкладка «Документы» реестра — одна таблица всех файлов (UI-013).

Проверка по тексту app.js и app.css, как у соседних тестов интерфейса: базу
не открывает и app/ не импортирует — живые данные тест не тронет.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "static" / "app.css").read_text(encoding="utf-8")


def body_of(name):
    """Тело функции верхнего уровня — до следующего объявления."""
    start = JS.index("\nfunction " + name + "(")
    end = JS.find("\nfunction ", start + 1)
    return JS[start:end if end > 0 else len(JS)]


def docs_branch():
    render = body_of("renderRegistry")
    start = render.index('regTab === "docs"')
    return render[start:render.index("} else if", start)]


class DocsTabTests(unittest.TestCase):
    def test_one_table_without_groups(self):
        """Не каталог на каждую группу, а один: строка приёма внутри рамки."""
        branch = docs_branch()
        self.assertIn("regDocList()", branch)
        self.assertEqual(branch.count("catalogBlock("), 1, branch)
        self.assertNotIn(".map(function (g)", branch)
        # Полоса приёма вне .cat красилась главной кнопкой (было 18.09.2026).
        self.assertNotRegex(branch, r"body \+= uploadZone")

    def test_columns(self):
        body = body_of("catalogBlock")
        # Графы — документ, версия, дата и время загрузки, состояние,
        # результат (решение пользователя 18.09.2026); «относится к» снята.
        self.assertNotIn('"относится к"', body)
        for column in ("документ", "версия", "дата и время загрузки", "состояние",
                       "результат обработки"):
            self.assertIn('"' + column + '"', body)
        for gone in ("кем загружен", "загрузил", "не привязан"):
            self.assertNotIn(gone, body.lower())
        # Результат — счёт кода, а не текст модели.
        self.assertIn("producedAny(v)", body)
        self.assertNotIn("summary", body)

    def test_relation_is_contract_or_section_never_warning(self):
        body = body_of("docRelation")
        self.assertIn("data-ctropen", body)
        self.assertIn("data-rtab", body)
        self.assertNotIn("warn", body)
        self.assertNotIn("привязан", body)
        self.assertIn('closest("[data-ctropen]")', JS)

    def test_no_unlinked_counter_on_tab(self):
        tabs = JS[JS.index("var tabsRow ="):JS.index('$("regview").innerHTML = tabsRow')]
        self.assertNotIn("без договора", tabs)
        self.assertNotIn("nFree", tabs)

    def test_contract_is_optional(self):
        """Договор у документа необязателен (пользователь 29.09.2026: «не у
        всех документов должна быть привязка к договору… есть же просто общие
        экономические показатели»; «сделай что-то общее, не зажимая в рамки»):
        очереди «не привязаны к договорам» нет, перечня видов нет. Свободному
        документу договор указывают из меню строки списком договоров реестра;
        «Новый договор…» — последним пунктом, поиск его не прячет."""
        self.assertNotIn('"документов не привязаны"', JS)
        self.assertNotIn(">Разобрать</button>", JS)
        menu = JS[JS.index("var wo = wgOrderOfDoc(+id);"):JS.index('label: "Удалить документ"')]
        self.assertIn('if (wo || b.closest("[data-unlinked]")) {', menu)
        self.assertIn("docPickContract(at, +id, name, wo)", menu)
        self.assertNotIn("askText(", menu)
        pick = body_of("docPickContract")
        self.assertIn('label: "Новый договор…", keep: true', pick)
        self.assertIn('askText("Договор для «" + name + "»", "Шифр договора")', pick)
        self.assertIn('"/api/document/" + docId + "/contract"', pick)
        self.assertIn("!it.keep", body_of("pickFrom"))

    def test_shared_file_counted_once(self):
        body = body_of("regDocList")
        self.assertIn("seen[e.root]", body)

    def test_date_and_state_do_not_wrap(self):
        self.assertRegex(CSS, r"\.view td\.dt\{[^}]*white-space:nowrap")
        widths = re.search(r'widths = \[([^\]]*)\]', body_of("catalogBlock")).group(1)
        self.assertIn('"176px", "172px"', widths)  # «Дата и время загрузки» — 176 px с 18.09.2026


if __name__ == "__main__":
    unittest.main()
