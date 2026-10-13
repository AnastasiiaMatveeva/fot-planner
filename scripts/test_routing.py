# -*- coding: utf-8 -*-
"""Регрессии UI-021 и UI-012: адрес экрана и чат с любого экрана.

Пользователь 30.09.2026: «добавь урлы, сейчас у тебя путь всегда один, при
перезагрузке всё сбрасывается. Нужно на все действия иметь пути, чтоб при
обновлении страницы всё не сбрасывалось на стартовую» и «сделай так, чтоб чат
с агентами можно было из любого места открыть, и если открыт, чтоб не
сбрасывался»; «зачем решётка в урлах» — адрес экрана с тех пор путь
(/plan/14/orders), а старая ссылка с «#» читается и переписывается.

Проверка по исходникам и в node без браузера и живой базы: разбор и сборка
адреса, выбор «шаг истории или замена», переходы через одно место, значок
чата на реестре, черновик и открытость чата в браузере. Перезагрузка и
«Назад» в самом браузере проверяются вручную (docs/ИНТЕРФЕЙС.md, UI-021).
"""
from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "static" / "app.css").read_text(encoding="utf-8")


def body_of(name):
    """Тело функции: от объявления до следующей на верхнем уровне."""
    start = JS.index("function " + name + "(")
    end = JS.index("\nfunction ", start + 1)
    return JS[start:end]


def between(start, end):
    """Кусок app.js от метки до метки (вторая не входит)."""
    a = JS.index(start)
    return JS[a:JS.index(end, a)]


#: Разбор и сборка адреса — чистые функции, в node их берём как есть.
ROUTES = between("var ROUTE_REG = ", "function routeState(")
#: Переходы: адрес из состояния, состояние из адреса, запись истории.
ROUTING = between("var ROUTE_REG = ", "function routeFromAddress(")
REG_TABS = between("var REG_TABS = [", "var REG_GROUPS")


def node(script):
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         encoding="utf-8", check=True)
    return json.loads(out.stdout)


def parse_all(addresses):
    return node(ROUTES + "console.log(JSON.stringify(%s.map(parseRoute)));" % json.dumps(addresses))


def path_all(routes):
    return node(ROUTES + "console.log(JSON.stringify(%s.map(routePath)));" % json.dumps(routes))


