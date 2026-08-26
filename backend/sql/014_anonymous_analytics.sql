-- Аналитика успеваемости без ФИО: обезличивание вместо ограничения по роли.
--
-- Применять:
--   python backend/scripts/apply_sql.py backend/sql/014_anonymous_analytics.sql --apply
--   (после 012_analytics_search.sql — здесь пересобираются его представления)
--
-- ЗАЧЕМ. Регламент прав (user_right.md) требует: данные студентов выводятся
-- ИСКЛЮЧИТЕЛЬНО в агрегированном или обезличенном виде, а статистика
-- отчислений — без ФИО. До сих пор student_rankings, academic_debts и
-- student_debts несли last_name/first_name/middle_name и выдавались деканату
-- и администрации поимённо. Это было осознанным расширением прав, но
-- регламенту оно противоречит, и решает вопрос не роль, а схема.
--
-- ПОЧЕМУ КОЛОНКИ, А НЕ ПРЕДСТАВЛЕНИЯ ЦЕЛИКОМ. Убрать вьюхи из whitelist
-- проще, но тогда деканат теряет ответы на свои же вопросы из регламента:
-- «какой средний GPA по факультету за весенний семестр», «сколько студентов
-- имеют академическую задолженность». Поэтому убираются ровно три колонки, а
-- ГРАНУЛЯЦИЯ СТРОКИ СОХРАНЯЕТСЯ: в student_rankings по-прежнему строка на
-- студента и семестр (GROUP BY s.id остаётся), в academic_debts — на студента
-- и кафедру. Благодаря этому avg() и count() считают то же самое, что и
-- раньше, и ни один агрегатный запрос переписывать не нужно.
--
-- Что при этом становится невозможным: «покажи топ-5 студентов С ИМЕНАМИ» и
-- «у какого студента больше всего долгов». Ответить на них теперь нечем — ФИО
-- в схеме не осталось ни у одной роли. Это и есть цель.
--
-- ЧТО ОСТАЁТСЯ ПОИМЁННЫМ. my_profile — собственная строка вошедшего студента,
-- отфильтрованная по app.student_id; человек видит своё имя, а не чужое.
-- teachers.full_name — публичные сведения о преподавателях, регламент их
-- выводить разрешает.
--
-- ПРО department_debts и student_debts. Обе вьюхи есть в базе, но в
-- репозитории их не было ни в одной миграции — они остались от более ранней
-- итерации схемы. department_debts зависит от academic_debts, поэтому
-- пересобрать вторую, не тронув первую, нельзя. Заодно они возвращаются в
-- репозиторий: раз источник истины — база, код обязан описывать то, что в ней
-- реально лежит.

-- ---------------------------------------------------------------------
-- 1. Зависимая вьюха — снимается первой
-- ---------------------------------------------------------------------
-- department_debts построена НАД academic_debts. Снимаем её явно, а не через
-- CASCADE: каскад молча утащил бы и то, о чём мы не знаем.

DROP VIEW IF EXISTS assistant.department_debts;

-- ---------------------------------------------------------------------
-- 2. Задолженности по кафедрам — без ФИО
-- ---------------------------------------------------------------------

DROP VIEW IF EXISTS assistant.academic_debts;
CREATE VIEW assistant.academic_debts AS
SELECT
    g.name          AS group_name,
    g.course,
    p.name          AS program_name,
    f.name          AS faculty_name,
    d.name          AS department_name,
    count(*) FILTER (WHERE gr.score = 2 OR gr.score IS NULL) AS debts_count,
    count(*) FILTER (WHERE gr.attempt > 1)                   AS retakes_count,
    count(*) FILTER (WHERE gr.score >= 3)                    AS passed_count,
    count(*)                                                 AS grades_total,
    to_tsvector('russian',
        coalesce(d.name, '') || ' ' || coalesce(f.name, '') || ' ' ||
        coalesce(p.name, '') || ' ' || coalesce(g.name, '')) AS search_vector
FROM assistant.students s
JOIN assistant.groups g       ON g.id = s.group_id
JOIN assistant.programs p     ON p.id = g.program_id
JOIN assistant.faculties f    ON f.id = p.faculty_id
JOIN assistant.enrollments e  ON e.student_id = s.id
JOIN assistant.curriculum c   ON c.id = e.curriculum_id
JOIN assistant.subjects sub   ON sub.id = c.subject_id
JOIN assistant.departments d  ON d.id = sub.department_id
JOIN assistant.grades gr      ON gr.enrollment_id = e.id
-- s.id в GROUP BY, но НЕ в SELECT: строка остаётся «на студента», из-за чего
-- count(*) по-прежнему считает людей, а не оценки, — но кто этот человек,
-- из представления не видно.
GROUP BY s.id, g.name, g.course, p.name, f.name, d.name;

