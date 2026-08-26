-- Поиск по аналитике: падежи, буквы корпусов, идентификатор преподавателя.
--
-- Применять:
--   python backend/scripts/apply_sql.py backend/sql/012_analytics_search.sql --apply
--
-- ЗАЧЕМ. Сквозной прогон вопросов через живую модель
-- (backend/tests/eval_live_questions.py) показал три промаха, которых не было
-- видно на рукописном SQL:
--
-- 1. «Сколько должников на кафедре "Программная инженерия"» -> пусто.
--    Человек называет кафедру в именительном падеже, в базе она записана как
--    «Кафедра программной инженерии». Морфологический поиск в справочнике
--    направлений уже есть (миграция 011), а в аналитике его не было.
--
-- 2. «Самая перегруженная аудитория в корпусе А» -> пусто, и модель выдала
--    шаблон с подстановками вида [номер аудитории] вместо ответа. Корпуса
--    называются «Главный корпус», «Корпус Б», «Корпус В» и «Лабораторный
--    корпус» — буквы «А» среди них нет, хотя спрашивают именно так.
--
-- 3. «Преподаватели, не ведущие дисциплин» -> ошибка: модель написала
--    NOT IN (SELECT teacher_id FROM teacher_semester_load), а колонки
--    teacher_id в представлении не было.

-- ---------------------------------------------------------------------
-- 1. Буква корпуса
-- ---------------------------------------------------------------------
-- Главный корпус — это и есть корпус «А» в обиходе; остальные буквы уже
-- зашиты в названиях. Колонка добавочная, названия корпусов не меняются.

ALTER TABLE assistant.rooms
    ADD COLUMN IF NOT EXISTS building_code text;

UPDATE assistant.rooms SET building_code = CASE
    WHEN building ILIKE '%главн%'      THEN 'А'
    WHEN building ILIKE '%лаборатор%'  THEN 'Л'
    WHEN building ILIKE '%корпус б%'   THEN 'Б'
    WHEN building ILIKE '%корпус в%'   THEN 'В'
    ELSE upper(left(regexp_replace(building, '^[^А-Яа-я]*', ''), 1))
END
WHERE building_code IS NULL;

CREATE INDEX IF NOT EXISTS idx_rooms_building_code
    ON assistant.rooms (building_code);

-- ---------------------------------------------------------------------
-- 2. Аудитории с буквой корпуса и поиском
-- ---------------------------------------------------------------------

DROP VIEW IF EXISTS assistant.room_load;
CREATE VIEW assistant.room_load AS
SELECT
    r.building,
    r.building_code,
    r.number        AS room_number,
    r.room_type,
    r.capacity,
    count(s.id)                     AS lessons_per_week,
    count(DISTINCT s.group_id)      AS groups_count,
    count(DISTINCT s.weekday)       AS busy_days,
    to_tsvector('russian',
        coalesce(r.building, '') || ' корпус ' || coalesce(r.building_code, '') ||
        ' ' || coalesce(r.room_type, '')) AS search_vector
FROM assistant.rooms r
LEFT JOIN assistant.schedule s ON s.room_id = r.id
GROUP BY r.id, r.building, r.building_code, r.number, r.room_type, r.capacity;

COMMENT ON VIEW assistant.room_load IS
    'Загруженность аудиторий. building_code — буква корпуса: А, Б, В, Л.';


DROP VIEW IF EXISTS assistant.room_availability;
CREATE VIEW assistant.room_availability AS
SELECT
    r.building,
    r.building_code,
    r.number        AS room_number,
    r.room_type,
    r.capacity,
    wd.weekday,
    pt.pair_number,
    pt.starts_at,
    pt.ends_at,
    NOT EXISTS (
        SELECT 1 FROM assistant.schedule s
        WHERE s.room_id = r.id
          AND s.weekday = wd.weekday
          AND s.pair_number = pt.pair_number
    ) AS is_free
FROM assistant.rooms r
CROSS JOIN generate_series(1, 6) AS wd(weekday)
CROSS JOIN assistant.pair_times pt;

-- ---------------------------------------------------------------------
-- 3. Аналитика с морфологическим поиском
-- ---------------------------------------------------------------------
-- search_vector собирается из текстовых полей самого представления, поэтому
-- один и тот же запрос находит и по кафедре, и по факультету, и по группе:
--   WHERE search_vector @@ plainto_tsquery('russian', 'программная инженерия')

