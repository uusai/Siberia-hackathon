"""Разводит пересечения в расписании точечными UPDATE. Ничего не удаляет.

    python backend/scripts/fix_schedule_conflicts.py            # отчёт, план правок
    python backend/scripts/fix_schedule_conflicts.py --apply    # применить
    python backend/scripts/fix_schedule_conflicts.py --apply --fix-gaps

На момент написания в assistant.schedule было 183 пересечения: 100 по
преподавателям, 48 по аудиториям, 35 по группам. Одна группа не может быть
на двух парах одновременно, один преподаватель — вести две, одна аудитория —
вмещать две группы. Расписание с такими пересечениями нельзя показывать
студенту как рабочее.

КАК ЧИНИТСЯ. Из каждой пересекающейся пары записей остаётся на месте та, у
которой меньше id, вторая переносится в свободный слот. Перенос — это
UPDATE weekday / pair_number / room_id. Разрушающих операций (удаления строк,
очистки таблиц) в этом файле нет физически, это проверяется тестом
backend/tests/test_data_integrity.py.

ЧЁТНОСТЬ НЕДЕЛИ. Слот занят не «в день и пару», а «в день, пару и чётность
недели». Запись с week_type='каждую' занимает обе чётности, 'чётная' —
только чётные. Без этого половина найденных пересечений была бы ложной, а
половина настоящих — пропущена.

ПЕРЕД ЗАПИСЬЮ делается снимок assistant.schedule_backup_<ГГГГММДД>
(CREATE TABLE ... AS SELECT). Вместе с файловой выгрузкой backup_db.py это
две независимые копии.
"""

import datetime as dt
import os
import sys

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common  # noqa: E402

BOTH = ("чётная", "нечётная")

# Пары 1..6 — разумное учебное время. Седьмая (18:45–20:20) остаётся
# запасным вариантом: ставить туда занятие можно, но только если больше
# некуда.
PREFERRED_PAIRS = (1, 2, 3, 4, 5, 6)
LAST_RESORT_PAIRS = (7,)
WEEKDAYS = (1, 2, 3, 4, 5, 6)

# Больше одной свободной пары подряд между занятиями — уже окно.
MAX_GAP = 2


def parities(week_type: str) -> tuple[str, ...]:
    return BOTH if week_type == "каждую" else (week_type,)


class Grid:
    """Занятость групп, преподавателей и аудиторий по слотам."""

    def __init__(self):
        self.group: dict = {}
        self.teacher: dict = {}
        self.room: dict = {}

    def keys(self, row, weekday, pair, room_id):
        for parity in parities(row["week_type"]):
            yield ("group", (row["group_id"], weekday, pair, parity))
            if row["teacher_id"] is not None:
                yield ("teacher", (row["teacher_id"], weekday, pair, parity))
            if room_id is not None:
                yield ("room", (room_id, weekday, pair, parity))

    def free(self, row, weekday, pair, room_id) -> bool:
        for kind, key in self.keys(row, weekday, pair, room_id):
            if getattr(self, kind).get(key) not in (None, row["id"]):
                return False
        return True

    def occupy(self, row, weekday, pair, room_id) -> None:
        for kind, key in self.keys(row, weekday, pair, room_id):
            getattr(self, kind)[key] = row["id"]

    def release(self, row, weekday, pair, room_id) -> None:
        for kind, key in self.keys(row, weekday, pair, room_id):
            getattr(self, kind).pop(key, None)

    def conflicts_with(self, row, weekday, pair, room_id) -> str | None:
        for kind, key in self.keys(row, weekday, pair, room_id):
            holder = getattr(self, kind).get(key)
            if holder not in (None, row["id"]):
                return kind
        return None


def load_rows(cur) -> list[dict]:
    cur.execute(
        "SELECT s.id, s.group_id, s.room_id, s.weekday, s.pair_number, s.week_type, "
        "       s.curriculum_id, c.teacher_id, r.room_type, r.capacity, "
        "       r.building, g.name "
        "FROM assistant.schedule s "
        "JOIN assistant.curriculum c ON c.id = s.curriculum_id "
        "JOIN assistant.groups g ON g.id = s.group_id "
        "LEFT JOIN assistant.rooms r ON r.id = s.room_id "
        "ORDER BY s.id"
    )
    names = [d.name for d in cur.description]
    rows = []
    for values in cur.fetchall():
        row = dict(zip(names, values))
        row["group_name"] = row.pop("name")
        row["pair"] = row.pop("pair_number")
        rows.append(row)
    return rows


