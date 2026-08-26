-- Расписание с датами, временем и типом занятия + детекторы конфликтов.
--
-- Применять:
--   python backend/scripts/apply_sql.py backend/sql/007_schedule_time.sql --apply
--
-- ЗАЧЕМ. assistant.schedule хранит только weekday + pair_number + week_type.
-- Этого хватает на «расписание моей группы», но не хватает ни на «что у меня
-- завтра», ни на «какая следующая пара», ни на «во сколько начало», ни на
-- «это лекция или лаба» — а это половина студенческих вопросов. Существующая
-- таблица не переписывается: даты появляются надстройкой сверху.
--
-- ПОЧЕМУ lesson_occurrences — ТАБЛИЦА, А НЕ MATERIALIZED VIEW.
-- Две независимые причины.
-- 1. ai_agent.get_db_schema() читает information_schema.columns, куда
--    materialized view НЕ попадает. Модель не увидела бы колонок и не смогла
--    бы построить запрос. Обычная таблица и обычная вьюха видны обе.
-- 2. db.py выставляет statement_timeout = 5000 мс. Разворачивать 968 строк
--    расписания через generate_series по датам семестра на КАЖДЫЙ вопрос —
--    десятки тысяч строк на ровном месте. Предрассчитанная таблица с
--    индексом по дате отвечает мгновенно.
--
-- Наполняет её backend/scripts/seed_lesson_occurrences.py (идемпотентно).

-- ---------------------------------------------------------------------
-- 1. Расписание звонков
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assistant.pair_times (
    pair_number integer PRIMARY KEY,
    starts_at time NOT NULL,
    ends_at time NOT NULL,
    data_status text NOT NULL DEFAULT 'demo'
        CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')),
    source_url text,
    CONSTRAINT pair_times_range_ck CHECK (ends_at > starts_at)
);

COMMENT ON TABLE assistant.pair_times IS
    'Время начала и окончания пары по её номеру.';

-- ---------------------------------------------------------------------
-- 2. Учебные семестры
-- ---------------------------------------------------------------------
-- first_week_parity нужен, чтобы связать чётность недели с календарём:
-- сама по себе «чётная неделя» — это не чётный номер недели в году, а
-- чётный номер недели ОТ НАЧАЛА СЕМЕСТРА, и стартовать он может с любой.

CREATE TABLE IF NOT EXISTS assistant.academic_terms (
    id serial PRIMARY KEY,
    academic_year text NOT NULL,              -- '2025/2026'
    name text NOT NULL CHECK (name IN ('осенний', 'весенний')),
    starts_on date NOT NULL,
    ends_on date NOT NULL,
    first_week_parity text NOT NULL CHECK (first_week_parity IN ('чётная', 'нечётная')),
    data_status text NOT NULL DEFAULT 'demo'
        CHECK (data_status IN ('official', 'historical', 'demo', 'unverified')),
    source_url text,
    CONSTRAINT academic_terms_range_ck CHECK (ends_on > starts_on),
    CONSTRAINT academic_terms_natural_key UNIQUE (academic_year, name)
);

-- ---------------------------------------------------------------------
-- 3. Тип занятия и семестр на существующем расписании
-- ---------------------------------------------------------------------

ALTER TABLE assistant.schedule
    ADD COLUMN IF NOT EXISTS lesson_type text,
    ADD COLUMN IF NOT EXISTS term_id integer REFERENCES assistant.academic_terms(id);

ALTER TABLE assistant.schedule DROP CONSTRAINT IF EXISTS schedule_lesson_type_ck;
ALTER TABLE assistant.schedule ADD CONSTRAINT schedule_lesson_type_ck
    CHECK (lesson_type IS NULL OR lesson_type IN ('лекция', 'практика',
                                                   'лабораторная', 'семинар'))
    NOT VALID;
ALTER TABLE assistant.schedule VALIDATE CONSTRAINT schedule_lesson_type_ck;

