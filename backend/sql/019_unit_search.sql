-- Поиск направлений по названию подразделения и по разговорным сокращениям.
--
-- Применять:
--   python backend/scripts/apply_sql.py backend/sql/019_unit_search.sql --apply
--
-- ---------------------------------------------------------------------
-- ЗАЧЕМ
-- ---------------------------------------------------------------------
--
-- Вопрос «на физмат» ассистент не понимал. Первая догадка — не хватает прав
-- роли guest — оказалась неверной: тот же вопрос под администрацией, у
-- которой доступны все 57 объектов, тоже возвращал пусто. И не только
-- «физмат», но и полное «физический факультет».
--
-- Причина в том, что ПОДРАЗДЕЛЕНИЕ НЕ УЧАСТВОВАЛО В ПОИСКЕ НИГДЕ.
-- edu_programs.search_vector — генерируемая колонка, собранная из name,
-- profile и code. Названия факультета или института в ней нет, а
-- programs_admission, program_exam_sets и passing_scores_view отдавали
-- ep.search_vector как есть. При том, что unit_name в этих представлениях
-- ЕСТЬ и показывается в ответе: увидеть подразделение можно, найти по нему —
-- нельзя. У Физического факультета семь направлений, и поиск по его
-- названию возвращал ноль.
--
-- Вторая половина задачи — сокращения. Абитуриенты пишут «физмат», «юрфак»,
-- «истфак», а не «Институт математики и информационных технологий». Ни в
-- одном официальном документе таких слов нет и быть не может.
--
-- ---------------------------------------------------------------------
-- 1. Словарь разговорных сокращений
-- ---------------------------------------------------------------------
--
-- ВАЖНО ПРО ПРИРОДУ ЭТИХ ДАННЫХ. Это НЕ сведения об университете и не
-- официальные названия. Это поисковый словарь: как люди называют
-- подразделения в разговоре. Он участвует ТОЛЬКО в сопоставлении и никогда
-- не попадает в ответ — в ответе всегда официальное название из
-- university_units. Поэтому колонки data_status здесь нет: помечать
-- происхождение нечему, это не факт о вузе.
--
-- «Физмат» намеренно ведёт на ДВА подразделения — физический факультет и
-- институт математики. Слово разговорное и неоднозначное; честнее показать
-- оба варианта, чем молча выбрать один и выдать догадку за факт.

CREATE TABLE IF NOT EXISTS assistant.unit_search_aliases (
    id      serial PRIMARY KEY,
    unit_id integer NOT NULL REFERENCES assistant.university_units(id) ON DELETE CASCADE,
    alias   text NOT NULL,
    UNIQUE (unit_id, alias)
);

COMMENT ON TABLE assistant.unit_search_aliases IS
    'Поисковый словарь: разговорные сокращения подразделений (физмат, юрфак). '
    'Не официальные названия и не данные об университете — участвуют только '
    'в поиске, в ответах никогда не показываются.';

INSERT INTO assistant.unit_search_aliases (unit_id, alias)
SELECT u.id, v.alias
FROM (VALUES
    ('Физический факультет',                'физфак'),
    ('Физический факультет',                'физмат'),
    ('Институт математики и информационных технологий', 'имит'),
    ('Институт математики и информационных технологий', 'матфак'),
    ('Институт математики и информационных технологий', 'физмат'),
    ('Юридический институт',                'юрфак'),
    ('Юридический институт',                'юридический факультет'),
    ('Исторический факультет',              'истфак'),
    ('Химический факультет',                'химфак'),
    ('Географический факультет',            'геофак'),
    ('Геологический факультет',             'геолфак'),
    ('Институт биологических наук',         'биофак'),
    ('Институт биологических наук',         'ибн'),
    ('Институт социальных наук',            'исн'),
    ('Институт филологии, иностранных языков и медиакоммуникации', 'ифиям'),
    ('Институт филологии, иностранных языков и медиакоммуникации', 'филфак'),
    ('Институт филологии, иностранных языков и медиакоммуникации', 'иняз'),
    ('Международный институт экономики и лингвистики', 'миэл'),
    ('Байкальская международная бизнес-школа (институт)', 'бмбш'),
    ('Педагогический институт',             'педфак'),
    ('Педагогический институт',             'пединститут'),
    ('Факультет психологии',                'психфак'),
    ('Факультет бизнес-коммуникаций и информатики', 'фбки')
) AS v(official_name, alias)
JOIN assistant.university_units u ON u.official_name = v.official_name
ON CONFLICT (unit_id, alias) DO NOTHING;


