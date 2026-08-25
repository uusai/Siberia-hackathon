/* ═══════════════════════════════════════════════════════════════════════
   ЭХОЛОТ — AI-ассистент университета · клиентская логика
   ───────────────────────────────────────────────────────────────────────
   Контракт с бэкендом намеренно минимальный:
       POST /chat   →   { "question": "...", "role": "applicant" }
       ответ        ←   { "answer": "текст" }
   Поле `role` бэкенд может игнорировать (pydantic отбрасывает лишние поля).

   Всё остальное — SQL-блок, разбор запроса и таблица результата — фронт
   извлекает из САМОГО текста ответа. Если бэкенд вложил в answer блок
   ```sql ...``` или строки вида «a|b|c» (ровно так psql -t -A -F'|'
   возвращает данные в security.execute_sql), интерфейс покажет их как
   подсвеченный SQL и настоящую <table>. Если пришёл обычный текст —
   он просто отрисуется как сообщение.
   ═══════════════════════════════════════════════════════════════════ */

'use strict';

/* ═══ 1. КОНФИГУРАЦИЯ ═════════════════════════════════════════════════ */

const API_URL  = 'http://localhost:8000/chat';
const API_BASE = API_URL.replace(/\/chat\/?$/, '');
const HEALTH_URL = `${API_BASE}/health`;

const REQUEST_TIMEOUT_MS = 90_000;   // ответ = LLM + SQL + LLM, бывает долго
const HEALTH_EVERY_MS    = 25_000;
const HISTORY_LIMIT      = 40;       // сообщений в localStorage
const STORE_KEY          = 'echolot:v1';

/* ═══ 2. РОЛИ И ПРЕСЕТЫ ═══════════════════════════════════════════════
   Вопросы взяты из раздела «Примеры бизнес-запросов» памятки участника.
   ═══════════════════════════════════════════════════════════════════ */

const ROLES = [
  {
    id: 'applicant',
    label: 'Абитуриент',
    icon: '<path d="M12 4 2.5 8.5 12 13l9.5-4.5L12 4Z" stroke-width="1.6" stroke-linejoin="round"/><path d="M6.5 10.7V15c0 1.7 2.5 3 5.5 3s5.5-1.3 5.5-3v-4.3" stroke-width="1.6" stroke-linecap="round"/>',
    greeting: 'Расскажу о направлениях, местах и проходных баллах',
    presets: [
      'Сколько бюджетных мест осталось?',
      'Какой средний балл ЕГЭ у поступивших в 2025 году?',
      'Сколько заявлений подано на направление «Экономика» в 2026 году?',
      'Какие направления подготовки есть в университете?'
    ]
  },
  {
    id: 'student',
    label: 'Студент',
    icon: '<path d="M4.5 5.4A2.4 2.4 0 0 1 6.9 3H20v14.6H6.9a2.4 2.4 0 0 0-2.4 2.4V5.4Z" stroke-width="1.6" stroke-linejoin="round"/><path d="M8.5 7.5h7M8.5 11h4.5" stroke-width="1.6" stroke-linecap="round"/>',
    greeting: 'Отвечу про дисциплины, расписание и учебные планы',
    presets: [
      'Какие дисциплины изучаются в этом семестре?',
      'Сколько студентов учится на моём направлении?',
      'Какие формы контроля по дисциплинам кафедры?',
      'Какое расписание у группы на этой неделе?'
    ]
  },
  {
    id: 'teacher',
    label: 'Преподаватель',
    icon: '<path d="M3.5 4.5h17v10.5h-17z" stroke-width="1.6" stroke-linejoin="round"/><path d="M12 15v2.5M9 21l3-3.5 3 3.5" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
    greeting: 'Покажу нагрузку, группы и статистику по дисциплинам',
    presets: [
      'Сколько студентов записано на курс «Базы данных»?',
      'Какая учебная нагрузка у преподавателей по семестрам?',
      'Сколько часов в дисциплинах моей кафедры?',
      'Сколько групп на каждом направлении?'
    ]
  },
  {
    id: 'staff',
    label: 'Администрация',
    icon: '<path d="M4 20.5V9l8-5 8 5v11.5" stroke-width="1.6" stroke-linejoin="round"/><path d="M9.5 20.5V15h5v5.5M3 20.5h18" stroke-width="1.6" stroke-linecap="round"/>',
    greeting: 'Соберу отчётность по факультетам, приёму и кафедрам',
    presets: [
      'Сколько студентов обучается на каждом факультете?',
      'Покажи динамику набора студентов за последние 5 лет',
      'Какая кафедра имеет наибольшую учебную нагрузку?',
      'Какова средняя заполняемость аудиторий?'
    ]
  }
];

