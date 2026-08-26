import os
import sys
import json
import re
import time
import urllib.request
import urllib.error

import psycopg

from . import db
from . import security


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
SYSTEM_PROMPT = "Отвечай пользователю на том же языке что он тебе пишет, при запросе пользователя по информации бд, не давай ему точные названия колонок или других данных, а переводи на язык пользователя и отвечай как человек."
API_URL = os.getenv("API_URL")

# Yandex API периодически отваливается: наблюдали SSL-таймаут на рукопожатии
# посреди рабочей сессии. Один такой сбой раньше уходил пользователю как
# «[Ошибка сети] ...» вместо ответа, поэтому временные сбои повторяем.
#
# Значения по умолчанию подобраны под бюджет фронтенда: он ждёт ответ от
# /chat не дольше REQUEST_TIMEOUT_MS = 90 с, а /chat вызывает модель ДВАЖДЫ
# (генерация SQL + объяснение результата). Худший случай на один вызов —
# (LLM_RETRIES + 1) * LLM_TIMEOUT_S + паузы = 2 * 20 + 1.5 ≈ 41.5 с,
# на два вызова ≈ 83 с, то есть укладываемся. Если поднимать таймаут или
# число повторов, синхронно поднимайте REQUEST_TIMEOUT_MS во фронтенде,
# иначе браузер отвалится раньше, чем бэкенд закончит повторять.
LLM_TIMEOUT_S = int(os.getenv("LLM_TIMEOUT_S", "20"))
LLM_RETRIES = int(os.getenv("LLM_RETRIES", "1"))
LLM_RETRY_PAUSE_S = float(os.getenv("LLM_RETRY_PAUSE_S", "1.5"))

LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.6"))

# Коды, при которых повтор осмыслен. Прочие 4xx (401 — плохой ключ, 400 —
# плохое тело) повторять бесполезно: со второй попытки они не исправятся.
_RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}

# Параметры подключения к БД живут в db.py; здесь они нужны только для
# приветственной строки CLI.
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")

# Fallback, подключаемый ТОЛЬКО если get_db_relationships() вернула пустую
# строку. В схеме assistant FK объявлены настоящими constraints, поэтому живой
# запрос всегда что-то находит и сюда исполнение не доходит — то есть список
# ниже не проверяется на практике и молча расходится с реальностью (так сюда и
# попала несуществующая administration.faculty_id). Сверяйте его с БД руками.
MANUAL_RELATIONSHIPS_FALLBACK = """Связи между таблицами (foreign keys) — заданы вручную, так как в БД они не объявлены как constraints:
- curriculum.program_id -> programs.id
- curriculum.subject_id -> subjects.id
- curriculum.teacher_id -> teachers.id
- groups.program_id -> programs.id
- students.group_id -> groups.id
- programs.faculty_id -> faculties.id
- teachers.department_id -> departments.id
- schedule.curriculum_id -> curriculum.id
- schedule.room_id -> rooms.id
- schedule.group_id -> groups.id
- enrollments.student_id -> students.id
- enrollments.curriculum_id -> curriculum.id
- grades.enrollment_id -> enrollments.id
- applications.campaign_id -> admission_campaigns.id
- admission_campaigns.program_id -> programs.id
- departments.faculty_id -> faculties.id"""


def build_model_uri() -> str:
    return f"gpt://{FOLDER_ID}/{MODEL_NAME}"


def get_db_schema() -> str:
    query = (
        "SELECT table_name, column_name, data_type "
        "FROM information_schema.columns "
        "WHERE table_schema = 'assistant' "
        "ORDER BY table_name, ordinal_position;"
    )
    try:
        rows = db.fetch_all("assistant", query)
    except db.DBUnavailable as e:
        return f"[Не удалось получить схему БД] {e}"
    except psycopg.Error as e:
        return f"[Ошибка получения схемы БД] {str(e).strip()}"

    tables: dict[str, list[str]] = {}
    for table, column, dtype in rows:
        tables.setdefault(table, []).append(f"{column} ({dtype})")

    if not tables:
        return "Схема БД пуста (нет таблиц в схеме assistant)."

    lines = ["Доступные таблицы и колонки в БД:"]
    for table in sorted(tables):
        lines.append(f"- {table}: {', '.join(tables[table])}")
    return "\n".join(lines)


def get_db_relationships() -> str:
    query = (
        "SELECT "
        "tc.table_name AS from_table, "
        "kcu.column_name AS from_column, "
        "ccu.table_name AS to_table, "
        "ccu.column_name AS to_column "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema "
        "JOIN information_schema.constraint_column_usage ccu "
        "  ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema "
        "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'assistant' "
        "ORDER BY tc.table_name;"
    )
    try:
        rows = db.fetch_all("assistant", query)
    except db.DBUnavailable as e:
        return f"[Не удалось получить связи БД] {e}"
    except psycopg.Error as e:
        return f"[Ошибка получения связей БД] {str(e).strip()}"

    lines = []
    for from_table, from_col, to_table, to_col in rows:
        lines.append(f"{from_table}.{from_col} -> {to_table}.{to_col}")

    if not lines:
        return ""
    return "Связи между таблицами (foreign keys):\n" + "\n".join(f"- {l}" for l in lines)


