import os
import re

import psycopg

from . import db


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


# Whitelist таблиц и представлений схемы 'assistant', доступных чат-агенту.
#
# НИКОГДА не добавлять сюда auth.users (учётные записи и bcrypt-хеши паролей).
# Через чат-путь эта таблица закрыта тремя независимыми слоями:
#   1) чат-путь ходит в БД через пул со своим search_path=assistant (см.
#      db.get_pool), поэтому неквалифицированное имя `users` не резолвится
#      вообще — в схеме assistant таблицы users не существует;
#   2) этот whitelist: в нём нет ни `users`, ни любой другой таблицы схемы auth;
#   3) _assert_whitelist_tables(): отклоняет ссылки со схемой (auth.users,
#      public.users), в двойных кавычках и перечисленные через запятую в FROM.
# Дополнительно схема auth структурно невидима для ai_agent.get_db_schema() и
# get_db_relationships() — обе фильтруют table_schema = 'assistant', так что
# auth.users не попадает даже в промпт модели.
# Отдельно про ege_scores: сырая таблица сюда НЕ добавляется никогда.
# В ней есть application_id — внешний ключ в applications, где лежат ФИО,
# паспорт, телефон и почта абитуриента. Даже без JOIN'а сам факт «заявление
# №N набрало 98 по химии» вместе с любой другой утечкой по applications
# позволяет опознать человека. Агенту доступно только агрегированное
# ege_scores_summary — тот же приём, что со students_summary.

# Справочники и расписание: ничего личного, доступны любой роли.
#
# ЧЕГО ЗДЕСЬ НАМЕРЕННО НЕТ: faculties, programs и departments.
#
# Это таблицы демонстрационного контура: пять выдуманных факультетов вроде
# «Факультета информационных технологий» и тринадцать направлений при них. На
# них завязаны студенты, группы и расписание, поэтому таблицы остаются в базе
# и работают — но модели они не показываются.
#
# Причина конкретная: на вопрос «сколько факультетов в ИГУ» модель уверенно
# отвечала «пять», взяв ответ из faculties, тогда как приём в 2026 году ведут
# 15 подразделений (assistant.university_units). Пока обе таблицы видны, выбор
# между ними — лотерея, а проигрыш выглядит как уверенно названная неправда о
# самом университете.
#
# Ничего не теряется: название факультета, направления и кафедры есть текстом
# в аналитических представлениях (faculty_name, program_name,
# department_name), а официальный каталог — в university_units и edu_programs.
_BASE_TABLES = {
    "subjects", "teachers", "rooms", "groups", "schedule",
    "admission_campaigns",
}

# Официальный справочник ИГУ (миграции 006/008) и надстройка расписания с
# датами (007). Всё это — публично опубликованные сведения: структура вуза,
# перечни вступительных испытаний, минимальные баллы, сроки приёма, стоимость
# обучения, адреса и телефоны приёмной комиссии. Ограничивать их по ролям
# незачем: абитуриент, который спрашивает «что сдавать на юриспруденцию»,
# заходит под той же учёткой, что и студент.
#
# ПРО КОЛОНКИ КОНТАКТОВ. В contacts, dormitories и university_units телефон и
# почта называются contact_phone и contact_email, а не phone и email. Это не
# косметика: _assert_no_forbidden_columns() ниже ищет FORBIDDEN_COLUMNS во
# ВСЁМ тексте запроса, разбивая его на токены [a-zA-Z_]+. Колонка с именем
# phone сделала бы вопрос «телефон приёмной комиссии» неотвечаемым — запрос
# отклонялся бы проверкой. Токены contact_phone и contact_email в чёрный
# список не входят, а сам список не ослабляется: он по-прежнему закрывает
# паспорт, телефон, почту и дату рождения студентов и абитуриентов.
_OFFICIAL_REFERENCE_TABLES = {
    # справочник и его источники
    "data_sources", "university_units", "edu_programs",
    # приёмная кампания
    "entrance_exams", "program_exams", "minimum_scores", "passing_scores",
    "enrollment_places", "tuition_fees", "admission_deadlines",
    "admission_documents", "benefits_quotas",
    # инфраструктура и справочная информация
    "dormitories", "campus_buildings", "contacts", "faq_entries",
    # денормализованные представления — именно ими и должна пользоваться
    # модель вместо самостоятельной сборки JOIN'ов
    "programs_admission", "program_exam_sets", "minimum_scores_view",
    "passing_scores_view", "data_status_summary",
    # расписание с датами и временем
    "pair_times", "academic_terms", "lesson_occurrences", "schedule_calendar",
}

