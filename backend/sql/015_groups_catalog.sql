-- Учебные группы и их направления, одним объектом.
--
-- Применять:
--   python backend/scripts/apply_sql.py backend/sql/015_groups_catalog.sql --apply
--
-- ЗАЧЕМ. На вопрос «какие группы учатся на направлении информационная
-- безопасность» ассистент выдал список из двух заглушек: «[название группы 1]»,
-- «[название группы 2]». SQL при этом был такой:
--
--     SELECT DISTINCT g.name FROM groups g
--     JOIN edu_programs ep ON g.program_id = ep.id
--     WHERE ep.name ILIKE '%информационная безопасность%'
--
-- Запрос вернул ноль строк и не мог вернуть больше: groups.program_id ведёт в
-- assistant.programs (демонстрационный контур, 13 направлений), а edu_programs —
-- официальный каталог ИГУ на 113 позиций. Значения там несопоставимы: program_id
-- не превышает 13, а «Информационная безопасность» в каталоге имеет id 108 и 109.
--
-- Ответ в базе при этом БЫЛ: по правильной связи это направление изучают
-- 17 групп. Дотянуться до него модель не могла — таблица programs от неё скрыта
-- (см. комментарий в security.py про пять выдуманных факультетов), поэтому связь
-- groups.program_id -> programs.id выпадала из промпта, и колонка program_id
-- оставалась висеть без единого объявленного соединения.
--
-- Лечится с двух сторон. В ai_agent.get_db_schema() такие висячие колонки больше
-- не показываются модели вовсе. А здесь появляется законный объект, который
-- отвечает на вопрос напрямую: названия направления, факультета и группы лежат
-- текстом, соединять ничего не надо.

DROP VIEW IF EXISTS assistant.groups_catalog;

CREATE VIEW assistant.groups_catalog AS
SELECT
    g.name          AS group_name,
    g.course,
    p.name          AS program_name,
    p.degree,
    p.study_form,
    f.name          AS faculty_name,
    g.start_year,
    count(s.id) FILTER (WHERE s.status = 'учится')  AS students_count,
    -- Падежи: спрашивают «на информационной безопасности», а в базе
    -- именительный. Тот же приём, что в 011/012.
    to_tsvector('russian',
        coalesce(g.name, '') || ' ' || coalesce(p.name, '') || ' ' ||
        coalesce(f.name, '')) AS search_vector
FROM assistant.groups g
JOIN assistant.programs p    ON p.id = g.program_id
JOIN assistant.faculties f   ON f.id = p.faculty_id
LEFT JOIN assistant.students s ON s.group_id = g.id
GROUP BY g.id, g.name, g.course, p.name, p.degree, p.study_form,
         f.name, g.start_year;

COMMENT ON VIEW assistant.groups_catalog IS
    'Учебные группы с направлением и факультетом текстом. Отвечает на «какие '
    'группы учатся на направлении X» без соединения демо-контура с официальным '
    'каталогом приёма — у них нет общих идентификаторов.';
