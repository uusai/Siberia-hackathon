-- Сводные представления: название факультета вместо его идентификатора.
--
-- Применять:
--   python backend/scripts/apply_sql.py backend/sql/016_summary_views.sql --apply
--
-- ---------------------------------------------------------------------
-- ЗАЧЕМ
-- ---------------------------------------------------------------------
--
-- students_summary и ege_scores_summary отдавали колонку faculty_id — голый
-- идентификатор из таблицы faculties, которая от модели СКРЫТА (это
-- демонстрационный контур, см. комментарий в security.py). Пользы от такой
-- колонки нет никакой: соединить её не с чем, а назвать факультет по числу
-- невозможно. Зато вред очевиден — ровно так модель и придумала соединение
-- groups.program_id с edu_programs: увидела идентификатор без объявленной
-- связи и подобрала таблицу по смыслу названия.
--
-- Для настоящих таблиц такие висячие ссылки вычищаются из промпта
-- автоматически (ai_agent._dangling_fk_columns по живым constraint'ам), но у
-- представлений внешних ключей нет, и их приходилось перечислять в коде
-- руками. Список в коде — это описание дефекта, а не его устранение.
-- Устранение здесь: представление отдаёт НАЗВАНИЕ факультета.
--
-- Заодно это добавляет возможность, которой не было: «средний балл ЕГЭ по
-- факультетам» раньше нельзя было даже сформулировать — группировать было
-- не по чему, кроме безымянного числа.
--
-- ---------------------------------------------------------------------
-- ВТОРОЕ: эти представления не были описаны НИ ОДНОЙ миграцией
-- ---------------------------------------------------------------------
--
-- students_summary, grades_summary и applications_summary существовали только
-- в живой базе — в backend/sql они лишь упоминались в комментариях. То же, что
-- было с audit_log (см. 014): на чистом развёртывании whitelist ссылается на
-- объекты, которых никто не создаёт. Поэтому ниже они описаны целиком, включая
-- те, что не меняются.
--
-- CREATE OR REPLACE VIEW переименовать колонку не умеет, поэтому пересоздаём.
-- Зависимых объектов у обоих нет (проверено через pg_depend), данные не
-- затрагиваются: это представления.

-- ---------------------------------------------------------------------
-- 1. Контингент студентов
-- ---------------------------------------------------------------------

DROP VIEW IF EXISTS assistant.students_summary;

CREATE VIEW assistant.students_summary AS
SELECT
    f.name        AS faculty_name,
    p.name        AS program_name,
    p.degree,
    g.course,
    s.status,
    s.funding,
    s.enrolled_year,
    count(*)      AS student_count
FROM assistant.students s
JOIN assistant.groups g     ON g.id = s.group_id
JOIN assistant.programs p   ON p.id = g.program_id
JOIN assistant.faculties f  ON f.id = p.faculty_id
GROUP BY f.name, p.name, p.degree, g.course, s.status, s.funding, s.enrolled_year;

COMMENT ON VIEW assistant.students_summary IS
    'Контингент в разрезе факультета, направления, курса, статуса и формы '
    'оплаты. Строка — это группа студентов, а не человек: чтобы получить '
    'число людей, суммируйте student_count.';


-- ---------------------------------------------------------------------
-- 2. Баллы ЕГЭ (замена faculty_id -> faculty_name, остальное как в 002)
-- ---------------------------------------------------------------------
--
-- Ни application_id, ни любого поля, позволяющего опознать абитуриента, здесь
-- нет и быть не должно — сырая ege_scores в whitelist не входит никогда.

DROP VIEW IF EXISTS assistant.ege_scores_summary;

CREATE VIEW assistant.ege_scores_summary AS
SELECT
    f.name        AS faculty_name,
    p.name        AS program_name,
    p.degree,
    ac.year       AS campaign_year,
    es.subject,
    count(*)                  AS applications_count,
    round(avg(es.score), 1)   AS avg_score,
    min(es.score)             AS min_score,
    max(es.score)             AS max_score
FROM assistant.ege_scores es
JOIN assistant.applications a          ON a.id  = es.application_id
JOIN assistant.admission_campaigns ac  ON ac.id = a.campaign_id
JOIN assistant.programs p              ON p.id  = ac.program_id
JOIN assistant.faculties f             ON f.id  = p.faculty_id
GROUP BY f.name, p.name, p.degree, ac.year, es.subject;

COMMENT ON VIEW assistant.ege_scores_summary IS
    'Баллы ЕГЭ по предметам в разрезе факультета, направления и года приёма. '
    'Обезличено: опознать абитуриента по этим полям нельзя.';


-- ---------------------------------------------------------------------
-- 3. Оценки и заявления — без изменений, только фиксируем определение
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW assistant.grades_summary AS
SELECT
    e.curriculum_id,
    g.attempt,
    count(*)                AS grades_count,
    round(avg(g.score), 2)  AS avg_score
FROM assistant.grades g
JOIN assistant.enrollments e ON e.id = g.enrollment_id
GROUP BY e.curriculum_id, g.attempt;

CREATE OR REPLACE VIEW assistant.applications_summary AS
SELECT
    campaign_id,
    status,
    funding_type,
    count(*)                    AS applications_count,
    round(avg(ege_total), 2)    AS avg_ege_total,
    min(ege_total)              AS min_ege_total,
    max(ege_total)              AS max_ege_total
FROM assistant.applications
GROUP BY campaign_id, status, funding_type;
