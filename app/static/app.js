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
          '<button type="button" class="yes">' + esc(okText || "Удалить") + "</button>" +
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

/* На строке списка кнопку удаления не ставят напрямую: там принято меню
   «…», которое появляется при наведении и собирает действия над записью.
   Кнопка-корзина остается для строк таблицы, где действие одно и колонка
   под него отведена. */
/* Два действия — две кнопки: меню при таком наборе только добавляет щелчок.
   Появляются при наведении на строку. */
function iconBtn(cls, attr, id, title, path) {
  return '<button class="rowbtn ' + cls + '" ' + attr + '="' + id + '" title="' + esc(title) +
         '" aria-label="' + esc(title) + '">' +
         '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" ' +
         'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">' + path +
         "</svg></button>";
}

/* Две стрелки в углы — «развернуть»; в свернутом виде та же иконка
   разворачивается обратно поворотом на 180°, отдельного значка не нужно. */
var ICON_WIDE =
  '<svg viewBox="0 0 16 16" width="15" height="15" fill="none" ' +
  'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" ' +
  'stroke-linejoin="round"><path d="M9.5 2.5h4v4M13.5 2.5 9 7"/>' +
  '<path d="M6.5 13.5h-4v-4M2.5 13.5 7 9"/></svg>';

var ICON_EDIT = '<path d="M11.3 2.6a1.4 1.4 0 0 1 2 2l-7.2 7.2-2.7.7.7-2.7z"/>' +
                '<path d="M10.2 3.7l2.1 2.1"/>';
var ICON_TRASH = '<path d="M2.8 4.3h10.4M6.4 4.3V3.1c0-.4.3-.7.7-.7h1.8c.4 0 .7.3.7.7v1.2"/>' +
                 '<path d="M4.2 4.3l.6 8.2c0 .6.5 1 1 1h4.4c.6 0 1-.4 1-1l.6-8.2"/>' +
                 '<path d="M6.7 6.8v4.2M9.3 6.8v4.2"/>';

function rowMenu(attr, id) {
  return '<button class="more" ' + attr + '="' + id + '" title="Действия" ' +
         'aria-label="Действия">' +
         '<svg viewBox="0 0 16 16" width="15" height="15" fill="currentColor">' +
         '<circle cx="8" cy="3.4" r="1.3"/><circle cx="8" cy="8" r="1.3"/>' +
         '<circle cx="8" cy="12.6" r="1.3"/></svg></button>';
}

/* Всплывающее меню действий над записью. */
function showMenu(anchor, items) {
  var open = document.querySelector(".menu");
  if (open) open.remove();
  var m = document.createElement("div");
  m.className = "menu";
  m.innerHTML = items.map(function (it, i) {
    return '<button type="button" class="mi' + (it.danger ? " danger" : "") +
           '" data-i="' + i + '">' + esc(it.label) + "</button>";
  }).join("");
  document.body.appendChild(m);

  var r = anchor.getBoundingClientRect();
  m.style.top = Math.min(r.bottom + 4, innerHeight - m.offsetHeight - 8) + "px";
  m.style.left = Math.min(r.left, innerWidth - m.offsetWidth - 8) + "px";

  function close() {
    m.remove();
    document.removeEventListener("mousedown", onDoc, true);
    document.removeEventListener("keydown", onKey, true);
  }
  function onDoc(e) { if (!m.contains(e.target)) close(); }
  function onKey(e) { if (e.key === "Escape") close(); }
  m.addEventListener("click", function (e) {
    var b = e.target.closest(".mi");
    if (!b) return;
    close();
    items[+b.getAttribute("data-i")].run();
  });
  setTimeout(function () {
    document.addEventListener("mousedown", onDoc, true);
    document.addEventListener("keydown", onKey, true);
  }, 0);
}

function delButton(attr, id, title) {
  return '<button class="del" ' + attr + '="' + id + '" title="' + esc(title) + '" ' +
         'aria-label="' + esc(title) + '">' +
         '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" ' +
         'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">' +
         '<path d="M2.8 4.3h10.4M6.4 4.3V3.1c0-.4.3-.7.7-.7h1.8c.4 0 .7.3.7.7v1.2"/>' +
         '<path d="M4.2 4.3l.6 8.2c0 .6.5 1 1 1h4.4c.6 0 1-.4 1-1l.6-8.2"/>' +
         '<path d="M6.7 6.8v4.2M9.3 6.8v4.2"/></svg></button>';
}

/* ── планы ─────────────────────────────────────────────────── */
function loadCases() {
  return api("/api/cases").then(function (rows) {
    // Плоский список, сверху последние измененные — так же, как в списках
    // переписок. Сортировку дает сервер: cases отдаются по updated.
    $("caselist").innerHTML = rows.length
      ? rows.map(function (c) {
          return '<div class="case' + (c.id === caseId ? " on" : "") +
                 '" data-id="' + c.id + '">' +
                 '<span class="acts">' +
                   iconBtn("edit", "data-rename", c.id, "Переименовать", ICON_EDIT) +
                   iconBtn("del", "data-del", c.id, "Удалить план", ICON_TRASH) +
                 "</span>" +
                 '<span class="cdot ' + stageClass(c.stage) +
                 '" title="' + esc(c.stage) + '"></span>' +
                 '<span class="ttl">' + esc(c.title) + "</span></div>";
        }).join("")
      : '<div class="empty" style="padding:0 16px">Планов пока нет</div>';
    Array.prototype.forEach.call(document.querySelectorAll(".case"), function (el) {
      el.addEventListener("click", function (e) {
        if (e.target.closest(".rowbtn") || e.target.closest("input")) return;
        open(+el.getAttribute("data-id"));
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".case .del"), function (b) {
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        removeCase(+b.getAttribute("data-del"));
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".case .edit"), function (b) {
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        startRename(b.closest(".case"));
      });
    });
    return rows;
  });
}

/* Переименование прямо в строке: отдельное окно ради одного поля — лишний шаг. */
function startRename(row) {
  var id = +row.getAttribute("data-id");
  var ttl = row.querySelector(".ttl");
  var was = ttl.textContent;
  var inp = document.createElement("input");
  inp.className = "rename";
  inp.value = was;
  ttl.replaceWith(inp);
  inp.focus();
  inp.select();

  var done = false;
  function finish(save) {
    if (done) return;
    done = true;
    var name = inp.value.trim();
    var span = document.createElement("span");
    span.className = "ttl";
    span.textContent = save && name ? name : was;
    inp.replaceWith(span);
    if (!save || !name || name === was) return;
    api("/api/case/" + id, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: name })
    }).then(function () { loadCases(); if (caseId === id) tick(); });
  }
  inp.addEventListener("keydown", function (e) {
    if (e.key === "Enter") finish(true);
    if (e.key === "Escape") finish(false);
  });
  inp.addEventListener("blur", function () { finish(true); });
  inp.addEventListener("click", function (e) { e.stopPropagation(); });
}

/* Состояние плана — точкой: слово «сбор данных» под каждым названием
   загромождало список, а цвет читается с одного взгляда. */
function stageClass(stage) {
  if (stage === "посчитано") return "ok";
  if (stage === "готово к расчету") return "ready";
  return "";
}

