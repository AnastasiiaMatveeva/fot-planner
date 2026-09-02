/* Лента плана: опрос состояния и отрисовка.
 *
 * Состояние целиком лежит на сервере, страница его только показывает. Поэтому
 * можно закрыть вкладку посреди разбора документа и вернуться через день —
 * увидишь, чем кончилось. Опрос раз в 1,5 секунды: живой картины работы
 * агентов достаточно, а сложности веб-сокетов для приложения в своей сети
 * не окупаются.
 */
(function () {
"use strict";

var $ = function (id) { return document.getElementById(id); };
var caseId = null, state = null, polling = null, lastSig = "";

function api(path, opts) {
  return fetch(path, opts).then(function (r) {
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  });
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
  });
}
function mo(v) {
  if (v == null) return "—";
  // Разряды разделяем неразрывным пробелом: с обычным «110 000» переносится
  // на две строки. Дробную часть у денег дописываем до копеек.
  var neg = v < 0;
  var n = Math.abs(Math.round(v * 100) / 100);
  var s = (n % 1 ? n.toFixed(2) : String(n)).replace(".", ",");
  var p = s.split(",");
  return (neg ? "−" : "") +
         p[0].replace(/\B(?=(\d{3})+(?!\d))/g, " ") + (p[1] ? "," + p[1] : "");
}
function px(n, a, b, c) {
  var d = n % 10, h = n % 100;
  if (h >= 11 && h <= 14) return c;
  if (d === 1) return a;
  if (d >= 2 && d <= 4) return b;
  return c;
}


/* ── подтверждение действия ───────────────────────────────────
 * Системный confirm() здесь не годится: встроенные браузеры и часть
 * корпоративных политик молча отклоняют такие диалоги, и удаление просто
 * не происходит — пользователь жмет крестик, а ничего не меняется.
 * Свое окно работает везде одинаково и говорит, что именно будет удалено.
 */
function ask(title, detail, okText) {
  return new Promise(function (resolve) {
    var back = document.createElement("div");
    back.className = "modal";
    back.innerHTML =
      '<div class="box" role="dialog" aria-modal="true">' +
        "<h4>" + esc(title) + "</h4>" +
        (detail ? "<p>" + esc(detail) + "</p>" : "") +
        '<div class="btns">' +
          '<button type="button" class="no">Отмена</button>' +
          '<button type="button" class="yes">' + esc(okText || "Убрать") + "</button>" +
        "</div>" +
      "</div>";
    document.body.appendChild(back);

    function close(answer) {
      document.removeEventListener("keydown", onKey);
      back.remove();
      resolve(answer);
    }
    function onKey(e) {
      if (e.key === "Escape") close(false);
      if (e.key === "Enter") close(true);
    }
    back.querySelector(".no").onclick = function () { close(false); };
    back.querySelector(".yes").onclick = function () { close(true); };
    back.onclick = function (e) { if (e.target === back) close(false); };
    document.addEventListener("keydown", onKey);
    back.querySelector(".yes").focus();
  });
}

/* ── планы ─────────────────────────────────────────────────── */
function loadCases() {
  return api("/api/cases").then(function (rows) {
    $("caselist").innerHTML = rows.length ? rows.map(function (c) {
      return '<div class="case' + (c.id === caseId ? " on" : "") + '" data-id="' + c.id + '">' +
             '<button class="del" data-del="' + c.id +
             '" title="Убрать план: ленту, работы агентов и расчеты">×</button>' +
             esc(c.title) + "<small>" + esc(c.stage) + " · документов " + c.documents +
             "</small></div>";
    }).join("") : '<div class="empty" style="padding:0 16px">Планов пока нет</div>';
    Array.prototype.forEach.call(document.querySelectorAll(".case"), function (el) {
      el.addEventListener("click", function (e) {
        if (e.target.closest(".del")) return;
        open(+el.getAttribute("data-id"));
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".case .del"), function (b) {
      b.addEventListener("click", function () { removeCase(+b.getAttribute("data-del")); });
    });
    return rows;
  });
}

function removeCase(id) {
  var el = document.querySelector('.case[data-id="' + id + '"]');
  var name = el ? el.querySelector("small").previousSibling.textContent.trim() : "план";
  ask("Убрать «" + name + "»?",
      "Лента, работы агентов и расчеты этого плана будут удалены. Документы, " +
      "договоры, штатное расписание и нормативы останутся — они общие для " +
      "организации.", "Убрать план").then(function (yes) {
    if (yes) doRemoveCase(id);
  });
}

function doRemoveCase(id) {
  api("/api/case/" + id, { method: "DELETE" }).then(function () {
    if (caseId === id) { caseId = null; state = null; }
    return loadCases();
  }).then(function (rows) {
    if (!caseId && rows.length) open(rows[0].id);
    else if (!rows.length) newCase();
  });
}

function newCase() {
  return api("/api/cases", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ year: 2026 })
  }).then(function (r) { return loadCases().then(function () { open(r.id); }); });
}

