-- Личные представления преподавателя.
--
-- Применять вручную:
--   psql -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME -f backend/sql/004_teacher_views.sql
--
-- Устройство то же, что у студенческих my_* из 003_role_access.sql: фильтр
-- живёт внутри вьюхи и опирается на сессионную переменную app.teacher_id,
-- которую бэкенд выставляет из проверенного токена. Модель пишет просто
-- SELECT * FROM my_teaching и подставить чужой идентификатор не может —
-- set_config запрещён проверкой в security.py.
--
-- Переменная не выставлена -> current_setting(..., true) вернёт NULL ->
-- ни одной строки. По умолчанию не видно ничего.

-- Что преподаватель ведёт: предметы, программы, семестры и сколько человек
-- на них записано.
CREATE OR REPLACE VIEW assistant.my_teaching AS
SELECT
    sub.name          AS subject_name,
    p.name            AS program_name,
    p.degree,
    c.semester,
    sub.control_form,
    sub.hours,
    COUNT(DISTINCT e.student_id) AS students_count
FROM assistant.curriculum c
JOIN assistant.subjects sub ON sub.id = c.subject_id
JOIN assistant.programs p   ON p.id = c.program_id
LEFT JOIN assistant.enrollments e ON e.curriculum_id = c.id
WHERE c.teacher_id = NULLIF(current_setting('app.teacher_id', true), '')::int
GROUP BY sub.name, p.name, p.degree, c.semester, sub.control_form, sub.hours;


-- Собственное расписание преподавателя, а не расписание группы.
CREATE OR REPLACE VIEW assistant.my_teaching_schedule AS
SELECT
    sc.weekday,
    sc.pair_number,
    sc.week_type,
    sub.name AS subject_name,
    g.name   AS group_name,
    g.course,
    r.building,
    r.number AS room_number
FROM assistant.schedule sc
JOIN assistant.curriculum c  ON c.id = sc.curriculum_id
JOIN assistant.subjects sub  ON sub.id = c.subject_id
JOIN assistant.groups g      ON g.id = sc.group_id
LEFT JOIN assistant.rooms r  ON r.id = sc.room_id
WHERE c.teacher_id = NULLIF(current_setting('app.teacher_id', true), '')::int;


-- Успеваемость по СВОИМ предметам. Строго агрегированно: ни фамилий, ни
-- идентификаторов студентов здесь нет и появляться не должно — иначе
-- вьюха превратится в обход закрытой таблицы students.
CREATE OR REPLACE VIEW assistant.my_students_performance AS
SELECT
    sub.name   AS subject_name,
    p.name     AS program_name,
    c.semester,
    COUNT(*)                                   AS grades_count,
    ROUND(AVG(gr.score), 2)                    AS avg_score,
    COUNT(*) FILTER (WHERE gr.score = 5)       AS excellent_count,
    COUNT(*) FILTER (WHERE gr.score = 2)       AS failed_count,
    COUNT(*) FILTER (WHERE gr.attempt > 1)     AS retake_count
FROM assistant.curriculum c
JOIN assistant.subjects sub  ON sub.id = c.subject_id
JOIN assistant.programs p    ON p.id = c.program_id
JOIN assistant.enrollments e ON e.curriculum_id = c.id
JOIN assistant.grades gr     ON gr.enrollment_id = e.id
WHERE c.teacher_id = NULLIF(current_setting('app.teacher_id', true), '')::int
GROUP BY sub.name, p.name, c.semester;