# Качество расписания — рабочий инструмент деканата, а не ответ студенту.
# Студенту незачем знать, что в его расписании нашлось пересечение: ему нужно
# расписание. Деканату — наоборот.
_SCHEDULE_QUALITY_TABLES = {
    "schedule_conflicts_group", "schedule_conflicts_teacher",
    "schedule_conflicts_room", "schedule_issues",
}

# Аналитика учебного процесса (миграция 009). Разложена по ролям по одному
# признаку: видно ли из представления КОНКРЕТНОГО ЧЕЛОВЕКА.

# Ничего личного: аудитории, учебные планы, количество мест. Любая роль.
_ANALYTICS_PUBLIC = {
    "room_load", "room_availability", "group_curriculum", "seats_ratio",
}

# Обезличенная успеваемость по дисциплинам: распределение оценок, доля
# сдавших с первой попытки. Ни ФИО, ни идентификаторов студентов — поэтому
# доступно преподавателю, которому надо понимать, как идёт его предмет.
_ANALYTICS_TEACHING = {"subject_performance"}

# ПОИМЁННАЯ успеваемость и нагрузка. Только деканат и администрация.
#
# student_rankings и academic_debts показывают ФИО студентов — это осознанное
# расширение прав деканата, который по должности работает с успеваемостью
# поимённо. Паспорта, телефона, почты и даты рождения в них нет: эти поля
# остались в FORBIDDEN_COLUMNS и в самих представлениях отсутствуют.
# Студенту и преподавателю эти объекты не выдаются.
_ANALYTICS_DEANS = {
    "student_rankings", "academic_debts", "department_performance",
    "department_workload", "teacher_semester_load", "funding_share",
}

# Приёмная кампания в разрезе дней и годов. Обезличено, но это внутренняя
# статистика вуза, поэтому только администрации — как и applications_summary.
_ANALYTICS_ADMIN = {"applications_by_day", "admission_dynamics"}

# Личные вьюхи студента. Они сами фильтруются по app.student_id, который
# бэкенд выставляет из проверенного токена (см. sql/003_role_access.sql):
# «свои данные» здесь — свойство самой вьюхи, а не обещание модели.
STUDENT_PERSONAL_TABLES = {"my_profile", "my_grades", "my_schedule"}

# Личные вьюхи преподавателя: что он ведёт, его расписание и успеваемость
# по его предметам. Фильтруются по app.teacher_id — см. 004_teacher_views.sql.
# Ролям выше (деканат, администрация) не выдаются: за ними не стоит
# конкретный преподаватель, и такой запрос вернул бы им пустоту.
TEACHER_PERSONAL_TABLES = {
    "my_teaching", "my_teaching_schedule", "my_students_performance",
}

# Доступ накопительный: каждая следующая роль видит всё, что предыдущая.
# Официальный справочник доступен всем — это опубликованные сведения.
ALLOWED_TABLES_BY_ROLE: dict[str, set[str]] = {
    "student": _BASE_TABLES | _OFFICIAL_REFERENCE_TABLES | _ANALYTICS_PUBLIC
    | STUDENT_PERSONAL_TABLES,
    "teacher": _BASE_TABLES | _OFFICIAL_REFERENCE_TABLES | _ANALYTICS_PUBLIC
    | _ANALYTICS_TEACHING | TEACHER_PERSONAL_TABLES | {
        "curriculum", "grades_summary",
    },
    "deans-office": _BASE_TABLES | _OFFICIAL_REFERENCE_TABLES | _ANALYTICS_PUBLIC
    | _ANALYTICS_TEACHING | _ANALYTICS_DEANS | _SCHEDULE_QUALITY_TABLES | {
        "curriculum", "grades_summary", "students_summary",
    },
    "administration": _BASE_TABLES | _OFFICIAL_REFERENCE_TABLES | _ANALYTICS_PUBLIC
    | _ANALYTICS_TEACHING | _ANALYTICS_DEANS | _ANALYTICS_ADMIN
    | _SCHEDULE_QUALITY_TABLES | {
        "curriculum", "grades_summary", "students_summary",
        "applications_summary", "ege_scores_summary",
    },
}

