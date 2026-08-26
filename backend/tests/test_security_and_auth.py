"""Проверки SQL-whitelist и авторизации.

В проекте нет ни pytest.ini, ни conftest.py, ни каталога tests, поэтому новый
тест-фреймворк не заводится — это обычный скрипт на assert'ах:

    python backend/tests/test_security_and_auth.py

Функции названы test_*, так что при появлении pytest он подберёт их как есть.
БД для этих тестов не нужна.
"""

import datetime as dt
import os
import sys
from pathlib import Path

# Корень репозитория в sys.path, чтобы работал импорт backend.app.*
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Секрет задаём ДО импорта auth: значения по умолчанию в коде намеренно нет.
os.environ.setdefault("JWT_SECRET", "test-only-secret-not-for-production")

import jwt  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from backend.app import ai_agent, auth, security  # noqa: E402


def _rejects(sql: str) -> bool:
    try:
        security._assert_whitelist_tables(sql)
    except security.SQLSecurityError:
        return True
    return False


def test_whitelist_rejects_auth_schema():
    assert _rejects("SELECT * FROM auth.users"), "auth.users должна отклоняться"


def test_whitelist_rejects_tables_absent_from_assistant():
    for sql in (
        "SELECT * FROM curricula",
        "SELECT * FROM administration",
        "SELECT * FROM admissions_stats",
    ):
        assert _rejects(sql), f"должно отклоняться: {sql}"


def test_whitelist_rejects_schema_qualified_references():
    assert _rejects("SELECT * FROM public.applicants")
    assert _rejects("SELECT * FROM assistant.students")


def test_whitelist_rejects_comma_join_bypass():
    # Историческая дыра: старый regex видел только первую таблицу после FROM,
    # поэтому вторая таблица через запятую проходила проверку целиком.
    assert _rejects("SELECT u.* FROM university_units f, auth.users u")
    assert _rejects("SELECT s.* FROM university_units f, assistant.students s")
    assert _rejects("SELECT * FROM university_units, students")


def test_whitelist_rejects_quoted_identifier():
    assert _rejects('SELECT * FROM "students"')


def test_whitelist_rejects_subquery_table():
    assert _rejects("SELECT * FROM (SELECT id FROM students) t")


def test_whitelist_accepts_students_summary():
    # Не должно бросать исключение.
    security._assert_whitelist_tables(
        "SELECT SUM(student_count) FROM students_summary WHERE status = 'учится'"
    )
    security._assert_whitelist_tables(
        "SELECT count(*) FROM schedule s JOIN groups g ON g.id = s.group_id"
    )


def _rejects_for(sql: str, role: str) -> bool:
    try:
        security.validate_sql(sql, role)
    except security.SQLSecurityError:
        return True
    return False


def test_roles_gate_aggregated_views():
    # Контингент — не для студента и не для преподавателя.
    assert _rejects_for("SELECT SUM(student_count) FROM students_summary", "student")
    assert _rejects_for("SELECT SUM(student_count) FROM students_summary", "teacher")
    assert not _rejects_for(
        "SELECT SUM(student_count) FROM students_summary", "deans-office")
    assert not _rejects_for(
        "SELECT SUM(student_count) FROM students_summary", "administration")

    # Приёмная кампания и ЕГЭ — только администрации.
    for role in ("student", "teacher", "deans-office"):
        assert _rejects_for("SELECT * FROM applications_summary", role)
        assert _rejects_for("SELECT * FROM ege_scores_summary", role)
    assert not _rejects_for("SELECT * FROM applications_summary", "administration")
    assert not _rejects_for("SELECT * FROM ege_scores_summary", "administration")

    # Успеваемость — преподавателю и выше, но не студенту.
    assert _rejects_for("SELECT * FROM grades_summary", "student")
    assert not _rejects_for("SELECT * FROM grades_summary", "teacher")


def test_base_tables_available_to_everyone():
    for role in security.STAFF_ROLES:
        assert not _rejects_for("SELECT * FROM teachers", role)
        assert not _rejects_for(
            "SELECT weekday, pair_number FROM schedule", role)