DROP VIEW IF EXISTS assistant.academic_debts;
CREATE VIEW assistant.academic_debts AS
SELECT
    s.last_name,
    s.first_name,
    s.middle_name,
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
GROUP BY s.id, s.last_name, s.first_name, s.middle_name, g.name, g.course,
         p.name, f.name, d.name;

COMMENT ON VIEW assistant.academic_debts IS
    'Задолженности по кафедрам. passed_count = 0 означает, что студент не '
    'сдал ни одного экзамена. Поиск по названиям — через search_vector.';


DROP VIEW IF EXISTS assistant.student_rankings;
CREATE VIEW assistant.student_rankings AS
SELECT
    s.last_name,
    s.first_name,
    s.middle_name,
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
GROUP BY s.id, s.last_name, s.first_name, s.middle_name, g.name, g.course,
         p.name, p.degree, f.name, c.semester, s.status, s.funding;


DROP VIEW IF EXISTS assistant.subject_performance;
CREATE VIEW assistant.subject_performance AS
SELECT
    sub.name        AS subject_name,
    d.name          AS department_name,
    f.name          AS faculty_name,
    p.name          AS program_name,
    c.semester,
    t.full_name     AS teacher_name,
    sub.control_form,
    count(*)                                    AS grades_count,
    round(avg(gr.score), 2)                     AS avg_score,
    count(*) FILTER (WHERE gr.score = 5)        AS excellent_count,
    count(*) FILTER (WHERE gr.score = 4)        AS good_count,
    count(*) FILTER (WHERE gr.score = 3)        AS satisfactory_count,
    count(*) FILTER (WHERE gr.score = 2)        AS failed_count,
    count(*) FILTER (WHERE gr.score IS NULL)    AS not_graded_count,
    count(*) FILTER (WHERE gr.attempt = 1)      AS first_attempt_total,
    count(*) FILTER (WHERE gr.attempt = 1 AND gr.score >= 3) AS first_attempt_passed,
    round(
        100.0 * count(*) FILTER (WHERE gr.attempt = 1 AND gr.score >= 3)
        / NULLIF(count(*) FILTER (WHERE gr.attempt = 1), 0), 1
    ) AS first_attempt_pass_rate,
    count(*) FILTER (WHERE gr.attempt > 1)      AS retake_count,
    to_tsvector('russian',
        coalesce(sub.name, '') || ' ' || coalesce(d.name, '') || ' ' ||
        coalesce(p.name, '')) AS search_vector
FROM assistant.grades gr
JOIN assistant.enrollments e  ON e.id = gr.enrollment_id
JOIN assistant.curriculum c   ON c.id = e.curriculum_id
JOIN assistant.subjects sub   ON sub.id = c.subject_id
JOIN assistant.departments d  ON d.id = sub.department_id
JOIN assistant.faculties f    ON f.id = d.faculty_id
JOIN assistant.programs p     ON p.id = c.program_id
LEFT JOIN assistant.teachers t ON t.id = c.teacher_id
GROUP BY sub.name, d.name, f.name, p.name, c.semester, t.full_name,
         sub.control_form;


DROP VIEW IF EXISTS assistant.department_performance;
CREATE VIEW assistant.department_performance AS
WITH university AS (
    SELECT round(avg(score), 2) AS avg_score
    FROM assistant.grades WHERE score IS NOT NULL
)
SELECT
    d.name          AS department_name,
    d.head_name,
    f.name          AS faculty_name,
    count(*)                        AS grades_count,
    round(avg(gr.score), 2)         AS avg_score,
    (SELECT avg_score FROM university) AS university_avg_score,
    round(avg(gr.score) - (SELECT avg_score FROM university), 2) AS diff_from_university,
    to_tsvector('russian', coalesce(d.name, '') || ' ' || coalesce(f.name, ''))
        AS search_vector
FROM assistant.departments d
JOIN assistant.faculties f    ON f.id = d.faculty_id
JOIN assistant.subjects sub   ON sub.department_id = d.id
JOIN assistant.curriculum c   ON c.subject_id = sub.id
JOIN assistant.enrollments e  ON e.curriculum_id = c.id
JOIN assistant.grades gr      ON gr.enrollment_id = e.id
WHERE gr.score IS NOT NULL
GROUP BY d.name, d.head_name, f.name;


