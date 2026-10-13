# -*- coding: utf-8 -*-
"""Регрессии UI-009: договор — экран, в который заходят, а не строка списка.

Проверка по исходникам интерфейса, без браузера и живой базы: состав экрана,
путь назад, типы контролов у условий плана. Внешний вид на контрольных
ширинах проверяется вручную (docs/ИНТЕРФЕЙС.md).

Отдельно закреплены два замечания владельца продукта от 16.09.2026: список
договоров не сжимается — графы сравнивают между договорами; условие задаётся
тем, чем оно является, а не одним полем для текста на все семь полей.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "static" / "app.css").read_text(encoding="utf-8")
sys.path.insert(0, str(ROOT / "app"))


def body_of(name):
    """Тело функции: от объявления до следующей на верхнем уровне."""
    start = JS.index("function " + name + "(")
    end = JS.index("\nfunction ", start + 1)
    return JS[start:end]


class ContractScreenTests(unittest.TestCase):
    def test_row_click_opens_screen_not_selection(self):
        """Щелчок по строке проваливает в договор, а не выделяет её.

        С 30.09.2026 переход идёт через navigate() — экран договора пишется в
        адрес (UI-021), поэтому шифр строки передаётся переходу, а не
        присваивается ctrScreen на месте."""
        self.assertIn('navigate({ screen: "registry", tab: "ctr", contract: tr.getAttribute("data-ctr"),',
                      JS)
        self.assertIn("data-ctr-back", body_of("contractScreen"))

    def test_screen_is_library_only(self):
        """Экран договора в библиотеке: наименование, реквизиты, документы.

        Состав взят из макета Contract.dc.html решением пользователя
        17.09.2026: строку полноты пакета заменила графа «источник» в
        реквизитах — она отвечает на тот же вопрос по каждой величине.
        Условия плана в библиотеке по-прежнему не показываются: они относятся
        к плану, а не к договору.
        """
        body = body_of("contractScreen")
        # Свойства и документы с 18.09.2026 разнесены по вкладкам договора
        # (решение пользователя: «как в прототипе — несколько вкладок внутри
        # договора»), поэтому их ищем в contractTabs, а не в самом экране.
        tabs = body_of("contractTabs")
        self.assertIn("data-ctrname", body)
        self.assertIn("contractTabs", body)
        # Вкладки «Надбавка 120» у договора нет с 18.09.2026 (UI-009).
        for part in ("requisitesBlock", "catalogBlock", "laborTable"):
            self.assertIn(part, tabs, part)
        for gone in ("sec120Table", "contractProps", "packageStrip", "overrideBlock", "conditionsBlock",
                     "data-incl", "В этом плане"):
            self.assertNotIn(gone, body + tabs, gone)

    def test_labor_lives_in_contract(self):
        """Трудоёмкость — вкладка договора, а не раздел реестра.

        Решение пользователя 18.09.2026. Графы таблицы те же, что были во
        вкладке реестра, кроме «договора»: внутри договора он один на все
        строки."""
        self.assertNotIn('key: "labor"', JS[JS.index("var REG_TABS"):][:900])
        table = body_of("laborTable").lower()
        # Графы — как «Трудоёмкость договоров» в отчёте плана без факта,
        # отклонений и статуса (решение пользователя 18.09.2026); расшифровка
        # документа остаётся в «Графах».
        for column in ("этап", "вид работ", "должность", "кол-во человек",
                       "на 1 человека", "чел.-мес.", "средняя, ₽", "начало этапа",
                       "окончание этапа", "год", "источник"):
            self.assertIn('label: "' + column + '"', table, column)
        for gone in ("факт", "отклонение", "статус", "план, ₽", "сумма"):
            self.assertNotIn('label: "' + gone, table, gone)
        # Таблица — на общем движке, как все таблицы реестра (решение
        # пользователя 18.09.2026: функциональность у всех таблиц одна).
        self.assertIn('reggrid("ctr-labor"', table)
        self.assertNotIn("function contractProps", JS)
        self.assertNotIn("function packageStrip", JS)

    def test_registry_has_no_plan(self):
        """Реестр — библиотека: ни выбора плана, ни флажков, ни версий «в расчёте»."""
        registry = JS[JS.index("function renderRegistry()"):JS.index("function catalogBlock(")]
        for gone in ("basisCase", "planPick", "inclall", "data-incl", "#bcase"):
            self.assertNotIn(gone, registry, gone)
        catalog = body_of("catalogBlock")
        self.assertNotIn("data-use", catalog)
        # На экране договора — одна кнопка загрузки; «Нового договора» нет нигде.
        self.assertIn('(ctrScreen ? "" :', registry)
        # «Создать договор» вернулась 17.09.2026 решением пользователя по
        # макету: она открывает выбор файлов, а не ввод шифра. Реестр при этом
        # по-прежнему не знает о планах — проверяется списком выше.
        self.assertIn("data-newctr", registry)

    def test_requisites_show_value_and_source(self):
        """Свойства: свойство и значение (компонент PropertySheet). Графы
        «Источник» нет (решение пользователя 29.09.2026: «да, убирай
        источник»): источник знали у договора целиком, а не у поля, и подпись
        сбивала с толку. Допущение — цветом внимания в самой ячейке,
        объяснение — в подсказке. Значение правится в ячейке тем же
        механизмом, что в списке реестра."""
        body = body_of("requisitesBlock")
        self.assertIn('widths: ["30%", "70%"]', body)
        self.assertIn("nohead: true", body)
        self.assertIn("editCell(f, v, true)", body)
        for gone in ('"Источник"', "docSrc", "srcA", "fromPkg", "из шапки документов",
                     "реквизит договора", "Не найдено в документах"):
            self.assertNotIn(gone, body)
        self.assertNotIn("REQ_FROM_DOC", JS)
        self.assertIn('"Допущение: в документах нет — в расчёт идёт это значение"', body)
        self.assertIn('(assumed ? " data-assumed" : "")', body_of("editCell"))
        self.assertIn(".view .req td .ed[data-assumed]{color:var(--warn)}", CSS)

    def test_empty_value_is_not_credited_to_a_document(self):
        """Пустое значение не приписывается документу (пользователь
        20.09.2026: «почему источник указан, а значение не найдено»); с
        29.09.2026 источника у свойств нет вовсе, пустое обязательное —
        словом в самой ячейке."""
        body = body_of("requisitesBlock")
        self.assertNotIn("x.source", body)
        self.assertIn("return editCell(f, v, true);", body)

    def test_properties_are_grouped_by_meaning(self):
        """Алфавит разносит связанные свойства по разным концам списка.

        «Срок действия», «Выплата оклада до» и «Выплата надбавок до» сверяют
        вместе, а по алфавиту между ними встают «Гособоронзаказ» и «Шифр».
        Поэтому порядок задают группы, а сортировки по имени в блоке нет.
        """
        body = body_of("requisitesBlock")
        self.assertNotIn("localeCompare", body)
        for group in ("Идентификация", "Режим работы", "Сроки",
                      "Деньги и начисления"):
            self.assertIn('["' + group + '"', body)
        # Заголовок группы — строка на всю ширину, а не отдельная таблица:
        # иначе значения разных групп не встают в один столбик.
        self.assertIn("cs: 2", body)

    def test_unfilled_properties_are_findable(self):
        """В договоре шестнадцать свойств — пустые в них глазами не найти.
        Отбор — флажок с подписью, как «Показывать версии» (дизайн-разбор
        29.09.2026: воронку пользователь не понял — «не очевидно»).
        Заполнили последнее — отбор снимается сам."""
        body = body_of("requisitesBlock")
        self.assertIn('<label class="fld"><input type="checkbox" data-reqgaps', body)
        self.assertIn("<span>Только незаполненные</span></label>", body)
        self.assertIn("if (!gaps) reqOnlyGaps = false;", body)
        self.assertNotIn("Не заполнено: ", body)
        self.assertNotIn("TB_FILTER", JS)
        self.assertIn("reqOnlyGaps = !!gapbtn.checked;", JS)

    def test_yes_no_has_no_dash_and_empty_is_an_assumption(self):
        """«Да/нет» — флажок (пользователь 29.09.2026: «почему он не предложил
        чекбокс? это вроде самое очевидное на да/нет»), в свойствах и в
        списке договоров. Выпадающего списка с прочерком нет («зачем он
        нужен?»), пары кнопок тоже. Пустое поле флажок показывает так, как его
        прочтёт расчёт: основное место и совместительство — «да», ГОЗ — «нет»."""
        cell = body_of("yesnoCell")
        self.assertIn('<input type="checkbox" data-yesno="', cell)
        self.assertIn('((v || dflt) === "да" ? " checked" : "")', cell)
        self.assertIn("В документах нет — в расчёт идёт «", cell)
        self.assertIn('var YESNO_DEFAULT = { allow_main: "да", allow_part_time: "да", goz: "нет", priority: "нет" };',
                      JS)
        edit = body_of("startCellEdit")
        self.assertNotIn('createElement("select")', edit)
        self.assertNotIn('el.className = "seg"', edit)
        self.assertNotIn('v || "—"', edit)
        self.assertNotIn(".seg", CSS[CSS.index(".view .req td .ed.ask"):CSS.index(".view .req td .ed.ask") + 600])
        body = body_of("requisitesBlock")
        for col in ("allow_main", "allow_part_time", "goz", "priority"):
            self.assertIn('check("%s")' % col, body)
            self.assertNotIn('cellA("%s")' % col, body)
        # Список договоров — тем же флажком; сохраняет явное «да» или «нет».
        self.assertIn('f.kind === "yesno" ? yesnoCell(f, x[f.key]) : editCell(f, x[f.key], need)', JS)
        self.assertIn('fields[cb.getAttribute("data-yesno")] = cb.checked ? "да" : "нет";', JS)

    def test_contract_screen_takes_full_width(self):
        """Экран договора — во всю ширину реестра: с пределом 1180 px его
        таблицы на большом экране обрывались посередине (пользователь
        29.09.2026: «на полном экране выглядит обрезанной по ширине»)."""
        start = CSS.index("\n.ctr{")
        rule = CSS[start:CSS.index("}", start)]
        self.assertNotIn("max-width", rule)

    def test_back_button_returns_to_list(self):
        """Экран без пути назад запирает экономиста внутри договора.

        С 30.09.2026 возврат к списку — переход с записью в адрес (UI-021):
        navigate с пустым договором, а не ctrScreen = null на месте."""
        self.assertRegex(JS, r'data-ctr-back\]"\)\)\s*\{\s*\n\s*'
                             r'navigate\(\{ screen: "registry", tab: "ctr", contract: null \}, "push"\);')

    def test_path_has_arrow_and_keyboard_twin(self):
        """Одна текстовая ссылка «Договоры» не читается как выход.

        Стрелка «←» называет действие знаком, а Esc даёт ему клавиатурный
        дубль: иначе выйти из записи можно только мышью.
        """
        body = body_of("contractScreen")
        self.assertIn("BACK_ICON", body)
        self.assertIn('class="issue-back"', body)
        self.assertIn('e.key === "Escape" && ctrScreen', JS)

    def test_code_is_not_editable_by_rename(self):
        """Шифр опознаёт договор: переименование меняет только наименование."""
        body = body_of("startCtrRename")
        self.assertIn('JSON.stringify({ name: name })', body)
        self.assertNotIn("code:", body.split("JSON.stringify")[1][:120])


# Шапка списка строится из shown — видимых граф без той, по которой список
# сгруппирован (меню графы, решение пользователя 18.09.2026).
REGISTRY_LIST = "shown.map(ctrColHead)"


class GridTests(unittest.TestCase):
    """Все таблицы реестра — на одном движке с одним меню графы.

    Решение пользователя 18.09.2026: «все таблицы по функциональности должны
    быть одинаковые, только столбцы — названия различаются». Проверяется, что
    таблицы вкладок строятся движком, а меню графы у него полное."""

    def test_registry_tables_use_the_grid(self):
        for key in ('regGrid("emp"', 'regGrid("ref"', 'regGrid("sub"',
                    'regGrid("secret"', 'regGrid("ctr-labor"'):
            self.assertIn(key, JS, key)

    def test_reference_sources_live_in_reference_documents(self):
        """Таблицы «величина — документ-источник» нет: документы-источники
        величин справочника — в «Документах справочника» (пользователь
        20.09.2026). Источник, чей файл лежит в другой группе, встаёт туда;
        источник без файла в реестре — строкой «нет в реестре»."""
        self.assertNotIn('regGrid("refsrc"', JS)
        self.assertIn("body += referenceCatalog(d);", JS)
        stub = ("function catalogBlock(title, entries) { return JSON.stringify(entries.map("
                "function (e) { return [e.root, e.versions[0].id, !!e.versions[0].ghost,"
                " e.versions[0].sources || null]; })); }")
        script = (stub + body_of("referenceCatalog") +
                  # В основании есть и списки строк — «вне расчёта» и перечень
                  # переопределяемых полей; на них сбор документов падал.
                  "var regData = {basis: {reference: [], unlinked: [{root: 7, versions: [{id: 7,"
                  " name: 'Положение.pdf'}]}], staff: [], excluded: ['C_X'],"
                  " overridable: ['начало', 'окончание']}};"
                  "var d = {reference_sources: {'оклад': {'документ': 'Положение.pdf',"
                  " 'document_id': 7}, 'П2556': {'документ': 'Приказ 2556.pdf', 'document_id': 30},"
                  " 'П4': {'документ': 'Справка П4.pdf', 'document_id': 26},"
                  " 'БЭП': {'документ': 'Приказ 2556.pdf', 'document_id': 30}}};"
                  "console.log(referenceCatalog(d));")
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                             encoding="utf-8", check=True)
        self.assertEqual(json.loads(out.stdout), [
            [7, 7, False, ["оклад"]],
            ["src:Приказ 2556.pdf", None, True, ["П2556", "БЭП"]],
            ["src:Справка П4.pdf", None, True, ["П4"]]])
        # Строка без файла — без перехода и меню, со словом «нет в реестре».
        block = body_of("catalogBlock")
        self.assertIn('v.ghost ? \'<span class="st warn">нет в реестре</span>\'', block)
        self.assertIn('{ v: v.ghost ? "" : rowMenu("data-docmenu", v.id), cls: "act" }', block)

    def test_contract_has_no_allowance_120_tab(self):
        # 120 — надбавка сотрудника: у договора своей вкладки нет, правка —
        # на вкладке реестра «Надбавка 120» (UI-009, 18.09.2026).
        start = JS.index("function contractTabs(")
        tabs = JS[start:JS.index("\nfunction ", start + 1)]
        self.assertNotIn('"a120"', tabs)

    def test_empty_table_keeps_its_head(self):
        """Пустая таблица — шапкой граф и подвалом, без фразы «…пока нет»
        (пользователь 29.09.2026: «может отображать пустую таблицу
        всё-таки?»). Так же и таблицы документов."""
        grid = body_of("regGrid")
        self.assertNotIn("opts.empty", grid)
        self.assertIn("emptyHead: true", grid)
        self.assertIn("if (!rows.length && !(opts && opts.emptyHead))", body_of("table"))
        cat = body_of("catalogBlock")
        self.assertNotIn("Документов пока нет", cat)
        self.assertIn("emptyHead: true", cat)
        for gone in ("Трудоёмкости пока нет", "Штатного расписания пока нет", "Справочника пока нет",
                     "Правил пока нет", "Надбавок 120 пока нет", "Зарплаты по месяцам пока нет",
                     "Приказа о рабочей группе пока нет"):
            self.assertNotIn(gone, JS)

    def test_grid_menu_is_complete(self):
        start = JS.index("function openGridMenu(")
        menu = JS[start:JS.index("\nfunction ", start + 1)]
        for item in ("Группировать по этой графе", "Отменить группировку",
                     "Закрепить графу слева", "Открепить графу", "Скрыть графу",
                     "Выбрать графы…", "Сбросить вид таблицы", "Снять сортировку",
                     "Сортировка от А до Я", "(Выделить все)"):
            self.assertIn(item, menu, item)

    def test_row_menu_opens_on_right_click(self):
        """Правый щелчок по строке открывает то же меню, что три точки в её
        конце (пользователь 28.09.2026: «можно ли опции, спрятанные в эти три
        точки, вынести в правый клик»). Кнопка остаётся — с клавиатуры
        действия достаются только ею; строка без меню правый щелчок не
        перехватывает."""
        helper = body_of("rowActionsAt")
        self.assertIn('var row = e.target.closest("tr, .doc, .case");', helper)
        self.assertIn('row.querySelector(".more[data-docmenu], .more[data-ctrmenu]")', helper)
        self.assertIn("if (!b) return false;", helper)
        self.assertIn("e.preventDefault();", helper)
        self.assertIn("b.click();", helper)
        # Меню встаёт у курсора; без координат (клавиша меню) — у кнопки.
        self.assertIn("menuPoint = e.clientX || e.clientY ? { x: e.clientX, y: e.clientY } : null;",
                      helper)
        show = body_of("showMenu")
        self.assertIn("var r = menuPoint", show)
        self.assertIn("menuPoint = null;", show)
        # Подключено и в реестре, и в плане.
        self.assertIn("rowActionsAt(e);", JS)
        self.assertIn('$("view").addEventListener("contextmenu", rowActionsAt);', JS)
        # Кнопка с тремя точками никуда не делась.
        self.assertIn('function rowMenu(attr, id) {', JS)
        self.assertIn('aria-label="Действия"', JS)

    def test_grid_cells_return_markup_as_object(self):
        """Ячейка таблицы отдаёт разметку объектом { v }: строку движок
        экранирует (gridCell), и кнопка или список выбора вышли бы текстом —
        так было со списками вкладки «Рабочие группы» и кнопкой лицевого
        счёта (28.09.2026). Проверка — по всем графам со своей ячейкой."""
        def block_end(text, open_at):
            """Индекс после фигурной скобки, закрывающей открытую в open_at."""
            depth, i = 0, open_at
            while True:
                depth += text[i] == "{"
                depth -= text[i] == "}"
                i += 1
                if depth == 0:
                    return i

        def own_code(body):
            """Тело без вложенных функций: их return — ответ им, а не ячейке."""
            while True:
                m = re.search(r"function\s*\([^)]*\)\s*\{", body)
                if not m:
                    return body
                body = body[:m.start()] + body[block_end(body, m.end() - 1):]

        bad = []
        for m in re.finditer(r"cell: function \(x\) \{", JS):
            body = own_code(JS[m.end():block_end(JS, m.end() - 1) - 1])
            if re.search(r"return\s+(?:[\w.\[\]]+\s*\?\s*)?['\"]<", body):
                bad.append(JS[:m.start()].count(chr(10)) + 1)
        self.assertEqual(bad, [], "ячейка отдаёт разметку строкой, строки app.js: %s" % bad)

    def test_columns_are_named_in_full(self):
        """Графа названа полным словом, а не обиходным обрывком: «Табельный
        номер» (пользователь 21.09.2026: «просто табельный звучит странно,
        разговорно»). Так же названа графа во входном файле, поэтому разбор
        принимает её и коротким словом."""
        self.assertNotIn('"Табельный"', JS)
        # Не меньше двух (штатка и зарплата): с 30.09.2026 так же названа графа
        # таблицы факта начислений — счёт «ровно 2» ломался бы на каждой новой.
        self.assertGreaterEqual(JS.count('{ k: "code", label: "Табельный номер", always: true }'), 2)
        # Поиск по людям — полным словом везде, где он есть (штатка, зарплата,
        # рабочая группа договора); обрывка «Табельный или ФИО» нет нигде.
        self.assertGreaterEqual(JS.count('placeholder: "Табельный номер или ФИО…"'), 2)
        self.assertNotIn('"Табельный или ФИО…"', JS)
        # В выгрузке графа названа тем же словом, что на экране.
        export = (ROOT / "app" / "export_report.py").read_text(encoding="utf-8")
        self.assertNotIn('"Табельный"', export)
        self.assertIn('"Табельный номер"', export)
        # Разбор входного файла прежний: любое название на «табельн».
        intake = (ROOT / "app" / "intake.py").read_text(encoding="utf-8")
        self.assertIn('"табельн" in head', intake)

    def test_long_row_name_wraps_in_its_own_width(self):
        """Имя строки в закреплённой графе переносится в 240 px, а не растягивает
        графу: «Должность» справочника занимала 453 px — пол-экрана. Ширина — на
        самом имени: «Табельный номер» с «E001» не раздувается, а ячейка своего вида
        (редактор, ссылка) не меняется."""
        start = JS.index("function gridCell(")
        cell = JS[start:JS.index("\n}", start) + 2]
        run = ("function esc(s){return String(s).replace(/</g,'&lt;');}"
               "function gridIsNum(c){return c.kind==='money'||c.kind==='number';}" + cell +
               "console.log(JSON.stringify(["
               "gridCell({k:'p',always:true},{p:'Заместитель директора'},false),"
               "gridCell({k:'s',kind:'money'},{s:'40 400'},false),"
               "gridCell({k:'p',always:true,cell:function(){"
               "return {v:'<span class=\"ed\">x</span>'};}},{},false),"
               "gridCell({k:'p',always:true},{p:''},false)]));")
        out = subprocess.run(["node", "-e", run], capture_output=True, text=True,
                             encoding="utf-8", check=True)
        name, money, edit, empty = json.loads(out.stdout)
        self.assertEqual(name, {"v": '<span class="kname">Заместитель директора</span>',
                                "cls": "key"})
        self.assertEqual(money["cls"], "n")
        self.assertEqual(edit["v"], '<span class="ed">x</span>')
        self.assertIsNone(empty["v"])
        rule = re.search(r"#regview \.gridwrap td\.key \.kname(?:,#view \.gridwrap td\.key \.kname)?\{([^}]*)\}", CSS)
        self.assertIsNotNone(rule)
        for part in ("width:max-content", "max-width:240px", "white-space:normal"):
            self.assertIn(part, rule.group(1))
        self.assertNotRegex(CSS, r"#regview \.gridwrap \.tpanel td\.key\{[^}]*min-width")

    def test_grouped_grid_keeps_totals_and_skips_pages(self):
        start = JS.index("function regGrid(")
        body = JS[start:JS.index("\nfunction ", start + 1)]
        grouped = body[body.index("} else if (gcol) {"):body.index("} else {\n    var pg")]
        self.assertIn("st.collapsed[g]", grouped)
        self.assertIn('c.kind === "money"', grouped)
        self.assertNotIn("paged(", grouped)


class ColumnMenuTests(unittest.TestCase):
    """Меню графы как в прототипе: группировка, закрепление, скрытие, сброс.

    Решение пользователя 18.09.2026 («при клике на фильтр графы появляется
    окно с группировкой и т. д. — сделай так же»). Отбор значений остаётся
    словами Excel (решение 17.09.2026)."""

    def menu(self):
        start = JS.index("function openColMenu(")
        return JS[start:JS.index("\nfunction ", start + 1)]

    def test_menu_has_view_actions_and_excel_filter(self):
        menu = self.menu()
        for item in ("Группировать по этой графе", "Отменить группировку",
                     "Закрепить графу слева", "Открепить графу", "Скрыть графу",
                     "Выбрать графы…", "Сбросить вид таблицы", "Снять сортировку",
                     "Сортировка от А до Я", "(Выделить все)"):
            self.assertIn(item, menu, item)

    def test_grouped_list_has_totals_and_no_pages(self):
        start = JS.index("function ctrGroupedTable(")
        grouped = JS[start:JS.index("\nfunction ", start + 1)]
        self.assertIn("ctrCollapsed[g]", grouped)
        self.assertIn("mine.length", grouped)
        self.assertIn('kind === "money"', grouped)
        self.assertNotIn("paged(", grouped)

    def test_menu_opens_by_right_click_on_header(self):
        self.assertIn('e.target.closest(".ctrlist th")', JS)


class ContractListTests(unittest.TestCase):
    def test_list_keeps_every_solver_column(self):
        """Ни одна графа входа решателя не потеряна: восемь показываются сразу,
        остальные добираются кнопкой «Графы» (решение пользователя 17.09.2026
        по макету Main.dc.html). Правятся графы в ячейке тем же CTR_FIELDS —
        один перечень и для заголовка, и для редактора (SOURCE-007)."""
        head = JS[JS.index(REGISTRY_LIST):][:900]
        self.assertIn("shown.map(", head)
        cols = JS[JS.index("var CTR_COLS"):][:1400]
        spec = JS[JS.index("var CTR_FIELDS"):][:1600]
        # Каждая графа CTR_FIELDS доступна в списке — сразу или через «Графы».
        # «Вид договора» снят решением пользователя 17.09.2026: решатель его не
        # читает (в правилах договор упоминается только через
        # is_goz_defense_order и priority_payment_mode), а заполнен он был
        # источником финансирования вместо вида работ, для которого заводился.
        # Признаки, которые считает решатель, остались отдельными графами.
        for column in ("number", "department", "account", "goz", "fund",
                       "date_from", "date_to", "kinds", "priority", "allow_main",
                       "allow_part_time", "salary_deadline", "allowance_deadline"):
            self.assertIn('col: "' + column + '"', cols, column)
            self.assertIn('col: "' + column + '"', spec, column)
        # Шифр не прячется: без него строка перестаёт быть чьей-то.
        self.assertIn('{ key: "code", label: "Шифр", always: true }', cols)
        self.assertNotIn('col: "kind"', cols)

    def test_list_has_no_package_column(self):
        """Графа «пакет» снята решением пользователя 17.09.2026.

        Состав пакета виден на экране договора графой «источник» — по каждой
        величине, а не одной оценкой на договор. В списке графа занимала место
        и ломала строку, поэтому её нет ни в каталоге граф, ни в коде.
        """
        cols = JS[JS.index("var CTR_COLS"):][:1400]
        self.assertNotIn('key: "pkg"', cols)
        self.assertNotIn("function packageState", JS)

    def test_cells_without_document_source_are_editable(self):
        """SOURCE-007: графы, которых нет в документах, правят в ячейке — чем
        они являются: дата календарём, да/нет выбором, выплаты флажками."""
        import basis
        spec = re.findall(r'\{ col: "(\w+)", key: "(\w+)", label: "[^"]+", kind: "(\w+)" \}',
                          JS[JS.index("var CTR_FIELDS"):][:1600])
        self.assertEqual({col for col, _, _ in spec}, set(basis.MANUAL.values()))
        kinds = {col: kind for col, _, kind in spec}
        self.assertEqual({c for c, k in kinds.items() if k == "date"},
                         {"date_from", "date_to", "salary_deadline", "allowance_deadline"})
        # «Приоритет 2030» — тоже «да/нет»: флажок (пользователь 29.09.2026:
        # «так приоритет тоже флажок, поменяй везде, где список на да или нет»).
        self.assertEqual({c for c, k in kinds.items() if k == "yesno"},
                         {"goz", "allow_main", "allow_part_time", "priority"})
        self.assertEqual(kinds["kinds"], "kinds")
        self.assertEqual(kinds["fund"], "money")
        body = body_of("editCell")
        self.assertIn('data-edit="', body)
        self.assertIn('tabindex="0"', body)
        # Щелчок по ячейке открывает редактор, а не договор.
        handler = JS[JS.index('var ed = e.target.closest("[data-edit]");'):][:200]
        self.assertIn("startCellEdit(ed); return;", handler)


class PlanContractsTabTests(unittest.TestCase):
    """UI-010: какие договоры и на каких условиях идут в этот расчёт."""

    def branch(self):
        # Такой же `if (view === "ctr")` есть в обработчике change раньше по
        # файлу: ветку отрисовки ищем только внутри renderView.
        start = JS.index('  if (view === "ctr") {', JS.index("function renderView()"))
        return JS[start:JS.index("\n  if (view === ", start + 10)]

    def test_plan_has_contracts_tab(self):
        html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
        # Первая вкладка плана — план открывается на ней (30.09.2026).
        self.assertIn('data-view="ctr" aria-current="page">Договоры<', html)

    def test_tab_loads_registry_rows_and_plan_basis_together(self):
        body = body_of("setView")
        self.assertIn('api("/api/case/" + caseId + "/basis")', body)
        self.assertIn("vbasis = r[1]", body)

    def test_inclusion_checkboxes_live_in_the_plan(self):
        b = self.branch()
        self.assertIn('data-incl="', b)
        self.assertIn('class="inclall"', b)
        self.assertIn("В расчёте ", b)
        # Оба флажка подписаны: колонка без заголовка иначе немая для диктора.
        self.assertIn('aria-label="Все договоры страницы в расчёте"', b)
        self.assertIn("' в расчёте\"", b)

    def test_conditions_open_in_a_side_panel(self):
        """Условия и закрепления — в панели справа от таблицы (решение
        пользователя 19.09.2026 по совету дизайнера): таблица сжимается, а не
        закрывается; связь держат выделенная строка и шифр в шапке панели.
        Раскрытие строкой давало блок в ~400 px, и строки прыгали."""
        b = self.branch()
        self.assertIn('<div class="ctrsplit"><div class="ctrmain pctr">', b)
        self.assertIn("ctrPanel(open, bs, !pctrShown)", b)
        self.assertNotIn('<tr class="cond', JS)
        # Шифр — кнопка открытия панели, доступная с клавиатуры (UI-008).
        self.assertIn('data-pctr-open="', b)
        self.assertIn('aria-controls="ctrpanel" aria-expanded="', b)
        panel = body_of("ctrPanel")
        self.assertIn('<aside class="ctrpanel', panel)
        self.assertIn("data-pctr-close", panel)
        self.assertIn("conditionsBlock(x, bs)", panel)
        # Щелчок по другой строке меняет панель, по той же — прячет её
        # (пользователь 29.09.2026: «пусть при нажатии раскрывается и
        # скрывается панель справа»); закрывают и × и Esc, фокус — на шифр.
        click = JS[JS.index('if (e.target.closest("[data-pctr-close]")) { ctrPanelClose(); return; }'):][:2400]
        self.assertIn("if (pctrOpen === code) { ctrPanelClose(); return; }", click)
        self.assertIn("if (e.key === \"Escape\" && pctrOpen && !e.defaultPrevented) {", JS)
        self.assertIn('[data-pctr-open="\' + fixAttr(was) + \'"]', body_of("ctrPanelClose"))
        # Панель — часть экрана: без тени и затемнения, рядом с таблицей.
        self.assertIn("grid-template-columns:minmax(0,1fr) min(600px,calc(100% - 190px))", CSS)
        rule = CSS[CSS.index(".ctrpanel{"):][:200]
        self.assertNotIn("box-shadow", rule)
        # Шапка панели отделена линией, иначе имя договора читалось подписью к
        # таблице условий (проект дизайнера 20.09.2026).
        self.assertIn("padding-bottom:12px;border-bottom:1px solid var(--line)}", CSS)
        # Обе таблицы панели — одна сетка и один шаг строки.
        self.assertIn(".ovprops .ovtable th:first-child,.ovprops .ovtable td:first-child,\n"
                      ".ovprops .fixes th:first-child,.ovprops .fixes td:first-child"
                      "{width:150px}", CSS)
        self.assertIn(".ovprops .ovtable td,.ovprops .fixes td{height:32px;", CSS)

    def test_preflight_shows_what_the_core_checks(self):
        """Первый взгляд — то, что ядро проверяет перед расчётом: ФОТ года,
        срок, трудоёмкость, нормативы. Разрешённые выплаты — условие,
        а не готовность: из таблицы ушли в раскрываемую строку.

        «Кассы» нет с 18.09.2026: поступления в расчёт не подаются, весь ФОТ
        договора считается поступившим в первый месяц выплат (правило 21), и
        графа показывала бы у каждого договора одно и то же «допущение»."""
        b = self.branch()
        # Сразу — шифр, наименование, ФОТ года и срок; прочие графы выбираются
        # кнопкой «Графы» (решение пользователя 20.09.2026). Граф «Нормативы»
        # и «Условия плана» нет: понятия нормативов у договора нет, а условия
        # открывает щелчок по строке.
        for column in ('label: fundTitle', 'label: "Срок"', 'label: "Наименование"'):
            self.assertIn(column, b, column)
        for gone in ('label: "Нормативы"', '"нормативы"', '"условия плана"', '"касса"'):
            self.assertNotIn(gone, b, gone)
        self.assertIn('{ k: "labor", label: "Трудоёмкость", off: true,', b)
        self.assertIn('{ k: "sign", label: "Признак", off: true,', b)
        # Кнопка «Графы» — та же, что в таблицах реестра, и перерисовывает план;
        # стоит в одной полосе со строкой «В расчёте N из M» (проект дизайнера
        # 20.09.2026: две полупустые полосы над таблицей).
        self.assertIn('<button type="button" data-tb data-gcols="pctr"', b)
        # «Графы» — и при открытой панели договора (пользователь 30.09.2026:
        # кнопка пропадала, когда выбран договор). Прежнее «при панели кнопки
        # нет» (21.09.2026: «что даёт эта кнопка на сайдбаре?») снято: на
        # широком окне рядом с панелью видны и другие графы.
        bar = b[b.index("var bar = "):b.index("var html = ")]
        self.assertIn("""var bar = '<div class="tbar">' + headline + '<span class="sp"></span>' +""",
                      bar)
        self.assertIn('data-gcols="pctr"', bar)
        self.assertNotIn("open ?", bar)
        # Полоса и таблица — одной панелью реестра (пользователь 30.09.2026:
        # «в планах сделай все таблицы в одном стиле — как в реестре»); прежнее
        # «полоса над разделением во всю ширину» этим решением снято.
        self.assertIn("""var list = '<div class="gridwrap"><div class="tpanel">' + bar + html""", b)
        self.assertIn('GRIDS["pctr"] = { cols: pcols, rows: c, render: renderView };', b)
        # Заголовок графы называет её, а не обрывок в родительном падеже
        # (21.09.2026, та же правка, что у «Табельного номера»).
        self.assertIn('label: "Правки условий"', b)
        self.assertIn('label: "Закрепления"', b)
        for gone in ('label: "Правок условий"', 'label: "Закреплений"'):
            self.assertNotIn(gone, JS, gone)
        # «Графы» закрываются и с клавиатуры (UI-017, п. 8).
        cols = body_of("openGridCols")
        self.assertIn('if (e.key === "Escape")', cols)
        self.assertIn("if (back && btn.isConnected) btn.focus();", cols)
        self.assertNotIn('"разрешенные выплаты"', b)
        self.assertIn("В расчёте ", b)

    def test_contract_without_fund_is_marked_as_dropped(self):
        """Договор без ФОТ года ядро выбрасывает молча (basis._fill_from_package):
        с флажком, но без строки во входе. Это единственная тревога на экране."""
        b = self.branch()
        self.assertIn("будут отброшены", b)
        self.assertIn("нет — в расчёт не пойдёт", b)
        self.assertIn('role(x.code, "fund").state !== "ok"', b)

    def test_assumptions_are_named_not_hidden(self):
        b = self.branch()
        self.assertIn("год плана — допущение", b)
        self.assertIn(" — допущение</span>", b)

    def test_one_alarm_and_headline_in_words(self):
        """Строка над таблицей — словами через запятую, как в карточке, без «·»
        (UI-015). Красный на вкладке один — договор без ФОТ; без трудоёмкости
        или нормативов договор в расчёт идёт, но не готов — предупреждение.
        Шифр и диапазон дат не рвутся переносом; в ячейке стоит величина и
        только она; флажок не ложится на номер."""
        b = self.branch()
        headline = b[b.index("var headline"):b.index("var known")]
        # «Готовы N» снято (пользователь 20.09.2026: «что значит готовы 1,
        # зачем это»): число считалось по четырём величинам, включая
        # нормативы, и ни о чём не говорило. Тревога на вкладке одна.
        self.assertNotIn("готовы", headline)
        self.assertIn('": нет "', headline)
        self.assertNotIn(" · ", headline)
        self.assertEqual(b.count('<span class="st bad">'), 2)
        self.assertIn("""labor.state === "ok" ? known(labor) : '<span class="st warn">нет</span>'""",
                      b)
        self.assertIn("known(fund)", b)
        # В ячейке — величина, и только она (пользователь 21.09.2026: «в
        # ячейке должна быть только величина»): метки происхождения нет ни в
        # каком виде, откуда взято — в подсказке значения.
        self.assertNotIn("из документов", b)
        self.assertNotIn('''<span class="kd">пакет</span>''', b)
        self.assertIn(''''<span title="' + esc(r.source ? "Источник: " + r.source : "")''', b)
        self.assertIn('.replace(/ – /g, "&nbsp;–&#8288;&nbsp;")', b)
        # Флажок участия — перед номером (решение пользователя 20.09.2026):
        # флажок на left:0, номер за ним, шифр — за номером.
        self.assertIn("{ plain: true, numAfter: 1,", b)
        self.assertIn(".view td.incl + td.num + td.key{white-space:nowrap}", CSS)
        self.assertIn(".view .hscroll td.incl + td.num,.view .hscroll th.incl + th.num{left:38px}", CSS)
        self.assertIn(".view .hscroll td.incl + td.num + td.key,.view .hscroll th.incl + th.num"
                      " + th.key{left:76px}", CSS)
        self.assertNotIn("td.num + td.incl", CSS)

    def test_document_version_is_chosen_in_the_plan(self):
        cond = body_of("conditionsBlock")
        self.assertIn('data-use="', cond)
        self.assertNotIn("data-use", body_of("catalogBlock"))

    def test_change_handler_routes_plan_basis(self):
        handler = JS[JS.index('$("view").addEventListener("change"'):][:2600]
        for key in ("[data-incl]", "[data-use]", "[data-ov]", "setBasis({ contract: pctrOpen"):
            self.assertIn(key, handler, key)


class OverrideControlTests(unittest.TestCase):
    def test_every_overridable_field_has_a_typed_control(self):
        """Дата — календарём, разрешение — выбором, выплаты — флажками.

        Одно поле для текста на все семь условий заставляло печатать
        «01.06.2026» руками и угадывать, как пишется «да».
        """
        import basis
        kinds = dict(re.findall(r'"([^"]+)": "(date|yesno|kinds|months)"',
                                JS[JS.index("var OV_KIND"):][:600]))
        self.assertEqual(set(kinds), set(basis.OVERRIDE),
                         "поле без своего контрола или контрол без поля")
        self.assertEqual({k for k, v in kinds.items() if v == "date"},
                         {"начало", "окончание", "оклад до", "надбавки до"})
        self.assertEqual({k for k, v in kinds.items() if v == "yesno"},
                         {"основное", "совместительство"})
        # Месяцы без выплат (01.10.2026) — лентой месяцев (ИНТЕРФЕЙС.md).
        self.assertEqual({k for k, v in kinds.items() if v == "months"}, {"месяцы без выплат"})

    def test_fields_are_ordered_by_meaning_not_alphabet(self):
        """По алфавиту «надбавки до» вставало перед «началом», и связанные
        поля расходились по списку."""
        import basis
        self.assertEqual(list(basis.OVERRIDE),
                         ["начало", "окончание", "разрешенные выплаты",
                          "оклад до", "надбавки до", "основное", "совместительство",
                          "месяцы без выплат"])
        # Порядок экрана — порядок каталога: сортировки между ними нет.
        self.assertIn('"overridable": list(OVERRIDE)',
                      (ROOT / "app" / "basis.py").read_text(encoding="utf-8"))

    def test_payment_kinds_match_solver_input(self):
        """Набор флажков — те же виды, что разворачивает вход решателя."""
        shown = re.search(r'var PAY_KINDS = \[([^\]]+)\]', JS).group(1)
        shown = [s.strip().strip('"') for s in shown.split(",")]
        build = (ROOT / "app" / "build_input.py").read_text(encoding="utf-8")
        used = re.findall(r'allow\("([^"]+)"\)', build)
        self.assertEqual(shown, used)

    def test_all_kinds_checked_writes_no_override(self):
        """Отмечены все — это «не сказано» в договоре, а не запрет нового."""
        self.assertIn("if (value.split(\", \").length === PAY_KINDS.length) value = \"\";", JS)

    def test_dates_convert_both_ways(self):
        """Календарь отдаёт ГГГГ-ММ-ДД, договор хранит ДД.ММ.ГГГГ."""
        self.assertIn("function dmyToIso", JS)
        self.assertIn("function isoToDmy", JS)
        self.assertIn("value = isoToDmy(ov.value)", JS)

    def test_reset_to_contract_value_exists(self):
        self.assertIn("data-ovclear", body_of("conditionsBlock"))
        # Сброс правки — значком у поля, а не кнопкой «как в договоре» под ним
        # (пользователь 29.09.2026: «может убрать явную кнопку и сделать
        # кнопку сброса?»); «да/нет» плана — флажком, как у договора.
        self.assertIn("svg16(ICON_RESET)", body_of("conditionsBlock"))
        # Значок встаёт в заранее оставленное место и поле не сужает
        # (пользователь 30.09.2026).
        av = CSS[CSS.index(".ovprops .av{"):][:200]
        self.assertIn("padding-right:30px", av)
        self.assertIn(".ovprops .av .iconbtn{position:absolute;", CSS)
        self.assertNotIn(">как в договоре</button>", body_of("conditionsBlock"))
        ov = body_of("ovControl")
        self.assertIn('<input type="checkbox" id="', ov)
        # После сброса поле показывает дату договора, а не пустоту
        # (пользователь 30.09.2026); равное договорному — не правка.
        self.assertIn('value="\' + esc(dmyToIso(set || was))', ov)
        self.assertIn('if (ov.hasAttribute("data-was") && value === ov.getAttribute("data-was")) value = "";',
                      JS)
        self.assertNotIn('(v || "как в договоре")', ov)
        self.assertIn('value = ov.checked ? "да" : "нет";', JS)
        self.assertIn("data-ovclear", JS[JS.index("var clr = e.target.closest"):][:200])

    def test_base_value_and_override_are_shown_together(self):
        """Экономист должен видеть, что изменил именно он (SOURCE-006)."""
        body = body_of("conditionsBlock")
        self.assertIn('class="kd"', body)
        self.assertIn("ovControl(", body)

    def test_conditions_are_a_table_contract_vs_plan(self):
        """Условия — таблицей «условие — в договоре — в плане» (решение
        пользователя 19.09.2026: сетка с подписью над полем читалась плохо)."""
        body = body_of("conditionsBlock")
        self.assertIn('<table class="ovtable">', body)
        for head in ("<th>Условие</th>", "<th>В договоре</th>", "<th>В плане</th>"):
            self.assertIn(head, body)
        # Правка плана выделена своей строкой — акцентом, не заливкой.
        self.assertIn("' class=\"ovon\"'", body)
        self.assertIn(".ovprops tr.ovon .ovf,", CSS)
        # Условия — в панели договора, строки условий в таблице больше нет.
        self.assertNotIn("tr.cond", CSS)
        # Общий .kd — строчный блок без переноса; в ячейке «В договоре» он
        # наезжал на флажки видов выплат.
        self.assertIn(".ovprops .ovtable td.kd{display:table-cell;white-space:normal;", CSS)
        # Прежняя сетка свойств больше нигде не рисуется.
        self.assertNotIn("ctrprops", JS)
        self.assertNotIn("ctrprops", CSS)

    def test_inclusion_checkbox_answers_before_the_server(self):
        """Флажок «в расчёте» меняет экран сразу: после снятия флажка в шапке
        строки ещё полсекунды стояли отмеченными (пользователь 19.09.2026)."""
        handler = JS[JS.index('$("view").addEventListener("change"'):][:1400]
        self.assertLess(handler.index("basisLocal(codes, all.checked"),
                        handler.index("setBasis({ contracts: codes"))
        self.assertLess(handler.index("basisLocal([code], one.checked"),
                        handler.index("setBasis({ contracts: [code]"))
        local = body_of("basisLocal")
        # Перечень снятых — по ключу: «excluded» у договоров, «staff_excluded»
        # у строк штатного расписания (30.09.2026).
        self.assertIn('key = key || "excluded";', local)
        self.assertIn("vbasis[key] = Object.keys(ex);", local)
        self.assertIn("renderView();", local)
        # Фокус остаётся на флажке, который сняли.
        self.assertIn("again.focus()", local)

    def test_screen_styles_exist(self):
        for selector in (".ctr{", ".ovprops .ovtable,", ".ovkinds{", ".ctr .rename{"):
            self.assertIn(selector, CSS, selector)



class NoDuplicateFunctionsTests(unittest.TestCase):
    """Одноимённая функция молча подменяет прежнюю: так шапка реестра договоров
    сломала шапки групп в отчёте результата."""

    def test_top_level_function_names_unique(self):
        import collections, re as _re
        js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        names = _re.findall(r"^function ([A-Za-z0-9_$]+)\(", js, _re.M)
        dup = [n for n, c in collections.Counter(names).items() if c > 1]
        self.assertEqual(dup, [])

if __name__ == "__main__":
    unittest.main()
