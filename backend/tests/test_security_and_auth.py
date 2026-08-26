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


# Роли учебного контура — все, кроме абитуриента.
#
# Абитуриент намеренно выпадает из накопительной цепочки: он не «студент с
# урезанными правами», а человек вне учебного процесса. Расписания, учебных
# планов, аудиторий и преподавателей у него нет вовсе, поэтому проверки вида
# «это доступно каждому» перебирают именно ACADEMIC_ROLES. Список считается
# из самой раскладки, чтобы новая роль не забылась молча.
ACADEMIC_ROLES = [
    r for r in security.ALLOWED_TABLES_BY_ROLE if r != "applicant"
]


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
    for role in ACADEMIC_ROLES:
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
        for role in ("applicant", "teacher", "deans-office", "administration"):
            assert _rejects_for(sql, role), f"{role} не должен видеть {sql}"


def test_teacher_views_only_for_teacher():
    for sql in ("SELECT * FROM my_teaching",
                "SELECT * FROM my_teaching_schedule",
                "SELECT * FROM my_students_performance"):
        assert not _rejects_for(sql, "teacher")
        for role in ("student", "deans-office", "administration"):
            assert _rejects_for(sql, role), f"{role} не должен видеть {sql}"


def test_official_reference_available_to_everyone():
    # Справочник приёма — публичные сведения: структура вуза, вступительные
    # испытания, сроки, стоимость. Это единственный набор, который целиком
    # достаётся ВСЕМ ролям, включая абитуриента.
    for role in security.ALLOWED_TABLES_BY_ROLE:
        for sql in (
            "SELECT official_name FROM university_units",
            "SELECT program_name, budget_seats FROM programs_admission",
            "SELECT subject, min_score FROM minimum_scores_view",
            "SELECT program_name, passing_score FROM passing_scores_view",
            "SELECT stage, date_to FROM admission_deadlines",
            "SELECT doc_name FROM admission_documents",
            "SELECT name, provided_to FROM dormitories",
            "SELECT question, answer FROM faq_entries",
        ):
            assert not _rejects_for(sql, role), f"{role} должен видеть: {sql}"

    # Расписание с датами — уже учебный контур, абитуриенту оно не положено.
    for role in ACADEMIC_ROLES:
        assert not _rejects_for(
            "SELECT lesson_date, subject_name FROM schedule_calendar", role)


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