/* Стадии пайплайна для индикатора-сонара. */
const SONAR_STEPS = [
  { text: 'Читаю схему базы данных',       at: 0 },
  { text: 'Генерирую SQL-запрос',          at: 700 },
  { text: 'Проверяю политику безопасности', at: 1800 },
  { text: 'Выполняю запрос в PostgreSQL',  at: 3100 }
];

/* ═══ 3. DOM ══════════════════════════════════════════════════════════ */

const $ = (id) => document.getElementById(id);

const dom = {
  body:       document.body,
  thread:     $('thread'),
  composer:   $('composer'),
  input:      $('userInput'),
  sendBtn:    $('sendBtn'),
  roles:      $('roles'),
  presets:    $('presets'),
  quick:      $('quick'),
  chatRole:   $('chatRole'),
  clearBtn:   $('clearBtn'),
  status:     $('status'),
  statusMini: $('statusMini'),
  statusText: $('statusText'),
  railOpen:   $('railOpen'),
  railClose:  $('railClose'),
  railScrim:  $('railScrim'),
  launcher:   $('widgetLauncher'),
  widgetClose:$('widgetClose')
};

/* ═══ 4. СОСТОЯНИЕ ════════════════════════════════════════════════════ */

const state = {
  roleId: ROLES[0].id,
  history: [],        // [{ who: 'user'|'bot'|'error', text, ts }]
  busy: false
};

function currentRole() {
  return ROLES.find((r) => r.id === state.roleId) || ROLES[0];
}

function loadState() {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);
    if (saved && ROLES.some((r) => r.id === saved.roleId)) state.roleId = saved.roleId;
    if (saved && Array.isArray(saved.history)) {
      state.history = saved.history
        .filter((m) => m && typeof m.text === 'string')
        .slice(-HISTORY_LIMIT);
    }
  } catch {
    /* повреждённое хранилище не должно ломать приложение */
  }
}

function saveState() {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({
      roleId: state.roleId,
      history: state.history.slice(-HISTORY_LIMIT)
    }));
  } catch {
    /* приватный режим — просто работаем без сохранения */
  }
}

/* ═══ 5. УТИЛИТЫ ══════════════════════════════════════════════════════ */

/** Экранирует символы, значимые для HTML. Всё, что пришло от LLM,
 *  проходит через неё либо вставляется через textContent. */
