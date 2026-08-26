import asyncio
import os
import sys
import json
import re
import time
import urllib.request
import urllib.error

import httpx
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

# Ручного списка связей здесь больше нет.
#
# Раньше тут лежал MANUAL_RELATIONSHIPS_FALLBACK — резервный перечень внешних
# ключей на случай, если живой запрос ничего не вернёт. В схеме assistant все
# FK объявлены настоящими constraints (сейчас их 56), поэтому запрос всегда
# что-то находит, и до резерва исполнение не доходило НИ РАЗУ. Список при этом
# продолжал жить своей жизнью и разошёлся со схемой: в нём не было ни одной из
# таблиц, добавленных миграциями 006-011.
#
# Неиспользуемый и заведомо неверный список хуже его отсутствия: рано или
# поздно кто-нибудь сверится с ним вместо базы. Если связи получить не удалось,
# честнее так и сказать модели — см. build_sql_system_prompt().

# Внешние ключи на data_sources есть почти у каждой справочной таблицы (23 из
# 56) и модели не нужны: соединять данные с реестром источников она не должна,
# ссылка и так лежит в колонке source_url рядом. В промпт они не попадают.
_NOISE_FK_COLUMNS = {"source_id"}

# Списка «висячих колонок представлений» здесь больше нет.
#
# Он был: ege_scores_summary.faculty_id и students_summary.faculty_id ведут в
# скрытую от модели faculties, а внешних ключей у представлений не бывает, и
# автоматический фильтр ниже их не находил. Но перечень в коде описывал дефект,
# а не устранял его. Устранён он там, где и возник, — в самих представлениях:
# миграция 016 отдаёт faculty_name вместо faculty_id. Заодно появилась
# возможность, которой не было: сгруппировать баллы ЕГЭ по факультетам.


def build_model_uri() -> str:
    return f"gpt://{FOLDER_ID}/{MODEL_NAME}"


def fetch_foreign_keys() -> tuple[list[tuple], str | None]:
    """Все внешние ключи схемы assistant: (строки, текст_ошибки).

    Забирается ОДИН раз на сборку промпта и используется дважды — блоком
    связей и фильтром висячих ссылок в блоке схемы. Раньше это были два
    независимых запроса, и фильтровать схему по связям было нечем.
    """
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
        return db.fetch_all("assistant", query, read_only=True), None
    except db.DBUnavailable as e:
        return [], f"[Не удалось получить связи БД] {e}"
    except psycopg.Error as e:
        return [], f"[Ошибка получения связей БД] {str(e).strip()}"


def fetch_object_comments() -> dict[str, str]:
    """Подписи (COMMENT ON) к таблицам и представлениям схемы assistant.

    Зачем они в промпте. По имени и списку колонок невозможно отличить
    «строка = студент» от «строка = студент × кафедра»: выглядят они
    одинаково. Из-за этого на вопрос «сколько должников на кафедре» ассистент
    просуммировал debts_count у academic_debts и ответил «10760 студентов с
    долгами» — при 4981 студенте в университете.

    Разрез представления — свойство данных, поэтому и записан он там же, в
    базе (миграция 017), а не продублирован отдельным списком в коде. Здесь
    он только читается: новая вьюха с подписью объяснит себя сама.
    """
    query = (
        "SELECT c.relname, obj_description(c.oid) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'assistant' AND c.relkind IN ('r', 'v', 'm') "
        "  AND obj_description(c.oid) IS NOT NULL"
    )
    try:
        rows = db.fetch_all("assistant", query, read_only=True)
    except (db.DBUnavailable, psycopg.Error):
        # Подписи — уточнение, а не основа промпта: без них схема остаётся
        # рабочей, поэтому сбой здесь не должен ронять сборку промпта.
        return {}
    return {name: " ".join(text.split()) for name, text in rows}


def _dangling_fk_columns(
    foreign_keys: list[tuple], allowed: set[str]
) -> set[tuple[str, str]]:
    """Колонки-внешние ключи, ведущие в НЕВИДИМЫЙ роли объект.

    Ради чего это всё. На вопрос «какие группы учатся на направлении
    информационная безопасность» модель выдала:

        SELECT g.name FROM groups g
        JOIN edu_programs ep ON g.program_id = ep.id

    и получила ноль строк, потому что groups.program_id ведёт в programs
    (демо-контур, 13 направлений), а edu_programs — официальный каталог на 113.
    Идентификаторы там несопоставимы: program_id не больше 13, а нужные строки
    каталога имеют id 108 и 109.

    Виновата не модель. Таблица programs от неё скрыта, поэтому связь
    groups.program_id -> programs.id выбрасывалась из блока связей, и в промпте
    оставалась колонка program_id БЕЗ ЕДИНОЙ объявленной связи — рядом с
    заманчиво подходящим по смыслу edu_programs. Соединить их было
    единственным доступным ходом.

    Лечение общее, а не заплатка на один случай: колонку, ведущую в скрытый
    объект, модель просто не видит. Соединить по ней невозможно.
    Таких колонок сейчас пять: groups.program_id, curriculum.program_id,
    admission_campaigns.program_id, teachers.department_id,
    subjects.department_id — и у роли student к ним добавляется
    schedule.curriculum_id.
    """
    return {
        (from_table, from_col)
        for from_table, from_col, to_table, _ in foreign_keys
        if to_table not in allowed
    }


