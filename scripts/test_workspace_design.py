"""UI-019: контраст палитры и доступность действий без живой базы."""
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def contrast(first, second):
    def luminance(color):
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        channels = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4
                    for c in channels]
        return sum(c * w for c, w in zip(channels, (.2126, .7152, .0722)))
    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + .05) / (dark + .05)


def js_function(name):
    """Функция app.js целиком: от объявления до следующей на верхнем уровне."""
    js = (ROOT / 'app/static/app.js').read_text(encoding='utf-8')
    start = js.index('function ' + name + '(')
    return js[start:js.index('\nfunction ', start + 1)]


def node(script):
    output = subprocess.run(['node', '-e', script], capture_output=True,
                            text=True, encoding='utf-8', check=True)
    return json.loads(output.stdout)


class WorkspaceDesignTests(unittest.TestCase):
    def test_text_contrast(self):
        css = (ROOT / 'app/static/workspace.css').read_text(encoding='utf-8')
        root = css.split(':root{', 1)[1].split('}', 1)[0]
        tokens = dict(re.findall(r'--([\w-]+):\s*(#[\da-fA-F]{6})', root))
        for text, background in [('ink', 'paper'), ('ink2', 'paper'),
                                 ('ink3', 'canvas'), ('onacc', 'acc'),
                                 ('navtext', 'nav'), ('navmuted', 'nav'),
                                 ('navtext', 'navon')]:
            with self.subTest(text=text, background=background):
                self.assertGreaterEqual(contrast(tokens[text], tokens[background]), 4.5)

    def test_contrast_rejects_faint_text(self):
        self.assertLess(contrast('#9AA1A3', '#FFFFFF'), 4.5)

    def test_registry_table_uses_ledger_surface(self):
        css = (ROOT / 'app/static/workspace.css').read_text(encoding='utf-8')
        self.assertIn('border:0;border-radius:0;background:transparent;box-shadow:none', css)
        self.assertIn('background:#F8F8F7;border-bottom:1px solid var(--line)', css)
        self.assertIn('max-width:100%;overflow-x:auto', css)

    def test_plan_actions_and_quiet_states(self):
        html = (ROOT / 'app/static/index.html').read_text(encoding='utf-8')
        js = (ROOT / 'app/static/app.js').read_text(encoding='utf-8')
        css = (ROOT / 'app/static/app.css').read_text(encoding='utf-8')
        workspace = (ROOT / 'app/static/workspace.css').read_text(encoding='utf-8')
        head = html.split('<header class="phead">', 1)[1].split('</header>', 1)[0]
        self.assertIn('class="pactions"', head)
        self.assertLess(head.index('id="solve"'), head.index('id="togglechat"'))
        # С 30.09.2026 действия — в строке названия и статуса: строкой ниже
        # они стояли вплотную над вкладками («ещё вот тут всё слиплось»).
        top = head[head.index('<div class="ptop">'):]
        self.assertLess(top.index('id="pverdict"'), top.index('class="pactions"'))
        self.assertNotIn('class="spacer"', head)
        self.assertNotIn(".phead .row", css)
        self.assertIn(".phead{flex:0 0 auto;padding:14px 20px 10px;", css)
        # Номер расчёта и дата связаны словом, а не точкой (UI-015).
        self.assertIn('">№ " + r.id + (r.created ? " от " + esc(r.created) : "")', js)
        self.assertIn('class="empty-state"', js)
        self.assertIn('data-act="solve"', js)
        self.assertIn('.view .none.loading::after', css)
        self.assertIn('conditionReveal', css)
        # Пустой план — без второй «Рассчитать план»: она уже в шапке
        # (пользователь 20.09.2026: «дублирование кнопок — не оч»).
        empty = js[js.index("  if (!vresult) {"):][:400]
        self.assertNotIn('data-act="solve"', empty)
        self.assertIn('showToast("Изменения сохранены")', js)
        self.assertIn('.toast.show', workspace)

    def test_plan_grids_look_like_registry_grids(self):
        """Таблицы движка реестра во вкладках плана («Факт начислений»,
        «Проекты приказов») — тем же видом, что в реестре: к правилам реестра
        добавлены те же селекторы для плана, новых стилей нет (пользователь
        30.09.2026: «поисковое окно отличается от того, что уже есть; не меняй
        стили, пусть всё однотипно»; «в реестре ты правильно сделал, а в плане
        нет»). :where() не добавляет веса: порядок правил тот же, что в
        реестре."""
        css = (ROOT / 'app/static/app.css').read_text(encoding='utf-8').replace('\r\n', '\n')
        ws = (ROOT / 'app/static/workspace.css').read_text(encoding='utf-8').replace('\r\n', '\n')
        for part in ("#regview .tbar .find > svg,#view :where(.gridwrap) .tbar .find > svg{",
                     "#regview .tbar [data-tbadd],#view :where(.gridwrap) .tbar [data-tbadd]{",
                     "#regview .tpanel .pager,#view :where(.gridwrap) .tpanel .pager{"):
            self.assertIn(part, ws, part)
        for part in ("#regview .tbar .ctrq,#view :where(.gridwrap) .tbar .ctrq{",
                     "#regview .tpanel td.key,#regview .tpanel th.key,"):
            self.assertIn(part, css, part)
        # Таблицы реестра со своими графами (рабочая группа договора) плану не
        # передаются.
        self.assertNotIn('#view .gridwrap[data-grid="ctr-wg"]', css)

    def test_plan_tables_are_registry_panels(self):
        """Таблицы плана — панелью реестра, вкладки плана переносятся без
        прокрутки; должность правится в плане (пользователь 30.09.2026)."""
        js = (ROOT / 'app/static/app.js').read_text(encoding='utf-8')
        ws = (ROOT / 'app/static/workspace.css').read_text(encoding='utf-8').replace('\r\n', '\n')
        self.assertIn("'<div class=\"gridwrap empov\"><div class=\"tpanel\">'", js)
        self.assertIn("var list = '<div class=\"gridwrap\"><div class=\"tpanel\">' + bar + html", js)
        self.assertNotIn("#view .pctr tbody td{height:40px", ws)
        self.assertIn(".tabs{column-gap:24px;padding:0 22px;flex-wrap:wrap;overflow-x:visible}", ws)
        self.assertIn('var EMP_FIELDS = ["должность", "ставка", "начало", "окончание"];', js)
        self.assertIn('el.setAttribute("list", "posopts");', js)
        basis = (ROOT / 'app/basis.py').read_text(encoding='utf-8')
        self.assertIn('"должность": "position"}', basis)

    def test_chat_panel_looks_like_interface(self):
        """Чат — видом остального интерфейса (пользователь 30.09.2026:
        «сделай боковую панель по размеру шрифта, стилю и т. д. такой же, как
        весь интерфейс»): заголовок 13 px, реплики без тени, «Отправить» —
        мягкой кнопкой полосы, залитой остаётся «Рассчитать план»."""
        ws = (ROOT / 'app/static/workspace.css').read_text(encoding='utf-8').replace('\r\n', '\n')
        tail = ws[ws.index('/* Чат — видом интерфейса'):]
        self.assertIn('.chatpanel .chath h2{font:600 13px/18px var(--f)', tail)
        self.assertIn('box-shadow:none', tail[tail.index('.chatpanel .msg .bubble{'):][:200])
        self.assertIn('background:var(--accbg);color:var(--accink)', tail)

    def test_cell_editor_lies_over_the_cell(self):
        """Поле правки в ячейке — поверх неё: ширина граф и таблицы не
        меняется (пользователь 30.09.2026: «при нажатии на окно с выбором
        даты вся таблица увеличивается»; «нажимаю — и как-то странно всё
        расширяется, потом опять сжимается»)."""
        css = (ROOT / 'app/static/app.css').read_text(encoding='utf-8')
        for part in (
                # Таблицы плана — тем же видом, что реестр (пользователь
                # 30.09.2026): правило и для #view .gridwrap.
                ".view .empov td:has(> .ed > input),.ovprops .fixes td:has(> .ed > input),\n"
                "#regview .gridwrap td:has(> .ed > input),#view .gridwrap td:has(> .ed > input)"
                "{position:relative}",
                ".view .empov td > .ed > input,.ovprops .fixes td > .ed > input.ovf,\n"
                "#regview .gridwrap td > .ed > input,#view .gridwrap td > .ed > input{position:absolute;",
                "width:max(100%,96px)",
                "#regview .gridwrap td > .ed > input[type=date],#view .gridwrap td > .ed > "
                "input[type=date]{width:max(100%,150px)}"):
            self.assertIn(part, css.replace("\r\n", "\n"), part)
        self.assertNotIn(".view .empov td .ed input{min-width:96px}", css)

    def test_toolbar_labels_and_escaping(self):
        js = (ROOT / 'app/static/app.js').read_text(encoding='utf-8')
        start = js.index('function tableBar(o)')
        end = js.index('function tbMatch', start)
        setup = "var TB_PLUS='', TB_FIND='', TB_COLS='', UPLOAD_ACCEPT='.pdf';"
        setup += "var ctrQuery='',tbQuery={}; function esc(s){return String(s).replace(/</g,'&lt;').replace(/\"/g,'&quot;');}"
        run = setup + js[start:end] + "console.log(JSON.stringify([tableBar({add:'data-newctr',addLabel:'Добавить договор',key:'ctr',cols:'data-cols'}),tableBar({upload:'',addLabel:'Выбрать файлы…',key:'docs'}),tableBar({add:'data-newctr',addLabel:'<unsafe>'})]));"
        output = subprocess.run(['node', '-e', run], capture_output=True,
                                text=True, encoding='utf-8', check=True)
        add, upload, escaped = json.loads(output.stdout)
        self.assertIn('<span>Добавить договор</span>', add)
        self.assertIn('<span>Графы</span>', add)
        self.assertIn('type="search"', add)
        self.assertIn('<span>Выбрать файлы…</span>', upload)
        self.assertIn('&lt;unsafe>', escaped)
        self.assertNotIn('<unsafe>', escaped)

    def test_hidden_views_stay_hidden(self):
        # Экраны прячутся атрибутом hidden, а правило с id сильнее .view[hidden]:
        # #regview:has(> .ctrlist){display:flex} оставляло реестр под лентой плана.
        js = (ROOT / 'app/static/app.js').read_text(encoding='utf-8')
        html = (ROOT / 'app/static/index.html').read_text(encoding='utf-8')
        hidden = set(re.findall(r'\$\("([\w-]+)"\)\.hidden\s*=', js))
        hidden |= set(re.findall(r'id="([\w-]+)"[^>]*\shidden', html))
        self.assertIn('regview', hidden)
        for name in ('app.css', 'workspace.css', 'issues.css'):
            css = (ROOT / 'app/static' / name).read_text(encoding='utf-8')
            css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
            for selectors, body in re.findall(r'([^{}]+)\{([^{}]*)\}', css):
                if not re.search(r'(?<![-\w])display\s*:\s*(?!none)', body):
                    continue
                for selector in selectors.split(','):
                    # Скобки :has()/:not() не делят селектор на части.
                    flat = selector
                    while re.search(r'\([^()]*\)', flat):
                        flat = re.sub(r'\([^()]*\)', '', flat)
                    subject = re.split(r'\s*[>+~]\s*|\s+', flat.strip())[-1]
                    ids = set(re.findall(r'#([\w-]+)', subject)) & hidden
                    if ids and '::' not in subject and 'hidden' not in selector:
                        self.fail(name + ': ' + selector.strip()
                                  + ' показывает скрытый экран; нужен :not([hidden])')

    def test_plan_empty_state_is_title_and_action(self):
        # UI-016 п. 2: пустое состояние — заголовок и действие. Фраза «Проверьте
        # договоры и условия выше…» объясняла и ошибалась: договоры на соседней
        # вкладке. Класс empty-action не был объявлен в ui-inventory.json.
        js = (ROOT / 'app/static/app.js').read_text(encoding='utf-8')
        start = js.index('class="empty-state"')
        state = js[start:js.index('return;', start)]
        self.assertIn('План ещё не рассчитан', state)
        # Кнопки в пустом состоянии нет: «Рассчитать план» стоит в шапке
        # плана, на той же странице (пользователь 20.09.2026: «дублирование
        # кнопок — не оч»).
        self.assertNotIn('data-act="solve"', state)
        self.assertNotIn('<span>', state)
        self.assertNotIn('empty-action', js)

    def test_messages_name_the_action_and_keep_the_text(self):
        # Реплика, которая не ушла, возвращается в поле с причиной, а не
        # пропадает молча; кнопка в окне удаления договора называет действие,
        # а не то, что останется («Оставить документы» читалась как отмена).
        js = (ROOT / 'app/static/app.js').read_text(encoding='utf-8')
        html = (ROOT / 'app/static/index.html').read_text(encoding='utf-8')
        start = js.index('$("composer").addEventListener("submit"')
        send = js[start:js.index('\n});', start)]
        self.assertIn('$("text").value = t', send)
        self.assertIn('showToast("Не отправлено: "', send)
        self.assertIn('aria-label="Сообщение агенту"', html)
        self.assertIn('data-keep>Удалить без документов</button>', js)
        self.assertNotIn('Оставить документы', js)

    def test_navigation_is_announced_and_reachable(self):
        # UI-008: договор открывается с клавиатуры, а не только щелчком по
        # строке; открытый план и вкладка слышны диктору, а не только видны.
        js = (ROOT / 'app/static/app.js').read_text(encoding='utf-8')
        html = (ROOT / 'app/static/index.html').read_text(encoding='utf-8')
        self.assertRegex(js, r'c\.key === "code"\) return \{ v: \'<button type="button" data-ctropen=')
        self.assertIn('role="link" tabindex="0"', js)
        self.assertIn('(c.id === caseId ? \' aria-current="page"\' : "")', js)
        self.assertIn('b.setAttribute("aria-current", "page")', js)
        self.assertIn('aria-label="Разделы плана"', html)
        self.assertIn('data-view="ctr" aria-current="page"', html)

    def test_chat_is_a_panel_not_a_tab(self):
        # Вкладки «Лента» нет (пользователь 30.09.2026: «Лента сейчас вообще
        # не информативна, нужна ли она?» — «чат панелью»): переписка и поле
        # ввода — в выдвижной панели справа по значку в шапке плана. Вкладки —
        # «Договоры · План ФОТ · Проекты приказов», план открывается на первой.
        js = (ROOT / 'app/static/app.js').read_text(encoding='utf-8')
        html = (ROOT / 'app/static/index.html').read_text(encoding='utf-8')
        start = html.index('<nav class="tabs" id="tabs"')
        tabs = html[start:html.index("</nav>", start)]
        self.assertNotIn('data-view="feed"', tabs)
        order = [tabs.index('data-view="%s"' % k) for k in ("ctr", "sum", "ord")]
        self.assertEqual(order, sorted(order))
        # Чат — правая колонка сетки (класс side), а не слой поверх панели
        # договора (пользователь 30.09.2026: «я уверена — дизайнер обосрёт»).
        panel = html[html.index('<aside class="side chatpanel" id="chatpanel"'):]
        panel = panel[:panel.index("</aside>")]
        for part in ('id="feed"', 'id="composer"', 'id="closechat"'):
            self.assertIn(part, panel)
        self.assertIn('id="togglechat"', html[html.index('<header class="phead">'):html.index("</header>")])
        self.assertIn('var view = "ctr",', js)
        self.assertNotIn('setView("feed")', js)
        self.assertIn('data-openchat>Ответить в чате</button>', js)
        # Колонка чата видна, когда он открыт и есть план, чья это переписка:
        # с 30.09.2026 чат открывают с любого экрана, и без плана его значок
        # недоступен (UI-012) — поэтому условие не chatOpen, а chatShown().
        self.assertIn('shell.classList.toggle("noside", !chatShown());', js)
        self.assertIn('function chatShown() { return chatOpen && !!caseId; }', js)
        self.assertIn('<div class="shell noside">', html)
        self.assertNotIn("Только в этот план", js)
        # Значок стоит по центру кнопки: браузерное поле кнопки обнулено.
        css = (ROOT / 'app/static/app.css').read_text(encoding='utf-8')
        self.assertIn('.iconbtn{width:26px;height:26px;display:grid;place-items:center;padding:0;', css)

    def test_faint_values_stay_readable(self):
        # UI-019: текст не бледнее 4,5:1. Пустое значение было --ink4 (3,15:1),
        # шифр снятого договора — --ink3 с прозрачностью .7 (2,94:1).
        css = (ROOT / 'app/static/app.css').read_text(encoding='utf-8')
        self.assertIn('.view td .ed.dim{color:var(--ink3)}', css)
        off = re.search(r'\.view tr\.off td\.key,\.view tr\.off \.tag\{([^}]*)\}', css)
        self.assertIsNotNone(off)
        self.assertNotIn('opacity', off.group(1))

    def test_controls_follow_one_scale(self):
        # Меры Consta (пользователь 20.09.2026: «возьми из библиотеки»):
        # контрол в таблице 24 px, в окне 32 px, радиус 4, флажок 16 px,
        # стрелка выбора своя.
        css = (ROOT / 'app/static/app.css').read_text(encoding='utf-8')
        self.assertIn('.view .ed input,.view .ed select{width:100%;min-width:96px;height:24px;', css)
        self.assertIn('.ovprops .ovf{flex:1 1 auto;width:100%;min-width:0;max-width:none;'
                      'height:24px;', css)
        self.assertIn('.modal .fld input,.modal .fld select{height:32px;', css)
        self.assertIn('input[type=checkbox],input[type=radio]{width:16px;height:16px}', css)
        self.assertIn('select:not([multiple]):not([size]){', css)
        self.assertIn('appearance:none;-webkit-appearance:none;min-width:56px;'
                      'padding-right:var(--selpad);', css)
        # Место под своей стрелкой — одной мерой, и правила, задающие полям в
        # ячейках свои отступы, её повторяют: иначе значение уезжает под
        # значок (пользователь 21.09.2026: «там нет зазоров»).
        self.assertIn('--selpad:24px;', css)
        # Кольцо фокуса у поля со своей рамкой лежит на рамке, а не через
        # зазор: при дробном масштабе страницы зазор ложится на полпикселя и
        # рамка читается съехавшей (пользователь 28.09.2026). У кнопок и
        # ссылок зазор остаётся, флажки не трогаем.
        ring = ('input:not([type=checkbox]):not([type=radio]):focus-visible,'
                '\nselect:not([multiple]):not([size]):focus-visible,'
                '\ntextarea:focus-visible{outline-offset:-1px}')
        self.assertIn(ring, css)
        # У кнопок, ссылок и строк зазор остаётся прежним.
        self.assertIn('button:focus-visible,select:focus-visible,a:focus-visible,'
                      'input:focus-visible,[tabindex]:focus-visible{',  css)
        self.assertIn('outline:2px solid var(--acc);outline-offset:2px}', css)
        # Фон поля выбора задаётся вместе со стрелкой: записанная отдельным
        # свойством, она стиралась правилом ячейки, и поле выглядело как
        # надпись (пользователь 21.09.2026: «с рамкой проблема»). Правило,
        # красящее заодно поля ввода, вправе не знать о стрелке — но тогда
        # ниже стоит правило того же места только для выбора.
        plain = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        rules = [(r.group(1).strip(), " ".join(r.group(2).split()))
                 for r in re.finditer(r"([^{}]*)\{([^{}]*)\}", plain)]
        for i, (sel, body) in enumerate(rules):
            own = [s.strip() for s in sel.split(",") if s.strip().endswith("select")]
            if not own or "background" not in body:
                continue
            if "--selarrow" in body:
                self.assertIn("--selpad", body, sel)
                continue
            later = [b for s, b in rules[i + 1:]
                     if "--selarrow" in b and any(x in [z.strip() for z in s.split(",")] for x in own)]
            self.assertTrue(later, "фон стирает стрелку и её никто не возвращает: " + sel)
        # Полосатых строк и прописных в шапке графы из Consta не берём.
        self.assertNotIn('zebra', css.lower())

    def test_focus_ring_is_one_rule_with_refinements(self):
        """Кольцо фокуса: одно общее правило в основании, частные уточняют его.

        Общее правило стояло посреди таблицы стилей, и у частных правил, стоящих
        выше, тот же вес селектора — общее молча их перебивало: у плана в списке
        и у ручки ширины кольцо возвращалось наружу, хотя для них задано внутрь
        (пользователь 28.09.2026: «проверь все рамки»).
        """
        css = (ROOT / 'app/static/app.css').read_text(encoding='utf-8')
        base = css.index('button:focus-visible,select:focus-visible,a:focus-visible,'
                         'input:focus-visible,[tabindex]:focus-visible{')
        # Ни одно правило фокуса не стоит выше общего: первое упоминание
        # :focus-visible в файле — это его же селектор.
        first = css.index(':focus-visible')
        self.assertEqual(base, first - len('button'), 'общее правило фокуса не в основании')
        # Частные решения ниже общего и потому действуют.
        for later in ('.grip:focus-visible{outline:none}',
                      '.case:focus-visible{outline:2px solid var(--accring);outline-offset:-2px}',
                      '.tabs .tab:focus-visible{outline-offset:-2px}'):
            self.assertIn(later, css, later)
            self.assertGreater(css.index(later), base, later)
        # Список с прокруткой оставляет место кольцу последней строки.
        self.assertIn('.substbox .sl,.menu.pick .plist,.menu[data-colmenu] .mvs{scroll-padding:4px}',
                      css)

    def test_tab_counter_is_a_muted_chip(self):
        """Счётчик у вкладки — приглушённая плашка со скруглёнными углами, а не
        голое число (решение пользователя 29.09.2026). У выбранной вкладки —
        тоном выбора, сигнальных цветов нет: это число строк, а не тревога."""
        css = (ROOT / 'app/static/app.css').read_text(encoding='utf-8')
        self.assertIn('#regview .reghead .rtabs button .c{display:inline-grid;place-items:center;'
                      'min-width:18px;', css)
        self.assertIn('border-radius:var(--r2);background:var(--s2);color:var(--ink3);', css)
        self.assertIn('#regview .reghead .rtabs button.on .c{background:var(--navon);'
                      'color:var(--acc)}', css)
        # Прежнее «без подложки» для вкладок снято.
        self.assertNotIn('#regview .reghead .rtabs button.on .c{display:inline;min-width:0;', css)

    def test_plan_tabs_look_like_registry_tabs(self):
        # Пользователь 20.09.2026: «сделай вкладки такие же по стилю» —
        # выбранная вкладка плана акцентом, как во вкладках реестра.
        css = (ROOT / 'app/static/workspace.css').read_text(encoding='utf-8')
        self.assertIn('#regview .reghead .rtabs button.on{color:var(--acc);'
                      'border-bottom-color:var(--acc);font-weight:500}', css)
        self.assertIn('.tabs .tab.on{color:var(--acc);border-bottom-color:var(--acc);'
                      'font-weight:500}', css)
        # Не помещаются — переносятся, без боковой прокрутки (UI-014).
        self.assertIn('.tabs{column-gap:24px;padding:0 22px;flex-wrap:wrap;overflow-x:visible}', css)
        self.assertIn('#regview .reghead .rtabrow > .rtabs{gap:0 24px}', css)

    def test_table_add_button_is_quiet(self):
        # Пользователь 19.09.2026: «Добавить сотрудника» и загрузка на панели
        # таблицы — не такие яркие. Заливкой акцентом остаётся одна кнопка на
        # экране — «Рассчитать план»; текст тихой кнопки читается не хуже 4,5:1.
        css = (ROOT / 'app/static/workspace.css').read_text(encoding='utf-8')
        rule = re.search(r'#regview \.tbar \[data-tbadd\](?:,#view :where\(\.gridwrap\) \.tbar '
                         r'\[data-tbadd\])?\{([^}]*)\}', css).group(1)
        self.assertIn('background:var(--accbg)', rule)
        self.assertIn('color:var(--accink)', rule)
        root = css.split(':root{', 1)[1].split('}', 1)[0]
        tokens = dict(re.findall(r'--([\w-]+):\s*(#[\da-fA-F]{6})', root))
        self.assertGreaterEqual(contrast(tokens['accink'], tokens['accbg']), 4.5)

    def test_fonts_are_local_files_of_the_design_system(self):
        # Гарнитуры дизайн-системы — Golos Text и PT Mono (UI-019). Сервис
        # работает в сети организации: файлы шрифтов лежат рядом, внешний
        # шрифтовой сервер не нужен; каждый подключённый файл на месте.
        css = (ROOT / 'app/static/fonts.css').read_text(encoding='utf-8')
        families = set(re.findall(r"font-family:'([^']+)'", css))
        self.assertEqual(families, {'Golos Text', 'PT Mono'})
        urls = re.findall(r"url\('([^']+)'\)", css)
        self.assertEqual(len(urls), 4)
        for url in urls:
            self.assertTrue(url.startswith('/fonts/'), url)
            self.assertTrue((ROOT / 'app/static' / url.lstrip('/')).is_file(), url)
        self.assertNotIn('http', css)
        html = (ROOT / 'app/static/index.html').read_text(encoding='utf-8')
        self.assertIn('<link rel="stylesheet" href="/fonts.css">', html)

    def test_issues_table_uses_design_tokens(self):
        # Таблица замечаний — токенами дизайн-системы, своих цветов нет (UI-019).
        css = (ROOT / 'app/static/issues.css').read_text(encoding='utf-8')
        self.assertEqual(re.findall(r'#[0-9a-fA-F]{3,8}\b', css), [])
        self.assertIn('var(--accbg)', css)

    def test_design_system_and_mockup_docs_are_whole(self):
        # Дизайн-система и макет в docs/ui (UI-019): данные читаются, ссылки
        # между страницами не битые, у каждого экрана макета есть и исходник
        # доски, и выгрузка в HTML.
        ui = ROOT / 'docs/ui'
        for name in ('design-system/design-system.json', 'design-system/tokens.json',
                     'mockup/project/canvas.json'):
            json.loads((ui / name).read_text(encoding='utf-8'))
        pages = sorted((ui / 'design-system').rglob('*.md')) + [
            ui / 'README.md', ui / 'mockup/README.md', ui / 'mockup/ЗАДАНИЕ.md']
        for md in pages:
            for link in re.findall(r'\]\(([^)#\s]+)', md.read_text(encoding='utf-8')):
                if not link.startswith(('http', 'mailto:')):
                    self.assertTrue((md.parent / link).exists(), '%s → %s' % (md.name, link))
        boards = {p.name[:-len('.dc.html')] for p in (ui / 'mockup/project').glob('*.dc.html')}
        exported = {p.stem for p in (ui / 'mockup/html').glob('*.html')}
        self.assertEqual(boards, exported)
        self.assertEqual(len(boards), 7)


