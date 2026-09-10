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
function svg16(path) {
  return '<svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" ' +
         'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">' + path + "</svg>";
}

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
var ICON_CLOSE = '<path d="M4 4l8 8M12 4l-8 8"/>';
var ICON_DOWN = '<path d="M8 2.5v8M4.8 7.3 8 10.5l3.2-3.2"/>' +
                '<path d="M2.8 11.2v1.3c0 .6.4 1 1 1h8.4c.6 0 1-.4 1-1v-1.3"/>';
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
    casesCache = rows;
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
  if (stage === "нет решения" || stage === "не успел") return "bad";
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
  vdata = null; vresult = null; vrun = null; vrep = null; chosenRun = null;
  setView("feed");
  loadCases();
  tick();
}

/* Текущий план: последний посчитанный, сразу сводкой года. Экономист заходит
 * посмотреть план, а не переписку, — лента остается на своей вкладке. */
var casesCache = [];
function openCurrent() {
  var computed = casesCache.filter(function (c) { return c.run_id; });
  if (!computed.length) {
    if (casesCache.length) open(casesCache[0].id);
    return;
  }
  var c = computed[0];
  // Если этот план уже открыт, open() не зовётся — но из реестра или со
  // страницы агентов вернуться всё равно надо.
  if (caseId !== c.id) open(c.id);
  else { leaveRegistry(); leaveAgents(); }
  // Сводке нужен список прогонов дела; он приходит первым опросом состояния,
  // а сервер после перезапуска отвечает не сразу — ждем состояние, не таймер.
  var tries = 0;
  (function whenReady() {
    if (state && state.runs && caseId === c.id) { setView("sum"); return; }
    if (tries++ < 50) setTimeout(whenReady, 200);
  })();
  $("opencurrent").classList.add("on");
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

/* Реплика, отправленная и еще не подтвержденная сервером. Пока она здесь,
   лента показывает ее эхом и строку «агент печатает»: ответ модели идет
   секунды, и без этого экономист не понимал, ушло сообщение или нет. */
var sending = null;

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
  }).join("") + waitingMsg();
  // Ждем ответа — прокручиваем всегда: экономист смотрит на свою реплику.
  if (atBottom || sending) feed.scrollTop = feed.scrollHeight;
}

/* Эхо своей реплики и строка ожидания ответа. Эхо снимается, как только та
   же реплика пришла с сервера, — иначе она двоилась. */
