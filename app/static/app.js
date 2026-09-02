/* Лента дела: опрос состояния и отрисовка.
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
  var s = (Math.round(v * 100) / 100).toString().replace(".", ",");
  var p = s.split(",");
  return p[0].replace(/\B(?=(\d{3})+(?!\d))/g, " ") + (p[1] ? "," + p[1] : "");
}
function px(n, a, b, c) {
  var d = n % 10, h = n % 100;
  if (h >= 11 && h <= 14) return c;
  if (d === 1) return a;
  if (d >= 2 && d <= 4) return b;
  return c;
}

/* ── дела ─────────────────────────────────────────────────── */
function loadCases() {
  return api("/api/cases").then(function (rows) {
    $("caselist").innerHTML = rows.length ? rows.map(function (c) {
      return '<div class="case' + (c.id === caseId ? " on" : "") + '" data-id="' + c.id + '">' +
             esc(c.title) + "<small>" + esc(c.stage) + " · документов " + c.documents +
             "</small></div>";
    }).join("") : '<div class="empty" style="padding:0 16px">Дел пока нет</div>';
    Array.prototype.forEach.call(document.querySelectorAll(".case"), function (el) {
      el.addEventListener("click", function () { open(+el.getAttribute("data-id")); });
    });
    return rows;
  });
}

function newCase() {
  return api("/api/cases", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ year: 2026 })
  }).then(function (r) { return loadCases().then(function () { open(r.id); }); });
}

function open(id) {
  caseId = id;
  lastSig = "";
  loadCases();
  tick();
}

/* ── отрисовка ────────────────────────────────────────────── */
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
    return '<div class="' + cls + '"><span class="dot"></span>' +
           '<span class="n">' + a.n + "</span>" +
           '<span class="b"><span class="t">' + esc(a.name) + "</span><br>" +
           '<span class="s">' + esc(sub) + "</span></span></div>";
  }).join("");

  // Оптимизатор в панель попадает, но отдельной строкой: он не агент.
  var s = state.solver || { name: "Оптимизатор", does: "ищет план: MIP, решатель HiGHS" };
  var sa = last.solver, scls = "ag", ssub = s.does;
  if (sa) {
    if (sa.state === "идет") { scls = "ag work"; ssub = sa.title; }
    else if (sa.state === "ошибка") { scls = "ag err"; ssub = sa.detail || "ошибка"; }
    else { scls = "ag done"; ssub = (sa.detail || s.does) + (sa.seconds != null ? " · " + sa.seconds + " с" : ""); }
  }
  $("agents").innerHTML += '<div class="' + scls + '"><span class="dot"></span>' +
    '<span class="n">—</span><span class="b"><span class="t">' + esc(s.name) + "</span><br>" +
    '<span class="s">' + esc(ssub) + "</span></span></div>";

  $("agentlegend").innerHTML = state.agents.map(function (a) {
    return '<div class="a' + (a.real ? "" : " off") + '"><b>' + a.n + "</b>" + esc(a.name) + "</div>";
  }).join("");
}

function renderDocs() {
  var el = $("docs");
  if (!state.documents.length) {
    el.innerHTML = '<div class="empty">Документов пока нет. Приложите файлы кнопкой «+» внизу ленты.</div>';
    return;
  }
  el.innerHTML = state.documents.map(function (d) {
    var cls = d.state === "разобран" ? "ok" : (d.state === "ожидает" ? "wait" : "bad");
    return '<div class="doc ' + cls + '"><div class="nm">' + esc(d.name) + "</div>" +
           '<div class="mt">' + esc(d.kind || d.state) +
           (d.summary ? " · " + esc(d.summary) : "") +
           (d.by ? " · " + esc(d.by) : "") + "</div></div>";
  }).join("");
}

function renderRuns() {
  var el = $("runs");
  if (!state.runs.length) { el.innerHTML = '<div class="empty">Расчет не выполнялся</div>'; return; }
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
                    : '<div class="empty">Ответьте в строке ниже.</div>');
}

function renderStage() {
  var c = state.case;
  var working = state.activities.filter(function (a) { return a.state === "идет"; });
  var pill = working.length
    ? '<span class="pill work">агент работает: ' + esc(working[0].title) + "</span>"
    : '<span class="pill' + (c.stage === "посчитано" ? " ok" : "") + '">' + esc(c.stage) + "</span>";
  $("stage").innerHTML = "<b>" + esc(c.title) + "</b>" + pill +
    '<span class="empty">обновлено ' + esc(c.updated) + "</span>";
  $("solve").disabled = !c.has_data || working.length > 0;
}

function safely(name, fn) {
  try { fn(); } catch (e) { console.error("не отрисовалось: " + name, e); }
}

function render() {
  if (!state) return;
  safely("шапка дела", renderStage);
  safely("лента", renderFeed);
  safely("агенты", renderAgents);
  safely("документы", renderDocs);
  safely("расчеты", renderRuns);
  safely("вопрос", renderAsk);
  var m = state.model;
  $("model").textContent = m.provider
    ? "разбор документов: " + m.name + ", " + m.label
    : "разбор документов: по заголовкам (" + m.why + ")";
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
    if (sig !== lastSig) { lastSig = sig; render(); }
  }).catch(function () {});
}

/* ── события ──────────────────────────────────────────────── */
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
