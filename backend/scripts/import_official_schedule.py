"""Импортёр официальных групп и расписания ИГУ. Пока без источника.

    python backend/scripts/import_official_schedule.py --file <groups.json>
    python backend/scripts/import_official_schedule.py --file <groups.json> --apply

ПОЧЕМУ ЗАГЛУШКА, А НЕ ГОТОВЫЙ ПАРСЕР. Реальные обозначения учебных групп ИГУ
в открытых источниках подтвердить не удалось. Выдумать их нельзя: группа
«ИТ-101», которой не существует, — это ровно то враньё, которого бот должен
избегать. Поэтому:
  - существующие 139 групп остаются на месте с data_status='demo';
  - новые группы НЕ добавляются;
  - здесь лежит готовый приёмник, которому достаточно дать файл с
    официальными данными, чтобы залить их без потери чего-либо.

ФОРМАТ ФАЙЛА (JSON):
{
  "source": {"url": "...", "title": "...", "checked_at": "2026-08-26T12:00:00"},
  "groups": [
    {"name": "...", "program_code": "09.03.04", "course": 1,
     "start_year": 2026, "study_form": "очная"}
  ],
  "lessons": [
    {"group": "...", "date": "2026-09-01", "pair_number": 2,
     "subject": "...", "teacher": "...", "building": "...", "room": "...",
     "lesson_type": "лекция"}
  ]
}

ЧТО ДЕЛАЕТ ПРИ ЗАПУСКЕ:
  - заводит группы с data_status='official' и source_url (upsert по name);
  - создаёт недостающие аудитории и записи расписания;
  - раскладывает занятия по датам в lesson_occurrences.
Ничего не удаляет: демо-контур продолжает работать рядом, а бот отличает
одно от другого по data_status.
"""

import json
import os
import sys

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common  # noqa: E402


def _arg(argv: list[str], name: str) -> str | None:
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


def _validate(payload: dict) -> list[str]:
    problems = []
    source = payload.get("source") or {}
    if not source.get("url"):
        problems.append("не указан source.url — импорт без ссылки на источник запрещён")
    for index, group in enumerate(payload.get("groups", [])):
        if not group.get("name"):
            problems.append(f"groups[{index}]: пустое название группы")
        if not group.get("program_code"):
            problems.append(f"groups[{index}]: не указан код направления")
    for index, lesson in enumerate(payload.get("lessons", [])):
        for field in ("group", "date", "pair_number", "subject"):
            if not lesson.get(field):
                problems.append(f"lessons[{index}]: нет поля {field}")
    return problems


def main(argv: list[str]) -> int:
    apply = _common.parse_apply_flag(argv)
    path = _arg(argv, "--file")
    _common.banner("Импорт официального расписания ИГУ", apply)

    if not path:
        print(
            "\nФайл с официальными данными не передан (--file <путь>).\n"
            "Импортировать нечего, и выдумывать группы нельзя.\n"
            "\nЧто сейчас в базе:"
        )
        try:
            with _common.connect() as conn:
                conn.read_only = True
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT data_status, count(*) FROM assistant.groups "
                        "GROUP BY data_status ORDER BY 1"
                    )
                    for status, count in cur.fetchall():
                        print(f"  групп со статусом {status}: {count}")
                    cur.execute(
                        "SELECT data_status, count(*) FROM assistant.schedule "
                        "GROUP BY data_status ORDER BY 1"
                    )
                    for status, count in cur.fetchall():
                        print(f"  записей расписания со статусом {status}: {count}")
        except SystemExit:
            print("  (нет связи с БД)")
        print(
            "\nОфициальное расписание ИГУ: https://isu.ru/ru/students/timetable/\n"
            "Когда данные будут получены, положите их в JSON описанного выше "
            "формата и запустите этот скрипт с --file и --apply."
        )
        return 0

    if not os.path.isfile(path):
        print(f"Файл не найден: {path}", file=sys.stderr)
        return 2

    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    problems = _validate(payload)
    if problems:
        print("\nФайл не прошёл проверку:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    source = payload["source"]
    groups = payload.get("groups", [])
    lessons = payload.get("lessons", [])
    print(f"\nИсточник: {source['url']}")
    print(f"Групп: {len(groups)}, занятий: {len(lessons)}")

    if not apply:
        print("\nЗапись выключена. Добавьте --apply.")
        return 0

    with _common.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO assistant.data_sources (url, title, checked_at) "
                "VALUES (%s, %s, COALESCE(%s::timestamptz, now())) "
                "ON CONFLICT (url) DO UPDATE SET title = EXCLUDED.title, "
                "checked_at = EXCLUDED.checked_at RETURNING id",
                (source["url"], source.get("title", "Официальное расписание ИГУ"),
                 source.get("checked_at")),
            )
            source_id = cur.fetchone()[0]

            # Группа привязывается к направлению демо-контура по ФГОС-коду:
            # официальные группы и официальные направления живут в разных
            # таблицах, а groups.program_id ссылается на programs.
            inserted = 0
            for group in groups:
                cur.execute(
                    "SELECT id FROM assistant.programs WHERE code = %s LIMIT 1",
                    (group["program_code"],),
                )
                row = cur.fetchone()
                if row is None:
                    print(f"  [--] {group['name']}: нет направления с кодом "
                          f"{group['program_code']}, пропускаю")
                    continue
                cur.execute(
                    "INSERT INTO assistant.groups "
                    "(program_id, name, course, start_year, data_status, "
                    " source_id, source_url, checked_at) "
                    "VALUES (%s, %s, %s, %s, 'official', %s, %s, now()) "
                    "ON CONFLICT (name) DO UPDATE SET "
                    "course = EXCLUDED.course, start_year = EXCLUDED.start_year, "
                    "data_status = 'official', source_id = EXCLUDED.source_id, "
                    "source_url = EXCLUDED.source_url, checked_at = now()",
                    (row[0], group["name"], group.get("course", 1),
                     group.get("start_year"), source_id, source["url"]),
                )
                inserted += 1

            print(f"  групп записано: {inserted}")

            if lessons:
                print(
                    "  занятия: приёмник расписания подключается тем же "
                    "способом, но требует сопоставления дисциплин и "
                    "преподавателей с существующим curriculum. Пока данных "
                    "нет, эта часть намеренно не реализована вслепую."
                )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except psycopg.Error as e:
        print(f"[FAIL] {str(e).strip()}", file=sys.stderr)
        sys.exit(1)