def build_sql_system_prompt() -> str:
    schema = get_db_schema()
    relationships = get_db_relationships()
    if not relationships:
        relationships = MANUAL_RELATIONSHIPS_FALLBACK

    # Правило про ЕГЭ добавляем, только если представление реально есть
    # в схеме. Иначе модель начнёт строить запросы к несуществующему
    # объекту и пользователь получит «relation does not exist» вместо
    # ответа. Схема тянется живьём, так что правило включится само, как
    # только применят backend/sql/002_ege_scores.sql — координировать
    # раскладку промпта и миграции руками не нужно.
    ege_rule = ""
    if "ege_scores_summary" in schema:
        ege_rule = (
            "\n7. Баллы ЕГЭ по отдельным предметам доступны через "
            "ege_scores_summary — там уже посчитаны avg_score, min_score, "
            "max_score и applications_count в разрезе subject, program_name, "
            "degree, faculty_id и campaign_year. Приём тот же, что с "
            "остальными представлениями: бери готовые агрегаты, а по "
            "нескольким строкам считай AVG(avg_score) или "
            "SUM(applications_count). Пример: SELECT subject, AVG(avg_score) "
            "FROM ege_scores_summary WHERE campaign_year = 2024 "
            "GROUP BY subject."
        )

    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"Ты — генератор SQL-запросов к базе данных университета. "
        f"Ниже реальная схема БД, используй ТОЛЬКО существующие таблицы и колонки.\n\n"
        f"{schema}\n\n"
        f"Используй ТОЛЬКО эти связи для построения JOIN. Не соединяй одну и ту же "
        f"таблицу саму с собой без явной необходимости.\n"
        f"{relationships}\n\n"
        f"ПРАВИЛА:\n"
        f"1. Генерируй ТОЛЬКО SELECT-запросы (или WITH ... SELECT).\n"
        f"2. Не используй таблицы вне схемы и не-modify данные.\n"
        f"3. Если нужно много строк — добавь LIMIT (например, LIMIT 50).\n"
        f"4. В ответе выдай ЕДИНСТВЕННУЮ вещь — SQL в блоке ```sql ... ```. "
        f"Никакого пояснительного текста до и после. Никакого другого текста.\n"
<<<<<<< HEAD
        f"5. Таблицы students, applications, grades и enrollments НАПРЯМУЮ "
        f"недоступны — они содержат персональные данные. Вместо них есть три "
        f"агрегированных представления, у каждого своя count-колонка:\n"
        f"   - students_summary (student_count)\n"
        f"   - applications_summary (applications_count)\n"
        f"   - grades_summary (grades_count)\n"
        f"   Строка представления — это уже целая группа, а не один человек, "
        f"поэтому COUNT(*) по ним считает группы, а не людей. Чтобы получить "
        f"итоговое число, суммируй count-колонку: "
        f"SELECT SUM(student_count) FROM students_summary. Для среза по условию "
        f"добавь WHERE или GROUP BY по нужной колонке и так же просуммируй, "
        f"например: SELECT SUM(student_count) FROM students_summary "
        f"WHERE status = 'учится'.\n"
        f"6. Точный список колонок бери из блока схемы выше — он получен из БД "
        f"на старте и всегда актуален. Смысловые уточнения, которые из названий "
        f"колонок не видны: degree — уровень образования ('бакалавриат', "
        f"'магистратура', 'специалитет'), funding — форма оплаты ('бюджет', "
        f"'контракт'), НЕ путай их. course — номер курса обучения "
        f"(1 = первокурсники, 2, 3, 4), а enrolled_year — календарный год "
        f"поступления (например, 2024), это НЕ курс: для вопросов про "
        f"'первокурсников' используй course = 1, а НЕ enrolled_year = 1. "
        f"program_name фильтруй через ILIKE '%текст%' для нечувствительности "
        f"к регистру. Пример: SELECT SUM(student_count) FROM students_summary "
=======
        f"5. Таблицы students, applications, applicants, grades и enrollments "
        f"НАПРЯМУЮ недоступны (содержат персональные данные). Вместо них используй "
        f"агрегированные представления students_summary, applications_summary, "
        f", grades_summary.\n"
        f"   Например, students_summary сгруппирован по group_id/status/funding/"
        f"enrolled_year и содержит колонку student_count (число студентов в каждой "
        f"группе). Чтобы получить ОБЩЕЕ количество студентов, просуммируй эту колонку "
        f"по всем группам: SELECT SUM(student_count) FROM students_summary. "
        f"Чтобы получить количество по конкретному условию (например, status = "
        f"'active'), добавь WHERE или GROUP BY по нужному полю и просуммируй "
        f"student_count: SELECT SUM(student_count) FROM students_summary WHERE "
        f"status = 'active'. Тот же приём (SUM соответствующей count-колонки) "
        f"применяй к applications_summary (applications_count), "
        f" (applicants_count) и grades_summary (grades_count)."
        f"6. students_summary теперь содержит: faculty_id, program_name, degree, "
        f"status, funding, enrolled_year, student_count. "
        f"degree — это уровень образования ('бакалавриат', 'магистратура'), "
        f"funding — форма оплаты ('бюджет', 'контракт'). НЕ путай их. "
        f"program_name — название направления, фильтруй через ILIKE '%текст%' "
        f"для нечувствительности к регистру. Пример: чтобы узнать сколько студентов "
        f"учится на юриспруденции на бакалавриате: "
        f"SELECT SUM(student_count) FROM students_summary "