-- ---------------------------------------------------------------------
-- 4. Конкретные проведения занятий по датам
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assistant.lesson_occurrences (
    id bigserial PRIMARY KEY,
    schedule_id integer NOT NULL REFERENCES assistant.schedule(id),
    term_id integer NOT NULL REFERENCES assistant.academic_terms(id),
    lesson_date date NOT NULL,
    -- Номер учебной недели от начала семестра, с 1. Хранится, а не считается
    -- на лету: по нему проверяется, что чётность недели разложена верно.
    week_no integer NOT NULL CHECK (week_no >= 1),
    CONSTRAINT lesson_occurrences_natural_key UNIQUE (schedule_id, lesson_date)
);

CREATE INDEX IF NOT EXISTS idx_lesson_occ_date
    ON assistant.lesson_occurrences (lesson_date);
CREATE INDEX IF NOT EXISTS idx_lesson_occ_schedule
    ON assistant.lesson_occurrences (schedule_id);

-- ---------------------------------------------------------------------
-- 5. Готовое расписание с датами — то, что видит модель
-- ---------------------------------------------------------------------
-- Одна вьюха вместо шести JOIN'ов, которые модель собирала бы сама.
-- Относительные даты считаются в часовом поясе Иркутска:
--   WHERE lesson_date = (now() AT TIME ZONE 'Asia/Irkutsk')::date

CREATE OR REPLACE VIEW assistant.schedule_calendar AS
SELECT
    lo.lesson_date,
    lo.week_no,
    sc.weekday,
    sc.pair_number,
    pt.starts_at,
    pt.ends_at,
    sc.week_type,
    sc.lesson_type,
    sub.name        AS subject_name,
    t.full_name     AS teacher_name,
    g.name          AS group_name,
    g.course,
    p.name          AS program_name,
    r.building,
    r.number        AS room_number,
    term.academic_year,
    term.name       AS term_name,
    sc.data_status,
    sc.source_url
FROM assistant.lesson_occurrences lo
JOIN assistant.schedule sc        ON sc.id = lo.schedule_id
JOIN assistant.academic_terms term ON term.id = lo.term_id
JOIN assistant.groups g           ON g.id = sc.group_id
JOIN assistant.programs p         ON p.id = g.program_id
JOIN assistant.curriculum c       ON c.id = sc.curriculum_id
JOIN assistant.subjects sub       ON sub.id = c.subject_id
LEFT JOIN assistant.teachers t    ON t.id = c.teacher_id
LEFT JOIN assistant.rooms r       ON r.id = sc.room_id
LEFT JOIN assistant.pair_times pt ON pt.pair_number = sc.pair_number;

COMMENT ON VIEW assistant.schedule_calendar IS
    'Расписание, разложенное по конкретным датам, со временем пар и типом занятия.';

-- ---------------------------------------------------------------------
-- 6. Детекторы конфликтов
-- ---------------------------------------------------------------------
--
-- Две записи расписания пересекаются, если совпали день и номер пары, а типы
-- недель накладываются. 'каждую' накладывается на всё, 'чётная' — только на
-- 'чётная' и 'каждую'. Именно это условие и вынесено в overlap ниже.
--
-- Вьюхи постоянные, а не разовый скрипт: расписание будут править и после
-- хакатона, и проверка должна остаться доступной — и тестам, и деканату.

CREATE OR REPLACE VIEW assistant.schedule_conflicts_group AS
SELECT
    a.id            AS schedule_id,
    b.id            AS other_schedule_id,
    a.group_id,
    g.name          AS group_name,
    a.weekday,
    a.pair_number,
    a.week_type     AS week_type_a,
    b.week_type     AS week_type_b
FROM assistant.schedule a
JOIN assistant.schedule b
  ON b.group_id = a.group_id
 AND b.weekday = a.weekday
 AND b.pair_number = a.pair_number
 AND b.id > a.id
 AND (a.week_type = b.week_type OR a.week_type = 'каждую' OR b.week_type = 'каждую')
JOIN assistant.groups g ON g.id = a.group_id;

CREATE OR REPLACE VIEW assistant.schedule_conflicts_room AS
SELECT
    a.id            AS schedule_id,
    b.id            AS other_schedule_id,
    a.room_id,
    r.building,
    r.number        AS room_number,
    a.weekday,
    a.pair_number,
    a.week_type     AS week_type_a,
    b.week_type     AS week_type_b
