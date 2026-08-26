-- Разговорные сокращения факультетов в поиске по аналитике.
--
-- Применять:
--   python backend/scripts/apply_sql.py backend/sql/021_faculty_aliases.sql --apply
--
-- ЗАЧЕМ. Вопрос «5 лучших студентов факультета IT» возвращал пусто. Модель
-- перевела «IT» буквально: WHERE faculty_name ILIKE '%IT%'. А факультет в
-- базе называется «Факультет информационных технологий» — двух латинских
-- букв там нет.
--
-- Это ровно та же болезнь, что была с «физматом» (миграция 019), только в
-- аналитическом контуре: люди называют подразделения сокращённо, а поиск
-- знает лишь полные названия. Лечение то же — словарь сокращений,
-- участвующий ТОЛЬКО в сопоставлении и никогда в ответе.

CREATE TABLE IF NOT EXISTS assistant.faculty_search_aliases (
    id         serial PRIMARY KEY,
    faculty_id integer NOT NULL REFERENCES assistant.faculties(id) ON DELETE CASCADE,
    alias      text NOT NULL,
    UNIQUE (faculty_id, alias)
);

COMMENT ON TABLE assistant.faculty_search_aliases IS
    'Поисковый словарь: как факультеты называют в разговоре (IT, айти, физмат). '
    'Не официальные названия — участвуют только в поиске.';

INSERT INTO assistant.faculty_search_aliases (faculty_id, alias)
SELECT f.id, v.alias
FROM (VALUES
    -- Латинское «it» здесь НЕ ЗАВОДИТСЯ намеренно: разбор русского языка
    -- выбрасывает двухбуквенные латинские токены целиком
    -- (plainto_tsquery('russian','IT') = пустой запрос), поэтому такой
    -- псевдоним не сработал бы никогда. Русские написания работают.
    ('Факультет информационных технологий', 'айти'),
    ('Факультет информационных технологий', 'фит'),
    ('Физико-математический факультет',     'физмат'),
    ('Физико-математический факультет',     'физфак'),
    ('Физико-математический факультет',     'матфак'),
    ('Юридический факультет',               'юрфак'),
    ('Экономический факультет',             'экономфак'),
    ('Экономический факультет',             'эконом'),
    ('Факультет биологии и экологии',       'биофак')
) AS v(faculty_name, alias)
JOIN assistant.faculties f ON f.name = v.faculty_name
ON CONFLICT (faculty_id, alias) DO NOTHING;

-- Служебное представление, как unit_search в миграции 019. В whitelist не
-- входит: им пользуются вьюхи ниже, самой модели он не нужен.
CREATE OR REPLACE VIEW assistant.faculty_search AS
SELECT
    f.id AS faculty_id,
    to_tsvector('russian',
        coalesce(f.name, '') || ' ' ||
        coalesce(string_agg(a.alias, ' '), '')) AS search_vector
FROM assistant.faculties f
LEFT JOIN assistant.faculty_search_aliases a ON a.faculty_id = f.id
GROUP BY f.id, f.name;


-- ---------------------------------------------------------------------
-- Пересборка аналитических представлений с расширенным поиском
-- ---------------------------------------------------------------------
-- Меняется только search_vector: к тексту факультета, направления и группы
-- добавляются сокращения. Колонки и разрез прежние (см. миграцию 020).

DROP VIEW IF EXISTS assistant.student_rankings;

CREATE VIEW assistant.student_rankings AS
SELECT
    g.name AS group_name, g.course, p.name AS program_name, f.name AS faculty_name,
    c.semester,
    count(*)                AS grades_count,
    round(avg(gr.score), 2) AS avg_score,
    fs.search_vector || to_tsvector('russian',
        coalesce(p.name, '') || ' ' || coalesce(g.name, '')) AS search_vector
FROM assistant.students s
JOIN assistant.groups g       ON g.id = s.group_id
JOIN assistant.programs p     ON p.id = g.program_id
JOIN assistant.faculties f    ON f.id = p.faculty_id
JOIN assistant.faculty_search fs ON fs.faculty_id = f.id
JOIN assistant.enrollments e  ON e.student_id = s.id
JOIN assistant.curriculum c   ON c.id = e.curriculum_id
JOIN assistant.grades gr      ON gr.enrollment_id = e.id
GROUP BY s.id, g.name, g.course, p.name, f.name, c.semester, fs.search_vector
HAVING count(gr.score) > 0;

