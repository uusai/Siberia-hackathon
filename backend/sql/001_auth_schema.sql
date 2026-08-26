-- Схема auth: учётные записи приложения.
--
-- Намеренно ОТДЕЛЬНАЯ схема (не assistant и не public): ai_agent.get_db_schema()
-- и get_db_relationships() фильтруют table_schema = 'assistant', поэтому auth
-- структурно не попадает ни в промпт модели, ни в список связей — независимо
-- от содержимого security.ALLOWED_TABLES.
--
-- Применять вручную, один раз:
--   psql -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME -f backend/sql/001_auth_schema.sql

CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE auth.users (
    id serial PRIMARY KEY,
    username text UNIQUE NOT NULL,
    password_hash text NOT NULL,
    role text NOT NULL CHECK (role IN ('student', 'teacher',
                                        'deans-office', 'administration')),
    created_at timestamptz NOT NULL DEFAULT now()
);