def get_db_schema(
    allowed: set[str] | None = None, foreign_keys: list[tuple] | None = None
) -> str:
    """Схема БД для промпта.

    allowed — набор таблиц, доступных роли. Показывать модели то, чего ей
    нельзя, вредно: она исправно построит запрос, проверка исправно его
    отклонит, а пользователь увидит отказ вместо ответа.

    foreign_keys — список связей, по которому вычищаются висячие ссылки
    (см. _dangling_fk_columns). Если не передан, забирается сам.
    """
    query = (
        "SELECT table_name, column_name, data_type "
        "FROM information_schema.columns "
        "WHERE table_schema = 'assistant' "
        "ORDER BY table_name, ordinal_position;"
    )
    try:
        rows = db.fetch_all("assistant", query, read_only=True)
    except db.DBUnavailable as e:
        return f"[Не удалось получить схему БД] {e}"
    except psycopg.Error as e:
        return f"[Ошибка получения схемы БД] {str(e).strip()}"

    if allowed is None:
        allowed = security.ALLOWED_TABLES
    if foreign_keys is None:
        foreign_keys, _ = fetch_foreign_keys()

    dangling = _dangling_fk_columns(foreign_keys, allowed)
    comments = fetch_object_comments()

    tables: dict[str, list[str]] = {}
    for table, column, dtype in rows:
        if table not in allowed:
            continue
        if (table, column) in dangling:
            continue
        tables.setdefault(table, []).append(f"{column} ({dtype})")

    if not tables:
        return "Схема БД пуста (нет доступных таблиц в схеме assistant)."

    lines = ["Доступные таблицы и колонки в БД:"]
    for table in sorted(tables):
        lines.append(f"- {table}: {', '.join(tables[table])}")
        note = comments.get(table)
        if note:
            lines.append(f"    {note}")
    return "\n".join(lines)


def get_db_relationships(
    allowed: set[str] | None = None, foreign_keys: list[tuple] | None = None
) -> str:
    """Связи между таблицами для промпта.

    allowed — набор объектов, доступных роли. Фильтр по нему обязателен по
    той же причине, что и в get_db_schema(): показывать модели связь с
    таблицей, которую ей нельзя читать, значит подталкивать её к запросу,
    который проверка отклонит. Раньше блок связей отдавался целиком всем
    ролям, и студент видел в промпте students, applications и grades.
    """
    if allowed is None:
        allowed = security.ALLOWED_TABLES

    if foreign_keys is None:
        foreign_keys, error = fetch_foreign_keys()
        if error:
            return error

    lines = []
    for from_table, from_col, to_table, to_col in foreign_keys:
        if from_col in _NOISE_FK_COLUMNS:
            continue
        if from_table not in allowed or to_table not in allowed:
            continue
        lines.append(f"{from_table}.{from_col} -> {to_table}.{to_col}")

    if not lines:
        return ""
    return "Связи между таблицами (foreign keys):\n" + "\n".join(f"- {l}" for l in lines)


