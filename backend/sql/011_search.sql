-- Поиск по названиям с учётом русской морфологии + короткие имена предметов.
--
-- Применять:
--   python backend/scripts/apply_sql.py backend/sql/011_search.sql --apply
--
-- ЗАЧЕМ. Живая проверка через модель показала два одинаковых по природе
-- промаха, и оба — про то, как люди пишут, а не про то, как устроена база.
--
-- 1. ПАДЕЖИ. Спрашивают «сколько стоит обучение на программной инженерии»,
--    а в базе лежит «Программная инженерия». Модель честно строит
--    ILIKE '%программной инженерии%' и получает ноль строк. Никакая
--    инструкция «подставляй корень» это надёжно не чинит: где обрезать
--    слово, модель угадывает через раз.
--    Решение — полнотекстовый поиск со словарём 'russian': он приводит
--    словоформы к основе сам, на стороне СУБД.
--
-- 2. ДЛИННЫЕ ИМЕНА ПРЕДМЕТОВ. Пользователь говорит «сдаю русский,
--    математику и информатику», а в справочнике «Математика (профильный
--    уровень)». Проверка вхождения массива
--    required_subjects <@ ARRAY['Русский язык','Математика','Информатика']
--    не срабатывает из-за одного уточнения в скобках.
--    Решение — короткое имя предмета рядом с полным.

-- ---------------------------------------------------------------------
-- 1. Поисковый вектор направления
-- ---------------------------------------------------------------------
-- Генерируемая колонка: пересчитывается сама при любом изменении названия,
-- рассинхронизироваться с данными не может.

ALTER TABLE assistant.edu_programs
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('russian',
                    coalesce(name, '') || ' ' || coalesce(profile, '') || ' ' ||
                    coalesce(code, ''))
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_edu_programs_search
    ON assistant.edu_programs USING gin (search_vector);

-- ---------------------------------------------------------------------
-- 2. Короткие имена предметов
-- ---------------------------------------------------------------------

ALTER TABLE assistant.entrance_exams
    ADD COLUMN IF NOT EXISTS short_name text;

UPDATE assistant.entrance_exams SET short_name = CASE name
    WHEN 'Русский язык'                    THEN 'русский'
    WHEN 'Математика (профильный уровень)' THEN 'математика'
    WHEN 'Информатика'                     THEN 'информатика'
    WHEN 'Физика'                          THEN 'физика'
    WHEN 'Химия'                           THEN 'химия'
    WHEN 'Биология'                        THEN 'биология'
    WHEN 'География'                        THEN 'география'
    WHEN 'История'                         THEN 'история'
    WHEN 'Обществознание'                  THEN 'обществознание'
    WHEN 'Литература'                      THEN 'литература'
    WHEN 'Иностранный язык'                THEN 'иностранный язык'
    ELSE lower(name)
END
WHERE short_name IS DISTINCT FROM CASE name
    WHEN 'Русский язык'                    THEN 'русский'
    WHEN 'Математика (профильный уровень)' THEN 'математика'
    WHEN 'Информатика'                     THEN 'информатика'
    WHEN 'Физика'                          THEN 'физика'
    WHEN 'Химия'                           THEN 'химия'
    WHEN 'Биология'                        THEN 'биология'
    WHEN 'География'                        THEN 'география'
    WHEN 'История'                         THEN 'история'
    WHEN 'Обществознание'                  THEN 'обществознание'
    WHEN 'Литература'                      THEN 'литература'
    WHEN 'Иностранный язык'                THEN 'иностранный язык'
    ELSE lower(name)
END;

-- ---------------------------------------------------------------------
-- 3. Представления с поиском
-- ---------------------------------------------------------------------