function removeCase(id) {
  var el = document.querySelector('.case[data-id="' + id + '"]');
  var t = el && el.querySelector(".ttl");
  var name = t ? t.textContent.trim() : "план";
  ask("Удалить «" + name + "»?",
      "Лента, работы агентов и расчеты этого плана будут удалены. Документы, " +
      "договоры, штатное расписание и нормативы останутся — они общие для " +
      "организации.", "Удалить план").then(function (yes) {
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
  leaveAgents();
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
         "<table><thead><tr><th>Должность</th><th>Величина</th><th>Сейчас</th>" +
         "<th>По документу</th><th>Изменение</th></tr></thead><tbody>" + rows + "</tbody></table>" +
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

/* Список агентов в боковой колонке: кто сейчас работает. Раньше функция
   называлась так же, как отрисовка «Хода работы», и вторая молча подменяла
   первую: колонка оставалась пустой, а каждая перерисовка ленты падала на
   незагруженных данных «Хода работы». */
function renderRoster() {
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
         rowMenu("data-docmenu", d.id) + "</div>" +
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
    var why = (s["анализ"] || []).slice(0, 2).map(function (t) {
      return '<div class="mt why">' + esc(t) + "</div>";
    }).join("");
    // Опыты с ослаблением: что пробовали снять и сошлось ли. Экономист видит
    // не только «нет решения», но и что это не лечится одним допущением.
    if ((s["опыты"] || []).length) {
      why += '<div class="mt why">опыты: ' + esc(s["опыты"].map(function (o) {
        return o[0] + " — " + o[1] + (o[2] ? " (" + o[2] + ")" : "");
      }).join("; ")) + "</div>";
    }
    return '<div class="run ' + cls + '"><b>' + esc(r.status) + "</b>" +
           (r.seconds != null ? " · " + r.seconds + " с" : "") +
           '<div class="mt">' + esc(r.created) +
           (s.plan_rows != null ? " · строк плана " + s.plan_rows : "") +
           (s.error && !why ? " · " + esc(String(s.error).slice(0, 120)) : "") + "</div>" +
           why +
           (r.status === "OPTIMAL"
             ? '<div class="mt"><a href="/api/case/' + caseId + "/result/" + r.id +
               '">скачать xlsx</a></div>' : "") +
           runSources(r.sources) +
           "</div>";
  }).join("");
}

/* Основание расчета: документы с версией и отпечатком, версии агентов.
   Свернуто в одну строку — раскрывается, когда нужно ответить «на чем
   считали». Тот же перечень лежит листом «источники» в файле результата. */
function runSources(s) {
  if (!s || !s["документы"] || !s["документы"].length) return "";
  var docs = s["документы"];
  var ag = s["агенты"] || {};
  return '<details class="srcs"><summary>' +
    "на основании " + docs.length + " " + px(docs.length, "документа", "документов", "документов") +
    "</summary><ul>" + docs.map(function (d) {
      return "<li>" + esc(d["имя"]) +
        (d["версия"] > 1 ? " · версия " + d["версия"] : "") +
        '<div class="mt">' + esc(Object.keys(d["строк"] || {}).map(function (k) {
          return k + " " + d["строк"][k];
        }).join(", ")) + "</div></li>";
    }).join("") + "</ul>" +
    '<div class="mt">агенты: ' + esc(Object.keys(ag).map(function (a) {
      return a + " " + ag[a];
    }).join(", ")) + "</div></details>";
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

function render() {
  if (!state) return;
  safely("вкладки", renderTabs);
  safely("заголовок плана", renderStage);
  if (view === "feed") safely("лента", renderFeed);
  safely("агенты", renderRoster);
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

/* Перед расчетом — намерение: что уйдет в решатель и что сервис примет за
   экономиста. Раньше кнопка запускала расчет молча, а о допущениях
   говорилось после — в ленте, когда решение уже искали на них. */
function solve() {
  $("solve").disabled = true;
  api("/api/case/" + caseId + "/preflight").then(function (pf) {
    var back = document.createElement("div");
    back.className = "modal";
    var rows = pf["строки"].map(function (kv) {
      return '<div class="pfrow"><span>' + esc(kv[0]) + "</span><b>" + kv[1] + "</b></div>";
    }).join("");
    var warn = (pf["допущения"] || []).map(function (w) {
      return '<div class="pfwarn">' + esc(w) + "</div>";
    }).join("");
    back.innerHTML =
      '<div class="box wide" role="dialog" aria-modal="true">' +
        "<h4>" + (pf["стоит"] ? "Считать не на чем" : "Что уйдет в расчет") + "</h4>" +
        '<div class="pfrows">' + rows + "</div>" +
        (warn ? '<div class="pfh">Допущения сервиса — проверьте</div>' + warn : "") +
        (pf["стоит"]
          ? '<p>Нет сотрудников или договоров. Загрузите документы в реестр.</p>' : "") +
        '<div class="btns">' +
          '<button type="button" class="no">Отмена</button>' +
          (pf["стоит"] ? "" :
           '<button type="button" class="yes">Запустить расчет</button>') +
        "</div></div>";
    document.body.appendChild(back);
    function close() { back.remove(); $("solve").disabled = false; }
    back.querySelector(".no").onclick = close;
    back.onclick = function (e) { if (e.target === back) close(); };
    var yes = back.querySelector(".yes");
    if (yes) yes.onclick = function () {
      back.remove();
      api("/api/case/" + caseId + "/solve", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}"
      }).then(tick);
    };
  }).catch(function () { $("solve").disabled = false; });
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
var view = "feed", vdata = null, vresult = null, vrun = null, vrules = null,
    vsum = null, vpay = null;
//: Сколько реплик пришло, пока лента не на виду.
var unseen = 0, lastMsgCount = null;

function markFeedTab() {
  var tab = document.querySelector('.tabs .tab[data-view="feed"]');
  if (!tab) return;
  tab.classList.toggle("hasnew", unseen > 0);
  tab.setAttribute("title", unseen ? "Новых реплик: " + unseen : "");
}

var MONTHS = ["янв", "фев", "мар", "апр", "май", "июн",
              "июл", "авг", "сен", "окт", "ноя", "дек"];
var KINDS = { oklad: "оклад", okl: "оклад", prk: "приказ",
              k120: "120", k122: "122", k124: "124", k152: "152" };
function monthName(m) { return MONTHS[m] || MONTHS[m - 1] || m; }

function setView(v) {
  view = v;
  // Ответ агента приходит в ленту. Если экономист смотрит план или сводку,
  // он о нем не узнает: раньше вопрос выглядел оставленным без ответа.
  if (v === "feed") unseen = 0;
  markFeedTab();
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
  var runs = state.runs || [];
  var ok = runs.filter(function (r) { return r.status === "OPTIMAL"; })[0];
  // Без удачного расчета показываем проверки последнего неудачного: где
  // именно не сошлось — и есть ответ на вопрос «почему решения нет».
  var failed = runs.filter(function (r) {
    return r.status !== "OPTIMAL" && r.status !== "идет" && r.summary && r.summary.has_result;
  })[0];
  var pick = ok || failed;
  if (!pick) { vresult = null; vrun = null; return Promise.resolve(); }
  if (vrun === pick.id) return Promise.resolve();
  return api("/api/case/" + caseId + "/run/" + pick.id + "/result").then(function (d) {
    d.status = pick.status;
    d.analysis = (pick.summary && pick.summary["анализ"]) || [];
    vresult = d; vrun = pick.id;
    // Правила и сводка считаются по тем же файлам, но отдельно: решатель о
    // них не отчитывается, это проверка его работы, а не его слова.
    return Promise.all([
      api("/api/case/" + caseId + "/run/" + pick.id + "/rules")
        .then(function (r) { vrules = r["правила"] || []; })
        .catch(function () { vrules = null; }),
      api("/api/case/" + caseId + "/run/" + pick.id + "/summary")
        .then(function (r) { vsum = r; })
        .catch(function () { vsum = null; }),
      api("/api/case/" + caseId + "/run/" + pick.id + "/payroll")
        .then(function (r) { vpay = r; })
        .catch(function () { vpay = null; }),
    ]);
  });
}

/* ── сводка года ───────────────────────────────────────────────
 * То, с чего финансист начинает разговор о годовом плане: сколько денег
 * заложено и сколько разошлось, как выплаты ложатся на месяцы рядом с
 * поступлениями, из чего складывается сумма, кто сколько получит. Порядок
 * стандартный для финансового дашборда: несколько ключевых чисел, затем
 * план-факт по месяцам, затем структура и разрезы.
 */
function bar(share, cls) {
  var w = Math.max(0, Math.min(1, share || 0));
  return '<span class="gauge ' + (cls || "") + '"><i style="width:' +
         (w * 100).toFixed(1) + '%"></i></span>';
}

function num(v) {
  return v == null ? "—" : String(v).replace(".", ",");
}

function pct(v) {
  return v == null ? "—" : Math.round(v * 100) + " %";
}

/* Помесячный график: поступления и выплаты столбиками, остаток линией.
   Рисуем SVG сами — библиотека ради двенадцати столбиков не нужна. */
function monthChart(labels, got, paid, rest) {
  var W = 720, H = 190, L = 54, B = 26, T = 10;
  var max = Math.max.apply(null, got.concat(paid).concat([1]));
  var step = (W - L) / labels.length;
  var bw = Math.max(4, step / 2 - 3);
  var y = function (v) { return T + (H - T - B) * (1 - v / max); };
  var bars = labels.map(function (m, i) {
    var x = L + i * step + 2;
    return '<rect class="b1" x="' + x.toFixed(1) + '" y="' + y(got[i]).toFixed(1) +
           '" width="' + bw.toFixed(1) + '" height="' +
           Math.max(0, H - B - y(got[i])).toFixed(1) + '"></rect>' +
           '<rect class="b2" x="' + (x + bw + 2).toFixed(1) + '" y="' + y(paid[i]).toFixed(1) +
           '" width="' + bw.toFixed(1) + '" height="' +
           Math.max(0, H - B - y(paid[i])).toFixed(1) + '"></rect>';
  }).join("");
  var maxRest = Math.max.apply(null, rest.concat([1]));
  var line = rest.map(function (v, i) {
    var x = L + i * step + step / 2;
    var yy = T + (H - T - B) * (1 - Math.max(0, v) / Math.max(maxRest, max));
    return (i ? "L" : "M") + x.toFixed(1) + " " + yy.toFixed(1);
  }).join(" ");
  var names = labels.map(function (m, i) {
    return '<text class="mx" x="' + (L + i * step + step / 2).toFixed(1) +
           '" y="' + (H - 8) + '">' + esc(m) + "</text>";
  }).join("");
  var ticks = [0, 0.5, 1].map(function (f) {
    var v = max * f;
    return '<line class="gl" x1="' + L + '" x2="' + W + '" y1="' + y(v).toFixed(1) +
           '" y2="' + y(v).toFixed(1) + '"></line>' +
           '<text class="ax" x="' + (L - 8) + '" y="' + (y(v) + 4).toFixed(1) +
           '">' + esc(mo(v)) + "</text>";
  }).join("");
  return '<div class="chart"><svg viewBox="0 0 ' + W + " " + H +
    '" preserveAspectRatio="none" role="img">' + ticks + bars +
    '<path class="ln" d="' + line + '"></path>' + names + "</svg>" +
    '<div class="lgd"><span class="k1">поступления</span>' +
    '<span class="k2">выплаты</span><span class="k3">остаток</span></div></div>';
}

function summaryView() {
  if (!vsum) return '<div class="none">Сводка недоступна: файлы прогона не сохранены</div>';
  var t = vsum["итого"], m = vsum["месяцы"];
  var cards = [
    ["ФОТ договоров", mo(t["ФОТ договоров"]) + " ₽", "заложено на год"],
    ["Распределено планом", mo(t["распределено планом"]) + " ₽",
     pct(t["освоение"]) + " от ФОТ"],
    ["Не распределено", mo(t["не распределено"]) + " ₽",
     t["не распределено"] > 0 ? "останется на договорах" : "остатка нет"],
    ["Сотрудников", t["сотрудников"], "договоров: " + t["договоров"]],
    ["На человека в месяц", mo(t["средняя выплата в месяц на человека"]) + " ₽",
     "в среднем по плану"],
  ].map(function (c) {
    return '<div class="kpi"><div class="kh">' + esc(c[0]) + "</div>" +
      '<div class="kv">' + esc(String(c[1])) + "</div>" +
      '<div class="kn">' + esc(String(c[2])) + "</div></div>";
  }).join("");

  var kinds = '<table class="mini"><thead><tr><th>Вид выплаты</th><th class="n">Сумма, ₽</th>' +
    '<th class="n">Доля</th><th></th></tr></thead><tbody>' +
    vsum["виды выплат"].map(function (k) {
      return "<tr><td>" + esc(k["вид"]) + '</td><td class="n">' + mo(k["сумма"]) +
        '</td><td class="n">' + pct(k["доля"]) + "</td><td>" + bar(k["доля"]) + "</td></tr>";
    }).join("") + "</tbody></table>";

  var ctrs = '<table class="mini"><thead><tr><th>Договор</th><th class="n">ФОТ, ₽</th>' +
    '<th class="n">Поступления, ₽</th><th class="n">Выплаты, ₽</th>' +
    '<th class="n">Остаток, ₽</th><th class="n">Освоение</th><th></th></tr></thead><tbody>' +
    vsum["договоры"].map(function (c) {
      return "<tr><td>" + esc(c["договор"]) +
        (c["ГОЗ"] ? '<span class="tag">ГОЗ</span>' : "") +
        '<div class="sub">' + esc(c["название"] || "") + "</div></td>" +
        '<td class="n">' + mo(c["ФОТ"]) + '</td><td class="n">' + mo(c["поступления"]) +
        '</td><td class="n">' + mo(c["выплаты"]) + '</td><td class="n' +
        (c["остаток"] > 0 ? " warn" : "") + '">' + mo(c["остаток"]) +
        '</td><td class="n">' + pct(c["освоение"]) + "</td><td>" +
        bar(c["освоение"], c["остаток"] > 0 ? "warn" : "ok") + "</td></tr>";
    }).join("") + "</tbody></table>";

  var pgp = paged("sum-people", vsum["люди"]);
  var people = table(
    ["табельный", "ФИО", "должность", "подразделение", { t: "ставка" },
     { t: "зарплата, ₽" }, { t: "за год, ₽" }, { t: "месяцев" }, "договоры"],
    pgp.rows.map(function (p) {
      return [p["код"], p["фио"], p["должность"], p["подразделение"] || "—",
              { v: p["ставка"], cls: "n" },
              { v: mo(p["зарплата"]), cls: "n" },
              { v: mo(p["за год"]), cls: "n" },
              { v: p["месяцев"], cls: "n" },
              (p["договоры"] || []).join(", ")];
    }), "", { startNum: pgp.from }) + pager("sum-people", pgp);

  return '<div class="kpis">' + cards + "</div>" +
    '<div class="grp"><h4>Поступления и выплаты по месяцам</h4>' +
    '<div class="vh">Столбики — деньги, пришедшие на договоры и выплаченные ' +
    "людям. Линия — остаток на счетах нарастающим итогом: он не может уйти " +
    "ниже нуля.</div>" +
    monthChart(m["подписи"], m["поступления"], m["выплаты"], m["остаток"]) + "</div>" +
    '<div class="two">' +
      '<div class="grp"><h4>Из чего складываются выплаты</h4>' +
        '<div class="hscroll">' + kinds + "</div></div>" +
      '<div class="grp"><h4>Договоры</h4>' +
        '<div class="hscroll">' + ctrs + "</div></div></div>" +
    '<div class="grp"><h4>Люди</h4>' + people + "</div>";
}

/* ── из чего складывается зарплата ─────────────────────────────
 * Матрица «ФИО на месяцы» с суммами ничего не сообщала: в каждой клетке
 * стоит месячная зарплата, потому что выплатить ее целиком — жесткое
 * условие. Смотреть надо на состав: с каких договоров человек финансируется,
 * на каких ставках сидит, из каких видов выплат складывается сумма и как
 * часто схема меняется. Смена договора оклада и смена состава выплат — это
 * приказы и допники: оптимизатор их штрафует, экономисту надо видеть,
 * сколько их вышло.
 */
var CTR_COLOR = ["c1", "c2", "c3", "c4", "c5", "c6"];

function ctrClass(code) {
  var list = (vpay && vpay["договоры"]) || [];
  for (var i = 0; i < list.length; i++) {
    if (list[i]["код"] === code) return CTR_COLOR[i % CTR_COLOR.length];
  }
  return "c1";
}

/* Полоса года: клетка на месяц. Высота — открытая ставка, доли внутри —
   договоры, которые ее держат. Пунктир — штатная ставка. Засечка сверху —
   месяц, где схема сменилась.

   Так ставка читается при любом числе смен: «1 → 1,5» текстом ложилось в
   одну ячейку и рассыпалось бы, если ставка меняется дважды за год. */
function yearStrip(person, maxRate) {
  var base = person["ставка"] || 1;
  return '<span class="strip" style="--base:' +
    (maxRate ? (base / maxRate * 100).toFixed(1) : 100) + '%">' +
    person["месяцы"].map(function (m, i) {
      if (!m) {
        return '<i class="off" title="' + MONTHS[i] + ': выплат нет"></i>';
      }
      var rate = m["ставка"] || 0;
      var h = maxRate ? Math.max(8, rate / maxRate * 100) : 100;
      var seg = m["договоры"].map(function (c) {
        var f = rate ? (c["ставка"] || 0) / rate : 1;
        return '<u class="' + ctrClass(c["код"]) + '" style="flex:' +
               (f || 0.001) + '"></u>';
      }).join("");
      var tip = MONTHS[i] + ": ставка " + (rate || "—") + " = " +
        m["договоры"].map(function (c) {
          return c["код"] + " " + (c["ставка"] || 0) + " (" + mo(c["сумма"]) + " ₽)";
        }).join(" + ") +
        (m["изменилось"].length ? " — " + m["изменилось"].join("; ") : "");
      return '<i class="' + (m["изменилось"].length ? "chg" : "") + '" title="' +
        esc(tip) + '"><b style="height:' + h.toFixed(1) + '%">' + seg + "</b></i>";
    }).join("") + "</span>";
}

//: Цвет вида выплаты: один на все места, где виды показаны рядом.
var KIND_CLASS = { "оклад": "k1", "120": "k2", "122": "k3", "124": "k4",
                   "152": "k5", "приказ": "k6" };

function kindClass(kind) {
  return KIND_CLASS[kind] || "k6";
}

function payrollView() {
  if (!vpay) {
    return '<div class="none">Состав выплат недоступен: файлы прогона не сохранены</div>';
  }
  var t = vpay["итого"];
  var legend = '<div class="lgd ctrs">' + vpay["договоры"].map(function (c) {
    return '<span class="' + ctrClass(c["код"]) + '">' + esc(c["код"]) +
           (c["ГОЗ"] ? " (ГОЗ)" : "") + "</span>";
  }).join("") + '<span class="chgk">смена схемы</span>' +
    '<span class="basek">штатная ставка</span>' +
    '<span class="note">полоса — год с января по декабрь: высота клетки — ' +
    "открытая ставка, доли внутри — договоры; наведите на месяц</span></div>";
  var head = '<div class="vh">Прогон № ' + vrun + ": " + t["человек"] + " " +
    px(t["человек"], "человек", "человека", "человек") + ", всего <b>" +
    mo(t["за год"]) + " ₽</b>. Сумма за месяц у каждого равна его зарплате — " +
    "это жесткое условие, и смотреть надо не на нее, а на то, из чего она " +
    "сложилась: договоры, ставки, виды выплат. Щелчок по строке — месяц за " +
    "месяцем.</div>" +
    '<div class="kpis small">' + [
      ["Месяцев со сменой структуры", t["месяцев со сменой структуры"],
       "менялись договор, ставка или доли"],
      ["Переводов оклада", t["смен договора оклада"],
       "оклад ушел на другой договор"],
      ["Открыто ставок", t["открыто ставок"], "добавился второй договор оклада"],
      ["Месяцев с совместительством", t["месяцев с совместительством"],
       "ставка сверх штатной"],
    ].map(function (c) {
      return '<div class="kpi"><div class="kh">' + esc(c[0]) + "</div>" +
        '<div class="kv">' + c[1] + "</div>" +
        '<div class="kn">' + esc(c[2]) + "</div></div>";
    }).join("") + "</div>";

  // Зачем открыты ставки — счетом. Это первый вопрос к плану, где у людей
  // появилось совместительство, и отвечать на него должен сервис, а не
  // экономист, разбирая таблицу.
  var why = (vpay["почему"] || []).length
    ? '<div class="whybox"><div class="fh">Почему открыты ставки</div>' +
      vpay["почему"].map(function (t) {
        return "<p>" + esc(t) + "</p>";
      }).join("") + "</div>"
    : "";
  var pg = paged("payroll", vpay["люди"]);
  // Шкала полос общая на всю страницу: иначе полторы ставки у одного и одна
  // у другого рисуются одинаково высокими.
  var maxRate = 1;
  vpay["люди"].forEach(function (p) {
    if (p["ставка макс"] > maxRate) maxRate = p["ставка макс"];
    if (p["ставка"] > maxRate) maxRate = p["ставка"];
  });
  var body = table(
    [{ v: "Сотрудник", cls: "key" }, "Должность", { t: "ставка" },
     { t: "зарплата, ₽" }, "Ставки и договоры по месяцам", "Финансируется",
     "Из чего складывается", { t: "смен" }, { t: "за год, ₽" }],
    pg.rows.map(function (p) {
      // Доли видов за год плюс то, как они менялись: набор надбавок мог не
      // меняться, а доля оклада — вырасти вместе с открытой ставкой.
      var kinds = (p["доли видов"] || []).map(function (k) {
        return '<span class="kd"><b class="' + kindClass(k["вид"]) + '"></b>' +
               esc(k["вид"]) + " " + Math.round(k["доля"] * 100) + " %</span>";
      }).join("");
      var sh = p["доля оклада"];
      if (sh && sh["макс"] - sh["мин"] > 0.01) {
        kinds += '<span class="kd note">доля оклада по месяцам: ' +
                 Math.round(sh["мин"] * 100) + "–" + Math.round(sh["макс"] * 100) +
                 " %</span>";
      }
      // Ставка числом — только границы за год: штатная и наибольшая. Как она
      // менялась, показывает полоса, а не текст в ячейке.
      var rate = num(p["ставка"]);
      if (p["ставка макс"] && p["ставка макс"] > p["ставка"] + 0.01) {
        rate += "–" + num(p["ставка макс"]);
      }
      var sw = p["месяцев со сменой структуры"];
      return [
        { v: '<span class="dname" data-emp="' + esc(p["код"]) + '">' +
             esc(p["фио"]) + "</span>", cls: "key" },
        p["должность"] || null,
        { v: rate, cls: "n" },
        { v: mo(p["зарплата"]), cls: "n" },
        { v: yearStrip(p, maxRate) },
        { v: p["договоры"].map(function (c) {
            return '<span class="ctag ' + ctrClass(c) + '">' + esc(c) + "</span>";
          }).join("") },
        { v: kinds },
        { v: sw ? sw : "—", cls: "n" + (sw > 2 ? " warn" : "") },
        { v: mo(p["за год"]), cls: "n tot" },
      ];
    }), "", { startNum: pg.from }) + pager("payroll", pg);
  return head + why + legend + body;
}

/* Месяц за месяцем по одному человеку: договоры со ставками, виды выплат
   суммами и отметка, что изменилось против прошлого месяца. */
function personDetail(code) {
  var p = ((vpay && vpay["люди"]) || []).filter(function (x) {
    return x["код"] === code;
  })[0];
  if (!p) return "";
  var kindOrder = ["оклад", "120", "122", "124", "152", "приказ"];
  var used = kindOrder.filter(function (k) {
    return p["месяцы"].some(function (m) {
      return m && m["договоры"].some(function (c) {
        return c["виды"].some(function (v) { return v["вид"] === k; });
      });
    });
  });
  var rows = [];
  p["месяцы"].forEach(function (m, i) {
    if (!m) return;
    rows.push([
      MONTHS[i],
      { v: m["договоры"].map(function (c) {
          return '<span class="ctag ' + ctrClass(c["код"]) + '">' + esc(c["код"]) +
                 (c["ставка"] ? " · " + c["ставка"] : "") + "</span>";
        }).join("") },
    ].concat(used.map(function (k) {
      var s = 0;
      m["договоры"].forEach(function (c) {
        c["виды"].forEach(function (v) { if (v["вид"] === k) s += v["сумма"]; });
      });
      return { v: s ? mo(s) : null, cls: "n" };
    })).concat([
      { v: mo(m["всего"]), cls: "n tot" },
      { v: m["изменилось"].length
          ? '<span class="chgtag">' + esc(m["изменилось"].join("; ")) + "</span>"
          : null },
    ]));
  });
  return '<div class="grp"><h4>' + esc(p["фио"]) + " — месяц за месяцем</h4>" +
    '<div class="vh">Ставка складывается из открытых ставок на договорах. ' +
    "«Изменилось» отмечает месяц, где сменился договор оклада или набор " +
    "надбавок: за каждой такой сменой стоит приказ или допник.</div>" +
    table(["Месяц", "Договоры и ставки"].concat(
            used.map(function (k) { return { t: k + ", ₽" }; }),
            [{ t: "всего, ₽" }, "Изменилось"]),
      rows, "", { plain: true }) + "</div>";
}

//: Состояние правила: как называется, каким цветом, в каком порядке.
var RULE_STATE = {
  "нарушено": { cls: "bad", n: 0 },
  "внимание": { cls: "warn", n: 1 },
  "соблюдено": { cls: "ok", n: 2 },
  "не применялось": { cls: "off", n: 3 },
};

/* Ограничения работы экономиста: по каждому — соблюдено, нарушено или не
   применялось в этом плане, и если нарушено, то где именно. Нарушения видны
   сразу, без раскрытия: ради них список и открывают. Внутри раздела нарушения
   идут первыми, дальше требующее внимания, соблюденное и неприменимое. */
function rulesView() {
  if (!vrules) return "";
  if (!vrules.length) {
    return '<div class="grp"><h4>Ограничения</h4>' +
      '<div class="none">Правила не проверены: файлы прогона не сохранены</div></div>';
  }
  var bad = vrules.filter(function (r) { return r["состояние"] === "нарушено"; });
  var warn = vrules.filter(function (r) { return r["состояние"] === "внимание"; }).length;
  var ok = vrules.filter(function (r) { return r["состояние"] === "соблюдено"; }).length;
  // Жесткие условия обсуждению не подлежат: нарушено хоть одно — план не
  // годится, и это ошибка в данных или в сервисе, а не повод для решения.
  var hard = vrules.filter(function (r) { return r["тип"] === "нарушать нельзя"; });
  var hardBad = hard.filter(function (r) { return r["состояние"] === "нарушено"; }).length;
  var hardOk = hard.filter(function (r) { return r["состояние"] === "соблюдено"; }).length;
  var head = hardBad
    ? "Нарушено жестких условий: " + hardBad + ". Такой план принимать нельзя: " +
      "ищите ошибку во входных данных."
    : "Жесткие условия соблюдены: " + hardOk + " из " +
      hard.filter(function (r) { return r["состояние"] !== "не применялось"; }).length +
      (bad.length ? ". Отклонения есть там, где они допускаются." : "") +
      (warn ? ". Есть что посмотреть." : "");
  var order = [];
  vrules.forEach(function (r) {
    if (order.indexOf(r["раздел"]) < 0) order.push(r["раздел"]);
  });
  return '<div class="grp"><h4>Ограничения<span class="c ' +
    (hardBad ? "bad" : "ok") + '">' + (hardBad || ok) + "</span></h4>" +
    '<div class="vh">' + esc(head) + ". Проверено по плану выплат и входным " +
    "данным, независимо от решателя.</div>" +
    order.map(function (section) {
      var rows = vrules.filter(function (r) { return r["раздел"] === section; })
        .sort(function (a, b) {
          return (RULE_STATE[a["состояние"]] || { n: 4 }).n -
                 (RULE_STATE[b["состояние"]] || { n: 4 }).n;
        });
      return '<div class="rsec"><div class="rsh">' + esc(section) + "</div>" +
        rows.map(ruleRow).join("") + "</div>";
    }).join("") + "</div>";
}

/* Условие в числах: чем подтверждено, насколько план подошел к пределу и
   таблица самых узких мест. «Соблюдено» без цифр не говорит ничего: важно,
   осталось ли до границы 40 000 ₽ или ноль. */
function ruleTable(r) {
  var rows = r["строки"] || [];
  if (!rows.length) return "";
  var numeric = rows[0]["предел"] !== undefined && rows[0]["предел"] !== null;
  if (!numeric) {
    return '<table class="mini rb2"><tbody>' + rows.map(function (s) {
      return "<tr><td>" + esc(s["объект"]) + "</td><td>" + esc(s["что"] || "") +
             "</td></tr>";
    }).join("") + "</tbody></table>";
  }
  var unit = r["единица"] === "₽" ? ", ₽" : (r["единица"] ? ", " + r["единица"] : "");
  var limitName = cap(r["подпись предела"] || "предел");
  return '<table class="mini rb2"><thead><tr><th>Где</th>' +
    '<th class="n">Факт' + esc(unit) + '</th><th class="n">' + esc(limitName) +
    esc(unit) + '</th><th class="n">Запас</th><th></th></tr></thead><tbody>' +
    rows.map(function (s) {
      return '<tr class="' + (s["нарушено"] ? "over" : "") + '"><td>' +
        esc(s["объект"]) +
        (s["таких же"] > 1 ? '<span class="same">и еще ' + (s["таких же"] - 1) +
                             " с тем же результатом</span>" : "") +
        '</td><td class="n">' + mo(s["факт"]) +
        '</td><td class="n">' + mo(s["предел"]) + '</td><td class="n' +
        (s["запас"] < 0 ? " over" : "") + '">' + mo(s["запас"]) + "</td><td>" +
        (s["доля"] == null ? "" : bar(s["доля"], s["нарушено"] ? "bad" : "ok")) +
        "</td></tr>";
    }).join("") + "</tbody></table>";
}

function ruleRow(r) {
  var st = RULE_STATE[r["состояние"]] || { cls: "off" };
  var hard = r["тип"] === "нарушать нельзя";
  return '<div class="rule ' + st.cls + (hard ? " hard" : "") + '">' +
    '<span class="cdot2"></span>' +
    '<div class="rn">' + esc(r["правило"]) +
      (r["тип"] !== "показатель"
        ? '<span class="rk' + (hard ? " h" : "") + '">' + esc(r["тип"]) + "</span>" : "") +
    "</div>" +
    '<div class="rs">' + esc(r["состояние"]) + "</div>" +
    '<div class="rm">' + esc(r["смысл"]) + "</div>" +
    '<div class="rf">' +
      (r["использовано"] != null
        ? bar(r["использовано"], r["состояние"] === "нарушено" ? "bad" : "ok") : "") +
      esc(r["факт"] || "") +
      (r["где"] ? '<span class="rw">где смотреть: ' + esc(r["где"]) + "</span>" : "") +
    "</div>" +
    (hard && r["состояние"] === "нарушено"
      ? '<div class="ralarm">Жесткое условие нарушено — план принимать нельзя. ' +
        "Это ошибка во входных данных или в сервисе, а не выбор между " +
        "вариантами.</div>" : "") +
    ruleTable(r) +
    "</div>";
}

/* Неудачный расчет: плана нет, есть причины и проверки. Плашка одна на все
   вкладки, чтобы пустая таблица плана не выглядела как «еще считает». */
function failBanner() {
  if (!vresult || vresult.status === "OPTIMAL") return "";
  return '<div class="failban"><b>Решения нет.</b> ' +
    (vresult.analysis.length
      ? "<ul>" + vresult.analysis.map(function (t) { return "<li>" + esc(t) + "</li>"; }).join("") + "</ul>"
      : "Решатель причину не назвал.") +
    '<div class="mt">Проверки решателя — на вкладке «Ограничения».</div></div>';
}

/* ── страницы ─────────────────────────────────────────────────
 * Справочник должностей — три десятка строк, штатное расписание вырастет
 * до сотен. Отдавать их одним полотном значит заставлять экономиста
 * прокручивать вслепую: непонятно, сколько осталось и где ты находишься.
 *
 * Резать список берется страница, а не сервер: данные и так приходят целиком
 * одним ответом, и пока это так, серверная выборка только добавит запросов,
 * ничего не ускорив. Когда полотно станет тяжелым для одного ответа, резать
 * придется в запросе — тогда меняется и это место.
 *
 * Номер страницы у каждой таблицы свой: вернувшись на вкладку, попадаешь
 * туда, где был.
 */
var PAGE_SIZES = [25, 50, 100];
var pageOf = {}, sizeOf = {};

function paged(key, rows) {
  var per = sizeOf[key] || PAGE_SIZES[0];
  var last = Math.max(1, Math.ceil(rows.length / per));
  var p = Math.min(pageOf[key] || 1, last);
  pageOf[key] = p;
  var from = (p - 1) * per;
  return { rows: rows.slice(from, from + per), from: from, page: p,
           last: last, total: rows.length, per: per };
}

/* Номера страниц с пропусками: подряд показываем начало, окрестность
   текущей и конец, иначе на полусотне страниц ряд номеров сам станет
   полотном. */
function pageNums(p, last) {
  var out = [], i;
  if (last <= 7) {
    for (i = 1; i <= last; i++) out.push(i);
    return out;
  }
  out.push(1);
  var a = Math.max(2, p - 1), b = Math.min(last - 1, p + 1);
  if (a > 2) out.push("…");
  for (i = a; i <= b; i++) out.push(i);
  if (b < last - 1) out.push("…");
  out.push(last);
  return out;
}

/* Панель под таблицей. Не показываем, пока список умещается на страницу:
   «1–3 из 3» под таблицей из трех строк — служебный шум. */
function pager(key, pg) {
  if (pg.total <= PAGE_SIZES[0]) return "";
  var arrow = function (to, label, off) {
    return '<button type="button" class="ar" data-pg="' + key + ":" + to + '"' +
           (off ? " disabled" : "") + ' aria-label="' + label + '">' +
           (to < pg.page ? "‹" : "›") + "</button>";
  };
  return '<div class="pager">' +
    '<label class="per">Строк на странице ' +
      '<select data-per="' + key + '">' +
      PAGE_SIZES.map(function (n) {
        return '<option value="' + n + '"' + (n === pg.per ? " selected" : "") +
               ">" + n + "</option>";
      }).join("") + "</select></label>" +
    '<span class="rng">' + (pg.from + 1) + "–" + (pg.from + pg.rows.length) +
      " из " + pg.total + "</span>" +
    '<nav class="pgs">' +
      arrow(pg.page - 1, "Предыдущая страница", pg.page === 1) +
      pageNums(pg.page, pg.last).map(function (n) {
        return n === "…"
          ? '<span class="gap">…</span>'
          : '<button type="button" data-pg="' + key + ":" + n + '"' +
            (n === pg.page ? ' class="on" aria-current="page"' : "") + ">" + n +
            "</button>";
      }).join("") +
      arrow(pg.page + 1, "Следующая страница", pg.page === pg.last) +
    "</nav></div>";
}

/* Заголовок графы — с прописной, как и подписи свойств в карточке. Регистр
   был разный в разных местах, и это бросалось в глаза раньше содержимого.
   Правим здесь, в одном месте: заголовки задаются во многих таблицах. */
function cap(s) {
  s = String(s == null ? "" : s);
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

/* Перечни всегда нумеруются: по номеру строки удобно сослаться в разговоре
   и найти место в длинной таблице. Колонку добавляем здесь, а не в каждом
   вызове, — чтобы нумерация была одинаковой везде. Отключается opts.plain. */
function table(head, rows, note, opts) {
  var num = !(opts && opts.plain);
  var h = note ? '<div class="vh">' + note + "</div>" : "";
  if (!rows.length) return h + '<div class="none">Пусто</div>';
  // Таблица прокручивается в своей полосе, а не вместе со всей страницей: у
  // договора четырнадцать граф, они не помещаются в ширину, а полоса
  // прокрутки страницы уезжает вниз — колонки выглядели пропавшими.
  // Ширины граф заданы заранее — тогда они не зависят от того, что стоит в
  // шапке. Без этого панель действий над выбранными строками (одна ячейка
  // на пять граф) заставляла браузер пересчитывать раскладку, и таблица
  // прыгала при первом же поставленном флажке.
  var widths = opts && opts.widths;
  var cols = widths
    ? "<colgroup>" + (num ? '<col style="width:44px">' : "") +
      widths.map(function (w) {
        return "<col" + (w ? ' style="width:' + w + '"' : "") + ">";
      }).join("") + "</colgroup>"
    : "";
  h += '<div class="hscroll"><table' + (cols ? ' class="fixed"' : "") + ">" + cols +
       "<thead" + (opts && opts.headCls ? ' class="' + opts.headCls + '"' : "") +
       "><tr>" + (num ? '<th class="num">№</th>' : "") +
       head.map(function (c) {
         // Заголовок бывает строкой, числовой колонкой {t} или готовой
         // разметкой {v} — в ней, например, флажок «выделить все».
         if (typeof c !== "object") return "<th>" + esc(cap(c)) + "</th>";
         if (c.v != null) return '<th class="' + (c.cls || "") + '"' +
                                 (c.span ? ' colspan="' + c.span + '"' : "") + ">" +
                                 c.v + "</th>";
         return '<th class="n">' + esc(cap(c.t)) + "</th>";
       }).join("") + "</tr></thead><tbody>";
  var first = (opts && opts.startNum) || 0;
  h += rows.map(function (r, i) {
    var rc = opts && opts.rowCls ? opts.rowCls(i) : "";
    return "<tr" + (rc ? ' class="' + rc + '"' : "") + ">" +
      (num ? '<td class="num">' + (first + i + 1) + "</td>" : "") +
      r.map(function (c) {
        return (c && typeof c === "object")
          ? '<td class="' + (c.cls || "") + '">' + (c.v == null ? "—" : c.v) + "</td>"
          : "<td>" + (c == null || c === "" ? "—" : esc(c)) + "</td>";
      }).join("") + "</tr>";
  }).join("");
  return h + "</tbody></table></div>";
}

function renderView() {
  var el = $("view");

  if (view === "emp") {
    var e = (vdata && vdata.employees) || [];
    var pge = paged("v-emp", e);
    el.innerHTML = table(
      ["табельный", "ФИО", "должность", { t: "ставка" }, { t: "оклад, ₽" },
       "с", "по", "источник"],
      pge.rows.map(function (x) {
        return [x.code, x.fio, x.position,
                { v: x.rate == null ? null : String(x.rate).replace(".", ","), cls: "n" },
                { v: x.salary == null ? null : mo(x.salary), cls: "n" },
                x.from, x.to, x.source];
      }),
      "Штатное расписание дела: <b>" + e.length + "</b> " +
      px(e.length, "сотрудник", "сотрудника", "сотрудников") +
      ". Собрано агентом из загруженных документов.",
      { startNum: pge.from }) + pager("v-emp", pge);
    return;
  }

  if (view === "ctr") {
    var c = (vdata && vdata.contracts) || [];
    var pgc = paged("v-ctr", c);
    el.innerHTML = table(
      ["шифр", "наименование", "номер", "вид", "ГОЗ", { t: "фонд, ₽" },
       "разрешенные выплаты", "источник"],
      pgc.rows.map(function (x) {
        return [x.code, x.name, x.number, x.kind,
                { v: x.goz ? '<span class="tag goz">' + esc(x.goz) + "</span>" : null },
                { v: x.fund == null ? null : mo(x.fund), cls: "n" },
                x.kinds, x.source];
      }),
      "Договоры дела: <b>" + c.length + "</b>.",
      { startNum: pgc.from }) + pager("v-ctr", pgc);
    return;
  }

  if (view === "ref") {
    var r = (vdata && vdata.reference) || [];
    var pgr = paged("v-ref", r);
    el.innerHTML = table(
      ["должность", "категория", { t: "оклад за 1,0 ставки, ₽" },
       { t: "П2556, ₽" }, { t: "П4, ₽" }, { t: "БЭП, ₽" }, "примечание"],
      pgr.rows.map(function (x) {
        return [x.pos, x.cat,
                { v: x.sal == null ? null : mo(x.sal), cls: "n" },
                { v: x.p2556 == null ? null : mo(x.p2556), cls: "n" },
                { v: x.p4 == null ? null : mo(x.p4), cls: "n" },
                { v: x.bep == null ? null : mo(x.bep), cls: "n" },
                x.note];
      }),
      "<b>Нормативная база организации</b>, общая для всех планов. " +
      "Справочник должностей: <b>" + r.length + "</b> " +
      px(r.length, "позиция", "позиции", "позиций") +
      ". Прочерк — отдельной строки для должности в источнике нет. " +
      "Обновляется, когда выходит новая редакция приказа или положения.",
      { startNum: pgr.from }) + pager("v-ref", pgr);
    return;
  }

  if (view === "sub") {
    var s = (vdata && vdata.substitutions) || [];
    var pgs = paged("v-sub", s);
    el.innerHTML = table(
      ["должность", "может быть замещена"],
      pgs.rows.map(function (x) { return [x.position, x.replaced_by]; }),
      "<b>Нормативная база организации</b>, общая для всех планов. " +
      "Правил замещения: <b>" + s.length + "</b>. Правила направленные — " +
      "слева должность сотрудника, справа те, которые ему можно дать " +
      "дополнительно. Уходят в расчет: работу по такой должности сотрудник " +
      "выполнить может, обратное — нет.",
      { startNum: pgs.from }) + pager("v-sub", pgs);
    return;
  }

  if (!vresult) {
    el.innerHTML = '<div class="none">Расчет еще не выполнен</div>';
    return;
  }
  if (vresult.status !== "OPTIMAL" && view !== "lim") {
    el.innerHTML = failBanner();
    return;
  }

  if (view === "sum") {
    el.innerHTML = failBanner() + summaryView();
    return;
  }

  if (view === "plan") {
    el.innerHTML = payrollView() + '<div id="plandetail"></div>';
    return;
  }

  if (view === "cash") {
    // Одна таблица: договор × показатель × месяц. Полоса под числом
    // показывает профиль года; остаток, ушедший в минус, подсвечен — это
    // нарушение, которого решатель не допускает, и если оно есть, его надо
    // увидеть сразу.
    var ccodes = Object.keys(vresult.cash || {}).sort();
    var order = ["Поступление", "Выплаты", "Остаток на конец"];
    var cpeak = 0;
    ccodes.forEach(function (code) {
      order.forEach(function (k) {
        ((vresult.cash[code] || {})[k] || []).forEach(function (v) {
          if (Math.abs(v) > cpeak) cpeak = Math.abs(v);
        });
      });
    });
    var crows = [];
    ccodes.forEach(function (code) {
      order.forEach(function (k, ki) {
        var arr = (vresult.cash[code] || {})[k];
        if (!arr) return;
        var sum = arr.reduce(function (a, v) { return a + (v || 0); }, 0);
        crows.push([{ v: ki ? "" : esc(code), cls: "key" }, k].concat(
          arr.map(function (v) {
            if (!v) return { v: null, cls: "n" };
            var share = cpeak ? Math.max(4, Math.round((Math.abs(v) / cpeak) * 100)) : 0;
            return { v: '<span class="bar' + (v < 0 ? " neg" : "") +
                        '" style="--f:' + share + '%">' + mo(v) + "</span>", cls: "n" };
          }),
          [{ v: k === "Остаток на конец" ? null : mo(sum), cls: "n tot" }]));
      });
    });
    el.innerHTML = '<div class="vh">Освоение по договорам: поступления, выплаты и ' +
      "остаток кассы по месяцам. Остаток переносится только вперед и не может " +
      "уходить в минус.</div>" +
      table([{ v: "Договор", cls: "key" }, "Показатель"].concat(
              MONTHS.map(function (m) { return { t: m }; }), [{ t: "за год, ₽" }]),
            crows, "");
    return;
  }

  if (view === "lim") {
    // Ограничения — это правила работы экономиста, а не показатели решателя.
    // «OPTIMAL за 2,8 с» не говорит, соблюдено ли, что человек получает всю
    // зарплату, что деньги не потрачены раньше поступления, что средняя по
    // ГОЗ в пределах базовой. Поэтому первым идет список правил, и каждое
    // проверено независимо — по входному файлу и плану выплат. Показатели
    // решателя остались ниже, свернутыми: они нужны, когда правило нарушено
    // и надо понять, что делал решатель.
    var sum = vresult.summary || [], warn = vresult.warnings || [];
    var STS = { "Выполнено": "ok", "ОК": "ok", "Предупреждение": "warn",
                "Ошибка": "bad", "Нарушено": "bad" };
    var checks = '<div class="grp"><h4>Ограничения</h4><div class="checks">' +
      sum.map(function (s) {
        if (!Array.isArray(s)) return "";
        var name = s[0], value = s[1], status = s[2], note = s[3];
        var cls = STS[String(status || "").trim()] || "off";
        var shown = (typeof value === "number" && Math.abs(value) >= 10000) ? mo(value)
                    : (value == null ? "" : String(value));
        return '<div class="check ' + cls + '"><span class="cdot2"></span>' +
          '<span class="cn">' + esc(name) + "</span>" +
          '<span class="cv">' + esc(shown) + "</span>" +
          (status ? '<span class="cs">' + esc(status) + "</span>" : "") +
          (note ? '<div class="cnote">' + esc(note) + "</div>" : "") + "</div>";
      }).join("") + "</div></div>";

    // Проблемы — ошибки первыми. У каждой — где смотреть и что сделать: это
    // и есть самое ценное в отчете решателя, а раньше склеивалось в строку.
    var groups = { "Ошибка": [], "Предупреждение": [] };
    warn.forEach(function (w) {
      if (!Array.isArray(w) || !groups[w[0]]) return;
      groups[w[0]].push(w);
    });
    var problems = Object.keys(groups).filter(function (k) { return groups[k].length; })
      .map(function (level) {
        return '<div class="grp"><h4>' + esc(level) + '<span class="c">' +
          groups[level].length + "</span></h4>" +
          groups[level].map(function (w) {
            var where = [w[1], w[2], w[3]].filter(function (x) {
              return x != null && x !== "" && x !== "—";
            }).map(String).join(" · ");
            return '<div class="prob ' + (level === "Ошибка" ? "bad" : "warn") + '">' +
              '<div class="pd">' + esc(w[4] || "") + "</div>" +
              (where ? '<div class="pw">' + esc(where) +
                       (w[5] ? " · " + esc(String(w[5])) : "") + "</div>" : "") +
              (w[6] ? '<div class="pr">' + esc(w[6]) + "</div>" : "") + "</div>";
          }).join("") + "</div>";
      }).join("");
    if (!problems) problems = '<div class="grp"><h4>Проблемы</h4>' +
      '<div class="none">Ошибок и предупреждений нет</div></div>';
    el.innerHTML = failBanner() + rulesView() +
      '<details class="fold2"><summary>Показатели расчета и сообщения решателя' +
      "</summary>" + checks + problems + "</details>";
  }
}



/* ── выделение строк реестра ──────────────────────────────────
 * При двух десятках договоров документы убирают пачкой — прошлогодние
 * разом, — а не по одному через меню строки. Поэтому в реестре флажки
 * и панель действий, которая появляется, только когда что-то отмечено.
 */
var picked = {};

function pickedIds() {
  return Object.keys(picked).filter(function (k) { return picked[k]; });
}

/* Действия над отмеченными строками занимают ряд шапки таблицы — тот самый,
   где стоит флажок «выделить все». Отдельная полоса добавляла третью
   горизонтальную полосу к вкладкам и шапке; во всех системах, где этот
   прием описан (Carbon, Polaris, Material), панель именно подменяет
   существующий ряд, а не появляется сверху. Высота ряда та же, поэтому
   таблица не дергается. */
/* Номер строки и флажок делят одну ячейку и одно место в ней: в покое виден
   номер, при наведении на строку он сменяется флажком. Два служебных столбца
   подряд — колонка пустых квадратиков и колонка номеров — занимают вдвое
   больше места, чем нужно, и первое, что видит глаз слева, оказывается рядом
   пустых рамок. Так устроены таблицы Notion и Airtable. Флажок остается в
   разметке всегда, а не появляется по наведению, — иначе до него не добраться
   с клавиатуры; при фокусе он тоже показывается. */
function pk(input, label) {
  return '<label class="pk">' + input + '<span class="rn">' + esc(label) + "</span></label>";
}

function pickCell(n) {
  return '<div class="pickrow"><span class="picked">' + n + " " +
         px(n, "документ выбран", "документа выбрано", "документов выбрано") +
         "</span>" +
         '<button type="button" id="pickdel">Удалить</button>' +
         '<button type="button" class="x" id="pickclear">Отмена</button></div>';
}

/* Панель отметок живет отдельно от таблицы: так выбор строк не заставляет
   перерисовывать список. */
/* Панель и ряд вкладок делят одно место, поэтому переключаем их вместе. */
function refreshPickBar() {
  if (regTab === "docs") renderRegistry();
}

/* Снять все отметки. Флажки остаются на месте — они появляются по наведению,
   а не по режиму. */
function stopSelecting() {
  picked = {};
  Array.prototype.forEach.call(document.querySelectorAll("#regview input[type=checkbox]"),
    function (b) { b.checked = false; });
  refreshPickBar();
}

function removePicked() {
  var ids = pickedIds();
  if (!ids.length) return;
  var names = ids.map(function (id) {
    var d = (regData.documents || []).filter(function (x) { return String(x.id) === id; })[0];
    return d ? d.name : id;
  });
  var one = ids.length === 1;
  ask("Удалить " + ids.length + " " +
      px(ids.length, "документ", "документа", "документов") + "?",
      names.slice(0, 4).join(", ") +
      (names.length > 4 ? " и еще " + (names.length - 4) : "") +
      // «Уйдут» — эвфемизм там, где действие необратимое: пишем «удалятся».
      // Документ и файл — одно и то же, называем одним словом.
      (one ? ". Вместе с документом удалятся извлеченные из него данные."
           : ". Вместе с документами удалятся извлеченные из них данные."),
      "Удалить").then(function (yes) {
    if (!yes) return;
    Promise.all(ids.map(function (id) {
      return api("/api/document/" + id, { method: "DELETE" }).catch(function () {});
    })).then(function () {
      picked = {};
      loadRegistry();
      tick();
    });
  });
}

/* ── карточка документа ───────────────────────────────────────
 * В реестре у документа были имя, вид и счетчик «сотрудников 4». Проверить
 * по такой строке нечего: какие это сотрудники, откуда взялся оклад — не
 * видно, а сам файл нельзя даже открыть. Для ГОЗ это главный вопрос
 * проверяющего, и отвечать на него экономист должен здесь, а не роясь в
 * папке загрузок.
 *
 * Карточка выезжает панелью справа и не уносит из реестра: закрыл — и ты на
 * том же месте списка, с теми же отметками.
 */
/* Статус — закрытый перечень, а не свободный текст. Состояния названы так,
   как назвал бы их финансист: «файл не прочитан» и «данные не извлечены» —
   разные вещи, и путать их нельзя. Почему именно так вышло, сказано в
   карточке документа: в реестре нужен статус, а не диагностика. */
var STATUS = {
  "разобран": { text: "Обработан", cls: "ok" },
  "ждет подтверждения": { text: "Ждет подтверждения", cls: "ask" },
  "не распознан": { text: "Данные не извлечены", cls: "warn" },
  "не прочитан": { text: "Файл не прочитан", cls: "bad" },
  "текст нечитаемый": { text: "Текст нечитаемый", cls: "bad" },
  "заменен": { text: "Заменен новой версией", cls: "wait" },
  "ожидает": { text: "В обработке", cls: "wait" },
};

function docStatus(state) {
  return STATUS[state] || { text: state || "—", cls: "wait" };
}

/* Что документ внес в реестр — только счетчики по сущностям, ничего больше. */
function produced(made) {
  var forms = {
    "сотрудников": ["сотрудник", "сотрудника", "сотрудников"],
    "договоров": ["договор", "договора", "договоров"],
    "правил замещения": ["правило замещения", "правила замещения",
                         "правил замещения"],
  };
  return Object.keys(made || {}).filter(function (k) { return made[k]; })
    .map(function (k) {
      var f = forms[k];
      return made[k] + " " + (f ? px(made[k], f[0], f[1], f[2]) : k);
    }).join(" · ");
}

/* Предпросмотр: книга — таблицей, PDF — как есть, остальное — тем текстом,
   который увидел разборщик. Последнее важнее всего там, где текст оказался
   кашей: причина отказа видна глазами, а не со слов агента. */
function preview(d) {
  var p = d["предпросмотр"] || {};
  var head = '<div class="grp"><h4>Предпросмотр</h4>';

  if (p["вид"] === "документ") {
    return head + '<iframe class="pdf" src="/api/document/' + d.id +
           '/file" title="Предпросмотр документа"></iframe></div>';
  }
  if (p["вид"] === "книга") {
    var names = p["листы"] || [];
    // У формы РКМ тридцать листов: рядом вкладок это стена в пол-панели.
    // Выбор списком занимает одну строку и не растет с числом листов.
    var pick = names.length > 1
      ? '<label class="spick">Лист <select id="sheetpick">' +
        names.map(function (n) {
          return '<option value="' + esc(n) + '">' + esc(n) + "</option>";
        }).join("") + "</select>" +
        '<span class="c">' + names.length + " " +
        px(names.length, "лист", "листа", "листов") + "</span></label>"
      : "";
    return head + pick +
           '<div class="sheetbox loading"><div class="sload">Готовлю лист…</div>' +
           '<iframe class="sheetview" src="/api/document/' + d.id +
           "/preview?sheet=" + encodeURIComponent(names[0] || "") +
           '" title="Предпросмотр листа"></iframe></div></div>';
  }
  if (p["вид"] === "текст") {
    // Абзацами и обычным шрифтом, а не моноширинным полотном: приказ и
    // положение об оплате труда — это проза, ее читают, а не разбирают по
    // колонкам. Пустые строки схлопываем: в вордовских файлах их подряд
    // бывает по нескольку.
    var paras = p["текст"].split("\n").map(function (s) { return s.trim(); })
      .filter(Boolean);
    return head + '<div class="ptext">' +
           paras.map(function (s) { return "<p>" + esc(s) + "</p>"; }).join("") +
           "</div>" +
           (p["обрезано"] ? '<div class="more">показано начало документа</div>' : "") +
           "</div>";
  }
  return head + '<div class="none">' + esc(p["почему"] || "показать нечего") +
         "</div></div>";
}

function bytes(n) {
  if (!n) return "—";
  if (n < 1024) return n + " Б";
  if (n < 1024 * 1024) return Math.round(n / 1024) + " КБ";
  return (n / 1024 / 1024).toFixed(1).replace(".", ",") + " МБ";
}

function meta(pairs) {
  return '<dl class="meta">' + pairs.filter(function (p) {
      return p[1] !== null && p[1] !== undefined && p[1] !== "";
    })
    .map(function (p) {
      // Значение — текст; готовая разметка передается как {v: html}.
      var v = (p[1] && typeof p[1] === "object") ? p[1].v : esc(p[1]);
      return "<dt>" + esc(p[0]) + "</dt><dd>" + v + "</dd>";
    }).join("") + "</dl>";
}

function openDocument(id) {
  // Две карточки одна поверх другой ни к чему: щелчок по второму имени
  // заменяет первую.
  var open = document.querySelector(".drawer");
  if (open) open.remove();

  var back = document.createElement("div");
  back.className = "drawer";
  // Ширину помним между открытиями: у форм РКМ два десятка колонок, и если
  // экономист раз развернул панель, следующий документ он смотрит так же.
  try {
    if (localStorage.getItem("docwide") === "1") back.classList.add("wide");
  } catch (e) { /* приватный режим — обойдемся без памяти */ }
  back.innerHTML = '<div class="scrim"></div><aside class="panel" role="dialog" ' +
                   'aria-modal="true"><div class="none">Загружаю…</div></aside>';
  document.body.appendChild(back);

  function close() {
    document.removeEventListener("keydown", onKey);
    back.remove();
  }
  function onKey(e) { if (e.key === "Escape") close(); }
  document.addEventListener("keydown", onKey);
  back.querySelector(".scrim").onclick = close;

  api("/api/document/" + id).then(function (d) {
    // Карточка одинакова у всех документов: свойства, что внесено в реестр,
    // предпросмотр. Раньше у договорных документов сюда же дорисовывались
    // таблицы сотрудников и договоров — те же самые строки, что и в реестре,
    // только вторым экземпляром: и разнобой, и дублирование. Сколько строк
    // документ дал, видно здесь, а сами строки — на своей вкладке реестра.
    var made = [];
    if (d.employees.length) made.push([d.employees.length, "сотрудник",
                                       "сотрудника", "сотрудников", "emp"]);
    if (d.contracts.length) made.push([d.contracts.length, "договор",
                                       "договора", "договоров", "ctr"]);
    if (d.substitutions.length) made.push([d.substitutions.length,
                                           "правило замещения",
                                           "правила замещения",
                                           "правил замещения", "sub"]);
    var got = '<div class="grp"><h4>Внесено в реестр</h4>' +
      (made.length
        ? '<div class="made">' + made.map(function (m) {
            return '<button type="button" class="mtag" data-rtab2="' + m[4] + '">' +
                   m[0] + " " + px(m[0], m[1], m[2], m[3]) + "</button>";
          }).join("") + "</div>"
        : '<div class="none">' + esc(d.summary || "строк реестра нет") + "</div>") +
      "</div>";
    // Предложения — до подтверждения, поэтому идут первыми и отдельно от
    // того, что уже в реестре: смешать их значило бы стереть разницу между
    // «проверено» и «модель так прочитала».
    var prop = "";
    if (d.proposals && d.proposals.length) {
      var byEntity = {};
      d.proposals.forEach(function (p) {
        (byEntity[p.entity] = byEntity[p.entity] || []).push(p);
      });
      // Оценка у каждой строки — куда смотреть в первую очередь. Сомнительные
      // идут первыми и не отмечены: принять их можно только рукой.
      var GR = { "сомнительно": 0, "проверить": 1, "надежно": 2 };
      var gsum = { "надежно": 0, "проверить": 0, "сомнительно": 0 };
      d.proposals.forEach(function (p) { if (p.grade in gsum) gsum[p.grade]++; });
      var gline = ["надежно", "проверить", "сомнительно"].filter(function (g) {
        return gsum[g];
      }).map(function (g) {
        return '<span class="gr g' + GR[g] + '">' + g + " " + gsum[g] + "</span>";
      }).join("");
      prop = '<div class="grp ask"><h4>Ждет подтверждения</h4>' +
        '<p class="hint">Документ не похож ни на одну знакомую форму, поэтому ' +
        'прочитан как есть. В реестр и в расчет эти строки не попадут, пока ' +
        'вы их не примете.' +
        (gsum["сомнительно"] ? " Сомнительные строки не отмечены: у них не " +
         "сошлось что-то с реестром или справочником." : "") + "</p>" +
        (gline ? '<div class="gline">' + gline + "</div>" : "") +
        Object.keys(byEntity).map(function (ent) {
          var rows = byEntity[ent].slice().sort(function (a, b) {
            return (GR[a.grade] == null ? 2 : GR[a.grade]) -
                   (GR[b.grade] == null ? 2 : GR[b.grade]);
          });
          return '<div class="pgrp"><h5>' + esc(ent) + "</h5>" +
            rows.map(function (p) {
              var g = p.grade in GR ? p.grade : "надежно";
              return '<label class="prow"><input type="checkbox" data-prop="' +
                p.id + '"' + (g === "сомнительно" ? "" : " checked") +
                '><span class="pv">' +
                esc(Object.keys(p.fields)
                      .filter(function (k) { return p.fields[k] != null && p.fields[k] !== ""; })
                      .map(function (k) { return p.fields[k]; }).join(" · ")) +
                '<span class="gr g' + GR[g] + '">' + g + "</span></span>" +
                (p.reason || p.evidence
                  ? '<span class="pw">' + esc(p.reason || "") +
                    (p.reason && p.evidence ? " — " : "") + esc(p.evidence || "") + "</span>"
                  : "") +
                "</label>";
            }).join("") + "</div>";
        }).join("") +
        '<div class="pacts"><button type="button" class="take">Принять выбранные' +
        '</button><button type="button" class="drop">Отклонить остальные</button>' +
        "</div></div>";
    }

    // Пересказ агента — «извлечено величин 94, величины: оклад» — это его
    // телеметрия, а не документ. Экономисту нужен сам документ: заглянуть и
    // убедиться, что разобрано именно то. Поэтому здесь предпросмотр, а
    // подробности работы остаются в ленте агентов.
    got += preview(d);

    // Разговор о документе. «Тут ошибка» говорят там, где ошибку видят, —
    // в карточке, а не в ленте плана. Правка уходит в реестр, в память агента
    // и в стенд.
    var talk = d["переписка"] || [];
    got += '<div class="grp"><h4>Разговор о документе</h4>' +
      '<div class="dtalk">' + (talk.length ? talk.map(function (m) {
        return '<div class="dm ' + (m["кто"] === "экономист" ? "me" : "ag") + '">' +
               '<div class="dmt">' + esc(m["текст"]) + "</div>" +
               '<div class="dmw">' + esc(m["когда"] || "") + "</div></div>";
      }).join("") : '<div class="none">Скажите, что разобрано неверно — агент ' +
                    "поправит и запомнит.</div>") + "</div>" +
      '<form class="dask"><input type="text" placeholder="Например: у Петрова оклад ' +
      '90 000, а не 60 000" autocomplete="off">' +
      '<button type="submit">Отправить</button></form></div>';

    back.querySelector(".panel").innerHTML =
      '<header class="dhead"><h3>' + esc(d.name) + "</h3>" +
        '<button type="button" class="wider" aria-label="Развернуть" ' +
        'title="Развернуть на весь экран">' + ICON_WIDE + "</button>" +
        '<button type="button" class="x" aria-label="Закрыть">×</button></header>' +
      '<div class="dbody">' +
        meta([["Вид", d.kind], ["Статус", docStatus(d.state).text],
              ["Версия", d["версия"] > 1
                 ? d["версия"] + (d["заменяет"] ? ", прежняя от " + d["заменяет"] : "")
                 : null],
              ["Формат", d["формат"]], ["Размер", bytes(d.size)],
              ["Загружен", d.uploaded]]) +
        (d.exists
          ? '<a class="dfile" href="/api/document/' + d.id +
            '/file" target="_blank" rel="noopener">Открыть файл</a>'
          : '<div class="none">Файла нет на диске</div>') +
        prop + got +
      "</div>" +
      '<footer class="dfoot"><button type="button" class="del">Удалить документ</button>' +
      "</footer>";

    back.querySelector(".x").onclick = close;

    var form = back.querySelector(".dask");
    if (form) form.addEventListener("submit", function (e) {
      e.preventDefault();
      var inp = form.querySelector("input"), text = inp.value.trim();
      if (!text) return;
      form.querySelector("button").disabled = true;
      api("/api/document/" + d.id + "/message", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text }),
      }).then(function () {
        openDocument(d.id);            // карточка перерисуется с ответом
        loadRegistry();
      }).catch(function () { form.querySelector("button").disabled = false; });
    });

    back.addEventListener("click", function (e) {
      var t = e.target.closest("[data-rtab2]");
      if (!t) return;
      close();
      regTab = t.getAttribute("data-rtab2");
      if (!inRegistry) openRegistry(); else renderRegistry();
    });

    var wider = back.querySelector(".wider");
    wider.onclick = function () {
      var on = back.classList.toggle("wide");
      wider.title = on ? "Свернуть" : "Развернуть на весь экран";
      wider.setAttribute("aria-label", on ? "Свернуть" : "Развернуть");
      try { localStorage.setItem("docwide", on ? "1" : "0"); } catch (e) {}
    };
    if (back.classList.contains("wide")) {
      wider.title = "Свернуть";
      wider.setAttribute("aria-label", "Свернуть");
    }

    // Лист переключаем перезагрузкой рамки: разметку собирает сервер.
    var frame = back.querySelector(".sheetview");
    if (frame) {
      frame.addEventListener("load", function () {
        var box = frame.closest(".sheetbox");
        if (box) box.classList.remove("loading");
      });
    }
    var pickEl = back.querySelector("#sheetpick");
    if (pickEl) {
      pickEl.onchange = function () {
        var box = frame.closest(".sheetbox");
        if (box) box.classList.add("loading");
        frame.src = "/api/document/" + d.id + "/preview?sheet=" +
                    encodeURIComponent(this.value);
      };
    }

    var take = back.querySelector(".take");
    if (take) {
      var decide = function (acceptChecked) {
        var boxes = Array.prototype.slice.call(
          back.querySelectorAll("[data-prop]"));
        var accept = [], reject = [];
        boxes.forEach(function (b) {
          var id = +b.getAttribute("data-prop");
          if (acceptChecked && b.checked) accept.push(id);
          else reject.push(id);
        });
        if (!accept.length && !reject.length) return;
        api("/api/document/" + d.id + "/proposals", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ accept: accept, reject: reject }),
        }).then(function () {
          close();
          loadRegistry();
          tick();
        });
      };
      take.onclick = function () { decide(true); };
      back.querySelector(".drop").onclick = function () {
        ask("Отклонить непринятые строки?",
            "Они исчезнут из карточки. Документ останется, его можно " +
            "загрузить заново.", "Отклонить").then(function (yes) {
          if (yes) decide(false);
        });
      };
    }
    back.querySelector(".del").onclick = function () {
      close();
      removeDoc(d.id, d.name, function () { loadRegistry(); tick(); });
    };
  }).catch(function (e) {
    back.querySelector(".panel").innerHTML =
      '<div class="none">' + esc(e.message || "не загрузилось") + "</div>";
  });
}