function open(id) {
  leaveRegistry();
  caseId = id;
  lastSig = "";
  vdata = null; vresult = null; vrun = null;
  setView("feed");
  loadCases();
  tick();
}

/* ── отрисовка ────────────────────────────────────────────── */
function title(key) {
  if (key === "solver") return "Оптимизатор";
  var a = (state.agents || []).filter(function (x) { return x.key === key; })[0];
  return a ? "Агент " + a.n : (key || "сервис");
}
function agentNum(key) {
  var a = (state.agents || []).filter(function (x) { return x.key === key; })[0];
  return a ? a.n : "";
}
function agentName(key) {
  var a = (state.agents || []).filter(function (x) { return x.key === key; })[0];
  return a ? a.name : key;
}

function renderFeed() {
  var feed = $("feed");
  var atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
  feed.innerHTML = state.messages.map(function (m) {
    if (m.who === "передача") {
      return '<div class="msg pass"><div class="bubble">' +
             esc(title(m.agent)) + " → " + esc(title(m.to)) + ": " + esc(m.text) +
             "</div></div>";
    }
    var cls = m.who === "экономист" ? "msg me" : (m.who === "система" ? "msg sys" : "msg");
    var who = m.who === "экономист" ? "Вы"
            : (m.agent === "solver" ? "Оптимизатор"
            : (m.agent ? "Агент " + agentNum(m.agent) + " · " + esc(agentName(m.agent)) : "Сервис"));
    return '<div class="' + cls + '">' +
             '<div class="head"><b>' + who + "</b><span>" + esc(m.created) + "</span></div>" +
             '<div class="bubble">' + esc(m.text) + attach(m.payload) + "</div>" +
           "</div>";
  }).join("");
  if (atBottom) feed.scrollTop = feed.scrollHeight;
}

function attach(p) {
  if (!p) return "";
  if (p.kind === "reference_diff") return diffCard(p);
  if (p.kind === "passport") return passportCard(p);
  if (p.kind === "run") return runCard(p);
  if (p.kind === "offer_solve")
    return '<div class="card"><div class="foot">' +
           '<button class="primary" style="width:auto" data-act="solve">Рассчитать план</button>' +
           "</div></div>";
  return "";
}

function passportCard(p) {
  if (!p.log || !p.log.length) return "";
  return '<div class="card"><div class="ch">что прочитано в документе</div>' +
         '<div class="log">' + esc(p.log.slice(0, 12).join("\n")) + "</div></div>";
}

function diffCard(p) {
  var head = "расхождения со справочником";
  if (p.by) head += " · " + esc(p.by);
  if (p.basis) head += " · " + esc(p.basis);
  if (p.effective_from) head += " · с " + esc(p.effective_from);
  var rows = p.changes.map(function (c) {
    var d = c.new - (c.old || 0);
    return "<tr><td>" + esc(c.pos) + "</td><td>" + esc(c.field) + "</td>" +
           '<td class="n">' + mo(c.old) + '</td><td class="n">' + mo(c.new) + "</td>" +
           '<td class="n up">' + (c.old ? (d > 0 ? "+" : "") + mo(d) : "новое") + "</td></tr>";
  }).join("");
  return '<div class="card"><div class="ch">' + head + "</div>" +
         "<table><thead><tr><th>должность</th><th>величина</th><th>сейчас</th>" +
         "<th>по документу</th><th>изменение</th></tr></thead><tbody>" + rows + "</tbody></table>" +
         '<div class="foot"><button class="primary" style="width:auto" data-act="applyref">' +
         "Записать в справочник</button>" +
         (p.unknown && p.unknown.length
           ? "<span class=\"empty\">вне справочника: " + esc(p.unknown.join(", ")) + "</span>" : "") +
         "</div></div>";
}

function runCard(p) {
  return '<div class="card"><div class="foot">' +
         '<a href="/api/case/' + caseId + "/result/" + p.run_id + '">Скачать план, xlsx</a>' +
         "</div></div>";
}