# Объединение по всем ролям — для мест, где роли нет: CLI-режим
# ai_agent.main() и проверка «существует ли такая таблица вообще».
ALLOWED_TABLES = set().union(*ALLOWED_TABLES_BY_ROLE.values())


def allowed_tables_for(role: str | None) -> set[str]:
    """Набор таблиц для роли.

    role=None — объединение всех (CLI). Неизвестная роль не получает
    ничего: неверный или подделанный role в токене должен приводить к
    отказу, а не к полному доступу.
    """
    if role is None:
        return ALLOWED_TABLES
    return ALLOWED_TABLES_BY_ROLE.get(role, set())


FORBIDDEN_COLUMNS = {"passport", "phone", "birth_date", "email"}

# Функции, которыми можно обойти ограничение личных данных или нагрузить
# сервер. set_config и current_setting здесь критичны: через них
# сгенерированный моделью запрос мог бы подменить app.student_id и
# прочитать чужой профиль — то есть обойти всю схему личных вьюх.
FORBIDDEN_FUNCTIONS = {
    "set_config", "current_setting",
    "pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir",
    "lo_import", "lo_export", "dblink", "dblink_exec",
    "query_to_xml", "pg_stat_file", "pg_terminate_backend",
}

DEFAULT_LIMIT = 25
MAX_LIMIT = 200

# Параметры подключения и statement_timeout теперь живут в db.py — здесь
# остаётся только политика безопасности SQL.


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

    banned = set(tokens) & FORBIDDEN_FUNCTIONS
    if banned:
        raise SQLSecurityError(
            f"Запрещённая функция: {', '.join(sorted(banned))}."
        )

    # Неподставленный шаблон вида {user_input}. Появляется, когда просят
    # «подставь мой текст в WHERE как есть»: модель послушно оставляет
    # заготовку. До БД такое доезжать не должно — там оно превращается в
    # синтаксическую ошибку, а пользователю показывается невнятный отказ
    # сервера вместо понятного объяснения.
    placeholder = re.search(r"\{[^{}]*\}", sql)
    if placeholder:
        raise SQLSecurityError(
            f"В запросе осталась неподставленная заготовка "
            f"'{placeholder.group(0)}'. Подстановка произвольного текста в "
            f"запрос не выполняется."
        )


# Токен: идентификатор (возможно составной через точку и/или в двойных
# кавычках), скобка, запятая либо любой другой непробельный кусок.
_SQL_TOKEN_RE = re.compile(
    r'"[^"]*"(?:\s*\.\s*(?:"[^"]*"|[A-Za-z_][A-Za-z0-9_$]*))*'
    r'|[A-Za-z_][A-Za-z0-9_$]*(?:\s*\.\s*(?:"[^"]*"|[A-Za-z_][A-Za-z0-9_$]*))*'
    r'|[(),]'
    r'|[^\s(),]+'
)

# Слова, после которых перечисление таблиц в FROM заведомо закончилось.
_TABLE_LIST_TERMINATORS = {
    "where", "group", "order", "having", "limit", "offset", "union",
    "intersect", "except", "on", "using", "join", "inner", "left",
    "right", "full", "cross", "natural", "window", "fetch", "select",
    "with", "and", "or",
}