def test_demo_structure_tables_are_hidden_from_the_model():
    # faculties, programs и departments — демонстрационный контур: пять
    # выдуманных факультетов и тринадцать направлений. Пока они видны модели,
    # вопрос «сколько факультетов в ИГУ» отвечается «пять» вместо пятнадцати.
    # Таблицы остаются в базе и работают, но SQL к ним модель не строит.
    for table in ("faculties", "programs", "departments"):
        for role in security.ALLOWED_TABLES_BY_ROLE:
            assert _rejects_for(f"SELECT * FROM {table}", role), (
                f"{table} не должна быть доступна роли {role}"
            )


def test_university_structure_comes_from_the_official_catalogue():
    # Замена закрытым таблицам: официальные подразделения и направления.
    for role in security.ALLOWED_TABLES_BY_ROLE:
        assert not _rejects_for("SELECT count(*) FROM university_units", role)
        assert not _rejects_for(
            "SELECT code, name FROM edu_programs WHERE level = 'бакалавриат'", role)


def test_personal_views_only_for_student():
    for sql in ("SELECT * FROM my_profile",
                "SELECT * FROM my_grades",
                "SELECT * FROM my_schedule"):
        assert not _rejects_for(sql, "student")
        for role in ("teacher", "deans-office", "administration"):
            assert _rejects_for(sql, role), f"{role} не должен видеть {sql}"


def test_teacher_views_only_for_teacher():
    for sql in ("SELECT * FROM my_teaching",
                "SELECT * FROM my_teaching_schedule",
                "SELECT * FROM my_students_performance"):
        assert not _rejects_for(sql, "teacher")
        for role in ("student", "deans-office", "administration"):
            assert _rejects_for(sql, role), f"{role} не должен видеть {sql}"


def test_official_reference_available_to_everyone():
    # Официальный справочник ИГУ (миграции 006/008) — публичные сведения:
    # структура вуза, вступительные испытания, сроки, стоимость. Ограничивать
    # их по ролям незачем, абитуриент заходит под той же учёткой.
    for role in security.STAFF_ROLES:
        for sql in (
            "SELECT official_name FROM university_units",
            "SELECT program_name, budget_seats FROM programs_admission",
            "SELECT subject, min_score FROM minimum_scores_view",
            "SELECT program_name, passing_score FROM passing_scores_view",
            "SELECT stage, date_to FROM admission_deadlines",
            "SELECT doc_name FROM admission_documents",
            "SELECT name, provided_to FROM dormitories",
            "SELECT question, answer FROM faq_entries",
            "SELECT lesson_date, subject_name FROM schedule_calendar",
        ):
            assert not _rejects_for(sql, role), f"{role} должен видеть: {sql}"


def test_public_contacts_survive_the_personal_data_filter():
    # FORBIDDEN_COLUMNS ищет 'phone' и 'email' по всему тексту запроса, поэтому
    # колонки контактов названы contact_phone/contact_email. Если кто-то
    # переименует их обратно, этот тест сломается раньше, чем демо.
    assert not _rejects_for(
        "SELECT title, contact_phone, contact_email FROM contacts", "student"
    )
    assert not _rejects_for(
        "SELECT name, contact_phone FROM dormitories", "student"
    )


def test_personal_data_columns_are_still_forbidden():
    # Ослабления чёрного списка не произошло: настоящие персональные данные
    # закрыты по-прежнему.
    for column in ("phone", "email", "passport", "birth_date"):
        assert _rejects_for(f"SELECT {column} FROM students", "administration"), (
            f"колонка {column} должна оставаться запрещённой"
        )


def test_schedule_quality_views_are_for_the_deans_office():
    # Студенту нужно расписание, а не список пересечений в нём.
    for sql in ("SELECT * FROM schedule_conflicts_group",
                "SELECT * FROM schedule_conflicts_teacher",
                "SELECT * FROM schedule_conflicts_room",
                "SELECT * FROM schedule_issues"):
        assert _rejects_for(sql, "student")
        assert _rejects_for(sql, "teacher")
        assert not _rejects_for(sql, "deans-office")
        assert not _rejects_for(sql, "administration")


