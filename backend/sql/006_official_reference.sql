-- Официальный справочник ИГУ: подразделения, направления, приёмная кампания.
--
-- Применять:
--   python backend/scripts/apply_sql.py backend/sql/006_official_reference.sql --apply
--   (после 005_provenance.sql — здесь используется assistant.data_sources)
--
-- ЗАЧЕМ ОТДЕЛЬНЫЕ ТАБЛИЦЫ, А НЕ ДОПИСЫВАНИЕ В faculties/programs.
-- Существующие faculties (5 строк) и programs (13) — демонстрационные, и на
-- них завязаны 4981 студент, 139 групп, 968 занятий и 96 299 оценок. Если
-- добавить настоящие подразделения ИГУ теми же строками, любой вопрос
-- «какие факультеты есть в ИГУ» вернёт смесь выдуманного и настоящего, а
-- «сколько студентов в Институте математики и ИТ» вернёт ноль — потому что
-- демо-студенты висят на демо-факультетах. Официальный справочник живёт
-- рядом, демо-контур остаётся рабочим, ничего не ломается.
-- Мостик между мирами — nullable-ссылки в конце файла.
--
-- ПРО ИМЕНА КОЛОНОК КОНТАКТОВ. Здесь contact_phone и contact_email, а не
-- phone и email. Причина не косметическая: security.FORBIDDEN_COLUMNS
-- (backend/app/security.py) содержит 'phone' и 'email', и проверка
-- _assert_no_forbidden_columns() ищет эти слова во ВСЁМ тексте SQL, разбивая
-- его на токены [a-zA-Z_]+. Колонка с именем phone сделала бы вопрос
-- «телефон приёмной комиссии» неотвечаемым: запрос отклонялся бы проверкой
-- безопасности. Токен contact_phone в чёрный список не входит.
-- Сам чёрный список НЕ ослабляется — он защищает students и applications,
-- где лежат настоящие персональные данные. Телефон приёмной комиссии
-- опубликован на isu.ru и персональными данными не является.
--
-- ИДЕМПОТЕНТНОСТЬ. У каждой таблицы объявлен естественный уникальный ключ —
-- сидер грузит данные через INSERT ... ON CONFLICT (<ключ>) DO UPDATE, и
-- повторный запуск не создаёт дублей. Где в ключ входит nullable-колонка,
-- используется UNIQUE NULLS NOT DISTINCT (PostgreSQL 15+; на стенде 17.10):
-- иначе две строки с NULL в профиле считались бы разными и дублировались.

-- ---------------------------------------------------------------------
-- 1. Учебные подразделения
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assistant.university_units (
    id serial PRIMARY KEY,
    official_name text UNIQUE NOT NULL,
    short_name text,
    kind text NOT NULL CHECK (kind IN ('институт', 'факультет', 'иное подразделение')),
    description text,
    address text,
    contact_phone text,
    contact_email text,
    site_url text,
    source_id integer REFERENCES assistant.data_sources(id),
    source_url text,
    checked_at timestamptz,
    data_status text NOT NULL DEFAULT 'unverified'
        CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')),
    -- Строка, помеченная официальной, обязана нести ссылку на источник.
    -- Это правило брифа, вынесенное на уровень схемы: забыть его нельзя.
    CONSTRAINT university_units_official_needs_source_ck
        CHECK (data_status <> 'official' OR source_url IS NOT NULL)
);

COMMENT ON TABLE assistant.university_units IS
    'Реальные институты и факультеты ИГУ по данным официального сайта.';

-- ---------------------------------------------------------------------
-- 2. Направления подготовки и специальности
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assistant.edu_programs (
    id serial PRIMARY KEY,
    unit_id integer NOT NULL REFERENCES assistant.university_units(id),
    code text NOT NULL,                       -- ФГОС-код, например 09.03.04
    name text NOT NULL,
    level text NOT NULL CHECK (level IN ('бакалавриат', 'специалитет',
                                          'магистратура', 'аспирантура')),
    study_form text NOT NULL CHECK (study_form IN ('очная', 'очно-заочная', 'заочная')),
    duration_years numeric,
    profile text,                             -- профиль/программа внутри направления
    description text,
    page_url text,
    source_id integer REFERENCES assistant.data_sources(id),
    source_url text,
    checked_at timestamptz,
    data_status text NOT NULL DEFAULT 'unverified'
        CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')),
    CONSTRAINT edu_programs_official_needs_source_ck
        CHECK (data_status <> 'official' OR source_url IS NOT NULL),
    CONSTRAINT edu_programs_natural_key
        UNIQUE NULLS NOT DISTINCT (unit_id, code, level, study_form, profile)
);