function renderAgents() {
  var last = {};
  state.activities.forEach(function (a) { last[a.agent] = a; });
  $("agents").innerHTML = state.agents.map(function (a) {
    var act = last[a.key];
    var cls = "ag", sub = a.does;
    if (!a.real) { cls += " mock"; sub = "в этой сборке не реализован"; }
    if (act) {
      if (act.state === "идет") { cls = "ag work"; sub = act.title; }
      else if (act.state === "ошибка") { cls = "ag err"; sub = act.detail || "ошибка"; }
      else if (a.real) {
        cls = "ag done";
        sub = (act.detail || act.title) + (act.seconds != null ? " · " + act.seconds + " с" : "");
      }
    }
    var has = state.activities.some(function (x) { return x.agent === a.key; });
    return '<div class="' + cls + (has ? " has" : "") +
           (openAgent === a.key ? " open" : "") + '" data-agent="' + a.key + '">' +
           '<span class="dot"></span><span class="n">' + a.n + "</span>" +
           '<span class="b"><span class="t">' + esc(a.name) + "</span><br>" +
           '<span class="s">' + esc(sub) + "</span></span></div>" +
           (openAgent === a.key ? '<div class="works">' + agentCard(a.key) + "</div>" : "");
  }).join("");

  // Оптимизатор в панель попадает, но отдельной строкой: он не агент.
  var s = state.solver || { name: "Оптимизатор", does: "ищет план: MIP, решатель HiGHS" };
  var sa = last.solver, scls = "ag", ssub = s.does;
  if (sa) {
    if (sa.state === "идет") { scls = "ag work"; ssub = sa.title; }
    else if (sa.state === "ошибка") { scls = "ag err"; ssub = sa.detail || "ошибка"; }
    else { scls = "ag done"; ssub = (sa.detail || s.does) + (sa.seconds != null ? " · " + sa.seconds + " с" : ""); }
  }
  var shas = state.activities.some(function (x) { return x.agent === "solver"; });
  $("agents").innerHTML += '<div class="' + scls + (shas ? " has" : "") +
    (openAgent === "solver" ? " open" : "") + '" data-agent="solver">' +
    '<span class="dot"></span><span class="n">—</span>' +
    '<span class="b"><span class="t">' + esc(s.name) + "</span><br>" +
    '<span class="s">' + esc(ssub) + "</span></span></div>" +
    (openAgent === "solver" ? '<div class="works">' + agentCard("solver") + "</div>" : "");

}

var openDocs = {};

function docCard(d) {
  var cls = d.state === "разобран" ? "ok" : (d.state === "ожидает" ? "wait" : "bad");
  return '<div class="doc ' + cls + '"><div class="nm">' + esc(d.name) +
         '<button class="del" data-doc="' + d.id +
         '" title="Убрать документ и все, что из него извлечено">×</button></div>' +
         '<div class="mt">' + esc(d.kind || d.state) +
         (d.summary ? " · " + esc(d.summary) : "") +
         (d.by ? " · " + esc(d.by) : "") + "</div></div>";
}

function renderDocs() {
  var el = $("docs");
  if (!state.documents.length) {
    el.innerHTML = '<div class="empty">Документов пока нет</div>';
    return;
  }
  // Документы разложены по видам: в общей куче двадцать договоров вперемешку
  // с приказами не читаются. Разобранные группы свернуты — они в порядке,
  // а требующие внимания раскрыты.
  var groups = [
    { key: "договоры", title: "По договорам",
      is: function (d) { return d.kind === "документ по договору" ||
                                d.kind === "штатное расписание"; } },
    { key: "нормативы", title: "Нормативная база",
      is: function (d) { return d.kind === "нормативный документ" ||
                                d.kind === "правила замещения должностей"; } },
    { key: "внимание", title: "Требуют внимания", open: true,
      is: function (d) { return d.state !== "разобран"; } },
  ];
  var taken = {};
  var html = "";
  groups.forEach(function (g) {
    var list = state.documents.filter(function (d) {
      if (taken[d.id]) return false;
      var hit = g.key === "внимание" ? g.is(d) : (d.state === "разобран" && g.is(d));
      if (hit) taken[d.id] = 1;
      return hit;
    });
    if (!list.length) return;
    var open = g.open || openDocs[g.key];
    html += '<div class="dgrp"><button type="button" class="dgh' + (open ? " on" : "") +
            '" data-grp="' + g.key + '"><span class="t">' + esc(g.title) + "</span>" +
            '<span class="c">' + list.length + "</span></button>" +
            (open ? list.map(docCard).join("") : "") + "</div>";
  });
  var rest = state.documents.filter(function (d) { return !taken[d.id]; });
  if (rest.length) {
    var open = openDocs["прочее"];
    html += '<div class="dgrp"><button type="button" class="dgh' + (open ? " on" : "") +
            '" data-grp="прочее"><span class="t">Прочее</span>' +
            '<span class="c">' + rest.length + "</span></button>" +
            (open ? rest.map(docCard).join("") : "") + "</div>";
  }
  el.innerHTML = html;
}

function renderRuns() {
  var el = $("runs");
  if (!state.runs.length) { el.innerHTML = '<div class="empty">Расчет еще не выполнен</div>'; return; }
  el.innerHTML = state.runs.map(function (r) {
    var cls = r.status === "OPTIMAL" ? "" : (r.status === "идет" ? "work" : "bad");
    var s = r.summary || {};
    return '<div class="run ' + cls + '"><b>' + esc(r.status) + "</b>" +
           (r.seconds != null ? " · " + r.seconds + " с" : "") +
           '<div class="mt">' + esc(r.created) +
           (s.plan_rows != null ? " · строк плана " + s.plan_rows : "") +
           (s.error ? " · " + esc(String(s.error).slice(0, 120)) : "") + "</div>" +
           (r.status === "OPTIMAL"
             ? '<div class="mt"><a href="/api/case/' + caseId + "/result/" + r.id +
               '">скачать xlsx</a></div>' : "") +
           "</div>";
  }).join("");
}

