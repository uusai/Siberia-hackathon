-- Отмена проведения занятия вместо удаления строки.
--
-- Применять:
--   python backend/scripts/apply_sql.py backend/sql/010_lesson_cancellation.sql --apply
--
-- ЗАЧЕМ. assistant.lesson_occurrences выводится из schedule: дата занятия
-- считается из дня недели и чётности недели. Когда занятие переносят на
-- другой день недели, количество дат может измениться — например, суббот в
-- семестре на одну меньше, чем понедельников. Лишняя строка становится
-- неверной, а удалять строки в этом проекте нельзя.
--
-- Поэтому лишние проведения не удаляются, а помечаются отменёнными:
-- cancelled_at + причина. Представление schedule_calendar их не показывает,
-- история изменений расписания при этом сохраняется целиком — что для
-- деканата скорее плюс, чем издержка.

ALTER TABLE assistant.lesson_occurrences
    ADD COLUMN IF NOT EXISTS cancelled_at timestamptz,
    ADD COLUMN IF NOT EXISTS cancel_reason text;

CREATE INDEX IF NOT EXISTS idx_lesson_occ_active
    ON assistant.lesson_occurrences (lesson_date)
    WHERE cancelled_at IS NULL;

-- Пересоздаём представление с фильтром. CREATE OR REPLACE сохраняет права и
-- зависимости, состав колонок не меняется.
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
JOIN assistant.schedule sc         ON sc.id = lo.schedule_id
JOIN assistant.academic_terms term ON term.id = lo.term_id
JOIN assistant.groups g            ON g.id = sc.group_id
JOIN assistant.programs p          ON p.id = g.program_id
JOIN assistant.curriculum c        ON c.id = sc.curriculum_id
JOIN assistant.subjects sub        ON sub.id = c.subject_id
LEFT JOIN assistant.teachers t     ON t.id = c.teacher_id
LEFT JOIN assistant.rooms r        ON r.id = sc.room_id
LEFT JOIN assistant.pair_times pt  ON pt.pair_number = sc.pair_number
WHERE lo.cancelled_at IS NULL;