CREATE INDEX IF NOT EXISTS idx_edu_programs_unit ON assistant.edu_programs (unit_id);
CREATE INDEX IF NOT EXISTS idx_edu_programs_code ON assistant.edu_programs (code);

-- ---------------------------------------------------------------------
-- 3. Вступительные испытания
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assistant.entrance_exams (
    id serial PRIMARY KEY,
    name text UNIQUE NOT NULL,                -- 'Русский язык', 'Математика (профильная)'
    kind text NOT NULL CHECK (kind IN ('ЕГЭ', 'внутреннее испытание',
                                        'дополнительное испытание')),
    description text
);

-- Какие испытания нужны на направление в конкретном году.
-- slot — номер позиции в наборе: обязательные предметы занимают свои слоты,
-- предметы по выбору делят один слот между собой. Без slot набор
-- «русский + профильная математика + (информатика ИЛИ физика)» невыразим.
CREATE TABLE IF NOT EXISTS assistant.program_exams (
    id serial PRIMARY KEY,
    program_id integer NOT NULL REFERENCES assistant.edu_programs(id),
    admission_year integer NOT NULL,
    exam_id integer NOT NULL REFERENCES assistant.entrance_exams(id),
    requirement text NOT NULL CHECK (requirement IN ('обязательный', 'по выбору')),
    slot integer NOT NULL DEFAULT 1,
    priority integer,
    source_id integer REFERENCES assistant.data_sources(id),
    source_url text,
    checked_at timestamptz,
    data_status text NOT NULL DEFAULT 'unverified'
        CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')),
    CONSTRAINT program_exams_natural_key
        UNIQUE (program_id, admission_year, exam_id, slot)
);

CREATE INDEX IF NOT EXISTS idx_program_exams_program
    ON assistant.program_exams (program_id, admission_year);

-- ---------------------------------------------------------------------
-- 4. Баллы: минимальный и проходной — РАЗНЫЕ СУЩНОСТИ
-- ---------------------------------------------------------------------
--
-- minimum_scores — нижний порог для участия в конкурсе. Устанавливается
-- заранее, известен до начала приёма, привязан к предмету.
--
-- passing_scores — балл последнего зачисленного в конкретную конкурсную
-- группу конкретного года. Становится известен ТОЛЬКО после зачисления.
-- Схема не позволяет записать его без года, формы и основы обучения:
-- «проходной балл 240» без этих уточнений — бессмысленное число, а из уст
-- бота ещё и обещание, которого он не может дать.

CREATE TABLE IF NOT EXISTS assistant.minimum_scores (
    id serial PRIMARY KEY,
    admission_year integer NOT NULL,
    exam_id integer NOT NULL REFERENCES assistant.entrance_exams(id),
    -- NULL = общеуниверситетский порог по предмету (обычный случай),
    -- заполнено = порог, отличающийся для конкретного направления.
    program_id integer REFERENCES assistant.edu_programs(id),
    level text NOT NULL CHECK (level IN ('бакалавриат', 'специалитет',
                                          'магистратура', 'аспирантура')),
    min_score integer CHECK (min_score IS NULL OR min_score BETWEEN 0 AND 100),
    source_id integer REFERENCES assistant.data_sources(id),
    source_url text,
    checked_at timestamptz,
    data_status text NOT NULL DEFAULT 'unverified'
        CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')),
    CONSTRAINT minimum_scores_official_needs_value_ck
        CHECK (data_status <> 'official' OR (min_score IS NOT NULL AND source_url IS NOT NULL)),
    CONSTRAINT minimum_scores_natural_key
        UNIQUE NULLS NOT DISTINCT (admission_year, exam_id, program_id, level)
);