def test_own_email_survives_the_personal_data_filter():
    # Тот же класс бага, что был у contacts/dormitories: student_email —
    # легитимная колонка my_profile (данные самого пользователя о самом
    # себе), но текстово содержит подстроку, похожую на forbidden-слово
    # только если её не переименовать. Явный список колонок должен
    # проходить, а не просто SELECT *.
    assert not _rejects_for(
        "SELECT last_name, student_email FROM my_profile", "student"
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


def test_performance_analytics_is_limited_to_the_deans_office():
    # Успеваемость и задолженности в разрезе подразделений — внутренние
    # сведения об учебном процессе. Обезличены (ФИО из этих представлений
    # убрала миграция 014), но студенту и преподавателю всё равно не
    # адресованы: преподавателю хватает своих предметов.
    for sql in ("SELECT faculty_name, avg(avg_score) FROM student_rankings "
                "GROUP BY faculty_name",
                "SELECT department_name, debts_count FROM academic_debts",
                "SELECT department_name, debtors_percent FROM department_debts",
                "SELECT group_name, debts_count FROM student_debts"):
        for role in ("applicant", "student", "teacher"):
            assert _rejects_for(sql, role), f"{role} не должен видеть: {sql}"
        assert not _rejects_for(sql, "deans-office")
        assert not _rejects_for(sql, "administration")


def test_no_role_can_reach_a_students_name():
    # Регламент (user_right.md): данные студентов выводятся исключительно в
    # агрегированном или обезличенном виде. Гарантию даёт СХЕМА, а не эта
    # проверка: колонок с ФИО в аналитических представлениях больше нет
    # (миграция 014), поэтому запрос отклонит уже сама БД.
    #
    # Здесь проверяется вторая половина: сырые таблицы с ФИО по-прежнему
    # закрыты whitelist'ом всем без исключения. Соответствие представлений
    # схеме проверяет test_data_integrity.test_no_view_exposes_student_names.
    for role in security.ALLOWED_TABLES_BY_ROLE:
        for sql in ("SELECT last_name, first_name FROM students",
                    "SELECT s.last_name FROM students s JOIN groups g "
                    "ON g.id = s.group_id",
                    "SELECT applicant_name FROM applications"):
            assert _rejects_for(sql, role), f"{role} не должен видеть: {sql}"


def test_applicant_sees_admission_data_and_nothing_else():
    # Абитуриент — самая узкая роль. Ему нужно выбрать, куда подавать
    # документы, и ровно это ему и доступно.
    for sql in (
        "SELECT program_name, budget_seats, paid_seats FROM programs_admission",
        "SELECT program_name, passing_score FROM passing_scores_view",
        "SELECT stage, date_to FROM admission_deadlines",
        "SELECT doc_name FROM admission_documents",
        "SELECT campaign_year, applications_count FROM admission_dynamics",
        "SELECT program_name, budget_percent FROM seats_ratio",
        "SELECT title, contact_phone FROM contacts",
    ):
        assert not _rejects_for(sql, "applicant"), f"абитуриент должен видеть: {sql}"

    # Учебного контура у него нет вовсе: он ещё не учится, и данных о нём в
    # assistant.students не существует.
    for sql in (
        "SELECT * FROM my_profile",
        "SELECT * FROM my_grades",
        "SELECT * FROM my_schedule",
        "SELECT lesson_date FROM schedule_calendar",
        "SELECT weekday FROM schedule",
        "SELECT * FROM group_curriculum",
        "SELECT * FROM room_availability",
        "SELECT full_name FROM teachers",
        "SELECT * FROM subject_performance",
        "SELECT * FROM students_summary",
    ):
        assert _rejects_for(sql, "applicant"), f"абитуриенту не положено: {sql}"


def test_anonymous_analytics_is_open_wider():
    # Обезличенная успеваемость по дисциплинам нужна преподавателю.
    assert _rejects_for("SELECT * FROM subject_performance", "student")
    assert not _rejects_for("SELECT * FROM subject_performance", "teacher")

    # Аудитории и учебные планы не содержат ничего личного — доступны всем,
    # кто внутри учебного процесса. Абитуриенту они не нужны: он выбирает
    # направление, а не ищет свободную аудиторию.
    for role in ACADEMIC_ROLES:
        assert not _rejects_for(
            "SELECT building, room_number FROM room_availability "
            "WHERE is_free AND weekday = 1 AND pair_number = 2", role)
        assert not _rejects_for("SELECT * FROM room_load", role)
        assert not _rejects_for("SELECT * FROM group_curriculum", role)


def test_admission_statistics_stay_with_administration():
    # Разбивка по дням подачи — внутренняя кухня приёмной комиссии: кто и в
    # какой день принёс документы. Остаётся за администрацией.
    sql = "SELECT * FROM applications_by_day"
    for role in ("applicant", "student", "teacher", "deans-office"):
        assert _rejects_for(sql, role)
    assert not _rejects_for(sql, "administration")


def test_admission_dynamics_is_open_to_the_applicant():
    # А вот динамика набора по годам — то, по чему абитуриент выбирает, куда
    # подавать документы: сколько подали и сколько зачислили на направление в
    # прошлые годы. Регламент отдаёт эту статистику именно ему. Данные
    # обезличены: счётчики и средние, ни ФИО, ни контактов.
    #
    # Доступна всем ролям, а не только абитуриенту: иначе вышла бы дыра
    # наоборот — абитуриент видит динамику набора, а декан нет.
    for role in security.ALLOWED_TABLES_BY_ROLE:
        assert not _rejects_for(
            "SELECT campaign_year, sum(applications_count) "
            "FROM admission_dynamics GROUP BY campaign_year", role
        ), f"{role} должен видеть динамику набора"


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