/* Правая колонка показывает документы и агентов открытого плана. На реестре
   и на странице агентов плана нет, а ширина нужна: у договора четырнадцать
   граф. Прячем на время и возвращаем, как было, при возврате к плану. */
var sideWasOpen = null;

function wideScreen(on) {
  var shell = document.querySelector(".shell");
  if (on) {
    if (sideWasOpen === null) sideWasOpen = !shell.classList.contains("noside");
    shell.classList.add("noside");
    $("toggleside").classList.add("off");
  } else if (sideWasOpen !== null) {
    shell.classList.toggle("noside", !sideWasOpen);
    $("toggleside").classList.toggle("off", !sideWasOpen);
    sideWasOpen = null;
  }
}

/* ── страница агентов ─────────────────────────────────────────
 * Схему из семи коробок со стрелками рисовать незачем: это картинка замысла,
 * а не состояния, и половина коробок в этой сборке не существует. Страница
 * отвечает на вопросы, которые у экономиста есть на самом деле:
 *
 *   что от меня ждут — вопросы агентов и документы, ждущие подтверждения,
 *                      собранные по всем планам в одно место;
 *   что уже сделано и на чем основано — журнал работ с артефактами: для ГОЗ
 *                      это ответ на «откуда эта цифра»;
 *   что не работает — агенты, которых в сборке нет, названы прямо, чтобы
 *                      экономист не ждал от них результата.
 */
