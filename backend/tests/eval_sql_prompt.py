"""Замер качества промпта генерации SQL.

Не юнит-тест: ходит в живую БД и в Yandex GPT, поэтому в общий прогон не
входит и времени стоит заметно. Запуск вручную:

    python backend/tests/eval_sql_prompt.py [повторов_на_вопрос]

Оценка сквозная: вопрос -> модель -> SQL -> проверка безопасности ->
выполнение в БД -> сравнение ЧИСЛА с эталоном, который считается тем же
прогоном отдельным запросом. Текст SQL не сверяем: правильных формулировок
много, важен ответ.

Нужен, чтобы правки промпта можно было подтверждать цифрой, а не на глаз:
прогнать до правки, прогнать после, сравнить проценты.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app import ai_agent, db, security  # noqa: E402

# (вопрос, эталонный SQL). Эталон считается в этом же прогоне, чтобы цифры
# не устаревали вместе с данными.
CASES = [
    ("сколько студентов учится на юриспруденции",
     "SELECT SUM(student_count) FROM students_summary WHERE program_name ILIKE '%юриспруденц%'"),
    ("сколько студентов на бакалавриате",
     "SELECT SUM(student_count) FROM students_summary WHERE degree = 'бакалавриат'"),
    ("сколько студентов в магистратуре",
     "SELECT SUM(student_count) FROM students_summary WHERE degree = 'магистратура'"),
    ("сколько студентов учится на контракте",
     "SELECT SUM(student_count) FROM students_summary WHERE funding = 'контракт'"),
    ("сколько студентов на бюджете",
     "SELECT SUM(student_count) FROM students_summary WHERE funding = 'бюджет'"),
    ("сколько первокурсников",
     "SELECT SUM(student_count) FROM students_summary WHERE course = 1"),
    ("сколько студентов отчислено",
     "SELECT SUM(student_count) FROM students_summary WHERE status = 'отчислен'"),
    ("сколько всего студентов в университете",
     "SELECT SUM(student_count) FROM students_summary"),
    ("сколько преподавателей работает в университете",
     "SELECT count(*) FROM teachers"),
    ("сколько аудиторий типа лаборатория",
     "SELECT count(*) FROM rooms WHERE room_type = 'лаборатория'"),
    ("сколько направлений бакалавриата в университете",
     "SELECT count(*) FROM edu_programs WHERE level = 'бакалавриат'"),
    ("сколько направлений очной формы обучения",
     "SELECT count(*) FROM edu_programs WHERE study_form = 'очная'"),
    ("сколько факультетов и институтов в ИГУ",
     "SELECT count(*) FROM university_units"),
]


# Вопросы абитуриента и студента из технического задания. Здесь сверяется НЕ
# значение, а маршрутизация: попала ли модель в нужную таблицу. Для «какие
# факультеты есть в ИГУ» правильных ответов много и все они списки — сравнивать
# первую ячейку с эталоном бессмысленно, а вот обращение к university_units
# вместо демонстрационной faculties проверяется однозначно.
#
# Набор намеренно включает опечатки, сокращения, падежи и разговорные
# формулировки: промпт должен переживать то, как люди пишут на самом деле.
ROUTING_CASES = [
    # --- абитуриент: структура и направления
    ("какие факультеты есть в ИГУ", {"university_units"}),
    ("какие институты в игу", {"university_units"}),
    ("какие специальности есть в институте математики",
     {"programs_admission", "edu_programs", "university_units"}),
    ("какие направления подготовки доступны", {"programs_admission", "edu_programs"}),
    # --- абитуриент: испытания и баллы
    ("что сдавать на программирование", {"programs_admission", "program_exam_sets"}),
    ("какие экзамены нужны на юриспруденцию",
     {"programs_admission", "program_exam_sets"}),
    ("какой минимальный балл по русскому языку", {"minimum_scores_view"}),
    ("какой был проходной балл на биологию в прошлом году", {"passing_scores_view"}),
    ("чем минимальный балл отличается от проходного",
     {"faq_entries", "minimum_scores_view", "passing_scores_view"}),
    ("я сдаю русский математику и информатику куда могу поступить",
     {"program_exam_sets"}),
    ("у меня 240 баллов хватит ли на бюджет",
     {"passing_scores_view", "minimum_scores_view"}),
    ("есть ли внутренние вступительные экзамены",
     {"entrance_exams", "program_exams", "faq_entries"}),
    # --- абитуриент: места, деньги, сроки
    ("сколько бюджетных мест", {"programs_admission", "enrollment_places"}),
    ("сколько стоит обучение", {"programs_admission", "tuition_fees"}),
    ("до какого числа подавать документы", {"admission_deadlines"}),
    ("когда заканчивается приём на бюджет", {"admission_deadlines"}),
    ("когда завершается приём оригиналов", {"admission_deadlines"}),
    ("какие документы нужны для поступления", {"admission_documents"}),
    ("можно ли подать документы онлайн", {"faq_entries"}),
    # --- абитуриент: быт и льготы
    ("есть ли общежитие", {"dormitories"}),
    ("кому дают общагу", {"dormitories"}),
    ("есть ли целевое обучение и льготы", {"benefits_quotas"}),
    ("как связаться с приемной комиссией", {"contacts"}),
    ("где находится приёмная комиссия", {"contacts", "campus_buildings"}),
    # --- студент: расписание
    ("какое у меня расписание сегодня", {"my_schedule", "schedule_calendar"}),
    ("что завтра по расписанию у группы ФИТ-0925-1", {"schedule_calendar"}),
    ("какая следующая пара", {"schedule_calendar", "my_schedule"}),
    ("в какой аудитории занятие", {"schedule_calendar", "my_schedule"}),
    ("во сколько начинается вторая пара", {"pair_times", "schedule_calendar"}),
    ("кто ведёт предмет", {"schedule_calendar", "my_schedule", "curriculum"}),
    ("расписание на 15 сентября", {"schedule_calendar"}),
]


def _tables_in(sql: str) -> set[str]:
    return {t.lower() for t in security._extract_table_refs(sql)}


def run_routing(prompt: str, repeats: int) -> tuple[int, int, list]:
    """Проверяет, в какую таблицу модель направляет вопрос."""
    total = passed = 0
    misses = []
    print("\nМАРШРУТИЗАЦИЯ ВОПРОСОВ (в какую таблицу пошла модель)\n")
    for question, expected in ROUTING_CASES:
        marks = []
        for _ in range(repeats):
            total += 1
            reply = ai_agent.call_gpt(prompt, question)
            sql = ai_agent.extract_sql(reply)
            if not sql:
                marks.append("нет SQL")
                misses.append((question, "модель не вернула SQL", reply[:90]))
                continue
            used = _tables_in(sql)
            if used & expected:
                passed += 1
                marks.append("ok")
            else:
                marks.append(",".join(sorted(used)) or "?")
                misses.append((
                    question,
                    f"ожидались {sorted(expected)}, использованы {sorted(used)}",
                    " ".join(sql.split())[:90],
                ))
        print(f"  [{'/'.join(marks):<26}] {question}")
    return passed, total, misses


def _first_cell(result: str) -> str:
    if not result or result.startswith("["):
        return ""
    lines = result.splitlines()
    return lines[0].split("|")[0].strip() if lines else ""


def main(repeats: int = 2) -> int:
    prompt = ai_agent.build_sql_system_prompt()
    print(f"промпт: {len(prompt)} символов, повторов на вопрос: {repeats}\n")

    total = passed = 0
    failures = []

    for question, reference_sql in CASES:
        expected = _first_cell(
            security.execute_validated_sql(security.validate_sql(reference_sql))
        )

        marks = []
        for _ in range(repeats):
            total += 1
            reply = ai_agent.call_gpt(prompt, question)
            sql = ai_agent.extract_sql(reply)
            if not sql:
                marks.append("нет SQL")
                failures.append((question, "модель не вернула SQL", reply[:90]))
                continue
            try:
                safe = security.validate_sql(sql)
            except security.SQLSecurityError as e:
                marks.append("отклонён")
                failures.append((question, f"security: {e}", sql[:90]))
                continue

            got = _first_cell(security.execute_validated_sql(safe))
            if got and got == expected:
                passed += 1
                marks.append("ok")
            else:
                marks.append(f"{got or 'пусто'}!={expected}")
                failures.append((
                    question,
                    f"получено {got or 'пусто'}, ожидалось {expected}",
                    " ".join(sql.split())[:90],
                ))

        print(f"  [{'/'.join(marks):<26}] эталон={expected:<6} {question}")

    print(f"\nЗНАЧЕНИЯ: {passed}/{total} = {100 * passed / total:.0f}%")
    if failures:
        print("\nразбор полётов:")
        for q, why, extra in failures[:14]:
            print(f"  - {q}\n      {why}\n      {extra}")

    if "--no-routing" not in sys.argv:
        r_passed, r_total, misses = run_routing(prompt, repeats)
        print(f"\nМАРШРУТИЗАЦИЯ: {r_passed}/{r_total} = "
              f"{100 * r_passed / r_total:.0f}%")
        if misses:
            print("\nпромахи маршрутизации:")
            for q, why, extra in misses[:14]:
                print(f"  - {q}\n      {why}\n      {extra}")

    db.close_all()
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(main(int(args[0]) if args else 2))