function waitingMsg() {
  if (!sending) return "";
  var echoed = state.messages.some(function (m) {
    return m.who === "экономист" && m.text === sending;
  });
  return (echoed ? "" :
    '<div class="msg me"><div class="head"><b>Вы</b><span>сейчас</span></div>' +
    '<div class="bubble">' + esc(sending) + "</div></div>") +
    '<div class="msg"><div class="head"><b>Агент</b><span>отвечает</span></div>' +
    '<div class="bubble typing"><i></i><i></i><i></i></div></div>';
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

var openDocs = { "реестр": true, "план": true };

/* Документ с флажком: поставлен — чат этого плана его учитывает, снят —
 * нет. Точка перед именем — состояние разбора. */
function docCard(d) {
  var cls = d.state === "разобран" ? "ok" : (d.state === "ожидает" ? "wait" : "bad");
  return '<div class="doc ' + cls + (d.muted ? " off" : "") + '">' +
         '<input type="checkbox" data-mute="' + d.id + '"' + (d.muted ? "" : " checked") +
         ' title="Учитывать в этом плане">' +
         '<div class="db"><div class="nm"><span><span class="st"></span>' + esc(d.name) + "</span>" +
         rowMenu("data-docmenu", d.id) + "</div>" +
         // Под именем — вид и состояние словом; подробности в подсказке.
         '<div class="mt" title="' + esc(d.summary || "") + '">' +
         esc(d.kind || "") + (d.kind && d.state ? " · " : "") + esc(d.state || "") +
         "</div></div></div>";
}

function renderDocs() {
  var el = $("docs");
  if (!state.documents.length) {
    el.innerHTML = '<div class="empty">Документов пока нет</div>';
    return;
  }
  // Две группы: реестр организации — общие документы, и песочница этого
  // плана — загружены в чат, в реестр не идут.
  var groups = [["реестр", "Реестр организации"], ["план", "В этом плане"]];
  el.innerHTML = groups.map(function (g) {
    var list = state.documents.filter(function (d) { return (d.scope || "реестр") === g[0]; });
    if (!list.length) return "";
    var open = openDocs[g[0]];
    return '<div class="dgrp"><button type="button" class="dgh' + (open ? " on" : "") +
           '" data-grp="' + g[0] + '"><span class="t">' + esc(g[1]) + "</span>" +
           '<span class="c">' + list.length + "</span></button>" +
           (open ? list.map(docCard).join("") : "") + "</div>";
  }).join("");
}

/* Цели решателя в карточке прогона: стадии-правила со значениями и
 * взвешенные цели с весом. По этим числам экономист видит, что дала
 * претензия: переводов было 4, стало 1. Претензии прогона — ниже списком. */
function runGoals(s) {
  var goals = s["цели"] || [], claims = s["претензии"] || [];
  if (!goals.length && !claims.length) return "";
  var html = "";
  if (goals.length) {
    html += '<table class="mini goals"><thead><tr><th>Цель</th><th class="n">Значение</th>' +
      "<th>Единица</th><th>Приоритет</th><th class=\"n\">Вес</th></tr></thead><tbody>" +
      goals.map(function (g) {
        return "<tr><td>" + esc(g["цель"]) + '</td><td class="n">' +
          esc(fmtGoal(g["значение"])) + "</td><td>" + esc(g["единица"] || "") +
          "</td><td>" + esc(g["приоритет"] || "") + '</td><td class="n">' +
          (g["вес"] != null ? esc(fmtGoal(g["вес"])) : "—") + "</td></tr>";
      }).join("") + "</tbody></table>";
  }
  if (claims.length) {
    html += '<div class="mt">' + claims.map(function (c) {
      return "претензия: " + esc(c["текст"] || "") + " → " + esc(c["направление"] || "") +
        " «" + esc(c["цель"] || "") + "» ×" + esc(String(c["множитель"] || "")) +
        " (вес " + esc(fmtGoal(c["вес"])) + ")";
    }).join("<br>") + "</div>";
  }
  return '<div class="mt">' + html + "</div>";
}
function fmtGoal(v) {
  if (v == null || v === "") return "—";
  var n = Number(v);
  if (isNaN(n)) return String(v);
  return (Math.abs(n) >= 100 ? Math.round(n) : Math.round(n * 100) / 100)
    .toLocaleString("ru-RU");
}

/* Версии расчета: номер, дата, статус и что изменилось против предыдущего
 * удачного — числами по целям решателя. Щелчок открывает отчет этой версии. */
var chosenRun = null;
var GOAL_SHORT = {
  "Смены договора оклада (переводы и открытия)": "смены",
  "Переводы оклада между договорами": "смены",
  "Административная сложность выплат": "сложность",
  "Отклонение от равномерного освоения": "освоение",
  "Изменение сумм выплат между месяцами": "суммы",
};
function goalDiff(cur, prev) {
  var goals = ((cur || {})["цели"] || []).filter(function (g) { return g["приоритет"] === "вес"; });
  if (!goals.length) return "";
  var before = {};
  ((prev || {})["цели"] || []).forEach(function (g) { before[g["цель"]] = g; });
  // Стрелки — только там, где число сдвинулось; одинаковый план показывает
  // просто свои значения.
  var moved = goals.some(function (g) {
    var b = before[g["цель"]];
    return b && Math.abs(Number(g["значение"]) - Number(b["значение"])) >= 0.5;
  });
  return goals.map(function (g) {
    var name = GOAL_SHORT[g["цель"]] || g["цель"];
    var f = g["единица"] === "₽" ? function (v) { return mo(Math.round(v)); } : fmtGoal;
    var b = before[g["цель"]];
    if (!b || !moved) return esc(name) + " " + esc(f(g["значение"]));
    var d = Number(g["значение"]) - Number(b["значение"]);
    var cls = Math.abs(d) < 0.5 ? "" : (d > 0 ? "up" : "dn");
    return esc(name) + ' <span class="' + cls + '">' + esc(f(b["значение"])) + " → " +
           esc(f(g["значение"])) + "</span>";
  }).join(" · ");
}
function renderRuns() {
  var el = $("runs");
  if (!el) return;
  if (!state.runs.length) { el.innerHTML = '<div class="empty">Расчет еще не выполнен</div>'; return; }
  var runs = state.runs;
  var shown = chosenRun || (runs.filter(function (r) { return r.status === "OPTIMAL"; })[0] || {}).id;
  el.innerHTML = runs.map(function (r, i) {
    var prev = runs.slice(i + 1).filter(function (x) { return x.status === "OPTIMAL"; })[0];
    var cls = r.status === "OPTIMAL" ? "" : (r.status === "идет" ? "work" : "bad");
    var diff = r.status === "OPTIMAL" ? goalDiff(r.summary, prev && prev.summary) : "";
    return '<div class="ver ' + cls + (r.id === shown ? " on" : "") + '" data-ver="' + r.id + '">' +
      '<div class="vh1"><span class="vn">№ ' + r.id + "</span>" +
      '<span class="vs' + (cls === "bad" ? " bad" : "") + '">' +
      esc(r.status === "OPTIMAL" ? "посчитан" : r.status) +
      (r.seconds != null ? " · " + r.seconds + " с" : "") + "</span>" +
      '<span class="vd">' + esc(r.created) + "</span></div>" +
      (diff ? '<div class="vdiff">' + diff + "</div>" : "") +
      (r.status === "OPTIMAL"
        ? '<div class="vdiff"><a href="/api/case/' + caseId + "/result/" + r.id + '">xlsx</a></div>'
        : "") +
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
  // Работы приходят от старой к новой: в шапку берём последнюю, иначе
  // первой встанет давняя строка, а не то, чем сервис занят сейчас.
  var working = state.activities.filter(function (a) { return a.state === "идет"; }).slice(-1);

  $("ptitle").textContent = c.title;

  // В строке под названием — состояние плана и то, чем сейчас занят сервис.
  var pill = working.length
    ? '<span class="pill work">агент работает: ' + esc(working[0].title) + "</span>"
    : '<span class="pill' + (c.stage === "посчитано" ? " ok" : (c.stage === "нет решения" || c.stage === "не успел" ? " bad" : "")) + '">' +
      esc(c.stage === "готово к расчету" && (state.runs || []).some(function (r) { return r.status === "OPTIMAL"; }) ? "готов к пересчёту" : c.stage) + "</span>";
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
    var rules = (pf["ограничения"] || []).map(function (kv) {
      return '<div class="pfrow pfrule"><span>' + esc(kv[0]) + "</span><b>" + esc(kv[1]) + "</b></div>";
    }).join("");
    back.innerHTML =
      '<div class="box wide" role="dialog" aria-modal="true">' +
        "<h4>" + (pf["стоит"] ? "Недостаточно данных" : "Проверьте данные перед расчётом") + "</h4>" +
        (pf["стоит"] ? "" : '<p class="pfintro">Расчёт будет выполнен по данным ниже.</p>') +
        '<div class="pfrows">' + rows + "</div>" +
        (rules ? '<div class="pfh">Правила оптимизатора</div><div class="pfrows">' + rules + "</div>" : "") +
        (warn ? '<div class="pfh">Что нужно проверить</div>' + warn : "") +
        (pf["стоит"]
          ? '<p>Нет сотрудников или договоров. Загрузите документы в реестр.</p>' : "") +
        '<div class="btns">' +
          '<button type="button" class="no">Отмена</button>' +
          (pf["стоит"] ? "" :
           '<button type="button" class="yes">Рассчитать</button>') +
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
    vsum = null, vpay = null, vrep = null;
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
  // Подробным таблицам нужна ширина: колонка контекста забирает 320 пикселей,
  // и месяцы перестают помещаться. На отчете ее убираем, а версию выбирают
  // в его же панели; вернуть колонку можно кнопкой в шапке.
  if (!inRegistry && !inAgents) wideScreen(v === "sum");
  // Ответ агента приходит в ленту. Если экономист смотрит план или сводку,
  // он о нем не узнает: раньше вопрос выглядел оставленным без ответа.
  if (v === "feed") unseen = 0;
  markFeedTab();
  var cur = $("opencurrent");
  if (cur) cur.classList.toggle("on", v === "sum" && !inRegistry && !inAgents);
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
  // Выбранная версия — если экономист щелкнул по ней в панели; иначе последняя.
  var ok = (chosenRun && runs.filter(function (r) {
              return r.id === chosenRun && r.status === "OPTIMAL"; })[0]) ||
           runs.filter(function (r) { return r.status === "OPTIMAL"; })[0];
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
      // Отчет в формах экономистов — им и заменены прежние таблицы результата.
      api("/api/case/" + caseId + "/run/" + pick.id + "/report")
        .then(function (r) { vrep = r; repOpenCache = null; })
        .catch(function () { vrep = null; repOpenCache = null; }),
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

  var ctrs = '<table class="mini"><thead><tr><th>Договор</th><th>ГОЗ</th><th class="n">ФОТ, ₽</th>' +
    '<th class="n">Поступления, ₽</th><th class="n">Выплаты, ₽</th>' +
    '<th class="n">Остаток, ₽</th><th class="n">Освоение</th><th></th></tr></thead><tbody>' +
    vsum["договоры"].map(function (c) {
      return "<tr><td>" + esc(c["договор"]) + "</td><td>" + (c["ГОЗ"] ? "да" : "—") + "</td>" +
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
    "сложилась: договоры, ставки, виды выплат. Ниже — все месяцы каждого " +
    "человека числами.</div>" +
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

/* Подробно: у каждого человека все двенадцать месяцев видно сразу.

   Компактная строка со полосой годится, чтобы охватить сотню людей взглядом,
   но экономист работает с числами: ставка, договоры, суммы по видам выплат.
   Прятать их за наведением и щелчком значит заставлять его открывать
   тридцать шесть подсказок подряд. Поэтому подробности раскрыты, а страницы
   режут список по людям, а не по месяцам. */
function payrollFull() {
  if (!vpay || !vpay["люди"].length) return "";
  var kindOrder = ["оклад", "120", "122", "124", "152", "приказ"];
  var pg = paged("payroll-full", vpay["люди"]);
  return pg.rows.map(function (p) {
    var used = kindOrder.filter(function (k) {
      return p["месяцы"].some(function (m) {
        return m && m["договоры"].some(function (c) {
          return c["виды"].some(function (v) { return v["вид"] === k; });
        });
      });
    });
    var rows = [];
    p["месяцы"].forEach(function (m, i) {
      if (!m) {
        rows.push([MONTHS[i], { v: "—", cls: "n" }, { v: "не работает" }]
          .concat(used.map(function () { return { v: null, cls: "n" }; }))
          .concat([{ v: null, cls: "n" }, { v: null }]));
        return;
      }
      rows.push([
        MONTHS[i],
        { v: num(m["ставка"]), cls: "n" +
             (m["ставка"] > (p["ставка"] || 1) + 0.01 ? " up" : "") },
        { v: m["договоры"].map(function (c) {
            return '<span class="ctag ' + ctrClass(c["код"]) + '">' + esc(c["код"]) +
                   (c["ставка"] ? " · " + num(c["ставка"]) : "") + "</span>";
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
    // Заголовок — только имя и должность: якорь, чтобы не потеряться при
    // прокрутке. Ставка, зарплата, за год и число смен стоят графами в
    // обзорной таблице выше и в строках месяцев ниже; повторять их фразой
    // у каждого человека значит писать одно и то же сто раз.
    return '<div class="grp person"><h4>' + esc(p["фио"]) +
      (p["должность"] ? ' <span class="dep">' + esc(p["должность"]) +
                        "</span>" : "") + "</h4>" +
      table(["Месяц", { t: "ставка" }, "Договоры и ставки"].concat(
              used.map(function (k) { return { t: k + ", ₽" }; }),
              [{ t: "всего, ₽" }, "Что изменилось"]),
        rows, "", { plain: true }) + "</div>";
  }).join("") + pager("payroll-full", pg);
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
    return '<table class="mini rb2"><tbody>' + rows.map(function (s, i) {
      return '<tr data-review-key="rule:' + vrules.indexOf(r) + ':' + i + '"><td>' + esc(s["объект"]) + "</td><td>" + esc(s["что"] || "") +
             "</td></tr>";
    }).join("") + "</tbody></table>";
  }
  var unit = r["единица"] === "₽" ? ", ₽" : (r["единица"] ? ", " + r["единица"] : "");
  var limitName = cap(r["подпись предела"] || "предел");
  return '<table class="mini rb2"><thead><tr><th>Где</th>' +
    '<th class="n">Факт' + esc(unit) + '</th><th class="n">' + esc(limitName) +
    esc(unit) + '</th><th class="n">Запас</th><th></th></tr></thead><tbody>' +
    rows.map(function (s, i) {
      return '<tr data-review-key="rule:' + vrules.indexOf(r) + ':' + i + '" class="' + (s["нарушено"] ? "over" : "") + '"><td>' +
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
      : "Решатель причину не назвал.") + "</div>";
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
          ? '<td class="' + (c.cls || "") + '"' + (c.cs ? ' colspan="' + c.cs + '"' : "") +
            ">" + (c.v == null ? "—" : c.v) + "</td>"
          : "<td>" + (c == null || c === "" ? "—" : esc(c)) + "</td>";
      }).join("") + "</tr>";
  }).join("");
  return h + "</tbody></table></div>";
}

/* ── план ФОТ на год: формы экономистов ───────────────────────
 * Двенадцать разделов числами, по согласованному прототипу. Данные
 * считает сервер (report.py), здесь только раскладка по вкладкам:
 * сводка — итоги, договоры, освоение; освоение — касса; план выплат —
 * виды, регистр, ставки; ограничения — БЭП, П4, трудоемкость, цели. */
var repPeriod = "year", repGroup = "e";
var MON = ["янв", "фев", "мар", "апр", "май", "июн",
           "июл", "авг", "сен", "окт", "ноя", "дек"];
function rmi(v) { return v == null ? "—" : mo(Math.round(v)); }
function rf(v) { return v == null ? "—" : Number(v).toFixed(2).replace(".", ","); }
function rm(v) { return v == null ? "—" : rmi(Math.round(v)); }
function rp(v) {
  return v == null ? "—" : String(Math.round(v * 1000) / 10).replace(".", ",") + " %";
}
// Процент числом, без знака: для граф, у которых «%» уже стоит в заголовке.
function rpn(v) {
  return v == null ? "—" : String(Math.round(v * 1000) / 10).replace(".", ",");
}
function rs(v, d) { return v == null ? "—" : (v > 0 ? "+" : (v < 0 ? "−" : "")) + (d ? rf(Math.abs(v)) : rmi(Math.abs(v))); }
function nc(v, cls) {
  var neg = typeof v === "string" && v.charAt(0) === "−";
  return { v: v == null || v === "—" ? "" : v, cls: "n" + (cls ? " " + cls : "") + (neg ? " neg" : "") };
}
function gc(v) { return { v: v == null || v === "—" ? "" : v, cls: "gray" }; }

/* Штриховка значит «значения здесь быть не может»: договор в этом месяце не
   действует или уже не платит, человек не работает, этап не идёт. Ноль —
   это не то же самое: договор действует, но выплаты в месяце не было, и
   такая клетка остаётся пустой. Раньше штриховались обе, и экономист не
   отличал закрытый месяц от нулевого.

   Окно договора берём из кассы: там месяцы вне срока — пустые, а после
   предела выплат — «запрет». */
var repOpenCache = null;
function repOpenMonths(code) {
  if (!repOpenCache) {
    repOpenCache = {};
    ((vrep && vrep["касса"]) || []).forEach(function (c) {
      var row = (c["строки"] || {})["доступно"] || [];
      repOpenCache[c["код"]] = row.map(function (v) { return v != null && v !== "запрет"; });
    });
  }
  return repOpenCache[code] || null;
}

/* Клетка месяца: число, пусто (ноль в открытом месяце) или штриховка. */
function mc(v, code, month, fmt, cls) {
  if (v) return nc(fmt(v), cls || "");
  var open = repOpenMonths(code);
  return (open && open[month - 1] === false) ? gc("—") : { v: "", cls: "n" };
}

/* ── сетка отчета ────────────────────────────────────────────
 * Плотная сетка, как в учетной системе: линии по обеим осям, шапка и первые
 * графы закреплены при прокрутке, одинаковые значения в соседних строках
 * объединены, строки-группы сворачиваются, итоги — жирным над двойной линией.
 * Ячейка — строка или объект {v, cls, span (rowspan), cs (colspan), skip};
 * строка — массив ячеек или объект {cells, cls, grp, g (ключ группы)}. */
var REP_SECTIONS = [];
/* Автофильтр как в Excel: у каждой графы кнопка со списком значений и
   сортировкой. Отбор и сортировка хранятся по таблице; таблицы нумеруются по
   порядку отрисовки, поэтому номера устойчивы между перерисовками. */
var repColFilter = {}, repSort = {}, repHideCol = {}, GRID_SEQ = 0, CUR_SEC = 0, GRID_DATA = {};
var REP_SHORT = { 1: "Итоги", 2: "Договоры", 3: "Освоение", 4: "Касса", 5: "Виды выплат",
                  6: "Выплаты", 7: "Ставки", 8: "БЭП", 9: "П4", 10: "Трудоёмкость",
                  11: "Исполнители", 12: "Нехватка", 13: "Расчёт", 14: "Дефицит",
                  15: "Контроль" };
function secH(n, t, meta) {
  REP_SECTIONS.push({ n: n, t: REP_SHORT[n] || t, meta: meta || "" });
  CUR_SEC = n;
  return '<div class="grp rep" id="sec-' + n + '"><div class="sech">' +
    '<span class="sn">' + n + '</span><span class="stt">' + esc(t) + "</span>" +
    (meta || "") + '<span class="sp"></span></div>';
}

/* Текст ячейки для отбора и сортировки: без разметки, в нижнем регистре. */
function cellText(c) {
  if (c == null) return "";
  var v = typeof c === "object" ? (c.skip ? "" : c.v) : c;
  return String(v == null ? "" : v).replace(/<[^>]+>/g, "").toLowerCase();
}
function cellShow(c) {
  if (c == null) return "";
  var v = typeof c === "object" ? (c.skip ? "" : c.v) : c;
  return String(v == null ? "" : v).replace(/<[^>]+>/g, "").replace(/&nbsp;/g, " ").trim();
}
function isDataRow(r) { return !r.grp && !(r.cls && /\bsum\b/.test(r.cls)); }
function numOf(t) {
  var s = String(t).replace(/[\s\u00a0\u202f]/g, "").replace("−", "-").replace(",", ".");
  return s !== "" && !isNaN(Number(s)) ? Number(s) : null;
}

/* Отбор и сортировка до объединения ячеек: строки ещё полные, ключи в них
   повторяются, и совпадение ищется по всей строке. Строка группы остаётся,
   если совпала сама или совпала хоть одна её строка; строка остаётся, если
   совпала сама или совпал заголовок её группы (у человека в регистре имя
   стоит только в заголовке). Сортируется ведущий блок обычных строк,
   итоговые в хвосте остаются на месте; таблицы с промежуточными итогами и
   группами не сортируются. */
function rowPasses(r, colf, skipCol) {
  var cells = r.cells || r;
  for (var c in colf) {
    if (skipCol != null && +c === skipCol) continue;
    if (!colf[c][cellText(cells[c])]) return false;
  }
  return true;
}
/* Отбор до объединения ячеек: строки ещё полные. Обычная строка остаётся,
   если проходит отбор по всем графам. Строка группы остаётся, если под ней
   осталась хоть одна строка; промежуточный итог — если в его блоке (от
   предыдущей группы или итога) что-то осталось; общий итог («all») — если
   осталось хоть что-то. Сортируется ведущий блок обычных строк, итоги в
   хвосте на месте; таблицы с группами и промежуточными итогами не
   сортируются. */
function gridPrepare(rows, colf, srt) {
  rows = rows.slice();
  if (colf && Object.keys(colf).length) {
    var parents = function (g) {
      var out = [], p = g.indexOf("\u0001");
      while (p >= 0) { out.push(g.slice(0, p)); p = g.indexOf("\u0001", p + 1); }
      out.push(g);
      return out;
    };
    var pass = rows.map(function (r) { return isDataRow(r) && rowPasses(r, colf); });
    var grpHit = {};
    rows.forEach(function (r, i) {
      if (pass[i] && r.g) parents(r.g).forEach(function (k) { grpHit[k] = true; });
    });
    var keep = [], block = false, any = false;
    rows.forEach(function (r, i) {
      if (r.grp) { if (grpHit[r.k || r.g]) keep.push(r); block = false; return; }
      if (isDataRow(r)) { if (pass[i]) { keep.push(r); block = true; any = true; } return; }
      if (r.cls && /\ball\b/.test(r.cls)) keep.push(r);
      else if (block) keep.push(r);
      block = false;
    });
    rows = any ? keep : [];
  }
  if (srt && srt.c != null) {
    var n = rows.length;
    while (n > 0 && !isDataRow(rows[n - 1])) n--;
    var head = rows.slice(0, n), tail = rows.slice(n);
    if (head.every(isDataRow)) {
      head.sort(function (a, b) {
        var ta = cellText((a.cells || a)[srt.c]), tb = cellText((b.cells || b)[srt.c]);
        var na = numOf(ta), nb = numOf(tb);
        var r = (na != null && nb != null) ? na - nb : ta.localeCompare(tb, "ru");
        return srt.d === "d" ? -r : r;
      });
      rows = head.concat(tail);
    }
  }
  return rows;
}
function gridSortable(rows) {
  var n = rows.length;
  while (n > 0 && !isDataRow(rows[n - 1])) n--;
  return n > 1 && rows.slice(0, n).every(isDataRow);
}
function subH(n, t) {
  return '<div class="sech sub"><span class="sn">' + n + '</span><span class="stt">' + esc(t) + "</span></div>";
}
function chip(v, cls) { return '<span class="chip ' + (cls || "") + '">' + v + "</span>"; }

/* Подпись договора одна на весь отчет. Шифр — то, чем договор называют и
   ищут, поэтому он жирный и всегда первый; название идет за ним и берется
   из реестра «Договоры» (графы «шифр» и «название»). Раньше каждый раздел
   склеивал подпись по-своему: где через тире, где через точку, где без
   названия, и один договор выглядел в отчете четырьмя разными строками. */

// Шапка группы: шифр договора и ничего больше.
function ctrHead(code) {
  return "<b>" + esc(code) + "</b>";
}
function thCell(c, i, fix, sep, so) {
  var cls = (i < fix ? "fx" : "") + (sep ? " gs" : ""), dc = (i < fix ? ' data-c="' + i + '"' : "") + ' scope="col"';
  var btn = "";
  if (so) {
    var on = so.f ? " fon" : "", sorted = so.s && so.s.c === so.c ? (so.s.d === "d" ? " sd" : " sa") : "";
    cls += " af" + on + sorted;
    btn = '<button type="button" class="fbtn" data-fc="' + so.c + '" aria-label="Отбор и сортировка"></button>';
  }
  var wrap = function (txt) { return btn ? '<span class="thw"><span>' + txt + "</span>" + btn + "</span>" : "<span>" + txt + "</span>"; };
  if (typeof c !== "object") return '<th class="' + cls + '"' + dc + ">" + wrap(esc(c)) + "</th>";
  if (c.v != null) return '<th class="' + cls + " " + (c.cls || "") + '"' + dc +
                          (c.span ? ' colspan="' + c.span + '"' : "") + ">" + c.v + "</th>";
  return '<th class="n ' + cls + '"' + dc + ">" + wrap(esc(c.t)) + "</th>";
}
/* Верхний ярус шапки: месяцы или кварталы над своими графами. Двенадцать
   подписей вида «03.26 по плану» читались как сплошная строка текста, а
   границы кварталов не были видны вовсе.
   lead — сколько граф слева не относится к месяцам, per — сколько граф на
   месяц, tail — сколько граф справа (итог за год и пределы). */
function monGroupRow(lead, per, tail, mode) {
  var g = [];
  if (lead) g.push({ t: "", span: lead });
  if (mode === "month") {
    repMon.forEach(function (m) { g.push({ t: MON[m - 1], span: per }); });
  } else {
    var qs = [[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11, 12]];
    var names = ["I квартал", "II квартал", "III квартал", "IV квартал"];
    qs.forEach(function (arr, i) {
      var n = arr.filter(function (m) { return repMon.indexOf(m) >= 0; }).length;
      if (n) g.push({ t: names[i], span: n * per });
    });
  }
  if (tail) g.push({ t: "", span: tail });
  return g;
}

function grid(head, rows, opts) {
  opts = opts || {};
  var tid = "g" + (GRID_SEQ++);
  var colf = repColFilter[tid] || {}, srt = repSort[tid];
  var sortable = gridSortable(rows);
  // Значения граф до отбора и объединения — для списков автофильтра.
  GRID_DATA[tid] = { rows: rows.map(function (r) { return { d: isDataRow(r), v: (r.cells || r).map(cellShow),
                                                            t: (r.cells || r).map(cellText) }; }),
                     sortable: sortable };
  var filtered = Object.keys(colf).length > 0;
  rows = gridPrepare(rows, colf, srt);
  if (opts.merge) mergeDown(rows, opts.merge, opts.fix || 0);
  if (!rows.length && !filtered) return '<div class="none">Пусто</div>';
  // Скрытые графы (меню автофильтра → «Скрыть графу»). Состояние хранится в
  // исходных номерах граф; при отрисовке скрытые пропускаются, объединённые
  // по горизонтали ячейки и ярусы шапки сужаются на число скрытых внутри них.
  var hid = repHideCol[tid] || {}, N = head.length, visPos = [], nvis = 0;
  for (var k = 0; k < N; k++) visPos[k] = hid[k] ? -1 : ++nvis;   // позиция среди видимых, 1-based
  var visIn = function (from, count) {   // сколько видимых граф в [from, from+count)
    var n = 0;
    for (var q = from; q < from + count && q < N; q++) if (!hid[q]) n++;
    return n;
  };
  GRID_DATA[tid].hidden = Object.keys(hid).length;
  // Первая графа — номер строки: по нему называют строку вслух и находят её
  // после прокрутки вбок, поэтому он закреплён вместе с ключевыми графами.
  // Считаем только строки данных: у групп и итогов номера нет.
  var fix = visIn(0, opts.fix || 0) + 1, num = 0;
  // Границы групп: по ним ставим более заметную линию и в шапке, и в строках.
  var sep = {}, at = 0;
  (opts.groups || []).forEach(function (g, i) {
    if (i) sep[at] = true;
    at += g.span || 1;
  });
  var gh = "";
  if (opts.groups) {
    at = 0;
    gh = '<tr class="g1"><th class="num fx" data-c="0"></th>' +
      opts.groups.map(function (g, i) {
        var from = at, count = g.span || 1;
        var cls = (i && sep[from] ? "gs " : "") + (g.t ? "gt" : "");
        var span = visIn(from, count);
        at += count;
        // Первый пустой диапазон стоит над ключевыми графами. Разбиваем его
        // на отдельные липкие ячейки: тогда кварталы прокручиваются под ними,
        // а адаптивное закрепление может снять лишнюю графу на узком экране.
        if (i === 0 && !g.t) {
          var fixedTop = "";
          for (var q = from; q < from + count && q < N; q++) {
            if (visPos[q] > 0) fixedTop += '<th class="fx gfix" data-c="' + visPos[q] + '"></th>';
          }
          return fixedTop;
        }
        return span ? '<th class="' + cls + '" colspan="' + span + '">' + esc(g.t || "") + "</th>" : "";
      }).join("") + "</tr>";
  }
  var h = '<div class="grid' + (opts.tall ? " tall" : "") + '" data-fix="' + fix + '" data-scroll-space="' + (opts.scrollSpace || 0) + '" data-tid="' + tid + '" tabindex="0" role="region" aria-label="Таблица данных"><table><thead>' + gh + "<tr>" +
    '<th class="num fx" data-c="0" scope="col"><span>№</span></th>' +
    head.map(function (c, i) {
      if (hid[i]) return "";
      var so = !(c && typeof c === "object" && c.v != null) ? { c: i, s: srt, f: !!colf[i] } : null;
      return thCell(c, visPos[i], fix, sep[i], so);
    }).join("") + "</tr></thead><tbody>";
  h += rows.map(function (r, i) {
    var cells = r.cells || r;
    // Класс строки — и свой, и от rowCls: полоса группы оборачивает строку
    // в объект, и без этого итоговая строка теряла «sum» и получала номер.
    var rc = (r.cls || "") + " " + (opts.rowCls ? opts.rowCls(i) || "" : "");
    if (r.grp) rc += " grp";
    var attrs = (rc.trim() ? ' class="' + rc.trim() + '"' : "") + (r.g ? ' data-g="' + esc(r.g) + '"' : "") +
                (r.k ? ' data-k="' + esc(r.k) + '"' : "");
    if (r.reviewKey) attrs += ' data-review-key="' + esc(r.reviewKey) + '"';
    var data = !r.grp && rc.indexOf("sum") < 0;
    var col = 1, swallow = 0;   // исходный номер графы, 1-based
    return "<tr" + attrs + '><td class="num fx" data-c="0">' + (data ? ++num : "") + "</td>" +
      cells.map(function (c) {
      // Заглушки {skip} бывают двух родов: после ячейки с colspan они лишь
      // держат место уже занятых граф (счётчик не двигаем), после
      // объединения по вертикали — занимают свою графу (двигаем).
      if (c && c.skip) { if (swallow > 0) swallow--; else col++; return ""; }
      var start = col, width = (c && typeof c === "object" && c.cs) ? c.cs : 1;
      col += width;
      swallow = width - 1;
      var vspan = visIn(start - 1, width);
      if (!vspan) return "";
      var vp = visPos[start - 1];
      if (vp < 0) { for (var q = start; q < start + width; q++) if (visPos[q - 1] > 0) { vp = visPos[q - 1]; break; } }
      var fx = vp < fix, cls = (fx ? "fx " : "") + (sep[start - 1] ? "gs " : "");
      var extra = ' data-source-col="' + start + '"' + (fx ? ' data-c="' + vp + '"' : '');
      if (c && typeof c === "object") {
        if (c.span) extra += ' rowspan="' + c.span + '"';
        if (vspan > 1) extra += ' colspan="' + vspan + '"';
        return '<td class="' + cls + (c.cls || "") + '"' + extra + ">" + (c.v == null ? "" : c.v) + "</td>";
      }
      return '<td class="' + cls + '"' + extra + ">" + (c == null ? "" : esc(c)) + "</td>";
    }).join("") + "</tr>";
  }).join("");
  return h + "</tbody></table></div>";
}
function repT(head, rows, rowCls, opts) {
  var o = opts || {};
  o.rowCls = rowCls;
  return grid(head, rows, o);
}
/* Одинаковые значения в соседних строках по заданным графам объединяются в
   одну ячейку. Графы идут слева направо: вторая объединяется только внутри
   объединения первой — как группировка в учетных формах. */
/* fixN — сколько граф слева будет закреплено при прокрутке вбок. Их
   объединять нельзя: браузер не держит липкую ячейку с rowspan, и при
   прокрутке таблица разъезжается. В таких графах повтор просто не печатаем —
   значение стоит один раз, как при объединении, а строка остаётся целой. */
function mergeDown(rows, cols, fixN) {
  var val = function (c) { return c && typeof c === "object" ? c.v : c; };
  var keys = rows.map(function (r) {
    var cells = r.cells || r, acc = "";
    return cols.map(function (ci) { acc += "\u0001" + (val(cells[ci]) == null ? "" : val(cells[ci])); return acc; });
  });
  // Полоса по первой графе: строки одного договора (или человека) лежат на
  // общем фоне. Без этого длинная таблица читается как сплошная сетка.
  if (cols.length) {
    var band = false;
    for (var r = 0; r < rows.length; r++) {
      if (r && keys[r][0] !== keys[r - 1][0]) band = !band;
      if (band) {
        if (rows[r].cells) rows[r].cls = ((rows[r].cls || "") + " alt").trim();
        else rows[r] = { cells: rows[r], cls: "alt" };
      }
    }
  }
  cols.forEach(function (ci, k) {
    var start = 0;
    for (var i = 1; i <= rows.length; i++) {
      if (i < rows.length && keys[i][k] === keys[start][k]) continue;
      if (i - start > 1) {
        var cells = rows[start].cells || rows[start], c = cells[ci];
        if (ci < (fixN || 0)) {
          for (var b = start + 1; b < i; b++) (rows[b].cells || rows[b])[ci] = "";
        } else {
          cells[ci] = (c && typeof c === "object") ? Object.assign({}, c, { span: i - start })
                    : { v: c == null ? "" : esc(c), span: i - start, cls: "top" };
          for (var j = start + 1; j < i; j++) (rows[j].cells || rows[j])[ci] = { skip: true };
        }
      }
      start = i;
    }
  });
  return rows;
}
var SVG = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">';
function repNav() {
  // Пометка замечания — точка: полный текст стоит в самом разделе, а в
  // оглавлении важно только, есть оно или нет и насколько серьёзно.
  return '<nav class="repnav" aria-label="Разделы отчёта">' + REP_SECTIONS.map(function (x) {
    var mark = /chip er/.test(x.meta || "") ? '<i class="mk er"></i>'
             : /chip wr/.test(x.meta || "") ? '<i class="mk"></i>' : "";
    return '<a href="#sec-' + x.n + '" data-sec="' + x.n + '"' +
           (x.n === repSec ? ' class="on"' : "") + '><b>' + x.n + "</b><span>" + esc(x.t) +
           "</span>" + mark + "</a>";
  }).join("") + "</nav>";
}

function repToolbar() {
  var title = (state.case && state.case.title) || "";
  var sectionTitle = ((REP_SECTIONS.find(function (x) { return x.n === repSec; }) || {}).t || "");
  var savedRuns = (state.runs || []).filter(function (r) { return r.status === "OPTIMAL"; });
  var runControl = savedRuns.length
    ? '<label class="fld" title="Версия расчёта"><select data-sel="run">' + savedRuns.map(function (r) {
        return '<option value="' + r.id + '"' + (r.id === vrun ? ' selected' : '') + '>№ ' +
               r.id + ' · ' + esc(r.created || "дата не указана") + '</option>';
      }).join('') + '</select></label>'
    : '<div class="fld">Расчёта нет</div>';
  // Действия справа: удалить версию, скачать, на весь экран. Корзина стояла
  // вплотную к выбору версии и читалась как часть поля; значок тот же, что у
  // удаления плана и документа — одно действие рисуется одним значком.
  // Значки живут одной группой со своим шагом: у самой панели просвет разный
  // в разных разделах (в «Итогах» он шире), и значки расходились по-разному.
  var reportActions = '<span class="ricons">' +
    (vrun ? '<button type="button" class="iconbtn delrun" data-ra="delrun" ' +
            'title="Удалить эту версию расчёта" aria-label="Удалить эту версию расчёта">' +
            svg16(ICON_TRASH) + '</button>' : "") +
    (vrun ? '<a class="iconbtn dfile" download href="/api/case/' + caseId + "/result/" + vrun + '" title="Скачать XLSX" aria-label="Скачать XLSX">' + svg16(ICON_DOWN) + '</a>' : "") +
    '<button type="button" data-ra="full" class="ico full" title="Отчет на весь экран" aria-label="Отчет на весь экран">' +
    SVG + '<path class="i-a" d="M9.5 2.5h4v4M13.5 2.5 9 7M6.5 13.5h-4v-4M2.5 13.5 7 9"/>' +
    '<path class="i-b" d="M13.5 6.5h-4v-4M9.5 6.5 13.5 2.5M2.5 9.5h4v4M6.5 9.5 2.5 13.5"/></svg></button>' +
    '</span>';
  // Разделы выбираются в боковом оглавлении отчёта (repNav), а не здесь:
  // панель — только версия расчёта, вид и действия, одной строкой.
  return '<div class="rtool"><div class="ftitle"><b>' + esc(title) + "</b>" +
  "</div>" +
  (issueReturn ? '<nav class="issue-context" aria-label="Путь к замечанию"><button type="button" data-issue-back title="Вернуться к списку замечаний">← Замечания к расчёту</button><span aria-hidden="true">/</span><span>' + esc(sectionTitle) + '</span><span class="issue-context-actions">' + reportActions + '</span></nav>' : '') +
  '<div class="acts">' +
  runControl +
  '<label class="fld" title="Период"><select data-sel="mset">' + MON_SETS.map(function (x) {
    return '<option value="' + x[1].join(",") + '"' +
           (x[1].join() === repMon.join() ? " selected" : "") + ">" + x[0] + "</option>";
  }).join("") + "</select></label>" +
  '<span class="sp"></span>' +
  (issueReturn ? '' : reportActions) + "</div></div>";
}
/* Оглавление показывает, где экономист сейчас: при прокрутке подсвечен
   раздел, чья шапка прошла под панелью. Внизу страницы — последний раздел:
   он может быть короче экрана и до края панели не дойти. */
var repSpyBusy = false;
function repSpy() {
  var el = $("view"), tool = el.querySelector(".rtool");
  if (!tool) return;
  // Разделы, кроме открытого, скрыты; у скрытых координаты нулевые, и
  // «последним на экране» становился раздел 15, хотя открыт шестой.
  var secs = Array.prototype.filter.call(el.querySelectorAll(".grp.rep"), function (g) {
    return g.offsetParent !== null;
  });
  if (!secs.length) return;
  var edge = tool.getBoundingClientRect().bottom + 6, id = secs[0].id;
  var scrolls = el.scrollHeight > el.clientHeight + 8;
  if (scrolls && el.scrollTop + el.clientHeight >= el.scrollHeight - 4) {
    id = secs[secs.length - 1].id;
  } else {
    // Текущий — тот раздел, который пересекает нижний край панели; если под
    // краем пусто (щелчок по оглавлению оставляет зазор), берем ближайший
    // раздел ниже, а не предыдущий.
    var best = Infinity;
    for (var i = 0; i < secs.length; i++) {
      var r = secs[i].getBoundingClientRect();
      if (r.bottom <= edge) continue;
      var d = r.top <= edge ? 0 : r.top - edge;
      if (d < best) { best = d; id = secs[i].id; }
    }
  }
  Array.prototype.forEach.call(el.querySelectorAll(".repnav a"), function (a) {
    a.classList.toggle("on", "sec-" + a.dataset.sec === id);
  });
}
$("view").addEventListener("scroll", function () {
  if (view !== "sum" || repSpyBusy) return;
  repSpyBusy = true;
  requestAnimationFrame(function () { repSpyBusy = false; repSpy(); });
});

/* Открыть раздел: остальные скрыты, поэтому таблица занимает всю площадь. */
function showSection(n) {
  repSec = n;
  var el = $("view");
  Array.prototype.forEach.call(el.querySelectorAll(".grp.rep"), function (g) {
    g.classList.toggle("active", g.id === "sec-" + n);
  });
  Array.prototype.forEach.call(el.querySelectorAll(".repnav a"), function (a) {
    var on = +a.dataset.sec === n;
    a.classList.toggle("on", on);
    // Строка разделов шире окна: открытый раздел подводим к видимой части,
    // иначе выбранный ярлык остаётся за краем.
    if (on && a.scrollIntoView) a.scrollIntoView({ block: "nearest", inline: "nearest" });
  });
  el.classList.toggle('annual-overview', n === 1);
  var period = el.querySelector('select[data-sel="mset"]');
  if (period) period.closest('label').hidden = PERIOD_SECTIONS.indexOf(n) < 0;
  el.scrollTop = 0;
  // Сразу и ещё раз после кадра: в скрытой вкладке кадры не приходят, и
  // масштаб оставался непосчитанным до первого щелчка.
  applyZoom();
  requestAnimationFrame(applyZoom);
}

/* Таблицы автоматически подстраиваются по ширине, сохраняя читаемый размер. */
/* Высота липкой панели: по ней встают заголовок раздела и шапки таблиц.
   Меряем не один раз при отрисовке, а следим: панель меняет высоту, когда
   поля переносятся на второй ряд или меняется её состав, — и тогда прежнее
   значение оставляло заголовок раздела висеть посреди страницы, наезжая на
   содержимое. */
var toolWatch = null;
function trackToolbar() {
  var v = $("view"), tool = v.querySelector(".rtool");
  if (!tool) return;
  // Липких ярусов два: панель и под ней строка разделов. Заголовок раздела
  // и шапка таблицы встают под обоими, поэтому высота строки разделов тоже
  // меряется: при одинаковом отступе они наезжали друг на друга.
  var nav = v.querySelector(".repnav");
  var upd = function () {
    var pad = parseFloat(getComputedStyle(v).paddingTop) || 0;
    v.style.setProperty("--toolh",
      Math.round(tool.getBoundingClientRect().height - pad) + "px");
    var navh = 0;
    if (nav && getComputedStyle(nav).position === "sticky") {
      navh = Math.round(nav.getBoundingClientRect().height);
    }
    v.style.setProperty("--navh", navh + "px");
    // Высота видимой части отчёта: по ней широкая таблица получает свою
    // высоту и прокручивается сама, оставляя шапку граф на месте.
    v.style.setProperty("--viewh", Math.round(v.clientHeight) + "px");
  };
  upd();
  if (window.ResizeObserver) {
    if (toolWatch) toolWatch.disconnect();
    toolWatch = new ResizeObserver(upd);
    toolWatch.observe(tool);
    toolWatch.observe(v);
    if (nav) toolWatch.observe(nav);
  }
}

var reportWidthWatch = null, reportWidthBody = null;
function applyZoom() {
  var body = $("view").querySelector(".repbody");
  if (!body) return;
  if (window.ResizeObserver && reportWidthBody !== body) {
    if (reportWidthWatch) reportWidthWatch.disconnect();
    reportWidthBody = body;
    var lastWidth = body.clientWidth;
    reportWidthWatch = new ResizeObserver(function () {
      var width = body.clientWidth;
      if (width && width !== lastWidth) {
        lastWidth = width;
        requestAnimationFrame(applyZoom);
      }
    });
    reportWidthWatch.observe(body);
  }
  trackToolbar();
  // Масштаб получают только таблицы: заголовки раздела и его переключатели
  // остаются в размере панели, иначе органы управления разных уровней
  // выходили разного роста.
  var grids = body.querySelectorAll(".grp.rep.active .grid");
  var setZoom = function (z) {
    body.style.zoom = z;   // на .repbody держим для старых измерений
    Array.prototype.forEach.call(grids, function (g) { g.style.zoom = z; g.style.setProperty("--gz", z); });
    body.style.zoom = 1;
  };
  {
    body.classList.add("compact");
    setZoom(1);
    // Сначала подбираем ширину граф: перенос длинного текста сужает таблицу,
    // и после него нужен меньший масштаб, а часто он и не нужен.
    Array.prototype.forEach.call(grids, wrapWideCols);
    // Масштаб — каждой таблице свой: в разделе 6 форма кадров (6.3, двадцать
    // три графы) втрое шире окна, и общий масштаб «по худшей» оставлял в
    // размере 100 % и прокрутке все три таблицы, включая помесячную.
    Array.prototype.forEach.call(grids, function (g) {
      var one = function (z) { g.style.zoom = z; g.style.setProperty("--gz", z); };
      var worst = g.scrollWidth > g.clientWidth + 2 ? g.scrollWidth / g.clientWidth : 1;
      // Масштаб и горизонтальную прокрутку не совмещаем: при CSS zoom липкие
      // графы смещаются относительно прокручиваемой части. Если для полного
      // размещения нужен масштаб ниже 80 %, оставляем размер 100 %
      // и обычную прокрутку. Запас в два процента убирает полосу в пару пикселей.
      var need = worst <= 1 ? 1 : 0.98 / worst;
      var autoZoom = need >= 0.80 ? Math.floor(need * 100) / 100 : 1;
      one(autoZoom);
      // CSS zoom меняет расчётную ширину контейнера не так, как ширину таблицы.
      // Поэтому после применения проверяем результат, а не полагаемся на
      // предварительное отношение размеров.
      if (autoZoom < 1 && g.scrollWidth > g.clientWidth + 2) one(1);
    });
  }
  body.dataset.zoom = grids.length ? grids[0].style.zoom : "1";
  // Высота заголовка раздела зависит от отметок в нём — липкой шапке таблицы
  // нужна фактическая.
  var sec = body.querySelector(".grp.rep.active"), sech = sec && sec.querySelector(".sech");
  if (sech) sec.style.setProperty("--sech", Math.round(sech.getBoundingClientRect().height) + "px");
  fixCols();
}

/* Закрепленным графам нужен отступ слева: он равен ширине граф перед ними и
   известен только после раскладки таблицы. */
/* Автоподбор ширины граф: графа шире 220 точек переносит текст по словам.
   Считаем по готовой раскладке — какой она вышла, такую и правим; поэтому
   класс ставится здесь, а не при сборке строк. */
function wrapWideCols(g) {
  var ths = g.querySelectorAll("thead tr:not(.g1) th");
  if (!ths.length) return;
  // Пометку только ставим и не снимаем: после переноса графа становится уже
  // 220 точек, и повторный проход снял бы её, а таблица снова разъехалась.
  // При новой отрисовке разметка строится заново, и пометки нет.
  var wide = [];
  for (var i = 0; i < ths.length; i++) {
    if (ths[i].classList.contains("n") || ths[i].classList.contains("num")) continue;
    if (ths[i].classList.contains("w")) continue;
    if (ths[i].offsetWidth > 220) wide.push(i);
  }
  if (!wide.length) return;
  wide.forEach(function (i) { ths[i].classList.add("w"); });
  Array.prototype.forEach.call(g.querySelectorAll("tbody tr"), function (tr) {
    wide.forEach(function (i) {
      var c = tr.children[i];
      // Ячейка с объединением закрывает несколько граф — её не трогаем.
      if (c && (c.colSpan || 1) === 1 && !c.classList.contains("n")) c.classList.add("w");
    });
  });
}

function fixCols() {
  Array.prototype.forEach.call($("view").querySelectorAll(".grid"), function (g) {
    wrapWideCols(g);
    // Высота верхнего яруса шапки в масштабе таблицы — для липкого второго яруса.
    var g1 = g.querySelector("thead tr.g1");
    if (g1) {
      var z = parseFloat(g.style.zoom) || 1;
      g.style.setProperty("--g1h", (g1.getBoundingClientRect().height / z).toFixed(2) + "px");
    }
    var n = +(g.dataset.fix || 0);
    if (!n) { g.classList.toggle("fit", g.scrollWidth <= g.clientWidth + 2); return; }
    // Отступ берём из раскладки таблицы, а не из экранных координат: при
    // масштабе и прокрутке вбок экранные врут, а offsetLeft — нет.
    // Берём строку заголовков граф, а не верхний ярус с месяцами: у него
    // ячейки объединённые, и отступы по ним уезжали.
    var ths = g.querySelectorAll("thead tr:not(.g1) th"), lefts = [];
    // Отступ отсчитываем от первой графы таблицы: offsetLeft сам по себе
    // меряется от страницы и сдвигал закреплённые графы на её поля.
    for (var i = 0; i < n && i < ths.length; i++) {
      lefts[i] = ths[i].offsetLeft - ths[0].offsetLeft;
    }
    // Таблица помещается по ширине — снимаем свою прокрутку: тогда шапка
    // граф липнет к странице, а не к невидимой полосе внутри таблицы.
    var fits = g.scrollWidth <= g.clientWidth + 2;
    // На узком экране все заявленные закреплённые графы могут занять всю
    // видимую область. Оставляем столько, чтобы справа всегда было видно
    // прокручиваемые данные; номер строки остаётся закреплённым минимумом.
    var pinned = Math.min(n, ths.length);
    // Для освоения сохраняем показатель вместе с договором, оставляя
    // справа место для нескольких месяцев вместо ограничения в полтаблицы.
    var scrollSpace = +(g.dataset.scrollSpace || 0);
    var maxPinned = scrollSpace ? Math.max(g.clientWidth * 0.5, g.clientWidth - scrollSpace) : g.clientWidth * 0.5;
    while (pinned > 1 && lefts[pinned - 1] + ths[pinned - 1].offsetWidth > maxPinned) pinned--;
    g.classList.toggle("fit", fits);
    Array.prototype.forEach.call(g.querySelectorAll(".fx"), function (c) {
      var ci = +c.dataset.c;
      var active = !fits && ci < pinned;
      c.style.left = active ? (lefts[ci] || 0) + "px" : "";
      c.classList.toggle("fx-off", !fits && !active);
      c.classList.toggle("fxl", active && ci + (c.colSpan || 1) >= pinned);
    });
  });
}
window.addEventListener("resize", function () { if (view === "sum") applyZoom(); });
/* Месяцы, показанные в помесячных таблицах. Двенадцать месяцев по три графы
   не помещаются ни в один экран, и экономист возит таблицу вправо-влево.
   Полугодие или квартал помещается целиком. */
var repMon = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12];
var repEven = false, repOst = false;
/* Открытый раздел. Отчёт показывает одну таблицу во всю площадь: полотно из
   тринадцати таблиц читалось только при мелком масштабе. */
var repSec = 1;
var MON_SETS = [["год", [1,2,3,4,5,6,7,8,9,10,11,12]],
                 ["I полугодие", [1,2,3,4,5,6]], ["II полугодие", [7,8,9,10,11,12]],
                 ["I квартал", [1,2,3]], ["II квартал", [4,5,6]],
                 ["III квартал", [7,8,9]], ["IV квартал", [10,11,12]]];
var PERIOD_SECTIONS = [3, 4, 6, 7, 11];
function monSetName() {
  for (var i = 0; i < MON_SETS.length; i++) {
    if (MON_SETS[i][1].join() === repMon.join()) return MON_SETS[i][0];
  }
  return "";
}
function monHead(fn) {
  return repMon.map(function (m) { return { t: fn ? fn(MON[m - 1], m - 1) : MON[m - 1] }; });
}
function repGoalsHtml() {
  var run = (state.runs || []).filter(function (r) { return r.id === vrun; })[0];
  var s = (run && run.summary) || {};
  // Только взвешенные цели: у них единицы понятны экономисту (штуки, рубли).
  // Значения стадий-правил — внутренние суммы штрафов, их не показываем.
  var goals = (s["цели"] || []).filter(function (g) { return g["приоритет"] === "вес"; });
  var settings = (vrep && vrep["настройки"]) || [];
  var h = "";
  // 13.1 Версии расчёта: статус, время, дата и взвешенные цели каждой версии
  // одной таблицей — сравнение версий по числам и есть работа отчёта.
  var runs = state.runs || [], gcols = [], seen = {};
  runs.forEach(function (r) {
    (((r.summary || {})["цели"]) || []).forEach(function (g) {
      if (g["приоритет"] !== "вес" || seen[g["цель"]]) return;
      seen[g["цель"]] = true;
      gcols.push({ key: g["цель"], t: (GOAL_SHORT[g["цель"]] || g["цель"]) + (g["единица"] ? ", " + g["единица"] : "") });
    });
  });
  if (runs.length) {
    var vrows = runs.map(function (r) {
      var by = {};
      (((r.summary || {})["цели"]) || []).forEach(function (g) { by[g["цель"]] = g; });
      var ok = r.status === "OPTIMAL";
      return { cls: r.id === vrun ? "sel" : "", cells: [
        nc(String(r.id)),
        { v: esc(ok ? "посчитан" : r.status), cls: ok ? "ok" : (r.status === "идет" ? "" : "er") },
        nc(r.seconds != null ? String(Math.round(r.seconds)) : "—"),
        r.created || "",
      ].concat(gcols.map(function (c) {
        var g = by[c.key];
        if (!g) return gc("—");
        return nc(g["единица"] === "₽" ? rmi(g["значение"]) : fmtGoal(g["значение"]));
      }), [ok ? { v: '<a class="xl" href="/api/case/' + caseId + "/result/" + r.id + '">xlsx</a>' } : ""]) };
    });
    h += secH(13, "Расчёт") + subH("13.1", "Версии") + repT(
      [{ t: "Версия" }, "Статус", { t: "Время, с" }, "Дата"].concat(
        gcols.map(function (c) { return { t: c.t }; }), ["Файл"]), vrows, null, { fix: 1 });
  }
  if (goals.length || settings.length) {
    var rows = goals.map(function (g) {
      return [g["цель"], nc(g["единица"] === "₽" ? rmi(g["значение"]) : fmtGoal(g["значение"])),
              g["единица"] || "", nc(g["вес"] != null ? fmtGoal(g["вес"]) : null)];
    });
    // Настройки расчёта — теми же строками: экономист меняет их в чате и
    // должен видеть, с чем считался этот план.
    settings.forEach(function (x) {
      var v = x["значение"], name = x["имя"], txt = String(v), unit = "";
      if (name === "допуск трудоёмкости") { txt = rpn(Number(v)); unit = "%"; }
      else if (name === "макс договоров оклада в год") unit = "шт";
      rows.push([{ v: esc(name), cls: "set" }, nc(txt), unit, ""]);
    });
    h += (runs.length ? "" : secH(13, "Расчёт")) + subH("13.2", "Цели и настройки") + repT(
      ["Цель", { t: "Значение" }, "Единица", { t: "Вес" }], rows);
  }
  if (runs.length || goals.length || settings.length) h += "</div>";
  var def = (vrep && vrep["дефицит"]) || [];
  if (def.length) {
    var total = 0;
    def.forEach(function (d) { total += d["дефицит"] || 0; });
    h += secH(14, "Дефицит выплат", chip(rm(total) + " ₽", "er")) + repT(
      ["Табельный", "Сотрудник", "Месяц", { t: "Положено, ₽" }, { t: "Выплачено, ₽" }, { t: "Дефицит, ₽" }, "Причина"],
      def.map(function (d, i) {
        return {reviewKey:"deficit:"+i,cells:[d["табельный"], d["фио"], MON[d["месяц"] - 1], nc(rm(d["положено"])),
                nc(rm(d["выплачено"])), nc(rm(d["дефицит"]), "err"), d["причина"] || ""]};
      }), null, { fix: 2, tall: true, merge: [0, 1] }) + "</div>";
  } else {
    h += secH(14, "Дефицит выплат", chip("не выявлен", "ok")) +
      '<div class="none">В выбранной версии расчёта дефицит выплат сотрудникам не выявлен.</div></div>';
  }
  return h;
}

/* Diagnostics use the selected run only; a successful solve is not a clean audit. */
var issueReturn = null;
function currentIssues() { return PlanIssues.collect(vrep, vrules, vresult, vrun); }
function reportIssues(report, rules, result) {
  return PlanIssues.collect(report,rules,result,vrun).map(function (x) { return {sec:x.target.section,level:x.severity==='error'?'error':'warn',title:x.title,detail:x.subject}; });
}
function reportOverview() { return PlanIssues.render(currentIssues()); }
function reportOverviewChips() {
  var rows=currentIssues();
  return rows.some(function(x){return x.severity==='error';}) ? chip('Есть ошибки','er') : rows.length ? chip('Есть замечания','wr') : chip('Замечаний нет','ok');
}
function openPlanIssue(id) {
  var issue=currentIssues().find(function(x){return x.id===id;});
  if(!issue) return;
  issueReturn={scroll:$("view").scrollTop,mon:repMon.slice(),filters:repColFilter,hidden:repHideCol,id:id};
  repColFilter={}; repHideCol={};
  if(issue.target.month) repMon=[issue.target.month];
  repSec=issue.target.section; renderView(); showSection(repSec);
  var section=$("view").querySelector('.rep.active');
  if(!section) return;
  requestAnimationFrame(function(){
    var row=Array.prototype.find.call(section.querySelectorAll('[data-review-key]'),function(r){return r.dataset.reviewKey===issue.target.row;});
    // Ключ по человеку и виду выплаты: строк столько же, сколько договоров,
    // и годится первая — она приведёт глаз в нужное место таблицы.
    if(!row && /^pay:/.test(issue.target.row||'')) row=section.querySelector('[data-review-key^="'+issue.target.row+'"]');
    if(!row) { var note=document.createElement('p');note.className='issue-empty';note.textContent='Точная строка недоступна в этой версии отчёта. '+issue.subject+' — '+issue.title;section.prepend(note);return; }
    var fold=row.closest('details');if(fold)fold.open=true;
    row.classList.add('review-target');row.tabIndex=-1;
    var cell=issue.target.column==null?null:row.querySelector('[data-source-col="'+issue.target.column+'"]');
    if(cell)cell.classList.add('issue-target-cell');
    (cell||row).scrollIntoView({block:'center',inline:'center'});row.focus({preventScroll:true});
  });
}

function reviewRuleTitle(r) {
  return r["правило"] === "ФОТ договоров освоен"
    ? "Нераспределённый ФОТ по договорам" : r["правило"];
}

function reviewRuleTarget(r) {
  var where = String(r["где"] || "").toLowerCase();
  if (where.indexOf("освоен") >= 0) return { section: 3, label: "Освоение" };
  if (where.indexOf("ставк") >= 0) return { section: 7, label: "Ставки" };
  if (where.indexOf("бэп") >= 0) return { section: 8, label: "БЭП" };
  if (where.indexOf("п4") >= 0) return { section: 9, label: "П4" };
  if (where.indexOf("труд") >= 0) return { section: 10, label: "Трудоёмкость" };
  if (where.indexOf("договор") >= 0) return { section: 2, label: "Договоры" };
  return null;
}

function reviewRuleSummary(r) {
  if (r["правило"] !== "ФОТ договоров освоен") return r["факт"] || "";
  var totals = (vrep && vrep["итоги"]) || {};
  var paid = totals["выплачено"], limit = totals["ФОТ"];
  if (paid == null || limit == null) return cap(r["факт"] || "");
  return "Выплачено " + mo(paid) + " ₽ из " + mo(limit) + " ₽. Не распределено " +
    mo(limit - paid) + " ₽ (" + rpn(limit ? (limit - paid) / limit : 0) + " % ФОТ).";
}

function reviewPercent(v) {
  if (v == null) return "—";
  var n = Math.round(v * 10000) / 100;
  var digits = n < 100 && n >= 99.9 ? 2 : 1;
  return n.toFixed(digits).replace(".", ",");
}

/* В карточке замечания названия колонок описывают бизнес-величины. Общие
   «Факт / Предел / Запас» не объясняли, является ли запас хорошим результатом
   или нераспределёнными деньгами. */
function reviewRuleTable(r) {
  var rows = r["строки"] || [];
  if (!rows.length) return "";
  var numeric = rows[0]["предел"] !== undefined && rows[0]["предел"] !== null;
  if (!numeric) {
    return '<div class="review-rule-scroll"><table class="review-table review-rule-table"><thead><tr>' +
      '<th scope="col">Объект проверки</th><th scope="col">Результат проверки</th></tr></thead><tbody>' +
      rows.map(function (s, i) {
        return '<tr data-review-key="rule:' + vrules.indexOf(r) + ':' + i + '"><th scope="row">' +
          esc(s["объект"]) + '</th><td>' + esc(s["что"] || "—") + '</td></tr>';
      }).join("") + "</tbody></table></div>";
  }
  var spend = r["правило"] === "ФОТ договоров освоен";
  var unit = r["единица"] || "";
  var suffix = unit ? ", " + unit : "";
  var headers = spend
    ? ["Договор", "Выплаты по плану, ₽", "Лимит ФОТ, ₽", "Не распределено, ₽", "Освоено, %"]
    : ["Объект проверки", "Проверенное значение" + suffix,
       cap(r["подпись предела"] || "допустимый предел") + suffix,
       "Результат относительно предела", "Использовано, %"];
  return '<div class="review-rule-scroll"><table class="review-table review-rule-table"><thead><tr>' +
    headers.map(function (h, i) {
      return '<th scope="col"' + (i ? ' class="amount"' : "") + '>' + esc(h) + '</th>';
    }).join("") + '</tr></thead><tbody>' + rows.map(function (s, i) {
      var delta;
      if (spend) delta = mo(s["запас"]);
      else if (s["запас"] < 0) delta = '<strong class="rule-over">превышение ' + mo(-s["запас"]) +
                                        (unit ? " " + esc(unit) : "") + '</strong>';
      else delta = '<span class="rule-reserve">запас ' + mo(s["запас"]) +
                   (unit ? " " + esc(unit) : "") + '</span>';
      return '<tr data-review-key="rule:' + vrules.indexOf(r) + ':' + i + '" class="' +
        (s["нарушено"] ? "over" : "") + '"><th scope="row">' + esc(s["объект"]) +
        (s["таких же"] > 1 ? '<small>Ещё ' + (s["таких же"] - 1) +
                             " с тем же результатом</small>" : "") +
        '</th><td class="amount">' + mo(s["факт"]) + '</td><td class="amount">' +
        mo(s["предел"]) + '</td><td class="amount rule-delta' +
        (spend && s["запас"] > 0.5 ? " warn" : (s["запас"] < 0 ? " error" : "")) + '">' +
        delta + '</td><td class="amount rule-share"><span>' + reviewPercent(s["доля"]) +
        ' %</span>' + bar(s["доля"], s["нарушено"] ? "bad" : (spend && s["запас"] > 0.5 ? "warn" : "ok")) +
        "</td></tr>";
    }).join("") + "</tbody></table></div>";
}

function groupedRunWarnings(warnings) {
  var groups = [], by = {};
  warnings.forEach(function (w) {
    // Остаток уже разобран выше по каждому договору; повторять его второй раз
    // в журнале значит выдавать одну проблему за две.
    if (/неосвоенный остаток/i.test(w[4] || "")) return;
    var key = [w[0], w[1], w[2], w[4], w[5], w[6]].join("\u0001");
    if (!by[key]) {
      by[key] = { level: w[0], area: w[1], subject: w[2], title: w[4],
                  value: w[5], action: w[6], periods: [] };
      groups.push(by[key]);
    }
    if (w[3] && by[key].periods.indexOf(w[3]) < 0) by[key].periods.push(w[3]);
  });
  return groups;
}

function warningPeriods(periods) {
  if (!periods.length) return "";
  if (periods.length <= 3) return periods.join(", ");
  return periods[0] + "–" + periods[periods.length - 1] + " · " + periods.length + " мес.";
}

function reportChecksSection() {
  var warnings = ((vresult || {}).warnings || []).filter(function (w) { return Array.isArray(w) && /^(Ошибка|Предупреждение)$/.test(w[0]); });
  var attention = (vrules || []).filter(function (r) { return r["состояние"] === "нарушено" || r["состояние"] === "внимание"; }).sort(function (a, b) { return (a["состояние"] === "нарушено" ? 0 : 1) - (b["состояние"] === "нарушено" ? 0 : 1); });
  var messageGroups = groupedRunWarnings(warnings);
  var badCount = attention.filter(function (r) { return r["состояние"] === "нарушено"; }).length;
  var headChips = attention.length
    ? (badCount ? chip(badCount + " " + px(badCount, "нарушение", "нарушения", "нарушений"), "er") : "") +
      chip(attention.length + " " + px(attention.length, "замечание", "замечания", "замечаний"), "wr")
    : chip("Замечаний нет", "ok");
  if (messageGroups.length) headChips += chip(messageGroups.length + " " +
    px(messageGroups.length, "группа сообщений", "группы сообщений", "групп сообщений"));

  var primary = attention.length
    ? '<section class="checks-attention" aria-label="Замечания к расчёту"><div class="review-section-title"><h3>Требует внимания</h3><span class="review-count">' +
      attention.length + '</span></div>' + attention.map(function (r) {
        var target = reviewRuleTarget(r);
        return '<article class="review-rule ' + (r["состояние"] === "нарушено" ? "bad" : "warn") +
          '" data-review-key="rule:' + vrules.indexOf(r) + ':-1"><div class="review-heading"><div class="review-heading-copy"><h4>' +
          esc(reviewRuleTitle(r)) + '</h4><p>' + esc(reviewRuleSummary(r)) + '</p></div><div class="review-heading-actions">' +
          chip(r["состояние"] === "нарушено" ? "Нарушение" : "Внимание",
               r["состояние"] === "нарушено" ? "er" : "wr") +
          (target ? '<button type="button" class="review-open" data-report-sec="' + target.section +
                    '">Открыть «' + esc(target.label) + '»</button>' : "") +
          '</div></div>' + reviewRuleTable(r) +
          '<details class="review-explain"><summary>Условие проверки</summary><p>' + esc(r["смысл"]) +
          '</p></details></article>';
      }).join("") + "</section>"
    : '<div class="checks-clear">Отклонений и замечаний по доступным проверкам нет.</div>';

  var allRules = vrules && vrules.length
    ? '<details class="fold2 checks-fold"><summary><span>Все проверки</span><span class="checks-fold-count">' +
      vrules.length + '</span></summary><div class="checks-fold-body">' + rulesView() + "</div></details>"
    : '<div class="none" data-review-key="checks-unavailable">Проверки этой версии недоступны.</div>';
  var messages = messageGroups.length
    ? '<details class="fold2 checks-fold"><summary><span>Дополнительные сообщения</span><span class="checks-fold-count">' +
      messageGroups.length + " " + px(messageGroups.length, "группа", "группы", "групп") + " · " +
      warnings.filter(function (w) { return !/неосвоенный остаток/i.test(w[4] || ""); }).length +
      ' записей</span></summary><div class="checks-fold-body message-groups">' + messageGroups.map(function (g) {
        return '<article class="message-group ' + (g.level === "Ошибка" ? "error" : "warn") + '"><div class="message-group-head"><strong>' +
          esc(g.title || g.area) + '</strong>' + chip(g.level === "Ошибка" ? "Ошибка" : "Внимание",
          g.level === "Ошибка" ? "er" : "wr") + '</div><p class="message-group-meta">' +
          esc([g.subject, g.area, warningPeriods(g.periods)].filter(Boolean).join(" · ")) + '</p>' +
          (g.action ? '<p class="message-group-action">' + esc(g.action) + '</p>' : "") + "</article>";
      }).join("") + "</div></details>" : "";
  return secH(15, "Контроль расчёта", headChips) + '<div class="checks-page">' + primary +
    '<div class="checks-secondary">' + allRules + messages + "</div></div></div>";
}

function repSummary() {
  var r = vrep, t = r["итоги"], fot = t["ФОТ"] || 0;
  var h = secH(1, "Итоги за " + (r["год"] || "") + " год", reportOverviewChips());
  h += reportOverview() + '<div class="review-section-title"><h3>Годовые показатели</h3></div><div class="review-block"><table class="review-table review-totals"><thead><tr><th scope="col">Показатель</th><th scope="col" class="amount">Сумма</th><th scope="col" class="amount">Доля от лимита ФОТ</th></tr></thead><tbody>' +
    [["Лимит ФОТ по договорам", t["ФОТ"]], ["Поступления на счета", t["поступило"]], ["Выплаты по плану", t["выплачено"]], ["Нераспределённый ФОТ на конец года", t["остаток ФОТ"]], ["Остаток средств на счетах на конец года", t["остаток на счетах"]]].map(function (x) {
      return '<tr><th scope="row">' + esc(x[0]) + '</th><td class="amount">' + esc(rm(x[1])) + ' ₽</td><td class="amount">' + (fot ? esc(rpn(x[1] / fot)) + ' %' : '—') + '</td></tr>';
    }).join('') + '</tbody></table></div></div>';

  var yy = String(r["год"] || "").slice(2);
  var stc = { ok: 0, wr: 0, er: 0 };
  r["договоры"].forEach(function (c) {
    var st = c["статус"]; stc[st === "в срок" ? "ok" : (/после|нет выплат/.test(st) ? "er" : "wr")]++;
  });
  // Одна графа — одно поле, поэтому условия договора и итог за год стоят
  // двумя таблицами: восемнадцать граф в один экран не помещаются.
  h += secH(2, "Договоры",
            (stc.ok ? chip(stc.ok + " в срок", "ok") : "") + (stc.wr ? chip(stc.wr + " не освоен", "wr") : "") +
            (stc.er ? chip(stc.er + " с нарушением", "er") : "")) +
    subH("2.1", "Условия договоров") + repT(
    ["Договор", "Наименование", "Подразделение", "Признак", "Лицевой счёт", "Дата начала",
     "Дата окончания", "Срок выплат", "Разрешённые выплаты"],
    r["договоры"].map(function (c) {
      // Признак — одно поле в словаре экономистов (грант, внебюджет, план,
      // ГОЗ, приоритет); пока в реестре нет своего поля, у обычного договора
      // здесь стоит вид работы.
      return [c["код"], c["название"] || "—", c["подразделение"] || "—", c["признак"] || "—",
              c["счет"] || "—",
              c["дата начала"], c["дата окончания"], c["срок выплат"], c["выплаты"] || "—"];
    }), null, { fix: 1 }) +
    subH("2.2", "Состояние на конец года") + repT(
    ["Договор", { t: "Месяцев с выплатами" },
     { t: "Месяцев действия" }, { t: "ФОТ за год, ₽" }, { t: "Поступило за год, ₽" },
     { t: "Выплачено за год, ₽" }, { t: "Остаток на 31.12, ₽" },
     { t: "Освоение за год, %" }, "Статус за год"],
    r["договоры"].map(function (c) {
      var st = c["статус"], scls = st === "в срок" ? "ok" : (/после|нет выплат/.test(st) ? "er" : "wr");
      return { reviewKey: c["код"], cells: [c["код"], nc(c["месяцев с выплатами"]), nc(c["месяцев в окне"]),
              nc(rm(c["ФОТ"])), nc(rm(c["поступило"])), nc(rm(c["выплачено"])),
              nc(rm(c["остаток"]), c["остаток"] > 0 ? "tight" : ""), nc(rpn(c["освоение"])),
              { v: esc(st), cls: scls }] };
    }), null, { fix: 1 }) + "</div>";

  // Дополнительные показатели идут строками, а не графами: графа на каждый
  // месяц утраивала ширину, и таблица снова начинала ездить вбок.
  var u = r["освоение"];
  // Все три показателя в таблице; лишние экономист убирает автофильтром по
  // графе «Показатель», как в Excel.
  var mets = [["Выплачено по плану, ₽", "факт"], ["Равномерное освоение, ₽", "план"],
              ["Остаток ФОТ на конец, ₽", "остатки"]];
  var head = ["Договор", "Признак", "Лицевой счёт", "Показатель"].concat(monHead());
  var val = function (src, key, i) {
    return key === "остатки" ? src["остатки"][i + 1] : src[key][i];
  };
  var urows = [];
  u["договоры"].forEach(function (c) {
    mets.forEach(function (mt) {
      urows.push([c["код"], c["признак"] || "—", c["счет"] || "—", mt[0]]
        .concat(repMon.map(function (mn) {
          var i = mn - 1, v = val(c, mt[1], i);
          if (v == null) return gc();
          var pl = c["план"][i];
          var off = mt[1] === "факт" && pl != null &&
                    Math.abs(v - pl) > Math.max(1, 0.05 * Math.abs(pl));
          return nc(rm(v), off ? "tight" : "");
        })));
    });
  });
  mets.forEach(function (mt, mi) {
    urows.push({ cls: "sum all", cells: [{ v: "Итого по организации", cs: 3 }, { skip: true }, { skip: true }, mt[0]]
      .concat(repMon.map(function (mn) {
        var v = val(u["итого"], mt[1], mn - 1);
        return v == null ? gc() : nc(rm(v));
      })) });
  });
  h += secH(3, "Освоение ФОТ по договорам") +
       repT(head, urows, null,
            { fix: 4, scrollSpace: 240, tall: true, groups: monGroupRow(4, 1, 0, "quarter"), merge: [0, 1, 2] }) +
       '<div class="lg"><i class="tight"></i>отличие от равномерного освоения более 5 %</div>' + "</div>";
  return h;
}

function repCash() {
  var rows = [];
  var names = [["начало", "Остаток на начало, ₽"], ["поступление", "Поступление, ₽"],
               ["доступно", "Доступно, ₽"], ["выплаты", "Выплаты, ₽"],
               ["конец", "Остаток на конец, ₽"], ["освоено", "Освоено нарастающим итогом, %"]];
  vrep["касса"].forEach(function (c) {
    names.forEach(function (n) {
      var arr = c["строки"][n[0]];
      var year = n[0] === "поступление" ? rm(c["год"]["поступление"])
               : (n[0] === "выплаты" ? rm(c["год"]["выплаты"])
               : (n[0] === "конец" ? rm(c["год"]["конец"]) : null));
      rows.push({ reviewKey:"cash:"+c["код"]+":"+n[0], cls: n[0] === "освоено" ? "sum" : "",
        cells: [c["код"], n[1]].concat(repMon.map(function (mn) {
          var v = arr[mn - 1];
          if (v == null) return gc();
          if (v === "запрет") return gc("запрет");
          return nc(n[0] === "освоено" ? rpn(v) : rmi(v), (n[0] === "конец" && v < 0) ? "err" : "");
        }), [year == null ? gc("—") : nc(year)]) });
    });
  });
  return secH(4, "Касса договоров по месяцам") +
    repT(["Договор", "Показатель"].concat(monHead(), [{ t: "За год" }]), rows, null,
         { fix: 2, tall: true, groups: monGroupRow(2, 1, 1, "quarter"), merge: [0] }) + "</div>";
}

function repRegisterRows() {
  var out = [];
  vrep["регистр"].forEach(function (s) {
    var months = [];
    s["периоды"].forEach(function (p) {
      for (var m = p["с"]; m <= p["по"]; m++) months.push({ m: m, p: p });
    });
    if (repPeriod === "month") {
      months = months.filter(function (x) { return repMon.indexOf(x.m) >= 0; });
    }
    var parts = [];
    months.forEach(function (x) {
      var last = parts[parts.length - 1];
      if (last && last.p === x.p && last.to === x.m - 1) { last.to = x.m; last.k++; }
      else parts.push({ from: x.m, to: x.m, k: 1, p: x.p });
    });
    parts.forEach(function (q) {
      var p = q.p, total = (p["фонд зп"] + p["фонд надбавок"]) * q.k;
      out.push({ s: s, p: p, q: q, total: total,
        key: repGroup === "e" ? s["табельный"] + "\u0001" + s["договор"] : s["договор"] + "\u0001" + s["табельный"] });
    });
  });
  out.sort(function (a, b) { return a.key < b.key ? -1 : (a.key > b.key ? 1 : 0); });
  return out;
}

/* Регистр как в учетной форме: строка-группа — сотрудник (или договор) с
   итогом, под ней строки по договорам (или людям) и периодам. */
function repRegisterGrid() {
  var regs = repRegisterRows(), grand = 0, rows = [], byE = repGroup === "e";
  var head = byE
    ? ["Договор", "Лицевой счёт оклада"]
    : ["Табельный", "ФИО", "Должность", "Лицевой счёт оклада"];
  head = head.concat(["Занятость", { t: "Ставка" }, "Период", { t: "Месяцев" },
    { t: "Фонд заработной платы в месяц, ₽" }, { t: "Фонд надбавок в месяц, ₽" },
    "Лицевой счёт надбавки", "Код надбавки", { t: "Итого за период, ₽" },
    { t: "Лимит на ставку, ₽" }, { t: "Запас до лимита, ₽" }]);
  var idn = byE ? 2 : 4, ncol = head.length;
  var gkey = null, gsum = 0, gi = -1;
  var flush = function () { if (gi >= 0) rows[gi].cells[1] = nc(rm(gsum)); };
  regs.forEach(function (x) {
    var s = x.s, p = x.p, q = x.q, k = byE ? s["табельный"] : s["договор"];
    if (k !== gkey) {
      flush(); gkey = k; gsum = 0; gi = rows.length;
      var label = byE ? "<b>" + esc(s["фио"]) + "</b>" : ctrHead(s["договор"]);
      rows.push({ grp: true, g: k, cells: [{ v: label, cs: ncol - 3 }, nc(""), { v: "", cs: 2 }] });
    }
    gsum += x.total; grand += x.total;
    var cells = byE
      ? [s["договор"], s["лицевой счет оклада"] || ""]
      : [s["табельный"], s["фио"], s["должность"], s["лицевой счет оклада"] || ""];
    cells = cells.concat([p["занятость"] || "", nc(p["ставка"] ? rf(p["ставка"]) : ""),
      q.from === q.to ? MON[q.from - 1] : MON[q.from - 1] + "–" + MON[q.to - 1], nc(q.k),
      nc(rm(p["фонд зп"])), nc(rm(p["фонд надбавок"])),
      s["лицевой счет надбавки"] || "", s["код надбавки"] || "", nc(rm(x.total)),
      nc(rm(p["лимит"])), nc(rm(p["запас"]), p["запас"] != null && p["запас"] <= 0 ? "err" : "")]);
    rows.push({ g: k, cells: cells });
  });
  flush();
  rows.push({ cls: "sum", cells: [{ v: esc("Итого" + (repPeriod === "year" ? " за год" : (repMon.length === 1
              ? " за " + MON[repMon[0] - 1] : " за выбранные месяцы"))), cs: ncol - 3 }, nc(rm(grand)), { v: "", cs: 2 }] });
  return grid(head, rows, { fix: idn, tall: true });
}

function repPlan() {
  var h = secH(5, "Выплаты по договорам и видам") + repT(
    ["Договор", "Вид выплаты", { t: "Сумма за год, ₽" }, { t: "Доля договора, %" },
     { t: "Доля фонда, %" }, { t: "Месяцев" }],
    vrep["виды"].map(function (k) {
      return [k["договор"], k["вид"], nc(rm(k["сумма"])), nc(rpn(k["доля договора"])),
              nc(rpn(k["доля фонда"])), nc(k["месяцев"])];
    }), null, { fix: 2, merge: [0] }) + "</div>";

  // Группировка одна на обе таблицы раздела; выбор строк — только регистра.
  var ctlG = '<div class="ctl"><label class="fld">Группировка<select data-rg>' +
    '<option value="e"' + (repGroup === "e" ? " selected" : "") + ">по сотрудникам</option>" +
    '<option value="c"' + (repGroup === "c" ? " selected" : "") + ">по договорам</option></select></label></div>";
  var ctl = '<div class="ctl"><label class="fld">Строки<select data-rp>' +
    '<option value="year"' + (repPeriod === "year" ? " selected" : "") + ">за год</option>" +
    '<option value="month"' + (repPeriod === "month" ? " selected" : "") + ">по выбранному периоду</option>" +
    "</select></label></div>";
  // 6.1 — план выплат по месяцам: сотрудник → договор → вид выплаты.
  var pm = vrep["помесячно"] || { "сотрудники": [], "итого": [], "год": 0 };
  var mrows = [], byE = repGroup === "e";
  var mcells = function (arr, code) {
    return repMon.map(function (mn) { return mc(arr[mn - 1], code, mn, rmi); });
  };
  var KIND_ORDER = ["оклад", "120", "122", "124", "152", "приказ"];
  if (byE) {
    // Договоры человека идут подряд, внутри договора оклад первым; строки
    // оклада выделены, чтобы находить их глазами, не разбивая договоры.
    pm["сотрудники"].forEach(function (p) {
      p["строки"].forEach(function (l) {
        mrows.push({ cls: l["вид"] === "оклад" ? "okl" : "",
          reviewKey: "pay:" + p["табельный"] + ":" + l["вид"],
          cells: [p["табельный"], p["фио"], l["договор"], l["вид"]].concat(
            mcells(l["месяцы"], l["договор"]), [nc(rmi(l["год"]))]) });
      });
      mrows.push({ cls: "sum", cells: [p["табельный"], p["фио"], { v: "Итого по сотруднику", cs: 2 }, { skip: true }]
        .concat(mcells(p["итого"]), [nc(rmi(p["год"]))]) });
    });
  } else {
    // По договорам: те же строки, перегруппированные; итог по договору по месяцам.
    var byC = {};
    pm["сотрудники"].forEach(function (p) {
      p["строки"].forEach(function (l) {
        (byC[l["договор"]] = byC[l["договор"]] || []).push({ p: p, l: l });
      });
    });
    Object.keys(byC).sort().forEach(function (code) {
      var tot = [], year = 0;
      byC[code].forEach(function (x) {
        mrows.push({ cls: x.l["вид"] === "оклад" ? "okl" : "",
          reviewKey: "pay:" + x.p["табельный"] + ":" + x.l["вид"],
          cells: [code, x.p["табельный"], x.p["фио"], x.l["вид"]].concat(
            mcells(x.l["месяцы"], code), [nc(rmi(x.l["год"]))]) });
        x.l["месяцы"].forEach(function (v, i) { if (v) tot[i] = (tot[i] || 0) + v; });
        year += x.l["год"] || 0;
      });
      mrows.push({ cls: "sum", cells: [code, { v: "Итого по договору", cs: 3 }, { skip: true }, { skip: true }]
        .concat(mcells(tot, code), [nc(rmi(year))]) });
    });
  }
  if (mrows.length) {
    mrows.push({ cls: "sum all", cells: [{ v: "Итого по организации", cs: 4 }, { skip: true }, { skip: true }, { skip: true }].concat(
      repMon.map(function (mn) { return mc(pm["итого"][mn - 1], null, mn, rmi); }),
      [nc(rmi(pm["год"]))]) });
  }
  h += secH(6, "Выплаты сотрудникам") + ctlG +
    subH("6.1", "По месяцам") + (mrows.length ? repT(
      (byE ? ["Табельный", "ФИО", "Договор", "Вид выплаты"] : ["Договор", "Табельный", "ФИО", "Вид выплаты"])
        .concat(monHead(), [{ t: "За год, ₽" }]),
      mrows, null, { fix: 4, tall: true, groups: monGroupRow(4, 1, 1, "quarter"), merge: [0, 1, 2] })
      : '<div class="none">выплат нет</div>') +
    subH("6.2", "Регистр по периодам") + ctl + repRegisterGrid() +
    subH("6.3", "ШР на дату, детализация назначений") + repStaffDetail() + "</div>";

  var srows = [];
  vrep["ставки"].forEach(function (p) {
    p["договоры"].forEach(function (c, ci) {
      [["основное", true], ["совместительство", false]].forEach(function (kind) {
        var vals = repMon.map(function (mn) {
          var mi = mn - 1, v = c["месяцы"][mi];
          return v && !!c["основное"][mi] === kind[1] ? v : null;
        });
        var all = c["месяцы"].filter(function (v, mi) { return v && !!c["основное"][mi] === kind[1]; });
        if (!all.length) return;
        var mx = Math.max.apply(null, all);
        srows.push({ reviewKey: "rate:" + p["табельный"], cells: [p["табельный"], p["фио"], c["код"], kind[0]].concat(
          vals.map(function (v, i) {
            return mc(v, c["код"], repMon[i], rf,
                      (!kind[1] && v >= 0.5 - 0.001) ? "tight" : "");
          }), [nc(rf(mx), (!kind[1] && mx >= 0.5 - 0.001) ? "tight" : "")]) });
      });
    });
    srows.push({ cls: "sum", reviewKey: "rate:" + p["табельный"], cells: [p["табельный"], p["фио"], "Всего", gc("—")].concat(
      repMon.map(function (mn) {
        var v = p["всего"][mn - 1];
        return v == null ? gc("—") : nc(rf(v), v >= p["предел"] - 0.001 ? "tight" : "");
      }), [nc(rf(p["макс"]), p["макс"] >= p["предел"] - 0.001 ? "tight" : "")]) });
  });
  // Сведения о человеке — должность, подразделение, категория, тип
  // занятости — стоят здесь один раз: регистр и ставки называют человека
  // табельным номером и ФИО, как договор в других разделах — шифром.
  var pinfo = {};
  (vrep["регистр"] || []).forEach(function (s) {
    if (!pinfo[s["табельный"]]) pinfo[s["табельный"]] = s;
  });
  var prows = vrep["ставки"].map(function (p) {
    var s = pinfo[p["табельный"]] || {};
    return [p["табельный"], p["фио"], s["должность"] || "—", s["отдел"] || "—",
            s["категория персонала"] || "—", p["тип занятости"] || s["тип занятости"] || "—",
            p["категория занятости"] || "—", nc(rf(p["штатная"])), nc(rf(p["предел"]))];
  });
  h += secH(7, "Сотрудники и ставки") +
    subH("7.1", "Сотрудники") + repT(
    ["Табельный", "ФИО", "Должность", "Подразделение", "Категория персонала", "Тип занятости",
     "Категория занятости", { t: "Штатная ставка" }, { t: "Предел ставки" }], prows, null, { fix: 1 }) +
    subH("7.2", "Ставки по месяцам") + repT(
    ["Табельный", "ФИО", "Договор", "Занятость"].concat(monHead(), [{ t: "Макс в плане" }]),
    srows, null,
    { fix: 3, tall: true, groups: monGroupRow(4, 1, 1, "quarter"), merge: [0, 1] }) +
    '<div class="lg"><i class="tight"></i>на пределе</div></div>';
  return h;
}

/* 6.3 — форма отдела кадров «ШР на дату, детализация»: строка на каждое
   назначение (человек, договор, параметр, период). Графы как в шаблоне
   кадров; номер и дата приказа сервису неизвестны, графы пустые. */
function repStaffDetail() {
  var rows = vrep["шр"] || [];
  if (!rows.length) return '<div class="none">назначений нет</div>';
  var head = ["Таб.№", "Назначение", "Фамилия И.О., уч. ст., уч. зван.", "Код подр.", "Подразделение",
    "Должность", "Категория персонала", "Код пар-ра", "Название параметра", { t: "Ставка" },
    { t: "Номинальное значение параметра" }, { t: "Значение параметра по ставке" }, { t: "Сумма в руб." },
    "Начало действия", "Окончание действия", "Код шифра затрат", "Шифр затрат", "Лицевой счет",
    "УИ ПНИЭР", "Номер приказа ввода", "Дата приказа ввода", "Номер приказа закрытия", "Дата приказа закрытия"];
  var body = rows.map(function (r) {
    return [r["таб"], r["назначение"] || gc("—"), r["фио"], r["код_подр"] || gc("—"), r["подразделение"] || gc("—"),
      r["должность"], r["категория"] || gc("—"), r["код"], r["параметр"], nc(rf(r["ставка"])),
      nc(rm(r["номинал"])), nc(rm(r["по_ставке"])), nc(rm(r["сумма"])), r["начало"], r["окончание"],
      r["код_шифра"], r["шифр"] || gc("—"), r["счет"] || gc("—"), gc("—"), gc("—"), gc("—"), gc("—"), gc("—")];
  });
  return repT(head, body, null, { fix: 3, tall: true, merge: [0, 2] });
}

function repLimits() {
  var dev = function (v) {
    return { v: v == null ? "—" : rs(v * 100, true), cls: "n " + (v == null ? "" : (v > 0.0005 ? "er" : (v < -0.0005 ? "ok" : ""))) };
  };
  var bepBad = vrep["бэп"].filter(function (b) { return b["отклонение"] > 0.0005; }).length;
  var h = secH(8, "БЭП по ГОЗ-договорам", vrep["бэп"].length
             ? (bepBad ? chip(bepBad + " выше БЭП", "er") : chip("в пределах", "ok")) : "") + (vrep["бэп"].length ? repT(
    ["Договор", "Месяц", { t: "Сумма оклада и 122, ₽" }, { t: "Сумма ставок" },
     { t: "Средняя на ставку, ₽" }, { t: "БЭП, ₽" }, { t: "Запас, ₽" }, { t: "Отклонение от БЭП, %" }],
    vrep["бэп"].map(function (b, i) {
      return {reviewKey:"bep:"+i,cells:[b["договор"], MON[b["месяц"] - 1], nc(rm(b["сумма"])), nc(rf(b["ставок"])),
              nc(rm(b["средняя"])), nc(rm(b["БЭП"])), nc(rm(b["запас"])), dev(b["отклонение"])]};
    }), null, { fix: 1, tall: true, merge: [0] }) : '<div class="none">ГОЗ-договоров в плане нет</div>') + "</div>";

  var p4Bad = vrep["п4"].filter(function (b) { return b["отклонение"] > 0.0005; }).length;
  h += secH(9, "П4 при надбавке за интенсивность", vrep["п4"].length
            ? (p4Bad ? chip(p4Bad + " выше предела", "er") : chip("в пределах", "ok")) : "") + (vrep["п4"].length ? repT(
    ["Табельный", "ФИО", "Месяц", { t: "Оклад, ₽" }, { t: "122, ₽" }, { t: "124, ₽" }, { t: "Итого по П4, ₽" },
     { t: "Суммарная ставка" }, { t: "Предел П4 на ставку, ₽" }, { t: "Запас, ₽" }, { t: "Отклонение от предела, %" }],
    vrep["п4"].map(function (b, i) {
      return {reviewKey:"p4:"+i,cells:[b["табельный"], b["фио"], MON[b["месяц"] - 1], nc(rm(b["оклад"])), nc(rm(b["122"])),
              nc(rm(b["124"])), nc(rm(b["итого"])), nc(rf(b["ставка"])), nc(rm(b["предел"])),
              nc(rm(b["запас"])), dev(b["отклонение"])]};
    }), null, { fix: 2, tall: true, merge: [0, 1] }) : '<div class="none">надбавка 124 в этом плане не назначалась</div>') + "</div>";

  var lbBad = vrep["трудоемкость"].filter(function (l) { return l["статус"] !== "сходится"; }).length;
  var lbTol = vrep["трудоемкость"].length ? vrep["трудоемкость"][0]["допуск"] : null;
  h += secH(10, "Трудоёмкость договоров", vrep["трудоемкость"].length
            ? (lbBad ? chip(lbBad + " не сходится", "wr") : chip("сходится", "ok")) +
              (lbTol != null ? chip("допуск " + rp(lbTol)) : "") : "") + (vrep["трудоемкость"].length ? repT(
    ["Договор", "Строка РКМ", { t: "План, чел.-мес." }, { t: "Факт, чел.-мес." }, { t: "Отклонение, чел.-мес." },
     { t: "Отклонение, %" },
     { t: "План, ₽" }, { t: "Факт, ₽" }, { t: "Отклонение, ₽" }, { t: "Отклонение, %" },
     { t: "Средняя план, ₽" }, { t: "Средняя факт, ₽" },
     { t: "Отклонение средней, ₽" }, { t: "Отклонение средней, %" },
     { t: "Предел людей" }, { t: "Людей в месяц, макс" }, "Статус"],
    vrep["трудоемкость"].map(function (l, i) {
      var bad = l["статус"] !== "сходится", out = l["вне допуска"] || {};
      // Отклонение внутри допуска — обычное число; вне допуска — красным.
      var dv = function (txt, over) { return { v: txt, cls: "n" + (over ? " er" : "") }; };
      // Доля рядом с рублями: допуск задан в процентах, и по рублям не видно,
      // близко ли расхождение к нему. 0,8 чел.-мес. — это 5 % от 16 и 10 % от 8.
      var pc = function (d, plan, over) {
        return { v: (d == null || !plan) ? "—" : rs(d / plan * 100, true),
                 cls: "n" + (over ? " er" : "") };
      };
      return { reviewKey: "labor:"+i, cells: [l["договор"], l["строка"], nc(rf(l["план чел-мес"])), nc(rf(l["факт чел-мес"])),
              dv(rs(l["д чел-мес"], true), out["чел-мес"]),
              pc(l["д чел-мес"], l["план чел-мес"], out["чел-мес"]),
              nc(rm(l["план сумма"])),
              nc(rm(l["факт сумма"])), dv(rs(l["д сумма"]), out["сумма"]),
              pc(l["д сумма"], l["план сумма"], out["сумма"]),
              nc(rm(l["средняя план"])),
              nc(rm(l["средняя факт"])), dv(rs(l["д средней"]), out["средняя"]),
              pc(l["д средней"], l["средняя план"], out["средняя"]),
              nc(l["людей предел"] == null ? "—" : l["людей предел"]),
              nc(l["людей макс"], l["людей предел"] != null && l["людей макс"] > l["людей предел"] ? "err" : ""),
              { v: esc(l["статус"]), cls: bad ? "wr" : "ok" }] };
    }), null, { fix: 2, merge: [0] }) : '<div class="none">трудоёмкость в РКМ не задана</div>') + "</div>";

  // Вопрос этой таблицы — кто именно и какой своей должностью закрывает
  // строку РКМ договора. Поэтому уровни: договор → строка РКМ → люди, а в
  // клетках только чел.-мес.; деньги по строке — в разделе 10, по людям — в
  // регистре. Шапка группы делится на закреплённую часть и остаток: цельная
  // ячейка на всю ширину уезжала при прокрутке вбок вместе с подписью.
  var wrows = [], wtail = repMon.length + 1, wneed = {};
  vrep["трудоемкость"].forEach(function (l) { wneed[l["договор"] + "\u0001" + l["строка"]] = l; });
  var prev = null;
  vrep["кто"].forEach(function (w) {
    if (w["договор"] !== prev) {
      prev = w["договор"];
      wrows.push({ grp: true, cls: "lvl1", k: prev, cells: [
        { v: ctrHead(w["договор"]), cs: 2 },
        { skip: true }, { v: "", cs: wtail }] });
    }
    var need = wneed[w["договор"] + "\u0001" + w["строка"]] || {};
    var bad = need["статус"] && need["статус"] !== "сходится";
    var wkey = w["договор"] + "\u0001" + w["строка"];
    // Якорь строки РКМ: по нему замечание «работу закрывает подходящая
    // должность» открывается на нужной строке, а не на разделе целиком.
    var wnum = vrep["трудоемкость"].findIndex(function (x) {
      return x["договор"] === w["договор"] && x["строка"] === w["строка"]; });
    wrows.push({ grp: true, g: w["договор"], k: wkey,
      reviewKey: wnum >= 0 ? "who:" + wnum : undefined, cells: [
      { v: esc(w["строка"]), cs: 2 }, { skip: true },
      { v: "", cs: repMon.length },
      need["план чел-мес"] != null ? nc(rf(need["план чел-мес"])) : { v: "" }] });
    // Строка с планом по месяцам: план — первой строкой группы, чтобы
    // закрытие по месяцам сравнивалось с ним, а не только с годом.
    if (w["план по месяцам"]) {
      wrows.push({ g: wkey, cells: [{ v: "План", cs: 2 }, { skip: true }].concat(
        repMon.map(function (mn) { return mc(w["план по месяцам"][mn - 1], w["договор"], mn, rf); }),
        [nc(rf(need["план чел-мес"]))]) });
    }
    w["люди"].forEach(function (p) {
      wrows.push({ g: wkey, cells: [p["фио"], p["должность"]].concat(
        repMon.map(function (mn) { return mc(p["ставка"][mn - 1], w["договор"], mn, rf); }),
        [nc(rf(p["ставка год"]))]) });
    });
    wrows.push({ cls: "sum", g: wkey, cells: [{ v: "Закрыто по строке", cs: 2 }, { skip: true }].concat(
      repMon.map(function (mn) { return mc(w["итого ставка"][mn - 1], w["договор"], mn, rf); }),
      [nc(rf(w["итого ставка год"]), bad ? "er" : "")]) });
  });
  h += secH(11, "Распределение трудоёмкости по исполнителям") + (wrows.length ? repT(
    ["ФИО", "Должность"].concat(monHead(function (m) { return m; }), [{ t: "За год" }]),
    wrows, null,
    { fix: 2, tall: true, groups: monGroupRow(2, 1, 1, "quarter") }) : '<div class="none">трудоёмкость в РКМ не задана</div>') + "</div>";

  if (vrep["незакрыто"].length) {
    var g = vrep["незакрыто"], gs = 0;
    g.forEach(function (x) { gs += x["не закрыто"] || 0; });
    var grows = g.map(function (x) {
      return [x["договор"], x["должность"], nc(rf(x["нужно"])), nc(rf(x["закрыто"])),
              nc(rf(x["не закрыто"]), "err"), nc(x["месяцев"]), nc(rf(x["ставок в месяц"]))];
    });
    grows.push({ cls: "sum all", cells: [{ v: "Итого не закрыто", cs: 4 }, { skip: true }, { skip: true }, { skip: true }, nc(rf(gs), "err"), "", ""] });
    h += secH(12, "Нехватка людей по строкам РКМ", chip(rf(gs) + " чел.-мес.", "er")) + repT(
      ["Договор", "Должность", { t: "Нужно, чел.-мес." }, { t: "Закрыли люди" }, { t: "Не закрыто" },
       { t: "Месяцев" }, { t: "Ставок в месяц" }], grows) + "</div>";
  } else {
    var hasLabor = (vrep["трудоемкость"] || []).length > 0;
    h += secH(12, "Нехватка людей по строкам РКМ", hasLabor ? chip("не выявлена", "ok") : "") +
      '<div class="none">' + (hasLabor
        ? 'В выбранной версии расчёта незакрытых человеко-месяцев по строкам РКМ нет.'
        : 'Трудоёмкость в РКМ не задана. Оценить нехватку людей по этим данным нельзя.') + '</div></div>';
  }
  return h + repGoalsHtml();
}

/* Меню автофильтра графы: сортировка, поиск, список значений с флажками.
   Значения — те, что остаются после отбора по другим графам, как в Excel. */
function closeFilterMenu() {
  var m = document.querySelector(".fmenu");
  if (m) m.remove();
}
function openFilterMenu(tid, c, anchor) {
  closeFilterMenu();
  var data = GRID_DATA[tid];
  if (!data) return;
  var colf = repColFilter[tid] || {}, cur = colf[c] || null;
  var seen = {}, vals = [];
  data.rows.forEach(function (r) {
    if (!r.d) return;
    var fake = { cells: r.t.map(function (t) { return t; }) };
    if (!rowPasses(fake, colf, c)) return;
    var key = r.t[c];
    if (seen[key]) return;
    seen[key] = true;
    vals.push({ k: key, s: r.v[c] || "(пусто)" });
  });
  vals.sort(function (a, b) {
    var na = numOf(a.k), nb = numOf(b.k);
    return (na != null && nb != null) ? na - nb : a.k.localeCompare(b.k, "ru");
  });
  var srt = repSort[tid], isNum = vals.length && vals.every(function (v) { return numOf(v.k) != null; });
  var m = document.createElement("div");
  m.className = "fmenu";
  m.dataset.tid = tid; m.dataset.c = c;
  m.innerHTML =
    (data.sortable
      ? '<button type="button" class="fmi' + (srt && srt.c === c && srt.d === "a" ? " on" : "") + '" data-fm="asc">' +
        (isNum ? "Сортировка по возрастанию" : "Сортировка от А до Я") + "</button>" +
        '<button type="button" class="fmi' + (srt && srt.c === c && srt.d === "d" ? " on" : "") + '" data-fm="desc">' +
        (isNum ? "Сортировка по убыванию" : "Сортировка от Я до А") + "</button>"
      : "") +
    '<button type="button" class="fmi" data-fm="clear"' + (cur ? "" : " disabled") + ">Снять отбор с графы</button>" +
    '<button type="button" class="fmi" data-fm="hide">Скрыть графу</button>' +
    (data.hidden ? '<button type="button" class="fmi" data-fm="unhide">Показать скрытые графы (' + data.hidden + ")</button>" : "") +
    '<div class="fsep"></div>' +
    '<input type="search" class="fsearch" placeholder="Поиск" aria-label="Поиск значения">' +
    '<div class="flist"><label><input type="checkbox" data-all' +
    (!cur ? " checked" : "") + '> (Выделить все)</label>' +
    vals.map(function (v) {
      return '<label><input type="checkbox" value="' + esc(v.k) + '"' +
             (!cur || cur[v.k] ? " checked" : "") + "> " + esc(v.s) + "</label>";
    }).join("") + "</div>" +
    '<div class="fbtns"><button type="button" class="pri" data-fm="ok">ОК</button>' +
    '<button type="button" data-fm="cancel">Отмена</button></div>';
  document.body.appendChild(m);
  var r = anchor.getBoundingClientRect(), mw = 250;
  var left = Math.min(r.right - mw, window.innerWidth - mw - 8), top = r.bottom + 4;
  if (left < 8) left = 8;
  m.style.left = left + "px";
  m.style.top = Math.min(top, window.innerHeight - 40) + "px";
  var maxH = window.innerHeight - top - 16;
  m.querySelector(".flist").style.maxHeight = Math.max(120, Math.min(280, maxH - 150)) + "px";
  m.querySelector(".fsearch").focus();
}
document.addEventListener("click", function (e) {
  var m = document.querySelector(".fmenu");
  if (!m) return;
  if (!m.contains(e.target)) { closeFilterMenu(); return; }
  var b = e.target.closest("button[data-fm]");
  if (!b) return;
  var tid = m.dataset.tid, c = +m.dataset.c;
  if (b.dataset.fm === "asc" || b.dataset.fm === "desc") {
    repSort[tid] = { c: c, d: b.dataset.fm === "asc" ? "a" : "d" };
  } else if (b.dataset.fm === "hide") {
    (repHideCol[tid] = repHideCol[tid] || {})[c] = true;
  } else if (b.dataset.fm === "unhide") {
    delete repHideCol[tid];
  } else if (b.dataset.fm === "clear") {
    if (repColFilter[tid]) { delete repColFilter[tid][c]; if (!Object.keys(repColFilter[tid]).length) delete repColFilter[tid]; }
  } else if (b.dataset.fm === "ok") {
    var boxes = Array.prototype.slice.call(m.querySelectorAll('.flist input[type="checkbox"]:not([data-all])'));
    var chosen = boxes.filter(function (x) { return x.checked; });
    if (chosen.length === boxes.length) {
      if (repColFilter[tid]) { delete repColFilter[tid][c]; if (!Object.keys(repColFilter[tid]).length) delete repColFilter[tid]; }
    } else {
      var set = {};
      chosen.forEach(function (x) { set[x.value] = true; });
      (repColFilter[tid] = repColFilter[tid] || {})[c] = set;
    }
  }
  closeFilterMenu();
  if (b.dataset.fm !== "cancel") renderView();
});
document.addEventListener("input", function (e) {
  var m = document.querySelector(".fmenu");
  if (!m || !m.contains(e.target)) return;
  if (e.target.classList.contains("fsearch")) {
    var q = e.target.value.trim().toLowerCase();
    Array.prototype.forEach.call(m.querySelectorAll(".flist label"), function (l) {
      if (l.querySelector("[data-all]")) return;
      l.hidden = q && l.textContent.toLowerCase().indexOf(q) < 0;
    });
  }
});
document.addEventListener("change", function (e) {
  var m = document.querySelector(".fmenu");
  if (!m || !m.contains(e.target)) return;
  var all = m.querySelector("[data-all]");
  var boxes = Array.prototype.slice.call(m.querySelectorAll('.flist input[type="checkbox"]:not([data-all])'));
  if (e.target === all) {
    boxes.forEach(function (x) { if (!x.closest("label").hidden) x.checked = all.checked; });
  } else {
    all.checked = boxes.every(function (x) { return x.checked; });
  }
});
document.addEventListener("keydown", function (e) {
  if (e.key === "Escape") closeFilterMenu();
});

$("view").addEventListener("change", function (e) {
  if (view !== "sum") return;
  if (e.target.matches('select[data-sel="sec"]')) {
    var nextSection=+e.target.value;
    if(issueReturn){issueReturn=null;repSec=nextSection;renderView();}
    showSection(nextSection);return;
  }
  if (e.target.matches('select[data-sel="run"]')) {
    chosenRun = +e.target.value; vrun = null; vrep = null;
    renderRuns();
    setView("sum");
    return;
  }
  var cb = e.target.closest("input[type=checkbox]");
  if (cb) {
    if (cb.hasAttribute("data-ost")) repOst = cb.checked;
    if (cb.hasAttribute("data-even")) repEven = cb.checked;
    renderView();
    return;
  }
  var ps = e.target.closest("select[data-rg],select[data-rp]");
  if (ps) {
    if (ps.hasAttribute("data-rg")) repGroup = ps.value;
    if (ps.hasAttribute("data-rp")) repPeriod = ps.value;
    renderView();
    return;
  }
  var s = e.target.closest("select[data-sel]");
  if (!s) return;
  if (s.dataset.sel === "mset") { repMon = s.value.split(",").map(Number); renderView(); }
});

$("view").addEventListener("click", function (e) {
  if (view !== "sum" || !vrep) return;
  var open=e.target.closest('[data-issue-open]');
  if(open){openPlanIssue(open.dataset.issueOpen);return;}
  if(e.target.closest('[data-issue-back]') && issueReturn){
    var saved=issueReturn;issueReturn=null;repMon=saved.mon;repColFilter=saved.filters;repHideCol=saved.hidden;repSec=1;renderView();showSection(1);
    $("view").scrollTop=saved.scroll;
    var origin=Array.prototype.find.call($("view").querySelectorAll('[data-issue-open]'),function(b){return b.dataset.issueOpen===saved.id;});
    if(origin)origin.focus({preventScroll:true});return;
  }
  var issue = e.target.closest("[data-report-sec]");
  if (issue) {
    var targetKey = issue.dataset.reviewTarget;
    if (targetKey) {
      // Restore hidden columns and filtered rows before locating the source.
      repColFilter = {}; repHideCol = {}; repSec = +issue.dataset.reportSec;
      renderView();
    }
    showSection(+issue.dataset.reportSec);
    if (targetKey) requestAnimationFrame(function () {
      var row = Array.prototype.find.call($("view").querySelectorAll(".rep.active tr[data-review-key]"), function (r) { return r.dataset.reviewKey === targetKey; });
      if (row) {
        $("view").querySelectorAll(".review-target").forEach(function (r) { r.classList.remove("review-target"); });
        row.classList.add("review-target"); row.tabIndex = -1;
        row.scrollIntoView({block:"center",inline:"nearest"}); row.focus({preventScroll:true});
      }
    });
    return;
  }
  var nav = e.target.closest("a[data-sec]");
  if (nav) {
    e.preventDefault();
    showSection(+nav.dataset.sec);
    return;
  }
  var fb = e.target.closest(".grid th .fbtn");
  if (fb) {
    e.stopPropagation();
    openFilterMenu(fb.closest(".grid").dataset.tid, +fb.dataset.fc, fb);
    return;
  }
  var ra = e.target.closest(".rtool button[data-ra]");
  if (ra) {
    if (ra.dataset.ra === "full") {
      fullReport(!document.querySelector(".shell").classList.contains("full"));
    }
    if (ra.dataset.ra === "delrun" && vrun) deleteRun(vrun);
    return;
  }
  var gr = e.target.closest("tr.grp");
  if (gr) {
    var closed = gr.classList.toggle("closed"), key = gr.dataset.k || gr.dataset.g;
    Array.prototype.forEach.call(gr.parentNode.querySelectorAll("tr[data-g]"), function (tr) {
      if (tr === gr) return;
      var g = tr.dataset.g;
      if (g === key || g.indexOf(key + "\u0001") === 0) {
        tr.classList.toggle("hide", closed);
        if (tr.classList.contains("grp")) tr.classList.toggle("closed", closed);
      }
    });
    return;
  }
  var tr = e.target.closest(".grid tbody tr");
  if (tr && !e.target.closest("a,button")) {
    var was = tr.classList.contains("sel");
    Array.prototype.forEach.call(tr.parentNode.querySelectorAll("tr.sel"), function (x) { x.classList.remove("sel"); });
    tr.classList.toggle("sel", !was);
  }

  var b = e.target.closest("button[data-rp],button[data-rg]");
  if (!b) return;
  if (b.dataset.rp) repPeriod = b.dataset.rp;
  if (b.dataset.rg) repGroup = b.dataset.rg;
  renderView();
});

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
    // Весь план ФОТ одной страницей, разделы по порядку — как в согласованном
    // прототипе. Вкладки резали нумерацию и прятали разделы друг от друга.
    if (vrep) {
      REP_SECTIONS = [];
      GRID_SEQ = 0;
      var body = repSummary() + repCash() + repPlan() + repLimits() + reportChecksSection();
      if (!REP_SECTIONS.some(function (x) { return x.n === repSec; })) {
        repSec = REP_SECTIONS.length ? REP_SECTIONS[0].n : 1;
      }
      el.innerHTML = failBanner() + repToolbar() +
        '<div class="repwrap">' + repNav() + '<div class="repbody">' + body + "</div></div>";
      showSection(repSec);
    } else {
      el.innerHTML = failBanner() + summaryView();
    }
    return;
  }

  if (view === "plan") {
    el.innerHTML = vrep ? failBanner() + repPlan()
                        : payrollView() + payrollFull() + '<div id="plandetail"></div>';
    return;
  }

  if (view === "cash" && vrep) {
    el.innerHTML = failBanner() + repCash();
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
            return { v: mo(v), cls: "n" + (v < 0 ? " neg" : "") };
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

  if (view === "lim" && vrep) {
    el.innerHTML = failBanner() + repLimits();
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

/* Результат обработки документа. Строки со ссылкой на документ считаем поштучно;
   у нормативного документа таких строк нет — он сверяет справочник
   должностей, и тогда показываем итог этой проверки. Это шире и точнее, чем
   «внесено в реестр»: проверка может ничего не добавлять в реестр. */
function producedAny(x) {
  var s = produced(x.produced);
  if (s) return s;
  var g = x.gave;
  if (!g) return "";
  var out = [];
  if (g["величин"]) out.push(g["величин"] + " " + px(g["величин"], "величина", "величины", "величин") + " справочника");
  else if (g["сверен"]) out.push("справочник должностей сверен");
  if (g["расхождений"]) out.push(g["расхождений"] + " " + px(g["расхождений"], "расхождение", "расхождения", "расхождений"));
  else if (g["величин"] || g["сверен"]) out.push("расхождений нет");
  return out.join(" · ");
}
function produced(made) {
  var forms = {
    "сотрудников": ["сотрудник", "сотрудника", "сотрудников"],
    "договоров": ["договор", "договора", "договоров"],
    "правил замещения": ["правило замещения", "правила замещения",
                         "правил замещения"],
    "поступлений": ["поступление", "поступления", "поступлений"],
    "строк трудоемкости": ["строка трудоемкости", "строки трудоемкости",
                           "строк трудоемкости"],
    "надбавок 120": ["надбавка 120", "надбавки 120", "надбавок 120"],
  };
  var sourceLabor = (made || {})["исходных строк трудоемкости"] || 0;
  return Object.keys(made || {}).filter(function (k) {
      return made[k] && k !== "исходных строк трудоемкости";
    })
    .map(function (k) {
      if (k === "строк трудоемкости" && sourceLabor > made[k]) {
        return sourceLabor + " " + px(sourceLabor, "строка", "строки", "строк") +
               " источника → " + made[k] + " " +
               px(made[k], "позиция", "позиции", "позиций") + " плана";
      }
      var f = forms[k];
      return made[k] + " " + (f ? px(made[k], f[0], f[1], f[2]) : k);
    }).join(" · ");
}

/* Предпросмотр: книга — таблицей, PDF — как есть, остальное — тем текстом,
   который увидел разборщик. Последнее важнее всего там, где текст оказался
   кашей: причина отказа видна глазами, а не со слов агента. */
/* Масштаб — свой у каждого документа: начинается со 100 %, а документ шире
   рамки вписывается по ширине сам, как «автоматический» масштаб в
   просмотрщиках. Запоминать между документами нельзя: маленький лист при
   запомненных 62 % выглядел крошечным. */
var docZoom = 100;
var DOC_ZOOMS = [30, 40, 50, 60, 70, 80, 90, 100, 125, 150, 200, 300];
/* Шагами, а не списком: масштаб подбирают на глаз — минус, плюс и колесо
   с Ctrl, как в просмотрщике. Число возвращает 100 %. */
function zoomPick() {
  return '<span class="dzoom" title="Ctrl и колесо мыши">' +
    '<button type="button" class="zb" data-zs="-1">−</button>' +
    '<span class="zv">' + docZoom + ' %</span>' +
    '<button type="button" class="zb" data-zs="1">+</button>' +
    '<button type="button" class="zf">по ширине</button></span>';
}
function zoomStep(cur, dir) {
  var i;
  if (dir > 0) {
    for (i = 0; i < DOC_ZOOMS.length; i++) if (DOC_ZOOMS[i] > cur) return DOC_ZOOMS[i];
    return DOC_ZOOMS[DOC_ZOOMS.length - 1];
  }
  for (i = DOC_ZOOMS.length - 1; i >= 0; i--) if (DOC_ZOOMS[i] < cur) return DOC_ZOOMS[i];
  return DOC_ZOOMS[0];
}

/* Маску держим включённой между открытиями: экономист смотрит документы
   подряд и хочет видеть одно и то же. */
var showMarks = true;
try { showMarks = localStorage.getItem("docmarks") !== "0"; } catch (e) {}
/* Вкладки вида, как в FineReader: «Документ» — как он есть (книгу и Word
   для этого печатает сам Excel и Word), «Текст» или «Таблица» — разметка, по
   которой выделяют куски и видна подсветка. Заголовка у раздела нет: он
   повторял бы первую вкладку. */
function viewSeg(has, left, right) {
  if (!has) return "";
  return '<span class="seg pdfseg">' +
    '<button type="button" class="on" data-pdf="doc">' + esc(left || "Документ") + "</button>" +
    '<button type="button" data-pdf="text">' + esc(right || "Текст") + "</button></span>";
}

/* Одна панель над предпросмотром, как у просмотрщика: слева вид, справа
   подсветка и масштаб. Во внешнем ONLYOFFICE наложить нашу маску нельзя:
   вместо неработающего тумблера показываем действие, которое открывает
   собственную таблицу/текст и сразу включает подсветку. */
function markToggle(seg, extra, externalView) {
  return '<div class="pvbar">' + (seg || "") + (extra || "") + '<span class="sp"></span>' +
         (externalView
           ? '<button type="button" class="markjump" title="Открыть таблицу и подсветить данные, извлечённые из файла">' +
             'Показать извлечённые данные</button>'
           : "") +
         '<label class="mtog" title="Подсветить данные, извлечённые из файла">' +
         '<span>Извлечённые данные</span><input type="checkbox" id="markson"' +
         (showMarks ? " checked" : "") + '><span class="sw"></span></label>' +
         zoomPick() + "</div>";
}

/* Просмотрщик ONLYOFFICE: показывает .xlsx и .docx постранично сам, без
   печати через Office. Включается настройкой FOT_ONLYOFFICE_URL на сервере;
   без неё карточка работает по-старому, через напечатанный PDF. */
function docFrame(d, path) {
  // ONLYOFFICE показывает документ сам; без него — напечатанный PDF.
  if (d["onlyoffice"]) {
    return '<div class="asdoc loading"><div class="sload">Открываю документ…</div>' +
           '<div class="ooview" id="oo' + d.id + '"></div></div>';
  }
  return '<div class="asdoc' + (d["печать готова"] ? "" : " loading") +
         '"><div class="sload">Готовлю документ…</div>' +
         '<iframe class="pdf" src="/api/document/' + d.id +
         (path || "/asis") + '#navpanes=0" title="Документ"></iframe></div>';
}

function ooMount(back, docId) {
  var box = back.querySelector(".ooview");
  if (!box || box.dataset.on) return;
  box.dataset.on = "1";
  api("/api/document/" + docId + "/oo").then(function (cfg) {
    var wrap = box.closest(".asdoc");
    var run = function () {
      try {
        new window.DocsAPI.DocEditor(box.id, cfg);
        // Просмотрщик заменяет наш блок своей рамкой: заставку снимаем,
        // когда рамка загрузилась, иначе она висит поверх документа.
        var frame = wrap && wrap.querySelector("iframe");
        if (!frame) return;
        frame.addEventListener("load", function () {
          if (wrap) wrap.classList.remove("loading");
        });
        setTimeout(function () {
          if (wrap) wrap.classList.remove("loading");
        }, 8000);
      } catch (e) {
        box.innerHTML = '<div class="none">ONLYOFFICE не открыл документ</div>';
      }
    };
    if (window.DocsAPI) return run();
    var s = document.createElement("script");
    s.src = cfg.apiUrl;
    s.onload = run;
    s.onerror = function () {
      box.innerHTML = '<div class="none">сервер ONLYOFFICE недоступен</div>';
    };
    document.head.appendChild(s);
  }).catch(function () {
    box.innerHTML = '<div class="none">не удалось получить настройки просмотра</div>';
  });
}

/* Полоса над предпросмотром зависит от того, что показано. Масштаб — только
   у нашего вида: у PDF своя линейка в просмотрщике, две подряд сбивают с
   толку. Маска — только там, где её видно: в просмотрщике PDF разметку не
   покрасить, а лист книги красится и на первой вкладке. */
function showZoomFor(back, asText) {
  var f = back.querySelector(".dzoom");
  if (f) f.hidden = !asText;
  var sp = back.querySelector(".pvbar .spick");
  if (sp) sp.hidden = !asText;
  var tog = back.querySelector(".mtog");
  if (tog && !tog.dataset.empty) {
    // ONLYOFFICE заменяет .ooview своим iframe после запуска, поэтому
    // признаком внешнего просмотра служит стабильная кнопка перехода.
    tog.hidden = !asText && !!back.querySelector(".asdoc .pdf, .markjump");
  }
  var jump = back.querySelector(".markjump");
  if (jump && !jump.dataset.empty) jump.hidden = asText;
}

/* Текст документа абзацами: приказ и положение — проза, её читают, а не
   разбирают по колонкам. Пустые строки схлопываем: в вордовских файлах их
   подряд бывает по нескольку. */
/* Текст как есть: строки подряд, ячейки через табуляцию. Для выделения
   удобнее — видно ровно то, что прочитал разборщик. */
function ptextPlain(p) {
  return "<pre>" + esc(p["текст"]) + "</pre>" +
    (p["обрезано"] ? '<div class="more">показано начало документа</div>' : "");
}

function ptextHtml(p) {
  // Строки с табуляцией — строки таблицы документа: показываем таблицей, а не
  // склеенным абзацем. Идущие подряд собираются в одну.
  var lines = p["текст"].split("\n").map(function (s) { return s.replace(/\s+$/, ""); })
    .filter(function (s) { return s.trim(); });
  var out = [], tbl = [];
  var flush = function () {
    if (!tbl.length) return;
    out.push('<table class="ptab"><tbody>' + tbl.map(function (row) {
      return "<tr>" + row.map(function (c) {
        // Числовая графа не переносится: «125 000» ломалось на «125» и «000».
        var num = /^[\d\s .,%-]+$/.test(c) && /\d/.test(c);
        return '<td class="' + (num ? "n" : "") + '">' + esc(c) + "</td>";
      }).join("") + "</tr>";
    }).join("") + "</tbody></table>");
    tbl = [];
  };
  lines.forEach(function (s) {
    if (s.indexOf("\t") >= 0) {
      tbl.push(s.split("\t").map(function (c) { return c.trim(); }));
    } else {
      flush();
      out.push("<p>" + esc(s.trim()) + "</p>");
    }
  });
  flush();
  return out.join("") +
    (p["обрезано"] ? '<div class="more">показано начало документа</div>' : "");
}

function preview(d) {
  var p = d["предпросмотр"] || {};
  var head = '<div class="grp docview">';

  if (p["вид"] === "документ") {
    var withText = !!p["текст"];
    return head + markToggle(viewSeg(withText), "", !!(d["onlyoffice"] && withText)) + docFrame(d, "/file") +
      (withText
        ? '<div class="astext" hidden>' +
          '<div class="ptext" data-doc="' + d.id + '">' + ptextHtml(p) + "</div></div>"
        : "") + "</div>";
  }
  if (p["вид"] === "книга") {
    // Переключатель маски: показать, что из листа ушло в расчёт.
    // Лист книги — тот же документ, и выделять в нём надо так же, как в
    // тексте: рамка своего происхождения, скрипт до неё дотягивается.
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
    // «Документ» — книга страницами, как её печатает Excel; «текстом» —
    // лист разметкой: по нему работают маска и выделение по ячейкам.
    var asis = !!p["как есть"];
    return head + markToggle(viewSeg(asis, "Документ", "Таблица"), pick,
                             !!(d["onlyoffice"] && asis)) +
           (asis
             ? docFrame(d)
             : "") +
           '<div class="astext"' + (asis ? " hidden" : "") + ">" +
           '<div class="sheetbox loading" data-doc="' + d.id + '">' +
           '<div class="sload">Готовлю лист…</div>' +
           '<iframe class="sheetview" src="/api/document/' + d.id +
           "/preview?sheet=" + encodeURIComponent(names[0] || "") +
           '" title="Предпросмотр листа"></iframe></div></div></div>';
  }
  if (p["вид"] === "текст") {
    // Word печатается в PDF — это и есть «как есть»; «текстом» — разметка
    // документа: заголовки, выравнивание, полужирный, таблицы. По ней
    // выделяют куски и видят маску. Разметки нет — показываем свой разбор.
    var wasis = !!p["как есть"];
    return head + markToggle(viewSeg(wasis), "", !!(d["onlyoffice"] && wasis)) +
      (wasis
        ? docFrame(d)
        : "") +
      '<div class="astext"' + (wasis ? " hidden" : "") + ">" +
      (p["разметка"]
        ? '<div class="richbox"><iframe class="richview" src="/api/document/' + d.id +
          '/rich" title="Документ разметкой"></iframe></div>'
        : '<div class="ptext" data-doc="' + d.id + '">' + ptextHtml(p) + "</div>") +
      "</div></div>";
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

/* Выделение куска документа → замечание агенту.

   Экономист показывает пальцем в сам документ и говорит словами, что не так;
   реплика уходит в разговор о документе вместе с местом — лист, строка,
   графа, текст ячейки, — поэтому агент правит именно ту строку, а не ищет её
   по описанию. Работает и в тексте, и в листе книги: лист приходит с того же
   адреса, до его разметки можно дотянуться. */
function noteBar(root, docId, place, getSel, onSaved, onSending) {
  var bar = null;
  // Окно замечания закрывается тремя обычными способами: крестиком, Esc и
  // щелчком мимо. Новое выделение просто заменяет старое — перевыделять
  // можно сколько угодно, ничего предварительно не закрывая.
  function hide() {
    if (!bar) return;
    bar.remove();
    bar = null;
    document.removeEventListener("keydown", onEsc, true);
  }
  function onEsc(e) { if (e.key === "Escape") { hide(); e.stopPropagation(); } }
  function show(sel, rect, frag) {
    hide();
    bar = document.createElement("div");
    bar.className = "nbar say";
    bar.innerHTML = '<div class="nq">«' + esc(sel.slice(0, 90)) +
      (sel.length > 90 ? "…" : "") + "»" +
      '<button type="button" class="nclose" title="Закрыть (Esc)" ' +
      'aria-label="Закрыть">×</button></div>' +
      '<div class="nr"><input type="text" aria-label="Замечание к фрагменту" ' +
      'autocomplete="off"><button type="button" class="ok">Отправить</button></div>';
    document.body.appendChild(bar);
    var w = bar.offsetWidth || 420;
    bar.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - w - 8)) + "px";
    bar.style.top = Math.max(8, rect.top - bar.offsetHeight - 8) + "px";
    var inp = bar.querySelector("input");
    inp.focus();
    var send = function () {
      var text = inp.value.trim();
      if (!text) { inp.focus(); return; }
      var sendButton = bar.querySelector(".ok");
      sendButton.disabled = true;
      if (onSending) onSending(text, frag);
      api("/api/document/" + docId + "/message", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text, fragment: frag }),
      }).then(function () { hide(); onSaved(); })
        .catch(function () { if (sendButton) sendButton.disabled = false; onSaved(); });
    };
    bar.querySelector(".ok").onclick = send;
    bar.querySelector(".nclose").onclick = hide;
    inp.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") send();
      if (ev.key === "Escape") hide();
    });
    document.addEventListener("keydown", onEsc, true);
  }
  root.addEventListener("mouseup", function () {
    setTimeout(function () {
      var s = getSel();
      // Щелчок без выделения — отказ от замечания: окно уходит.
      if (!s || !s.text || s.text.length < 2) { hide(); return; }
      var frag = place(s) || {};
      frag["цитата"] = s.text.slice(0, 300);
      show(s.text, s.rect, frag);
    }, 10);
  });
  // Щелчок мимо — и в самой странице, и внутри рамки листа.
  var away = function (e) {
    if (bar && !bar.contains(e.target)) hide();
  };
  document.addEventListener("mousedown", away);
  if (root !== document) {
    (root.ownerDocument || root).addEventListener("mousedown", away);
  }
}

/* Значение ячейки к тому же виду, что и на сервере: числа без разделителей,
   текст в нижнем регистре и без двойных пробелов. Иначе «1 740 000» из
   документа и 1740000 из реестра не совпадут. */
/* Отметить внутри строки те числа, которые ушли в расчёт: величина стоит в
   строке таблицы («| Научные работники | 280 023,62 |»), и целиком строка ни
   с чем не совпадает. Правим текстовые узлы, разметку не трогаем. */
function markInside(el, marks) {
  if (!el) return 0;
  var doc = el.ownerDocument || document;
  var nodes = [], walk = doc.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
  var t;
  while ((t = walk.nextNode())) if (t.nodeValue && /\d/.test(t.nodeValue)) nodes.push(t);
  var n = 0;
  nodes.forEach(function (node) {
    var text = node.nodeValue;
    if (text.length > 400) return;
    var re = /\d[\d\s ]*(?:[.,]\d+)?/g, hits = [], m;
    while ((m = re.exec(text))) {
      var k = markKey(m[0]);
      if (k && marks[k] && String(k).length >= 4) hits.push([m.index, m[0], marks[k]]);
    }
    if (!hits.length) return;
    var frag = doc.createDocumentFragment(), at = 0;
    hits.forEach(function (h) {
      if (h[0] > at) frag.appendChild(doc.createTextNode(text.slice(at, h[0])));
      var span = doc.createElement("span");
      span.className = "used";
      span.setAttribute("data-mk", "1");
      span.setAttribute("title", "в расчёт: " + h[2]);
      span.textContent = h[1];
      frag.appendChild(span);
      at = h[0] + h[1].length;
      n++;
    });
    if (at < text.length) frag.appendChild(doc.createTextNode(text.slice(at)));
    node.parentNode.replaceChild(frag, node);
  });
  return n;
}

function markKey(v) {
  var s = String(v == null ? "" : v).replace(/\u00a0/g, " ").trim();
  if (!s) return "";
  var num = s.replace(/[\s]/g, "").replace(",", ".");
  if (num && !isNaN(Number(num))) {
    var f = Number(num);
    return f === Math.round(f) ? String(Math.round(f)) : String(Math.round(f * 10000) / 10000);
  }
  return s.replace(/\s+/g, " ").toLowerCase();
}

/* Наложить маску на разметку листа или текста: ячейки, чьи значения ушли в
   расчёт, подсвечиваются и подписываются графой входного файла. */
/* Величина без строки подсветила бы любую графу с тем же числом: в приказе
   № 2556 «110 000» стоит и у советника, которого в справочнике нет. Поэтому
   для нормативных документов приходит ещё и список строк, которые сошлись со
   справочником, — красим только их. */
function rowText(tr) {
  var c = (tr.cells || [])[0];
  if (!c) return "";
  return String(c.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
}
function lineHit(keys, tr) {
  if (!keys || !keys.length) return true;   // книга реестра: строк не присылают
  var t = rowText(tr);
  if (!t) return null;
  for (var i = 0; i < keys.length; i++) {
    var k = keys[i].key, m = Math.min(24, Math.min(k.length, t.length));
    if (m >= 8 && k.slice(0, m) === t.slice(0, m)) return keys[i];
  }
  return null;
}
function applyMarks(doc, marks, lines) {
  if (!doc || !marks) return 0;
  var n = 0;
  var keys = (lines || []).map(function (l) {
    return {key: String(l["строка"] || "").replace(/\s+/g, " ").trim().toLowerCase(),
            vals: l["значения"] || []};
  });
  // Совпадение одной короткой величины ничего не значит: «1» и «2» стоят в
  // любой форме. Строку считаем той самой, если в ней сошлись хотя бы две
  // разные графы; одиночное совпадение принимаем только у длинных значений.
  var rows = doc.querySelectorAll("tr");
  if (rows.length) {
    Array.prototype.forEach.call(rows, function (tr, ri) {
      // Шапка листа — не данные. Слово в ней может совпасть со значением из
      // другой графы: «Приоритет» — это и название графы, и вид договора, и
      // подсвечивалась вся графа, будто её прочитали как величину.
      if (ri === 0 && rows.length > 1) return;
      var line = lineHit(keys, tr);
      if (!line) return;
      // У строки приказа своя графа: предельный уровень по должности стоит в
      // последней колонке, а соседние — уровни для других категорий.
      var only = line === true ? null : line.vals;
      var hits = [];
      Array.prototype.forEach.call(tr.cells || [], function (c) {
        if (c.querySelector("td,th")) return;
        var k = markKey(c.textContent);
        if (only && only.indexOf(k) < 0) return;
        if (k && marks[k]) hits.push([c, marks[k], k]);
      });
      // Опорное совпадение — то, которое само по себе что-то значит: имя,
      // шифр, крупное число. Ставка «1» или «2 человека» совпадает где
      // угодно, поэтому подсвечиваем такие только рядом с опорным.
      var strong = hits.some(function (h) {
        var k = h[2];
        return isNaN(Number(k)) ? k.length >= 4 : k.length >= 4;
      });
      if (!strong) return;
      hits.forEach(function (h) {
        h[0].classList.add("used");
        h[0].setAttribute("title", "в расчёт: " + h[1]);
        n++;
      });
    });
  }
  // Дальше — строки вне таблиц. У документа, прочитанного моделью, таблица
  // может остаться строкой текста («| Научные работники | 280 023,62 |»), и
  // разбор по строкам её не видит.
  Array.prototype.forEach.call(doc.querySelectorAll("p,li,td,th"), function (c) {
    if (c.classList.contains("used")) return;
    if (rows.length && c.closest && c.closest("tr")) return;
    var k = markKey(c.textContent);
    if (k && marks[k] && k.length >= 6) {
      c.classList.add("used");
      c.setAttribute("title", "в расчёт: " + marks[k]);
      n++;
      return;
    }
    // Строка целиком не совпала: у документа, прочитанного моделью, величина
    // стоит внутри строки таблицы («| Научные работники | 280 023,62 |»).
    // Отмечаем само число, а не всю строку.
    n += markInside(c, marks);
  });
  return n;
}

/* Формы со стабильными адресами ячеек красим по координатам. Так значение
   «1 человек» не подсвечивает номер первой строки, а даты и обе графы
   трудоёмкости отмечаются именно там, откуда их прочитал разборщик. */
function applyCellMarks(root, cells) {
  if (!root || !cells || !cells.length) return 0;
  var doc = root.ownerDocument || root, n = 0;
  cells.forEach(function (mark) {
    var cell = doc.getElementById(mark.id);
    if (!cell) return;
    cell.classList.add("used");
    cell.setAttribute("title", "извлечено: " + mark.title);
    n++;
  });
  return n;
}

/* Нормативный документ: что он дал справочнику должностей. Расхождения —
   таблицей с кнопкой записи прямо здесь, а не карточкой где-то в чате. */
function refBlock(g) {
  var ch = g["расхождения"] || [];
  var head = '<div class="grp refgrp"><h4>Справочник должностей</h4>';
  var basis = [g["основание"] ? esc(g["основание"]) : "",
               g["действует с"] ? "с " + esc(g["действует с"]) : ""]
    .filter(Boolean).join(" · ");
  var facts = '<div class="reffacts">' +
    (g["величин"] || 0) + " " + px(g["величин"] || 0, "величина", "величины", "величин") +
    (basis ? '<span class="dot">·</span>' + basis : "") + "</div>";
  var miss = (g["вне справочника"] || []).map(function (m) {
    return typeof m === "string" ? m : m["строка"];
  });
  var missHtml = miss.length
    ? '<div class="refmiss">вне справочника: ' + esc(miss.map(function (s) {
        return s.length > 60 ? s.slice(0, 57) + "…" : s;
      }).join("; ")) + "</div>"
    : "";
  if (!ch.length) {
    // Документ разобран до того, как сервис начал хранить сами расхождения:
    // счётчик есть, строк нет. Обещать «расхождений нет» в этом случае нельзя.
    var n = g["расхождений"] || 0;
    return head + facts + '<div class="none">' +
           (n ? n + " " + px(n, "расхождение", "расхождения", "расхождений") +
                " — перечитайте документ, чтобы увидеть строки"
              : "расхождений со справочником нет") + "</div>" +
           missHtml + "</div>";
  }
  return head + facts +
    '<table class="reftab"><thead><tr><th>Должность</th><th>Графа</th>' +
    '<th class="n">Сейчас</th><th class="n">По документу</th></tr></thead><tbody>' +
    ch.map(function (c) {
      return '<tr data-pos="' + esc(c.pos) + '" data-field="' + esc(c.field) +
             '" data-new="' + c.new + '"><td>' + esc(c.pos) + "</td><td>" + esc(c.field) +
             '</td><td class="n">' + (c.old == null ? "—" : mo(c.old)) +
             '</td><td class="n up">' + mo(c.new) + "</td></tr>";
    }).join("") + "</tbody></table>" +
    '<div class="refacts"><button type="button" class="refapply">Записать в справочник</button></div>' +
    missHtml + "</div>";
}

/* Замечания по фрагментам: что процитировано и в какой реплике. Цитату
   сервис дописывает к словам экономиста строкой «▸ «…»», из неё же метим
   ячейки листа. */
function talkQuotes(talk) {
  var out = [];
  (talk || []).forEach(function (m, i) {
    if (m["кто"] !== "экономист") return;
    var q = /\u25b8 «([^»]+)»/.exec(String(m["текст"] || ""));
    if (!q) return;
    out.push({ i: i, quote: q[1], text: String(m["текст"]).split("\n")[0] });
  });
  return out;
}

/* Одна реплика разговора. */
function dmsg(m, i) {
  return '<div class="dm ' + (m["кто"] === "экономист" ? "me" : "ag") + '"' +
         (i == null ? "" : ' data-msg="' + i + '"') + ">" +
         '<div class="dmt">' + esc(m["текст"]) + "</div>" +
         '<div class="dmw">' + esc(m["когда"] || "") + "</div></div>";
}

function openDocument(id) {
  // Две карточки одна поверх другой ни к чему: щелчок по второму имени
  // заменяет первую.
  var open = document.querySelector(".drawer");
  if (open) open.remove();

  var back = document.createElement("div");
  back.className = "drawer";
  back.innerHTML = '<section class="panel" role="dialog" aria-modal="true">' +
                   '<div class="none">Загружаю…</div></section>';
  document.body.appendChild(back);

  function close() {
    document.removeEventListener("keydown", onKey);
    back.remove();
  }
  function onKey(e) { if (e.key === "Escape") close(); }
  document.addEventListener("keydown", onKey);

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
    var got = made.length
      ? '<div class="grp"><h4>Внесено в реестр</h4><div class="made">' +
        made.map(function (m) {
          return '<button type="button" class="mtag" data-rtab2="' + m[4] + '">' +
                 m[0] + " " + px(m[0], m[1], m[2], m[3]) + "</button>";
        }).join("") + "</div></div>"
      : (d.gave && "величин" in d.gave
          ? refBlock(d.gave)
          : '<div class="grp"><h4>Внесено в реестр</h4><div class="none">' +
            esc(d.summary || "строк реестра нет") + "</div></div>");
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
    // Разговор о документе. «Тут ошибка» говорят там, где ошибку видят, —
    // в карточке, а не в ленте плана. Правка уходит в реестр, в память агента
    // и в стенд.
    var talk = d["переписка"] || [];
    var talkHtml = '<div class="dtalk">' + (talk.length ? talk.map(function (m, i) { return dmsg(m, i); }).join("")
        : '<div class="none">Скажите, что разобрано неверно — агент ' +
          "поправит и запомнит.</div>") + "</div>" +
      '<form class="dask"><input type="text" placeholder="Например: у Петрова оклад ' +
      '90 000, а не 60 000" autocomplete="off">' +
      '<button type="submit">Отправить</button></form>';

    // Учётные сведения — одной строкой в шапке: их пять, и все короткие.
    var facts = [d.kind, docStatus(d.state).text,
                 d["версия"] > 1
                   ? "версия " + d["версия"] +
                     (d["заменяет"] ? ", прежняя от " + d["заменяет"] : "")
                   : null,
                 d["формат"], bytes(d.size), d.uploaded]
      .filter(Boolean).map(esc).join('<span class="dot">·</span>');

    back.querySelector(".panel").innerHTML =
      '<header class="dhead"><div class="dttl"><h3>' + esc(d.name) + "</h3>" +
        '<div class="dfacts">' + facts + "</div></div>" +
        // Действия — значками с подсказкой, как в реестре. Книгу и Word
        // браузер не показывает, только сохраняет: значок «скачать»
        // сохраняет файл под именем документа.
        (d.exists
          ? '<a class="iconbtn dfile" href="/api/document/' + d.id + '/file" download="' +
            esc(d.name) + '" title="Скачать" aria-label="Скачать">' + svg16(ICON_DOWN) + "</a>"
          : '<span class="none">Файла нет на диске</span>') +
        '<button type="button" class="iconbtn ddel" title="Удалить документ" ' +
        'aria-label="Удалить документ">' + svg16(ICON_TRASH) + "</button>" +
        '<button type="button" class="iconbtn x" title="Закрыть" ' +
        'aria-label="Закрыть">' + svg16(ICON_CLOSE) + "</button></header>" +
      '<div class="dbody dcols">' +
        '<section class="dmain">' + preview(d) + "</section>" +
        '<aside class="dside">' +
          (prop ? '<div class="dside-top">' + prop + "</div>" : "") +
          '<div class="dside-reg">' + got + "</div>" +
          '<div class="dside-talk"><h4>Разговор о документе</h4>' + talkHtml + "</div>" +
        "</aside>" +
      "</div>";

    back.querySelector(".x").onclick = close;

    // Разговор дорисовываем на месте: перерисовка всей карточки уводила экран
    // и перезагружала просмотрщик. Последняя реплика — в поле зрения.
    var talkBox = back.querySelector(".dtalk");
    // Цитаты замечаний: по ним метятся ячейки листа. Обновляются вместе с
    // перепиской — новое замечание помечает ячейку сразу.
    var quotes = talkQuotes(d["переписка"]);
    var scrollTalk = function () {
      if (talkBox) talkBox.scrollTop = talkBox.scrollHeight;
    };
    var refreshTalk = function () {
      return api("/api/document/" + d.id).then(function (fresh) {
        if (talkBox) talkBox.innerHTML = (fresh["переписка"] || [])
          .map(function (m, i) { return dmsg(m, i); }).join("");
        quotes = talkQuotes(fresh["переписка"]);
        markNoted();
        scrollTalk();
        loadRegistry();
      });
    };
    /* Пометить в листе и в тексте ячейки, по которым есть замечания.
       Сравниваем по тому же ключу, что и маска «Извлечённые данные»: в
       документе «1 740 000», в цитате — «1 740 000 ₽» или иной пробел. */
    var markNoted = function () {
      var roots = [];
      var pt = back.querySelector(".ptext");
      if (pt) roots.push(pt);
      ["sheetview", "richview"].forEach(function (cls) {
        var fr = back.querySelector("." + cls);
        try { if (fr && fr.contentDocument) roots.push(fr.contentDocument.body); } catch (e) {}
      });
      var byKey = {};
      quotes.forEach(function (q) {
        var k = markKey(q.quote);
        if (k && !byKey[k]) byKey[k] = q;
      });
      roots.forEach(function (root) {
        Array.prototype.forEach.call(root.querySelectorAll(".noted"), function (c) {
          c.classList.remove("noted");
          c.removeAttribute("data-note");
        });
        if (!quotes.length) return;
        Array.prototype.forEach.call(root.querySelectorAll("td,th,p,li"), function (c) {
          if (c.querySelector("td,th")) return;
          var q = byKey[markKey(c.textContent)];
          if (!q) return;
          c.classList.add("noted");
          c.setAttribute("title", q.text);
          c.setAttribute("data-note", q.i);
        });
      });
    };
    /* Щелчок по помеченной ячейке — к своей реплике в разговоре. */
    var noteJump = function (ev) {
      var cell = ev.target && ev.target.closest && ev.target.closest("[data-note]");
      if (!cell || !talkBox) return;
      var msg = talkBox.querySelector('[data-msg="' + cell.getAttribute("data-note") + '"]');
      if (!msg) return;
      msg.scrollIntoView({ block: "center" });
      msg.classList.add("lit");
      setTimeout(function () { msg.classList.remove("lit"); }, 1200);
    };
    var showPendingTalk = function (text, fragment) {
      if (!talkBox) return;
      var empty = talkBox.querySelector(".none");
      if (empty) empty.remove();
      var shown = text;
      if (fragment && fragment["цитата"]) {
        shown += "\n«" + String(fragment["цитата"]).slice(0, 120) +
          (String(fragment["цитата"]).length > 120 ? "…" : "") + "»";
      }
      talkBox.insertAdjacentHTML("beforeend", dmsg({
        "кто": "экономист", "текст": shown, "когда": "сейчас",
      }) + '<div class="dm ag pending" role="status"><span>Проверяю документ</span><i aria-hidden="true"></i></div>');
      scrollTalk();
    };
    scrollTalk();

    var form = back.querySelector(".dask");
    if (form) form.addEventListener("submit", function (e) {
      e.preventDefault();
      var inp = form.querySelector("input"), text = inp.value.trim();
      if (!text) return;
      var sendButton = form.querySelector("button");
      sendButton.disabled = true;
      inp.value = "";
      // Открытое окно замечания к отправке из общего поля отношения не
      // имеет — закрываем, иначе висит поверх документа.
      var openNote = document.querySelector(".nbar");
      if (openNote) openNote.remove();
      showPendingTalk(text);
      api("/api/document/" + d.id + "/message", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text }),
      }).then(refreshTalk, refreshTalk)
        .then(function () { sendButton.disabled = false; });
    });

    back.addEventListener("click", function (e) {
      var t = e.target.closest("[data-rtab2]");
      if (!t) return;
      close();
      regTab = t.getAttribute("data-rtab2");
      if (!inRegistry) openRegistry(); else renderRegistry();
    });

    // Маска: что из документа ушло в расчёт. Значения приходят одним
    // списком, места ищет сама рамка — разбор идёт по заголовкам, и
    // координат ячеек у него нет.
    var marksData = null, markBox = back.querySelector("#markson");
    var paintMarks = function () {
      var docs = [];
      var pt = back.querySelector(".ptext");
      if (pt) docs.push(pt);
      var fr = back.querySelector(".sheetview");
      try { if (fr && fr.contentDocument) docs.push(fr.contentDocument.body); } catch (e) {}
      var rv = back.querySelector(".richview");
      try { if (rv && rv.contentDocument) docs.push(rv.contentDocument.body); } catch (e) {}
      var total = 0;
      docs.forEach(function (root) {
        if (!root) return;
        // Свои обёртки вокруг чисел снимаем целиком: иначе пустой <span>
        // остаётся в строке и на следующем проходе перекрывает разметку.
        Array.prototype.forEach.call(root.querySelectorAll("span[data-mk]"), function (c) {
          c.parentNode.replaceChild(c.ownerDocument.createTextNode(c.textContent), c);
        });
        Array.prototype.forEach.call(root.querySelectorAll(".used"), function (c) {
          c.classList.remove("used");
          c.removeAttribute("title");
        });
        root.classList.toggle("marks", !!(showMarks && marksData));
        if (showMarks && marksData) {
          total += applyMarks(root, marksData["значения"], marksData["строки"]);
          total += applyCellMarks(root, marksData["ячейки"]);
        }
      });

    };
    // Масштаб: своей разметке — zoom на теле рамки, нашей — на блоке текста.
    var applyDocZoom = function () {
      var z = docZoom / 100;
      Array.prototype.forEach.call(back.querySelectorAll(".ptext"), function (el) {
        el.style.zoom = z;
      });
      ["richview", "sheetview"].forEach(function (cls) {
        var fr = back.querySelector("." + cls);
        try {
          if (fr && fr.contentDocument && fr.contentDocument.body) {
            fr.contentDocument.body.style.zoom = z;
            if (bindWheel) bindWheel(fr.contentDocument.body);
          }
        } catch (e) { /* чужое происхождение */ }
      });
    };
    var setDocZoom = function (v) {
      docZoom = Math.max(DOC_ZOOMS[0], Math.min(DOC_ZOOMS[DOC_ZOOMS.length - 1], v));
      var lbl = back.querySelector(".dzoom .zv");
      if (lbl) lbl.textContent = docZoom + " %";
      applyDocZoom();
    };
    /* «По ширине»: меряем вёрстку при масштабе 1 — иначе замер сам зависел бы
       от текущего масштаба и ширина подбиралась бы с ошибкой. */
    var fitDocZoom = function (cap) {
      var best = 0;
      var take = function (el, avail) {
        if (!el || !avail) return;
        var prev = el.style.zoom;
        el.style.zoom = 1;
        var w = el.scrollWidth;
        el.style.zoom = prev;
        if (w > 0) {
          var z = Math.floor(avail / w * 100);
          if (!best || z < best) best = z;
        }
      };
      Array.prototype.forEach.call(back.querySelectorAll(".ptext"), function (el) {
        if (el.offsetParent) take(el, el.parentElement.clientWidth);
      });
      ["richview", "sheetview"].forEach(function (cls) {
        var fr = back.querySelector("." + cls);
        try {
          if (fr && fr.offsetParent && fr.contentDocument && fr.contentDocument.body) {
            take(fr.contentDocument.body, fr.clientWidth);
          }
        } catch (e) { /* чужое происхождение */ }
      });
      if (best) setDocZoom(cap ? Math.min(best, cap) : best);
    };
    // Автоматический масштаб: пока экономист не трогал масштаб сам, широкий
    // документ вписываем по ширине, узкий оставляем как есть.
    var zoomTouched = false;
    var autoFit = function () { if (!zoomTouched) fitDocZoom(100); };

    var zbox = back.querySelector(".dzoom");
    if (zbox) zbox.addEventListener("click", function (ev) {
      var b = ev.target.closest("button");
      if (!b) return;
      zoomTouched = true;
      if (b.classList.contains("zf")) { fitDocZoom(); return; }
      setDocZoom(zoomStep(docZoom, +b.getAttribute("data-zs")));
    });
    /* Колесо с Ctrl — и над нашим текстом, и внутри рамки документа: иначе
       рамка отдала бы жест браузеру и поехала бы вся страница. */
    var wheelZoom = function (ev) {
      if (!ev.ctrlKey) return;
      ev.preventDefault();
      zoomTouched = true;
      setDocZoom(zoomStep(docZoom, ev.deltaY < 0 ? 1 : -1));
    };
    var bindWheel = function (node) {
      if (!node || node.dataset.zw) return;
      node.dataset.zw = "1";
      node.addEventListener("wheel", wheelZoom, { passive: false });
    };
    Array.prototype.forEach.call(back.querySelectorAll(".astext"), bindWheel);

    setDocZoom(100);
    autoFit();
    showZoomFor(back, !back.querySelector(".asdoc") ||
                      back.querySelector(".asdoc").hidden);
    if (markBox) {
      api("/api/document/" + d.id + "/marks").then(function (m) {
        marksData = m;
        // Показывать нечего — не обещаем: у нормативных документов величины
        // ложатся в справочник должностей, а он к документу не привязан.
        var any = m && m["значения"] && Object.keys(m["значения"]).length;
        var tog = back.querySelector(".mtog");
        var jump = back.querySelector(".markjump");
        if (!any) {
          if (tog) { tog.dataset.empty = "1"; tog.hidden = true; }
          if (jump) { jump.dataset.empty = "1"; jump.hidden = true; }
          return;
        }
        paintMarks();
      }).catch(function () {});
      markBox.addEventListener("change", function () {
        showMarks = markBox.checked;
        try { localStorage.setItem("docmarks", showMarks ? "1" : "0"); } catch (e) {}
        paintMarks();
      });
    }

    // ONLYOFFICE находится на другом origin: его ячейки недоступны для нашей
    // маски. Действие переводит пользователя в доступную разметку и включает
    // подсветку, поэтому результат виден сразу после одного нажатия.
    var markJump = back.querySelector(".markjump");
    if (markJump) markJump.addEventListener("click", function () {
      showMarks = true;
      if (markBox) markBox.checked = true;
      try { localStorage.setItem("docmarks", "1"); } catch (e) {}
      var textButton = back.querySelector('.pdfseg button[data-pdf="text"]');
      if (textButton) textButton.click();
    });

    // Выделение в тексте документа → замечание агенту.
    var ptext = back.querySelector(".ptext");
    if (ptext) {
      markNoted();
      ptext.addEventListener("click", noteJump);
      noteBar(ptext, d.id, function (sel) {
        var p = sel.node && (sel.node.nodeType === 1 ? sel.node : sel.node.parentElement);
        p = p && p.closest("p");
        var all = Array.prototype.slice.call(ptext.querySelectorAll("p"));
        var i2 = p ? all.indexOf(p) : -1;
        return i2 >= 0 ? { "место": "абзац " + (i2 + 1) } : {};
      }, function () {
        var s = window.getSelection();
        if (!s || s.isCollapsed) return null;
        var t = String(s).trim();
        if (!t || !ptext.contains(s.anchorNode)) return null;
        return { text: t, rect: s.getRangeAt(0).getBoundingClientRect(), node: s.anchorNode };
      }, refreshTalk, showPendingTalk);
    }

    if (d["onlyoffice"]) ooMount(back, d.id);

    // Печать книги идёт секунды: пока рамка не загрузилась, в ней видно,
    // что документ готовится.
    var pdfFrame = back.querySelector(".asdoc .pdf");
    if (pdfFrame) pdfFrame.addEventListener("load", function () {
        var box = pdfFrame.closest(".asdoc");
        if (box) box.classList.remove("loading");
      });

    // Разметка документа Word: та же рамка своего происхождения, поэтому
    // выделение для замечаний и маска работают, как в листе книги.
    var rich = back.querySelector(".richview");
    if (rich) {
      rich.addEventListener("load", function () {
        try {
          var rdoc = rich.contentDocument;
          var st = rdoc.createElement("style");
          st.textContent = ".used{background:#FFF3C4;box-shadow:inset 0 0 0 1px #E6A700}" +
            ".noted{border-bottom:2px dotted #1687b8;cursor:pointer}";
          rdoc.head.appendChild(st);
          applyDocZoom();
          autoFit();
          paintMarks();
          markNoted();
          rdoc.addEventListener("click", noteJump);
          noteBar(rdoc, d.id, function (sel) {
            var node = sel.node && (sel.node.nodeType === 1 ? sel.node : sel.node.parentElement);
            var cell = node && node.closest("td,th");
            var out = {};
            if (cell) {
              var tr = cell.parentElement, table = cell.closest("table");
              var rows = table ? Array.prototype.slice.call(table.rows) : [];
              var ri = rows.indexOf(tr);
              if (ri >= 0) out["строка"] = "строка таблицы " + (ri + 1);
              var first = tr.cells[0];
              if (first && first !== cell) {
                var ft = first.textContent.trim();
                if (ft) out["ключ строки"] = ft.slice(0, 60);
              }
            } else if (node) {
              var pel = node.closest("p,h1,h2,h3,h4,li");
              var all = Array.prototype.slice.call(rdoc.querySelectorAll("p,h1,h2,h3,h4,li"));
              var i2 = pel ? all.indexOf(pel) : -1;
              if (i2 >= 0) out["место"] = "абзац " + (i2 + 1);
            }
            return out;
          }, function () {
            var s = rdoc.getSelection();
            if (!s || s.isCollapsed) return null;
            var t = String(s).replace(/[\s\u00a0]+/g, " ").trim();
            if (!t) return null;
            var r = s.getRangeAt(0).getBoundingClientRect();
            var f = rich.getBoundingClientRect();
            return { text: t, node: s.anchorNode,
                     rect: { left: f.left + r.left, top: f.top + r.top } };
          }, refreshTalk, showPendingTalk);
        } catch (e) { /* чужое происхождение — выделение недоступно */ }
      });
    }

    // Лист переключаем перезагрузкой рамки: разметку собирает сервер.
    var frame = back.querySelector(".sheetview");
    if (frame) {
      function setupSheetFrame() {
        var box = frame.closest(".sheetbox");
        if (box) box.classList.remove("loading");
        // Разметка листа приходит с сервера без стилей карточки — свои
        // добавляем в саму рамку.
        try {
          var st = frame.contentDocument.createElement("style");
          st.textContent = ".used{background:#FFF3C4;box-shadow:inset 0 0 0 1px #E6A700}" +
            "body.marks .used{outline:none}" +
            ".noted{border-bottom:2px dotted #1687b8;cursor:pointer}";
          frame.contentDocument.head.appendChild(st);
        } catch (e) {}
        applyDocZoom();
        autoFit();
        paintMarks();
        markNoted();
        try { frame.contentDocument.addEventListener("click", noteJump); } catch (e) {}
        // Лист приходит с того же адреса, поэтому выделение внутри рамки
        // доступно как в своей странице: замечание ставят прямо по ячейкам.
        try {
          var idoc = frame.contentDocument;
          // Кэшированный лист иногда готов до подписки на load. Пометка стоит
          // на самом Document: при смене листа создастся новый объект, а на
          // одном листе обработчик выделения не продублируется.
          if (idoc.__fotNoteBarBound) return;
          idoc.__fotNoteBarBound = true;
          var pickNow = back.querySelector("#sheetpick");
          noteBar(idoc, d.id, function (sel) {
            // Где именно выделено: лист, номер строки, заголовок графы и
            // первая ячейка строки — по ним агент находит строку в файле.
            var name = pickNow ? pickNow.value
                     : decodeURIComponent((frame.src.split("sheet=")[1] || "").split("&")[0]);
            var out = { "место": name ? "лист «" + name + "»" : null };
            var cell = sel.node && (sel.node.nodeType === 1 ? sel.node : sel.node.parentElement);
            cell = cell && cell.closest("td,th");
            if (cell) {
              var tr = cell.parentElement, table = cell.closest("table");
              var rows = table ? Array.prototype.slice.call(table.rows) : [];
              var ri = rows.indexOf(tr), ci = Array.prototype.slice.call(tr.cells).indexOf(cell);
              if (ri >= 0) out["строка"] = "строка " + (ri + 1);
              var head = rows[0] && rows[0].cells[ci];
              if (head && head !== cell) {
                var ht = head.textContent.trim();
                if (ht) out["графа"] = "графа «" + ht + "»";
              }
              var first = tr.cells[0];
              if (first && first !== cell) {
                var ft = first.textContent.trim();
                if (ft) out["ключ строки"] = ft.slice(0, 60);
              }
            }
            return out;
          }, function () {
            var s = idoc.getSelection();
            if (!s || s.isCollapsed) return null;
            var t = String(s).replace(/[\s\u00a0]+/g, " ").trim();
            if (!t) return null;
            var r = s.getRangeAt(0).getBoundingClientRect();
            var f = frame.getBoundingClientRect();
            return { text: t, node: s.anchorNode,
                     rect: { left: f.left + r.left, top: f.top + r.top } };
          }, refreshTalk, showPendingTalk);
        } catch (e) { /* чужое происхождение — выделение недоступно */ }
      }
      frame.addEventListener("load", setupSheetFrame);
      // Не полагаемся только на load: предпросмотр из кэша может успеть
      // загрузиться раньше, чем панель навесит обработчик.
      try {
        if (frame.contentDocument && frame.contentDocument.readyState === "complete") {
          setupSheetFrame();
        }
      } catch (e) {}
    }
    // PDF: тот же файл документом или текстом. Маска и выделение живут в
    // текстовом виде — браузерную отрисовку PDF не разметить.
    var pdfSeg = back.querySelector(".pdfseg");
    if (pdfSeg) pdfSeg.addEventListener("click", function (e) {
      var b = e.target.closest("button[data-pdf]");
      if (!b) return;
      var asText = b.dataset.pdf === "text";
      Array.prototype.forEach.call(pdfSeg.querySelectorAll("button"), function (x) {
        x.classList.toggle("on", x === b);
      });
      var docEl = back.querySelector(".asdoc"), textEl = back.querySelector(".astext");
      if (docEl) docEl.hidden = asText;
      if (textEl) textEl.hidden = !asText;
      showZoomFor(back, asText);
      if (asText) autoFit();
      paintMarks();
    });

    // Выбор листа: рамка перезагружается, разметку собирает сервер.
    var pickEl = back.querySelector("#sheetpick");
    if (pickEl && frame) {
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
    // Запись расхождений в справочник — из карточки документа.
    var bindRef = function () {
      var btn = back.querySelector(".refapply");
      if (!btn) return;
      btn.onclick = function () {
        var edits = Array.prototype.map.call(
          back.querySelectorAll(".reftab tbody tr"), function (tr) {
            return { pos: tr.getAttribute("data-pos"), field: tr.getAttribute("data-field"),
                     value: +tr.getAttribute("data-new") };
          });
        btn.disabled = true;
        btn.textContent = "Записываю…";
        api("/api/document/" + d.id + "/reference", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ edits: edits }),
        }).then(function (r) {
          var g = Object.assign({}, d.gave || {}, { "расхождения": r["осталось"] || [] });
          d.gave = g;
          var box = back.querySelector(".dside-reg");
          if (box) box.innerHTML = refBlock(g);
          bindRef();
          refreshTalk();
          tick();
        }).catch(function () {
          btn.disabled = false;
          btn.textContent = "Записать в справочник";
        });
      };
    };
    bindRef();

    back.querySelector(".ddel").onclick = function () {
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

/* Отчет на весь экран: обе боковые колонки убраны, Esc возвращает. */
function fullReport(on) {
  document.querySelector(".shell").classList.toggle("full", on);
  var b = document.querySelector(".rtool .full");
  if (b) {
    b.classList.toggle("on", on);
    b.title = on ? "Выйти из полного экрана (Esc)" : "Отчет на весь экран";
    b.setAttribute("aria-label", b.title);
  }
}
document.addEventListener("keydown", function (e) {
  if (e.key === "Escape" && document.querySelector(".shell.full")) fullReport(false);
});
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
  if ($("openagents")) $("openagents").classList.add("on");
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
  if ($("openagents")) $("openagents").classList.remove("on");
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
  // Ссылка «весь реестр» нужна из плана; на самом реестре она вела бы в себя.
  // Колонка «документы в этом плане» на реестре тоже ни к чему — и кнопка,
  // которая её возвращает, на реестре скрыта.
  if ($("toreg")) $("toreg").hidden = true;
  $("toggleside").hidden = true;
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
  if ($("toreg")) $("toreg").hidden = false;
  $("toggleside").hidden = false;
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
    // перечня и «результат обработки» — проверяемые счетчики или итог сверки.
    // Вид документа убран:
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
        : [pickBox, "документ", "загружен", "статус", "результат обработки", ""],
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
            { v: producedAny(x) || "—" },
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
                  "подразделение", "счет", "ГОЗ",
                  { t: "фонд, ₽" }, "начало", "окончание", "разрешенные выплаты", "приоритет",
                  "основное", "совмест.", "оклад до", "надбавки до"],
      pgc.rows.map(function (x) {
        return [{ v: esc(x.code), cls: "key" }, x.name, x.number, x.kind, x.department, x.account,
                { v: x.goz ? '<span class="tag goz">' + esc(x.goz) + "</span>" : null },
                { v: x.fund == null ? null : mo(x.fund), cls: "n" },
                x.from, x.to, x.kinds, x.priority, x.allow_main, x.allow_part,
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
                  "начало", "окончание", "разрешены", "запрещены"],
      pge.rows.map(function (x) {
        return [{ v: esc(x.code), cls: "key" }, x.fio, x.position, x.department,
                { v: x.rate == null ? null : String(x.rate).replace(".", ","), cls: "n" },
                x.employment, x.category,
                { v: x.salary == null ? null : mo(x.salary), cls: "n" },
                x.from, x.to, x.allowed, x.forbidden];
      }),
      "", { startNum: pge.from }) + pager("emp", pge);
  } else if (regTab === "labor") {
    // Реестр показывает строки источника, а не только две агрегированные
    // позиции оптимизатора. Так видно этап, вид работ, даты и контрольную
    // сумму; объединённый помесячный план остаётся в разделе 10 результата.
    var sourceRows = [];
    (d.labor || []).forEach(function (x) {
      var details = x.details && x.details.length ? x.details : [null];
      details.forEach(function (one) { sourceRows.push({ plan: x, detail: one }); });
    });
    // После агрегации одинаковые должности лежат рядом, поэтому без сортировки
    // строки одного файла шли 1, 3, 2, 4. Возвращаем порядок исходного листа.
    sourceRows.sort(function (a, b) {
      if (!a.detail || !b.detail || a.plan.source !== b.plan.source) return 0;
      function rowNumber(x) {
        var cells = x.detail.cells || {};
        var id = cells.stage || cells.position || "";
        var match = String(id).match(/![A-Z]+(\d+)$/i);
        return match ? Number(match[1]) : Number.MAX_SAFE_INTEGER;
      }
      return rowNumber(a) - rowNumber(b);
    });
    var pgl = paged("labor", sourceRows);
    body = table(["договор", { t: "год" }, "этап", "вид работ", "должность",
                  { t: "специалистов" }, "единица", { t: "на специалиста" },
                  { t: "всего по источнику" }, { t: "для расчёта, чел.-мес." },
                  { t: "стоимость единицы, ₽" }, { t: "сумма, ₽" },
                  "начало", "окончание", "источник"],
      pgl.rows.map(function (row) {
        var x = row.plan, one = row.detail || {};
        var pm = one.person_months == null ? x.person_months : one.person_months;
        var cost = one.avg_cost == null ? x.avg_cost : one.avg_cost;
        var heads = one.headcount == null ? x.headcount : one.headcount;
        var perPerson = one.labor_per_person;
        var unit = one.labor_unit || "чел.-мес.";
        var sourceTotal = one.source_total_labor == null ? pm : one.source_total_labor;
        var sourceCost = one.source_unit_cost == null ? cost : one.source_unit_cost;
        var total = one.total_cost;
        if (total == null && pm != null && cost != null) total = pm * cost;
        return [x.contract, { v: x.year == null ? null : String(x.year), cls: "n" },
                one.stage || null, one.work_type || null,
                one.position || x.position,
                { v: heads == null ? null : String(heads).replace(".0", ""), cls: "n" },
                unit,
                { v: perPerson == null ? null : String(perPerson).replace(".", ","), cls: "n" },
                { v: sourceTotal == null ? null : String(sourceTotal).replace(".", ","), cls: "n" },
                { v: pm == null ? null : String(pm).replace(".", ","), cls: "n" },
                { v: sourceCost == null ? null : mo(sourceCost), cls: "n" },
                { v: total == null ? null : mo(total), cls: "n" },
                one.from || null, one.to || null, x.source || null];
      }),
      "", { startNum: pgl.from }) + pager("labor", pgl);
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
          return { v: mo(v), cls: "n" };
        });
        return [{ v: esc(years[key][0]), cls: "key" },
                { v: years[key][1] == null ? null : String(years[key][1]), cls: "n" }]
          .concat(cells, [{ v: mo(total), cls: "n tot" }]);
      }),
      "", { startNum: pgi.from }) + pager("inflow", pgi);
  } else if (regTab === "secret") {
    var pgs2 = paged("secret", d.secret || []);
    body = table(["сотрудник", "договор секретности", { t: "ставка 120" }],
      pgs2.rows.map(function (x) {
        return [x.employee, x.contract,
                { v: x.rate == null ? null : String(x.rate).replace(".", ","),
                  cls: "n" }];
      }),
      "", { startNum: pgs2.from }) + pager("secret", pgs2);
  } else if (regTab === "ref") {
    var pgr = paged("ref", d.reference || []);
    // Откуда величины: оклад — из положения, П2556 — из приказа, П4 — из
    // справки, БЭП — из письма. Связь ставит агент при записи величин или
    // экономист вручную; она уходит в основание каждого расчёта.
    var srcs = d.reference_sources || {};
    var srcRows = ["оклад", "П2556", "П4", "БЭП"].map(function (f) {
      var x = srcs[f] || {};
      return [f,
              { v: x["документ"] ? '<span class="dname" data-doc="' + esc(String(x["document_id"] || "")) +
                   '" role="button" tabindex="0">' + esc(x["документ"]) + "</span>" : gc("—") },
              x["загружен"] || null, x["основание"] || null];
    });
    body = table(["величина", "документ-источник", "загружен", "основание"],
                 srcRows, "", { plain: true }) + "<div style=\"height:12px\"></div>";
    // Окладной группы здесь нет: взаимозаменяемость задают правила замещения,
    // а группировка из положения об оплате труда устарела и в решении больше
    // не участвует. БЭП, наоборот, показываем — по нему считается средний
    // предел по ГОЗ-договору, а раньше его было не видно.
    body += table(["должность", "категория",
                  { t: "оклад за 1,0 ставки, ₽" }, { t: "П2556, ₽" },
                  { t: "П4, ₽" }, { t: "БЭП, ₽" }, "примечание"],
      pgr.rows.map(function (x) {
        return [x.pos, x.cat,
                { v: x.sal == null ? null : mo(x.sal), cls: "n" },
                { v: x.p2556 == null ? null : mo(x.p2556), cls: "n" },
                { v: x.p4 == null ? null : mo(x.p4), cls: "n" },
                { v: x.bep == null ? null : mo(x.bep), cls: "n" }, x.note];
      }),
      "", { startNum: pgr.from }) + pager("ref", pgr);
  } else if (regTab === "sub") {
    var pgs = paged("sub", d.substitutions || []);
    body = table(["должность", "может быть замещена"],
      pgs.rows.map(function (x) { return [x.position, x.replaced_by]; }),
      "", { startNum: pgs.from }) + pager("sub", pgs);
  }

  var tabsRow =
    '<div class="reghead"><div class="rtabs">' + REG_TABS.map(function (t) {
      var visibleLaborRows = (d.labor || []).reduce(function (sum, x) {
        return sum + (x.details && x.details.length ? x.details.length : 1);
      }, 0);
      var n = { docs: docs.length, ctr: (d.contracts || []).length,
                emp: (d.employees || []).length, labor: visibleLaborRows,
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
var runWas = {};
function tick() {
  if (!caseId) return Promise.resolve();
  return api("/api/case/" + caseId).then(function (s) {
    state = s;
    // Расчет закончился удачно — открываем отчет сами: экономист ждет
    // результат, а не сообщение о нем.
    var finished = s.runs.some(function (r) {
      return r.status === "OPTIMAL" && runWas[r.id] === "идет";
    });
    runWas = {};
    s.runs.forEach(function (r) { runWas[r.id] = r.status; });
    if (finished && !inRegistry && !inAgents) {
      vrun = null; vrep = null; chosenRun = null;
      setTimeout(function () { setView("sum"); }, 300);
    }
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

if ($("openagents")) $("openagents").addEventListener("click", openAgents);

$("openreg").addEventListener("click", openRegistry);
$("opencurrent").addEventListener("click", openCurrent);

/* Версия расчета: щелчок открывает ее отчет. */
// Версии переключаются полем «Версия» в панели отчёта; карточек в колонке
// справа больше нет.
if ($("toreg")) $("toreg").addEventListener("click", openRegistry);

/* Флажок документа: снят — чат этого плана документ не смотрит. */
$("docs").addEventListener("change", function (e) {
  var cb = e.target.closest("input[data-mute]");
  if (!cb) return;
  api("/api/case/" + caseId + "/doc/" + cb.getAttribute("data-mute") + "/mute", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ muted: !cb.checked })
  }).then(tick);
});

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
  showMenu(b, [
    { label: "Открыть", run: function () { openDocument(+b.getAttribute("data-docmenu")); } },
    { label: "Удалить документ", danger: true, run: function () {
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
  showMenu(b, [
    { label: "Открыть", run: function () { openDocument(+b.getAttribute("data-docmenu")); } },
    { label: "Удалить документ", danger: true, run: function () {
      removeDoc(b.getAttribute("data-docmenu"), name, tick);
    } }]);
});

/* Удалить версию расчёта. Реестр не трогается: прогон — только результат
   счёта, данные организации живут отдельно. */
function deleteRun(id) {
  var saved = (state.runs || []).filter(function (r) { return r.status === "OPTIMAL"; });
  ask("Удалить версию расчёта № " + id + "?",
      saved.length > 1
        ? "Останутся другие версии; данные реестра не изменятся."
        : "Это единственная посчитанная версия. После удаления план вернётся "
          + "к состоянию «готово к расчету», данные реестра не изменятся.",
      "Удалить версию").then(function (yes) {
    if (!yes) return;
    api("/api/case/" + caseId + "/run/" + id, { method: "DELETE" }).then(function () {
      vrun = null; vrep = null; vrules = null; vresult = null; vsum = null; vpay = null;
      tick();
    });
  });
}

function removeDoc(id, name, done) {
  ask("Удалить «" + String(name).trim() + "»?",
      "Вместе с документом удалятся извлеченные из него данные: сотрудники, " +
      "договоры и правила замещения.", "Удалить документ").then(function (yes) {
    if (!yes) return;
    api("/api/document/" + id, { method: "DELETE" }).then(done || tick);
  });
}

if ($("agents")) $("agents").addEventListener("click", function (e) {
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

/* Куда класть файл из чата: в реестр организации (общие данные) или только
 * в этот план (песочница). Вопрос задается до выбора файла. */
var uploadScope = null;
$("attach").addEventListener("click", function () {
  showMenu($("attach"), [
    { label: "В реестр организации", run: function () { uploadScope = "реестр"; $("files").click(); } },
    { label: "Только в этот план", run: function () { uploadScope = "план"; $("files").click(); } },
  ]);
});
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
  // Из реестра — в реестр организации; из чата — как ответил экономист на
  // вопрос кнопки «+» (по умолчанию тоже в реестр).
  fd.append("scope", inRegistry ? "реестр" : (uploadScope || "реестр"));
  uploadScope = null;
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

// Enter отправляет реплику. Неявная отправка формы браузером работает не
// везде (и не во встроенном окне приложения), а экономист набирает текст и
// жмёт Enter — реплика оставалась в поле.
$("text").addEventListener("keydown", function (e) {
  if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
  e.preventDefault();
  $("composer").dispatchEvent(new Event("submit", { cancelable: true }));
});

$("composer").addEventListener("submit", function (e) {
  e.preventDefault();
  var t = $("text").value.trim();
  if (!t || !caseId || sending) return;
  $("text").value = "";
  // Реплика показывается сразу, ответ модели идет секунды: ждать пустого
  // экрана экономист не должен.
  sending = t;
  renderFeed();
  var done = function () { sending = null; return tick(); };
  api("/api/case/" + caseId + "/message", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: t })
  }).then(done, function () { done(); renderFeed(); });
});

$("solve").addEventListener("click", solve);

/* старт: открываем последнее дело или заводим первое */
loadCases().then(function (rows) {
  // Вход в сервис — текущий план сводкой; лента нужна, только когда план
  // еще не посчитан.
  if (rows.some(function (c) { return c.run_id; })) openCurrent();
  else if (rows.length) open(rows[0].id);
  else newCase();
  polling = setInterval(tick, 1500);
});

})();