def build_sql_system_prompt(role: str | None = None) -> str:
    """Системный промпт под конкретную роль.

    Схема урезается до того, что роли действительно доступно, иначе модель
    будет уверенно строить запросы к закрытым для неё таблицам.
    """
    allowed = security.allowed_tables_for(role)
    # Связи забираем один раз: они нужны и блоку связей, и фильтру висячих
    # ссылок внутри блока схемы.
    foreign_keys, fk_error = fetch_foreign_keys()
    schema = get_db_schema(allowed, foreign_keys)
    relationships = fk_error or get_db_relationships(allowed, foreign_keys)
    if not relationships:
        # Связи не получены (БД недоступна или роли не видно ни одной пары
        # связанных объектов). Врать про схему нельзя: пусть модель знает,
        # что JOIN'ы придётся выводить из названий колонок, и осторожничает.
        relationships = (
            "Связи между таблицами получить не удалось. Не выдумывай JOIN'ы: "
            "если соединение не очевидно из имён колонок, ограничься одной "
            "таблицей."
        )

    # Правило про ЕГЭ добавляем, только если представление реально есть
    # в схеме. Иначе модель начнёт строить запросы к несуществующему
    # объекту и пользователь получит «relation does not exist» вместо
    # ответа. Схема тянется живьём, так что правило включится само, как
    # только применят backend/sql/002_ege_scores.sql — координировать
    # раскладку промпта и миграции руками не нужно.
    # Личные вьюхи есть только у студента — и объяснить их надо явно,
    # иначе модель начнёт искать «мои оценки» в закрытой таблице grades.
    personal_rule = ""
    if security.STUDENT_PERSONAL_TABLES & allowed:
        personal_rule = (
            "\n8. Личные данные самого пользователя доступны через "
            "my_profile (ФИО, группа, курс, направление, статус, форма "
            "оплаты), my_grades (оценки: subject_name, semester, score, "
            "attempt, graded_at) и my_schedule (расписание его группы: "
            "weekday, pair_number, week_type, subject_name, teacher_name, "
            "building, room_number). Эти представления УЖЕ показывают "
            "только данные текущего пользователя — не добавляй в них "
            "фильтр по студенту, по имени или по идентификатору, его "
            "подставляет сервер. На вопросы вида «мои оценки», «моё "
            "расписание», «на каком я курсе» отвечай запросом к ним. "
            "Пример: SELECT subject_name, score FROM my_grades "
            "ORDER BY graded_at DESC."
        )

    teaching_rule = ""
    if security.TEACHER_PERSONAL_TABLES & allowed:
        teaching_rule = (
            "\n9. Данные самого преподавателя доступны через my_teaching "
            "(что он ведёт: subject_name, program_name, semester, "
            "control_form, hours, students_count), my_teaching_schedule "
            "(его собственное расписание: weekday, pair_number, week_type, "
            "subject_name, group_name, course, building, room_number) и "
            "my_students_performance (успеваемость по его предметам: "
            "grades_count, avg_score, excellent_count, failed_count, "
            "retake_count). Эти представления УЖЕ ограничены текущим "
            "преподавателем — не добавляй фильтр по его имени или "
            "идентификатору. На вопросы «что я веду», «моё расписание», "
            "«как сдают мой предмет» отвечай запросом к ним, а не к "
            "curriculum или grades_summary по всему вузу."
        )

    # Правила ниже включаются по факту наличия объекта в схеме — так же, как
    # ege_rule. Пока миграции 006/007/008 не применены, промпт остаётся
    # прежним, и координировать раскладку с состоянием БД руками не нужно.

    admission_rule = ""
    if "programs_admission" in allowed:
        admission_rule = (
            "\n10. ВОПРОСЫ О ПОСТУПЛЕНИИ. Официальные сведения об ИГУ лежат "
            "отдельно от учебного контура. Маршрут такой:\n"
            "   - направления, места, стоимость, перечень испытаний -> "
            "programs_admission (там же unit_name — подразделение, "
            "budget_seats, paid_seats, tuition_rub, exams_required, "
            "exams_choice, admission_year);\n"
            "   - «куда я могу поступить с предметами X, Y, Z» -> "
            "program_exam_sets, проверка вхождения массива по КОРОТКИМ именам "
            "предметов: WHERE required_short <@ "
            "ARRAY['русский','математика','информатика']. Короткие имена — в "
            "нижнем регистре и без уточнений: 'русский', 'математика', "
            "'информатика', 'физика', 'химия', 'биология', 'география', "
            "'история', 'обществознание', 'литература', 'иностранный язык'. "
            "Колонки required_subjects и choice_subjects содержат полные "
            "названия и годятся для показа, а не для сравнения;\n"
            "   - минимальный балл (порог допуска) -> minimum_scores_view;\n"
            "   - проходной балл (последний зачисленный) -> "
            "passing_scores_view;\n"
            "   - сроки подачи, приём оригиналов и согласий -> "
            "admission_deadlines. НЕ фильтруй по колонке stage: там свободные "
            "формулировки этапов, угадать их нельзя, и запрос вернёт пусто. "
            "Бери все сроки года и сортируй по дате: SELECT stage, date_from, "
            "date_to, description, source_url FROM admission_deadlines WHERE "
            "admission_year = 2026 ORDER BY COALESCE(date_to, date_from). "
            "По той же причине не фильтруй по funding_basis, если пользователь "
            "не сказал явно «бюджет» или «платно» — у части этапов там NULL;\n"
            "   - документы -> admission_documents. По умолчанию бери только "
            "основной перечень: WHERE applicant_category = 'все'. Списки для "
            "особой и отдельной квот и для сирот выдавай, ТОЛЬКО если про них "
            "спросили прямо — иначе в ответ попадают справки об инвалидности, "
            "свидетельства о смерти и военные документы, к вопросу «какие "
            "документы нужны» отношения не имеющие;\n"
            "   - льготы, целевое, квоты -> benefits_quotas; общежития -> "
            "dormitories; адреса и телефоны -> contacts (колонки "
            "contact_phone, contact_email).\n"
            "   - СТРУКТУРА УНИВЕРСИТЕТА — только university_units. «Сколько "
            "факультетов», «какие институты есть», «что за подразделения» — "
            "считай и перечисляй по ней: SELECT count(*) FROM "
            "university_units, при необходимости с WHERE kind = 'факультет' "
            "или kind = 'институт'. Перечень направлений и специальностей — "
            "edu_programs и programs_admission.\n"
            "   МИНИМАЛЬНЫЙ И ПРОХОДНОЙ БАЛЛ — РАЗНЫЕ ВЕЩИ, не подменяй одно "
            "другим. Минимальный — порог, ниже которого не допускают к "
            "конкурсу, он известен заранее. Проходной — балл последнего "
            "зачисленного в конкретном году, он появляется только после "
            "зачисления.\n"
            "   Проходной балл ВСЕГДА выбирай вместе с admission_year, "
            "study_form, funding_basis и competition_group — без них это "
            "бессмысленное число.\n"
            "   Если год в вопросе не назван, возьми максимальный доступный "
            "и ОБЯЗАТЕЛЬНО включи admission_year в SELECT, чтобы в ответе "
            "можно было назвать год.\n"
            "   ПОИСК ПО НАЗВАНИЮ НАПРАВЛЕНИЯ. В базе названия лежат в "
            "именительном падеже, а спрашивают в любом: «на юриспруденцию», "
            "«программной инженерии». ILIKE тут не годится. У представлений "
            "programs_admission, program_exam_sets и passing_scores_view есть "
            "колонка search_vector с русской морфологией — ищи через неё, "
            "передавая слова пользователя КАК ЕСТЬ: WHERE search_vector @@ "
            "plainto_tsquery('russian', 'программной инженерии'). Падежи она "
            "приводит сама. ILIKE оставь для случаев, когда search_vector у "
            "объекта нет."
        )

    schedule_rule = ""
    # Объект расписания зависит от роли: сотрудникам — schedule_calendar со
    # всеми полями, гостю из публичного виджета — public_schedule без ФИО
    # преподавателей (миграция 018). Правило одно, имя объекта подставляется.
    schedule_view = None
    if "schedule_calendar" in allowed:
        schedule_view = "schedule_calendar"
    elif "public_schedule" in allowed:
        schedule_view = "public_schedule"

    if schedule_view:
        who_column = (
            "teacher_name, " if schedule_view == "schedule_calendar" else ""
        )
        who_question = (
            "«кто ведёт», " if schedule_view == "schedule_calendar" else ""
        )
        no_teacher_note = "" if schedule_view == "schedule_calendar" else (
            "   ФИО преподавателей в этом представлении НЕТ и получить их "
            "неоткуда. На вопрос «кто ведёт пару» отвечай, что такие сведения "
            "здесь не выдаются, — не подставляй вместо них ничего.\n"
        )
        schedule_rule = (
            "\n11. РАСПИСАНИЕ ПО ДАТАМ. Для вопросов «что сегодня», «что "
            "завтра», «расписание на 15 сентября», «какая следующая пара», "
            f"«во сколько», «в какой аудитории», {who_question}используй "
            f"{schedule_view}: там уже есть lesson_date, starts_at, ends_at, "
            f"lesson_type, subject_name, {who_column}group_name, building, "
            "room_number. Таблица schedule без дат — только для вопросов о "
            "недельной сетке.\n"
            f"{no_teacher_note}"
            "   Текущую дату бери в часовом поясе Иркутска: "
            "(now() AT TIME ZONE 'Asia/Irkutsk')::date. «Завтра» — это "
            "+ INTERVAL '1 day' от неё. Пример: SELECT starts_at, "
            f"subject_name, room_number FROM {schedule_view} WHERE "
            "group_name = 'ФИТ-0925-1' AND lesson_date = "
            "(now() AT TIME ZONE 'Asia/Irkutsk')::date ORDER BY pair_number.\n"
            "   «Следующая пара» — ближайшее занятие ПОСЛЕ текущего момента. "
            "Складывай дату и время в один момент: WHERE "
            "(lesson_date + starts_at) > (now() AT TIME ZONE 'Asia/Irkutsk') "
            "ORDER BY lesson_date, starts_at LIMIT 1. НЕ пиши "
            "(lesson_date, starts_at) > now() — это сравнение кортежа с "
            "датой, запрос упадёт с ошибкой типов.\n"
            "   НИКОГДА не подставляй год наугад. Ты не знаешь сегодняшнюю "
            "дату, поэтому если пользователь назвал только день и месяц "
            "(«10 сентября»), собери дату из текущего года: "
            "make_date(EXTRACT(YEAR FROM (now() AT TIME ZONE 'Asia/Irkutsk'))::int, "
            "9, 10). Написанный вручную '2024-09-10' почти наверняка вернёт "
            "пустой ответ."
        )

    provenance_rule = ""
    if "data_sources" in allowed:
        provenance_rule = (
            "\n12. СЛУЖЕБНЫЕ КОЛОНКИ. Колонки data_status, source_url, "
            "source_id, checked_at и page_url в SELECT НЕ добавляй. Они нужны "
            "загрузчикам данных и отчётам, а пользователю не адресованы: в "
            "ответе не должно быть ни пометок о происхождении, ни ссылок на "
            "сайт.\n"
            "   Фильтровать по data_status тоже не надо — выбирай все строки, "
            "какие подходят по существу вопроса."
        )

    analytics_rule = ""
    if "subject_performance" in allowed or "room_availability" in allowed:
        analytics_rule = (
            "\n14. АНАЛИТИКА УЧЕБНОГО ПРОЦЕССА. Не собирай статистику из "
            "students, grades и enrollments — они закрыты. Для этого есть "
            "готовые представления, и в них уже посчитаны нужные величины:\n"
            "   СЧИТАЯ ЛЮДЕЙ, БЕРИ ПРЕДСТАВЛЕНИЕ, ГДЕ СТРОКА — ЧЕЛОВЕК. "
            "У каждого объекта в схеме выше есть подпись с его разрезом: "
            "«строка = один студент», «строка = пара студент × кафедра» и так "
            "далее. Сверяйся с ней перед COUNT и SUM. Складывать счётчик "
            "долгов (debts_count) и выдавать сумму за число студентов нельзя: "
            "это разные величины.\n"
            "   - успеваемость по дисциплине, распределение оценок, доля "
            "сдавших с первой попытки -> subject_summary (одна строка на "
            "дисциплину: grades_count, avg_score, excellent_count, "
            "good_count, satisfactory_count, failed_count, "
            "first_attempt_pass_rate, retake_count). Разбивка того же по "
            "направлениям и семестрам — subject_performance, но на вопрос "
            "«какой процент сдал предмет» она даёт несколько разных чисел;\n"
            "   - «сколько должников», «кто не сдал ни одного экзамена», «у "
            "кого больше всего долгов» -> student_debts (строка = один "
            "студент: debts_count, retakes_count, passed_count). Число "
            "должников — это COUNT(*) WHERE debts_count > 0;\n"
            "   - должники и нагрузка В РАЗРЕЗЕ КАФЕДРЫ -> department_debts "
            "(строка = одна кафедра: students_total, debtors_count — сколько "
            "ЧЕЛОВЕК, debts_total — сколько задолженностей, debtors_percent). "
            "Кафедру ищи через search_vector;\n"
            "   - рейтинг студентов, средний балл конкретного человека -> "
            "student_rankings (last_name, first_name, group_name, "
            "faculty_name, semester, avg_score);\n"
            "   - поимённый разбор долгов ПО КАФЕДРАМ -> academic_debts "
            "(строка — пара «студент × кафедра», поэтому COUNT(*) по ней "
            "считает пары, а не людей; для людей есть student_debts и "
            "department_debts выше). КАФЕДРУ ищи ТОЛЬКО через search_vector: "
            "WHERE search_vector @@ plainto_tsquery('russian', 'программная "
            "инженерия'). ILIKE по department_name не сработает — в базе "
            "кафедра записана как «Кафедра программной инженерии», и шаблон "
            "'%Программная инженерия%' не совпадёт ни с чем;\n"
            "   - кафедры: средний балл против общего -> "
            "department_performance (avg_score, university_avg_score, "
            "diff_from_university); нагрузка -> department_workload "
            "(avg_hours_per_teacher, total_hours);\n"
            "   - преподаватели по семестрам -> teacher_semester_load "
            "(subjects_count, students_count, semester). Строка с "
            "subjects_count = 0 означает, что в этом семестре преподаватель "
            "не ведёт ничего — так и ищи «кто не ведёт дисциплин»;\n"
            "   - аудитории: загруженность -> room_load (lessons_per_week); "
            "свободна ли в слоте -> room_availability (is_free = true). "
            "Корпус спрашивают буквой («в корпусе А») — фильтруй по "
            "building_code ('А', 'Б', 'В', 'Л'), а не по building: там полные "
            "названия вроде «Главный корпус»;\n"
            "   - «кто не сдал ни одного экзамена» -> academic_debts, условие "
            "passed_count = 0;\n"
            "   - «кто не ведёт дисциплин в семестре» -> "
            "teacher_semester_load WHERE semester = N AND subjects_count = 0; "
            "в этом же представлении есть teacher_id, если нужен "
            "идентификатор;\n"
            "   - учебный план группы -> group_curriculum (group_name, "
            "semester, term_name, subject_name, teacher_name);\n"
            "   - доля платных студентов -> funding_share (paid_percent);\n"
            "   - места приёма демо-контура -> seats_ratio (budget_seats, "
            "paid_seats, budget_percent, campaign_year);\n"
            "   - заявления по дням -> applications_by_day. «За последние N "
            "дней кампании» и «в последний день» считаются ОТ КОНЦА ПРИЁМА, а "
            "не от сегодняшней даты: кампания могла закончиться год назад. "
            "Для этого есть готовые колонки: days_before_deadline (0 — "
            "последний день, 7 — за неделю до конца) и is_last_day. Пример: "
            "SELECT sum(applications_count) FROM applications_by_day WHERE "
            "campaign_year = 2025 AND days_before_deadline BETWEEN 0 AND 7. "
            "У applications_summary дат нет вовсе — для вопросов про дни она "
            "не годится;\n"
            "   - динамика приёма по годам -> admission_dynamics.\n"
            "   ПОИСК ПО НАЗВАНИЯМ В АНАЛИТИКЕ. У этих представлений тоже "
            "есть search_vector с русской морфологией — он собран из названий "
            "кафедры, факультета, направления, группы и дисциплины. Ищи "
            "через него, передавая слова пользователя как есть: WHERE "
            "search_vector @@ plainto_tsquery('russian', 'программная "
            "инженерия'). Точное сравнение department_name = 'Программная "
            "инженерия' вернёт пусто: в базе кафедра записана как «Кафедра "
            "программной инженерии».\n"
            "   Если нужного представления в списке доступных нет — значит, "
            "роли эти данные не положены. Не пытайся собрать их обходным "
            "путём из других таблиц."
        )

    groups_rule = ""
    if "groups_catalog" in allowed:
        groups_rule = (
            "\n15. ГРУППЫ И ИХ НАПРАВЛЕНИЯ. «Какие группы учатся на "
            "направлении X», «на каком направлении группа Y», «сколько групп "
            "на курсе» — только groups_catalog (group_name, course, "
            "program_name, degree, study_form, faculty_name, students_count). "
            "Направление ищи через search_vector: WHERE search_vector @@ "
            "plainto_tsquery('russian', 'информационная безопасность').\n"
            "   Учебные группы и официальный каталог направлений "
            "(edu_programs, programs_admission) — РАЗНЫЕ вещи. Первое про "
            "тех, кто уже учится, второе про приём. Соединять их запросом "
            "нельзя: общих идентификаторов у них нет, и такой JOIN всегда "
            "даёт пустой результат. Названия направлений в groups_catalog "
            "лежат текстом — этого достаточно."
        )

    faq_rule = ""
    if "faq_entries" in allowed:
        faq_rule = (
            "\n13. ОБЩИЕ ВОПРОСЫ. Если вопрос не ложится ни на одну таблицу "
            "данных («можно ли подать документы онлайн», «есть ли внутренние "
            "экзамены», «как связаться с вузом», «что с сессией и "
            "библиотекой»), ищи в faq_entries: SELECT question, answer "
            "FROM faq_entries WHERE question ILIKE "
            "'%ключевое слово%' OR answer ILIKE '%ключевое слово%' OR "
            "keywords && ARRAY['ключевое слово']. Оператор && проверяет "
            "пересечение массивов и терпим к формулировке вопроса.\n"
            "   FAQ — ЭТО ЗАПАСНОЙ ПУТЬ ДЛЯ ВОПРОСОВ ОБ УНИВЕРСИТЕТЕ, а не "
            "универсальный ответ на всё. НЕ откатывайся сюда, когда:\n"
            "     - просят персональные данные конкретного человека: паспорт, "
            "телефон, почту, дату рождения студента, преподавателя или "
            "абитуриента;\n"
            "     - просят ИЗМЕНИТЬ данные — добавить, обновить, удалить "
            "запись;\n"
            "     - спрашивают про устройство самой базы: перечень таблиц, "
            "колонки, служебные идентификаторы, учётные записи, пароли.\n"
            "   Это не вопросы про университет, и ответа на них нет ни в "
            "faq_entries, ни в любой другой таблице. Поиск по FAQ превращает "
            "такой случай в пустой ответ, тогда как отказ должен быть виден: "
            "ассистент работает только на чтение и только по учебным данным.\n"
            "   Сюда же откатывайся, если профильная таблица по теме вопроса "
            "оказалась пустой: в faq_entries лежит объяснение со ссылкой на "
            "официальный источник, и оно полезнее, чем «ничего не найдено». "
            "Так устроены, например, вопросы про общежития и минимальные "
            "баллы — точных цифр в открытых документах нет, а порядок "
            "действий описан."
        )

    ege_rule = ""
    if "ege_scores_summary" in allowed:
        ege_rule = (
            "\n7. Баллы ЕГЭ по отдельным предметам доступны через "
            "ege_scores_summary — там уже посчитаны avg_score, min_score, "
            "max_score и applications_count в разрезе subject, program_name, "
            "degree, faculty_name и campaign_year. Приём тот же, что с "
            "остальными представлениями: бери готовые агрегаты, а по "
            "нескольким строкам считай AVG(avg_score) или "
            "SUM(applications_count). Пример: SELECT subject, AVG(avg_score) "
            "FROM ege_scores_summary WHERE campaign_year = 2024 "
            "GROUP BY subject."
        )

    # Где искать группу по названию — зависит от роли: у гостя нет ни
    # group_curriculum, ни schedule_calendar, и отправлять его туда значит
    # гарантировать отказ проверки вместо ответа.
    group_sources = [
        name for name in ("groups_catalog", "group_curriculum",
                          "schedule_calendar", "public_schedule")
        if name in allowed
    ]
    group_lookup_rule = ""
    if group_sources:
        group_lookup_rule = (
            f"   - «ФИТ-0925-1» и подобное — это ГРУППА: ищи по колонке "
            f"group_name в {' или '.join(group_sources)}. В таблице groups "
            f"колонка называется просто name, а не group_name.\n"
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
        f"1. Генерируй ТОЛЬКО SELECT-запросы (или WITH ... SELECT). Изменять "
        f"данные ассистент не может: на «добавь», «обнови», «удали» не "
        f"подбирай обходной путь и не ищи ответ в справочных таблицах.\n"
        f"2. Только таблицы из списка выше. Служебного устройства базы — "
        f"перечня таблиц, описания колонок, внутренних идентификаторов, "
        f"учётных записей и паролей — в этом списке нет и не будет. Это не "
        f"данные университета, отвечать на такие вопросы нечем: не подставляй "
        f"похожую таблицу и не откатывайся в faq_entries.\n"
        f"3. Если нужно много строк — добавь LIMIT (например, LIMIT 50).\n"
        f"4. В ответе выдай ЕДИНСТВЕННУЮ вещь — SQL в блоке ```sql ... ```. "
        f"Никакого пояснительного текста до и после. Никакого другого текста.\n"
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
        f"WHERE program_name ILIKE '%юриспруденция%' AND degree = 'бакалавриат'.\n"
        f"7. ТЕКУЩАЯ ДАТА. Сегодняшний день бери из БД, а не из головы: "
        f"(now() AT TIME ZONE 'Asia/Irkutsk')::date. Текущий год — "
        f"EXTRACT(YEAR FROM (now() AT TIME ZONE 'Asia/Irkutsk'))::int. Это "
        f"разрешённые конструкции, FROM внутри EXTRACT таблицей не считается.\n"
        f"8. КАВЫЧКИ В ТЕКСТЕ. Апостроф внутри литерала удваивается, и только "
        f"так: WHERE full_name LIKE '%Д''Артаньян%'. Обратная косая черта "
        f"перед апострофом — синтаксическая ошибка PostgreSQL, никогда её не "
        f"пиши. Процент как обычный символ ищи через POSITION: WHERE "
        f"POSITION('%' IN name) > 0 — это надёжнее конструкции с ESCAPE.\n"
        f"9. ОДНО СЛОВО В ВОПРОСЕ. Истории переписки у тебя нет: каждый вопрос "
        f"приходит отдельно, и уточнение приходит без контекста. Если пришло "
        f"одно слово или короткая фраза без глагола, считай это названием и "
        f"покажи по нему сводку:\n"
        f"   - «психология», «юриспруденция», «лингвистика» — это НАПРАВЛЕНИЕ: "
        f"SELECT program_name, level, study_form, unit_name, exams_required, "
        f"budget_seats, tuition_rub FROM programs_admission WHERE "
        f"search_vector @@ plainto_tsquery('russian', 'психология');\n"
        f"{group_lookup_rule}"
        f"   Не отказывайся и не переспрашивай — покажи, что нашлось."
        f"{ege_rule}"
        f"{personal_rule}"
        f"{teaching_rule}"
        f"{admission_rule}"
        f"{schedule_rule}"
        f"{provenance_rule}"
        f"{analytics_rule}"
        f"{faq_rule}"
        f"{groups_rule}"
    )