def test_named_student_data_is_limited_to_the_deans_office():
    # student_rankings и academic_debts показывают ФИО студентов. Это
    # осознанное расширение прав деканата, а не дыра — и оно не должно
    # расползтись на студента и преподавателя.
    for sql in ("SELECT last_name, avg_score FROM student_rankings",
                "SELECT last_name, debts_count FROM academic_debts"):
        assert _rejects_for(sql, "student")
        assert _rejects_for(sql, "teacher")
        assert not _rejects_for(sql, "deans-office")
        assert not _rejects_for(sql, "administration")


def test_anonymous_analytics_is_open_wider():
    # Обезличенная успеваемость по дисциплинам нужна преподавателю.
    assert _rejects_for("SELECT * FROM subject_performance", "student")
    assert not _rejects_for("SELECT * FROM subject_performance", "teacher")

    # Аудитории и учебные планы не содержат ничего личного — доступны всем.
    for role in security.STAFF_ROLES:
        assert not _rejects_for(
            "SELECT building, room_number FROM room_availability "
            "WHERE is_free AND weekday = 1 AND pair_number = 2", role)
        assert not _rejects_for("SELECT * FROM room_load", role)
        assert not _rejects_for("SELECT * FROM group_curriculum", role)


def test_admission_statistics_stay_with_administration():
    for sql in ("SELECT * FROM applications_by_day",
                "SELECT * FROM admission_dynamics"):
        for role in ("student", "teacher", "deans-office"):
            assert _rejects_for(sql, role)
        assert not _rejects_for(sql, "administration")


def test_applicant_contacts_are_unreachable_for_everyone():
    # «Покажи контакты абитуриента с лучшим баллом» не должно работать ни у
    # кого: applications закрыта, а её обезличенные срезы полей связи не несут.
    for role in security.ALLOWED_TABLES_BY_ROLE:
        assert _rejects_for("SELECT applicant_name, phone FROM applications", role)
        assert _rejects_for("SELECT applicant_name, email FROM applications", role)
        assert _rejects_for("SELECT * FROM ege_scores", role)


def test_write_and_ddl_attempts_are_rejected():
    # Прямые попытки изменить данные и служебные запросы к каталогу.
    for sql in (
        "UPDATE grades SET score = 5 WHERE enrollment_id = 1",
        "INSERT INTO students (last_name) VALUES ('Иванов')",
        "DELETE FROM students WHERE id = 1",
        "SELECT * FROM university_units; DROP TABLE students; --",
        "SELECT count(*) FROM students; DROP TABLE students",
        "SELECT * FROM information_schema.columns",
        "SELECT * FROM pg_catalog.pg_tables",
        "SELECT set_config('app.student_id', '999', false)",
        "SELECT current_setting('app.student_id')",
        "SELECT pg_sleep(10) FROM university_units",
    ):
        for role in security.ALLOWED_TABLES_BY_ROLE:
            assert _rejects_for(sql, role), f"должно отклоняться: {sql}"


def test_rejections_are_explained_in_plain_language():
    # Пользователь спрашивает «обнови мою оценку», а видит «Не удалось
    # определить таблицу в запросе» — это ответ не на его вопрос. Причина
    # отказа понятная, и сказать её надо словами.
    def explain(sql, role="student"):
        try:
            security.validate_sql(sql, role)
        except security.SQLSecurityError as e:
            return security.explain_rejection(e)
        raise AssertionError(f"должно было отклониться: {sql}")

    assert "только на чтение" in explain("UPDATE grades SET score = 5")
    assert "только на чтение" in explain("DELETE FROM students WHERE id = 1")
    assert "только на чтение" in explain(
        "SELECT * FROM teachers; DROP TABLE students")
    assert "администратор" in explain(
        "SELECT * FROM student_rankings", "student")
    assert "закрыты для всех ролей" in explain(
        "SELECT passport FROM my_profile", "student")
    assert "произвольный текст" in explain(
        "SELECT * FROM faq_entries WHERE {user_input}")

    # Технический текст сохраняется для лога аудита — подменяется только то,
    # что уходит пользователю.
    try:
        security.validate_sql("UPDATE grades SET score = 5", "student")
    except security.SQLSecurityError as e:
        assert "SELECT" in str(e)


