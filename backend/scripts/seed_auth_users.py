"""Разовый скрипт: заводит демо-учётки в auth.users.

НЕ импортируется приложением (ни main.py, ни ai_agent.py, ни security.py).
Запускать руками:

    python backend/scripts/seed_auth_users.py

Перед первым запуском применить backend/sql/001_auth_schema.sql.

Подключение — через psycopg, как и остальное приложение (backend/app/db.py),
на тех же переменных окружения DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD.
Пул здесь не нужен: скрипт делает несколько вставок и завершается.
"""

import os
import sys
import time

import bcrypt
import psycopg

# Должно совпадать с backend/app/auth.py: там та же обрезка до 72 байт.
# Если поменяете там — поменяйте и здесь, иначе сид-хеши перестанут сходиться
# с проверкой при логине.
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8")[:_BCRYPT_MAX_BYTES], bcrypt.gensalt()
    ).decode("utf-8")

# ВНИМАНИЕ: демо-учётки, у которых пароль СОВПАДАЕТ С ЛОГИНОМ.
# Годится только для хакатонского стенда.
DEMO_USERS = [
    # Абитуриент. Требует применённой миграции 015_applicant_role.sql —
    # без неё CHECK на auth.users.role отвергнет эту строку.
    ("applicant", "applicant"),
    ("student", "student"),
    ("teacher", "teacher"),
    ("deans-office", "deans-office"),
    ("administration", "administration"),
]


def _load_dotenv(path: str = ".env") -> None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        pass


def _link_demo_people(conninfo: str) -> int:
    """Привязывает демо-учётки к реальным людям в assistant.

    Без привязки личные вьюхи my_* не вернут ничего: они фильтруются по
    app.student_id, а брать его неоткуда. Выбираем не первого попавшегося,
    а того, у кого больше всего оценок — чтобы на демо было что показать.
    Запрос идемпотентен: проставляем связь только там, где её ещё нет.
    """
    picks = [
        (
            "student",
            "student_id",
            "SELECT s.id FROM assistant.students s "
            "JOIN assistant.enrollments e ON e.student_id = s.id "
            "JOIN assistant.grades g ON g.enrollment_id = e.id "
            "WHERE s.status = 'учится' "
            "GROUP BY s.id ORDER BY count(*) DESC, s.id LIMIT 1",
        ),
        (
            "teacher",
            "teacher_id",
            "SELECT t.id FROM assistant.teachers t "
            "JOIN assistant.curriculum c ON c.teacher_id = t.id "
            "GROUP BY t.id ORDER BY count(*) DESC, t.id LIMIT 1",
        ),
    ]

    print("\nПривязываю демо-учётки к людям в базе:")
    failures = 0
    for username, column, pick_sql in picks:
        try:
            with psycopg.connect(conninfo) as conn:
                with conn.cursor() as cur:
                    cur.execute(pick_sql)
                    row = cur.fetchone()
                    if row is None:
                        print(f"  [--]   {username}: подходящих записей нет, пропускаю")
                        continue
                    person_id = row[0]
                    cur.execute(
                        f"UPDATE auth.users SET {column} = %s "
                        f"WHERE username = %s AND {column} IS NULL",
                        (person_id, username),
                    )
                    changed = cur.rowcount
            note = "привязан" if changed else "уже был привязан"
            print(f"  [OK]   {username} -> {column}={person_id} ({note})")
        except psycopg.Error as e:
            print(f"  [FAIL] {username}: {str(e).strip()}", file=sys.stderr)
            failures += 1
    return failures


def seed() -> int:
    _load_dotenv()

    db_host = os.getenv("DB_HOST")
    db_port = os.getenv("DB_PORT")
    db_name = os.getenv("DB_NAME")
    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")

    if not all([db_host, db_port, db_name, db_user, db_password]):
        print(
            "Не заданы DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD "
            "(см. .env.example).",
            file=sys.stderr,
        )
        return 1

    statement = (
        "INSERT INTO auth.users (username, password_hash, role) "
        "VALUES (%s, %s, %s) "
        "ON CONFLICT (username) DO NOTHING"
    )

    conninfo = psycopg.conninfo.make_conninfo(
        host=db_host, port=db_port, dbname=db_name,
        user=db_user, password=db_password,
        connect_timeout=10, client_encoding="UTF8",
    )

    print("Создаю демо-учётки в auth.users:")

    failures = 0
    for username, password in DEMO_USERS:
        # Роль совпадает с логином — см. CHECK-констрейнт в 001_auth_schema.sql.
        role = username
        # Пароль хешируется здесь и в открытом виде никуда не пишется —
        # ни в лог, ни в запрос.
        password_hash = hash_password(password)

        # Стенд рвёт часть соединений — пробуем несколько раз.
        for attempt in range(3):
            try:
                with psycopg.connect(conninfo) as conn:
                    with conn.cursor() as cur:
                        cur.execute(statement, (username, password_hash, role))
                print(f"  [OK]   {username} (роль: {role})")
                break
            except psycopg.Error as e:
                if attempt == 2:
                    print(f"  [FAIL] {username}: {str(e).strip()}", file=sys.stderr)
                    failures += 1
                else:
                    time.sleep(1)

    failures += _link_demo_people(conninfo)

    print()
    print("=" * 70)
    print("  ВНИМАНИЕ: это ДЕМО-учётки — пароль совпадает с логином.")
    print("  Годятся только для хакатонского стенда. Перед любым реальным")
    print("  развёртыванием удалите их и заведите нормальные пароли.")
    print("=" * 70)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(seed())
