-- Происхождение данных: откуда запись, когда проверена, можно ли ей верить.
--
-- Применять:
--   python backend/scripts/apply_sql.py backend/sql/005_provenance.sql --apply
--
-- ЗАЧЕМ. В базе лежат два разных сорта данных, и до сих пор их было не
-- отличить: демонстрационный контур (выдуманные факультеты, группы вида
-- ФИТ-0925-1, сгенерированные студенты и расписание) и официальные сведения
-- об ИГУ, которые добавляются миграцией 006. Бот, который выдаёт выдуманный
-- проходной балл с той же уверенностью, что и официальный, хуже бота,
-- который честно говорит «уточняется» — поэтому статус источника становится
-- частью самих данных, а не устной договорённостью.
--
-- ЧТО ЭТО НЕ ДЕЛАЕТ. Ни одна существующая строка не удаляется и не меняется.
-- Все существующие записи получают data_status = 'demo' значением по
-- умолчанию — это ровно то, чем они и являются.
--
-- БЕЗОПАСНОСТЬ ДЛЯ БОЛЬШИХ ТАБЛИЦ. ADD COLUMN ... DEFAULT в PostgreSQL 11+
-- не переписывает таблицу, значение по умолчанию хранится в метаданных.
-- Поэтому миграция дешёвая даже на schedule. Таблицы grades (96 299 строк)
-- и enrollments (92 653) не трогаем вовсе: происхождение оценки смысла не
-- имеет, а лишний ALTER на них — лишний риск.

-- ---------------------------------------------------------------------
-- 1. Реестр источников
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assistant.data_sources (
    id serial PRIMARY KEY,
    url text UNIQUE NOT NULL,
    title text NOT NULL,
    publisher text,
    -- Когда страницу в последний раз открывали и сверяли. Без этого поля
    -- «официальные» данные через год незаметно превращаются в исторические.
    checked_at timestamptz NOT NULL DEFAULT now(),
    note text
);

COMMENT ON TABLE assistant.data_sources IS
    'Официальные источники сведений об ИГУ: ссылка, название, дата проверки.';

-- ---------------------------------------------------------------------
-- 2. Колонки происхождения на существующих справочниках
-- ---------------------------------------------------------------------
--
-- Словарь статусов:
--   official    — подтверждено официальным источником ИГУ, ссылка обязательна
--   historical  — официальные данные ПРОШЛЫХ лет (проходные баллы, стоимость)
--   demo        — демонстрационные данные хакатонского стенда
--   unverified  — подтвердить не удалось; значение NULL, бот отвечает «уточняется»

ALTER TABLE assistant.faculties
    ADD COLUMN IF NOT EXISTS data_status text NOT NULL DEFAULT 'demo',
    ADD COLUMN IF NOT EXISTS source_id integer REFERENCES assistant.data_sources(id),
    ADD COLUMN IF NOT EXISTS source_url text,
    ADD COLUMN IF NOT EXISTS checked_at timestamptz;

ALTER TABLE assistant.programs
    ADD COLUMN IF NOT EXISTS data_status text NOT NULL DEFAULT 'demo',
    ADD COLUMN IF NOT EXISTS source_id integer REFERENCES assistant.data_sources(id),
    ADD COLUMN IF NOT EXISTS source_url text,
    ADD COLUMN IF NOT EXISTS checked_at timestamptz;

ALTER TABLE assistant.groups
    ADD COLUMN IF NOT EXISTS data_status text NOT NULL DEFAULT 'demo',
    ADD COLUMN IF NOT EXISTS source_id integer REFERENCES assistant.data_sources(id),
    ADD COLUMN IF NOT EXISTS source_url text,
    ADD COLUMN IF NOT EXISTS checked_at timestamptz;

ALTER TABLE assistant.schedule
    ADD COLUMN IF NOT EXISTS data_status text NOT NULL DEFAULT 'demo',
    ADD COLUMN IF NOT EXISTS source_id integer REFERENCES assistant.data_sources(id),
    ADD COLUMN IF NOT EXISTS source_url text,
    ADD COLUMN IF NOT EXISTS checked_at timestamptz;

ALTER TABLE assistant.admission_campaigns
    ADD COLUMN IF NOT EXISTS data_status text NOT NULL DEFAULT 'demo',
    ADD COLUMN IF NOT EXISTS source_id integer REFERENCES assistant.data_sources(id),
    ADD COLUMN IF NOT EXISTS source_url text,
    ADD COLUMN IF NOT EXISTS checked_at timestamptz;