var inAgents = false, agentData = null, openWork = {};

function openAgents() {
  inAgents = true;
  leaveRegistry();
  wideScreen(true);
  setView("feed");
  $("feed").hidden = true;
  $("view").hidden = true;
  $("regview").hidden = true;
  $("agentview").hidden = false;
  document.querySelector(".tabs").hidden = true;
  document.querySelector(".phead").hidden = true;
  document.querySelector(".composer").hidden = true;
  $("openagents").classList.add("on");
  $("openreg").classList.remove("on");
  Array.prototype.forEach.call(document.querySelectorAll(".case"), function (el) {
    el.classList.remove("on");
  });
  $("agentview").innerHTML = '<div class="none">Загружаю…</div>';
  api("/api/agents").then(function (d) {
    agentData = d;
    renderAgents();
  }).catch(function (e) {
    $("agentview").innerHTML = '<div class="none">' +
      esc(e.message || "не загрузилось") + "</div>";
  });
}

function leaveAgents() {
  if (!inAgents) return;
  inAgents = false;
  wideScreen(false);
  $("agentview").hidden = true;
  document.querySelector(".tabs").hidden = false;
  document.querySelector(".phead").hidden = false;
  document.querySelector(".composer").hidden = false;
  $("openagents").classList.remove("on");
}