DROP VIEW IF EXISTS assistant.department_workload;
CREATE VIEW assistant.department_workload AS
SELECT
    d.name          AS department_name,
    d.head_name,
    f.name          AS faculty_name,
    count(t.id)                                 AS teachers_count,
    sum(t.hours_per_year)                       AS total_hours,
    round(avg(t.hours_per_year), 1)             AS avg_hours_per_teacher,
    max(t.hours_per_year)                       AS max_hours_per_teacher,
    to_tsvector('russian', coalesce(d.name, '') || ' ' || coalesce(f.name, ''))
        AS search_vector
FROM assistant.departments d
JOIN assistant.faculties f ON f.id = d.faculty_id
LEFT JOIN assistant.teachers t ON t.department_id = d.id
GROUP BY d.name, d.head_name, f.name;


-- teacher_id добавлен, чтобы работали запросы вида
-- NOT IN (SELECT teacher_id FROM teacher_semester_load WHERE ...).
DROP VIEW IF EXISTS assistant.teacher_semester_load;
CREATE VIEW assistant.teacher_semester_load AS
SELECT
    t.id            AS teacher_id,
    t.full_name     AS teacher_name,
    t.position,
    t.degree,
    d.name          AS department_name,
    f.name          AS faculty_name,
    t.hours_per_year,
    sem.semester,
    count(DISTINCT c.subject_id)    AS subjects_count,
    count(DISTINCT c.program_id)    AS programs_count,
    count(DISTINCT e.student_id)    AS students_count,
    coalesce(sum(sub.hours), 0)     AS semester_hours,
    to_tsvector('russian',
        coalesce(t.full_name, '') || ' ' || coalesce(d.name, '') || ' ' ||
        coalesce(f.name, '')) AS search_vector
FROM assistant.teachers t
JOIN assistant.departments d ON d.id = t.department_id
JOIN assistant.faculties f   ON f.id = d.faculty_id
CROSS JOIN generate_series(1, 8) AS sem(semester)
LEFT JOIN assistant.curriculum c
       ON c.teacher_id = t.id AND c.semester = sem.semester
LEFT JOIN assistant.subjects sub ON sub.id = c.subject_id
LEFT JOIN assistant.enrollments e ON e.curriculum_id = c.id
GROUP BY t.id, t.full_name, t.position, t.degree, d.name, f.name,
         t.hours_per_year, sem.semester;


DROP VIEW IF EXISTS assistant.group_curriculum;
CREATE VIEW assistant.group_curriculum AS
SELECT
    g.name          AS group_name,
    g.course,
    p.name          AS program_name,
    p.degree,
    f.name          AS faculty_name,
    c.semester,
    CASE WHEN c.semester % 2 = 1 THEN 'осенний' ELSE 'весенний' END AS term_name,
    sub.name        AS subject_name,
    sub.control_form,
    sub.hours,
    d.name          AS department_name,
    t.full_name     AS teacher_name,
    to_tsvector('russian',
        coalesce(g.name, '') || ' ' || coalesce(sub.name, '') || ' ' ||
        coalesce(p.name, '') || ' ' || coalesce(f.name, '')) AS search_vector
FROM assistant.groups g
JOIN assistant.programs p    ON p.id = g.program_id
JOIN assistant.faculties f   ON f.id = p.faculty_id
JOIN assistant.curriculum c  ON c.program_id = p.id
JOIN assistant.subjects sub  ON sub.id = c.subject_id
JOIN assistant.departments d ON d.id = sub.department_id
LEFT JOIN assistant.teachers t ON t.id = c.teacher_id;


DROP VIEW IF EXISTS assistant.funding_share;
CREATE VIEW assistant.funding_share AS
SELECT
    p.name          AS program_name,
    p.degree,
    p.study_form,
    f.name          AS faculty_name,
    count(*)                                        AS students_total,
    count(*) FILTER (WHERE s.funding = 'бюджет')    AS budget_students,
    count(*) FILTER (WHERE s.funding = 'контракт')  AS paid_students,
    round(100.0 * count(*) FILTER (WHERE s.funding = 'контракт') / count(*)) AS paid_percent,
    to_tsvector('russian', coalesce(p.name, '') || ' ' || coalesce(f.name, ''))
        AS search_vector
FROM assistant.students s
JOIN assistant.groups g    ON g.id = s.group_id
JOIN assistant.programs p  ON p.id = g.program_id
JOIN assistant.faculties f ON f.id = p.faculty_id
GROUP BY p.name, p.degree, p.study_form, f.name;
