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
    ("сколько программ магистратуры",
     "SELECT count(*) FROM programs WHERE degree = 'магистратура'"),
    ("сколько программ заочной формы обучения",
     "SELECT count(*) FROM programs WHERE study_form = 'заочная'"),
]


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

    print(f"\nИТОГО: {passed}/{total} = {100 * passed / total:.0f}%")
    if failures:
        print("\nразбор полётов:")
        for q, why, extra in failures[:14]:
            print(f"  - {q}\n      {why}\n      {extra}")

    db.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 2))