function renderAsk() {
  var q = state.questions.filter(function (x) { return !x.answer; })[0];
  var box = $("ask");
  if (!q) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  box.innerHTML = '<div class="q"><b>Агент ' + agentNum(q.agent) + " · " +
                  esc(agentName(q.agent)) + " спрашивает:</b> " +
                  esc(q.text) + "</div>" +
                  (q.options && q.options.length
                    ? '<div class="opts">' + q.options.map(function (o) {
                        return '<button type="button" data-q="' + q.id + '" data-opt="' +
                               esc(o) + '">' + esc(o) + "</button>";
                      }).join("") + "</div>"
                    : '<div class="empty">Ответьте в строке ниже</div>');
}

function renderStage() {
  var c = state.case;
  var working = state.activities.filter(function (a) { return a.state === "идет"; });

  $("ptitle").textContent = c.title;

  // В строке под названием — состояние плана и то, чем сейчас занят сервис.
  var pill = working.length
    ? '<span class="pill work">агент работает: ' + esc(working[0].title) + "</span>"
    : '<span class="pill' + (c.stage === "посчитано" ? " ok" : "") + '">' +
      esc(c.stage) + "</span>";
  var why = !c.has_data ? "для расчета нужны документы по договорам" : "";
  $("pmeta").innerHTML = pill +
    "<span>обновлено " + esc(c.updated) + "</span>" +
    (why ? "<span>· " + esc(why) + "</span>" : "");

  $("solve").disabled = !c.has_data || working.length > 0;
}

/* Секции не должны гасить друг друга: сбой в одной — не повод оставить
   весь экран пустым. */
function safely(name, fn) {
  try { fn(); } catch (e) { console.error("не отрисовалось: " + name, e); }
}

function renderTabs() {
  var c = (state.case && state.case.counts) || {};
  var n = {};
  Array.prototype.forEach.call(document.querySelectorAll(".tabs .tab"), function (b) {
    var k = b.getAttribute("data-view");
    var base = (b.getAttribute("data-label")
             || (b.setAttribute("data-label", b.textContent), b.textContent));
    b.innerHTML = esc(base) + (n[k] ? '<span class="c">' + n[k] + "</span>" : "");
  });
}

function renderMode() {
  var m = state.model;
  $("mode").textContent = m.provider
    ? "модель " + m.name + ", " + m.label
    : "без модели: разбор по заголовкам";
}

function render() {
  if (!state) return;
  safely("режим", renderMode);
  safely("вкладки", renderTabs);
  safely("заголовок плана", renderStage);
  if (view === "feed") safely("лента", renderFeed);
  safely("агенты", renderAgents);
  safely("документы", renderDocs);
  safely("расчеты", renderRuns);
  safely("вопрос", renderAsk);
  wire();
}

/* Кнопки внутри лениво перерисовываемой ленты — вешаем после отрисовки. */
function wire() {
  Array.prototype.forEach.call(document.querySelectorAll("[data-act=solve]"), function (b) {
    b.onclick = solve;
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-act=applyref]"), function (b) {
    b.onclick = function () { applyRef(b); };
  });
  Array.prototype.forEach.call(document.querySelectorAll(".opts button"), function (b) {
    b.onclick = function () {
      api("/api/case/" + caseId + "/answer/" + b.getAttribute("data-q"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answer: b.getAttribute("data-opt") })
      }).then(tick);
    };
  });
}

function applyRef(btn) {
  var card = btn.closest(".card");
  var rows = Array.prototype.map.call(card.querySelectorAll("tbody tr"), function (tr) {
    var td = tr.querySelectorAll("td");
    return { pos: td[0].textContent, field: td[1].textContent,
             value: parseFloat(td[3].textContent.replace(/[  ]/g, "").replace(",", ".")) };
  });
  btn.disabled = true; btn.textContent = "Записываю…";
  api("/api/reference", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ edits: rows, case_id: caseId })
  }).then(tick);
}

function solve() {
  $("solve").disabled = true;
  api("/api/case/" + caseId + "/solve", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: "{}"
  }).then(tick);
}



/* ── артефакты работы агентов ─────────────────────────────────
 * У каждой работы агента есть след: что подано на вход, чем разобрано, что
 * получилось и что отброшено. Панель показывает итог одной строкой, а по
 * клику раскрывается сам артефакт — для ГОЗ важно уметь показать не только
 * «сделано», но и на чем именно.
 */
var openAgent = null;

function artifactRows(a) {
  return Object.keys(a).map(function (k) {
    var v = a[k];
    if (v === null || v === undefined || v === "") v = "—";
    else if (typeof v === "boolean") v = v ? "да" : "нет";
    else if (Array.isArray(v)) v = v.length ? v.join("\n") : "—";
    return '<div class="ar"><span class="ak">' + esc(k) + "</span>" +
           '<span class="av">' + esc(String(v)) + "</span></div>";
  }).join("");
}

function agentCard(key) {
  var acts = state.activities.filter(function (a) { return a.agent === key; });
  if (!acts.length) return '<div class="none">Агент еще не работал</div>';
  return acts.slice().reverse().map(function (a) {
    return '<div class="work"><div class="wh">' + esc(a.title) +
           '<span>' + esc(a.state) + (a.seconds != null ? " · " + a.seconds + " с" : "") +
           " · " + esc(a.started) + "</span></div>" +
           (a.artifact ? artifactRows(a.artifact)
                       : '<div class="ar"><span class="ak">итог</span><span class="av">' +
                         esc(a.detail || "—") + "</span></div>") +
           "</div>";
  }).join("");
}