def load_rooms(cur) -> list[dict]:
    cur.execute(
        "SELECT id, building, number, capacity, room_type FROM assistant.rooms ORDER BY id"
    )
    names = [d.name for d in cur.description]
    return [dict(zip(names, v)) for v in cur.fetchall()]


def _gap_penalty(row, weekday, pair, day_pairs: dict) -> int:
    """Насколько кандидат-слот рвёт день группы.

    Ставить занятие впритык к уже существующим — хорошо, отдельно стоящая
    пара в конце дня с окном в три пары — плохо. Считаем максимальное окно,
    которое получится в этот день.
    """
    pairs = sorted(day_pairs.get((row["group_id"], weekday), set()) | {pair})
    if len(pairs) < 2:
        return 0
    worst = max(b - a for a, b in zip(pairs, pairs[1:]))
    return max(0, worst - MAX_GAP) * 5


def find_slot(row, grid, rooms, day_pairs, keep_room_first=True, keep_weekday=True):
    """Ищет лучший свободный слот для записи. Возвращает (weekday, pair, room_id).

    keep_weekday=True — переносить только внутри того же дня недели.
    Это не косметическое ограничение: даты в assistant.lesson_occurrences
    вычислены из weekday и week_type, и смена дня недели сделала бы их
    неверными. Удалять их нельзя, а пересчитать «на месте» не всегда
    получится — количество дат для разных дней недели отличается. Внутри дня
    доступно 7 пар на 128 аудиторий, места хватает с запасом.
    """
    room_candidates = []
    if keep_room_first and row["room_id"] is not None:
        room_candidates.append(row["room_id"])
    # Замена аудитории — только на такую же по типу и не меньшую по вместимости,
    # иначе лекционный поток уедет в лабораторию на 12 мест.
    for room in rooms:
        if room["id"] == row["room_id"]:
            continue
        if row["room_type"] is not None and room["room_type"] != row["room_type"]:
            continue
        if row["capacity"] is not None and room["capacity"] < row["capacity"]:
            continue
        room_candidates.append(room["id"])
    if not room_candidates:
        room_candidates = [row["room_id"]]

    weekdays = (row["weekday"],) if keep_weekday else WEEKDAYS

    best = None
    for pair_pool, pool_penalty in ((PREFERRED_PAIRS, 0), (LAST_RESORT_PAIRS, 30)):
        for weekday in weekdays:
            for pair in pair_pool:
                for index, room_id in enumerate(room_candidates):
                    if not grid.free(row, weekday, pair, room_id):
                        continue
                    penalty = pool_penalty
                    penalty += 0 if weekday == row["weekday"] else 8
                    penalty += 0 if index == 0 else 4 + min(index, 5)
                    penalty += abs(pair - row["pair"])
                    penalty += _gap_penalty(row, weekday, pair, day_pairs)
                    if best is None or penalty < best[0]:
                        best = (penalty, weekday, pair, room_id)
        if best is not None:
            break
    return None if best is None else best[1:]


def build_grid(rows):
    """Раскладывает записи по сетке. Возвращает (grid, day_pairs, конфликтующие)."""
    grid = Grid()
    day_pairs: dict = {}
    conflicting = []
    for row in rows:
        kind = grid.conflicts_with(row, row["weekday"], row["pair"], row["room_id"])
        if kind is not None:
            conflicting.append((row, kind))
            continue
        grid.occupy(row, row["weekday"], row["pair"], row["room_id"])
        day_pairs.setdefault((row["group_id"], row["weekday"]), set()).add(row["pair"])
    return grid, day_pairs, conflicting


def reduce_gaps(rows, grid, day_pairs, rooms, moved_ids, limit=200):
    """Второй проход: подтягивает занятия, стоящие за большим окном."""
    moves = []
    for row in rows:
        if len(moves) >= limit:
            break
        pairs = sorted(day_pairs.get((row["group_id"], row["weekday"]), set()))
        if len(pairs) < 2:
            continue
        index = pairs.index(row["pair"]) if row["pair"] in pairs else -1
        if index <= 0 or pairs[index] - pairs[index - 1] <= MAX_GAP:
            continue

        grid.release(row, row["weekday"], row["pair"], row["room_id"])
        day_pairs[(row["group_id"], row["weekday"])].discard(row["pair"])
        target = None
        for pair in range(pairs[index - 1] + 1, row["pair"]):
            if grid.free(row, row["weekday"], pair, row["room_id"]):
                target = (row["weekday"], pair, row["room_id"])
                break
        if target is None:
            grid.occupy(row, row["weekday"], row["pair"], row["room_id"])
            day_pairs[(row["group_id"], row["weekday"])].add(row["pair"])
            continue

        weekday, pair, room_id = target
        grid.occupy(row, weekday, pair, room_id)
        day_pairs.setdefault((row["group_id"], weekday), set()).add(pair)
        moves.append((row, weekday, pair, room_id, "окно"))
        moved_ids.add(row["id"])
        row["weekday"], row["pair"], row["room_id"] = weekday, pair, room_id
    return moves


