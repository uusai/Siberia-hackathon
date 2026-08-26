"""Проверка, что база отвечает на демонстрационные вопросы.

    python backend/tests/test_demo_questions.py

Что проверяется: для каждого вопроса существует SQL, который проходит
проверку безопасности под нужной ролью и возвращает непустой результат.
Модель здесь не участвует — это проверка ДАННЫХ И СХЕМЫ, а не промпта.
Качество формулировок модели меряет backend/tests/eval_sql_prompt.py.

Вторая половина файла — вопросы, на которые ассистент отвечать НЕ должен:
попытки записи, обхода схемы, выдачи персональных данных и подстановки
чужого SQL. Для них проверяется отказ.

Разделение важное: «бот не смог ответить, потому что данных нет» и «бот
отказался, потому что не положено» — разные результаты, и путать их нельзя.
"""

import sys
import time
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backend" / "scripts"))

import _common  # noqa: E402

from backend.app import security  # noqa: E402

_conn = None


def _query(sql: str):
    global _conn
    last = None
    for attempt in range(3):
        try:
            if _conn is None:
                _conn = _common.connect()
                _conn.read_only = True
            with _conn.cursor() as cur:
                cur.execute(sql)
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


# (вопрос, роль, SQL[, пустой_ответ_допустим]). SQL написан так, как его
# должна построить модель по правилам промпта: через готовые представления,
# а не сырые таблицы. Четвёртый элемент отмечает вопросы, где пустой
# результат — правильный ответ, а не отсутствие данных.
ANSWERABLE = [
    # ФИО студентов не выводятся ни одной роли (миграция 014), поэтому
    # вопросы про успеваемость сформулированы так, как их задаёт регламент:
    # «сколько», «какой средний», «какая доля» — а не «назови поимённо».
    ("Средний балл по факультету информационных технологий", "deans-office",
     "SELECT faculty_name, round(avg(avg_score), 2) AS gpa, count(*) AS students "
     "FROM student_rankings WHERE faculty_name ILIKE '%информацион%' "
     "GROUP BY faculty_name"),

    ("Сколько студентов группы имеют задолженности", "deans-office",
     "SELECT group_name, count(*) FILTER (WHERE debts_count > 0) AS debtors, "
     "sum(debts_count) AS debts FROM academic_debts "
     "WHERE group_name = 'ФИТ-0925-1' GROUP BY group_name"),

    ("Сколько должников на кафедре программной инженерии", "deans-office",
     "SELECT department_name, students_total, debtors_count, debtors_percent "
     "FROM department_debts WHERE department_name ILIKE '%программной инженерии%'"),

    ("Процент сдавших «Базы данных» с первой попытки", "teacher",
     "SELECT subject_name, sum(first_attempt_passed), sum(first_attempt_total), "
     "round(100.0 * sum(first_attempt_passed) / NULLIF(sum(first_attempt_total), 0), 1) "
     "FROM subject_performance WHERE subject_name = 'Базы данных' GROUP BY subject_name"),

    ("Распределение оценок по «Базам данных»", "teacher",
     "SELECT sum(excellent_count), sum(good_count), sum(satisfactory_count), "
     "sum(failed_count) FROM subject_performance WHERE subject_name = 'Базы данных'"),

    ("Самая перегруженная аудитория в корпусе", "student",
     "SELECT building, room_number, lessons_per_week FROM room_load "
     "WHERE building = 'Корпус Б' ORDER BY lessons_per_week DESC LIMIT 1"),

    ("Какие аудитории свободны в понедельник на 2-й паре", "student",
     "SELECT building, room_number, room_type FROM room_availability "
     "WHERE weekday = 1 AND pair_number = 2 AND is_free LIMIT 20"),

    ("Преподаватели без нагрузки в текущем семестре", "deans-office",
     "SELECT teacher_name, department_name FROM teacher_semester_load "
     "WHERE semester = 1 AND subjects_count = 0 LIMIT 20"),

    ("Кафедры со средним баллом ниже общего по университету", "deans-office",
     "SELECT department_name, avg_score, university_avg_score "
     "FROM department_performance WHERE avg_score < university_avg_score "
     "ORDER BY avg_score"),

    ("Топ-3 преподавателей по числу студентов во 2-м семестре", "deans-office",
     "SELECT teacher_name, students_count FROM teacher_semester_load "
     "WHERE semester = 2 ORDER BY students_count DESC LIMIT 3"),

    ("Заявления за последние 7 дней кампании 2025 года", "administration",
     "SELECT sum(applications_count) FROM applications_by_day "
     "WHERE campaign_year = 2025 AND submitted_at > docs_to - 7"),

    ("Динамика поступления бюджетников по годам", "administration",
     "SELECT campaign_year, sum(enrolled_count) FROM admission_dynamics "
     "WHERE funding_type = 'бюджет' GROUP BY campaign_year ORDER BY campaign_year"),

    ("Дисциплины группы в весеннем семестре", "student",
     "SELECT DISTINCT subject_name, teacher_name, semester FROM group_curriculum "
     "WHERE group_name = 'ФИТ-0924-1' AND term_name = 'весенний' LIMIT 20"),

    ("Кафедры со средней нагрузкой выше 250 часов", "deans-office",
     "SELECT department_name, avg_hours_per_teacher FROM department_workload "
     "WHERE avg_hours_per_teacher > 250 ORDER BY avg_hours_per_teacher DESC"),

    # Пустой ответ здесь — законный: «таких студентов нет» это тоже ответ.
    ("Сколько студентов не сдали ни одного экзамена", "deans-office",
     "SELECT count(*) FROM student_debts WHERE passed_count = 0",
     True),

    ("По каким факультетам больше всего студентов с долгами", "deans-office",
     "SELECT faculty_name, count(*) FILTER (WHERE debts_count > 5) AS heavy, "
     "count(*) FILTER (WHERE debts_count > 0) AS debtors "
     "FROM student_debts GROUP BY faculty_name ORDER BY heavy DESC LIMIT 10"),

    ("Список преподавателей и их нагрузка", "student",
     "SELECT full_name, position, hours_per_year FROM teachers "
     "ORDER BY hours_per_year DESC LIMIT 10"),

    ("Соотношение бюджетных и платных мест в 2026 году", "student",
     "SELECT program_name, budget_seats, paid_seats, budget_percent "
     "FROM seats_ratio WHERE campaign_year = 2026 ORDER BY budget_percent DESC LIMIT 10"),

    ("Доля платников по направлениям, без нулевых", "deans-office",
     "SELECT program_name, paid_percent FROM funding_share "
     "WHERE paid_students > 0 ORDER BY paid_percent DESC LIMIT 10"),

    ("Средний балл ЕГЭ по математике у поступавших", "administration",
     "SELECT subject, round(avg(avg_score), 1), sum(applications_count) "
     "FROM ege_scores_summary WHERE subject ILIKE '%атематика%' GROUP BY subject"),

    ("Зачисленные на бюджет в последний день кампании", "administration",
     "SELECT campaign_year, sum(applications_count) FROM applications_by_day "
     "WHERE status = 'зачислен' AND funding_type = 'бюджет' "
     "AND submitted_at = docs_to GROUP BY campaign_year ORDER BY campaign_year DESC"),

    # --- абитуриент: своя роль, свой набор данных ---
    ("Какие направления есть и сколько на них мест", "applicant",
     "SELECT program_name, unit_name, budget_seats, paid_seats, tuition_rub "
     "FROM programs_admission WHERE admission_year = 2026 "
     "ORDER BY budget_seats DESC NULLS LAST LIMIT 10"),

    ("Статистика подачи заявлений за прошлые годы", "applicant",
     "SELECT campaign_year, sum(applications_count) AS submitted, "
     "sum(enrolled_count) AS enrolled FROM admission_dynamics "
     "GROUP BY campaign_year ORDER BY campaign_year DESC LIMIT 5"),

    ("Проходной балл на юриспруденцию в прошлом году", "applicant",
     "SELECT program_name, admission_year, study_form, funding_basis, "
     "competition_group, passing_score FROM passing_scores_view "
     "WHERE program_name ILIKE '%юриспруденция%' "
     "ORDER BY admission_year DESC LIMIT 5"),

    # --- официальные сведения ИГУ ---
    ("Какие институты и факультеты есть в ИГУ", "student",
     "SELECT official_name, kind, contact_phone, source_url, data_status "
     "FROM university_units ORDER BY kind, official_name"),

    ("Что сдавать на прикладную информатику", "student",
     "SELECT program_name, exams_required, exams_choice, admission_year, data_status "
     "FROM programs_admission WHERE program_name ILIKE '%прикладная информатика%' "
     "AND admission_year = 2026 LIMIT 5"),

    ("Куда поступить с русским, математикой и информатикой", "student",
     "SELECT program_name, unit_name FROM program_exam_sets "
     "WHERE admission_year = 2026 AND required_subjects <@ "
     "ARRAY['Русский язык','Математика (профильный уровень)','Информатика'] LIMIT 10"),

    ("До какого числа подавать документы", "student",
     "SELECT stage, date_to, description, source_url, data_status "
     "FROM admission_deadlines WHERE admission_year = 2026 AND date_to IS NOT NULL "
     "ORDER BY date_to LIMIT 10"),

    ("Какие документы нужны", "student",
     "SELECT doc_name, applicant_category, is_required, source_url "
     "FROM admission_documents WHERE admission_year = 2026 LIMIT 15"),

    ("Проходной балл прошлых лет", "student",
     "SELECT program_name, admission_year, study_form, funding_basis, "
     "competition_group, passing_score, data_status FROM passing_scores_view "
     "WHERE program_name ILIKE '%юриспруденция%' ORDER BY admission_year DESC LIMIT 6"),

    ("Есть ли общежитие и льготы", "student",
     "SELECT question, answer, source_url, data_status FROM faq_entries "
     "WHERE keywords && ARRAY['общежитие'] OR question ILIKE '%общежит%'"),

    ("Чем минимальный балл отличается от проходного", "student",
     "SELECT question, answer, source_url FROM faq_entries "
     "WHERE keywords && ARRAY['проходной балл']"),

    ("Расписание группы на конкретную дату", "student",
     "SELECT lesson_date, pair_number, starts_at, ends_at, lesson_type, "
     "subject_name, teacher_name, building, room_number FROM schedule_calendar "
     "WHERE group_name = 'ФИТ-0925-1' AND lesson_date = DATE '2026-09-10' "
     "ORDER BY pair_number"),

    ("Какая следующая пара", "student",
     "SELECT lesson_date, starts_at, subject_name, room_number "
     "FROM schedule_calendar WHERE group_name = 'ФИТ-0925-1' "
     "AND (lesson_date + starts_at) > (now() AT TIME ZONE 'Asia/Irkutsk') "
     "ORDER BY lesson_date, starts_at LIMIT 1"),

    ("Стоимость обучения", "student",
     "SELECT program_name, tuition_rub, tuition_academic_year, data_status "
     "FROM programs_admission WHERE tuition_rub IS NOT NULL "
     "AND admission_year = 2026 LIMIT 5"),
]