/* ── просмотр данных ──────────────────────────────────────────
 * Лента — основной режим работы, но собранное должно открываться и читаться:
 * штатное расписание, договоры, справочник, план, освоение, ограничения.
 * Входные данные приходят из базы, результат — из файла, который сохранил
 * решатель: для ГОЗ важно показывать ровно то, на чем построен план.
 */
var view = "feed", vdata = null, vresult = null, vrun = null;

var MONTHS = ["янв", "фев", "мар", "апр", "май", "июн",
              "июл", "авг", "сен", "окт", "ноя", "дек"];
var KINDS = { oklad: "оклад", okl: "оклад", prk: "приказ",
              k120: "120", k122: "122", k124: "124", k152: "152" };
function monthName(m) { return MONTHS[m] || MONTHS[m - 1] || m; }

function setView(v) {
  view = v;
  Array.prototype.forEach.call(document.querySelectorAll(".tabs .tab"), function (b) {
    b.classList.toggle("on", b.getAttribute("data-view") === v);
  });
  $("feed").hidden = v !== "feed";
  $("view").hidden = v === "feed";
  if (v === "feed") return;
  $("view").innerHTML = '<div class="none">Загружаю…</div>';
  var need = (v === "emp" || v === "ctr" || v === "ref" || v === "sub")
    ? api("/api/case/" + caseId + "/data").then(function (d) { vdata = d; })
    : loadResult();
  need.then(renderView).catch(function (e) {
    $("view").innerHTML = '<div class="none">' + esc(e.message || "не загрузилось") + "</div>";
  });
}

function loadResult() {
  var ok = (state.runs || []).filter(function (r) { return r.status === "OPTIMAL"; })[0];
  if (!ok) { vresult = null; vrun = null; return Promise.resolve(); }
  if (vrun === ok.id) return Promise.resolve();
  return api("/api/case/" + caseId + "/run/" + ok.id + "/result").then(function (d) {
    vresult = d; vrun = ok.id;
  });
}

/* Перечни всегда нумеруются: по номеру строки удобно сослаться в разговоре
   и найти место в длинной таблице. Колонку добавляем здесь, а не в каждом
   вызове, — чтобы нумерация была одинаковой везде. Отключается opts.plain. */
function table(head, rows, note, opts) {
  var num = !(opts && opts.plain);
  var h = note ? '<div class="vh">' + note + "</div>" : "";
  if (!rows.length) return h + '<div class="none">Пусто</div>';
  h += '<table><thead><tr>' + (num ? '<th class="num">№</th>' : "") +
       head.map(function (c) {
         return (typeof c === "object" ? '<th class="n">' + esc(c.t) : "<th>" + esc(c)) + "</th>";
       }).join("") + "</tr></thead><tbody>";
  h += rows.map(function (r, i) {
    return "<tr>" + (num ? '<td class="num">' + (i + 1) + "</td>" : "") +
      r.map(function (c) {
        return (c && typeof c === "object")
          ? '<td class="' + (c.cls || "") + '">' + (c.v == null ? "—" : c.v) + "</td>"
          : "<td>" + (c == null || c === "" ? "—" : esc(c)) + "</td>";
      }).join("") + "</tr>";
  }).join("");
  return h + "</tbody></table>";
}