FROM assistant.schedule a
JOIN assistant.schedule b
  ON b.room_id = a.room_id
 AND b.weekday = a.weekday
 AND b.pair_number = a.pair_number
 AND b.id > a.id
 AND (a.week_type = b.week_type OR a.week_type = 'каждую' OR b.week_type = 'каждую')
JOIN assistant.rooms r ON r.id = a.room_id;

CREATE OR REPLACE VIEW assistant.schedule_conflicts_teacher AS
SELECT
    a.id            AS schedule_id,
    b.id            AS other_schedule_id,
    ca.teacher_id,
    t.full_name     AS teacher_name,
    a.weekday,
    a.pair_number,
    a.week_type     AS week_type_a,
    b.week_type     AS week_type_b
FROM assistant.schedule a
JOIN assistant.curriculum ca ON ca.id = a.curriculum_id
JOIN assistant.schedule b
  ON b.weekday = a.weekday
 AND b.pair_number = a.pair_number
 AND b.id > a.id
 AND (a.week_type = b.week_type OR a.week_type = 'каждую' OR b.week_type = 'каждую')
JOIN assistant.curriculum cb ON cb.id = b.curriculum_id AND cb.teacher_id = ca.teacher_id
JOIN assistant.teachers t ON t.id = ca.teacher_id;

-- Сводная вьюха: все виды проблем расписания одним запросом.
-- issue_type — ровно тот словарь, что задан в брифе.
CREATE OR REPLACE VIEW assistant.schedule_issues AS
-- пересечения
SELECT 'conflict_group'::text AS issue_type, schedule_id, other_schedule_id,
       weekday, pair_number,
       'группа ' || group_name AS details
FROM assistant.schedule_conflicts_group
UNION ALL
SELECT 'conflict_teacher', schedule_id, other_schedule_id, weekday, pair_number,
       'преподаватель ' || teacher_name
FROM assistant.schedule_conflicts_teacher
UNION ALL
SELECT 'conflict_room', schedule_id, other_schedule_id, weekday, pair_number,
       'аудитория ' || building || ' ' || room_number
FROM assistant.schedule_conflicts_room
UNION ALL
-- время вне разумных границ: день недели не 1..6 или номер пары не описан
-- в pair_times (значит, времени начала и конца у него просто нет)
SELECT 'invalid_time_range', s.id, NULL, s.weekday, s.pair_number,
       'нет времени для пары №' || s.pair_number || ' или день недели вне 1..6'
FROM assistant.schedule s
WHERE s.weekday NOT BETWEEN 1 AND 6
   OR NOT EXISTS (SELECT 1 FROM assistant.pair_times pt
                  WHERE pt.pair_number = s.pair_number)
UNION ALL
-- полный дубль: та же группа, тот же слот, та же дисциплина
SELECT 'duplicate_lesson', a.id, b.id, a.weekday, a.pair_number,
       'дубль записи расписания'
FROM assistant.schedule a
JOIN assistant.schedule b
  ON b.group_id = a.group_id
 AND b.weekday = a.weekday
 AND b.pair_number = a.pair_number
 AND b.week_type = a.week_type
 AND b.curriculum_id = a.curriculum_id
 AND b.id > a.id
UNION ALL
-- окно больше одной пары подряд между занятиями одного дня
SELECT 'excessive_gap', cur_id, NULL, weekday, pair_number,
       'окно ' || (gap - 1) || ' пар(ы) до следующего занятия'
FROM (
    SELECT s.id AS cur_id,
           s.weekday,
           s.pair_number,
           LEAD(s.pair_number) OVER (
               PARTITION BY s.group_id, s.weekday ORDER BY s.pair_number
           ) - s.pair_number AS gap
    FROM assistant.schedule s
) gaps
WHERE gap > 2;

COMMENT ON VIEW assistant.schedule_issues IS
    'Все проблемы расписания одним списком: пересечения, дубли, окна, битое время.';