/* Карточка плана: пять этапов чипами, под ними — что мешает. Щелчок по
   этапу раскрывает его строки и действие. Цвет чипа — состояние, а не
   украшение: стоит, внимание, готово, не начато. */
var STAGE_CLS = { "готово": "ok", "внимание": "warn", "стоит": "bad", "не начато": "off" };

function planCard(p) {
  var opened = openWork["p:" + p.id];
  var chips = p["этапы"].map(function (s, i) {
    return '<button type="button" class="stage ' + STAGE_CLS[s["состояние"]] +
           (opened === i ? " on" : "") + '" data-stage="' + p.id + ":" + i + '">' +
           '<span class="sdot"></span>' + esc(s["имя"]) + "</button>";
  }).join('<span class="sarrow">›</span>');
  var detail = "";
  if (opened != null && p["этапы"][opened]) {
    var s = p["этапы"][opened];
    var act = s["действие"];
    detail = '<div class="sdetail">' +
      '<div class="ssum">' + esc(s["итог"]) + "</div>" +
      (s["строки"].length
        ? '<div class="srows">' + s["строки"].map(function (r) {
            return '<div class="srow' + (r["внимание"] ? " warn" : "") + '"' +
              (r["document_id"] ? ' data-doc2="' + r["document_id"] + '"' : "") +
              (r["case_id"] ? ' data-case="' + r["case_id"] + '"' : "") + ">" +
              esc(r["текст"]) +
              (r["состояние"] ? ' <span class="c">' + esc(r["состояние"]) + "</span>" : "") +
              "</div>";
          }).join("") + "</div>"
        : "") +
      (act ? '<button type="button" class="sact" data-go="' + esc(act["куда"]) + '"' +
             (act["document_id"] ? ' data-doc2="' + act["document_id"] + '"' : "") +
             (act["case_id"] ? ' data-case="' + act["case_id"] + '"' : "") +
             (act["вкладка"] ? ' data-tab="' + esc(act["вкладка"]) + '"' : "") + ">" +
             esc(act["текст"]) + "</button>" : "") +
      "</div>";
  }
  return '<div class="plan"><div class="ph"><span class="pt">' + esc(p["план"]) +
    "</span>" + '<span class="py">' + p["год"] + "</span></div>" +
    '<div class="stages">' + chips + "</div>" +
    (p["мешает"]
      ? '<div class="pblock"><b>' + esc(p["мешает_этап"]) + ":</b> " + esc(p["мешает"]) + "</div>"
      : '<div class="pok">Ничего не мешает — план на этапе «' + esc(p["этап"]) + "»</div>") +
    detail + "</div>";
}