function renderView() {
  var el = $("view");

  if (view === "emp") {
    var e = (vdata && vdata.employees) || [];
    el.innerHTML = table(
      ["табельный", "ФИО", "должность", { t: "ставка" }, { t: "оклад, ₽" },
       "с", "по", "источник"],
      e.map(function (x) {
        return [x.code, x.fio, x.position,
                { v: x.rate == null ? null : String(x.rate).replace(".", ","), cls: "n" },
                { v: x.salary == null ? null : mo(x.salary), cls: "n" },
                x.from, x.to, x.source];
      }),
      "Штатное расписание дела: <b>" + e.length + "</b> " +
      px(e.length, "сотрудник", "сотрудника", "сотрудников") +
      ". Собрано агентом из загруженных документов.");
    return;
  }

  if (view === "ctr") {
    var c = (vdata && vdata.contracts) || [];
    el.innerHTML = table(
      ["шифр", "наименование", "номер", "вид", "ГОЗ", { t: "фонд, ₽" },
       "разрешенные выплаты", "источник"],
      c.map(function (x) {
        return [x.code, x.name, x.number, x.kind,
                { v: x.goz ? '<span class="tag goz">' + esc(x.goz) + "</span>" : null },
                { v: x.fund == null ? null : mo(x.fund), cls: "n" },
                x.kinds, x.source];
      }),
      "Договоры дела: <b>" + c.length + "</b>.");
    return;
  }

  if (view === "ref") {
    var r = (vdata && vdata.reference) || [];
    el.innerHTML = table(
      ["должность", "категория", { t: "оклад за 1,0 ставки, ₽" }, { t: "П2556, ₽" },
       { t: "П4, ₽" }, "примечание"],
      r.map(function (x) {
        return [x.pos, x.cat,
                { v: x.sal == null ? null : mo(x.sal), cls: "n" },
                { v: x.p2556 == null ? null : mo(x.p2556), cls: "n" },
                { v: x.p4 == null ? null : mo(x.p4), cls: "n" },
                x.note];
      }),
      "<b>Нормативная база организации</b>, общая для всех планов. " +
      "Справочник должностей: <b>" + r.length + "</b> " +
      px(r.length, "позиция", "позиции", "позиций") +
      ". Прочерк — отдельной строки для должности в источнике нет. " +
      "Обновляется, когда выходит новая редакция приказа или положения.");
    return;
  }

  if (view === "sub") {
    var s = (vdata && vdata.substitutions) || [];
    el.innerHTML = table(
      ["должность", "может быть замещена"],
      s.map(function (x) { return [x.position, x.replaced_by]; }),
      "<b>Нормативная база организации</b>, общая для всех планов. " +
      "Правил замещения: <b>" + s.length + "</b>. Правила направленные — " +
      "кого кем можно заменить, не наоборот. В расчет пока не подставляются: " +
      "модель работает симметричными группами взаимозаменяемости.");
    return;
  }

  if (!vresult) {
    el.innerHTML = '<div class="none">Расчет еще не выполнен</div>';
    return;
  }

  if (view === "plan") {
    var byEmp = {}, names = {};
    vresult.plan.forEach(function (p) { (byEmp[p.emp] = byEmp[p.emp] || []).push(p); });
    vresult.employees.forEach(function (x) { names[x.code] = x.fio || x.code; });
    var total = vresult.plan.reduce(function (a, p) { return a + p.sum; }, 0);
    el.innerHTML = '<div class="vh">План выплат, прогон № ' + vrun + ": <b>" +
      vresult.plan.length + "</b> " +
      px(vresult.plan.length, "строка", "строки", "строк") +
      ", всего <b>" + mo(total) + " ₽</b>.</div>" +
      Object.keys(byEmp).map(function (code) {
        var rows = byEmp[code].slice().sort(function (a, b) { return a.m - b.m; });
        var sum = rows.reduce(function (a, p) { return a + p.sum; }, 0);
        return '<div class="grp"><h4>' + esc(names[code] || code) + " · " + mo(sum) + " ₽</h4>" +
               table(["месяц", "договор", "вид", { t: "сумма, ₽" }],
                 rows.map(function (p) {
                   return [monthName(p.m), p.ctr, KINDS[p.kind] || p.kind,
                           { v: mo(p.sum), cls: "n" }];
                 })) + "</div>";
      }).join("");
    return;
  }

  if (view === "cash") {
    var codes = Object.keys(vresult.cash || {});
    el.innerHTML = '<div class="vh">Освоение по договорам: поступления, выплаты ' +
      "и остаток кассы по месяцам. Остаток не может уходить в минус и переносится " +
      "только вперед.</div>" +
      codes.map(function (code) {
        var c = vresult.cash[code] || {};
        var keys = Object.keys(c);                 // «Поступление», «Выплаты», «Остаток»
        var len = keys.length ? (c[keys[0]] || []).length : 0;
        var rows = [];
        for (var i = 0; i < len; i++) {
          var row = [monthName(i)];
          keys.forEach(function (k) { row.push({ v: mo((c[k] || [])[i] || 0), cls: "n" }); });
          rows.push(row);
        }
        return '<div class="grp"><h4>' + esc(code) + "</h4>" +
               table(["месяц"].concat(keys.map(function (k) { return { t: k + ", ₽" }; })),
                     rows) + "</div>";
      }).join("");
    return;
  }

  if (view === "lim") {
    var lim = vresult.limits || [], warn = vresult.warnings || [], sum = vresult.summary || [];
    el.innerHTML =
      '<div class="grp"><h4>Итоги расчета</h4>' +
      table(["показатель", "значение"],
        sum.map(function (s) {
          if (!Array.isArray(s)) return [String(s), ""];
          var v = s.slice(1).filter(function (x) { return x != null && x !== ""; });
          // Крупные суммы читаются только с разделителями разрядов.
          return [String(s[0]), v.map(function (x) {
            return (typeof x === "number" && Math.abs(x) >= 10000) ? mo(x) : String(x);
          }).join(" · ")];
        })) + "</div>" +
      '<div class="grp"><h4>Связывающие ограничения</h4>' +
      (lim.length
        ? table(["ограничение", "где", "значение"],
            lim.map(function (l) {
              return Array.isArray(l) ? l.map(String) : [String(l), "", ""];
            }))
        : '<div class="none">Связывающих ограничений нет</div>') +
      "</div>" +
      '<div class="grp"><h4>Предупреждения</h4>' +
      (warn.length
        ? table(["сообщение"], warn.map(function (w) {
            return [Array.isArray(w) ? w.join(" · ") : String(w)];
          }))
        : '<div class="none">Предупреждений нет</div>') + "</div>";
  }
}