# Функции, у которых FROM — часть СОБСТВЕННОГО синтаксиса, а не начало
# перечисления таблиц: EXTRACT(YEAR FROM now()), SUBSTRING(s FROM 2 FOR 3),
# TRIM(BOTH ' ' FROM s), POSITION(a IN b), OVERLAY(s PLACING x FROM 2).
#
# Без этого списка проверка видела «FROM now()» и требовала таблицу с именем
# NOW — то есть отклоняла совершенно нормальный запрос «за последние 5 лет».
# Ловилось это только вживую: рукописный SQL в тестах EXTRACT не использовал.
_FROM_INSIDE_FUNCTIONS = {
    "extract", "substring", "trim", "overlay", "position",
}


def _extract_table_refs(sql: str) -> list[str]:
    """Возвращает все ссылки на таблицы в позиции FROM/JOIN.

    В отличие от простого `FROM\\s+(\\w+)`, учитывает:
    - перечисление через запятую (FROM a, b): старый regex видел только `a`,
      из-за чего `FROM faculties f, auth.users u` проходила whitelist;
    - схему перед именем (auth.users): возвращается вместе с точкой, чтобы
      вызывающий код мог отклонить такую ссылку явно, а не полагаться на то,
      что regex «случайно» остановится перед точкой;
    - идентификаторы в двойных кавычках ("students").

    Подзапросы (FROM (SELECT ...)) пропускаются: их собственный FROM попадает
    в разбор отдельно и проверяется на общих основаниях.

    Известное ограничение (поведение не изменилось): имя CTE из
    `WITH x AS (...) SELECT * FROM x` в whitelist не входит и будет отклонено.
    Отказ в безопасную сторону — это осознанно.
    """
    # Строковые литералы вырезаем, иначе ILIKE '% from students %' дал бы
    # ложное срабатывание.
    sanitized = re.sub(r"'(?:[^']|'')*'", " '' ", sql)

    refs: list[str] = []
    state = "normal"
    # Глубина вложенности внутри EXTRACT/SUBSTRING/TRIM и подобных: пока она
    # больше нуля, слово FROM к таблицам отношения не имеет.
    skip_from_until_depth: list[int] = []
    depth = 0
    previous = ""

    for token in _SQL_TOKEN_RE.findall(sanitized):
        lowered = token.lower()

        if token == "(":
            depth += 1
            if previous in _FROM_INSIDE_FUNCTIONS:
                skip_from_until_depth.append(depth)
            previous = token
            # Скобка сразу после FROM — производная таблица или подзапрос:
            # её собственный FROM разберётся отдельно, здесь имени нет.
            if state in ("expect_table", "after_table"):
                state = "normal"
            continue
        if token == ")":
            if skip_from_until_depth and skip_from_until_depth[-1] == depth:
                skip_from_until_depth.pop()
            depth -= 1
            previous = token
            # Закрывающая скобка сама по себе может завершать перечисление
            # таблиц — прежнее поведение сохраняем.
            if state in ("expect_table", "after_table"):
                state = "normal"
            continue
        previous = lowered

        if skip_from_until_depth and lowered in ("from", "join"):
            continue

        if state == "expect_table":
            if token == "(":
                state = "normal"          # производная таблица / подзапрос
            elif token == ",":
                pass
            elif lowered in _TABLE_LIST_TERMINATORS:
                state = "normal"
            else:
                refs.append(token)
                state = "after_table"
            continue

        if state == "after_table":
            if token == ",":
                state = "expect_table"    # comma-join: дальше ещё таблица
            elif lowered in ("from", "join"):
                state = "expect_table"
            elif lowered in _TABLE_LIST_TERMINATORS or token in ("(", ")"):
                state = "normal"
            # иначе это алиас (`t x` / `t AS x`) — остаёмся в after_table
            continue

        if lowered in ("from", "join"):
            state = "expect_table"

    return refs