-- ---------------------------------------------------------------------
-- 2. Поисковый вектор подразделения
-- ---------------------------------------------------------------------
-- Служебное представление: собирает в один tsvector официальное название,
-- краткое имя и все сокращения. В whitelist модели НЕ входит — им
-- пользуются представления ниже, а самой модели он не нужен.

CREATE OR REPLACE VIEW assistant.unit_search AS
SELECT
    u.id AS unit_id,
    to_tsvector('russian',
        coalesce(u.official_name, '') || ' ' ||
        coalesce(u.short_name, '') || ' ' ||
        coalesce(string_agg(a.alias, ' '), '')) AS search_vector
FROM assistant.university_units u
LEFT JOIN assistant.unit_search_aliases a ON a.unit_id = u.id
GROUP BY u.id, u.official_name, u.short_name;


-- ---------------------------------------------------------------------
-- 3. Представления приёма: подразделение теперь ищется
-- ---------------------------------------------------------------------
-- Меняется ровно одно: search_vector = вектор направления ПЛЮС вектор его
-- подразделения. Остальные колонки прежние, кроме двух удалённых — см. ниже.

DROP VIEW IF EXISTS assistant.programs_admission;

CREATE VIEW assistant.programs_admission AS
WITH years AS (
    SELECT program_id, admission_year FROM assistant.program_exams
    UNION
    SELECT program_id, admission_year FROM assistant.enrollment_places
    UNION
    SELECT program_id, admission_year FROM assistant.passing_scores
), places AS (
    SELECT
        program_id,
        admission_year,
        sum(seats) FILTER (WHERE funding_basis = 'бюджет')          AS budget_seats,
        sum(seats) FILTER (WHERE funding_basis = 'контракт')        AS paid_seats,
        sum(seats) FILTER (WHERE quota_kind = 'целевая квота')      AS target_quota_seats,
        sum(seats) FILTER (WHERE quota_kind = 'особая квота')       AS special_quota_seats,
        sum(seats) FILTER (WHERE quota_kind = 'отдельная квота')    AS separate_quota_seats
    FROM assistant.enrollment_places
    GROUP BY program_id, admission_year
), exams AS (
    SELECT
        pe.program_id,
        pe.admission_year,
        string_agg(DISTINCT ee.name, ', ') FILTER (WHERE pe.requirement = 'обязательный') AS exams_required,
        string_agg(DISTINCT ee.name, ' или ') FILTER (WHERE pe.requirement = 'по выбору') AS exams_choice
    FROM assistant.program_exams pe
    JOIN assistant.entrance_exams ee ON ee.id = pe.exam_id
    GROUP BY pe.program_id, pe.admission_year
)
SELECT
    ep.id           AS program_id,
    uu.official_name AS unit_name,
    ep.code         AS program_code,
    ep.name         AS program_name,
    ep.profile,
    ep.level,
    ep.study_form,
    y.admission_year,
    pl.budget_seats,
    pl.paid_seats,
    pl.target_quota_seats,
    pl.special_quota_seats,
    pl.separate_quota_seats,
    tf.price_rub    AS tuition_rub,
    tf.academic_year AS tuition_academic_year,
    ex.exams_required,
    ex.exams_choice,
    ep.search_vector || us.search_vector AS search_vector,
    ep.page_url,
    coalesce(ep.source_url, uu.source_url) AS source_url,
    ep.data_status