/* ── реестр организации ───────────────────────────────────────
 * Документы, договоры, штатное расписание и нормативы служат всем планам
 * сразу: договор заключается на несколько лет, штатка меняется приказами,
 * нормативы — раз в год. Держать их внутри плана значит загружать два
 * десятка документов каждый январь заново, поэтому у них свой раздел.
 *
 * План на год остается тем, чем и должен быть: лента работы, расчет и его
 * результат.
 */
var inRegistry = false, regTab = "docs", regData = null;

var REG_TABS = [
  { key: "docs", title: "Документы" },
  { key: "ctr", title: "Договоры" },
  { key: "emp", title: "Штатное расписание" },
  { key: "ref", title: "Справочник должностей" },
  { key: "sub", title: "Правила замещения" },
];

function openRegistry() {
  inRegistry = true;
  setView("feed");
  $("feed").hidden = true;
  $("view").hidden = true;
  $("regview").hidden = false;
  document.querySelector(".tabs").hidden = true;
  document.querySelector(".phead").hidden = true;
  $("openreg").classList.add("on");
  Array.prototype.forEach.call(document.querySelectorAll(".case"), function (el) {
    el.classList.remove("on");
  });
  loadRegistry();
}

function leaveRegistry() {
  if (!inRegistry) return;
  inRegistry = false;
  $("regview").hidden = true;
  document.querySelector(".tabs").hidden = false;
  document.querySelector(".phead").hidden = false;
  $("openreg").classList.remove("on");
}

function loadRegistry() {
  $("regview").innerHTML = '<div class="none">Загружаю…</div>';
  Promise.all([api("/api/documents"), api("/api/case/" + (caseId || 0) + "/data")
                 .catch(function () { return {}; })])
    .then(function (r) { regData = { documents: r[0], data: r[1] }; renderRegistry(); })
    .catch(function (e) {
      $("regview").innerHTML = '<div class="none">' + esc(e.message || "не загрузилось") + "</div>";
    });
}

function renderRegistry() {
  if (!regData) return;
  var d = regData.data || {}, docs = regData.documents || [];
  var body = "";

  if (regTab === "docs") {
    body = table(["документ", "вид", "состояние", "чем разобрано", "что извлек", "загружен", ""],
        docs.map(function (x) {
          var made = Object.keys(x.produced || {})
            .filter(function (k) { return x.produced[k]; })
            .map(function (k) { return k + " " + x.produced[k]; }).join(", ");
          return [x.name, x.kind, x.state, x.by, made || x.summary, x.uploaded,
                  { v: '<button class="del" data-doc="' + x.id +
                       '" title="Убрать документ и его данные">×</button>' }];
        }));
  } else if (regTab === "ctr") {
    var c = d.contracts || [];
    body = table(["шифр", "наименование", "номер", "вид", "ГОЗ", { t: "фонд, ₽" },
                  "с", "по", "разрешенные выплаты", "источник"],
      c.map(function (x) {
        return [x.code, x.name, x.number, x.kind,
                { v: x.goz ? '<span class="tag goz">' + esc(x.goz) + "</span>" : null },
                { v: x.fund == null ? null : mo(x.fund), cls: "n" },
                x.from, x.to, x.kinds, x.source];
      }),
      "");
  } else if (regTab === "emp") {
    var e = d.employees || [];
    body = table(["табельный", "ФИО", "должность", { t: "ставка" }, { t: "оклад, ₽" },
                  "с", "по", "источник"],
      e.map(function (x) {
        return [x.code, x.fio, x.position,
                { v: x.rate == null ? null : String(x.rate).replace(".", ","), cls: "n" },
                { v: x.salary == null ? null : mo(x.salary), cls: "n" },
                x.from, x.to, x.source];
      }),
      "");
  } else if (regTab === "ref") {
    var r = d.reference || [];
    body = table(["должность", "категория", { t: "оклад за 1,0 ставки, ₽" },
                  { t: "П2556, ₽" }, { t: "П4, ₽" }, "примечание"],
      r.map(function (x) {
        return [x.pos, x.cat,
                { v: x.sal == null ? null : mo(x.sal), cls: "n" },
                { v: x.p2556 == null ? null : mo(x.p2556), cls: "n" },
                { v: x.p4 == null ? null : mo(x.p4), cls: "n" }, x.note];
      }),
      "Прочерк — отдельной строки для должности в источнике нет.");
  } else if (regTab === "sub") {
    var s = d.substitutions || [];
    body = table(["должность", "может быть замещена"],
      s.map(function (x) { return [x.position, x.replaced_by]; }),
      "Правила направленные: кого кем можно заменить, не наоборот. " +
      "В расчет пока не подставляются.");
  }

  $("regview").innerHTML =
    '<div class="rtabs">' + REG_TABS.map(function (t) {
      var n = { docs: docs.length, ctr: (d.contracts || []).length,
                emp: (d.employees || []).length, ref: (d.reference || []).length,
                sub: (d.substitutions || []).length }[t.key];
      return '<button type="button" data-rtab="' + t.key + '"' +
             (regTab === t.key ? ' class="on"' : "") + ">" + esc(t.title) +
             (n ? '<span class="c"> ' + n + "</span>" : "") + "</button>";
    }).join("") + "</div>" + body;
}