class ModalFocusTests(unittest.TestCase):
    """DEV-UI-MODAL-ENTER-001: окно держит фокус, Enter нажимает кнопку в фокусе."""

    def test_hold_focus_cycles_and_returns(self):
        setup = (
            "var document={activeElement:null,body:{name:'body'},contains:function(){return true;}};"
            "function el(n){return {name:n,disabled:false,tabIndex:0,"
            "getClientRects:function(){return [1];},focus:function(){document.activeElement=this;}};}"
            "var opener=el('opener'),a=el('a'),b=el('b');document.activeElement=opener;"
            "var handler=null,closed=0;"
            "var wrap={addEventListener:function(t,f){handler=f;},"
            "querySelectorAll:function(){return [a,b];}};")
        run = setup + js_function('holdFocus') + (
            "var release=holdFocus(wrap,function(){closed++;});"
            "function key(k,s){var e={key:k,shiftKey:!!s,prevented:false,"
            "preventDefault:function(){this.prevented=true;}};handler(e);return e;}"
            "var out={};"
            "b.focus();key('Tab');out.tab=document.activeElement.name;"
            "a.focus();key('Tab',true);out.back=document.activeElement.name;"
            "a.focus();out.inside=key('Tab').prevented;"
            "var en=key('Enter');out.enter=en.prevented||closed>0;"
            "key('Escape');out.closed=closed;"
            "release();out.focus=document.activeElement.name;"
            "console.log(JSON.stringify(out));")
        out = node(run)
        self.assertEqual(out['tab'], 'a')          # с последнего органа — на первый
        self.assertEqual(out['back'], 'b')         # Shift+Tab с первого — на последний
        self.assertFalse(out['inside'])            # внутри окна Tab идёт своим ходом
        self.assertFalse(out['enter'])             # Enter окно не перехватывает
        self.assertEqual(out['closed'], 1)         # Esc закрывает
        self.assertEqual(out['focus'], 'opener')   # фокус вернулся к вызвавшему

    def test_every_modal_holds_focus(self):
        # Раньше каждое окно вешало keydown на document, а ask() на Enter
        # подтверждал удаление, даже когда фокус стоял на «Отмене».
        js = (ROOT / 'app/static/app.js').read_text(encoding='utf-8')
        starts = [m.start() for m in re.finditer(r'^function \w+\(', js, re.M)] + [len(js)]
        bodies = [js[s:e] for s, e in zip(starts, starts[1:])
                  if 'className = "modal"' in js[s:e]]
        self.assertGreaterEqual(len(bodies), 6)
        for body in bodies:
            with self.subTest(body[:body.index('(')]):
                self.assertIn('holdFocus(', body)
                self.assertNotIn('document.addEventListener("keydown"', body)
        ask = js_function('ask')
        self.assertIn('querySelector(".no").focus()', ask)
        self.assertNotIn('"Enter"', ask)