# ---------------------------------------------------------------------------
# Защита от ответа-бланка
# ---------------------------------------------------------------------------
#
# Наблюдалось на стенде. Вопрос «какие группы учатся на направлении
# информационная безопасность», выборка вернула пусто, и вторая фаза выдала:
#
#   «На направлении «Информационная безопасность» учатся следующие группы:
#    [название группы 1]
#    [название группы 2]»
#
# а в следующий раз — вместе с собственными рассуждениями модели:
#
#   «(перечислить группы, если бы они были в ответе). Если данных нет, то: …»
#
# Это худший исход из возможных: выглядит как ответ, читается как ответ и
# ответом не является. Правило 2 интерпретирующего промпта прямо это запрещает
# (см. build_interpret_system_prompt) — и, как видно, не всегда срабатывает.
# Промпт остаётся, но опираться теперь есть на что: текст проверяется кодом.
#
# Регулярка заглушек взята из backend/tests/eval_live_questions.py, где она
# ловила ровно этот исход под именем «шаблон». Теперь она здесь, и тест берёт
# её отсюда: проверка и рабочий код должны считать бланком одно и то же.
PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{2,40}\]|\{[а-яa-z_ ]{2,40}\}", re.I)

# Утёкшие наружу ветвления: модель отдала черновик вместо ответа.
LEAKED_BRANCHING_RE = re.compile(
    r"если\s+данных\s+нет"
    r"|если\s+бы\s+(?:они|он|она)\s+был"
    r"|\(\s*перечислить"
    r"|если\s+результат\s+пуст",
    re.I,
)

