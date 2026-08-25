import os
import sys
import json
import re
import subprocess
import urllib.request
import urllib.error

import security  # модуль безопасности SQL-запросов


def load_dotenv(path: str = ".env") -> None:
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


load_dotenv()

API_KEY = os.getenv("API_KEY")
FOLDER_ID = os.getenv("FOLDER_ID")
MODEL_NAME = os.getenv("MODEL_NAME")
SYSTEM_PROMPT = os.getenv("AGENT_SYSTEM_PROMPT")
API_URL = os.getenv("API_URL")

# Параметры подключения к БД
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")


def build_model_uri() -> str:
    return f"gpt://{FOLDER_ID}/{MODEL_NAME}"


def get_db_schema() -> str:
    """Получает список таблиц и их колонок из БД через psql.

    Используется схема 'assistant' (в ней находятся заполненные таблицы).
    """
    query = (
        "SELECT table_name, column_name, data_type "
        "FROM information_schema.columns "
        "WHERE table_schema = 'assistant' "
        "ORDER BY table_name, ordinal_position;"
    )
    env = os.environ.copy()
    env["PGPASSWORD"] = DB_PASSWORD
    try:
        result = subprocess.run(
            [
                "psql",
                "-h", DB_HOST,
                "-p", str(DB_PORT),
                "-U", DB_USER,
                "-d", DB_NAME,
                "-t",          # только кортежи, без заголовков
                "-A",          # невыровненный вывод
                "-F", "|",     # разделитель полей
                "-c", query,
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return f"[Не удалось получить схему БД] {e}"

    if result.returncode != 0:
        return f"[Ошибка получения схемы БД] {result.stderr.strip()}"

    # Группируем колонки по таблицам
    tables: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        table, column, dtype = parts[0], parts[1], parts[2]
        tables.setdefault(table, []).append(f"{column} ({dtype})")

    if not tables:
        return "Схема БД пуста (нет таблиц в схеме public)."

    lines = ["Доступные таблицы и колонки в БД:"]
    for table in sorted(tables):
        lines.append(f"- {table}: {', '.join(tables[table])}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Фаза 1: генерация SQL
# ---------------------------------------------------------------------------

def build_sql_system_prompt() -> str:
    """Системный промпт для генерации SQL.

    Модель получает схему БД и должна выдать ТОЛЬКО SELECT-запрос.
    """
    schema = get_db_schema()
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"Ты — генератор SQL-запросов к базе данных университета. "
        f"Ниже реальная схема БД, используй ТОЛЬКО существующие таблицы и колонки.\n\n"
        f"{schema}\n\n"
        f"ПРАВИЛА:\n"
        f"1. Генерируй ТОЛЬКО SELECT-запросы (или WITH ... SELECT).\n"
        f"2. Не используй таблицы вне схемы и не-modify данные.\n"
        f"3. Если нужно много строк — добавь LIMIT (например, LIMIT 50).\n"
        f"4. В ответе выдай ЕДИНСТВЕННУЮ вещь — SQL в блоке ```sql ... ```. "
        f"Никакого пояснительного текста до и после. Никакого другого текста.\n"
        f"5. Таблицы students, applications, applicants, grades и enrollments "
        f"НАПРЯМУЮ недоступны (содержат персональные данные). Вместо них используй "
        f"агрегированные представления students_summary, applications_summary, "
        f"applicants_summary, grades_summary.\n"
        f"   Например, students_summary сгруппирован по group_id/status/funding/"
        f"enrolled_year и содержит колонку student_count (число студентов в каждой "
        f"группе). Чтобы получить ОБЩЕЕ количество студентов, просуммируй эту колонку "
        f"по всем группам: SELECT SUM(student_count) FROM students_summary. "
        f"Чтобы получить количество по конкретному условию (например, status = "
        f"'active'), добавь WHERE или GROUP BY по нужному полю и просуммируй "
        f"student_count: SELECT SUM(student_count) FROM students_summary WHERE "
        f"status = 'active'. Тот же приём (SUM соответствующей count-колонки) "
        f"применяй к applications_summary (applications_count), "
        f"applicants_summary (applicants_count) и grades_summary (grades_count)."
    )


def extract_sql(text: str) -> str | None:
    """Извлекает SQL-запрос из ответа модели.

    Поддерживаются варианты:
    - чистый SQL без обёртки
    - SQL внутри блока ```sql ... ```
    """
    text = text.strip()

    # SQL внутри markdown-блока (приоритет)
    fence = re.search(r"```(?:sql)?\s*(.*?)\s*```", text, re.IGNORECASE | re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
        if candidate:
            return candidate

    # Иначе считаем, что весь ответ — это SQL (убираем лишний мусор)
    match = re.search(r"(SELECT|WITH)\s.+", text, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(0).strip().rstrip(";")
    return None


# ---------------------------------------------------------------------------
# Фаза 2: объяснение результата БД на человеческом языке
# ---------------------------------------------------------------------------

def build_interpret_system_prompt() -> str:
    """Системный промпт для интерпретации результата БД.

    Модель НЕ имеет схемы и не генерирует SQL — только объясняет данные.
    """
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"Ты — помощник, который объясняет результаты запросов к базе данных "
        f"обычному человеку. Тебе дадут SQL-запрос и сырые данные из БД. "
        f"Твоя задача — на основе этих данных сформулировать понятный, дружелюбный "
        f"ответ на русском языке, отвечающий на исходный вопрос пользователя.\n\n"
        f"ПРАВИЛА:\n"
        f"1. НЕ генерируй и не выдавай никаких SQL-запросов.\n"
        f"2. Если данных нет (пустой результат) — честно скажи, что ничего не найдено.\n"
        f"3. Если вместо данных пришла ошибка безопасности или БД — объясни её "
        f"простыми словами и предложи, как переформулировать вопрос.\n"
        f"4. Ответ должен быть на русском, без технического мусора."
    )


# ---------------------------------------------------------------------------
# Обращение к Yandex GPT
# ---------------------------------------------------------------------------

def call_gpt(system_text: str, user_text: str) -> str:
    """Делает один вызов к API Yandex GPT и возвращает текст ответа."""
    messages = [
        {"role": "system", "text": system_text},
        {"role": "user", "text": user_text},
    ]

    body = {
        "modelUri": build_model_uri(),
        "completionOptions": {
            "stream": False,
            "temperature": 0.6,
            "maxTokens": 2000,
        },
        "messages": messages,
    }

    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Api-Key {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return f"[Ошибка API {e.code}] {e.read().decode('utf-8', errors='replace')}"
    except urllib.error.URLError as e:
        return f"[Ошибка сети] {e.reason}"
    except (KeyError, IndexError, ValueError) as e:
        return f"[Ошибка разбора ответа] {e}"

    try:
        return data["result"]["alternatives"][0]["message"]["text"]
    except (KeyError, IndexError) as e:
        return f"[Неожиданный ответ] {data}"


def run_sql_through_security(sql: str) -> str:
    """Передаёт SQL в security.py, возвращает результат БД или причину отказа."""
    try:
        return security.execute_sql(sql)
    except security.SQLSecurityError as e:
        return f"[Запрос отклонён проверкой безопасности] {e}"


# ---------------------------------------------------------------------------
# Основной цикл
# ---------------------------------------------------------------------------

def main() -> None:
    global FOLDER_ID
    if not FOLDER_ID:
        FOLDER_ID = input("Введите YANDEX FOLDER ID: ").strip()
        if not FOLDER_ID:
            print("Folder ID обязателен для работы Yandex GPT. Выход.")
            sys.exit(1)

    print("=" * 50)
    print("  ИИ-агент на базе Yandex GPT")
    print(f"  Модель: {MODEL_NAME}  |  Folder: {FOLDER_ID}")
    print(f"  БД: {DB_NAME} @ {DB_HOST}:{DB_PORT}")
    print("  Введите 'exit' или 'quit' для выхода")
    print("=" * 50)

    # Системные промпты формируются один раз (схема БД кешируется)
    sql_system = build_sql_system_prompt()
    interpret_system = build_interpret_system_prompt()

    print("Система готова. Жду ваши вопросы.")
    print("=" * 50)

    while True:
        try:
            user_input = input("\nВы: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nЗавершение работы.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "выход"):
            print("До свидания!")
            break

        # --- Фаза 1: генерация SQL на основе вопроса пользователя ---
        print("Агент генерирует SQL-запрос...")
        sql_reply = call_gpt(sql_system, user_input)

        sql = extract_sql(sql_reply)
        if not sql:
            print("Не удалось извлечь SQL-запрос из ответа агента.")
            print(f"Ответ агента: {sql_reply}")
            continue

        print(f"Сгенерирован SQL: {sql}")

        # --- Проверка безопасности + отправка в БД ---
        print("Проверка в security.py и выполнение в БД...")
        db_result = run_sql_through_security(sql)
        print(f"Результат БД:\n{db_result}")

        # --- Фаза 2: отдаём результат БД нейронке для расшифровки ---
        interpret_input = (
            f"Исходный вопрос пользователя:\n{user_input}\n\n"
            f"Выполненный SQL-запрос:\n{sql}\n\n"
            f"Результат из базы данных:\n{db_result}"
        )
        print("Агент расшифровывает результат на человеческий язык...")
        human_answer = call_gpt(interpret_system, interpret_input)
        print(f"\nОтвет:\n{human_answer}")


if __name__ == "__main__":
    main()