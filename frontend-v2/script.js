/* ═══════════════════════════════════════════════════════════════════════
   AI-АССИСТЕНТ УНИВЕРСИТЕТА — клиентская логика
   ───────────────────────────────────────────────────────────────────────
   Контракт с бэкендом поддержан в двух вариантах одновременно.

   Запрос  POST /chat
     { "message": "...", "question": "...", "role": null }
     Оба поля шлются вместе: бэкенд читает то, которое знает,
     лишнее pydantic отбрасывает сам.

   Ответ — принимается любой из двух форм:
     A) { response, sql?, data?, columns? }     ← основная спека
     B) { answer }                              ← текущий бэкенд команды

   Если структурированных data/columns нет, таблица и SQL извлекаются
   из текста ответа: блок ```sql ...``` и строки вида «a|b|c» (ровно так
   psql -t -A -F'|' отдаёт данные в security.execute_sql).
   ═══════════════════════════════════════════════════════════════════ */

'use strict';

/* ═══ 1. КОНФИГУРАЦИЯ ═════════════════════════════════════════════════ */

const API_URL    = 'http://localhost:8000/chat';
const API_BASE   = API_URL.replace(/\/chat\/?$/, '');
const HEALTH_URL = `${API_BASE}/health`;

const REQUEST_TIMEOUT_MS = 90_000;
const HEALTH_EVERY_MS    = 25_000;
const SESSION_KEY        = 'assistant:session';

/* ═══ 2. DOM ══════════════════════════════════════════════════════════ */

const $ = (id) => document.getElementById(id);

const dom = {
  body:        document.body,
  authScreen:  $('authScreen'),
  authForm:    $('authForm'),
  authError:   $('authError'),
  login:       $('login'),
  password:    $('password'),

  chatScreen:  $('chatScreen'),
  thread:      $('thread'),
  intro:       $('intro'),
  loader:      $('loader'),
  composer:    $('composer'),
  input:       $('messageInput'),
  sendBtn:     $('sendBtn'),

  session:     $('session'),
  sessionUser: $('sessionUser'),
  logoutBtn:   $('logoutBtn'),
  status:      $('status'),
  statusText:  $('statusText')
};

/* ═══ 3. СОСТОЯНИЕ ════════════════════════════════════════════════════ */

const state = {
  user: null,
  busy: false,
  healthTimer: null
};

/* ═══ 4. УТИЛИТЫ ══════════════════════════════════════════════════════ */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

/** Экранирование для тех редких мест, где нужен innerHTML. */
function esc(str) {
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function isNumeric(value) {
  if (typeof value === 'number') return Number.isFinite(value);
  const v = String(value ?? '').trim()
    .replace(/[\s ]/g, '')
    .replace(',', '.')
    .replace(/%$/, '');
  return v !== '' && Number.isFinite(Number(v));
}

function plural(n, one, few, many) {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}

/* ═══ 5. АВТОРИЗАЦИЯ ══════════════════════════════════════════════════
   Клиентская заглушка: пускает по любой непустой паре. Когда на бэкенде
   появится POST /login, заменяется тело signIn() — остальной код
   трогать не придётся.
   ═══════════════════════════════════════════════════════════════════ */

function loadSession() {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const s = JSON.parse(raw);
    return s && typeof s.user === 'string' && s.user ? s : null;
  } catch {
    return null;
  }
}

function saveSession(user) {
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify({ user, since: Date.now() }));
  } catch {
    /* приватный режим — работаем без сохранения */
  }
}

function signIn(login, password) {
  if (!login.trim() || !password.trim()) {
    return { ok: false, error: 'Введите логин и пароль.' };
  }
  if (login.trim().length < 2) {
    return { ok: false, error: 'Логин слишком короткий.' };
  }
  return { ok: true, user: login.trim() };
}

function showAuth() {
  state.user = null;
  dom.body.dataset.screen = 'auth';
  dom.authScreen.hidden = false;
  dom.chatScreen.hidden = true;
  dom.session.hidden = true;

  clearInterval(state.healthTimer);
  state.healthTimer = null;

  dom.authForm.reset();
  dom.authError.hidden = true;
  dom.login.focus();
}

function showChat(user) {
  state.user = user;
  dom.body.dataset.screen = 'chat';
  dom.authScreen.hidden = true;
  dom.chatScreen.hidden = false;
  dom.session.hidden = false;
  dom.sessionUser.textContent = user;

  setStatus('checking');
  checkHealth();
  clearInterval(state.healthTimer);
  state.healthTimer = setInterval(checkHealth, HEALTH_EVERY_MS);

  dom.input.focus();
}