# Что отдаём вместо бланка. Честный отказ полезнее правдоподобной выдумки.
NOTHING_FOUND_ANSWER = (
    "По этому вопросу в базе ничего не нашлось. Возможно, таких данных нет "
    "или формулировку стоит уточнить — назовите направление, группу, "
    "дисциплину или год."
)

# Отдельный ответ для случая «вопрос вообще не про наши данные».
#
# Наблюдалось на просьбах «покажи список таблиц базы данных и их пароли» и
# «добавь нового студента Иванова». Модель не пыталась дотянуться до закрытых
# таблиц — она уходила в faq_entries по ключевому слову («%пароли%») и
# возвращала пусто. Утечки тут нет и быть не может, но и ответа нет: человек
# получал «ничего не нашлось» вместо «так нельзя».
#
# Промптом это не лечится — проверено тремя редакциями правил, включая перенос
# запрета в начало списка. Поэтому решается кодом: если отвечали ТОЛЬКО поиском
# по справке и не нашли ничего, значит вопрос за пределами области ассистента,
# и сказать надо именно это.
OUT_OF_SCOPE_ANSWER = (
    "В справочной информации такого нет. Ассистент отвечает по учебной части, "
    "расписанию, успеваемости и приёму — и только на чтение: устройство самой "
    "базы, учётные записи и изменение записей ему недоступны."
)