class RouteAddressTests(unittest.TestCase):
    """Адрес ↔ экран: всё, что пишется, читается обратно тем же экраном."""

    def test_round_trip(self):
        routes = [
            {"screen": "registry", "tab": t, "contract": None, "ctab": "props", "doc": None,
             "on": None}
            for t in ("ctr", "emp", "zp", "ref", "sub", "secret", "docs")
        ] + [
            {"screen": "registry", "tab": "ctr", "contract": "C_BASE-26", "ctab": c, "doc": None,
             "on": None}
            for c in ("props", "wg", "labor", "docs")
        ] + [
            # Шифр со знаками адреса и кириллицей не рвёт адрес.
            {"screen": "registry", "tab": "ctr", "contract": "ГОЗ/12 ?#&", "ctab": "labor",
             "doc": 57, "on": None},
            # Справочник на дату (SOURCE-012): дата — в адресе вкладки.
            {"screen": "registry", "tab": "ref", "contract": None, "ctab": "props", "doc": None,
             "on": "2026-03-01"},
        ] + [
            {"screen": "plan", "plan": 14, "view": v, "pctr": None,
             "sec": 1 if v == "sum" else None, "run": None, "doc": None}
            for v in ("ctr", "emp", "zp", "sum", "ord")
        ] + [
            {"screen": "plan", "plan": 14, "view": "ctr", "pctr": "C_BASE-26/1", "sec": None,
             "run": None, "doc": None},
            {"screen": "plan", "plan": 14, "view": "sum", "pctr": None, "sec": 11, "run": 37,
             "doc": None},
            {"screen": "plan", "plan": 3, "view": "ord", "pctr": None, "sec": None, "run": 5,
             "doc": 9},
        ]
        paths = path_all(routes)
        self.assertEqual(parse_all(paths), routes)
        # Старая ссылка с «#» читается тем же экраном.
        self.assertEqual(parse_all(["#" + x for x in paths]), routes)
        # Адрес — путь без «#»: латиница, цифры и закодированный шифр,
        # подписи вкладок в него не попадают.
        for h in paths:
            # Значения запроса — числа и дата справочника «2026-03-01».
            self.assertRegex(h, r"^/(registry|plan)/[A-Za-z0-9/%_.\-]*(\?[a-z]+=[0-9-]+(&[a-z]+=[0-9-]+)*)?$")

    def test_readable_addresses(self):
        """Адреса читаются: вкладки словами, свойства договора и первый раздел
        отчёта — без лишнего слова, расчёт по умолчанию — без параметра."""
        got = path_all([
            {"screen": "registry", "tab": "emp"},
            {"screen": "registry", "tab": "ctr", "contract": "C_BASE-26", "ctab": "props"},
            {"screen": "registry", "tab": "ctr", "contract": "C_BASE-26", "ctab": "wg"},
            {"screen": "plan", "plan": 14, "view": "ctr", "pctr": "C_BASE-26"},
            {"screen": "plan", "plan": 14, "view": "sum", "sec": 1},
            {"screen": "plan", "plan": 14, "view": "sum", "sec": 11, "run": 37},
            {"screen": "registry", "tab": "docs", "doc": 57},
            # Шифр в адресе — только у вкладки «Договоры»; на плане — только на
            # «Договорах», раздел — только у отчёта.
            {"screen": "registry", "tab": "emp", "contract": "C_BASE-26"},
            {"screen": "plan", "plan": 14, "view": "emp", "pctr": "C_BASE-26", "sec": 5},
            None,
        ])
        self.assertEqual(got, [
            "/registry/staff",
            "/registry/contracts/C_BASE-26",
            "/registry/contracts/C_BASE-26/workgroup",
            "/plan/14/contracts/C_BASE-26",
            "/plan/14/result",
            "/plan/14/result/11?run=37",
            "/registry/documents?doc=57",
            "/registry/staff",
            "/plan/14/staff",
            "",
        ])

    def test_foreign_and_broken_addresses(self):
        """Чужой или испорченный адрес не ломает вход: не наш — null (вход как
        без адреса), неизвестное слово — первая вкладка, хвост после него не
        читается, неверная кодировка — как есть."""
        got = parse_all(["", "#", "#sec-3", "#/plan/abc", "#/nope", "#/plan/0",
                         "#/registry", "#/registry/nope/X", "#/plan/14",
                         "#/plan/14/nope/x", "#/plan/14/result/0", "#/plan/14/result/x?run=-1",
                         "#/registry/contracts/%E0%A4%A", "#/registry/contracts/", "#/plan/14?doc=abc"])
        self.assertEqual(got[:6], [None] * 6)
        registry_list = {"screen": "registry", "tab": "ctr", "contract": None, "ctab": "props",
                         "doc": None, "on": None}
        self.assertEqual(got[6], registry_list)
        self.assertEqual(got[7], registry_list)
        plan = {"screen": "plan", "plan": 14, "view": "ctr", "pctr": None, "sec": None,
                "run": None, "doc": None}
        self.assertEqual(got[8], plan)
        self.assertEqual(got[9], plan)
        self.assertEqual(got[10]["sec"], 1)
        self.assertEqual((got[11]["sec"], got[11]["run"]), (1, None))
        self.assertEqual(got[12]["contract"], "%E0%A4%A")
        self.assertEqual(got[13], registry_list)
        self.assertEqual(got[14], plan)
        # То же путём, без «#».
        paths = parse_all(["/", "/plan/abc", "/nope", "/registry", "/plan/14/result/11?run=37"])
        self.assertEqual(paths[:3], [None] * 3)
        self.assertEqual(paths[3], registry_list)
        self.assertEqual((paths[4]["sec"], paths[4]["run"]), (11, 37))

    def test_every_tab_has_a_word(self):
        """У каждой вкладки реестра, договора и плана — своё слово адреса."""
        keys = re.findall(r'key: "(\w+)"', REG_TABS)
        slugs = node(ROUTES + "console.log(JSON.stringify([ROUTE_REG, ROUTE_CTAB, ROUTE_VIEW]));")
        self.assertEqual(sorted(keys), sorted(slugs[0]))
        ctabs = re.findall(r'\["(\w+)", "', body_of("contractTabs"))
        self.assertEqual(sorted(ctabs), sorted(slugs[1]))
        views = re.findall(r'data-view="(\w+)"', HTML)
        self.assertEqual(sorted(views), sorted(slugs[2]))
        for word in list(slugs[0].values()) + list(slugs[1].values()) + list(slugs[2].values()):
            self.assertRegex(word, r"^[a-z0-9-]*$")


