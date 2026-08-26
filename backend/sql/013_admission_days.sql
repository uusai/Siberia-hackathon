-- Дни приёмной кампании: «за последние 7 дней» и «в последний день».
--
-- Применять:
--   python backend/scripts/apply_sql.py backend/sql/013_admission_days.sql --apply
--
-- ЗАЧЕМ. Живой прогон вопросов показал устойчивый промах: на «сколько
-- заявлений подано за последние 7 дней приёмной кампании 2025 года» модель
-- писала submitted_at >= now() - interval '7 days'. Логика понятная, но
-- неверная: кампания 2025 года закончилась в 2025-м, и «последние семь дней»
-- отсчитываются от ЕЁ окончания, а не от сегодняшнего числа. Ответ выходил
-- пустым.
--
-- Считать разницу дат должна база, а не модель: docs_to лежит в той же
-- строке, и вычесть из него дату подачи — это одна колонка, которую нельзя
-- понять неправильно.

DROP VIEW IF EXISTS assistant.applications_by_day;
CREATE VIEW assistant.applications_by_day AS
SELECT
    ac.year         AS campaign_year,
    a.submitted_at,
    ac.docs_from,
    ac.docs_to,
    -- Сколько дней до конца приёма было подано заявление. 0 — последний
    -- день кампании, 7 — за неделю до конца. Отрицательных значений быть не
    -- должно, но если появятся, это признак данных вне сроков кампании.
    (ac.docs_to - a.submitted_at)    AS days_before_deadline,
    (a.submitted_at = ac.docs_to)    AS is_last_day,
    p.name          AS program_name,
    p.degree,
    a.status,
    a.funding_type,
    count(*)                    AS applications_count,
    round(avg(a.ege_total), 1)  AS avg_ege_total,
    max(a.ege_total)            AS max_ege_total
FROM assistant.applications a
JOIN assistant.admission_campaigns ac ON ac.id = a.campaign_id
JOIN assistant.programs p ON p.id = ac.program_id
GROUP BY ac.year, a.submitted_at, ac.docs_from, ac.docs_to, p.name, p.degree,
         a.status, a.funding_type;

COMMENT ON VIEW assistant.applications_by_day IS
    'Заявления по дням подачи. days_before_deadline — сколько дней оставалось '
    'до конца приёма; is_last_day = true — подано в последний день. '
    'Обезличено: ни ФИО, ни контактов абитуриента здесь нет.';
