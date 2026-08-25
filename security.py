"""Модуль безопасности для выполнения SQL-запросов.

Политика:
- Разрешены ТОЛЬКО SELECT-запросы.
- Имена таблиц должны входить в whitelist (разрешённые таблицы).
- К запросу автоматически добавляется LIMIT (если не указан).
- Перед выполнением применяется statement_timeout (защита от долгих запросов).
"""

import os
import re
import subprocess

# Whitelist разрешённых таблиц/представлений (схема 'assistant' БД vesna-db9)
ALLOWED_TABLES = {
    # открытые справочники
    "faculties", "departments", "programs", "curricula", "curriculum",
    "subjects", "teachers", "administration", "rooms", "schedule",
    "groups", "admission_campaigns",
    "admissions_stats",
    "students_summary", "applications_summary", "applicants_summary",
}

# Максимальное число возвращаемых строк (если LIMIT не указан)
DEFAULT_LIMIT = 5

# Таймаут выполнения запроса в миллисекундах (например, 5000 = 5 сек)
STATEMENT_TIMEOUT_MS = int(os.getenv("SQL_STATEMENT_TIMEOUT_MS", "5000"))

# Параметры подключения к БД
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")


class SQLSecurityError(Exception):
    """Исключение при нарушении политики безопасности SQL."""


def _normalize_sql(sql: str) -> str:
    """Убирает комментарии и лишние пробелы, приводит к единому виду."""
    # Удаляем строчные -- комментарии
    sql = re.sub(r"--[^\n]*", " ", sql)
    # Удаляем блочные /* */ комментарии
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return " ".join(sql.split())


def _assert_select_only(sql: str) -> None:
    """Проверяет, что запрос начинается со SELECT/WITH и не содержит
    запрещённых конструкций (INSERT/UPDATE/DELETE/DROP и т.п.)."""
    lowered = sql.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise SQLSecurityError("Разрешены только SELECT-запросы.")

    forbidden = [
        "insert", "update", "delete", "drop", "alter", "truncate",
        "create", "replace", "grant", "revoke", "merge", "call",
        "exec", "execute", "commit", "rollback", "vacuum", "reindex",
        "copy", "lock", "comment",
    ]
    # Ищем запрещённые слова как отдельные токены
    tokens = re.findall(r"[a-zA-Z_]+", lowered)
    for word in forbidden:
        if word in tokens:
            raise SQLSecurityError(
                f"Обнаружена запрещённая конструкция: '{word.upper()}'."
            )


def _assert_whitelist_tables(sql: str) -> None:
    """Проверяет, что все упомянутые в FROM/JOIN таблицы входят в whitelist."""
    # Находим все идентификаторы после FROM / JOIN
    pattern = re.compile(
        r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)(?:\s+(?:as\s+)?[a-zA-Z_][a-zA-Z0-9_]*)?",
        re.IGNORECASE,
    )
    found = pattern.findall(sql)
    if not found:
        raise SQLSecurityError("Не удалось определить таблицу в запросе.")

    for table in found:
        if table.lower() not in ALLOWED_TABLES:
            raise SQLSecurityError(
                f"Таблица '{table}' не входит в список разрешённых."
            )


def _ensure_limit(sql: str) -> str:
    """Добавляет LIMIT, если он не указан явно."""
    if re.search(r"\blimit\s+\d+", sql, re.IGNORECASE):
        return sql
    return f"{sql} LIMIT {DEFAULT_LIMIT}"


def _assert_single_statement(sql: str) -> None:
    """Запрещает несколько инструкций в одном запросе (через ';').

    После нормализации разделитель ';' сохраняется. Допускаем
    не более одного содержательного оператора (завершающий ';' игнорируем).
    """
    statements = [s for s in sql.split(";") if s.strip()]
    if len(statements) > 1:
        raise SQLSecurityError(
            "Разрешён только один SQL-оператор за раз (обнаружен ';')."
        )


def validate_sql(sql: str) -> str:
    """Проверяет SQL-запрос по политике безопасности и возвращает
    безопасную (нормализованную) версию с добавленным LIMIT.

    Выбрасывает SQLSecurityError при нарушении политики.
    """
    normalized = _normalize_sql(sql)
    if not normalized:
        raise SQLSecurityError("Пустой SQL-запрос.")

    _assert_select_only(normalized)
    _assert_single_statement(normalized)
    _assert_whitelist_tables(normalized)
    return _ensure_limit(normalized)


def execute_sql(sql: str) -> str:
    """Валидирует и выполняет SELECT-запрос через psql с statement_timeout.

    Таймаут применяется через PGOPTIONS (передаётся серверу при подключении),
    чтобы не смешивать SET с самим запросом в одном -c (иначе psql выводит
    только подтверждение SET и теряет результат SELECT).

    Возвращает текстовый результат выполнения запроса.
    """
    safe_sql = validate_sql(sql)

    env = os.environ.copy()
    env["PGPASSWORD"] = DB_PASSWORD
    # statement_timeout задаётся в миллисекундах; search_path=assistant,
    # чтобы запросы без явного указания схемы шли в схему assistant (с данными).
    env["PGOPTIONS"] = (
        f"-c statement_timeout={STATEMENT_TIMEOUT_MS} -c search_path=assistant"
    )

    try:
        result = subprocess.run(
            [
                "psql",
                "-h", DB_HOST,
                "-p", str(DB_PORT),
                "-U", DB_USER,
                "-d", DB_NAME,
                "-t",          # без заголовков
                "-A",          # невыровненный вывод
                "-F", "|",     # разделитель полей
                "-c", safe_sql,
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
    except FileNotFoundError as e:
        return f"[Ошибка] psql не найден: {e}"
    except subprocess.TimeoutExpired:
        return "[Ошибка] Превышен таймаут выполнения процесса psql."

    if result.returncode != 0:
        return f"[Ошибка БД] {result.stderr.strip()}"

    return result.stdout.strip()