# Гостю из публичного виджета доступно меньше, и перечислять ему успеваемость
# значит обещать то, чего он не получит.
GUEST_OUT_OF_SCOPE_ANSWER = (
    "Такого здесь нет. Помощник отвечает по поступлению — направления, "
    "вступительные испытания, сроки, места и стоимость, — а также по "
    "расписанию занятий, корпусам и аудиториям. Сведений о конкретных людях, "
    "студентах и преподавателях, у него нет."
)


def is_out_of_scope(executed_sql: str | None) -> bool:
    """Отвечали ТОЛЬКО поиском по справке — значит вопрос не про наши данные.

    Развилка живёт здесь, чтобы /chat и eval_live_questions.py судили об одном
    и том же одинаково.
    """
    return bool(executed_sql) and security.tables_in(executed_sql) == {"faq_entries"}


def out_of_scope_answer(role: str | None = None) -> str:
    return GUEST_OUT_OF_SCOPE_ANSWER if role == "guest" else OUT_OF_SCOPE_ANSWER


def empty_result_answer(executed_sql: str | None, role: str | None = None) -> str:
    """Что отвечать на пустую выборку."""
    if is_out_of_scope(executed_sql):
        return out_of_scope_answer(role)
    return NOTHING_FOUND_ANSWER


def blank_answer_reason(answer: str) -> str | None:
    """Причина, по которой ответ считается бланком, либо None.

    Возвращается строка для audit_log — чтобы подмена была видна в аудите,
    а не происходила молча.
    """
    match = PLACEHOLDER_RE.search(answer)
    if match:
        return f"заглушка вместо данных: {match.group(0)[:60]}"
    match = LEAKED_BRANCHING_RE.search(answer)
    if match:
        return f"утёкшее рассуждение модели: {match.group(0)[:60]}"
    return None


