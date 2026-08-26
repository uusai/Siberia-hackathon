-- Ролевой доступ + привязка учётной записи к человеку в базе.
--
-- Применять вручную:
--   psql -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME -f backend/sql/003_role_access.sql
--
-- ИДЕЯ ЛИЧНЫХ ДАННЫХ. SQL пишет языковая модель, поэтому доверять ей
-- подстановку «своего» идентификатора нельзя: она может написать любой.
-- Вместо этого личные вьюхи my_* сами фильтруются по сессионной переменной
-- app.student_id, которую бэкенд выставляет из ПРОВЕРЕННОГО JWT перед
-- каждым запросом (db.fetch_all(..., session_vars=...)). Модель пишет
-- просто SELECT * FROM my_grades и физически не может дотянуться до чужих
-- данных: фильтр живёт внутри вьюхи, а не в её запросе.
--
-- Если переменная не выставлена, current_setting(..., true) вернёт NULL,
-- сравнение s.id = NULL не даст ни одной строки. То есть поведение по
-- умолчанию — «ничего не видно», а не «видно всё».

-- ---------------------------------------------------------------------
-- 1. Привязка учётки к человеку
-- ---------------------------------------------------------------------

ALTER TABLE auth.users
    ADD COLUMN IF NOT EXISTS student_id integer REFERENCES assistant.students(id),
    ADD COLUMN IF NOT EXISTS teacher_id integer REFERENCES assistant.teachers(id);

-- Привязка к студенту осмысленна только у роли student, к преподавателю —
-- только у teacher. Констрейнт не даёт развести их со временем.
ALTER TABLE auth.users DROP CONSTRAINT IF EXISTS users_person_link_ck;
ALTER TABLE auth.users ADD CONSTRAINT users_person_link_ck CHECK (
    (student_id IS NULL OR role = 'student')
    AND (teacher_id IS NULL OR role = 'teacher')
);

-- ---------------------------------------------------------------------
-- 2. Личные вьюхи студента
-- ---------------------------------------------------------------------
-- Паспорт, телефон и дата рождения не выставляются нигде: они и в
-- security.FORBIDDEN_COLUMNS, и по существу не нужны для ответов.

CREATE OR REPLACE VIEW assistant.my_profile AS
SELECT
    s.last_name,
    s.first_name,
    s.middle_name,
    s.email,
    g.name  AS group_name,
    g.course,
    p.name  AS program_name,
    p.degree,
    p.study_form,
    f.name  AS faculty_name,
    s.enrolled_year,
    s.status,
    s.funding
FROM assistant.students s
JOIN assistant.groups g    ON g.id = s.group_id
JOIN assistant.programs p  ON p.id = g.program_id
JOIN assistant.faculties f ON f.id = p.faculty_id
WHERE s.id = NULLIF(current_setting('app.student_id', true), '')::int;


CREATE OR REPLACE VIEW assistant.my_grades AS
SELECT
    sub.name        AS subject_name,
    c.semester,
    t.full_name     AS teacher_name,
    gr.score,
    gr.attempt,
    gr.graded_at
FROM assistant.students s
JOIN assistant.enrollments e ON e.student_id = s.id
JOIN assistant.curriculum c  ON c.id = e.curriculum_id
JOIN assistant.subjects sub  ON sub.id = c.subject_id
LEFT JOIN assistant.teachers t ON t.id = c.teacher_id
JOIN assistant.grades gr     ON gr.enrollment_id = e.id
WHERE s.id = NULLIF(current_setting('app.student_id', true), '')::int;


CREATE OR REPLACE VIEW assistant.my_schedule AS
SELECT
    sc.weekday,
    sc.pair_number,
    sc.week_type,
    sub.name    AS subject_name,
    t.full_name AS teacher_name,
    r.building,
    r.number    AS room_number,
    g.name      AS group_name
FROM assistant.students s
JOIN assistant.groups g      ON g.id = s.group_id
JOIN assistant.schedule sc   ON sc.group_id = g.id
JOIN assistant.curriculum c  ON c.id = sc.curriculum_id
JOIN assistant.subjects sub  ON sub.id = c.subject_id
LEFT JOIN assistant.teachers t ON t.id = c.teacher_id
LEFT JOIN assistant.rooms r    ON r.id = sc.room_id
WHERE s.id = NULLIF(current_setting('app.student_id', true), '')::int;