def test_quotes_and_wildcards_in_literals_are_safe():
    # Апостроф в имени и процент в шаблоне — обычные данные, а не инъекция:
    # запрос должен пройти, а не упасть и не быть отклонённым.
    assert not _rejects_for(
        "SELECT full_name FROM teachers WHERE full_name LIKE '%Д''Артаньян%'",
        "student")
    assert not _rejects_for(
        "SELECT name FROM subjects WHERE name LIKE '%\\%%'", "student")


def test_limit_is_enforced_on_unbounded_queries():
    # «Выведи вообще все оценки за всю историю» не должно выгружать базу.
    bounded = security.validate_sql("SELECT * FROM subject_performance", "teacher")
    assert "LIMIT" in bounded.upper()
    capped = security.validate_sql(
        "SELECT * FROM subject_performance LIMIT 100000", "teacher")
    assert f"LIMIT {security.MAX_LIMIT}" in capped


def test_auth_schema_still_closed_after_widening_the_whitelist():
    # Whitelist заметно расширился — проверяем, что дыра при этом не открылась.
    for role in security.ALLOWED_TABLES_BY_ROLE:
        assert _rejects_for("SELECT * FROM auth.users", role)
        assert _rejects_for("SELECT * FROM users", role)
        assert _rejects_for("SELECT * FROM students", role)
        assert _rejects_for(
            "SELECT u.* FROM university_units f, auth.users u", role
        )


def test_personal_views_do_not_cross_roles():
    # Студенческие вьюхи закрыты преподавателю и наоборот: у каждой роли
    # выставляется только своя сессионная переменная.
    assert _rejects_for("SELECT * FROM my_grades", "teacher")
    assert _rejects_for("SELECT * FROM my_teaching", "student")


def test_stale_token_version_is_rejected():
    # Токен прежней версии: подпись и срок в порядке, но состава полей нет.
    # Раньше он молча проходил, и ассистент «не знал», кто вошёл.
    stale = jwt.encode(
        {
            "sub": "student",
            "role": "student",
            "ver": auth.TOKEN_VERSION - 1,
            "exp": int(
                (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).timestamp()
            ),
        },
        os.environ["JWT_SECRET"],
        algorithm=auth.JWT_ALGORITHM,
    )
    assert _current_user_status(f"Bearer {stale}") == 401

    # И совсем без поля версии — тоже отказ.
    no_ver = jwt.encode(
        {
            "sub": "student",
            "role": "student",
            "exp": int(
                (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).timestamp()
            ),
        },
        os.environ["JWT_SECRET"],
        algorithm=auth.JWT_ALGORITHM,
    )
    assert _current_user_status(f"Bearer {no_ver}") == 401


def test_extract_sql_handles_fence_variants():
    # «``` sql» с пробелом раньше давало «sql SELECT ...» и отказ валидатора.
    for fenced in (
        "``` sql\nSELECT 1 FROM faculties\n```",
        "```sql\nSELECT 1 FROM faculties\n```",
        "```SQL\nSELECT 1 FROM faculties\n```",
        "```\nSELECT 1 FROM faculties\n```",
    ):
        got = ai_agent.extract_sql(fenced)
        assert got == "SELECT 1 FROM faculties", f"{fenced!r} -> {got!r}"


def test_teacher_id_survives_the_token():
    token = auth.create_access_token(
        username="teacher", role="teacher", teacher_id=57)
    got = auth.get_current_user(authorization=f"Bearer {token}")
    assert got["teacher_id"] == 57
    assert got["student_id"] is None


def test_chat_path_transaction_is_read_only():
    """Чат-путь не должен мочь писать в БД.

    У пользователя БД полные права на запись, поэтому единственной
    преградой между сгенерированным моделью запросом и DELETE остаётся
    режим транзакции. Единственный тест здесь, которому нужна живая БД —
    без неё тихо пропускается.
    """
    from backend.app import db

    try:
        db.fetch_all("assistant", "SELECT 1", read_only=True)
    except Exception:
        print("       (БД недоступна — проверка read-only пропущена)")
        return

    for stmt in (
        "DELETE FROM assistant.audit_log WHERE id = -1",
        "UPDATE assistant.students SET status = 'x' WHERE id = -1",
        "INSERT INTO assistant.audit_log(username, verdict) VALUES ('x','y')",
    ):
        try:
            db.fetch_all("assistant", stmt, read_only=True)
        except Exception as e:
            assert "read-only" in str(e), f"неожиданная ошибка на {stmt}: {e}"
        else:
            raise AssertionError(f"запись прошла в read-only транзакции: {stmt}")


