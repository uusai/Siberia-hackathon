-- Денормализованные представления приёмной кампании — то, что видит модель.
--
-- Применять:
--   python backend/scripts/apply_sql.py backend/sql/008_admission_views.sql --apply
--   (после 006_official_reference.sql)
--
-- ЗАЧЕМ. Справочник из 006 нормализован: направление, экзамены, места,
-- стоимость и баллы лежат в пяти разных таблицах. Просить языковую модель
-- собрать из них JOIN на каждый вопрос — значит закладываться на то, что она
-- ни разу не ошибётся в связях. Дешевле собрать соединения один раз здесь.
--
-- Сырые таблицы при этом остаются слоем хранения: их правят сидер и
-- импортёр, а не модель.

-- ---------------------------------------------------------------------
-- 1. Направление + подразделение + места + стоимость + экзамены
-- ---------------------------------------------------------------------
-- Закрывает вопросы: «какие направления есть», «сколько бюджетных мест»,
-- «сколько стоит обучение», «что сдавать на <направление>»,
-- «какие специальности на этом факультете».

CREATE OR REPLACE VIEW assistant.programs_admission AS
WITH years AS (
    -- Год берётся из любой таблицы, где о направлении вообще что-то есть:
    -- иначе направление без набора мест выпало бы из выдачи целиком.
    SELECT program_id, admission_year FROM assistant.program_exams
    UNION
    SELECT program_id, admission_year FROM assistant.enrollment_places
    UNION
    SELECT program_id, admission_year FROM assistant.passing_scores
),
places AS (
    SELECT
        program_id,
        admission_year,
        SUM(seats) FILTER (WHERE funding_basis = 'бюджет')        AS budget_seats,
        SUM(seats) FILTER (WHERE funding_basis = 'контракт')      AS paid_seats,
        SUM(seats) FILTER (WHERE quota_kind = 'целевая квота')    AS target_quota_seats,
        SUM(seats) FILTER (WHERE quota_kind = 'особая квота')     AS special_quota_seats,
        SUM(seats) FILTER (WHERE quota_kind = 'отдельная квота')  AS separate_quota_seats
    FROM assistant.enrollment_places
    GROUP BY program_id, admission_year
),
exams AS (
    SELECT
        pe.program_id,
        pe.admission_year,
        string_agg(DISTINCT ee.name, ', ')
            FILTER (WHERE pe.requirement = 'обязательный')  AS exams_required,
        string_agg(DISTINCT ee.name, ' или ')
            FILTER (WHERE pe.requirement = 'по выбору')     AS exams_choice
    FROM assistant.program_exams pe
    JOIN assistant.entrance_exams ee ON ee.id = pe.exam_id
    GROUP BY pe.program_id, pe.admission_year
)
SELECT
    ep.id                       AS program_id,
    uu.official_name            AS unit_name,
    uu.short_name               AS unit_short_name,
    ep.code                     AS program_code,
    ep.name                     AS program_name,
    ep.profile,
    ep.level,
    ep.study_form,
    ep.duration_years,
    y.admission_year,
    pl.budget_seats,
    pl.paid_seats,
    pl.target_quota_seats,
    pl.special_quota_seats,
    pl.separate_quota_seats,
    tf.price_rub                AS tuition_rub,
    tf.academic_year            AS tuition_academic_year,
    ex.exams_required,
    ex.exams_choice,
    ep.page_url,
    COALESCE(ep.source_url, uu.source_url) AS source_url,
    ep.data_status
FROM years y
JOIN assistant.edu_programs ep    ON ep.id = y.program_id
JOIN assistant.university_units uu ON uu.id = ep.unit_id
LEFT JOIN places pl ON pl.program_id = y.program_id AND pl.admission_year = y.admission_year
LEFT JOIN exams ex  ON ex.program_id = y.program_id AND ex.admission_year = y.admission_year
LEFT JOIN assistant.tuition_fees tf
       ON tf.program_id = y.program_id
      AND tf.study_form = ep.study_form
      AND tf.academic_year = y.admission_year || '/' || (y.admission_year + 1);

COMMENT ON VIEW assistant.programs_admission IS
    'Направления ИГУ с местами, стоимостью и вступительными испытаниями по годам.';

-- ---------------------------------------------------------------------
-- 2. Наборы предметов — под вопрос «куда я могу поступить»
-- ---------------------------------------------------------------------
-- Массивы, а не строки: только так работает проверка «мой набор предметов
-- покрывает обязательные для направления»:
--   WHERE required_subjects <@ ARRAY['Русский язык','Математика (профильная)',
--                                    'Информатика и ИКТ']