#: Заглушки экрана для переходов в node: что вызвано — в журнале.
STAGE = r"""
var log = [];
var location = { hash: "", pathname: "/", search: "" };
function go(h) {
  var i = h.indexOf("?");
  location.pathname = i < 0 ? h : h.slice(0, i);
  location.search = i < 0 ? "" : h.slice(i);
  location.hash = "";
}
var history = { state: null,
  pushState: function (s, t, h) { log.push("push " + h); this.state = s; go(h); },
  replaceState: function (s, t, h) { log.push("replace " + h); this.state = s; go(h); } };
var document = { querySelector: function () { return null; } };
var regview = { scrollTop: 40 };
function $(id) { return id === "regview" ? regview : { scrollTop: 0 }; }
var inRegistry = false, inAgents = false, caseId = 14, view = "ctr", pctrOpen = null,
    repSec = 1, chosenRun = null, regTab = "ctr", ctrScreen = null, ctrTab = "props",
    ctrOpen = null, docShown = null, vrun = 1, vrep = {}, refOn = null;
function openRegistry() { inRegistry = true; log.push("openRegistry"); }
function leaveRegistry() { inRegistry = false; }
function leaveAgents() { inAgents = false; }
function renderRegistry() { log.push("renderRegistry"); routeWrite("replace"); }
function setView(v) { view = v; log.push("setView " + v); }
function renderView() { log.push("renderView"); }
function showSection(n) { repSec = n; log.push("showSection " + n); }
function planSwitch(id) { caseId = id; chosenRun = null; pctrOpen = null; log.push("planSwitch " + id); }
function issueLeave() { return null; }
function loadCases() {}
function tick() {}
function fullReport() {}
function chatSync() {}
function showDocument(id) {
  docShown = { id: id, close: function () { docShown = null; log.push("closeDoc " + id); } };
  log.push("showDocument " + id);
}
"""


def run_stage(steps):
    """Прогнать переходы и вернуть журнал: что нарисовано и что записано в адрес."""
    script = (STAGE + REG_TABS + ROUTING + steps +
              "console.log(JSON.stringify({log: log, address: location.pathname + location.search,"
              " hash: location.hash, state: history.state,"
              " regTab: regTab, ctrScreen: ctrScreen, ctrTab: ctrTab, view: view,"
              " pctrOpen: pctrOpen, caseId: caseId, inRegistry: inRegistry}));")
    return node(script)


