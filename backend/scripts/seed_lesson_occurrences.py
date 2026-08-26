"""Разворачивает недельную сетку расписания в конкретные даты занятий.

    python backend/scripts/seed_lesson_occurrences.py            # показать план
    python backend/scripts/seed_lesson_occurrences.py --apply    # записать

Что делает:
  1. заводит учебные семестры (assistant.academic_terms), если их ещё нет;
  2. заполняет расписание звонков (assistant.pair_times);
  3. проставляет schedule.lesson_type и schedule.term_id, где они пусты;
  4. раскладывает каждую запись assistant.schedule по датам её семестра
     в assistant.lesson_occurrences.

Чего НЕ делает: ничего не удаляет и не переписывает уже заполненные поля.
Повторный запуск не создаёт дублей — всё через ON CONFLICT DO NOTHING и
UPDATE ... WHERE <поле> IS NULL.

РАСПРЕДЕЛЕНИЕ ПО СЕМЕСТРАМ. У записи расписания своего семестра нет, он есть
у учебного плана: curriculum.semester. Нечётные семестры — осенние, чётные —
весенние, поэтому занятие попадает ровно в один семестр, а не дублируется в
оба. Это заодно вдвое сокращает объём таблицы.

ЧЁТНОСТЬ НЕДЕЛИ. Неделя считается от понедельника недели, в которую попадает
начало семестра, а не от 1 января: «чётная неделя» в расписании вуза — это
номер недели ОТ НАЧАЛА СЕМЕСТРА. Стартовая чётность задаётся в
academic_terms.first_week_parity.
"""

import datetime as dt
import os
import sys

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common  # noqa: E402

# Демонстрационные семестры: официальный календарь ИГУ автоматически получить
# не удалось, поэтому даты помечены data_status='demo' и заменяются импортёром
# без удаления чего-либо (backend/scripts/import_official_schedule.py).
TERMS = [
    {
        "academic_year": "2026/2027",
        "name": "осенний",
        "starts_on": dt.date(2026, 9, 1),
        "ends_on": dt.date(2026, 12, 27),
        "first_week_parity": "нечётная",
        "semester_parity": 1,   # нечётные семестры учебного плана
    },
    {
        "academic_year": "2026/2027",
        "name": "весенний",
        "starts_on": dt.date(2027, 2, 8),
        "ends_on": dt.date(2027, 5, 30),
        "first_week_parity": "нечётная",
        "semester_parity": 0,   # чётные семестры
    },
]

# Сетка звонков: 7 пар, как в существующем расписании (pair_number 1..7).
PAIR_TIMES = [
    (1, dt.time(8, 0), dt.time(9, 35)),
    (2, dt.time(9, 45), dt.time(11, 20)),
    (3, dt.time(11, 40), dt.time(13, 15)),
    (4, dt.time(13, 30), dt.time(15, 5)),
    (5, dt.time(15, 15), dt.time(16, 50)),
    (6, dt.time(17, 0), dt.time(18, 35)),
    (7, dt.time(18, 45), dt.time(20, 20)),
]


def _lesson_type_for(room_type: str | None, subject_name: str) -> str:
    """Тип занятия выводим из типа аудитории — другого признака в базе нет.

    Не угадываем по названию дисциплины: «Физика» бывает и лекцией, и
    лабораторной, а вот лаборатория лекцией не бывает.
    """
    rt = (room_type or "").lower()
    if "лаборат" in rt or "компьютер" in rt:
        return "лабораторная"
    if "лекц" in rt or "поточ" in rt:
        return "лекция"
    if "семинар" in rt:
        return "семинар"
    return "практика"


def _parity_of(week_no: int, first_week_parity: str) -> str:
    """Чётность недели с номером week_no (нумерация с 1)."""
    odd_is = first_week_parity                     # чётность первой недели
    even_is = "чётная" if odd_is == "нечётная" else "нечётная"
    return odd_is if week_no % 2 == 1 else even_is


_dates_cache: dict = {}


def _dates_for(term: dict, weekday: int, week_type: str) -> list[tuple[dt.date, int]]:
    """Все даты семестра, попадающие в слот (день недели + чётность).

    Результат кешируется: различных слотов всего 6 дней × 3 типа недели на
    семестр, а записей расписания около тысячи — без кеша один и тот же
    календарь пересчитывался бы сотни раз.
    """
    cache_key = (term["academic_year"], term["name"], weekday, week_type)
    cached = _dates_cache.get(cache_key)
    if cached is not None:
        return cached

    start, end = term["starts_on"], term["ends_on"]
    # Понедельник недели, в которую попадает начало семестра.
    week_start = start - dt.timedelta(days=start.isoweekday() - 1)

    result = []
    day = start
    while day <= end:
        if day.isoweekday() == weekday:
            week_no = (day - week_start).days // 7 + 1
            if week_type == "каждую" or week_type == _parity_of(
                week_no, term["first_week_parity"]
            ):
                result.append((day, week_no))
        day += dt.timedelta(days=1)

    _dates_cache[cache_key] = result
    return result