/* ── опрос ────────────────────────────────────────────────── */
function tick() {
  if (!caseId) return Promise.resolve();
  return api("/api/case/" + caseId).then(function (s) {
    state = s;
    // Перерисовываем только когда что-то изменилось: иначе прыгает прокрутка
    // и слетает набранный текст.
    var sig = JSON.stringify([s.case.stage, s.case.updated, s.messages.length,
                              s.documents.map(function (d) { return d.state; }),
                              s.activities.map(function (a) { return [a.id, a.state]; }),
                              s.questions.map(function (q) { return [q.id, !!q.answer]; }),
                              s.runs.map(function (r) { return [r.id, r.status]; })]);
    if (sig !== lastSig) {
      lastSig = sig;
      render();
      // Перерисовка ленты не должна выбрасывать из открытого раздела.
      if (inRegistry) {
        $("feed").hidden = true; $("view").hidden = true; $("regview").hidden = false;
        document.querySelector(".tabs").hidden = true;
        document.querySelector(".phead").hidden = true;
      } else if (view !== "feed") {
        $("feed").hidden = true; $("view").hidden = false;
      }
    }
  }).catch(function () {});
}

/* ── события ──────────────────────────────────────────────── */
$("toggleside").addEventListener("click", function () {
  var shell = document.querySelector(".shell");
  var off = shell.classList.toggle("noside");
  this.classList.toggle("off", off);
  this.title = off ? "Показать колонку контекста" : "Скрыть колонку контекста";
  this.setAttribute("aria-label", this.title);
});

$("openagents").addEventListener("click", function () {
  // Агенты — контекст плана, а не отдельный экран: открываем колонку и
  // подсвечиваем ее, а не уводим пользователя со страницы.
  var shell = document.querySelector(".shell");
  shell.classList.remove("noside");
  $("toggleside").classList.remove("off");
  var el = document.querySelector(".side section");
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
});

$("openreg").addEventListener("click", openRegistry);
if ($("toreg")) $("toreg").addEventListener("click", openRegistry);

$("regview").addEventListener("click", function (e) {
  var t = e.target.closest("[data-rtab]");
  if (t) { regTab = t.getAttribute("data-rtab"); renderRegistry(); return; }
  var b = e.target.closest(".del");
  if (!b) return;
  var row = b.closest("tr");
  var name = row ? row.children[1].textContent : "документ";
  ask("Убрать «" + name + "»?",
      "Вместе с документом уйдут данные, извлеченные из него: сотрудники, " +
      "договоры и правила замещения.", "Убрать документ").then(function (yes) {
    if (!yes) return;
    b.disabled = true;
    api("/api/document/" + b.getAttribute("data-doc"), { method: "DELETE" })
      .then(loadRegistry).then(tick);
  });
});

$("docs").addEventListener("click", function (e) {
  var g = e.target.closest(".dgh");
  if (g) {
    var k = g.getAttribute("data-grp");
    openDocs[k] = !openDocs[k];
    render();
    return;
  }
  var b = e.target.closest(".del");
  if (!b) return;
  var card = b.closest(".doc"), name = card.querySelector(".nm").textContent.replace(/×$/, "");
  ask("Убрать «" + name.trim() + "»?",
      "Вместе с документом уйдут данные, извлеченные из него: сотрудники, " +
      "договоры и правила замещения.", "Убрать документ").then(function (yes) {
    if (!yes) return;
    b.disabled = true;
    api("/api/document/" + b.getAttribute("data-doc"), { method: "DELETE" }).then(tick);
  });
});

$("agents").addEventListener("click", function (e) {
  var el = e.target.closest(".ag");
  if (!el) return;
  var key = el.getAttribute("data-agent");
  openAgent = openAgent === key ? null : key;
  render();
});

$("tabs").addEventListener("click", function (e) {
  var b = e.target.closest(".tab");   // кнопка расчета не .tab и сюда не попадет
  if (b) setView(b.getAttribute("data-view"));
});

$("newcase").addEventListener("click", newCase);

$("files").addEventListener("change", function () {
  if (!this.files.length || !caseId) return;
  var fd = new FormData();
  Array.prototype.forEach.call(this.files, function (f) { fd.append("files", f); });
  this.value = "";
  api("/api/case/" + caseId + "/upload", { method: "POST", body: fd }).then(tick);
});

$("composer").addEventListener("submit", function (e) {
  e.preventDefault();
  var t = $("text").value.trim();
  if (!t || !caseId) return;
  $("text").value = "";
  api("/api/case/" + caseId + "/message", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: t })
  }).then(tick);
});

$("solve").addEventListener("click", solve);

/* старт: открываем последнее дело или заводим первое */
loadCases().then(function (rows) {
  if (rows.length) open(rows[0].id);
  else newCase();
  polling = setInterval(tick, 1500);
});

})();