class ApiErrorTests(unittest.TestCase):
    """UI-016 п. 2: ошибка говорит словами сервера, а не кодом HTTP."""

    def test_error_says_server_words(self):
        run = js_function('api') + (
            "function call(resp){global.fetch=function(){return resp;};"
            "return api('/x').then(function(v){return {ok:v};},"
            "function(e){return {msg:e.message,status:e.status||null};});}"
            "Promise.all(["
            "call(Promise.resolve({ok:false,status:404,json:function(){"
            "return Promise.resolve({detail:'план не найден'});}})),"
            "call(Promise.resolve({ok:false,status:500,json:function(){"
            "return Promise.reject(new Error('html'));}})),"
            "call(Promise.reject(new TypeError('Failed to fetch'))),"
            "call(Promise.resolve({ok:true,status:200,json:function(){"
            "return Promise.resolve({a:1});}}))"
            "]).then(function(r){console.log(JSON.stringify(r));});")
        said, broken, offline, fine = node(run)
        self.assertEqual(said, {'msg': 'план не найден', 'status': 404})
        self.assertEqual(broken, {'msg': 'сервер ответил ошибкой 500', 'status': 500})
        self.assertEqual(offline, {'msg': 'нет связи с сервером', 'status': None})
        self.assertEqual(fine, {'ok': {'a': 1}})
        js = (ROOT / 'app/static/app.js').read_text(encoding='utf-8')
        self.assertNotIn('"HTTP " + r.status', js)
        self.assertNotIn('/HTTP 404/', js)


if __name__ == '__main__':
    unittest.main()
