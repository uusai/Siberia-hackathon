import os
import re
import subprocess
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

_load_dotenv()


ALLOWED_TABLES = {
    "faculties", "departments", "programs", "curricula", "curriculum",
    "subjects", "teachers", "administration", "rooms", "schedule",
    "groups", "admission_campaigns",
    "admissions_stats",
    "students_summary", "applications_summary",
    "grades_summary",
}

FORBIDDEN_COLUMNS = {"passport", "phone", "birth_date", "email"}

DEFAULT_LIMIT = 25
MAX_LIMIT = 200

STATEMENT_TIMEOUT_MS = int(os.getenv("SQL_STATEMENT_TIMEOUT_MS", "5000"))

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")


class SQLSecurityError(Exception):
    pass


def _normalize_sql(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = " ".join(sql.split())
    return sql.rstrip(";").strip()


def _assert_select_only(sql: str) -> None:
    lowered = sql.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise SQLSecurityError("Разрешены только SELECT-запросы.")

    forbidden = [
        "insert", "update", "delete", "drop", "alter", "truncate",
        "create", "replace", "grant", "revoke", "merge", "call",
        "exec", "execute", "commit", "rollback", "vacuum", "reindex",
        "copy", "lock", "comment",
    ]
    tokens = re.findall(r"[a-zA-Z_]+", lowered)
    for word in forbidden:
        if word in tokens:
            raise SQLSecurityError(
                f"Обнаружена запрещённая конструкция: '{word.upper()}'."
            )


def _assert_whitelist_tables(sql: str) -> None:
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


def _assert_no_forbidden_columns(sql: str) -> None:
    tokens = re.findall(r"[a-zA-Z_]+", sql.lower())
    found = set(tokens) & FORBIDDEN_COLUMNS
    if found:
        raise SQLSecurityError(
            f"Query contains forbidden personal-data fields: {', '.join(sorted(found))}"
        )


def _ensure_limit(sql: str) -> str:
    match = re.search(r"\blimit\s+(\d+)", sql, re.IGNORECASE)
    if not match:
        return f"{sql} LIMIT {DEFAULT_LIMIT}"
    requested = int(match.group(1))
    if requested > MAX_LIMIT:
        return re.sub(r"\blimit\s+\d+", f"LIMIT {MAX_LIMIT}", sql, flags=re.IGNORECASE)
    return sql


def _assert_single_statement(sql: str) -> None:
    statements = [s for s in sql.split(";") if s.strip()]
    if len(statements) > 1:
        raise SQLSecurityError(
            "Разрешён только один SQL-оператор за раз (обнаружен ';')."
        )


def validate_sql(sql: str) -> str:
    normalized = _normalize_sql(sql)
    if not normalized:
        raise SQLSecurityError("Пустой SQL-запрос.")

    _assert_select_only(normalized)
    _assert_single_statement(normalized)
    _assert_whitelist_tables(normalized)
    _assert_no_forbidden_columns(normalized)
    return _ensure_limit(normalized)


def execute_sql(sql: str) -> str:
    safe_sql = validate_sql(sql)

    env = os.environ.copy()
    env["PGPASSWORD"] = DB_PASSWORD
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
                "-t",
                "-A",
                "-F", "|",
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


_AUDIT_NULL_SENTINEL = "\x01__AUDIT_NULL__\x01"


def log_audit_entry(
    username: str | None,
    role: str | None,
    question: str,
    generated_sql: str | None,
    executed_sql: str | None,
    verdict: str,
    reject_reason: str | None,
    row_count: int | None,
    duration_ms: int,
    llm_ms: int,
    model: str,
) -> None:
    def enc(value) -> str:
        return _AUDIT_NULL_SENTINEL if value is None else str(value)

    variables = {
        "audit_username": enc(username),
        "audit_role": enc(role),
        "audit_question": enc(question),
        "audit_generated_sql": enc(generated_sql),
        "audit_executed_sql": enc(executed_sql),
        "audit_verdict": enc(verdict),
        "audit_reject_reason": enc(reject_reason),
        "audit_row_count": enc(row_count),
        "audit_duration_ms": enc(duration_ms),
        "audit_llm_ms": enc(llm_ms),
        "audit_model": enc(model),
        "audit_null": _AUDIT_NULL_SENTINEL,
    }

    query = (
        "INSERT INTO assistant.audit_log "
        "(username, role, question, generated_sql, executed_sql, verdict, "
        "reject_reason, row_count, duration_ms, llm_ms, model) VALUES ("
        "NULLIF(:'audit_username', :'audit_null'), "
        "NULLIF(:'audit_role', :'audit_null'), "
        "NULLIF(:'audit_question', :'audit_null'), "
        "NULLIF(:'audit_generated_sql', :'audit_null'), "
        "NULLIF(:'audit_executed_sql', :'audit_null'), "
        "NULLIF(:'audit_verdict', :'audit_null'), "
        "NULLIF(:'audit_reject_reason', :'audit_null'), "
        "NULLIF(:'audit_row_count', :'audit_null')::integer, "
        "NULLIF(:'audit_duration_ms', :'audit_null')::integer, "
        "NULLIF(:'audit_llm_ms', :'audit_null')::integer, "
        "NULLIF(:'audit_model', :'audit_null')"
        ");"
    )

    env = os.environ.copy()
    env["PGPASSWORD"] = DB_PASSWORD
    env["PGOPTIONS"] = "-c search_path=assistant"

    cmd = [
        "psql",
        "-h", DB_HOST,
        "-p", str(DB_PORT),
        "-U", DB_USER,
        "-d", DB_NAME,
        "-v", "ON_ERROR_STOP=1",
    ]
    for key, value in variables.items():
        cmd += ["-v", f"{key}={value}"]

    result = subprocess.run(
        cmd,
        input=query,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