def _term_dates(cur) -> dict:
    """Даты семестров: {term_id: (starts_on, ends_on, first_week_parity)}."""
    cur.execute(
        "SELECT id, starts_on, ends_on, first_week_parity FROM assistant.academic_terms"
    )
    return {row[0]: row[1:] for row in cur.fetchall()}


def _dates_for(term, weekday: int, week_type: str) -> list[tuple]:
    """Даты семестра для слота. Повторяет логику seed_lesson_occurrences."""
    start, end, first_parity = term
    even = "чётная" if first_parity == "нечётная" else "нечётная"
    week_start = start - dt.timedelta(days=start.isoweekday() - 1)

    result = []
    day = start
    while day <= end:
        if day.isoweekday() == weekday:
            week_no = (day - week_start).days // 7 + 1
            parity = first_parity if week_no % 2 == 1 else even
            if week_type == "каждую" or week_type == parity:
                result.append((day, week_no))
        day += dt.timedelta(days=1)
    return result


def resync_occurrences(cur, moved_ids: set[int]) -> tuple[int, int, int]:
    """Пересчитывает даты занятий для перенесённых записей расписания.

    Строки НЕ удаляются. Порядок действий:
      1) существующие проведения уводятся на служебные даты 1900 года —
         иначе UPDATE на новую дату сталкивается с UNIQUE(schedule_id,
         lesson_date) по строке, которую ещё не успели переписать;
      2) сколько дат совпало по количеству — столько строк переписывается
         на новые даты;
      3) лишние помечаются cancelled_at (см. 010_lesson_cancellation.sql);
      4) недостающие досоздаются INSERT'ом.
    """
    if not moved_ids:
        return 0, 0, 0

    terms = _term_dates(cur)
    cur.execute(
        "SELECT id, weekday, week_type, term_id FROM assistant.schedule "
        "WHERE id = ANY(%s) ORDER BY id",
        (sorted(moved_ids),),
    )
    schedules = cur.fetchall()

    updated = cancelled = inserted = 0
    parking = dt.date(1900, 1, 1)

    for schedule_id, weekday, week_type, term_id in schedules:
        if term_id is None or term_id not in terms:
            continue
        cur.execute(
            "SELECT id FROM assistant.lesson_occurrences "
            "WHERE schedule_id = %s AND cancelled_at IS NULL ORDER BY lesson_date",
            (schedule_id,),
        )
        existing = [row[0] for row in cur.fetchall()]
        if not existing:
            continue

        # 1) уводим на служебные даты
        for offset, occurrence_id in enumerate(existing):
            cur.execute(
                "UPDATE assistant.lesson_occurrences SET lesson_date = %s "
                "WHERE id = %s",
                (parking + dt.timedelta(days=offset), occurrence_id),
            )

        wanted = _dates_for(terms[term_id], weekday, week_type)

        # 2) переписываем на новые даты
        for occurrence_id, (day, week_no) in zip(existing, wanted):
            cur.execute(
                "UPDATE assistant.lesson_occurrences "
                "SET lesson_date = %s, week_no = %s WHERE id = %s",
                (day, week_no, occurrence_id),
            )
            updated += 1

        # 3) лишние — отменяем, оставляя строку в истории
        for offset, occurrence_id in enumerate(existing[len(wanted):]):
            cur.execute(
                "UPDATE assistant.lesson_occurrences "
                "SET cancelled_at = now(), cancel_reason = %s, lesson_date = %s "
                "WHERE id = %s",
                ("занятие перенесено на другой день недели",
                 parking + dt.timedelta(days=900 + offset), occurrence_id),
            )
            cancelled += 1

        # 4) недостающие — досоздаём
        for day, week_no in wanted[len(existing):]:
            cur.execute(
                "INSERT INTO assistant.lesson_occurrences "
                "(schedule_id, term_id, lesson_date, week_no) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (schedule_id, lesson_date) DO NOTHING",
                (schedule_id, term_id, day, week_no),
            )
            inserted += 1

    return updated, cancelled, inserted


