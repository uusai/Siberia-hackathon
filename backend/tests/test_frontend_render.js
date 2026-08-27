/**
 * Проверки чистых функций рендера из frontend/script.js.
 *
 *     node backend/tests/test_frontend_render.js
 *
 * Ни браузера, ни DOM здесь нет — берём из скрипта только функции, которые
 * работают с данными, и прогоняем их на случаях, ради которых они написаны:
 * длинные ссылки-источники, служебные колонки и пустые выборки.
 *
 * Зачем отдельным файлом: ошибка в этих трёх функциях ломает вёрстку любой
 * таблицы в ответе, а заметно это только глазами на демо.
 */

const fs = require('fs');
const path = require('path');

const SCRIPT = path.join(__dirname, '..', '..', 'frontend', 'script.js');
const source = fs.readFileSync(SCRIPT, 'utf8');

// Вырезаем объявления нужных функций из скрипта. Загрузить его целиком нельзя:
// на верхнем уровне он обращается к document и window.
// constantValue отсюда убран вместе с механизмом выноса колонки-источника
// под таблицу: ассистент отвечает по базе, и подпись «источник: isu.ru» под
// ответом выглядела так, будто данные взяты с сайта. Колонки происхождения
// теперь скрыты и от модели, и из таблицы — выносить стало нечего.
const NAMES = ['isUrl', 'linkLabel'];
let extracted = '';
for (const name of NAMES) {
  const match = source.match(new RegExp(`function ${name}\\([\\s\\S]*?\\n}`, 'm'));
  if (!match) {
    console.error(`Не найдена функция ${name} — проверьте frontend/script.js`);
    process.exit(1);
  }
  extracted += match[0] + '\n';
}
eval(extracted);

let failures = 0;

function check(label, got, expected) {
  const ok = JSON.stringify(got) === JSON.stringify(expected);
  if (!ok) failures += 1;
  const shown = JSON.stringify(got);
  console.log(
    `  ${ok ? '[OK]  ' : '[FAIL]'} ${label} -> ${shown}` +
    (ok ? '' : ` , ожидалось ${JSON.stringify(expected)}`)
  );
}

console.log('isUrl — ссылку отличаем от текста:');
check('pdf', isUrl('https://isu.ru/a.pdf'), true);
check('http', isUrl('http://isu.ru'), true);
check('название направления', isUrl('Юриспруденция'), false);
check('пусто', isUrl(''), false);
check('почта не ссылка', isUrl('zpk@isu.ru'), false);

console.log('\nlinkLabel — длинный URL сокращаем до подписи:');
check(
  'файл приложения',
  linkLabel('https://isu.ru/export/sites/isu/Abitur/pk2026/.galleries/docs/' +
            'docs_bak_2025/PRIL_BAK_2026.pdf'),
  'PRIL_BAK_2026.pdf'
);
check('страница', linkLabel('https://www.isu.ru/ru/university/structure/faculties/main/'), 'isu.ru');
check('битый адрес', linkLabel('не ссылка'), 'источник');

console.log('\nHIDDEN_COLUMNS — обвязка источников в таблицу не попадает:');
// Ассистент отвечает ПО БАЗЕ. Адрес PDF-файла, с которого данные однажды
// загрузили, в ответе выглядел так, будто ответ взят с сайта. Модель эти
// колонки уже не видит (ai_agent._PROVENANCE_COLUMNS), здесь вторая линия:
// даже если они как-то придут, в таблицу они не попадут.
const hiddenMatch = source.match(/const HIDDEN_COLUMNS = new Set\(\[([\s\S]*?)\]\)/);
const hiddenList = hiddenMatch ? (hiddenMatch[1].match(/'[^']+'/g) || []).map((s) => s.slice(1, -1)) : [];
['data_status', 'source_url', 'source_id', 'checked_at', 'page_url'].forEach((column) => {
  check(`${column} скрыт`, hiddenList.includes(column), true);
});
check('содержательные колонки не трогаем', hiddenList.includes('program_name'), false);

console.log(failures ? `\nНе пройдено: ${failures}` : '\nВсе проверки пройдены.');
process.exit(failures ? 1 : 0);
