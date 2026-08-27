/* ═══════════════════════════════════════════════════════════════════════
   ВСТРАИВАЕМЫЙ ВИДЖЕТ — загрузчик
   ───────────────────────────────────────────────────────────────────────
   Одна строка на чужой странице:

     <script src="http://ваш-хост:8080/widget.js" defer></script>

   Необязательные настройки атрибутами:

     data-api      адрес бэкенда, если он не на :8000 того же хоста
     data-title    подпись в шапке виджета
     data-position left | right (по умолчанию right)

   ПОЧЕМУ IFRAME, А НЕ РАЗМЕТКА ПРЯМО НА СТРАНИЦЕ. Виджет встраивается на
   чужой сайт, где уже есть свой CSS, свои сбросы стилей и свои z-index.
   Вставленная в документ разметка неизбежно с ними столкнётся — либо сайт
   перекрасит виджет, либо виджет протечёт в сайт. Внутри iframe стили
   изолированы браузером, и обе стороны остаются целы. Плата за это —
   отдельный документ и обмен через postMessage, здесь он минимальный:
   только высота и запрос на закрытие.

   На странице-хозяине скрипт создаёт РОВНО два элемента: кнопку и iframe.
   Глобальных имён не заводит, обработчиков на document не вешает.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  var script = document.currentScript;
  if (!script) return;                       // подключили не тегом — уходим
  if (window.__universityAssistantWidget) return;   // уже встроен
  window.__universityAssistantWidget = true;

  var base = new URL('.', script.src).href;

  // Адрес бэкенда: либо задан явно, либо тот же хост на порту 8000 —
  // так же, как его выводит основной фронтенд.
  var api = script.dataset.api;
  if (!api) {
    var loc = new URL(script.src);
    api = loc.protocol + '//' + loc.hostname + ':8000';
  }
  api = String(api).replace(/\/+$/, '');

  var side = script.dataset.position === 'left' ? 'left' : 'right';
  var title = script.dataset.title || 'Спросить про поступление';

  var frameUrl = base + 'index.html'
    + '?api=' + encodeURIComponent(api)
    + '&title=' + encodeURIComponent(title);

  /* ── кнопка ─────────────────────────────────────────────────────── */
  var button = document.createElement('button');
  button.type = 'button';
  button.setAttribute('aria-label', title);
  button.textContent = title;
  style(button, {
    position: 'fixed', bottom: '24px', zIndex: '2147483000',
    padding: '13px 26px', border: '0', borderRadius: '75px',
    background: '#302f2c', color: '#efede3',
    font: '400 15px/1.15 Inter, system-ui, -apple-system, "Segoe UI", sans-serif',
    letterSpacing: '.01em', cursor: 'pointer',
    boxShadow: '0 6px 28px rgba(0,0,0,.28)',
    transition: 'transform .25s cubic-bezier(0.19,1,0.22,1), opacity .25s'
  });
  button.style[side] = '24px';

  /* ── окно ───────────────────────────────────────────────────────── */
  var frame = document.createElement('iframe');
  frame.title = title;
  frame.src = frameUrl;
  // Разрешаем ровно то, что нужно: свой скрипт и обращения к бэкенду.
  // allow-same-origin нужен виджету для localStorage (в нём хранится токен).
  frame.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms');
  style(frame, {
    position: 'fixed', bottom: '88px', zIndex: '2147483000',
    width: 'min(400px, calc(100vw - 32px))',
    height: 'min(600px, calc(100vh - 128px))',
    border: '0', borderRadius: '18px',
    boxShadow: '0 18px 60px rgba(0,0,0,.32)',
    background: '#302f2c',
    display: 'none',
    colorScheme: 'normal'
  });
  frame.style[side] = '24px';

  function style(node, rules) {
    for (var key in rules) node.style[key] = rules[key];
  }

  var open = false;
  function toggle(next) {
    open = next === undefined ? !open : next;
    frame.style.display = open ? 'block' : 'none';
    button.textContent = open ? 'Свернуть' : title;
    button.setAttribute('aria-expanded', String(open));
    if (open) frame.contentWindow.postMessage({ type: 'assistant:opened' }, '*');
  }

  button.addEventListener('click', function () { toggle(); });

  // Виджет просит закрыть себя сам (крестик в его шапке). Слушаем только
  // сообщения от нашего же окна: чужие страницы шлют в window что угодно.
  window.addEventListener('message', function (event) {
    if (event.source !== frame.contentWindow) return;
    if (event.data && event.data.type === 'assistant:close') toggle(false);
  });

  // Esc закрывает — привычно и не мешает странице-хозяину.
  window.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && open) toggle(false);
  });

  function mount() {
    document.body.appendChild(frame);
    document.body.appendChild(button);
  }

  if (document.body) mount();
  else document.addEventListener('DOMContentLoaded', mount);
})();
