/* ═══════════════════════════════════════════════════════════════════════
   ВИДЖЕТ — логика
   ───────────────────────────────────────────────────────────────────────
   Отличие от основного интерфейса одно, но принципиальное: здесь НЕТ входа.
   Посетитель сайта вуза не заводит логин, чтобы спросить, что сдавать на
   прикладную информатику.

   Вместо этого виджет берёт токен у POST /auth/guest — без пароля, роль
   `guest`. У этой роли в whitelist только официальный справочник приёма
   (backend/app/security.py, _GUEST_TABLES): справочник приёма, расписание
   БЕЗ ФИО преподавателей, корпуса и аудитории. Ни студентов, ни оценок, ни
   нагрузки. То есть доступно ровно столько же,
   сколько вуз публикует открыто, и утечка такого токена ничего не даёт.

   Бэкенд ограничивает частоту по адресу — и выдачу токенов, и сами вопросы:
   каждый вопрос стоит обращения к платной модели.
   ═══════════════════════════════════════════════════════════════════ */

'use strict';

var params = new URLSearchParams(location.search);
var API = (params.get('api') || location.origin).replace(/\/+$/, '');
var TITLE = params.get('title');

var TOKEN_KEY = 'assistant-widget:token';
var THEME_KEY = 'assistant-widget:theme';
// Бэкенд обращается к модели дважды за вопрос, при ошибке СУБД — трижды.
// Значение согласовано с REQUEST_TIMEOUT_MS основного фронтенда.
var TIMEOUT_MS = 150000;

var $ = function (id) { return document.getElementById(id); };
var dom = {
  thread: $('thread'), hello: $('hello'), hints: $('hints'),
  form: $('ask'), input: $('input'), send: $('send'), typing: $('typing'),
  themeBtn: $('themeBtn'), themeText: $('themeText'), closeBtn: $('closeBtn'),
  title: $('title'), dot: document.querySelector('.bar__dot')
};

var state = { token: null, busy: false };

if (TITLE) { dom.title.textContent = TITLE; document.title = TITLE; }

/* ── мелочи ──────────────────────────────────────────────────────── */

function el(tag, cls, text) {
  var node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function isNumeric(value) {
  var v = String(value == null ? '' : value).trim()
    .replace(/[\s ]/g, '').replace(',', '.').replace(/%$/, '');
  return v !== '' && Number.isFinite(Number(v));
}

function plural(n, one, few, many) {
  var a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}

/* ── тема ────────────────────────────────────────────────────────── */

var THEME_LABEL = { dark: 'тёмная', light: 'светлая' };

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  dom.themeText.textContent = THEME_LABEL[theme];
  try { localStorage.setItem(THEME_KEY, theme); } catch (e) { /* приватный режим */ }
}

dom.themeBtn.addEventListener('click', function () {
  applyTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light');
});

/* ── связь ───────────────────────────────────────────────────────── */

function setOnline(ok) {
  dom.dot.dataset.state = ok ? 'online' : 'offline';
  dom.dot.title = ok ? 'Сервис на связи' : 'Нет связи с сервисом';
}

/**
 * Гостевой токен. Держим его в localStorage, чтобы не просить новый на
 * каждый вопрос: выдача токенов тоже ограничена по частоте.
 */
async function ensureToken(force) {
  if (state.token && !force) return state.token;
  if (!force) {
    try {
      var saved = localStorage.getItem(TOKEN_KEY);
      if (saved) { state.token = saved; return saved; }
    } catch (e) { /* приватный режим */ }
  }

  var res = await fetch(API + '/auth/guest', { method: 'POST' });
  if (res.status === 429) {
    var wait = res.headers.get('Retry-After');
    throw new Error('Слишком много обращений'
      + (wait ? '. Попробуйте через ' + wait + ' с.' : '. Попробуйте позже.'));
  }
  if (!res.ok) throw new Error('Сервис недоступен (' + res.status + ')');

  var data = await res.json();
  if (!data || !data.access_token) throw new Error('Сервис не выдал токен');
  state.token = data.access_token;
  try { localStorage.setItem(TOKEN_KEY, state.token); } catch (e) {}
  return state.token;
}

/* ── разбор ответа ───────────────────────────────────────────────── */

var REFUSAL = { rejected: 1, out_of_scope: 1, empty: 1, no_sql: 1 };
var FAILURE = { db_error: 1, placeholder: 1 };
var VERDICT_LABEL = {
  rejected: 'отказано по правилу',
  out_of_scope: 'вне области ассистента',
  empty: 'данных не нашлось',
  no_sql: 'вопрос не распознан',
  db_error: 'ошибка базы данных',
  placeholder: 'ответ не сформулирован'
};

function buildTable(rows, columns) {
  if (!Array.isArray(rows) || !rows.length || !Array.isArray(columns)) return null;
  return { columns: columns.map(String), rows: rows };
}

/* ── отрисовка ───────────────────────────────────────────────────── */

