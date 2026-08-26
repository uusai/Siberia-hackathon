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

from backend.app import auth, security  # noqa: E402


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
    assert auth.get_current_user(authorization=f"Bearer {token}") == {
        "username": "teacher",
        "role": "teacher",
    }


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
    print()
    print("Все проверки пройдены." if not failed else f"Провалено проверок: {failed}")
    sys.exit(1 if failed else 0)