def test_unknown_role_gets_nothing():
    # Подделанный или устаревший role в токене — отказ, а не полный доступ.
    assert _rejects_for("SELECT * FROM university_units", "root")
    assert security.allowed_tables_for("root") == set()


def test_set_config_is_blocked():
    # Через set_config можно было бы подменить app.student_id и прочитать
    # чужой профиль — это обход всей схемы личных данных.
    assert _rejects_for(
        "SELECT set_config('app.student_id','1',true) FROM my_profile", "student")
    assert _rejects_for(
        "SELECT current_setting('app.student_id') FROM my_profile", "student")
    assert _rejects_for("SELECT pg_sleep(10) FROM university_units", "student")


def test_cte_names_are_accepted_inside_their_own_query():
    # Раньше WITH не работал вообще: имя CTE в whitelist не входит и не войдёт,
    # поэтому FROM x отклонялся как «таблица не в списке разрешённых». А модель
    # пишет через WITH ровно те вопросы, ради которых заведена аналитика:
    # «кафедры со средним баллом ниже общего», «доля платников по направлениям».
    assert not _rejects_for(
        "WITH avg_by_dept AS ("
        "  SELECT department_name, avg_score FROM department_performance"
        ") SELECT department_name FROM avg_by_dept WHERE avg_score < 4",
        "deans-office",
    )
    # Несколько CTE через запятую и RECURSIVE.
    assert not _rejects_for(
        "WITH a AS (SELECT 1 AS n FROM university_units), "
        "b AS (SELECT n FROM a) SELECT n FROM b", "student"
    )
    assert not _rejects_for(
        "WITH RECURSIVE t AS (SELECT 1 AS n FROM university_units) "
        "SELECT n FROM t", "student"
    )


def test_cte_does_not_smuggle_a_closed_table():
    # Имя CTE разрешает ссылаться на само выражение, а не на что угодно внутри
    # него: тело проверяется на общих основаниях.
    assert _rejects_for(
        "WITH x AS (SELECT * FROM students) SELECT * FROM x", "administration")
    assert _rejects_for(
        "WITH x AS (SELECT * FROM auth.users) SELECT * FROM x", "administration")
    # И CTE не действует за пределами своего запроса.
    assert _rejects_for("SELECT * FROM avg_by_dept", "deans-office")


def test_outer_limit_survives_a_nested_one():
    # «Выведи вообще все оценки за всю историю»: подзапрос со своим LIMIT
    # раньше засчитывался за внешний, и выборка уходила неограниченной.
    bounded = security.validate_sql(
        "SELECT * FROM (SELECT * FROM subject_performance LIMIT 5) t", "teacher")
    assert bounded.rstrip().upper().endswith(f"LIMIT {security.DEFAULT_LIMIT}"), bounded
    # Настоящий внешний LIMIT по-прежнему уважается и режется по потолку.
    assert "LIMIT 10" in security.validate_sql(
        "SELECT * FROM subject_performance LIMIT 10", "teacher")
    capped = security.validate_sql(
        "SELECT * FROM (SELECT * FROM subject_performance LIMIT 5) t LIMIT 99999",
        "teacher")
    assert f"LIMIT {security.MAX_LIMIT}" in capped
    # Внутренний LIMIT при этом не тронут.
    assert "LIMIT 5" in capped