def build_correction_input(question: str, sql: str, error: str) -> str:
    """Просьба переписать запрос после ошибки СУБД.

    Зачем повтор вообще нужен. Больше половины оставшихся промахов на живом
    прогоне — это не «модель не поняла вопрос», а «модель взяла колонку не из
    того представления»: semester у academic_debts, group_name у groups.
    PostgreSQL в таких случаях отвечает предельно конкретно — «column
    "semester" does not exist», — и одной этой строки модели хватает, чтобы
    исправиться с первого раза. Показывать пользователю ошибку СУБД вместо
    ответа, имея на руках такую подсказку, просто расточительно.

    Повтор ровно один: если и он не помог, дело не в опечатке.
    """
    return (
        f"Твой предыдущий запрос не выполнился.\n\n"
        f"Вопрос пользователя:\n{question}\n\n"
        f"Запрос, который ты выдал:\n{sql}\n\n"
        f"Ответ СУБД:\n{error}\n\n"
        f"Исправь запрос. Скорее всего, взята колонка, которой нет в этом "
        f"представлении: сверься с блоком схемы выше и используй только "
        f"перечисленные там колонки. Верни ТОЛЬКО исправленный SQL в блоке "
        f"```sql ... ``` без пояснений."
    )


def extract_sql(text: str) -> str | None:
    text = text.strip()

    # \s* до и после метки языка: модель периодически пишет «``` sql» с
    # пробелом, и тогда прежний вариант отдавал «sql SELECT ...» — запрос
    # падал на проверке «Разрешены только SELECT-запросы».
    fence = re.search(
        r"```\s*(?:sql\b)?\s*(.*?)\s*```", text, re.IGNORECASE | re.DOTALL
    )
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
        f"2. Если данных нет (пустой результат) — честно скажи, что ничего не "
        f"найдено. НИКОГДА не выдумывай ответ по форме вопроса и не оставляй "
        f"подстановки вида [номер аудитории], [количество мест], {{значение}}. "
        f"Ответ с квадратными скобками вместо цифр — это не ответ, а бланк: "
        f"он выглядит достоверно и вводит человека в заблуждение. Нет "
        f"данных — так и скажи одной фразой.\n"
        f"3. Если вместо данных пришла ошибка безопасности или БД — объясни её "
        f"простыми словами и предложи, как переформулировать вопрос.\n"
        f"4. Ответ должен быть на русском, без технического мусора.\n"
        # Служебные пометки в ответ не выносим. Колонка data_status нужна
        # внутри системы — чтобы отличать источники при загрузке и в отчётах,
        # — но пользователю она не адресована: ответ должен читаться как
        # ответ, а не как справка о состоянии базы.
        f"5. НЕ комментируй происхождение данных. Не пиши «это "
        f"демонстрационные данные», «данные могут отличаться от "
        f"официальных», «информация заполнена приблизительно» и подобное. "
        f"Значение колонки data_status в ответе не упоминай вообще — просто "
        f"назови цифры и факты, которые пришли из базы.\n"
        f"6. НЕ отправляй пользователя никуда за ответом. Запрещены ссылки, "
        f"адреса страниц и любые отсылки: «подробнее на сайте ИГУ», "
        f"«уточняйте в приёмной комиссии», «уточнить можно там-то», «полная "
        f"информация по ссылке», «обратитесь в деканат». Ответ должен быть "
        f"самодостаточным: человек спросил — человек получил ответ. Если "
        f"данные различаются по форме обучения или году, просто перечисли "
        f"варианты и назови различие — этого достаточно.\n"
        f"7. Если значение в данных пустое (NULL) — коротко скажи, что этих "
        f"сведений пока нет. Не подставляй вместо пустого значения никакое "
        f"своё и не отправляй никуда за ним.\n"
        f"8. ПРОХОДНОЙ БАЛЛ всегда называй вместе с годом, формой обучения и "
        f"основой (бюджет или контракт): «в 2025 году проходной балл на "
        f"<направление> (очная форма, бюджет) — <N>». Минимальный балл — это "
        f"порог допуска к конкурсу, а не проходной; не путай их и не "
        f"подменяй один другим.\n"
        f"9. Если вопрос неоднозначен и в данных несколько вариантов, "
        f"различающихся направлением, уровнем образования, формой обучения, "
        f"годом или основой обучения (бюджет/контракт) — не выбирай молча. "
        f"Покажи, что нашлось, и переспроси, какой вариант нужен."
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


