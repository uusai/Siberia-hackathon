-- Аналитика учебного процесса: успеваемость, нагрузка, аудитории, приём.
--
-- Применять:
--   python backend/scripts/apply_sql.py backend/sql/009_analytics_views.sql --apply
--
-- ЗАЧЕМ. Демо-контур уже содержит 4981 студента, 96 299 оценок и 968 занятий,
-- но добраться до них можно было только через три агрегата (students_summary,
-- grades_summary, applications_summary), где нет ни дисциплины, ни кафедры, ни
-- преподавателя. Вопросы вида «процент сдавших базы данных с первой попытки»,
-- «какие аудитории свободны в понедельник на второй паре» или «кафедры со
-- средним баллом ниже общего» на них не отвечались вообще — не потому что
-- данных нет, а потому что до них не дотянуться.
--
-- ПРО ПЕРСОНАЛЬНЫЕ ДАННЫЕ. Два представления ниже (student_rankings и
-- academic_debts) показывают ФИО студентов. Это осознанное расширение, а не
-- недосмотр:
--   - выдаются ТОЛЬКО ролям deans-office и administration (см. security.py);
--   - содержат фамилию, имя, отчество, группу, факультет и успеваемость;
--   - НЕ содержат паспорт, телефон, почту и дату рождения — эти поля
--     остаются в security.FORBIDDEN_COLUMNS и не появляются здесь ни при
--     каких условиях.
-- Деканат по должности работает с успеваемостью поимённо; студент и
-- преподаватель этих представлений не получают.
--
-- Абитуриенты (assistant.applications) поимённо не раскрываются никому:
-- там паспорт, телефон и почта живого человека, и никакой роли они не нужны.

-- ---------------------------------------------------------------------
-- 1. Успеваемость студентов (deans-office, administration)
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW assistant.student_rankings AS
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
    count(*) FILTER (WHERE gr.score = 2)    AS failed_count
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

COMMENT ON VIEW assistant.student_rankings IS
    'Средний балл студента по семестрам. Только для деканата и администрации.';


-- Академические задолженности: двойка либо неаттестация (score IS NULL).
-- Строка на связку студент + кафедра, чтобы отвечать и «кто самый
-- проблемный в группе», и «сколько должников на кафедре».
CREATE OR REPLACE VIEW assistant.academic_debts AS
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
    count(*)                                                 AS grades_total
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
    'Задолженности студента по кафедрам: двойки, неаттестации, пересдачи.';

-- ---------------------------------------------------------------------
-- 2. Успеваемость по дисциплинам (teacher и выше)
-- ---------------------------------------------------------------------
-- Обезличено: ни ФИО, ни идентификаторов студентов. Поэтому доступно и
-- преподавателю, а не только деканату.

CREATE OR REPLACE VIEW assistant.subject_performance AS
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
    -- Доля сдавших с первой попытки. NULLIF защищает от деления на ноль,
    -- если по дисциплине вообще не было первых попыток.
    round(
        100.0 * count(*) FILTER (WHERE gr.attempt = 1 AND gr.score >= 3)
        / NULLIF(count(*) FILTER (WHERE gr.attempt = 1), 0), 1
    ) AS first_attempt_pass_rate,
    count(*) FILTER (WHERE gr.attempt > 1)      AS retake_count
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

COMMENT ON VIEW assistant.subject_performance IS
    'Успеваемость по дисциплинам: распределение оценок и доля сдавших с первой попытки.';

-- ---------------------------------------------------------------------
-- 3. Кафедры (deans-office, administration)
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW assistant.department_performance AS
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
    round(avg(gr.score) - (SELECT avg_score FROM university), 2) AS diff_from_university
FROM assistant.departments d
JOIN assistant.faculties f    ON f.id = d.faculty_id
JOIN assistant.subjects sub   ON sub.department_id = d.id
JOIN assistant.curriculum c   ON c.subject_id = sub.id
JOIN assistant.enrollments e  ON e.curriculum_id = c.id
JOIN assistant.grades gr      ON gr.enrollment_id = e.id
WHERE gr.score IS NOT NULL
GROUP BY d.name, d.head_name, f.name;

COMMENT ON VIEW assistant.department_performance IS
    'Средний балл по кафедрам в сравнении со средним по университету.';


