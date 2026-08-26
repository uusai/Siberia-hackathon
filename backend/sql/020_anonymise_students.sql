-- Обезличивание студентов в аналитике.
--
-- Применять:
--   python backend/scripts/apply_sql.py backend/sql/020_anonymise_students.sql --apply
--
-- ---------------------------------------------------------------------
-- ЗАЧЕМ
-- ---------------------------------------------------------------------
--
-- Памятка участникам хакатона, раздел «Разграничение доступа и персональные
-- данные», говорит прямо:
--
--   ЗАПРЕЩЕНО ВЫВОДИТЬ: персональные данные студентов; персональные данные
--   абитуриентов; любые данные, позволяющие ИДЕНТИФИЦИРОВАТЬ обучающихся.
--
--   Все ответы должны предоставляться в агрегированном или обезличенном
--   формате, если речь идёт о студентах или абитуриентах.
--
-- Оговорок про роли там нет. А student_rankings, academic_debts и
-- student_debts отдавали ФИО студентов деканату и администрации — это было
-- записано в README как осознанное расширение прав, но требованию оно
-- противоречит напрямую.
--
-- Убираем last_name, first_name и middle_name из всех трёх. Остаётся то, что
-- отвечает на вопрос по существу и никого не опознаёт: группа, курс,
-- направление, факультет, кафедра и сами числа.
--
-- ФИО ПРЕПОДАВАТЕЛЕЙ НЕ ТРОГАЕМ: их вывод памятка разрешает явно
-- («ФИО преподавателей, деканов, сотрудников университета»).
--
-- my_profile тоже остаётся с именем: это профиль САМОГО вошедшего студента,
-- отфильтрованный по его идентификатору из подписанного токена. Показать
-- человеку его собственное имя — не раскрытие персональных данных третьему
-- лицу, а ответ на вопрос «что обо мне знает система».
--
-- ЧТО ЭТО ЗНАЧИТ ДЛЯ ОТВЕТОВ. Вопрос «выведи 5 лучших студентов С ИХ ИМЕНАМИ»
-- перестанет отвечаться именами — вернётся обезличенный рейтинг: группа,
-- направление и средний балл. Это не потеря функциональности, а соблюдение
-- требования: система не обязана отвечать на всё, но обязана не раскрывать
-- обучающихся.
--
-- ГРАНУЛЯРНОСТЬ НЕ МЕНЯЕТСЯ. Во всех трёх представлениях группировка идёт по
-- s.id, а имена — функционально зависимые от первичного ключа колонки.
-- Убрать их из SELECT можно, не трогая GROUP BY: строка по-прежнему один
-- студент (или пара «студент × кафедра» у academic_debts), и построенный
-- поверх department_debts продолжает считать людей верно.

-- ---------------------------------------------------------------------
-- 1. Рейтинг успеваемости
-- ---------------------------------------------------------------------

DROP VIEW IF EXISTS assistant.student_rankings;

CREATE VIEW assistant.student_rankings AS
SELECT
    g.name      AS group_name,
    g.course,
    p.name      AS program_name,
    f.name      AS faculty_name,
    c.semester,
    count(*)                    AS grades_count,
    round(avg(gr.score), 2)     AS avg_score,
    to_tsvector('russian',
        coalesce(f.name, '') || ' ' || coalesce(p.name, '') || ' ' ||
        coalesce(g.name, '')) AS search_vector
FROM assistant.students s
JOIN assistant.groups g      ON g.id = s.group_id
JOIN assistant.programs p    ON p.id = g.program_id
JOIN assistant.faculties f   ON f.id = p.faculty_id
JOIN assistant.enrollments e ON e.student_id = s.id
JOIN assistant.curriculum c  ON c.id = e.curriculum_id
JOIN assistant.grades gr     ON gr.enrollment_id = e.id
GROUP BY s.id, g.name, g.course, p.name, f.name, c.semester
-- Строки, где ни одной оценки ещё не выставлено, в рейтинге бессмысленны:
-- avg_score у них NULL, а ORDER BY avg_score DESC ставит NULL ПЕРВЫМИ — то
-- есть «топ студентов» начинался с тех, у кого баллов нет вовсе.
HAVING count(gr.score) > 0;

COMMENT ON VIEW assistant.student_rankings IS
    'Строка = пара «студент × семестр», БЕЗ ФИО. Средний балл здесь за один '
    'семестр. Обезличено: назвать конкретного студента по этим полям нельзя. '
    'Для «лучших» сортируйте по avg_score DESC — строки без оценок исключены.';


-- ---------------------------------------------------------------------
-- 2. Задолженности по кафедрам
-- ---------------------------------------------------------------------