function esc(str) {
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function svg(inner, viewBox = '0 0 24 24') {
  const node = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  node.setAttribute('viewBox', viewBox);
  node.setAttribute('aria-hidden', 'true');
  node.innerHTML = inner;
  return node;
}

function clockLabel(ts) {
  return new Date(ts).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}

/** Значение выглядит числом (для выравнивания колонки вправо). */
function isNumeric(value) {
  const v = String(value).trim()
    .replace(/[\s ]/g, '')   // пробелы-разделители разрядов, в т.ч. неразрывные
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

/* ═══ 6. РАЗБОР ОТВЕТА НА БЛОКИ ═══════════════════════════════════════
   Ответ приходит одной строкой. Достаём из неё SQL и таблицы.
   ═══════════════════════════════════════════════════════════════════ */

const MD_SEPARATOR = /^\s*\|?[\s:|-]*-[\s:|-]*\|[\s:|-]*$/;

function splitRow(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|'))   s = s.slice(0, -1);
  return s.split('|').map((c) => c.trim());
}

/** Строка похожа на строку таблицы, а не на прозу с вертикальной чертой. */
function looksTabular(line) {
  if (!line.includes('|')) return false;
  const cells = splitRow(line);
  if (cells.length < 2) return false;
  // В прозе ячейки длинные и содержат точки-разделители предложений.
  return cells.every((c) => c.length <= 60 && !/\.\s/.test(c));
}

/**
 * Превращает текст ответа в последовательность блоков:
 *   { type: 'text',  text }
 *   { type: 'sql',   sql }
 *   { type: 'table', columns: string[]|null, rows: string[][] }
 */
function parseAnswer(raw) {
  const blocks = [];
  const text = String(raw ?? '').replace(/\r\n/g, '\n').trim();
  if (!text) return blocks;

  // 6.1 — сначала вынимаем ограждённые блоки кода
  const fence = /```([a-zA-Z]*)\r?\n?([\s\S]*?)```/g;
  let cursor = 0;
  let match;

  while ((match = fence.exec(text)) !== null) {
    pushProse(text.slice(cursor, match.index));

    const lang = (match[1] || '').toLowerCase();
    const code = match[2].trim();

    if (code) {
      if (lang === 'sql' || /^\s*(select|with)\b/i.test(code)) blocks.push({ type: 'sql', sql: code });
      else pushProse(code);
    }
    cursor = fence.lastIndex;
  }
  pushProse(text.slice(cursor));

  return blocks;

  // 6.2 — в обычном тексте ищем таблицы и «голый» SQL
  function pushProse(chunk) {
    const body = chunk.replace(/^\n+|\n+$/g, '');
    if (!body.trim()) return;

    const lines = body.split('\n');
    let buffer = [];   // накопитель обычного текста
    let i = 0;

    const flushText = () => {
      const t = buffer.join('\n').trim();
      if (t) blocks.push({ type: 'text', text: t });
      buffer = [];
    };

    while (i < lines.length) {
      const line = lines[i];

      // Незакрытый / неразмеченный SQL, начинающийся с новой строки
      if (/^\s*(SELECT|WITH)\b/i.test(line) && /\bFROM\b/i.test(lines.slice(i, i + 6).join(' '))) {
        const sqlLines = [];
        while (i < lines.length && lines[i].trim() !== '') {
          sqlLines.push(lines[i]);
          if (/;\s*$/.test(lines[i])) { i++; break; }
          i++;
        }
        flushText();
        blocks.push({ type: 'sql', sql: sqlLines.join('\n').trim().replace(/;$/, '') });
        continue;
      }

      // Блок табличных строк
      if (looksTabular(line)) {
        const run = [];
        while (i < lines.length && looksTabular(lines[i])) { run.push(lines[i]); i++; }

        const table = buildTable(run);
        if (table) { flushText(); blocks.push(table); }
        else buffer.push(...run);        // не похоже на таблицу — вернём как текст
        continue;
      }

      buffer.push(line);
      i++;
    }
    flushText();
  }
}

/** Собирает блок таблицы из набора строк. Возвращает null, если не вышло. */
function buildTable(run) {
  const lines = run.slice();

  // Markdown-разделитель ниже заголовка → первая строка это шапка
  let columns = null;
  if (lines.length >= 2 && MD_SEPARATOR.test(lines[1])) {
    columns = splitRow(lines[0]);
    lines.splice(0, 2);
  }

  let rows = lines.map(splitRow).filter((r) => r.some((c) => c !== ''));
  if (!rows.length && !columns) return null;

  // Одиночная строка без шапки — скорее проза, чем таблица
  if (!columns && rows.length < 2) return null;

  // Ширина колонок должна быть согласованной
  const width = Math.max(columns ? columns.length : 0, ...rows.map((r) => r.length));
  const consistent = rows.every((r) => Math.abs(r.length - width) <= 1);
  if (!consistent) return null;

  // Шапки не было, но первая строка выглядит как заголовки
  if (!columns && rows.length >= 2) {
    const head = rows[0];
    const headIsText = head.every((c) => c !== '' && !isNumeric(c));
    const bodyHasNums = rows.slice(1).some((r) => r.some(isNumeric));
    if (headIsText && bodyHasNums) { columns = head; rows = rows.slice(1); }
  }

  if (!rows.length) return null;

  const pad = (r) => Array.from({ length: width }, (_, k) => r[k] ?? '');
  return { type: 'table', columns: columns ? pad(columns) : null, rows: rows.map(pad) };
}

/* ═══ 7. РАЗБОР SQL: EXPLAINABLE AI ═══════════════════════════════════
   Считаем объяснение прямо из текста запроса — какие таблицы, JOIN,
   фильтры, агрегаты и ограничения использованы.
   ═══════════════════════════════════════════════════════════════════ */

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
  const grouping = groupMatch ? groupMatch[1].trim() : null;

  const aggregates = [...new Set(
    [...flat.matchAll(/\b(count|sum|avg|min|max|round)\s*\(/gi)].map((m) => m[1].toUpperCase())
  )];

  const limitMatch = flat.match(/\blimit\s+(\d+)/i);
  const limit = limitMatch ? Number(limitMatch[1]) : null;

  return { tables, joins, filters, grouping, aggregates, limit };
}

/* Подсветка SQL. Экранируем один раз, затем размечаем токены за один
   проход — так разметка не попадает внутрь уже размеченных фрагментов. */
const SQL_KEYWORDS = [
  'select', 'from', 'where', 'inner', 'left', 'right', 'full', 'cross', 'outer',
  'join', 'on', 'group', 'order', 'by', 'having', 'limit', 'offset', 'as',
  'and', 'or', 'not', 'in', 'is', 'null', 'distinct', 'with', 'union', 'all',
  'case', 'when', 'then', 'else', 'end', 'asc', 'desc', 'between', 'like',
  'ilike', 'exists', 'over', 'partition'
];

const SQL_TOKEN = new RegExp(
  `('(?:[^']|'')*')` +                      // строковые литералы
  `|\\b(${SQL_KEYWORDS.join('|')})\\b` +    // ключевые слова
  `|\\b([a-zA-Z_]\\w*)(?=\\s*\\()` +        // имена функций
  `|\\b(\\d+(?:\\.\\d+)?)\\b`,              // числа
  'gi'
);

function highlightSql(sql) {
  return esc(sql).replace(SQL_TOKEN, (whole, str, kw, fn, num) => {
    if (str) return `<span class="sql__str">${str}</span>`;
    if (kw)  return `<span class="sql__kw">${kw}</span>`;
    if (fn)  return `<span class="sql__fn">${fn}</span>`;
    if (num) return `<span class="sql__num">${num}</span>`;
    return whole;
  });
}

/* ═══ 8. РЕНДЕР ═══════════════════════════════════════════════════════ */

/** Лёгкий markdown: **жирный**, `код`, маркированные и нумерованные списки. */
function renderProse(text) {
  const frag = document.createDocumentFragment();
  const lines = text.split('\n');
  let i = 0;

  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');

  while (i < lines.length) {
    const bullet = /^\s*[-*•]\s+(.*)$/;
    const number = /^\s*(\d+)[.)]\s+(.*)$/;

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
      const div = el('div', 'msg__text');
      div.innerHTML = inline(body);
      frag.appendChild(div);
    }
  }
  return frag;
}