CREATE OR REPLACE VIEW assistant.department_workload AS
SELECT
    d.name          AS department_name,
    d.head_name,
    f.name          AS faculty_name,
    count(t.id)                                 AS teachers_count,
    sum(t.hours_per_year)                       AS total_hours,
    round(avg(t.hours_per_year), 1)             AS avg_hours_per_teacher,
    max(t.hours_per_year)                       AS max_hours_per_teacher
FROM assistant.departments d
JOIN assistant.faculties f ON f.id = d.faculty_id
LEFT JOIN assistant.teachers t ON t.department_id = d.id
GROUP BY d.name, d.head_name, f.name;

-- ---------------------------------------------------------------------
-- 4. Нагрузка преподавателей (deans-office, administration)
-- ---------------------------------------------------------------------
-- CROSS JOIN по семестрам нужен, чтобы в выдаче были и те, у кого в семестре
-- НОЛЬ дисциплин: вопрос «кто не ведёт ничего в этом семестре» иначе
-- неотвечаем — таких строк в curriculum просто нет.

CREATE OR REPLACE VIEW assistant.teacher_semester_load AS
SELECT
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
    coalesce(sum(sub.hours), 0)     AS semester_hours
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

COMMENT ON VIEW assistant.teacher_semester_load IS
    'Нагрузка преподавателя по семестрам. subjects_count = 0 означает, '
    'что в этом семестре он не ведёт ничего.';

-- ---------------------------------------------------------------------
-- 5. Аудитории (доступно всем ролям — ничего личного)
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW assistant.room_load AS
SELECT
    r.building,
    r.number        AS room_number,
    r.room_type,
    r.capacity,
    count(s.id)                     AS lessons_per_week,
    count(DISTINCT s.group_id)      AS groups_count,
    count(DISTINCT s.weekday)       AS busy_days
FROM assistant.rooms r
LEFT JOIN assistant.schedule s ON s.room_id = r.id
GROUP BY r.id, r.building, r.number, r.room_type, r.capacity;

COMMENT ON VIEW assistant.room_load IS
    'Загруженность аудиторий: сколько занятий в неделю в каждой.';


-- Свободна ли аудитория в конкретном слоте. Строится перекрёстным
-- произведением аудиторий, дней и пар: «свободных» строк в schedule нет по
-- определению, их приходится порождать.
CREATE OR REPLACE VIEW assistant.room_availability AS
SELECT
    r.building,
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

COMMENT ON VIEW assistant.room_availability IS
    'Занятость аудиторий по слотам: is_free = true означает «свободна».';

-- ---------------------------------------------------------------------
-- 6. Учебный план группы (доступно всем ролям)
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW assistant.group_curriculum AS
SELECT
    g.name          AS group_name,
    g.course,
    p.name          AS program_name,
    p.degree,
    f.name          AS faculty_name,
    c.semester,
    -- Нечётные семестры читаются осенью, чётные весной: другого признака
    -- сезона в учебном плане нет.
    CASE WHEN c.semester % 2 = 1 THEN 'осенний' ELSE 'весенний' END AS term_name,
    sub.name        AS subject_name,
    sub.control_form,
    sub.hours,
    d.name          AS department_name,
    t.full_name     AS teacher_name
FROM assistant.groups g
JOIN assistant.programs p    ON p.id = g.program_id
JOIN assistant.faculties f   ON f.id = p.faculty_id
JOIN assistant.curriculum c  ON c.program_id = p.id
JOIN assistant.subjects sub  ON sub.id = c.subject_id
JOIN assistant.departments d ON d.id = sub.department_id
LEFT JOIN assistant.teachers t ON t.id = c.teacher_id;

COMMENT ON VIEW assistant.group_curriculum IS
    'Какие дисциплины читаются у группы в каком семестре и кем.';

-- ---------------------------------------------------------------------
-- 7. Контингент и оплата (deans-office, administration)
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW assistant.funding_share AS
SELECT
    p.name          AS program_name,
    p.degree,
    p.study_form,
    f.name          AS faculty_name,
    count(*)                                        AS students_total,
    count(*) FILTER (WHERE s.funding = 'бюджет')    AS budget_students,
    count(*) FILTER (WHERE s.funding = 'контракт')  AS paid_students,
    round(100.0 * count(*) FILTER (WHERE s.funding = 'контракт') / count(*)) AS paid_percent