DROP VIEW IF EXISTS assistant.department_debts;
DROP VIEW IF EXISTS assistant.academic_debts;

CREATE VIEW assistant.academic_debts AS
SELECT
    g.name      AS group_name,
    g.course,
    p.name      AS program_name,
    f.name      AS faculty_name,
    d.name      AS department_name,
    count(*) FILTER (WHERE gr.score = 2 OR gr.score IS NULL) AS debts_count,
    count(*) FILTER (WHERE gr.attempt > 1)                   AS retakes_count,
    count(*) FILTER (WHERE gr.score >= 3)                    AS passed_count,
    count(*)                                                 AS grades_total,
    to_tsvector('russian',
        coalesce(d.name, '') || ' ' || coalesce(f.name, '') || ' ' ||
        coalesce(p.name, '') || ' ' || coalesce(g.name, '')) AS search_vector
FROM assistant.students s
JOIN assistant.groups g      ON g.id = s.group_id
JOIN assistant.programs p    ON p.id = g.program_id
JOIN assistant.faculties f   ON f.id = p.faculty_id
JOIN assistant.enrollments e ON e.student_id = s.id
JOIN assistant.curriculum c  ON c.id = e.curriculum_id
JOIN assistant.subjects sub  ON sub.id = c.subject_id
JOIN assistant.departments d ON d.id = sub.department_id
JOIN assistant.grades gr     ON gr.enrollment_id = e.id
GROUP BY s.id, g.name, g.course, p.name, f.name, d.name;

COMMENT ON VIEW assistant.academic_debts IS
    'Строка = пара «студент × кафедра», БЕЗ ФИО: студент встречается столько '
    'раз, сколько кафедр ведёт его предметы. Для подсчёта ЛЮДЕЙ берите '
    'student_debts или department_debts, здесь COUNT(*) считает пары.';

-- Пересоздаём поверх обезличенной academic_debts. Логика не изменилась:
-- внутри одной кафедры строка — ровно один студент, поэтому count(*) честно
-- считает людей.
CREATE VIEW assistant.department_debts AS
SELECT
    department_name,
    faculty_name,
    count(*)                                    AS students_total,
    count(*) FILTER (WHERE debts_count > 0)     AS debtors_count,
    sum(debts_count)                            AS debts_total,
    sum(retakes_count)                          AS retakes_total,
    round(100.0 * count(*) FILTER (WHERE debts_count > 0)
          / nullif(count(*), 0), 1)             AS debtors_percent,
    to_tsvector('russian',
        coalesce(department_name, '') || ' ' || coalesce(faculty_name, ''))
        AS search_vector
FROM assistant.academic_debts
GROUP BY department_name, faculty_name;

COMMENT ON VIEW assistant.department_debts IS
    'Строка = ОДНА кафедра. debtors_count — сколько ЧЕЛОВЕК имеют долги, '
    'debts_total — сколько всего задолженностей. Это разные числа.';


-- ---------------------------------------------------------------------
-- 3. Задолженности по студентам
-- ---------------------------------------------------------------------

DROP VIEW IF EXISTS assistant.student_debts;

CREATE VIEW assistant.student_debts AS
SELECT
    g.name      AS group_name,
    g.course,
    p.name      AS program_name,
    f.name      AS faculty_name,
    count(*) FILTER (WHERE gr.score = 2 OR gr.score IS NULL) AS debts_count,
    count(*) FILTER (WHERE gr.attempt > 1)                   AS retakes_count,
    count(*) FILTER (WHERE gr.score >= 3)                    AS passed_count,
    count(*)                                                 AS grades_total,
    to_tsvector('russian',
        coalesce(f.name, '') || ' ' || coalesce(p.name, '') || ' ' ||
        coalesce(g.name, '')) AS search_vector
FROM assistant.students s
JOIN assistant.groups g      ON g.id = s.group_id
JOIN assistant.programs p    ON p.id = g.program_id
JOIN assistant.faculties f   ON f.id = p.faculty_id
JOIN assistant.enrollments e ON e.student_id = s.id
JOIN assistant.curriculum c  ON c.id = e.curriculum_id
JOIN assistant.grades gr     ON gr.enrollment_id = e.id
GROUP BY s.id, g.name, g.course, p.name, f.name;

COMMENT ON VIEW assistant.student_debts IS
    'Строка = ОДИН студент, БЕЗ ФИО. Долги, пересдачи и сданное по всему '
    'обучению. Для подсчёта людей используйте это представление, а не '
    'academic_debts. Назвать конкретного студента по этим полям нельзя.';