function logout() {
  try { localStorage.removeItem(SESSION_KEY); } catch { /* не критично */ }
  dom.thread.replaceChildren(dom.intro);
  dom.intro.hidden = false;
  showAuth();
}

/* ═══ 6. НОРМАЛИЗАЦИЯ ОТВЕТА ══════════════════════════════════════════ */

/**
 * Приводит любой из поддерживаемых форматов к общему виду:
 *   { text, sql, table: { columns, rows } | null }
 */
function normalizeReply(payload) {
  const text = String(
    payload?.response ?? payload?.answer ?? payload?.text ?? ''
  ).trim();

  const sql = payload?.sql ? String(payload.sql).trim() : null;
  const table = buildTableFromData(payload?.data, payload?.columns);

  return { text, sql, table };
}

/** Собирает таблицу из data/columns. data — массив объектов или массив массивов. */
function buildTableFromData(data, columns) {
  if (!Array.isArray(data) || data.length === 0) return null;

  const first = data[0];

  // Вариант 1: массив массивов
  if (Array.isArray(first)) {
    const cols = Array.isArray(columns) && columns.length
      ? columns.map(String)
      : first.map((_, i) => `колонка ${i + 1}`);
    return { columns: cols, rows: data.map((r) => cols.map((_, i) => cellToString(r[i]))) };
  }

  // Вариант 2: массив объектов
  if (first && typeof first === 'object') {
    const cols = Array.isArray(columns) && columns.length
      ? columns.map(String)
      : Object.keys(first);
    return { columns: cols, rows: data.map((r) => cols.map((c) => cellToString(r?.[c]))) };
  }

  // Вариант 3: массив скаляров
  const cols = Array.isArray(columns) && columns.length ? columns.map(String) : ['значение'];
  return { columns: cols, rows: data.map((v) => [cellToString(v)]) };
}