def test_words_inside_string_literals_are_data_not_code():
    # Промпт сам предписывает искать по faq_entries ключевыми словами, а
    # блеклист читал содержимое кавычек как код и отклонял запрос.
    assert not _rejects_for(
        "SELECT question, answer FROM faq_entries "
        "WHERE question ILIKE '%email%'", "student")
    assert not _rejects_for(
        "SELECT name FROM subjects WHERE name ILIKE '%create%'", "student")
    assert not _rejects_for(
        "SELECT name FROM subjects WHERE name ILIKE '%drop%'", "student")
    # Точка с запятой внутри литерала ничего не разделяет.
    assert not _rejects_for(
        "SELECT name FROM subjects WHERE name LIKE '%a;b%'", "student")
    # При этом настоящие конструкции вне кавычек закрыты по-прежнему.
    assert _rejects_for("SELECT * FROM subjects; DROP TABLE students", "student")
    assert _rejects_for("SELECT email FROM my_profile", "student")


def test_groups_catalog_answers_the_question_that_used_to_be_invented():
    # Объект, которого не было: «какие группы учатся на направлении X».
    # Без него модель соединяла groups с edu_programs и получала пустоту.
    for role in security.STAFF_ROLES:
        assert not _rejects_for(
            "SELECT group_name, course FROM groups_catalog "
            "WHERE search_vector @@ plainto_tsquery('russian', "
            "'информационная безопасность')", role)


def test_guest_sees_only_the_public_admission_reference():
    # Гость — посетитель сайта вуза через встраиваемый виджет. Токен ему
    # выдаётся БЕЗ ПАРОЛЯ любому желающему (POST /auth/guest), поэтому набор
    # обязан быть самым узким из всех: только то, что и так опубликовано.
    for sql in (
        "SELECT program_name, budget_seats, tuition_rub FROM programs_admission",
        "SELECT required_subjects FROM program_exam_sets",
        "SELECT stage, date_to FROM admission_deadlines",
        "SELECT doc_name FROM admission_documents",
        "SELECT question, answer FROM faq_entries",
        "SELECT official_name, kind FROM university_units",
        "SELECT title, contact_phone FROM contacts",
        # Расписание вузы публикуют открыто, и это самый частый вопрос на
        # сайте. Гостю оно доступно через public_schedule — без ФИО.
        "SELECT lesson_date, subject_name, room_number FROM public_schedule",
        "SELECT pair_number, starts_at FROM pair_times",
        "SELECT term_name, date_from FROM academic_terms",
        "SELECT building, number, capacity FROM rooms",
    ):
        assert not _rejects_for(sql, "guest"), f"гостю нужен доступ: {sql}"

    # Всё, что про людей и учебный процесс, закрыто.
    for sql in (
        "SELECT * FROM students_summary",
        "SELECT * FROM grades_summary",
        "SELECT * FROM academic_debts",
        "SELECT * FROM student_debts",
        "SELECT * FROM student_rankings",
        "SELECT * FROM subject_performance",
        "SELECT * FROM teachers",
        "SELECT * FROM my_profile",
        "SELECT * FROM ege_scores_summary",
        "SELECT * FROM applications_summary",
        "SELECT * FROM auth.users",
        # Расписание С ФИО преподавателей гостю закрыто: одно дело —
        # опубликованное расписание занятий, другое — возможность у анонимного
        # посетителя спросить, где конкретный преподаватель в четверг в 14:00.
        "SELECT * FROM schedule_calendar",
        "SELECT * FROM lesson_occurrences",
    ):
        assert _rejects_for(sql, "guest"), f"гость не должен видеть: {sql}"

    # Гость шире студента ровно на один объект — public_schedule, и это
    # намеренно. Сотрудникам безымянная копия не нужна: у них есть
    # schedule_calendar со всеми полями, а два объекта на одни и те же данные
    # плодят двусмысленность, из-за которой модель уже отвечала «по одним
    # данным 88%, по другим 86,8%».
    assert (security.allowed_tables_for("guest") - {"public_schedule"}
            <= security.allowed_tables_for("student"))
    assert "public_schedule" not in security.allowed_tables_for("student")