def main(argv: list[str]) -> int:
    apply = _common.parse_apply_flag(argv)
    fix_gaps = "--fix-gaps" in argv
    # --move-across-days снимает ограничение «тот же день недели». Включать
    # только вместе с последующим пересчётом lesson_occurrences.
    keep_weekday = "--move-across-days" not in argv
    _common.banner("Разведение конфликтов расписания", apply)
    print(f"\nПеренос {'только внутри дня недели' if keep_weekday else 'по всем дням'}")

    with _common.connect() as conn:
        with conn.cursor() as cur:
            rows = load_rows(cur)
            rooms = load_rooms(cur)
            print(f"\nЗаписей расписания: {len(rows)}, аудиторий: {len(rooms)}")

            grid, day_pairs, conflicting = build_grid(rows)
            by_kind: dict[str, int] = {}
            for _, kind in conflicting:
                by_kind[kind] = by_kind.get(kind, 0) + 1
            print(f"Записей, стоящих в занятом слоте: {len(conflicting)}")
            for kind, count in sorted(by_kind.items()):
                print(f"  по ресурсу '{kind}': {count}")

            moves = []
            failed = []
            moved_ids: set[int] = set()
            for row, kind in conflicting:
                slot = find_slot(row, grid, rooms, day_pairs, keep_weekday=keep_weekday)
                if slot is None:
                    failed.append(row)
                    # Слота нет — оставляем запись как есть и занимаем её
                    # исходное место, чтобы дальнейший поиск это учитывал.
                    grid.occupy(row, row["weekday"], row["pair"], row["room_id"])
                    continue
                weekday, pair, room_id = slot
                grid.occupy(row, weekday, pair, room_id)
                day_pairs.setdefault((row["group_id"], weekday), set()).add(pair)
                moves.append((row, weekday, pair, room_id, kind))
                moved_ids.add(row["id"])
                row["weekday"], row["pair"], row["room_id"] = weekday, pair, room_id

            if fix_gaps:
                gap_moves = reduce_gaps(rows, grid, day_pairs, rooms, moved_ids)
                print(f"Переносов ради устранения окон: {len(gap_moves)}")
                moves.extend(gap_moves)

            print(f"\nПереносов запланировано: {len(moves)}")
            if failed:
                print(f"Не удалось разместить: {len(failed)} — слотов не нашлось",
                      file=sys.stderr)
            for row, weekday, pair, room_id, kind in moves[:15]:
                print(f"  id={row['id']:<5} {row['group_name']:<14} "
                      f"-> день {weekday}, пара {pair}, аудитория {room_id} ({kind})")
            if len(moves) > 15:
                print(f"  ... и ещё {len(moves) - 15}")

            if not apply:
                print("\nЗапись выключена. Добавьте --apply.")
                return 0
            if not moves:
                print("\nПереносить нечего.")
                return 0

            stamp = dt.datetime.now().strftime("%Y%m%d")
            backup = f"schedule_backup_{stamp}"
            cur.execute(
                f'CREATE TABLE IF NOT EXISTS assistant."{backup}" AS '
                f"SELECT * FROM assistant.schedule"
            )
            cur.execute(f'SELECT count(*) FROM assistant."{backup}"')
            print(f"\nСнимок assistant.{backup}: {cur.fetchone()[0]} строк")

            cur.executemany(
                "UPDATE assistant.schedule "
                "SET weekday = %s, pair_number = %s, room_id = %s WHERE id = %s",
                [(w, p, r, row["id"]) for row, w, p, r, _ in moves],
            )
            print(f"Обновлено записей: {cur.rowcount if cur.rowcount != -1 else len(moves)}")

            # Даты занятий выведены из дня недели, поэтому после переноса их
            # надо пересчитать. Строки при этом не удаляются: лишние
            # помечаются отменёнными.
            updated, cancelled, inserted = resync_occurrences(cur, moved_ids)
            if updated or cancelled or inserted:
                print(f"\nПересчёт дат занятий: обновлено {updated}, "
                      f"отменено {cancelled}, добавлено {inserted}")

            for view in ("schedule_conflicts_group", "schedule_conflicts_teacher",
                         "schedule_conflicts_room"):
                cur.execute(f"SELECT count(*) FROM assistant.{view}")
                print(f"  {view}: {cur.fetchone()[0]}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except psycopg.Error as e:
        print(f"[FAIL] {str(e).strip()}", file=sys.stderr)
        sys.exit(1)