/** Раскрывающийся блок SQL + бейджи объяснения. */
function renderSqlBlock(sql) {
  const info = explainSql(sql);

  const box = el('details', 'sql');
  // Продвинутым пользователям SQL показываем сразу, остальным — по клику,
  // чтобы не загромождать ответ технической деталью.
  box.open = state.roleId === 'teacher' || state.roleId === 'staff';

  const head = el('summary', 'sql__head');

  head.appendChild(svg('<path d="M7 4l6 6-6 6" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>', '0 0 20 20'))
      .classList.add('sql__chevron');
  head.appendChild(el('span', null, 'SQL-запрос'));
  head.appendChild(el('span', 'sql__badge', 'проверен'));

  const copy = el('button', 'sql__copy', 'Копировать');
  copy.type = 'button';
  copy.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    copyText(sql).then(() => {
      copy.textContent = 'Скопировано';
      copy.dataset.done = '1';
      setTimeout(() => { copy.textContent = 'Копировать'; delete copy.dataset.done; }, 1600);
    });
  });
  head.appendChild(copy);
  box.appendChild(head);

  const code = el('pre', 'sql__code');
  code.innerHTML = highlightSql(sql);
  box.appendChild(code);

  // Explainable AI — структурированный разбор запроса
  const chips = el('div', 'explain');
  const chip = (label, value, mod) => {
    const c = el('span', `explain__chip${mod ? ` explain__chip--${mod}` : ''}`);
    c.appendChild(el('span', null, label));
    c.appendChild(el('b', null, value));
    c.title = `${label} ${value}`;
    chips.appendChild(c);
  };

  info.tables.forEach((t) => chip('таблица', t));
  info.joins.forEach((j) => chip('связь', j, 'join'));
  info.filters.forEach((f) => chip('фильтр', f.length > 42 ? `${f.slice(0, 41)}…` : f));
  if (info.grouping) chip('группировка', info.grouping.length > 42 ? `${info.grouping.slice(0, 41)}…` : info.grouping);
  info.aggregates.forEach((a) => chip('агрегат', `${a}()`, 'agg'));
  if (info.limit != null) chip('ограничение', `LIMIT ${info.limit}`, 'limit');

  if (chips.childElementCount) box.appendChild(chips);

  return { node: box, info };
}