def _ensure_terms(cur, apply: bool) -> dict[int, dict]:
    """Заводит семестры, возвращает {semester_parity: {...term..., 'id': N}}."""
    by_parity: dict[int, dict] = {}
    for term in TERMS:
        cur.execute(
            "SELECT id FROM assistant.academic_terms "
            "WHERE academic_year = %s AND name = %s",
            (term["academic_year"], term["name"]),
        )
        row = cur.fetchone()
        if row:
            term_id = row[0]
            print(f"  семестр {term['academic_year']} {term['name']}: уже есть (id={term_id})")
        elif apply:
            cur.execute(
                "INSERT INTO assistant.academic_terms "
                "(academic_year, name, starts_on, ends_on, first_week_parity, data_status) "
                "VALUES (%s, %s, %s, %s, %s, 'demo') "
                "ON CONFLICT (academic_year, name) DO NOTHING RETURNING id",
                (term["academic_year"], term["name"], term["starts_on"],
                 term["ends_on"], term["first_week_parity"]),
            )
            row = cur.fetchone()
            if row is None:
                # Гонка: кто-то создал семестр между SELECT и INSERT.
                cur.execute(
                    "SELECT id FROM assistant.academic_terms "
                    "WHERE academic_year = %s AND name = %s",
                    (term["academic_year"], term["name"]),
                )
                row = cur.fetchone()
            term_id = row[0]
            print(f"  семестр {term['academic_year']} {term['name']}: создан (id={term_id})")
        else:
            term_id = None
            print(f"  семестр {term['academic_year']} {term['name']}: будет создан")

        by_parity[term["semester_parity"]] = {**term, "id": term_id}
    return by_parity


def _ensure_pair_times(cur, apply: bool) -> None:
    if not apply:
        print(f"  расписание звонков: будет заполнено ({len(PAIR_TIMES)} пар)")
        return
    cur.executemany(
        "INSERT INTO assistant.pair_times (pair_number, starts_at, ends_at, data_status) "
        "VALUES (%s, %s, %s, 'demo') ON CONFLICT (pair_number) DO NOTHING",
        PAIR_TIMES,
    )
    cur.execute("SELECT count(*) FROM assistant.pair_times")
    print(f"  расписание звонков: {cur.fetchone()[0]} пар в таблице")


def main(argv: list[str]) -> int:
    apply = _common.parse_apply_flag(argv)
    _common.banner("Разворот расписания по датам", apply)

    with _common.connect() as conn:
        with conn.cursor() as cur:
            print("\nСправочники:")
            terms = _ensure_terms(cur, apply)
            _ensure_pair_times(cur, apply)

            # Все записи расписания вместе с семестром учебного плана и типом
            # аудитории — одним запросом, чтобы не дёргать БД построчно.
            cur.execute(
                "SELECT s.id, s.weekday, s.pair_number, s.week_type, "
                "       c.semester, r.room_type, sub.name, s.lesson_type, s.term_id "
                "FROM assistant.schedule s "
                "JOIN assistant.curriculum c ON c.id = s.curriculum_id "
                "JOIN assistant.subjects sub ON sub.id = c.subject_id "
                "LEFT JOIN assistant.rooms r ON r.id = s.room_id "
                "ORDER BY s.id"
            )
            rows = cur.fetchall()
            print(f"\nЗаписей расписания: {len(rows)}")

            occurrences: list[tuple] = []
            type_updates: list[tuple] = []
            term_updates: list[tuple] = []
            per_term: dict[str, int] = {}

            for (sid, weekday, pair, week_type, semester, room_type,
                 subject, lesson_type, cur_term_id) in rows:
                term = terms[semester % 2]
                if term["id"] is None and apply:
                    print("Семестр не создан — прерываю.", file=sys.stderr)
                    return 1

                if lesson_type is None:
                    type_updates.append((_lesson_type_for(room_type, subject), sid))
                if cur_term_id is None and term["id"] is not None:
                    term_updates.append((term["id"], sid))

                key = f"{term['academic_year']} {term['name']}"
                for day, week_no in _dates_for(term, weekday, week_type):
                    if term["id"] is not None:
                        occurrences.append((sid, term["id"], day, week_no))
                    per_term[key] = per_term.get(key, 0) + 1

            print("Занятий к разворачиванию:")
            for key, count in sorted(per_term.items()):
                print(f"  {key}: {count}")
            print(f"  проставить тип занятия: {len(type_updates)}")
            print(f"  проставить семестр: {len(term_updates)}")

            if not apply:
                print("\nЗапись выключена. Добавьте --apply.")
                return 0

            # lesson_type и term_id проставляем ТОЛЬКО там, где их ещё нет:
            # уже заполненное значение может прийти из официального импорта,
            # и затирать его вычисленным нельзя.
            if type_updates:
                cur.executemany(
                    "UPDATE assistant.schedule SET lesson_type = %s "
                    "WHERE id = %s AND lesson_type IS NULL",
                    type_updates,
                )
            if term_updates:
                cur.executemany(
                    "UPDATE assistant.schedule SET term_id = %s "
                    "WHERE id = %s AND term_id IS NULL",
                    term_updates,
                )

            cur.executemany(
                "INSERT INTO assistant.lesson_occurrences "
                "(schedule_id, term_id, lesson_date, week_no) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (schedule_id, lesson_date) DO NOTHING",
                occurrences,
            )

            cur.execute("SELECT count(*) FROM assistant.lesson_occurrences")
            total = cur.fetchone()[0]
            cur.execute(
                "SELECT min(lesson_date), max(lesson_date) "
                "FROM assistant.lesson_occurrences"
            )
            first, last = cur.fetchone()

    print(f"\n[OK] lesson_occurrences: {total} строк, период {first} — {last}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except psycopg.Error as e:
        print(f"[FAIL] {str(e).strip()}", file=sys.stderr)
        sys.exit(1)