function renderAgents() {
  var d = agentData;
  if (!d) return;                       // еще не загружено или сервер недоступен
  var wait = d["ждет"] || [];

  var head = '<div class="vh">Где каждый план сейчас и что мешает ему дойти до ' +
    "результата. Подробности и журнал — ниже, по желанию.</div>";

  // Блок «требует вас» идет первым и только если есть что: пустой блок с
  // надписью «ничего не ждет» — та же реклама, что и схема со стрелками.
  var need = "";
  if (wait.length) {
    need = '<div class="grp need"><h4>Ждет вашего решения<span class="c">' +
      wait.length + "</span></h4>" +
      wait.map(function (w) {
        return '<div class="wrow" ' +
          (w["case_id"] ? 'data-case="' + w["case_id"] + '"' : "") +
          (w["document_id"] ? 'data-doc2="' + w["document_id"] + '"' : "") +
          '><div class="wt">' + esc(w["текст"]) + "</div>" +
          '<div class="wm">' +
          esc(w["агент"] || w["документ"] || "") +
          (w["план"] ? " · " + esc(w["план"]) : "") +
          (w["строк"] ? " · " + w["строк"] + " " +
           px(w["строк"], "строка", "строки", "строк") : "") +
          "</div></div>";
      }).join("") + "</div>";
  }

  // Верхний уровень — след по документу. В строке: документ, сколько шагов,
  // чем кончилось, что внесено. Провалиться можно в шаги, из шага — в
  // артефакт. Все общение агентов подряд экономисту не нужно: нужно «что
  // стало с моим документом».
  var traces = d["следы"] || [];
  var trail = '<div class="grp"><h4>По документам</h4>' +
    (traces.length ? traces.map(function (t) {
      var open = openWork["t:" + t["документ"]];
      var forms = {
        "сотрудников": ["сотрудник", "сотрудника", "сотрудников"],
        "договоров": ["договор", "договора", "договоров"],
        "правил замещения": ["правило замещения", "правила замещения", "правил замещения"],
        "строк трудоемкости": ["строка трудоемкости", "строки трудоемкости", "строк трудоемкости"],
        "поступлений": ["поступление", "поступления", "поступлений"],
      };
      var made = Object.keys(t["внесено"] || {}).filter(function (k) {
        return t["внесено"][k];
      }).map(function (k) {
        var n = t["внесено"][k], f = forms[k];
        return n + " " + (f ? px(n, f[0], f[1], f[2]) : k);
      }).join(" · ");
      var st = docStatus(t["статус"]);
      var bad = t["шаги"].some(function (s) { return s["состояние"] === "ошибка"; });
      return '<div class="trace' + (open ? " open" : "") + '" data-trace="' +
        esc(t["документ"]) + '">' +
        '<div class="th"><span class="st ' + (bad ? "bad" : st.cls) + '">' +
        esc(bad ? "Ошибка" : st.text) + "</span>" +
        '<span class="tn">' + esc(t["документ"]) + "</span>" +
        '<span class="td">' + t["шаги"].length + " " +
        px(t["шаги"].length, "шаг", "шага", "шагов") +
        (made ? " · " + esc(made) : "") +
        (t["ждет"] ? ' · <b>ждет подтверждения ' + t["ждет"] + "</b>" : "") +
        "</span></div>" +
        (open ? '<div class="steps">' + t["шаги"].map(function (s) {
          var so = openWork[s.id];
          return '<div class="step' + (so ? " open" : "") + '" data-step="' + s.id + '">' +
            '<div class="sh"><span class="sdot ' +
            (s["состояние"] === "ошибка" ? "bad" : s["состояние"] === "готово" ? "ok" : "wait") +
            '"></span><span class="sn">' + esc(s["что"]) + "</span>" +
            '<span class="sd">' + esc(s["агент"]) +
            (s["секунд"] ? " · " + s["секунд"] + " с" : "") + "</span></div>" +
            (s["подробность"] ? '<div class="sdet">' + esc(s["подробность"]) + "</div>" : "") +
            (so && s["артефакт"].length
              ? meta(s["артефакт"].map(function (kv) {
                  var v = kv[1];
                  return [kv[0], Array.isArray(v) ? v.join(", ")
                               : (typeof v === "boolean" ? (v ? "да" : "нет") : v)];
                }))
              : "") + "</div>";
        }).join("") + "</div>" : "") +
        "</div>";
    }).join("") : '<div class="none">Документов еще не было</div>') + "</div>";

  var work = d["работы"] || [];
  var journal = '<div class="grp"><h4>Все работы подряд</h4>' +
    (work.length
      ? work.map(function (w) {
          var open = openWork[w.id];
          return '<div class="arow' + (open ? " open" : "") + '" data-work="' +
            w.id + '">' +
            '<div class="ah"><span class="an">' + (w["номер"] || "—") + "</span>" +
            '<span class="at">' + esc(w["что"]) + "</span>" +
            '<span class="ad">' + esc(w["агент"]) +
            (w["план"] ? " · " + esc(w["план"]) : "") +
            (w["секунд"] ? " · " + w["секунд"] + " с" : "") + "</span>" +
            (w["артефакт"].length
              ? '<span class="acount">' + w["артефакт"].length + "</span>" : "") +
            "</div>" +
            (w["подробность"]
              ? '<div class="adet">' + esc(w["подробность"]) + "</div>" : "") +
            (open && w["артефакт"].length
              ? meta(w["артефакт"].map(function (kv) {
                  var v = kv[1];
                  return [kv[0], Array.isArray(v) ? v.join(", ")
                               : (typeof v === "boolean" ? (v ? "да" : "нет") : v)];
                }))
              : "") +
            "</div>";
        }).join("")
      : '<div class="none">Агенты еще не работали</div>') + "</div>";

  // Нереализованных называем прямо: иначе экономист ждет результата от того,
  // чего в сборке нет.
  var off = (d["агенты"] || []).filter(function (a) { return !a["работает"]; });
  var missing = off.length
    ? '<div class="grp"><h4>Не работает в этой сборке</h4>' +
      off.map(function (a) {
        return '<div class="arow off"><div class="ah">' +
          '<span class="an">' + a["номер"] + "</span>" +
          '<span class="at">' + esc(a["имя"]) + "</span>" +
          '<span class="ad">' + esc(a["делает"]) + "</span></div></div>";
      }).join("") + "</div>"
    : "";

  // Сводка → подробности → журнал. Большинство остановится на сводке, и
  // это правильно: ей и должно хватать. Подробности и журнал свернуты.
  var plans = (d["планы"] || []).map(planCard).join("") ||
              '<div class="none">Планов пока нет</div>';
  var more = function (key, title, html) {
    var open = !!openWork[key];
    return '<div class="fold' + (open ? " open" : "") + '" data-fold="' + key + '">' +
           '<div class="fh">' + esc(title) + "</div>" +
           (open ? html : "") + "</div>";
  };
  $("agentview").innerHTML = head + need + plans +
    more("f:trail", "Подробности по документам", trail) +
    more("f:journal", "Журнал всех работ", journal) +
    more("f:missing", "Что не работает в этой сборке", missing);
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

//: Что принимаем на загрузку — ровно то, что читает docread. Один список на
//: скрепку в ленте и на кнопку в реестре: расходиться им незачем, а обещать
//: формат, который потом «не прочитан», — тем более.
var UPLOAD_ACCEPT = ".xlsx,.xlsm,.xls,.pdf,.doc,.docx";

//: Документы, загруженные в этот заход: за ними следим до тех пор, пока
//: экономист не закроет полосу.
var fresh = [];

/* Что стало с только что загруженным — прямо в реестре, без ухода в ленту
   плана. Пока агент читает, здесь видно «в обработке»; когда прочитал —
   что он взял и что просит подтвердить, с переходом в карточку документа.
   Разбираться с документом надо там, где он открыт: в карточке и предпросмотр,
   и извлеченные строки, и разговор с агентом о правках. */
//: Состояния, при которых документ ждет человека, а не агента.
var FRESH_ACT = {
  "ждет подтверждения": "Проверить строки",
  "не прочитан": "Посмотреть",
  "текст нечитаемый": "Посмотреть",
  "не распознан": "Посмотреть",
};

function freshStrip(docs) {
  if (!fresh.length) return "";
  var rows = fresh.map(function (id) {
    return docs.filter(function (d) { return d.id === id; })[0];
  }).filter(Boolean);
  var busy = rows.filter(function (d) { return d.state === "ожидает"; });
  var need = rows.filter(function (d) { return FRESH_ACT[d.state]; });
  // Разобранное без вопросов уходит из полосы само. Плашка о законченной
  // работе, которую надо закрывать рукой, читается как невыполненное
  // требование: человек ищет, что от него хотят, а хотеть уже нечего.
  // Остается только то, что еще читается или ждет решения.
  rows = busy.concat(need);
  fresh = rows.map(function (d) { return d.id; });
  if (!rows.length) return "";
  return '<div class="fresh' + (need.length ? " act" : "") + '"><div class="fh">' +
    (busy.length ? "Агент читает загруженное" : "Требует вашего решения") +
    '<button type="button" class="fx" id="freshclose">Скрыть</button></div>' +
    rows.map(function (d) {
      var s = docStatus(d.state);
      var act = FRESH_ACT[d.state];
      return '<div class="frow"><span class="fn">' + esc(d.name) + "</span>" +
        '<span class="st ' + s.cls + '">' + esc(s.text) + "</span>" +
        '<span class="fp">' + (produced(d.produced) || "") + "</span>" +
        '<button type="button" class="fopen' + (act ? " go" : "") +
        '" data-fresh="' + d.id + '">' + (act || "Открыть") + "</button></div>";
    }).join("") +
    (need.length
      ? '<div class="fnote">Откройте документ: там видно, что из него взято, ' +
        "и есть разговор с агентом — напишите, если разобрано неверно, он " +
        "поправит реестр и запомнит правку.</div>"
      : "") + "</div>";
}

//: Короткие имена месяцев для графика поступлений.
var MONTHS = ["янв", "фев", "мар", "апр", "май", "июн",
              "июл", "авг", "сен", "окт", "ноя", "дек"];

var REG_TABS = [
  { key: "docs", title: "Документы" },
  { key: "ctr", title: "Договоры" },
  { key: "emp", title: "Штатное расписание" },
  { key: "labor", title: "Трудоемкость" },
  { key: "inflow", title: "Поступления" },
  { key: "secret", title: "Надбавка 120" },
  { key: "ref", title: "Справочник должностей" },
  { key: "sub", title: "Правила замещения" },
];

function openRegistry() {
  leaveAgents();
  inRegistry = true;
  // Правая колонка — контекст плана: на реестре она не к месту и забирает
  // 320 пикселей ширины, которых таблицам как раз не хватает.
  wideScreen(true);
  setView("feed");
  $("feed").hidden = true;
  $("view").hidden = true;
  $("regview").hidden = false;
  document.querySelector(".tabs").hidden = true;
  document.querySelector(".phead").hidden = true;
  document.querySelector(".composer").hidden = true;
  $("openreg").classList.add("on");
  Array.prototype.forEach.call(document.querySelectorAll(".case"), function (el) {
    el.classList.remove("on");
  });
  loadRegistry();
}

function leaveRegistry() {
  if (!inRegistry) return;
  inRegistry = false;
  wideScreen(false);
  picked = {};
  refreshPickBar();
  $("regview").hidden = true;
  document.querySelector(".tabs").hidden = false;
  document.querySelector(".phead").hidden = false;
  document.querySelector(".composer").hidden = false;
  $("openreg").classList.remove("on");
}

function loadRegistry(quiet) {
  // Обновление на месте (пока разбирается загруженный файл) не должно
  // мигать «Загружаю…» на весь экран: таблица просто перерисуется.
  if (!quiet) $("regview").innerHTML = '<div class="none">Загружаю…</div>';
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
    // Реестр документов, а не заметки по файлам: графы формальные, с закрытым
    // содержимым. «Что дал» держал в одной клетке и счетчики, и состояние, и
    // английскую диагностику библиотеки — свалка, которой в продукте для
    // финансиста быть не должно. Разведено надвое: «статус» из закрытого
    // перечня и «внесено в реестр» — только счетчики. Вид документа убран:
    // он нужен сервису, чтобы выбрать разборщик, а в списке не говорит
    // ничего; остался в карточке.
    // Номер виден всегда, флажок подменяет его при наведении: ряд пустых
    // квадратиков в спокойном состоянии — лишний шум, а нумерация нужна,
    // чтобы сослаться на строку.
    var pg = paged("docs", docs);
    // «Отметить все» относится к текущей странице — так в почте и в списках
    // задач: отмечать вслепую то, чего не видно, опасно. Отметки со страницы
    // на страницу при этом не теряются.
    var allOn = pg.rows.length > 0 && pg.rows.every(function (x) { return picked[x.id]; });
    var nPick = pickedIds().length;
    var pickBox = { v: pk('<input type="checkbox" class="pickall"' +
                          (allOn ? " checked" : "") + ">", "№"), cls: "pick" };
    body = table(
        nPick
        ? [pickBox, { v: pickCell(nPick), cls: "pickcell", span: 5 }]
        : [pickBox, "документ", "загружен", "статус", "внесено в реестр", ""],
        pg.rows.map(function (x, i) {
          var s = docStatus(x.state);
          return [
            { v: pk('<input type="checkbox" data-pick="' + x.id + '"' +
                    (picked[x.id] ? " checked" : "") + ">",
                    String(pg.from + i + 1)),
              cls: "pick" },
            { v: '<span class="dname" data-doc="' + x.id + '" role="button" ' +
                 'tabindex="0">' + esc(x.name) + "</span>" },
            x.uploaded,
            { v: '<span class="st ' + s.cls + '">' + esc(s.text) + "</span>",
              cls: "status" },
            { v: produced(x.produced) || "—" },
            { v: rowMenu("data-docmenu", x.id), cls: "act" }];
        }), "",
        { plain: true, headCls: nPick ? "picking" : "",
          // Имя документа тянется, остальное — по содержимому.
          widths: ["46px", "", "118px", "152px", "178px", "44px"],
          rowCls: function (i) { return picked[pg.rows[i].id] ? "sel" : ""; } }) +
        pager("docs", pg);
    body = freshStrip(docs) + body;
    if (!docs.length) {
      body = '<div class="empty2"><b>Документов пока нет</b>' +
        "<p>Перетащите сюда файлы или нажмите «Загрузить документы». " +
        "Подойдет любой документ: штатное расписание, договор, расчетно-" +
        "калькуляционные материалы, приказ, положение. Агент прочитает и " +
        "разложит по реестру, а спорное покажет на подтверждение.</p></div>";
    }
  } else if (regTab === "ctr") {
    var pgc = paged("ctr", d.contracts || []);
    // Графы те же, что во входном файле решателя: счет решает, можно ли
    // платить 120 без оклада, приоритет — на каком договоре держать оклад,
    // а основное место и совместительство — можно ли открыть ставку.
    body = table([{ v: "Шифр", cls: "key" }, "наименование", "номер", "вид",
                  "счет", "ГОЗ",
                  { t: "фонд, ₽" }, "срок", "разрешенные выплаты", "приоритет",
                  "основное", "совмест.", "оклад до", "надбавки до"],
      pgc.rows.map(function (x) {
        var term = [x.from, x.to].filter(Boolean).join(" — ");
        return [{ v: esc(x.code), cls: "key" }, x.name, x.number, x.kind, x.account,
                { v: x.goz ? '<span class="tag goz">' + esc(x.goz) + "</span>" : null },
                { v: x.fund == null ? null : mo(x.fund), cls: "n" },
                term, x.kinds, x.priority, x.allow_main, x.allow_part,
                x.salary_deadline, x.allowance_deadline];
      }),
      "", { startNum: pgc.from }) + pager("ctr", pgc);
  } else if (regTab === "emp") {
    var pge = paged("emp", d.employees || []);
    // Тип и категория занятости решают, можно ли открыть вторую ставку и до
    // какого предела: у студента и аспиранта он свой. Раньше их не было
    // видно, потому что в расчет уходило жестко «основное» и «основной».
    body = table([{ v: "Табельный", cls: "key" }, "ФИО", "должность",
                  "подразделение",
                  { t: "ставка" }, "занятость", "категория", { t: "зарплата, ₽" },
                  "срок", "разрешены", "запрещены"],
      pge.rows.map(function (x) {
        var term = [x.from, x.to].filter(Boolean).join(" — ");
        return [{ v: esc(x.code), cls: "key" }, x.fio, x.position, x.department,
                { v: x.rate == null ? null : String(x.rate).replace(".", ","), cls: "n" },
                x.employment, x.category,
                { v: x.salary == null ? null : mo(x.salary), cls: "n" },
                term, x.allowed, x.forbidden];
      }),
      "", { startNum: pge.from }) + pager("emp", pge);
  } else if (regTab === "labor") {
    var pgl = paged("labor", d.labor || []);
    body = table(["договор", { t: "год" }, "должность", "окладная группа",
                  { t: "чел.-мес." }, { t: "средняя стоимость, ₽" }],
      pgl.rows.map(function (x) {
        var grp = [x.page, x.group, x.level].filter(function (v) {
          return v != null && v !== "";
        }).join(" · ");
        return [x.contract, { v: x.year == null ? null : String(x.year), cls: "n" },
                x.position, grp || null,
                { v: x.person_months == null ? null
                     : String(x.person_months).replace(".", ","), cls: "n" },
                { v: x.avg_cost == null ? null : mo(x.avg_cost), cls: "n" }];
      }),
      "План договора в человеко-месяцах и стоимость одного месяца. Главное " +
      "содержание расчетно-калькуляционных материалов и Формы 9д.",
      { startNum: pgl.from }) + pager("labor", pgl);
  } else if (regTab === "inflow") {
    // График поступлений читают строкой по месяцам, а не списком из двух
    // десятков записей: девятнадцать строк «договор, год, месяц, сумма» — это
    // та же матрица, разложенная в столбик, и по ней не видно ни провала в
    // середине года, ни того, когда договор начинается.
    var rows = d.inflows || [];
    var by = {}, years = {};
    rows.forEach(function (x) {
      var key = x.contract + "|" + (x.year == null ? "" : x.year);
      (by[key] = by[key] || {})[x.month] = x.amount;
      years[key] = [x.contract, x.year];
    });
    var keys = Object.keys(by).sort();
    // Наибольшее поступление задает длину полос: сравнивать месяцы имеет
    // смысл между собой, а не с чужой таблицей.
    var peak = 0;
    rows.forEach(function (x) { if (x.amount > peak) peak = x.amount; });
    var pgi = paged("inflow", keys);
    body = table(
      [{ v: "Договор", cls: "key" }, { t: "год" }].concat(
        MONTHS.map(function (m) { return { t: m }; }),
        [{ t: "всего, ₽" }]),
      pgi.rows.map(function (key) {
        var months = by[key], total = 0;
        var cells = MONTHS.map(function (m, i) {
          var v = months[i + 1];
          if (v == null) return { v: null, cls: "n" };
          total += v;
          // Полоса под числом: провал в середине года и месяц, с которого
          // договор начинается, видно раньше, чем прочитаны цифры.
          var share = peak ? Math.max(4, Math.round((v / peak) * 100)) : 0;
          return { v: '<span class="bar" style="--f:' + share + '%">' +
                      mo(v) + "</span>", cls: "n" };
        });
        return [{ v: esc(years[key][0]), cls: "key" },
                { v: years[key][1] == null ? null : String(years[key][1]), cls: "n" }]
          .concat(cells, [{ v: mo(total), cls: "n tot" }]);
      }),
      "Когда деньги приходят на договор. Решатель не может потратить их " +
      "раньше поступления; без графика фонд раскладывается ровно по месяцам " +
      "действия, и об этом говорится перед расчетом.",
      { startNum: pgi.from }) + pager("inflow", pgi);
  } else if (regTab === "secret") {
    var pgs2 = paged("secret", d.secret || []);
    body = table(["сотрудник", "договор секретности", { t: "ставка 120" }],
      pgs2.rows.map(function (x) {
        return [x.employee, x.contract,
                { v: x.rate == null ? null : String(x.rate).replace(".", ","),
                  cls: "n" }];
      }),
      "Кому платится 120 и какой договор задает период секретности. Сумма " +
      "считается процентом от оклада.",
      { startNum: pgs2.from }) + pager("secret", pgs2);
  } else if (regTab === "ref") {
    var pgr = paged("ref", d.reference || []);
    // Окладной группы здесь нет: взаимозаменяемость задают правила замещения,
    // а группировка из положения об оплате труда устарела и в решении больше
    // не участвует. БЭП, наоборот, показываем — по нему считается средний
    // предел по ГОЗ-договору, а раньше его было не видно.
    body = table(["должность", "категория",
                  { t: "оклад за 1,0 ставки, ₽" }, { t: "П2556, ₽" },
                  { t: "П4, ₽" }, { t: "БЭП, ₽" }, "примечание"],
      pgr.rows.map(function (x) {
        return [x.pos, x.cat,
                { v: x.sal == null ? null : mo(x.sal), cls: "n" },
                { v: x.p2556 == null ? null : mo(x.p2556), cls: "n" },
                { v: x.p4 == null ? null : mo(x.p4), cls: "n" },
                { v: x.bep == null ? null : mo(x.bep), cls: "n" }, x.note];
      }),
      "Прочерк — отдельной строки для должности в источнике нет.",
      { startNum: pgr.from }) + pager("ref", pgr);
  } else if (regTab === "sub") {
    var pgs = paged("sub", d.substitutions || []);
    body = table(["должность", "может быть замещена"],
      pgs.rows.map(function (x) { return [x.position, x.replaced_by]; }),
      "Правила направленные: кого кем можно заменить, не наоборот. " +
      "Уходят в расчет отдельным листом входного файла.",
      { startNum: pgs.from }) + pager("sub", pgs);
  }

  var tabsRow =
    '<div class="reghead"><div class="rtabs">' + REG_TABS.map(function (t) {
      var n = { docs: docs.length, ctr: (d.contracts || []).length,
                emp: (d.employees || []).length, labor: (d.labor || []).length,
                inflow: new Set((d.inflows || []).map(function (x) {
                  return x.contract + "|" + x.year;
                })).size,
                secret: (d.secret || []).length,
                ref: (d.reference || []).length,
                sub: (d.substitutions || []).length }[t.key];
      return '<button type="button" data-rtab="' + t.key + '"' +
             (regTab === t.key ? ' class="on"' : "") + ">" + esc(t.title) +
             (n ? '<span class="c"> ' + n + "</span>" : "") + "</button>";
    }).join("") + "</div>" +
    '<label class="regup" title="Или перетащите файлы в окно">' +
      '<input type="file" multiple accept="' + UPLOAD_ACCEPT + '" hidden>' +
      "<span>Загрузить документы</span></label></div>";

  $("regview").innerHTML = tabsRow + body;
  // Промежуточное состояние: отмечена часть строк.
  var head = document.querySelector("#regview .pickall");
  if (head) {
    var n = pickedIds().length, all = (regData.documents || []).length;
    head.indeterminate = n > 0 && n < all;
  }
  return;
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
    // Новые реплики, пока лента закрыта, отмечаем точкой на вкладке.
    if (lastMsgCount !== null && s.messages.length > lastMsgCount &&
        (view !== "feed" || inRegistry || inAgents)) {
      unseen += s.messages.length - lastMsgCount;
    }
    lastMsgCount = s.messages.length;
    markFeedTab();
    if (sig !== lastSig) {
      lastSig = sig;
      render();
      // Перерисовка ленты не должна выбрасывать из открытого раздела.
      if (inRegistry) {
        // Состав документов или их состояние изменились — реестр открыт и
        // должен это показать, иначе загруженный файл появится в нем только
        // после повторного входа.
        loadRegistry(true);
        $("feed").hidden = true; $("view").hidden = true; $("regview").hidden = false;
        document.querySelector(".tabs").hidden = true;
        document.querySelector(".phead").hidden = true;
        document.querySelector(".composer").hidden = true;
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

$("openagents").addEventListener("click", openAgents);

$("openreg").addEventListener("click", openRegistry);
if ($("toreg")) $("toreg").addEventListener("click", openRegistry);

document.addEventListener("keydown", function (e) {
  if (e.key === "Escape" && pickedIds().length && !document.querySelector(".modal")) {
    stopSelecting();
  }
});

document.addEventListener("click", function (e) {
  if (e.target.closest("#pickdel")) { removePicked(); return; }
  if (e.target.closest("#pickclear")) stopSelecting();
});

$("regview").addEventListener("change", function (e) {
  var per = e.target.closest("[data-per]");
  if (per) {
    var k = per.getAttribute("data-per");
    sizeOf[k] = +per.value;
    pageOf[k] = 1;
    renderRegistry();
    return;
  }
  // Перерисовывать таблицу на каждый щелчок нельзя: сбивается фокус и
  // теряются отметки при быстром выборе. Обновляем только панель и подсветку.
  var all = e.target.closest(".pickall");
  if (all) {
    // Только строки текущей страницы: их и видно.
    Array.prototype.forEach.call(document.querySelectorAll("#regview [data-pick]"),
      function (b) { picked[b.getAttribute("data-pick")] = all.checked; });
    refreshPickBar();
    return;
  }
  var one = e.target.closest("[data-pick]");
  if (one) {
    var id = one.getAttribute("data-pick");
    picked[id] = one.checked;
    refreshPickBar();
    // Перерисовка заменила узел — возвращаем фокус, иначе клавиатурой
    // не отметить вторую строку подряд.
    var back = document.querySelector('#regview [data-pick="' + id + '"]');
    if (back && document.activeElement === document.body) back.focus();
  }
});

$("agentview").addEventListener("click", function (e) {
  // Этап плана: раскрыть его строки. Действие этапа: уйти по назначению.
  var go = e.target.closest("[data-go]");
  if (go) {
    var where = go.getAttribute("data-go");
    if (where === "реестр") { openRegistry(); return; }
    if (where === "документ") { openDocument(+go.getAttribute("data-doc2")); return; }
    if (where === "план") {
      open(+go.getAttribute("data-case"));
      var tab = go.getAttribute("data-tab");
      if (tab) setTimeout(function () { setView(tab); }, 600);
      return;
    }
  }
  var st = e.target.closest("[data-stage]");
  if (st) {
    var parts = st.getAttribute("data-stage").split(":");
    var key = "p:" + parts[0], idx = +parts[1];
    openWork[key] = openWork[key] === idx ? null : idx;
    renderAgents();
    return;
  }
  var fold = e.target.closest("[data-fold]");
  if (fold && e.target.closest(".fh")) {
    var fk = fold.getAttribute("data-fold");
    openWork[fk] = !openWork[fk];
    renderAgents();
    return;
  }
  // Провал: документ → шаги → артефакт. Щелчок по шагу не должен сворачивать
  // документ, поэтому шаг проверяется первым.
  var step = e.target.closest("[data-step]");
  if (step) {
    var sid = +step.getAttribute("data-step");
    openWork[sid] = !openWork[sid];
    renderAgents();
    return;
  }
  var tr = e.target.closest("[data-trace]");
  if (tr) {
    var key = "t:" + tr.getAttribute("data-trace");
    openWork[key] = !openWork[key];
    renderAgents();
    return;
  }
  var row = e.target.closest("[data-work]");
  if (row) {
    var id = +row.getAttribute("data-work");
    openWork[id] = !openWork[id];
    renderAgents();
    return;
  }
  // Из «требует вас» уводим прямо к делу: к плану с вопросом или к карточке
  // документа с предложенными строками.
  var w = e.target.closest("[data-doc2]");
  if (w) {
    openDocument(+w.getAttribute("data-doc2"));
    return;
  }
  w = e.target.closest("[data-case]");
  if (w) open(+w.getAttribute("data-case"));
});

$("view").addEventListener("click", function (e) {
  var who = e.target.closest("[data-emp]");
  if (!who) return;
  var code = who.getAttribute("data-emp");
  var box = $("plandetail");
  if (!box) return;
  if (box.getAttribute("data-for") === code) {
    box.innerHTML = ""; box.removeAttribute("data-for"); return;
  }
  box.setAttribute("data-for", code);
  box.innerHTML = personDetail(code);
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
});

$("regview").addEventListener("keydown", function (e) {
  if (e.key !== "Enter" && e.key !== " ") return;
  var nm = e.target.closest(".dname");
  if (!nm) return;
  e.preventDefault();
  openDocument(+nm.getAttribute("data-doc"));
});

$("regview").addEventListener("click", function (e) {
  var pgb = e.target.closest("[data-pg]");
  if (pgb && !pgb.disabled) {
    var v = pgb.getAttribute("data-pg").split(":");
    pageOf[v[0]] = +v[1];
    renderRegistry();
    // Со второй страницы читают сверху, а не с того места, где нажали.
    $("regview").scrollTop = 0;
    return;
  }
  var t = e.target.closest("[data-rtab]");
  if (t) {
    regTab = t.getAttribute("data-rtab");
    if (regTab !== "docs") { picked = {}; refreshPickBar(); }
    renderRegistry();
    return;
  }

  var nm = e.target.closest(".dname");
  if (nm) {
    openDocument(+nm.getAttribute("data-doc"));
    return;
  }

  if (e.target.closest("#freshclose")) {
    fresh = [];
    renderRegistry();
    return;
  }
  var fo = e.target.closest("[data-fresh]");
  if (fo) {
    openDocument(+fo.getAttribute("data-fresh"));
    return;
  }

  var b = e.target.closest(".more");
  if (!b) return;
  var row = b.closest("tr");
  var name = row ? row.children[1].textContent : "документ";
  showMenu(b, [{ label: "Удалить документ", danger: true, run: function () {
    removeDoc(b.getAttribute("data-docmenu"), name, function () {
      loadRegistry(); tick();
    });
  } }]);
});

/* Страницы в разделах плана переключаются так же, как в реестре. */
$("view").addEventListener("click", function (e) {
  var b = e.target.closest("[data-pg]");
  if (!b || b.disabled) return;
  var v = b.getAttribute("data-pg").split(":");
  pageOf[v[0]] = +v[1];
  renderView();
  $("view").scrollTop = 0;
});

$("view").addEventListener("change", function (e) {
  var per = e.target.closest("[data-per]");
  if (!per) return;
  var k = per.getAttribute("data-per");
  sizeOf[k] = +per.value;
  pageOf[k] = 1;
  renderView();
});

$("docs").addEventListener("click", function (e) {
  var g = e.target.closest(".dgh");
  if (g) {
    var k = g.getAttribute("data-grp");
    openDocs[k] = !openDocs[k];
    render();
    return;
  }
  var b = e.target.closest(".more");
  if (!b) return;
  var card = b.closest(".doc"), name = card.querySelector(".nm").textContent;
  showMenu(b, [{ label: "Удалить документ", danger: true, run: function () {
    removeDoc(b.getAttribute("data-docmenu"), name, tick);
  } }]);
});

function removeDoc(id, name, done) {
  ask("Удалить «" + String(name).trim() + "»?",
      "Вместе с документом удалятся извлеченные из него данные: сотрудники, " +
      "договоры и правила замещения.", "Удалить документ").then(function (yes) {
    if (!yes) return;
    api("/api/document/" + id, { method: "DELETE" }).then(done || tick);
  });
}

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
  // Список файлов у поля живой: как только поле очищено, он пуст. Поэтому
  // сначала копия, потом очистка — иначе отправлять оказывалось нечего, и
  // выбор файла заканчивался ничем.
  var list = Array.prototype.slice.call(this.files);
  this.value = "";
  sendFiles(list);
});

/* Документы кладут в реестр — там они и живут. Раньше файлы принимала
   только лента плана: чтобы добавить документ, из реестра приходилось выйти
   в план и найти скрепку у поля сообщения. Теперь то же самое делается там,
   где человек и так находится: кнопкой в шапке реестра или перетаскиванием
   файлов в окно. Отправка одна на оба места. */
function sendFiles(list) {
  // Копия на входе: у поля выбора список живой и обнуляется вместе с полем.
  list = list ? Array.prototype.slice.call(list) : [];
  if (!list.length) return Promise.resolve();
  if (!caseId) {
    // Документ общий для организации, но разбирает его агент в рамках плана:
    // без плана некуда писать ленту работ. Заводим и продолжаем.
    return newCase().then(function () { return sendFiles(list); });
  }
  var fd = new FormData();
  Array.prototype.forEach.call(list, function (f) { fd.append("files", f); });
  var box = document.querySelector("#regview .regup");
  if (box) box.classList.add("busy");
  return api("/api/case/" + caseId + "/upload", { method: "POST", body: fd })
    .then(function (r) {
      // Запоминаем, что именно сейчас загрузили: пока агент читает, эти
      // документы показываются отдельной полосой над таблицей. Иначе
      // загрузка в реестре была бы действием без ответа: файл где-то в
      // списке, а что с ним стало — написано в ленте плана, которой отсюда
      // не видно.
      (r && r.documents || []).forEach(function (id) {
        if (fresh.indexOf(id) < 0) fresh.push(id);
      });
      if (inRegistry) loadRegistry(true);
      return tick();
    })
    .catch(function (e) {
      if (box) box.classList.remove("busy");
      alert("Не удалось загрузить: " + (e.message || "ошибка сети"));
    });
}

/* Перетаскивание файлов в реестр. Считаем вложенность: dragleave приходит и
   при переходе на дочерний элемент, без счетчика подсветка мигает. */
var dragDepth = 0;

function armDropZone() {
  var zone = $("regview");
  zone.addEventListener("dragenter", function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth++;
    zone.classList.add("dropping");
  });
  zone.addEventListener("dragover", function (e) {
    if (hasFiles(e)) e.preventDefault();
  });
  zone.addEventListener("dragleave", function () {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) zone.classList.remove("dropping");
  });
  zone.addEventListener("drop", function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth = 0;
    zone.classList.remove("dropping");
    sendFiles(e.dataTransfer.files);
  });
  // Файл, брошенный мимо реестра, браузер откроет вместо страницы — и
  // несохраненное состояние пропадет. Гасим это на всем окне.
  ["dragover", "drop"].forEach(function (name) {
    document.addEventListener(name, function (e) {
      if (hasFiles(e) && !$("regview").contains(e.target)) e.preventDefault();
    });
  });
  // Выбор файла кнопкой в шапке реестра.
  zone.addEventListener("change", function (e) {
    if (!e.target.matches('input[type="file"]')) return;
    var list = Array.prototype.slice.call(e.target.files);
    e.target.value = "";
    sendFiles(list);
  });
}

function hasFiles(e) {
  var t = e.dataTransfer && e.dataTransfer.types;
  return !!t && Array.prototype.indexOf.call(t, "Files") >= 0;
}
armDropZone();

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