/** Таблица результата + выгрузка CSV. */
function renderTable(block) {
  const wrap = el('div', 'tbl-block');

  const head = el('div', 'tbl-head');
  const n = block.rows.length;
  head.appendChild(el('span', 'tbl-head__count',
    `${n} ${plural(n, 'строка', 'строки', 'строк')} · ${block.columns ? block.columns.length : block.rows[0].length} ${plural(block.columns ? block.columns.length : block.rows[0].length, 'колонка', 'колонки', 'колонок')}`));

  const csvBtn = el('button', 'tbl-csv', 'Скачать CSV');
  csvBtn.type = 'button';
  csvBtn.addEventListener('click', () => downloadCsv(block));
  head.appendChild(csvBtn);
  wrap.appendChild(head);

  const scroll = el('div', 'tbl-scroll');
  const table = el('table', 'tbl');

  if (block.columns) {
    const thead = el('thead');
    const tr = el('tr');
    block.columns.forEach((c) => tr.appendChild(el('th', null, c)));
    thead.appendChild(tr);
    table.appendChild(thead);
  }

  // Колонка считается числовой, если числом является большинство её значений
  const width = block.columns ? block.columns.length : block.rows[0].length;
  const numericCol = Array.from({ length: width }, (_, k) => {
    const vals = block.rows.map((r) => r[k]).filter((v) => v !== '');
    return vals.length > 0 && vals.filter(isNumeric).length / vals.length >= 0.7;
  });

  const tbody = el('tbody');
  block.rows.forEach((row) => {
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

  table.appendChild(tbody);
  scroll.appendChild(table);
  wrap.appendChild(scroll);
  return wrap;
}

function renderNotice(text) {
  const box = el('div', 'notice');
  box.appendChild(el('span', 'notice__icon', '!'));
  box.appendChild(el('span', null, text));
  return box;
}

const AVATAR_BOT = '<circle cx="12" cy="12" r="2.4" fill="currentColor" stroke="none"/><path d="M12 6.5a5.5 5.5 0 0 1 5.5 5.5" stroke-width="1.8" stroke-linecap="round"/><path d="M12 2.5A9.5 9.5 0 0 1 21.5 12" stroke-width="1.6" stroke-linecap="round" opacity=".45"/>';

/** Создаёт каркас сообщения. */
function messageShell(who) {
  const article = el('article', `msg msg--${who}`);

  const avatar = el('div', 'msg__avatar');
  if (who === 'user') avatar.textContent = 'Я';
  else if (who === 'error') avatar.textContent = '!';
  else avatar.appendChild(svg(AVATAR_BOT));
  article.appendChild(avatar);

  const body = el('div', 'msg__body');
  const bubble = el('div', 'msg__bubble');
  body.appendChild(bubble);
  article.appendChild(body);

  return { article, body, bubble };
}

/** Рисует сообщение и добавляет его в ленту. */
function paintMessage(entry) {
  const { article, body, bubble } = messageShell(entry.who);

  if (entry.who === 'user') {
    bubble.textContent = entry.text;
  } else {
    const blocks = parseAnswer(entry.text);
    if (!blocks.length) {
      bubble.appendChild(el('div', 'msg__text', entry.text || 'Пустой ответ от сервера.'));
    }

    let limit = null;
    let lastRows = 0;

    blocks.forEach((block) => {
      if (block.type === 'text') {
        bubble.appendChild(renderProse(block.text));
      } else if (block.type === 'sql') {
        const { node, info } = renderSqlBlock(block.sql);
        bubble.appendChild(node);
        if (info.limit != null) limit = info.limit;
      } else if (block.type === 'table') {
        bubble.appendChild(renderTable(block));
        lastRows = block.rows.length;
      }
    });

    // Выборка упёрлась в LIMIT — предупреждаем и предлагаем сузить запрос
    if (limit != null && lastRows >= limit) {
      bubble.appendChild(renderNotice(
        `Показаны первые ${limit} ${plural(limit, 'строка', 'строки', 'строк')} — выборка ограничена. ` +
        'Уточните вопрос фильтрами (год, факультет, направление, статус), чтобы получить точный ответ.'
      ));
    }
  }

  body.appendChild(el('time', 'msg__time', clockLabel(entry.ts)));
  dom.thread.appendChild(article);
  return article;
}

/* — экран приветствия — */
function paintHello() {
  const role = currentRole();
  const hello = el('div', 'hello');

  const eye = el('div', 'hello__sonar');
  eye.appendChild(svg(
    '<circle cx="20" cy="20" r="3.6" fill="currentColor" stroke="none" style="color:var(--ice)"/>' +
    '<circle class="brand-mark__ring" cx="20" cy="20" r="9"    style="--i:0"/>' +
    '<circle class="brand-mark__ring" cx="20" cy="20" r="14"   style="--i:1"/>' +
    '<circle class="brand-mark__ring" cx="20" cy="20" r="18.5" style="--i:2"/>',
    '0 0 40 40'
  ));
  hello.appendChild(eye);

  hello.appendChild(el('h2', 'hello__title', 'Спросите о данных университета обычным языком'));
  hello.appendChild(el('p', 'hello__lead', `${role.greeting}. Ассистент сам построит запрос к базе, проверит его на безопасность и покажет результат.`));

  const cards = el('div', 'hello__cards');
  role.presets.slice(0, 3).forEach((q) => {
    const card = el('button', 'hello__card', q);
    card.type = 'button';
    card.addEventListener('click', () => ask(q));
    cards.appendChild(card);
  });
  hello.appendChild(cards);

  dom.thread.appendChild(hello);
}

function repaintThread() {
  dom.thread.replaceChildren();
  if (!state.history.length) { paintHello(); return; }
  state.history.forEach(paintMessage);
  scrollToEnd(true);
}

function scrollToEnd(force = false) {
  const t = dom.thread;
  const nearBottom = t.scrollHeight - t.scrollTop - t.clientHeight < 160;
  if (force || nearBottom) t.scrollTop = t.scrollHeight;
}

/* ═══ 9. СОНАР — ИНДИКАТОР ОБРАБОТКИ ══════════════════════════════════ */

function showSonar() {
  const { article, bubble } = messageShell('bot');
  article.dataset.pending = '1';

  const sonar = el('div', 'sonar');

  const eye = el('div', 'sonar__eye');
  eye.appendChild(svg(
    '<circle class="sonar__core" cx="17" cy="17" r="3"/>' +
    '<circle class="sonar__wave" cx="17" cy="17" r="7"  style="--i:0"/>' +
    '<circle class="sonar__wave" cx="17" cy="17" r="11" style="--i:1"/>' +
    '<circle class="sonar__wave" cx="17" cy="17" r="15" style="--i:2"/>',
    '0 0 34 34'
  ));
  sonar.appendChild(eye);

  const steps = el('div', 'sonar__steps');
  const nodes = SONAR_STEPS.map((s) => {
    const n = el('div', 'sonar__step', s.text);
    n.dataset.state = 'idle';
    steps.appendChild(n);
    return n;
  });
  sonar.appendChild(steps);
  bubble.appendChild(sonar);

  dom.thread.appendChild(article);
  scrollToEnd(true);

  // Подсвечиваем стадии по мере прохождения пайплайна
  nodes[0].dataset.state = 'active';
  const timers = SONAR_STEPS.slice(1).map((s, k) => setTimeout(() => {
    nodes[k].dataset.state = 'done';
    nodes[k + 1].dataset.state = 'active';
    scrollToEnd();
  }, s.at));

  return () => {
    timers.forEach(clearTimeout);
    article.remove();
  };
}

/* ═══ 10. ОБМЕН С БЭКЕНДОМ ════════════════════════════════════════════ */

function pushHistory(who, text) {
  const entry = { who, text, ts: Date.now() };
  state.history.push(entry);
  if (state.history.length > HISTORY_LIMIT) state.history = state.history.slice(-HISTORY_LIMIT);
  saveState();
  return entry;
}

async function ask(question) {
  const q = String(question || '').trim();
  if (!q || state.busy) return;

  // Первое сообщение убирает экран приветствия
  const hello = dom.thread.querySelector('.hello');
  if (hello) hello.remove();

  setBusy(true);
  paintMessage(pushHistory('user', q));
  scrollToEnd(true);

  const stopSonar = showSonar();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  try {
    const response = await fetch(API_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q, role: state.roleId }),
      signal: controller.signal
    });

    if (!response.ok) throw new Error(`сервер ответил ${response.status} ${response.statusText}`.trim());

    const data = await response.json();
    const answer = typeof data?.answer === 'string' && data.answer.trim()
      ? data.answer
      : 'Сервер вернул пустой ответ. Попробуйте переформулировать вопрос.';

    stopSonar();
    setStatus('online');

    // Ответы вида «[Ошибка БД] …» или «[Запрос отклонён…]» — это не сбой связи,
    // а корректно обработанная ситуация; показываем их отдельным стилем.
    const isRejected = /^\s*\[(ошибка|запрос отклонён|неожиданный ответ)/i.test(answer);
    paintMessage(pushHistory(isRejected ? 'error' : 'bot', answer));

  } catch (err) {
    stopSonar();
    setStatus('offline');

    const reason = err.name === 'AbortError'
      ? `Ассистент не ответил за ${Math.round(REQUEST_TIMEOUT_MS / 1000)} секунд. Возможно, запрос слишком тяжёлый — попробуйте сузить его фильтрами.`
      : `Не удалось связаться с ассистентом: ${err.message}. Проверьте, что бэкенд запущен на ${API_BASE}.`;

    paintMessage(pushHistory('error', reason));
  } finally {
    clearTimeout(timeout);
    setBusy(false);
    scrollToEnd(true);
    if (!isEmbedCollapsed()) dom.input.focus();
  }
}

