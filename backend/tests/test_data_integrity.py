"""Проверки достоверности данных и целостности расписания.

    python backend/tests/test_data_integrity.py

В отличие от test_security_and_auth.py, этим проверкам НУЖНА база: они
смотрят не на код, а на то, что в ней лежит. Если связи с БД нет, скрипт
честно сообщает об этом и выходит с кодом 2 — «не проверено», а не «всё
хорошо».

Как и соседний файл, это обычный скрипт на assert'ах с функциями test_*:
pytest подберёт их как есть, если когда-нибудь появится.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backend" / "scripts"))

import _common  # noqa: E402

VALID_STATUSES = {"official", "historical", "demo", "unverified"}

# Таблицы официального справочника и их естественные ключи. Ключ — это и есть
# гарантия идемпотентности: повторный INSERT ... ON CONFLICT по нему обновляет
# строку, а не плодит копии.
NATURAL_KEYS = {
    "university_units": ["official_name"],
    "edu_programs": ["unit_id", "code", "level", "study_form", "profile"],
    "entrance_exams": ["name"],
    "program_exams": ["program_id", "admission_year", "exam_id", "slot"],
    "minimum_scores": ["admission_year", "exam_id", "program_id", "level"],
    "passing_scores": ["program_id", "admission_year", "study_form",
                       "funding_basis", "competition_group"],
    "enrollment_places": ["program_id", "admission_year", "study_form",
                          "funding_basis", "quota_kind"],
    "tuition_fees": ["program_id", "academic_year", "study_form"],
    "admission_deadlines": ["admission_year", "level", "study_form",
                            "funding_basis", "stage"],
    "admission_documents": ["admission_year", "level", "applicant_category",
                            "doc_name"],
    "benefits_quotas": ["admission_year", "kind", "title"],
    "dormitories": ["name"],
    "campus_buildings": ["name", "address"],
    "contacts": ["scope", "title"],
    "faq_entries": ["category", "question"],
    "lesson_occurrences": ["schedule_id", "lesson_date"],
    "academic_terms": ["academic_year", "name"],
}

# Таблицы, у которых после миграции 005 должна быть колонка data_status.
STATUS_TABLES = [
    "faculties", "programs", "groups", "schedule", "admission_campaigns",
    "departments", "teachers", "rooms", "subjects",
    *NATURAL_KEYS.keys(),
]

_conn = None


def conn():
    global _conn
    if _conn is None:
        _conn = _common.connect()
        _conn.read_only = True
    return _conn


def q(sql, params=None):
    """SELECT с переподключением.

    Стенд рвёт соединения посреди работы — это задокументировано в
    backend/app/db.py и наблюдалось прямо на прогоне этих тестов: одно
    падение соединения роняло все оставшиеся проверки разом, и отчёт
    показывал «10 из 11 не пройдено» вместо реальной картины.
    """
    global _conn
    last = None
    for attempt in range(3):
        try:
            with conn().cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        except (psycopg.OperationalError, psycopg.InterfaceError) as e:
            last = e
            try:
                if _conn is not None:
                    _conn.close()
            except psycopg.Error:
                pass
            _conn = None
            time.sleep(1.0 * (attempt + 1))
    raise last


def _existing_tables() -> set[str]:
    return {
        name for (name,) in q(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'assistant'"
        )
    }


# ---------------------------------------------------------------------
# Достоверность
# ---------------------------------------------------------------------

def test_data_status_values_are_from_dictionary():
    """Ни одной записи со статусом вне словаря."""
    tables = _existing_tables()
    for table in STATUS_TABLES:
        if table not in tables:
            continue
        columns = {
            c for (c,) in q(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'assistant' AND table_name = %s",
                (table,),
            )
        }
        if "data_status" not in columns:
            continue
        bad = q(
            f"SELECT DISTINCT data_status FROM assistant.{table} "
            f"WHERE data_status <> ALL(%s)",
            (sorted(VALID_STATUSES),),
        )
        assert not bad, f"{table}: недопустимые статусы {bad}"


def test_official_rows_carry_a_source():
    """Официальная запись без ссылки на источник — это не официальная запись."""
    tables = _existing_tables()
    for table in NATURAL_KEYS:
        if table not in tables:
            continue
        columns = {
            c for (c,) in q(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'assistant' AND table_name = %s",
                (table,),
            )
        }
        if not {"data_status", "source_url"} <= columns:
            continue
        (count,), = q(
            f"SELECT count(*) FROM assistant.{table} "
            f"WHERE data_status = 'official' AND source_url IS NULL"
        )
        assert count == 0, f"{table}: {count} официальных записей без source_url"


def test_passing_scores_never_lose_their_context():
    """Проходной балл бессмысленен без года, формы и основы обучения."""
    if "passing_scores" not in _existing_tables():
        return
    (count,), = q(
        "SELECT count(*) FROM assistant.passing_scores "
        "WHERE admission_year IS NULL OR study_form IS NULL "
        "   OR funding_basis IS NULL OR competition_group IS NULL"
    )
    assert count == 0, f"{count} проходных баллов без обязательного контекста"


def test_minimum_and_passing_scores_are_separate_tables():
    """Порог допуска и балл последнего зачисленного не должны смешиваться."""
    tables = _existing_tables()
    if not {"minimum_scores", "passing_scores"} <= tables:
        return
    min_columns = {
        c for (c,) in q(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'assistant' AND table_name = 'minimum_scores'"
        )
    }
    assert "min_score" in min_columns
    assert "score" not in min_columns, "в minimum_scores не должно быть проходного балла"


# ---------------------------------------------------------------------
# Расписание
# ---------------------------------------------------------------------

def test_public_schedule_carries_no_person():
    """В публичном расписании физически нет полей о людях.

    public_schedule отдаётся роли `guest`, токен которой выдаётся БЕЗ ПАРОЛЯ
    любому посетителю сайта через встраиваемый виджет. Защита здесь не в том,
    что модель попросили не выбирать ФИО, а в том, что такой колонки НЕТ:
    запрос `SELECT teacher_name FROM public_schedule` не отклоняется
    проверкой — он падает в СУБД, потому что выбирать нечего.

    Если кто-то добавит колонку обратно, тест сломается раньше, чем виджет
    попадёт на сайт вуза.
    """
    tables = _existing_tables()
    if "public_schedule" not in tables:
        return
    columns = {
        c for (c,) in q(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'assistant' AND table_name = 'public_schedule'"
        )
    }
    for forbidden in ("teacher_name", "teacher_id", "full_name", "last_name",
                      "first_name", "email", "phone"):
        assert forbidden not in columns, (
            f"public_schedule не должен содержать {forbidden}: "
            f"он доступен анонимному посетителю сайта"
        )
    # И то, ради чего представление существует, на месте.
    assert {"lesson_date", "subject_name", "group_name", "room_number"} <= columns


def test_no_schedule_conflicts():
    """Группа, преподаватель и аудитория не могут быть в двух местах сразу."""
    tables = _existing_tables()
    for view in ("schedule_conflicts_group", "schedule_conflicts_teacher",
                 "schedule_conflicts_room"):
        if view not in tables:
            continue
        (count,), = q(f"SELECT count(*) FROM assistant.{view}")
        assert count == 0, f"{view}: осталось {count} пересечений"


def test_lesson_occurrences_are_consistent():
    """Даты занятий соответствуют дню недели и попадают в свой семестр."""
    if "lesson_occurrences" not in _existing_tables():
        return

    # Отменённые проведения из проверок исключаются: они намеренно оставлены
    # в таблице как история переносов (см. 010_lesson_cancellation.sql) и в
    # schedule_calendar не попадают.
    active = "lo.cancelled_at IS NULL"

    (wrong_weekday,), = q(
        "SELECT count(*) FROM assistant.lesson_occurrences lo "
        "JOIN assistant.schedule s ON s.id = lo.schedule_id "
        f"WHERE {active} AND EXTRACT(ISODOW FROM lo.lesson_date)::int <> s.weekday"
    )
    assert wrong_weekday == 0, f"{wrong_weekday} занятий стоят не в свой день недели"

    (outside,), = q(
        "SELECT count(*) FROM assistant.lesson_occurrences lo "
        "JOIN assistant.academic_terms t ON t.id = lo.term_id "
        f"WHERE {active} AND (lo.lesson_date < t.starts_on OR lo.lesson_date > t.ends_on)"
    )
    assert outside == 0, f"{outside} занятий вне границ своего семестра"

    # Одна группа — одна пара в один момент времени, уже по конкретным датам.
    (clashes,), = q(
        "SELECT count(*) FROM ("
        "  SELECT s.group_id, lo.lesson_date, s.pair_number "
        "  FROM assistant.lesson_occurrences lo "
        "  JOIN assistant.schedule s ON s.id = lo.schedule_id "
        f"  WHERE {active} "
        "  GROUP BY 1, 2, 3 HAVING count(*) > 1"
        ") x"
    )
    assert clashes == 0, f"{clashes} совпадений группа+дата+пара"

    # То же для преподавателя и аудитории — по конкретным датам, а не по
    # недельной сетке: именно это видит студент, открывая расписание.
    for resource, join, label in (
        ("c.teacher_id",
         "JOIN assistant.curriculum c ON c.id = s.curriculum_id", "преподаватель"),
        ("s.room_id", "", "аудитория"),
    ):
        (count,), = q(
            "SELECT count(*) FROM ("
            f"  SELECT {resource}, lo.lesson_date, s.pair_number "
            "  FROM assistant.lesson_occurrences lo "
            "  JOIN assistant.schedule s ON s.id = lo.schedule_id "
            f"  {join} "
            f"  WHERE {active} AND {resource} IS NOT NULL "
            "  GROUP BY 1, 2, 3 HAVING count(*) > 1"
            ") x"
        )
        assert count == 0, f"{count} пересечений по ресурсу «{label}» на конкретных датах"


def test_every_pair_has_a_time():
    """Пара без времени начала не позволяет ответить «во сколько»."""
    tables = _existing_tables()
    if "pair_times" not in tables:
        return
    (missing,), = q(
        "SELECT count(DISTINCT s.pair_number) FROM assistant.schedule s "
        "WHERE NOT EXISTS (SELECT 1 FROM assistant.pair_times pt "
        "                  WHERE pt.pair_number = s.pair_number)"
    )
    assert missing == 0, f"{missing} номеров пар без времени в pair_times"


# ---------------------------------------------------------------------
# Идемпотентность и неразрушительность
# ---------------------------------------------------------------------

def test_every_reference_table_has_a_natural_key():
    """Без уникального ключа повторный запуск сидера наплодил бы дубли."""
    tables = _existing_tables()
    for table, key in NATURAL_KEYS.items():
        if table not in tables:
            continue
        indexes = q(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = 'assistant' AND tablename = %s",
            (table,),
        )
        expected = set(key)
        found = any(
            "UNIQUE" in definition
            and expected <= set(re.findall(r"[a-z_]+", definition.split("(")[-1]))
            for (definition,) in indexes
        )
        assert found, f"{table}: нет уникального индекса по {key}"


def test_no_duplicates_in_reference_tables():
    tables = _existing_tables()
    for table, key in NATURAL_KEYS.items():
        if table not in tables:
            continue
        columns = ", ".join(key)
        (dupes,), = q(
            f"SELECT count(*) FROM ("
            f"  SELECT {columns} FROM assistant.{table} "
            f"  GROUP BY {columns} HAVING count(*) > 1"
            f") x"
        )
        assert dupes == 0, f"{table}: {dupes} дублей по естественному ключу"


def test_nothing_was_deleted_since_the_backup():
    """Счётчики существующих таблиц не должны падать ниже снимка."""
    latest = REPO_ROOT / "backend" / "backups" / "latest.json"
    if not latest.exists():
        print("  (снимка нет — проверка пропущена, запустите backup_db.py)")
        return
    with open(latest, encoding="utf-8") as fh:
        manifest_path = json.load(fh)["manifest"]
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    for key, info in manifest["tables"].items():
        schema, table = key.split(".", 1)
        (count,), = q(f'SELECT count(*) FROM "{schema}"."{table}"')
        assert count >= info["rows"], (
            f"{key}: было {info['rows']}, стало {count} — записи исчезли"
        )


def test_scripts_contain_no_destructive_statements():
    """В операторских скриптах не должно быть DELETE, TRUNCATE и DROP TABLE."""
    pattern = re.compile(
        r"\b(delete\s+from|truncate|drop\s+(table|database|schema))\b",
        re.IGNORECASE,
    )
    scripts = (REPO_ROOT / "backend" / "scripts").glob("*.py")
    for path in scripts:
        text = path.read_text(encoding="utf-8")
        # apply_sql.py содержит этот шаблон намеренно — он им ЗАПРЕЩАЕТ такие
        # конструкции в миграциях, а не выполняет их.
        if path.name == "apply_sql.py":
            continue
        hit = pattern.search(text)
        assert hit is None, f"{path.name}: найдено '{hit.group(0)}'"


def main() -> int:
    tests = [(name, value) for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    try:
        conn()
    except SystemExit as e:
        print(f"Нет связи с БД: {e}", file=sys.stderr)
        return 2

    failures = 0
    for name, test in tests:
        try:
            test()
            print(f"  [OK]   {name}")
        except AssertionError as e:
            failures += 1
            print(f"  [FAIL] {name}: {e}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — отчёт важнее трассировки
            failures += 1
            print(f"  [ERR]  {name}: {type(e).__name__}: {e}", file=sys.stderr)

    print(f"\nПройдено {len(tests) - failures} из {len(tests)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
