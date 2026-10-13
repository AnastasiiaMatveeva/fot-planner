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

/* Ключ операции (RUN-001): оборвавшийся запрос повторяется тем же ключом и
   получает тот же ответ, а не вторую реплику в ленте или второе применение
   строк. Ключ рождается при нажатии, а не при отправке. */
function opId() {
  return (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
    : String(Date.now()) + "-" + Math.random().toString(16).slice(2);
}

/* Ошибка говорит словами сервера («план не найден»), а не кодом: «HTTP 400»
   экономисту ничего не объясняет (UI-016 п. 2). Код остаётся в err.status
   для веток, которым он нужен. */
function api(path, opts) {
  return fetch(path, opts).then(function (r) {
    if (r.ok) return r.json();
    return r.json().catch(function () { return {}; }).then(function (body) {
      var said = body && typeof body.detail === "string" ? body.detail : "";
      var err = new Error(said || "сервер ответил ошибкой " + r.status);
      err.status = r.status;
      throw err;
    });
  }, function () {
    throw new Error("нет связи с сервером");
  });
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
  });
}
var toastTimer = null;
function showToast(message) {
  var toast = document.querySelector(".toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.className = "toast";
    toast.setAttribute("role", "status");
    toast.setAttribute("aria-live", "polite");
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  toast.classList.remove("show");
  requestAnimationFrame(function () { toast.classList.add("show"); });
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { toast.classList.remove("show"); }, 2300);
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
/* Окно держит фокус (UI-008, DEV-UI-MODAL-ENTER-001). Клавиши слушает само
   окно, а не document: Tab и Shift+Tab ходят по его органам по кругу, Esc
   закрывает. Enter не перехватывается — он нажимает ту кнопку, на которой
   стоит фокус; раньше Enter где угодно значил «Удалить». После закрытия фокус
   возвращается туда, откуда окно открыли. */
function holdFocus(wrap, onEsc, from) {
  from = from || document.activeElement;
  wrap.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { e.preventDefault(); onEsc(); return; }
    if (e.key !== "Tab") return;
    var all = Array.prototype.filter.call(
      wrap.querySelectorAll("button, input, select, textarea, a[href], [tabindex]"),
      function (el) { return !el.disabled && el.tabIndex >= 0 && el.getClientRects().length > 0; });
    if (!all.length) return;
    var i = all.indexOf(document.activeElement);
    if (e.shiftKey ? i <= 0 : (i < 0 || i === all.length - 1)) {
      e.preventDefault();
      all[e.shiftKey ? all.length - 1 : 0].focus();
    }
  });
  return function () {
    if (from && from !== document.body && document.contains(from)) from.focus();
  };
}

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

    var release = holdFocus(back, function () { close(false); });
    function close(answer) {
      back.remove();
      release();
      resolve(answer);
    }
    back.querySelector(".no").onclick = function () { close(false); };
    back.querySelector(".yes").onclick = function () { close(true); };
    back.onclick = function (e) { if (e.target === back) close(false); };
    // Фокус на «Отмене»: удаление выбирают Tab, а не случайным Enter.
    back.querySelector(".no").focus();
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
/* Вернуть как было — стрелка против часовой (сброс правки плана). */
var ICON_RESET = '<path d="M1.8 3v3.8h3.8"/><path d="M3.3 10.2a5.6 5.6 0 1 0 1.3-5.9L1.8 6.8"/>';
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

/* Точка, у которой открыть следующее меню: правый щелчок по строке открывает
   её меню там, где стоит курсор, а не в конце строки у кнопки. Ставится
   на один вызов и тут же гасится. */
var menuPoint = null;

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

  var r = menuPoint
    ? { bottom: menuPoint.y - 4, left: menuPoint.x }
    : anchor.getBoundingClientRect();
  menuPoint = null;
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
    // Планы группами по годам, как в макете: год прошлого плана — подписью
    // над своей группой. Год самого свежего плана уже назван подписью
    // «Планы» над списком, второй раз его не пишем.
    var year = rows.length ? rows[0].year : null;
    $("caselist").innerHTML = rows.length
      ? rows.map(function (c) {
          var head = c.year === year ? "" :
            '<div class="ctitle">' + esc(String(c.year || "без года")) + "</div>";
          year = c.year;
          // Строка плана — как в прототипе «ФОТ · прототип интерфейса»
          // (решение пользователя 18.09.2026): только название, действия — в
          // контекстном меню по правому щелчку и клавише меню. Карандаш и
          // корзина в каждой строке превращали навигацию в панель действий.
          // Состояние, которое зовёт в план («требует пересмотра», «нет
          // решения»), — точкой справа со словом для диктора и в подсказке:
          // дописанное к имени, оно красило его целиком и уводило на вторую
          // строку. «Посчитано» — обычное состояние, отметки не требует.
          var stale = c.review === "review_required";
          var bad = c.stage === "нет решения" || c.stage === "не успел";
          var note = stale ? "требует пересмотра" : bad ? c.stage : "";
          // Строка ведёт в план — для диктора это ссылка, открытый план —
          // текущая страница, а не только класс on.
          return head + '<div class="case' + (c.id === caseId ? " on" : "") +
                 '" data-id="' + c.id + '" role="link" tabindex="0" aria-haspopup="menu"' +
                 (c.id === caseId ? ' aria-current="page"' : "") +
                 ' title="' + esc(c.title + (note ? " · " + note : "") + " · " + c.stage) + '">' +
                 '<span class="ttl" data-title="' + esc(c.title) +
                 '" data-full="' + esc(c.title) + '">' + esc(c.title) + "</span>" +
                 (note ? '<i class="cdot ' + (stale ? "ready" : "bad") + '" aria-hidden="true"></i>' +
                         '<span class="review-sr"> — ' + esc(note) + "</span>" : "") +
                 "</div>";
        }).join("")
      : '<div class="empty" style="padding:0 16px">Планов пока нет</div>';
    fitTitles();
    // На реестре открытого плана нет: план, чей чат справа, текущей
    // страницей не назван — как и при входе в реестр (openRegistry).
    if (inRegistry) planMarkOff();
    Array.prototype.forEach.call(document.querySelectorAll(".case"), function (el) {
      el.addEventListener("click", function (e) {
        if (e.target.closest("textarea, input")) return;
        open(+el.getAttribute("data-id"));
      });
      // Правый щелчок — меню плана. Клавиатурный дубль — клавиша меню и
      // Shift+F10: действие, доступное только мышью, недоступно вовсе.
      el.addEventListener("contextmenu", function (e) {
        if (e.target.closest("textarea")) return;
        e.preventDefault();
        openPlanMenu(el, e.clientX, e.clientY);
      });
      el.addEventListener("keydown", function (e) {
        if (e.target !== el) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open(+el.getAttribute("data-id"));
        } else if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) {
          e.preventDefault();
          var r = el.getBoundingClientRect();
          openPlanMenu(el, r.left + 12, r.bottom);
        }
      });
    });
    return rows;
  });
}

/* Меню плана — то же, что в прототипе, но только из того, что умеет сервис:
   переименовать и удалить. «Сравнить» и «Дублировать» прототипа здесь нет —
   у сервиса нет ни сравнения вариантов, ни копирования плана. Удаление
   отделено чертой и по-прежнему спрашивает подтверждение (removeCase). */
function openPlanMenu(row, x, y) {
  var was = document.querySelector(".menu[data-planm]");
  if (was) was.remove();
  var id = +row.getAttribute("data-id");
  var box = document.createElement("div");
  box.className = "menu";
  box.setAttribute("data-planm", String(id));
  box.setAttribute("role", "menu");
  box.innerHTML =
    '<button type="button" class="mi" role="menuitem" data-prename>Переименовать</button>' +
    '<div class="msep"></div>' +
    '<button type="button" class="mi danger" role="menuitem" data-pdel>Удалить план</button>';
  document.body.appendChild(box);
  var r = box.getBoundingClientRect();
  box.style.left = Math.max(8, Math.min(x, window.innerWidth - r.width - 8)) + "px";
  box.style.top = Math.max(8, Math.min(y, window.innerHeight - r.height - 8)) + "px";
  box.querySelector(".mi").focus();
  var close = function () {
    box.remove();
    document.removeEventListener("mousedown", away, true);
  };
  var away = function (e) { if (!box.contains(e.target)) close(); };
  document.addEventListener("mousedown", away, true);
  box.addEventListener("click", function (e) {
    if (e.target.closest("[data-prename]")) { close(); startRename(row); return; }
    if (e.target.closest("[data-pdel]")) { close(); removeCase(id); }
  });
  box.addEventListener("keydown", function (e) {
    var items = Array.prototype.slice.call(box.querySelectorAll(".mi"));
    var i = items.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); close(); row.focus(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); items[(i + items.length - 1) % items.length].focus(); }
  });
}

/* Название плана в его шапке правится на месте (макет Plan.dc.html):
   то же поле, что в списке планов (UI-004) — растёт по высоте под текст,
   Enter сохраняет, Esc отменяет, щелчок мимо сохраняет. */
/* Имя плана — кнопка внутри заголовка: заголовок остаётся заголовком, а
   правка получает свой орган. Имя стоит содержимым кнопки, а не в aria-label:
   иначе диктор назвал бы её «переименовать» вместо названия плана. */
function headNameHtml(name) {
  return '<button type="button" id="pname" title="Переименовать план">' +
    esc(name || "без названия") + "</button>";
}

function startHeadRename() {
  var head = $("ptitle"), btn = $("pname");
  if (!caseId || head.querySelector("textarea")) return;
  var was = (btn || head).textContent;
  var inp = document.createElement("textarea");
  inp.className = "rename";
  inp.rows = 1;
  inp.value = was;
  inp.setAttribute("aria-label", "Название плана");
  if (btn) btn.remove();
  head.appendChild(inp);
  var fit = function () {
    inp.style.height = "auto";
    inp.style.height = (inp.scrollHeight + inp.offsetHeight - inp.clientHeight) + "px";
  };
  fit();
  inp.addEventListener("input", fit);
  inp.focus();
  inp.select();
  var done = false;
  function finish(save) {
    if (done) return;
    done = true;
    var name = inp.value.trim();
    head.innerHTML = headNameHtml(save && name ? name : was);
    if (!save || !name || name === was) return;
    api("/api/case/" + caseId, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: name })
    }).then(function () { loadCases(); tick(); });
  }
  inp.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  inp.addEventListener("blur", function () { finish(true); });
}

/* Кнопка внутри заголовка сама отвечает на Enter и пробел — своих
   обработчиков клавиатуры ей не нужно. Слушаем заголовок, потому что кнопку
   на время правки из него вынимают. */
$("ptitle").addEventListener("click", function (e) {
  if (e.target.closest("#pname")) startHeadRename();
});

/* Переименование прямо в строке: отдельное окно ради одного поля — лишний шаг.
   Поле многострочное и на всю ширину строки: однострочное влезало в место
   названия между точкой и кнопками — 82 px, — и от «План ФОТ на 2026 год ·
   живой прогон» было видно «зой прогон». Кнопки на время правки убираются. */
function startRename(row) {
  var id = +row.getAttribute("data-id");
  var ttl = row.querySelector(".ttl");
  // Показанное имя обрезано в середине, и к нему дописано состояние —
  // править надо настоящее, а состояние вернуть на место после правки.
  var was = ttl.getAttribute("data-title") || ttl.textContent;
  var note = ttl.getAttribute("data-note") || "";
  var inp = document.createElement("textarea");
  inp.className = "rename";
  inp.rows = 1;
  inp.value = was;
  row.classList.add("renaming");
  ttl.replaceWith(inp);
  var fit = function () {
    inp.style.height = "auto";
    // scrollHeight не включает рамку: при border-box поле подрезало низ
    // последней строки на 2 px, поэтому рамку добавляем явно.
    inp.style.height = (inp.scrollHeight + inp.offsetHeight - inp.clientHeight) + "px";
  };
  fit();
  inp.addEventListener("input", fit);
  inp.focus();
  inp.select();

  var done = false;
  function finish(save) {
    if (done) return;
    done = true;
    var name = inp.value.trim();
    var span = document.createElement("span");
    var shown = save && name ? name : was;
    span.className = "ttl";
    span.setAttribute("data-title", shown);
    span.setAttribute("data-note", note);
    span.setAttribute("data-full", shown + note);
    span.title = shown + note;
    span.textContent = shown + note;
    inp.replaceWith(span);
    fitTitles();
    row.classList.remove("renaming");
    if (!save || !name || name === was) return;
    api("/api/case/" + id, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: name })
    }).then(function () { loadCases(); if (caseId === id) tick(); });
  }
  inp.addEventListener("keydown", function (e) {
    // Название — одна строка: Enter сохраняет, а не переносит.
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") finish(false);
  });
  inp.addEventListener("blur", function () { finish(true); });
  inp.addEventListener("click", function (e) { e.stopPropagation(); });
}

/* Имя плана переносится по словам и обрезается на второй строке — этим
   занят браузер, считать нечего. Функция осталась точкой, где имя
   возвращается к полному виду после правки. */
function fitTitles() {
  Array.prototype.forEach.call(document.querySelectorAll("#caselist .ttl"),
    function (el) {
      var full = el.getAttribute("data-full");
      if (full != null && el.textContent !== full) el.textContent = full;
    });
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
  var name = t ? (t.getAttribute("data-title") || t.textContent).trim() : "план";
  // Что уйдёт и что останется — двумя короткими фразами. «Этого плана» лишнее:
  // план назван в заголовке. Перечень «документы, договоры, штатное расписание,
  // нормативы» — это и есть реестр организации; целое называть вернее, чем три
  // сущности из пяти.
  ask("Удалить «" + name + "»?",
      "Лента, работы агентов и расчёты уйдут вместе с ним. " +
      "Реестр организации не изменится.", "Удалить план").then(function (yes) {
    if (yes) doRemoveCase(id);
  });
}

function doRemoveCase(id) {
  api("/api/case/" + id, { method: "DELETE" }).then(function () {
    var gone = caseId === id;
    if (gone) { caseId = null; state = null; chatSync(); }
    return loadCases().then(function (rows) {
      if (!gone) return;
      // Удалён открытый план. На реестре чат переходит к другому плану, а
      // экран остаётся; на экране плана открывается другой. Шаг истории
      // заменяется: удалённого плана по адресу больше нет.
      if (inRegistry) {
        if (rows.length) { planSwitch(rows[0].id); tick(); }
        return;
      }
      if (rows.length) open(rows[0].id, "replace");
      else newCase("replace");
    });
  });
}

/* how — как записать переход в адрес (navigate): «replace» при входе и после
   удаления, иначе новый шаг истории; «stay» — завести план, не уходя с
   реестра: он нужен чату и разбору загруженных файлов, а не как экран. */
function newCase(how) {
  return api("/api/cases", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ year: 2026 })
  }).then(function (r) {
    return loadCases().then(function () {
      if (how === "stay") { planSwitch(r.id); tick(); } else open(r.id, how);
    });
  });
}

/* План открывается на «Договорах»: «Ленты» среди вкладок нет (30.09.2026).
   Повторный щелчок по открытому плану перечитывает его, как раньше. */
function open(id, how) {
  navigate({ screen: "plan", plan: id, view: "ctr", pctr: null, run: null, reload: true }, how);
}

/* Текущий план: последний посчитанный, сразу сводкой года. Экономист заходит
 * посмотреть план, а не переписку. Посчитанного нет — первый план, планов
 * нет — реестр. Сводке нужен список прогонов, и setView ждёт состояние плана
 * сама (stateReady), а не таймер. */
var casesCache = [];
function openCurrent(how) {
  var computed = casesCache.filter(function (c) { return c.run_id; });
  var c = computed[0] || casesCache[0];
  if (!c) { navigate({ screen: "registry" }, how); return; }
  navigate({ screen: "plan", plan: c.id, view: computed.length ? "sum" : "ctr",
             pctr: null, run: null, reload: true }, how);
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
var sending = null, sendingFor = null;

function renderFeed() {
  var feed = $("feed");
  // Переписка другого плана под этим не показывается: ответ по только что
  // открытому плану ещё не пришёл (planSwitch очистил ленту).
  if (!state || !state.case || state.case.id !== caseId) { feed.innerHTML = ""; return; }
  // Закрытая панель прокрутки не держит (display:none): где стояла лента,
  // помнят feedPin и feedTop, и setChat возвращает её туда при открытии.
  var seen = feed.clientHeight > 0;
  var atBottom = seen ? feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60 : feedPin;
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
  if (seen && (atBottom || (sending && sendingFor === caseId))) feed.scrollTop = feed.scrollHeight;
}

/* Эхо своей реплики и строка ожидания ответа. Эхо снимается, как только та
   же реплика пришла с сервера, — иначе она двоилась. */
function waitingMsg() {
  // Эхо — только в ленте того плана, куда реплика ушла: чат переживает
  // переход к другому плану, а ответ ещё идёт.
  if (!sending || sendingFor !== caseId) return "";
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
         '<div class="foot"><button class="primary" style="width:auto" data-act="applyref"' +
         (p.effective_from ? ' data-eff="' + esc(p.effective_from) + '"' : "") + ">" +
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
    else if (a.available === false) { cls += " mock"; sub = "сейчас недоступен — " + (a.why || "нет ресурса"); }
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
/* Названия целей расчёта в графах версий (раздел 13.1) — формальные, как
   у решателя (решение владельца 30.09.2026): обрывки «смены», «суммы» не
   говорили, что измерено. Скобка с пояснением у первой цели снята — что
   считается, говорит единица графы. */
var GOAL_SHORT = {
  "Смены договора оклада (переводы и открытия)": "Смены договора оклада",
  "Переводы оклада между договорами": "Переводы оклада между договорами",
  "Административная сложность выплат": "Административная сложность выплат",
  "Отклонение от равномерного освоения": "Отклонение от равномерного освоения",
  "Изменение сумм выплат между месяцами": "Изменение сумм выплат между месяцами",
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
                    // Поле ввода — в панели чата: кнопка открывает её.
                    : '<div class="opts"><button type="button" data-openchat>Ответить в чате</button></div>');
}

function renderStage() {
  var c = state.case;
  // Работы приходят от старой к новой: в шапку берём последнюю, иначе
  // первой встанет давняя строка, а не то, чем сервис занят сейчас.
  var working = state.activities.filter(function (a) { return a.state === "идет"; }).slice(-1);

  var title = $("ptitle");
  if (!title.querySelector("textarea")) title.innerHTML = headNameHtml(c.title);

  // Вердикт справа от названия: годится ли план и чем сейчас занят сервис.
  // Слово одно — состояние, а не набор меток: дату посчитанного расчёта
  // несёт выбор расчёта ниже, а не отдельная подпись «обновлено».
  var lastOk = (state.runs || []).filter(function (r) { return r.status === "OPTIMAL"; })[0];
  var verdict, vcls = "";
  if (working.length) {
    verdict = "агент работает: " + working[0].title;
  } else if (lastOk && lastOk.review === "review_required") {
    verdict = "План требует пересмотра"; vcls = " warn";
  } else if (c.stage === "нет решения" || c.stage === "не успел") {
    verdict = "Решения нет"; vcls = " bad";
  } else if (!c.has_data) {
    verdict = "Нужны документы по договорам"; vcls = " warn";
  } else if (c.stage === "посчитано") {
    verdict = "План посчитан"; vcls = " ok";
  } else {
    verdict = cap(c.stage);
  }
  var pv = $("pverdict");
  pv.className = "pverdict" + vcls;
  pv.textContent = verdict;
  pv.title = lastOk && lastOk.review === "review_required"
    ? (lastOk.review_log || []).map(function (x) { return x["что"]; }).join("; ") : "";

  // Какой расчёт открыт — в шапке плана, а не только в панели отчёта: это
  // свойство плана, и по нему читается дата последнего счёта.
  // Выбран тот, что в адресе (?run=), иначе открытый в отчёте: вкладка
  // «Проекты приказов» отчёт не грузит, а расчёт у неё тот же. Номер и дата
  // связаны словом, а не точкой (UI-015).
  var saved = (state.runs || []).filter(function (r) { return r.status === "OPTIMAL"; });
  var picked = chosenRun || vrun;
  $("prun").innerHTML = saved.length
    ? '<label class="fld">Расчёт<select data-sel="run">' + saved.map(function (r) {
        return '<option value="' + r.id + '"' + (r.id === picked ? " selected" : "") +
               ">№ " + r.id + (r.created ? " от " + esc(r.created) : "") + "</option>";
      }).join("") + "</select></label>"
    : "";

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
  // Лента живёт в панели чата и отрисовывается всегда: панель открывают в
  // любой момент, и переписка должна быть уже на месте.
  safely("лента", renderFeed);
  // Имя плана в шапке чата на реестре — то же, что после переименования.
  safely("чат", chatSync);
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
    // Величины документа действуют с его даты (01.10.2026), прежние — до неё.
    body: JSON.stringify({ edits: rows, case_id: caseId,
                           effective_from: btn.getAttribute("data-eff") || null })
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
        // Почему считать нельзя — словами сервера: нет сотрудников или
        // договоров, не задана зарплата месяцев.
        (pf["стоит"]
          ? (pf["почему"] || ["Нет сотрудников или договоров. Загрузите документы в реестр."])
              .map(function (t) { return "<p>" + esc(t) + "</p>"; }).join("") : "") +
        '<div class="btns">' +
          '<button type="button" class="no">Отмена</button>' +
          (pf["стоит"] ? "" :
           '<button type="button" class="yes">Рассчитать</button>') +
        "</div></div>";
    document.body.appendChild(back);
    // Кнопка расчёта отключена на время окна и фокус теряет — возвращаем явно.
    var release = holdFocus(back, close, $("solve"));
    function close() { back.remove(); $("solve").disabled = false; release(); }
    back.querySelector(".no").onclick = close;
    back.onclick = function (e) { if (e.target === back) close(); };
    var yes = back.querySelector(".yes");
    if (yes) yes.onclick = function () {
      back.remove();
      api("/api/case/" + caseId + "/solve", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ operation_id: opId() })
      }).then(function () { showToast("Расчёт запущен"); return tick(); });
    };
    (yes || back.querySelector(".no")).focus();
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
/* vbasis — основание открытого плана: снятые договоры, условия, версии
   (SOURCE-003, SOURCE-006); pctrOpen — договор, чьи условия раскрыты. */
var vbasis = null, pctrOpen = null, pctrShown = null;
var view = "ctr", vdata = null, vresult = null, vrun = null, vrules = null,
    vsum = null, vpay = null, vrep = null;
//: Сколько реплик пришло, пока лента не на виду.
var unseen = 0, lastMsgCount = null;

/* Переключатели чата — в шапке плана и в строке заголовка реестра (UI-012,
   30.09.2026: «чтоб чат с агентами можно было из любого места открыть»).
   Новые реплики, пока чат закрыт, — точкой на значке. Чат — переписка
   плана: без плана значок недоступен и в подсказке говорит почему. */
function markFeedTab() {
  var t = !caseId ? "Чат с агентами: планов нет"
        : unseen ? "Чат с агентами — новых реплик: " + unseen : "Чат с агентами";
  Array.prototype.forEach.call(document.querySelectorAll("[data-chattoggle]"), function (b) {
    if (unseen > 0 && caseId) b.setAttribute("data-new", ""); else b.removeAttribute("data-new");
    b.setAttribute("title", t);
    b.setAttribute("aria-label", caseId ? t : "Чат с агентами");
    b.setAttribute("aria-expanded", chatShown() ? "true" : "false");
    // aria-disabled, а не disabled: у отключённой кнопки подсказка в части
    // браузеров не всплывает, а она и объясняет, почему чата нет.
    if (caseId) b.removeAttribute("aria-disabled"); else b.setAttribute("aria-disabled", "true");
  });
}

var MONTHS = ["янв", "фев", "мар", "апр", "май", "июн",
              "июл", "авг", "сен", "окт", "ноя", "дек"];
var KINDS = { oklad: "оклад", okl: "оклад", prk: "приказ",
              k120: "120", k122: "122", k124: "124", k152: "152" };
function monthName(m) { return MONTHS[m] || MONTHS[m - 1] || m; }

/* Вкладка плана. Сама адрес не пишет: переход с записью в адрес — navigate().
   Ответ, пришедший после перехода на другую вкладку или план, экран не
   трогает (viewSeq): иначе медленный отчёт ложился поверх договоров. */
var viewSeq = 0;
function setView(v) {
  view = v;
  var seq = ++viewSeq;
  markFeedTab();
  var cur = $("opencurrent");
  if (cur) cur.classList.toggle("on", v === "sum" && !inRegistry && !inAgents);
  Array.prototype.forEach.call(document.querySelectorAll(".tabs .tab"), function (b) {
    var here = b.getAttribute("data-view") === v;
    b.classList.toggle("on", here);
    // Выбранная вкладка слышна диктору, а не только видна классом.
    if (here) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  });
  $("view").hidden = false;
  $("view").innerHTML = '<div class="none loading" role="status" aria-live="polite">Загружаю…</div>';
  var need = v === "ctr" || v === "emp" || v === "zp" || v === "fact"
    // Договорам плана нужны и строки реестра, и основание этого плана:
    // без второго не видно, что снято и что переопределено.
    ? Promise.all([api("/api/case/" + caseId + "/data"),
                   api("/api/case/" + caseId + "/basis")])
        .then(function (r) {
          vdata = r[0]; vbasis = r[1];
          // Реестр сравнялся с правкой плана — правка снята сама
          // (пользователь 30.09.2026: «снимать правку автоматически»).
          if ((vbasis.dissolved || []).length) {
            showToast("Реестр сравнялся с правкой плана, правка снята: " +
                      vbasis.dissolved.join("; "));
          }
        })
    : (v === "ref" || v === "sub")
    ? api("/api/case/" + caseId + "/data").then(function (d) { vdata = d; })
    : v === "ord"
    ? stateReady().then(loadOrders)
    : stateReady().then(loadResult);
  need.then(function () {
    if (seq === viewSeq) renderView();
  }).catch(function (e) {
    if (seq !== viewSeq) return;
    $("view").innerHTML = '<div class="none">' + esc(e.message || "не загрузилось") + "</div>";
  });
}

/* Отчёту нужен список расчётов плана — он приходит опросом состояния. План,
   открытый по адресу или только что выбранный, первого ответа ещё не имеет:
   ждём его, а не таймер. */
function stateReady() {
  if (state && state.case && state.case.id === caseId) return Promise.resolve();
  return tick().then(function () {
    if (!(state && state.case && state.case.id === caseId)) throw new Error("план не загрузился");
  });
}

function loadResult() {
  var runs = state.runs || [];
  // Выбранная версия — из шапки плана и адреса (?run=); иначе последняя.
  var ok = (chosenRun && runs.filter(function (r) {
              return r.id === chosenRun && r.status === "OPTIMAL"; })[0]) ||
           runs.filter(function (r) { return r.status === "OPTIMAL"; })[0];
  // Расчёт из адреса удалён или не посчитан — показываем последний, а адрес
  // перестаёт его называть.
  if (chosenRun && !(ok && ok.id === chosenRun)) {
    chosenRun = null;
    routeWrite("replace");
  }
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

/* Помесячный график: выплаты по плану столбиками, остаток ФОТ линией.
   Поступлений нет: в расчёт они не подаются (SOURCE-009). Рисуем SVG сами —
   библиотека ради двенадцати столбиков не нужна. */
function monthChart(labels, paid, rest) {
  var W = 720, H = 190, L = 54, B = 26, T = 10;
  var max = Math.max.apply(null, paid.concat([1]));
  var step = (W - L) / labels.length;
  var bw = Math.max(4, step / 2 - 3);
  var y = function (v) { return T + (H - T - B) * (1 - v / max); };
  var bars = labels.map(function (m, i) {
    var x = L + i * step + step / 2 - bw / 2;
    return '<rect class="b2" x="' + x.toFixed(1) + '" y="' + y(paid[i]).toFixed(1) +
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
    '<div class="lgd"><span class="k2">выплаты по плану</span>' +
    '<span class="k3">остаток ФОТ на конец месяца</span></div></div>';
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

  var ctrs = '<table class="mini"><thead><tr><th>Договор</th><th>ГОЗ</th><th class="n">ФОТ за год, ₽</th>' +
    '<th class="n">Выплаты по плану за год, ₽</th>' +
    '<th class="n">Нераспределённый ФОТ, ₽</th><th class="n">Освоение ФОТ</th><th></th></tr></thead><tbody>' +
    vsum["договоры"].map(function (c) {
      return "<tr><td>" + esc(c["договор"]) + "</td><td>" + (c["ГОЗ"] ? "да" : "") + "</td>" +
        '<td class="n">' + mo(c["ФОТ"]) +
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
    '<div class="grp"><h4>Выплаты по плану и остаток ФОТ по месяцам</h4>' +
    monthChart(m["подписи"], m["выплаты"], m["остаток ФОТ"] || []) + "</div>" +
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
  // Отклонение — значение минус предел или норма, одним знаком для обоих:
  // «запас» у нормы-равенства говорил не то.
  return '<table class="mini rb2"><thead><tr><th>Объект проверки</th>' +
    '<th class="n">Значение' + esc(unit) + '</th><th class="n">' + esc(limitName) +
    esc(unit) + '</th><th class="n">Отклонение' + esc(unit) + '</th><th></th></tr></thead><tbody>' +
    rows.map(function (s, i) {
      var d = s["факт"] - s["предел"];
      return '<tr data-review-key="rule:' + vrules.indexOf(r) + ':' + i + '" class="' + (s["нарушено"] ? "over" : "") + '"><td>' +
        esc(s["объект"]) +
        (s["таких же"] > 1 ? '<span class="same">и ещё ' + (s["таких же"] - 1) +
                             " с тем же результатом</span>" : "") +
        '</td><td class="n">' + mo(s["факт"]) +
        '</td><td class="n">' + mo(s["предел"]) + '</td><td class="n' +
        (s["нарушено"] ? " over" : "") + '">' + (d > 0.004 ? "+" : "") + mo(d) + "</td><td>" +
        (s["доля"] == null ? "" : bar(s["доля"], s["нарушено"] ? "bad" : "ok")) +
        "</td></tr>";
    }).join("") + "</tbody></table>";
}

function ruleRow(r) {
  var st = RULE_STATE[r["состояние"]] || { cls: "off" };
  var hard = r["тип"] === "нарушать нельзя";
  return '<div class="rule ' + st.cls + (hard ? " hard" : "") + '">' +
    '<span class="cdot2"></span>' +
    '<div class="rn">' + esc(r["название"] || r["правило"]) +
      (r["тип"] !== "показатель"
        ? '<span class="rk' + (hard ? " h" : "") + '">' + esc(r["тип"]) + "</span>" : "") +
    "</div>" +
    '<div class="rs">' + esc(r["состояние"]) + "</div>" +
    '<div class="rm">' + esc(r["смысл"]) + "</div>" +
    '<div class="rf">' +
      (r["использовано"] != null
        ? bar(r["использовано"], r["состояние"] === "нарушено" ? "bad" : "ok") : "") +
      esc(r["факт"] || "") +
      (r["где"] ? '<span class="rw">Где смотреть: ' + esc(r["где"]) + "</span>" : "") +
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
  // По умолчанию 50 строк (UI-017).
  var per = sizeOf[key] || 50;
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
/* lead — действие добавления слева в подвале: подвал липнет к низу окна, и
   «+ Добавить» под рукой при любой длине таблицы (решение 19.09.2026). */
function pager(key, pg, lead) {
  // Полоса показывается всегда, даже когда страница одна: счёт строк живёт
  // здесь, а не в заголовке раздела.
  // Пустой список тоже с подвалом: место органов не прыгает (UI-017).
  var arrow = function (to, label, off) {
    return '<button type="button" class="ar" data-pg="' + key + ":" + to + '"' +
           (off ? " disabled" : "") + ' aria-label="' + label + '">' +
           (to < pg.page ? "‹" : "›") + "</button>";
  };
  return '<div class="pager">' + (lead ? '<span class="padd">' + lead + "</span>" : "") +
    '<label class="per">Строк на странице ' +
      '<select data-per="' + key + '">' +
      PAGE_SIZES.map(function (n) {
        return '<option value="' + n + '"' + (n === pg.per ? " selected" : "") +
               ">" + n + "</option>";
      }).join("") + "</select></label>" +
    '<span class="rng">' + (pg.total ? (pg.from + 1) + "–" + (pg.from + pg.rows.length) : "0") +
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
/* opts.numOf(i) — номер строки i или "" для строки без номера (группа,
   «Итого»): с ним графа «№» есть и у таблицы plain. opts.numHead — разметка
   клетки шапки над номерами вместо «№». */
function table(head, rows, note, opts) {
  var numOf = opts && opts.numOf;
  var num = !(opts && opts.plain) || !!numOf;
  // numAfter — сколько граф стоит перед номером: у договоров плана флажок
  // «в расчёте» перед номером (решение пользователя 20.09.2026).
  var after = (opts && opts.numAfter) || 0;
  var put = function (cells, numCell) {
    return num ? cells.slice(0, after).concat([numCell], cells.slice(after)) : cells;
  };
  var h = note ? '<div class="vh">' + note + "</div>" : "";
  // emptyHead — пустая таблица остаётся таблицей: шапка граф без строк.
  if (!rows.length && !(opts && opts.emptyHead)) return h + '<div class="none">Пусто</div>';
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
  // Карточке свойств шапка граф не нужна: графы там называются заголовками
  // групп, а «свойство / значение / источник» повторяло бы очевидное.
  if (opts && opts.nohead) {
    h += '<div class="hscroll"><table' + (cols ? ' class="fixed"' : "") + ">" + cols +
         "<tbody>";
  } else {
  h += '<div class="hscroll"><table' + (cols ? ' class="fixed"' : "") + ">" + cols +
       "<thead" + (opts && opts.headCls ? ' class="' + opts.headCls + '"' : "") +
       "><tr>" + put(head.map(function (c) {
         // Заголовок бывает строкой, числовой колонкой {t} или готовой
         // разметкой {v} — в ней, например, флажок «выделить все».
         if (typeof c !== "object") return "<th>" + esc(cap(c)) + "</th>";
         if (c.v != null) return '<th class="' + (c.cls || "") + '"' +
                                 (c.span ? ' colspan="' + c.span + '"' : "") + ">" +
                                 c.v + "</th>";
         return '<th class="n">' + esc(cap(c.t)) + "</th>";
       }), '<th class="num">' + (opts && opts.numHead || "№") + "</th>").join("") +
       "</tr></thead><tbody>";
  }
  var first = (opts && opts.startNum) || 0;
  h += rows.map(function (r, i) {
    var rc = opts && opts.rowCls ? opts.rowCls(i) : "";
    var ra = opts && opts.rowAttr ? opts.rowAttr(i) : "";
    return "<tr" + (rc ? ' class="' + rc + '"' : "") + (ra ? " " + ra : "") + ">" +
      put(r.map(function (c) {
        return (c && typeof c === "object")
          ? '<td class="' + (c.cls || "") + '"' + (c.cs ? ' colspan="' + c.cs + '"' : "") +
            ">" + (c.v == null ? "—" : c.v) + "</td>"
          : "<td>" + (c == null || c === "" ? "—" : esc(c)) + "</td>";
      }), '<td class="num">' + (numOf ? numOf(i) : first + i + 1) + "</td>").join("") + "</tr>";
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

   Окно договора — месяцы его выплат из раздела 2 (report.py, «окно»): от
   начала договора до позднего из сроков выплат. Прежде окно бралось из кассы
   договора, которой в отчёте больше нет (SOURCE-009). */
var repOpenCache = null;
function repOpenMonths(code) {
  if (!repOpenCache) {
    repOpenCache = {};
    ((vrep && vrep["договоры"]) || []).forEach(function (c) {
      var w = c["окно"] || [1, 12];
      repOpenCache[c["код"]] = MON.map(function (x, i) { return i + 1 >= w[0] && i + 1 <= w[1]; });
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
/* Краткие названия разделов: оглавление отчёта, кнопки перехода протокола
   контроля и листы выгрузки (export_report.SHEETS) называют раздел одними
   словами; полное формальное название — в заголовке раздела. Владелец
   продукта 30.09.2026: «в результатах расчёта есть таблицы, например —
   нехватка, очень плохо названа неформально. Нужно жёстко формализовать».
   Номера разделов прежние: по ним владелец называет таблицы («6.2», «11»). */
var REP_SHORT = { 1: "Сводные показатели", 2: "Договоры", 3: "Освоение помесячно",
                  4: "Освоение нарастающим итогом", 5: "Выплаты по видам",
                  6: "Выплаты сотрудникам", 7: "Сотрудники и ставки", 8: "Сравнение с БЭП",
                  9: "Приказы № 2556 и № 4", 10: "Трудоёмкость по РКМ",
                  11: "Распределение трудоёмкости", 12: "Незакрытая трудоёмкость",
                  13: "Версии и параметры", 14: "Недоплата заработной платы",
                  15: "Протокол контроля", 16: "По подразделениям" };
/* mark — отметка раздела в оглавлении («er» или «wr»), когда нарушение
   названо не в заголовке раздела, а в заголовках его таблиц (раздел 9). */
function secH(n, t, meta, mark) {
  REP_SECTIONS.push({ n: n, t: REP_SHORT[n] || t, meta: meta || "", mark: mark || "" });
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
function subH(n, t, meta) {
  return '<div class="sech sub"><span class="sn">' + n + '</span><span class="stt">' + esc(t) + "</span>" +
    (meta || "") + "</div>";
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
  return '<th class="n ' + cls + '"' + dc + (c.fact ? REP_FACT_ATTR : "") + ">" + wrap(esc(c.t)) + "</th>";
}
/* Месяцы факта (правило 26; владелец продукта 30.09.2026: «пересчёт с
   текущими вводными… не трогая январь и февраль»): 1…F — начисления, а не
   план. Отметка одна на все помесячные таблицы отчёта и стоит только здесь и
   в monHead: над месяцами факта — «Факт по <месяц>» вместо кварталов, у
   самих месяцев — признак data-fact и подсказка. Числа месяцев факта
   показаны и входят в итоги, но не судятся. */
var REP_FACT_ATTR = ' data-fact="1" title="Факт начислений: месяц не пересчитывается"';
function repFactTo() { return (vrep && +vrep["факт по"]) || 0; }
function repFactName(n) {
  var names = typeof MONTH_NAMES !== "undefined" ? MONTH_NAMES : MON;
  return names[n - 1];
}
/* Верхний ярус шапки: месяцы или кварталы над своими графами. Двенадцать
   подписей вида «03.26 по плану» читались как сплошная строка текста, а
   границы кварталов не были видны вовсе.
   lead — сколько граф слева не относится к месяцам, per — сколько граф на
   месяц, tail — сколько граф справа (итог за год и пределы), leadLabel —
   подпись над ключевыми графами слева: у таблицы в одной величине это её
   единица («Чел.-мес.» в разделе 11). Месяцы факта выбранного периода — одной
   ячейкой «Факт по <месяц>», кварталы — по оставшимся месяцам. */
function monGroupRow(lead, per, tail, mode, leadLabel) {
  var g = [], F = repFactTo();
  if (lead) g.push({ t: "", span: lead, lead: leadLabel || "" });
  var fact = repMon.filter(function (m) { return m <= F; });
  var plan = repMon.filter(function (m) { return m > F; });
  if (fact.length) g.push({ t: "Факт по " + repFactName(F), span: fact.length * per, fact: true,
                            tip: fact.length > 1 ? "Факт начислений: месяцы не пересчитываются"
                                                 : "Факт начислений: месяц не пересчитывается" });
  if (mode === "month") {
    plan.forEach(function (m) { g.push({ t: MON[m - 1], span: per }); });
  } else {
    var qs = [[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11, 12]];
    var names = ["I квартал", "II квартал", "III квартал", "IV квартал"];
    qs.forEach(function (arr, i) {
      var n = arr.filter(function (m) { return plan.indexOf(m) >= 0; }).length;
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
          // Подпись над ключевыми графами (g.lead) — в первой видимой из них:
          // липкая ячейка с объединением на узком экране уезжала бы вместе с
          // незакреплённой соседней графой.
          var fixedTop = "";
          for (var q = from; q < from + count && q < N; q++) {
            if (visPos[q] > 0) fixedTop += '<th class="fx gfix" data-c="' + visPos[q] + '">' +
              (g.lead && !fixedTop ? esc(g.lead) : "") + '</th>';
          }
          return fixedTop;
        }
        return span ? '<th class="' + cls + '" colspan="' + span + '"' +
          (g.fact ? ' data-fact="1" title="' + esc(g.tip || "") + '"' : "") + '>' +
          esc(g.t || "") + "</th>" : "";
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
    var mark = x.mark === "er" || /chip er/.test(x.meta || "")
      ? '<i class="mk er" title="В разделе есть блокирующие ошибки"></i>' +
        '<span class="review-sr"> — есть блокирующие ошибки</span>'
      : x.mark === "wr" || /chip wr/.test(x.meta || "")
      ? '<i class="mk" title="В разделе есть замечания"></i>' +
        '<span class="review-sr"> — есть замечания</span>' : "";
    // Ссылка — адрес раздела (UI-021): открытая в новой вкладке, она ведёт
    // в тот же раздел того же расчёта.
    var to = routePath({ screen: "plan", plan: caseId, view: "sum", sec: x.n, run: chosenRun });
    return '<a href="' + esc(to) + '" data-sec="' + x.n + '"' +
           (x.n === repSec ? ' class="on"' : "") + '><b>' + x.n + "</b><span>" + esc(x.t) +
           "</span>" + mark + "</a>";
  }).join("") + "</nav>";
}

function repToolbar() {
  var title = (state.case && state.case.title) || "";
  var sectionTitle = ((REP_SECTIONS.find(function (x) { return x.n === repSec; }) || {}).t || "");
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
  // Разделы выбираются в боковом оглавлении отчёта (repNav), а не здесь;
  // выбор расчёта переехал в шапку плана (макет Plan.dc.html) — это свойство
  // плана, а не панели. Здесь остаётся период, вид и действия одной строкой.
  return '<div class="rtool"><div class="ftitle"><b>' + esc(title) + "</b>" +
  "</div>" +
  (issueReturn ? '<nav class="issue-context" aria-label="Путь к замечанию"><button type="button" data-issue-back title="Вернуться к списку замечаний">← Замечания к расчёту</button><span aria-hidden="true">/</span><span>' + esc(sectionTitle) + '</span><span class="issue-context-actions">' + reportActions + '</span></nav>' : '') +
  '<div class="acts">' +
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
  var F = repFactTo();
  return repMon.map(function (m) {
    return { t: fn ? fn(MON[m - 1], m - 1) : MON[m - 1], fact: m <= F };
  });
}
function repGoalsHtml() {
  var run = (state.runs || []).filter(function (r) { return r.id === vrun; })[0];
  var s = (run && run.summary) || {};
  // Только взвешенные цели: у них единицы понятны экономисту (штуки, рубли).
  // Значения стадий-правил — внутренние суммы штрафов, их не показываем.
  var goals = (s["цели"] || []).filter(function (g) { return g["приоритет"] === "вес"; });
  var settings = (vrep && vrep["настройки"]) || [];
  var h = "";
  // 13.1 Версии расчёта: состояние, время, дата и взвешенные цели каждой
  // версии одной таблицей — сравнение версий по числам и есть работа отчёта.
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
      // Прежний результат после смены данных остаётся, но помечен: применять
      // его дальше без пересмотра нельзя (VER-003). Причины — в ленте.
      var stale = ok && r.review === "review_required";
      var why = stale ? (r.review_log || []).map(function (x) { return x["что"]; }).join("; ") : "";
      // Состояние словами, без знака-разделителя (UI-015).
      return { cls: r.id === vrun ? "sel" : "", cells: [
        nc(String(r.id)),
        { v: '<span title="' + esc(why) + '">' + esc(ok ? (stale ? "Рассчитан, требует пересмотра" : "Рассчитан") : r.status) +
             (r.retry_of ? esc(", повтор расчёта № " + r.retry_of) : "") + "</span>",
          cls: ok ? (stale ? "er" : "ok") : (r.status === "идет" ? "" : "er") },
        nc(r.seconds != null ? String(Math.round(r.seconds)) : ""),
        r.created || "",
      ].concat(gcols.map(function (c) {
        var g = by[c.key];
        if (!g) return gc("—");
        return nc(g["единица"] === "₽" ? rmi(g["значение"]) : fmtGoal(g["значение"]));
      }), [ok ? { v: '<a class="xl" href="/api/case/' + caseId + "/result/" + r.id + '">xlsx</a>' }
               : (r.retryable ? { v: '<a class="xl" href="#" data-retry="' + r.id +
                                     '" title="Повторить расчёт по закреплённому входу">повторить</a>' } : "")]) };
    });
    h += secH(13, "Версии и параметры расчёта") + subH("13.1", "Версии расчёта") + repT(
      [{ t: "Версия расчёта" }, "Состояние", { t: "Время расчёта, с" }, "Дата расчёта"].concat(
        gcols.map(function (c) { return { t: c.t }; }), ["Файл результата"]), vrows, null, { fix: 1 });
  }
  if (goals.length || settings.length) {
    var rows = goals.map(function (g) {
      return [g["цель"], nc(g["единица"] === "₽" ? rmi(g["значение"]) : fmtGoal(g["значение"])),
              g["единица"] || "", nc(g["вес"] != null ? fmtGoal(g["вес"]) : null)];
    });
    // Параметры расчёта — теми же строками: экономист меняет их в чате и
    // должен видеть, с чем считался этот план. Название — формальное
    // (report.py), значение допуска — в процентах.
    settings.forEach(function (x) {
      var v = x["значение"], name = x["имя"], txt = String(v), unit = x["единица"] || "";
      if (name === "допуск трудоёмкости") { txt = rpn(Number(v)); unit = "%"; }
      rows.push([{ v: esc(x["название"] || name), cls: "set" }, nc(txt), unit, ""]);
    });
    h += (runs.length ? "" : secH(13, "Версии и параметры расчёта")) +
      subH("13.2", "Целевые показатели и параметры расчёта") + repT(
      ["Целевой показатель или параметр", { t: "Значение" }, "Единица измерения",
       { t: "Вес в целевой функции" }], rows);
  }
  if (runs.length || goals.length || settings.length) h += "</div>";
  // 14: недоплата — заработная плата месяца против выплат по плану. Бывает,
  // только когда параметры расчёта её разрешают.
  var def = (vrep && vrep["дефицит"]) || [];
  var title14 = "Недоплата заработной платы сотрудникам по месяцам, ₽";
  if (def.length) {
    var total = 0;
    def.forEach(function (d) { total += d["дефицит"] || 0; });
    h += secH(14, title14, chip("Недоплата за год: " + rm(total) + " ₽", "er")) + repT(
      ["Табельный номер", "ФИО", "Месяц", { t: "Заработная плата за месяц, ₽" },
       { t: "Выплаты по плану, ₽" }, { t: "Недоплата, ₽" }, "Причина"],
      def.map(function (d, i) {
        return {reviewKey:"deficit:"+i,cells:[d["табельный"], d["фио"], MON[d["месяц"] - 1], nc(rm(d["положено"])),
                nc(rm(d["выплачено"])), nc(rm(d["дефицит"]), "err"), d["причина"] || ""]};
      }), null, { fix: 2, tall: true, merge: [0, 1] }) + "</div>";
  } else {
    h += secH(14, title14, chip("Недоплаты нет", "ok")) + "</div>";
  }
  return h;
}

/* 16: ФОТ по управленческим подразделениям помесячно (01.10.2026, свод в
   стиле 1С по варианту дизайнера): подразделение — его люди и итог; в
   месяцах факта — факт. Суммы и итоги считает сервер (report._by_department),
   страница их не складывает (UI-017). */
function repDepartments() {
  var pd = (vrep && vrep["по подразделениям"]) || null;
  var title = "ФОТ по управленческим подразделениям помесячно, ₽";
  if (!pd || !pd["подразделения"].length) {
    return secH(16, title) + '<div class="none">Выплат нет</div></div>';
  }
  var months = function (arr) {
    return repMon.map(function (mn) { return mc(arr[mn - 1], null, mn, rmi); });
  };
  var rows = [];
  pd["подразделения"].forEach(function (d) {
    d["люди"].forEach(function (p) {
      rows.push({ cells: [d["подразделение"], p["табельный"], p["фио"]].concat(
        months(p["месяцы"]), [nc(rmi(p["год"]))]) });
    });
    rows.push({ cls: "sum", cells: [d["подразделение"], { v: "Итого по подразделению", cs: 2 },
      { skip: true }].concat(months(d["месяцы"]), [nc(rmi(d["год"]))]) });
  });
  rows.push({ cls: "sum all", cells: [{ v: "Итого по организации", cs: 3 }, { skip: true },
    { skip: true }].concat(months(pd["итого"]), [nc(rmi(pd["год"]))]) });
  return secH(16, title) + repT(
    ["Управленческое подразделение", "Табельный номер", "ФИО"].concat(monHead(),
      [{ t: "За год, ₽" }]),
    rows, null, { fix: 3, tall: true, groups: monGroupRow(3, 1, 1, "quarter"), merge: [0] }) +
    "</div>";
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
  // Раздел замечания — шаг истории: «Назад» браузера возвращает к списку
  // замечаний так же, как ссылка в пути (issueLeave в routeGo).
  routeWrite("push");
  var section=$("view").querySelector('.rep.active');
  if(!section) return;
  requestAnimationFrame(function(){
    var row=Array.prototype.find.call(section.querySelectorAll('[data-review-key]'),function(r){return r.dataset.reviewKey===issue.target.row;});
    // Ключ по человеку и виду выплаты: строк столько же, сколько договоров,
    // и годится первая — она приведёт глаз в нужное место таблицы.
    if(!row && /^pay:/.test(issue.target.row||'')) row=section.querySelector('[data-review-key^="'+issue.target.row+'"]');
    // Если точной строки нет, пользователь уже находится в нужном разделе.
    // Не добавляем в отчёт пояснительный текст от агента.
    if(!row) return;
    var fold=row.closest('details');if(fold)fold.open=true;
    row.classList.add('review-target');row.tabIndex=-1;
    var cell=issue.target.column==null?null:row.querySelector('[data-source-col="'+issue.target.column+'"]');
    if(cell)cell.classList.add('issue-target-cell');
    (cell||row).scrollIntoView({block:'center',inline:'center'});row.focus({preventScroll:true});
  });
}

/* Условие называется формально (rules.TITLES): ключ «правило» остаётся для
   матрицы проверок и списка замечаний, экран показывает «название». */
function reviewRuleTitle(r) {
  return r["название"] || r["правило"];
}

/* Раздел условия — тот же, что у замечаний (issues.js, RULE_SECTION):
   одна таблица соответствия вместо догадки по слову в «где смотреть». */
function reviewRuleTarget(r) {
  var n = PlanIssues.ruleSection ? PlanIssues.ruleSection(r["правило"]) : null;
  return n ? { section: n, label: REP_SHORT[n] || String(n) } : null;
}

function reviewRuleSummary(r) {
  return r["факт"] || "";
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
          esc(s["объект"]) + '</th><td>' + esc(s["что"] || "") + '</td></tr>';
      }).join("") + "</tbody></table></div>";
  }
  var spend = r["правило"] === "ФОТ договоров освоен";
  // У нормы-равенства (П4, заработная плата месяца, трудоёмкость) вторая
  // величина — норма, у потолка — предел. Отклонение одно на оба случая:
  // значение минус предел или норма. Прежние «запас» и «превышение» у
  // нормы говорили не то: запаса до нормы не бывает.
  var eq = r["режим"] === "равно";
  var unit = r["единица"] || "";
  var suffix = unit ? ", " + unit : "";
  var what = eq ? "нормы" : "предела";
  var headers = spend
    ? ["Договор", "Выплаты по плану, ₽", "ФОТ договора, ₽", "Нераспределённый ФОТ, ₽", "Освоение ФОТ, %"]
    : ["Объект проверки", "Значение" + suffix, (eq ? "Норма" : "Предел") + suffix,
       "Отклонение от " + what + suffix, "Доля " + what + ", %"];
  return '<div class="review-rule-scroll"><table class="review-table review-rule-table"><thead><tr>' +
    headers.map(function (h, i) {
      return '<th scope="col"' + (i ? ' class="amount"' : "") + '>' + esc(h) + '</th>';
    }).join("") + '</tr></thead><tbody>' + rows.map(function (s, i) {
      var d = spend ? s["запас"] : s["факт"] - s["предел"];
      var warn = s["внимание"] || (spend && s["запас"] > 0.5);
      var tone = s["нарушено"] ? " error" : (warn ? " warn" : "");
      var text = (spend || d <= 0.004 ? "" : "+") + mo(d) + (spend || !unit ? "" : " " + unit);
      return '<tr data-review-key="rule:' + vrules.indexOf(r) + ':' + i + '" class="' +
        (s["нарушено"] ? "over" : "") + '"><th scope="row">' + esc(s["объект"]) +
        (s["таких же"] > 1 ? '<small>Ещё ' + (s["таких же"] - 1) +
                             " с тем же результатом</small>" : "") +
        '</th><td class="amount">' + mo(s["факт"]) + '</td><td class="amount">' +
        mo(s["предел"]) + '</td><td class="amount rule-delta' + tone + '">' + esc(text) +
        '</td><td class="amount rule-share"><span>' + reviewPercent(s["доля"]) +
        ' %</span>' + bar(s["доля"], s["нарушено"] ? "bad" : (warn ? "warn" : "ok")) +
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
  return periods[0] + "–" + periods[periods.length - 1] + ", " + periods.length + " мес.";
}

function reportChecksSection() {
  var warnings = ((vresult || {}).warnings || []).filter(function (w) { return Array.isArray(w) && /^(Ошибка|Предупреждение)$/.test(w[0]); });
  var attention = (vrules || []).filter(function (r) { return r["состояние"] === "нарушено" || r["состояние"] === "внимание"; }).sort(function (a, b) { return (a["состояние"] === "нарушено" ? 0 : 1) - (b["состояние"] === "нарушено" ? 0 : 1); });
  var messageGroups = groupedRunWarnings(warnings);
  var badCount = attention.filter(function (r) { return r["состояние"] === "нарушено"; }).length;
  var warnCount = attention.length - badCount;
  // Отметки — «слово: число», без согласования числительного: «1 не освоен»
  // и «2 не освоен» читались по-разному неверно.
  var headChips = (badCount ? chip("Нарушений: " + badCount, "er") : "") +
    (warnCount ? chip("Замечаний: " + warnCount, "wr") : "") +
    (attention.length ? "" : chip("Нарушений и замечаний нет", "ok"));
  if (messageGroups.length) headChips += chip("Групп сообщений расчёта: " + messageGroups.length);

  var primary = attention.length
    ? '<section class="checks-attention" aria-label="Нарушения и замечания"><div class="review-section-title"><h3>Нарушения и замечания</h3><span class="review-count">' +
      attention.length + '</span></div>' + attention.map(function (r) {
        var target = reviewRuleTarget(r);
        return '<article class="review-rule ' + (r["состояние"] === "нарушено" ? "bad" : "warn") +
          '" data-review-key="rule:' + vrules.indexOf(r) + ':-1"><div class="review-heading"><div class="review-heading-copy"><h4>' +
          esc(reviewRuleTitle(r)) + '</h4><p>' + esc(reviewRuleSummary(r)) + '</p></div><div class="review-heading-actions">' +
          chip(r["состояние"] === "нарушено" ? "Нарушение" : "Внимание",
               r["состояние"] === "нарушено" ? "er" : "wr") +
          (target ? '<button type="button" class="review-open" data-report-sec="' + target.section +
                    '">Открыть раздел «' + esc(target.label) + '»</button>' : "") +
          '</div></div>' + reviewRuleTable(r) +
          '<details class="review-explain"><summary>Содержание условия</summary><p>' + esc(r["смысл"]) +
          '</p></details></article>';
      }).join("") + "</section>"
    : "";

  var allRules = vrules && vrules.length
    ? '<details class="fold2 checks-fold"><summary><span>Все условия контроля</span><span class="checks-fold-count">' +
      vrules.length + '</span></summary><div class="checks-fold-body">' + rulesView() + "</div></details>"
    : '<div class="none" data-review-key="checks-unavailable">Проверки этой версии недоступны</div>';
  var messages = messageGroups.length
    ? '<details class="fold2 checks-fold"><summary><span>Сообщения расчёта</span><span class="checks-fold-count">групп: ' +
      messageGroups.length + ", записей: " +
      warnings.filter(function (w) { return !/неосвоенный остаток/i.test(w[4] || ""); }).length +
      '</span></summary><div class="checks-fold-body message-groups">' + messageGroups.map(function (g) {
        return '<article class="message-group ' + (g.level === "Ошибка" ? "error" : "warn") + '"><div class="message-group-head"><strong>' +
          esc(g.title || g.area) + '</strong>' + chip(g.level === "Ошибка" ? "Ошибка" : "Внимание",
          g.level === "Ошибка" ? "er" : "wr") + '</div><p class="message-group-meta">' +
          esc([g.subject, g.area, warningPeriods(g.periods)].filter(Boolean).join(", ")) + '</p>' +
          (g.action ? '<p class="message-group-action">' + esc(g.action) + '</p>' : "") + "</article>";
      }).join("") + "</div></details>" : "";
  return secH(15, "Протокол контроля плана ФОТ", headChips) + '<div class="checks-page">' + primary +
    '<div class="checks-secondary">' + allRules + messages + "</div></div></div>";
}

/* Состояние договора на конец года (раздел 2.2): слово и цвет — тот же
   перечень, что считает report.py (CONTRACT_STATES). */
var CONTRACT_STATE = { "Выплат нет": "er", "Выплаты после срока выплаты оклада": "er",
  "Выплаты после срока выплаты надбавок": "er", "Перерыв в выплатах": "wr",
  "ФОТ освоен до срока выплат": "wr", "ФОТ освоен не полностью": "wr", "Замечаний нет": "ok" };
var LEVEL_ORDER = { er: 0, wr: 1, ok: 2 };
function repSummary() {
  var r = vrep, t = r["итоги"], fot = t["ФОТ"] || 0, year = r["год"] || "";
  var h = secH(1, "Сводные показатели плана ФОТ на " + year + " год", reportOverviewChips());
  // Поступлений и остатка средств на счетах нет: поступления в расчёт не
  // подаются, а кассу договора отдельной величиной не показываем (SOURCE-009).
  h += reportOverview() + '<div class="review-section-title"><h3>Годовые показатели</h3></div><div class="review-block"><table class="review-table review-totals"><thead><tr><th scope="col">Показатель</th><th scope="col" class="amount">За год, ₽</th><th scope="col" class="amount">Доля ФОТ по договорам, %</th></tr></thead><tbody>' +
    [["ФОТ по договорам", t["ФОТ"]], ["Выплаты по плану", t["выплачено"]],
     ["Нераспределённый ФОТ на 31.12." + year, t["остаток ФОТ"]]].map(function (x) {
      return '<tr><th scope="row">' + esc(x[0]) + '</th><td class="amount">' + esc(rm(x[1])) +
        '</td><td class="amount">' + (fot && x[1] != null ? esc(rpn(x[1] / fot)) : "") + '</td></tr>';
    }).join('') + '</tbody></table></div></div>';

  // Одна графа — одно поле, поэтому реквизиты договора и исполнение за год
  // стоят двумя таблицами: восемнадцать граф в один экран не помещаются.
  // Отметки раздела — по одной на состояние: «слово: число договоров».
  var counts = {}, states = [];
  r["договоры"].forEach(function (c) {
    (c["статусы"] || [c["статус"]]).forEach(function (st) {
      if (!counts[st]) { counts[st] = 0; states.push(st); }
      counts[st]++;
    });
  });
  var stLevel = function (st) { return CONTRACT_STATE[st] || "wr"; };
  states.sort(function (a, b) { return LEVEL_ORDER[stLevel(a)] - LEVEL_ORDER[stLevel(b)]; });
  h += secH(2, "Договоры, включённые в план", states.map(function (st) {
         return chip(esc(st) + ": " + counts[st], stLevel(st));
       }).join("")) +
    subH("2.1", "Реквизиты и условия договоров") + repT(
    ["Договор", "Наименование", "Подразделение", "Признак", "Лицевой счёт", "Дата начала",
     "Дата окончания", "Срок выплаты оклада", "Срок выплаты надбавок", "Разрешённые выплаты"],
    r["договоры"].map(function (c) {
      // Признак — «ГОЗ» или «Приоритет 2030» (UI-014); без них — вид работы
      // договора из реестра. Пусто — значит пусто, без прочерка (UI-016).
      return [c["код"], c["название"] || "", c["подразделение"] || "", c["признак"] || "",
              c["счет"] || "", c["дата начала"], c["дата окончания"], c["срок выплаты оклада"],
              c["срок выплаты надбавок"], c["выплаты"] || ""];
    }), null, { fix: 1 }) +
    subH("2.2", "Исполнение договоров по ФОТ на 31.12." + year) + repT(
    ["Договор", { t: "Месяцев с выплатами" }, { t: "Месяцев до срока выплаты оклада" },
     { t: "ФОТ за год, ₽" }, { t: "Выплаты по плану за год, ₽" },
     { t: "Нераспределённый ФОТ на 31.12, ₽" }, { t: "Освоение ФОТ за год, %" }, "Состояние"],
    r["договоры"].map(function (c) {
      var level = (c["статусы"] || [c["статус"]]).map(stLevel)
        .sort(function (a, b) { return LEVEL_ORDER[a] - LEVEL_ORDER[b]; })[0] || "ok";
      return { reviewKey: c["код"], cells: [c["код"], nc(c["месяцев с выплатами"]),
              nc(c["месяцев до срока оклада"]), nc(rm(c["ФОТ"])), nc(rm(c["выплачено"])),
              nc(rm(c["остаток"]), c["остаток"] > 0.5 ? "tight" : ""), nc(rpn(c["освоение"])),
              { v: esc(c["статус"]), cls: level }] };
    }), null, { fix: 1 }) + "</div>";

  // 3: выплаты по плану против равномерного графика освоения — ФОТ ровно по
  // месяцам окна выплат договора, как план освоения решателя, — и
  // отклонение. Остатка здесь нет: позиция нарастающим итогом — раздел 4,
  // строки двух разделов не повторяются (UI-002). Отклонение от графика —
  // не нарушение, равномерность лишь цель расчёта: знак без цвета.
  var u = r["освоение"];
  var mets = [["выплаты", "Выплаты по плану"], ["график", "Равномерный график освоения"],
              ["отклонение", "Отклонение от равномерного графика"]];
  // В месяце факта отклонения нет: его выплаты — история, график с него не
  // начинается (правило 26). Клетка говорит «Факт», без цвета.
  var F3 = repFactTo();
  var cell3 = function (key, v, code, mn) {
    if (key !== "отклонение") return mc(v, code, mn, rmi);
    var open = code == null ? null : repOpenMonths(code);
    if (open && open[mn - 1] === false && v == null) return gc();
    if (mn <= F3) return { v: "Факт", cls: "n pos" };
    if (v != null) return { v: rs(v), cls: "n" };
    return { v: "", cls: "n" };
  };
  var urows = [];
  u["договоры"].forEach(function (c) {
    mets.forEach(function (mt) {
      urows.push([c["код"], c["признак"] || "", c["счет"] || "", mt[1]]
        .concat(repMon.map(function (mn) { return cell3(mt[0], c[mt[0]][mn - 1], c["код"], mn); })));
    });
  });
  mets.forEach(function (mt) {
    urows.push({ cls: "sum all", cells: [{ v: "Итого по организации", cs: 3 }, { skip: true }, { skip: true }, mt[1]]
      .concat(repMon.map(function (mn) { return cell3(mt[0], (u["итого"][mt[0]] || [])[mn - 1], null, mn); })) });
  });
  h += secH(3, "Освоение ФОТ по договорам помесячно в сравнении с равномерным графиком, ₽") +
       repT(["Договор", "Признак", "Лицевой счёт", "Показатель"].concat(monHead()), urows, null,
            { fix: 4, scrollSpace: 240, tall: true, groups: monGroupRow(4, 1, 0, "quarter"), merge: [0, 1, 2] }) +
       "</div>";
  return h;
}

/* 4: позиция договора на конец каждого месяца без поступлений — они в
   расчёт не подаются, касса договора отдельной величиной не показывается
   (SOURCE-009). Весь ФОТ считается поступившим в первый месяц выплат
   (правило 21): остаток ФОТ на конец месяца — ФОТ за вычетом выплат
   нарастающим итогом; минус — нарушение. Прежняя «касса» показывала
   остаток на начало месяца, сдвинутый на месяц. */
function repCash() {
  var n4 = vrep["нарастающим"] || { "договоры": [], "итого": {} };
  var names = [["выплаты", "Выплаты нарастающим итогом, ₽"], ["остаток", "Остаток ФОТ на конец месяца, ₽"],
               ["освоение", "Освоение ФОТ нарастающим итогом, %"]];
  // Красное — минус остатка в месяце плана сверх уровня, с которым его
  // оставил факт (report.py, «сверх ФОТ»): минус в месяцах факта — история.
  var cell = function (key, v, code, mn, over) {
    if (v == null) return code == null ? { v: "", cls: "n" } : mc(null, code, mn, rmi);
    if (key === "освоение") return { v: rpn(v), cls: "n" };
    return { v: rmi(v), cls: "n" + (key === "остаток" && over ? " err" : "") };
  };
  var rows = [];
  n4["договоры"].forEach(function (c) {
    names.forEach(function (n) {
      rows.push({ reviewKey: "cum:" + c["код"] + ":" + n[0], cells: [c["код"], n[1]].concat(
        repMon.map(function (mn) {
          var over = c["сверх ФОТ"] ? c["сверх ФОТ"][mn - 1] : (c[n[0]] || [])[mn - 1] < -0.5;
          return cell(n[0], (c[n[0]] || [])[mn - 1], c["код"], mn, over);
        })) });
    });
  });
  names.forEach(function (n) {
    rows.push({ cls: "sum all", cells: ["Итого по организации", n[1]].concat(
      repMon.map(function (mn) { return cell(n[0], (n4["итого"][n[0]] || [])[mn - 1], null, mn); })) });
  });
  return secH(4, "Освоение ФОТ по договорам нарастающим итогом") +
    repT(["Договор", "Показатель"].concat(monHead()), rows, null,
         { fix: 2, tall: true, groups: monGroupRow(2, 1, 0, "quarter"), merge: [0] }) + "</div>";
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
      var p = q.p, total = ((p["оклад"] || 0) + (p["122"] || 0) + (p["прочие"] || 0)) * q.k;
      out.push({ s: s, p: p, q: q, total: total,
        key: repGroup === "e" ? s["табельный"] + "\u0001" + s["договор"] : s["договор"] + "\u0001" + s["табельный"] });
    });
  });
  out.sort(function (a, b) { return a.key < b.key ? -1 : (a.key > b.key ? 1 : 0); });
  return out;
}

/* Регистр как в учетной форме: строка-группа — сотрудник (или договор) с
   итогом, под ней строки по договорам (или людям) и периодам.

   Предел П2556 ограничивает только оклад с надбавкой 122 (правило 4),
   поэтому 122 — своей графой, а запас до предела — предел на открытую
   ставку минус оклад и 122. Прежде «запас до лимита» вычитал из предела все
   надбавки и приказ, а «фонд заработной платы» был одним окладом:
   сравнивались несоизмеримые величины (владелец продукта 30.09.2026: «в 6.2
   какая-то фигня, несоизмеримые величины сравниваются вроде, что-то с
   лимитом на ставку»). Красное — только превышение больше рубля: равенство
   пределу — цель расчёта. */
function repRegisterGrid() {
  var regs = repRegisterRows(), grand = 0, rows = [], byE = repGroup === "e";
  var head = byE
    ? ["Договор", "Лицевой счёт оклада"]
    : ["Табельный номер", "ФИО", "Должность", "Лицевой счёт оклада"];
  head = head.concat(["Занятость", { t: "Открытая ставка" }, "Период", { t: "Месяцев" },
    { t: "Оклад в месяц, ₽" }, { t: "Надбавка 122 в месяц, ₽" },
    { t: "Прочие надбавки и выплата приказом в месяц, ₽" }, "Лицевой счёт надбавки",
    "Виды выплат в периоде", { t: "Выплаты за период, ₽" },
    { t: "Предел П2556 на открытую ставку в месяц, ₽" },
    { t: "Запас до предела П2556 по окладу и 122 в месяц, ₽" }]);
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
    var reserve = p["запас"], fact = !!p["факт"];
    // Период факта (правило 26) назван словом, связь — запятой (UI-015):
    // «янв–фев, факт». Его запас показан, но не красится.
    cells = cells.concat([p["занятость"] || "", nc(p["ставка"] ? rf(p["ставка"]) : ""),
      (q.from === q.to ? MON[q.from - 1] : MON[q.from - 1] + "–" + MON[q.to - 1]) + (fact ? ", факт" : ""), nc(q.k),
      nc(rm(p["оклад"])), nc(p["122"] ? rm(p["122"]) : ""), nc(p["прочие"] ? rm(p["прочие"]) : ""),
      p["лицевой счет надбавки"] || "", p["виды"] || "", nc(rm(x.total)),
      nc(p["лимит"] == null ? "" : rm(p["лимит"])),
      { v: reserve == null ? "" : rm(reserve), cls: "n" + (!fact && reserve != null && reserve < -1 ? " err" : "") }]);
    rows.push({ g: k, cells: cells });
  });
  flush();
  rows.push({ cls: "sum", cells: [{ v: esc("Итого" + (repPeriod === "year" ? " за год" : (repMon.length === 1
              ? " за " + MON[repMon[0] - 1] : " за выбранные месяцы"))), cs: ncol - 3 }, nc(rm(grand)), { v: "", cs: 2 }] });
  return grid(head, rows, { fix: idn, tall: true });
}

function repPlan() {
  var year = vrep["год"] || "";
  // 5: вид выплаты — код своей графой, наименование — своей (UI-015):
  // прежнее «120 — гостайна» склеивало их тире. «Доля фонда» была долей
  // всех выплат плана, а не ФОТ, — так её и называем.
  var h = secH(5, "Выплаты по договорам и видам выплат за " + year + " год") + repT(
    ["Договор", "Вид выплаты", "Наименование вида выплаты", { t: "Выплаты за год, ₽" },
     { t: "Доля в выплатах договора, %" }, { t: "Доля в выплатах по плану, %" },
     { t: "Месяцев с выплатами" }],
    vrep["виды"].map(function (k) {
      return [k["договор"], k["вид"], k["наименование"] || "", nc(rm(k["сумма"])),
              nc(rpn(k["доля договора"])), nc(rpn(k["доля плана"])), nc(k["месяцев"])];
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
      var tot = [], total = 0;
      byC[code].forEach(function (x) {
        mrows.push({ cls: x.l["вид"] === "оклад" ? "okl" : "",
          reviewKey: "pay:" + x.p["табельный"] + ":" + x.l["вид"],
          cells: [code, x.p["табельный"], x.p["фио"], x.l["вид"]].concat(
            mcells(x.l["месяцы"], code), [nc(rmi(x.l["год"]))]) });
        x.l["месяцы"].forEach(function (v, i) { if (v) tot[i] = (tot[i] || 0) + v; });
        total += x.l["год"] || 0;
      });
      mrows.push({ cls: "sum", cells: [code, { v: "Итого по договору", cs: 3 }, { skip: true }, { skip: true }]
        .concat(mcells(tot, code), [nc(rmi(total))]) });
    });
  }
  if (mrows.length) {
    mrows.push({ cls: "sum all", cells: [{ v: "Итого по организации", cs: 4 }, { skip: true }, { skip: true }, { skip: true }].concat(
      repMon.map(function (mn) { return mc(pm["итого"][mn - 1], null, mn, rmi); }),
      [nc(rmi(pm["год"]))]) });
  }
  h += secH(6, "Выплаты сотрудникам") + ctlG +
    subH("6.1", "Выплаты по сотрудникам, договорам и видам выплат помесячно, ₽") + (mrows.length ? repT(
      (byE ? ["Табельный номер", "ФИО", "Договор", "Вид выплаты"] : ["Договор", "Табельный номер", "ФИО", "Вид выплаты"])
        .concat(monHead(), [{ t: "За год, ₽" }]),
      mrows, null, { fix: 4, tall: true, groups: monGroupRow(4, 1, 1, "quarter"), merge: [0, 1, 2] })
      : '<div class="none">Выплат нет</div>') +
    subH("6.2", "Регистр назначений по периодам действия условий оплаты") + ctl + repRegisterGrid() +
    subH("6.3", "ШР на дату, детализация назначений") + repStaffDetail() + "</div>";

  // Ставка месяца факта (бывает и 0,3) — история: отметка «равна пределу» —
  // только у месяцев плана (правило 26).
  var F7 = repFactTo();
  var planMax = function (arr, keep) {
    var vals = arr.filter(function (v, mi) { return mi + 1 > F7 && v != null && (!keep || keep(mi)); });
    return vals.length ? Math.max.apply(null, vals) : null;
  };
  var srows = [];
  vrep["ставки"].forEach(function (p) {
    p["договоры"].forEach(function (c) {
      [["Основное место работы", true], ["Совместительство", false]].forEach(function (kind) {
        var vals = repMon.map(function (mn) {
          var mi = mn - 1, v = c["месяцы"][mi];
          return v && !!c["основное"][mi] === kind[1] ? v : null;
        });
        var all = c["месяцы"].filter(function (v, mi) { return v && !!c["основное"][mi] === kind[1]; });
        if (!all.length) return;
        var mx = Math.max.apply(null, all);
        var mxPlan = planMax(c["месяцы"], function (mi) { return c["месяцы"][mi] && !!c["основное"][mi] === kind[1]; });
        srows.push({ reviewKey: "rate:" + p["табельный"], cells: [p["табельный"], p["фио"], c["код"], kind[0]].concat(
          vals.map(function (v, i) {
            return mc(v, c["код"], repMon[i], rf,
                      (!kind[1] && repMon[i] > F7 && v >= 0.5 - 0.001) ? "tight" : "");
          }), [nc(rf(mx), (!kind[1] && mxPlan != null && mxPlan >= 0.5 - 0.001) ? "tight" : "")]) });
      });
    });
    var totalPlan = planMax(p["всего"]);
    srows.push({ cls: "sum", reviewKey: "rate:" + p["табельный"], cells: [p["табельный"], p["фио"], "Суммарная открытая ставка", gc()].concat(
      repMon.map(function (mn) {
        var v = p["всего"][mn - 1];
        return v == null ? gc() : nc(rf(v), mn > F7 && v >= p["предел"] - 0.001 ? "tight" : "");
      }), [nc(rf(p["макс"]), totalPlan != null && totalPlan >= p["предел"] - 0.001 ? "tight" : "")]) });
  });
  // 7.1 — сведения о строке сотрудника из неё самой: у двух строк одного
  // табельного номера должности разные, и поиск по номеру в регистре
  // подставлял второй строке должность первой. Предел — как у решателя
  // (report.py, row_max_total_quarters): студенту и лаборанту — штатная ставка.
  var prows = vrep["ставки"].map(function (p) {
    return [p["табельный"], p["фио"], p["должность"] || "", p["отдел"] || "",
            p["категория персонала"] || "", p["тип занятости"] || "", p["категория занятости"] || "",
            nc(rf(p["штатная"])), nc(rf(p["предел"]))];
  });
  h += secH(7, "Сотрудники и ставки") +
    subH("7.1", "Сведения о сотрудниках") + repT(
    ["Табельный номер", "ФИО", "Должность", "Подразделение", "Категория персонала", "Тип занятости",
     "Категория занятости", { t: "Штатная ставка" }, { t: "Предел суммарной открытой ставки сотрудника" }],
    prows, null, { fix: 1 }) +
    subH("7.2", "Открытые ставки по договорам помесячно") + repT(
    ["Табельный номер", "ФИО", "Договор", "Занятость"].concat(monHead(), [{ t: "Наибольшая ставка за год" }]),
    srows, null,
    { fix: 3, tall: true, groups: monGroupRow(4, 1, 1, "quarter"), merge: [0, 1] }) +
    '<div class="lg"><i class="tight"></i>Ставка равна пределу</div></div>';
  return h;
}

/* 6.3 — форма отдела кадров «ШР на дату, детализация»: строка на каждое
   назначение (человек, договор, параметр, период). Графы как в шаблоне
   кадров; номер и дата приказа сервису неизвестны, графы пустые. */
/* Графы формы 6.3 «ШР на дату, детализация назначений» — шаблон кадров
   пользователя («ШР на 01.06.2026_детализация_ШАБЛОН»), как facts.SHR_HEAD:
   один перечень на раздел 6.3 отчёта и на вкладки «Факт начислений». */
var SHR_HEAD = ["Таб.№", "Назначение", "Фамилия И.О., уч. ст., уч. зван.", "Код подр.",
  "Подразделение", "Должность", "Категория персонала", "Код пар-ра", "Название параметра",
  "Ставка", "Номинальное значение параметра", "Значение параметра по ставке", "Сумма в руб.",
  "Начало действия", "Окончание действия", "Код шифра затрат", "Шифр затрат", "Лицевой счет",
  "УИ ПНИЭР", "Номер приказа ввода", "Дата приказа ввода", "Номер приказа закрытия",
  "Дата приказа закрытия"];

function repStaffDetail() {
  var rows = vrep["шр"] || [];
  if (!rows.length) return '<div class="none">назначений нет</div>';
  var head = SHR_HEAD.map(function (t, i) { return i >= 9 && i <= 12 ? { t: t } : t; });
  var body = rows.map(function (r) {
    return [r["таб"], r["назначение"] || gc("—"), r["фио"], r["код_подр"] || gc("—"), r["подразделение"] || gc("—"),
      r["должность"], r["категория"] || gc("—"), r["код"], r["параметр"], nc(rf(r["ставка"])),
      nc(rm(r["номинал"])), nc(rm(r["по_ставке"])), nc(rm(r["сумма"])), r["начало"], r["окончание"],
      r["код_шифра"], r["шифр"] || gc("—"), r["счет"] || gc("—"), gc("—"), gc("—"), gc("—"), gc("—"), gc("—")];
  });
  return repT(head, body, null, { fix: 3, tall: true, merge: [0, 2] });
}

function repLimits() {
  var year = vrep["год"] || "";
  // Отклонение окрашено только там, где оно нарушение или замечание:
  // значение ниже предела — норма, а не «хорошо», и зелёным не выделяется.
  var sgn = function (v, tone) { return { v: v == null ? "" : rs(v), cls: "n" + (tone ? " " + tone : "") }; };
  var spc = function (v, tone) { return { v: v == null ? "" : rs(v * 100, true), cls: "n" + (tone ? " " + tone : "") }; };
  var reserve = function (v) { return { v: v == null ? "" : rm(v), cls: "n" + (v != null && v < -1 ? " err" : "") }; };
  // Доля запаса — со знаком минус у превышения, как рубли рядом (rpn
  // печатал дефис вместо минуса).
  var reservePct = function (v, rub) {
    return { v: v == null ? "" : (v < -0.00005 ? "−" : "") + rf(Math.abs(v * 100)),
             cls: "n" + (rub != null && rub < -1 ? " err" : "") };
  };

  // 8: оклад с 122 на открытые ставки ГОЗ-договора против БЭП — суммы и
  // средние на 1,0 ставки. Равно БЭП — цель расчёта, ниже — допустимо, но
  // нежелательно, выше — нарушение (владелец продукта 30.09.2026: «не
  // критично если меньше получается, но лучше ровно… делай ровно среднюю»).
  var bep = vrep["бэп"] || [];
  var bepState = function (b) {
    if (b["состояние"]) return b["состояние"];
    var d = b["отклонение"] || 0;
    return d > 0.0005 ? "Выше БЭП" : (d < -0.0005 ? "Ниже БЭП" : "Равно БЭП");
  };
  // «Факт» — месяц начислений до пересчёта (правило 26): показан, но не
  // судится — без цвета и вне отметок раздела.
  var BEP_TONE = { "Равно БЭП": "ok", "Ниже БЭП": "wr", "Выше БЭП": "er", "Факт": "" };
  var nAbove = 0, nBelow = 0, nJudged = 0;
  bep.forEach(function (b) {
    var st = bepState(b);
    if (st === "Факт") return;
    nJudged++;
    if (st === "Выше БЭП") nAbove++;
    else if (st === "Ниже БЭП") nBelow++;
  });
  var bepChips = !nJudged ? "" : (nAbove ? chip("Выше БЭП: " + nAbove, "er") : "") +
    (nBelow ? chip("Ниже БЭП: " + nBelow, "wr") : "") + (nAbove || nBelow ? "" : chip("Равно БЭП", "ok"));
  var h = secH(8, "Средняя оклада и надбавки 122 на 1,0 ставки по ГОЗ-договорам в сравнении с БЭП, ₽ в месяц", bepChips) +
    (bep.length ? repT(
    ["Договор", "Месяц", { t: "Оклад и надбавка 122 по договору в месяц, ₽" }, { t: "Сумма открытых ставок" },
     { t: "Средняя на 1,0 ставки в месяц, ₽" }, { t: "БЭП на 1,0 ставки в месяц, ₽" },
     { t: "Отклонение средней от БЭП, ₽" }, { t: "Отклонение средней от БЭП, %" },
     { t: "Недобор до БЭП по договору в месяц, ₽" }, "Состояние"],
    bep.map(function (b, i) {
      var st = bepState(b), tone = BEP_TONE[st] || "";
      var mark = tone === "ok" ? "" : tone;
      return { reviewKey: "bep:" + i, cells: [b["договор"], MON[b["месяц"] - 1], nc(rm(b["сумма"])),
        nc(rf(b["ставок"])), nc(rm(b["средняя"])), nc(rm(b["БЭП"])), sgn(b["отклонение"], mark),
        spc(b["отклонение %"], mark), nc(b["недобор"] ? rm(b["недобор"]) : ""), { v: esc(st), cls: tone }] };
    }), null, { fix: 1, tall: true, merge: [0] }) : '<div class="none">ГОЗ-договоров в плане нет</div>') + "</div>";

  // 9: приказы о предельных уровнях — три таблицы (пользователь 30.09.2026:
  // «в результатах там должна быть таблица по 2556»). 9.1 — оклад и 122 не
  // выше П2556 на открытую ставку договора (правило 4), те же строки, что у
  // проверки «П2556»; 9.2 — штатная часть в месяцы 124 равна уровню П4
  // (правило 5, норма, а не потолок); 9.3 — выплата приказом не выше П2556
  // на штатную ставку (правило 4, «тем же пределом отдельно»).
  var p25 = vrep["п2556"] || [], p4 = vrep["п4"] || [], ord = vrep["п2556 приказ"] || [];
  // Состояние строки 9.1 считает сервер; у отчёта без него — по запасу.
  // «Факт» (месяц до пересчёта) не судится: ни цвета, ни отметки.
  var p25State = function (b) {
    return b["состояние"] || (b["запас"] != null && b["запас"] < -1 ? "Выше предела" : "Не выше предела");
  };
  var p25Bad = p25.filter(function (b) { return p25State(b) === "Выше предела"; }).length;
  var p4State = function (b) {
    if (b["состояние"]) return b["состояние"];
    var d = (b["итого"] || 0) - (b["уровень"] != null ? b["уровень"] : (b["предел"] || 0));
    return d > 1 ? "Выше уровня П4" : (d < -1 ? "Ниже уровня П4" : "Равно уровню П4");
  };
  var p4Bad = p4.filter(function (b) {
    var st = p4State(b);
    return st !== "Равно уровню П4" && st !== "Факт";
  }).length;
  var ordBad = ord.filter(function (b) { return b["состояние"] === "Выше предела"; }).length;
  h += secH(9, "Соблюдение уровней оплаты труда по приказам № 2556 и № 4", "",
            p25Bad || p4Bad || ordBad ? "er" : "") +
    subH("9.1", "Приказ № 2556: оклад и надбавка 122 в сравнении с предельным уровнем", p25.length
         ? (p25Bad ? chip("Выше предела П2556: " + p25Bad, "er") : chip("Превышений предела П2556 нет", "ok")) : "") +
    (p25.length ? repT(
    ["Табельный номер", "ФИО", "Должность", "Договор", "Месяц", { t: "Оклад в месяц, ₽" },
     { t: "Надбавка 122 в месяц, ₽" }, { t: "Оклад и надбавка 122 в месяц, ₽" }, { t: "Открытая ставка" },
     { t: "П2556 на 1,0 ставки, ₽" }, { t: "Предел П2556 на открытую ставку в месяц, ₽" },
     { t: "Запас до предела П2556 в месяц, ₽" }, { t: "Запас до предела П2556, %" }, "Состояние"],
    p25.map(function (b, i) {
      var st = p25State(b), bad = st === "Выше предела";
      return { reviewKey: "p2556:" + i, cells: [b["табельный"], b["фио"], b["должность"] || "", b["договор"],
              MON[b["месяц"] - 1], nc(rm(b["оклад"])), nc(rm(b["122"])), nc(rm(b["итого"])), nc(rf(b["ставка"])),
              nc(rm(b["П2556"])), nc(rm(b["предел"])),
              bad ? reserve(b["запас"]) : { v: b["запас"] == null ? "" : rm(b["запас"]), cls: "n" },
              bad ? reservePct(b["запас %"], b["запас"]) : reservePct(b["запас %"], null),
              { v: esc(st), cls: bad ? "er" : "" }] };
    }), null, { fix: 2, tall: true, merge: [0, 1, 2] })
    : '<div class="none">В справочнике нет П2556 по должностям плана</div>');

  h += subH("9.2", "Приказ № 4: штатная часть в месяцы надбавки 124 в сравнении с уровнем П4", p4.length
            ? (p4Bad ? chip("Отклонений от уровня П4: " + p4Bad, "er") : chip("Равно уровню П4", "ok")) : "") +
    (p4.length ? repT(
    ["Табельный номер", "ФИО", "Должность", "Месяц", { t: "Оклад в месяц, ₽" }, { t: "Надбавка 122 в месяц, ₽" },
     { t: "Надбавка 124 в месяц, ₽" }, { t: "Штатная часть в месяц, ₽" }, { t: "Суммарная открытая ставка" },
     { t: "П4 на 1,0 ставки, ₽" }, { t: "Уровень П4 на суммарную ставку в месяц, ₽" },
     { t: "Отклонение от уровня П4, ₽" }, { t: "Отклонение от уровня П4, %" }, "Состояние"],
    p4.map(function (b, i) {
      var st = p4State(b), bad = st !== "Равно уровню П4" && st !== "Факт";
      return { reviewKey: "p4:" + i, cells: [b["табельный"], b["фио"], b["должность"] || "", MON[b["месяц"] - 1],
              nc(rm(b["оклад"])), nc(rm(b["122"])), nc(rm(b["124"])), nc(rm(b["итого"])), nc(rf(b["ставка"])),
              nc(rm(b["П4"])), nc(rm(b["уровень"])), sgn(b["отклонение"], bad ? "er" : ""),
              spc(b["отклонение %"], bad ? "er" : ""), { v: esc(st), cls: bad ? "er" : (st === "Факт" ? "" : "ok") }] };
    }), null, { fix: 2, tall: true, merge: [0, 1, 2] })
    : '<div class="none">Надбавка 124 в этом плане не назначалась</div>');

  h += subH("9.3", "Приказ № 2556: стимулирующая выплата приказом", ord.length
            ? (ordBad ? chip("Выше предела П2556: " + ordBad, "er") : chip("Превышений предела П2556 нет", "ok")) : "") +
    (ord.length ? repT(
    ["Табельный номер", "ФИО", "Должность", "Месяц", "Договоры выплаты приказом",
     { t: "Выплата приказом в месяц, ₽" }, { t: "Штатная ставка" }, { t: "П2556 на 1,0 ставки, ₽" },
     { t: "Предел П2556 на штатную ставку в месяц, ₽" }, { t: "Запас до предела П2556 в месяц, ₽" }, "Состояние"],
    ord.map(function (b, i) {
      var bad = b["состояние"] === "Выше предела";
      return { reviewKey: "p2556o:" + i, cells: [b["табельный"], b["фио"], b["должность"] || "", MON[b["месяц"] - 1],
              b["договоры"] || "", nc(rm(b["приказ"])), nc(rf(b["ставка"])), nc(rm(b["П2556"])),
              nc(rm(b["предел"])), reserve(b["запас"]), { v: esc(b["состояние"] || ""), cls: bad ? "er" : "" }] };
    }), null, { fix: 2, tall: true, merge: [0, 1, 2] })
    : '<div class="none">Выплат приказом в плане нет</div>') + "</div>";

  // 10: трудоёмкость строки РКМ за год — чел.-мес., стоимость и средняя
  // стоимость 1 чел.-мес. по РКМ против закрытого исполнителями. Допуск в
  // отметке раздела — тот же, по которому считается состояние строки
  // (rules.labor_tolerance): прежде отметка говорила 5 %, а состояние
  // прощало ещё 0,5 чел.-мес.
  var lb = vrep["трудоемкость"] || [];
  var lbTol = vrep["допуск"] != null ? vrep["допуск"] : (lb.length ? lb[0]["допуск"] : null);
  var outOf = function (l) {
    var f = l["вне допуска"] || {};
    return !!(f["чел-мес"] || f["сумма"] || f["средняя"] || f["этап"]);
  };
  var lbBad = lb.filter(outOf).length;
  h += secH(10, "Трудоёмкость по строкам РКМ за " + year + " год", lb.length
            ? (lbBad ? chip("Строк РКМ вне допуска: " + lbBad, "wr") : chip("В пределах допуска", "ok")) +
              (lbTol != null ? chip("Допуск " + rp(lbTol)) : "") : "") + (lb.length ? repT(
    ["Договор", "Строка РКМ", { t: "Трудоёмкость по РКМ за год, чел.-мес." },
     { t: "Закрыто исполнителями за год, чел.-мес." }, { t: "Отклонение трудоёмкости, чел.-мес." },
     { t: "Отклонение трудоёмкости, %" }, { t: "Стоимость трудоёмкости по РКМ за год, ₽" },
     { t: "Выплаты исполнителям за год, ₽" }, { t: "Отклонение стоимости, ₽" }, { t: "Отклонение стоимости, %" },
     { t: "Средняя стоимость 1 чел.-мес. по РКМ, ₽" }, { t: "Средняя стоимость 1 чел.-мес. по плану, ₽" },
     { t: "Отклонение средней стоимости, ₽" }, { t: "Отклонение средней стоимости, %" },
     { t: "Число исполнителей по РКМ, чел." }, { t: "Число исполнителей в месяц, наибольшее, чел." }, "Состояние"],
    lb.map(function (l, i) {
      var out = l["вне допуска"] || {};
      // Доля рядом с величиной: допуск задан в процентах, и по абсолютному
      // числу не видно, близко ли расхождение к нему. Долю считает сервер;
      // у отчёта без неё — здесь.
      var share = function (p, d, plan) { return p != null ? p : (d == null || !plan ? null : d / plan); };
      var dv = function (txt, over) { return { v: txt, cls: "n" + (over ? " er" : "") }; };
      var pc = function (v, over) { return { v: v == null ? "" : rs(v * 100, true), cls: "n" + (over ? " er" : "") }; };
      return { reviewKey: "labor:" + i, cells: [l["договор"], l["строка"], nc(rf(l["план чел-мес"])), nc(rf(l["факт чел-мес"])),
              dv(rs(l["д чел-мес"], true), out["чел-мес"]),
              pc(share(l["д чел-мес %"], l["д чел-мес"], l["план чел-мес"]), out["чел-мес"]),
              nc(rm(l["план сумма"])), nc(rm(l["факт сумма"])), dv(rs(l["д сумма"]), out["сумма"]),
              pc(share(l["д сумма %"], l["д сумма"], l["план сумма"]), out["сумма"]),
              nc(rm(l["средняя план"])), nc(rm(l["средняя факт"])), dv(rs(l["д средней"]), out["средняя"]),
              pc(share(l["д средней %"], l["д средней"], l["средняя план"]), out["средняя"]),
              nc(l["людей предел"] == null ? "" : l["людей предел"]),
              nc(l["людей макс"], l["людей предел"] != null &&
                 (l["людей макс план"] != null ? l["людей макс план"] : l["людей макс"]) > l["людей предел"] ? "err" : ""),
              { v: esc(l["статус"]), cls: outOf(l) ? "wr" : (l["статус"] === "Отклонение сложилось в месяцах факта" ? "" : "ok") }] };
    }), null, { fix: 2, merge: [0] }) : '<div class="none">Трудоёмкость в РКМ не задана</div>') + "</div>";

  // 11: кто и какой своей должностью закрывает строку РКМ — по месяцам, в
  // одной величине, чел.-мес. (открытая ставка месяца и есть чел.-мес.).
  // Дерево: договор → строка РКМ → под ней план, закрытое и отклонение,
  // затем исполнители по ФИО. Владелец продукта 30.09.2026: «в 11 таблице
  // вид неудобен вроде» — прежде план висел числом в строке РКМ, а итог
  // «Закрыто по строке» стоял внизу, после людей, и сравнивать его с планом
  // приходилось через всю группу.
  var wrows = [], wtail = repMon.length + 1, prev = null;
  var ctrTitle = function (code) {
    var c = (vrep["договоры"] || []).filter(function (x) { return x["код"] === code; })[0];
    return "<b>" + esc(code) + "</b>" + (c && c["название"] ? " " + esc(c["название"]) : "");
  };
  var pmCell = function (v, code, mn) { return code == null ? { v: v ? rf(v) : "", cls: "n" } : mc(v, code, mn, rf); };
  var F11 = repFactTo();
  (vrep["кто"] || []).forEach(function (w) {
    var code = w["договор"];
    if (code !== prev) {
      prev = code;
      wrows.push({ grp: true, cls: "lvl1", k: code, cells: [
        { v: ctrTitle(code), cs: 2 }, { skip: true }, { v: "", cs: wtail }] });
    }
    var wkey = code + "\u0001" + w["строка"];
    // Якорь строки РКМ: по нему замечание «работу закрывает подходящая
    // должность» открывается на нужной строке, а не на разделе целиком.
    var wnum = (vrep["трудоемкость"] || []).findIndex(function (x) {
      return x["договор"] === code && x["строка"] === w["строка"]; });
    wrows.push({ grp: true, g: code, k: wkey, reviewKey: wnum >= 0 ? "who:" + wnum : undefined, cells: [
      { v: esc(w["строка"]), cs: 2, cls: "lv2" }, { skip: true }, { v: "", cs: wtail }] });
    var plan = w["план по месяцам"];
    var planM = w["план"] != null ? w["план"] : null;
    wrows.push({ cls: "sum", g: wkey, cells: [{ v: "План по РКМ", cs: 2, cls: "lv3" }, { skip: true }].concat(
      repMon.map(function (mn) { return plan ? pmCell(plan[mn - 1], code, mn) : { v: "", cls: "n" }; }),
      [pmCell(planM)]) });
    wrows.push({ cls: "sum", g: wkey, cells: [{ v: "Закрыто исполнителями", cs: 2, cls: "lv3" }, { skip: true }].concat(
      repMon.map(function (mn) { return pmCell((w["закрыто"] || [])[mn - 1], code, mn); }),
      [pmCell(w["закрыто год"])]) });
    // Отклонение от плана — только у строки с планом по месяцам; за год —
    // у любой. Красное за год — строка вне допуска по трудоёмкости или
    // этапу; в месяце — закрыто там, где плана нет.
    wrows.push({ cls: "sum", g: wkey, cells: [{ v: "Отклонение от плана", cs: 2, cls: "lv3" }, { skip: true }].concat(
      repMon.map(function (mn) {
        var i = mn - 1, v = (w["отклонение"] || [])[i], outside = (w["вне этапа"] || [])[i];
        // Месяц факта план месяца не проверяет (правило 26): «Факт», без цвета.
        if (mn <= F11 && plan) return { v: "Факт", cls: "n pos" };
        var txt = v == null ? "" : rs(v, true);
        if (outside) return { v: '<span title="Закрыто вне этапа РКМ">' + txt + "</span>", cls: "n err" };
        return { v: txt, cls: "n" };
      }), [{ v: w["отклонение год"] == null ? "" : rs(w["отклонение год"], true),
             cls: "n" + (w["вне допуска"] ? " err" : "") }]) });
    (w["люди"] || []).forEach(function (p) {
      wrows.push({ g: wkey, cells: [{ v: esc(p["фио"]), cls: "lv3" }, { v: esc(p["должность"] || ""), cls: "pos" }].concat(
        repMon.map(function (mn) { return pmCell(p["ставка"][mn - 1], code, mn); }),
        [pmCell(p["ставка год"])]) });
    });
  });
  h += secH(11, "Распределение трудоёмкости строк РКМ по исполнителям помесячно, чел.-мес.") +
    (wrows.length ? repT(
    ["Исполнитель", "Должность исполнителя"].concat(monHead(), [{ t: "За год, чел.-мес." }]),
    wrows, null,
    { fix: 2, tall: true, groups: monGroupRow(2, 1, 1, "quarter", "Чел.-мес.") })
    : '<div class="none">Трудоёмкость в РКМ не задана</div>') + "</div>";

  // 12: строки РКМ, закрытые меньше плана сверх допуска — тем же порогом,
  // что состояние в разделе 10. Месяцы выполнения работ — месяцы этапа РКМ,
  // у годовой строки — месяцы, где у договора возможна открытая ставка.
  var gaps = vrep["незакрыто"] || [], hasLabor = lb.length > 0;
  if (gaps.length) {
    var gs = 0;
    gaps.forEach(function (x) { gs += x["не закрыто"] || 0; });
    var grows = gaps.map(function (x) {
      return [x["договор"], x["строка"] || x["должность"] || "", nc(rf(x["по РКМ"] != null ? x["по РКМ"] : x["нужно"])),
              nc(rf(x["закрыто"])), nc(rf(x["не закрыто"]), "err"), nc(x["месяцев"]), nc(rf(x["ставок в месяц"]))];
    });
    grows.push({ cls: "sum all", cells: [{ v: "Итого не закрыто, чел.-мес.", cs: 4 }, { skip: true }, { skip: true },
                                         { skip: true }, nc(rf(gs), "err"), "", ""] });
    h += secH(12, "Незакрытая трудоёмкость по строкам РКМ, чел.-мес.",
              chip("Строк РКМ с незакрытой трудоёмкостью: " + gaps.length, "er")) + repT(
      ["Договор", "Строка РКМ", { t: "Трудоёмкость по РКМ, чел.-мес." }, { t: "Закрыто исполнителями, чел.-мес." },
       { t: "Не закрыто, чел.-мес." }, { t: "Месяцев выполнения работ" },
       { t: "Требуется дополнительно ставок в месяц" }], grows) + "</div>";
  } else {
    h += secH(12, "Незакрытая трудоёмкость по строкам РКМ, чел.-мес.",
              hasLabor ? chip("Незакрытой трудоёмкости нет", "ok") : "") +
      (hasLabor ? "" : '<div class="none">Трудоёмкость в РКМ не задана</div>') + "</div>";
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
  if (view === "ctr") {
    // Флажки, версии и условия — основание этого плана (SOURCE-003/006).
    var all = e.target.closest(".inclall");
    if (all) {
      var codes = Array.prototype.map.call($("view").querySelectorAll("[data-incl]"),
        function (b) { return b.getAttribute("data-incl"); });
      basisLocal(codes, all.checked, ".inclall");
      setBasis({ contracts: codes, included: all.checked }, ".inclall");
      return;
    }
    var one = e.target.closest("[data-incl]");
    if (one) {
      var code = one.getAttribute("data-incl");
      var osel = '[data-incl="' + code.replace(/"/g, '\\"') + '"]';
      basisLocal([code], one.checked, osel);
      setBasis({ contracts: [code], included: one.checked }, osel);
      return;
    }
    var use = e.target.closest("[data-use]");
    if (use) {
      setBasis({ document: +use.getAttribute("data-use"), version: +use.value });
      return;
    }
    var ov = e.target.closest("[data-ov]");
    if (ov && pctrOpen) {
      var name = ov.getAttribute("data-ov"), value;
      if (OV_KIND[name] === "date") value = isoToDmy(ov.value);
      else if (OV_KIND[name] === "kinds") {
        value = Array.prototype.filter.call(ov.querySelectorAll("input[type=checkbox]"),
          function (b) { return b.checked; }).map(function (b) { return b.value; }).join(", ");
        // Отмечены все — это и есть «не сказано» в договоре: пустая графа
        // разрешает все виды, а полный перечень записал бы запрет нового.
        if (value.split(", ").length === PAY_KINDS.length) value = "";
      } else if (OV_KIND[name] === "yesno") value = ov.checked ? "да" : "нет";
      else value = String(ov.value || "").trim();
      // Значение, равное договорному, — не правка: правку снимаем, а не
      // пишем её копией договора (значок сброса у поля тогда пропадает).
      if (ov.hasAttribute("data-was") && value === ov.getAttribute("data-was")) value = "";
      var fields = {};
      fields[name] = value;
      setBasis({ contract: pctrOpen, fields: fields });
    }
    return;
  }
  if (view === "emp") {
    // Строки штатного расписания в расчёте этого плана (30.09.2026).
    var eall = e.target.closest("[data-empall]"), eone = e.target.closest("[data-empincl]");
    if (!eall && !eone) return;
    var ecodes = eall
      ? Array.prototype.map.call($("view").querySelectorAll("[data-empincl]"),
          function (b) { return b.getAttribute("data-empincl"); })
      : [eone.getAttribute("data-empincl")];
    var on = (eall || eone).checked;
    var esel = eall ? "[data-empall]"
      : '[data-empincl="' + ecodes[0].replace(/"/g, '\\"') + '"]';
    basisLocal(ecodes, on, esel, "staff_excluded");
    setBasis({ employees: ecodes, included: on }, esel);
    return;
  }
  if (view !== "sum") return;
  if (e.target.matches('select[data-sel="sec"]')) {
    var nextSection=+e.target.value;
    if(issueReturn){issueReturn=null;repSec=nextSection;renderView();}
    showSection(nextSection);routeWrite("push");return;
  }
  // Выбор расчёта живёт в шапке плана (#prun), а не в #view: его ловит
  // свой обработчик ниже, у «адреса страницы».
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

/* Закрепление с клавиатуры: Enter и пробел на ячейке открывают её правку,
   как щелчок. Esc без открытой правки закрывает панель договора. */
$("view").addEventListener("keydown", function (e) {
  if (view !== "ctr") return;
  if (e.target.matches("[data-fixcell]") && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    fixCellEdit(e.target);
    return;
  }
  if (e.target.matches("[data-wgkind]") && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    wgKindEdit(e.target);
    return;
  }
  // Esc закрывает панель договора и возвращает фокус на его шифр.
  if (e.key === "Escape" && pctrOpen && !e.defaultPrevented) {
    e.preventDefault();
    ctrPanelClose();
  }
});

/* Панель закрывают ×, Esc и повторный щелчок по договору. Адрес — заменой,
   как и при открытии (navigate в обработчике щелчка ниже). */
function ctrPanelClose() {
  var was = pctrOpen;
  navigate({ pctr: null }, "replace");
  var key = was && $("view").querySelector('[data-pctr-open="' + fixAttr(was) + '"]');
  if (key) key.focus();
}

/* Уйти из раздела замечания: вернуть период, отборы и скрытые графы, какими
   они были у списка замечаний. Возвращает то, что было запомнено, или null. */
function issueLeave() {
  if (!issueReturn) return null;
  var saved = issueReturn;
  issueReturn = null;
  repMon = saved.mon; repColFilter = saved.filters; repHideCol = saved.hidden;
  return saved;
}
$("view").addEventListener("click", function (e) {
  if (view !== "sum" || !vrep) return;
  var open=e.target.closest('[data-issue-open]');
  if(open){openPlanIssue(open.dataset.issueOpen);return;}
  if(e.target.closest('[data-issue-back]') && issueReturn){
    var saved=issueLeave();repSec=1;renderView();showSection(1);
    $("view").scrollTop=saved.scroll;
    routeWrite("push");
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
    routeWrite("push");
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
  // Ctrl и Shift — открыть раздел в новой вкладке: это делает браузер по адресу.
  if (nav && !(e.ctrlKey || e.metaKey || e.shiftKey)) {
    // Раздел отчёта — страница: шаг истории, как вкладка (UI-021).
    e.preventDefault();
    showSection(+nav.dataset.sec);
    routeWrite("push");
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

  if (view === "fact") {
    el.innerHTML = factPlanView();
    return;
  }

  if (view === "emp" || view === "zp") {
    // Прокрутка переживает перерисовку: флажок внизу списка не отбрасывает к
    // началу.
    var sTop = el.scrollTop, sHs = el.querySelector(".hscroll"), sLeft = sHs ? sHs.scrollLeft : 0;
    el.innerHTML = staffPlanTable(view);
    el.scrollTop = sTop;
    sHs = el.querySelector(".hscroll");
    if (sHs && sLeft) sHs.scrollLeft = sLeft;
    var eh = el.querySelector("[data-empall]");
    if (eh) {
      var eb = el.querySelectorAll("[data-empincl]");
      var ek = Array.prototype.filter.call(eb, function (b) { return b.checked; }).length;
      eh.indeterminate = ek > 0 && ek < eb.length;
    }
    return;
  }

  if (view === "ord") {
    el.innerHTML = ordersView();
    return;
  }

  if (view === "ctr") {
    // Вкладка отвечает на один вопрос: какие договоры и на каких условиях
    // идут в этот расчёт. Реестр — библиотека, выбор — здесь (SOURCE-003).
    var c = (vdata && vdata.contracts) || [];
    var bs = vbasis || {}, off = {}, pk = bs.packages || {};
    (bs.excluded || []).forEach(function (k) { off[k] = true; });
    var pgc = paged("v-ctr", c);
    // Договора из адреса в плане нет (удалён из реестра) — панель закрыта, и
    // адрес его больше не называет. Пока строки не пришли, судить не о чем.
    if (pctrOpen && vdata && !c.some(function (x) { return x.code === pctrOpen; })) {
      pctrOpen = null;
      routeWrite("replace");
    }
    // Предполётная таблица: то, что ядро проверяет перед расчётом. Договор без
    // ФОТ года ядро выбрасывает молча — с флажком, но без строки во входе;
    // это единственная тревога на экране, и она должна быть первой.
    var role = function (code, key) {
      return (pk[code] || []).filter(function (r) { return r.key === key; })[0]
             || { state: "missing" };
    };
    var inAll = c.filter(function (x) { return !off[x.code]; });
    var dropped = inAll.filter(function (x) { return role(x.code, "fund").state !== "ok"; });
    var fundTitle = (c.length && role(c[0].code, "fund").title) || "ФОТ";
    // Строка — словами через запятую, как в UI-010; «·» не связывает части
    // (UI-015). «Готовы N» снято (пользователь 20.09.2026: «что значит готовы
    // 1, зачем это»): готовность считалась по четырём величинам, включая
    // нормативы, и числом ни о чём не говорила. Тревога на вкладке одна — нет
    // ФОТ года: такой договор ядро выбрасывает молча.
    var headline = '<div class="preflight">В расчёте ' + inAll.length + " из " + c.length +
      (dropped.length
        ? ', <span class="st bad">будут отброшены ' + dropped.length + ": нет " +
          esc(fundTitle) + "</span>" : "") + "</div>";
    // В ячейке — величина, и только она (пользователь 21.09.2026: «в ячейке
    // должна быть только величина»). Откуда она взята — документ или реестр
    // договоров — отвечает подсказка значения: метка в ячейке ломала столбик
    // разрядов и стояла лишь у части строк.
    // Срок не рвётся внутри диапазона дат: после тире перенос разрешён даже
    // перед неразрывным пробелом, его держит невидимая связка U+2060.
    var known = function (r) {
      return '<span title="' + esc(r.source ? "Источник: " + r.source : "") + '">' +
        esc(r.value || "есть").replace(/ – /g, "&nbsp;–&#8288;&nbsp;") + "</span>";
    };
    var allIn = pgc.rows.length > 0 && pgc.rows.every(function (x) { return !off[x.code]; });
    var inclBox = { v: '<input type="checkbox" class="inclall" ' +
                       'aria-label="Все договоры страницы в расчёте"' +
                       (allIn ? " checked" : "") + ">", cls: "incl" };
    // Графы выбираются кнопкой «Графы», как в таблицах реестра (решение
    // пользователя 20.09.2026). Сразу показываются шифр, наименование, ФОТ
    // года и срок; трудоёмкость, признак, номер, подразделение, число правок
    // и закреплений — по выбору. Граф «Нормативы» и «Условия плана» нет:
    // понятия нормативов у договора нет, а условия открывает щелчок по строке.
    var pcols = [
      { k: "code", label: "Шифр", always: true,
        cell: function (x) {
          return { v: '<button type="button" data-pctr-open="' + esc(x.code) +
            '" aria-controls="ctrpanel" aria-expanded="' + (pctrOpen === x.code) + '">' +
            esc(x.code) + "</button>" };
        } },
      { k: "name", label: "Наименование",
        cell: function (x) {
          return { v: x.name ? '<span class="clamp2" title="' + esc(x.name) + '">' +
                   esc(x.name) + "</span>" : null };
        } },
      { k: "fund", label: fundTitle, kind: "money",
        val: function (x) { var r = role(x.code, "fund"); return r.state === "ok" ? r.value : ""; },
        cell: function (x) {
          var fund = role(x.code, "fund");
          return { v: fund.state === "ok" ? known(fund)
                 : !off[x.code] ? '<span class="st bad">нет — в расчёт не пойдёт</span>'
                 : '<span class="kd">нет</span>', cls: "n" };
        } },
      { k: "term", label: "Срок",
        val: function (x) { var r = role(x.code, "term"); return r.state === "ok" ? r.value : ""; },
        cell: function (x) {
          var term = role(x.code, "term");
          return { v: term.state === "ok" ? known(term)
                 : '<span class="st warn">год плана — допущение</span>' };
        } },
      // Выплаты по месяцам (01.10.2026, выбор владельца продукта по совету
      // дизайнера): срок договора залит, щелчок по месяцу закрывает его —
      // с договора в этом месяце не платят никому. Перерывы всех договоров
      // видны разом, без панели.
      { k: "months", label: "Выплаты по месяцам",
        val: function (x) {
          var shut = pmonthsShut(x.code, bs);
          return shut.length ? "без выплат: " + shut.map(function (m) {
            return MONTH_NAMES[m - 1];
          }).join(", ") : "";
        },
        cell: function (x) { return { v: pmonthsCell(x, bs, !!off[x.code]) }; } },
      // Красный на вкладке один — договор без ФОТ (UI-010). Без трудоёмкости
      // договор в расчёт идёт, но не готов.
      { k: "labor", label: "Трудоёмкость", off: true,
        val: function (x) { var r = role(x.code, "labor"); return r.state === "ok" ? r.value : ""; },
        cell: function (x) {
          var labor = role(x.code, "labor");
          return { v: labor.state === "ok" ? known(labor) : '<span class="st warn">нет</span>' };
        } },
      { k: "sign", label: "Признак", off: true,
        val: function (x) {
          return x.goz === "да" ? "ГОЗ" : x.priority === "да" ? "Приоритет 2030" : "";
        } },
      { k: "number", label: "Номер договора", off: true },
      { k: "department", label: "Подразделение", off: true },
      { k: "edits", label: "Правки условий", off: true, kind: "number",
        val: function (x) {
          var n = Object.keys((bs.overrides || {})[x.code] || {}).length;
          return n ? String(n) : "";
        } },
      { k: "pins", label: "Закрепления", off: true, kind: "number",
        val: function (x) {
          var n = (bs.fixes || []).filter(function (f) { return f["договор"] === x.code; }).length;
          return n ? String(n) : "";
        } }
    ];
    GRIDS["pctr"] = { cols: pcols, rows: c, render: renderView };
    var pvis = gridShown("pctr", pcols);
    // Условия и закрепления договора — в панели справа от таблицы (решение
    // пользователя 19.09.2026 по совету дизайнера): таблица сжимается, а не
    // закрывается; щелчок по другой строке меняет содержимое панели на месте.
    // Раскрытие строкой под договором давало блок в ~400 px: строка уезжала
    // за край, а соседние прыгали.
    var open = pctrOpen && c.filter(function (x) { return x.code === pctrOpen; })[0];
    // Строка «В расчёте N из M» и «Графы» — одна полоса над таблицей, а не
    // две полупустых (проект дизайнера 20.09.2026).
    // «Графы» — выбор граф этой таблицы, и при открытой панели договора
    // тоже (пользователь 30.09.2026: кнопка пропадала, когда выбран договор;
    // прежнее «кнопки при панели нет» от 21.09.2026 снято): на широком окне
    // рядом с панелью видны и другие графы, и их набирают, не закрывая её.
    var bar = '<div class="tbar">' + headline + '<span class="sp"></span>' +
      '<button type="button" data-tb data-gcols="pctr" title="Графы" aria-label="Графы"' +
      ' aria-haspopup="true">' + TB_COLS + "<span>Графы</span></button></div>";
    var html = table([inclBox].concat(pvis.map(function (col) {
        return { v: esc(cap(col.label)), cls: gridIsNum(col) ? "n" : (col.always ? "key" : "") };
      })),
      pgc.rows.map(function (x) {
        return [{ v: '<input type="checkbox" data-incl="' + esc(x.code) + '"' +
                     (!off[x.code] ? " checked" : "") + ' aria-label="' + esc(x.code) +
                     ' в расчёте">', cls: "incl" }].concat(pvis.map(function (col) {
          return gridCell(col, x, false);
        }));
      }),
      "",
      { plain: true, numAfter: 1,
        numOf: function (i) { return String(pgc.from + i + 1); },
        rowCls: function (i) {
          var x = pgc.rows[i];
          return (off[x.code] ? "off" : "") + (pctrOpen === x.code ? " sel" : "");
        },
        rowAttr: function (i) { return 'data-pctr="' + esc(pgc.rows[i].code) + '"'; } });
    // Полоса и таблица — одной панелью реестра (пользователь 30.09.2026: «в
    // планах сделай все таблицы в одном стиле — как в реестре»).
    var list = '<div class="gridwrap"><div class="tpanel">' + bar + html + pager("v-ctr", pgc) +
      "</div></div>";
    // Прокрутка списка, панели и таблицы вбок переживает перерисовку: иначе
    // после каждой правки всё отпрыгивало к началу.
    var mainWas = el.querySelector(".ctrmain"), panelWas = el.querySelector(".ctrpanel");
    var hsWas = el.querySelector(".hscroll"), hsLeft = hsWas ? hsWas.scrollLeft : 0;
    var top = mainWas ? mainWas.scrollTop : el.scrollTop;
    var panelTop = panelWas && open && panelWas.getAttribute("data-code") === open.code
      ? panelWas.scrollTop : 0;
    el.innerHTML = (open
      ? '<div class="ctrsplit"><div class="ctrmain pctr">' + list + "</div>" +
        ctrPanel(open, bs, !pctrShown) + "</div>"
      : '<div class="pctr">' + list + "</div>");
    pctrShown = open ? open.code : null;
    var mainNow = el.querySelector(".ctrmain"), panelNow = el.querySelector(".ctrpanel");
    if (mainNow) mainNow.scrollTop = top; else el.scrollTop = top;
    if (panelNow) panelNow.scrollTop = panelTop;
    var hsNow = el.querySelector(".hscroll");
    if (hsNow && hsLeft) {
      hsNow.scrollLeft = hsLeft;
      hsNow.classList.toggle("scrolled", hsLeft > 0);
    }
    var head = el.querySelector(".inclall");
    if (head) {
      var boxes = el.querySelectorAll("[data-incl]");
      var k = Array.prototype.filter.call(boxes, function (b) { return b.checked; }).length;
      head.indeterminate = k > 0 && k < boxes.length;
    }
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
    // Кнопки расчёта здесь нет: «Рассчитать план» уже в шапке плана, вторая
    // такая же на той же странице — дубль (пользователь 20.09.2026).
    el.innerHTML = '<div class="empty-state" role="status" aria-live="polite">' +
      "<strong>План ещё не рассчитан</strong></div>";
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
      var body = repSummary() + repCash() + repPlan() + repLimits() + reportChecksSection() +
        repDepartments();
      // Раздела из адреса в этом расчёте нет — первый раздел, и адрес тоже.
      if (!REP_SECTIONS.some(function (x) { return x.n === repSec; })) {
        repSec = REP_SECTIONS.length ? REP_SECTIONS[0].n : 1;
        routeWrite("replace");
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
    "величин пакета": ["величина пакета", "величины пакета", "величин пакета"],
  };
  var sourceLabor = (made || {})["исходных строк трудоемкости"] || 0;
  // Поступления извлекаются, но в расчёт не идут (правило 21, SOURCE-009):
  // «24 поступления» в итоге разбора читались бы как данные, от которых
  // зависит план.
  return Object.keys(made || {}).filter(function (k) {
      return made[k] && k !== "исходных строк трудоемкости" && k !== "поступлений";
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

/* Карточка документа — часть адреса (?doc=, UI-021): открытая, она переживает
   перезагрузку, «Назад» её закрывает. Открывают её только так; саму карточку
   рисует showDocument. */
function openDocument(id) {
  navigate({ doc: id }, "push");
}

//: Открытая карточка документа: { id, el, close(quiet) } или null.
var docShown = null;

/* Карточку закрыли сами (×, Esc, решение по строкам): если её открыли
   переходом, шаг назад убирает её из истории, и «Вперёд» откроет снова;
   открытую по ссылке или перезагрузкой — адрес заменяется экраном под ней. */
function docLeave(id) {
  if (history.state && history.state.doc === id) history.back();
  else routeWrite("replace");
}

function showDocument(id) {
  // Две карточки одна поверх другой ни к чему: щелчок по второму имени
  // заменяет первую.
  if (docShown) docShown.close(true);
  var open = document.querySelector(".drawer");
  if (open) open.remove();

  var back = document.createElement("div");
  back.className = "drawer";
  back.innerHTML = '<section class="panel" role="dialog" aria-modal="true">' +
                   '<div class="none loading" role="status" aria-live="polite">Загружаю…</div></section>';
  document.body.appendChild(back);

  // quiet — закрыть без записи в адрес: закрывает сам переход (routeGo) или
  // уход из карточки в реестр, который пишет адрес сам.
  function close(quiet) {
    if (!back.isConnected) return;
    document.removeEventListener("keydown", onKey);
    back.remove();
    if (docShown && docShown.el === back) docShown = null;
    if (quiet !== true) docLeave(id);
  }
  function onKey(e) { if (e.key === "Escape") close(); }
  document.addEventListener("keydown", onKey);
  docShown = { id: id, el: back, close: close };

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
      // Из карточки — во вкладку реестра новым шагом истории; карточка
      // остаётся шагом позади, и «Назад» откроет её снова.
      close(true);
      navigate({ screen: "registry", tab: t.getAttribute("data-rtab2") }, "push");
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
          body: JSON.stringify({ accept: accept, reject: reject, document_sha256: d.sha256 || null,
                                 operation_id: opId() }),
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
          body: JSON.stringify({ edits: edits, document_sha256: d.sha256 || null }),
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
    if (!back.isConnected) return;
    // Документа по адресу нет (удалён) — карточка не открывается, адрес
    // возвращается к экрану под ней, причина — словами сервера.
    if (e && e.status === 404) {
      close(true);
      routeWrite("replace");
      showToast(cap(e.message || "документ не найден"));
      return;
    }
    back.querySelector(".panel").innerHTML =
      '<div class="none">' + esc(e.message || "не загрузилось") + "</div>";
  });
}


/* Отчет на весь экран: обе боковые колонки убраны, Esc возвращает. */
function fullReport(on) {
  document.querySelector(".shell").classList.toggle("full", on);
  var b = document.querySelector(".rtool .full");
  if (b) {
    b.classList.toggle("on", on);
    b.title = on ? "Выйти из полного экрана (Esc)" : "Отчет на весь экран";
    b.setAttribute("aria-label", b.title);
  }
  // Открытый чат на весь экран не закрывался, а прятался вместе с колонками:
  // вернулся на экран — реплики, пришедшие за это время, уже видны.
  if (!on && chatShown()) { unseen = 0; markFeedTab(); }
}
document.addEventListener("keydown", function (e) {
  if (e.key === "Escape" && document.querySelector(".shell.full")) fullReport(false);
});
/* Справа — только чат (30.09.2026): колонки контекста «Документы в этом
   плане» больше нет. Переход между экранами чат не закрывает (пользователь
   30.09.2026: «если открыт, чтоб не сбрасывался») — прежний wideScreen,
   закрывавший его на реестре, снят; закрывает чат только экономист. */

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
  $("view").hidden = true;
  $("regview").hidden = true;
  $("agentview").hidden = false;
  document.querySelector(".tabs").hidden = true;
  document.querySelector(".phead").hidden = true;
  if ($("openagents")) $("openagents").classList.add("on");
  $("openreg").classList.remove("on");
  planMarkOff();
  chatSync();
  $("agentview").innerHTML = '<div class="none loading" role="status" aria-live="polite">Загружаю…</div>';
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
  $("agentview").hidden = true;
  document.querySelector(".tabs").hidden = false;
  document.querySelector(".phead").hidden = false;
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
var inRegistry = false, regTab = "ctr", regData = null;
/* Справочник должностей «на дату» (SOURCE-012): ISO-дата поля, пусто —
   сегодня; раскрытые строки истории — по должности. */
var refOn = null, refOpen = {};
//: План, для которого действуют флажки договоров и выбор версий в реестре
//: (SOURCE-003): открытый план, иначе последний из списка. Раскрытый в
//: каталоге договор и раскрытые цепочки версий — только на время просмотра.
var ctrOpen = null, versOpen = {}, uploadContract = null;
/* Шифр договора, экран которого открыт; null — список (UI-009). Договор —
   объект, в который заходят: держать в голове, какая строка выделена, чтобы
   понять, к чему относится каталог под таблицей, экономисту не нужно. */
var ctrScreen = null;

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

/* Вкладки реестра по природе сущности (свод дизайн-системы «Реестр
   организации»): данные ведут и правят, справочники поддерживают раз в
   квартал, источники — загруженные файлы и состояние их разбора. У каждой
   группы свой ряд: в одном ряду с разделителями группы на второй строке
   перемешивались бы и читались кашей из восьми слов. */
var REG_TABS = [
  { key: "ctr", title: "Договоры", group: "Данные" },
  { key: "emp", title: "Штатное расписание", group: "Данные" },
  // Зарплата меняется в течение года — своя таблица по месяцам, а не графа
  // штатного расписания (решение пользователя 19.09.2026, макет «Кадры»).
  { key: "zp", title: "Заработная плата по месяцам", group: "Данные" },
  // Факт начислений прошедших месяцев (решение пользователя 30.09.2026):
  // данные организации, как штатное расписание; месяцы факта план берёт
  // сам, без выбора (SOURCE-011).
  { key: "fact", title: "Факт начислений", group: "Данные" },
  { key: "ref", title: "Справочник должностей", group: "Справочники" },
  { key: "sub", title: "Правила замещения", group: "Справочники" },
  { key: "secret", title: "Надбавка 120", group: "Справочники" },
  // Управленческие подразделения (01.10.2026): внутренняя структура для
  // отчёта руководству, как «Структура предприятия» в 1С.
  { key: "mgmt", title: "Управленческие подразделения", group: "Справочники" },
  { key: "docs", title: "Документы", group: "Источники" }
];
var REG_GROUPS = ["Данные", "Справочники", "Источники"];

/* Дата «дд.мм.гггг» числом для сравнения: 20260312. */
function wgDay(s) {
  var p = String(s || "").split(".");
  if (p.length !== 3) return 0;
  var y = +p[2];
  if (y < 100) y += 2000;
  return y * 10000 + (+p[1]) * 100 + (+p[0]);
}

/* Строки рабочей группы договора — все приказы, от старого к новому. */
function wgRowsOf(code) {
  var d = (regData && regData.data) || {};
  return (d.workgroups || []).filter(function (r) { return r.contract === code; })
    .sort(function (a, b) { return wgDay(a.date) - wgDay(b.date) || a.id - b.id; });
}

/* Действующий состав: по каждой строке штатного расписания решает последний
   приказ, где она названа, — «включить» или «исключить». */
function wgCurrent(rows) {
  var last = {};
  rows.forEach(function (r) { if (r.employee) last[r.employee] = r; });
  return Object.keys(last).map(function (k) { return last[k]; })
    .filter(function (r) { return r.action !== "исключить"; });
}

/* Что показывает вкладка «Рабочая группа»: действующий состав и строки,
   которые ещё не сопоставлены со штатным расписанием — их надо выбрать.
   Исключённые прежними приказами не показываются: это история, она в самих
   приказах. */
function wgShownOf(code) {
  var rows = wgRowsOf(code);
  return wgCurrent(rows).concat(rows.filter(function (r) { return !r.employee; }));
}

/* Рабочие группы строки штатного расписания: договоры, где она в
   действующем составе. Срок группы истёк — договор остаётся, но тише. */
function wgOfRow(code) {
  var d = (regData && regData.data) || {}, by = {};
  (d.workgroups || []).forEach(function (r) {
    if (r.contract && r.employee === code) (by[r.contract] = by[r.contract] || []).push(r);
  });
  var today = new Date(), now = today.getFullYear() * 10000 + (today.getMonth() + 1) * 100 +
              today.getDate();
  return Object.keys(by).sort().map(function (c) {
    var rows = by[c].sort(function (a, b) { return wgDay(a.date) - wgDay(b.date) || a.id - b.id; });
    var r = rows[rows.length - 1];
    if (r.action === "исключить") return null;
    return { contract: c, order: r, over: r.to && wgDay(r.to) < now };
  }).filter(Boolean);
}

/* Вкладка договора «Рабочая группа»: состав по приказам этого договора.
   Строка — член группы (строка штатного расписания), приказ при нём. Приказ,
   загруженный здесь, привязывается к этому договору: договор выбрал
   экономист, и полосы сверки л/с и соглашения приказа с договором нет
   (пользователь 29.09.2026: «зачем это окно?» — сверка пересказывала
   прочитанные поля и висела, пока договор не сменят). */
function workgroupTable(rows, x) {
  var d = (regData && regData.data) || {}, code = x.code;
  var wemp = {};
  (d.employees || []).forEach(function (e) { wemp[e.code] = e; });
  // Строки «Основание» над составом нет (пользователь 29.09.2026: «может
  // это убрать, а сделать переход к документу через гиперссылку в
  // ячейке?»): приказ — ссылкой в графе «Приказ» своей строки.
  return regGrid("ctr-wg", [
      // Табельный номер, ФИО и должность — одна строка штатного расписания
      // (пользователь 28.09.2026: «нельзя выбрать табельный номер одного
      // сотрудника и ФИО другого»). Нашлась — все три графы берутся из неё;
      // не нашлась — показано, что написано в приказе: номер и должность
      // тише, ФИО цветом внимания; щелчок по ФИО — выбор строки (wgPickRow).
      { k: "person_code", label: "Табельный номер", always: true,
        val: function (r) {
          var e = r.employee && wemp[r.employee];
          return e ? (e.person_code || e.code) : r.person_code;
        },
        cell: function (r) {
          var e = r.employee && wemp[r.employee];
          return { v: e ? esc(e.person_code || e.code)
                        : '<span class="kd" title="Так в приказе: в штатном расписании не найден">' +
                          esc(r.person_code || "") + "</span>" };
        } },
      { k: "fio", label: "ФИО",
        val: function (r) {
          var e = r.employee && wemp[r.employee];
          return e ? e.fio : r.fio;
        },
        cell: function (r) {
          var e = r.employee && wemp[r.employee];
          return { v: '<span class="ed" data-wgpick="' + r.id + '" role="button" tabindex="0"' +
            (e ? ' title="Выбрать другого сотрудника">' + esc(e.fio || e.code)
               : ' data-miss title="Нет в штатном расписании — выбрать строку">' +
                 esc(r.fio || "")) + "</span>" };
        } },
      { k: "position", label: "Должность",
        val: function (r) {
          var e = r.employee && wemp[r.employee];
          return e ? e.position : r.position;
        },
        cell: function (r) {
          var e = r.employee && wemp[r.employee];
          return { v: e ? esc(e.position || "") : '<span class="kd" title="Так в приказе">' +
                                                   esc(r.position || "") + "</span>" };
        } },
      // Вид работ — целиком, с переносом: две строки с подсказкой прятали
      // описание (пользователь 29.09.2026: «туда не помещается описание вид
      // работ»). Графа не уже 280 px: иначе в узком окне описание вставало
      // столбиком в одно-два слова.
      { k: "work", label: "Вид работ",
        cell: function (r) { return r.work ? { v: esc(r.work), cls: "work" } : null; } },
      { k: "order", label: "Приказ",
        val: function (r) { return "№ " + (r.number || "?") + " от " + (r.date || "?"); },
        cell: function (r) {
          var text = "№ " + (r.number || "?") + " от " + (r.date || "?") +
            (r.kind === "изменение" ? " (изменение)" : "");
          return { v: r.document_id
            ? '<span class="dname" data-doc="' + r.document_id + '" role="button" tabindex="0"' +
              ' title="Открыть приказ">' + esc(text) + "</span>"
            : esc(text) };
        } },
      { k: "span", label: "Срок группы",
        val: function (r) { return (r.from || "") + " – " + (r.to || ""); } },
      { k: "action", label: "Действие", off: true },
      { k: "leader", label: "Руководитель", off: true,
        val: function (r) { return r.leader ? "да" : ""; } },
      { k: "department", label: "Подразделение", off: true }
    ], rows, { bar: { upload: code, addLabel: "Загрузить приказ о рабочей группе",
                      placeholder: "Табельный номер или ФИО…" } });
}

/* Приказ о рабочей группе, прочитанный из этого документа, или null. */
function wgOrderOfDoc(docId) {
  var d = (regData && regData.data) || {};
  return (d.workorders || []).filter(function (w) { return w.document_id === docId; })[0] || null;
}

/* Результат обработки приказа о рабочей группе — величина, и только она:
   сколько людей в приказе и что не сошлось. Части связаны словами, а не
   знаком (UI-015); не сошлось — весь итог статусом внимания. Договора нет —
   это главное: без него группы нет, а строки штатного расписания сверяют
   уже в договоре. Ссылки на группу здесь нет: графы у всех документов одни
   (пользователь 29.09.2026: «в документах будут просто все вообще
   документы»), группа — во вкладке договора. */
function wgDocResult(docId) {
  var o = wgOrderOfDoc(docId);
  if (!o) return null;
  var people = o.people + " " + px(o.people, "человек", "человека", "человек");
  var miss = !o.contract ? "договор не найден"
    : o.lost ? o.lost + " " + px(o.lost, "не найден", "не найдены", "не найдены") +
               " в штатном расписании"
    : "";
  return miss ? '<span class="st warn">' + esc(people + ", " + miss) + "</span>" : esc(people);
}

/* Договор документа — из меню строки, списком договоров реестра с поиском,
   одним для любого документа (решение пользователя 29.09.2026: «сделай
   что-то общее, не зажимая в рамки»). Похожие по лицевому счёту или номеру
   соглашения, которые назвал сам документ (приказ о рабочей группе), —
   первыми: точное совпадение разбор нашёл бы сам. «Новый договор…» — последним
   и при любом поиске: шифр вводят руками, только когда такого договора ещё
   нет (SOURCE-005), он заводится. У приказа его нет — приказ издают по
   договору, который уже есть; меняется только договор приказа, выбранные
   строки штатного расписания остаются. */
function docPickContract(anchor, docId, name, o) {
  var d = (regData && regData.data) || {};
  var acc = acctKey(o && o.account), num = acctKey(o && o.agreement);
  var near = function (c) {
    var ca = acctKey(c.account), cn = acctKey(c.number);
    return !!((acc && ca && (ca.indexOf(acc) >= 0 || acc.indexOf(ca) >= 0)) ||
              (num && cn && (cn.indexOf(num) >= 0 || num.indexOf(cn) >= 0)));
  };
  var all = (d.contracts || []).slice().sort(function (a, b) {
    return (near(b) - near(a)) || String(a.code).localeCompare(String(b.code), "ru");
  });
  var items = all.map(function (c) { return { id: c.code, label: c.code, sub: c.name || "" }; });
  if (!o) items.push({ id: "", label: "Новый договор…", keep: true });
  function save(code) {
    api(o ? "/api/workorder/" + o.id + "/contract" : "/api/document/" + docId + "/contract", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: code })
    }).then(function () {
      showToast("«" + name + "» — в договоре " + code);
      loadRegistry(true);
      tick();
    }, function (err) {
      alert("Не удалось указать договор: " + (err.message || "ошибка сети"));
    });
  }
  pickFrom(anchor, items, "Найти договор", "Договоров в реестре нет", function (code) {
    if (code) { save(code); return; }
    askText("Договор для «" + name + "»", "Шифр договора").then(function (typed) {
      if (typed) save(typed);
    });
  });
}

/* Строка штатного расписания члена группы — списком с поиском, как договор
   документа: выбор только щелчком или Enter. Прежний раскрывающийся список
   сохранял выбор на любое изменение, и случайная стрелка записала человеку
   из приказа чужую строку (пользователь 29.09.2026: Авдонюшкина нет в
   штатном расписании, а он встал на строку Иванова). Первыми — строки с
   табельным номером из приказа: у человека их бывает две, основная и
   совместительство, и в группу входит конкретная. Последний пункт —
   «Нет в штатном расписании»: снять ошибочный выбор. */
function wgPickRow(anchor, x) {
  var all = (((regData || {}).data || {}).employees || []).slice();
  var mine = function (r) { return (r.person_code || r.code) === x.person_code; };
  all.sort(function (a, b) {
    return (mine(b) - mine(a)) ||
      String(a.fio || a.code).localeCompare(String(b.fio || b.code), "ru");
  });
  var items = all.map(function (r) {
    return { id: r.code, label: r.fio || r.code,
             sub: "табельный номер " + (r.person_code || r.code) +
                  (r.position ? ", " + r.position : "") };
  });
  if (x.employee) items.push({ id: "", label: "Нет в штатном расписании", keep: true });
  pickFrom(anchor, items, "Найти сотрудника", "В штатном расписании никого нет", function (code) {
    api("/api/workorder/member/" + x.id, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ employee: code || null })
    }).then(function () {
      showToast(code ? "Строка штатного расписания указана" : "Выбор строки снят");
      loadRegistry(true);
    }, function (er) {
      alert("Не сохранилось: " + (er.message || "ошибка сети"));
    });
  });
}

/* Щелчок по ФИО члена группы — выбор его строки штатного расписания; с
   клавиатуры — Enter или пробел. */
document.addEventListener("click", function (e) {
  var t = e.target.closest && e.target.closest("[data-wgpick]");
  if (!t) return;
  var id = +t.getAttribute("data-wgpick");
  var x = (((regData || {}).data || {}).workgroups || []).filter(function (r) {
    return r.id === id;
  })[0];
  if (x) wgPickRow(t, x);
});
document.addEventListener("keydown", function (e) {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches &&
      e.target.matches("[data-wgpick]")) {
    e.preventDefault();
    e.target.click();
  }
});

/* Флажок «да/нет» договора сохраняется сразу: «да» или «нет» явным
   значением, реестр перечитывается. Строка списка и экран договора несут
   шифр в data-ctr. */
document.addEventListener("change", function (e) {
  var cb = e.target;
  if (!cb || !cb.matches || !cb.matches("input[data-yesno]")) return;
  var host = cb.closest("[data-ctr]"), code = host && host.getAttribute("data-ctr");
  if (!code) return;
  var fields = {};
  fields[cb.getAttribute("data-yesno")] = cb.checked ? "да" : "нет";
  api("/api/contracts/" + encodeURIComponent(code), {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fields: fields })
  }).then(function () { loadRegistry(true); }, function (er) {
    alert("Не сохранилось: " + (er.message || "ошибка сети"));
    renderRegistry();
  });
});

/* Лицевой счёт в штатном расписании и счёт договора — одно и то же число,
   записанное людьми: сравниваем без пробелов и регистра. */
function acctKey(v) {
  return String(v == null ? "" : v).replace(/\s+/g, "").toLowerCase();
}

/* Счёт → шифр договора. Пока договоры не загружены, соответствия нет, и
   счёт в штатке остаётся текстом; после загрузки та же графа сама становится
   ссылкой (решение пользователя 28.09.2026). */
function ctrByAccount() {
  var d = (regData && regData.data) || {}, out = {};
  (d.contracts || []).forEach(function (c) {
    var k = acctKey(c.account);
    if (k && !out[k]) out[k] = c.code;
  });
  return out;
}

/* Все документы реестра — из тех же каталогов, что стоят на экранах:
   без договора, по договорам, штатного, справочника, правил замещения. */
function regDocGroups() {
  var bs = (regData && regData.basis) || {};
  var d = (regData && regData.data) || {};
  var out = [];
  if ((bs.unlinked || []).length) out.push({ title: "Без договора", list: bs.unlinked,
                                             contract: null, unlinked: true });
  (d.contracts || []).forEach(function (c) {
    var list = (bs.contracts || {})[c.code] || [];
    if (list.length) out.push({ title: c.code + (c.name ? " · " + c.name : ""),
                                list: list, contract: c.code });
  });
  [["staff", "Штатное расписание"], ["salary", "Заработная плата по месяцам"],
   ["facts", "Факт начислений"], ["reference", "Справочник должностей"],
   ["substitutions", "Правила замещения"]].forEach(function (g) {
    var list = bs[g[0]] || [];
    if (list.length) out.push({ title: g[1], list: list, contract: null, section: g[0] });
  });
  return out;
}

/* Все документы одним списком (UI-013): общий файл («Договоры 2026.xlsx»)
   стоит в каталоге каждого своего договора, а здесь — одной строкой. Свежие
   сверху: номер документа растёт с каждой загрузкой. */
function regDocList() {
  var seen = {}, out = [];
  regDocGroups().forEach(function (g) {
    g.list.forEach(function (e) {
      if (seen[e.root]) return;
      seen[e.root] = true;
      out.push({ e: e, g: g });
    });
  });
  return out.sort(function (a, b) { return b.e.versions[0].id - a.e.versions[0].id; });
}

/* Графа «относится к»: шифр договора — у общего файла все шифры — или раздел
   реестра. Ни к чему не отнесён — графа пуста: справка или штатное
   расписание к договору и не должны относиться. */
var DOC_SECTION_TAB = { staff: "emp", salary: "zp", facts: "fact", reference: "ref",
                        substitutions: "sub" };

function docRelation(e, g, known) {
  if (e.contracts.length) {
    return e.contracts.map(function (code) {
      return known[code]
        ? '<button type="button" data-ctropen="' + esc(code) + '">' + esc(code) + "</button>"
        : esc(code);
    }).join(", ");
  }
  var tab = DOC_SECTION_TAB[g.section];
  return tab ? '<button type="button" data-rtab="' + tab + '">' + esc(g.title) + "</button>" : "";
}

/* Экран реестра — без записи в адрес: переходы в реестр идут через
   navigate(). Чат при входе не закрывается (30.09.2026): его значок стоит в
   строке заголовка реестра, а в шапке чата названо, чья это переписка. */
function openRegistry() {
  leaveAgents();
  inRegistry = true;
  $("view").hidden = true;
  $("regview").hidden = false;
  document.querySelector(".tabs").hidden = true;
  document.querySelector(".phead").hidden = true;
  $("openreg").classList.add("on");
  $("opencurrent").classList.remove("on");
  planMarkOff();
  chatSync();
  loadRegistry();
}

/* Открытого плана в колонке нет: реестр и страница агентов — не план. */
function planMarkOff() {
  Array.prototype.forEach.call(document.querySelectorAll(".case"), function (el) {
    el.classList.remove("on");
    el.removeAttribute("aria-current");
  });
}

function leaveRegistry() {
  if (!inRegistry) return;
  inRegistry = false;
  $("regview").hidden = true;
  document.querySelector(".tabs").hidden = false;
  document.querySelector(".phead").hidden = false;
  $("openreg").classList.remove("on");
}

function loadRegistry(quiet) {
  // Обновление на месте (пока разбирается загруженный файл) не должно
  // мигать «Загружаю…» на весь экран: таблица просто перерисуется.
  if (!quiet) $("regview").innerHTML = '<div class="none loading" role="status" aria-live="polite">Загружаю…</div>';
  Promise.all([api("/api/documents"), api("/api/case/" + (caseId || 0) + "/data")
                 .catch(function () { return {}; }),
               // План 0 — реестр без плана: каталог и пакеты, без выбора.
               api("/api/case/0/basis").catch(function () { return {}; }),
               // Каталог открытого плана — чтобы список версий штатного
               // расписания показывал ту, что взял этот план, а не последнюю.
               caseId ? api("/api/case/" + caseId + "/basis").catch(function () { return {}; })
                      : Promise.resolve(null)])
    .then(function (r) {
      regData = { documents: r[0], data: r[1], basis: r[2], planBasis: r[3] };
      renderRegistry();
    })
    .catch(function (e) {
      $("regview").innerHTML = '<div class="none">' + esc(e.message || "не загрузилось") + "</div>";
    });
}

function renderRegistry() {
  if (!regData) return;
  if (regTab === "labor" || regTab === "inflow") regTab = "ctr";
  var d = regData.data || {}, docs = regData.documents || [];
  var body = "";

  if (regTab === "ctr") {
    var bs = regData.basis || {};
    var ctrs = d.contracts || [];
    // Открыт экран договора или список (UI-009). Договор мог исчезнуть из
    // реестра, пока экран был открыт, или адрес называет удалённый: тогда
    // возвращаемся к списку сами, и адрес — тоже.
    var one = ctrScreen && ctrs.filter(function (x) { return x.code === ctrScreen; })[0];
    if (ctrScreen && !one) { ctrScreen = null; routeWrite("replace"); }
    if (one) {
      body = contractScreen(one, bs);
    } else {
      // Поиск — по шифру и наименованию, как в макете: остальные графы
      // сравнивают, а ищут по этим двум.
      var q = ctrQuery.trim().toLowerCase();
      if (q) {
        ctrs = ctrs.filter(function (x) {
          return (x.code || "").toLowerCase().indexOf(q) >= 0 ||
                 (x.name || "").toLowerCase().indexOf(q) >= 0;
        });
      }
      var vis = ctrPinOrder(ctrCols());
      ctrs = ctrArrange(ctrs, vis);
      // Группировка из меню графы (как в прототипе): графа группировки уходит
      // из строк — её значение стоит в шапке группы. Когда граф остаётся всего
      // две, она остаётся: строка без неё немая.
      var gcol = ctrGroup && vis.filter(function (c) { return ctrColKey(c) === ctrGroup; })[0];
      var shown = gcol && vis.length > 2 ? vis.filter(function (c) { return c !== gcol; }) : vis;
      var cellsOf = function (x) {
        return shown.map(function (c) {
          var pin = ctrPin.indexOf(ctrColKey(c)) >= 0 ? " pin" : "";
          // Шифр — кнопка: договор открывается и с клавиатуры, а не только
          // щелчком по строке (UI-008). Обработчик тот же, что у «относится к».
          if (c.key === "code") return { v: '<button type="button" data-ctropen="' + esc(x.code) +
                                            '">' + esc(x.code) + "</button>", cls: "key" };
          if (c.key === "name") {
            // Пока разбор не дал наименования, строка — не договор без данных,
            // а незавершённая запись: состояние её документов (UI-006).
            return { v: x.name ? '<span class="clamp2 rowkey" title="' + esc(x.name) + '">' +
                       esc(x.name) + "</span>"
                     : ctrParsing(x.code, bs) ? '<span class="st wait">' +
                       esc(STATUS["ожидает"].text) + "</span>" : "", cls: pin.trim() };
          }
          if (c.key === "sign") return { v: esc(ctrSign(x)), cls: "w-nowrap" + pin };
          if (c.key === "docs") {
            var n = ((bs.contracts || {})[x.code] || []).length;
            return { v: n ? String(n) : "", cls: "n" + pin };
          }
          var f = ctrField(c.col);
          var need = ctrMissing(x, bs).indexOf(c.col) >= 0 && "не заполнено";
          return { v: f.kind === "yesno" ? yesnoCell(f, x[f.key]) : editCell(f, x[f.key], need),
                   cls: (f.kind === "money" || f.kind === "date" ? "n" : "") + pin };
        });
      };
      var pgc = paged("ctr", ctrs);
      // Графы те же, что во входном файле решателя: счет решает, можно ли
      // платить 120 без оклада, приоритет — на каком договоре держать оклад,
      // а основное место и совместительство — можно ли открыть ставку. Список
      // остаётся местом, где их сравнивают между договорами: ради одного
      // договора за ними не проваливаются, их смотрят по всем сразу. Добавлен
      // «пакет» — он отвечает, есть ли чем считать, и в графах его не видно.
      // Графы между шифром и пакетом правятся в ячейке (SOURCE-007): в
      // документах их нет, экономист вводит их здесь, один раз на договор.
      // Сгруппированный список — целиком, без страниц: группа, разорванная
      // между страницами, теряет смысл итога в своей шапке.
      body = '<div class="ctrlist"><div class="tpanel">' + ctrTools(ctrs.length) + (gcol
        ? ctrGroupedTable(ctrs, shown, gcol, cellsOf)
        : table(shown.map(ctrColHead), pgc.rows.map(cellsOf).concat(
              ctrTotalRow(shown, d.contracts_fund)),
            "", { plain: true,
            // Номер — место строки после отбора и порядка, на второй странице
            // продолжается; «Итого» номера не получает.
            numOf: function (i) { return pgc.rows[i] ? String(pgc.from + i + 1) : ""; },
            rowCls: function (i) {
              var x = pgc.rows[i];
              return !x ? "total" : ctrMissing(x, bs).length ? "draft" : "";
            },
            rowAttr: function (i) {
              return pgc.rows[i] ? 'data-ctr="' + esc(pgc.rows[i].code) + '"' : "";
            } }) +
          pager("ctr", pgc)) + "</div></div>";
      if (!ctrs.length) {
        body = '<div class="ctrlist"><div class="tpanel">' + ctrTools(0) +
          '<div class="none">Ничего не найдено</div></div></div>';
      }
      // Очереди «N документов не привязаны к договорам» нет (решение
      // пользователя 29.09.2026): договор у документа необязателен, и
      // документ без него — не долг экономиста, а просто документ
      // организации.
    }
    body = freshStrip(docs) + body;
    if (!ctrs.length && !docs.length) {
      body = '<div class="empty2"><b>Документов пока нет</b>' +
        "<p>Перетащите сюда файлы или нажмите «Загрузить документы». " +
        "Договоры появятся сами — по шифру и теме из документов. " +
        "Подойдет любой документ: штатное расписание, расчетно-калькуляционные " +
        "материалы, структура цены, приказ, положение.</p></div>";
    }
  } else if (regTab === "emp") {
    // Тип и категория занятости решают, можно ли открыть вторую ставку и до
    // какого предела: у студента и аспиранта он свой. Раньше их не было
    // видно, потому что в расчет уходило жестко «основное» и «основной».
    // Таблица — на общем движке (grid): те же графы, те же значения.
    body = regGrid("emp", [
      { k: "code", label: "Табельный номер", always: true },
      { k: "fio", label: "ФИО" },
      { k: "position", label: "Должность" },
      { k: "department", label: "Подразделение" },
      // Лицевой счёт, на котором сотрудник сидит сейчас. Пока договора с
      // таким счётом в реестре нет — просто число; как только договоры
      // загрузят, то же значение становится ссылкой в договор, и ничего
      // перезагружать руками не нужно (решение пользователя 28.09.2026).
      { k: "account", label: "Лицевой счёт",
        val: function (x) { return x.account || null; },
        cell: function (x) {
          var code = ctrByAccount()[acctKey(x.account)];
          if (!x.account) return null;
          // Разметку движок таблицы принимает только объектом { v }: строку он
          // экранирует и показал бы кнопку текстом.
          return { v: code
            ? '<button type="button" data-ctropen="' + esc(code) + '" title="' +
              esc("Договор " + code) + '">' + esc(x.account) + "</button>"
            : esc(x.account) };
        } },
      // Рабочие группы строки — договоры, в чьём действующем составе она
      // есть (пользователь 29.09.2026): со стороны людей видно, где человек
      // сидит, без обхода договоров. Шифр — кнопкой в договор; группа с
      // истёкшим сроком — тише, срок в подсказке.
      { k: "groups", label: "Рабочие группы",
        val: function (x) {
          return wgOfRow(x.code).map(function (g) { return g.contract; }).join(", ");
        },
        cell: function (x) {
          var gs = wgOfRow(x.code);
          if (!gs.length) return null;
          return { v: gs.map(function (g) {
            return '<button type="button" data-ctropen="' + esc(g.contract) + '" data-ctrtab="wg"' +
              (g.over ? " data-over" : "") + ' title="' +
              esc("Приказ № " + (g.order.number || "?") + " от " + (g.order.date || "?") +
                  (g.order.from ? ", группа " + g.order.from + " – " + (g.order.to || "") : "") +
                  (g.over ? ", срок истёк" : "")) + '">' + esc(g.contract) + "</button>";
          }).join(", ") };
        } },
      { k: "rate", label: "Ставка", kind: "number",
        val: function (x) { return x.rate == null ? null : String(x.rate).replace(".", ","); } },
      { k: "employment", label: "Занятость" },
      { k: "category", label: "Категория" },
      // Зарплаты здесь нет: она по месяцам на своей вкладке (19.09.2026).
      { k: "from", label: "Начало", kind: "date" },
      { k: "to", label: "Окончание", kind: "date" },
      // Одна графа вместо «Разрешены» и «Запрещены» (пользователь
      // 30.09.2026: «может не вводить две колонки? а просто сделать список с
      // возможностью выбора? и сделать выбрать все»): щелчок — список
      // договоров с галочками. Хранится одно из двух, что короче (сервер).
      { k: "contracts", label: "Разрешённые договоры",
        val: function (x) { return empContractsText(x); },
        cell: function (x) {
          var on = empContractsOn(x), all = regContractCodes();
          var chips = on.length === all.length ? ["Все договоры"]
            : on.length > all.length / 2
              ? ["Все, кроме " + all.filter(function (c) { return on.indexOf(c) < 0; }).join(", ")]
              : on;
          return { v: '<span data-empctr="' + esc(x.code) + '" role="button" tabindex="0"' +
                      ' title="Изменить договоры сотрудника">' +
                      (chips.length ? chips.map(function (p) {
                         return '<span class="chip">' + esc(p) + "</span>";
                       }).join("") : '<span class="kd">Указать договоры</span>') + "</span>" };
        } }
    ], d.employees || [], {
      bar: { upload: "", addLabel: "Загрузить штатное расписание", placeholder: "Табельный номер или ФИО…" } });
    body += catalogFor("staff", "Документы штатного расписания");
  } else if (regTab === "zp") {
    // Строки приносит документ зарплаты или экономист, выбрав сотрудника по
    // ФИО из штатного расписания (решение пользователя 19.09.2026). Документ
    // загружают строкой каталога под таблицей — единственный вход для файлов
    // вкладки (решение 18.09.2026), второй кнопки загрузки в шапке нет. В
    // месяце — своя сумма или зарплата штатного расписания; правят в ячейке;
    // пустая ячейка — месяц вне срока строки. Итог считает сервер (UI-017).
    // Удалить строку — из меню правым щелчком, как в других таблицах реестра.
    var zs = d.salary || { rows: [], totals: [] };
    var ztot = {};
    (zs.totals || []).forEach(function (t, i) { ztot["m" + (i + 1)] = t; });
    body = regGrid("zp", [
      { k: "code", label: "Табельный номер", always: true },
      { k: "fio", label: "ФИО" },
      // Управленческое подразделение строки (01.10.2026): у человека в двух
      // подразделениях — две строки со своими суммами. Щелчок — выбор из
      // справочника; пусто — пустая ячейка.
      { k: "dept", label: "Управленческое подразделение",
        val: function (x) { return x.dept || ""; },
        cell: function (x) {
          return { v: '<span class="ed" data-zpdept="' + x.id + '" role="button" tabindex="0"' +
            ' aria-label="' + esc("Управленческое подразделение: " + (x.fio || x.code)) +
            '" title="' + (x.dept ? "Изменить подразделение" : "Указать подразделение") + '">' +
            esc(x.dept || "") + "</span>" };
        } }
    ].concat(MONTHS.map(function (name, i) {
      return { k: "m" + (i + 1), label: name.charAt(0).toUpperCase() + name.slice(1),
               kind: "money",
               val: function (x) { return x.months[i] == null ? null : mo(x.months[i]); },
               cell: function (x) { return zpCell(x, i + 1); } };
    })), zs.rows || [], { totals: ztot,
      bar: { add: "data-zpadd", addLabel: "Добавить сотрудника", placeholder: "Табельный номер или ФИО…" },
      rowAttr: function (x) {
        return 'data-zprow="' + x.id + '" title="Источник: ' + esc(x.source || "—") + '"';
      } });
    body += catalogFor("salary", "Документы зарплаты по месяцам");
  } else if (regTab === "fact") {
    // Факт начислений (30.09.2026): прошедшие месяцы, как были начислены, —
    // формой 6.3, шаблоном кадров (factsGrid); загрузка — в шапке панели,
    // список загруженных файлов — каталогом под ней.
    body = factsGrid("fact", d.facts_detail || []);
    body += catalogFor("facts", "Документы факта начислений");
  } else if (regTab === "secret") {
    body = secretGrid(d) + catalogFor("secret", "Документы надбавки 120");
  } else if (regTab === "mgmt") {
    // Справочник управленческих подразделений (01.10.2026): наименование и
    // порядок — в нём подразделения стоят в разделе 16 отчёта. Правка — из
    // меню строки, как у правил замещения.
    body = regGrid("mgmt", [
      { k: "name", label: "Наименование", always: true },
      { k: "position", label: "Порядок", kind: "number",
        val: function (x) { return x.position == null ? null : String(x.position); } },
      { k: "rows", label: "Строк зарплаты", kind: "number",
        val: function (x) { return x.rows ? String(x.rows) : null; } }
    ], d.mgmt_departments || [], {
      bar: { add: "data-mgmtadd", addLabel: "Добавить подразделение", placeholder: "Подразделение…" },
      rowAttr: function (x) { return 'data-mgmtrow="' + x.id + '"'; } });
  } else if (regTab === "ref") {
    // Окладной группы здесь нет: взаимозаменяемость задают правила замещения,
    // а группировка из положения об оплате труда устарела и в решении больше
    // не участвует. БЭП, наоборот, показываем — по нему считается средний
    // предел по ГОЗ-договору, а раньше его было не видно.
    var money = function (key) {
      return function (x) { return x[key] == null ? null : mo(x[key]); };
    };
    // Справочник на дату (SOURCE-012): величины, действующие в этот день; у
    // должности с историей — ▸, раскрывающий её строками по датам.
    var refHist = refHistory(d.reference_history || []);
    var refDay = refOn || isoToday();
    var refRows = (d.reference || []).map(function (x) { return refRowOn(x, refHist, refDay); });
    body += regGrid("ref", [
      { k: "pos", label: "Должность", always: true,
        cell: function (x) {
          if (!refHist[x.pos]) return { v: esc(x.pos) };
          var open = !!refOpen[x.pos];
          return { v: '<button type="button" data-refopen="' + esc(x.pos) + '" aria-expanded="' +
            open + '" aria-label="' + esc((open ? "Скрыть историю: " : "Показать историю: ") +
            x.pos) + '"><i aria-hidden="true">' + (open ? "▾" : "▸") + "</i><b>" + esc(x.pos) +
            "</b></button>" };
        } },
      { k: "cat", label: "Категория" },
      { k: "sal", label: "Оклад за 1,0 ставки, ₽", kind: "money", val: money("sal") },
      { k: "p2556", label: "П2556, ₽", kind: "money", val: money("p2556") },
      { k: "p4", label: "П4, ₽", kind: "money", val: money("p4") },
      { k: "bep", label: "БЭП, ₽", kind: "money", val: money("bep") },
      { k: "note", label: "Примечание" }
    ], refRows, {
      bar: { upload: "", addLabel: "Загрузить справочник", placeholder: "Должность…",
             right: '<label data-tb data-refon' + (refOn && refOn !== isoToday() ? " data-moved" : "") +
               '><span>На дату</span><input type="date" value="' + esc(refDay) +
               '" aria-label="Справочник на дату"></label>' },
      sub: function (x, shown) { return refOpen[x.pos] ? refSubRows(x, refHist, refDay, shown) : []; } });
    body += referenceCatalog(d);
  } else if (regTab === "docs") {
    // Все файлы реестра одной таблицей (UI-013): к чему относится документ,
    // говорит графа, а не заголовок блока. Строка приёма — в рамке таблицы.
    var known = {}, rel = {};
    (d.contracts || []).forEach(function (c) { known[c.code] = true; });
    var items = regDocList();
    items.forEach(function (x) {
      rel[x.e.root] = { v: docRelation(x.e, x.g, known), unlinked: !!x.g.unlinked };
    });
    // Приём файлов — строкой внизу таблицы, как во всех таблицах документов
    // (решение пользователя 18.09.2026); кнопки над таблицей нет.
    body = catalogBlock(null, items.map(function (x) { return x.e; }), null, "", false, false, rel);
  } else if (regTab === "sub") {
    // Правила правятся здесь же (SOURCE-010), как в прототипе: набор —
    // щелчком по нему, изменить и удалить — правым щелчком по строке. Графы
    // «сколько» нет: число должностей видно по самому набору (решение
    // пользователя 18.09.2026). Источник правила — в подсказке строки.
    // Названия граф — по смыслу правила, как его читает решатель: работнику
    // на этой должности можно открыть ещё одну ставку на перечисленных
    // (решение пользователя 18.09.2026; прежнее «может быть замещена»
    // читалось наоборот). Таблица — на общем движке (grid).
    body = regGrid("sub", [
      { k: "position", label: "Должность работника", always: true },
      { k: "replaced_by", label: "Допустимые должности для дополнительной ставки",
        cell: function (x) {
          var set = substList(x.replaced_by);
          return { v: '<span data-subedit="' + x.id + '" role="button" tabindex="0"' +
                      ' title="Изменить набор должностей">' +
                      (set.length ? set.map(function (p) {
                         return '<span class="chip">' + esc(p) + "</span>";
                       }).join("") : '<span class="kd">Указать должности</span>') + "</span>" };
        } },
      { k: "source", label: "Источник", off: true }
    ], d.substitutions || [], {
      bar: { add: "data-subadd", addLabel: "Добавить правило", placeholder: "Должность…" },
      rowAttr: function (x) {
        return 'data-sub="' + x.id + '" title="Источник: ' + esc(x.source || "—") + '"';
      } });
    body += catalogFor("substitutions", "Документы правил замещения");
  }

  var tabsRow =
    // Значок чата — справа в строке заголовка, как в шапке плана (UI-012):
    // чат открывают из любого экрана.
    '<div class="regtitle"><h1>Реестр организации</h1>' + CHAT_TOGGLE + "</div>" +
    '<div class="reghead">' +
    // Вкладки — одной строкой, группы разделены чертой без подписей (UI-014).
    '<div class="rtabrow"><div class="rtabs" role="tablist" aria-label="Разделы реестра">' +
    REG_GROUPS.map(function (g) {
    return REG_TABS.filter(function (t) { return t.group === g; }).map(function (t) {
      var visibleLaborRows = (d.labor || []).reduce(function (sum, x) {
        return sum + (x.details && x.details.length ? x.details.length : 1);
      }, 0);
      // Документ считается один раз: общий файл стоит у каждого своего договора.
      var nDocs = t.key === "docs" ? regDocList().length : 0;
      var n = { docs: nDocs, ctr: (d.contracts || []).length,
                emp: (d.employees || []).length, labor: visibleLaborRows,
                inflow: new Set((d.inflows || []).map(function (x) {
                  return x.contract + "|" + x.year;
                })).size,
                secret: (d.secret || []).length,
                ref: (d.reference || []).length,
                mgmt: (d.mgmt_departments || []).length,
                sub: (d.substitutions || []).length }[t.key];
      var active = regTab === t.key;
      // Внутри записи счётчики прячутся: они считают строки списка, а список
      // уже позади — на экране договора «Договоры 5» отвечает не на тот
      // вопрос, который в этот момент задают. Сами вкладки остаются: из
      // договора переходят в «Штатное расписание», не возвращаясь назад.
      // Счётчика неотнесённых нет (UI-013): документ не обязан относиться
      // к договору, и такой счётчик выдавал нормальное состояние за долг.
      // Последняя вкладка группы: после неё зазор шире — группы разделяет
      // расстояние, а не черта (при переносе ряда черта повисала в конце).
      var mine = REG_TABS.filter(function (z) { return z.group === g; });
      var glast = mine[mine.length - 1] === t && g !== REG_GROUPS[REG_GROUPS.length - 1];
      return '<button type="button" role="tab" data-rtab="' + t.key + '"' +
             (glast ? " data-glast" : "") +
             ' aria-selected="' + (active ? "true" : "false") + '"' +
             (active ? ' class="on"' : "") + ">" + esc(t.title) +
             (n && !ctrScreen ? '<span class="c"> ' + n + "</span>" : "") + "</button>";
    }).join("");
    }).join("") + "</div></div>" +
    // Действия — строкой под вкладками, как в макете: в заголовке остаётся
    // только имя раздела. «Нового договора» нет: договор заводится из файлов
    // по шифру в шапке (SOURCE-005). Окно с обязательным шифром заставляло
    // экономиста перепечатывать то, что написано в самом документе.
    (ctrScreen ? "" : regActions(regTab)) +
    "</div>";

  $("regview").innerHTML = tabsRow + body;
  // Значок чата нарисован заново — состояние ему ставит тот же markFeedTab.
  markFeedTab();
  stickPinned();
  return;
}

/* Главное действие принадлежит вкладке, а не реестру целиком. Файлы
   загружают строкой внизу таблицы документов вкладки — одной на вкладку
   (решение пользователя 18.09.2026); кнопки загрузки над таблицей нет. */
/* Вкладки, где запись создают руками, а не загружают: договор — окном выбора
   файлов, правило замещения — окном правки (SOURCE-010). */
var REG_CREATE = {
  ctr: "Создать договор",
  sub: "Добавить правило"
};

/* Надбавка 120 (правило 10): сотрудник, ставка, договор секретности, окно и
   закреплённый договор выплаты. Всё правится в ячейке; пустое окно — срок
   договора секретности, и он показан обычным значением: это даты, с которыми
   пойдёт расчёт. Незакреплённый договор выплаты — «не закреплён» бледным. Строк у сотрудника может быть
   несколько — по договору секретности на каждую. */
function secretGrid(d) {
  var ctrs = {}, emps = {};
  (d.contracts || []).forEach(function (c) { ctrs[c.code] = c; });
  (d.employees || []).forEach(function (e) { emps[e.code] = e; });
  var ed = function (x, field, text, dim, title) {
    return { v: '<span class="ed' + (dim ? " dim" : "") + '" data-secedit="' + x.id + "|" + field +
                '" tabindex="0" role="button" title="' + esc(title) + '">' + esc(text) + "</span>" };
  };
  var pct = function (rate) { return String(Math.round(rate * 10000) / 100).replace(".", ","); };
  var acc23 = function (code) {
    return /^23/.test(String((ctrs[code] || {}).account || "").replace(/\D/g, ""));
  };
  return regGrid("secret", [
    { k: "employee", label: "Сотрудник", always: true,
      val: function (x) { var e = emps[x.employee]; return e && e.fio ? e.fio : x.employee; },
      cell: function (x) {
        var e = emps[x.employee];
        return ed(x, "employee", e && e.fio ? e.fio + " (" + x.employee + ")" : x.employee, false,
                  "Сотрудник — изменить");
      } },
    { k: "rate", label: "Ставка, %", kind: "number",
      val: function (x) { return x.rate == null ? null : pct(x.rate); },
      cell: function (x) {
        return ed(x, "rate", x.rate == null ? "5" : pct(x.rate), x.rate == null, "Ставка 120 — изменить");
      } },
    { k: "contract", label: "Договор секретности",
      cell: function (x) { return ed(x, "contract", x.contract || "", false, "Договор секретности — изменить"); } },
    { k: "from", label: "Начало",
      cell: function (x) {
        var c = ctrs[x.contract] || {};
        return ed(x, "date_from", x.from || c.from || "", false,
                  x.from ? "Начало — изменить" : "По сроку договора секретности — задать своё");
      } },
    { k: "to", label: "Окончание",
      cell: function (x) {
        var c = ctrs[x.contract] || {};
        return ed(x, "date_to", x.to || c.allowance_deadline || c.to || "", false,
                  x.to ? "Окончание — изменить" : "По сроку договора секретности — задать своё");
      } },
    { k: "pay_contract", label: "Договор выплаты",
      val: function (x) { return x.pay_contract || "не закреплён"; },
      cell: function (x) {
        return ed(x, "pay_contract", x.pay_contract
                    ? x.pay_contract + (acc23(x.pay_contract) ? ", счёт 23" : "")
                    : "не закреплён", !x.pay_contract,
                  x.pay_contract && !acc23(x.pay_contract)
                    ? "Оклад в месяцы 120 — на этом же договоре"
                    : x.pay_contract ? "Счёт 23: 120 без оклада"
                    : "Договор выплаты выберет расчёт — закрепить");
      } },
    { k: "source", label: "Источник", off: true }
  ], d.secret || [], {
    rowAttr: function (x) { return 'data-sec="' + x.id + '"'; },
    bar: { add: (d.employees || []).length && (d.contracts || []).length ? "data-secadd" : "",
           addLabel: "Добавить надбавку", placeholder: "Сотрудник или договор…" }
  });
}

/* Правка ячейки 120: выбор для сотрудника и договоров, поле для ставки и дат.
   Сохраняет по выбору, Enter или уходу из поля; Esc — как было. */
function secCellEdit(span) {
  if (span.querySelector("input,select")) return;
  var parts = span.getAttribute("data-secedit").split("|"), id = +parts[0], field = parts[1];
  var d = (regData && regData.data) || {};
  var row = (d.secret || []).filter(function (x) { return x.id === id; })[0];
  if (!row) return;
  var cur = { employee: row.employee, contract: row.contract, pay_contract: row.pay_contract || "",
              rate: row.rate == null ? "" : String(Math.round(row.rate * 10000) / 100).replace(".", ","),
              date_from: row.from || "", date_to: row.to || "" }[field];
  var el;
  if (field === "employee" || field === "contract" || field === "pay_contract") {
    el = document.createElement("select");
    var opts = field === "employee"
      ? (d.employees || []).map(function (e) { return [e.code, (e.fio || e.code) + " (" + e.code + ")"]; })
      : (d.contracts || []).map(function (c) { return [c.code, c.code]; });
    if (field === "pay_contract") opts.unshift(["", "не закреплён"]);
    el.innerHTML = opts.map(function (o) {
      return '<option value="' + esc(o[0]) + '"' + (o[0] === cur ? " selected" : "") + ">" +
             esc(o[1]) + "</option>";
    }).join("");
  } else {
    el = document.createElement("input");
    el.type = field === "rate" ? "text" : "date";
    el.value = field === "rate" ? cur : dmyToIso(cur);
  }
  el.setAttribute("aria-label", span.getAttribute("title") || field);
  var done = false;
  var cancel = function () { if (!done) { done = true; renderRegistry(); } };
  var save = function () {
    if (done) return;
    var v = el.tagName === "SELECT" ? el.value : field === "rate" ? el.value.trim() : isoToDmy(el.value);
    if (v === cur) { cancel(); return; }
    done = true;
    var body = {};
    body[field] = v;
    api("/api/secret_allowances/" + id, { method: "PATCH",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function () { loadRegistry(true); tick(); }, function (err) {
        alert("Не удалось сохранить: " + (err.message || "ошибка сети"));
        renderRegistry();
      });
  };
  el.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); save(); }
    if (e.key === "Escape") { e.preventDefault(); cancel(); }
  });
  el.addEventListener("change", function () { if (el.tagName === "SELECT" || el.type === "date") save(); });
  el.addEventListener("blur", save);
  span.textContent = "";
  span.appendChild(el);
  el.focus();
}

/* Ячейка месяца на вкладке «Заработная плата по месяцам». Зарплаты в
   штатном расписании нет (решение пользователя 19.09.2026): месяц срока без
   суммы — пустая ячейка, в которую сумму вписывают; вне срока строки месяц
   пуст и не правится. */
function zpCell(x, m) {
  var v = x.months[m - 1];
  if (v == null && x.active.indexOf(m) < 0) return { v: null };
  return { v: '<span class="ed" data-zpedit="' + x.id + "|" + m +
              '" role="button" tabindex="0" aria-label="' +
              esc("Зарплата " + x.code + ", " + MONTHS[m - 1]) + '" title="' +
              (v == null ? "Зарплата не задана — указать" : "Изменить") + '">' +
              (v == null ? "" : mo(v)) + "</span>" };
}

/* Правка суммы месяца: Enter или уход из поля сохраняет, Esc — как было.
   Пустое поле снимает сумму; ноль сервер не примет. */
function zpCellEdit(span) {
  if (span.querySelector("input")) return;
  var parts = span.getAttribute("data-zpedit").split("|"), rid = +parts[0], month = +parts[1];
  var zs = ((regData && regData.data) || {}).salary || {};
  var row = (zs.rows || []).filter(function (x) { return x.id === rid; })[0];
  if (!row) return;
  var code = row.code;
  var start = row.months[month - 1] == null ? "" : String(row.months[month - 1]).replace(".", ",");
  var el = document.createElement("input");
  el.type = "text";
  el.inputMode = "decimal";
  el.value = start;
  el.setAttribute("aria-label", "Зарплата " + code + ", " + MONTHS[month - 1]);
  var done = false;
  var cancel = function () { if (!done) { done = true; renderRegistry(); } };
  var save = function () {
    if (done) return;
    var v = el.value.trim();
    if (v === start) { cancel(); return; }
    done = true;
    api("/api/salary_months", { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: code, row: rid, year: zs.year, month: month, amount: v }) })
      .then(function () { loadRegistry(true); tick(); }, function (err) {
        alert("Не удалось сохранить: " + (err.message || "ошибка сети"));
        renderRegistry();
      });
  };
  el.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); save(); }
    if (e.key === "Escape") { e.preventDefault(); cancel(); }
  });
  el.addEventListener("blur", save);
  span.textContent = "";
  span.appendChild(el);
  el.focus();
  el.select();
}

/* «Добавить сотрудника»: строка таблицы зарплаты для сотрудника, выбранного
   по ФИО из штатного расписания. Сумм у новой строки нет — месяцы берут
   зарплату штатного расписания, пока их не исправят в ячейке. */
function zpAddDialog() {
  var zs = ((regData && regData.data) || {}).salary || {};
  var list = (zs.candidates || []).slice().sort(function (a, b) {
    return String(a.fio || a.code).localeCompare(String(b.fio || b.code), "ru");
  });
  if (!list.length) { showToast("Все сотрудники штатного расписания уже в таблице"); return; }
  var wrap = document.createElement("div");
  wrap.className = "modal";
  wrap.innerHTML = '<div class="box" role="dialog" aria-modal="true" aria-labelledby="zpaddt">' +
    '<h4 id="zpaddt">Добавить сотрудника</h4>' +
    '<label class="fld">ФИО<select data-zpwho>' + list.map(function (e) {
      return '<option value="' + esc(e.code) + '">' + esc((e.fio || e.code) + " (" + e.code + ")") +
             "</option>";
    }).join("") + "</select></label>" +
    '<div class="btns"><button type="button" class="no">Отмена</button>' +
    '<button type="button" class="ok">Добавить</button></div></div>';
  document.body.appendChild(wrap);
  var release = holdFocus(wrap, function () { close(); });
  var close = function () { wrap.remove(); release(); };
  wrap.addEventListener("click", function (e) {
    if (e.target === wrap || e.target.closest(".no")) { close(); return; }
    var ok = e.target.closest(".ok");
    if (!ok) return;
    ok.disabled = true;
    api("/api/salary_rows", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: wrap.querySelector("[data-zpwho]").value, year: zs.year }) })
      .then(function () { close(); loadRegistry(true); }, function (err) {
        ok.disabled = false;
        alert("Не удалось добавить: " + (err.message || "ошибка сети"));
      });
  });
  wrap.querySelector("[data-zpwho]").focus();
}

/* Подразделение строки зарплаты (01.10.2026): список справочника с поиском,
   последним пунктом — «Новое подразделение…». У строки с суммами после выбора
   спрашивается «с какого месяца»: январь — исправить всю строку, другой месяц
   — перевод, как кадровый перевод в 1С. */
function zpDeptPick(anchor, rowId, after) {
  var d = (regData && regData.data) || {};
  var zs = d.salary || {};
  var row = (zs.rows || []).filter(function (x) { return x.id === rowId; })[0];
  if (!row) return;
  var list = d.mgmt_departments || [];
  var items = list.map(function (x) { return { label: x.name, id: x.id }; });
  items.push({ label: "Без подразделения", id: null });
  items.push({ label: "Новое подразделение…", keep: true, id: "new" });
  // pickFrom отдаёт id пункта: «new» — завести подразделение.
  pickFrom(anchor, items, "Подразделение", "Подразделений пока нет", function (picked) {
    var go = function (dept) {
      if (after) { after(dept); return; }
      var has = row.months.some(function (v) { return v != null; });
      (has && dept !== row.dept_id ? zpFromMonth(row) : Promise.resolve(1)).then(function (m) {
        if (!m) return;
        api("/api/salary_rows/" + rowId, { method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ dept: dept, from: m }) })
          .then(function () { loadRegistry(true); }, function (err) {
            alert("Не удалось изменить подразделение: " + (err.message || "ошибка сети"));
          });
      });
    };
    if (picked !== "new") { go(picked); return; }
    askText("Новое управленческое подразделение", "Наименование").then(function (name) {
      if (!name) return;
      api("/api/mgmt_departments", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: name }) })
        .then(function (r) { go(r.id); }, function (err) {
          alert("Не удалось завести подразделение: " + (err.message || "ошибка сети"));
        });
    });
  });
}

/* С какого месяца новое подразделение: январь — исправление всей строки. */
function zpFromMonth(row) {
  return new Promise(function (resolve) {
    var wrap = document.createElement("div");
    wrap.className = "modal";
    var first = row.months.findIndex(function (v) { return v != null; });
    wrap.innerHTML = '<div class="box" role="dialog" aria-modal="true" aria-labelledby="zpfromt">' +
      '<h4 id="zpfromt">' + esc(row.fio || row.code) + "</h4>" +
      '<label class="fld">С месяца<select data-zpfrom>' + MONTHS.map(function (name, i) {
        return '<option value="' + (i + 1) + '"' + (i === Math.max(first, 0) ? " selected" : "") +
          ">" + esc(name.charAt(0).toUpperCase() + name.slice(1)) + "</option>";
      }).join("") + "</select></label>" +
      '<div class="btns"><button type="button" class="no">Отмена</button>' +
      '<button type="button" class="ok">Готово</button></div></div>';
    document.body.appendChild(wrap);
    var release = holdFocus(wrap, function () { close(null); });
    var close = function (v) { wrap.remove(); release(); resolve(v); };
    wrap.addEventListener("click", function (e) {
      if (e.target === wrap || e.target.closest(".no")) { close(null); return; }
      if (e.target.closest(".ok")) close(+wrap.querySelector("[data-zpfrom]").value);
    });
    wrap.querySelector("[data-zpfrom]").focus();
  });
}

/* Меню строки справочника подразделений: переименовать, порядок, удалить. */
function openMgmtMenu(id, x, y) {
  var was = document.querySelector(".menu[data-mgmtm]");
  if (was) was.remove();
  var dep = (((regData && regData.data) || {}).mgmt_departments || [])
    .filter(function (r) { return r.id === id; })[0];
  if (!dep) return;
  var box = document.createElement("div");
  box.className = "menu";
  box.setAttribute("data-mgmtm", String(id));
  box.setAttribute("role", "menu");
  box.innerHTML =
    '<button type="button" class="mi" role="menuitem" data-mgmtren>Переименовать…</button>' +
    '<button type="button" class="mi" role="menuitem" data-mgmtpos>Порядок…</button>' +
    '<div class="msep"></div>' +
    '<button type="button" class="mi danger" role="menuitem" data-mgmtdel>Удалить</button>';
  document.body.appendChild(box);
  var r = box.getBoundingClientRect();
  box.style.left = Math.max(8, Math.min(x, window.innerWidth - r.width - 8)) + "px";
  box.style.top = Math.max(8, Math.min(y, window.innerHeight - r.height - 8)) + "px";
  box.querySelector(".mi").focus();
  var close = function () {
    box.remove();
    document.removeEventListener("mousedown", away, true);
  };
  var away = function (e) { if (!box.contains(e.target)) close(); };
  document.addEventListener("mousedown", away, true);
  box.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
  var send = function (method, body) {
    return api("/api/mgmt_departments/" + id, { method: method,
      headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined })
      .then(function () { loadRegistry(true); }, function (err) {
        alert("Не удалось: " + (err.message || "ошибка сети"));
      });
  };
  box.addEventListener("click", function (e) {
    if (e.target.closest("[data-mgmtren]")) {
      close();
      askText("Переименовать «" + dep.name + "»", "Наименование").then(function (v) {
        if (v) send("PATCH", { name: v });
      });
    } else if (e.target.closest("[data-mgmtpos]")) {
      close();
      askText("Порядок «" + dep.name + "» в отчёте", "Номер по порядку").then(function (v) {
        if (v != null) send("PATCH", { position: v.replace(/\D/g, "") || null });
      });
    } else if (e.target.closest("[data-mgmtdel]")) {
      close();
      ask("Удалить подразделение «" + dep.name + "»?", "", "Удалить").then(function (yes) {
        if (yes) send("DELETE");
      });
    }
  });
}

/* Меню строки зарплаты — правым щелчком и клавишей меню. Суммы строки
   уходят вместе с ней: месяцы снова берут зарплату штатного расписания. */
function openZpMenu(id, x, y) {
  var was = document.querySelector(".menu[data-zpm]");
  if (was) was.remove();
  var zs = ((regData && regData.data) || {}).salary || {};
  var row = (zs.rows || []).filter(function (r) { return r.id === id; })[0];
  if (!row) return;
  var box = document.createElement("div");
  box.className = "menu";
  box.setAttribute("data-zpm", String(id));
  box.setAttribute("role", "menu");
  // Вторая строка того же человека — в другом управленческом подразделении
  // (01.10.2026): «дублируешь его просто как фамилию… и по управленке
  // относишь не только к отделу №1».
  box.innerHTML = '<button type="button" class="mi" role="menuitem" data-zpmore>' +
                  "Добавить строку в другом подразделении…</button>" +
                  '<div class="msep"></div>' +
                  '<button type="button" class="mi danger" role="menuitem" data-zpdel>' +
                  "Удалить строку</button>";
  document.body.appendChild(box);
  var r = box.getBoundingClientRect();
  box.style.left = Math.max(8, Math.min(x, window.innerWidth - r.width - 8)) + "px";
  box.style.top = Math.max(8, Math.min(y, window.innerHeight - r.height - 8)) + "px";
  box.querySelector(".mi").focus();
  var close = function () {
    box.remove();
    document.removeEventListener("mousedown", away, true);
  };
  var away = function (e) { if (!box.contains(e.target)) close(); };
  document.addEventListener("mousedown", away, true);
  box.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
  box.addEventListener("click", function (e) {
    if (e.target.closest("[data-zpmore]")) {
      close();
      var anchor = $("regview").querySelector('[data-zpdept="' + id + '"]') ||
                   $("regview").querySelector('tr[data-zprow="' + id + '"]');
      zpDeptPick(anchor, id, function (dept) {
        api("/api/salary_rows", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ code: row.code, year: zs.year, dept: dept }) })
          .then(function () { loadRegistry(true); }, function (err) {
            alert("Не удалось добавить строку: " + (err.message || "ошибка сети"));
          });
      });
      return;
    }
    if (!e.target.closest("[data-zpdel]")) return;
    close();
    ask("Удалить строку " + (row.fio || row.code) + (row.dept ? " — " + row.dept : "") + "?",
        row.months.some(function (v) { return v != null; })
          ? "Суммы месяцев удалятся, посчитанные планы этого года будут " +
            "помечены к пересмотру." : "", "Удалить строку")
      .then(function (yes) {
        if (!yes) return;
        api("/api/salary_rows/" + id, { method: "DELETE" })
          .then(function () { loadRegistry(true); tick(); }, function (err) {
            alert("Не удалось удалить строку: " + (err.message || "ошибка сети"));
          });
      });
  });
}

/* Новая строка 120: первый сотрудник и первый договор — дальше правят в
   ячейках. Окно пустое (срок договора), договор выплаты выбирает расчёт. */
function secAdd() {
  var d = (regData && regData.data) || {};
  var e = (d.employees || [])[0], c = (d.contracts || [])[0];
  if (!e || !c) return;
  api("/api/secret_allowances", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ employee: e.code, contract: c.code, rate: 5 }) })
    .then(function () { loadRegistry(true); tick(); }, function (err) {
      alert("Не удалось добавить: " + (err.message || "ошибка сети"));
    });
}

/* Меню строки 120 — правым щелчком, как у правил замещения. */
function openSecMenu(id, x, y) {
  var was = document.querySelector(".menu[data-secm]");
  if (was) was.remove();
  var box = document.createElement("div");
  box.className = "menu";
  box.setAttribute("data-secm", String(id));
  box.setAttribute("role", "menu");
  box.innerHTML = '<button type="button" class="mi danger" role="menuitem" data-secdel>Удалить строку</button>';
  document.body.appendChild(box);
  var r = box.getBoundingClientRect();
  box.style.left = Math.max(8, Math.min(x, window.innerWidth - r.width - 8)) + "px";
  box.style.top = Math.max(8, Math.min(y, window.innerHeight - r.height - 8)) + "px";
  box.querySelector(".mi").focus();
  var close = function () { box.remove(); document.removeEventListener("mousedown", away, true); };
  var away = function (e) { if (!box.contains(e.target)) close(); };
  document.addEventListener("mousedown", away, true);
  box.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
  box.addEventListener("click", function (e) {
    if (!e.target.closest("[data-secdel]")) return;
    close();
    ask("Удалить строку надбавки 120?", "Посчитанные планы будут помечены к пересмотру.",
        "Удалить строку").then(function (yes) {
      if (!yes) return;
      api("/api/secret_allowances/" + id, { method: "DELETE" })
        .then(function () { loadRegistry(true); tick(); }, function (err) {
          alert("Не удалось удалить: " + (err.message || "ошибка сети"));
        });
    });
  });
}

/* Набор должностей правила: строка графы через запятую → список. */
function substList(value) {
  return String(value || "").split(",").map(function (s) { return s.trim(); })
    .filter(Boolean);
}
function substById(id) {
  return (((regData || {}).data || {}).substitutions || [])
    .filter(function (x) { return x.id === id; })[0] || null;
}

/* Окно правила замещения — как в прототипе: должность выпадающим списком,
   набор флажками с поиском (SOURCE-010). Должности — из справочника, чтобы
   правило совпало с тем, как решатель называет должность. Для самой
   должности в наборе есть «(в другом подразделении)»: так решатель читает
   вторую ставку по той же должности. Должности набора, которых в
   справочнике нет, остаются в списке отмеченными — правка их не теряет. */
function substDialog(rule) {
  var d = (regData && regData.data) || {};
  var known = (d.reference || []).map(function (x) { return x.pos; }).filter(Boolean);
  var taken = {};
  (d.substitutions || []).forEach(function (x) { taken[x.position] = x.id; });
  var chosen = {};
  substList(rule && rule.replaced_by).forEach(function (p) { chosen[p] = true; });
  var positions = known.slice();
  if (rule && positions.indexOf(rule.position) < 0) positions.unshift(rule.position);
  var start = rule ? rule.position
    : positions.filter(function (p) { return !taken[p]; })[0] || positions[0] || "";

  var wrap = document.createElement("div");
  wrap.className = "modal";
  wrap.innerHTML = '<div class="box substbox" role="dialog" aria-modal="true"' +
    ' aria-label="Правило замещения">' +
    '<div class="nhead"><h4>Правило замещения</h4>' +
      '<button type="button" class="no x" aria-label="Закрыть">×</button></div>' +
    '<div class="nbody">' +
      '<label class="sf">Должность работника<select data-spos>' +
        positions.map(function (p) {
          var busy = taken[p] && (!rule || taken[p] !== rule.id);
          return '<option value="' + esc(p) + '"' + (p === start ? " selected" : "") +
            (busy ? " disabled" : "") + ">" + esc(p) + (busy ? " — правило уже есть" : "") +
            "</option>";
        }).join("") + "</select></label>" +
      '<div class="sf">Допустимые должности для дополнительной ставки' +
        '<input type="search" data-sq placeholder="Найти должность…"' +
        ' aria-label="Поиск должности"></div>' +
      '<div class="sl" role="group" aria-label="Набор должностей"></div>' +
    "</div>" +
    '<div class="btns"><button type="button" class="no">Отмена</button>' +
      '<span class="sp"></span><button type="button" class="ok">Сохранить</button></div>' +
    "</div>";
  document.body.appendChild(wrap);
  var sel = wrap.querySelector("[data-spos]"), q = wrap.querySelector("[data-sq]");
  var list = wrap.querySelector(".sl");
  var draw = function () {
    var me = sel.value, s = q.value.trim().toLowerCase();
    var own = me + " (в другом подразделении)";
    var names = [own].concat(positions.filter(function (p) { return p !== me; }));
    Object.keys(chosen).forEach(function (p) { if (names.indexOf(p) < 0) names.push(p); });
    list.innerHTML = names.filter(function (p) {
      return !s || p.toLowerCase().indexOf(s) >= 0;
    }).map(function (p) {
      return '<label><input type="checkbox" value="' + esc(p) + '"' +
        (chosen[p] ? " checked" : "") + "><span>" + esc(p) + "</span></label>";
    }).join("") || '<div class="none">Нет совпадений</div>';
  };
  draw();
  q.addEventListener("input", draw);
  sel.addEventListener("change", draw);
  list.addEventListener("change", function (e) {
    if (e.target.type === "checkbox") chosen[e.target.value] = e.target.checked;
  });
  var release = holdFocus(wrap, function () { close(); });
  var close = function () { wrap.remove(); release(); };
  wrap.addEventListener("click", function (e) {
    if (e.target === wrap || e.target.closest(".no")) { close(); return; }
    if (!e.target.closest(".ok")) return;
    var set = Object.keys(chosen).filter(function (p) { return chosen[p]; });
    var body = { position: sel.value, replaced_by: set };
    e.target.disabled = true;
    api(rule ? "/api/substitutions/" + rule.id : "/api/substitutions", {
      method: rule ? "PATCH" : "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function () { close(); loadRegistry(true); tick(); }, function (err) {
      e.target.disabled = false;
      alert("Не удалось сохранить правило: " + (err.message || "ошибка сети"));
    });
  });
  sel.focus();
}

/* Справочник по датам (SOURCE-012). Ключ величины — как в reference.COLS. */
var REF_FIELD_KEY = { "оклад": "sal", "П2556": "p2556", "П4": "p4", "БЭП": "bep" };

function isoToday() {
  var t = new Date();
  return t.getFullYear() + "-" + ("0" + (t.getMonth() + 1)).slice(-2) + "-" +
    ("0" + t.getDate()).slice(-2);
}

/* «01.09.2026» → «2026-09-01»; пусто — "" (величина «с самого начала»). */
function refIso(dmy) { return dmyToIso(dmy) || ""; }

/* История по должности: {должность: {величина: [{from, value, doc, doc_id}]}},
   записи — по дате, ранняя («с самого начала») первой. */
function refHistory(list) {
  var out = {};
  list.forEach(function (e) {
    var p = out[e.pos] = out[e.pos] || {};
    (p[e.field] = p[e.field] || []).push(e);
  });
  Object.keys(out).forEach(function (pos) {
    Object.keys(out[pos]).forEach(function (f) {
      out[pos][f].sort(function (a, b) { return refIso(a.from) < refIso(b.from) ? -1 : 1; });
    });
  });
  return out;
}

/* Запись, действующая в день (ISO): последняя с датой не позже дня — то же
   правило, что у сервера (reference._value_at). */
function refActive(entries, day) {
  var got = null;
  (entries || []).forEach(function (e) { if (refIso(e.from) <= day) got = e; });
  return got;
}

/* Строка справочника с величинами на день. */
function refRowOn(x, hist, day) {
  var h = hist[x.pos];
  if (!h) return x;
  var y = {};
  Object.keys(x).forEach(function (k) { y[k] = x[k]; });
  Object.keys(h).forEach(function (f) {
    var e = refActive(h[f], day);
    if (e && REF_FIELD_KEY[f]) y[REF_FIELD_KEY[f]] = e.value;
  });
  return y;
}

/* Строки истории должности: строка — одна дата; величина — только в графах,
   что сменились в эту дату. Ранняя запись без даты — «до» дня перед
   следующей датой этой величины. Не действующие на день — тише (old). */
function refSubRows(x, hist, day, shown) {
  var h = hist[x.pos] || {}, rows = {}, inReg = {};
  // Документ — переходом, если он в реестре; иначе имя и «нет в реестре».
  regDocList().forEach(function (it) {
    it.e.versions.forEach(function (v) { inReg[v.id] = true; });
  });
  Object.keys(h).forEach(function (f) {
    var list = h[f], active = refActive(list, day);
    list.forEach(function (e, i) {
      var key, label;
      if (e.from) {
        key = refIso(e.from); label = "с " + e.from;
      } else {
        var next = list[i + 1] ? refIso(list[i + 1].from) : "";
        var before = next ? new Date(next + "T00:00:00") : null;
        if (before) before.setDate(before.getDate() - 1);
        label = before ? "до " + ("0" + before.getDate()).slice(-2) + "." +
          ("0" + (before.getMonth() + 1)).slice(-2) + "." + before.getFullYear() : "с самого начала";
        key = before ? before.toISOString().slice(0, 10) + "z" : "";
      }
      var r = rows[key] = rows[key] || { key: key, label: label, cells: {}, docs: [], now: false };
      r.cells[REF_FIELD_KEY[f]] = e.value;
      if (e === active) r.now = true;
      var doc = e.doc_id && inReg[e.doc_id] ? '<span class="dname" data-doc="' + esc(e.doc_id) + '" role="button"' +
          ' tabindex="0">' + esc(e.doc || "документ") + "</span>"
        : e.doc && e.doc !== "документ удалён" ? esc(e.doc) + ", нет в реестре"
        : e.doc === "документ удалён" ? "документ удалён" : "вручную";
      if (r.docs.indexOf(doc) < 0) r.docs.push(doc);
    });
  });
  return Object.keys(rows).sort().map(function (k) {
    var r = rows[k];
    return {
      cls: r.now ? "" : "old",
      attr: r.now ? 'aria-current="true"' : "",
      cells: shown.map(function (c) {
        if (c.k === "pos") return { v: '<span class="kd">' + esc(r.label) + "</span>" };
        if (c.k === "note") return { v: r.docs.join(", ") };
        if (c.k in r.cells) return { v: mo(r.cells[c.k]), cls: "n" };
        return { v: "" };
      })
    };
  });
}

/* Договоры сотрудника в штатном расписании — одним списком (пользователь
   30.09.2026). Шифры в строке — через «;» или «,». */
function empCodes(text) {
  return String(text || "").split(/[;,]/).map(function (c) { return c.trim(); })
    .filter(Boolean);
}
function regContractCodes() {
  var d = (regData && regData.data) || {};
  return (d.contracts || []).map(function (c) { return c.code; });
}
/* Договоры рабочих групп строки — разрешены приказом (пользователь
   01.10.2026: «если он есть в рабочем приказе, он должен сразу галочку
   ставить»); расчёт разрешает их так же (build_input.group_contracts). */
function empGroupContracts(x) {
  return wgOfRow(x.code).map(function (g) { return g.contract; });
}
/* Отмеченные договоры строки: разрешённые, а если их нет — все, кроме
   запрещённых; договоры рабочих групп — всегда. */
function empContractsOn(x) {
  var all = regContractCodes(), allowed = empCodes(x.allowed), no = empCodes(x.forbidden);
  var group = empGroupContracts(x);
  return all.filter(function (c) {
    return group.indexOf(c) >= 0 ||
      ((allowed.length ? allowed.indexOf(c) >= 0 : true) && no.indexOf(c) < 0);
  });
}
function empContractsText(x) {
  if (!x.allowed && !x.forbidden) return "Все договоры";
  return empContractsOn(x).join(", ");
}
function empContractsDialog(code) {
  var d = (regData && regData.data) || {};
  var x = (d.employees || []).filter(function (e) { return e.code === code; })[0];
  if (!x) return;
  var ctrs = d.contracts || [];
  var chosen = {}, group = empGroupContracts(x);
  empContractsOn(x).forEach(function (c) { chosen[c] = true; });
  var wrap = document.createElement("div");
  wrap.className = "modal";
  wrap.innerHTML = '<div class="box substbox" role="dialog" aria-modal="true"' +
    ' aria-label="Договоры сотрудника">' +
    '<div class="nhead"><h4>' + esc(x.code + (x.fio ? " · " + x.fio : "")) + "</h4>" +
      '<button type="button" class="no x" aria-label="Закрыть">×</button></div>' +
    '<div class="nbody">' +
      '<div class="sf">Разрешённые договоры' +
        '<input type="search" data-sq placeholder="Шифр или название договора…"' +
        ' aria-label="Поиск договора"></div>' +
      '<div class="sl" role="group" aria-label="Договоры"></div>' +
    "</div>" +
    '<div class="btns"><button type="button" class="no">Отмена</button>' +
      '<span class="sp"></span><button type="button" class="ok">Сохранить</button></div>' +
    "</div>";
  document.body.appendChild(wrap);
  var q = wrap.querySelector("[data-sq]"), list = wrap.querySelector(".sl");
  var ok = wrap.querySelector(".ok");
  var shown = function () {
    var s = q.value.trim().toLowerCase();
    return ctrs.filter(function (c) {
      return !s || (c.code + " " + (c.name || "")).toLowerCase().indexOf(s) >= 0;
    });
  };
  var draw = function () {
    var rows = shown();
    var n = rows.filter(function (c) { return chosen[c.code]; }).length;
    list.innerHTML = rows.length
      ? '<label data-all><input type="checkbox"' + (n === rows.length ? " checked" : "") +
          "><span>" + (q.value.trim() ? "Все найденные" : "Все договоры") + "</span></label>" +
        rows.map(function (c) {
          // Договор рабочей группы разрешён приказом: галочку не снять.
          var byOrder = group.indexOf(c.code) >= 0;
          return '<label title="' + esc(byOrder ? "Член рабочей группы договора — разрешён приказом"
                                                : c.name || "") +
            '"><input type="checkbox" value="' + esc(c.code) + '"' +
            (chosen[c.code] ? " checked" : "") + (byOrder ? " disabled" : "") + "><span>" +
            esc(c.code) + (c.name ? " · " + esc(c.name) : "") +
            (byOrder ? " · по приказу" : "") + "</span></label>";
        }).join("")
      : '<div class="none">Нет совпадений</div>';
    var all = list.querySelector("[data-all] input");
    if (all) all.indeterminate = n > 0 && n < rows.length;
    ok.disabled = !ctrs.some(function (c) { return chosen[c.code]; });
    ok.title = ok.disabled ? "Отметьте хотя бы один договор" : "";
  };
  draw();
  q.addEventListener("input", draw);
  list.addEventListener("change", function (e) {
    if (e.target.type !== "checkbox") return;
    if (e.target.closest("[data-all]")) {
      shown().forEach(function (c) {
        if (group.indexOf(c.code) < 0) chosen[c.code] = e.target.checked;
      });
    } else {
      chosen[e.target.value] = e.target.checked;
    }
    draw();
  });
  var release = holdFocus(wrap, function () { close(); });
  var close = function () { wrap.remove(); release(); };
  wrap.addEventListener("click", function (e) {
    if (e.target === wrap || e.target.closest(".no")) { close(); return; }
    if (!e.target.closest(".ok")) return;
    ok.disabled = true;
    api("/api/employees/" + encodeURIComponent(code) + "/contracts", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ checked: ctrs.map(function (c) { return c.code; })
        .filter(function (c) { return chosen[c]; }) })
    }).then(function () { close(); loadRegistry(true); tick(); }, function (err) {
      ok.disabled = false;
      alert("Не удалось сохранить договоры: " + (err.message || "ошибка сети"));
    });
  });
  q.focus();
}

/* Меню строки правила — правым щелчком и клавишей меню, как в прототипе.
   Удаление отделено чертой и спрашивает подтверждение. */
function openSubstMenu(id, x, y) {
  var was = document.querySelector(".menu[data-substm]");
  if (was) was.remove();
  var rule = substById(id);
  if (!rule) return;
  var box = document.createElement("div");
  box.className = "menu";
  box.setAttribute("data-substm", String(id));
  box.setAttribute("role", "menu");
  box.innerHTML =
    '<button type="button" class="mi" role="menuitem" data-sedit>Изменить набор должностей</button>' +
    '<div class="msep"></div>' +
    '<button type="button" class="mi danger" role="menuitem" data-sdel>Удалить правило</button>';
  document.body.appendChild(box);
  var r = box.getBoundingClientRect();
  box.style.left = Math.max(8, Math.min(x, window.innerWidth - r.width - 8)) + "px";
  box.style.top = Math.max(8, Math.min(y, window.innerHeight - r.height - 8)) + "px";
  box.querySelector(".mi").focus();
  var close = function () {
    box.remove();
    document.removeEventListener("mousedown", away, true);
  };
  var away = function (e) { if (!box.contains(e.target)) close(); };
  document.addEventListener("mousedown", away, true);
  box.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
  box.addEventListener("click", function (e) {
    if (e.target.closest("[data-sedit]")) { close(); substDialog(rule); return; }
    if (!e.target.closest("[data-sdel]")) return;
    close();
    ask("Удалить правило для «" + rule.position + "»?",
        "Посчитанные планы будут помечены к пересмотру.", "Удалить правило")
      .then(function (yes) {
        if (!yes) return;
        api("/api/substitutions/" + id, { method: "DELETE" })
          .then(function () { loadRegistry(true); tick(); }, function (err) {
            alert("Не удалось удалить правило: " + (err.message || "ошибка сети"));
          });
      });
  });
}

/* Версия документа, которую берёт открытый план. Каталог реестра грузится
   «без плана» и всегда показывает последнюю — выбранную плану версию знает
   его собственный каталог (regData.planBasis). */
function usedVersion(root) {
  var b = regData && regData.planBasis;
  if (!b) return null;
  var found = null;
  var look = function (list) {
    (list || []).forEach(function (e) { if (e.root === root) found = e.used; });
  };
  Object.keys(b.contracts || {}).forEach(function (k) { look(b.contracts[k]); });
  ["unlinked", "staff", "salary", "facts", "reference", "substitutions"].forEach(function (g) {
    look(b[g]);
  });
  return found;
}

/* Выбор версии — в графе «Версия» у самого документа, а не кнопкой вкладки
   (решение пользователя 18.09.2026). Версия выбирается для открытого плана
   (basis.set_version): разные варианты плана могут считаться по разным
   версиям. Список — только когда версий больше одной; без открытого плана он
   отключён и говорит почему. Версия без сохранённых строк выбрана быть не
   может и помечена. */
function versionPick(e, top) {
  var used = usedVersion(e.root) || e.used || top.id;
  return '<select data-docuse="' + e.root + '" aria-label="Версия «' + esc(top.name) +
    '» в расчёте"' + (caseId ? "" : ' disabled title="Версию выбирают для плана — откройте план"') +
    ">" + e.versions.map(function (v) {
      return '<option value="' + v.id + '"' + (v.id === used ? " selected" : "") +
        (v.selectable ? "" : " disabled") + ">" + v.version +
        (v.selectable ? "" : " — строк нет") + "</option>";
    }).join("") + "</select>";
}

/* Строка действия вкладки над рамкой таблицы. Органы вида (поиск, графы)
   живут в рамке самой таблицы (ctrTools), UI-014. */
function regActions(tab) {
  // Органы таблиц — в шапке их панели (tableBar); над таблицами ничего.
  return "";
}


/* «Итого» по ФОТ — сумма с сервера (UI-017). Пока список сужен поиском или
   отбором, серверный итог по всему реестру не совпал бы со строками, а
   складывать на странице нельзя — строки нет. */
function ctrTotalRow(shown, sum) {
  var at = shown.map(ctrColKey).indexOf("fund");
  if (at < 0 || sum == null || ctrQuery.trim() || Object.keys(ctrFilter).length) return [];
  var row = [{ v: "Итого", cs: at || 1 }];
  if (at === 0) row = [];
  row.push({ v: mo(sum), cls: "n" });
  for (var i = at + 1; i < shown.length; i++) row.push({ v: "" });
  return [row];
}

/* Строка органов вида под вкладками: поиск слева, «Графы» справа (UI-014).
   Вкладки её не сдвигают: в строке вкладок ничего, кроме вкладок, нет. */
function ctrTools(n) {
  var hidden = CTR_COLS.filter(function (c) {
    return !c.always && !ctrShown[ctrColKey(c)];
  }).length;
  // Кнопка синеет, только когда состав отличается от принятого по умолчанию:
  // по умолчанию часть граф скрыта всегда, и постоянная подсветка ничего бы
  // не сообщала.
  var changed = CTR_COLS.some(function (c) {
    return !c.always && !!ctrShown[ctrColKey(c)] !== !!c.def;
  });
  // «+» — тихий значок первым в строке, как «Планы +» в левой колонке; поиск —
  // лупа, которая раскрывается в поле по нажатию и остаётся раскрытой, пока в
  // ней есть текст (решение пользователя 19.09.2026).
  // «Графы» синеет, когда состав граф изменён, — вместо счётчика рядом.
  return tableBar({ add: "data-newctr", addLabel: "Добавить договор", key: "ctr",
    placeholder: "Шифр или наименование…",
    cols: 'class="' + (changed ? "on" : "") + '" data-cols aria-haspopup="true"' +
      ' aria-expanded="false" aria-label="Графы" title="' +
      (hidden ? "Графы: скрыто " + hidden : "Графы") + '"' });
}

/* Каталог документов: у договора — все его файлы, у штатного расписания,
   справочника и правил замещения — свои. Версии одного документа лежат
   цепочкой: последняя строкой, прежние раскрываются под ней. Какая версия
   идёт в расчёт, решает план на своей вкладке «Договоры» (SOURCE-003):
   библиотека хранит все, выбирать в ней нечего. */
function catalogBlock(title, entries, contract, extra, unlinked, noUpload, rel) {
  var rows = [], cls = [], attrs = [];
  var catQ = "cat:" + (contract || title || "docs");
  entries = entries.filter(function (e) {
    return tbMatch(catQ, e.versions.map(function (v) { return v.name; }).join(" "));
  });
  entries.forEach(function (e) {
    var open = !!versOpen[e.root], older = e.versions.length - 1;
    // rel — графа «относится к» вкладки «Документы» (UI-013): {корень: {v, unlinked}}.
    var r = rel && rel[e.root];
    e.versions.forEach(function (v, i) {
      var top = i === 0;
      if (!top && !open) return;
      var s = docStatus(v.state);
      // Редакции раскрываются значком у своего документа, а не подписью
       // «ещё N версий»: в макете это одна колонка раскрытия, и по ней видно,
       // у каких документов редакции вообще есть.
      var more = top && older
        ? '<button type="button" class="vers" data-vers="' + e.root + '"' +
          ' aria-expanded="' + (open ? "true" : "false") + '" aria-label="' +
          (open ? "Скрыть версии" : "Показать версии") + '">' +
          (open ? "▾" : "▸") + "</button>"
        : "";
      // Перечня договоров под именем общего файла нет (решение пользователя
      // 18.09.2026): внутри договора он шум, на вкладке «Документы» он в
      // графе «относится к».
      // Источник величины справочника, чьего файла в реестре нет, — строка
      // без перехода и меню: открывать и удалять нечего.
      var src = v.sources && v.sources.length
        ? "источник: " + v.sources.join(", ") : "";
      var row = [
        { v: v.ghost ? "<span>" + esc(v.name) + "</span>"
             : more + '<span class="dname" data-doc="' + v.id + '" role="button" tabindex="0">' +
               esc(v.name) + "</span>" },
        // «Версия», а не «редакция»: штатное расписание, договоры, РКМ — это
        // файлы, которые загружают заново, а не статья, которую правят
        // (решение пользователя 18.09.2026). Номер — под заголовком графы.
        { v: v.ghost ? "—" : top && older ? versionPick(e, v) : String(v.version), cls: "n" },
        { v: esc(v.uploaded || "—"), cls: "dt" },
        { v: v.ghost ? '<span class="st warn">нет в реестре</span>'
             : '<span class="st ' + s.cls + '">' + esc(s.text) + "</span>", cls: "status" },
        // Счёт кода по строкам со ссылкой на документ, не пересказ модели;
        // для какой величины справочника документ — отдельной меткой (UI-015).
        { v: wgDocResult(v.id) ||
              (producedAny(v) + (src ? (producedAny(v) ? " " : "") + '<span class="kd">' +
              esc(src) + "</span>" : "")) || "—" },
        { v: v.ghost ? "" : rowMenu("data-docmenu", v.id), cls: "act" }];
      rows.push(row);
      cls.push(top ? "" : "old");
      // «Указать договор…» в меню — по строке: в общей таблице группы нет.
      attrs.push(r && r.unlinked ? 'data-unlinked="1"' : "");
    });
  });
  // Добавление — не кнопка над таблицей, а строка в её конце: место, куда
  // смотрят, когда ищут «куда положить ещё один файл». Она же принимает
  // перетаскивание, она же открывает выбор из папки (Jira, GitHub, 1С).
  // Загрузка — «+» в панели таблицы; пунктирной строки внизу нет (решение
  // пользователя 19.09.2026), перетаскивание работает на всю вкладку.
  var catKey = "cat:" + (contract || title || "docs");
  var up = "";
  // Раскрыть все цепочки разом, а не по одной (макет Contract.dc.html):
  // при сравнении редакций разных документов их открывают все.
  var chains = entries.filter(function (e) { return e.versions.length > 1; });
  var allOpen = chains.length > 0 && chains.every(function (e) { return !!versOpen[e.root]; });
  var showAll = chains.length
    ? '<label class="fld"><input type="checkbox" data-allvers' +
      (allOpen ? " checked" : "") + "><span>Показывать версии</span></label>"
    : "";
  // Документам без договора меню даёт «Указать договор…» — единственный
  // ручной ввод шифра: когда в самом документе его нет (SOURCE-005).
  // Ширины по самому длинному значению: дата — 127 px, «Ждет подтверждения» —
  // 171 px; в 124 и 132 дата переносилась, а состояние вылезало в соседнюю графу.
  var nums = [], k = 0;
  cls.forEach(function (c) { nums.push(c === "old" ? "" : String(++k)); });
  // Графы — документ, версия, дата и время загрузки, состояние, результат
  // (решение пользователя 18.09.2026); к чему документ относится — в его меню.
  var head = ["документ", { t: "версия" }, "дата и время загрузки", "состояние",
              "результат обработки", ""];
  var widths = ["", "76px", "176px", "172px", "", "44px"];
  // Заголовка у вкладки «Документы» нет: его имя уже на вкладке (UI-002).
  var cath = title || showAll
    ? '<div class="cath">' + (title ? "<h3>" + esc(title) + "</h3>" : "") + showAll + "</div>" : "";
  var bar = noUpload ? "" : tableBar({ upload: contract || "", addLabel: "Загрузить документы",
    key: catKey, placeholder: "Имя документа…" });
  // Пустая таблица документов — шапкой граф, как все таблицы (29.09.2026).
  // Поиск без совпадений говорит об этом словами: это ответ на поиск.
  if (!rows.length && (tbQuery[catKey] || "").trim()) {
    return '<div class="cat"' + (unlinked ? ' data-unlinked="1"' : "") + ">" + cath +
      (extra || "") + '<div class="tpanel">' + bar +
      '<div class="none">Ничего не найдено</div></div></div>';
  }
  return '<div class="cat"' + (unlinked ? ' data-unlinked="1"' : "") + ">" + cath +
    (extra || "") + '<div class="tpanel">' + bar + table(head, rows, "",
          { plain: true, widths: widths, emptyHead: true,
            // Номер у документа, не у его прежних версий под ним.
            numOf: function (i) { return nums[i]; },
            rowCls: function (i) { return cls[i]; },
            rowAttr: function (i) { return attrs[i]; } }) + "</div>" + up + "</div>";
}

/* Новая версия документа: один файл встаёт следующей версией в его цепочку. */
function pickVersion(id) {
  var inp = document.createElement("input");
  inp.type = "file";
  inp.accept = UPLOAD_ACCEPT;
  inp.hidden = true;
  document.body.appendChild(inp);
  inp.addEventListener("change", function () {
    var list = Array.prototype.slice.call(inp.files);
    inp.remove();
    if (!list.length) return;
    uploadContract = ctrScreen || null;
    uploadVersionOf = +id;
    sendFiles(list.slice(0, 1));
  });
  inp.click();
}

/* Область загрузки: и кнопка, и место, куда бросают файлы. Одна на каталог —
   или одна на весь экран, когда каталогов на нём много. */
function uploadZone(contract) {
  return '<label class="regup line" data-contract="' + esc(contract || "") +
    '"><input type="file" multiple accept="' + UPLOAD_ACCEPT + '" hidden>' +
    '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor"' +
    ' stroke-width="1.5" aria-hidden="true"><path d="M8 10.6V3.2"></path>' +
    '<path d="M5.4 5.6 8 3l2.6 2.6"></path><path d="M3 10.6v2.4h10v-2.4"></path></svg>' +
    // Формулировка как у Jira («Drop files to attach, or browse») и GitHub:
    // называются оба способа, и у «выбрать» есть дополнение — иначе фраза
    // обрывается на глаголе.
    "<span>Перетащите файлы сюда или выберите на компьютере</span></label>";
}

/* Виды выплат — известный набор: столько же граф разворачивает вход решателя
   из одной графы договора (app/build_input.py). Поэтому здесь флажки, а не
   строка через запятую: печатать «124» руками и гадать про пробел не нужно. */
var PAY_KINDS = ["оклад", "120", "122", "124", "152", "приказ"];

/* Чем задаётся условие: у даты — календарь, у разрешения — да/нет, у видов
   выплат — флажки. Одно текстовое поле на все семь полей писать было проще,
   но оно заставляло экономиста печатать «01.06.2026» и угадывать, как
   пишется «да». */
var OV_KIND = { "начало": "date", "окончание": "date", "оклад до": "date",
                "надбавки до": "date", "основное": "yesno",
                "совместительство": "yesno", "разрешенные выплаты": "kinds",
                "месяцы без выплат": "months" };
/* Подпись условия на экране: имя поля в данных остаётся прежним. */
var OV_LABEL = { "разрешенные выплаты": "Разрешённые выплаты" };
/* Месяцы без выплат (01.10.2026) — лентой месяцев в таблице договоров плана. */
var MONTH_SHORT = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт",
                   "ноя", "дек"];

function dmyToIso(t) {
  var m = /^(\d{1,2})\.(\d{1,2})\.(\d{4})$/.exec(String(t == null ? "" : t).trim());
  return m ? m[3] + "-" + ("0" + m[2]).slice(-2) + "-" + ("0" + m[1]).slice(-2) : "";
}

function isoToDmy(t) {
  var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(t == null ? "" : t).trim());
  return m ? m[3] + "." + m[2] + "." + m[1] : "";
}

function ovControl(name, was, set, id, canPick) {
  var off = canPick ? "" : " disabled";
  var kind = OV_KIND[name] || "text";
  if (kind === "date") {
    // Поле показывает дату, с которой пойдёт расчёт: правку плана, иначе дату
    // договора. После сброса поле не пустеет (пользователь 30.09.2026: «при
    // нажатии на стрелку дата сбрасывается вообще, никакая не указана»), а
    // дата, равная договорной, правкой не считается (data-was).
    return '<input type="date" id="' + id + '" class="ovf" data-ov="' + esc(name) +
      '" data-was="' + esc(was || "") + '" value="' + esc(dmyToIso(set || was)) + '"' + off + ">";
  }
  if (kind === "yesno") {
    // Флажком, как «да/нет» договора (пользователь 29.09.2026: «если
    // заменишь для да/нет выпадающий список, то тут тоже надо»). Флажок
    // показывает, что пойдёт в расчёт: правку плана, иначе значение
    // договора, иначе «да» — пустое основное место и совместительство расчёт
    // читает как «да». Щелчок пишет правку; «как в договоре» — значком сброса.
    var eff = String(set || was || "да").trim().toLowerCase();
    // Подписи-<label> у флажка нет: щелчок по названию условия в соседней
    // графе молча переключил бы правку. Имя — для чтеца экрана.
    return '<input type="checkbox" id="' + id + '" data-ov="' + esc(name) + '"' +
      ' data-was="' + esc(String(was || "да").trim().toLowerCase()) + '"' +
      (eff === "да" ? " checked" : "") + ' aria-label="' + esc(OV_LABEL[name] || cap(name)) +
      '"' + off + ">";
  }
  if (kind === "kinds") {
    // Пусто в договоре значит «не сказано» и разрешает все виды: флажки
    // показывают то же, иначе «как в договоре» выглядело бы как запрет всего.
    var chosen = set != null && set !== "" ? String(set) : (was == null || was === "" ? null : String(was));
    var on = chosen === null ? null : chosen.toLowerCase().split(/[,;]/).map(function (s) { return s.trim(); });
    return '<span class="ovkinds" data-ov="' + esc(name) + '" role="group" aria-label="' +
      esc(name) + '">' + PAY_KINDS.map(function (k, j) {
        var kid = id + "k" + j;
        return '<label class="fld" for="' + kid + '"><input type="checkbox" id="' + kid +
          '" value="' + esc(k) + '"' + (on === null || on.indexOf(k) >= 0 ? " checked" : "") +
          off + ">" + esc(k) + "</label>";
      }).join("") + "</span>";
  }
  return '<input type="text" id="' + id + '" class="ovf" data-ov="' + esc(name) +
    '" value="' + esc(set || "") + '" placeholder="как в договоре…" autocomplete="off"' +
    off + ">";
}

/* Панель договора справа от таблицы: шифр и наименование в шапке, ниже —
   условия и закрепления. Появляется, только когда её открывают из закрытого
   состояния; смена договора — мгновенно, без анимации. ФОТ и срок в шапке не
   повторяются — они в строке таблицы (UI-002). */
function ctrPanel(x, bs, reveal) {
  return '<aside class="ctrpanel' + (reveal ? " reveal" : "") + '" id="ctrpanel" data-code="' +
    esc(x.code) + '" aria-labelledby="ctrpanel-h"><div class="cph"><div class="cpt">' +
    '<div class="code">' + esc(x.code) + '</div><h2 id="ctrpanel-h">' + esc(x.name || x.code) +
    "</h2></div>" + '<button type="button" class="iconbtn x" data-pctr-close title="Закрыть"' +
    ' aria-label="Закрыть условия договора">' + svg16(ICON_CLOSE) + "</button></div>" +
    conditionsBlock(x, bs) + "</aside>";
}

/* Условия договора в этом плане (SOURCE-006): значение договора и правка
   плана рядом, а не вместо друг друга — экономист должен видеть, что изменил
   именно он. «Как в договоре» снимает правку. Ниже — версия документа, если
   их несколько: по какой считает этот план (SOURCE-003). */
function conditionsBlock(x, bs) {
  // Месяцы без выплат — лентой в таблице договоров (pmonthsCell), не здесь.
  var fields = (bs.overridable || []).filter(function (n) { return OV_KIND[n] !== "months"; });
  var now = (bs.overrides || {})[x.code] || {};
  var base = { "разрешенные выплаты": x.kinds, "начало": x.from, "окончание": x.to,
               "основное": x.allow_main, "совместительство": x.allow_part,
               "оклад до": x.salary_deadline, "надбавки до": x.allowance_deadline };
  // Таблица «условие — в договоре — в плане» (решение пользователя 19.09.2026:
  // прежние два столбца с подписями над полями читались плохо). Значение
  // договора — своей графой, правка плана — своей; значок сброса у правки
  // возвращает «как в договоре» (пользователь 29.09.2026: «может убрать явную
  // кнопку и сделать кнопку сброса?»). Изменённое в плане выделено строкой.
  var rows = fields.map(function (name, i) {
    var was = base[name], set = now[name], id = "ovf" + i;
    var shown = OV_LABEL[name] || cap(name);
    var label = OV_KIND[name] === "kinds" || OV_KIND[name] === "yesno" ? esc(shown)
      : '<label for="' + id + '">' + esc(shown) + "</label>";
    // Договор в реестре изменился после правки условия (30.09.2026): значение
    // договора — цветом внимания, подсказка — каким оно было при правке.
    // Решает экономист: оставить правку или вернуть значком сброса.
    var snap = ((bs.overrides_base || {})[x.code] || {})[name];
    var drift = set && snap != null && String(snap) !== String(was == null ? "" : was);
    return "<tr" + (set ? ' class="ovon"' : "") + "><td>" + label + "</td>" +
      '<td class="kd"' + (drift ? ' data-stale title="' + esc("При правке в договоре было " +
        (snap === "" ? "не задано" : snap) + " — договор в реестре изменился") + '"' : "") + ">" +
      esc(was == null || was === "" ? "не задано" : String(was)) + "</td>" +
      '<td><span class="av">' + ovControl(name, was, set, id, true) +
      (set ? '<button type="button" class="iconbtn" data-ovclear="' + esc(name) +
             '" title="Как в договоре" aria-label="Вернуть как в договоре: ' + esc(shown) + '">' +
             svg16(ICON_RESET) + "</button>" : "") + "</span></td></tr>";
  }).join("");
  var versions = ((bs.contracts || {})[x.code] || []).filter(function (e) {
    return e.versions.length > 1;
  }).map(function (e, j) {
    var id = "use" + j;
    var used = e.versions.filter(function (v) { return v.id === e.used; })[0];
    return '<tr><td><label for="' + id + '">' + esc(e.versions[0].name) + "</label></td>" +
      '<td class="kd">' + (used ? "версия " + used.version : "") + "</td><td>" +
      '<select id="' + id + '" class="ovf" data-use="' + e.root + '">' +
      e.versions.map(function (v) {
        return '<option value="' + v.id + '"' + (e.used === v.id ? " selected" : "") +
               (v.selectable ? "" : " disabled") + ">версия " + v.version +
               " · " + esc(v.uploaded || "") + (v.selectable ? "" : " — строк нет") +
               "</option>";
      }).join("") + "</select></td></tr>";
  }).join("");
  return '<div class="ovprops"><table class="ovtable"><thead><tr><th>Условие</th>' +
    "<th>В договоре</th><th>В плане</th></tr></thead><tbody>" + rows + versions +
    "</tbody></table>" + fixesBlock(x, now) + "</div>";
}

/* Закрепления сотрудников за договором в этом плане (решение пользователя
   19.09.2026): кто, в какие месяцы, каким видом выплаты. Вид можно не
   задавать — тогда в каждом месяце срока с договора хотя бы одна выплата
   любого вида. Тот же список, что заполняет чат. */
var FIX_KINDS = ["оклад", "120", "122", "124", "152", "приказ"];
var MONTH_NAMES = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
                   "август", "сентябрь", "октябрь", "ноябрь", "декабрь"];
var MONTH_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
                 "августа", "сентября", "октября", "ноября", "декабря"];
var MONTH_SHORT = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт",
                   "ноя", "дек"];
/* fixBusy — ключи закреплений, чья правка сейчас сохраняется. */
var fixBusy = {};

/* Строка таблицы — сотрудник на договоре: виды выплат перечислены в одной
   ячейке (решение пользователя 21.09.2026: «множественный выбор»). Ключ
   строки — пара и срок: записи пары с разным сроком (их ставит чат) остаются
   разными строками, иначе один срок пришлось бы выдумать. */
function fixKey(f) {
  return f["сотрудник"] + "|" + f["договор"] + "|" + f["с"] + "-" + f["по"];
}

/* Записи основания — в строки таблицы: вид «любой» виден пустым списком
   видов, признак «только» — «вся зарплата с этого договора». */
function fixRows(code) {
  var by = {}, out = [];
  ((vbasis && vbasis.fixes) || []).forEach(function (f) {
    if (code && f["договор"] !== code) return;
    var key = fixKey(f), r = by[key];
    if (!r) {
      r = by[key] = { key: key, "сотрудник": f["сотрудник"], "фио": f["фио"],
                      "договор": f["договор"], "с": f["с"], "по": f["по"],
                      kinds: [], "сумма": null, "только": false };
      out.push(r);
    }
    if (f["вид"] !== "любой" && r.kinds.indexOf(f["вид"]) < 0) r.kinds.push(f["вид"]);
    if (f["только"]) r["только"] = true;
    if (f["сумма"]) r["сумма"] = f["сумма"];
  });
  return out;
}

function fixFind(key) {
  return fixRows(null).filter(function (f) { return f.key === key; })[0];
}

function fixYear() {
  var c = casesCache.filter(function (p) { return p.id === caseId; })[0];
  return c && c.year;
}

/* Месяцы года плана, в которые договор платит, — по сроку договора с правкой
   плана. Вне их закрепление не выполнить: клетка серая и не выбирается. */
function fixActive(x, now) {
  var year = fixYear();
  var at = function (text) {
    var p = String(text || "").split(".");
    return p.length === 3 && +p[1] && +p[2] ? +p[2] * 12 + (+p[1] - 1) : null;
  };
  var a = at((now && now["начало"]) || x.from), b = at((now && now["окончание"]) || x.to);
  return MONTH_NAMES.map(function (name, i) {
    var k = year * 12 + i;
    return !year || ((a === null || k >= a) && (b === null || k <= b));
  });
}

/* Месяцы без выплат договора в этом плане: [1–12]. */
function pmonthsShut(code, bs) {
  var now = ((bs && bs.overrides) || {})[code] || {};
  return String(now["месяцы без выплат"] || "").split(/[,;]/)
    .map(function (t) { return +t.trim(); })
    .filter(function (m) { return m >= 1 && m <= 12; });
}

/* Лента месяцев договора: в сроке — кнопка «платит / без выплат», вне
   срока — пустая клетка. Месяц называет подсказка и имя для чтеца экрана. */
function pmonthsCell(x, bs, excluded) {
  var now = ((bs && bs.overrides) || {})[x.code] || {};
  var on = fixActive(x, now), shut = pmonthsShut(x.code, bs);
  // После последней даты выплат (оклад до, надбавки до) договор не платит —
  // такие месяцы тоже вне срока.
  var year = fixYear(), last = null;
  [now["оклад до"] || x.salary_deadline, now["надбавки до"] || x.allowance_deadline]
    .forEach(function (t) {
      var d = String(t || "").split(".");
      if (d.length === 3 && +d[2] && +d[1]) {
        var k = +d[2] * 12 + (+d[1] - 1);
        last = last === null ? k : Math.max(last, k);
      }
    });
  if (year && last !== null) on = on.map(function (v, i) { return v && year * 12 + i <= last; });
  return '<span data-pmonths role="group" aria-label="Выплаты по месяцам: ' + esc(x.code) + '">' +
    MONTH_NAMES.map(function (name, i) {
      var m = i + 1, label = cap(name);
      if (!on[i]) return '<span data-pmout title="' + esc(label + ": вне срока договора") + '"></span>';
      var closed = shut.indexOf(m) >= 0;
      return '<button type="button" data-pmon="' + esc(x.code) + '" data-m="' + m + '"' +
        ' aria-pressed="' + closed + '" title="' + esc(label + (closed
          ? ": без выплат — щелчок вернёт выплаты" : ": платит — щелчок закроет месяц")) +
        '" aria-label="' + esc(label + (closed ? ", без выплат" : ", платит")) + '"' +
        (excluded ? " disabled" : "") + "></button>";
    }).join("") + "</span>";
}

/* Срок нового закрепления — все месяцы договора в году плана. */
function fixDefaultSpan(on) {
  var a = on.indexOf(true), b = on.lastIndexOf(true);
  return a < 0 ? [1, 12] : [a + 1, b + 1];
}

/* Срок словами, для подсказки и диктора: «с 1 марта по 31 мая 2026
   включительно» (пользователь 19.09.2026: «сроки включительно или нет?»). */
function fixSpanWords(a, b) {
  var year = fixYear() || new Date().getFullYear();
  return "с 1 " + MONTH_GEN[a - 1] + " по " + new Date(year, b, 0).getDate() + " " +
         MONTH_GEN[b - 1] + " " + year + " включительно";
}

function fixesBlock(x, now) {
  // Закрепления — обычной таблицей (решение пользователя 20.09.2026: «мне не
  // нравится идея выделения квадратиков с месяцами, я хочу чтобы это была
  // традиционная таблица»): сотрудник, начало, окончание, выплата, сумма в
  // месяц. Значение правится щелчком по ячейке на месте, как в таблицах
  // реестра. Строки — по ФИО: сервер дописывает изменённое закрепление в
  // конец списка, и после правки строка уезжала из-под курсора.
  var list = fixRows(x.code).sort(function (a, b) {
    return String(a["фио"] || a["сотрудник"]).localeCompare(String(b["фио"] || b["сотрудник"]), "ru") ||
           a["с"] - b["с"];
  });
  var on = fixActive(x, now), year = fixYear() || "";
  // Срок договора в году плана с правками плана: месяцы закрепления
  // выбираются только в нём, а срок, выбранный раньше шире, показан и уходит
  // в расчёт урезанным до него (пользователь 29.09.2026: «чтоб можно было
  // выбрать месяцы в период договора»; вход урезает main._apply_plan_settings).
  var lo = on.indexOf(true) + 1, hi = on.lastIndexOf(true) + 1;
  if (!lo) { lo = 1; hi = 12; }
  // Виды, которые договор платит в этом плане: правка «Разрешённых выплат»
  // видна и здесь — закрепление снятым видом не выполнить.
  var allow = String(now["разрешенные выплаты"] || x.kinds || "").toLowerCase().trim();
  // Графа суммы — только когда у кого-то назван вид: у «любой» суммы нет, и
  // пустая графа лишь отнимала место в узкой панели.
  var sums = list.some(function (f) { return f.kinds.length > 0; });
  // Члены рабочей группы договора (пользователь 30.09.2026: «а что с
  // сотрудниками, которые в группе? их же тоже нужно отобразить, просто не
  // дать удалить»): закрепления из приказа — без корзины и без правки, состав
  // меняется приказом. Срок — срок группы в году плана в пределах срока
  // договора; то же уходит в расчёт (main._apply_plan_settings). Не
  // сопоставленные со штатным расписанием и группы вне года плана — тише,
  // причина в подсказке: в расчёт они не идут.
  // Сотрудник, снятый с расчёта плана на вкладке «Штатное расписание»:
  // его закрепления хранятся, но в расчёт не идут — строки тише, причина в
  // подсказке (main._apply_plan_settings).
  var staffOff = staffOffMap();
  // Выплату члену группы можно задать, снять его нельзя (пользователь
  // 30.09.2026: «какую выплату ему дать, решает расчёт — это можно задать,
  // только удалить нельзя, если не задано — то по возможности ВСЮ»). Не
  // задано — «вся, по возможности»: расчёт стремится платить всё с этого
  // договора (стадия после БЭП), остальное — с других договоров плана.
  var wgPay = (vbasis && vbasis.workgroup_pay) || {};
  var groupRows = fixGroupRows(x.code, lo, hi).map(function (g) {
    var out = g.employee && staffOff[g.employee];
    var choice = (g.employee && wgPay[x.code + "|" + g.employee]) || {};
    var gk = choice["виды"] || [];
    var gText = choice["только"] ? "вся зарплата" : gk.length ? gk.join(", ") : "вся, по возможности";
    var gTip = choice["только"]
      ? "Все выплаты сотрудника идут с этого договора: платить ему с прочих договоров плана запрещено. Изменить"
      : gk.length ? "Изменить"
      : "По умолчанию для рабочей группы: расчёт старается платить всю зарплату с этого договора, остальное — с других договоров плана. Изменить";
    var gLive = g.employee && g.span && !out;
    var gCell = gLive
      ? '<span class="ed" data-wgkind="' + esc(g.employee) + '" data-who="' + esc(g.who) +
        '" role="button" tabindex="0" aria-label="' + esc("Выплата: " + g.who) + '" title="' +
        esc(gTip) + '">' + esc(gText) + "</span>"
      : esc(gText);
    var tip = "Член рабочей группы по приказу № " + (g.number || "?") + " от " + (g.date || "?") +
      (!g.employee ? ". Нет в штатном расписании — в расчёт не идёт; строку выбирают во вкладке " +
                     "«Рабочая группа» договора"
       : out ? ". Сотрудник вне расчёта этого плана"
       : !g.span ? ". Срок группы по приказу в году плана не действует"
       : ". Снять нельзя — состав меняется приказом");
    return "<tr data-wgpin" + (g.employee && g.span && !out ? "" : " data-off") + ' title="' + esc(tip) +
      '"><td>' + esc(g.who) + "</td><td>" + (g.span ? fixMonthText(g.span[0]) : "") +
      "</td><td>" + (g.span ? fixMonthText(g.span[1]) : "") + "</td><td>" + gCell + "</td>" +
      (sums ? '<td class="n">' + (gk.length === 1 && choice["сумма"] ? mo(choice["сумма"]) : "") +
              "</td>" : "") + '<td class="act"></td></tr>';
  }).join("");
  var rows = list.map(function (f) {
    var key = fixKey(f), who = f["фио"] || f["сотрудник"];
    // Срок, который действует: пересечение выбранного с месяцами договора.
    // Пересечения нет — закрепление не действует: месяцы тише, причина — в
    // подсказке, без приписки в ячейке.
    var a = Math.max(f["с"], lo), b = Math.min(f["по"], hi), none = a > b;
    var gone = !!staffOff[f["сотрудник"]];
    if (none) { a = f["с"]; b = f["по"]; }
    var words = cap(fixSpanWords(a, b)) +
      (gone ? ". Сотрудник вне расчёта этого плана — закрепление не действует"
       : none ? ". Договор в эти месяцы не платит — закрепление не действует" : "");
    var cell = function (field, label, tip, inner, cls) {
      return '<span class="ed' + (cls ? " " + cls : "") + '" data-fixcell="' + field +
        '" role="button" tabindex="0"' +
        ' aria-label="' + esc(label + ": " + who) + '" title="' + esc(tip) + '">' + inner + "</span>";
    };
    // Месяц и год числами (пользователь 20.09.2026: «даты хочу чтоб было
    // месяц и год», «можно 11.26»). Словами срок — в подсказке. Приписки
    // «вне срока договора» нет (пользователь 29.09.2026: «зачем ты добавил
    // сюда подпись?»): вне срока месяц не выбрать.
    var month = function (field, m) {
      return cell(field, field === "from" ? "Начало" : "Окончание", words,
                  fixMonthText(m), none ? "dim" : "");
    };
    // «Вся зарплата» — закрепление любым видом и запрет платить этому
    // человеку с прочих договоров плана (решение пользователя 21.09.2026).
    var one = f.kinds.length === 1;
    // Вид, который договор больше не платит (его сняли в «Разрешённых
    // выплатах» плана), назван словом, а не только несбывшимся расчётом
    // (UI-016): такое закрепление не выполнить.
    var bad = f.kinds.filter(function (k) { return allow && allow.indexOf(k) < 0; });
    var kindText = (f["только"] ? "вся зарплата"
      : f.kinds.length ? esc(f.kinds.join(", ")) : "любая") +
      (bad.length ? ' <span class="st warn">договор не платит: ' +
       esc(bad.join(", ")) + "</span>" : "");
    var kindTip = f["только"]
      ? "Все выплаты сотрудника идут с этого договора: платить ему с прочих договоров плана запрещено. Изменить"
      : "Изменить";
    return '<tr data-fixrow="' + esc(key) + '"' +
      (gone ? ' data-off title="Сотрудник вне расчёта этого плана — закрепление не действует"' : "") +
      "><td>" + esc(who) + "</td><td>" +
      month("from", a) + "</td><td>" + month("to", b) + "</td><td>" +
      cell("kind", "Выплата", kindTip, kindText) + "</td>" +
      // Сумма бывает только у одного названного вида: у «любой» и у
      // нескольких видов ячейка пуста, но говорит почему (пользователь
      // 20.09.2026: «сумму в месяц не даёт редачить»).
      (sums ? '<td class="n"' + (one ? "" : ' title="' + (f.kinds.length
                ? "Сумма задаётся, когда назван один вид выплаты"
                : "Сумма задаётся у названного вида выплаты: выберите вид в графе «Выплата»") +
                '"') + ">" + (one ? cell("sum", "Сумма в месяц",
                f["сумма"] ? "Изменить" : "Сумма не задана — указать",
                f["сумма"] ? mo(f["сумма"]) : "") : "") + "</td>" : "") +
      '<td class="act">' +
      iconBtn("del", "data-fixdel", esc(key), "Снять закрепление: " + who, ICON_TRASH) +
      "</td></tr>";
  }).join("");
  // Шапка граф — всегда, и у пустой таблицы (пользователь 29.09.2026: «пусть
  // таблица всегда отрисовывается со столбцами, и если нет записей — пустая,
  // и кнопка «закрепить сотрудника»»).
  var head = "<thead><tr><th>Закреплённый сотрудник</th><th>Начало</th>" +
    "<th>Окончание</th><th>Выплата</th>" +
    (sums ? '<th class="n">Сумма в месяц, ₽</th>' : "") + "<th></th></tr></thead>";
  return '<div class="fixes"><table>' + head + "<tbody>" + groupRows + rows +
    '<tr class="addrow"><td colspan="' + (sums ? 6 : 5) + '"><button type="button" class="vers" data-fixadd="' +
    esc(x.code) + '" aria-haspopup="dialog"><svg width="12" height="12" viewBox="0 0 16 16"' +
    ' fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">' +
    '<path d="M8 3v10M3 8h10"></path></svg>Закрепить сотрудника</button></td></tr>' +
    "</tbody></table></div>";
}

/* Действующий состав рабочей группы договора для панели плана — из
   приказов (vdata.workgroups): по каждой строке штатного расписания решает
   последний приказ, где она названа; не сопоставленные — строкой из приказа.
   span — срок группы в году плана в пределах [lo, hi] или null. */
function fixGroupRows(code, lo, hi) {
  var d = vdata || {}, emps = {}, year = fixYear();
  (d.employees || []).forEach(function (e) { emps[e.code] = e; });
  var rows = (d.workgroups || []).filter(function (r) { return r.contract === code; })
    .sort(function (a, b) { return wgDay(a.date) - wgDay(b.date) || a.id - b.id; });
  var last = {}, free = [];
  rows.forEach(function (r) { if (r.employee) last[r.employee] = r; else free.push(r); });
  var at = function (text, dflt) {
    var p = String(text || "").split(".");
    if (p.length !== 3) return dflt;
    var y = +p[2];
    return (y < 100 ? y + 2000 : y) * 12 + (+p[1] - 1);
  };
  return Object.keys(last).map(function (k) { return last[k]; })
    .filter(function (r) { return r.action !== "исключить"; })
    .concat(free).map(function (r) {
      var e = r.employee && emps[r.employee], span = null;
      if (year) {
        var a = Math.max(at(r.from, -Infinity) - year * 12 + 1, lo);
        var b = Math.min(at(r.to, Infinity) - year * 12 + 1, hi);
        if (a <= b) span = [a, b];
      }
      return { who: e ? (e.fio || e.code) : (r.fio || ""), employee: e ? r.employee : null,
               number: r.number, date: r.date, span: span };
    }).sort(function (a, b) { return String(a.who).localeCompare(String(b.who), "ru"); });
}

/* ── вкладки плана «Штатное расписание» и «Заработная плата по месяцам» ──
   Правка поверх реестра для расчёта «а что если» (пользователь 30.09.2026:
   «для нового плана возможно они захотят менять штатное расписание и с
   разными зарплатами пересчитывать»). Двумя вкладками с именами реестра, а
   не одной «Сотрудники»: «это не штатка и не зарплата — гибрид, им не
   понравится». Как условия договора (SOURCE-006): в ячейке — значение, с
   которым пойдёт расчёт; правка плана — акцентом, в подсказке — значение
   реестра; щелчок правит на месте, пустое или равное реестровому снимает
   правку; стрелка в конце строки возвращает строку этой вкладки. Реестр и
   соседние планы правка не трогает (basis.set_employee_overrides). */
var EMP_FIELDS = ["должность", "ставка", "начало", "окончание"];

/* Месяц, по который факт в расчёте: месяцы факта подряд с января, не дальше
   ноября — как facts.through на сервере. */
function factsThrough(facts) {
  var have = {}, m = 0;
  facts.forEach(function (f) { have[f.month] = true; });
  while (m < 11 && have[m + 1]) m++;
  return m;
}

/* Факт начислений — вкладка реестра и вкладка плана (SOURCE-011): таблица
   формы 6.3 «ШР на дату, детализация назначений» — графы шаблона кадров
   пользователя (30.09.2026: «вот этот шаблон, я же скидывала»; о плоской
   таблице и 6.1: «очень похожа, но не та»). Собрана из деталей реестра, как
   «Штатное расписание» (разбор дизайнера 30.09.2026: сетка отчёта с полосой
   органов вне панели выглядела чужой — «здесь что-то странное с
   интерфейсом»): панель движка regGrid, в её шапке — «Загрузить факт
   начислений», лупа, «Форма для заполнения» и «Графы»; пустая таблица —
   шапкой граф и подвалом. Строка без договора в реестре или без сотрудника
   в штатном расписании тише, причина — в подсказке. detail — назначения по
   периодам (facts.detail_rows); render — чем перерисовать: он есть у
   вкладки плана (renderView), и там загрузки нет — факт принадлежит
   реестру, а план поверх реестра только правит своё (пользователь
   30.09.2026: «в реестре — то, что переиспользуется, но также мы можем
   перезаписать внутри плана»); «Форма для заполнения» остаётся и там
   («нет, форму оставь»). */
function factsGrid(id, detail, render) {
  var num = function (v) { return v == null ? null : mo(v); };
  var cols = SHR_HEAD.map(function (label, i) {
    var k = ["таб", "назначение", "фио", "код_подр", "подразделение", "должность", "категория",
             "код", "параметр", "ставка", "номинал", "по_ставке", "сумма", "начало", "окончание",
             "код_шифра", "шифр", "счет", "уи", "приказ_ввода", "дата_ввода", "приказ_закрытия",
             "дата_закрытия"][i];
    var c = { k: k, label: label };
    if (i === 0) c.always = true;
    // Суммы разных назначений в группе не складываются — графы чисел, а не денег.
    if (i === 9) { c.kind = "number"; c.val = function (x) { return x["ставка"] == null ? null : rf(x["ставка"]); }; }
    if (i >= 10 && i <= 12) { c.kind = "number"; c.val = function (x) { return num(x[k]); }; }
    if (i === 13 || i === 14 || i === 20 || i === 22) c.kind = "date";
    return c;
  });
  var form = caseId ? '<a data-tb href="/api/case/' + caseId + '/facts/form" download>' +
    TB_DOWN + "<span>Форма для заполнения</span></a>" : "";
  return regGrid(id, cols, detail, {
    render: render,
    bar: render ? { placeholder: "Таб.№ или ФИО", right: form }
      : { upload: "", addLabel: "Загрузить факт начислений", placeholder: "Таб.№ или ФИО",
          right: form },
    rowCls: function (x) { return x.known && x.matched ? "" : "off"; },
    rowAttr: function (x) {
      return x.known && x.matched ? "" : 'title="' + esc(!x.known
        ? "Договора нет в реестре — строка в расчёт не идёт"
        : "Нет в штатном расписании — деньги с договора в расчёте учтены") + '"';
    } });
}

/* Вкладка плана «Факт начислений» — входные данные: загрузить документ и
   сразу увидеть, что в нём. Месяцы, за которые факт есть, расчёт берёт как
   были и пересчитывает остальные; выбирать месяц не нужно (пользователь
   30.09.2026: «зачем выбирать месяц, я не поняла?»). Над таблицей — одна
   строка статуса: по какой месяц факт в расчёте и, при разрыве, какой месяц
   в расчёт не идёт. В реестре её нет: реестр о планах не знает (UI-005). */
function factPlanView() {
  var facts = (vdata && vdata.facts) || [];
  var F = factsThrough(facts), later = {};
  facts.forEach(function (f) { if (f.month > F) later[f.month] = true; });
  var lm = Object.keys(later).map(Number).sort(function (a, b) { return a - b; });
  var head = F || lm.length
    ? '<div class="preflight">Факт в расчёте: ' + (F ? esc(F === 1 ? MONTH_NAMES[0]
        : MONTH_NAMES[0] + "–" + MONTH_NAMES[F - 1]) : "нет") +
      (lm.length ? ', <span class="st warn">не в расчёте: ' + esc(lm.map(function (m) {
        return MONTH_NAMES[m - 1]; }).join(", ")) + "</span>" : "") + "</div>"
    : "";
  return head + factsGrid("pfact", (vdata && vdata.facts_detail) || [], renderView);
}

/* Строки штатного расписания, снятые с расчёта этого плана: {код: true}. */
function staffOffMap() {
  var off = {};
  ((vbasis && vbasis.staff_excluded) || []).forEach(function (k) { off[k] = true; });
  return off;
}

function staffPlanTable(kind) {
  var d = vdata || {}, own = (vbasis && vbasis.staff_overrides) || {};
  // Флажок «в расчёте» (пользователь 30.09.2026: «добавь опцию выбора
  // галочками нужных сотрудников, вдруг уволить захотят кого-то») — как у
  // договоров плана: снятая строка остаётся в реестре и в соседних планах, а
  // из расчёта этого плана уходит вместе с 120, суммами месяцев и
  // закреплениями (basis.set_staff_included). Флажок — на «Штатном
  // расписании»; на «Заработной плате» снятая строка только тише.
  var off = staffOffMap();
  var pay = {};
  ((d.salary && d.salary.rows) || []).forEach(function (r) { pay[r.code] = r.months || []; });
  var emps = (d.employees || []).slice().sort(function (a, b) {
    return String(a.fio || a.code).localeCompare(String(b.fio || b.code), "ru");
  });
  var rateText = function (v) { return v == null || v === "" ? "" : String(v).replace(".", ","); };
  // Синхронизация с реестром (пользователь 30.09.2026: «подумай, как
  // синхронизироваться с реестром, а как кастомить»): правка, равная
  // значению реестра, — уже не правка; правка поверх значения, которое в
  // реестре с тех пор изменилось, помечена, и подсказка говорит, каким оно
  // было при правке. Решение — за экономистом: оставить или вернуть стрелкой.
  var cell = function (code, field, base, set, shown, label, was) {
    var b = base == null ? "" : String(base);
    var has = set != null && set !== "" && String(set) !== b;
    var stale = has && was != null && String(was) !== b;
    // В подсказке суммы — деньгами, как в ячейке: «95 000», а не «95000».
    var say = function (v) {
      return v === "" || v == null ? "пусто" : /^m\d+$/.test(field) ? mo(+v) : String(v);
    };
    return '<span class="ed" data-empov="' + esc(code + "|" + field) + '" data-base="' +
      esc(b) + '" data-val="' + esc(has ? String(set) : b) +
      '"' + (has ? " data-set" : "") + (stale ? " data-stale" : "") +
      ' role="button" tabindex="0" title="' +
      esc(label + (has ? ": в плане; в реестре — " + say(b) +
                         (stale ? "; при правке в реестре было " + say(was) + " — реестр изменился"
                                : "")
                       : " — изменить в этом плане")) + '">' + esc(shown) + "</span>";
  };
  var zp = kind === "zp";
  var rows = emps.map(function (e) {
    var o = own[e.code] || {}, months = pay[e.code] || [], opay = o["зарплата"] || {};
    var reg = o["реестр"] || {};
    var rate = rateText(e.rate);
    var cells = [e.person_code || e.code, e.fio || ""], any;
    if (!zp) cells.unshift({ v: '<input type="checkbox" data-empincl="' + esc(e.code) + '"' +
                                (!off[e.code] ? " checked" : "") + ' aria-label="' +
                                esc((e.fio || e.code) + " в расчёте") + '">', cls: "incl" });
    if (zp) {
      for (var m = 1; m <= 12; m++) {
        var base = months[m - 1], set = opay[String(m)];
        var has = set != null;
        cells.push({ v: cell(e.code, "m" + m, base == null ? "" : String(base), has ? String(set) : null,
                             has ? mo(set) : (base == null ? "" : mo(base)), MONTH_NAMES[m - 1],
                             reg["м" + m] == null ? null : String(reg["м" + m])),
                     cls: "n" });
      }
      any = Object.keys(opay).length > 0;
    } else {
      // Должность правится в плане, как ставка и срок (пользователь
      // 30.09.2026: «тут добавь возможность вручную менять должность»): от
      // неё зависят категория, БЭП и пределы приказов.
      cells.push({ v: cell(e.code, "должность", e.position || "", o["должность"],
                           o["должность"] || e.position || "", "Должность", reg["должность"]) },
        { v: cell(e.code, "ставка", rate, o["ставка"], o["ставка"] || rate, "Ставка", reg["ставка"]),
          cls: "n" },
        { v: cell(e.code, "начало", e.from || "", o["начало"], o["начало"] || e.from || "", "Начало",
                  reg["начало"]), cls: "dt" },
        { v: cell(e.code, "окончание", e.to || "", o["окончание"], o["окончание"] || e.to || "",
                  "Окончание", reg["окончание"]), cls: "dt" });
      any = EMP_FIELDS.some(function (k) { return k in o; });
    }
    cells.push({ v: any ? iconBtn("", "data-empreset", esc(e.code + "|" + kind),
                                  "Вернуть как в реестре: " + (e.fio || e.code), ICON_RESET) : "",
                 cls: "act" });
    return cells;
  });
  var left = emps.filter(function (e) { return !off[e.code]; }).length;
  var allBox = { v: '<input type="checkbox" data-empall aria-label="Все сотрудники в расчёте"' +
                    (emps.length && left === emps.length ? " checked" : "") + ">", cls: "incl" };
  var head = zp
    ? ["Табельный номер", "ФИО"].concat(MONTH_SHORT.map(function (m) { return { t: m }; })).concat([""])
    : [allBox, "Табельный номер", "ФИО", "Должность", { t: "Ставка" }, "Начало", "Окончание", ""];
  // Правки и флажки строк, которых в реестре больше нет (уволен, новая
  // версия штатного расписания): в расчёт не идут, и строка это говорит, а
  // убрать их — одной кнопкой.
  var orphans = (vbasis && vbasis.staff_orphans) || [];
  var orphanLine = orphans.length
    ? ', <span class="st warn">правки по строкам, которых нет в реестре: ' + esc(orphans.join(", ")) +
      '</span> <button type="button" class="vers" data-emporphans>Убрать правки</button>'
    : "";
  // Панель реестра: строка «В расчёте N из M» — в шапке панели, таблица —
  // тем же видом, что таблицы реестра (пользователь 30.09.2026: «в планах
  // сделай все таблицы в одном стиле — как в реестре»).
  var line = zp ? (orphans.length ? orphanLine.slice(2) : "")
                : "В расчёте " + left + " из " + emps.length + orphanLine;
  return '<div class="gridwrap empov"><div class="tpanel">' +
    (line ? '<div class="tbar"><div class="preflight">' + line + "</div></div>" : "") +
    table(head, rows, "", {
      plain: true, emptyHead: true,
      rowCls: function (i) { return off[emps[i].code] ? "off" : ""; },
      rowAttr: function (i) {
        return zp && off[emps[i].code]
          ? 'title="Вне расчёта этого плана — флажок на вкладке «Штатное расписание»"' : "";
      } }) + "</div></div>";
}

/* Правка ячейки сотрудника в плане: Enter или уход из поля сохраняют, Esc —
   как было. Пустое или равное реестровому — снять правку. */
function empCellEdit(span) {
  if (span.querySelector("input")) return;
  var key = span.getAttribute("data-empov").split("|"), code = key[0], field = key[1];
  var base = span.getAttribute("data-base") || "", cur = span.getAttribute("data-val") || "";
  var date = field === "начало" || field === "окончание", month = /^m(\d+)$/.exec(field);
  var was = span.innerHTML, done = false;
  var el = document.createElement("input");
  el.type = date ? "date" : "text";
  if (!date) { el.inputMode = "decimal"; el.autocomplete = "off"; }
  el.value = date ? dmyToIso(cur) : month && cur ? mo(+cur) : cur;
  el.setAttribute("aria-label", span.getAttribute("title") || field);
  // Должность — с подсказками из справочника должностей: от её названия
  // зависят нормативы, опечатка оставила бы строку без них.
  if (field === "должность") {
    var dl = document.getElementById("posopts");
    if (!dl) {
      dl = document.createElement("datalist");
      dl.id = "posopts";
      document.body.appendChild(dl);
    }
    dl.innerHTML = ((vdata && vdata.reference) || []).map(function (r) {
      return '<option value="' + esc(r.pos) + '">';
    }).join("");
    el.setAttribute("list", "posopts");
  }
  var restore = function (back) {
    if (done) return;
    done = true;
    span.innerHTML = was;
    if (back) span.focus();
  };
  var norm = function (v) { return String(v == null ? "" : v).replace(/\s/g, "").replace(",", "."); };
  var save = function () {
    if (done) return;
    var value = date ? isoToDmy(el.value) : el.value.trim();
    if (norm(value) === norm(cur)) { restore(false); return; }
    if (norm(value) === norm(base)) value = "";
    done = true;
    var fields = {};
    if (month) { fields["зарплата"] = {}; fields["зарплата"][month[1]] = value; }
    else fields[field] = value;
    api("/api/case/" + caseId + "/basis", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ employee: code, fields: fields })
    }).then(function (b) {
      vbasis = b;
      renderView();
      showToast(value === "" ? "Как в реестре" : "Изменено в этом плане");
    }, function (er) {
      span.innerHTML = was;
      alert("Не сохранилось: " + (er.message || "ошибка сети"));
    });
  };
  el.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); save(); }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); restore(true); }
  });
  el.addEventListener("blur", save);
  if (date) el.addEventListener("change", save);
  span.innerHTML = "";
  span.appendChild(el);
  el.focus();
}

document.addEventListener("click", function (e) {
  if (view !== "emp" && view !== "zp") return;
  var orph = e.target.closest && e.target.closest("[data-emporphans]");
  if (orph && caseId) {
    var codes = ((vbasis && vbasis.staff_orphans) || []).slice();
    var clearAll = { "ставка": "", "начало": "", "окончание": "", "зарплата": {} };
    for (var mm = 1; mm <= 12; mm++) clearAll["зарплата"][mm] = "";
    var post = function (body) {
      return api("/api/case/" + caseId + "/basis", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) });
    };
    codes.reduce(function (p, c) {
      return p.then(function () { return post({ employee: c, fields: clearAll }); });
    }, Promise.resolve()).then(function () {
      return post({ employees: codes, included: true });
    }).then(function (b) { vbasis = b; renderView(); showToast("Правки убраны"); },
            function (er) { alert("Не сохранилось: " + (er.message || "ошибка сети")); });
    return;
  }
  var r = e.target.closest && e.target.closest("[data-empreset]");
  if (r) {
    // Стрелка возвращает строку своей вкладки: на «Штатном расписании» —
    // ставку и срок, на «Заработной плате» — суммы месяцев.
    var rk = r.getAttribute("data-empreset").split("|"), code = rk[0];
    var o = ((vbasis && vbasis.staff_overrides) || {})[code] || {};
    var fields = {};
    if (rk[1] === "zp") {
      if (o["зарплата"]) {
        fields["зарплата"] = {};
        Object.keys(o["зарплата"]).forEach(function (m) { fields["зарплата"][m] = ""; });
      }
    } else {
      EMP_FIELDS.forEach(function (k) { if (k in o) fields[k] = ""; });
    }
    api("/api/case/" + caseId + "/basis", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ employee: code, fields: fields })
    }).then(function (b) { vbasis = b; renderView(); showToast("Как в реестре"); },
            function (er) { alert("Не сохранилось: " + (er.message || "ошибка сети")); });
    return;
  }
  var span = e.target.closest && e.target.closest("[data-empov]");
  if (span) empCellEdit(span);
});
document.addEventListener("keydown", function (e) {
  if ((view !== "emp" && view !== "zp") || (e.key !== "Enter" && e.key !== " ")) return;
  if (e.target.matches && e.target.matches("[data-empov]")) {
    e.preventDefault();
    empCellEdit(e.target);
  }
});

/* «11.26»: месяц и две последние цифры года плана. */
function fixMonthText(m) {
  var year = fixYear();
  return ("0" + m).slice(-2) + (year ? "." + String(year).slice(-2) : "");
}

/* Правка ячейки закрепления на месте, как в таблицах реестра: месяц —
   календарём месяцев (пользователь 20.09.2026: «выбор календаря») в пределах
   года плана, вид выплаты — выбором, сумма — полем. Месяц и вид сохраняются
   сразу; сумма — по Enter или уходу из поля; Esc — как было. Начало позже
   окончания — второй месяц догоняет изменённый, а не ошибка. */
function fixCellEdit(span) {
  if (span.querySelector("select, input")) return;
  var row = span.closest("[data-fixrow]"), key = row && row.getAttribute("data-fixrow");
  var f = key && fixFind(key);
  if (!f || fixBusy[key]) return;
  var x = ((vdata && vdata.contracts) || []).filter(function (c) {
    return c.code === f["договор"];
  })[0];
  if (!x) return;
  var now = ((vbasis && vbasis.overrides) || {})[x.code] || {};
  var field = span.getAttribute("data-fixcell"), was = span.innerHTML, done = false;
  var who = f["фио"] || f["сотрудник"];
  var restore = function (back) {
    if (done) return;
    done = true;
    span.innerHTML = was;
    if (back) span.focus();
  };
  var save = function (body, toast) {
    if (done) return;
    done = true;
    fixBusy[key] = true;
    fixPost(body, { done: toast, sel: '[data-fixcell="' + field + '"]',
                    key: body.employee + "|" + body.contract + "|" + body.from + "-" + body.to,
                    ok: function () { delete fixBusy[key]; },
                    fail: function () { delete fixBusy[key]; span.innerHTML = was; } });
  };
  // Срок и сумма правятся у всей строки — она и есть закрепление этого
  // сотрудника на этом договоре, со всеми названными видами.
  var base = { employee: f["сотрудник"], contract: f["договор"], from: f["с"], to: f["по"],
               kinds: f.kinds, only: f["только"],
               amount: f["сумма"] && f.kinds.length === 1 ? String(f["сумма"]) : "" };
  var el;
  if (field === "from" || field === "to") {
    var year = fixYear();
    var ym = function (m) { return year + "-" + ("0" + m).slice(-2); };
    // Месяцы — только в сроке договора в году плана с правками плана
    // (пользователь 29.09.2026: «чтоб можно было выбрать месяцы в период
    // договора»); прочие календарь не даёт.
    var act = fixActive(x, now), lo = act.indexOf(true) + 1, hi = act.lastIndexOf(true) + 1;
    if (!lo) { lo = 1; hi = 12; }
    var cur = Math.min(Math.max(field === "from" ? f["с"] : f["по"], lo), hi);
    el = document.createElement("input");
    el.type = "month";
    if (year) {
      el.min = ym(lo);
      el.max = ym(hi);
      el.value = ym(cur);
    }
    el.setAttribute("aria-label", (field === "from" ? "Начало: " : "Окончание: ") + who);
    el.addEventListener("change", function () {
      var got = /^(\d{4})-(\d{2})$/.exec(el.value);
      if (!got || (year && +got[1] !== year) || +got[2] < lo || +got[2] > hi) return;
      var m = +got[2], a = Math.max(f["с"], lo), b = Math.min(f["по"], hi);
      if (m === cur) { restore(false); return; }
      if (field === "from") { a = m; if (b < a) b = a; } else { b = m; if (a > b) a = b; }
      base.from = a;
      base.to = b;
      save(base, "Срок изменён");
    });
    el.addEventListener("blur", function () { restore(false); });
  } else if (field === "kind") {
    // Видов до шести, и выбрать можно несколько: в ячейку 24 px они не
    // помещаются, поэтому выбор — окошком у ячейки (пользователь 21.09.2026:
    // «множественный выбор сделать»).
    fixKindsPick(span, f, x, now, function (chosen, only) {
      base.kinds = chosen;
      base.only = only;
      // Сумма живёт при одном названном виде; в прочих случаях её нет.
      base.amount = chosen.length === 1 ? base.amount : "";
      save(base, "Выплата изменена");
    }, function () { restore(true); });
    return;
  } else {
    el = document.createElement("input");
    el.type = "text";
    el.inputMode = "decimal";
    el.autocomplete = "off";
    el.value = f["сумма"] ? mo(f["сумма"]) : "";
    el.setAttribute("aria-label", "Сумма в месяц, ₽: " + who);
    var commit = function () {
      if (fixAmount(el.value) === (f["сумма"] || null)) { restore(false); return; }
      base.amount = el.value.trim();
      save(base, "Сумма изменена");
    };
    el.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); commit(); }
    });
    el.addEventListener("blur", commit);
  }
  el.className = "ovf";
  el.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); restore(true); }
  });
  if (el.tagName === "SELECT") el.addEventListener("blur", function () { restore(false); });
  span.innerHTML = "";
  span.appendChild(el);
  el.focus();
}

/* Что сотрудник получает с этого договора: любая выплата, вся зарплата или
   названные виды. Флажки — те же, что у «Разрешённых выплат» договора, и
   сохраняются, когда окошко закрывают; Esc — как было. «Вся зарплата» и
   названные виды исключают друг друга: вся зарплата и так любой вид. */
function fixKindsPick(anchor, f, x, now, onSave, onCancel, note) {
  var open = document.querySelector(".menu");
  if (open) open.remove();
  var allowed = String((now && now["разрешенные выплаты"]) || x.kinds || "").toLowerCase();
  var kinds = FIX_KINDS.filter(function (k) {
    return !allowed.trim() || allowed.indexOf(k) >= 0 || f.kinds.indexOf(k) >= 0;
  });
  var who = f["фио"] || f["сотрудник"];
  var m = document.createElement("div");
  m.className = "menu kindpick";
  m.setAttribute("role", "dialog");
  m.setAttribute("aria-label", "Выплата: " + who);
  m.innerHTML = '<p class="mt">Что получает с этого договора</p>' +
    '<label class="fld all"><input type="checkbox" data-fixonly' +
    (f["только"] ? " checked" : "") + ">вся зарплата с этого договора</label>" +
    '<span class="ovkinds" role="group" aria-label="Виды выплат">' +
    kinds.map(function (k, i) {
      return '<label class="fld" for="fk' + i + '"><input type="checkbox" id="fk' + i +
        '" value="' + esc(k) + '"' + (f.kinds.indexOf(k) >= 0 ? " checked" : "") +
        (f["только"] ? " disabled" : "") + ">" + esc(k) + "</label>";
    }).join("") + "</span>" +
    '<p class="mn">' + esc(note || "Ни одного вида — любая выплата: расчёт решит, какая.") +
    "</p>";
  document.body.appendChild(m);
  var r = anchor.getBoundingClientRect();
  m.style.top = Math.max(8, Math.min(r.bottom + 4, innerHeight - m.offsetHeight - 8)) + "px";
  m.style.left = Math.max(8, Math.min(r.left, innerWidth - m.offsetWidth - 8)) + "px";
  var only = m.querySelector("[data-fixonly]");
  var boxes = Array.prototype.slice.call(m.querySelectorAll(".ovkinds input"));
  var chosen = function () {
    return boxes.filter(function (b) { return b.checked; }).map(function (b) { return b.value; });
  };
  var same = function () {
    var now2 = chosen();
    return !!only.checked === !!f["только"] && now2.length === f.kinds.length &&
           now2.every(function (k) { return f.kinds.indexOf(k) >= 0; });
  };
  var done = false;
  function close(save) {
    if (done) return;
    done = true;
    var out = chosen(), all = only.checked, changed = !same();
    m.remove();
    document.removeEventListener("mousedown", onDoc, true);
    if (save && changed) onSave(all ? [] : out, all);
    else onCancel();
  }
  function onDoc(e) { if (!m.contains(e.target)) close(true); }
  only.addEventListener("change", function () {
    // Вся зарплата — это и есть любой вид: названные виды тогда не нужны.
    boxes.forEach(function (b) {
      b.disabled = only.checked;
      if (only.checked) b.checked = false;
    });
  });
  m.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(false); }
    else if (e.key === "Enter") { e.preventDefault(); close(true); }
  });
  setTimeout(function () { document.addEventListener("mousedown", onDoc, true); }, 0);
  (only.checked ? only : boxes[0] || only).focus();
}

/* Проекты приказов (решения пользователя 28–30.09.2026: «отдельная вкладка
   приказы»; «по конкретному договору выбрать тип — приказ, в выбранный месяц
   для выбранного сотрудника, чтоб в шаблон подставилось и выгрузилось»; о
   каскаде выборов: «форма выбора приказа на сотрудника не продуманная»;
   спецификация дизайнера 30.09.2026). Одна таблица: строка — приказ вида ×
   договора × месяца со списком сотрудников (Т-11а); ▸ или щелчок по строке
   раскрывает сотрудников, у каждого — свой приказ на одного (Т-11). Отбор —
   автофильтром граф, а не каскадом выборов; всё — из расчёта, выбранного в
   шапке. Заготовки бланков на экране нет: экономисту она не нужна
   («зачем эта кнопка?»), она — в /api/order/templates. */
var vorders = null, ordOpen = {};
var MONTHS_OF = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
                 "сентября", "октября", "ноября", "декабря"];

/* Расчёт вкладки: выбранный в шапке (адрес ?run=), иначе последний удачный. */
function ordersRun() {
  var runs = (state && state.runs) || [];
  return (chosenRun && runs.filter(function (r) {
    return r.id === chosenRun && r.status === "OPTIMAL"; })[0]) ||
    runs.filter(function (r) { return r.status === "OPTIMAL"; })[0] || null;
}

function loadOrders() {
  var run = ordersRun();
  vorders = null;
  if (!run) return Promise.resolve();
  return api("/api/case/" + caseId + "/orders?run=" + run.id).then(function (d) {
    vorders = d;
  }, function (e) { vorders = { error: e.message || "не загрузилось" }; });
}

function ordersView() {
  if (!ordersRun()) {
    return '<div class="empty-state" role="status" aria-live="polite">' +
      "<strong>План ещё не рассчитан</strong></div>";
  }
  if (!vorders) return '<div class="none loading" role="status" aria-live="polite">Загружаю…</div>';
  if (vorders.error) return '<div class="none">' + esc(vorders.error) + "</div>";
  var run = vorders.run;
  // Поиск находит и строки сотрудников: найденный приказ раскрыт на них.
  var hits = function (o) {
    var q = (tbQuery.ord || "").trim().toLowerCase();
    return !q ? [] : o.lines.filter(function (l) {
      return [l.fio, l.person_code, l.contract].join(" ").toLowerCase().indexOf(q) >= 0;
    });
  };
  var file = function (o, emp) {
    return "/api/case/" + caseId + "/orders/file?run=" + run + "&kind=" + encodeURIComponent(o.kind) +
      "&contract=" + encodeURIComponent(o.contract) + "&month=" + o.month +
      (emp ? "&employee=" + encodeURIComponent(emp) : "");
  };
  var sum = function (v) { return v == null ? null : mo(v); };
  var cols = [
    { k: "title", label: "Приказ", always: true, val: function (o) { return o.title; },
      // Название приказа и есть кнопка раскрытия: ▸ перед ним — её знак.
      cell: function (o) {
        var open = !!ordOpen[o.key] || hits(o).length > 0;
        return { v: '<button type="button" data-ordopen="' + esc(o.key) + '" aria-expanded="' +
          open + '" aria-label="' + esc((open ? "Скрыть сотрудников: " : "Показать сотрудников: ") +
          o.title) + '"><i aria-hidden="true">' + (open ? "▾" : "▸") + "</i><b>" + esc(o.title) +
          "</b></button>" };
      } },
    // Шифр — переход к договору в этом плане, как «Относится к» (UI-013).
    { k: "contract", label: "Договор", val: function (o) { return o.contract; },
      cell: function (o) {
        return { v: '<button type="button" data-ctropen="' + esc(o.contract) + '">' +
          esc(o.contract) + "</button>", cls: "rel" };
      } },
    { k: "cname", label: "Наименование договора", off: true,
      val: function (o) { return o.contract_name || ""; } },
    { k: "month", label: "Месяц", val: function (o) { return fixMonthText(o.month); } },
    { k: "kinds", label: "Вид выплаты", val: function (o) { return o.kinds.join(", "); } },
    { k: "people", label: "Сотрудники", kind: "number" },
    // Суммы разных месяцев не складываются: графы — числа, итога нет.
    { k: "before", label: "Было в месяц, ₽", kind: "number", val: function (o) { return sum(o.before); } },
    { k: "after", label: "Стало в месяц, ₽", kind: "number", val: function (o) { return sum(o.after); } },
    { k: "file", act: true, cell: function (o) {
        return { v: '<a class="vers" href="' + esc(file(o)) + '" download aria-label="' +
          esc("Скачать приказ: " + o.title + ", " + o.contract + ", " + fixMonthText(o.month)) +
          '">' + TB_DOWN + "Скачать</a>", cls: "act" };
      } }
  ];
  return regGrid("ord", cols, vorders.orders, {
    render: renderView,
    bar: { placeholder: "ФИО или шифр" },
    text: function (o) {
      return o.lines.map(function (l) { return [l.fio, l.person_code, l.contract].join(" "); }).join(" ");
    },
    rowAttr: function (o) { return 'data-ordrow="' + esc(o.key) + '"'; },
    // Строки сотрудников: ФИО и «Скачать» — в первой строке человека; было
    // и стало — двумя графами, действие надбавки — в подсказке строки.
    sub: function (o, shown) {
      var found = hits(o);
      if (!ordOpen[o.key] && !found.length) return [];
      var seen = {};
      return (found.length ? found : o.lines).map(function (l) {
        var first = !seen[l.employee];
        seen[l.employee] = true;
        var cell = {
          title: { v: first ? '<span title="' + esc("Таб. № " + l.person_code +
            (l.position ? ", " + l.position : "")) + '">' + esc(l.fio) + "</span>" : "" },
          contract: { v: o.kind === "источник" ? esc(l.contract) : "" },
          kinds: { v: esc(l.kind) },
          before: { v: l.before == null ? "" : mo(l.before), cls: "n" },
          after: { v: l.after == null ? "" : mo(l.after), cls: "n" },
          file: { v: first ? '<a class="vers" href="' + esc(file(o, l.employee)) +
            '" download aria-label="' + esc("Скачать приказ: " + l.fio) + '">' + TB_DOWN +
            "Скачать</a>" : "", cls: "act" }
        };
        return { cells: shown.map(function (c) { return cell[c.k] || { v: "" }; }),
                 attr: "data-ordline" + (l.action ? ' title="' + esc(cap(l.action) + " с 1 " +
                   MONTHS_OF[o.month - 1]) + '"' : "") };
      });
    } });
}

/* Выплата члену рабочей группы: тот же выбор видов, что у закрепления,
   только «ни одного вида» значит «вся, по возможности». Состав группы
   меняется приказом, поэтому строку не снять. */
function wgKindEdit(span) {
  var code = pctrOpen, emp = span.getAttribute("data-wgkind");
  var x = ((vdata && vdata.contracts) || []).filter(function (c) { return c.code === code; })[0];
  if (!x || !caseId) return;
  var now = ((vbasis && vbasis.overrides) || {})[code] || {};
  var choice = ((vbasis && vbasis.workgroup_pay) || {})[code + "|" + emp] || {};
  var f = { kinds: choice["виды"] || [], "только": !!choice["только"],
            "фио": span.getAttribute("data-who"), "сотрудник": emp };
  fixKindsPick(span, f, x, now, function (kinds, all) {
    api("/api/case/" + caseId + "/fixes/workgroup", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ employee: emp, contract: code, kinds: kinds, only: all })
    }).then(function () {
      showToast(all ? "Вся зарплата с договора" : kinds.length ? "Выплата задана" : "Вся, по возможности");
      return fixReload(null);
    }, function (er) { alert("Не сохранилось: " + (er.message || "ошибка сети")); });
  }, function () { span.focus(); },
  "Ни одного вида — вся, по возможности: расчёт старается платить всю зарплату с этого договора.");
}

function fixAttr(v) {
  return String(v).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

/* Основание плана заново — без «Загружаю…» на весь экран: панель остаётся
   открытой, прокрутка на месте. Фокус — в строку закрепления, а если её не
   назвали — туда, где он стоял: сохранение по уходу фокуса иначе сбрасывало
   его с поля, на которое экономист только что перешёл. */
function fixReload(key, sel) {
  return api("/api/case/" + caseId + "/basis").then(function (b) {
    var mark = key ? null : viewFocusMark();
    vbasis = b;
    renderView();
    if (key) fixFocus(key, sel);
    else if (mark) {
      var el = $("view").querySelector(mark);
      if (el) el.focus();
    }
  });
}

function fixFocus(key, sel) {
  var row = key && $("view").querySelector('[data-fixrow="' + fixAttr(key) + '"]');
  var el = row && row.querySelector(sel || "[data-fixcell]");
  if (el) el.focus();
}

/* Где стоит фокус во вкладке — так, чтобы найти то же поле после
   перерисовки: по id, иначе по ключу строки закрепления и атрибуту поля. */
function viewFocusMark() {
  var a = document.activeElement;
  if (!a || a === document.body || !$("view").contains(a)) return null;
  if (a.id) return "#" + a.id;
  var n = ["data-fixcell", "data-fixdel", "data-fixadd", "data-wgkind",
           "data-pctr-open", "data-pctr-close", "data-incl", "data-ov", "data-use"]
    .filter(function (x) { return a.hasAttribute(x); })[0];
  if (!n) return null;
  var v = a.getAttribute(n), sel = "[" + n + (v ? '="' + fixAttr(v) + '"' : "") + "]";
  var row = a.closest("[data-fixrow]");
  return row ? '[data-fixrow="' + fixAttr(row.getAttribute("data-fixrow")) + '"] ' + sel : sel;
}

/* Сохранить закрепление: o — {done: тост, key и sel: куда вернуть фокус,
   ok: до перерисовки, fail: после отказа}. Отказ сервера — сообщением, как у
   прочих правок основания плана; строка остаётся какой была. */
function fixPost(body, o) {
  return api("/api/case/" + caseId + "/fixes", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then(function () {
      if (o.ok) o.ok();
      showToast(o.done);
      return fixReload(o.key, o.sel);
    }, function (er) {
      alert("Не удалось сохранить закрепление: " + (er.message || "ошибка сети"));
      if (o.fail) o.fail();
    });
}

function fixAmount(text) {
  var v = parseFloat(String(text == null ? "" : text).replace(/\s/g, "").replace(",", "."));
  return isNaN(v) ? null : v;
}

/* Всплывающий список с поиском — выбрать одно из многих. Стрелки ходят по
   списку, Enter выбирает, Esc закрывает и возвращает фокус. */
function pickFrom(anchor, items, title, empty, onPick) {
  var open = document.querySelector(".menu");
  if (open) open.remove();
  var m = document.createElement("div");
  m.className = "menu pick";
  m.setAttribute("role", "dialog");
  m.setAttribute("aria-label", title);
  // Пункт с keep — действие, а не значение («Новый договор…»): поиск его не
  // прячет, а «Не найдено» говорит о значениях.
  var real = items.filter(function (it) { return !it.keep; }).length;
  m.innerHTML = '<input type="search" class="fsearch" placeholder="' + esc(title) +
    '…" aria-label="' + esc(title) + '" autocomplete="off"><div class="plist">' +
    items.map(function (it, i) {
      return '<button type="button" class="mi" data-i="' + i + '">' + esc(it.label) +
        (it.sub ? '<span class="sub">' + esc(it.sub) + "</span>" : "") + "</button>";
    }).join("") + '</div><p class="none"' + (real ? " hidden" : "") + ">" +
    esc(real ? "Не найдено" : empty) + "</p>";
  document.body.appendChild(m);
  // Под якорем; снизу не помещается — над ним, а не поверх самого якоря.
  // Вправо не помещается — правым краем к правому краю якоря.
  var r = anchor.getBoundingClientRect(), h = m.offsetHeight, w = m.offsetWidth;
  var top = r.bottom + 4 + h <= innerHeight - 8 ? r.bottom + 4 : r.top - 4 - h;
  var left = r.left + w <= innerWidth - 8 ? r.left : r.right - w;
  m.style.top = Math.max(8, Math.min(top, innerHeight - h - 8)) + "px";
  m.style.left = Math.max(8, Math.min(left, innerWidth - w - 8)) + "px";
  var q = m.querySelector(".fsearch");
  var shown = function () {
    return Array.prototype.filter.call(m.querySelectorAll(".mi"), function (b) { return !b.hidden; });
  };
  function close(back) {
    m.remove();
    document.removeEventListener("mousedown", onDoc, true);
    if (back) anchor.focus();
  }
  function onDoc(e) { if (!m.contains(e.target)) close(false); }
  q.addEventListener("input", function () {
    var s = q.value.trim().toLowerCase();
    Array.prototype.forEach.call(m.querySelectorAll(".mi"), function (b) {
      var it = items[+b.getAttribute("data-i")];
      b.hidden = !!s && !it.keep && b.textContent.toLowerCase().indexOf(s) < 0;
    });
    m.querySelector(".none").hidden = !!real && shown().some(function (b) {
      return !items[+b.getAttribute("data-i")].keep;
    });
  });
  m.addEventListener("keydown", function (e) {
    var list = shown(), at = list.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(true); }
    else if (e.key === "ArrowDown" && list.length) {
      e.preventDefault();
      list[Math.min(at + 1, list.length - 1)].focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (at <= 0) q.focus(); else list[at - 1].focus();
    } else if (e.key === "Enter" && document.activeElement === q && list.length) {
      e.preventDefault();
      list[0].click();
    }
  });
  m.addEventListener("click", function (e) {
    var b = e.target.closest(".mi");
    if (!b) return;
    close(false);
    onPick(items[+b.getAttribute("data-i")].id);
  });
  setTimeout(function () { document.addEventListener("mousedown", onDoc, true); }, 0);
  q.focus();
}

/* «+ Закрепить сотрудника»: список ФИО с поиском. Щелчок по ФИО — сотрудник
   сразу закреплён на все месяцы договора с любой выплатой; срок и выплату
   меняют потом в его строке. Кто уже закреплён на этом договоре — не в
   списке, какой бы ни была выплата: выбор добавлял ему вторую строку «любая»
   рядом с прежней, и в таблице они читались как один сотрудник дважды
   (пользователь 21.09.2026: «выбрала сотрудника и появились две строки
   сразу»). Другая выплата тому же человеку — правкой графы «Выплата» в его
   строке, а не вторым закреплением. */
function fixAdd(anchor, code) {
  var x = ((vdata && vdata.contracts) || []).filter(function (c) { return c.code === code; })[0];
  if (!x) return;
  var taken = {};
  ((vbasis && vbasis.fixes) || []).forEach(function (f) {
    if (f["договор"] === code) taken[f["сотрудник"]] = true;
  });
  var gone = staffOffMap();
  var people = ((vdata && vdata.employees) || [])
    .filter(function (e) { return !taken[e.code] && !gone[e.code]; })
    .sort(function (a, b) {
      return String(a.fio || a.code).localeCompare(String(b.fio || b.code), "ru");
    });
  pickFrom(anchor, people.map(function (e) {
    return { id: e.code, label: e.fio || e.code, sub: e.position || "" };
  }), "Найти сотрудника", "Все сотрудники уже закреплены", function (emp) {
    var now = ((vbasis && vbasis.overrides) || {})[code] || {};
    var span = fixDefaultSpan(fixActive(x, now));
    fixPost({ employee: emp, contract: code, from: span[0], to: span[1], kinds: [],
              amount: "" }, { done: "Сотрудник закреплён",
                              key: emp + "|" + code + "|" + span[0] + "-" + span[1] });
  });
}

function fixDelete(key) {
  var p = key.split("|"), f = fixFind(key);
  var who = (f && (f["фио"] || f["сотрудник"])) || p[0];
  ask("Снять закрепление за " + p[1] + "?",
      "Сотрудник: " + who + ". Посчитанные планы этого плана будут помечены к пересмотру.",
      "Снять")
    .then(function (yes) {
      if (!yes) return;
      api("/api/case/" + caseId + "/fixes", { method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ employee: p[0], contract: p[1], all: true }) })
        .then(function () {
          showToast("Закрепление снято");
          return api("/api/case/" + caseId + "/basis").then(function (b) {
            vbasis = b;
            renderView();
            var add = $("view").querySelector('[data-fixadd="' + fixAttr(p[1]) + '"]');
            if (add) add.focus();
          });
        }, function (er) {
          alert("Не удалось снять закрепление: " + (er.message || "ошибка сети"));
        });
    });
}

/* ── Таблица реестра: одна на всех ─────────────────────────────
   Решение пользователя 18.09.2026: «все таблицы по функциональности должны
   быть одинаковые, только столбцы — названия различаются». Поэтому движок
   один: порядок щелчком по заголовку; меню графы — сортировка, группировка,
   закрепление слева, скрыть, выбрать графы, сброс вида и отбор значений
   словами Excel; группы с итогом по денежным графам; страницы.

   Графа: {k, label, kind: "text" | "money" | "number" | "date", always —
   не прячется и стоит первой, off — скрыта, пока её не выберут,
   val(x) — значение строкой: по нему порядок, отбор и группы (то же, что
   видно в ячейке), cell(x) — ячейка: строка (экранируется) или {v, cls}}.

   Вид таблицы (порядок, отбор, группа) живёт, пока открыт реестр; состав
   граф помнится между визитами. Таблицы разделов результата плана сюда не
   входят: у них свои встроенные группировки. */
var GV = {}, GRIDS = {};
var FUNNEL = '<svg width="9" height="9" viewBox="0 0 12 12" fill="currentColor" aria-hidden="true">' +
             '<path d="M1 2h10L7 6.6V11L5 9.6V6.6z"></path></svg>';
var GRID_EMPTY = "не заполнено";

function gv(id) {
  if (!GV[id]) {
    var hidden = null;
    try { hidden = JSON.parse(localStorage.getItem("fot.grid." + id) || "null"); }
    catch (e) { hidden = null; }
    GV[id] = { sort: null, filter: {}, group: null, collapsed: {}, pin: [], hidden: hidden };
  }
  return GV[id];
}
function gridSaveCols(id) {
  try { localStorage.setItem("fot.grid." + id, JSON.stringify(gv(id).hidden)); }
  catch (e) { /* хранилища нет — состав граф живёт до перезагрузки */ }
}
function gridShown(id, cols) {
  var st = gv(id);
  return cols.filter(function (c) {
    if (c.always || c.act) return true;
    if (st.hidden && Object.prototype.hasOwnProperty.call(st.hidden, c.k)) return !st.hidden[c.k];
    return !c.off;
  });
}
function gridCol(id, k) {
  return ((GRIDS[id] || {}).cols || []).filter(function (c) { return c.k === k; })[0] || null;
}
function gridVal(c, x) {
  var v = c.val ? c.val(x) : x[c.k];
  return v == null || v === "" ? GRID_EMPTY : String(v);
}
function gridNum(s) {
  var n = parseFloat(String(s).replace(/[\s  ]/g, "").replace("−", "-").replace(",", "."));
  return isNaN(n) ? 0 : n;
}
/* Порядок по виду величины: числа по величине, даты по хронологии, текст
   по алфавиту. Незаполненное — в конце: это не значение. */
function gridCmp(c, a, b) {
  var ea = a === GRID_EMPTY, eb = b === GRID_EMPTY;
  if (ea !== eb) return ea ? 1 : -1;
  if (c.kind === "money" || c.kind === "number") return gridNum(a) - gridNum(b);
  if (c.kind === "date") return (dmyToIso(a) || "").localeCompare(dmyToIso(b) || "");
  return String(a).localeCompare(String(b), "ru", { numeric: true });
}
/* Закреплённые графы — сразу за первой: так они и держатся при прокрутке. */
function gridOrder(id, vis) {
  var st = gv(id);
  var lead = vis.filter(function (c) { return c.always; });
  var pinned = st.pin.map(function (k) {
    return vis.filter(function (c) { return c.k === k && !c.always; })[0];
  }).filter(Boolean);
  var rest = vis.filter(function (c) { return lead.indexOf(c) < 0 && pinned.indexOf(c) < 0; });
  // Графа действий — последней, какие бы графы ни закрепили.
  return lead.concat(pinned, rest.filter(function (c) { return !c.act; }),
                     rest.filter(function (c) { return c.act; }));
}
function gridIsNum(c) { return c.kind === "money" || c.kind === "number"; }
/* Заголовок: подпись с порядком и значок меню. Стрелка — у той графы, по
   которой упорядочено; значок синеет, когда по графе стоит отбор. */
function gridHead(id, c) {
  var st = gv(id), on = st.sort && st.sort.k === c.k, filtered = !!st.filter[c.k];
  return { v: '<span class="thc">' +
      '<button type="button" data-gsort="' + esc(id + "|" + c.k) + '"' + (on ? ' class="on"' : "") + ">" +
        esc(c.label) + (on ? '<span class="ar">' + (st.sort.dir > 0 ? "▲" : "▼") + "</span>" : "") +
      "</button>" +
      '<button type="button" class="fbtn' + (filtered ? " on" : "") + '" data-gmenu="' +
        esc(id + "|" + c.k) + '" aria-haspopup="true" aria-expanded="false" aria-label="Меню графы ' +
        esc(c.label) + '">' + FUNNEL + "</button></span>",
    cls: (gridIsNum(c) ? "n" : "") + (c.always ? " key" : "") +
         (st.pin.indexOf(c.k) >= 0 && !c.always ? " pin" : "") };
}
function gridCell(c, x, pin) {
  // Ячейка — то же значение, что участвует в порядке и отборе, если своего
  // вида у графы нет: иначе отбор ловил бы одно, а глаз читал другое.
  var r = c.cell ? c.cell(x) : c.val ? c.val(x) : x[c.k];
  var o = r && typeof r === "object" ? r : { v: r == null || r === "" ? null : esc(String(r)) };
  // Имя строки в закреплённой графе переносится в своей ширине, а не
  // растягивает графу на пол-экрана (стиль .kname, UI-014).
  if (c.always && o !== r && o.v) o.v = '<span class="kname">' + o.v + "</span>";
  var cls = (o.cls || "") + (gridIsNum(c) && !/\bn\b/.test(o.cls || "") ? " n" : "") +
            (c.always && !/\bkey\b/.test(o.cls || "") ? " key" : "") + (pin ? " pin" : "");
  return { v: o.v, cls: cls.trim() };
}

/* Панель органов таблицы — шапка её рамки: «+», лупа и «Графы» тремя
   одинаковыми тихими значками (решение пользователя 19.09.2026). Так
   устроены таблицы всего реестра, включая таблицы документов; у документов
   «+» открывает выбор файлов, а перетаскивание работает на всю вкладку.
   o.add — атрибуты кнопки добавления, o.upload — шифр договора (или "")
   для загрузки файлов, o.addLabel — подсказка; o.key — ключ поиска;
   o.right — разметка справа перед «Графами» (выгрузки); o.cols — атрибуты
   кнопки «Графы». */
var tbQuery = {};
var TB_PLUS = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor"' +
  ' stroke-width="1.6" aria-hidden="true"><path d="M8 3v10M3 8h10"></path></svg>';
var TB_FIND = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor"' +
  ' stroke-width="1.5" aria-hidden="true"><circle cx="7" cy="7" r="4.5"></circle>' +
  '<path d="M10.6 10.6 14 14"></path></svg>';
var TB_DOWN = '<svg width="13" height="13" viewBox="0 0 16 16" fill="none"' +
  ' stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"' +
  ' aria-hidden="true"><path d="M8 2.5v8M4.8 7.3 8 10.5l3.2-3.2"></path>' +
  '<path d="M2.8 11.2v1.3c0 .6.4 1 1 1h8.4c.6 0 1-.4 1-1v-1.3"></path></svg>';
var TB_COLS = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor"' +
  ' stroke-width="1.4" aria-hidden="true"><rect x="2.5" y="3" width="11" height="10" rx="1.5">' +
  '</rect><path d="M6.3 3v10M9.7 3v10"></path></svg>';

function tableBar(o) {
  // Шапка панели (дизайн-система, Panel: «иконочные действия принадлежат
  // панели и стоят в её шапке»). Слева — «+» и лупа: добавляют и ищут там,
  // где глаз начинает строку (шифр, наименование), как «Создать» и «Найти» в
  // 1С; справа — «Графы»: настройка вида. Счёта строк здесь нет — он во
  // вкладке и в подвале (UI-002).
  var h = '<div class="tbar">';
  var add = "";
  if (o.upload != null) {
    add = '<label data-tb data-tbadd data-contract="' + esc(o.upload) + '" title="' +
      esc(o.addLabel) + '" aria-label="' + esc(o.addLabel) + '"><input type="file" multiple accept="' +
      UPLOAD_ACCEPT + '" hidden>' + TB_PLUS + '<span>' + esc(o.addLabel) + "</span></label>";
  } else if (o.add) {
    add = '<button type="button" data-tb data-tbadd ' + o.add + ' title="' + esc(o.addLabel) +
      '" aria-label="' + esc(o.addLabel) + '">' + TB_PLUS + '<span>' + esc(o.addLabel) + "</span></button>";
  }
  h += add;
  if (o.key) {
    var q = o.key === "ctr" ? ctrQuery : (tbQuery[o.key] || "");
    h += '<label class="find' + (q ? " has" : "") + '">' + TB_FIND +
      '<input type="search" class="ctrq" ' +
      (o.key === "ctr" ? 'id="ctrq"' : 'data-tbq="' + esc(o.key) + '"') +
      // Заполнитель всегда кончается многоточием: оно дописано здесь.
      ' value="' + esc(q) + '" placeholder="' +
      esc((o.placeholder || 'Найти').replace(/…$/, '')) + '…"' +
      ' aria-label="Поиск"></label>';
  }
  h += '<span class="sp"></span>';
  if (o.right) h += o.right;
  if (o.cols) h += '<button type="button" data-tb ' + o.cols + ">" + TB_COLS + "<span>Графы</span></button>";
  return h + "</div>";
}
/* Строка проходит поиск таблицы: текст ищется во всём, что в ней видно. */
function tbMatch(key, text) {
  var q = (tbQuery[key] || "").trim().toLowerCase();
  return !q || String(text || "").toLowerCase().indexOf(q) >= 0;
}

/* Таблица: отбор → порядок → группы или страницы. opts: rowAttr(x) — атрибуты
   строки, num — графа «№» слева (по умолчанию есть), sub(x) — строки под
   строкой (прежние версии документа), widths. Пустая таблица — шапкой граф и
   подвалом, без фразы «…пока нет» (пользователь 29.09.2026: «может
   отображать пустую таблицу всё-таки?»): видно, какие графы будут, а «+» в
   шапке панели — действие. */
function regGrid(id, cols, rows, opts) {
  opts = opts || {};
  // render — чем перерисовать таблицу после сортировки, отбора и выбора граф:
  // во вкладке плана — renderView, в реестре — renderRegistry (по умолчанию).
  GRIDS[id] = { cols: cols, rows: rows, render: opts.render };
  var barOf = function (n) {
    return opts.bar ? tableBar({ add: opts.bar.add, upload: opts.bar.upload,
      addLabel: opts.bar.addLabel, key: id, placeholder: opts.bar.placeholder,
      right: opts.bar.right,
      cols: 'data-gcols="' + esc(id) + '" title="Графы" aria-label="Графы" aria-haspopup="true"' }) : "";
  };
  var st = gv(id);
  var vis = gridOrder(id, gridShown(id, cols));
  var list = rows.slice();
  if (opts.bar && (tbQuery[id] || "").trim()) {
    // opts.text(x) — что ещё искать в строке: строки под ней (сотрудники
    // приказа) не графы, но поиск по ФИО должен их находить.
    list = list.filter(function (x) {
      return tbMatch(id, cols.filter(function (c) { return !c.act; })
        .map(function (c) { return gridVal(c, x); }).join(" ") + (opts.text ? " " + opts.text(x) : ""));
    });
  }
  Object.keys(st.filter).forEach(function (k) {
    var c = gridCol(id, k), allow = st.filter[k];
    if (c && allow) list = list.filter(function (x) { return allow.indexOf(gridVal(c, x)) >= 0; });
  });
  if (st.sort) {
    var sc = gridCol(id, st.sort.k);
    if (sc) list.sort(function (a, b) { return gridCmp(sc, gridVal(sc, a), gridVal(sc, b)) * st.sort.dir; });
  }
  // Графа группировки уходит из строк: её значение стоит в шапке группы.
  // Когда граф остаётся всего две, она остаётся — строка без неё немая.
  var gcol = st.group && vis.filter(function (c) { return c.k === st.group; })[0];
  var shown = gcol && vis.length > 2 ? vis.filter(function (c) { return c !== gcol; }) : vis;
  var num = opts.num !== false;
  // Графа действий (c.act) — без подписи, порядка и отбора: в ней ссылки.
  var head = (num ? [{ v: "№", cls: "num" }] : []).concat(shown.map(function (c) {
    return c.act ? { v: "", cls: "act" } : gridHead(id, c);
  }));
  var width = head.length;
  var out = [], cls = [], attr = [], n = 0;
  var push = function (x, first) {
    n++;
    out.push((num ? [{ v: String(first + n), cls: "num" }] : []).concat(shown.map(function (c) {
      return gridCell(c, x, st.pin.indexOf(c.k) >= 0 && !c.always);
    })));
    cls.push(opts.rowCls ? opts.rowCls(x) : "");
    attr.push(opts.rowAttr ? opts.rowAttr(x) : "");
    (opts.sub ? opts.sub(x, shown) || [] : []).forEach(function (s) {
      out.push((num ? [{ v: "", cls: "num" }] : []).concat(s.cells));
      cls.push(s.cls || "");
      attr.push(s.attr || "");
    });
  };
  var foot = "";
  if (!list.length && rows.length) {
    out.push([{ v: "Ничего не найдено", cs: width }]);
    cls.push("");
    attr.push("");
  } else if (gcol) {
    // Группы — целиком, без страниц: группа, разорванная между страницами,
    // теряет смысл итога в своей шапке. Свёрнутая прячет строки, не итог.
    var keyOf = function (x) { return gridVal(gcol, x); };
    var groups = [];
    list.forEach(function (x) { var k = keyOf(x); if (groups.indexOf(k) < 0) groups.push(k); });
    groups.sort(function (a, b) { return gridCmp(gcol, a, b); });
    var money = shown.filter(function (c) { return c.kind === "money"; });
    groups.forEach(function (g) {
      var mine = list.filter(function (x) { return keyOf(x) === g; });
      var open = !st.collapsed[g];
      var sums = money.map(function (c) {
        return esc(c.label) + " " + mo(mine.reduce(function (t, x) {
          return t + gridNum(gridVal(c, x) === GRID_EMPTY ? 0 : gridVal(c, x));
        }, 0));
      });
      out.push([{ v: '<i aria-hidden="true">' + (open ? "▾" : "▸") + "</i>" +
                     "<small>" + esc(gcol.label) + ":</small> <b>" + esc(g) + "</b>" +
                     "<small> · " + mine.length + " " + px(mine.length, "строка", "строки", "строк") +
                     (sums.length ? " · " + sums.join(" · ") : "") + "</small>", cs: width }]);
      cls.push("grp");
      attr.push('data-ggrp="' + esc(id + "|" + g) + '" tabindex="0" aria-expanded="' + open + '"');
      if (open) mine.forEach(function (x) { push(x, 0); });
    });
  } else {
    var pg = paged(id, list);
    pg.rows.forEach(function (x) { push(x, pg.from); });
    foot = pager(id, pg);
    // opts.totals — итог по графам с сервера (UI-017), строкой подвала. При
    // поиске и отборе его нет: сумма видимых строк была бы другой.
    if (opts.totals && rows.length && list.length === rows.length) {
      out.push((num ? [{ v: "", cls: "num" }] : []).concat(shown.map(function (c, i) {
        // Итог графы без единого значения пуст, а не «0».
        return c.k in opts.totals
          ? { v: opts.totals[c.k] == null ? "" : mo(opts.totals[c.k]), cls: "n" }
          : { v: i === 0 ? "Итого" : "" };
      })));
      cls.push("total");
      attr.push("");
    }
  }
  return '<div class="gridwrap" data-grid="' + esc(id) + '"><div class="tpanel">' +
    barOf(list.length) +
    table(head, out, "", { plain: true, widths: opts.widths, emptyHead: true,
      rowCls: function (i) { return cls[i]; }, rowAttr: function (i) { return attr[i]; } }) +
    foot + "</div></div>";
}

/* Перерисовать таблицу движка там, где она стоит: во вкладке плана или в
   реестре. */
function gridRender(id) {
  var g = GRIDS[id];
  ((g && g.render) || renderRegistry)();
}

/* Меню графы — одно для всех таблиц реестра (как в прототипе): вид таблицы
   сверху, отбор значений снизу словами Excel. */
function openGridMenu(btn, id, k) {
  var was = document.querySelector(".menu[data-gridmenu]");
  if (was) {
    var same = was.getAttribute("data-gridmenu") === id + "|" + k;
    was.remove();
    if (same) { btn.setAttribute("aria-expanded", "false"); return; }
  }
  var spec = GRIDS[id], col = gridCol(id, k);
  if (!spec || !col) return;
  var st = gv(id);
  var all = spec.rows.map(function (x) { return gridVal(col, x); });
  var uniq = all.filter(function (v, i) { return all.indexOf(v) === i; })
    .sort(function (a, b) { return gridCmp(col, a, b); });
  var allow = st.filter[k];
  var kind = gridIsNum(col) ? "число" : col.kind === "date" ? "дата" : "текст";
  var up = { "текст": "Сортировка от А до Я", "число": "Сортировка от минимального к максимальному",
             "дата": "Сортировка от старых к новым" }[kind];
  var down = { "текст": "Сортировка от Я до А", "число": "Сортировка от максимального к минимальному",
               "дата": "Сортировка от новых к старым" }[kind];
  var gnow = st.group && gridCol(id, st.group);
  var pinned = st.pin.indexOf(k) >= 0;
  var dirty = st.group || st.pin.length || Object.keys(st.filter).length || st.sort || st.hidden;
  var box = document.createElement("div");
  box.className = "menu";
  box.setAttribute("data-gridmenu", id + "|" + k);
  box.setAttribute("role", "group");
  box.setAttribute("aria-label", "Меню графы " + col.label);
  box.innerHTML =
    '<button type="button" class="mi" data-gm="asc">' + up + "</button>" +
    '<button type="button" class="mi" data-gm="desc">' + down + "</button>" +
    (st.sort && st.sort.k === k ? '<button type="button" class="mi" data-gm="unsort">Снять сортировку</button>' : "") +
    '<div class="msep"></div>' +
    (st.group === k
      ? '<button type="button" class="mi" data-gm="ungroup">Отменить группировку</button>'
      : '<button type="button" class="mi" data-gm="group">Группировать по этой графе</button>' +
        (gnow ? '<button type="button" class="mi" data-gm="ungroup">Отменить группировку по «' +
                esc(gnow.label) + "»</button>" : "")) +
    '<div class="msep"></div>' +
    (col.always ? "" :
      '<button type="button" class="mi" data-gm="pin">' +
        (pinned ? "Открепить графу" : "Закрепить графу слева") + "</button>" +
      '<button type="button" class="mi" data-gm="hide">Скрыть графу</button>') +
    '<button type="button" class="mi" data-gm="cols">Выбрать графы…</button>' +
    '<button type="button" class="mi" data-gm="reset"' + (dirty ? "" : " disabled") +
      ">Сбросить вид таблицы</button>" +
    '<div class="msep"></div>' +
    '<button type="button" class="mi" data-gm="clear"' + (allow ? "" : " disabled") + ">" +
      "Удалить фильтр с «" + esc(col.label) + "»</button>" +
    '<div class="msep"></div>' +
    '<div class="mfind"><input type="search" data-mq placeholder="Поиск…"' +
      ' aria-label="Поиск значения графы"></div>' +
    '<div class="mvs">' +
      '<label class="mi" data-all><input type="checkbox" data-allbox' + (allow ? "" : " checked") +
        "><span>(Выделить все)</span></label>" +
      uniq.map(function (v) {
        return '<label class="mi" data-v="' + esc(v.toLowerCase()) + '">' +
          '<input type="checkbox" value="' + esc(v) + '"' +
          (!allow || allow.indexOf(v) >= 0 ? " checked" : "") + "><span>" + esc(v) + "</span></label>";
      }).join("") + '<div class="none" hidden>Нет совпадений</div></div>';
  box.setAttribute("data-colmenu", "1");
  var r = btn.getBoundingClientRect();
  box.style.top = (r.bottom + 4) + "px";
  box.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 256)) + "px";
  document.body.appendChild(box);
  btn.setAttribute("aria-expanded", "true");
  var done = function () { box.remove(); document.removeEventListener("mousedown", away, true); gridRender(id); };
  box.addEventListener("click", function (e) {
    var a = e.target.closest("[data-gm]");
    if (!a || a.disabled) return;
    var act = a.getAttribute("data-gm");
    if (act === "asc" || act === "desc") st.sort = { k: k, dir: act === "asc" ? 1 : -1 };
    else if (act === "unsort") st.sort = null;
    else if (act === "group") { st.group = k; st.collapsed = {}; }
    else if (act === "ungroup") { st.group = null; st.collapsed = {}; }
    else if (act === "pin") st.pin = pinned ? st.pin.filter(function (x) { return x !== k; }) : st.pin.concat([k]);
    else if (act === "hide") {
      st.hidden = st.hidden || {};
      st.hidden[k] = true;
      st.pin = st.pin.filter(function (x) { return x !== k; });
      if (st.group === k) st.group = null;
      delete st.filter[k];
      gridSaveCols(id);
    } else if (act === "cols") {
      box.remove();
      document.removeEventListener("mousedown", away, true);
      openGridCols(btn, id);
      return;
    } else if (act === "reset") {
      GV[id] = { sort: null, filter: {}, group: null, collapsed: {}, pin: [], hidden: null };
      gridSaveCols(id);
    } else if (act === "clear") delete st.filter[k];
    done();
  });
  box.addEventListener("input", function (e) {
    if (!e.target || !e.target.hasAttribute("data-mq")) return;
    var q = e.target.value.trim().toLowerCase(), shown = 0;
    Array.prototype.forEach.call(box.querySelectorAll(".mvs .mi[data-v]"), function (el) {
      var hit = !q || el.getAttribute("data-v").indexOf(q) >= 0;
      el.hidden = !hit;
      if (hit) shown++;
    });
    box.querySelector(".mvs .none").hidden = shown > 0;
  });
  box.addEventListener("change", function (e) {
    if (!e.target || e.target.type !== "checkbox") return;
    var boxes = Array.prototype.slice.call(box.querySelectorAll(".mvs input[value]"));
    // «(Выделить все)» ставит и снимает разом — как в Excel.
    if (e.target.hasAttribute("data-allbox")) {
      boxes.forEach(function (i) { if (!i.closest(".mi").hidden) i.checked = e.target.checked; });
    } else {
      var allb = box.querySelector("[data-allbox]");
      allb.checked = boxes.every(function (i) { return i.checked; });
      allb.indeterminate = !allb.checked && boxes.some(function (i) { return i.checked; });
    }
    var picked = boxes.filter(function (i) { return i.checked; }).map(function (i) { return i.value; });
    // Отмечено всё — это не отбор, а его отсутствие.
    if (picked.length === uniq.length) delete st.filter[k];
    else st.filter[k] = picked;
    box.querySelector('[data-gm="clear"]').disabled = !st.filter[k];
    pageOf[id] = 1;
    gridRender(id);
  });
  var away = function (e) {
    if (box.contains(e.target) || e.target.closest("[data-gmenu]")) return;
    box.remove();
    document.removeEventListener("mousedown", away, true);
  };
  document.addEventListener("mousedown", away, true);
}

/* Выбор граф таблицы — флажками; первая графа не снимается: без неё строка
   ничья. Выбор помнится между визитами. */
function openGridCols(btn, id) {
  var was = document.querySelector(".menu[data-gridcols]");
  if (was) { was.remove(); return; }
  var spec = GRIDS[id];
  if (!spec) return;
  var vis = gridShown(id, spec.cols);
  var box = document.createElement("div");
  box.className = "menu";
  box.setAttribute("data-gridcols", id);
  box.setAttribute("role", "group");
  box.setAttribute("aria-label", "Графы таблицы");
  box.innerHTML = spec.cols.filter(function (c) { return !c.act; }).map(function (c) {
    return '<label class="mi"><input type="checkbox" value="' + esc(c.k) + '"' +
      (vis.indexOf(c) >= 0 ? " checked" : "") + (c.always ? " disabled" : "") +
      "><span>" + esc(c.label) + "</span></label>";
  }).join("");
  var r = btn.getBoundingClientRect();
  box.style.top = (r.bottom + 4) + "px";
  box.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 256)) + "px";
  document.body.appendChild(box);
  box.addEventListener("change", function (e) {
    if (!e.target || e.target.type !== "checkbox") return;
    var st = gv(id);
    st.hidden = st.hidden || {};
    st.hidden[e.target.value] = !e.target.checked;
    gridSaveCols(id);
    (spec.render || renderRegistry)();
  });
  var close = function (back) {
    box.remove();
    document.removeEventListener("mousedown", away, true);
    document.removeEventListener("keydown", onKey, true);
    if (back && btn.isConnected) btn.focus();
  };
  var away = function (e) {
    if (box.contains(e.target) || e.target.closest("[data-gcols]")) return;
    close(false);
  };
  // Esc отменяет — как в любом меню и поле таблицы (UI-017, п. 8): раньше
  // «Графы» закрывались только щелчком мимо, и с клавиатуры выйти было нечем.
  var onKey = function (e) {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(true); }
  };
  document.addEventListener("mousedown", away, true);
  document.addEventListener("keydown", onKey, true);
}

/* Графы договора, которые правят в ячейке списка (SOURCE-007). col — графа
   модели, key — ключ в ответе реестра, kind — чем правится: текстом, суммой,
   датой календарём, выбором да/нет, флажками выплат. Порядок — порядок граф. */
/* Графы списка договоров (макет Main.dc.html, COLS). Шесть показываются
   сразу, остальные добираются кнопкой «Графы»: восемнадцать граф разом
   превращают таблицу в простыню, по которой едут вбок, чтобы прочесть строку.
   Шифр не прячется — без него строка ничья. «Пакет» наш: он отвечает, есть ли
   чем считать, и в графах договора этого не видно. */
var CTR_COLS = [
  { key: "code", label: "Шифр", always: true },
  { key: "name", label: "Наименование", def: true },
  { col: "date_from", label: "Начало", def: true },
  { col: "date_to", label: "Окончание", def: true },
  // «Признак» — ГОЗ или «Приоритет 2030»: договор не бывает тем и другим
  // сразу, поэтому графа одна; без признака пусто, «нет» не пишется (UI-014).
  { key: "sign", label: "Признак", def: true },
  // ФОТ — последней графой по умолчанию, по правому краю.
  { col: "fund", label: "ФОТ, ₽", def: true },
  { col: "department", label: "Структурное подразделение" },
  { col: "priority", label: "Приоритет 2030" },
  { col: "number", label: "Номер договора" },
  { col: "account", label: "Лицевой счёт" },
  { col: "goz", label: "Гособоронзаказ" },
  { col: "kinds", label: "Разрешённые виды начислений" },
  { col: "allow_main", label: "Основное место работы" },
  { col: "allow_part_time", label: "Внутреннее совместительство" },
  { col: "salary_deadline", label: "Выплата оклада до" },
  { col: "allowance_deadline", label: "Выплата надбавок до" },
  { key: "docs", label: "Документов", num: true }
];

/* Что показано, что ищут, как отсортировано и что отобрано — помнится:
   экономист настраивает список под свою работу один раз, а не каждый заход. */
var ctrShown = null, ctrQuery = "";
var ctrSort = { key: "code", dir: 1 }, ctrFilter = {};
/* Вид списка из меню графы (прототип «ФОТ · прототип интерфейса»): по какой
   графе сгруппировано, какие группы свёрнуты, какие графы закреплены слева. */
var ctrGroup = null, ctrCollapsed = {}, ctrPin = [];

/* Порядок граф с закреплёнными: шифр первый всегда, за ним закреплённые в
   порядке закрепления, дальше остальные — так они и держатся при прокрутке
   вбок. */
function ctrPinOrder(vis) {
  var code = vis.filter(function (c) { return c.key === "code"; });
  var pinned = ctrPin.map(function (k) {
    return vis.filter(function (c) { return ctrColKey(c) === k; })[0];
  }).filter(Boolean);
  var rest = vis.filter(function (c) {
    return c.key !== "code" && pinned.indexOf(c) < 0;
  });
  return code.concat(pinned, rest);
}

/* Список договоров группами. Шапка группы — значение графы, число договоров
   и сумма денежных граф: свёрнутая группа прячет строки, но не итог. Щелчок
   по шапке сворачивает и разворачивает группу. */
function ctrGroupedTable(rows, shown, gcol, cellsOf) {
  var keyOf = function (x) { return ctrValue(gcol, x); };
  var groups = [];
  rows.forEach(function (x) { var k = keyOf(x); if (groups.indexOf(k) < 0) groups.push(k); });
  groups.sort(function (a, b) { return ctrCompare(gcol, a, b); });
  var money = shown.filter(function (c) { return c.col && ctrField(c.col).kind === "money"; });
  var out = [], cls = [], attr = [];
  groups.forEach(function (g) {
    var mine = rows.filter(function (x) { return keyOf(x) === g; });
    var open = !ctrCollapsed[g];
    var sums = money.map(function (c) {
      var f = ctrField(c.col);
      var s = mine.reduce(function (t, x) {
        var v = parseFloat(x[f.key]);
        return t + (isNaN(v) ? 0 : v);
      }, 0);
      return esc(c.label) + " " + mo(s);
    });
    out.push([{ v: '<i aria-hidden="true">' + (open ? "▾" : "▸") + "</i>" +
                   "<small>" + esc(gcol.label) + ":</small> <b>" + esc(g) + "</b>" +
                   "<small> · " + mine.length + " " +
                   px(mine.length, "договор", "договора", "договоров") +
                   (sums.length ? " · " + sums.join(" · ") : "") + "</small>",
                cs: shown.length }]);
    cls.push("grp");
    attr.push('data-grp="' + esc(g) + '" tabindex="0" aria-expanded="' + open + '"');
    if (open) mine.forEach(function (x) {
      out.push(cellsOf(x));
      cls.push(ctrMissing(x, (regData || {}).basis).length ? "draft" : "");
      attr.push('data-ctr="' + esc(x.code) + '"');
    });
  });
  // Номера — у строк договоров подряд через группы; группа без номера.
  var nums = [], k = 0;
  cls.forEach(function (c) { nums.push(c === "grp" ? "" : String(++k)); });
  return table(shown.map(ctrColHead), out, "", { plain: true,
    numOf: function (i) { return nums[i]; },
    rowCls: function (i) { return cls[i]; }, rowAttr: function (i) { return attr[i]; } });
}

/* Закреплённые графы держатся у левого края при прокрутке вбок. Отступ
   каждой — там, где она стоит без прокрутки: браузер знает его только после
   раскладки, поэтому ставится после отрисовки. */
function stickPinned() {
  Array.prototype.forEach.call(document.querySelectorAll(
      "#regview .ctrlist table, #regview .gridwrap table"), function (tb) {
    Array.prototype.forEach.call(tb.querySelectorAll("thead th"), function (th, i) {
      if (!th.classList.contains("pin")) return;
      var left = th.offsetLeft + "px";
      th.style.left = left;
      Array.prototype.forEach.call(tb.querySelectorAll("tbody tr:not(.grp)"), function (tr) {
        if (tr.children[i]) tr.children[i].style.left = left;
      });
    });
  });
}

/* Значение графы строкой — то же, что видно в таблице: по нему сортируют,
   отбирают и показывают список значений в меню. Иначе отбор ловил бы одно, а
   глаз читал другое. */
function ctrValue(c, x) {
  if (c.key === "code") return x.code || "";
  if (c.key === "name") return x.name || "";
  if (c.key === "docs") return null;
  if (c.key === "sign") return ctrSign(x);
  var f = ctrField(c.col);
  var v = x[f.key];
  if (v == null || v === "") return "не заполнено";
  return f.kind === "money" ? mo(v) : String(v);
}

/* Порядок значений: числа по величине, даты по хронологии, остальное по
   алфавиту. «не заполнено» и «не найдено» уходят в конец — это не значение. */
function ctrCompare(c, a, b) {
  var empty = function (v) { return v === "не заполнено" || v === "не найдено"; };
  if (empty(a) !== empty(b)) return empty(a) ? 1 : -1;
  var f = c.col && ctrField(c.col);
  if (f && f.kind === "money") {
    var na = parseFloat(String(a).replace(/[^0-9.,-]/g, "").replace(",", ".")) || 0;
    var nb = parseFloat(String(b).replace(/[^0-9.,-]/g, "").replace(",", ".")) || 0;
    return na - nb;
  }
  if (f && f.kind === "date") return (dmyToIso(a) || "").localeCompare(dmyToIso(b) || "");
  return String(a).localeCompare(String(b), "ru", { numeric: true });
}

/* Шапка графы: подпись с сортировкой и значок меню. Стрелка — только у той
   графы, по которой сортируют; значок синеет, когда по графе стоит отбор. */
function ctrColHead(c) {
  var k = ctrColKey(c);
  if (c.key === "docs") return c.num ? { t: c.label } : c.label;
  var on = ctrSort.key === k, filtered = !!ctrFilter[k];
  return { v: '<span class="thc">' +
    '<button type="button" data-sort="' + esc(k) + '"' + (on ? ' class="on"' : "") + ">" +
      esc(c.label) + (on ? '<span class="ar">' + (ctrSort.dir > 0 ? "▲" : "▼") + "</span>" : "") +
    "</button>" +
    '<button type="button" class="fbtn' + (filtered ? " on" : "") + '" data-cmenu="' +
      esc(k) + '" aria-haspopup="true" aria-expanded="false" aria-label="Меню графы ' +
      esc(c.label) + '">' +
      '<svg width="9" height="9" viewBox="0 0 12 12" fill="currentColor" aria-hidden="true">' +
      '<path d="M1 2h10L7 6.6V11L5 9.6V6.6z"></path></svg></button></span>',
    // Шифр держится у левого края вместе со своими ячейками; закреплённые
    // графы — за ним (ctrPinOrder, stickPinned).
    cls: "w-" + k + ((c.col && /^(money|date)$/.test(ctrField(c.col).kind)) ? " n" : "") +
         (c.key === "code" ? " key" : "") +
         (ctrPin.indexOf(k) >= 0 ? " pin" : "") };
}

/* Отбор и порядок применяются до страниц: иначе на второй странице оказалось
   бы не то, что отобрали. */
function ctrArrange(rows, vis) {
  var byKey = {};
  vis.forEach(function (c) { byKey[ctrColKey(c)] = c; });
  Object.keys(ctrFilter).forEach(function (k) {
    var c = byKey[k], allow = ctrFilter[k];
    if (!c || !allow) return;
    rows = rows.filter(function (x) { return allow.indexOf(ctrValue(c, x)) >= 0; });
  });
  var s = byKey[ctrSort.key];
  if (s) {
    rows = rows.slice().sort(function (a, b) {
      return ctrCompare(s, ctrValue(s, a), ctrValue(s, b)) * ctrSort.dir;
    });
  }
  return rows;
}
function ctrColKey(c) { return c.key || c.col; }
/* Обязательные поля договора, пустые и после заполнения из пакета
   (basis.package, UI-006): без них договор — черновик и в расчёт не входит
   (UI-014). Состав — по макету реестра: начало, окончание, ФОТ. */
var CTR_REQUIRED = [["date_from", "from", "term"], ["date_to", "to", "term"],
                    ["fund", "fund", "fund"]];
function ctrMissing(x, bs) {
  var roles = ((bs || {}).packages || {})[x.code] || [];
  var ok = function (key) {
    return roles.some(function (r) { return r.key === key && r.state === "ok"; });
  };
  return CTR_REQUIRED.filter(function (q) {
    var v = x[q[1]];
    return (v == null || v === "") && !ok(q[2]);
  }).map(function (q) { return q[0]; });
}
/* Признак договора одним словом: ГОЗ, «Приоритет 2030» или пусто. */
function ctrSign(x) {
  var yes = function (v) { return /^(да|yes|true|1)$/i.test(String(v == null ? "" : v).trim()); };
  return yes(x.goz) ? "ГОЗ" : yes(x.priority) ? "Приоритет 2030" : "";
}
/* Договор ещё разбирается: наименования нет, а документ договора в обработке. */
function ctrParsing(code, bs) {
  var ids = {};
  ((bs.contracts || {})[code] || []).forEach(function (e) {
    e.versions.forEach(function (v) { ids[v.id] = 1; });
  });
  return ((regData && regData.documents) || []).some(function (d) {
    return ids[d.id] && d.state === "ожидает";
  });
}
function ctrCols() {
  if (!ctrShown) {
    try { ctrShown = JSON.parse(localStorage.getItem("fot.ctrcols3") || "null"); }
    catch (e) { ctrShown = null; }
    if (!ctrShown) {
      ctrShown = {};
      CTR_COLS.forEach(function (c) { ctrShown[ctrColKey(c)] = !!(c.def || c.always); });
    }
  }
  return CTR_COLS.filter(function (c) { return c.always || ctrShown[ctrColKey(c)]; });
}
function ctrColsSave() {
  try { localStorage.setItem("fot.ctrcols3", JSON.stringify(ctrShown)); } catch (e) {}
}
function ctrField(col) {
  return CTR_FIELDS.filter(function (z) { return z.col === col; })[0];
}

var CTR_FIELDS = [
  { col: "number", key: "number", label: "номер", kind: "text" },
  { col: "kind", key: "kind", label: "вид", kind: "text" },
  { col: "department", key: "department", label: "подразделение", kind: "text" },
  { col: "account", key: "account", label: "счет", kind: "text" },
  { col: "goz", key: "goz", label: "ГОЗ", kind: "yesno" },
  { col: "fund", key: "fund", label: "фонд, ₽", kind: "money" },
  { col: "date_from", key: "from", label: "начало", kind: "date" },
  { col: "date_to", key: "to", label: "окончание", kind: "date" },
  { col: "kinds", key: "kinds", label: "разрешенные выплаты", kind: "kinds" },
  { col: "priority", key: "priority", label: "приоритет", kind: "yesno" },
  { col: "allow_main", key: "allow_main", label: "основное", kind: "yesno" },
  { col: "allow_part_time", key: "allow_part", label: "совмест.", kind: "yesno" },
  { col: "salary_deadline", key: "salary_deadline", label: "оклад до", kind: "date" },
  { col: "allowance_deadline", key: "allowance_deadline", label: "надбавки до", kind: "date" }
];

/* «Да/нет» — флажком (пользователь 29.09.2026: «почему он не предложил
   чекбокс? это вроде самое очевидное на да/нет»): так булевы реквизиты
   показывают 1С и любая форма. Флажок показывает то, что прочтёт расчёт:
   пустое поле build_input читает как «да» у основного места и
   совместительства и как «нет» у ГОЗ — это сказано в подсказке. Щелчок или
   пробел сохраняют явное значение; стрелки флажок не меняют. */
var YESNO_DEFAULT = { allow_main: "да", allow_part_time: "да", goz: "нет", priority: "нет" };
var YESNO_LABEL = { allow_main: "Основное место", allow_part_time: "Совместительство",
                    goz: "Гособоронзаказ", priority: "Приоритет 2030" };
function yesnoCell(f, value) {
  var v = value == null || value === "" ? "" : String(value).trim().toLowerCase();
  var dflt = YESNO_DEFAULT[f.col] || "нет";
  var name = YESNO_LABEL[f.col] || cap(f.label);
  return '<input type="checkbox" data-yesno="' + f.col + '"' +
    ((v || dflt) === "да" ? " checked" : "") + ' aria-label="' + esc(name) + '" title="' +
    esc(v ? name : "В документах нет — в расчёт идёт «" + dflt + "»") + '">';
}

/* Ячейка с правкой: показ как в таблице, щелчок или Enter открывает редактор. */
function editCell(f, value, ask, assumed) {
  var empty = value == null || value === "";
  // В списке реестра пустая клетка — прочерк: там сравнивают заполненные
  // строки, и пустая не должна весить как заполненная. В карточке свойств
  // наоборот: там ходят заполнять, и незаполненное зовут словом.
  // Пустое необязательное — пусто, без прочерка (UI-016); ask строкой —
  // слово для пустого обязательного, «не заполнено» в списке (UI-014).
  var shown = empty ? (ask ? (typeof ask === "string" ? ask : "Указать") : "")
    : f.kind === "money" ? mo(value)
    : f.col === "goz" ? '<span class="tag goz">' + esc(value) + "</span>"
    : esc(String(value));
  // assumed — объяснение допущения: поле пусто, и в расчёт идёт показанное
  // значение. Ячейка тогда цветом внимания, объяснение — в подсказке: графы
  // «Источник» у свойств договора нет (29.09.2026).
  return '<span class="ed' + (empty ? (ask ? " ask" : " dim") : "") + '" data-edit="' + f.col +
    '"' + (assumed ? " data-assumed" : "") + ' tabindex="0" role="button" title="' +
    esc((assumed ? assumed + ". " + cap(f.label) : f.label) + " — изменить") + '">' + shown + "</span>";
}

/* Редактор в ячейке. Сохраняет по Enter, смене значения или уходу из поля;
   Esc возвращает как было. Флажки выплат сохраняются, когда уходят из группы:
   иначе каждый флажок перерисовывал бы таблицу и закрывал редактор. */
function startCellEdit(span) {
  var tr = span.closest("[data-ctr]"), code = tr && tr.getAttribute("data-ctr");
  var f = CTR_FIELDS.filter(function (z) { return z.col === span.getAttribute("data-edit"); })[0];
  // «Да/нет» правится флажком (yesnoCell), а не редактором ячейки.
  if (!code || !f || f.kind === "yesno" || span.querySelector("input, select")) return;
  var x = (((regData || {}).data || {}).contracts || [])
    .filter(function (c) { return c.code === code; })[0] || {};
  var cur = x[f.key] == null ? "" : String(x[f.key]), done = false, el;
  function save(value) {
    if (done) return;
    done = true;
    if (String(value == null ? "" : value) === cur) { renderRegistry(); return; }
    var fields = {};
    fields[f.col] = value;
    api("/api/contracts/" + encodeURIComponent(code), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fields: fields })
    }).then(function () { loadRegistry(true); }, function (e) {
      alert("Не сохранилось: " + (e.message || "ошибка сети"));
      renderRegistry();
    });
  }
  function cancel() { if (!done) { done = true; renderRegistry(); } }
  span.innerHTML = "";
  if (f.kind === "kinds") {
    el = document.createElement("span");
    el.className = "ovkinds";
    el.setAttribute("role", "group");
    el.setAttribute("aria-label", f.label);
    var on = cur.toLowerCase().split(/[,;]/).map(function (s) { return s.trim(); });
    PAY_KINDS.forEach(function (k) {
      var lab = document.createElement("label"), cb = document.createElement("input");
      lab.className = "fld";
      cb.type = "checkbox"; cb.value = k; cb.checked = on.indexOf(k) >= 0;
      lab.appendChild(cb); lab.appendChild(document.createTextNode(k));
      el.appendChild(lab);
    });
    var picked = function () {
      return Array.prototype.filter.call(el.querySelectorAll("input"), function (b) {
        return b.checked;
      }).map(function (b) { return b.value; }).join(", ");
    };
    el.addEventListener("focusout", function (e) {
      if (!el.contains(e.relatedTarget)) save(picked());
    });
    el.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); save(picked()); }
      if (e.key === "Escape") { e.preventDefault(); cancel(); }
    });
  } else {
    el = document.createElement("input");
    el.type = f.kind === "date" ? "date" : "text";
    el.value = f.kind === "date" ? dmyToIso(cur) : cur;
    el.setAttribute("aria-label", f.label);
    var read = function () { return f.kind === "date" ? isoToDmy(el.value) : el.value.trim(); };
    el.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); save(read()); }
      if (e.key === "Escape") { e.preventDefault(); cancel(); }
    });
    if (f.kind === "date") el.addEventListener("change", function () { save(read()); });
    el.addEventListener("blur", function () { save(read()); });
  }
  span.appendChild(el);
  (el.tagName === "SPAN" ? el.querySelector("input") : el).focus();
}

/* Свойства договора (макет Contract.dc.html): свойство и значение.
   Значение правится в ячейке тем же механизмом, что в списке реестра
   (SOURCE-007). Графы «Источник» нет (решение пользователя 29.09.2026: «да,
   убирай источник»): источник знали у договора целиком, а не у поля — номер,
   поправленный руками, всё равно подписывался документом, шифр — всегда «из
   шапки документов», и подпись сбивала с толку. Допущение — значение, с
   которым пойдёт расчёт, — в самой ячейке цветом внимания, объяснение — в
   подсказке. */
function requisitesBlock(x, roles) {
  var by = {};
  (roles || []).forEach(function (r) { by[r.key] = r; });
  var fld = function (col) {
    return CTR_FIELDS.filter(function (z) { return z.col === col; })[0];
  };
  var gaps = 0;
  var cell = function (col) {
    var f = fld(col), v = x[f.key];
    if (v == null || v === "") gaps++;
    return editCell(f, v, true);
  };
  // Трудоёмкость не пересказывается итогом: строка ведёт на вкладку (UI-006).
  // В документах её нет (basis.package) — «нет», как пустое обязательное.
  var laborRow = function () {
    if ((by.labor || {}).state === "missing") {
      gaps++;
      return ["Трудоёмкость", { v: '<span class="ed ask">нет</span>' }];
    }
    return ["Трудоёмкость", { v: '<button type="button" class="go" data-ctab="labor">' +
                                  "Открыть трудоёмкость</button>" }];
  };
  // Начало и сроки выплат оптимизатору нужны всегда; пустые, они идут в
  // расчёт допущением (excel/load.py): начало — 1 января года плана, сроки
  // выплат — окончание договора. Показываем это значение, а не пустоту (UI-006).
  var ASSUMED = {
    date_from: function () { return "1 января года плана"; },
    salary_deadline: function () { return x.to || "окончание договора"; },
    allowance_deadline: function () { return x.to || "окончание договора"; }
  };
  // «Да/нет» — флажком: он всегда показывает значение, с которым пойдёт
  // расчёт, поэтому пропуском не считается (yesnoCell).
  var check = function (col) { var f = fld(col); return yesnoCell(f, x[f.key]); };
  var guess = function (col) {
    var f = fld(col), v = x[f.key];
    if (!ASSUMED[col] || !(v == null || v === "")) return null;
    return ASSUMED[col]();
  };
  var cellA = function (col) {
    var g = guess(col);
    if (g == null) return cell(col);
    gaps++;
    return editCell(fld(col), null, g, "Допущение: в документах нет — в расчёт идёт это значение");
  };
  /* Свойства собраны по смыслу, а не по алфавиту. Алфавит разносил «Срок
     действия», «Выплата оклада до» и «Выплата надбавок до» по разным концам
     списка, хотя сверяют их вместе; «Шифр» при этом вставал между «Трудо-
     ёмкостью» и «ФОТ». Внутри группы — в порядке заполнения. */
  var groups = [
    ["Идентификация", [
      ["Шифр", { v: '<span class="key ro">' + esc(x.code) + "</span>" }],
      ["Номер договора", { v: cell("number") }],
      ["Подразделение", { v: cell("department") }],
      ["Лицевой счёт", { v: cell("account") }]
    ]],
    ["Режим работы", [
      ["Основное место", { v: check("allow_main") }],
      ["Совместительство", { v: check("allow_part_time") }],
      ["Гособоронзаказ", { v: check("goz") }],
      ["Приоритет 2030", { v: check("priority") }]
    ]],
    ["Сроки", [
      ["Срок действия",
       { v: '<span class="pair">' + cellA("date_from") + '<span class="kd">–</span>' +
            cell("date_to") + "</span>" }],
      ["Выплата оклада до", { v: cellA("salary_deadline") }],
      ["Выплата надбавок до", { v: cellA("allowance_deadline") }]
    ]],
    ["Деньги и начисления", [
      ["ФОТ по договору, ₽", { v: cell("fund") }],
      ["Разрешённые виды начислений", { v: cell("kinds") }],
      laborRow()
    ]]
  ];
  // Заголовок группы — строка таблицы на всю ширину: отдельными таблицами
  // группы разъезжались бы по ширине граф, а значения должны стоять в один
  // столбик через всю карточку.
  var rows = [], cls = [], total = 0;
  var isGap = function (r) {
    return r.slice(1).map(function (c) { return (c && c.v) || ""; }).join(" ")
      .indexOf("ed ask") >= 0;
  };
  groups.forEach(function (g) {
    var mine = g[1].map(isGap);
    rows.push([{ v: esc(g[0]), cs: 2 }]);
    cls.push("grp" + (mine.some(Boolean) ? " gap" : ""));
    g[1].forEach(function (r, i) {
      rows.push(r);
      cls.push(mine[i] ? "gap" : "");
      total++;
    });
  });
  // Отбор — флажок с подписью, как «Показывать версии» (дизайн-разбор
  // 29.09.2026): это состояние вида, а не действие, и галочка однозначно
  // говорит «включено». Воронку пользователь не понял («не очевидно»), а в
  // продукте она уже значит меню отбора графы; счётчика незаполненных числом
  // нет (UI-019). Заполнили последнее — отбор снимается сам: иначе
  // включённый отбор оставлял пустую карточку без флажка.
  if (!gaps) reqOnlyGaps = false;
  var head = '<div class="cath"><h3>Свойства договора</h3>' +
    (gaps ? '<label class="fld"><input type="checkbox" data-reqgaps' +
            (reqOnlyGaps ? " checked" : "") + "><span>Только незаполненные</span></label>"
          : "") +
    "</div>";
  return '<div class="cat req' + (reqOnlyGaps ? " gaps" : "") + '">' + head +
    // Шапки граф нет: графы называют заголовки групп, а «свойство /
    // значение» повторяло бы очевидное (её ставили ради третьей графы).
    table(["Свойство", "Значение"], rows, "", { plain: true, nohead: true,
                          widths: ["30%", "70%"],
                          rowCls: function (i) { return cls[i]; } }) + "</div>";
}

/* Отбор «Только незаполненные»: в договоре семнадцать свойств, и глазами
   пустые в них не найти. Отбор прячет строки правилом CSS, а не перестройкой
   таблицы: иначе он закрывал бы открытый редактор соседней строки. */
var reqOnlyGaps = false;

/* Стрелка «←» рисуется контуром, а не знаком «←» из шрифта: у Golos Text
   она заметно легче остального набора и в 22-пиксельной кнопке теряется. */
/* Строки трудоёмкости договора — строки источника, а не две агрегированные
   позиции оптимизатора: так видно этап, вид работ, даты и контрольную сумму;
   объединённый помесячный план остаётся в разделе 10 результата. */
function laborRowsOf(code) {
  var d = (regData && regData.data) || {};
  var sourceRows = [];
  (d.labor || []).forEach(function (x) {
    if (code && x.contract !== code) return;
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
  return sourceRows;
}

/* Таблица трудоёмкости договора. Графы те же, что были во вкладке реестра;
   нет только «договора» — внутри договора он один на все строки. */
/* Первый или последний месяц плана строки трудоёмкости датой (ДД.ММ.ГГГГ). */
function monthEdge(x, first) {
  var ms = Object.keys(x.months || {}).map(Number).filter(function (m) { return m >= 1 && m <= 12; });
  if (!ms.length || !x.year) return null;
  var m = first ? Math.min.apply(null, ms) : Math.max.apply(null, ms);
  var day = first ? 1 : new Date(x.year, m, 0).getDate();
  return (day < 10 ? "0" : "") + day + "." + (m < 10 ? "0" : "") + m + "." + x.year;
}
/* План по месяцам для подсказки: «июн 1, июл 1, авг 1». */
function monthSplit(x) {
  var ms = Object.keys(x.months || {}).map(Number).sort(function (a, b) { return a - b; });
  if (!ms.length) return "";
  return "По месяцам: " + ms.map(function (m) {
    return MONTHS[m - 1] + " " + String(x.months[m]).replace(".", ",");
  }).join(", ");
}
function laborTable(rows, code) {
  // Строка источника — одно значение на графу: те же правила, что были в
  // таблице (деталь строки важнее агрегата, сумма считается, если её нет).
  var rowsOut = rows.map(function (row) {
    var x = row.plan, one = row.detail || {};
    var pm = one.person_months == null ? x.person_months : one.person_months;
    var cost = one.avg_cost == null ? x.avg_cost : one.avg_cost;
    var heads = one.headcount == null ? x.headcount : one.headcount;
    var sourceTotal = one.source_total_labor == null ? pm : one.source_total_labor;
    var sourceCost = one.source_unit_cost == null ? cost : one.source_unit_cost;
    var total = one.total_cost;
    if (total == null && pm != null && cost != null) total = pm * cost;
    var dec = function (v) { return v == null ? null : String(v).replace(".", ","); };
    return { year: x.year == null ? null : String(x.year), stage: one.stage || null,
             work: one.work_type || null, position: one.position || x.position,
             heads: heads == null ? null : String(heads).replace(".0", ""),
             unit: one.labor_unit || "чел.-мес.", per: dec(one.labor_per_person),
             srctotal: dec(sourceTotal), pm: dec(pm),
             cost: sourceCost == null ? null : mo(sourceCost),
             total: total == null ? null : mo(total),
             // Строка без этапа, но с планом по месяцам: период — из месяцев,
             // чтобы видно было, в каких месяцах решатель закрывает чел.-мес.
             from: one.from || monthEdge(x, true), to: one.to || monthEdge(x, false),
             split: row.detail ? null : monthSplit(x),
             source: x.source || null,
             id: x.id, manual: !x.document_id && x.source === "введено вручную", plan: x };
  });
  // Ручная строка правится в ячейке; строку из документа правят документом:
  // за ней стоит расшифровка по этапам.
  var ed = function (field, text) {
    return function (x) {
      var shown = text(x);
      if (!x.manual) return shown == null ? null : { v: esc(String(shown)) };
      return { v: '<span class="ed' + (shown == null || shown === "" ? " dim" : "") +
                  '" data-labedit="' + x.id + "|" + field + '" tabindex="0" role="button"' +
                  ' title="Изменить">' + esc(shown == null || shown === "" ? "Указать" : String(shown)) +
                  "</span>" };
    };
  };
  return regGrid("ctr-labor", [
    // Графы — в порядке документа (Структура цены, форма 9 РКМ): этап, вид
    // работ, должность, людей, на одного, чел.-мес., средняя, период этапа.
    // Итога ФОТ по строке нет: средняя — по группе внутри должности, у людей
    // зарплаты разные, и произведение не значит ничего (решение пользователя
    // 18.09.2026). Год и источник — в «Графах».
    { k: "stage", label: "Этап" },
    { k: "work", label: "Вид работ" },
    { k: "position", label: "Должность", always: true,
      cell: ed("position", function (x) { return x.position; }) },
    { k: "heads", label: "Кол-во человек", kind: "number",
      cell: ed("headcount", function (x) { return x.heads; }) },
    { k: "per", label: "На 1 человека", kind: "number" },
    { k: "pm", label: "Чел.-мес.", kind: "number",
      cell: function (x) {
        var c = ed("person_months", function (y) { return y.pm; })(x);
        // Раскладка по месяцам — подсказкой: даты этапа уже в своих графах.
        if (c && x.split) c.v = '<span title="' + esc(x.split) + '">' + c.v + "</span>";
        return c;
      } },
    { k: "cost", label: "Средняя, ₽", kind: "money",
      cell: ed("avg_cost", function (x) { return x.cost; }) },
    { k: "from", label: "Начало этапа", kind: "date" },
    { k: "to", label: "Окончание этапа", kind: "date" },
    { k: "year", label: "Год", kind: "number", off: true,
      cell: ed("year", function (x) { return x.year; }) },
    { k: "unit", label: "Единица", off: true },
    { k: "srctotal", label: "Всего по источнику", kind: "number", off: true },
    { k: "source", label: "Источник", off: true }
  ], rowsOut, {
    rowAttr: function (x) { return x.manual ? 'data-lab="' + x.id + '"' : ""; },
    bar: { add: code ? 'data-labadd="' + esc(code) + '"' : "", addLabel: "Добавить строку",
           placeholder: "Должность или этап…" }
  });
}

/* Правка ручной строки трудоёмкости в ячейке: должность — выбором из
   справочника, остальное — полем. Enter или уход из поля сохраняют, Esc —
   как было. */
function labCellEdit(span) {
  if (span.querySelector("input,select")) return;
  var parts = span.getAttribute("data-labedit").split("|"), id = +parts[0], field = parts[1];
  var d = (regData && regData.data) || {};
  var row = (d.labor || []).filter(function (x) { return x.id === id; })[0];
  if (!row) return;
  var cur = row[field] == null ? "" : String(row[field]).replace(".", ",");
  var el;
  if (field === "position") {
    var names = (d.reference || []).map(function (x) { return x.pos; }).filter(Boolean);
    if (cur && names.indexOf(cur) < 0) names.unshift(cur);
    el = document.createElement("select");
    el.innerHTML = (cur ? "" : '<option value="" selected></option>') + names.map(function (n) {
      return '<option value="' + esc(n) + '"' + (n === cur ? " selected" : "") + ">" + esc(n) + "</option>";
    }).join("");
  } else {
    el = document.createElement("input");
    el.type = "text";
    el.value = cur;
  }
  el.setAttribute("aria-label", field);
  var done = false;
  var cancel = function () { if (!done) { done = true; renderRegistry(); } };
  var save = function () {
    if (done) return;
    var v = el.value.trim();
    if (v === cur) { cancel(); return; }
    done = true;
    var body = {};
    body[field] = v;
    api("/api/labor/" + id, { method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) })
      .then(function () { loadRegistry(true); tick(); }, function (err) {
        alert("Не удалось сохранить: " + (err.message || "ошибка сети"));
        renderRegistry();
      });
  };
  el.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); save(); }
    if (e.key === "Escape") { e.preventDefault(); cancel(); }
  });
  if (el.tagName === "SELECT") el.addEventListener("change", save);
  el.addEventListener("blur", save);
  span.textContent = "";
  span.appendChild(el);
  el.focus();
}

/* Новая ручная строка: год плана по умолчанию — текущий, остальное пусто и
   заполняется в ячейках. */
function labAdd(code) {
  api("/api/labor", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ contract: code, year: new Date().getFullYear() }) })
    .then(function () { loadRegistry(true); tick(); }, function (err) {
      alert("Не удалось добавить: " + (err.message || "ошибка сети"));
    });
}

/* Меню ручной строки трудоёмкости — правым щелчком. */
function openLabMenu(id, x, y) {
  var was = document.querySelector(".menu[data-labm]");
  if (was) was.remove();
  var box = document.createElement("div");
  box.className = "menu";
  box.setAttribute("data-labm", String(id));
  box.setAttribute("role", "menu");
  box.innerHTML = '<button type="button" class="mi danger" role="menuitem" data-labdel>Удалить строку</button>';
  document.body.appendChild(box);
  var r = box.getBoundingClientRect();
  box.style.left = Math.max(8, Math.min(x, window.innerWidth - r.width - 8)) + "px";
  box.style.top = Math.max(8, Math.min(y, window.innerHeight - r.height - 8)) + "px";
  box.querySelector(".mi").focus();
  var close = function () { box.remove(); document.removeEventListener("mousedown", away, true); };
  var away = function (e) { if (!box.contains(e.target)) close(); };
  document.addEventListener("mousedown", away, true);
  box.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
  box.addEventListener("click", function (e) {
    if (!e.target.closest("[data-labdel]")) return;
    close();
    ask("Удалить строку трудоёмкости?", "Посчитанные планы будут помечены к пересмотру.",
        "Удалить строку").then(function (yes) {
      if (!yes) return;
      api("/api/labor/" + id, { method: "DELETE" })
        .then(function () { loadRegistry(true); tick(); }, function (err) {
          alert("Не удалось удалить: " + (err.message || "ошибка сети"));
        });
    });
  });
}

/* Вкладка договора, открытая сейчас. Переход в другой договор начинает со
   свойств. Вкладки «Надбавка 120» у договора нет: 120 — надбавка сотрудника,
   она правится на вкладке реестра «Надбавка 120» (UI-009, 18.09.2026). */
var ctrTab = "props";

var BACK_ICON = '<svg viewBox="0 0 16 16" width="16" height="16" fill="none"' +
  ' stroke="currentColor" stroke-width="1.5" stroke-linecap="round"' +
  ' stroke-linejoin="round" aria-hidden="true"><path d="M12.5 8H4"/>' +
  '<path d="M7.5 4.5 4 8l3.5 3.5"/></svg>';

/* Экран договора в библиотеке: путь назад, наименование, реквизиты с
   источником, документы. Из документа проваливаются дальше — в предпросмотр
   с выделением того, что из него забрали. Состав взят из макета
   Contract.dc.html решением пользователя 17.09.2026: строку полноты пакета
   заменила графа «источник» — она отвечает на тот же вопрос и по каждой
   величине, а не по пяти сразу. */
/* Вкладки договора — как в прототипе: у договора несколько сторон, и
   свойства, трудоёмкость, надбавка и файлы не должны стоять одной лентой.
   Счётчик у вкладки — сколько строк за ней; у свойств счётчика нет. */
function contractTabs(x, bs) {
  var code = x.code, d = (regData && regData.data) || {};
  var labor = laborRowsOf(code);
  var docs = (bs.contracts || {})[code] || [];
  // Рабочая группа — вкладка договора, а не раздел реестра (решение
  // пользователя 29.09.2026): приказ о рабочей группе принадлежит договору,
  // как трудоёмкость (18.09.2026).
  var group = wgShownOf(code);
  var tabs = [["props", "Свойства"], ["wg", "Рабочая группа", group.length],
              ["labor", "Трудоёмкость", labor.length], ["docs", "Документы", docs.length]];
  if (!tabs.some(function (t) { return t[0] === ctrTab; })) ctrTab = "props";
  var bar = '<div class="ctabs" role="tablist" aria-label="Разделы договора">' +
    tabs.map(function (t) {
      var on = ctrTab === t[0];
      return '<button type="button" role="tab" data-ctab="' + t[0] + '" aria-selected="' +
        (on ? "true" : "false") + '"' + (on ? ' class="on"' : "") + ">" + esc(t[1]) +
        (t[2] != null ? '<span class="c"> ' + t[2] + "</span>" : "") + "</button>";
    }).join("") + "</div>";
  var body = ctrTab === "wg"
      ? workgroupTable(group, x)
    : ctrTab === "labor"
      ? laborTable(labor, code)
    : ctrTab === "docs"
      ? catalogBlock("Документы договора", docs, code)
    : requisitesBlock(x, (bs.packages || {})[code]);
  return bar + body;
}

function contractScreen(x, bs) {
  var code = x.code;
  return '<div class="ctr" data-ctr="' + esc(code) + '">' +
    // Путь: стрелка «←», раздел ссылкой, разделитель, шифр текущей записи.
    // Стрелка обязательна — одна текстовая ссылка «Договоры» не читается как
    // выход, а путь без неё выглядит подписью. То же делает Esc.
    // Шифр не ссылка и не полужирный: он машинный идентификатор, а полужирный
    // тёмный шифр над полужирным тёмным названием — два заголовка подряд.
    // Действия записи стоят в этой же строке справа, а не у наименования:
    // путь, шифр и «⋮» — одна строка, название — вторая.
    '<nav class="issue-context" aria-label="Путь к договору">' +
      '<button type="button" class="issue-back" data-ctr-back' +
        ' title="Назад к списку договоров (Esc)"' +
        ' aria-label="Назад к списку договоров">' + BACK_ICON + "</button>" +
      '<button type="button" data-ctr-back>Договоры</button>' +
      '<span aria-hidden="true">/</span>' +
      '<span class="key" aria-current="page">' + esc(code) + "</span>" +
      rowMenu("data-ctrmenu", esc(code)) + "</nav>" +
    '<div class="cath"><h3><span class="dname" data-ctrname="' + esc(code) +
      '" role="button" tabindex="0" title="Переименовать договор">' +
      esc(x.name || "без наименования") + "</span></h3></div>" +
    contractTabs(x, bs) + "</div>";
}

/* Действия над договором — меню у наименования: удаление не равноправно
   переименованию, и в списке реестра его нет намеренно (щелчок по строке там
   проваливает в договор, промах стоил бы дорого). */
function openCtrMenu(btn, code) {
  var was = document.querySelector(".menu[data-ctrm]");
  if (was) { was.remove(); return; }
  var box = document.createElement("div");
  box.className = "menu";
  box.setAttribute("data-ctrm", code);
  box.innerHTML =
    // Без многоточия: оно значит «откроется окно и потребуется ввод».
    // Переименование правит на месте, а у удаления окно только подтверждает.
    '<button type="button" class="mi" data-ctrrename>Переименовать</button>' +
    '<div class="msep"></div>' +
    '<button type="button" class="mi danger" data-ctrdel>Удалить договор</button>';
  var r = btn.getBoundingClientRect();
  box.style.top = (r.bottom + 4) + "px";
  box.style.left = Math.max(8, Math.min(r.left - 160, window.innerWidth - 200)) + "px";
  document.body.appendChild(box);
  box.addEventListener("click", function (e) {
    if (e.target.closest("[data-ctrrename]")) {
      box.remove();
      var span = document.querySelector('.dname[data-ctrname="' + code + '"]');
      if (span) startCtrRename(span);
      return;
    }
    if (e.target.closest("[data-ctrdel]")) { box.remove(); askCtrDelete(code); }
  });
  var away = function (e) {
    if (box.contains(e.target) || e.target.closest("[data-ctrmenu]")) return;
    box.remove();
    document.removeEventListener("mousedown", away, true);
  };
  document.addEventListener("mousedown", away, true);
}

/* Окно удаления: последствия числами, а не вопрос «вы уверены». Два действия,
   потому что это два разных намерения: договор завели по ошибке — уходит с
   документами; договор лишний, а файлы нужны — документы остаются и попадают
   в «Документы без договора» (SOURCE-005). */
function askCtrDelete(code) {
  api("/api/contracts/" + encodeURIComponent(code) + "/impact").then(function (im) {
    var parts = [];
    [["документов", "документ", "документа", "документов"],
     ["строк трудоёмкости", "строка", "строки", "строк"],
     ["поступлений", "поступление", "поступления", "поступлений"],
     ["величин", "величина", "величины", "величин"],
     ["надбавок 120", "надбавка 120", "надбавки 120", "надбавок 120"]]
      .forEach(function (f) {
        var n = im[f[0]] || 0;
        if (n) parts.push(n + " " + px(n, f[1], f[2], f[3]));
      });
    var plans = im["планов в расчёте"] || 0;
    var wrap = document.createElement("div");
    wrap.className = "modal";
    wrap.innerHTML = '<div class="box ctrdel" role="dialog" aria-modal="true"' +
      ' aria-label="Удаление договора">' +
      "<h4>Удалить договор " + esc(code) + "?</h4>" +
      '<p class="dsum">' + (parts.length ? esc(parts.join(" · ")) : "Данных по нему нет") +
        (plans ? " · участвует в " + plans + " " +
                 px(plans, "плане", "планах", "планах") : "") + "</p>" +
      (im["файлы"] && im["файлы"].length
        ? '<ul class="dfiles">' + im["файлы"].map(function (n) {
            return "<li>" + esc(n) + "</li>";
          }).join("") + "</ul>"
        : "") +
      '<div class="btns">' +
        '<button type="button" class="no">Отмена</button>' +
        '<span class="sp"></span>' +
        '<button type="button" class="ok" data-keep>Удалить без документов</button>' +
        '<button type="button" class="yes" data-withdocs>Удалить с документами</button>' +
      "</div></div>";
    document.body.appendChild(wrap);
    var release = holdFocus(wrap, function () { close(); });
    var close = function () { wrap.remove(); release(); };
    wrap.addEventListener("click", function (e) {
      if (e.target === wrap || e.target.closest(".no")) { close(); return; }
      var keep = e.target.closest("[data-keep]"), all = e.target.closest("[data-withdocs]");
      if (!keep && !all) return;
      var mode = all ? "с документами" : "без документов";
      Array.prototype.forEach.call(wrap.querySelectorAll("button"), function (b) {
        b.disabled = true;
      });
      api("/api/contracts/" + encodeURIComponent(code), {
        method: "DELETE", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ "документы": mode })
      }).then(function () {
        close();
        // Удалённого договора по адресу больше нет: шаг истории заменяется
        // списком, «Назад» ведёт туда, откуда пришли к договору.
        navigate({ screen: "registry", tab: "ctr", contract: null }, "replace");
        loadRegistry(true);
        tick();
      }, function (err) {
        close();
        alert("Не удалось удалить: " + (err.message || "ошибка сети"));
      });
    });
    wrap.querySelector(".no").focus();
  }, function (err) {
    alert("Не удалось прочитать состав договора: " + (err.message || "ошибка сети"));
  });
}

/* Переименование договора на его экране — тем же полем, что название плана
   (UI-004): растёт по высоте под текст, Enter сохраняет, Esc отменяет, щелчок
   мимо сохраняет. Шифр не правится: он опознаёт договор (SOURCE-005). */
function startCtrRename(span) {
  var code = span.getAttribute("data-ctrname");
  var was = span.textContent === "без наименования" ? "" : span.textContent;
  var inp = document.createElement("textarea");
  inp.className = "rename";
  inp.rows = 1;
  inp.value = was;
  inp.setAttribute("aria-label", "Наименование договора " + code);
  span.replaceWith(inp);
  var fit = function () {
    inp.style.height = "auto";
    // scrollHeight не включает рамку: при border-box поле подрезало низ.
    inp.style.height = (inp.scrollHeight + inp.offsetHeight - inp.clientHeight) + "px";
  };
  var done = false;
  function finish(save) {
    if (done) return;
    done = true;
    var name = inp.value.trim();
    if (!save || name === was) { renderRegistry(); return; }
    fetch("/api/contracts/" + encodeURIComponent(code), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name })
    }).then(function () { loadRegistry(true); }, function () { renderRegistry(); });
  }
  inp.addEventListener("input", fit);
  inp.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  inp.addEventListener("blur", function () { finish(true); });
  fit();
  inp.focus();
  inp.setSelectionRange(inp.value.length, inp.value.length);
}

/* Окно «Создание договора» (макет NewContract.dc.html): выбирают файлы
   одного договора и создают. Шифр и наименование берутся из самих документов
   (SOURCE-005) — перепечатывать их не надо, поэтому полей ввода тут нет. */
/* Файлы, добавленные при создании договора, не остаются без договора
   (решение пользователя 18.09.2026: «если при создании договора я добавлю
   документы к договору, то документы не могут быть не привязаны»).

   Шифр по-прежнему берётся из самих документов (SOURCE-005): окно ждёт
   разбора и смотрит, к какому договору отнесён каждый файл. Файл без шифра
   прикрепляется к договору, найденному в остальных файлах этого создания.
   Если шифра нет ни в одном — окно просит его: это и есть единственный
   ручной ввод шифра, когда в документе его нет. Если договоров нашлось
   несколько — для файла без шифра выбирается один из них. */
var DOC_FINAL = { "разобран": 1, "ждет подтверждения": 1, "не распознан": 1,
                  "не прочитан": 1, "текст нечитаемый": 1 };

function attachCreated(wrap, ids, close) {
  var box = wrap.querySelector(".newctr");
  var body = box.querySelector(".nbody"), btns = box.querySelector(".btns");
  var started = Date.now();
  body.innerHTML = '<div class="nstate">Разбираю документы: ' + ids.length + "</div>";
  btns.innerHTML = '<button type="button" class="no">Закрыть</button>';
  var codesOf = function (basis, id) {
    var found = [];
    Object.keys(basis.contracts || {}).forEach(function (code) {
      basis.contracts[code].forEach(function (e) {
        if (e.versions.some(function (v) { return v.id === id; }) && found.indexOf(code) < 0) {
          found.push(code);
        }
      });
    });
    return found;
  };
  var poll = function () {
    if (!document.body.contains(wrap)) return;
    api("/api/documents").then(function (docs) {
      var list = Array.isArray(docs) ? docs : (docs.documents || []);
      var mine = list.filter(function (d) { return ids.indexOf(d.id) >= 0; });
      var done = mine.filter(function (d) { return DOC_FINAL[d.state]; }).length;
      body.querySelector(".nstate").textContent =
        "Разбираю документы: " + done + " из " + ids.length;
      if (done < ids.length && Date.now() - started < 180000) {
        setTimeout(poll, 1500);
        return;
      }
      return api("/api/case/0/basis").then(function (basis) {
        var found = [], loose = [];
        ids.forEach(function (id) {
          var c = codesOf(basis, id);
          if (c.length) c.forEach(function (x) { if (found.indexOf(x) < 0) found.push(x); });
          else loose.push(mine.filter(function (d) { return d.id === id; })[0] || { id: id });
        });
        if (!loose.length) { finish(found); return; }
        if (found.length === 1) { attach(loose, found[0]).then(function () { finish(found); }); return; }
        askCode(loose, found);
      });
    }, function () { setTimeout(poll, 3000); });
  };
  var attach = function (docs, code) {
    return Promise.all(docs.map(function (d) {
      return api("/api/document/" + d.id + "/contract", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: code })
      });
    }));
  };
  var finish = function (codes) {
    close();
    // Один договор — сразу в него: создавали его, его и показываем. Экран
    // рисуется по свежим данным реестра (в прежних договора ещё нет), адрес
    // пишется сразу — новым шагом истории.
    if (codes.length === 1) {
      regTab = "ctr"; ctrScreen = codes[0]; ctrTab = "props"; ctrOpen = ctrScreen;
      routeWrite("push");
    }
    loadRegistry(true);
    tick();
  };
  var askCode = function (loose, found) {
    body.innerHTML = '<div class="nstate">' +
      (found.length ? "У этих файлов шифра нет — выберите договор:"
                    : "В документах шифра договора нет — укажите его:") + "</div>" +
      '<ul class="nloose">' + loose.map(function (d) {
        return "<li>" + esc(d.name || ("документ " + d.id)) + "</li>";
      }).join("") + "</ul>" +
      (found.length
        ? '<label class="sf">Договор<select data-ncode>' + found.map(function (c) {
            return '<option value="' + esc(c) + '">' + esc(c) + "</option>";
          }).join("") + "</select></label>"
        : '<label class="sf">Шифр договора<input type="text" data-ncode' +
          ' placeholder="Например, C_GOZ-26…" aria-label="Шифр договора"></label>');
    btns.innerHTML = '<button type="button" class="no">Оставить без договора</button>' +
      '<span class="sp"></span><button type="button" class="ok">Прикрепить</button>';
    var field = body.querySelector("[data-ncode]");
    field.focus();
    btns.querySelector(".ok").addEventListener("click", function (e) {
      var code = String(field.value || "").trim();
      if (!code) { field.focus(); return; }
      e.target.disabled = true;
      attach(loose, code).then(function () {
        finish(found.indexOf(code) >= 0 || !found.length ? [code] : found);
      }, function (err) {
        e.target.disabled = false;
        alert("Не удалось прикрепить: " + (err.message || "ошибка сети"));
      });
    });
  };
  poll();
}

function newContractDialog() {
  var picked = [];
  var wrap = document.createElement("div");
  wrap.className = "modal";
  wrap.innerHTML =
    '<div class="box newctr" role="dialog" aria-modal="true"' +
      ' aria-label="Создание договора">' +
      '<div class="nhead"><h4>Создание договора</h4>' +
        '<button type="button" class="no x" aria-label="Закрыть">' +
        '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="m4 4 8 8M12 4l-8 8"></path></svg>' + "</button></div>" +
      '<div class="nbody">' +
        // Одна полоса приёма: она же кнопка выбора, она же принимает
        // перетаскивание. Второй кнопки «Добавить» нет — добавляют здесь же.
        '<div class="fdrop"><span class="fhint">Перетащите сюда файлы договора</span>' +
          '<label class="ghost">' + '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M8 10.6V3.2"></path><path d="M5.4 5.6 8 3l2.6 2.6"></path><path d="M3 10.6v2.4h10v-2.4"></path></svg>' +
          "Выбрать файлы…" +
          '<input type="file" multiple accept="' + UPLOAD_ACCEPT + '"' +
          ' aria-label="Файлы документов договора"></label></div>' +
        '<div class="flist"></div>' +
      "</div>" +
      '<div class="btns">' +
        '<button type="button" class="no">Отмена</button>' +
        '<span class="sp"></span>' +
        '<button type="button" class="ok" disabled>Создать договор</button>' +
      "</div></div>";
  document.body.appendChild(wrap);

  var list = wrap.querySelector(".flist");
  var make = wrap.querySelector(".ok");
  var release = holdFocus(wrap, function () { close(); });
  var close = function () { wrap.remove(); release(); };

  function size(n) {
    return n >= 1048576 ? (n / 1048576).toFixed(1).replace(".", ",") + " МБ"
         : n >= 1024 ? Math.round(n / 1024) + " КБ" : n + " Б";
  }
  function draw() {
    make.disabled = !picked.length;
    // Пустое окно — только полоса приёма с подсказкой; пустая таблица на
    // пол-окна не нужна. С файлами подсказка уходит, полоса сжимается.
    wrap.querySelector(".newctr").classList.toggle("has", picked.length > 0);
    list.innerHTML = picked.map(function (f, i) {
      return '<div class="frow"><span class="fn">' + esc(f.name) + "</span>" +
        '<span class="fs">' + size(f.size) + "</span>" +
        '<button type="button" class="del" data-drop="' + i +
        '" aria-label="Убрать файл ' + esc(f.name) + '">' +
        '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor"' +
        ' stroke-width="1.6" aria-hidden="true"><path d="m4 4 8 8M12 4l-8 8"></path></svg>' +
        "</button></div>";
    }).join("");
  }

  // Перетаскивание в полосу приёма: рамка показывает, что готова принять.
  var zone = wrap.querySelector(".fdrop");
  ["dragenter", "dragover"].forEach(function (t) {
    zone.addEventListener(t, function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();
      zone.classList.add("over");
    });
  });
  ["dragleave", "drop"].forEach(function (t) {
    zone.addEventListener(t, function () { zone.classList.remove("over"); });
  });
  zone.addEventListener("drop", function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    add(e.dataTransfer.files);
  });

  function add(files) {
    Array.prototype.forEach.call(files, function (f) {
      if (!picked.some(function (p) { return p.name === f.name && p.size === f.size; })) {
        picked.push(f);
      }
    });
    draw();
  }
  draw();

  wrap.addEventListener("change", function (e) {
    if (e.target.type !== "file") return;
    add(e.target.files);
    e.target.value = "";
  });
  wrap.addEventListener("click", function (e) {
    if (e.target === wrap) { close(); return; }
    var drop = e.target.closest("[data-drop]");
    if (drop) { picked.splice(+drop.getAttribute("data-drop"), 1); draw(); return; }
    if (e.target.closest(".no")) { close(); return; }
    if (e.target.closest(".ok") && !attaching) {
      if (!picked.length) return;
      make.disabled = true;
      make.textContent = "Разбираю…";
      attaching = true;
      sendFiles(picked).then(function (r) {
        var ids = (r && r.documents) || [];
        if (!ids.length) { close(); return; }
        attachCreated(wrap, ids, close);
      }, close);
    }
  });
  var attaching = false;
  // Фокус на самом окне, а не на поле файла: в макете кнопка выбора серая,
  // а подсветка фокуса красила её синим при каждом открытии. Esc работает,
  // Tab уводит на выбор файлов первым.
  var box = wrap.querySelector(".newctr");
  box.tabIndex = -1;
  box.focus();
}

/* Меню графы: порядок, снятие отбора и значения флажками. Значения берутся
   из того, что сейчас в реестре, и стоят по возрастанию; список прокручивается,
   потому что значений бывает больше, чем помещается. */
function openColMenu(btn, key) {
  var was = document.querySelector(".menu[data-colmenu]");
  if (was) {
    var same = was.getAttribute("data-colmenu") === key;
    was.remove();
    if (same) { btn.setAttribute("aria-expanded", "false"); return; }
  }
  var col = CTR_COLS.filter(function (c) { return ctrColKey(c) === key; })[0];
  if (!col) return;
  var all = ((regData.data || {}).contracts || []).map(function (x) {
    return ctrValue(col, x);
  });
  var uniq = all.filter(function (v, i) { return v != null && all.indexOf(v) === i; })
    .sort(function (a, b) { return ctrCompare(col, a, b); });
  var allow = ctrFilter[key];
  var box = document.createElement("div");
  box.className = "menu";
  box.setAttribute("data-colmenu", key);
  box.setAttribute("role", "group");
  box.setAttribute("aria-label", "Меню графы " + col.label);
  // Слова те же, что в Excel: экономист работает там и ждёт знакомых пунктов.
  // Порядок называется по виду величины: у текста «от А до Я», у чисел «от
  // минимального», у дат «от старых».
  var f = col.col && ctrField(col.col);
  var kind = f && f.kind === "money" ? "число" : f && f.kind === "date" ? "дата" : "текст";
  var up = { "текст": "Сортировка от А до Я",
             "число": "Сортировка от минимального к максимальному",
             "дата": "Сортировка от старых к новым" }[kind];
  var down = { "текст": "Сортировка от Я до А",
               "число": "Сортировка от максимального к минимальному",
               "дата": "Сортировка от новых к старым" }[kind];
  // Вид таблицы — как в меню графы прототипа: группировка, закрепление,
  // скрытие, выбор граф и сброс. Отбор значений — ниже, словами Excel.
  var gnow = ctrGroup && CTR_COLS.filter(function (c) { return ctrColKey(c) === ctrGroup; })[0];
  var pinned = ctrPin.indexOf(key) >= 0;
  var dirty = ctrGroup || ctrPin.length || Object.keys(ctrFilter).length ||
              ctrSort.key !== "code" || ctrSort.dir !== 1;
  box.innerHTML =
    '<button type="button" class="mi" data-cmsort="1">' + up + "</button>" +
    '<button type="button" class="mi" data-cmsort="-1">' + down + "</button>" +
    (ctrSort.key === key && (key !== "code" || ctrSort.dir !== 1)
      ? '<button type="button" class="mi" data-cmunsort>Снять сортировку</button>' : "") +
    '<div class="msep"></div>' +
    (ctrGroup === key
      ? '<button type="button" class="mi" data-cmgroup="">Отменить группировку</button>'
      : '<button type="button" class="mi" data-cmgroup="' + esc(key) + '">' +
          "Группировать по этой графе</button>" +
        (gnow ? '<button type="button" class="mi" data-cmgroup="">Отменить группировку по «' +
                esc(gnow.label) + "»</button>" : "")) +
    '<div class="msep"></div>' +
    (col.always ? "" :
      '<button type="button" class="mi" data-cmpin>' +
        (pinned ? "Открепить графу" : "Закрепить графу слева") + "</button>" +
      '<button type="button" class="mi" data-cmhide>Скрыть графу</button>') +
    '<button type="button" class="mi" data-cmcols>Выбрать графы…</button>' +
    '<button type="button" class="mi" data-cmreset' + (dirty ? "" : " disabled") +
      ">Сбросить вид таблицы</button>" +
    '<div class="msep"></div>' +
    '<button type="button" class="mi" data-cmclear' + (allow ? "" : " disabled") + '>' +
      'Удалить фильтр с «' + esc(col.label) + "»</button>" +
    '<div class="msep"></div>' +
    '<div class="mfind"><input type="search" data-mq' +
      ' placeholder="Поиск…" aria-label="Поиск значения графы"></div>' +
    '<div class="mvs">' +
      '<label class="mi" data-all><input type="checkbox" data-allbox' +
        (allow ? "" : " checked") + "><span>(Выделить все)</span></label>" +
      uniq.map(function (v) {
        return '<label class="mi" data-v="' + esc(v.toLowerCase()) + '">' +
          '<input type="checkbox" value="' + esc(v) + '"' +
          (!allow || allow.indexOf(v) >= 0 ? " checked" : "") + "><span>" + esc(v) +
          "</span></label>";
      }).join("") + '<div class="none" hidden>Нет совпадений</div></div>';
  var r = btn.getBoundingClientRect();
  box.style.top = (r.bottom + 4) + "px";
  box.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 256)) + "px";
  document.body.appendChild(box);
  btn.setAttribute("aria-expanded", "true");

  box.addEventListener("click", function (e) {
    var s = e.target.closest("[data-cmsort]");
    if (s) {
      ctrSort = { key: key, dir: +s.getAttribute("data-cmsort") };
      box.remove(); renderRegistry(); return;
    }
    if (e.target.closest("[data-cmclear]")) {
      delete ctrFilter[key];
      box.remove(); renderRegistry(); return;
    }
    if (e.target.closest("[data-cmunsort]")) {
      ctrSort = { key: "code", dir: 1 };
      box.remove(); renderRegistry(); return;
    }
    var g = e.target.closest("[data-cmgroup]");
    if (g) {
      ctrGroup = g.getAttribute("data-cmgroup") || null;
      ctrCollapsed = {};
      box.remove(); renderRegistry(); return;
    }
    if (e.target.closest("[data-cmpin]")) {
      ctrPin = ctrPin.indexOf(key) >= 0
        ? ctrPin.filter(function (k) { return k !== key; }) : ctrPin.concat([key]);
      box.remove(); renderRegistry(); return;
    }
    if (e.target.closest("[data-cmhide]")) {
      ctrShown[key] = false;
      ctrPin = ctrPin.filter(function (k) { return k !== key; });
      if (ctrGroup === key) ctrGroup = null;
      delete ctrFilter[key];
      ctrColsSave();
      box.remove(); renderRegistry(); return;
    }
    if (e.target.closest("[data-cmcols]")) {
      box.remove();
      var cb = document.querySelector("[data-cols]");
      if (cb) openColsMenu(cb);
      return;
    }
    if (e.target.closest("[data-cmreset]")) {
      ctrSort = { key: "code", dir: 1 };
      ctrFilter = {}; ctrGroup = null; ctrCollapsed = {}; ctrPin = [];
      try { localStorage.removeItem("fot.ctrcols3"); } catch (err) { /* нет хранилища */ }
      ctrShown = null;
      box.remove(); renderRegistry(); return;
    }
  });
  box.addEventListener("input", function (e) {
    if (!e.target || !e.target.hasAttribute("data-mq")) return;
    var q = e.target.value.trim().toLowerCase(), shown = 0;
    Array.prototype.forEach.call(box.querySelectorAll(".mvs .mi"), function (el) {
      var hit = !q || el.getAttribute("data-v").indexOf(q) >= 0;
      el.hidden = !hit;
      if (hit) shown++;
    });
    box.querySelector(".mvs .none").hidden = shown > 0;
  });
  box.addEventListener("change", function (e) {
    if (!e.target || e.target.type !== "checkbox") return;
    var boxes = Array.prototype.slice.call(box.querySelectorAll(".mvs input[value]"));
    // «(Выделить все)» ставит и снимает разом — как в Excel.
    if (e.target.hasAttribute("data-allbox")) {
      boxes.forEach(function (i) { if (!i.closest(".mi").hidden) i.checked = e.target.checked; });
    } else {
      var all = box.querySelector("[data-allbox]");
      all.checked = boxes.every(function (i) { return i.checked; });
      all.indeterminate = !all.checked && boxes.some(function (i) { return i.checked; });
    }
    var picked = boxes.filter(function (i) { return i.checked; })
      .map(function (i) { return i.value; });
    // Отмечено всё — это не отбор, а его отсутствие: графа не помечается.
    if (picked.length === uniq.length) delete ctrFilter[key];
    else ctrFilter[key] = picked;
    // «Удалить фильтр» оживает, как только отбор появился: меню остаётся
    // открытым, и перерисовка таблицы его не трогает.
    box.querySelector("[data-cmclear]").disabled = !ctrFilter[key];
    pageOf.ctr = 1;
    renderRegistry();
  });
  var away = function (e) {
    if (box.contains(e.target) || e.target.closest("[data-cmenu]")) return;
    box.remove();
    document.removeEventListener("mousedown", away, true);
  };
  document.addEventListener("mousedown", away, true);
}

/* Меню граф: список с флажками. Отмеченное показывается, снятое прячется;
   выбор помнится. Шифр в списке есть, но снять его нельзя — без него строка
   перестаёт быть чьей-то. */
function openColsMenu(btn) {
  var was = document.querySelector(".menu[data-colsmenu]");
  if (was) { was.remove(); btn.setAttribute("aria-expanded", "false"); return; }
  var box = document.createElement("div");
  box.className = "menu";
  box.setAttribute("data-colsmenu", "1");
  box.setAttribute("role", "group");
  box.setAttribute("aria-label", "Графы списка");
  box.innerHTML = CTR_COLS.map(function (c) {
    var k = ctrColKey(c);
    return '<label class="mi"><input type="checkbox" value="' + esc(k) + '"' +
      (c.always || ctrShown[k] ? " checked" : "") + (c.always ? " disabled" : "") +
      "><span>" + esc(c.label) + "</span></label>";
  }).join("");
  var r = btn.getBoundingClientRect();
  box.style.top = (r.bottom + 4) + "px";
  box.style.right = Math.max(8, window.innerWidth - r.right) + "px";
  document.body.appendChild(box);
  btn.setAttribute("aria-expanded", "true");
  box.addEventListener("change", function (e) {
    var el = e.target;
    if (!el || el.type !== "checkbox") return;
    ctrShown[el.value] = el.checked;
    ctrColsSave();
    renderRegistry();
    // Перерисовка убирает кнопку вместе с меню — открываем его заново.
    var again = document.querySelector("[data-cols]");
    if (again) openColsMenu(again);
  });
  var away = function (e) {
    if (box.contains(e.target) || e.target.closest("[data-cols]")) return;
    box.remove();
    document.removeEventListener("mousedown", away, true);
  };
  document.addEventListener("mousedown", away, true);
}

/* Документы вкладки со строкой загрузки внизу; перетаскивание работает на
   всю вкладку. */
/* Документы справочника вместе с документами-источниками его величин:
   оклад — из положения, П2556 — из приказа, П4 — из справки, БЭП — из
   письма. Отдельной таблицы «величина — документ-источник» нет (пользователь
   20.09.2026: «убери эту таблицу, пусть эти документы будут в таблице
   документы справочника»). Источник, чей файл лежит в реестре в другой
   группе, встаёт сюда; источник без файла в реестре — строкой «нет в
   реестре». Для какой величины документ — в графе результата. */
function referenceCatalog(d) {
  var groups = regData.basis || {};
  var entries = (groups.reference || []).slice();
  // Среди ключей основания есть и списки строк («вне расчёта», перечень
  // переопределяемых полей) — берём только записи документов.
  var pool = [];
  Object.keys(groups).forEach(function (g) {
    if (g === "reference" || !Array.isArray(groups[g])) return;
    pool = pool.concat(groups[g].filter(function (e) {
      return e && Array.isArray(e.versions);
    }));
  });
  var srcs = d.reference_sources || {}, ghosts = {}, forDoc = {};
  ["оклад", "П2556", "П4", "БЭП"].forEach(function (f) {
    var s = srcs[f];
    if (!s || !s["документ"]) return;
    var id = +s["document_id"] || 0;
    (forDoc[id || s["документ"]] = forDoc[id || s["документ"]] || []).push(f);
    var has = function (e) {
      return e && e.versions && e.versions.some(function (v) { return v.id === id; });
    };
    if (id && entries.some(has)) return;
    var found = id && pool.filter(has)[0];
    if (found) { entries.push(found); return; }
    var key = s["документ"];
    if (!ghosts[key]) {
      ghosts[key] = { root: "src:" + key, versions: [{ id: null, ghost: true, name: key,
        version: 1, uploaded: s["загружен"] || null, sources: [] }] };
    }
    ghosts[key].versions[0].sources.push(f);
  });
  entries.forEach(function (e) {
    e.versions.forEach(function (v) { v.sources = forDoc[v.id] || v.sources; });
  });
  return catalogBlock("Документы справочника",
                      entries.concat(Object.keys(ghosts).map(function (k) { return ghosts[k]; })),
                      null, "", false, false);
}

function catalogFor(group, title) {
  var entries = (regData.basis || {})[group] || [];
  // Строка загрузки внизу — и у пустого каталога: это единственный вход
  // для файлов этой вкладки (решение пользователя 18.09.2026).
  return catalogBlock(title, entries, null, "", false, false);
}

/* Флажок «в расчёте» — сразу на экране, не дожидаясь сервера: раньше после
   снятия флажка в шапке строки ещё полсекунды оставались отмеченными, а
   строка «В расчёте N из M» — прежней. Ответ сервера потом перерисует то же
   самое; отказ вернёт вид с сервера (setBasis). Фокус остаётся на флажке. */
function basisLocal(codes, included, focusSel, key) {
  // key — перечень снятых: «excluded» у договоров, «staff_excluded» у строк
  // штатного расписания.
  key = key || "excluded";
  if (!vbasis) return;
  var ex = {};
  (vbasis[key] || []).forEach(function (k) { ex[k] = true; });
  codes.forEach(function (k) { if (included) delete ex[k]; else ex[k] = true; });
  vbasis[key] = Object.keys(ex);
  renderView();
  var again = focusSel && $("view").querySelector(focusSel);
  if (again) again.focus();
}

/* Изменение основания плана: сразу на сервер, ответ — каталог заново. */
function setBasis(body, focusSel) {
  if (!caseId) return;
  api("/api/case/" + caseId + "/basis", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  }).then(function (b) {
    vbasis = b;
    showToast("Изменения сохранены");
    if (view === "ctr" || view === "emp") {
      renderView();
      // Фокус — на том же флажке: клавиатурой снимают строки подряд.
      var again = focusSel && $("view").querySelector(focusSel);
      if (again) again.focus();
    }
    tick();
  }, function (e) {
    alert("Не удалось изменить основание плана: " + (e.message || "ошибка сети"));
    if (view === "ctr" || view === "emp") setView(view);
  });
}

/* ── опрос ────────────────────────────────────────────────── */
var runWas = {};
var factSig = null;

function tick() {
  if (!caseId) return Promise.resolve();
  var id = caseId;
  return api("/api/case/" + id).then(function (s) {
    // Ответ по плану, с которого уже ушли, экран и чат не трогает: иначе
    // под новым планом мелькала переписка прежнего.
    if (id !== caseId) return;
    state = s;
    // Расчет закончился удачно — открываем отчет сами: экономист ждет
    // результат, а не сообщение о нем. Это переход — шаг истории.
    var finished = s.runs.some(function (r) {
      return r.status === "OPTIMAL" && runWas[r.id] === "идет";
    });
    runWas = {};
    s.runs.forEach(function (r) { runWas[r.id] = r.status; });
    if (finished && !inRegistry && !inAgents) {
      vrun = null; vrep = null; chosenRun = null;
      setTimeout(function () {
        if (caseId === id && !inRegistry && !inAgents) {
          navigate({ screen: "plan", view: "sum", run: null, reload: true }, "push");
        }
      }, 300);
    }
    // Перерисовываем только когда что-то изменилось: иначе прыгает прокрутка
    // и слетает набранный текст.
    var sig = JSON.stringify([s.case.stage, s.case.updated, s.messages.length,
                              s.documents.map(function (d) { return d.state; }),
                              s.activities.map(function (a) { return [a.id, a.state]; }),
                              s.questions.map(function (q) { return [q.id, !!q.answer]; }),
                              s.runs.map(function (r) { return [r.id, r.status]; })]);
    // Новые реплики, пока чат не на виду, отмечаем точкой на его значке. Чат
    // открыт и на реестре — там реплики видны, как на экране плана.
    if (lastMsgCount !== null && s.messages.length > lastMsgCount && !chatSeen()) {
      unseen += s.messages.length - lastMsgCount;
    }
    lastMsgCount = s.messages.length;
    markFeedTab();
    // Файл факта разобран, пока открыта вкладка «Факт начислений», — она
    // перечитывается сама: загрузка должна заканчиваться таблицей, а не
    // сообщением где-то в чате.
    var fsig = JSON.stringify([s.case.fact_months, s.documents.filter(function (d) {
      return d.kind === "факт начислений"; }).map(function (d) { return [d.id, d.state]; })]);
    if (factSig !== null && fsig !== factSig && view === "fact" && !inRegistry) {
      Promise.all([api("/api/case/" + id + "/data"), api("/api/case/" + id + "/basis")])
        .then(function (r) {
          if (id !== caseId || view !== "fact") return;
          vdata = r[0]; vbasis = r[1];
          renderView();
        });
    }
    factSig = fsig;
    if (sig !== lastSig) {
      lastSig = sig;
      render();
      // Перерисовка ленты не должна выбрасывать из открытого раздела.
      if (inRegistry) {
        // Состав документов или их состояние изменились — реестр открыт и
        // должен это показать, иначе загруженный файл появится в нем только
        // после повторного входа.
        loadRegistry(true);
        $("view").hidden = true; $("regview").hidden = false;
        document.querySelector(".tabs").hidden = true;
        document.querySelector(".phead").hidden = true;
      } else if (!inAgents) {
        $("view").hidden = false;
      }
    }
  }).catch(function (e) {
    if (id === caseId && e && e.status === 404) planGone();
  });
}

/* План удалили — в другой вкладке или чисткой базы: опрос упирался в 404
   каждые полторы секунды бесконечно, а экран держал план, которого нет.
   Отпускаем его. На реестре чат переходит к первому из оставшихся планов, а
   экран остаётся; на экране плана открывается первый из оставшихся, без
   планов — реестр. Шаг истории заменяется: удалённого плана по адресу нет. */
function planGone() {
  caseId = null;
  chatSync();
  loadCases().then(function (rows) {
    var next = rows && rows.length ? rows[0].id : null;
    if (inRegistry) {
      if (next) { planSwitch(next); tick(); }
      return;
    }
    if (next) open(next, "replace");
    else navigate({ screen: "registry" }, "replace");
  });
}

/* ── ширина колонки планов ─────────────────────────────────
   Имена планов длинные и похожие: сколько места отдать списку, знает
   экономист. Тянется мышью за границу и стрелками с клавиатуры; значение
   помнится между заходами. Пределы — чтобы колонка не съела рабочую область
   и не схлопнулась до нечитаемой полосы. */
var ASIDE_MIN = 180, ASIDE_MAX = 460, ASIDE_DEF = 240;

function setAside(px, save) {
  var room = Math.round(window.innerWidth * 0.4);
  px = Math.max(ASIDE_MIN, Math.min(Math.min(ASIDE_MAX, Math.max(ASIDE_MIN, room)),
                                    Math.round(px)));
  document.documentElement.style.setProperty("--aside", px + "px");
  var grip = $("asidegrip");
  if (grip) grip.setAttribute("aria-valuenow", String(px));
  if (save) { try { localStorage.setItem("fot.aside", String(px)); } catch (e) {} }
  // Обрезка имени считается от ширины колонки — пересчитываем.
  fitTitles();
}

(function () {
  var grip = $("asidegrip");
  if (!grip) return;
  var saved = null;
  try { saved = localStorage.getItem("fot.aside"); } catch (e) {}
  if (saved) setAside(+saved, false);

  var shell = document.querySelector(".shell"), dragging = false;
  grip.addEventListener("pointerdown", function (e) {
    dragging = true;
    grip.setPointerCapture(e.pointerId);
    shell.classList.add("sizing");
    e.preventDefault();
  });
  grip.addEventListener("pointermove", function (e) {
    if (!dragging) return;
    setAside(e.clientX - shell.getBoundingClientRect().left, false);
  });
  var stop = function (e) {
    if (!dragging) return;
    dragging = false;
    try { grip.releasePointerCapture(e.pointerId); } catch (err) {}
    shell.classList.remove("sizing");
    setAside(parseInt(getComputedStyle(document.documentElement)
                        .getPropertyValue("--aside"), 10), true);
  };
  grip.addEventListener("pointerup", stop);
  grip.addEventListener("pointercancel", stop);
  // Двойной щелчок возвращает ширину макета: искать её мышью не надо.
  grip.addEventListener("dblclick", function () { setAside(ASIDE_DEF, true); });

  grip.addEventListener("keydown", function (e) {
    var cur = parseInt(getComputedStyle(document.documentElement)
                         .getPropertyValue("--aside"), 10) || ASIDE_DEF;
    var step = e.shiftKey ? 32 : 8, next = null;
    if (e.key === "ArrowLeft") next = cur - step;
    if (e.key === "ArrowRight") next = cur + step;
    if (e.key === "Home") next = ASIDE_MIN;
    if (e.key === "End") next = ASIDE_MAX;
    if (next === null) return;
    e.preventDefault();
    setAside(next, true);
  });

  // Окно стало уже — колонка не должна занять всю рабочую область.
  window.addEventListener("resize", function () {
    setAside(parseInt(getComputedStyle(document.documentElement)
                        .getPropertyValue("--aside"), 10) || ASIDE_DEF, false);
  });
})();

/* ── чат с агентами ───────────────────────────────────────────
   Панель справа по значку, а не вкладка «Лента» (решение пользователя
   30.09.2026: «Лента сейчас вообще не информативна, нужна ли она?» — «чат
   панелью»). Значок — в шапке плана и в строке заголовка реестра: «сделай
   так, чтоб чат с агентами можно было из любого места открыть, и если
   открыт, чтоб не сбрасывался» (пользователь 30.09.2026). Поэтому переходы
   чат не закрывают и ленту не перерисовывают, а открытость и набранный текст
   переживают перезагрузку. Чат — переписка плана: на реестре — последнего
   открытого, и шапка чата его называет. Открытие гасит точку новых реплик и
   ставит курсор в поле; Esc и × закрывают, фокус возвращается на значок. */
var chatOpen = false;
//: Где стояла лента, пока панель была на виду: у самого низа (feedPin) или на
//: прокрутке feedTop. Скрытая панель (display:none) прокрутку не держит.
var feedPin = true, feedTop = 0;

/* Значок чата в строке заголовка реестра — тот же, что в шапке плана;
   состояние (нажат, точка, недоступен) ставит markFeedTab. */
var CHAT_TOGGLE = '<button type="button" class="iconbtn" data-chattoggle aria-controls="chatpanel"' +
  ' aria-expanded="false" title="Чат с агентами" aria-label="Чат с агентами">' +
  '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor"' +
  ' stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M14 10a1.3 1.3 0 0 1-1.3 1.3H4.7L2 14V3.3A1.3 1.3 0 0 1 3.3 2h9.4A1.3 1.3 0 0 1 14 3.3z"/>' +
  "</svg></button>";

/* Чат на экране: открыт, и есть план, чья это переписка. */
function chatShown() { return chatOpen && !!caseId; }
/* Реплики видны глазу: отчёт на весь экран прячет обе колонки, и чат тоже. */
function chatSeen() { return chatShown() && !document.querySelector(".shell.full"); }

function chatSync() {
  // Чат — правая колонка сетки: открытый, он сдвигает рабочую область, а не
  // ложится поверх панели договора (пользователь 30.09.2026: «я уверена —
  // дизайнер обосрёт» о панели поверх). На узком окне колонки нет — там он
  // выдвигается поверх (app.css).
  var shell = document.querySelector(".shell");
  if (shell) shell.classList.toggle("noside", !chatShown());
  markFeedTab();
  chatTitle();
}

/* Шапка чата. На экране плана его имя — в заголовке плана, второй раз не
   пишется (UI-002). На реестре плана на экране нет, и чат называет его сам:
   «Чат: <план>» — части связаны словом, а не знаком (UI-015). */
function chatTitle() {
  var h = document.querySelector("#chatpanel .chath h2");
  if (!h) return;
  var c = state && state.case && state.case.id === caseId ? state.case
        : casesCache.filter(function (p) { return p.id === caseId; })[0];
  var name = c && c.title ? String(c.title) : "";
  var own = (inRegistry || inAgents) && name;
  var text = own ? "Чат: " + name : "Чат с агентами";
  if (h.textContent !== text) h.textContent = text;
  if (own) h.setAttribute("title", "Чат с агентами по плану «" + name + "»");
  else h.removeAttribute("title");
}

/* quiet — открыть без курсора в поле: при входе на страницу он помешал бы
   клавиатурному ходу по ней. */
function setChat(on, quiet) {
  chatOpen = !!on;
  try { localStorage.setItem("fot.chat", chatOpen ? "1" : "0"); } catch (e) {}
  chatSync();
  if (!chatShown()) return;
  unseen = 0;
  markFeedTab();
  // Ленту не перерисовываем: render держит её свежей и при закрытой панели.
  // Прокрутка — где оставили; стояла у последней реплики — к последней.
  var f = $("feed");
  f.scrollTop = feedPin ? f.scrollHeight : feedTop;
  if (!quiet) $("text").focus();
}

/* Значок чата, видимый на этом экране: на него возвращается фокус. */
function chatToggleShown() {
  return Array.prototype.filter.call(document.querySelectorAll("[data-chattoggle]"), function (b) {
    return b.getClientRects().length > 0;
  })[0] || null;
}
function chatClose() {
  setChat(false);
  var t = chatToggleShown();
  if (t) t.focus();
}
// Значков два (шапка плана и реестр), обработчик один.
document.addEventListener("click", function (e) {
  var t = e.target.closest && e.target.closest("[data-chattoggle]");
  if (!t || t.getAttribute("aria-disabled") === "true") return;
  setChat(!chatOpen);
});
$("closechat").addEventListener("click", chatClose);
// Esc в панели закрывает её и дальше не идёт — иначе он же закрыл бы панель
// договора под ней.
document.addEventListener("keydown", function (e) {
  if (e.key === "Escape" && chatOpen && $("chatpanel").contains(document.activeElement)) {
    e.preventDefault();
    e.stopPropagation();
    chatClose();
  }
}, true);
// «Ответить в чате» у вопроса агента открывает панель.
document.addEventListener("click", function (e) {
  if (e.target.closest && e.target.closest("[data-openchat]")) setChat(true);
});
// Место ленты помним, пока она на виду: закрытая панель прокрутки не держит.
$("feed").addEventListener("scroll", function () {
  var f = $("feed");
  if (!f.clientHeight) return;
  feedPin = f.scrollHeight - f.scrollTop - f.clientHeight < 60;
  feedTop = f.scrollTop;
}, { passive: true });

/* Набранный текст — у каждого плана свой, как черновик у переписки: реплика,
   начатая про один план, не уходит в другой. Хранится в браузере и
   переживает переходы и перезагрузку; отправленная — стирается. */
var DRAFT_KEY = "fot.chat.draft";
function draftMap() {
  try {
    var m = JSON.parse(localStorage.getItem(DRAFT_KEY) || "{}");
    return m && typeof m === "object" ? m : {};
  } catch (e) { return {}; }
}
function draftPut(id, t) {
  if (!id) return;
  var m = draftMap();
  if (t) m[id] = t; else delete m[id];
  try { localStorage.setItem(DRAFT_KEY, JSON.stringify(m)); } catch (e) {}
}
function draftSave() { draftPut(caseId, $("text").value); }
function draftLoad() {
  $("text").value = (caseId && draftMap()[caseId]) || "";
}
$("text").addEventListener("input", draftSave);

/* Открытость чата — с прошлого захода. */
function chatRestore() {
  var saved = null;
  try { saved = localStorage.getItem("fot.chat"); } catch (e) {}
  if (saved === "1") setChat(true, true); else chatSync();
}

/* Сменить план: переписка, черновик и точка новых реплик — его. Экран не
   трогается — его меняет navigate(). Лента очищается сразу: переписка
   прежнего плана не показывается под новым, пока не пришёл ответ.
   Последний открытый план помнится: на реестре после перезагрузки чат —
   его. */
function planSwitch(id) {
  if (id === caseId) return;
  issueLeave();
  caseId = id;
  lastSig = "";
  vdata = null; vresult = null; vrun = null; vrep = null; chosenRun = null; pctrOpen = null;
  lastMsgCount = null; unseen = 0;
  $("feed").innerHTML = "";
  feedPin = true; feedTop = 0;
  draftLoad();
  try { localStorage.setItem("fot.plan", String(id)); } catch (e) {}
  chatSync();
}

/* ── адрес страницы (UI-021) ──────────────────────────────────
   Каждое состояние экрана — в адресе: перезагрузка не сбрасывает на
   стартовую, «Назад» и «Вперёд» браузера ходят по экранам (пользователь
   30.09.2026: «добавь урлы, сейчас у тебя путь всегда один, при перезагрузке
   всё сбрасывается. Нужно на все действия иметь пути»). Адрес — путь, без
   «#» (пользователь 30.09.2026: «зачем решётка в урлах»): сервер отдаёт ту
   же страницу на /plan/… и /registry/…, а экран по адресу выбирает она
   сама. Старая ссылка с «#/…» открывается и переписывается путём. Слова
   адреса латиницей и постоянные — от подписей вкладок они не зависят:

     /registry/<вкладка>              contracts, staff, salary, facts,
                                      positions, substitutions, allowance-120,
                                      documents
     /registry/contracts/<шифр>[/<вкладка договора>]
                                      workgroup, labor, documents; свойства —
                                      без слова
     /plan/<id>/<вкладка>             contracts, staff, salary, facts, result,
                                      orders
     /plan/<id>/contracts/<шифр>      панель договора на «Договорах»
     /plan/<id>/result/<раздел>       раздел «Плана ФОТ»; первый — без номера
     ?run=<id>                        выбранный расчёт, если не последний
     ?doc=<id>                        открытая карточка документа

   Экран, вкладка, договор, раздел отчёта, расчёт и карточка документа —
   новый шаг истории (pushState). Панель договора в плане — замена
   (replaceState): это выбор строки в списке рядом с ним, а не новый экран, и
   «Назад» перебирал бы каждый просмотренный договор, прежде чем уйти с
   вкладки. Заменой же пишется то, что экран поправил сам: договор, план,
   раздел или расчёт из старой ссылки, которых уже нет, — ближайший живой. */
var ROUTE_REG = { ctr: "contracts", emp: "staff", zp: "salary", fact: "facts",
                  ref: "positions", sub: "substitutions", secret: "allowance-120",
                  mgmt: "departments",
                  docs: "documents" };
var ROUTE_CTAB = { props: "", wg: "workgroup", labor: "labor", docs: "documents" };
var ROUTE_VIEW = { ctr: "contracts", emp: "staff", zp: "salary", fact: "facts", sum: "result",
                   ord: "orders" };

/* Ключ по слову адреса или null. */
function routeKey(map, slug) {
  for (var k in map) if (Object.prototype.hasOwnProperty.call(map, k) && map[k] === slug) return k;
  return null;
}
/* Часть адреса, закодированную неверно руками, читаем как есть. */
function routeDecode(s) {
  try { return decodeURIComponent(s); } catch (e) { return s; }
}
function routeNum(v) {
  return /^\d+$/.test(String(v == null ? "" : v)) && +v > 0 ? +v : null;
}

/* Адрес экрана — путь с запросом: r — {screen: "registry", tab, contract,
   ctab, doc} или {screen: "plan", plan, view, pctr, sec, run, doc}. */
function routePath(r) {
  if (!r) return "";
  var path, q = [];
  if (r.screen === "registry") {
    path = "/registry/" + (ROUTE_REG[r.tab] || ROUTE_REG.ctr);
    if ((r.tab || "ctr") === "ctr" && r.contract) {
      // Шифр кодируется целиком: «/» и «?» в шифре не рвут адрес.
      path += "/" + encodeURIComponent(r.contract);
      if (ROUTE_CTAB[r.ctab]) path += "/" + ROUTE_CTAB[r.ctab];
    }
  } else if (r.screen === "plan" && r.plan) {
    var v = ROUTE_VIEW[r.view] ? r.view : "ctr";
    path = "/plan/" + r.plan + "/" + ROUTE_VIEW[v];
    if (v === "ctr" && r.pctr) path += "/" + encodeURIComponent(r.pctr);
    if (v === "sum" && r.sec > 1) path += "/" + r.sec;
    if (r.run) q.push("run=" + r.run);
  } else {
    return "";
  }
  if (r.doc) q.push("doc=" + r.doc);
  if (r.screen === "registry" && r.on) q.push("on=" + r.on);
  return path + (q.length ? "?" + q.join("&") : "");
}

/* Адрес, который сейчас в строке браузера: путь с запросом; старая ссылка
   с «#/…» — её хвост. */
function routeCurrent() {
  var h = location.hash || "";
  return h.indexOf("#/") === 0 ? h.slice(1) : location.pathname + location.search;
}

/* Экран по адресу (пути; старая ссылка — с «#») или null, если адрес не наш.
   Неизвестная вкладка — первая; план — только числом. */
function parseRoute(address) {
  var h = String(address || "").replace(/^#/, "");
  if (h.charAt(0) !== "/") return null;
  var qi = h.indexOf("?");
  var parts = (qi < 0 ? h : h.slice(0, qi)).split("/").filter(Boolean).map(routeDecode);
  var q = {};
  (qi < 0 ? "" : h.slice(qi + 1)).split("&").forEach(function (kv) {
    var i = kv.indexOf("=");
    if (i > 0) q[kv.slice(0, i)] = routeDecode(kv.slice(i + 1));
  });
  var doc = routeNum(q.doc);
  // Хвост после неизвестного слова не читается: «…/nope/X» — не договор X.
  if (parts[0] === "registry") {
    var tab = routeKey(ROUTE_REG, parts[1]);
    var code = tab === "ctr" && parts[2] ? parts[2] : null;
    return { screen: "registry", tab: tab || "ctr", contract: code,
             ctab: code ? routeKey(ROUTE_CTAB, parts[3] || "") || "props" : "props", doc: doc,
             on: /^\d{4}-\d{2}-\d{2}$/.test(q.on || "") ? q.on : null };
  }
  if (parts[0] === "plan" && routeNum(parts[1])) {
    var v = routeKey(ROUTE_VIEW, parts[2]);
    return { screen: "plan", plan: routeNum(parts[1]), view: v || "ctr",
             pctr: v === "ctr" && parts[3] ? parts[3] : null,
             sec: v === "sum" ? routeNum(parts[3]) || 1 : null,
             run: routeNum(q.run), doc: doc };
  }
  return null;
}

/* Состояние экрана для адреса. null — экран ещё не выбран (вход, страница
   агентов): такое в адрес не пишется. */
function routeState() {
  if (inAgents) return null;
  var doc = docShown ? docShown.id : null;
  if (inRegistry) {
    return { screen: "registry", tab: regTab, contract: regTab === "ctr" ? ctrScreen : null,
             ctab: ctrTab, doc: doc, on: regTab === "ref" ? refOn : null };
  }
  if (!caseId) return null;
  return { screen: "plan", plan: caseId, view: view, pctr: view === "ctr" ? pctrOpen : null,
           sec: view === "sum" ? repSec : null, run: chosenRun, doc: doc };
}

//: routeApplying — экран меняется по адресу, и адрес в это время не пишется;
//: routeSeen — адрес, который экран уже показывает.
var routeApplying = false, routeSeen = null;

/* Записать адрес по состоянию экрана: how — «push» (новый шаг истории) или
   «replace». Единственное место, где меняется адрес. В записи истории —
   карточка документа, которую открыл этот шаг: закрывая её, шаг убирают
   (docLeave). */
function routeWrite(how) {
  if (routeApplying) return;
  var h = routePath(routeState());
  if (!h) return;
  // Старая ссылка с «#» переписывается путём и без хвоста «#…».
  if (h !== routeCurrent() || location.hash) {
    var doc = docShown ? docShown.id : null;
    try {
      if (how === "push") history.pushState({ doc: doc }, "", h);
      else history.replaceState({ doc: history.state && history.state.doc === doc ? doc : null },
                                "", h);
    } catch (e) { return; }
  }
  routeSeen = routeCurrent();
}

/* Всё состояние экранов, и реестра, и плана: переход меняет в нём только
   названное, остальное помнится, как и раньше, — вкладка реестра, открытый
   договор, раздел отчёта. */
function routeFull() {
  return { screen: inRegistry ? "registry" : inAgents ? "agents" : "plan",
           tab: regTab, contract: ctrScreen,
           ctab: ctrTab, plan: caseId, view: view, pctr: pctrOpen, sec: repSec,
           run: chosenRun, doc: docShown ? docShown.id : null };
}

/* Переход — одним путём для щелчка, «Назад» и входа по адресу: patch — что
   меняется ({screen, tab, contract, ctab, plan, view, pctr, sec, run, doc,
   reload — перечитать экран, даже если он тот же}), how — «push» или
   «replace». */
function navigate(patch, how) {
  var r = routeFull();
  for (var k in patch) if (Object.prototype.hasOwnProperty.call(patch, k)) r[k] = patch[k];
  applyRoute(r);
  routeWrite(how === "replace" ? "replace" : "push");
}

function applyRoute(r) {
  routeApplying = true;
  try { routeGo(r); } finally { routeApplying = false; }
}

/* Привести экран к r. Меняется только то, что отличается: панель договора и
   раздел отчёта перерисовываются на месте, вкладка и план — загружаются. */
function routeGo(r) {
  var v = ROUTE_VIEW[r.view] ? r.view
        : r.view === "plan" || r.view === "cash" || r.view === "lim" ? "sum" : "ctr";
  // Отчёт на весь экран — вид «Плана ФОТ», а не экран: уходя с него, колонки
  // возвращаем.
  if (document.querySelector(".shell.full") && !(r.screen === "plan" && v === "sum")) {
    fullReport(false);
  }
  if (r.screen === "registry") {
    var tab = REG_TABS.some(function (t) { return t.key === r.tab; }) ? r.tab : "ctr";
    // Открытый договор и его вкладка помнятся, пока смотрят список и другие
    // вкладки реестра: к ним возвращает вкладка «Договоры», как и раньше. В
    // адресе списка вкладки договора нет — она не должна сбрасываться.
    var code = tab === "ctr" ? r.contract || null : ctrScreen;
    var ctab = tab === "ctr" && code
      ? (Object.prototype.hasOwnProperty.call(ROUTE_CTAB, r.ctab) ? r.ctab : "props") : ctrTab;
    var on = tab === "ref" ? r.on || null : refOn;
    var moved = regTab !== tab || ctrScreen !== code || ctrTab !== ctab || on !== refOn;
    regTab = tab; ctrScreen = code; ctrTab = ctab; refOn = on;
    if (code) ctrOpen = code;
    if (!inRegistry || r.reload) openRegistry();
    else if (moved) { renderRegistry(); $("regview").scrollTop = 0; }
  } else if (r.screen === "plan" && r.plan) {
    var other = r.plan !== caseId, back = inRegistry || inAgents;
    if (other) planSwitch(r.plan);
    leaveRegistry();
    leaveAgents();
    var run = r.run || null, runMoved = run !== chosenRun;
    chosenRun = run;
    if (runMoved) { vrun = null; vrep = null; }
    // Панель договора помнится, пока смотрят другие вкладки плана.
    var pctr = v === "ctr" ? r.pctr || null : pctrOpen;
    var pctrMoved = pctr !== pctrOpen;
    pctrOpen = pctr;
    var sec = v === "sum" ? r.sec || 1 : repSec;
    var secMoved = sec !== repSec;
    repSec = sec;
    // «Назад» из раздела замечания — к списку замечаний с прежними отборами,
    // как ссылка «← Замечания к расчёту».
    var left = secMoved ? issueLeave() : null;
    if (other || back || runMoved || r.reload || v !== view) setView(v);
    else if (v === "ctr" && pctrMoved) renderView();
    else if (v === "sum" && secMoved) {
      if (left) { renderView(); if (repSec === 1) $("view").scrollTop = left.scroll; }
      else showSection(repSec);
    }
    if (other || back) loadCases();
    if (other) tick();
  }
  // Карточка документа — поверх любого экрана.
  var want = r.doc || null, has = docShown ? docShown.id : null;
  if (want !== has) {
    if (docShown) docShown.close(true);
    if (want) showDocument(want);
  }
  chatSync();
}

/* Адрес сменили снаружи: «Назад», «Вперёд», правка адреса руками. При правке
   руками приходят оба события — второе ничего не делает. */
function routeFromAddress() {
  if (routeCurrent() === routeSeen) return;
  routeSeen = routeCurrent();
  routeTo(parseRoute(routeCurrent()), false);
}
window.addEventListener("popstate", routeFromAddress);
window.addEventListener("hashchange", routeFromAddress);

/* Показать экран по адресу. План, которого нет, — ближайший живой экран
   (текущий план), с сообщением; но сначала список планов перечитывается:
   план могли завести в другой вкладке. Реестру чат нужен от плана —
   последнего открытого. */
function routeTo(r, fresh) {
  if (r && r.screen === "plan" && !casesCache.some(function (c) { return c.id === r.plan; })) {
    if (!fresh) { loadCases().then(function () { routeTo(r, true); }); return; }
    showToast("План не найден");
    r = null;
  }
  if (!r) { openCurrent("replace"); return; }
  if (r.screen === "registry" && !caseId) {
    var last = null;
    try { last = +localStorage.getItem("fot.plan"); } catch (e) {}
    var pick = casesCache.some(function (c) { return c.id === last; }) ? last
             : casesCache.length ? casesCache[0].id : null;
    if (pick) { planSwitch(pick); tick(); }
  }
  applyRoute(r);
  routeWrite("replace");
}

/* Вход: адрес, если он есть; иначе — как раньше, текущий план сводкой, а без
   планов — новый план. */
function routeStart(rows) {
  var r = parseRoute(routeCurrent());
  if (r) { routeTo(r, true); return; }
  if (rows.length) openCurrent("replace");
  else newCase("replace");
}

/* Расчёт выбирают в шапке плана: выбор — шаг истории и часть адреса
   (?run=), последний посчитанный в адрес не пишется — он и так по умолчанию.
   Раньше обработчик висел на #view и выбор в шапке не срабатывал. */
$("prun").addEventListener("change", function (e) {
  var s = e.target.closest && e.target.closest('select[data-sel="run"]');
  if (!s || !caseId) return;
  var id = +s.value;
  var last = ((state && state.runs) || []).filter(function (r) { return r.status === "OPTIMAL"; })[0];
  // Список приказов — тоже по расчёту из шапки: смена расчёта оставляет на
  // «Проектах приказов», а не уводит на «План ФОТ».
  navigate({ screen: "plan", view: view === "ord" ? "ord" : "sum",
             run: last && last.id === id ? null : id }, "push");
});

/* ── события ──────────────────────────────────────────────── */

if ($("openagents")) $("openagents").addEventListener("click", openAgents);

// Разделы колонки — переходы с записью в адрес. «Реестр организации» ещё раз
// перечитывает реестр, как и прежде.
$("openreg").addEventListener("click", function () {
  navigate({ screen: "registry", reload: true }, "push");
});
$("opencurrent").addEventListener("click", function () { openCurrent("push"); });

/* Версия расчета: щелчок открывает ее отчет. */
// Версии переключаются полем «Версия» в панели отчёта; карточек в колонке
// справа больше нет.

/* Повтор прерванного расчёта по закреплённому входу прежней попытки
   (RUN-002): считаются те же байты, а не сегодняшний реестр. */
document.addEventListener("click", function (e) {
  var a = e.target.closest("a[data-retry]");
  if (!a || !caseId) return;
  e.preventDefault();
  api("/api/case/" + caseId + "/run/" + a.getAttribute("data-retry") + "/retry", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ operation_id: opId() })
  }).then(tick, tick);
});

/* Правый щелчок по строке — то же меню, что за тремя точками в её конце
   (решение пользователя 28.09.2026: «можно ли опции, спрятанные в эти три
   точки, вынести в правый клик»). Кнопка остаётся: правым щелчком пользуются
   не все, и с клавиатуры действия достаются только ею. Клавиша контекстного
   меню приходит тем же событием, поэтому работает и она — меню встаёт у
   кнопки, потому что координат у такого события нет. */
function rowActionsAt(e) {
  if (e.target.closest(".menu")) return false;
  var row = e.target.closest("tr, .doc, .case");
  var b = row && row.querySelector(".more[data-docmenu], .more[data-ctrmenu]");
  if (!b) return false;
  e.preventDefault();
  menuPoint = e.clientX || e.clientY ? { x: e.clientX, y: e.clientY } : null;
  b.click();
  return true;
}

/* Правый щелчок: по заголовку графы списка договоров — меню графы, как в
   прототипе; по строке правила замещения — его меню (SOURCE-010). */
$("regview").addEventListener("contextmenu", function (e) {
  var gth = e.target.closest(".gridwrap th");
  var gmb = gth && gth.querySelector("[data-gmenu]");
  if (gmb) {
    e.preventDefault();
    var gmk = gmb.getAttribute("data-gmenu").split("|");
    openGridMenu(gmb, gmk[0], gmk[1]);
    return;
  }
  var th = e.target.closest(".ctrlist th");
  var cm = th && th.querySelector("[data-cmenu]");
  if (cm) {
    e.preventDefault();
    openColMenu(cm, cm.getAttribute("data-cmenu"));
    return;
  }
  var trl = e.target.closest("tr[data-lab]");
  if (trl) {
    e.preventDefault();
    openLabMenu(+trl.getAttribute("data-lab"), e.clientX, e.clientY);
    return;
  }
  var trs = e.target.closest("tr[data-sec]");
  if (trs) {
    e.preventDefault();
    openSecMenu(+trs.getAttribute("data-sec"), e.clientX, e.clientY);
    return;
  }
  var trm = e.target.closest("tr[data-mgmtrow]");
  if (trm) {
    e.preventDefault();
    openMgmtMenu(+trm.getAttribute("data-mgmtrow"), e.clientX, e.clientY);
    return;
  }
  var trz = e.target.closest("tr[data-zprow]");
  if (trz) {
    e.preventDefault();
    openZpMenu(+trz.getAttribute("data-zprow"), e.clientX, e.clientY);
    return;
  }
  var tr = e.target.closest("tr[data-sub]");
  if (tr) {
    e.preventDefault();
    openSubstMenu(+tr.getAttribute("data-sub"), e.clientX, e.clientY);
    return;
  }
  rowActionsAt(e);
});

/* Те же действия правым щелчком в плане: каталог документов и экран
   договора живут здесь. */
$("view").addEventListener("contextmenu", rowActionsAt);

$("regview").addEventListener("change", function (e) {
  var ron = e.target.closest("[data-refon] input");
  if (ron) {
    // Очищенное поле — сегодня: пустого состояния у справочника нет.
    refOn = ron.value && ron.value !== isoToday() ? ron.value : null;
    renderRegistry();
    routeWrite("replace");
    var back = $("regview").querySelector("[data-refon] input");
    if (back) back.focus();
    return;
  }
  var per = e.target.closest("[data-per]");
  if (per) {
    var k = per.getAttribute("data-per");
    sizeOf[k] = +per.value;
    pageOf[k] = 1;
    renderRegistry();
    return;
  }
  // Версия документа — для открытого плана: какую версию файла он берёт в
  // расчёт (basis.set_version). Прежние версии хранятся снимком, и таблицы
  // вкладки показывают строки выбранной.
  var use = e.target.closest("[data-docuse]");
  if (use && caseId) {
    use.disabled = true;
    api("/api/case/" + caseId + "/basis", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document: +use.getAttribute("data-docuse"),
                             version: +use.value })
    }).then(function () { loadRegistry(true); tick(); }, function (err) {
      alert("Не удалось выбрать версию: " + (err.message || "ошибка сети"));
      loadRegistry(true);
    });
  }
});

$("agentview").addEventListener("click", function (e) {
  // Этап плана: раскрыть его строки. Действие этапа: уйти по назначению.
  var go = e.target.closest("[data-go]");
  if (go) {
    var where = go.getAttribute("data-go");
    if (where === "реестр") { navigate({ screen: "registry" }, "push"); return; }
    if (where === "документ") { openDocument(+go.getAttribute("data-doc2")); return; }
    if (where === "план") {
      // Сразу в нужную вкладку плана одним переходом, а не таймером после
      // открытия: адрес — один шаг истории.
      navigate({ screen: "plan", plan: +go.getAttribute("data-case"),
                 view: go.getAttribute("data-tab") || "ctr", pctr: null, run: null,
                 reload: true }, "push");
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

$("regview").addEventListener("input", function (e) {
  var tq = e.target.closest("[data-tbq]");
  if (tq) {
    var key = tq.getAttribute("data-tbq");
    tbQuery[key] = tq.value;
    pageOf[key] = 1;
    var pos = tq.selectionStart;
    renderRegistry();
    var back = document.querySelector('[data-tbq="' + key.replace(/"/g, '\"') + '"]');
    if (back) {
      back.focus();
      try { back.setSelectionRange(pos, pos); } catch (err) {}
    }
    return;
  }
  var q = e.target.closest("#ctrq");
  if (!q) return;
  ctrQuery = q.value;
  pageOf.ctr = 1;
  var at = q.selectionStart;
  renderRegistry();
  var again = document.getElementById("ctrq");
  if (again) {
    again.focus();
    try { again.setSelectionRange(at, at); } catch (err) {}
  }
});

$("regview").addEventListener("keydown", function (e) {
  // Esc возвращает к списку: у стрелки «←» должен быть клавиатурный дубль,
  // иначе выход из записи доступен только мышью. Внутри редактора ячейки Esc
  // — его собственное дело: там он отменяет правку.
  if (e.key === "Escape" && ctrScreen && regTab === "ctr" && !e.target.closest("input, select")) {
    e.preventDefault();
    navigate({ screen: "registry", tab: "ctr", contract: null }, "push");
    return;
  }
  // Сумма месяца с клавиатуры: Enter и пробел открывают правку, как щелчок;
  // клавиша меню и Shift+F10 — меню строки, как правый щелчок.
  var zdk = e.target.closest("[data-zpdept]");
  if (zdk && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    zpDeptPick(zdk, +zdk.getAttribute("data-zpdept"));
    return;
  }
  var zk = e.target.closest("[data-zpedit]");
  if (zk && !e.target.closest("input") && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    zpCellEdit(zk);
    return;
  }
  var zr = e.target.closest("tr[data-zprow]");
  if (zr && !e.target.closest("input") && (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10"))) {
    e.preventDefault();
    var zrr = e.target.getBoundingClientRect();
    openZpMenu(+zr.getAttribute("data-zprow"), zrr.left + 8, zrr.bottom);
    return;
  }
  // Шапка группы таблицы реестра с клавиатуры: Enter и пробел.
  var ggk2 = e.target.closest("tr.grp[data-ggrp]");
  if (ggk2 && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    var gkey = ggk2.getAttribute("data-ggrp"), gi = gkey.indexOf("|");
    var gstate = gv(gkey.slice(0, gi)), gval = gkey.slice(gi + 1);
    gstate.collapsed[gval] = !gstate.collapsed[gval];
    renderRegistry();
    return;
  }
  // Шапка группы с клавиатуры: Enter и пробел сворачивают и разворачивают.
  var gk = e.target.closest(".ctrlist tr.grp[data-grp]");
  if (gk && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    var gkv = gk.getAttribute("data-grp");
    ctrCollapsed[gkv] = !ctrCollapsed[gkv];
    renderRegistry();
    var again = document.querySelector('.ctrlist tr.grp[data-grp="' +
      gkv.replace(/"/g, '\\"') + '"]');
    if (again) again.focus();
    return;
  }
  // Правило замещения с клавиатуры: Enter и пробел на наборе — правка,
  // клавиша меню и Shift+F10 — меню строки, тот же, что по правому щелчку.
  var sk = e.target.closest("[data-subedit]");
  if (sk && (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10"))) {
    e.preventDefault();
    var sr = sk.getBoundingClientRect();
    openSubstMenu(+sk.getAttribute("data-subedit"), sr.left + 8, sr.bottom);
    return;
  }
  if (sk && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    substDialog(substById(+sk.getAttribute("data-subedit")));
    return;
  }
  var ek = e.target.closest("[data-empctr]");
  if (ek && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    empContractsDialog(ek.getAttribute("data-empctr"));
    return;
  }
  if (e.key !== "Enter" && e.key !== " ") return;
  // Enter на ячейке с правкой открывает редактор; Enter внутри редактора —
  // его собственное дело, повторно он не открывается.
  var ed = e.key === "Enter" && e.target.closest("[data-edit]");
  if (ed && !ed.querySelector("input, select")) { e.preventDefault(); startCellEdit(ed); return; }
  if (ed) return;
  var nm = e.target.closest(".dname");
  if (!nm) return;
  e.preventDefault();
  // Тем же начертанием показано и имя документа, и наименование договора:
  // открывает документ только первое, второе переименовывает (UI-009).
  if (nm.hasAttribute("data-ctrname")) startCtrRename(nm);
  else openDocument(+nm.getAttribute("data-doc"));
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
  // Вкладка реестра — шаг истории и адрес (UI-021).
  var t = e.target.closest("[data-rtab]");
  if (t) {
    navigate({ screen: "registry", tab: t.getAttribute("data-rtab") }, "push");
    return;
  }
  // Шифр в графе «относится к» (UI-013) открывает экран договора.
  var co = e.target.closest("[data-ctropen]");
  if (co) {
    // Ссылка может вести сразу во вкладку договора: из рабочих групп
    // штатного расписания — в «Рабочую группу», а не на «Свойства»
    // (пользователь 29.09.2026: «будут ссылки на вложенные вкладки?»).
    navigate({ screen: "registry", tab: "ctr", contract: co.getAttribute("data-ctropen"),
               ctab: co.getAttribute("data-ctrtab") || "props" }, "push");
    return;
  }
  var cmb = e.target.closest("[data-ctrmenu]");
  if (cmb) { openCtrMenu(cmb, cmb.getAttribute("data-ctrmenu")); return; }
  var sb = e.target.closest("[data-sort]");
  if (sb) {
    var sk = sb.getAttribute("data-sort");
    ctrSort = { key: sk, dir: ctrSort.key === sk ? -ctrSort.dir : 1 };
    renderRegistry();
    return;
  }
  var cm = e.target.closest("[data-cmenu]");
  if (cm) {
    openColMenu(cm, cm.getAttribute("data-cmenu"));
    return;
  }
  if (e.target.closest("[data-newctr]")) { newContractDialog(); return; }
  if (e.target.closest("[data-secadd]")) { secAdd(); return; }
  var la = e.target.closest("[data-labadd]");
  if (la) { labAdd(la.getAttribute("data-labadd")); return; }
  var le = e.target.closest("[data-labedit]");
  if (le) { labCellEdit(le); return; }
  var se = e.target.closest("[data-secedit]");
  if (se) { secCellEdit(se); return; }
  var ze = e.target.closest("[data-zpedit]");
  if (ze) { zpCellEdit(ze); return; }
  var zd = e.target.closest("[data-zpdept]");
  if (zd) { zpDeptPick(zd, +zd.getAttribute("data-zpdept")); return; }
  if (e.target.closest("[data-mgmtadd]")) {
    askText("Новое управленческое подразделение", "Наименование").then(function (name) {
      if (!name) return;
      api("/api/mgmt_departments", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: name }) })
        .then(function () { loadRegistry(true); }, function (err) {
          alert("Не удалось завести подразделение: " + (err.message || "ошибка сети"));
        });
    });
    return;
  }
  // Таблицы реестра (grid): порядок щелчком по подписи графы, меню графы
  // значком, шапка группы сворачивает группу, «Графы» — выбор граф.
  var gs = e.target.closest("[data-gsort]");
  if (gs) {
    var gsk = gs.getAttribute("data-gsort").split("|"), gst = gv(gsk[0]);
    gst.sort = { k: gsk[1], dir: gst.sort && gst.sort.k === gsk[1] ? -gst.sort.dir : 1 };
    renderRegistry();
    return;
  }
  var gm = e.target.closest("[data-gmenu]");
  if (gm) {
    var gmk = gm.getAttribute("data-gmenu").split("|");
    openGridMenu(gm, gmk[0], gmk[1]);
    return;
  }
  var gg = e.target.closest("tr.grp[data-ggrp]");
  if (gg) {
    var ggk = gg.getAttribute("data-ggrp"), ggi = ggk.indexOf("|");
    var ggs = gv(ggk.slice(0, ggi)), ggv = ggk.slice(ggi + 1);
    ggs.collapsed[ggv] = !ggs.collapsed[ggv];
    renderRegistry();
    return;
  }
  var gc = e.target.closest("[data-gcols]");
  if (gc) { openGridCols(gc, gc.getAttribute("data-gcols")); return; }
  // Шапка группы списка договоров сворачивает и разворачивает группу.
  var gr = e.target.closest(".ctrlist tr.grp[data-grp]");
  if (gr) {
    var gv = gr.getAttribute("data-grp");
    ctrCollapsed[gv] = !ctrCollapsed[gv];
    renderRegistry();
    return;
  }
  if (e.target.closest("[data-subadd]")) { substDialog(null); return; }
  if (e.target.closest("[data-zpadd]")) { zpAddDialog(); return; }
  var sedit = e.target.closest("[data-subedit]");
  if (sedit) { substDialog(substById(+sedit.getAttribute("data-subedit"))); return; }
  var ectr = e.target.closest("[data-empctr]");
  if (ectr) { empContractsDialog(ectr.getAttribute("data-empctr")); return; }
  var rop = e.target.closest("[data-refopen]");
  if (rop) {
    var rpos = rop.getAttribute("data-refopen");
    refOpen[rpos] = !refOpen[rpos];
    renderRegistry();
    var rback = $("regview").querySelector('[data-refopen="' + fixAttr(rpos) + '"]');
    if (rback) rback.focus();
    return;
  }
  var cb = e.target.closest("[data-cols]");
  if (cb) {
    openColsMenu(cb);
    return;
  }
  var vb = e.target.closest("[data-vers]");
  if (vb) {
    var root = vb.getAttribute("data-vers");
    versOpen[root] = !versOpen[root];
    renderRegistry();
    return;
  }
  var va = e.target.closest("[data-allvers]");
  if (va) {
    var on = va.checked;
    Array.prototype.forEach.call(
      document.querySelectorAll("#regview [data-vers]"), function (b) {
        versOpen[b.getAttribute("data-vers")] = on;
      });
    renderRegistry();
    return;
  }

  var nm = e.target.closest(".dname");
  if (nm) {
    if (nm.hasAttribute("data-ctrname")) startCtrRename(nm);
    else openDocument(+nm.getAttribute("data-doc"));
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

  // Отбор незаполненных: переключается класс на карточке, а не состав строк,
  // — так открытый редактор соседней строки переживает переключение.
  var gapbtn = e.target.closest("input[data-reqgaps]");
  if (gapbtn) {
    reqOnlyGaps = !!gapbtn.checked;
    var card = gapbtn.closest(".req");
    if (card) card.classList.toggle("gaps", reqOnlyGaps);
    return;
  }

  // Вкладка договора — тоже адрес: «…/workgroup», «…/labor», «…/documents».
  var ct = e.target.closest("[data-ctab]");
  if (ct) {
    navigate({ screen: "registry", ctab: ct.getAttribute("data-ctab") }, "push");
    return;
  }

  // Путь «← Договоры» возвращает к списку новым шагом истории.
  if (e.target.closest("[data-ctr-back]")) {
    navigate({ screen: "registry", tab: "ctr", contract: null }, "push");
    return;
  }

  // Ячейка с правкой: щелчок открывает редактор, а не договор (SOURCE-007).
  var ed = e.target.closest("[data-edit]");
  if (ed) { startCellEdit(ed); return; }

  var tr = e.target.closest("tr[data-ctr]");
  if (tr && !e.target.closest("input, select, label, .more, .dname")) {
    // Щелчок по строке проваливает в договор, а не выделяет её (UI-009).
    navigate({ screen: "registry", tab: "ctr", contract: tr.getAttribute("data-ctr"),
               ctab: "props" }, "push");
    return;
  }

  var b = e.target.closest(".more");
  if (!b) return;
  var row = b.closest("tr"), id = b.getAttribute("data-docmenu");
  var nm = row && row.querySelector(".dname");
  var name = nm ? nm.textContent : "документ";
  var items = [{ label: "Открыть", run: function () { openDocument(+id); } }];
  // Новая версия файла — у самого документа, а не отдельной кнопкой вкладки:
  // версия принадлежит документу (решение пользователя 18.09.2026). Файл
  // встаёт следующей версией в цепочку этого документа, как бы он ни
  // назывался (SOURCE-003).
  items.push({ label: "Загрузить новую версию…", run: function () { pickVersion(id); } });
  // Договор у документа необязателен (решение пользователя 29.09.2026: «не у
  // всех документов должна быть привязка к договору… есть же просто общие
  // экономические показатели»). Указать его можно у документа, который не
  // лёг ни в договор, ни в раздел реестра, и у приказа о рабочей группе —
  // у привязанного приказа сменить, если разбор ошибся. Список встаёт под
  // именем документа: у «⋮» в конце строки он открывался у края окна
  // поверх соседних строк («криво окно появляется»).
  var wo = wgOrderOfDoc(+id);
  var at = (row && row.querySelector(".dname")) || b;
  if (wo || b.closest("[data-unlinked]")) {
    items.push({ label: wo && wo.contract ? "Сменить договор…" : "Указать договор…",
                 run: function () { docPickContract(at, +id, name, wo); } });
  }
  items.push({ label: "Удалить документ", danger: true, run: function () {
    removeDoc(id, name, function () { loadRegistry(); tick(); });
  } });
  showMenu(b, items);
});

/* Окно с одним полем. Единственный случай — шифр для документа, в котором
   его нет. Enter отправляет только из поля (форма), Esc и щелчок мимо
   отменяют; обработчик клавиш не решает за кнопку, на которой стоит фокус. */
function askText(title, label) {
  return new Promise(function (resolve) {
    var back = document.createElement("div");
    back.className = "modal";
    back.innerHTML =
      '<form class="box" role="dialog" aria-modal="true" aria-labelledby="askt" novalidate>' +
        '<h4 id="askt">' + esc(title) + "</h4>" +
        '<label class="fld">' + esc(label) +
          '<input type="text" maxlength="60" autocomplete="off"></label>' +
        '<div class="btns"><button type="button" class="no">Отмена</button>' +
        '<button type="submit" class="primary">Готово</button></div></form>';
    document.body.appendChild(back);
    var form = back.querySelector("form"), inp = form.querySelector("input"), done = false;
    var release = holdFocus(back, function () { close(null); });
    function close(value) {
      if (done) return;
      done = true;
      back.remove();
      release();
      resolve(value);
    }
    form.onsubmit = function (e) {
      e.preventDefault();
      var value = inp.value.trim();
      if (value) close(value); else inp.focus();
    };
    back.querySelector(".no").onclick = function () { close(null); };
    back.onclick = function (e) { if (e.target === back) close(null); };
    inp.focus();
  });
}

/* Таблица прокручена вбок — у закреплённой графы появляется край (UI-014).
   Событие прокрутки не всплывает, поэтому слушаем его на перехвате. */
document.addEventListener("scroll", function (e) {
  var h = e.target;
  if (h && h.classList && h.classList.contains("hscroll")) {
    h.classList.toggle("scrolled", h.scrollLeft > 0);
  }
}, true);

/* Страницы в разделах плана переключаются так же, как в реестре. */
$("view").addEventListener("click", function (e) {
  if (view === "ctr") {
    var clr = e.target.closest("[data-ovclear]");
    if (clr && pctrOpen) {
      var cleared = {};
      cleared[clr.getAttribute("data-ovclear")] = "";
      setBasis({ contract: pctrOpen, fields: cleared });
      return;
    }
    // Закрепления сотрудников в условиях договора (19.09.2026): список ФИО
    // по «+ Закрепить сотрудника», срок — двумя щелчками по клеткам месяцев,
    // выплата — на месте.
    var fxa = e.target.closest("[data-fixadd]");
    if (fxa) { fixAdd(fxa, fxa.getAttribute("data-fixadd")); return; }
    var wgk = e.target.closest("[data-wgkind]");
    if (wgk) { wgKindEdit(wgk); return; }
    // Щелчок по ячейке, а не точно по значению: пустая сумма занимала строку
    // в 18 px, и мимо неё щелчок не открывал правку (пользователь 20.09.2026:
    // «не даёт сумму назначить»).
    var fxtd = e.target.closest(".fixes td");
    var fxc = e.target.closest("[data-fixcell]") || (fxtd && fxtd.querySelector("[data-fixcell]"));
    if (fxc) { fixCellEdit(fxc); return; }
    var fxd = e.target.closest("[data-fixdel]");
    if (fxd) { fixDelete(fxd.getAttribute("data-fixdel")); return; }
    // Панель договора открывает шифр — кнопка, доступная с клавиатуры;
    // щелчок по самой строке — удобство мышью. Повторный щелчок по тому же
    // договору панель прячет (пользователь 29.09.2026: «пусть при нажатии
    // раскрывается и скрывается панель справа»); закрывают её и × и Esc.
    var gcp = e.target.closest("[data-gcols]");
    if (gcp) { openGridCols(gcp, gcp.getAttribute("data-gcols")); return; }
    if (e.target.closest("[data-pctr-close]")) { ctrPanelClose(); return; }
    var pmb = e.target.closest("[data-pmon]");
    if (pmb) {
      var pmc = pmb.getAttribute("data-pmon"), pmm = +pmb.getAttribute("data-m");
      var pms = pmonthsShut(pmc, vbasis).filter(function (m) { return m !== pmm; });
      if (pmb.getAttribute("aria-pressed") !== "true") pms.push(pmm);
      pms.sort(function (a, b) { return a - b; });
      setBasis({ contract: pmc, fields: { "месяцы без выплат": pms.join(", ") } },
               '[data-pmon="' + fixAttr(pmc) + '"][data-m="' + pmm + '"]');
      return;
    }
    if (e.target.closest("[data-pmonths]")) return;
    var tog = e.target.closest("[data-pctr-open]") ||
              (e.target.closest("input, select, label, button, a") ? null
               : e.target.closest("tr[data-pctr]"));
    if (tog) {
      var code = tog.getAttribute("data-pctr-open") || tog.getAttribute("data-pctr");
      if (pctrOpen === code) { ctrPanelClose(); return; }
      // Панель — выбор строки, а не новый экран: адрес заменяется, чтобы
      // «Назад» не перебирал каждый просмотренный договор (UI-021).
      navigate({ pctr: code }, "replace");
      // Шифр переводит в панель: фокус — на её первом органе.
      if (tog.hasAttribute("data-pctr-open")) {
        var into = $("view").querySelector("[data-pctr-close]");
        if (into) into.focus();
      }
      return;
    }
  }
  // Приказ раскрывается ▸ или щелчком по своей строке — в сотрудников.
  var oo = view === "ord" && (e.target.closest("[data-ordopen]") ||
    (e.target.closest("a, button, input, label") ? null : e.target.closest("tr[data-ordrow]")));
  if (oo) {
    var ok = oo.getAttribute("data-ordopen") || oo.getAttribute("data-ordrow");
    ordOpen[ok] = !ordOpen[ok];
    renderView();
    var back = $("view").querySelector('[data-ordopen="' + fixAttr(ok) + '"]');
    if (back && oo.hasAttribute("data-ordopen")) back.focus();
    return;
  }
  // Шифр в «Проектах приказов» ведёт к договору в этом плане: панель на
  // «Договорах». Раньше кнопка шифра здесь ничего не делала — её обработчик
  // жил только в реестре.
  var oc = view === "ord" && e.target.closest("[data-ctropen]");
  if (oc) {
    navigate({ screen: "plan", view: "ctr", pctr: oc.getAttribute("data-ctropen") }, "push");
    return;
  }
  var b = e.target.closest("[data-pg]");
  if (!b || b.disabled) return;
  var v = b.getAttribute("data-pg").split(":");
  pageOf[v[0]] = +v[1];
  renderView();
  $("view").scrollTop = 0;
});

/* Таблицы движка реестра во вкладках плана («Проекты приказов»): порядок
   щелчком по подписи, меню графы, группы, «Графы» и поиск — как в реестре,
   перерисовка — своей вкладкой (GRIDS[id].render). У «Договоров» плана
   «Графы» свои — их ловит обработчик вкладки. */
$("view").addEventListener("click", function (e) {
  if (view === "ctr" || !e.target.closest(".gridwrap")) return;
  var gs = e.target.closest("[data-gsort]");
  if (gs) {
    var gsk = gs.getAttribute("data-gsort").split("|"), gst = gv(gsk[0]);
    gst.sort = { k: gsk[1], dir: gst.sort && gst.sort.k === gsk[1] ? -gst.sort.dir : 1 };
    gridRender(gsk[0]);
    return;
  }
  var gm = e.target.closest("[data-gmenu]");
  if (gm) {
    var gmk = gm.getAttribute("data-gmenu").split("|");
    openGridMenu(gm, gmk[0], gmk[1]);
    return;
  }
  var gg = e.target.closest("tr.grp[data-ggrp]");
  if (gg) {
    var ggk = gg.getAttribute("data-ggrp"), ggi = ggk.indexOf("|");
    var ggs = gv(ggk.slice(0, ggi)), ggv = ggk.slice(ggi + 1);
    ggs.collapsed[ggv] = !ggs.collapsed[ggv];
    gridRender(ggk.slice(0, ggi));
    return;
  }
  var gc = e.target.closest("[data-gcols]");
  if (gc) openGridCols(gc, gc.getAttribute("data-gcols"));
});
$("view").addEventListener("keydown", function (e) {
  var gg = e.target.closest && e.target.closest(".gridwrap tr.grp[data-ggrp]");
  if (!gg || (e.key !== "Enter" && e.key !== " ")) return;
  e.preventDefault();
  var ggk = gg.getAttribute("data-ggrp"), ggi = ggk.indexOf("|");
  var ggs = gv(ggk.slice(0, ggi)), ggv = ggk.slice(ggi + 1);
  ggs.collapsed[ggv] = !ggs.collapsed[ggv];
  gridRender(ggk.slice(0, ggi));
});
$("view").addEventListener("input", function (e) {
  var tq = e.target.closest && e.target.closest("[data-tbq]");
  if (!tq) return;
  var key = tq.getAttribute("data-tbq");
  tbQuery[key] = tq.value;
  pageOf[key] = 1;
  var pos = tq.selectionStart;
  gridRender(key);
  var back = $("view").querySelector('[data-tbq="' + fixAttr(key) + '"]');
  if (back) {
    back.focus();
    try { back.setSelectionRange(pos, pos); } catch (err) { /* поле без выделения */ }
  }
});

$("view").addEventListener("change", function (e) {
  var per = e.target.closest("[data-per]");
  if (!per) return;
  var k = per.getAttribute("data-per");
  sizeOf[k] = +per.value;
  pageOf[k] = 1;
  renderView();
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

// Вкладка плана — шаг истории и адрес; щелчок по открытой перечитывает её,
// как и прежде.
$("tabs").addEventListener("click", function (e) {
  var b = e.target.closest(".tab");   // кнопка расчета не .tab и сюда не попадет
  if (b) navigate({ screen: "plan", view: b.getAttribute("data-view"), reload: true }, "push");
});

$("newcase").addEventListener("click", function () { newCase("push"); });

/* Куда класть файл из чата: в реестр организации (общие данные) или только
 * в этот план (песочница). Вопрос задается до выбора файла. */
var uploadScope = null;
$("attach").addEventListener("click", function () {
  // Документы — в реестр организации: песочницы «только в этот план» больше
  // нет — её документы показывала снятая колонка «Документы в этом плане»
  // (30.09.2026), без неё они стали бы невидимы.
  uploadScope = "реестр";
  $("files").click();
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
var uploadVersionOf = null;
function sendFiles(list) {
  // Копия на входе: у поля выбора список живой и обнуляется вместе с полем.
  list = list ? Array.prototype.slice.call(list) : [];
  if (!list.length) return Promise.resolve();
  if (!caseId) {
    // Документ общий для организации, но разбирает его агент в рамках плана:
    // без плана некуда писать ленту работ. Заводим и продолжаем; с реестра
    // при этом не уходим — файлы бросили туда, там и смотрят результат.
    return newCase(inRegistry ? "stay" : "replace").then(function () { return sendFiles(list); });
  }
  var fd = new FormData();
  Array.prototype.forEach.call(list, function (f) { fd.append("files", f); });
  // Из реестра — в реестр организации; из чата — как ответил экономист на
  // вопрос кнопки «+» (по умолчанию тоже в реестр).
  fd.append("scope", inRegistry ? "реестр" : (uploadScope || "реестр"));
  uploadScope = null;
  // Из каталога договора — в этот договор: файл встаёт в его каталог, а
  // версии ищутся среди документов договора (SOURCE-003).
  if (inRegistry && uploadContract) fd.append("contract", uploadContract);
  uploadContract = null;
  // Новая версия в выбранный документ — как в Confluence: в его цепочку,
  // как бы ни назывался файл.
  if (uploadVersionOf) fd.append("version_of", String(uploadVersionOf));
  uploadVersionOf = null;
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
      // Ответ загрузки — дальше: окно «Создание договора» по нему ждёт
      // разбора своих файлов и прикрепляет их к договору.
      return Promise.resolve(tick()).then(function () { return r; });
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
    var lab = e.target.closest("label[data-contract]");
    uploadContract = lab ? lab.getAttribute("data-contract") || null : null;
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
  var id = caseId;
  $("text").value = "";
  draftSave();
  // Реплика показывается сразу, ответ модели идет секунды: ждать пустого
  // экрана экономист не должен.
  sending = t;
  sendingFor = id;
  renderFeed();
  var done = function () { sending = null; return tick(); };
  api("/api/case/" + id + "/message", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: t, operation_id: opId() })
  }).then(done, function (err) {
    done();
    renderFeed();
    // Реплика не ушла — возвращаем её в поле, а не теряем молча; если ушли в
    // другой план — она ждёт в черновике своего.
    if (caseId !== id) draftPut(id, t);
    else if (!$("text").value) { $("text").value = t; draftSave(); }
    showToast("Не отправлено: " + (err.message || "ошибка сети"));
  });
});

$("solve").addEventListener("click", solve);

/* Старт: экран по адресу страницы (UI-021); без адреса — текущий план
   сводкой, без планов — новый план. Открытость чата — с прошлого захода: она
   ставится после выбора плана, чтобы значок не мигал «планов нет». Адрес
   реестра прячет шапку плана сразу, до ответа сервера: иначе при
   перезагрузке реестра мелькала шапка пустого плана. */
if ((parseRoute(routeCurrent()) || {}).screen === "registry") {
  document.querySelector(".tabs").hidden = true;
  document.querySelector(".phead").hidden = true;
}
loadCases().then(function (rows) {
  routeStart(rows);
  chatRestore();
  polling = setInterval(tick, 1500);
});

})();