function setBusy(value) {
  state.busy = value;
  dom.sendBtn.disabled = value || !dom.input.value.trim();
  dom.input.readOnly = value;
}

/* ═══ 11. СТАТУС ПОДКЛЮЧЕНИЯ ══════════════════════════════════════════ */

const STATUS_TEXT = {
  online:   'Ассистент на связи',
  offline:  'Бэкенд недоступен',
  checking: 'Проверяю связь…'
};

function setStatus(kind) {
  dom.status.dataset.state = kind;
  dom.statusMini.dataset.state = kind;
  dom.statusText.textContent = STATUS_TEXT[kind] || '';
  dom.statusMini.title = STATUS_TEXT[kind] || '';
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

/* ═══ 12. РОЛИ И ПРЕСЕТЫ В ИНТЕРФЕЙСЕ ════════════════════════════════ */

function paintRoles() {
  dom.roles.replaceChildren();

  ROLES.forEach((role) => {
    const btn = el('button', 'role');
    btn.type = 'button';
    btn.setAttribute('role', 'radio');
    btn.setAttribute('aria-checked', String(role.id === state.roleId));

    const icon = el('span', 'role__icon');
    icon.appendChild(svg(role.icon));
    btn.appendChild(icon);
    btn.appendChild(el('span', 'role__label', role.label));

    btn.addEventListener('click', () => selectRole(role.id));
    dom.roles.appendChild(btn);
  });
}

function paintPresets() {
  const role = currentRole();

  dom.presets.replaceChildren();
  dom.quick.replaceChildren();

  role.presets.forEach((q) => {
    const li = el('li');
    const btn = el('button', 'preset', q);
    btn.type = 'button';
    btn.addEventListener('click', () => ask(q));
    li.appendChild(btn);
    dom.presets.appendChild(li);

    const chip = el('button', 'quick__chip', q);
    chip.type = 'button';
    chip.addEventListener('click', () => ask(q));
    dom.quick.appendChild(chip);
  });

  dom.chatRole.textContent = `режим: ${role.label.toLowerCase()}`;
}

function selectRole(id) {
  if (id === state.roleId) return;
  state.roleId = id;
  saveState();

  paintRoles();
  paintPresets();

  // Приветствие подстраивается под роль, история сохраняется
  if (!state.history.length) repaintThread();
  closeRail();
}

/* ═══ 13. ВИДЖЕТ И БОКОВАЯ ПАНЕЛЬ ════════════════════════════════════ */

const isEmbed = new URLSearchParams(location.search).get('embed') === '1';

function isEmbedCollapsed() {
  return isEmbed && !dom.body.classList.contains('is-open');
}

function toggleWidget(open) {
  dom.body.classList.toggle('is-open', open);
  dom.launcher.setAttribute('aria-expanded', String(open));
  if (open) setTimeout(() => dom.input.focus(), 220);
}

function openRail()  { dom.body.classList.add('rail-open');  dom.railScrim.hidden = false; }
function closeRail() { dom.body.classList.remove('rail-open'); dom.railScrim.hidden = true; }

/* ═══ 14. ВСПОМОГАТЕЛЬНЫЕ ДЕЙСТВИЯ ═══════════════════════════════════ */

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // clipboard API недоступен вне https — старый способ
    const ta = el('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch { /* ничего не поделать */ }
    ta.remove();
  }
}