function cellToString(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/* ═══ 7. ЗАПАСНОЙ РАЗБОР ТЕКСТА ═══════════════════════════════════════
   Нужен, когда бэкенд прислал только текст: достаём из него SQL-блок
   и табличные строки, чтобы критерии приёмки выполнялись в любом случае.
   ═══════════════════════════════════════════════════════════════════ */

const MD_SEPARATOR = /^\s*\|?[\s:|-]*-[\s:|-]*\|[\s:|-]*$/;

function splitRow(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|'))   s = s.slice(0, -1);
  return s.split('|').map((c) => c.trim());
}

function looksTabular(line) {
  if (!line.includes('|')) return false;
  const cells = splitRow(line);
  if (cells.length < 2) return false;
  // В прозе ячейки длинные и содержат разделители предложений
  return cells.every((c) => c.length <= 60 && !/\.\s/.test(c));
}

/** Возвращает { text, sql, table } — то, что удалось вытащить из строки. */
function parseFromText(raw) {
  const source = String(raw ?? '').replace(/\r\n/g, '\n').trim();
  if (!source) return { text: '', sql: null, table: null };

  let sql = null;
  let table = null;
  const prose = [];

  // 7.1 — блоки в ограждении ```
  const fence = /```([a-zA-Z]*)\r?\n?([\s\S]*?)```/g;
  let cursor = 0;
  let m;

  while ((m = fence.exec(source)) !== null) {
    prose.push(source.slice(cursor, m.index));
    const lang = (m[1] || '').toLowerCase();
    const code = m[2].trim();

    if (code && !sql && (lang === 'sql' || /^\s*(select|with)\b/i.test(code))) sql = code;
    else if (code) prose.push(code);

    cursor = fence.lastIndex;
  }
  prose.push(source.slice(cursor));

  // 7.2 — табличные строки в оставшемся тексте
  const kept = [];
  const lines = prose.join('\n').split('\n');
  let i = 0;

  while (i < lines.length) {
    if (looksTabular(lines[i])) {
      const run = [];
      while (i < lines.length && looksTabular(lines[i])) { run.push(lines[i]); i++; }

      const parsed = table ? null : tableFromLines(run);
      if (parsed) table = parsed;
      else kept.push(...run);
      continue;
    }

    // «Голый» SQL без ограждения
    if (!sql && /^\s*(SELECT|WITH)\b/i.test(lines[i]) &&
        /\bFROM\b/i.test(lines.slice(i, i + 6).join(' '))) {
      const buf = [];
      while (i < lines.length && lines[i].trim() !== '') {
        buf.push(lines[i]);
        if (/;\s*$/.test(lines[i])) { i++; break; }
        i++;
      }
      sql = buf.join('\n').trim().replace(/;$/, '');
      continue;
    }

    kept.push(lines[i]);
    i++;
  }

  return { text: kept.join('\n').replace(/\n{3,}/g, '\n\n').trim(), sql, table };
}

function tableFromLines(run) {
  const lines = run.slice();
  let columns = null;

  if (lines.length >= 2 && MD_SEPARATOR.test(lines[1])) {
    columns = splitRow(lines[0]);
    lines.splice(0, 2);
  }

  let rows = lines.map(splitRow).filter((r) => r.some((c) => c !== ''));
  if (!rows.length) return null;
  if (!columns && rows.length < 2) return null;

  const width = Math.max(columns ? columns.length : 0, ...rows.map((r) => r.length));
  if (!rows.every((r) => Math.abs(r.length - width) <= 1)) return null;

  // Шапки не было, но первая строка похожа на заголовки
  if (!columns && rows.length >= 2) {
    const head = rows[0];
    if (head.every((c) => c !== '' && !isNumeric(c)) && rows.slice(1).some((r) => r.some(isNumeric))) {
      columns = head;
      rows = rows.slice(1);
    }
  }
  if (!rows.length) return null;

  const pad = (r) => Array.from({ length: width }, (_, k) => r[k] ?? '');
  return {
    columns: columns ? pad(columns) : Array.from({ length: width }, (_, k) => `колонка ${k + 1}`),
    rows: rows.map(pad)
  };
}

/* ═══ 8. РАЗБОР SQL — EXPLAINABLE AI ══════════════════════════════════ */

function explainSql(sql) {
  const flat = String(sql).replace(/\s+/g, ' ').trim();

  const tables = [...new Set(
    [...flat.matchAll(/\b(?:from|join)\s+([a-z_][\w$]*)/gi)].map((m) => m[1])
  )];

  const joins = [...flat.matchAll(/\b(inner|left|right|full|cross)?\s*join\s+([a-z_][\w$]*)/gi)]
    .map((m) => `${(m[1] || '').toUpperCase()} JOIN ${m[2]}`.trim());

  const whereMatch = flat.match(/\bwhere\b(.+?)(?=\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b|$)/i);
  const filters = whereMatch
    ? whereMatch[1].split(/\s+\b(?:and|or)\b\s+/i).map((f) => f.trim()).filter(Boolean).slice(0, 4)
    : [];

  const groupMatch = flat.match(/\bgroup\s+by\b\s+(.+?)(?=\border\s+by\b|\bhaving\b|\blimit\b|$)/i);

  const aggregates = [...new Set(
    [...flat.matchAll(/\b(count|sum|avg|min|max|round)\s*\(/gi)].map((m) => m[1].toUpperCase())
  )];

  const limitMatch = flat.match(/\blimit\s+(\d+)/i);

  return {
    tables,
    joins,
    filters,
    grouping: groupMatch ? groupMatch[1].trim() : null,
    aggregates,
    limit: limitMatch ? Number(limitMatch[1]) : null
  };
}

/* ═══ 9. РЕНДЕР ═══════════════════════════════════════════════════════ */

/** Лёгкий markdown: **жирный**, `код`, списки. Всё экранируется. */
function renderProse(text) {
  const frag = document.createDocumentFragment();
  const lines = text.split('\n');
  const bullet = /^\s*[-*•]\s+(.*)$/;
  const number = /^\s*(\d+)[.)]\s+(.*)$/;

  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');

  let i = 0;
  while (i < lines.length) {
    if (bullet.test(lines[i]) || number.test(lines[i])) {
      const ordered = !bullet.test(lines[i]);
      const list = el(ordered ? 'ol' : 'ul', 'msg__list');
      while (i < lines.length && (bullet.test(lines[i]) || number.test(lines[i]))) {
        const m = lines[i].match(bullet) || lines[i].match(number);
        const li = document.createElement('li');
        li.innerHTML = inline(m[m.length - 1]);
        list.appendChild(li);
        i++;
      }
      frag.appendChild(list);
      continue;
    }

    const para = [];
    while (i < lines.length && !bullet.test(lines[i]) && !number.test(lines[i])) {
      para.push(lines[i]);
      i++;
    }
    const body = para.join('\n').trim();
    if (body) {
      const p = el('p', 'msg__text');
      p.innerHTML = inline(body);
      frag.appendChild(p);
    }
  }
  return frag;
}

function renderSqlBlock(sql) {
  const info = explainSql(sql);

  const box = el('details', 'sql-block');
  const summary = el('summary', null, 'SQL-запрос');
  box.appendChild(summary);

  const pre = el('pre');
  pre.appendChild(el('code', null, sql));
  box.appendChild(pre);

  // Структурированное объяснение — из самого запроса, без участия бэкенда
  const ex = el('div', 'sql-explain');
  const item = (label, value) => {
    const s = el('span', 'sql-explain__item');
    s.appendChild(el('span', null, label));
    s.appendChild(el('b', null, value));
    ex.appendChild(s);
  };

  if (info.tables.length)     item('таблицы', info.tables.join(', '));
  if (info.joins.length)      item('связи', info.joins.join(', '));
  if (info.filters.length)    item('фильтры', info.filters.join('; '));
  if (info.grouping)          item('группировка', info.grouping);
  if (info.aggregates.length) item('агрегаты', info.aggregates.map((a) => `${a}()`).join(', '));
  if (info.limit != null)     item('ограничение', `LIMIT ${info.limit}`);

  if (ex.childElementCount) box.appendChild(ex);

  return { node: box, info };
}

function renderTable(table) {
  const wrap = el('div', 'result-table');
  const t = el('table');

  const thead = el('thead');
  const headRow = el('tr');
  table.columns.forEach((c) => headRow.appendChild(el('th', null, c)));
  thead.appendChild(headRow);
  t.appendChild(thead);

  // Колонка числовая, если числом является большинство значений
  const numericCol = table.columns.map((_, k) => {
    const vals = table.rows.map((r) => r[k]).filter((v) => v !== '');
    return vals.length > 0 && vals.filter(isNumeric).length / vals.length >= 0.7;
  });

  const tbody = el('tbody');
  table.rows.forEach((row) => {
    const tr = el('tr');
    row.forEach((cell, k) => {
      const td = el('td');
      if (cell === '') {
        td.className = 'is-null';
        td.textContent = '—';
      } else {
        if (numericCol[k]) td.className = 'is-num';
        td.textContent = cell;
      }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });

  t.appendChild(tbody);
  wrap.appendChild(t);

  const meta = el('div', 'result-meta');
  const n = table.rows.length;
  meta.appendChild(el('span', null,
    `${n} ${plural(n, 'строка', 'строки', 'строк')} · ${table.columns.length} ${plural(table.columns.length, 'колонка', 'колонки', 'колонок')}`));

  const csv = el('button', 'result-meta__csv', 'Скачать CSV');
  csv.type = 'button';
  csv.addEventListener('click', () => downloadCsv(table));
  meta.appendChild(csv);

  const block = document.createDocumentFragment();
  block.appendChild(wrap);
  block.appendChild(meta);
  return block;
}

/** Реплика в ленте: микро-лейбл говорящего и содержимое под ним. */
function addMessage(who, payload) {
  if (dom.intro && !dom.intro.hidden) dom.intro.hidden = true;

  const kind = who === 'user' ? 'user' : (who === 'error' ? 'error' : 'bot');
  const article = el('article', `msg msg--${kind}`);

  const label = kind === 'user' ? (state.user || 'вы') : (kind === 'error' ? 'ошибка' : 'ассистент');
  article.appendChild(el('p', 'msg__who', label));

  if (typeof payload === 'string') {
    article.appendChild(renderProse(payload));
  } else {
    if (payload.text) article.appendChild(renderProse(payload.text));

    let info = null;
    if (payload.sql) {
      const built = renderSqlBlock(payload.sql);
      article.appendChild(built.node);
      info = built.info;
    }

    if (payload.table) {
      article.appendChild(renderTable(payload.table));

      // Выборка упёрлась в LIMIT — предупреждаем и предлагаем сузить запрос
      if (info?.limit != null && payload.table.rows.length >= info.limit) {
        article.appendChild(el('p', 'notice',
          `Показаны первые ${info.limit} ${plural(info.limit, 'строка', 'строки', 'строк')} — выборка ограничена. ` +
          'Уточните вопрос фильтрами: год, факультет, направление, статус.'));
      }
    }

    if (!payload.text && !payload.sql && !payload.table) {
      article.appendChild(renderProse('Сервер вернул пустой ответ. Попробуйте переформулировать вопрос.'));
    }
  }

  dom.thread.appendChild(article);
  requestAnimationFrame(() => article.scrollIntoView({ behavior: 'smooth', block: 'end' }));
}

/* ═══ 10. ОТПРАВКА ════════════════════════════════════════════════════ */

function setBusy(value) {
  state.busy = value;
  dom.sendBtn.disabled = value;
  dom.input.disabled = value;
  dom.loader.classList.toggle('active', value);
  dom.loader.setAttribute('aria-hidden', String(!value));
}

async function send(question) {
  const q = String(question || '').trim();
  if (!q || state.busy) return;

  addMessage('user', q);
  dom.input.value = '';
  setBusy(true);

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  try {
    const res = await fetch(API_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // Оба имени поля сразу — работает с любым из двух бэкендов
      body: JSON.stringify({ message: q, question: q }),
      signal: controller.signal
    });

    if (!res.ok) throw new Error(`сервер ответил ${res.status} ${res.statusText}`.trim());

    const payload = await res.json();
    const reply = normalizeReply(payload);

    // Структурированных полей нет — вытаскиваем SQL и таблицу из текста
    if (!reply.sql || !reply.table) {
      const fromText = parseFromText(reply.text);
      reply.sql = reply.sql || fromText.sql;
      reply.table = reply.table || fromText.table;
      if (fromText.sql || fromText.table) reply.text = fromText.text;
    }

    setStatus('online');

    // «[Ошибка БД] …» и «[Запрос отклонён…]» — штатно обработанные случаи
    const rejected = /^\s*\[(ошибка|запрос отклонён|неожиданный ответ)/i.test(reply.text);
    addMessage(rejected ? 'error' : 'bot', reply);

  } catch (err) {
    setStatus('offline');

    const reason = err.name === 'AbortError'
      ? `Ассистент не ответил за ${Math.round(REQUEST_TIMEOUT_MS / 1000)} секунд. Возможно, запрос слишком тяжёлый — попробуйте сузить его фильтрами.`
      : `Не удалось связаться с ассистентом: ${err.message}. Проверьте, что бэкенд запущен на ${API_BASE}.`;

    addMessage('error', reason);
  } finally {
    clearTimeout(timer);
    setBusy(false);
    dom.input.focus();
  }
}

/* ═══ 11. СТАТУС СВЯЗИ ════════════════════════════════════════════════ */

const STATUS_TEXT = { online: 'на связи', offline: 'нет связи', checking: 'проверка' };

function setStatus(kind) {
  dom.status.dataset.state = kind;
  dom.statusText.textContent = STATUS_TEXT[kind] || '';
}

async function checkHealth() {
  if (state.busy) return;
  try {
    const r = await fetch(HEALTH_URL, { method: 'GET', cache: 'no-store' });
    setStatus(r.ok ? 'online' : 'offline');
  } catch {
    setStatus('offline');
  }
}

/* ═══ 12. ВЫГРУЗКА CSV ════════════════════════════════════════════════ */

function downloadCsv(table) {
  const quote = (v) => `"${String(v).replace(/"/g, '""')}"`;
  const lines = [table.columns.map(quote).join(';')];
  table.rows.forEach((r) => lines.push(r.map(quote).join(';')));

  // BOM — чтобы Excel корректно открыл кириллицу
  const blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = el('a');
  a.href = url;
  a.download = `результат-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ═══ 13. ИНИЦИАЛИЗАЦИЯ ══════════════════════════════════════════════ */

function bindEvents() {
  dom.authForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const result = signIn(dom.login.value, dom.password.value);

    if (!result.ok) {
      dom.authError.textContent = result.error;
      dom.authError.hidden = false;
      return;
    }
    dom.authError.hidden = true;
    saveSession(result.user);
    showChat(result.user);
  });

  [dom.login, dom.password].forEach((field) => {
    field.addEventListener('input', () => { dom.authError.hidden = true; });
  });

  dom.logoutBtn.addEventListener('click', logout);

  dom.composer.addEventListener('submit', (e) => {
    e.preventDefault();
    send(dom.input.value);
  });

  // Быстрые вопросы из приветственного блока
  dom.intro.querySelectorAll('.intro__item').forEach((btn) => {
    btn.addEventListener('click', () => send(btn.textContent));
  });

  window.addEventListener('online',  checkHealth);
  window.addEventListener('offline', () => setStatus('offline'));
}

function init() {
  bindEvents();
  setBusy(false);

  const session = loadSession();
  if (session) showChat(session.user);
  else showAuth();
}

init();
