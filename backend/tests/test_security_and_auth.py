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
    assert _rejects("SELECT u.* FROM faculties f, auth.users u")
    assert _rejects("SELECT s.* FROM faculties f, assistant.students s")
    assert _rejects("SELECT * FROM faculties, students")


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
        "SELECT SUM(s.student_count) FROM students_summary s "
        "JOIN programs p ON p.faculty_id = s.faculty_id"
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
    for role in security.ALLOWED_TABLES_BY_ROLE:
        assert not _rejects_for("SELECT * FROM faculties", role)
        assert not _rejects_for(
            "SELECT weekday, pair_number FROM schedule", role)


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
    assert _rejects_for("SELECT * FROM faculties", "root")
    assert security.allowed_tables_for("root") == set()


def test_set_config_is_blocked():
    # Через set_config можно было бы подменить app.student_id и прочитать
    # чужой профиль — это обход всей схемы личных данных.
    assert _rejects_for(
        "SELECT set_config('app.student_id','1',true) FROM my_profile", "student")
    assert _rejects_for(
        "SELECT current_setting('app.student_id') FROM my_profile", "student")
    assert _rejects_for("SELECT pg_sleep(10) FROM faculties", "student")


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