function downloadCsv(block) {
  const quote = (v) => `"${String(v).replace(/"/g, '""')}"`;
  const lines = [];
  if (block.columns) lines.push(block.columns.map(quote).join(';'));
  block.rows.forEach((r) => lines.push(r.map(quote).join(';')));

  // BOM — чтобы Excel корректно открыл кириллицу
  const blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = el('a');
  a.href = url;
  a.download = `эхолот-результат-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function autoGrow() {
  dom.input.style.height = 'auto';
  dom.input.style.height = `${Math.min(dom.input.scrollHeight, 150)}px`;
}

function clearHistory() {
  if (state.history.length && !confirm('Очистить историю переписки?')) return;
  state.history = [];
  saveState();
  repaintThread();
  dom.input.focus();
}

/* ═══ 15. ИНИЦИАЛИЗАЦИЯ ══════════════════════════════════════════════ */

function bindEvents() {
  dom.composer.addEventListener('submit', (e) => {
    e.preventDefault();
    if (state.busy) return;                 // не теряем текст при двойной отправке
    const q = dom.input.value;
    if (!q.trim()) return;
    dom.input.value = '';
    autoGrow();
    ask(q);
  });

  dom.input.addEventListener('input', () => {
    autoGrow();
    dom.sendBtn.disabled = state.busy || !dom.input.value.trim();
  });

  dom.input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      dom.composer.requestSubmit();
    }
  });

  dom.clearBtn.addEventListener('click', clearHistory);

  dom.railOpen.addEventListener('click', openRail);
  dom.railClose.addEventListener('click', closeRail);
  dom.railScrim.addEventListener('click', closeRail);

  dom.launcher.addEventListener('click', () => toggleWidget(!dom.body.classList.contains('is-open')));
  dom.widgetClose.addEventListener('click', () => toggleWidget(false));

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (dom.body.classList.contains('rail-open')) closeRail();
    else if (isEmbed && dom.body.classList.contains('is-open')) toggleWidget(false);
  });

  window.addEventListener('online',  checkHealth);
  window.addEventListener('offline', () => setStatus('offline'));
}

function init() {
  if (isEmbed) dom.body.classList.add('is-embed');

  loadState();
  paintRoles();
  paintPresets();
  repaintThread();
  bindEvents();
  autoGrow();
  setBusy(false);

  setStatus('checking');
  checkHealth();
  setInterval(checkHealth, HEALTH_EVERY_MS);

  if (!isEmbed) dom.input.focus();
}

init();