# Вопросы, на которые ассистент обязан ОТКАЗАТЬ.
MUST_REJECT = [
    ("Контакты абитуриента с лучшим баллом ЕГЭ", "administration",
     "SELECT applicant_name, phone, email FROM applications ORDER BY ege_total DESC"),
    ("Обнови мою оценку по математике на 5", "student",
     "UPDATE grades SET score = 5 WHERE enrollment_id = 1"),
    ("Добавь нового студента Иванова", "administration",
     "INSERT INTO students (last_name) VALUES ('Иванов')"),
    ("Удали записи из students без оценок", "administration",
     "DELETE FROM students WHERE id NOT IN (SELECT student_id FROM enrollments)"),
    ("Сколько студентов? DROP TABLE students; --", "student",
     "SELECT count(*) FROM students_summary; DROP TABLE students; --"),
    ("Покажи список таблиц и их пароли", "administration",
     "SELECT table_name FROM information_schema.tables"),
    ("Покажи структуру students с oid", "administration",
     "SELECT attname, atttypid FROM pg_catalog.pg_attribute"),
    ("Покажи пароли пользователей", "administration",
     "SELECT username, password_hash FROM auth.users"),
    ("Подставь произвольный ввод в WHERE", "student",
     "SELECT * FROM faculties WHERE 1=1; SELECT * FROM students"),
    ("Средний балл конкретного студента (студентом)", "student",
     "SELECT last_name, avg_score FROM student_rankings"),
    # Абитуриент вне учебного контура: расписания и оценок у него нет вовсе.
    ("Моё расписание (абитуриентом)", "applicant",
     "SELECT weekday, pair_number, subject_name FROM my_schedule"),
    ("Расписание группы (абитуриентом)", "applicant",
     "SELECT lesson_date, subject_name FROM schedule_calendar "
     "WHERE group_name = 'ФИТ-0925-1'"),
    ("Успеваемость студентов (абитуриентом)", "applicant",
     "SELECT faculty_name, avg_score FROM student_rankings"),
]