function renderTable(table) {
  // Виджет узкий, а выборка бывает широкой: показываем первые строки, а не
  // всё подряд — иначе таблица заслоняет собой ответ, ради которого пришли.
  var LIMIT = 8;
  var shown = table.rows.slice(0, LIMIT);

  var wrap = el('div', 'table-wrap');
  var t = el('table');

  var thead = el('thead'), hr = el('tr');
  table.columns.forEach(function (c) { hr.appendChild(el('th', null, c)); });
  thead.appendChild(hr);
  t.appendChild(thead);

  var numeric = table.columns.map(function (_, k) {
    var vals = shown.map(function (r) { return r[k]; }).filter(function (v) { return v !== ''; });
    return vals.length && vals.filter(isNumeric).length / vals.length >= 0.7;
  });

  var tbody = el('tbody');
  shown.forEach(function (row) {
    var tr = el('tr');
    row.forEach(function (cell, k) {
      var td = el('td');
      if (cell === '' || cell == null) { td.className = 'is-null'; td.textContent = '—'; }
      else { if (numeric[k]) td.className = 'is-num'; td.textContent = cell; }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  t.appendChild(tbody);
  wrap.appendChild(t);

  var frag = document.createDocumentFragment();
  frag.appendChild(wrap);
  var n = table.rows.length;
  var note = n > LIMIT
    ? 'Показаны ' + LIMIT + ' из ' + n + ' ' + plural(n, 'строки', 'строк', 'строк')
    : n + ' ' + plural(n, 'строка', 'строки', 'строк');
  frag.appendChild(el('p', 'table-note', note));
  return frag;
}

/**
 * Показ SQL-запроса под ответом.
 *
 * Свёрнут по умолчанию: посетителю сайта запрос не нужен, ему нужен ответ.
 * Но возможность посмотреть — это то, чем ассистент над базой отличается от
 * генератора правдоподобного текста: ответ можно проверить, а не поверить.
 * На защите это же и показывают.
 */
/**
 * Разбор SQL на понятные части: что взяли, как соединили, чем отфильтровали.
 *
 * Считается ИЗ САМОГО ЗАПРОСА, на стороне клиента — модель к этому объяснению
 * не привлекается вообще. Это важно: объяснение, сочинённое той же моделью,
 * что писала запрос, ничего не подтверждает — она с равным успехом опишет и
 * то, чего в запросе нет. Разбор текста запроса такой возможности не имеет.
 *
 * Тот же приём, что в основном интерфейсе (frontend/script.js, explainSql).
 */
function explainSql(sql) {
  var flat = String(sql).replace(/\s+/g, ' ').trim();
  var uniq = function (a) { return a.filter(function (v, i) { return a.indexOf(v) === i; }); };
  var all = function (re) {
    var out = [], m;
    while ((m = re.exec(flat)) !== null) out.push(m);
    return out;
  };

  var tables = uniq(all(/\b(?:from|join)\s+([a-z_][\w$]*)/gi).map(function (m) { return m[1]; }));
  var joins = all(/\b(inner|left|right|full|cross)?\s*join\s+([a-z_][\w$]*)/gi)
    .map(function (m) { return ((m[1] || '').toUpperCase() + ' JOIN ' + m[2]).trim(); });

  var where = flat.match(/\bwhere\b(.+?)(?=\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b|$)/i);
  var filters = where
    ? where[1].split(/\s+\b(?:and|or)\b\s+/i).map(function (f) { return f.trim(); })
        .filter(Boolean).slice(0, 4)
    : [];

  var group = flat.match(/\bgroup\s+by\b\s+(.+?)(?=\border\s+by\b|\bhaving\b|\blimit\b|$)/i);
  var aggregates = uniq(all(/\b(count|sum|avg|min|max|round)\s*\(/gi)
    .map(function (m) { return m[1].toUpperCase(); }));
  var limit = flat.match(/\blimit\s+(\d+)/i);

  return {
    tables: tables,
    joins: joins,
    filters: filters,
    grouping: group ? group[1].trim() : null,
    aggregates: aggregates,
    limit: limit ? Number(limit[1]) : null
  };
}

function renderSql(sql) {
  var box = el('details', 'sql');
  box.appendChild(el('summary', null, 'Показать SQL-запрос'));

  var pre = el('pre');
  pre.appendChild(el('code', null, sql));
  box.appendChild(pre);

  // Структурированное объяснение: какие таблицы, связи, фильтры и агрегаты.
  var info = explainSql(sql);
  var ex = el('dl', 'sql-explain');
  var item = function (label, value) {
    if (!value) return;
    ex.appendChild(el('dt', null, label));
    ex.appendChild(el('dd', null, value));
  };
  item('таблицы', info.tables.join(', '));
  item('связи', info.joins.join(', '));
  item('фильтры', info.filters.join('; '));
  item('группировка', info.grouping);
  item('агрегаты', info.aggregates.map(function (a) { return a + '()'; }).join(', '));
  item('ограничение', info.limit != null ? 'LIMIT ' + info.limit : null);
  if (ex.childElementCount) box.appendChild(ex);

  var copy = el('button', 'sql__copy', 'Скопировать');
  copy.type = 'button';
  copy.addEventListener('click', function () {
    // Буфер обмена недоступен без https и без разрешения — это не повод
    // показывать ошибку, достаточно честно сказать, что не вышло.
    navigator.clipboard.writeText(sql).then(
      function () { copy.textContent = 'Скопировано'; },
      function () { copy.textContent = 'Не удалось'; }
    );
    setTimeout(function () { copy.textContent = 'Скопировать'; }, 2000);
  });
  box.appendChild(copy);

  return box;
}

function addMessage(kind, payload) {
  if (dom.hello && !dom.hello.hidden) dom.hello.hidden = true;

  var article = el('article', 'msg msg--' + kind);
  var head = el('p', 'msg__who');
  head.appendChild(el('span', null, kind === 'user' ? 'вы' : 'ассистент'));

  var text = typeof payload === 'string' ? payload : payload.text;
  if (typeof payload === 'object' && payload.verdict && VERDICT_LABEL[payload.verdict]) {
    head.appendChild(el('span', 'msg__verdict', VERDICT_LABEL[payload.verdict]));
  }
  article.appendChild(head);

  if (text) article.appendChild(el('p', 'msg__text', text));
  if (typeof payload === 'object' && payload.table) {
    article.appendChild(renderTable(payload.table));
  }
  // SQL показываем и у отказов тоже: там видно, ЧТО именно было отклонено.
  if (typeof payload === 'object' && payload.sql) {
    article.appendChild(renderSql(payload.sql));
  }

  dom.thread.appendChild(article);
  requestAnimationFrame(function () {
    dom.thread.scrollTop = dom.thread.scrollHeight;
  });
}

/* ── отправка ────────────────────────────────────────────────────── */

function setBusy(value) {
  state.busy = value;
  dom.input.disabled = value;
  dom.send.disabled = value;
  dom.typing.hidden = !value;
}

async function ask(question) {
  var q = String(question || '').trim();
  if (!q || state.busy) return;

  addMessage('user', q);
  dom.input.value = '';
  setBusy(true);

  var controller = new AbortController();
  var timer = setTimeout(function () { controller.abort(); }, TIMEOUT_MS);

  try {
    var token = await ensureToken(false);

    var res = await fetch(API + '/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
      body: JSON.stringify({ question: q }),
      signal: controller.signal
    });

    // Гостевой токен живёт 8 часов, но мог протухнуть или быть отозван
    // сменой версии токена на бэкенде — берём новый и повторяем один раз.
    if (res.status === 401) {
      token = await ensureToken(true);
      res = await fetch(API + '/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
        body: JSON.stringify({ question: q }),
        signal: controller.signal
      });
    }

    if (res.status === 429) {
      var wait = res.headers.get('Retry-After');
      setOnline(true);
      addMessage('refusal', {
        text: 'Слишком много вопросов подряд.'
          + (wait ? ' Попробуйте через ' + wait + ' секунд.' : ' Попробуйте чуть позже.')
      });
      return;
    }
    if (!res.ok) throw new Error('сервис ответил ' + res.status);

    var data = await res.json();
    setOnline(true);

    var kind = 'bot';
    if (REFUSAL[data.verdict]) kind = 'refusal';
    else if (FAILURE[data.verdict]) kind = 'error';

    addMessage(kind, {
      text: data.answer,
      verdict: data.verdict,
      sql: data.sql,
      table: buildTable(data.rows, data.columns)
    });

  } catch (err) {
    setOnline(false);
    addMessage('error', {
      text: err.name === 'AbortError'
        ? 'Ответ занял слишком много времени. Попробуйте задать вопрос короче.'
        : 'Не удалось связаться с ассистентом. ' + (err.message || '')
    });
  } finally {
    clearTimeout(timer);
    setBusy(false);
    dom.input.focus();
  }
}

/* ── события ─────────────────────────────────────────────────────── */

dom.form.addEventListener('submit', function (e) {
  e.preventDefault();
  ask(dom.input.value);
});

// Обработчик на списке, а не на кнопках: так он переживёт их пересборку.
dom.hints.addEventListener('click', function (e) {
  var button = e.target.closest('button');
  if (button) ask(button.textContent);
});

// Крестик сворачивает рамку — но только если рамка есть. При открытии
// страницы напрямую (http://localhost:8080/index.html) сворачивать нечего,
// и кнопка, которая ничего не делает, хуже её отсутствия.
var embedded = window.parent !== window;
if (embedded) {
  dom.closeBtn.addEventListener('click', function () {
    parent.postMessage({ type: 'assistant:close' }, '*');
  });
} else {
  dom.closeBtn.hidden = true;
  document.body.classList.add('standalone');
}

// Загрузчик сообщает, что виджет раскрыли — ставим фокус в поле.
window.addEventListener('message', function (event) {
  if (event.data && event.data.type === 'assistant:opened') dom.input.focus();
});

/* ── старт ───────────────────────────────────────────────────────── */

applyTheme(document.documentElement.dataset.theme === 'light' ? 'light' : 'dark');

fetch(API + '/health', { cache: 'no-store' })
  .then(function (r) { return r.ok ? r.json() : null; })
  .then(function (h) { setOnline(Boolean(h && h.db === 'ok')); })
  .catch(function () { setOnline(false); });