class RouteHistoryTests(unittest.TestCase):
    """Какой переход — шаг истории, какой — замена; экран меняется одним путём."""

    def test_panel_replaces_screen_pushes(self):
        out = run_stage(
            'go("/plan/14/contracts");'
            'navigate({ pctr: "A" }, "replace");'
            'navigate({ pctr: "B" }, "replace");'
            'navigate({ screen: "plan", view: "sum", reload: true }, "push");'
            'navigate({ sec: 11 }, "push");')
        self.assertEqual(out["log"], [
            # Панель договора — выбор строки: адрес заменяется, «Назад» не
            # перебирает просмотренные договоры.
            "renderView", "replace /plan/14/contracts/A",
            "renderView", "replace /plan/14/contracts/B",
            # Вкладка и раздел отчёта — шаги истории.
            "setView sum", "push /plan/14/result",
            "showSection 11", "push /plan/14/result/11",
        ])

    def test_old_hash_link_is_rewritten_as_path(self):
        """Старая ссылка с «#» открывает тот же экран, и адрес переписывается
        путём — без «#» (пользователь 30.09.2026: «зачем решётка в урлах»)."""
        out = run_stage(
            'location.hash = "#/plan/14/contracts";'
            'navigate({ pctr: "A" }, "replace");')
        self.assertEqual(out["log"], ["renderView", "replace /plan/14/contracts/A"])
        self.assertEqual((out["address"], out["hash"]), ("/plan/14/contracts/A", ""))

    def test_same_screen_writes_nothing(self):
        out = run_stage(
            'go("/plan/14/contracts");'
            'navigate({ screen: "plan", view: "ctr" }, "push");')
        self.assertEqual(out["log"], [])

    def test_registry_and_contract(self):
        out = run_stage(
            'navigate({ screen: "registry" }, "push");'
            'inRegistry = true;'
            'navigate({ screen: "registry", tab: "emp" }, "push");'
            'navigate({ screen: "registry", tab: "ctr", contract: "ГОЗ/12", ctab: "wg" }, "push");'
            'navigate({ screen: "registry", contract: null }, "push");')
        self.assertEqual(out["log"], [
            "openRegistry", "push /registry/contracts",
            # Переход рисует экран, а запись адреса на это время молчит:
            # renderRegistry зовёт routeWrite, но пишет только navigate.
            "renderRegistry", "push /registry/staff",
            "renderRegistry", "push /registry/contracts/%D0%93%D0%9E%D0%97%2F12/workgroup",
            "renderRegistry", "push /registry/contracts",
        ])
        # Вкладка договора помнится, пока открыт список: к ней вернёт шифр.
        self.assertEqual(out["ctrTab"], "wg")

    def test_back_restores_screen_and_keeps_remembered_contract(self):
        """«Назад» ставит экран по адресу тем же путём, что щелчок, и ничего
        не пишет в историю сам."""
        out = run_stage(
            'inRegistry = true; regTab = "ctr"; ctrScreen = "X"; ctrTab = "labor";'
            'go("/registry/contracts/X/labor");'
            'applyRoute(parseRoute("/registry/staff"));')
        self.assertEqual(out["log"], ["renderRegistry"])
        self.assertEqual((out["regTab"], out["ctrScreen"], out["ctrTab"]), ("emp", "X", "labor"))
        out = run_stage(
            'inRegistry = true; regTab = "emp"; ctrScreen = "X"; ctrTab = "labor";'
            'applyRoute(parseRoute("/plan/15/result/3?run=7"));')
        self.assertEqual(out["log"], ["planSwitch 15", "setView sum"])
        self.assertFalse(out["inRegistry"])

    def test_document_card_is_a_step(self):
        out = run_stage(
            'inRegistry = true; regTab = "docs"; go("/registry/documents");'
            'navigate({ doc: 57 }, "push");'
            # «Назад» с карточки: экран тот же, карточка закрыта.
            'applyRoute(parseRoute("/registry/documents"));')
        self.assertEqual(out["log"], ["showDocument 57", "push /registry/documents?doc=57",
                                      "closeDoc 57"])
        # В шаге истории записано, какую карточку он открыл: закрывая её
        # крестиком, этот шаг убирают (docLeave).
        self.assertEqual(out["state"], {"doc": 57})

    def test_plan_switch_resets_plan_choices(self):
        out = run_stage(
            'pctrOpen = "A"; chosenRun = 37; go("/plan/14/contracts/A?run=37");'
            'navigate({ screen: "plan", plan: 15, view: "ctr", pctr: null, run: null,'
            ' reload: true }, "push");')
        self.assertEqual(out["log"], ["planSwitch 15", "setView ctr", "push /plan/15/contracts"])