CREATE OR REPLACE VIEW assistant.program_exam_sets AS
SELECT
    ep.id                   AS program_id,
    ep.code                 AS program_code,
    ep.name                 AS program_name,
    ep.level,
    ep.study_form,
    uu.official_name        AS unit_name,
    pe.admission_year,
    array_agg(DISTINCT ee.name)
        FILTER (WHERE pe.requirement = 'обязательный')  AS required_subjects,
    array_agg(DISTINCT ee.name)
        FILTER (WHERE pe.requirement = 'по выбору')     AS choice_subjects,
    max(pe.source_url)      AS source_url,
    min(pe.data_status)     AS data_status
FROM assistant.program_exams pe
JOIN assistant.edu_programs ep     ON ep.id = pe.program_id
JOIN assistant.university_units uu ON uu.id = ep.unit_id
JOIN assistant.entrance_exams ee   ON ee.id = pe.exam_id
GROUP BY ep.id, ep.code, ep.name, ep.level, ep.study_form,
         uu.official_name, pe.admission_year;

-- ---------------------------------------------------------------------
-- 3. Минимальные баллы — порог допуска, НЕ проходной
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW assistant.minimum_scores_view AS
SELECT
    ms.admission_year,
    ms.level,
    ee.name                 AS subject,
    ms.min_score,
    ep.code                 AS program_code,
    ep.name                 AS program_name,
    CASE WHEN ms.program_id IS NULL
         THEN 'общий порог по университету'
         ELSE 'порог для конкретного направления'
    END                     AS scope,
    ms.source_url,
    ms.data_status
FROM assistant.minimum_scores ms
JOIN assistant.entrance_exams ee ON ee.id = ms.exam_id
LEFT JOIN assistant.edu_programs ep ON ep.id = ms.program_id;

COMMENT ON VIEW assistant.minimum_scores_view IS
    'Минимальный балл — нижний порог допуска к конкурсу. Это НЕ проходной балл.';

-- ---------------------------------------------------------------------
-- 4. Проходные баллы — всегда с годом, формой и основой обучения
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW assistant.passing_scores_view AS
SELECT
    ps.admission_year,
    ep.code                 AS program_code,
    ep.name                 AS program_name,
    ep.level,
    ps.study_form,
    ps.funding_basis,
    ps.competition_group,
    ps.score                AS passing_score,
    uu.official_name        AS unit_name,
    ps.source_url,
    ps.data_status
FROM assistant.passing_scores ps
JOIN assistant.edu_programs ep     ON ep.id = ps.program_id
JOIN assistant.university_units uu ON uu.id = ep.unit_id;

COMMENT ON VIEW assistant.passing_scores_view IS
    'Проходной балл — результат последнего зачисленного в конкретном году. '
    'Не является гарантией поступления в следующем году.';

-- ---------------------------------------------------------------------
-- 5. Сводка по статусам данных
-- ---------------------------------------------------------------------
-- Нужна и на защите («покажите, что вы не выдумываете»), и боту: по ней он
-- может честно сказать, чего в базе пока нет.

CREATE OR REPLACE VIEW assistant.data_status_summary AS
SELECT 'university_units' AS table_name, data_status, count(*) AS rows
FROM assistant.university_units GROUP BY data_status
UNION ALL
SELECT 'edu_programs', data_status, count(*) FROM assistant.edu_programs GROUP BY data_status
UNION ALL
SELECT 'program_exams', data_status, count(*) FROM assistant.program_exams GROUP BY data_status
UNION ALL
SELECT 'minimum_scores', data_status, count(*) FROM assistant.minimum_scores GROUP BY data_status
UNION ALL
SELECT 'passing_scores', data_status, count(*) FROM assistant.passing_scores GROUP BY data_status
UNION ALL
SELECT 'enrollment_places', data_status, count(*) FROM assistant.enrollment_places GROUP BY data_status
UNION ALL
SELECT 'tuition_fees', data_status, count(*) FROM assistant.tuition_fees GROUP BY data_status
UNION ALL
SELECT 'admission_deadlines', data_status, count(*) FROM assistant.admission_deadlines GROUP BY data_status
UNION ALL
SELECT 'admission_documents', data_status, count(*) FROM assistant.admission_documents GROUP BY data_status
UNION ALL
SELECT 'benefits_quotas', data_status, count(*) FROM assistant.benefits_quotas GROUP BY data_status
UNION ALL
SELECT 'dormitories', data_status, count(*) FROM assistant.dormitories GROUP BY data_status
UNION ALL
SELECT 'contacts', data_status, count(*) FROM assistant.contacts GROUP BY data_status
UNION ALL
SELECT 'faq_entries', data_status, count(*) FROM assistant.faq_entries GROUP BY data_status
UNION ALL
SELECT 'faculties', data_status, count(*) FROM assistant.faculties GROUP BY data_status
UNION ALL
SELECT 'groups', data_status, count(*) FROM assistant.groups GROUP BY data_status
UNION ALL
SELECT 'schedule', data_status, count(*) FROM assistant.schedule GROUP BY data_status;