FROM assistant.students s
JOIN assistant.groups g    ON g.id = s.group_id
JOIN assistant.programs p  ON p.id = g.program_id
JOIN assistant.faculties f ON f.id = p.faculty_id
GROUP BY p.name, p.degree, p.study_form, f.name;

COMMENT ON VIEW assistant.funding_share IS
    'Доля платных и бюджетных студентов по направлениям, в процентах.';

-- ---------------------------------------------------------------------
-- 8. Приёмная кампания демо-контура (administration)
-- ---------------------------------------------------------------------
-- Строго обезличено: дата, статус, форма оплаты и счётчики. Ни ФИО, ни
-- паспорта, ни телефона, ни почты абитуриента здесь нет и быть не может —
-- иначе представление превратится в обход закрытой таблицы applications.

CREATE OR REPLACE VIEW assistant.applications_by_day AS
SELECT
    ac.year         AS campaign_year,
    a.submitted_at,
    ac.docs_from,
    ac.docs_to,
    p.name          AS program_name,
    p.degree,
    a.status,
    a.funding_type,
    count(*)                    AS applications_count,
    round(avg(a.ege_total), 1)  AS avg_ege_total,
    max(a.ege_total)            AS max_ege_total
FROM assistant.applications a
JOIN assistant.admission_campaigns ac ON ac.id = a.campaign_id
JOIN assistant.programs p ON p.id = ac.program_id
GROUP BY ac.year, a.submitted_at, ac.docs_from, ac.docs_to, p.name, p.degree,
         a.status, a.funding_type;

COMMENT ON VIEW assistant.applications_by_day IS
    'Заявления абитуриентов по дням подачи. Обезличено.';


CREATE OR REPLACE VIEW assistant.admission_dynamics AS
SELECT
    ac.year         AS campaign_year,
    p.name          AS program_name,
    p.degree,
    f.name          AS faculty_name,
    a.funding_type,
    count(*)                                        AS applications_count,
    count(*) FILTER (WHERE a.status = 'зачислен')   AS enrolled_count,
    round(avg(a.ege_total), 1)                      AS avg_ege_total,
    min(ac.budget_seats)                            AS budget_seats,
    min(ac.paid_seats)                              AS paid_seats
FROM assistant.applications a
JOIN assistant.admission_campaigns ac ON ac.id = a.campaign_id
JOIN assistant.programs p  ON p.id = ac.program_id
JOIN assistant.faculties f ON f.id = p.faculty_id
GROUP BY ac.year, p.name, p.degree, f.name, a.funding_type;

COMMENT ON VIEW assistant.admission_dynamics IS
    'Динамика приёма по годам: подано, зачислено, средний балл ЕГЭ, места.';


-- Соотношение бюджетных и платных мест по направлениям демо-контура.
-- Для официальных мест ИГУ есть отдельное представление programs_admission.
CREATE OR REPLACE VIEW assistant.seats_ratio AS
SELECT
    ac.year         AS campaign_year,
    p.name          AS program_name,
    p.degree,
    p.study_form,
    f.name          AS faculty_name,
    ac.budget_seats,
    ac.paid_seats,
    ac.budget_seats + ac.paid_seats AS total_seats,
    round(100.0 * ac.budget_seats / NULLIF(ac.budget_seats + ac.paid_seats, 0)) AS budget_percent,
    ac.min_score,
    ac.docs_from,
    ac.docs_to,
    ac.data_status
FROM assistant.admission_campaigns ac
JOIN assistant.programs p  ON p.id = ac.program_id
JOIN assistant.faculties f ON f.id = p.faculty_id;

-- ---------------------------------------------------------------------
-- 9. Индексы под новые представления
-- ---------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_schedule_room_slot
    ON assistant.schedule (room_id, weekday, pair_number);
CREATE INDEX IF NOT EXISTS idx_subjects_department
    ON assistant.subjects (department_id);
CREATE INDEX IF NOT EXISTS idx_teachers_department
    ON assistant.teachers (department_id);
CREATE INDEX IF NOT EXISTS idx_applications_submitted
    ON assistant.applications (submitted_at);
CREATE INDEX IF NOT EXISTS idx_grades_attempt
    ON assistant.grades (attempt);