class RouteWiringTests(unittest.TestCase):
    """Все переходы идут через navigate(), адрес пишет только routeWrite()."""

    def test_history_is_written_in_one_place(self):
        writes = [m.start() for m in re.finditer(r"history\.(push|replace)State\(", JS)]
        self.assertEqual(len(writes), 2)
        route_write = JS.index("function routeWrite(")
        end = JS.index("\nfunction ", route_write + 1)
        for at in writes:
            self.assertTrue(route_write < at < end, "адрес пишется в обход routeWrite")
        self.assertIn('window.addEventListener("popstate", routeFromAddress);', JS)
        # «#» — только у старых ссылок, набранных руками: их ловит hashchange.
        self.assertIn('window.addEventListener("hashchange", routeFromAddress);', JS)
        # При правке адреса руками приходят оба события — второе не повторяет.
        self.assertIn("if (routeCurrent() === routeSeen) return;", body_of("routeFromAddress"))
        # Пока экран ставится по адресу, адрес не пишется.
        self.assertIn("if (routeApplying) return;", body_of("routeWrite"))

    def test_entry_points_navigate(self):
        for part in (
            # план из колонки, вкладка плана, «Текущий план», новый план
            'navigate({ screen: "plan", plan: id, view: "ctr", pctr: null, run: null, reload: true }, how);',
            'navigate({ screen: "plan", view: b.getAttribute("data-view"), reload: true }, "push");',
            '$("opencurrent").addEventListener("click", function () { openCurrent("push"); });',
            # реестр, его вкладки, договор, вкладка договора, путь назад
            'navigate({ screen: "registry", reload: true }, "push");',
            'navigate({ screen: "registry", tab: t.getAttribute("data-rtab") }, "push");',
            'navigate({ screen: "registry", tab: "ctr", contract: co.getAttribute("data-ctropen"),',
            'navigate({ screen: "registry", ctab: ct.getAttribute("data-ctab") }, "push");',
            # панель договора в плане — заменой; её закрытие — тоже
            'navigate({ pctr: code }, "replace");',
            'navigate({ pctr: null }, "replace");',
            # карточка документа, расчёт в шапке плана
            'navigate({ doc: id }, "push");',
            # — на «Проектах приказов» смена расчёта оставляет на них (30.09.2026)
            'navigate({ screen: "plan", view: view === "ord" ? "ord" : "sum",\n'
            '             run: last && last.id === id ? null : id }, "push");',
        ):
            self.assertIn(part, JS, part)
        # Раздел отчёта и раздел замечания сами показывают себя, адрес —
        # следом, шагом истории.
        self.assertIn("showSection(+nav.dataset.sec);\n    routeWrite(\"push\");", JS)
        self.assertIn('routeWrite("push");', body_of("openPlanIssue"))
        # Вкладку плана переключает только переход; setView(view) — перечитать
        # ту же вкладку после отказа сервера, это не переход.
        calls = [JS[m.start() - 40:m.end() + 12] for m in re.finditer(r"[^.\w]setView\(", JS)
                 if not JS[m.start() - 10:m.start() + 1].endswith("function ")]
        allowed = ("setView(v);", "setView(view);")
        for call in calls:
            self.assertTrue(any(a in call for a in allowed), call)
        # Ссылка раздела отчёта — адрес этого раздела: новая вкладка браузера
        # открывает тот же раздел, а не «#sec-N».
        self.assertNotIn('href="#sec-', JS)
        self.assertIn('var to = routePath({ screen: "plan", plan: caseId, view: "sum", sec: x.n, run: chosenRun });',
                      body_of("repNav"))
        self.assertNotIn("routeHash", JS)

    def test_stale_addresses_fall_back(self):
        """Удалённое по старой ссылке — ближайший живой экран, адрес заменой."""
        self.assertIn('if (ctrScreen && !one) { ctrScreen = null; routeWrite("replace"); }',
                      body_of("renderRegistry"))
        render = body_of("renderView")
        self.assertIn("if (pctrOpen && vdata && !c.some(function (x) { return x.code === pctrOpen; })) {",
                      render)
        self.assertIn('repSec = REP_SECTIONS.length ? REP_SECTIONS[0].n : 1;\n        routeWrite("replace");',
                      render)
        self.assertIn('chosenRun = null;\n    routeWrite("replace");', body_of("loadResult"))
        self.assertIn('showToast("План не найден");', body_of("routeTo"))
        show = body_of("showDocument")
        self.assertIn("if (e && e.status === 404) {", show)
        self.assertIn('routeWrite("replace");', show)
        # Опрос по плану, с которого ушли, экран не трогает.
        self.assertIn("if (id !== caseId) return;", body_of("tick"))

    def test_start_reads_the_address(self):
        start = JS[JS.index("loadCases().then(function (rows) {\n  routeStart(rows);"):]
        self.assertIn("chatRestore();", start[:200])
        route_start = body_of("routeStart")
        self.assertIn("var r = parseRoute(routeCurrent());", route_start)
        self.assertIn('openCurrent("replace")', route_start)

    def test_server_gives_the_page_at_screen_addresses(self):
        """Путь экрана — та же страница: сервер отдаёт index.html на /plan/…
        и /registry/…, иначе перезагрузка по такому адресу давала бы 404.
        Маршруты стоят раньше раздачи статики."""
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        for route in ('@app.get("/plan/{rest:path}", include_in_schema=False)',
                      '@app.get("/registry", include_in_schema=False)',
                      '@app.get("/registry/{rest:path}", include_in_schema=False)'):
            self.assertLess(main.index(route), main.index('app.mount("/", NoCacheStatic('), route)
        self.assertIn('return FileResponse(os.path.join(STATIC, "index.html"),',
                      main[main.index("def screen_address("):])
        # Ресурсы страницы — от корня: с адреса /plan/14/orders они не ищутся
        # в /plan/14/.
        for ref in re.findall(r'(?:src|href)="([^"#]+)"', HTML):
            self.assertTrue(ref.startswith(("/", "http")), ref)