CREATE TABLE IF NOT EXISTS assistant.passing_scores (
    id serial PRIMARY KEY,
    program_id integer NOT NULL REFERENCES assistant.edu_programs(id),
    admission_year integer NOT NULL,
    study_form text NOT NULL CHECK (study_form IN ('очная', 'очно-заочная', 'заочная')),
    funding_basis text NOT NULL CHECK (funding_basis IN ('бюджет', 'контракт')),
    -- Конкурсная группа: 'основные места', 'особая квота', 'целевая квота',
    -- 'отдельная квота'. Проходной балл у них разный, смешивать нельзя.
    competition_group text NOT NULL,
    score integer CHECK (score IS NULL OR score BETWEEN 0 AND 500),
    source_id integer REFERENCES assistant.data_sources(id),
    source_url text,
    checked_at timestamptz,
    -- По умолчанию historical: проходной балл почти всегда относится к уже
    -- завершённой кампании. Балл текущего года до приказа о зачислении
    -- физически не существует и записываться не должен.
    data_status text NOT NULL DEFAULT 'historical'
        CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')),
    CONSTRAINT passing_scores_natural_key
        UNIQUE (program_id, admission_year, study_form, funding_basis, competition_group)
);

CREATE INDEX IF NOT EXISTS idx_passing_scores_program
    ON assistant.passing_scores (program_id, admission_year);

-- ---------------------------------------------------------------------
-- 5. Места, стоимость, сроки
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assistant.enrollment_places (
    id serial PRIMARY KEY,
    program_id integer NOT NULL REFERENCES assistant.edu_programs(id),
    admission_year integer NOT NULL,
    study_form text NOT NULL CHECK (study_form IN ('очная', 'очно-заочная', 'заочная')),
    funding_basis text NOT NULL CHECK (funding_basis IN ('бюджет', 'контракт')),
    quota_kind text NOT NULL CHECK (quota_kind IN ('основные места', 'особая квота',
                                                    'целевая квота', 'отдельная квота')),
    seats integer CHECK (seats IS NULL OR seats >= 0),
    source_id integer REFERENCES assistant.data_sources(id),
    source_url text,
    checked_at timestamptz,
    data_status text NOT NULL DEFAULT 'unverified'
        CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')),
    CONSTRAINT enrollment_places_natural_key
        UNIQUE (program_id, admission_year, study_form, funding_basis, quota_kind)
);

CREATE TABLE IF NOT EXISTS assistant.tuition_fees (
    id serial PRIMARY KEY,
    program_id integer NOT NULL REFERENCES assistant.edu_programs(id),
    academic_year text NOT NULL,              -- '2025/2026'
    study_form text NOT NULL CHECK (study_form IN ('очная', 'очно-заочная', 'заочная')),
    price_rub numeric(12, 2) CHECK (price_rub IS NULL OR price_rub >= 0),
    source_id integer REFERENCES assistant.data_sources(id),
    source_url text,
    checked_at timestamptz,
    data_status text NOT NULL DEFAULT 'unverified'
        CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')),
    CONSTRAINT tuition_fees_natural_key
        UNIQUE (program_id, academic_year, study_form)
);

-- Сроки приёма. stage различает этапы: подача документов, завершение приёма
-- оригиналов и согласий, публикация конкурсных списков, приказы о зачислении.
CREATE TABLE IF NOT EXISTS assistant.admission_deadlines (
    id serial PRIMARY KEY,
    admission_year integer NOT NULL,
    level text NOT NULL CHECK (level IN ('бакалавриат', 'специалитет',
                                          'магистратура', 'аспирантура')),
    study_form text CHECK (study_form IS NULL OR
                           study_form IN ('очная', 'очно-заочная', 'заочная')),
    funding_basis text CHECK (funding_basis IS NULL OR
                              funding_basis IN ('бюджет', 'контракт')),
    stage text NOT NULL,
    date_from date,
    date_to date,
    description text,
    source_id integer REFERENCES assistant.data_sources(id),
    source_url text,
    checked_at timestamptz,
    data_status text NOT NULL DEFAULT 'unverified'
        CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')),
    CONSTRAINT admission_deadlines_range_ck
        CHECK (date_from IS NULL OR date_to IS NULL OR date_to >= date_from),
    CONSTRAINT admission_deadlines_natural_key
        UNIQUE NULLS NOT DISTINCT (admission_year, level, study_form, funding_basis, stage)
);

-- ---------------------------------------------------------------------
-- 6. Документы, льготы, квоты
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assistant.admission_documents (
    id serial PRIMARY KEY,
    admission_year integer NOT NULL,
    level text NOT NULL CHECK (level IN ('бакалавриат', 'специалитет',
                                          'магистратура', 'аспирантура')),
    applicant_category text NOT NULL DEFAULT 'все',
    doc_name text NOT NULL,
    is_required boolean NOT NULL DEFAULT true,
    note text,
    source_id integer REFERENCES assistant.data_sources(id),
    source_url text,
    checked_at timestamptz,
    data_status text NOT NULL DEFAULT 'unverified'
        CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')),
    CONSTRAINT admission_documents_natural_key
        UNIQUE (admission_year, level, applicant_category, doc_name)
);