def _assert_whitelist_tables(sql: str, allowed: set[str] | None = None) -> None:
    if allowed is None:
        allowed = ALLOWED_TABLES
    found = _extract_table_refs(sql)
    if not found:
        raise SQLSecurityError("Не удалось определить таблицу в запросе.")

    for table in found:
        if "." in table:
            raise SQLSecurityError(
                f"Ссылки с указанием схемы запрещены: '{table}'. "
                f"Обращайтесь к таблицам без префикса схемы."
            )
        if '"' in table:
            raise SQLSecurityError(
                f"Идентификаторы в двойных кавычках запрещены: {table}."
            )
        name = table.lower()
        if name in allowed:
            continue
        # Отказ по роли и отказ по несуществующей таблице — разные вещи,
        # и пользователю их надо объяснять по-разному: в первом случае
        # данные есть, но не для него, во втором их нет вовсе.
        if name in ALLOWED_TABLES:
            raise SQLSecurityError(
                f"Данные таблицы '{table}' недоступны вашей роли."
            )
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


def validate_sql(sql: str, role: str | None = None) -> str:
    """Проверяет запрос по политике безопасности для указанной роли.

    role=None означает объединение всех ролей и используется только
    CLI-режимом ai_agent.main(). Веб-путь обязан передавать роль из
    проверенного токена.
    """
    normalized = _normalize_sql(sql)
    if not normalized:
        raise SQLSecurityError("Пустой SQL-запрос.")

    _assert_select_only(normalized)
    _assert_single_statement(normalized)
    _assert_whitelist_tables(normalized, allowed_tables_for(role))
    _assert_no_forbidden_columns(normalized)
    return _ensure_limit(normalized)


def explain_rejection(error: SQLSecurityError | str) -> str:
    """Переводит отказ проверки на человеческий язык.

    Тексты самих исключений писались для разработчика и в лог аудита идут как
    есть — там нужна точность. Пользователю же «Не удалось определить таблицу
    в запросе» в ответ на «обнови мою оценку на 5» не объясняет ничего: он
    спрашивал не про таблицы. Причина отказа при этом совершенно понятная —
    ассистент не меняет данные, — и сказать её надо словами.
    """
    text = str(error)
    lowered = text.lower()

    if "только select" in lowered or "запрещённая конструкция" in lowered:
        return ("Ассистент работает только на чтение: добавлять, изменять и "
                "удалять данные через него нельзя.")
    if "недоступны вашей роли" in lowered:
        return (f"{text} Если данные нужны по работе, доступ выдаёт "
                f"администратор системы.")
    if "не входит в список разрешённых" in lowered or "схемы запрещены" in lowered:
        return ("Таких данных у ассистента нет. Он отвечает по учебной части, "
                "расписанию и приёму — служебные таблицы базы закрыты.")
    if "не удалось определить таблицу" in lowered:
        # Сюда попадают два разных случая: вопрос не про данные вообще и
        # просьба что-то изменить, на которую модель выдала запрос без FROM.
        # Различить их здесь нечем, поэтому отвечаем на оба сразу.
        return ("Не понял, какие именно данные нужны. Если вы просили что-то "
                "изменить или удалить — ассистент работает только на чтение. "
                "Иначе переформулируйте вопрос: назовите направление, группу "
                "или дисциплину.")
    if "заготовка" in lowered:
        return ("Подставлять произвольный текст прямо в запрос нельзя. "
                "Спросите обычными словами — ассистент составит запрос сам.")
    if "один sql-оператор" in lowered:
        return ("В одном вопросе — один запрос. Несколько команд подряд "
                "ассистент не выполняет.")
    if "персональных данных" in lowered or "personal-data" in lowered:
        return ("Персональные данные — паспорт, телефон, почта, дата "
                "рождения — закрыты для всех ролей.")
    return text


def _format_value(value) -> str:
    """Приводит значение к тому же виду, что раньше печатал psql -t -A.

    Формат важен: фронтенд разбирает строки вида «a|b|c», а модель во второй
    фазе видит тот же текст. NULL у psql — пустая строка, булево — t/f.
    """
    if value is None:
        return ""
    if value is True:
        return "t"
    if value is False:
        return "f"
    if isinstance(value, list):
        # Массивы (required_subjects и подобные) str() отдаёт питоновским
        # repr: ['Математика (профильный уровень)', 'Русский язык']. Это
        # доезжает и до модели, и до таблицы во фронтенде — квадратные скобки
        # с кавычками там лишние, а ведущая «[» ещё и похожа на признак
        # ошибки, которым помечаются служебные сообщения.
        return ", ".join("" if v is None else str(v) for v in value)
    return str(value)