COMMENT ON VIEW assistant.academic_debts IS
    'Задолженности по кафедрам, обезличенно: строка — один студент на одной '
    'кафедре, ФИО не выводится. passed_count = 0 означает, что студент не '
    'сдал ни одного экзамена. Поиск по названиям — через search_vector.';

-- ---------------------------------------------------------------------
-- 3. Задолженности по факультетам — без ФИО
-- ---------------------------------------------------------------------

DROP VIEW IF EXISTS assistant.student_debts;
CREATE VIEW assistant.student_debts AS
SELECT
    g.name          AS group_name,
    g.course,
    p.name          AS program_name,
    f.name          AS faculty_name,
    count(*) FILTER (WHERE gr.score = 2 OR gr.score IS NULL) AS debts_count,
    count(*) FILTER (WHERE gr.attempt > 1)                   AS retakes_count,
    count(*) FILTER (WHERE gr.score >= 3)                    AS passed_count,
    count(*)                                                 AS grades_total,
    to_tsvector('russian',
        coalesce(f.name, '') || ' ' || coalesce(p.name, '') || ' ' ||
        coalesce(g.name, '')) AS search_vector
FROM assistant.students s
JOIN assistant.groups g       ON g.id = s.group_id
JOIN assistant.programs p     ON p.id = g.program_id
JOIN assistant.faculties f    ON f.id = p.faculty_id
JOIN assistant.enrollments e  ON e.student_id = s.id
JOIN assistant.curriculum c   ON c.id = e.curriculum_id
JOIN assistant.grades gr      ON gr.enrollment_id = e.id
GROUP BY s.id, g.name, g.course, p.name, f.name;

COMMENT ON VIEW assistant.student_debts IS
    'Задолженности в разрезе группы и факультета, обезличенно: строка — один '
    'студент, ФИО не выводится. Без разбивки по кафедрам — для неё есть '
    'academic_debts.';

-- ---------------------------------------------------------------------
-- 4. Успеваемость по семестрам — без ФИО
-- ---------------------------------------------------------------------

DROP VIEW IF EXISTS assistant.student_rankings;
CREATE VIEW assistant.student_rankings AS
SELECT
    g.name          AS group_name,
    g.course,
    p.name          AS program_name,
    p.degree,
    f.name          AS faculty_name,
    c.semester,
    s.status,
    s.funding,
    count(gr.score)                         AS grades_count,
    round(avg(gr.score), 2)                 AS avg_score,
    count(*) FILTER (WHERE gr.score = 5)    AS excellent_count,
    count(*) FILTER (WHERE gr.score = 2)    AS failed_count,
    to_tsvector('russian',
        coalesce(f.name, '') || ' ' || coalesce(p.name, '') || ' ' ||
        coalesce(g.name, '')) AS search_vector
FROM assistant.students s
JOIN assistant.groups g       ON g.id = s.group_id
JOIN assistant.programs p     ON p.id = g.program_id
JOIN assistant.faculties f    ON f.id = p.faculty_id
JOIN assistant.enrollments e  ON e.student_id = s.id
JOIN assistant.curriculum c   ON c.id = e.curriculum_id
JOIN assistant.grades gr      ON gr.enrollment_id = e.id
WHERE gr.score IS NOT NULL
GROUP BY s.id, g.name, g.course, p.name, p.degree, f.name, c.semester,
         s.status, s.funding;

COMMENT ON VIEW assistant.student_rankings IS
    'Средний балл за семестр, обезличенно: строка — один студент в одном '
    'семестре, ФИО не выводится. Средний балл по факультету или группе '
    'считается как avg(avg_score) с нужным GROUP BY.';

-- ---------------------------------------------------------------------
-- 5. Возврат зависимой вьюхи
-- ---------------------------------------------------------------------
-- Определение прежнее: academic_debts под ней уже обезличена, а сама она ФИО
-- никогда и не показывала — это сводка по кафедрам.

CREATE VIEW assistant.department_debts AS
SELECT
    department_name,
    faculty_name,
    count(*)                                    AS students_total,
    count(*) FILTER (WHERE debts_count > 0)     AS debtors_count,
    sum(debts_count)                            AS debts_total,
    sum(retakes_count)                          AS retakes_total,
    round(100.0 * count(*) FILTER (WHERE debts_count > 0)::numeric
          / NULLIF(count(*), 0)::numeric, 1)    AS debtors_percent,
    to_tsvector('russian',
        coalesce(department_name, '') || ' ' || coalesce(faculty_name, ''))
        AS search_vector
FROM assistant.academic_debts
GROUP BY department_name, faculty_name;

COMMENT ON VIEW assistant.department_debts IS
    'Задолженности в разрезе кафедры: сколько всего студентов, сколько из них '
    'должников и какая это доля. Готовый ответ на «сколько должников на '
    'кафедре X» — считать по academic_debts вручную не нужно.';
