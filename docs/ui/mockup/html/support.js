/* Среда выполнения досок макета: разворачивает sc-for и sc-if, подставляет
   {{ дырки }}, вешает обработчики. Нужна, чтобы экраны открывались из
   репозитория так же, как на доске Claude Artifacts, где эту работу делает
   её собственная среда.

   Что понимает:
     {{ путь.через.точку }}   в тексте и в любом значении атрибута
     <sc-for list="{{ … }}" as="имя">   повтор по списку
     <sc-if value="{{ … }}">            ветка по условию
     onClick / onChange       обработчик из значения
     value / checked / disabled         свойства поля
   Подсказки hint-placeholder-* — для редактора доски, здесь снимаются.

   Ограничение: onChange привязан к событию change, то есть срабатывает по
   уходу из поля, а не на каждой букве. Это нарочно: перерисовка на каждой
   букве уводила бы курсор из поля. */

(function () {
  'use strict';

  var HOLE = /\{\{\s*([A-Za-z_$][\w$]*(?:\.[\w$]+)*)\s*\}\}/g;
  var SKIP_ATTR = { 'list': 1, 'as': 1, 'hint-placeholder-count': 1, 'hint-placeholder-val': 1 };

  function lookup(path, scope) {
    var parts = path.split('.');
    var cur = scope;
    for (var i = 0; i < parts.length; i++) {
      if (cur === null || cur === undefined) { return undefined; }
      cur = cur[parts[i]];
    }
    return cur;
  }

  // Значение одной дырки, занимающей атрибут целиком: нужно само значение,
  // а не его текст, иначе функция-обработчик превратится в строку.
  function soleHole(value) {
    var m = /^\{\{\s*([A-Za-z_$][\w$]*(?:\.[\w$]+)*)\s*\}\}$/.exec(value);
    return m ? m[1] : null;
  }

  function fill(text, scope) {
    return text.replace(HOLE, function (_, path) {
      var v = lookup(path, scope);
      return v === undefined || v === null || v === false ? '' : String(v);
    });
  }

  function childScope(scope, name, value) {
    var next = Object.create(scope);
    next[name] = value;
    return next;
  }

  function render(tpl, scope, out) {
    for (var i = 0; i < tpl.childNodes.length; i++) {
      renderNode(tpl.childNodes[i], scope, out);
    }
  }

  function renderNode(node, scope, out) {
    if (node.nodeType === 3) {
      var t = node.nodeValue;
      if (HOLE.test(t)) { HOLE.lastIndex = 0; t = fill(t, scope); }
      out.appendChild(document.createTextNode(t));
      return;
    }
    if (node.nodeType !== 1) { return; }

    var tag = node.tagName.toLowerCase();

    if (tag === 'sc-for') {
      var path = soleHole(node.getAttribute('list') || '');
      var list = path ? lookup(path, scope) : null;
      var as = node.getAttribute('as') || 'item';
      if (Array.isArray(list)) {
        for (var k = 0; k < list.length; k++) {
          render(node, childScope(scope, as, list[k]), out);
        }
      }
      return;
    }

    if (tag === 'sc-if') {
      var vp = soleHole(node.getAttribute('value') || '');
      if (vp && lookup(vp, scope)) { render(node, scope, out); }
      return;
    }

    var el = node.namespaceURI === 'http://www.w3.org/2000/svg'
      ? document.createElementNS(node.namespaceURI, tag)
      : document.createElement(tag);

    for (var a = 0; a < node.attributes.length; a++) {
      var at = node.attributes[a];
      var name = at.name, raw = at.value;
      if (SKIP_ATTR[name]) { continue; }

      var sole = soleHole(raw);
      var val = sole ? lookup(sole, scope) : null;

      if (name === 'onClick' || name === 'onclick') {
        if (typeof val === 'function') { el.addEventListener('click', val); }
        continue;
      }
      if (name === 'onChange' || name === 'onchange') {
        if (typeof val === 'function') { el.addEventListener('change', val); }
        continue;
      }
      if (name === 'checked' || name === 'disabled') {
        if (sole ? !!val : raw !== '' && raw !== 'false') { el.setAttribute(name, ''); }
        continue;
      }
      if (name === 'value' && (tag === 'input' || tag === 'select' || tag === 'textarea')) {
        el.setAttribute('value', sole ? (val === undefined ? '' : String(val)) : fill(raw, scope));
        continue;
      }
      el.setAttribute(name, fill(raw, scope));
    }

    render(node, scope, el);

    // select показывает выбранным то, что стоит в его value
    if (tag === 'select') {
      var want = el.getAttribute('value');
      if (want !== null) {
        for (var o = 0; o < el.options.length; o++) {
          if (el.options[o].textContent === want) { el.selectedIndex = o; break; }
        }
      }
    }
    out.appendChild(el);
  }

  window.DCLogic = function DCLogic(props) {
    this.props = props || {};
    this.state = {};
  };
  window.DCLogic.prototype.setState = function (patch) {
    for (var k in patch) { this.state[k] = patch[k]; }
    if (window.__dcDraw) { window.__dcDraw(); }
  };

  document.addEventListener('DOMContentLoaded', function () {
    var host = document.querySelector('x-dc');
    if (!host || typeof window.Component !== 'function') { return; }

    var tpl = document.createElement('div');
    while (host.firstChild) { tpl.appendChild(host.firstChild); }

    var root = document.createElement('div');
    host.appendChild(root);

    var comp = new window.Component({});

    window.__dcDraw = function () {
      // где стоял курсор, туда и вернём: перерисовывается всё дерево
      var active = document.activeElement;
      var mark = active && root.contains(active) ? active.getAttribute('data-dc-focus') : null;
      if (active && root.contains(active) && !mark) {
        mark = 'f' + Date.now();
        active.setAttribute('data-dc-focus', mark);
      }
      var caret = active && 'selectionStart' in active ? active.selectionStart : null;

      var next = document.createElement('div');
      render(tpl, comp.renderVals(), next);
      root.innerHTML = '';
      while (next.firstChild) { root.appendChild(next.firstChild); }

      if (mark) {
        var back = root.querySelector('[data-dc-focus="' + mark + '"]');
        if (back) {
          back.focus();
          if (caret !== null && 'setSelectionRange' in back) {
            try { back.setSelectionRange(caret, caret); } catch (e) { /* не текстовое поле */ }
          }
        }
      }
    };
    window.__dcDraw();
  });
})();