def test_guest_refusals_are_worded_for_a_website_visitor():
    # Посетителю сайта нечего делать с фразой «доступ выдаёт администратор
    # системы»: он не сотрудник и прав не просил.
    def explain(sql, role):
        try:
            security.validate_sql(sql, role)
        except security.SQLSecurityError as e:
            return security.explain_rejection(e, role)
        raise AssertionError(f"должно было отклониться: {sql}")

    guest_text = explain("SELECT * FROM student_debts", "guest")
    assert "поступлени" in guest_text
    assert "администратор" not in guest_text

    # Для сотрудников формулировка прежняя.
    assert "администратор" in explain("SELECT * FROM student_debts", "student")


def test_spoken_refusal_is_told_apart_from_a_broken_query():
    # Модель регулярно отказывает СЛОВАМИ, заворачивая фразу в SELECT без
    # FROM. Формально это запрос без таблицы, и проверка отклоняет его с
    # «Не удалось определить таблицу» — текстом, который пользователю ничего
    # не объясняет: он спрашивал не про таблицы.
    for sql in (
        "SELECT 'Сведения о преподавателях не предоставляются.'",
        "SELECT 'В базе данных нет информации о контактах абитуриентов.'",
        "SELECT 'Доступ к такой информации не предоставлен' AS message",
        "select 'нет данных'   ",
    ):
        assert security.looks_like_a_spoken_refusal(sql), sql

    # Настоящие запросы за отказ не принимаются.
    for sql in (
        "SELECT program_name FROM programs_admission",
        "SELECT 'бюджет' AS kind, count(*) FROM enrollment_places",
        "SELECT count(*) FROM university_units",
        "SELECT 1",
    ):
        assert not security.looks_like_a_spoken_refusal(sql), sql


def test_guest_throttle_is_separate_from_login():
    from backend.app import throttle

    throttle.clear()
    try:
        assert throttle.guest_retry_after("10.0.0.1") == 0
        for _ in range(throttle.GUEST_MAX):
            throttle.note_guest_request("10.0.0.1")
        assert throttle.guest_retry_after("10.0.0.1") > 0
        # Другой адрес не задет, и вход по паролю тоже.
        assert throttle.guest_retry_after("10.0.0.2") == 0
        assert throttle.retry_after("10.0.0.1") == 0
    finally:
        throttle.clear()


def test_login_throttle_counts_and_resets():
    from backend.app import throttle

    throttle.clear()
    try:
        assert throttle.retry_after("student") == 0
        for _ in range(throttle.MAX_ATTEMPTS):
            throttle.register_failure("student")
        assert throttle.retry_after("student") > 0, "перебор должен блокироваться"
        # Блокировка адресная: соседняя учётка продолжает работать.
        assert throttle.retry_after("teacher") == 0
        # Успешный вход обнуляет счётчик.
        throttle.reset("student")
        assert throttle.retry_after("student") == 0
    finally:
        throttle.clear()


def test_blank_result_covers_the_null_aggregate():
    # SELECT SUM(...) с фильтром, который ничего не нашёл, возвращает ОДНУ
    # строку с NULL, а не ноль строк. Формально выборка непустая, и раньше она
    # уходила модели «на объяснение»: на вопрос «сколько студентов на
    # факультете» пользователь получал «Произошла ошибка при попытке получить
    # данные», хотя ошибки не было.
    null_row = security.QueryResult(["sum"], [[""]], "", None)
    assert not null_row.is_empty, "строка есть — значит не is_empty"
    assert null_row.is_blank, "но показывать в ней нечего"

    # Ноль — это ответ, а не пустота.
    zero = security.QueryResult(["count"], [["0"]], "0", None)
    assert not zero.is_blank

    real = security.QueryResult(["n"], [["17"]], "17", None)
    assert not real.is_blank and not real.is_empty
    assert security.QueryResult([], [], "", None).is_empty


def test_out_of_scope_is_told_apart_from_no_data():
    # «Покажи пароли» и «добавь студента» модель уводит в faq_entries по
    # ключевому слову и получает пусто. Это не «данных нет», а «вопрос не про
    # наши данные» — и сказать надо именно это.
    assert ai_agent.empty_result_answer(
        "SELECT question, answer FROM faq_entries WHERE question ILIKE '%пароли%'"
    ) is ai_agent.OUT_OF_SCOPE_ANSWER
    # А вот пустой ответ по настоящим данным — обычное «не нашлось».
    assert ai_agent.empty_result_answer(
        "SELECT group_name FROM groups_catalog WHERE course = 9"
    ) is ai_agent.NOTHING_FOUND_ANSWER
    # Справка вместе с данными — уже не отказ по области.
    assert ai_agent.empty_result_answer(
        "SELECT f.answer FROM faq_entries f, university_units u"
    ) is ai_agent.NOTHING_FOUND_ANSWER
    assert ai_agent.empty_result_answer(None) is ai_agent.NOTHING_FOUND_ANSWER