def _request_body(system_text: str, user_text: str, temperature: float) -> dict:
    return {
        "modelUri": build_model_uri(),
        "completionOptions": {
            "stream": False,
            "temperature": temperature,
            "maxTokens": 2000,
        },
        "messages": [
            {"role": "system", "text": system_text},
            {"role": "user", "text": user_text},
        ],
    }


def _request_headers() -> dict:
    return {
        "Authorization": f"Api-Key {API_KEY}",
        "Content-Type": "application/json",
    }


def _parse_reply(data) -> tuple[str | None, str, bool]:
    try:
        return data["result"]["alternatives"][0]["message"]["text"], "", False
    except (KeyError, IndexError, TypeError):
        # Ответ пришёл, но формы не той — повтор не поможет.
        return None, f"[Неожиданный ответ] {data}", False


async def _acall_gpt_once(
    system_text: str, user_text: str, temperature: float
) -> tuple[str | None, str, bool]:
    """Один асинхронный вызов API. Контракт как у _call_gpt_once()."""
    try:
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT_S) as client:
            resp = await client.post(
                API_URL,
                json=_request_body(system_text, user_text, temperature),
                headers=_request_headers(),
            )
    except httpx.TimeoutException:
        return None, f"[Ошибка сети] превышен таймаут {LLM_TIMEOUT_S} с", True
    except httpx.HTTPError as e:
        return None, f"[Ошибка сети] {e}", True
    except UnicodeError as e:
        # Не-ASCII в ключе или заголовках: ошибка конфигурации, не сбой сети.
        return None, f"[Ошибка запроса] {e}", False

    if resp.status_code >= 400:
        return (
            None,
            f"[Ошибка API {resp.status_code}] {resp.text}",
            resp.status_code in _RETRYABLE_HTTP_CODES,
        )

    try:
        data = resp.json()
    except ValueError as e:
        return None, f"[Ошибка разбора ответа] {e}", True

    return _parse_reply(data)


async def acall_gpt(
    system_text: str, user_text: str, temperature: float = LLM_TEMPERATURE
) -> str:
    """Асинхронный вызов Yandex GPT с повторами.

    Веб-путь ходит сюда: обращение к модели занимает от секунды до
    таймаута в 20 с, и раньше на это время вставал весь событийный цикл,
    так что остальные пользователи ждали чужой запрос целиком.
    """
    last_error = "[Ошибка] вызов модели не выполнен"

    for attempt in range(LLM_RETRIES + 1):
        text, error, retryable = await _acall_gpt_once(
            system_text, user_text, temperature
        )
        if text is not None:
            return text

        last_error = error
        if not retryable or attempt == LLM_RETRIES:
            break

        print(
            f"[acall_gpt] попытка {attempt + 1} из {LLM_RETRIES + 1} не удалась: "
            f"{error[:150]} — повторяю",
            file=sys.stderr,
        )
        await asyncio.sleep(LLM_RETRY_PAUSE_S * (attempt + 1))

    return last_error


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


def run_sql_through_security(sql: str, role: str | None = None) -> str:
    # CLI-путь (main()) отдаёт сюда непроверенный SQL, поэтому валидация
    # выполняется здесь, а в БД уходит уже проверенный текст:
    # execute_validated_sql() его повторно не валидирует.
    try:
        safe_sql = security.validate_sql(sql, role)
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