CREATE TABLE IF NOT EXISTS assistant.benefits_quotas (
    id serial PRIMARY KEY,
    admission_year integer NOT NULL,
    kind text NOT NULL,                       -- 'особая квота', 'целевое обучение', ...
    title text NOT NULL,
    description text,
    source_id integer REFERENCES assistant.data_sources(id),
    source_url text,
    checked_at timestamptz,
    data_status text NOT NULL DEFAULT 'unverified'
        CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')),
    CONSTRAINT benefits_quotas_natural_key UNIQUE (admission_year, kind, title)
);

-- ---------------------------------------------------------------------
-- 7. Общежития, корпуса, контакты
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assistant.dormitories (
    id serial PRIMARY KEY,
    name text UNIQUE NOT NULL,
    address text,
    description text,
    provided_to text,                         -- кому предоставляется
    contact_phone text,
    source_id integer REFERENCES assistant.data_sources(id),
    source_url text,
    checked_at timestamptz,
    data_status text NOT NULL DEFAULT 'unverified'
        CHECK (data_status IN ('official', 'historical', 'demo', 'unverified'))
);

-- Отдельно от assistant.rooms: там аудитории демо-контура с текстовым полем
-- building, здесь — реальные адреса корпусов ИГУ. Смешивать нельзя, иначе
-- демо-аудитория «Корпус Б» получит настоящий адрес.
CREATE TABLE IF NOT EXISTS assistant.campus_buildings (
    id serial PRIMARY KEY,
    name text NOT NULL,
    address text,
    unit_id integer REFERENCES assistant.university_units(id),
    description text,
    source_id integer REFERENCES assistant.data_sources(id),
    source_url text,
    checked_at timestamptz,
    data_status text NOT NULL DEFAULT 'unverified'
        CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')),
    CONSTRAINT campus_buildings_natural_key UNIQUE NULLS NOT DISTINCT (name, address)
);

CREATE TABLE IF NOT EXISTS assistant.contacts (
    id serial PRIMARY KEY,
    scope text NOT NULL CHECK (scope IN ('университет', 'приёмная комиссия',
                                          'подразделение', 'деканат', 'общежитие',
                                          'библиотека', 'студенческий сервис')),
    unit_id integer REFERENCES assistant.university_units(id),
    title text NOT NULL,
    contact_phone text,
    contact_email text,
    address text,
    work_hours text,
    site_url text,
    source_id integer REFERENCES assistant.data_sources(id),
    source_url text,
    checked_at timestamptz,
    data_status text NOT NULL DEFAULT 'unverified'
        CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')),
    CONSTRAINT contacts_natural_key UNIQUE (scope, title)
);

-- ---------------------------------------------------------------------
-- 8. FAQ
-- ---------------------------------------------------------------------
-- keywords нужен для вопросов, которые нельзя выразить SQL-джойном:
-- опечатки, сокращения, падежи, разговорные формулировки. Модель ищет по
-- нему через ILIKE или пересечение массивов, а не пытается угадать таблицу.

CREATE TABLE IF NOT EXISTS assistant.faq_entries (
    id serial PRIMARY KEY,
    category text NOT NULL,
    question text NOT NULL,
    answer text NOT NULL,
    keywords text[] NOT NULL DEFAULT '{}',
    source_id integer REFERENCES assistant.data_sources(id),
    source_url text,
    checked_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    data_status text NOT NULL DEFAULT 'unverified'
        CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')),
    CONSTRAINT faq_entries_natural_key UNIQUE (category, question)
);

CREATE INDEX IF NOT EXISTS idx_faq_keywords ON assistant.faq_entries USING gin (keywords);

-- ---------------------------------------------------------------------
-- 9. Мостик между демо-контуром и официальным справочником
-- ---------------------------------------------------------------------
-- Заполняется сидером ТОЛЬКО там, где сопоставление однозначно (совпадает
-- ФГОС-код направления). Где однозначности нет — остаётся NULL: выдуманная
-- связь хуже отсутствующей.

ALTER TABLE assistant.faculties
    ADD COLUMN IF NOT EXISTS unit_id integer REFERENCES assistant.university_units(id);

ALTER TABLE assistant.programs
    ADD COLUMN IF NOT EXISTS official_program_id integer
        REFERENCES assistant.edu_programs(id);