>>>>>>> c750fddeb754b02bd021fbfca1a3898cdb6bcc6d
        f"WHERE program_name ILIKE '%юриспруденция%' AND degree = 'бакалавриат'."
        f"{ege_rule}"
    )


def extract_sql(text: str) -> str | None:
    text = text.strip()

    fence = re.search(r"```(?:sql)?\s*(.*?)\s*```", text, re.IGNORECASE | re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
        if candidate:
            return candidate

    match = re.search(r"(SELECT|WITH)\s.+", text, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(0).strip().rstrip(";")
    return None


def build_interpret_system_prompt() -> str:
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


def _call_gpt_once(
    system_text: str, user_text: str, temperature: float
) -> tuple[str | None, str, bool]:
    """Один вызов API.

    Возвращает (текст, описание_ошибки, стоит_ли_повторять). При успехе
    текст не None, при ошибке — None и заполненное описание.
    """
    messages = [
        {"role": "system", "text": system_text},
        {"role": "user", "text": user_text},
    ]

    body = {
        "modelUri": build_model_uri(),
        "completionOptions": {
            "stream": False,
            "temperature": temperature,
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

    # Порядок except важен: HTTPError наследует URLError, а URLError и
    # TimeoutError — OSError.
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        return None, f"[Ошибка API {e.code}] {detail}", e.code in _RETRYABLE_HTTP_CODES
    except urllib.error.URLError as e:
        return None, f"[Ошибка сети] {e.reason}", True
    except TimeoutError:
        return None, f"[Ошибка сети] превышен таймаут {LLM_TIMEOUT_S} с", True
    except OSError as e:
        return None, f"[Ошибка сети] {e}", True
    except UnicodeError as e:
        # Не-ASCII в ключе или заголовках: ошибка конфигурации, а не сбой
        # сети. Ловим до JSONDecodeError — оба наследуют ValueError.
        return None, f"[Ошибка запроса] {e}", False
    except json.JSONDecodeError as e:
        # Битый JSON — обычно обрезанный ответ, повтор осмыслен.
        return None, f"[Ошибка разбора ответа] {e}", True

    try:
        return data["result"]["alternatives"][0]["message"]["text"], "", False
    except (KeyError, IndexError, TypeError):
        # Ответ пришёл, но формы не той — повтор не поможет.
        return None, f"[Неожиданный ответ] {data}", False


def call_gpt(
    system_text: str, user_text: str, temperature: float = LLM_TEMPERATURE
) -> str:
    """Вызывает Yandex GPT, повторяя временные сбои.

    Возвращает текст ответа либо строку, начинающуюся с «[Ошибка» —
    на это опираются вторая фаза и фронтенд, поэтому исключения наружу
    не выпускаются.
    """
    last_error = "[Ошибка] вызов модели не выполнен"

    for attempt in range(LLM_RETRIES + 1):
        text, error, retryable = _call_gpt_once(system_text, user_text, temperature)
        if text is not None:
            return text

        last_error = error
        if not retryable or attempt == LLM_RETRIES:
            break

        print(
            f"[call_gpt] попытка {attempt + 1} из {LLM_RETRIES + 1} не удалась: "
            f"{error[:150]} — повторяю",
            file=sys.stderr,
        )
        time.sleep(LLM_RETRY_PAUSE_S * (attempt + 1))

    return last_error


def run_sql_through_security(sql: str) -> str:
    # CLI-путь (main()) отдаёт сюда непроверенный SQL, поэтому валидация
    # выполняется здесь, а в БД уходит уже проверенный текст:
    # execute_validated_sql() его повторно не валидирует.
    try:
        safe_sql = security.validate_sql(sql)
    except security.SQLSecurityError as e:
        return f"[Запрос отклонён проверкой безопасности] {e}"
    return security.execute_validated_sql(safe_sql)


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

        print("Агент генерирует SQL-запрос...")
        sql_reply = call_gpt(sql_system, user_input)

        sql = extract_sql(sql_reply)
        if not sql:
            print("Не удалось извлечь SQL-запрос из ответа агента.")
            print(f"Ответ агента: {sql_reply}")
            continue

        print(f"Сгенерирован SQL: {sql}")

        print("Проверка в security.py и выполнение в БД...")
        db_result = run_sql_through_security(sql)
        print(f"Результат БД:\n{db_result}")

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