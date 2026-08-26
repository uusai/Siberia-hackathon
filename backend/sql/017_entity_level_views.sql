-- Представления, у которых строка — это сущность, а не пара сущностей.
--
-- Применять:
--   python backend/scripts/apply_sql.py backend/sql/017_entity_level_views.sql --apply
--
-- ---------------------------------------------------------------------
-- ЗАЧЕМ
-- ---------------------------------------------------------------------
--
-- На вопрос «сколько должников учится на кафедре» ассистент ответил:
-- «на кафедрах в сумме обучается 10760 студентов с долгами» — при том, что
-- студентов в университете 4981. Ответ невозможен арифметически.
--
-- Модель не ошиблась. Её поставили в положение, где ошибиться было проще, чем
-- не ошибиться: academic_debts группируется по s.id И d.name, то есть строка
-- там — это ПАРА «студент × кафедра». 16 364 строки на 4 566 студентов.
-- Отсюда три разных числа на один вопрос про кафедру программной инженерии:
--
--     SUM(debts_count)                 = 488   долги, а не люди
--     COUNT(*) WHERE debts_count > 0   = 400   пары «студент × кафедра»
--     разных студентов с долгами       = 368   <- единственное верное
--
-- Причём 400 — это то, что было записано как эталонный SQL в
-- backend/tests/test_demo_questions.py: тест закреплял неверный запрос.
--
-- Та же ловушка была в subject_performance: «Базы данных» читаются на двух
-- направлениях, поэтому строк две, и на вопрос «какой процент сдал с первой
-- попытки» ассистент честно отвечал «по одним данным 88%, по другим 86,8%».
-- Однозначного ответа в этом представлении просто не существовало.
--
-- Лечится не правилом в промпте, а данными: ниже объекты, где строка — это
-- ровно одна сущность и считать уже нечего.

-- ---------------------------------------------------------------------
-- 1. Задолженности в разрезе СТУДЕНТА
-- ---------------------------------------------------------------------
-- Отличие от academic_debts ровно одно: нет разреза по кафедре, поэтому один
-- студент — одна строка. Для «сколько всего должников», «кто не сдал ни одного
-- экзамена», «у кого больше всего долгов» годится только это представление.

DROP VIEW IF EXISTS assistant.student_debts;

CREATE VIEW assistant.student_debts AS
SELECT
    s.last_name,
    s.first_name,
    s.middle_name,
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
GROUP BY s.id, s.last_name, s.first_name, s.middle_name,
         g.name, g.course, p.name, f.name;

COMMENT ON VIEW assistant.student_debts IS
    'Строка = ОДИН студент. Долги, пересдачи и сданное по всему обучению. '
    'Для подсчёта людей используйте это представление, а не academic_debts.';


-- ---------------------------------------------------------------------
-- 2. Задолженности в разрезе КАФЕДРЫ
-- ---------------------------------------------------------------------
-- Считается поверх academic_debts, где строка — пара «студент × кафедра»:
-- внутри одной кафедры это ровно один студент на строку, поэтому count(*)
-- здесь честно считает людей.
--
-- debtors_count и debts_total намеренно РАЗНЫЕ колонки: первое — сколько
-- человек, второе — сколько задолженностей. Именно их подмена и дала «10760
-- студентов».

DROP VIEW IF EXISTS assistant.department_debts;

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
-- 3. Успеваемость в разрезе ДИСЦИПЛИНЫ
-- ---------------------------------------------------------------------
-- subject_performance разбит по направлениям и семестрам, поэтому у одной
-- дисциплины там несколько строк с разными процентами. Здесь они свёрнуты:
-- доля сдавших с первой попытки считается от общих итогов, а не усредняется
-- по строкам (среднее из процентов дало бы неверный результат при разных
-- размерах групп).

DROP VIEW IF EXISTS assistant.subject_summary;

CREATE VIEW assistant.subject_summary AS
SELECT
    subject_name,
    department_name,
    control_form,
    sum(grades_count)           AS grades_count,
    round(sum(avg_score * grades_count) / nullif(sum(grades_count), 0), 2)
                                AS avg_score,
    sum(excellent_count)        AS excellent_count,
    sum(good_count)             AS good_count,
    sum(satisfactory_count)     AS satisfactory_count,
    sum(failed_count)           AS failed_count,
    sum(first_attempt_total)    AS first_attempt_total,
    sum(first_attempt_passed)   AS first_attempt_passed,
    round(100.0 * sum(first_attempt_passed)
          / nullif(sum(first_attempt_total), 0), 1) AS first_attempt_pass_rate,
    sum(retake_count)           AS retake_count,
    to_tsvector('russian',
        coalesce(subject_name, '') || ' ' || coalesce(department_name, ''))
        AS search_vector
FROM assistant.subject_performance
GROUP BY subject_name, department_name, control_form;

COMMENT ON VIEW assistant.subject_summary IS
    'Строка = ОДНА дисциплина, итоги по всем направлениям и семестрам. '
    'Отвечает на «какой процент сдал предмет» одним числом.';


-- ---------------------------------------------------------------------
-- 4. Подписи к существующим представлениям
-- ---------------------------------------------------------------------
-- Подписи попадают в промпт модели (см. ai_agent.get_db_schema), поэтому
-- разрез представления виден там же, где его колонки. Без этого узнать, что
-- строка — не то, чем кажется, было неоткуда: имя и колонки выглядят
-- одинаково и у «строка = студент», и у «строка = студент × кафедра».

COMMENT ON VIEW assistant.academic_debts IS
    'Строка = пара «студент × кафедра»: студент встречается столько раз, '
    'сколько кафедр ведёт его предметы. Для подсчёта ЛЮДЕЙ берите '
    'student_debts или department_debts, здесь COUNT(*) считает пары.';

COMMENT ON VIEW assistant.student_rankings IS
    'Строка = пара «студент × семестр». Средний балл здесь — за один семестр, '
    'и один человек встречается несколько раз.';

COMMENT ON VIEW assistant.subject_performance IS
    'Строка = дисциплина в разрезе направления и семестра, поэтому у одного '
    'предмета строк несколько. Итог по предмету целиком — subject_summary.';

COMMENT ON VIEW assistant.teacher_semester_load IS
    'Строка = пара «преподаватель × семестр». subjects_count = 0 означает, '
    'что в этом семестре он не ведёт ничего.';

COMMENT ON VIEW assistant.department_performance IS
    'Строка = одна кафедра. diff_from_university — отклонение от среднего '
    'балла по университету.';

COMMENT ON VIEW assistant.department_workload IS
    'Строка = одна кафедра. avg_hours_per_teacher — часы в год на человека.';

COMMENT ON VIEW assistant.group_curriculum IS
    'Строка = дисциплина в учебном плане группы, а не группа.';

COMMENT ON VIEW assistant.funding_share IS
    'Строка = направление в разрезе уровня и формы обучения.';

COMMENT ON VIEW assistant.students_summary IS
    'Строка = ГРУППА студентов с одинаковыми признаками, а не человек. '
    'Число людей = SUM(student_count).';