-- CREATE OR REPLACE VIEW не умеет вставлять колонки в СЕРЕДИНУ списка, а
-- новые search_vector и *_short встают между существующими. Пересоздаём.
-- Это представления, а не таблицы: данных в них нет, терять нечего.
DROP VIEW IF EXISTS assistant.program_exam_sets;
CREATE VIEW assistant.program_exam_sets AS
SELECT
    ep.id                   AS program_id,
    ep.code                 AS program_code,
    ep.name                 AS program_name,
    ep.profile,
    ep.level,
    ep.study_form,
    uu.official_name        AS unit_name,
    pe.admission_year,
    array_agg(DISTINCT ee.name)
        FILTER (WHERE pe.requirement = 'обязательный')  AS required_subjects,
    array_agg(DISTINCT ee.name)
        FILTER (WHERE pe.requirement = 'по выбору')     AS choice_subjects,
    -- Короткие имена в нижнем регистре — под проверку вхождения массива
    -- с тем, что назвал пользователь.
    array_agg(DISTINCT ee.short_name)
        FILTER (WHERE pe.requirement = 'обязательный')  AS required_short,
    array_agg(DISTINCT ee.short_name)
        FILTER (WHERE pe.requirement = 'по выбору')     AS choice_short,
    ep.search_vector,
    max(pe.source_url)      AS source_url,
    min(pe.data_status)     AS data_status
FROM assistant.program_exams pe
JOIN assistant.edu_programs ep     ON ep.id = pe.program_id
JOIN assistant.university_units uu ON uu.id = ep.unit_id
JOIN assistant.entrance_exams ee   ON ee.id = pe.exam_id
GROUP BY ep.id, ep.code, ep.name, ep.profile, ep.level, ep.study_form,
         uu.official_name, pe.admission_year, ep.search_vector;

COMMENT ON VIEW assistant.program_exam_sets IS
    'Наборы вступительных испытаний по направлениям. required_short и '
    'choice_short — короткие имена предметов для подбора по набору ЕГЭ.';


-- programs_admission пересоздаётся целиком: добавляем profile и
-- search_vector, остальное без изменений.
DROP VIEW IF EXISTS assistant.programs_admission;
CREATE VIEW assistant.programs_admission AS
WITH years AS (
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
    ep.search_vector,
    ep.page_url,
    COALESCE(ep.source_url, uu.source_url) AS source_url,
    ep.data_status
FROM years y
JOIN assistant.edu_programs ep    ON ep.id = y.program_id
JOIN assistant.university_units uu ON uu.id = ep.unit_id
LEFT JOIN places pl ON pl.program_id = y.program_id AND pl.admission_year = y.admission_year
LEFT JOIN exams ex  ON ex.program_id = y.program_id AND ex.admission_year = y.admission_year
-- Стоимость берём ПОСЛЕДНЮЮ известную, а не строго за год строки.
-- Раньше join шёл по academic_year = admission_year + 1, и в строках за
-- 2024-2025 (они приходят из проходных баллов) цена оказывалась пустой —
-- на вопрос «сколько стоит обучение» бот отвечал пустотой, если модель не
-- догадалась дофильтровать год. За какой именно год цена, видно в
-- tuition_academic_year.
LEFT JOIN LATERAL (
    SELECT price_rub, academic_year
    FROM assistant.tuition_fees tf
    WHERE tf.program_id = y.program_id AND tf.study_form = ep.study_form
    ORDER BY tf.academic_year DESC
    LIMIT 1
) tf ON true;


DROP VIEW IF EXISTS assistant.passing_scores_view;
CREATE VIEW assistant.passing_scores_view AS
SELECT
    ps.admission_year,
    ep.code                 AS program_code,
    ep.name                 AS program_name,
    ep.profile,
    ep.level,
    ps.study_form,
    ps.funding_basis,
    ps.competition_group,
    ps.score                AS passing_score,
    uu.official_name        AS unit_name,
    ep.search_vector,
    ps.source_url,
    ps.data_status
FROM assistant.passing_scores ps
JOIN assistant.edu_programs ep     ON ep.id = ps.program_id
JOIN assistant.university_units uu ON uu.id = ep.unit_id;