FROM years y
JOIN assistant.edu_programs ep      ON ep.id = y.program_id
JOIN assistant.university_units uu  ON uu.id = ep.unit_id
JOIN assistant.unit_search us       ON us.unit_id = ep.unit_id
LEFT JOIN places pl ON pl.program_id = y.program_id AND pl.admission_year = y.admission_year
LEFT JOIN exams ex  ON ex.program_id = y.program_id AND ex.admission_year = y.admission_year
LEFT JOIN LATERAL (
    SELECT tf_1.price_rub, tf_1.academic_year
    FROM assistant.tuition_fees tf_1
    WHERE tf_1.program_id = y.program_id AND tf_1.study_form = ep.study_form
    ORDER BY tf_1.academic_year DESC
    LIMIT 1
) tf ON true;

-- Из состава убраны unit_short_name и duration_years: обе пусты у ВСЕХ строк
-- (university_units.short_name — 0 из 15, edu_programs.duration_years — 0 из
-- 113). Колонка, которая может вернуть только NULL, не даёт ответа, но
-- приглашает модель её выбрать — и вопрос «сколько лет учиться» уходил в
-- пустоту вместо честного «таких сведений нет». Появятся значения — вернём
-- колонки вместе с ними.

COMMENT ON VIEW assistant.programs_admission IS
    'Строка = направление в разрезе года приёма. Поиск по search_vector '
    'находит и по названию направления, и по названию подразделения, и по '
    'разговорному сокращению («физмат», «юрфак»).';


DROP VIEW IF EXISTS assistant.program_exam_sets;

CREATE VIEW assistant.program_exam_sets AS
SELECT
    ep.id           AS program_id,
    ep.code         AS program_code,
    ep.name         AS program_name,
    ep.profile,
    ep.level,
    ep.study_form,
    uu.official_name AS unit_name,
    pe.admission_year,
    array_agg(DISTINCT ee.name) FILTER (WHERE pe.requirement = 'обязательный') AS required_subjects,
    array_agg(DISTINCT ee.name) FILTER (WHERE pe.requirement = 'по выбору')    AS choice_subjects,
    array_agg(DISTINCT ee.short_name) FILTER (WHERE pe.requirement = 'обязательный') AS required_short,
    array_agg(DISTINCT ee.short_name) FILTER (WHERE pe.requirement = 'по выбору')    AS choice_short,
    ep.search_vector || us.search_vector AS search_vector,
    max(pe.source_url) AS source_url,
    min(pe.data_status) AS data_status
FROM assistant.program_exams pe
JOIN assistant.edu_programs ep     ON ep.id = pe.program_id
JOIN assistant.university_units uu ON uu.id = ep.unit_id
JOIN assistant.unit_search us      ON us.unit_id = ep.unit_id
JOIN assistant.entrance_exams ee   ON ee.id = pe.exam_id
GROUP BY ep.id, ep.code, ep.name, ep.profile, ep.level, ep.study_form,
         uu.official_name, pe.admission_year, ep.search_vector, us.search_vector;

COMMENT ON VIEW assistant.program_exam_sets IS
    'Строка = набор вступительных испытаний направления в конкретном году. '
    'required_short — короткие имена предметов для подбора по набору ЕГЭ.';


DROP VIEW IF EXISTS assistant.passing_scores_view;

CREATE VIEW assistant.passing_scores_view AS
SELECT
    ps.admission_year,
    ep.code         AS program_code,
    ep.name         AS program_name,
    ep.profile,
    ep.level,
    ps.study_form,
    ps.funding_basis,
    ps.competition_group,
    ps.score        AS passing_score,
    uu.official_name AS unit_name,
    ep.search_vector || us.search_vector AS search_vector,
    ps.source_url,
    ps.data_status
FROM assistant.passing_scores ps
JOIN assistant.edu_programs ep     ON ep.id = ps.program_id
JOIN assistant.university_units uu ON uu.id = ep.unit_id
JOIN assistant.unit_search us      ON us.unit_id = ep.unit_id;

COMMENT ON VIEW assistant.passing_scores_view IS
    'Строка = проходной балл направления в конкретном году, форме обучения и '
    'основе. Без этих трёх признаков число бессмысленно.';