ALTER TABLE assistant.departments
    ADD COLUMN IF NOT EXISTS data_status text NOT NULL DEFAULT 'demo',
    ADD COLUMN IF NOT EXISTS source_id integer REFERENCES assistant.data_sources(id),
    ADD COLUMN IF NOT EXISTS source_url text,
    ADD COLUMN IF NOT EXISTS checked_at timestamptz;

ALTER TABLE assistant.teachers
    ADD COLUMN IF NOT EXISTS data_status text NOT NULL DEFAULT 'demo',
    ADD COLUMN IF NOT EXISTS source_id integer REFERENCES assistant.data_sources(id),
    ADD COLUMN IF NOT EXISTS source_url text,
    ADD COLUMN IF NOT EXISTS checked_at timestamptz;

ALTER TABLE assistant.rooms
    ADD COLUMN IF NOT EXISTS data_status text NOT NULL DEFAULT 'demo',
    ADD COLUMN IF NOT EXISTS source_id integer REFERENCES assistant.data_sources(id),
    ADD COLUMN IF NOT EXISTS source_url text,
    ADD COLUMN IF NOT EXISTS checked_at timestamptz;

ALTER TABLE assistant.subjects
    ADD COLUMN IF NOT EXISTS data_status text NOT NULL DEFAULT 'demo',
    ADD COLUMN IF NOT EXISTS source_id integer REFERENCES assistant.data_sources(id),
    ADD COLUMN IF NOT EXISTS source_url text,
    ADD COLUMN IF NOT EXISTS checked_at timestamptz;

-- ---------------------------------------------------------------------
-- 3. Констрейнты словаря
-- ---------------------------------------------------------------------
-- DROP IF EXISTS + ADD, как в 003_role_access.sql: ALTER TABLE ADD CONSTRAINT
-- IF NOT EXISTS в PostgreSQL нет, а миграция должна переживать повторный
-- запуск. Констрейнт добавляется NOT VALID и валидируется отдельно — так
-- проверка существующих строк не держит блокировку всей таблицы.

ALTER TABLE assistant.faculties DROP CONSTRAINT IF EXISTS faculties_data_status_ck;
ALTER TABLE assistant.faculties ADD CONSTRAINT faculties_data_status_ck
    CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')) NOT VALID;
ALTER TABLE assistant.faculties VALIDATE CONSTRAINT faculties_data_status_ck;

ALTER TABLE assistant.programs DROP CONSTRAINT IF EXISTS programs_data_status_ck;
ALTER TABLE assistant.programs ADD CONSTRAINT programs_data_status_ck
    CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')) NOT VALID;
ALTER TABLE assistant.programs VALIDATE CONSTRAINT programs_data_status_ck;

ALTER TABLE assistant.groups DROP CONSTRAINT IF EXISTS groups_data_status_ck;
ALTER TABLE assistant.groups ADD CONSTRAINT groups_data_status_ck
    CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')) NOT VALID;
ALTER TABLE assistant.groups VALIDATE CONSTRAINT groups_data_status_ck;

ALTER TABLE assistant.schedule DROP CONSTRAINT IF EXISTS schedule_data_status_ck;
ALTER TABLE assistant.schedule ADD CONSTRAINT schedule_data_status_ck
    CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')) NOT VALID;
ALTER TABLE assistant.schedule VALIDATE CONSTRAINT schedule_data_status_ck;

ALTER TABLE assistant.admission_campaigns DROP CONSTRAINT IF EXISTS admission_campaigns_data_status_ck;
ALTER TABLE assistant.admission_campaigns ADD CONSTRAINT admission_campaigns_data_status_ck
    CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')) NOT VALID;
ALTER TABLE assistant.admission_campaigns VALIDATE CONSTRAINT admission_campaigns_data_status_ck;

-- ---------------------------------------------------------------------
-- 4. Индексы под фильтр по статусу
-- ---------------------------------------------------------------------
-- Бот почти всегда спрашивает срез одного статуса («покажи официальные
-- подразделения»), поэтому частичные индексы дешевле полных.

CREATE INDEX IF NOT EXISTS idx_faculties_status ON assistant.faculties (data_status);
CREATE INDEX IF NOT EXISTS idx_programs_status ON assistant.programs (data_status);
CREATE INDEX IF NOT EXISTS idx_groups_status ON assistant.groups (data_status);
CREATE INDEX IF NOT EXISTS idx_schedule_status ON assistant.schedule (data_status);
