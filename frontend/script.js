/* ═══════════════════════════════════════════════════════════════════════
   AI-АССИСТЕНТ УНИВЕРСИТЕТА — клиентская логика
   ───────────────────────────────────────────────────────────────────────
   Контракт с бэкендом поддержан в двух вариантах одновременно.

   Вход    POST /auth/login
     { "username": "...", "password": "..." }
     -> { access_token, token_type: "bearer", role }
     Токен кладётся в state.token и в localStorage, живёт 8 часов.

   Запрос  POST /chat            (требует Authorization: Bearer <token>)
     { "message": "...", "question": "..." }
     Оба имени поля шлются вместе: бэкенд читает то, которое знает,
     лишнее pydantic отбрасывает сам. Роль в теле больше не передаётся —
     бэкенд берёт её из токена, клиенту тут доверия нет.

   Ответ — принимается любой из двух форм:
     A) { response, sql?, data?, columns? }     ← основная спека
     B) { answer }                              ← текущий бэкенд команды

   Если структурированных data/columns нет, таблица и SQL извлекаются
   из текста ответа: блок ```sql ...``` и строки вида «a|b|c» (ровно так
   psql -t -A -F'|' отдаёт данные в security.execute_sql).
   ═══════════════════════════════════════════════════════════════════ */

'use strict';

/* ═══ 1. КОНФИГУРАЦИЯ ═════════════════════════════════════════════════ */

/* Адрес бэкенда выводится из адреса страницы, а не зашит в localhost.
 *
 * Раньше здесь стояло http://localhost:8000, и это работало ровно до первой
 * попытки открыть демо с другого устройства: фронтенд отдаётся с сервера, а
 * запросы уходили на localhost зрителя. Docker-раскладка кладёт фронтенд на
 * порт 80, бэкенд — на 8000 того же хоста, поэтому хост берём из страницы.
 *
 * Явное переопределение: window.ASSISTANT_API = 'http://host:port' до
 * загрузки скрипта — нужно, если бэкенд вынесен на другую машину.
 */
const API_BASE = (() => {
  if (typeof window !== 'undefined' && window.ASSISTANT_API) {
    return String(window.ASSISTANT_API).replace(/\/+$/, '');
  }
  const { protocol, hostname } = window.location;
  // file:// — страница открыта двойным кликом, хоста нет.
  if (protocol === 'file:' || !hostname) return 'http://localhost:8000';
  return `${protocol}//${hostname}:8000`;
})();

const API_URL    = `${API_BASE}/chat`;
const HEALTH_URL = `${API_BASE}/health`;

// Бэкенд обращается к модели дважды (генерация SQL + объяснение результата),
// а при ошибке СУБД делает ещё одну попытку исправить запрос — итого до трёх
// вызовов. При LLM_TIMEOUT_S=20 и одном повторе худший случай около 125 с,
// поэтому браузер ждёт с запасом. Меняете здесь — сверьтесь с LLM_TIMEOUT_S
// и LLM_RETRIES в .env.
const REQUEST_TIMEOUT_MS = 150_000;
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
  introList:   $('introList'),
  introPolicy: $('introPolicy'),
  loader:      $('loader'),
  composer:    $('composer'),
  input:       $('messageInput'),
  sendBtn:     $('sendBtn'),

  session:     $('session'),
  sessionUser: $('sessionUser'),
  sessionRole: $('sessionRole'),
  logoutBtn:   $('logoutBtn'),
  status:      $('status'),
  statusText:  $('statusText')
};

/* ═══ 3. СОСТОЯНИЕ ════════════════════════════════════════════════════ */

const state = {
  user: null,
  token: null,   // JWT из /auth/login, уходит в Authorization на /chat
  role: null,    // роль с бэкенда; клиент ей не доверяет, она справочная
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
   Настоящая авторизация против бэкенда: POST /auth/login отдаёт JWT,
   он живёт в state.token и уходит заголовком Authorization на /chat.
   Регистрации нет — учётки заводятся скриптом seed_auth_users.py.
   ═══════════════════════════════════════════════════════════════════ */

const LOGIN_URL = `${API_BASE}/auth/login`;

function loadSession() {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const s = JSON.parse(raw);
    const valid = s && typeof s.user === 'string' && s.user
                    && typeof s.token === 'string' && s.token;
    return valid ? s : null;
  } catch {
    return null;
  }
}

