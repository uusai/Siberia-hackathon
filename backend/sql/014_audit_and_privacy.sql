-- Журнал обращений и одна поправка приватности.
--
-- Применять:
--   python backend/scripts/apply_sql.py backend/sql/014_audit_and_privacy.sql --apply
--
-- ---------------------------------------------------------------------
-- 1. assistant.audit_log
-- ---------------------------------------------------------------------
--
-- Таблица существовала в живой базе (на момент написания — 230 записей), но
-- миграции на неё не было НИ ОДНОЙ: ни здесь, ни в scripts. То есть на чистом
-- развёртывании /chat поднимался без журнала, и молча — security.alog_audit_entry
-- вызывается через _safe_log_audit(), который гасит исключение и печатает
-- строку в stderr. Работающее демо, ноль записей аудита и никакого сигнала.
--
-- Поэтому здесь описана та же структура, что уже в базе, и описана дважды:
-- CREATE TABLE IF NOT EXISTS поднимает её с нуля, следом ADD COLUMN IF NOT
-- EXISTS дотягивает недостающие колонки там, где таблица уже есть, но старая.
-- Оба шага идемпотентны, повторный запуск безопасен.

CREATE TABLE IF NOT EXISTS assistant.audit_log (
    id              bigserial PRIMARY KEY,
    ts              timestamptz NOT NULL DEFAULT now(),
    username        text,
    role            text,
    question        text,
    generated_sql   text,
    executed_sql    text,
    verdict         text NOT NULL,
    reject_reason   text,
    row_count       integer,
    duration_ms     integer,
    llm_ms          integer,
    model           text
);

ALTER TABLE assistant.audit_log
    ADD COLUMN IF NOT EXISTS ts            timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS username      text,
    ADD COLUMN IF NOT EXISTS role          text,
    ADD COLUMN IF NOT EXISTS question      text,
    ADD COLUMN IF NOT EXISTS generated_sql text,
    ADD COLUMN IF NOT EXISTS executed_sql  text,
    ADD COLUMN IF NOT EXISTS reject_reason text,
    ADD COLUMN IF NOT EXISTS row_count     integer,
    ADD COLUMN IF NOT EXISTS duration_ms   integer,
    ADD COLUMN IF NOT EXISTS llm_ms        integer,
    ADD COLUMN IF NOT EXISTS model         text;

-- Набор исходов расширился: к ok / rejected / no_sql добавились db_error
-- (СУБД не выполнила запрос даже после самоисправления), empty (выборка
-- пуста — отвечаем честно и без второго обращения к модели), placeholder
-- (модель выдала бланк вместо ответа, текст подменён) и три исхода входа.
-- Если на таблице когда-то появится CHECK со старым списком, он это запретит,
-- поэтому снимаем его заранее и не заводим новый: список исходов живёт в коде.
ALTER TABLE assistant.audit_log DROP CONSTRAINT IF EXISTS audit_log_verdict_check;

-- Отчёты в /meta/stats читают последние сутки и группируют по исходу.
--
-- Индекс по одному ts здесь НЕ заводится: в базе уже есть idx_audit_ts, а
-- второй такой же — лишняя работа на каждой вставке в журнал и ничего больше.
-- Составной (verdict, ts) покрывает разбивку по исходам и им не дублируется.
DROP INDEX IF EXISTS assistant.idx_audit_log_ts;
CREATE INDEX IF NOT EXISTS idx_audit_log_verdict_ts
    ON assistant.audit_log (verdict, ts DESC);
-- На случай развёртывания с нуля, где idx_audit_ts не создавался ничем иным.
CREATE INDEX IF NOT EXISTS idx_audit_ts ON assistant.audit_log (ts DESC);

COMMENT ON TABLE assistant.audit_log IS
    'Журнал обращений к ассистенту: вопрос, сгенерированный и выполненный SQL, '
    'исход, причина отказа, время ответа. Паролей здесь нет и быть не может — '
    'записываются только вопрос, SQL и метаданные.';


-- ---------------------------------------------------------------------
-- 2. my_profile без почты
-- ---------------------------------------------------------------------
--
-- README обещает: «Паспорта, телефоны, почты и даты рождения закрыты для ВСЕХ
-- ролей без исключения — они в security.FORBIDDEN_COLUMNS». Представление
-- my_profile при этом отдавало s.email.
--
-- Правило получалось непоследовательным: SELECT email FROM my_profile
-- проверка отклоняла (слово email в чёрном списке), а SELECT * FROM my_profile
-- ту же почту спокойно возвращал — и она уезжала в промпт второй фазы.
-- Убираем из представления, чтобы обещание и поведение совпали.
--
-- CREATE OR REPLACE VIEW удалить колонку не умеет (PostgreSQL разрешает только
-- дописывать в конец), поэтому пересоздаём. Данные не затрагиваются: это
-- представление, а не таблица.

DROP VIEW IF EXISTS assistant.my_profile;

CREATE VIEW assistant.my_profile AS
SELECT
    s.last_name,
    s.first_name,
    s.middle_name,
    g.name  AS group_name,
    g.course,
    p.name  AS program_name,
    p.degree,
    p.study_form,
    f.name  AS faculty_name,
    s.enrolled_year,
    s.status,
    s.funding
FROM assistant.students s
JOIN assistant.groups g    ON g.id = s.group_id
JOIN assistant.programs p  ON p.id = g.program_id
JOIN assistant.faculties f ON f.id = p.faculty_id
WHERE s.id = NULLIF(current_setting('app.student_id', true), '')::int;

COMMENT ON VIEW assistant.my_profile IS
    'Профиль текущего студента. Фильтруется по app.student_id, который бэкенд '
    'выставляет из проверенного JWT. Почты, телефона и даты рождения здесь нет '
    'намеренно — они закрыты для всех ролей.';