def execute_validated_sql(safe_sql: str, session_vars: dict | None = None) -> str:
    """Выполняет УЖЕ ПРОВЕРЕННЫЙ SELECT-запрос.

    Функция намеренно не вызывает validate_sql(): на вход подаётся ровно то,
    что вернул validate_sql(). Раньше проверка шла дважды — здесь и у
    вызывающего кода, которому safe_sql всё равно нужен для audit_log.

    Возвращает строки в формате «поле|поле|поле», по строке на запись, — то
    же, что раньше отдавал psql -t -A -F'|'. При ошибке возвращает текст,
    начинающийся с «[Ошибка», — на это опирается main.py и фронтенд.
    """
    try:
        rows = db.fetch_all(
            "assistant", safe_sql, session_vars=session_vars, read_only=True
        )
    except db.DBUnavailable as e:
        return f"[Ошибка БД] {e}"
    except psycopg.Error as e:
        # Ошибка самого запроса (несуществующая колонка, деление на ноль...):
        # отдаём текст сервера, его вторая фаза объяснит пользователю.
        return f"[Ошибка БД] {str(e).strip()}"

    return "\n".join("|".join(_format_value(v) for v in row) for row in rows)


async def aexecute_validated_sql(
    safe_sql: str, session_vars: dict | None = None
) -> str:
    """Асинхронный вариант execute_validated_sql(). Контракт тот же."""
    try:
        rows = await db.afetch_all(
            "assistant", safe_sql, session_vars=session_vars, read_only=True
        )
    except db.DBUnavailable as e:
        return f"[Ошибка БД] {e}"
    except psycopg.Error as e:
        return f"[Ошибка БД] {str(e).strip()}"

    return "\n".join("|".join(_format_value(v) for v in row) for row in rows)


def _audit_statement_and_params(
    username, role, question, generated_sql, executed_sql, verdict,
    reject_reason, row_count, duration_ms, llm_ms, model,
):
    query = (
        "INSERT INTO assistant.audit_log "
        "(username, role, question, generated_sql, executed_sql, verdict, "
        "reject_reason, row_count, duration_ms, llm_ms, model) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    params = (
        username, role, question, generated_sql, executed_sql, verdict,
        reject_reason, row_count, duration_ms, llm_ms, model,
    )
    return query, params


async def alog_audit_entry(
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
    """Асинхронный вариант log_audit_entry(). Контракт тот же."""
    query, params = _audit_statement_and_params(
        username, role, question, generated_sql, executed_sql, verdict,
        reject_reason, row_count, duration_ms, llm_ms, model,
    )
    try:
        await db.aexecute("assistant", query, params)
    except db.DBUnavailable as e:
        raise RuntimeError(f"БД недоступна: {e}") from e
    except psycopg.Error as e:
        raise RuntimeError(str(e).strip()) from e


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
    """Пишет запись в assistant.audit_log.

    Значения уходят настоящими параметрами запроса (%s), а не подстановками
    psql через -v/:'name'. Прежний вариант терял кириллицу: значения ехали
    аргументами командной строки, Windows конвертировал их в ANSI-кодовую
    страницу, и вопрос пользователя сохранялся как «??????» (len == bytes
    в базе). Теперь текст доезжает как есть, а None становится NULL сам,
    без sentinel-строки и NULLIF.

    Пароли сюда не попадают: логируются только вопрос, SQL и метаданные.
    """
    query, params = _audit_statement_and_params(
        username, role, question, generated_sql, executed_sql, verdict,
        reject_reason, row_count, duration_ms, llm_ms, model,
    )

    try:
        db.execute("assistant", query, params)
    except db.DBUnavailable as e:
        raise RuntimeError(f"БД недоступна: {e}") from e
    except psycopg.Error as e:
        raise RuntimeError(str(e).strip()) from e