function saveSession(user, token, role) {
  try {
    localStorage.setItem(
      SESSION_KEY,
      JSON.stringify({ user, token, role, since: Date.now() })
    );
  } catch {
    /* приватный режим — работаем без сохранения */
  }
}

/**
 * Забирает у бэкенда токен.
 * Поле в теле называется username — именно так его ждёт LoginRequest
 * в main.py; при имени login FastAPI ответит 422, а не 401.
 */
async function signIn(login, password) {
  const user = login.trim();
  if (!user || !password.trim()) {
    return { ok: false, error: 'Введите логин и пароль.' };
  }

  let res;
  try {
    res = await fetch(LOGIN_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: user, password })
    });
  } catch {
    return {
      ok: false,
      error: `Нет связи с сервером. Проверьте, что бэкенд запущен на ${API_BASE}.`
    };
  }

  if (res.status === 401) return { ok: false, error: 'Неверный логин или пароль.' };
  if (res.status === 503) return { ok: false, error: 'База данных недоступна, попробуйте ещё раз.' };
  if (!res.ok) return { ok: false, error: `Сервер ответил ${res.status}. Попробуйте ещё раз.` };

  let data;
  try {
    data = await res.json();
  } catch {
    return { ok: false, error: 'Сервер вернул некорректный ответ.' };
  }

  if (!data || !data.access_token) {
    return { ok: false, error: 'Сервер не выдал токен.' };
  }

  return { ok: true, user, token: data.access_token, role: data.role || null };
}