class ChatEverywhereTests(unittest.TestCase):
    """UI-012, 30.09.2026: чат открывают с любого экрана, переход его не
    закрывает и не перерисовывает, открытость и черновик переживают
    перезагрузку."""

    def test_toggle_on_plan_header_and_registry_title(self):
        head = HTML[HTML.index('<header class="phead">'):HTML.index("</header>")]
        self.assertIn('id="togglechat" data-chattoggle', head)
        toggle = between("var CHAT_TOGGLE = ", "/* Чат на экране")
        for part in ('class="iconbtn"', "data-chattoggle", 'aria-controls="chatpanel"',
                     'aria-label="Чат с агентами"'):
            self.assertIn(part, toggle)
        registry = body_of("renderRegistry")
        self.assertIn("""'<div class="regtitle"><h1>Реестр организации</h1>' + CHAT_TOGGLE""", registry)
        # Значок нарисован заново — состояние ему ставит тот же markFeedTab.
        self.assertLess(registry.index('$("regview").innerHTML = tabsRow + body;'),
                        registry.index("markFeedTab();"))
        # Один обработчик на оба значка.
        self.assertIn('var t = e.target.closest && e.target.closest("[data-chattoggle]");', JS)
        self.assertIn('.iconbtn[data-chattoggle][aria-expanded="true"]{', CSS)
        self.assertIn('.iconbtn[data-chattoggle][aria-disabled="true"]{', CSS)

    def test_navigation_does_not_close_the_chat(self):
        self.assertNotIn("function wideScreen(", JS)
        for name in ("openRegistry", "openAgents", "leaveRegistry", "leaveAgents", "routeGo",
                     "planSwitch"):
            body = body_of(name)
            self.assertNotIn("setChat(", body, name)
            # Поле ввода — в панели чата: прятать его на реестре нельзя.
            self.assertNotIn('.composer").hidden', body, name)
        self.assertNotIn('document.querySelector(".composer").hidden = true', JS)

    def test_opening_does_not_rerender_the_feed(self):
        body = body_of("setChat")
        self.assertNotIn("renderFeed", body)
        self.assertIn("f.scrollTop = feedPin ? f.scrollHeight : feedTop;", body)
        self.assertIn('try { localStorage.setItem("fot.chat", chatOpen ? "1" : "0"); } catch (e) {}', body)
        # Закрытая панель прокрутки не держит: место помнит обработчик прокрутки.
        self.assertIn("feedPin = f.scrollHeight - f.scrollTop - f.clientHeight < 60;", JS)
        feed = body_of("renderFeed")
        self.assertIn("if (!state || !state.case || state.case.id !== caseId)", feed)
        self.assertIn("var seen = feed.clientHeight > 0;", feed)

    def test_toggle_state_and_title(self):
        stage = (
            "var nodes = [{a: {}, setAttribute: function (k, v) { this.a[k] = String(v); },"
            " removeAttribute: function (k) { delete this.a[k]; }}];"
            "var h2 = {textContent: 'Чат с агентами', a: {}, setAttribute: function (k, v) { this.a[k] = v; },"
            " removeAttribute: function (k) { delete this.a[k]; }};"
            "var document = {querySelectorAll: function () { return nodes; },"
            " querySelector: function (s) { return s === '#chatpanel .chath h2' ? h2 : null; }};"
            "var caseId = null, unseen = 0, chatOpen = true, inRegistry = true, inAgents = false,"
            " state = null, casesCache = [{id: 14, title: 'План ФОТ на 2026 год'}];"
            + body_of("chatShown") + body_of("markFeedTab") + body_of("chatTitle") +
            "var out = {};"
            "markFeedTab(); out.none = JSON.parse(JSON.stringify(nodes[0].a));"
            "caseId = 14; unseen = 2; markFeedTab(); out.plan = JSON.parse(JSON.stringify(nodes[0].a));"
            "chatTitle(); out.registry = [h2.textContent, h2.a.title];"
            "inRegistry = false; chatTitle(); out.onplan = [h2.textContent, h2.a.title || null];"
            "console.log(JSON.stringify(out));")
        out = node(stage)
        # Без плана — чата нет: значок недоступен, причина — в подсказке.
        self.assertEqual(out["none"]["aria-disabled"], "true")
        self.assertEqual(out["none"]["title"], "Чат с агентами: планов нет")
        self.assertEqual(out["none"]["aria-label"], "Чат с агентами")
        self.assertEqual(out["none"]["aria-expanded"], "false")
        # С планом — нажат, точка новых реплик, недоступности нет.
        self.assertNotIn("aria-disabled", out["plan"])
        self.assertEqual(out["plan"]["aria-expanded"], "true")
        self.assertIn("data-new", out["plan"])
        # На реестре шапка называет план словом, а не знаком (UI-015); на
        # экране плана имя уже в заголовке плана (UI-002).
        self.assertEqual(out["registry"], ["Чат: План ФОТ на 2026 год",
                                           "Чат с агентами по плану «План ФОТ на 2026 год»"])
        self.assertEqual(out["onplan"], ["Чат с агентами", None])

    def test_draft_is_kept_per_plan(self):
        stage = (
            "var store = {}, broken = false;"
            "var localStorage = {getItem: function (k) { if (broken) throw new Error('x');"
            " return k in store ? store[k] : null; },"
            " setItem: function (k, v) { if (broken) throw new Error('x'); store[k] = String(v); }};"
            "var input = {value: ''};"
            "function $(id) { return input; }"
            "var caseId = 14;"
            + between("var DRAFT_KEY = ", "$(\"text\").addEventListener(\"input\", draftSave);") +
            "var out = {};"
            "input.value = 'поправь оклад'; draftSave();"
            "caseId = 15; draftLoad(); out.other = input.value;"
            "input.value = 'про другой план'; draftSave();"
            "caseId = 14; draftLoad(); out.back = input.value;"
            "input.value = ''; draftSave(); out.cleared = JSON.parse(store['fot.chat.draft']);"
            "store['fot.chat.draft'] = '{broken'; out.badjson = draftMap();"
            "broken = true; input.value = 'x'; draftSave(); draftLoad(); out.nostorage = input.value;"
            "console.log(JSON.stringify(out));")
        out = node(stage)
        self.assertEqual(out["other"], "")
        self.assertEqual(out["back"], "поправь оклад")
        self.assertEqual(out["cleared"], {"15": "про другой план"})
        self.assertEqual(out["badjson"], {})
        # Хранилище браузера недоступно (приватное окно) — поле не ломается.
        self.assertEqual(out["nostorage"], "")
        # Отправленная реплика стирает черновик; не ушедшая — возвращается.
        send = JS[JS.index('$("composer").addEventListener("submit"'):]
        send = send[:send.index("\n});")]
        self.assertIn('$("text").value = "";\n  draftSave();', send)
        self.assertIn("if (caseId !== id) draftPut(id, t);", send)

    def test_open_state_survives_reload(self):
        restore = body_of("chatRestore")
        self.assertIn('try { saved = localStorage.getItem("fot.chat"); } catch (e) {}', restore)
        self.assertIn("setChat(true, true)", restore)
        # На реестре чат — последнего открытого плана.
        self.assertIn('try { localStorage.setItem("fot.plan", String(id)); } catch (e) {}',
                      body_of("planSwitch"))
        self.assertIn('last = +localStorage.getItem("fot.plan");', body_of("routeTo"))


class ColumnsButtonTests(unittest.TestCase):
    """«Графы» на «Договорах» плана — и при открытой панели договора
    (30.09.2026: кнопка пропадала, когда выбран договор)."""

    def test_columns_button_does_not_depend_on_panel(self):
        start = JS.index('  if (view === "ctr") {', JS.index("function renderView()"))
        branch = JS[start:JS.index("\n  if (view === ", start + 10)]
        bar = branch[branch.index("var bar = "):branch.index("var html = ")]
        self.assertIn('data-gcols="pctr"', bar)
        self.assertNotIn("open ?", bar)
        # Щелчок по кнопке открывает выбор граф и при открытой панели.
        self.assertIn('if (gcp) { openGridCols(gcp, gcp.getAttribute("data-gcols")); return; }', JS)


if __name__ == "__main__":
    unittest.main()