COMMENT ON VIEW assistant.student_rankings IS
    'Строка = пара «студент × семестр», БЕЗ ФИО. Средний балл за один семестр. '
    'Обезличено: назвать конкретного студента по этим полям нельзя. Факультет '
    'ищите через search_vector — он понимает сокращения (IT, физмат, юрфак).';


DROP VIEW IF EXISTS assistant.department_debts;
DROP VIEW IF EXISTS assistant.academic_debts;

CREATE VIEW assistant.academic_debts AS
SELECT
    g.name AS group_name, g.course, p.name AS program_name,
    f.name AS faculty_name, d.name AS department_name,
    count(*) FILTER (WHERE gr.score = 2 OR gr.score IS NULL) AS debts_count,
    count(*) FILTER (WHERE gr.attempt > 1)                   AS retakes_count,
    count(*) FILTER (WHERE gr.score >= 3)                    AS passed_count,
    count(*)                                                 AS grades_total,
    fs.search_vector || to_tsvector('russian',
        coalesce(d.name, '') || ' ' || coalesce(p.name, '') || ' ' ||
        coalesce(g.name, '')) AS search_vector
FROM assistant.students s
JOIN assistant.groups g       ON g.id = s.group_id
JOIN assistant.programs p     ON p.id = g.program_id
JOIN assistant.faculties f    ON f.id = p.faculty_id
JOIN assistant.faculty_search fs ON fs.faculty_id = f.id
JOIN assistant.enrollments e  ON e.student_id = s.id
JOIN assistant.curriculum c   ON c.id = e.curriculum_id
JOIN assistant.subjects sub   ON sub.id = c.subject_id
JOIN assistant.departments d  ON d.id = sub.department_id
JOIN assistant.grades gr      ON gr.enrollment_id = e.id
GROUP BY s.id, g.name, g.course, p.name, f.name, d.name, fs.search_vector;

COMMENT ON VIEW assistant.academic_debts IS
    'Строка = пара «студент × кафедра», БЕЗ ФИО. Для подсчёта ЛЮДЕЙ берите '
    'student_debts или department_debts, здесь COUNT(*) считает пары.';

CREATE VIEW assistant.department_debts AS
SELECT
    department_name, faculty_name,
    count(*)                                AS students_total,
    count(*) FILTER (WHERE debts_count > 0) AS debtors_count,
    sum(debts_count)                        AS debts_total,
    sum(retakes_count)                      AS retakes_total,
    round(100.0 * count(*) FILTER (WHERE debts_count > 0)
          / nullif(count(*), 0), 1)         AS debtors_percent,
    to_tsvector('russian',
        coalesce(department_name, '') || ' ' || coalesce(faculty_name, ''))
        AS search_vector
FROM assistant.academic_debts
GROUP BY department_name, faculty_name;

COMMENT ON VIEW assistant.department_debts IS
    'Строка = ОДНА кафедра. debtors_count — сколько ЧЕЛОВЕК имеют долги, '
    'debts_total — сколько всего задолженностей. Это разные числа.';


DROP VIEW IF EXISTS assistant.student_debts;

CREATE VIEW assistant.student_debts AS
SELECT
    g.name AS group_name, g.course, p.name AS program_name, f.name AS faculty_name,
    count(*) FILTER (WHERE gr.score = 2 OR gr.score IS NULL) AS debts_count,
    count(*) FILTER (WHERE gr.attempt > 1)                   AS retakes_count,
    count(*) FILTER (WHERE gr.score >= 3)                    AS passed_count,
    count(*)                                                 AS grades_total,
    fs.search_vector || to_tsvector('russian',
        coalesce(p.name, '') || ' ' || coalesce(g.name, '')) AS search_vector
FROM assistant.students s
JOIN assistant.groups g       ON g.id = s.group_id
JOIN assistant.programs p     ON p.id = g.program_id
JOIN assistant.faculties f    ON f.id = p.faculty_id
JOIN assistant.faculty_search fs ON fs.faculty_id = f.id
JOIN assistant.enrollments e  ON e.student_id = s.id
JOIN assistant.curriculum c   ON c.id = e.curriculum_id
JOIN assistant.grades gr      ON gr.enrollment_id = e.id
GROUP BY s.id, g.name, g.course, p.name, f.name, fs.search_vector;

COMMENT ON VIEW assistant.student_debts IS
    'Строка = ОДИН студент, БЕЗ ФИО. Для подсчёта людей используйте это '
    'представление, а не academic_debts.';