function showAuth() {
  state.user = null;
  state.token = null;
  state.role = null;
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

/* ═══ 5a. ПРИВЕТСТВЕННЫЕ ПОДСКАЗКИ ═══════════════════════════════════
 *
 * Набор доступных данных зависит от роли: абитуриенту закрыт весь учебный
 * контур, студенту — контингент, преподавателю — аналитика по кафедрам,
 * внутренняя статистика приёмной комиссии есть только у администрации.
 * Предлагать всем один список значит гарантированно показать части
 * пользователей отказ проверки безопасности вместо ответа.
 *
 * ФИО студентов не показываются НИ ОДНОЙ роли: успеваемость и задолженности
 * обезличены на уровне самих представлений (см. sql/014_anonymous_analytics).
 *
 * Роль приходит с бэкенда и здесь используется ТОЛЬКО для оформления. Доступ
 * решает сервер по проверенному токену: подмена роли в localStorage меняет
 * подсказки и ничего больше.
 */
const INTRO = {
  applicant: {
    items: [
      'Какие направления есть на факультете информационных технологий?',
      'Сколько бюджетных и платных мест на «Экономику»?',
      'Какой был проходной балл на юриспруденцию в прошлом году?',
      'До какого числа принимаются документы?'
    ],
    policy: 'Доступны направления, места, сроки приёма, проходные баллы ' +
            'прошлых лет и обезличенная статистика подачи заявлений. ' +
            'Расписание занятий и учебные планы — для тех, кто уже учится.'
  },
  student: {
    items: [
      'Какие институты и факультеты есть в ИГУ?',
      'Что сдавать на прикладную информатику?',
      'До какого числа подавать документы в 2026 году?',
      'Какая у меня следующая пара?'
    ],
    policy: 'Направления, вступительные испытания и сроки приёма — по ' +
            'данным приёмной кампании 2026 года. Свои оценки и расписание ' +
            'видите только вы.'
  },
  teacher: {
    items: [
      'Что я веду в этом семестре?',
      'Какой процент студентов сдал «Базы данных» с первой попытки?',
      'Какое у меня расписание завтра?',
      'Какие аудитории свободны в понедельник на второй паре?'
    ],
    policy: 'Успеваемость по дисциплинам показывается обезличенно: ' +
            'распределение оценок и доли, без фамилий студентов.'
  },
  'deans-office': {
    items: [
      'Какой средний балл по факультету информационных технологий?',
      'Сколько должников на кафедре программной инженерии?',
      'Какие преподаватели не ведут дисциплин в первом семестре?',
      'Какие кафедры имеют средний балл ниже общего по университету?'
    ],
    policy: 'Успеваемость и задолженности показываются обезличенно: ' +
            'счётчики, доли и средние баллы без фамилий студентов. ' +
            'Паспорта, телефоны, почты и даты рождения закрыты для всех ролей.'
  },
  administration: {
    items: [
      'Покажи динамику зачисления бюджетников по годам',
      'Сколько заявлений подано за последние 7 дней кампании 2025 года?',
      'Какой средний балл ЕГЭ по информатике у поступавших?',
      'Соотношение бюджетных и платных мест по направлениям в 2026 году'
    ],
    policy: 'Статистика приёма выводится обезличенно: даты, статусы и ' +
            'счётчики. Контакты абитуриентов недоступны ни одной роли.'
  }
};

// Служебные имена ролей приходят с бэкенда как есть; для шапки нужны
// человеческие.
const ROLE_TITLE = {
  applicant: 'абитуриент',
  student: 'студент',
  teacher: 'преподаватель',
  'deans-office': 'деканат',
  administration: 'администрация'
};

const INTRO_DEFAULT = {
  items: [
    'Какие институты и факультеты есть в ИГУ?',
    'Какие документы нужны для поступления?',
    'Чем минимальный балл отличается от проходного?',
    'Есть ли общежитие?'
  ],
  policy: 'Персональные данные студентов и абитуриентов не выводятся.'
};

function renderIntro(role) {
  const preset = INTRO[role] || INTRO_DEFAULT;
  const items = preset.items.map((text) => {
    const button = document.createElement('button');
    button.className = 'intro__item';
    button.type = 'button';
    button.textContent = text;
    const li = document.createElement('li');
    li.append(button);
    return li;
  });
  dom.introList.replaceChildren(...items);
  dom.introPolicy.textContent = preset.policy;
}

function showChat(user, token, role) {
  state.user = user;
  state.token = token || null;
  state.role = role || null;
  renderIntro(state.role);
  dom.body.dataset.screen = 'chat';
  dom.authScreen.hidden = true;
  dom.chatScreen.hidden = false;
  dom.session.hidden = false;
  dom.sessionUser.textContent = user;

  const roleTitle = ROLE_TITLE[state.role] || state.role;
  dom.sessionRole.textContent = roleTitle || '';
  dom.sessionRole.hidden = !roleTitle;
  dom.sessionRole.title = roleTitle
    ? `Роль определяет, какие данные доступны. Проверяется на сервере по токену.`
    : '';

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

/* ═══ 9a. ССЫЛКИ НА ИСТОЧНИКИ ═════════════════════════════════════════
 *
 * Служебные колонки приезжают из выборки одинаковыми во всех строках, а URL
 * источника занимает под сотню символов и разносит вёрстку таблицы вширь.
 *
 * Поэтому колонка, у которой значение одно на всю выборку, выносится из
 * таблицы под неё: показывается один раз и как подпись, а не как данные.
 * Если значения различаются — колонка остаётся на месте, потому что тогда
 * это уже содержательный признак строки.
 *
 * data_status в подписи не показывается: это внутренняя пометка загрузчиков
 * данных, пользователю она не адресована. Из таблицы её убираем, наружу не
 * выносим.
 */
const HIDDEN_COLUMNS = new Set(['data_status']);
const LIFTABLE = new Set(['source_url', 'source', 'page_url', 'site_url']);

function isUrl(value) {
  return /^https?:\/\/\S+$/i.test(value);
}

/** Значение колонки, если оно одно на всю выборку; иначе null. */
function constantValue(table, index) {
  const first = table.rows[0]?.[index];
  if (first === undefined || first === '') return null;
  return table.rows.every((r) => r[index] === first) ? first : null;
}

/** Короткая подпись для ссылки: имя файла или домен. */
function linkLabel(url) {
  try {
    const parsed = new URL(url);
    const last = parsed.pathname.split('/').filter(Boolean).pop();
    if (last && /\.(pdf|docx?|xlsx?)$/i.test(last)) return last;
    return parsed.hostname.replace(/^www\./, '');
  } catch {
    return 'источник';
  }
}

function renderLink(url, label) {
  const a = el('a', 'src-link', label || linkLabel(url));
  a.href = url;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  a.title = url;
  return a;
}

/** Подпись под таблицей со ссылкой на официальную страницу. */
function renderProvenance(lifted) {
  if (!lifted.length) return null;
  const strip = el('p', 'provenance');
  lifted.forEach(({ column, value }) => {
    if (isUrl(value)) {
      strip.appendChild(renderLink(value, `источник: ${linkLabel(value)}`));
    } else {
      strip.appendChild(el('span', 'provenance__note', `${column}: ${value}`));
    }
  });
  return strip;
}

function renderTable(table) {
  // Выносим постоянные служебные колонки, а таблицу строим по оставшимся.
  const lifted = [];
  const keep = [];
  table.columns.forEach((column, index) => {
    const name = String(column).trim().toLowerCase();
    // Служебная колонка — просто не показываем.
    if (HIDDEN_COLUMNS.has(name)) return;
    if (LIFTABLE.has(name) && table.rows.length > 0) {
      const value = constantValue(table, index);
      if (value !== null) {
        lifted.push({ column: name, value });
        return;
      }
    }
    keep.push(index);
  });
  // Если выносить пришлось всё — оставляем таблицу как есть, пустая
  // бессмысленна.
  const columns = keep.length ? keep.map((i) => table.columns[i]) : table.columns;
  const rows = keep.length
    ? table.rows.map((r) => keep.map((i) => r[i]))
    : table.rows;

  const wrap = el('div', 'result-table');
  const t = el('table');

  const thead = el('thead');
  const headRow = el('tr');
  columns.forEach((c) => headRow.appendChild(el('th', null, c)));
  thead.appendChild(headRow);
  t.appendChild(thead);

  // Колонка числовая, если числом является большинство значений
  const numericCol = columns.map((_, k) => {
    const vals = rows.map((r) => r[k]).filter((v) => v !== '');
    return vals.length > 0 && vals.filter(isNumeric).length / vals.length >= 0.7;
  });

  const tbody = el('tbody');
  rows.forEach((row) => {
    const tr = el('tr');
    row.forEach((cell, k) => {
      const td = el('td');
      if (cell === '') {
        td.className = 'is-null';
        td.textContent = '—';
      } else if (isUrl(cell)) {
        // Ссылка целиком — это 100+ символов в одной ячейке; таблица от
        // такого расползается на несколько экранов вширь.
        td.className = 'is-link';
        td.appendChild(renderLink(cell));
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
  const n = rows.length;
  meta.appendChild(el('span', null,
    `${n} ${plural(n, 'строка', 'строки', 'строк')} · ${columns.length} ${plural(columns.length, 'колонка', 'колонки', 'колонок')}`));

  const csv = el('button', 'result-meta__csv', 'Скачать CSV');
  csv.type = 'button';
  // Выгружаем ИСХОДНУЮ таблицу со всеми колонками: в файле служебные поля
  // нужны, это не про экономию места на экране.
  csv.addEventListener('click', () => downloadCsv(table));
  meta.appendChild(csv);

  const block = document.createDocumentFragment();
  const provenance = renderProvenance(lifted);
  if (provenance) block.appendChild(provenance);
  block.appendChild(wrap);
  block.appendChild(meta);
  return block;
}

/** Кнопка «скопировать ответ». Текст уходит в буфер без разметки. */
function renderCopyAction(text) {
  const button = el('button', 'msg__action', 'Скопировать ответ');
  button.type = 'button';
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = 'Скопировано';
    } catch {
      // Буфер обмена недоступен без https и без разрешения — не повод
      // показывать ошибку, достаточно честно сказать, что не вышло.
      button.textContent = 'Не удалось скопировать';
    }
    setTimeout(() => { button.textContent = 'Скопировать ответ'; }, 2000);
  });
  return button;
}

/** Реплика в ленте: микро-лейбл говорящего и содержимое под ним. */
function addMessage(who, payload) {
  if (dom.intro && !dom.intro.hidden) dom.intro.hidden = true;

  const kind = who === 'user' ? 'user' : (who === 'error' ? 'error' : 'bot');
  const article = el('article', `msg msg--${kind}`);

  const label = kind === 'user' ? (state.user || 'вы') : (kind === 'error' ? 'ошибка' : 'ассистент');
  const head = el('p', 'msg__who');
  head.appendChild(el('span', 'msg__author', label));
  // Время реплики. В длинной ленте без него непонятно, какой ответ свежий,
  // а на защите — сколько заняли те самые два обращения к модели.
  const time = new Date();
  const stamp = el('time', 'msg__time',
    time.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }));
  stamp.dateTime = time.toISOString();
  head.appendChild(stamp);
  article.appendChild(head);

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

    // Копировать имеет смысл ответ, а не текст ошибки.
    if (kind === 'bot' && payload.text) {
      article.appendChild(renderCopyAction(payload.text));
    }
  }

  // Ошибка сети или таймаут — почти всегда лечится повтором того же вопроса.
  // Заставлять человека перенабирать его руками незачем.
  if (kind === 'error' && payload && payload.retry) {
    const again = el('button', 'msg__action', 'Повторить вопрос');
    again.type = 'button';
    again.addEventListener('click', () => send(payload.retry));
    article.appendChild(again);
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
      headers: {
        'Content-Type': 'application/json',
        // /chat закрыт авторизацией: без Bearer-токена бэкенд ответит 401
        'Authorization': `Bearer ${state.token || ''}`
      },
      // Оба имени поля сразу — работает с любым из двух бэкендов
      body: JSON.stringify({ message: q, question: q }),
      signal: controller.signal
    });

    // Токен истёк (JWT живёт 8 часов) либо недействителен — на экран входа
    if (res.status === 401) {
      setStatus('online');
      addMessage('error', 'Сессия истекла. Войдите заново.');
      logout();
      return;
    }

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

    // retry несёт исходный вопрос: сеть и таймаут лечатся повтором, и
    // перенабирать текст руками человеку незачем.
    addMessage('error', { text: reason, retry: q });
  } finally {
    clearTimeout(timer);
    setBusy(false);
    // После 401 мы уже на экране входа — фокус туда возвращать не надо
    if (dom.body.dataset.screen === 'chat') dom.input.focus();
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
  dom.authForm.addEventListener('submit', async (e) => {
    e.preventDefault();

    // signIn теперь ходит по сети — блокируем кнопку на время запроса
    const submitBtn = dom.authForm.querySelector('button[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;
    dom.authError.hidden = true;

    try {
      const result = await signIn(dom.login.value, dom.password.value);

      if (!result.ok) {
        dom.authError.textContent = result.error;
        dom.authError.hidden = false;
        return;
      }
      saveSession(result.user, result.token, result.role);
      showChat(result.user, result.token, result.role);
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  });

  [dom.login, dom.password].forEach((field) => {
    field.addEventListener('input', () => { dom.authError.hidden = true; });
  });

  dom.logoutBtn.addEventListener('click', logout);

  dom.composer.addEventListener('submit', (e) => {
    e.preventDefault();
    send(dom.input.value);
  });

  // Быстрые вопросы из приветственного блока. Обработчик висит на списке, а
  // не на кнопках: кнопки создаются заново под роль при каждом входе
  // (renderIntro), и подписка на конкретные элементы отвалилась бы сразу.
  dom.introList.addEventListener('click', (event) => {
    const button = event.target.closest('.intro__item');
    if (button) send(button.textContent);
  });

  window.addEventListener('online',  checkHealth);
  window.addEventListener('offline', () => setStatus('offline'));
}

function init() {
  bindEvents();
  setBusy(false);

  const session = loadSession();
  if (session) showChat(session.user, session.token, session.role);
  else showAuth();
}

init();