def test_tables_in_reports_what_was_touched():
    assert security.tables_in("SELECT * FROM faq_entries") == {"faq_entries"}
    assert security.tables_in(
        "SELECT * FROM groups_catalog g JOIN schedule s ON s.group_id = g.id"
    ) == {"groups_catalog", "schedule"}


def test_blank_answers_are_detected():
    # Ровно тот текст, который система выдала на стенде вместо ответа.
    assert ai_agent.blank_answer_reason(
        "На направлении «Информационная безопасность» учатся следующие группы: "
        "[название группы 1] [название группы 2]"
    )
    assert ai_agent.blank_answer_reason(
        "(перечислить группы, если бы они были в ответе)"
    )
    assert ai_agent.blank_answer_reason("Если данных нет, то: ничего не найдено")
    # Нормальный ответ бланком не считается.
    assert ai_agent.blank_answer_reason(
        "На направлении «Информационная безопасность» учатся 17 групп: "
        "ФИТ-0925-1, ФИТ-0924-1."
    ) is None


def _current_user_status(authorization):
    """Возвращает None при успехе, иначе HTTP-код ошибки."""
    try:
        auth.get_current_user(authorization=authorization)
    except HTTPException as e:
        return e.status_code
    return None


def test_get_current_user_rejects_missing_and_malformed():
    assert _current_user_status(None) == 401
    assert _current_user_status("") == 401
    assert _current_user_status("Bearer") == 401
    assert _current_user_status("Bearer ") == 401
    assert _current_user_status("Basic abc") == 401
    assert _current_user_status("Bearer not-a-jwt") == 401


def test_get_current_user_rejects_expired_token():
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    expired = jwt.encode(
        {"sub": "student", "role": "student", "exp": int(past.timestamp())},
        os.environ["JWT_SECRET"],
        algorithm=auth.JWT_ALGORITHM,
    )
    assert _current_user_status(f"Bearer {expired}") == 401


def test_get_current_user_rejects_wrong_signature():
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    forged = jwt.encode(
        {
            "sub": "administration",
            "role": "administration",
            "exp": int(future.timestamp()),
        },
        "some-other-secret",
        algorithm=auth.JWT_ALGORITHM,
    )
    assert _current_user_status(f"Bearer {forged}") == 401


def test_get_current_user_accepts_fresh_token():
    token = auth.create_access_token(username="teacher", role="teacher")
    got = auth.get_current_user(authorization=f"Bearer {token}")
    assert got["username"] == "teacher"
    assert got["role"] == "teacher"
    assert got["student_id"] is None


def test_student_id_survives_the_token():
    # По этому идентификатору фильтруются личные вьюхи, поэтому важно,
    # что он доезжает из токена в неизменном виде.
    token = auth.create_access_token(
        username="student", role="student", student_id=544)
    got = auth.get_current_user(authorization=f"Bearer {token}")
    assert got["student_id"] == 544
    assert got["role"] == "student"


def test_password_hash_roundtrip():
    hashed = auth.hash_password("student")
    assert hashed != "student", "пароль не должен храниться в открытом виде"
    assert auth.verify_password("student", hashed)
    assert not auth.verify_password("wrong", hashed)


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  [OK]   {name}")
            except AssertionError as e:
                print(f"  [FAIL] {name}: {e}")
                failed += 1
    # Проверка read-only могла поднять пул — закрываем, иначе psycopg
    # пытается дожать свои потоки на финализации интерпретатора.
    from backend.app import db as _db
    _db.close_all()

    print()
    print("Все проверки пройдены." if not failed else f"Провалено проверок: {failed}")
    sys.exit(1 if failed else 0)
