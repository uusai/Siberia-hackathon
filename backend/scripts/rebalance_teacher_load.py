"""Разгружает перегруженных преподавателей, передавая курсы коллегам.

    python backend/scripts/rebalance_teacher_load.py            # отчёт
    python backend/scripts/rebalance_teacher_load.py --apply    # применить

ЗАЧЕМ. В демо-контуре нагрузка распределена случайно, и у части
преподавателей она физически невозможна: у одного 63 занятия в неделю при
том, что в неделе 6 дней по 7 пар. Пока это так, бесконфликтного расписания
не существует в принципе — никакой перестановкой пар двух одновременных
занятий одного человека не развести.

ЧТО ДЕЛАЕТ. Находит преподавателей выше порога нагрузки и передаёт часть их
курсов (строк assistant.curriculum) наименее загруженным коллегам ТОЙ ЖЕ
кафедры: физику не должен читать юрист. Меняется только curriculum.teacher_id
— строки не удаляются, дисциплины и расписание остаются на месте.

ВЕС СЛОТА. Занятие «каждую неделю» занимает и чётную, и нечётную неделю,
поэтому весит 2, а занятие по одной чётности — 1. Потолок 6 дней x 7 пар x
2 чётности = 84. Порог по умолчанию заметно ниже потолка: расписание, где
преподаватель занят каждый слот недели, формально бесконфликтно, но
бессмысленно.
"""

import os
import sys

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common  # noqa: E402

# Потолок физический — 6 дней x 7 пар x 2 чётности = 84 слота.
#
# Порог 64 выбран не «на глаз», а из фактической ёмкости факультетов:
# физико-математический несёт суммарный вес 905 на 16 преподавателей, то есть
# в среднем 56.6 на человека. Любой порог ниже этого недостижим, не перенося
# дисциплины на другой факультет, — а физику юристу не отдашь. 64 оставляет
# запас над средним и при этом заметно ниже физического потолка, так что
# развести пересечения перестановкой пар становится возможно.
MAX_SLOT_WEIGHT = 64


def _load(cur) -> dict[int, int]:
    cur.execute(
        "SELECT c.teacher_id, "
        "       COALESCE(SUM(CASE WHEN s.week_type = 'каждую' THEN 2 ELSE 1 END), 0) "
        "FROM assistant.curriculum c "
        "LEFT JOIN assistant.schedule s ON s.curriculum_id = c.id "
        "GROUP BY c.teacher_id"
    )
    return {teacher_id: int(weight) for teacher_id, weight in cur.fetchall()}


def main(argv: list[str]) -> int:
    apply = _common.parse_apply_flag(argv)
    _common.banner("Разгрузка преподавателей", apply)

    with _common.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.department_id, d.faculty_id, t.full_name "
                "FROM assistant.teachers t "
                "JOIN assistant.departments d ON d.id = t.department_id"
            )
            teachers = {tid: (dept, faculty, name)
                        for tid, dept, faculty, name in cur.fetchall()}
            by_department: dict[int, list[int]] = {}
            by_faculty: dict[int, list[int]] = {}
            for tid, (dept, faculty, _) in teachers.items():
                by_department.setdefault(dept, []).append(tid)
                by_faculty.setdefault(faculty, []).append(tid)

            weight = _load(cur)
            for tid in teachers:
                weight.setdefault(tid, 0)

            # Вес каждого курса: сколько слотов недели он занимает.
            cur.execute(
                "SELECT c.id, c.teacher_id, "
                "       COALESCE(SUM(CASE WHEN s.week_type = 'каждую' THEN 2 ELSE 1 END), 0) "
                "FROM assistant.curriculum c "
                "LEFT JOIN assistant.schedule s ON s.curriculum_id = c.id "
                "GROUP BY c.id, c.teacher_id"
            )
            courses = [(cid, tid, int(w)) for cid, tid, w in cur.fetchall()]
            by_teacher: dict[int, list[tuple[int, int]]] = {}
            for course_id, teacher_id, course_weight in courses:
                by_teacher.setdefault(teacher_id, []).append((course_id, course_weight))

            overloaded = sorted(
                (tid for tid, w in weight.items() if w > MAX_SLOT_WEIGHT),
                key=lambda tid: -weight[tid],
            )
            print(f"\nПорог нагрузки: {MAX_SLOT_WEIGHT} слотов в неделю")
            print(f"Перегружены: {len(overloaded)} преподавателей")
            for tid in overloaded:
                print(f"  {teachers[tid][2][:34]:<36} вес {weight[tid]}")

            # Итеративная разгрузка: на каждом шаге берём самого загруженного
            # и передаём один его курс наименее загруженному коллеге. Один
            # проход «сверху вниз» не работает — освободившееся место у
            # коллеги надо учитывать сразу, а перегруженных больше, чем
            # свободных мест на отдельно взятой кафедре.
            moves = []
            stuck: set[int] = set()
            while True:
                candidates = [t for t in weight
                              if weight[t] > MAX_SLOT_WEIGHT and t not in stuck
                              and t in teachers]
                if not candidates:
                    break
                tid = max(candidates, key=lambda t: weight[t])
                dept, faculty, _ = teachers[tid]

                # Сначала своя кафедра, затем — факультет. Дальше не идём:
                # передавать курс физики юристу нельзя ни при какой нагрузке.
                colleagues = [c for c in by_department.get(dept, []) if c != tid]
                if all(weight[c] + 1 > MAX_SLOT_WEIGHT for c in colleagues):
                    colleagues = [c for c in by_faculty.get(faculty, []) if c != tid]

                moved = False
                # Лёгкие курсы отдаём первыми: так проще попасть в остаток
                # свободного места, не создавая перегрузку у коллеги.
                for course_id, course_weight in sorted(
                    by_teacher.get(tid, []), key=lambda x: x[1]
                ):
                    fits = [c for c in colleagues
                            if weight[c] + course_weight <= MAX_SLOT_WEIGHT]
                    if not fits:
                        continue
                    target = min(fits, key=lambda c: weight[c])
                    weight[tid] -= course_weight
                    weight[target] += course_weight
                    by_teacher[tid].remove((course_id, course_weight))
                    by_teacher.setdefault(target, []).append((course_id, course_weight))
                    moves.append((target, course_id, teachers[tid][2],
                                  teachers[target][2], course_weight))
                    moved = True
                    break
                if not moved:
                    stuck.add(tid)

            print(f"\nПередач курсов: {len(moves)}")
            for target, course_id, source_name, target_name, course_weight in moves[:12]:
                print(f"  curriculum={course_id:<4} {source_name[:26]:<28} -> "
                      f"{target_name[:26]:<28} вес {course_weight}")
            if len(moves) > 12:
                print(f"  ... и ещё {len(moves) - 12}")

            still = [tid for tid in weight if weight[tid] > MAX_SLOT_WEIGHT]
            if still:
                print(f"\nОстались выше порога: {len(still)}")

            if not apply or not moves:
                if not apply:
                    print("\nЗапись выключена. Добавьте --apply.")
                return 0

            cur.executemany(
                "UPDATE assistant.curriculum SET teacher_id = %s WHERE id = %s",
                [(target, course_id) for target, course_id, _, _, _ in moves],
            )
            print(f"\n[OK] Обновлено строк curriculum: {len(moves)}")

            after = _load(cur)
            worst = max(after.values()) if after else 0
            print(f"Максимальная нагрузка после разгрузки: {worst} слотов")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except psycopg.Error as e:
        print(f"[FAIL] {str(e).strip()}", file=sys.stderr)
        sys.exit(1)
