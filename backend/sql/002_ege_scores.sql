-- Побалльные результаты ЕГЭ: одна строка на предмет на заявление.
--
-- Применять вручную:
--   psql -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME -f backend/sql/002_ege_scores.sql
--
-- ВАЖНО про доступ агента: сырая таблица ege_scores В whitelist НЕ входит —
-- в ней есть application_id, внешний ключ в applications, где лежат ФИО,
-- паспорт, телефон и почта абитуриента. Через связку по этому ключу можно
-- было бы восстановить, кто именно сколько набрал. Агенту доступно только
-- агрегированное представление ege_scores_summary (см. ALLOWED_TABLES
-- в backend/app/security.py) — тот же приём, что со students_summary
-- и applications_summary.

CREATE TABLE IF NOT EXISTS assistant.ege_scores (
    id serial PRIMARY KEY,
    application_id integer NOT NULL REFERENCES assistant.applications(id),
    subject text NOT NULL,
    score integer NOT NULL CHECK (score BETWEEN 0 AND 100),
    UNIQUE (application_id, subject)
);

-- Выборка почти всегда идёт по предмету — под неё индекс.
CREATE INDEX IF NOT EXISTS idx_ege_scores_subject ON assistant.ege_scores (subject);

-- Агрегированное представление: ни application_id, ни любого поля,
-- позволяющего опознать абитуриента, здесь нет и быть не должно.
CREATE OR REPLACE VIEW assistant.ege_scores_summary AS
SELECT
    p.faculty_id,
    p.name        AS program_name,
    p.degree,
    ac.year       AS campaign_year,
    es.subject,
    COUNT(*)                  AS applications_count,
    ROUND(AVG(es.score), 1)   AS avg_score,
    MIN(es.score)             AS min_score,
    MAX(es.score)             AS max_score
FROM assistant.ege_scores es
JOIN assistant.applications a          ON a.id  = es.application_id
JOIN assistant.admission_campaigns ac  ON ac.id = a.campaign_id
JOIN assistant.programs p              ON p.id  = ac.program_id
GROUP BY p.faculty_id, p.name, p.degree, ac.year, es.subject;