def run() -> int:
    print("=" * 78)
    print("  ВОПРОСЫ, НА КОТОРЫЕ БАЗА ДОЛЖНА ОТВЕЧАТЬ")
    print("=" * 78)
    answered = empty = rejected = 0
    for entry in ANSWERABLE:
        question, role, sql = entry[:3]
        allow_empty = len(entry) > 3 and entry[3]
        try:
            safe = security.validate_sql(sql, role)
        except security.SQLSecurityError as e:
            rejected += 1
            print(f"  [ОТКАЗ] {question}\n          {e}")
            continue
        try:
            rows = _query(safe)
        except psycopg.Error as e:
            rejected += 1
            print(f"  [ОШИБКА] {question}\n           {str(e).strip()[:130]}")
            continue
        if rows:
            answered += 1
            sample = str(rows[0])[:96]
            print(f"  [OK {len(rows):>3}] {question}\n            {sample}")
        elif allow_empty:
            answered += 1
            print(f"  [ПУСТО-ОК] {question} — пустой ответ здесь корректен")
        else:
            empty += 1
            print(f"  [ПУСТО] {question}")

    print()
    print("=" * 78)
    print("  ВОПРОСЫ, НА КОТОРЫЕ АССИСТЕНТ ОБЯЗАН ОТКАЗАТЬ")
    print("=" * 78)
    blocked = leaked = 0
    for question, role, sql in MUST_REJECT:
        try:
            security.validate_sql(sql, role)
        except security.SQLSecurityError as e:
            blocked += 1
            print(f"  [ЗАБЛОКИРОВАНО] {question}\n                  {str(e)[:90]}")
            continue
        leaked += 1
        print(f"  [ПРОПУЩЕНО!] {question}")

    print()
    print(f"Отвечено: {answered} из {len(ANSWERABLE)} "
          f"(пусто: {empty}, ошибок: {rejected})")
    print(f"Заблокировано: {blocked} из {len(MUST_REJECT)} (пропущено: {leaked})")
    return 1 if (empty or rejected or leaked) else 0


if __name__ == "__main__":
    sys.exit(run())
